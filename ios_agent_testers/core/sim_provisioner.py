"""iOS Simulator joint runtime/device resolver, persona provisioner, and doctor."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .personas import VALID_TEXT_SIZES, load_personas

logger = logging.getLogger("ios-agent-testers.provisioner")

TERMINAL_PHONE_HOME = Path(
    os.environ.get(
        "TERMINAL_PHONE_HOME",
        str(Path.home() / "Library" / "Application Support" / "terminal-phone"),
    )
)
DEFAULT_COMPANION_BIN = (
    TERMINAL_PHONE_HOME / "dependencies" / "companion" / "idb_companion"
)
QA_SOCK_DIR = Path(f"/tmp/ios-agent-testers-{os.getuid()}")
PLISTBUDDY_BIN = Path("/usr/libexec/PlistBuddy")
SIPS_BIN = Path("/usr/bin/sips")

_US_QWERTY_KEYBOARD = "en_US@sw=QWERTY;hw=US"
_SIM_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")
_CRITICAL_DEFAULTS_KEYS = frozenset({"AppleLanguages", "AppleLocale"})
_DEFAULTS_MAX_CONCURRENCY = 6


def resolve_companion_bin() -> Path | None:
    """Locate the `idb_companion` binary from env override, terminal-phone, or PATH."""
    override = (os.environ.get("IOS_AGENT_TESTERS_COMPANION") or "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p
    if DEFAULT_COMPANION_BIN.is_file():
        return DEFAULT_COMPANION_BIN
    which = shutil.which("idb_companion")
    if which:
        return Path(which)
    return None


def _parse_version_tuple(version_str: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", str(version_str or "0"))]
    return tuple(nums) if nums else (0,)


def _parse_major(wanted_ios_major: int | str | None) -> int | None:
    if wanted_ios_major is None:
        return None
    text = str(wanted_ios_major).strip()
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


def _is_runtime_available(rt: dict[str, Any]) -> bool:
    if "isAvailable" in rt:
        return bool(rt["isAvailable"])
    avail = str(rt.get("availability") or "").lower()
    return "(available)" in avail and "unavailable" not in avail


def _runtime_supported_devices(
    rt: dict[str, Any],
    all_devicetypes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if "supportedDeviceTypes" in rt and rt["supportedDeviceTypes"] is not None:
        return [d for d in rt["supportedDeviceTypes"] if isinstance(d, dict)]
    return [d for d in all_devicetypes if isinstance(d, dict)]


def _match_device_in_list(
    wanted_sim: str,
    devices: list[dict[str, Any]],
) -> dict[str, Any] | None:
    wanted_norm = wanted_sim.strip()
    for d in devices:
        if str(d.get("name") or "").strip() == wanted_norm:
            return d
    wanted_low = wanted_norm.lower()
    for d in devices:
        if str(d.get("name") or "").strip().lower() == wanted_low:
            return d
    return None


def _score_fallback_device(wanted_sim: str, candidate_name: str) -> tuple[int, int, str]:
    """Score fallback candidates (lower tuple = better match)."""
    w = wanted_sim.lower()
    c = candidate_name.lower()

    is_duo = "duo" in c
    want_ipad = w.startswith("ipad")
    cand_ipad = c.startswith("ipad")
    family_penalty = 0 if (want_ipad == cand_ipad) else 100

    # Strongly avoid experimental/foldable 'iPhone Duo' as a generic iPhone fallback
    duo_penalty = 50 if (is_duo and "duo" not in w) else 0

    tier_penalty = 10
    if want_ipad and cand_ipad:
        if "mini" in w:
            tier_penalty = 0 if "mini" in c else 15
        elif "pro" in w:
            if "pro" in c:
                if ("13" in w or "12.9" in w) and ("13" in c or "12.9" in c):
                    tier_penalty = 0
                elif "11" in w and "11" in c:
                    tier_penalty = 0
                else:
                    tier_penalty = 2
            else:
                tier_penalty = 12
        elif "air" in w:
            tier_penalty = 0 if "air" in c else 12
        else:
            # Standard iPad
            if "pro" not in c and "air" not in c and "mini" not in c:
                tier_penalty = 0
            else:
                tier_penalty = 8
    elif not want_ipad and not cand_ipad:
        if "se" in w or "mini" in w:
            if "se (3rd" in c:
                tier_penalty = 0
            elif "se" in c:
                tier_penalty = 1
            elif "mini" in c:
                tier_penalty = 2
            else:
                tier_penalty = 10
        elif "pro max" in w:
            tier_penalty = 0 if "pro max" in c else (3 if "plus" in c else 10)
        elif "plus" in w:
            tier_penalty = 0 if "plus" in c else (3 if "pro max" in c else 10)
        elif "pro" in w:
            tier_penalty = 0 if ("pro" in c and "max" not in c) else 8
        elif "16e" in w or "17e" in w:
            tier_penalty = 0 if re.search(r"\d+e\b", c) else 6
        else:
            # Standard non-Pro/non-Max iPhone preferred
            if not any(k in c for k in ("pro", "max", "plus", "se", "mini", "duo")):
                tier_penalty = 0
            else:
                tier_penalty = 5

    # Prefer higher model generation number within same tier (negated)
    nums = [int(x) for x in re.findall(r"\d+", c)]
    gen = nums[0] if nums else 0
    return (family_penalty + duo_penalty + tier_penalty, -gen, candidate_name)


def resolve_sim_target(
    wanted_sim: str,
    wanted_ios_major: int | str | None,
    simctl_data: dict[str, Any],
) -> dict[str, Any]:
    """Resolve `(device_type, runtime)` jointly using each runtime's `supportedDeviceTypes`.

    Avoids the iOS 27.1 (`iPhone Duo`-only) vs iOS 27.0 (62 standard devices) mismatch
    by always verifying that the selected runtime actually supports the selected device type.
    """
    runtimes_raw = simctl_data.get("runtimes") or []
    all_devicetypes = simctl_data.get("devicetypes") or []

    ios_runtimes: list[tuple[tuple[int, ...], dict[str, Any], list[dict[str, Any]]]] = []
    for rt in runtimes_raw:
        if not isinstance(rt, dict):
            continue
        ident = str(rt.get("identifier") or "")
        if ".SimRuntime.iOS" not in ident:
            continue
        if not _is_runtime_available(rt):
            continue
        ver_tuple = _parse_version_tuple(str(rt.get("version") or "0"))
        supported = _runtime_supported_devices(rt, all_devicetypes)
        if supported:
            ios_runtimes.append((ver_tuple, rt, supported))

    if not ios_runtimes:
        raise RuntimeError(
            "No available iOS Simulator runtimes with supported devices were found. "
            "Install an iOS Simulator runtime in Xcode Settings -> Components."
        )

    # Sort newest runtime version first
    ios_runtimes.sort(key=lambda item: item[0], reverse=True)

    wanted_major = _parse_major(wanted_ios_major)
    wanted_clean = (wanted_sim or "iPhone 16").strip()

    # Pass 1: Exact requested device on a runtime matching requested iOS major
    if wanted_major is not None:
        for ver_tuple, rt, supported in ios_runtimes:
            if ver_tuple[0] == wanted_major:
                dt = _match_device_in_list(wanted_clean, supported)
                if dt is not None:
                    return {
                        "device_type_id": str(dt["identifier"]),
                        "device_type_name": str(dt["name"]),
                        "runtime_id": str(rt["identifier"]),
                        "runtime_version": str(rt["version"]),
                        "fallback_reason": None,
                    }

    # Pass 2: Exact requested device on the newest available runtime that supports it
    for ver_tuple, rt, supported in ios_runtimes:
        dt = _match_device_in_list(wanted_clean, supported)
        if dt is not None:
            reason: str | None = None
            if wanted_major is not None and ver_tuple[0] != wanted_major:
                reason = (
                    f"Requested iOS {wanted_major} not available for '{dt['name']}'; "
                    f"using iOS {rt['version']}"
                )
            return {
                "device_type_id": str(dt["identifier"]),
                "device_type_name": str(dt["name"]),
                "runtime_id": str(rt["identifier"]),
                "runtime_version": str(rt["version"]),
                "fallback_reason": reason,
            }

    # Pass 3: Fallback device within same family across available runtimes
    # Evaluate candidates jointly across all supporting runtimes so a runtime that
    # only supports e.g. 'iPhone Duo' does not outrank a runtime supporting standard models.
    best_choice: tuple[tuple[int, int, tuple[int, ...], int, str], dict[str, Any], dict[str, Any]] | None = None
    for ver_tuple, rt, supported in ios_runtimes:
        major_mismatch = 0 if (wanted_major is None or ver_tuple[0] == wanted_major) else 1
        neg_ver = tuple(-p for p in ver_tuple)
        for dt in supported:
            cand_name = str(dt.get("name") or "")
            if not cand_name:
                continue
            tier_pen, neg_gen, _ = _score_fallback_device(wanted_clean, cand_name)
            # Rank: family+duo+tier fit first, then major match, then newest runtime, then generation
            rank = (tier_pen, major_mismatch, neg_ver, neg_gen, cand_name)
            if best_choice is None or rank < best_choice[0]:
                best_choice = (rank, dt, rt)

    assert best_choice is not None
    _, dt, rt = best_choice
    reason = (
        f"Requested device '{wanted_clean}' unavailable on installed runtimes; "
        f"fell back to '{dt['name']}' on iOS {rt['version']}"
    )
    return {
        "device_type_id": str(dt["identifier"]),
        "device_type_name": str(dt["name"]),
        "runtime_id": str(rt["identifier"]),
        "runtime_version": str(rt["version"]),
        "fallback_reason": reason,
    }


def list_available_runtimes_and_devices() -> dict[str, Any]:
    """Query `xcrun simctl list -j devicetypes runtimes` synchronously."""
    proc = subprocess.run(
        ["xcrun", "simctl", "list", "-j", "devicetypes", "runtimes"],
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"xcrun simctl list failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return json.loads(proc.stdout)


def extract_bundle_id(app_path: str | Path) -> str:
    """Extract `CFBundleIdentifier` from `<app_path>/Info.plist` using PlistBuddy with plistlib fallback."""
    app = Path(app_path)
    plist_path = app / "Info.plist"
    if not plist_path.is_file():
        raise FileNotFoundError(f"Info.plist not found in app bundle: {plist_path}")

    if PLISTBUDDY_BIN.is_file():
        proc = subprocess.run(
            [str(PLISTBUDDY_BIN), "-c", "Print :CFBundleIdentifier", str(plist_path)],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        bid = proc.stdout.strip()
        if proc.returncode == 0 and bid:
            return bid

    with plist_path.open("rb") as fh:
        data = plistlib.load(fh)
    bid = str(data.get("CFBundleIdentifier") or "").strip()
    if not bid:
        raise ValueError(f"CFBundleIdentifier missing or empty in {plist_path}")
    return bid


def extract_executable_name(app_path: str | Path) -> str | None:
    """Extract `CFBundleExecutable` from `<app_path>/Info.plist` (PlistBuddy + plistlib -> stem)."""
    raw_str = str(app_path or "").strip()
    if not raw_str:
        return None
    app = Path(raw_str)
    plist_path = app / "Info.plist"

    if plist_path.is_file():
        if PLISTBUDDY_BIN.is_file():
            with contextlib.suppress(Exception):
                proc = subprocess.run(
                    [str(PLISTBUDDY_BIN), "-c", "Print :CFBundleExecutable", str(plist_path)],
                    capture_output=True,
                    text=True,
                    timeout=10.0,
                    check=False,
                )
                exe = proc.stdout.strip()
                if proc.returncode == 0 and exe:
                    return exe

        with contextlib.suppress(Exception):
            with plist_path.open("rb") as fh:
                data = plistlib.load(fh)
            if isinstance(data, dict):
                exe = str(data.get("CFBundleExecutable") or "").strip()
                if exe:
                    return exe

    stem = app.stem.strip()
    return stem or None


async def resolve_executable_name(udid: str, bundle_id: str) -> str | None:
    """Resolve an installed app's `CFBundleExecutable` via `simctl get_app_container`."""
    bid = (bundle_id or "").strip()
    if not udid or not bid:
        return None
    try:
        rc, out, _ = await _run_subproc(
            ["xcrun", "simctl", "get_app_container", udid, bid, "app"],
            timeout=10.0,
        )
    except (TimeoutError, Exception) as exc:
        logger.debug("get_app_container failed for %s on %s: %s", bid, udid[:8], exc)
        return None
    if rc != 0:
        return None
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not lines:
        return None
    container_path = Path(lines[-1])
    return await asyncio.to_thread(extract_executable_name, container_path)


def build_locale_keyboard_commands(udid: str, locale: str) -> list[list[str]]:
    """Build exact simulator-scoped locale, RTL, US QWERTY, and keyboard hygiene commands."""
    loc = (locale or "en-US").strip()
    loc_underscore = loc.replace("-", "_")
    base = ["xcrun", "simctl", "spawn", udid, "defaults", "write"]

    commands: list[list[str]] = [
        [*base, "-g", "AppleLanguages", "-array", loc],
        [*base, "-g", "AppleLocale", "-string", loc_underscore],
        [*base, "-g", "AppleKeyboards", "-array", _US_QWERTY_KEYBOARD],
        [*base, "com.apple.Preferences", "KeyboardLastUsed", "-string", _US_QWERTY_KEYBOARD],
        [*base, "com.apple.Preferences", "KeyboardAutocapitalization", "-bool", "NO"],
        [*base, "com.apple.Preferences", "KeyboardAutocorrection", "-bool", "NO"],
        [*base, "com.apple.Preferences", "KeyboardPrediction", "-bool", "NO"],
        [*base, "com.apple.Preferences", "KeyboardCheckSpelling", "-bool", "NO"],
    ]

    for domain in ("com.apple.Preferences", "com.apple.keyboard.preferences"):
        commands.extend(
            [
                [*base, domain, "DidShowContinuousPathIntroduction", "-bool", "YES"],
                [*base, domain, "KeyboardDidShowProductivityTutorial", "-bool", "YES"],
                [*base, domain, "DidShowGestureKeyboardIntroduction", "-bool", "YES"],
            ]
        )

    return commands


async def _run_subproc(
    args: list[str],
    *,
    timeout: float = 30.0,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode or 0,
            out_b.decode("utf-8", errors="replace"),
            err_b.decode("utf-8", errors="replace"),
        )
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        raise TimeoutError(f"Command timed out after {timeout}s: {' '.join(args)}")


async def _boot_and_wait(udid: str) -> None:
    rc, _, err = await _run_subproc(["xcrun", "simctl", "boot", udid], timeout=45.0)
    if rc != 0 and "current state: Booted" not in err:
        raise RuntimeError(f"simctl boot failed for {udid}: {err.strip()}")
    rc, _, err = await _run_subproc(
        ["xcrun", "simctl", "bootstatus", udid, "-b"], timeout=120.0
    )
    if rc != 0:
        raise RuntimeError(f"simctl bootstatus failed for {udid}: {err.strip()}")


async def _lookup_simulator_name(udid: str) -> str | None:
    rc, out, _ = await _run_subproc(
        ["xcrun", "simctl", "list", "devices", "-j"], timeout=10.0
    )
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    target_upper = udid.upper()
    for dev_list in (data.get("devices") or {}).values():
        for dev in dev_list or []:
            if str(dev.get("udid") or "").upper() == target_upper:
                return str(dev.get("name") or "")
    return None


async def provision_persona_sim(
    run_id: str,
    persona: dict[str, Any],
    *,
    app_path: str | None = None,
    bundle_id: str | None = None,
    record_video: bool = False,
    video_path: Path | None = None,
    keep_simulator: bool = False,
) -> dict[str, Any]:
    """Create, boot, configure locale/keyboard/theme/text_size/geo, install & launch app."""
    persona_id = str(persona.get("id") or "persona")
    device_cfg = persona.get("device") or {}
    wanted_sim = str(device_cfg.get("simulator") or "iPhone 16")
    wanted_ios = device_cfg.get("ios")
    theme = str(device_cfg.get("theme") or "light")
    text_size = str(device_cfg.get("text_size") or "large")
    locale = str(persona.get("locale") or "en-US")
    geo = persona.get("geo")

    if theme not in ("light", "dark"):
        theme = "light"
    if text_size not in VALID_TEXT_SIZES:
        text_size = "large"

    simctl_data = await asyncio.to_thread(list_available_runtimes_and_devices)
    target = resolve_sim_target(wanted_sim, wanted_ios, simctl_data)

    short_run = _SIM_NAME_SAFE_RE.sub("", str(run_id or "run"))[-12:] or "run"
    safe_persona = _SIM_NAME_SAFE_RE.sub("", persona_id)[:20] or "persona"
    sim_name = f"qa-{short_run}-{safe_persona}"

    rc, out, err = await _run_subproc(
        [
            "xcrun",
            "simctl",
            "create",
            sim_name,
            target["device_type_id"],
            target["runtime_id"],
        ],
        timeout=30.0,
    )
    if rc != 0:
        raise RuntimeError(
            f"simctl create failed for {sim_name} ({target['device_type_name']} / "
            f"iOS {target['runtime_version']}): {err.strip() or out.strip()}"
        )
    udid = out.strip().splitlines()[-1].strip()
    if not udid:
        raise RuntimeError(f"simctl create returned empty UDID for {sim_name}")

    video_pid: int | None = None
    try:
        # 1. Boot #1
        await _boot_and_wait(udid)

        # 2. Configure locale/RTL + US QWERTY + suppress autocorrect & keyboard tutorials
        # Run bounded-parallel direct `simctl spawn defaults write` (NEVER `/bin/sh -c`,
        # which loses DYLD_ROOT_PATH under macOS SIP and aborts with rc=134).
        defaults_sem = asyncio.Semaphore(_DEFAULTS_MAX_CONCURRENCY)

        async def _apply_default(cmd: list[str]) -> None:
            is_critical = any(k in cmd for k in _CRITICAL_DEFAULTS_KEYS)
            attempts = 2 if is_critical else 1
            async with defaults_sem:
                for attempt in range(attempts):
                    try:
                        c_rc, _, c_err = await _run_subproc(cmd, timeout=30.0)
                    except TimeoutError as exc:
                        if attempt + 1 < attempts:
                            logger.warning(
                                "Critical defaults write timed out (%s), retrying once: %s",
                                " ".join(cmd[5:]),
                                exc,
                            )
                            await asyncio.sleep(0.25)
                            continue
                        logger.warning(
                            "Non-fatal defaults write timeout (%s): %s",
                            " ".join(cmd[5:]),
                            exc,
                        )
                        return
                    if c_rc != 0:
                        logger.warning(
                            "Non-fatal defaults write failure (%s): %s",
                            " ".join(cmd[5:]),
                            c_err.strip(),
                        )
                    return

        await asyncio.gather(
            *(_apply_default(cmd) for cmd in build_locale_keyboard_commands(udid, locale))
        )

        # 3. Reboot so locale/RTL & keyboard preferences activate cleanly
        await _run_subproc(["xcrun", "simctl", "shutdown", udid], timeout=30.0)
        await _boot_and_wait(udid)

        # 4. Dynamic persona settings: appearance, Dynamic Type content_size, GPS location
        rc_ui, _, err_ui = await _run_subproc(
            ["xcrun", "simctl", "ui", udid, "appearance", theme], timeout=15.0
        )
        if rc_ui != 0:
            logger.warning("Failed to set appearance=%s for %s: %s", theme, udid, err_ui.strip())

        rc_ts, _, err_ts = await _run_subproc(
            ["xcrun", "simctl", "ui", udid, "content_size", text_size], timeout=15.0
        )
        if rc_ts != 0:
            logger.warning(
                "Failed to set content_size=%s for %s: %s", text_size, udid, err_ts.strip()
            )

        if isinstance(geo, (list, tuple)) and len(geo) == 2:
            geo_arg = f"{float(geo[0])},{float(geo[1])}"
            rc_loc, _, err_loc = await _run_subproc(
                ["xcrun", "simctl", "location", udid, "set", geo_arg], timeout=10.0
            )
            if rc_loc != 0:
                logger.warning("Non-fatal location set failure for %s: %s", udid, err_loc.strip())

        # 5. Install app & extract real CFBundleIdentifier / CFBundleExecutable if app_path given
        resolved_bundle_id = bundle_id
        executable_name: str | None = None
        if app_path:
            app_p = Path(app_path)
            rc_inst, _, err_inst = await _run_subproc(
                ["xcrun", "simctl", "install", udid, str(app_p)], timeout=120.0
            )
            if rc_inst != 0:
                raise RuntimeError(
                    f"simctl install failed for {app_p} on {udid}: {err_inst.strip()}"
                )
            with contextlib.suppress(Exception):
                resolved_bundle_id = await asyncio.to_thread(extract_bundle_id, app_p)
            with contextlib.suppress(Exception):
                executable_name = await asyncio.to_thread(extract_executable_name, app_p)

        if not executable_name and resolved_bundle_id:
            with contextlib.suppress(Exception):
                executable_name = await resolve_executable_name(udid, resolved_bundle_id)

        # 6. Launch app and wait briefly for initial UI settle (capped at 6s)
        if resolved_bundle_id:
            rc_launch, _, err_launch = await _run_subproc(
                ["xcrun", "simctl", "launch", udid, resolved_bundle_id], timeout=40.0
            )
            if rc_launch != 0:
                raise RuntimeError(
                    f"simctl launch failed for {resolved_bundle_id} on {udid}: {err_launch.strip()}"
                )
            deadline = time.monotonic() + 5.0
            for _ in range(10):
                if time.monotonic() >= deadline:
                    break
                try:
                    l_rc, l_out, _ = await _run_subproc(
                        ["xcrun", "simctl", "spawn", udid, "launchctl", "list"], timeout=8.0
                    )
                except TimeoutError as exc:
                    logger.warning(
                        "Non-fatal launchctl list poll timeout for %s: %s",
                        udid[:8],
                        exc,
                    )
                    await asyncio.sleep(0.5)
                    continue
                if l_rc == 0 and resolved_bundle_id in l_out:
                    await asyncio.sleep(1.5)
                    break
                await asyncio.sleep(0.5)

        # 7. Optional H.264 video recording in dedicated process group
        if record_video and video_path is not None:
            vp = Path(video_path)
            vp.parent.mkdir(parents=True, exist_ok=True)
            rec_proc = subprocess.Popen(
                [
                    "xcrun",
                    "simctl",
                    "io",
                    udid,
                    "recordVideo",
                    "--codec=h264",
                    "--force",
                    str(vp),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            await asyncio.sleep(1.0)
            if rec_proc.poll() is None:
                video_pid = rec_proc.pid
            else:
                logger.warning(
                    "Video recording exited immediately (rc=%s) for %s",
                    rec_proc.returncode,
                    udid,
                )

    except Exception:
        if video_pid is not None:
            with contextlib.suppress(Exception):
                await stop_video_recording(video_pid, timeout=5.0)
        if not keep_simulator:
            with contextlib.suppress(Exception):
                await _run_subproc(["xcrun", "simctl", "shutdown", udid], timeout=20.0)
                await _run_subproc(["xcrun", "simctl", "delete", udid], timeout=20.0)
        raise

    return {
        "udid": udid,
        "device_label": f"{target['device_type_name']}, iOS {target['runtime_version']}",
        "runtime_version": target["runtime_version"],
        "bundle_id": resolved_bundle_id,
        "executable_name": executable_name,
        "ephemeral": True,
        "video_pid": video_pid,
        "video_path": str(video_path) if (video_pid is not None and video_path is not None) else None,
        "fallback_reason": target["fallback_reason"],
    }


def _pid_alive(pid: int) -> bool:
    with contextlib.suppress(ChildProcessError, OSError):
        waited_pid, _ = os.waitpid(pid, os.WNOHANG)
        if waited_pid == pid:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def stop_video_recording(pid: int | None, timeout: float = 30.0) -> None:
    """Stop a background `simctl io recordVideo` cleanly via SIGINT so MP4 moov atom finalizes."""
    if pid is None or pid <= 0:
        return
    if not _pid_alive(pid):
        return

    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGINT)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGINT)

    deadline = time.monotonic() + max(0.5, float(timeout))
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        await asyncio.sleep(0.25)

    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGTERM)
    await asyncio.sleep(1.0)
    if _pid_alive(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGKILL)


async def teardown_persona_sim(
    udid: str,
    *,
    ephemeral: bool = True,
    delete_simulator: bool = True,
    video_pid: int | None = None,
) -> dict[str, Any]:
    """Stop video recording, shut down simulator, and delete ONLY verified ephemeral simulators."""
    await stop_video_recording(video_pid)

    sim_name = await _lookup_simulator_name(udid)

    rc_shut, _, err_shut = await _run_subproc(
        ["xcrun", "simctl", "shutdown", udid], timeout=30.0
    )
    shutdown_ok = rc_shut == 0 or "current state: Shutdown" in err_shut

    deleted = False
    if ephemeral and delete_simulator:
        # Safety invariant: never delete a simulator unless we know it is a QA ephemeral sim
        if sim_name is not None and not sim_name.startswith("qa-"):
            logger.warning(
                "Refusing to delete non-ephemeral simulator %s (%s)", sim_name, udid
            )
        else:
            rc_del, _, err_del = await _run_subproc(
                ["xcrun", "simctl", "delete", udid], timeout=30.0
            )
            deleted = rc_del == 0
            if not deleted:
                logger.warning("simctl delete failed for %s: %s", udid, err_del.strip())

    # Clean up our isolated socket file if left behind
    sock = QA_SOCK_DIR / f"{udid}.sock"
    with contextlib.suppress(OSError):
        if sock.exists():
            sock.unlink(missing_ok=True)

    return {
        "ok": True,
        "udid": udid,
        "shutdown": shutdown_ok,
        "deleted": deleted,
    }


def doctor_check() -> dict[str, Any]:
    """Inspect local macOS host environment for iOS Agent Testers readiness."""
    checks: dict[str, Any] = {}
    warnings: list[str] = []
    runtimes_summary: list[dict[str, Any]] = []

    # 1. Python + fb-idb
    has_idb = importlib.util.find_spec("idb") is not None
    checks["python"] = {
        "ok": True,
        "executable": sys.executable,
        "version": sys.version.split()[0],
    }
    checks["fb_idb"] = {"ok": has_idb}
    if not has_idb:
        warnings.append(
            "fb-idb not importable in current Python; use terminal-phone's idb-venv/bin/python3."
        )

    # 2. idb_companion
    companion = resolve_companion_bin()
    checks["idb_companion"] = {
        "ok": companion is not None and os.access(companion, os.X_OK),
        "path": str(companion) if companion else None,
    }
    if not checks["idb_companion"]["ok"]:
        warnings.append(
            "idb_companion binary not found; install terminal-phone or set IOS_AGENT_TESTERS_COMPANION."
        )

    # 3. xcrun simctl & runtimes
    simctl_data: dict[str, Any] | None = None
    try:
        simctl_data = list_available_runtimes_and_devices()
        checks["xcrun_simctl"] = {"ok": True}
    except Exception as exc:
        checks["xcrun_simctl"] = {"ok": False, "error": str(exc)}
        warnings.append(f"xcrun simctl unavailable: {exc}")

    if simctl_data is not None:
        all_dts = simctl_data.get("devicetypes") or []
        for rt in simctl_data.get("runtimes") or []:
            ident = str(rt.get("identifier") or "")
            if ".SimRuntime.iOS" not in ident:
                continue
            avail = _is_runtime_available(rt)
            supported = _runtime_supported_devices(rt, all_dts)
            runtimes_summary.append(
                {
                    "name": rt.get("name"),
                    "version": rt.get("version"),
                    "identifier": ident,
                    "available": avail,
                    "supported_device_count": len(supported),
                }
            )
            if avail and len(supported) <= 2:
                warnings.append(
                    f"Runtime {rt.get('name')} supports only {len(supported)} device type(s); "
                    "joint resolver will automatically select a compatible runtime."
                )

        # Verify bundled personas resolve cleanly
        try:
            personas = load_personas()
            resolutions: dict[str, str] = {}
            for p in personas:
                dev = p["device"]
                res = resolve_sim_target(dev["simulator"], dev.get("ios"), simctl_data)
                resolutions[p["id"]] = f"{res['device_type_name']} (iOS {res['runtime_version']})"
            checks["personas_compatible"] = {
                "ok": True,
                "count": len(resolutions),
                "sample": dict(list(resolutions.items())[:3]),
            }
        except Exception as exc:
            checks["personas_compatible"] = {"ok": False, "error": str(exc)}
            warnings.append(f"Persona simulator resolution check failed: {exc}")
    else:
        checks["personas_compatible"] = {"ok": False}

    # 4. xcodebuild, sips, PlistBuddy, optional gh
    try:
        xb = subprocess.run(
            ["xcodebuild", "-version"], capture_output=True, text=True, timeout=10.0, check=False
        )
        checks["xcodebuild"] = {
            "ok": xb.returncode == 0,
            "version": xb.stdout.strip().splitlines()[0] if xb.stdout.strip() else None,
        }
    except Exception as exc:
        checks["xcodebuild"] = {"ok": False, "error": str(exc)}

    checks["sips"] = {"ok": SIPS_BIN.is_file(), "path": str(SIPS_BIN)}
    checks["plistbuddy"] = {"ok": PLISTBUDDY_BIN.is_file(), "path": str(PLISTBUDDY_BIN)}

    gh_path = shutil.which("gh")
    checks["gh_cli"] = {"ok": gh_path is not None, "path": gh_path}
    if not gh_path:
        warnings.append("gh CLI not found (optional; needed only for fetching GitHub issue URLs).")

    # 5. Non-destructive check of Simulator hardware keyboard setting
    try:
        kb = subprocess.run(
            ["defaults", "read", "com.apple.iphonesimulator", "ConnectHardwareKeyboard"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        hw_kb_on = kb.returncode == 0 and kb.stdout.strip() in ("1", "true", "YES")
        checks["connect_hardware_keyboard"] = {"enabled": hw_kb_on}
        if hw_kb_on:
            warnings.append(
                "Simulator ConnectHardwareKeyboard is enabled; on-screen software keyboard may stay hidden "
                "(disable via: defaults write com.apple.iphonesimulator ConnectHardwareKeyboard -bool false)."
            )
    except Exception:
        checks["connect_hardware_keyboard"] = {"enabled": None}

    has_usable_runtime = any(
        r["available"] and r["supported_device_count"] > 0 for r in runtimes_summary
    )
    overall_ok = bool(
        checks["fb_idb"]["ok"]
        and checks["idb_companion"]["ok"]
        and checks["xcrun_simctl"]["ok"]
        and has_usable_runtime
    )

    return {
        "ok": overall_ok,
        "checks": checks,
        "runtimes": runtimes_summary,
        "warnings": warnings,
    }
