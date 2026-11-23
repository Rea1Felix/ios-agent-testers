"""Unit tests for ios_agent_testers.core.sim_provisioner.

Includes regression tests for the macOS Xcode 27 runtime matrix:
iOS 27.0 (62 standard devices) vs iOS 27.1 (iPhone Duo only).
"""

from __future__ import annotations

import asyncio
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ios_agent_testers.core.personas import load_personas
from ios_agent_testers.core.sim_provisioner import (
    build_locale_keyboard_commands,
    doctor_check,
    extract_bundle_id,
    extract_executable_name,
    provision_persona_sim,
    resolve_executable_name,
    resolve_sim_target,
    teardown_persona_sim,
)

# Faithful snapshot of this Mac's iOS 27.0 (62 devices) vs iOS 27.1 (iPhone Duo only) matrix
_IOS_27_0_DEVICES = [
    "iPhone 18 Pro",
    "iPhone 18 Pro Max",
    "iPhone 17 Pro",
    "iPhone 17 Pro Max",
    "iPhone 17e",
    "iPhone Air",
    "iPhone 17",
    "iPhone 16 Pro",
    "iPhone 16 Pro Max",
    "iPhone 16e",
    "iPhone 16",
    "iPhone 16 Plus",
    "iPhone 15 Pro",
    "iPhone 15 Pro Max",
    "iPhone 15",
    "iPhone 15 Plus",
    "iPhone 14 Pro",
    "iPhone 14 Pro Max",
    "iPhone 14",
    "iPhone 14 Plus",
    "iPhone SE (3rd generation)",
    "iPhone 13 Pro",
    "iPhone 13 Pro Max",
    "iPhone 13",
    "iPhone 13 mini",
    "iPhone 12 Pro",
    "iPhone 12 Pro Max",
    "iPhone 12",
    "iPhone 12 mini",
    "iPhone SE (2nd generation)",
    "iPhone 11 Pro",
    "iPhone 11 Pro Max",
    "iPhone 11",
    "iPad Pro 13-inch (M5) (16GB)",
    "iPad Pro 13-inch (M5)",
    "iPad Pro 11-inch (M5) (16GB)",
    "iPad Pro 11-inch (M5)",
    "iPad Pro 11-inch (M4) (16GB)",
    "iPad Pro 11-inch (M4)",
    "iPad Pro 13-inch (M4) (16GB)",
    "iPad Pro 13-inch (M4)",
    "iPad Air 13-inch (M4)",
    "iPad Air 11-inch (M4)",
    "iPad (A16)",
    "iPad Air 13-inch (M3)",
    "iPad Air 11-inch (M3)",
    "iPad Air 11-inch (M2)",
    "iPad Air 13-inch (M2)",
    "iPad mini (A17 Pro)",
    "iPad Pro (11-inch) (4th generation) (16GB)",
    "iPad Pro (11-inch) (4th generation)",
    "iPad Pro (12.9-inch) (6th generation) (16GB)",
    "iPad Pro (12.9-inch) (6th generation)",
    "iPad (10th generation)",
    "iPad Air (5th generation)",
    "iPad mini (6th generation)",
    "iPad Pro (11-inch) (3rd generation)",
    "iPad Pro (12.9-inch) (5th generation)",
    "iPad (9th generation)",
    "iPad Air (4th generation)",
    "iPad Pro (11-inch) (2nd generation)",
    "iPad Pro (12.9-inch) (4th generation)",
]


def _dt_entry(name: str) -> dict:
    slug = name.replace(" ", "-").replace("(", "").replace(")", "")
    return {
        "name": name,
        "identifier": f"com.apple.CoreSimulator.SimDeviceType.{slug}",
        "productFamily": "iPad" if name.startswith("iPad") else "iPhone",
    }


def _host_simctl_fixture() -> dict:
    dt_27_0 = [_dt_entry(n) for n in _IOS_27_0_DEVICES]
    dt_27_1 = [_dt_entry("iPhone Duo")]
    return {
        "devicetypes": dt_27_0 + dt_27_1,
        "runtimes": [
            {
                "name": "iOS 27.0",
                "version": "27.0",
                "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
                "isAvailable": True,
                "supportedDeviceTypes": dt_27_0,
            },
            {
                "name": "iOS 27.1",
                "version": "27.1",
                "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-27-1",
                "isAvailable": True,
                "supportedDeviceTypes": dt_27_1,
            },
            {
                "name": "watchOS 12.0",
                "version": "12.0",
                "identifier": "com.apple.CoreSimulator.SimRuntime.watchOS-12-0",
                "isAvailable": True,
                "supportedDeviceTypes": [],
            },
        ],
    }


class TestSimProvisioner(unittest.TestCase):
    def test_regression_ios_27_0_vs_27_1_iphone_duo_all_10_personas(self) -> None:
        """All 10 personas must resolve to iOS 27.0, NEVER iOS 27.1 (iPhone Duo only)."""
        fixture = _host_simctl_fixture()
        self.assertEqual(len(fixture["runtimes"][0]["supportedDeviceTypes"]), 62)
        self.assertEqual(len(fixture["runtimes"][1]["supportedDeviceTypes"]), 1)

        for persona in load_personas():
            wanted_sim = persona["device"]["simulator"]
            wanted_ios = persona["device"]["ios"]
            resolved = resolve_sim_target(wanted_sim, wanted_ios, fixture)
            self.assertEqual(
                resolved["runtime_version"],
                "27.0",
                msg=f"{persona['id']} ({wanted_sim}) picked wrong runtime {resolved}",
            )
            self.assertEqual(
                resolved["runtime_id"],
                "com.apple.CoreSimulator.SimRuntime.iOS-27-0",
            )
            self.assertEqual(resolved["device_type_name"], wanted_sim)
            self.assertIsNotNone(resolved["fallback_reason"])  # iOS 18 -> 27.0 noted

    def test_iphone_duo_explicit_picks_ios_27_1(self) -> None:
        fixture = _host_simctl_fixture()
        resolved = resolve_sim_target("iPhone Duo", 27, fixture)
        self.assertEqual(resolved["runtime_version"], "27.1")
        self.assertEqual(resolved["device_type_name"], "iPhone Duo")
        self.assertIsNone(resolved["fallback_reason"])

    def test_legacy_device_fallback_avoids_iphone_duo(self) -> None:
        fixture = _host_simctl_fixture()

        # Legacy small SE -> must fall back to iPhone SE (3rd generation) on iOS 27.0
        se1 = resolve_sim_target("iPhone SE (1st generation)", 18, fixture)
        self.assertEqual(se1["runtime_version"], "27.0")
        self.assertEqual(se1["device_type_name"], "iPhone SE (3rd generation)")
        self.assertIn("unavailable", se1["fallback_reason"] or "")

        # Legacy iPhone 8 -> standard iPhone on iOS 27.0, NOT iPhone Duo on 27.1
        ip8 = resolve_sim_target("iPhone 8", 18, fixture)
        self.assertEqual(ip8["runtime_version"], "27.0")
        self.assertNotIn("Duo", ip8["device_type_name"])
        self.assertTrue(ip8["device_type_name"].startswith("iPhone"))

        # Legacy iPad mini 5th gen -> iPad mini (A17 Pro) on iOS 27.0
        mini = resolve_sim_target("iPad mini (5th generation)", 18, fixture)
        self.assertEqual(mini["runtime_version"], "27.0")
        self.assertIn("iPad mini", mini["device_type_name"])

        # Legacy iPad Air 3rd gen -> iPad Air on iOS 27.0
        air = resolve_sim_target("iPad Air (3rd generation)", 18, fixture)
        self.assertEqual(air["runtime_version"], "27.0")
        self.assertIn("iPad Air", air["device_type_name"])

    def test_exact_major_match_preferred_over_newer(self) -> None:
        fixture = _host_simctl_fixture()
        fixture["runtimes"].append(
            {
                "name": "iOS 18.4",
                "version": "18.4",
                "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-18-4",
                "isAvailable": True,
                "supportedDeviceTypes": [_dt_entry("iPhone 16 Pro")],
            }
        )
        resolved = resolve_sim_target("iPhone 16 Pro", 18, fixture)
        self.assertEqual(resolved["runtime_version"], "18.4")
        self.assertIsNone(resolved["fallback_reason"])

    def test_no_available_runtimes_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            resolve_sim_target("iPhone 16", 18, {"runtimes": [], "devicetypes": []})

    def test_locale_and_keyboard_commands(self) -> None:
        cmds = build_locale_keyboard_commands("UDID-123", "ar-SA")
        joined = [" ".join(c) for c in cmds]
        self.assertTrue(any("AppleLanguages -array ar-SA" in s for s in joined))
        self.assertTrue(any("AppleLocale -string ar_SA" in s for s in joined))
        self.assertTrue(
            any("AppleKeyboards -array en_US@sw=QWERTY;hw=US" in s for s in joined)
        )
        self.assertTrue(any("KeyboardAutocorrection -bool NO" in s for s in joined))
        self.assertTrue(
            any(
                "com.apple.Preferences DidShowContinuousPathIntroduction -bool YES" in s
                for s in joined
            )
        )
        self.assertTrue(
            any(
                "com.apple.keyboard.preferences DidShowGestureKeyboardIntroduction -bool YES"
                in s
                for s in joined
            )
        )

    def test_extract_bundle_id_xml_and_binary_plist(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            for fmt in (plistlib.FMT_XML, plistlib.FMT_BINARY):
                app = Path(td) / f"Demo_{fmt.name}.app"
                app.mkdir()
                with (app / "Info.plist").open("wb") as fh:
                    plistlib.dump({"CFBundleIdentifier": "com.example.demo"}, fh, fmt=fmt)
                self.assertEqual(extract_bundle_id(app), "com.example.demo")

    def test_provision_persona_sim_command_order(self) -> None:
        fixture = _host_simctl_fixture()
        persona = load_personas()[4]  # amira (ar-SA, iPhone 16 Pro, light, large)
        recorded: list[list[str]] = []

        async def fake_subproc(args: list[str], *, timeout: float = 30.0):
            recorded.append(args)
            if args[:3] == ["xcrun", "simctl", "create"]:
                return 0, "FAKE-UDID-0001\n", ""
            if "launchctl" in args:
                return 0, "123 0 com.example.demo\n", ""
            return 0, "", ""

        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "Demo.app"
            app.mkdir()
            with (app / "Info.plist").open("wb") as fh:
                plistlib.dump({"CFBundleIdentifier": "com.example.demo"}, fh)

            with (
                patch(
                    "ios_agent_testers.core.sim_provisioner.list_available_runtimes_and_devices",
                    return_value=fixture,
                ),
                patch(
                    "ios_agent_testers.core.sim_provisioner._run_subproc",
                    new=AsyncMock(side_effect=fake_subproc),
                ),
                patch("asyncio.sleep", new=AsyncMock()),
            ):
                result = asyncio.run(
                    provision_persona_sim(
                        "20261123-run01",
                        persona,
                        app_path=str(app),
                    )
                )

        self.assertEqual(result["udid"], "FAKE-UDID-0001")
        self.assertEqual(result["runtime_version"], "27.0")
        self.assertEqual(result["bundle_id"], "com.example.demo")
        self.assertTrue(result["ephemeral"])

        # Verify critical sequence milestones
        verbs = [" ".join(c[:3]) for c in recorded]
        self.assertEqual(verbs[0], "xcrun simctl create")
        self.assertIn("com.apple.CoreSimulator.SimRuntime.iOS-27-0", recorded[0])
        self.assertTrue(recorded[0][3].startswith("qa-"))

        boot_indices = [i for i, c in enumerate(recorded) if c[:3] == ["xcrun", "simctl", "boot"]]
        shutdown_indices = [
            i for i, c in enumerate(recorded) if c[:3] == ["xcrun", "simctl", "shutdown"]
        ]
        defaults_indices = [
            i for i, c in enumerate(recorded) if "AppleLanguages" in c
        ]
        ui_indices = [i for i, c in enumerate(recorded) if c[:3] == ["xcrun", "simctl", "ui"]]
        install_indices = [
            i for i, c in enumerate(recorded) if c[:3] == ["xcrun", "simctl", "install"]
        ]
        launch_indices = [
            i for i, c in enumerate(recorded) if c[:3] == ["xcrun", "simctl", "launch"]
        ]

        # Boot #1 < defaults < shutdown < Boot #2 < ui settings < install < launch
        self.assertGreaterEqual(len(boot_indices), 2)
        self.assertTrue(
            boot_indices[0]
            < defaults_indices[0]
            < shutdown_indices[0]
            < boot_indices[1]
            < ui_indices[0]
            < install_indices[0]
            < launch_indices[0]
        )

    def test_teardown_refuses_to_delete_non_qa_simulator(self) -> None:
        called: list[list[str]] = []

        async def fake_subproc(args: list[str], *, timeout: float = 30.0):
            called.append(args)
            return 0, "", ""

        with (
            patch(
                "ios_agent_testers.core.sim_provisioner._lookup_simulator_name",
                new=AsyncMock(return_value="Felix Personal iPhone"),
            ),
            patch(
                "ios_agent_testers.core.sim_provisioner._run_subproc",
                new=AsyncMock(side_effect=fake_subproc),
            ),
        ):
            res = asyncio.run(
                teardown_persona_sim("PERSONAL-UDID", ephemeral=True, delete_simulator=True)
            )

        self.assertFalse(res["deleted"])
        self.assertFalse(any(c[:3] == ["xcrun", "simctl", "delete"] for c in called))

    def test_doctor_check_contract_shape(self) -> None:
        report = doctor_check()
        self.assertIn("ok", report)
        self.assertIn("checks", report)
        self.assertIn("runtimes", report)
        self.assertIn("warnings", report)
        self.assertIsInstance(report["checks"], dict)
        self.assertIsInstance(report["runtimes"], list)
        self.assertIsInstance(report["warnings"], list)

    def test_idb_driver_permission_grant_bool_mapping(self) -> None:
        from types import SimpleNamespace
        from idb.common.types import Permission
        from ios_agent_testers.core.idb_driver import IDBDriver

        driver = IDBDriver()
        fake_client = SimpleNamespace(
            approve=AsyncMock(),
            revoke=AsyncMock(),
        )
        ctrl = driver._get_control("UDID-PERM")

        async def fake_run(fn, *, timeout=15.0, retry_on_disconnect=False, **_kwargs):
            return await fn(fake_client)

        ctrl.run = fake_run  # type: ignore[method-assign]

        # grant=False must revoke, NOT silently grant
        res_revoke = asyncio.run(
            driver.act(
                "UDID-PERM",
                {"kind": "permission", "service": "location", "grant": False},
                bundle_id="com.example.app",
            )
        )
        self.assertTrue(res_revoke["ok"])
        self.assertIn("revoke", res_revoke["detail"])
        fake_client.revoke.assert_awaited_once_with("com.example.app", {Permission.LOCATION})
        fake_client.approve.assert_not_awaited()

        # grant=True must approve
        res_grant = asyncio.run(
            driver.act(
                "UDID-PERM",
                {"kind": "permission", "service": "location", "grant": True},
                bundle_id="com.example.app",
            )
        )
        self.assertTrue(res_grant["ok"])
        self.assertIn("grant", res_grant["detail"])
        fake_client.approve.assert_awaited_once_with("com.example.app", {Permission.LOCATION})

    def test_extract_executable_name_differs_from_bundle_id_tail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            for fmt in (plistlib.FMT_XML, plistlib.FMT_BINARY):
                app = Path(td) / f"DayTrace_{fmt.name}.app"
                app.mkdir()
                with (app / "Info.plist").open("wb") as fh:
                    plistlib.dump(
                        {
                            "CFBundleIdentifier": "app.daytrace.recorder",
                            "CFBundleExecutable": "DayTrace",
                        },
                        fh,
                        fmt=fmt,
                    )
                self.assertEqual(extract_executable_name(app), "DayTrace")

            # Verify plistlib fallback when PlistBuddy is absent
            app_xml = Path(td) / "DayTrace_FMT_XML.app"
            with patch(
                "ios_agent_testers.core.sim_provisioner.PLISTBUDDY_BIN",
                Path("/nonexistent/PlistBuddy"),
            ):
                self.assertEqual(extract_executable_name(app_xml), "DayTrace")

            # Verify stem fallback when CFBundleExecutable is absent
            stem_app = Path(td) / "FallbackApp.app"
            stem_app.mkdir()
            with (stem_app / "Info.plist").open("wb") as fh:
                plistlib.dump({"CFBundleIdentifier": "com.example.other"}, fh)
            self.assertEqual(extract_executable_name(stem_app), "FallbackApp")

    def test_resolve_executable_name_via_get_app_container(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "Preferences.app"
            app.mkdir()
            with (app / "Info.plist").open("wb") as fh:
                plistlib.dump(
                    {
                        "CFBundleIdentifier": "com.apple.Preferences",
                        "CFBundleExecutable": "Preferences",
                    },
                    fh,
                )

            async def fake_subproc(args: list[str], *, timeout: float = 30.0):
                self.assertEqual(
                    args,
                    ["xcrun", "simctl", "get_app_container", "UDID-SYS", "com.apple.Preferences", "app"],
                )
                return 0, f"{app}\n", ""

            with patch(
                "ios_agent_testers.core.sim_provisioner._run_subproc",
                new=AsyncMock(side_effect=fake_subproc),
            ):
                exe = asyncio.run(resolve_executable_name("UDID-SYS", "com.apple.Preferences"))
            self.assertEqual(exe, "Preferences")

    def test_provision_parallel_defaults_timeout_resilience_and_critical_retry(self) -> None:
        fixture = _host_simctl_fixture()
        persona = load_personas()[0]
        in_flight = 0
        max_in_flight = 0
        defaults_timeouts: list[float] = []
        cmd_attempts: dict[str, int] = {}
        recorded: list[list[str]] = []

        async def fake_subproc(args: list[str], *, timeout: float = 30.0):
            nonlocal in_flight, max_in_flight
            recorded.append(args)
            if args[:3] == ["xcrun", "simctl", "create"]:
                return 0, "FAKE-UDID-PAR\n", ""
            if "defaults" in args and "write" in args:
                self.assertNotIn("/bin/sh", args)
                defaults_timeouts.append(timeout)
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                await asyncio.sleep(0.01)
                in_flight -= 1
                key = args[args.index("write") + 2]
                cmd_attempts[key] = cmd_attempts.get(key, 0) + 1
                # Critical keys time out once and succeed on retry
                if key in ("AppleLanguages", "AppleLocale") and cmd_attempts[key] == 1:
                    raise TimeoutError(f"timed out writing {key}")
                # Non-critical tutorial key times out permanently -> must stay non-fatal
                if key == "DidShowContinuousPathIntroduction":
                    raise TimeoutError("non-critical timeout")
                return 0, "", ""
            if "launchctl" in args:
                # launchctl poll times out -> must remain non-fatal!
                raise TimeoutError("launchctl stalled")
            return 0, "", ""

        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "DayTrace.app"
            app.mkdir()
            with (app / "Info.plist").open("wb") as fh:
                plistlib.dump(
                    {
                        "CFBundleIdentifier": "app.daytrace.recorder",
                        "CFBundleExecutable": "DayTrace",
                    },
                    fh,
                )

            real_sleep = asyncio.sleep

            async def fast_sleep(delay: float) -> None:
                await real_sleep(0.005 if delay <= 0.02 else 0)

            with (
                patch(
                    "ios_agent_testers.core.sim_provisioner.list_available_runtimes_and_devices",
                    return_value=fixture,
                ),
                patch(
                    "ios_agent_testers.core.sim_provisioner._run_subproc",
                    new=AsyncMock(side_effect=fake_subproc),
                ),
                patch("asyncio.sleep", side_effect=fast_sleep),
            ):
                result = asyncio.run(
                    provision_persona_sim(
                        "20261123-par01",
                        persona,
                        app_path=str(app),
                    )
                )

        # Provisioning must succeed without deleting the simulator
        self.assertEqual(result["udid"], "FAKE-UDID-PAR")
        self.assertEqual(result["bundle_id"], "app.daytrace.recorder")
        self.assertEqual(result["executable_name"], "DayTrace")
        self.assertFalse(any(c[:3] == ["xcrun", "simctl", "delete"] for c in recorded))

        # Bounded concurrency: >1 concurrent defaults writes and <=6
        self.assertGreater(max_in_flight, 1)
        self.assertLessEqual(max_in_flight, 6)
        self.assertTrue(all(t == 30.0 for t in defaults_timeouts))

        # Critical keys retried once (2 attempts each); non-critical NOT retried
        self.assertEqual(cmd_attempts["AppleLanguages"], 2)
        self.assertEqual(cmd_attempts["AppleLocale"], 2)
        # DidShowContinuousPathIntroduction appears in 2 domains -> 1 attempt per domain = 2 total
        self.assertEqual(cmd_attempts["DidShowContinuousPathIntroduction"], 2)

    def test_provision_initial_launch_timeout_is_40s_and_system_app_resolves_exec(self) -> None:
        fixture = _host_simctl_fixture()
        persona = load_personas()[0]
        launch_timeouts: list[float] = []

        with tempfile.TemporaryDirectory() as td:
            sys_app = Path(td) / "Preferences.app"
            sys_app.mkdir()
            with (sys_app / "Info.plist").open("wb") as fh:
                plistlib.dump(
                    {
                        "CFBundleIdentifier": "com.apple.Preferences",
                        "CFBundleExecutable": "Preferences",
                    },
                    fh,
                )

            async def fake_subproc(args: list[str], *, timeout: float = 30.0):
                if args[:3] == ["xcrun", "simctl", "create"]:
                    return 0, "FAKE-UDID-SYS\n", ""
                if args[:3] == ["xcrun", "simctl", "get_app_container"]:
                    return 0, f"{sys_app}\n", ""
                if args[:3] == ["xcrun", "simctl", "launch"]:
                    launch_timeouts.append(timeout)
                    return 0, "", ""
                if "launchctl" in args:
                    return 0, "42 0 com.apple.Preferences\n", ""
                return 0, "", ""

            with (
                patch(
                    "ios_agent_testers.core.sim_provisioner.list_available_runtimes_and_devices",
                    return_value=fixture,
                ),
                patch(
                    "ios_agent_testers.core.sim_provisioner._run_subproc",
                    new=AsyncMock(side_effect=fake_subproc),
                ),
                patch("asyncio.sleep", new=AsyncMock()),
            ):
                result = asyncio.run(
                    provision_persona_sim(
                        "20261123-sys01",
                        persona,
                        bundle_id="com.apple.Preferences",
                    )
                )

        self.assertEqual(launch_timeouts, [40.0])
        self.assertEqual(result["executable_name"], "Preferences")


if __name__ == "__main__":
    unittest.main()
