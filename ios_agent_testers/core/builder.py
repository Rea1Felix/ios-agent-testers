"""iOS Simulator app framework detection and unsigned arm64 builder.

Ports mobile-agent-testers' `scripts/build/{detect.sh, args.sh, ios.sh}` cleanly
to Python using stdlib only.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Any

DERIVED_DATA_SENTINEL = ".ios-agent-testers-dd-cache"
DERIVED_DATA_LAST_USED = ".last_used"
DEFAULT_DERIVED_DATA_KEEP = 5

_SKIP_DIRS = frozenset(
    {
        "Pods",
        ".git",
        ".build",
        "DerivedData",
        "node_modules",
        ".dart_tool",
        "build",
    }
)

_KMP_RE = re.compile(r'multiplatform|kotlin\("multiplatform"\)|kotlinMultiplatform')
_MAUI_TFM_RE = re.compile(r"net[0-9.]+-ios")


def detect_framework(project_path: str | Path) -> str:
    """Detect mobile project framework in exact upstream precedence order.

    Precedence: flutter -> capacitor -> expo -> react-native -> maui -> kmp -> native.
    """
    root = Path(project_path)
    if not root.exists():
        raise FileNotFoundError(f"Project path does not exist: {root}")

    if (root / "pubspec.yaml").is_file():
        return "flutter"

    if any(root.glob("capacitor.config.*")):
        return "capacitor"

    pkg_json = root / "package.json"
    if pkg_json.is_file():
        pkg_text = pkg_json.read_text(encoding="utf-8", errors="replace")
        has_expo = '"expo"' in pkg_text
        has_rn = '"react-native"' in pkg_text
        if has_expo and not (root / "android").is_dir() and not (root / "ios").is_dir():
            return "expo"
        if has_rn:
            return "react-native"

    # Check root and immediate child dirs for .csproj with UseMaui
    csproj_candidates = list(root.glob("*.csproj")) + list(root.glob("*/*.csproj"))
    for csproj in csproj_candidates:
        if csproj.is_file():
            text = csproj.read_text(encoding="utf-8", errors="replace")
            if "UseMaui" in text:
                return "maui"

    # Check Kotlin Multiplatform gradle build scripts
    gradle_candidates: list[Path] = []
    for name in ("build.gradle.kts", "settings.gradle.kts"):
        p = root / name
        if p.is_file():
            gradle_candidates.append(p)
    gradle_candidates.extend(p for p in root.glob("*/build.gradle.kts") if p.is_file())
    for gfile in gradle_candidates:
        text = gfile.read_text(encoding="utf-8", errors="replace")
        if _KMP_RE.search(text):
            return "kmp"

    return "native"


def parse_build_args(
    build_args: str | None,
    secret_args: str | None = None,
) -> list[str]:
    """Split shell-style build args safely via `shlex.split` without leaking secrets."""
    parsed: list[str] = []
    if build_args and build_args.strip():
        try:
            parsed.extend(shlex.split(build_args))
        except ValueError as exc:
            raise ValueError(f"Invalid build_args syntax: {exc}") from exc

    if secret_args and secret_args.strip():
        try:
            parsed.extend(shlex.split(secret_args))
        except ValueError as exc:
            raise ValueError(
                "Invalid secret_args syntax (unbalanced quotes or escape sequence)"
            ) from None

    return parsed


async def _run_cmd(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 900.0,
) -> tuple[int, str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        env=merged_env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode("utf-8", errors="replace")
        return proc.returncode or 0, output
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        raise TimeoutError(f"Build command timed out after {timeout}s: {' '.join(args)}")


def _tail_lines(text: str, count: int = 40) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-count:])


def _find_xcode_container(search_dir: Path, max_depth: int = 2) -> tuple[str, Path]:
    """Find `.xcworkspace` (preferred) or `.xcodeproj` up to `max_depth`, skipping Pods."""
    workspaces: list[Path] = []
    projects: list[Path] = []

    def scan(current: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for entry in entries:
            name = entry.name
            if name in _SKIP_DIRS:
                continue
            if name.endswith(".xcworkspace") and entry.is_dir():
                workspaces.append(entry)
                continue
            if name.endswith(".xcodeproj") and entry.is_dir():
                projects.append(entry)
                # Do NOT descend inside .xcodeproj (it contains project.xcworkspace)
                continue
            if entry.is_dir() and depth < max_depth:
                scan(entry, depth + 1)

    scan(search_dir, 0)

    if workspaces:
        workspaces.sort(key=lambda p: (len(p.relative_to(search_dir).parts), p.name))
        return ("-workspace", workspaces[0])
    if projects:
        projects.sort(key=lambda p: (len(p.relative_to(search_dir).parts), p.name))
        return ("-project", projects[0])
    raise FileNotFoundError(
        f"No .xcworkspace or .xcodeproj found under {search_dir} (max depth {max_depth})"
    )


async def _resolve_scheme(
    flag: str,
    container: Path,
    requested_scheme: str | None,
) -> str:
    if requested_scheme and requested_scheme.strip():
        return requested_scheme.strip()

    rc, output = await _run_cmd(
        ["xcodebuild", flag, str(container), "-list", "-json"],
        cwd=container.parent,
        timeout=60.0,
    )
    if rc != 0:
        raise RuntimeError(
            f"Failed to list Xcode schemes for {container.name}:\n{_tail_lines(output)}"
        )

    json_start = output.find("{")
    if json_start < 0:
        raise RuntimeError(f"Unexpected xcodebuild -list output: {output[:300]}")

    data = json.loads(output[json_start:])
    section = data.get("workspace") or data.get("project") or {}
    schemes = [str(s) for s in (section.get("schemes") or []) if s]
    if not schemes:
        raise RuntimeError(f"No build schemes found in {container.name}")

    stem = container.stem
    if stem in schemes:
        return stem
    for s in schemes:
        low = s.lower()
        if "pods" not in low and not low.endswith("tests") and not low.endswith("uitests"):
            return s
    return schemes[0]


def _find_built_app(
    search_roots: list[Path],
    *,
    scheme: str | None = None,
) -> Path:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in search_roots:
        if not root.exists():
            continue
        for app in root.rglob("*.app"):
            if not app.is_dir():
                continue
            resolved = app.resolve()
            if resolved in seen:
                continue
            rel_parents = app.relative_to(root).parent.parts
            rel_parents_low = [p.lower() for p in rel_parents]
            # Never pick embedded WatchKit/AppClip/PlugIn bundles nested inside another .app
            if any(
                p.endswith((".app", ".appex", ".framework", ".xcarchive"))
                or p == "intermediates.noindex"
                for p in rel_parents_low
            ):
                continue
            name_low = app.name.lower()
            if "tests.app" in name_low or "uitests.app" in name_low:
                continue
            parent_low = str(app.parent).lower()
            if "iphonesimulator" in parent_low or "iossimulator" in parent_low:
                seen.add(resolved)
                candidates.append(app)

    if not candidates:
        raise FileNotFoundError(
            f"No built iOS Simulator .app bundle found under: {[str(r) for r in search_roots]}"
        )

    scheme_low = (scheme or "").strip().lower()

    def _rank(p: Path) -> tuple[int, int, float]:
        parent_name_low = p.parent.name.lower()
        direct_sim = int(
            "iphonesimulator" in parent_name_low or "iossimulator" in parent_name_low
        )
        stem_low = p.stem.lower()
        if scheme_low and stem_low == scheme_low:
            scheme_score = 2
        elif scheme_low and scheme_low in stem_low:
            scheme_score = 1
        else:
            scheme_score = 0
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (direct_sim, scheme_score, mtime)

    candidates.sort(key=_rank, reverse=True)
    return candidates[0]


async def _maybe_pod_install(pod_dir: Path, logs: list[str]) -> None:
    if not (pod_dir / "Podfile").is_file():
        return
    if not shutil.which("pod"):
        return
    rc, out = await _run_cmd(["pod", "install"], cwd=pod_dir, timeout=600.0)
    logs.append(out)
    if rc != 0:
        raise RuntimeError(f"pod install failed in {pod_dir}:\n{_tail_lines(out)}")


async def _maybe_npm_install(root: Path, logs: list[str]) -> None:
    if (root / "node_modules").is_dir():
        return
    cmd = ["npm", "ci"] if (root / "package-lock.json").is_file() else ["npm", "install"]
    rc, out = await _run_cmd(cmd, cwd=root, timeout=600.0)
    logs.append(out)
    if rc != 0 and cmd == ["npm", "ci"]:
        rc, out = await _run_cmd(["npm", "install"], cwd=root, timeout=600.0)
        logs.append(out)
    if rc != 0:
        raise RuntimeError(f"npm install failed in {root}:\n{_tail_lines(out)}")


def get_derived_data_cache_root(cache_root: str | Path | None = None) -> Path:
    """Resolve the managed DerivedData cache root directory."""
    if cache_root is not None:
        return Path(cache_root).expanduser().resolve()
    env_override = (os.environ.get("IOS_AGENT_TESTERS_DERIVED_DATA_ROOT") or "").strip()
    if env_override:
        return Path(env_override).expanduser().resolve()
    return (
        Path.home() / "Library" / "Caches" / "ios-agent-testers" / "DerivedData"
    ).resolve()


def resolve_managed_derived_data_dir(
    project_root: str | Path,
    scheme: str,
    configuration: str,
    *,
    cache_root: str | Path | None = None,
) -> Path:
    """Compute a deterministic managed DerivedData cache directory for a build target."""
    resolved_root = Path(project_root).resolve()
    clean_scheme = str(scheme or "").strip()
    clean_config = str(configuration or "Debug").strip()
    key = "\0".join((str(resolved_root), clean_scheme, clean_config))
    hash12 = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    raw_slug = re.sub(
        r"[^A-Za-z0-9_-]+", "-", f"{resolved_root.name}-{clean_scheme}"
    ).strip("-").lower()
    slug = raw_slug[:36] or "app"
    return get_derived_data_cache_root(cache_root) / f"{slug}-{hash12}"


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            with contextlib.suppress(OSError):
                total += fpath.lstat().st_size
    return total


def _mark_managed_derived_data(dd: Path) -> None:
    dd.mkdir(parents=True, exist_ok=True)
    sentinel = dd / DERIVED_DATA_SENTINEL
    if not sentinel.is_file():
        sentinel.write_text("ios-agent-testers derived data cache\n", encoding="utf-8")
    last_used = dd / DERIVED_DATA_LAST_USED
    last_used.write_text(f"{time.time():.6f}\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.utime(last_used, None)


def prune_derived_data_cache(
    cache_root: str | Path | None = None,
    *,
    keep: int = DEFAULT_DERIVED_DATA_KEEP,
    exclude: str | Path | None = None,
) -> dict[str, Any]:
    """Prune old sentinel-marked DerivedData cache directories by `.last_used` LRU recency.

    Strictly skips any directory lacking the `.ios-agent-testers-dd-cache` sentinel file.
    """
    root = get_derived_data_cache_root(cache_root)
    keep_n = max(0, int(keep))
    exclude_resolved = Path(exclude).expanduser().resolve() if exclude is not None else None

    if not root.is_dir():
        return {"removed": [], "kept": [], "freed_bytes": 0}

    marked: list[tuple[float, str, Path]] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return {"removed": [], "kept": [], "freed_bytes": 0}

    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        sentinel = entry / DERIVED_DATA_SENTINEL
        if not sentinel.is_file():
            continue
        last_used = entry / DERIVED_DATA_LAST_USED
        mtime = 0.0
        for candidate in (last_used, sentinel, entry):
            try:
                mtime = candidate.stat().st_mtime
                break
            except OSError:
                continue
        marked.append((mtime, entry.name, entry))

    marked.sort(key=lambda item: (item[0], item[1]), reverse=True)

    kept_paths: list[Path] = []
    to_remove: list[Path] = []
    for _, _, entry in marked:
        is_excluded = (
            exclude_resolved is not None and entry.resolve() == exclude_resolved
        )
        if is_excluded or len(kept_paths) < keep_n:
            kept_paths.append(entry)
        else:
            to_remove.append(entry)

    removed: list[str] = []
    freed_bytes = 0
    for entry in to_remove:
        size = _dir_size_bytes(entry)
        try:
            shutil.rmtree(entry)
            removed.append(str(entry))
            freed_bytes += size
        except OSError:
            kept_paths.append(entry)

    return {
        "removed": removed,
        "kept": [str(p) for p in kept_paths],
        "freed_bytes": freed_bytes,
    }


async def _xcbuild(
    search_dir: Path,
    *,
    project_root: Path,
    scheme: str | None,
    configuration: str,
    extra_args: list[str],
    derived_data: Path | None,
    logs: list[str],
    clean: bool = False,
    max_depth: int = 2,
) -> tuple[Path, str, Path]:
    flag, container = _find_xcode_container(search_dir, max_depth=max_depth)
    resolved_scheme = await _resolve_scheme(flag, container, scheme)

    is_managed = derived_data is None
    if is_managed:
        dd = resolve_managed_derived_data_dir(
            project_root, resolved_scheme, configuration
        )
        if clean and dd.is_dir() and (dd / DERIVED_DATA_SENTINEL).is_file():
            shutil.rmtree(dd)
        _mark_managed_derived_data(dd)
    else:
        assert derived_data is not None
        dd = derived_data.resolve()
        dd.mkdir(parents=True, exist_ok=True)

    actions = ["clean", "build"] if clean else ["build"]
    cmd = [
        "xcodebuild",
        flag,
        str(container),
        "-scheme",
        resolved_scheme,
        "-configuration",
        configuration,
        "-sdk",
        "iphonesimulator",
        "-destination",
        "generic/platform=iOS Simulator",
        "-derivedDataPath",
        str(dd),
        "EXCLUDED_ARCHS=x86_64",
        "ONLY_ACTIVE_ARCH=YES",
        "CODE_SIGNING_ALLOWED=NO",
        *extra_args,
        *actions,
    ]
    rc, out = await _run_cmd(cmd, cwd=container.parent, timeout=900.0)
    logs.append(out)
    if rc != 0:
        raise RuntimeError(
            f"xcodebuild failed (exit {rc}) for scheme {resolved_scheme!r}:\n{_tail_lines(out)}"
        )

    app_path = _find_built_app([dd / "Build" / "Products", dd], scheme=resolved_scheme)
    if is_managed:
        _mark_managed_derived_data(dd)
        with contextlib.suppress(Exception):
            prune_derived_data_cache(keep=DEFAULT_DERIVED_DATA_KEEP, exclude=dd)
    return app_path, resolved_scheme, dd


async def build_ios_simulator_app(
    project_path: str | Path,
    *,
    scheme: str | None = None,
    configuration: str = "Debug",
    build_args: str | None = None,
    derived_data_dir: str | Path | None = None,
    clean: bool = False,
) -> dict[str, Any]:
    """Build an unsigned arm64 iOS Simulator `.app` bundle and return build metadata."""
    from .sim_provisioner import extract_bundle_id

    root = Path(project_path).resolve()
    if not root.exists():
        raise FileNotFoundError(f"Project path does not exist: {root}")

    framework = detect_framework(root)
    extra_args = parse_build_args(build_args)
    explicit_dd: Path | None = (
        Path(derived_data_dir).resolve() if derived_data_dir is not None else None
    )
    used_dd: Path | None = explicit_dd

    logs: list[str] = []
    resolved_scheme: str | None = scheme
    app_path: Path

    if framework == "flutter":
        if clean:
            rc, out = await _run_cmd(["flutter", "clean"], cwd=root, timeout=300.0)
            logs.append(out)
            if rc != 0:
                raise RuntimeError(f"flutter clean failed:\n{_tail_lines(out)}")
        rc, out = await _run_cmd(["flutter", "pub", "get"], cwd=root, timeout=300.0)
        logs.append(out)
        if rc != 0:
            raise RuntimeError(f"flutter pub get failed:\n{_tail_lines(out)}")
        mode_flag = "--release" if configuration.lower() == "release" else "--debug"
        rc, out = await _run_cmd(
            ["flutter", "build", "ios", "--simulator", mode_flag, *extra_args],
            cwd=root,
            timeout=900.0,
        )
        logs.append(out)
        if rc != 0:
            raise RuntimeError(f"flutter build ios failed:\n{_tail_lines(out)}")
        app_path = _find_built_app([root / "build" / "ios" / "iphonesimulator"])

    elif framework == "expo":
        await _maybe_npm_install(root, logs)
        rc, out = await _run_cmd(
            ["npx", "expo", "prebuild", "--platform", "ios", "--no-install"],
            cwd=root,
            env={"CI": "1"},
            timeout=300.0,
        )
        logs.append(out)
        if rc != 0:
            raise RuntimeError(f"expo prebuild failed:\n{_tail_lines(out)}")
        await _maybe_pod_install(root / "ios", logs)
        app_path, resolved_scheme, used_dd = await _xcbuild(
            root / "ios",
            project_root=root,
            scheme=scheme,
            configuration=configuration,
            extra_args=extra_args,
            derived_data=explicit_dd,
            logs=logs,
            clean=clean,
        )

    elif framework == "react-native":
        await _maybe_npm_install(root, logs)
        await _maybe_pod_install(root / "ios", logs)
        app_path, resolved_scheme, used_dd = await _xcbuild(
            root / "ios",
            project_root=root,
            scheme=scheme,
            configuration=configuration,
            extra_args=extra_args,
            derived_data=explicit_dd,
            logs=logs,
            clean=clean,
        )

    elif framework == "capacitor":
        await _maybe_npm_install(root, logs)
        rc, out = await _run_cmd(
            ["npm", "run", "build", "--if-present"], cwd=root, timeout=300.0
        )
        logs.append(out)
        if rc != 0:
            raise RuntimeError(f"npm run build failed:\n{_tail_lines(out)}")
        rc, out = await _run_cmd(["npx", "cap", "sync", "ios"], cwd=root, timeout=300.0)
        logs.append(out)
        if rc != 0:
            raise RuntimeError(f"npx cap sync ios failed:\n{_tail_lines(out)}")
        await _maybe_pod_install(root / "ios" / "App", logs)
        app_path, resolved_scheme, used_dd = await _xcbuild(
            root / "ios" / "App",
            project_root=root,
            scheme=scheme,
            configuration=configuration,
            extra_args=extra_args,
            derived_data=explicit_dd,
            logs=logs,
            clean=clean,
        )

    elif framework == "maui":
        csproj_files = [
            p
            for p in list(root.glob("*.csproj")) + list(root.glob("*/*.csproj"))
            if p.is_file() and "UseMaui" in p.read_text(encoding="utf-8", errors="replace")
        ]
        if not csproj_files:
            raise FileNotFoundError(f"No MAUI .csproj found under {root}")
        csproj = csproj_files[0]
        tfm_match = _MAUI_TFM_RE.search(csproj.read_text(encoding="utf-8", errors="replace"))
        if not tfm_match:
            raise RuntimeError(f"Could not detect net*-ios TargetFramework in {csproj.name}")
        tfm = tfm_match.group(0)
        dotnet_cmd = [
            "dotnet",
            "build",
            str(csproj),
            "-f",
            tfm,
            "-c",
            configuration,
            "-p:RuntimeIdentifier=iossimulator-arm64",
        ]
        if clean:
            dotnet_cmd.append("--no-incremental")
        dotnet_cmd.extend(extra_args)
        rc, out = await _run_cmd(
            dotnet_cmd,
            cwd=csproj.parent,
            timeout=900.0,
        )
        logs.append(out)
        if rc != 0:
            raise RuntimeError(f"dotnet build failed:\n{_tail_lines(out)}")
        app_path = _find_built_app([csproj.parent / "bin"])

    elif framework == "kmp":
        flag, container = _find_xcode_container(root, max_depth=3)
        app_path, resolved_scheme, used_dd = await _xcbuild(
            container.parent,
            project_root=root,
            scheme=scheme,
            configuration=configuration,
            extra_args=extra_args,
            derived_data=explicit_dd,
            logs=logs,
            clean=clean,
            max_depth=1,
        )

    else:
        # Native Xcode project
        if (root / "Podfile").is_file() and not (root / "Pods").is_dir():
            await _maybe_pod_install(root, logs)
        elif (root / "ios" / "Podfile").is_file() and not (root / "ios" / "Pods").is_dir():
            await _maybe_pod_install(root / "ios", logs)
        app_path, resolved_scheme, used_dd = await _xcbuild(
            root,
            project_root=root,
            scheme=scheme,
            configuration=configuration,
            extra_args=extra_args,
            derived_data=explicit_dd,
            logs=logs,
            clean=clean,
            max_depth=2,
        )

    bundle_id = extract_bundle_id(app_path)
    combined_log = "\n".join(logs)
    return {
        "framework": framework,
        "app_path": str(app_path),
        "bundle_id": bundle_id,
        "scheme": resolved_scheme,
        "derived_data_dir": str(used_dd) if used_dd else None,
        "log_tail": _tail_lines(combined_log, 40),
    }
