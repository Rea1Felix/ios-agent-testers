"""Unit tests for ios_agent_testers.core.builder."""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ios_agent_testers.core.builder import (
    DERIVED_DATA_LAST_USED,
    DERIVED_DATA_SENTINEL,
    _find_built_app,
    _find_xcode_container,
    build_ios_simulator_app,
    detect_framework,
    parse_build_args,
    prune_derived_data_cache,
)


class TestBuilder(unittest.TestCase):
    def test_detect_framework_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            # 1. Native default
            self.assertEqual(detect_framework(root), "native")

            # 2. KMP
            (root / "shared").mkdir()
            (root / "shared" / "build.gradle.kts").write_text(
                'plugins { kotlin("multiplatform") }', encoding="utf-8"
            )
            self.assertEqual(detect_framework(root), "kmp")

            # 3. MAUI beats KMP
            (root / "App.csproj").write_text(
                "<Project><PropertyGroup><UseMaui>true</UseMaui></PropertyGroup></Project>",
                encoding="utf-8",
            )
            self.assertEqual(detect_framework(root), "maui")

            # 4. React Native beats MAUI
            (root / "package.json").write_text(
                json.dumps({"dependencies": {"react-native": "0.76.0"}}),
                encoding="utf-8",
            )
            self.assertEqual(detect_framework(root), "react-native")

            # 5. Expo (when no ios/ and no android/) beats React Native
            (root / "package.json").write_text(
                json.dumps({"dependencies": {"expo": "^52.0.0", "react-native": "0.76.0"}}),
                encoding="utf-8",
            )
            self.assertEqual(detect_framework(root), "expo")

            # Expo with existing ios/ falls through to react-native
            (root / "ios").mkdir()
            self.assertEqual(detect_framework(root), "react-native")
            (root / "ios").rmdir()

            # 6. Capacitor beats Expo
            (root / "capacitor.config.ts").write_text("export default {};", encoding="utf-8")
            self.assertEqual(detect_framework(root), "capacitor")

            # 7. Flutter beats everything
            (root / "pubspec.yaml").write_text("name: demo_app\n", encoding="utf-8")
            self.assertEqual(detect_framework(root), "flutter")

    def test_parse_build_args_and_secret_redaction(self) -> None:
        self.assertEqual(parse_build_args(None), [])
        self.assertEqual(
            parse_build_args('API_URL="https://staging.example.com" FOO=bar'),
            ["API_URL=https://staging.example.com", "FOO=bar"],
        )
        self.assertEqual(
            parse_build_args("--flag 1", 'SECRET_TOKEN="s3cr3t"'),
            ["--flag", "1", "SECRET_TOKEN=s3cr3t"],
        )

        with self.assertRaises(ValueError):
            parse_build_args('UNCLOSED="quote')

        # Secret syntax error MUST NOT leak the secret string
        secret = "SUPER_SECRET_KEY_999"
        with self.assertRaises(ValueError) as ctx:
            parse_build_args(None, f'TOKEN="{secret}')
        self.assertNotIn(secret, str(ctx.exception))

    def test_find_xcode_container_prefers_workspace_and_skips_pods(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Pods workspace must be ignored
            (root / "Pods" / "Pods.xcworkspace").mkdir(parents=True)
            # .xcodeproj containing inner project.xcworkspace must resolve to .xcodeproj
            proj = root / "Demo.xcodeproj"
            (proj / "project.xcworkspace").mkdir(parents=True)
            flag, path = _find_xcode_container(root)
            self.assertEqual(flag, "-project")
            self.assertEqual(path, proj)

            # Adding top-level .xcworkspace takes precedence over .xcodeproj
            ws = root / "Demo.xcworkspace"
            ws.mkdir()
            flag2, path2 = _find_xcode_container(root)
            self.assertEqual(flag2, "-workspace")
            self.assertEqual(path2, ws)

    def test_build_ios_simulator_app_native_mocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = root / "MyApp.xcodeproj"
            proj.mkdir()
            dd = root / "dd"

            recorded_cmds: list[list[str]] = []

            async def fake_run_cmd(args: list[str], *, cwd: Path, env=None, timeout=900.0):
                recorded_cmds.append(args)
                if "-list" in args:
                    return 0, json.dumps({"project": {"schemes": ["MyApp", "MyAppTests"]}})
                # Simulate xcodebuild producing both MyApp.app and MyAppTests.app
                products = dd / "Build" / "Products" / "Debug-iphonesimulator"
                products.mkdir(parents=True, exist_ok=True)
                test_app = products / "MyAppTests.app"
                test_app.mkdir(exist_ok=True)
                app = products / "MyApp.app"
                app.mkdir(exist_ok=True)
                with (app / "Info.plist").open("wb") as fh:
                    plistlib.dump({"CFBundleIdentifier": "com.example.myapp"}, fh)
                return 0, "BUILD SUCCEEDED\n"

            with patch(
                "ios_agent_testers.core.builder._run_cmd",
                new=AsyncMock(side_effect=fake_run_cmd),
            ):
                res = asyncio.run(
                    build_ios_simulator_app(
                        root,
                        configuration="Debug",
                        build_args='GCC_PREPROCESSOR_DEFINITIONS="QA=1"',
                        derived_data_dir=dd,
                    )
                )

            self.assertEqual(res["framework"], "native")
            self.assertEqual(res["scheme"], "MyApp")
            self.assertEqual(res["bundle_id"], "com.example.myapp")
            self.assertTrue(res["app_path"].endswith("MyApp.app"))
            self.assertIn("BUILD SUCCEEDED", res["log_tail"])

            # Verify unsigned arm64 simulator flags
            build_cmd = recorded_cmds[-1]
            self.assertIn("-sdk", build_cmd)
            self.assertIn("iphonesimulator", build_cmd)
            self.assertIn("generic/platform=iOS Simulator", build_cmd)
            self.assertIn("EXCLUDED_ARCHS=x86_64", build_cmd)
            self.assertIn("ONLY_ACTIVE_ARCH=YES", build_cmd)
            self.assertIn("CODE_SIGNING_ALLOWED=NO", build_cmd)
            self.assertIn("GCC_PREPROCESSOR_DEFINITIONS=QA=1", build_cmd)
            self.assertEqual(res["derived_data_dir"], str(dd.resolve()))

    def test_managed_derived_data_deterministic_scheme_none_vs_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "DayTraceProj"
            cache_root = Path(td) / "dd-cache"
            (root / "DayTrace.xcodeproj").mkdir(parents=True)

            async def fake_run_cmd(args: list[str], *, cwd: Path, env=None, timeout=900.0):
                if "-list" in args:
                    return 0, json.dumps({"project": {"schemes": ["DayTrace"]}})
                dd_idx = args.index("-derivedDataPath") + 1
                dd_path = Path(args[dd_idx])
                products = dd_path / "Build" / "Products" / "Debug-iphonesimulator"
                products.mkdir(parents=True, exist_ok=True)
                app = products / "DayTrace.app"
                app.mkdir(exist_ok=True)
                with (app / "Info.plist").open("wb") as fh:
                    plistlib.dump({"CFBundleIdentifier": "app.daytrace.recorder"}, fh)
                return 0, "BUILD SUCCEEDED\n"

            with (
                patch.dict(os.environ, {"IOS_AGENT_TESTERS_DERIVED_DATA_ROOT": str(cache_root)}),
                patch(
                    "ios_agent_testers.core.builder._run_cmd",
                    new=AsyncMock(side_effect=fake_run_cmd),
                ),
            ):
                res_auto = asyncio.run(build_ios_simulator_app(root, scheme=None))
                res_explicit = asyncio.run(build_ios_simulator_app(root, scheme="DayTrace"))

            self.assertIsNotNone(res_auto["derived_data_dir"])
            self.assertEqual(res_auto["derived_data_dir"], res_explicit["derived_data_dir"])
            dd_dir = Path(res_auto["derived_data_dir"])
            self.assertEqual(dd_dir.parent, cache_root.resolve())
            self.assertTrue((dd_dir / DERIVED_DATA_SENTINEL).is_file())
            self.assertTrue((dd_dir / DERIVED_DATA_LAST_USED).is_file())

    def test_prune_derived_data_cache_lru_and_sentinel_safety(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache_root = Path(td) / "dd-cache"
            cache_root.mkdir()

            # 7 sentinel-marked dirs with staggered .last_used mtimes
            marked_dirs: list[Path] = []
            for idx in range(7):
                d = cache_root / f"app-{idx}-deadbeef000{idx}"
                d.mkdir()
                (d / DERIVED_DATA_SENTINEL).write_text("sentinel\n", encoding="utf-8")
                lu = d / DERIVED_DATA_LAST_USED
                lu.write_text(f"{1000 + idx}\n", encoding="utf-8")
                (d / "artifact.bin").write_bytes(b"x" * 1024)
                os.utime(lu, (1_700_000_000 + idx * 100, 1_700_000_000 + idx * 100))
                marked_dirs.append(d)

            # Unmarked directory must NEVER be deleted
            unmarked = cache_root / "user-manual-dir"
            unmarked.mkdir()
            (unmarked / "important.txt").write_text("keep me", encoding="utf-8")

            report = prune_derived_data_cache(cache_root, keep=5)
            self.assertEqual(len(report["removed"]), 2)
            self.assertEqual(len(report["kept"]), 5)
            self.assertGreaterEqual(report["freed_bytes"], 2048)
            self.assertFalse(marked_dirs[0].exists())
            self.assertFalse(marked_dirs[1].exists())
            for kept_dir in marked_dirs[2:]:
                self.assertTrue(kept_dir.exists())
            self.assertTrue(unmarked.exists())
            self.assertTrue((unmarked / "important.txt").is_file())

            # keep=0 with exclude protects the excluded dir only
            report2 = prune_derived_data_cache(cache_root, keep=0, exclude=marked_dirs[2])
            self.assertEqual(len(report2["kept"]), 1)
            self.assertEqual(report2["kept"][0], str(marked_dirs[2].resolve()))
            self.assertTrue(marked_dirs[2].exists())
            self.assertEqual(len(report2["removed"]), 4)
            self.assertTrue(unmarked.exists())

    def test_clean_flag_wipes_managed_cache_and_runs_clean_build(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "CleanProj"
            cache_root = Path(td) / "dd-cache"
            (root / "CleanProj.xcodeproj").mkdir(parents=True)
            recorded_cmds: list[list[str]] = []

            async def fake_run_cmd(args: list[str], *, cwd: Path, env=None, timeout=900.0):
                recorded_cmds.append(args)
                dd_idx = args.index("-derivedDataPath") + 1
                dd_path = Path(args[dd_idx])
                products = dd_path / "Build" / "Products" / "Debug-iphonesimulator"
                products.mkdir(parents=True, exist_ok=True)
                app = products / "CleanProj.app"
                app.mkdir(exist_ok=True)
                with (app / "Info.plist").open("wb") as fh:
                    plistlib.dump({"CFBundleIdentifier": "com.example.clean"}, fh)
                return 0, "BUILD SUCCEEDED\n"

            with (
                patch.dict(os.environ, {"IOS_AGENT_TESTERS_DERIVED_DATA_ROOT": str(cache_root)}),
                patch(
                    "ios_agent_testers.core.builder._run_cmd",
                    new=AsyncMock(side_effect=fake_run_cmd),
                ),
            ):
                res1 = asyncio.run(build_ios_simulator_app(root, scheme="CleanProj"))
                stale_file = Path(res1["derived_data_dir"]) / "stale.tmp"
                stale_file.write_text("old", encoding="utf-8")
                self.assertTrue(stale_file.is_file())

                res2 = asyncio.run(
                    build_ios_simulator_app(root, scheme="CleanProj", clean=True)
                )

            self.assertEqual(res1["derived_data_dir"], res2["derived_data_dir"])
            self.assertFalse(stale_file.exists())
            self.assertEqual(recorded_cmds[-1][-2:], ["clean", "build"])

    def test_non_xcode_flutter_creates_no_derived_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "FlutterApp"
            cache_root = Path(td) / "dd-cache"
            root.mkdir()
            (root / "pubspec.yaml").write_text("name: demo\n", encoding="utf-8")

            async def fake_run_cmd(args: list[str], *, cwd: Path, env=None, timeout=900.0):
                if args[:2] == ["flutter", "build"]:
                    app = root / "build" / "ios" / "iphonesimulator" / "Runner.app"
                    app.mkdir(parents=True, exist_ok=True)
                    with (app / "Info.plist").open("wb") as fh:
                        plistlib.dump({"CFBundleIdentifier": "com.example.flutter"}, fh)
                return 0, "OK\n"

            with (
                patch.dict(os.environ, {"IOS_AGENT_TESTERS_DERIVED_DATA_ROOT": str(cache_root)}),
                patch(
                    "ios_agent_testers.core.builder._run_cmd",
                    new=AsyncMock(side_effect=fake_run_cmd),
                ),
            ):
                res = asyncio.run(build_ios_simulator_app(root))

            self.assertEqual(res["framework"], "flutter")
            self.assertIsNone(res["derived_data_dir"])
            self.assertFalse(cache_root.exists())

    def test_find_built_app_skips_nested_watch_and_appclip_even_with_newer_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dd = Path(td) / "dd"
            products = dd / "Build" / "Products" / "Debug-iphonesimulator"
            main_app = products / "DayTrace.app"
            watch_app = main_app / "Watch" / "DayTrace Watch.app"
            clip_app = main_app / "AppClips" / "DayTraceClip.app"
            other_top = products / "HelperTool.app"
            watch_sim_app = dd / "Build" / "Products" / "Debug-watchsimulator" / "DayTrace Watch.app"

            for p in (main_app, watch_app, clip_app, other_top, watch_sim_app):
                p.mkdir(parents=True)

            # Give embedded Watch/AppClip and unrelated HelperTool newer mtimes than DayTrace.app
            os.utime(main_app, (1_700_000_100, 1_700_000_100))
            os.utime(watch_app, (1_700_000_900, 1_700_000_900))
            os.utime(clip_app, (1_700_000_950, 1_700_000_950))
            os.utime(other_top, (1_700_000_800, 1_700_000_800))
            os.utime(watch_sim_app, (1_700_000_990, 1_700_000_990))

            # With scheme="DayTrace", DayTrace.app wins over newer Watch, AppClip, HelperTool, and watchsimulator
            found = _find_built_app([dd / "Build" / "Products", dd], scheme="DayTrace")
            self.assertEqual(found, main_app)

            # Even without scheme (when only DayTrace.app + nested bundles exist), Watch/AppClip are skipped
            other_top.rmdir()
            found_no_scheme = _find_built_app([dd / "Build" / "Products", dd])
            self.assertEqual(found_no_scheme, main_app)


if __name__ == "__main__":
    unittest.main()
