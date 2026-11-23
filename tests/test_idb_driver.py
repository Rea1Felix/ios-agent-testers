"""Unit tests for ios_agent_testers.core.idb_driver (v0.2 hardening regressions)."""

from __future__ import annotations

import asyncio
import json
import os
import struct
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from idb.common.types import (
    AccessibilityBackend,
    AccessibilityOutputFormat,
    AppProcessState,
    IdbException,
)

from ios_agent_testers.core.idb_driver import (
    IDBDriver,
    _build_log_predicate,
    _resolve_ax_backend,
)


def _fake_png(width: int = 402, height: int = 874) -> bytes:
    """Build a minimal valid PNG header with IHDR dimensions."""
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"


class TestIDBDriver(unittest.TestCase):
    def test_control_client_never_retries_timeout_and_formats_message(self) -> None:
        driver = IDBDriver()
        ctrl = driver._get_control("11111111-2222-3333-4444-555555555555")
        fake_client = SimpleNamespace()
        calls = 0

        async def fake_ensure() -> SimpleNamespace:
            ctrl._client = fake_client  # type: ignore[assignment]
            return fake_client

        ctrl._ensure_connected_unlocked = fake_ensure  # type: ignore[method-assign]

        async def slow_rpc(_client: SimpleNamespace) -> str:
            nonlocal calls
            calls += 1
            await asyncio.sleep(5.0)
            return "never"

        with self.assertRaises(TimeoutError) as ctx:
            asyncio.run(
                ctrl.run(
                    slow_rpc,
                    timeout=0.02,
                    retry_on_disconnect=True,
                    op_name="accessibility_info",
                )
            )

        # Must NEVER retry on TimeoutError and must produce descriptive non-empty message
        self.assertEqual(calls, 1)
        msg = str(ctx.exception)
        self.assertTrue(msg)
        self.assertIn("accessibility_info", msg)
        self.assertIn("11111111", msg)
        self.assertIsNone(ctrl._client)

    def test_control_client_retries_disconnect_once_but_not_app_error(self) -> None:
        driver = IDBDriver()
        ctrl = driver._get_control("22222222-3333-4444-5555-666666666666")
        fake_client = SimpleNamespace()

        async def fake_ensure() -> SimpleNamespace:
            ctrl._client = fake_client  # type: ignore[assignment]
            return fake_client

        ctrl._ensure_connected_unlocked = fake_ensure  # type: ignore[method-assign]

        # 1. Genuine transport disconnect retries once and succeeds
        disc_calls = 0

        async def flaky_disconnect(_client: SimpleNamespace) -> str:
            nonlocal disc_calls
            disc_calls += 1
            if disc_calls == 1:
                raise ConnectionResetError("socket dropped")
            return "recovered"

        with patch("asyncio.sleep", new=AsyncMock()):
            res = asyncio.run(
                ctrl.run(
                    flaky_disconnect,
                    timeout=5.0,
                    retry_on_disconnect=True,
                    op_name="describe",
                )
            )
        self.assertEqual(res, "recovered")
        self.assertEqual(disc_calls, 2)

        # 2. Application-level IdbException does NOT retry inside _ControlClient.run
        app_calls = 0

        async def app_err(_client: SimpleNamespace) -> str:
            nonlocal app_calls
            app_calls += 1
            raise IdbException("no frontmost application")

        with self.assertRaises(IdbException):
            asyncio.run(
                ctrl.run(
                    app_err,
                    timeout=5.0,
                    retry_on_disconnect=True,
                    op_name="accessibility_info",
                )
            )
        self.assertEqual(app_calls, 1)

    def test_ensure_connected_timeout_raises_descriptive_error(self) -> None:
        driver = IDBDriver()
        udid = "33333333-4444-5555-6666-777777777777"
        ctrl = driver._get_control(udid)

        class HangingCM:
            async def __aenter__(self):
                raise asyncio.TimeoutError()

            async def __aexit__(self, *args):
                return None

        with (
            patch.object(driver, "ensure_companion", new=AsyncMock(return_value=Path("/tmp/fake.sock"))),
            patch("ios_agent_testers.core.idb_driver.Client.build", return_value=HangingCM()),
        ):
            with self.assertRaises(TimeoutError) as ctx:
                asyncio.run(ctrl._ensure_connected_unlocked())

        self.assertIn("idb connect timed out", str(ctx.exception))
        self.assertIn("33333333", str(ctx.exception))

    def test_describe_screen_fast_path_cache_and_rotate_invalidation(self) -> None:
        driver = IDBDriver()
        udid = "44444444-5555-6666-7777-888888888888"
        ctrl = driver._get_control(udid)

        describe_timeouts: list[float] = []
        fake_desc = SimpleNamespace(
            screen_dimensions=SimpleNamespace(
                width=1206,
                height=2622,
                width_points=402,
                height_points=874,
                density=3.0,
            )
        )
        fake_client = SimpleNamespace(
            describe=AsyncMock(return_value=fake_desc),
            set_orientation=AsyncMock(),
        )

        async def fake_run(fn, *, timeout=15.0, retry_on_disconnect=False, op_name="rpc"):
            if op_name == "describe":
                describe_timeouts.append(timeout)
            return await fn(fake_client)

        ctrl.run = fake_run  # type: ignore[method-assign]

        first = asyncio.run(driver.describe_screen(udid))
        second = asyncio.run(driver.describe_screen(udid))

        self.assertEqual(first["width_points"], 402)
        self.assertEqual(second["height_points"], 874)
        # Second call hit cache -> describe() called only ONCE, with timeout=6.0
        self.assertEqual(fake_client.describe.await_count, 1)
        self.assertEqual(describe_timeouts, [6.0])

        # Rotate invalidates cache so next describe_screen queries again
        asyncio.run(driver.act(udid, {"kind": "rotate", "orientation": "landscape_left"}))
        asyncio.run(driver.describe_screen(udid))
        self.assertEqual(fake_client.describe.await_count, 2)

    def test_fetch_ax_tree_circuit_breaks_on_timeout_and_retries_no_frontmost(self) -> None:
        driver = IDBDriver()
        udid = "55555555-6666-7777-8888-999999999999"
        ctrl = driver._get_control(udid)

        # 1. TimeoutError -> circuit-break immediately after 1 call (NO nested=False fallback)
        timeout_calls = 0

        async def run_timeout(fn, *, timeout=15.0, retry_on_disconnect=False, op_name="rpc"):
            nonlocal timeout_calls
            timeout_calls += 1
            self.assertEqual(timeout, 8.0)
            raise TimeoutError(f"idb {op_name} timed out after {timeout:.1f}s for {udid[:8]}")

        ctrl.run = run_timeout  # type: ignore[method-assign]
        tree, reason = asyncio.run(driver._fetch_ax_tree(udid))
        self.assertEqual(tree, [])
        self.assertEqual(timeout_calls, 1)
        self.assertIsNotNone(reason)
        self.assertIn("timed out", reason or "")

        # 2. Transient "no frontmost application" retries ONCE after 0.5s and succeeds
        nf_calls = 0
        seen_opts: list[object] = []

        async def fake_a11y(_target, opts):
            seen_opts.append(opts)
            nonlocal nf_calls
            nf_calls += 1
            if nf_calls == 1:
                raise IdbException(
                    "The simulator's accessibility service named no frontmost application"
                )
            return SimpleNamespace(
                json=json.dumps(
                    [{"type": "Button", "AXLabel": "OK", "frame": {"x": 20, "y": 20, "width": 60, "height": 44}}]
                )
            )

        fake_client = SimpleNamespace(accessibility_info=fake_a11y)

        async def run_ok(fn, *, timeout=15.0, retry_on_disconnect=False, op_name="rpc"):
            return await fn(fake_client)

        ctrl.run = run_ok  # type: ignore[method-assign]
        with patch("asyncio.sleep", new=AsyncMock()) as sleep_mock:
            tree2, reason2 = asyncio.run(driver._fetch_ax_tree(udid))
        self.assertIsNone(reason2)
        self.assertEqual(len(tree2), 1)
        self.assertEqual(nf_calls, 2)
        sleep_mock.assert_awaited_once_with(0.5)
        self.assertEqual(seen_opts[0].backend, AccessibilityBackend.AX)
        self.assertEqual(seen_opts[0].format, AccessibilityOutputFormat.NESTED)

    def test_ax_backend_env_override(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IOS_AGENT_TESTERS_AX_BACKEND", None)
            self.assertEqual(_resolve_ax_backend(), AccessibilityBackend.AX)
        with patch.dict(os.environ, {"IOS_AGENT_TESTERS_AX_BACKEND": "ax_coresimulator"}):
            self.assertEqual(_resolve_ax_backend(), AccessibilityBackend.AX_CORESIMULATOR)
        with patch.dict(os.environ, {"IOS_AGENT_TESTERS_AX_BACKEND": "axbridge_persistent"}):
            self.assertEqual(_resolve_ax_backend(), AccessibilityBackend.AXBRIDGE_PERSISTENT)

    def test_observe_targeted_axbridge_grafting_and_clean_a11y(self) -> None:
        driver = IDBDriver()
        udid = "66666666-7777-8888-9999-aaaaaaaaaaaa"
        driver._dimensions_cache[driver._key(udid)] = {
            "width": 1206,
            "height": 2622,
            "width_points": 402,
            "height_points": 874,
            "density": 3.0,
        }
        ctrl = driver._get_control(udid)
        ax_calls: list[tuple[AccessibilityBackend | None, float, bool]] = []

        # Scenario A: Normal screen (2+ elements, no opaque containers) -> NO AXBRIDGE query
        normal_ax = [
            {"type": "Button", "AXLabel": "Back", "frame": {"x": 16, "y": 62, "width": 44, "height": 44}},
            {"type": "StaticText", "AXLabel": "Settings", "frame": {"x": 150, "y": 70, "width": 100, "height": 24}},
        ]

        # Scenario B: DayTrace Appearance screen with childless unlabeled TabGroup
        opaque_ax = [
            {"type": "Button", "AXLabel": "設定", "frame": {"x": 16, "y": 62, "width": 44, "height": 44}},
            {"type": "StaticText", "AXLabel": "外觀", "frame": {"x": 160, "y": 72, "width": 80, "height": 24}},
            {
                "type": "TabGroup",
                "role": "AXTabGroup",
                "AXLabel": "",
                "frame": {"x": 32, "y": 171.33, "width": 338, "height": 31},
                "enabled": True,
            },
        ]
        bridge_tree = [
            # Occluded undersized/unnamed noise outside box -> MUST NOT pollute elements OR a11y!
            {"type": "Button", "AXLabel": "", "frame": {"x": 10, "y": 600, "width": 12, "height": 12}},
            {"type": "Button", "AXLabel": "開始錄音", "frame": {"x": 120, "y": 700, "width": 120, "height": 48}},
            {
                "type": "SegmentedControl",
                "frame": {"x": 32, "y": 171.33, "width": 338, "height": 31},
                "children": [
                    {"type": "Button", "AXLabel": "深色", "frame": {"x": 32, "y": 171.33, "width": 112, "height": 32}},
                    {"type": "Button", "AXLabel": "淺色", "frame": {"x": 144, "y": 171.33, "width": 113, "height": 32}},
                    {"type": "Button", "AXLabel": "跟隨系統", "frame": {"x": 257, "y": 171.33, "width": 113, "height": 32}},
                ],
            },
        ]

        current_ax_payload: list[dict] = normal_ax
        fail_ax_timeout = False

        async def fake_a11y(_target, opts):
            if fail_ax_timeout and opts.backend == AccessibilityBackend.AX:
                raise TimeoutError("idb accessibility_info timed out after 8.0s")
            if opts.backend == AccessibilityBackend.AXBRIDGE_PERSISTENT:
                return SimpleNamespace(json=json.dumps(bridge_tree))
            return SimpleNamespace(json=json.dumps(current_ax_payload))

        fake_client = SimpleNamespace(
            screenshot=AsyncMock(return_value=_fake_png(402, 874)),
            accessibility_info=fake_a11y,
            list_apps=AsyncMock(
                return_value=[
                    SimpleNamespace(
                        bundle_id="app.daytrace.recorder",
                        process_state=AppProcessState.RUNNING,
                    )
                ]
            ),
            crash_list=AsyncMock(return_value=[]),
        )

        async def fake_run(fn, *, timeout=15.0, retry_on_disconnect=False, op_name="rpc"):
            if "accessibility_info" in op_name:
                # Inspect options passed into accessibility_info
                orig_a11y = fake_client.accessibility_info

                async def capture_a11y(target, opts):
                    ax_calls.append((opts.backend, timeout, retry_on_disconnect))
                    return await orig_a11y(target, opts)

                fake_client.accessibility_info = capture_a11y
                try:
                    return await fn(fake_client)
                finally:
                    fake_client.accessibility_info = orig_a11y
            return await fn(fake_client)

        ctrl.run = fake_run  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as td:
            # A. Normal screen -> 1 AX call, 0 AXBRIDGE calls
            obs_a = asyncio.run(
                driver.observe(
                    udid,
                    bundle_id="app.daytrace.recorder",
                    output_png=Path(td) / "01.png",
                    run_start_ts=time.time(),
                )
            )
            self.assertIsNone(obs_a["ax_unavailable_reason"])
            self.assertEqual(len(ax_calls), 1)
            self.assertEqual(ax_calls[0][0], AccessibilityBackend.AX)

            # B. Opaque TabGroup screen -> AX + 1 targeted AXBRIDGE_PERSISTENT (timeout=6.0, no retry)
            ax_calls.clear()
            current_ax_payload = opaque_ax
            obs_b = asyncio.run(
                driver.observe(
                    udid,
                    bundle_id="app.daytrace.recorder",
                    output_png=Path(td) / "02.png",
                    run_start_ts=time.time(),
                )
            )
            self.assertIsNone(obs_b["ax_unavailable_reason"])
            self.assertEqual(len(ax_calls), 2)
            self.assertEqual(ax_calls[1], (AccessibilityBackend.AXBRIDGE_PERSISTENT, 6.0, False))
            labels_b = [e["label"] for e in obs_b["elements"]]
            self.assertIn("深色", labels_b)
            self.assertIn("淺色", labels_b)
            self.assertIn("跟隨系統", labels_b)
            self.assertNotIn("開始錄音", labels_b)
            # Clean AX a11y audit: 0 false positives!
            self.assertEqual(obs_b["a11y"]["unnamed"], [])
            self.assertEqual(obs_b["a11y"]["undersized"], [])
            self.assertIsNone(obs_b["a11y_line"])

            # C. Primary AX timeout -> NO AXBRIDGE query, ax_unavailable_reason populated
            ax_calls.clear()
            fail_ax_timeout = True
            obs_c = asyncio.run(
                driver.observe(
                    udid,
                    bundle_id="app.daytrace.recorder",
                    output_png=Path(td) / "03.png",
                    run_start_ts=time.time(),
                )
            )
            self.assertEqual(len(ax_calls), 1)
            self.assertIsNotNone(obs_c["ax_unavailable_reason"])
            self.assertIn("timed out", obs_c["ax_unavailable_reason"])

            # D. Primary AX succeeds with < 2 controls -> triggers AXBRIDGE_PERSISTENT fallback
            ax_calls.clear()
            fail_ax_timeout = False
            current_ax_payload = [
                {"type": "StaticText", "AXLabel": "OnlyOne", "frame": {"x": 20, "y": 40, "width": 100, "height": 24}}
            ]
            obs_d = asyncio.run(
                driver.observe(
                    udid,
                    bundle_id="app.daytrace.recorder",
                    output_png=Path(td) / "04.png",
                    run_start_ts=time.time(),
                )
            )
            self.assertIsNone(obs_d["ax_unavailable_reason"])
            self.assertEqual(len(ax_calls), 2)
            self.assertEqual(ax_calls[1], (AccessibilityBackend.AXBRIDGE_PERSISTENT, 6.0, False))
            self.assertIn("開始錄音", [e["label"] for e in obs_d["elements"]])
            # audit_ios remains strictly on primary AX tree (0 false positives)
            self.assertEqual(obs_d["a11y"]["unnamed"], [])
            self.assertEqual(obs_d["a11y"]["undersized"], [])

    def test_collect_crashes_single_call_and_matches_process_name(self) -> None:
        driver = IDBDriver()
        udid = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"
        key = driver._key(udid)
        driver._process_names[key] = "DayTrace"
        ctrl = driver._get_control(udid)

        crash_list_calls: list[tuple[object, float]] = []
        fake_crashes = [
            SimpleNamespace(
                name="DayTrace-2026-11-23.ips",
                bundle_id=None,
                process_name="DayTrace",
                timestamp=int(time.time()),
            ),
            SimpleNamespace(
                name="SpringBoard-2026-11-23.ips",
                bundle_id="com.apple.springboard",
                process_name="SpringBoard",
                timestamp=int(time.time()),
            ),
        ]
        fake_client = SimpleNamespace(
            crash_list=AsyncMock(return_value=fake_crashes),
            crash_show=AsyncMock(return_value=SimpleNamespace(contents="EXC_BAD_ACCESS DayTrace")),
        )

        async def fake_run(fn, *, timeout=15.0, retry_on_disconnect=False, op_name="rpc"):
            if op_name == "crash_list":
                crash_list_calls.append((op_name, timeout))
            return await fn(fake_client)

        ctrl.run = fake_run  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as td:
            diag_dir = Path(td) / "DiagnosticReports"
            diag_dir.mkdir()
            crashes_out = Path(td) / "crashes"
            ips_target = diag_dir / "DayTrace-local.ips"
            ips_target.write_text('{"procName": "DayTrace"}', encoding="utf-8")
            ips_other = diag_dir / "WindowServer-local.ips"
            ips_other.write_text('{"procName": "WindowServer"}', encoding="utf-8")

            with patch("ios_agent_testers.core.idb_driver.DIAGNOSTIC_REPORTS_DIR", diag_dir):
                found = asyncio.run(
                    driver._collect_crashes(
                        udid,
                        bundle_id="app.daytrace.recorder",
                        run_start_ts=time.time() - 10.0,
                        crashes_dir=crashes_out,
                    )
                )

        # Exactly ONE crash_list call with timeout=5.0
        self.assertEqual(len(crash_list_calls), 1)
        self.assertEqual(crash_list_calls[0][1], 5.0)
        # Matched DayTrace from both idb and local .ips; excluded SpringBoard & WindowServer
        names = [c["name"] for c in found]
        self.assertEqual(names, ["DayTrace-2026-11-23.ips", "DayTrace-local.ips"])

        # close_udid clears stored process_name
        asyncio.run(driver.close_udid(udid))
        self.assertNotIn(key, driver._process_names)

    def test_start_log_tail_predicate_escaping_and_banner_stripping(self) -> None:
        pred = _build_log_predicate("app.daytrace.recorder", "DayTrace")
        assert pred is not None
        self.assertIn('process == "DayTrace"', pred)
        self.assertIn('subsystem == "app.daytrace.recorder"', pred)

        escaped = _build_log_predicate('com.ex."bad"', 'My"App\\1')
        assert escaped is not None
        self.assertIn(r'process == "My\"App\\1"', escaped)
        self.assertIn(r'subsystem == "com.ex.\"bad\""', escaped)

        driver = IDBDriver()
        udid = "88888888-9999-aaaa-bbbb-cccccccccccc"
        captured_args: list[list[str]] = []

        class FakeTailClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def tail_logs(self, *, stop, arguments=None):
                captured_args.append(list(arguments or []))
                yield 'Filtering the log data using "process == \\"DayTrace\\""\n'
                yield "Timestamp                       Ty Process[PID:TID]\n"
                yield "2026-11-24 01:05:29.790 Df DayTrace[41215:1cc567] Audio engine started\n"
                yield "2026-11-24 01:05:30.001 E  DayTrace[41215:1cc567] Error: mic denied\n"

        with tempfile.TemporaryDirectory() as td:
            log_file = Path(td) / "logs.txt"

            async def _run_tail() -> None:
                with (
                    patch.object(
                        driver,
                        "ensure_companion",
                        new=AsyncMock(return_value=Path("/tmp/fake.sock")),
                    ),
                    patch(
                        "ios_agent_testers.core.idb_driver.Client.build",
                        return_value=FakeTailClient(),
                    ),
                ):
                    await driver.start_log_tail(
                        udid,
                        log_file,
                        bundle_id="app.daytrace.recorder",
                        process_name="DayTrace",
                    )
                    # Wait for background worker to finish yielding chunks
                    _, task = driver._log_tasks[driver._key(udid)]
                    await asyncio.wait_for(task, timeout=2.0)

            asyncio.run(_run_tail())

            self.assertEqual(driver._process_names[driver._key(udid)], "DayTrace")
            self.assertEqual(len(captured_args), 1)
            self.assertIn("--predicate", captured_args[0])
            pred_val = captured_args[0][captured_args[0].index("--predicate") + 1]
            self.assertIn('process == "DayTrace"', pred_val)
            self.assertIn('subsystem == "app.daytrace.recorder"', pred_val)

            written = log_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(written), 2)
            self.assertTrue(all("Filtering the log data" not in ln for ln in written))
            self.assertTrue(all(not ln.startswith("Timestamp ") for ln in written))
            self.assertIn("Audio engine started", written[0])
            self.assertIn("Error: mic denied", written[1])

    def test_relaunch_and_open_timeouts_and_single_terminate_path(self) -> None:
        driver = IDBDriver()
        udid = "99999999-aaaa-bbbb-cccc-dddddddddddd"
        ctrl = driver._get_control(udid)

        run_records: list[tuple[str, float]] = []
        subproc_records: list[tuple[list[str], float]] = []
        sleep_durations: list[float] = []

        terminate_should_fail = False
        launch_should_fail = True

        async def fake_terminate(_bid: str) -> None:
            if terminate_should_fail:
                raise RuntimeError("idb terminate failed")

        async def fake_launch(_bid: str, **_kw) -> None:
            if launch_should_fail:
                raise TimeoutError("idb launch timed out")

        fake_client = SimpleNamespace(terminate=fake_terminate, launch=fake_launch)

        async def fake_run(fn, *, timeout=15.0, retry_on_disconnect=False, op_name="rpc"):
            run_records.append((op_name, timeout))
            return await fn(fake_client)

        async def fake_subproc(args: list[str], *, timeout: float = 15.0, stdin_bytes=None):
            subproc_records.append((args, timeout))
            return 0, b"", b""

        async def fake_sleep(delay: float) -> None:
            sleep_durations.append(delay)

        ctrl.run = fake_run  # type: ignore[method-assign]

        with (
            patch("ios_agent_testers.core.idb_driver._run_subproc", side_effect=fake_subproc),
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            # Case 1: c.terminate succeeds -> MUST NOT call simctl terminate; settle=1.8s; launch=25s -> simctl=40s
            res1 = asyncio.run(
                driver.act(udid, {"kind": "relaunch"}, bundle_id="app.daytrace.recorder")
            )
            self.assertTrue(res1["ok"])
            self.assertEqual(run_records, [("terminate", 10.0), ("launch", 25.0)])
            self.assertEqual(sleep_durations, [1.8, 1.5])
            self.assertEqual(len(subproc_records), 1)
            self.assertEqual(
                subproc_records[0],
                (["xcrun", "simctl", "launch", udid, "app.daytrace.recorder"], 40.0),
            )

            # Case 2: c.terminate fails -> falls back to simctl terminate (10s)
            run_records.clear()
            subproc_records.clear()
            sleep_durations.clear()
            terminate_should_fail = True
            launch_should_fail = False
            res2 = asyncio.run(
                driver.act(udid, {"kind": "relaunch"}, bundle_id="app.daytrace.recorder")
            )
            self.assertTrue(res2["ok"])
            self.assertEqual(
                subproc_records,
                [(["xcrun", "simctl", "terminate", udid, "app.daytrace.recorder"], 10.0)],
            )
            self.assertEqual(run_records, [("terminate", 10.0), ("launch", 25.0)])

            # Case 3: open action uses 25s c.launch -> 40s simctl launch fallback
            run_records.clear()
            subproc_records.clear()
            launch_should_fail = True
            res3 = asyncio.run(
                driver.act(udid, {"kind": "open"}, bundle_id="app.daytrace.recorder")
            )
            self.assertTrue(res3["ok"])
            self.assertEqual(run_records, [("launch", 25.0)])
            self.assertEqual(
                subproc_records,
                [(["xcrun", "simctl", "launch", udid, "app.daytrace.recorder"], 40.0)],
            )

    def test_screenshot_and_app_running_timeouts(self) -> None:
        driver = IDBDriver()
        udid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        ctrl = driver._get_control(udid)
        rpc_timeouts: dict[str, float] = {}
        subproc_timeouts: list[tuple[list[str], float]] = []

        async def fake_run(fn, *, timeout=15.0, retry_on_disconnect=False, op_name="rpc"):
            rpc_timeouts[op_name] = timeout
            raise TimeoutError(f"idb {op_name} timed out after {timeout:.1f}s")

        ctrl.run = fake_run  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as td:
            out_png = Path(td) / "shot.png"

            async def fake_subproc(args: list[str], *, timeout: float = 15.0, stdin_bytes=None):
                subproc_timeouts.append((args, timeout))
                if "screenshot" in args:
                    out_png.write_bytes(_fake_png(402, 874))
                    return 0, b"", b""
                if "launchctl" in args:
                    return 0, b"123 0 app.daytrace.recorder\n", b""
                return 0, b"", b""

            with patch("ios_agent_testers.core.idb_driver._run_subproc", side_effect=fake_subproc):
                dims = asyncio.run(driver._capture_point_scaled_png(udid, out_png, 402))
                running = asyncio.run(driver._check_app_running(udid, "app.daytrace.recorder"))

        self.assertEqual(dims, (402, 874))
        self.assertTrue(running)
        self.assertEqual(rpc_timeouts["screenshot"], 8.0)
        self.assertEqual(rpc_timeouts["list_apps"], 5.0)
        self.assertEqual(subproc_timeouts[0][1], 10.0)
        self.assertEqual(subproc_timeouts[1][1], 5.0)


if __name__ == "__main__":
    unittest.main()
