"""Comprehensive tests for ios-agent-testers MCP server, loopback HTTP server,
plugin bundle manifests, workflow syntax, security guardrails, and human takeover.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ios_agent_testers.core.run_store import RunStore
from server.main import (
    APP_MIME_TYPE,
    APP_RESOURCE_URI,
    MAX_CALL_TOOL_RESULT_BYTES,
    MCPQATestersServer,
    TOOLS_SCHEMA,
    _enforce_result_size_limit,
    _format_exc,
)

# Minimal valid 1x1 PNG bytes
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a8b8AAAAASUVORK5CYII="
)
# Minimal valid JPEG SOI/EOI payload for transport tests
TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"

IOS_PHONE_PANE_TOOLS = {
    "open_phone_pane",
    "app_list_phones",
    "app_select_phone",
    "app_frame",
    "app_tap",
    "app_swipe",
    "app_type",
    "app_key",
    "app_button",
    "app_device_action",
}

TERMINAL_PHONE_TOOLS = {
    "phone_list",
    "phone_create",
    "phone_boot",
    "phone_stop",
    "phone_screen",
    "phone_tap",
    "phone_swipe",
    "phone_type",
    "phone_button",
    "phone_install",
    "phone_launch",
    "phone_open_url",
}

ALLOWED_PLUGIN_TOP_KEYS = {
    "name",
    "displayName",
    "description",
    "version",
    "author",
    "homepage",
    "repository",
    "logo",
    "suggestedPrompts",
    "disabled",
}


class FakeIDBDriver:
    """In-memory mock of ios_agent_testers.core.idb_driver.IDBDriver."""

    def __init__(self) -> None:
        self.connected: list[tuple[str, bool]] = []
        self.closed: list[str] = []
        self.observe_calls: list[dict[str, Any]] = []
        self.act_calls: list[dict[str, Any]] = []
        self.log_tails: dict[str, Path] = {}
        self.log_tail_calls: list[dict[str, Any]] = []
        self.fail_stream_udids: set[str] = set()
        self.timeout_udids: set[str] = set()
        self.next_ax_unavailable_reason: str | None = None

    async def connect(self, udid: str, ephemeral: bool = True) -> None:
        self.connected.append((udid, ephemeral))

    async def describe_screen(self, udid: str) -> dict[str, Any]:
        return {
            "width": 750,
            "height": 1334,
            "width_points": 375,
            "height_points": 667,
            "density": 2.0,
        }

    async def observe(
        self,
        udid: str,
        *,
        bundle_id: str | None,
        output_png: Path,
        run_start_ts: float,
        crashes_dir: Path | None = None,
        max_elements: int = 120,
    ) -> dict[str, Any]:
        self.observe_calls.append({"udid": udid, "bundle_id": bundle_id, "output_png": str(output_png)})
        output_png.parent.mkdir(parents=True, exist_ok=True)
        output_png.write_bytes(TINY_PNG)
        elements = [
            {
                "ref": "e1",
                "kind": "button",
                "role": "Button",
                "label": "Continue",
                "identifier": "continue_btn",
                "value": "",
                "cx": 188,
                "cy": 520,
                "frame": {"x": 40.0, "y": 496.0, "width": 295.0, "height": 48.0},
                "enabled": True,
            },
            {
                "ref": "e2",
                "kind": "button",
                "role": "Button",
                "label": "",
                "identifier": "small_icon",
                "value": "",
                "cx": 350,
                "cy": 44,
                "frame": {"x": 336.0, "y": 30.0, "width": 28.0, "height": 28.0},
                "enabled": True,
            },
        ]
        return {
            "screenshot_path": str(output_png),
            "width_points": 375,
            "height_points": 667,
            "elements": elements,
            "elements_text": 'e1 [button] "Continue" (188,520)\ne2 [button] "" (350,44)',
            "a11y": {
                "summary_line": "1 unnamed active control; 1 touch target <44pt",
                "unnamed": [
                    {
                        "category": "unnamed",
                        "kind": "Button",
                        "name": "",
                        "x": 336.0,
                        "y": 30.0,
                        "width": 28.0,
                        "height": 28.0,
                        "cx": 350,
                        "cy": 44,
                    }
                ],
                "undersized": [
                    {
                        "category": "undersized",
                        "kind": "Button",
                        "name": "",
                        "x": 336.0,
                        "y": 30.0,
                        "width": 28.0,
                        "height": 28.0,
                        "cx": 350,
                        "cy": 44,
                    }
                ],
            },
            "a11y_line": "1 unnamed active control; 1 touch target <44pt",
            "ax_unavailable_reason": self.next_ax_unavailable_reason,
            "app_running": True,
            "crashes": [],
            "recent_logs": ["INFO app launched cleanly"],
        }

    async def act(
        self,
        udid: str,
        action: dict[str, Any],
        *,
        bundle_id: str | None = None,
        last_elements: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.act_calls.append(
            {
                "udid": udid,
                "action": action,
                "bundle_id": bundle_id,
                "last_elements": list(last_elements) if last_elements is not None else None,
            }
        )
        return {
            "ok": True,
            "kind": str(action.get("kind", "unknown")),
            "detail": f"mocked {action.get('kind')}",
            "resolved_point": [188, 520] if action.get("kind") == "tap" else None,
        }

    async def get_jpeg_frame(
        self, udid: str, max_width: int = 480, quality: float = 0.75
    ) -> bytes:
        if udid in self.timeout_udids:
            raise TimeoutError()
        if udid in self.fail_stream_udids:
            raise RuntimeError(f"simulator offline: {udid}")
        return TINY_JPEG

    async def stream_mjpeg(
        self, udid: str, fps: int = 24, scale: float = 0.5
    ) -> AsyncIterator[bytes]:
        if udid in self.timeout_udids:
            raise TimeoutError()
        if udid in self.fail_stream_udids:
            raise RuntimeError(f"stream unavailable: {udid}")
        yield TINY_JPEG
        yield TINY_JPEG

    async def start_log_tail(
        self,
        udid: str,
        log_file: Path,
        bundle_id: str | None = None,
        process_name: str | None = None,
    ) -> None:
        self.log_tails[udid] = log_file
        self.log_tail_calls.append(
            {
                "udid": udid,
                "log_file": log_file,
                "bundle_id": bundle_id,
                "process_name": process_name,
            }
        )
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("log line 1\n", encoding="utf-8")

    async def stop_log_tail(self, udid: str) -> None:
        self.log_tails.pop(udid, None)

    def get_recent_logs(self, udid: str, limit: int = 25) -> list[str]:
        return ["INFO recent line"]

    async def close_udid(self, udid: str) -> None:
        self.closed.append(udid)

    async def close_all(self) -> None:
        self.closed.append("__all__")


async def _raw_http_request(
    port: int,
    method: str,
    path_and_query: str,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        req_lines = [f"{method} {path_and_query} HTTP/1.1", f"Host: 127.0.0.1:{port}"]
        for k, v in (headers or {}).items():
            req_lines.append(f"{k}: {v}")
        req_lines.append("Connection: close")
        writer.write(("\r\n".join(req_lines) + "\r\n\r\n").encode("ascii"))
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout=5.0)
    finally:
        writer.close()
        await writer.wait_closed()

    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ", 2)[1])
    resp_headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            hk, hv = line.split(":", 1)
            resp_headers[hk.strip().lower()] = hv.strip()
    return status, resp_headers, body


class TestPluginBundleAndWorkflow(unittest.TestCase):
    def test_plugin_json_valid_and_strict_keys(self) -> None:
        manifest_path = PROJECT_ROOT / "plugin.json"
        self.assertTrue(manifest_path.is_file())
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "ios-agent-testers")
        self.assertEqual(data["displayName"], "iOS Agent Testers")
        self.assertEqual(data["version"], "0.2.0")
        self.assertEqual(data["logo"], "assets/logo.svg")
        unknown_keys = set(data.keys()) - ALLOWED_PLUGIN_TOP_KEYS
        self.assertEqual(unknown_keys, set(), f"Disallowed plugin.json keys: {unknown_keys}")
        prompts = data.get("suggestedPrompts") or []
        self.assertGreaterEqual(len(prompts), 1)
        self.assertLessEqual(len(prompts), 3)

        logo_path = PROJECT_ROOT / data["logo"]
        self.assertTrue(logo_path.is_file())
        svg = logo_path.read_text(encoding="utf-8")
        self.assertIn("<svg", svg)
        self.assertIn('width="128"', svg)
        self.assertIn('height="128"', svg)

    def test_mcp_config_and_launcher(self) -> None:
        cfg_path = PROJECT_ROOT / "mcp_config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        servers = cfg["mcpServers"]
        self.assertIn("testers", servers)
        entry = servers["testers"]
        self.assertEqual(entry["command"], "bash")
        self.assertEqual(entry["args"], ["${PLUGIN_ROOT}/server/bin/ios-agent-testers-server"])
        self.assertEqual(
            entry["env"]["IOS_AGENT_TESTERS_HTML"], "${PLUGIN_ROOT}/dist/app.html"
        )
        self.assertEqual(entry["env"]["IOS_AGENT_TESTERS_DATA"], "${PLUGIN_DATA}")

        launcher = PROJECT_ROOT / "server" / "bin" / "ios-agent-testers-server"
        self.assertTrue(launcher.is_file())
        self.assertTrue(os.access(launcher, os.X_OK), "Launcher must be executable")

    def test_skill_agent_and_workflow_syntax(self) -> None:
        skill = (PROJECT_ROOT / "skills" / "ios-agent-testers" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertTrue(skill.startswith("---\n"))
        self.assertIn("name: ios-agent-testers", skill)
        self.assertIn("qa_create_run", skill)
        self.assertIn("open_qa_dashboard", skill)

        agent_doc = (PROJECT_ROOT / "agents" / "ios-persona-tester.md").read_text(
            encoding="utf-8"
        )
        self.assertTrue(agent_doc.startswith("---\n"))
        self.assertIn("name: ios-persona-tester", agent_doc)
        self.assertIn("paused_by_human", agent_doc)

        wf_path = PROJECT_ROOT / "workflows" / "run_qa_matrix.py"
        wf_src = wf_path.read_text(encoding="utf-8")
        # Antigravity run_workflow supports top-level await.
        compile(wf_src, str(wf_path), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        self.assertIn("parallel(", wf_src)
        self.assertIn("phase(", wf_src)
        self.assertIn("except Exception", wf_src)
        self.assertIn("qa_summarize_run", wf_src)
        self.assertIn("likely test artefact", wf_src)
        self.assertIn("grouping_items", wf_src)
        self.assertIn('"max_actions": max_actions', wf_src)
        self.assertIn("include_image=false", skill)
        self.assertIn("grouping_items", skill)
        self.assertIn("ax_unavailable_reason", skill)
        self.assertIn("include_image=false", agent_doc)
        self.assertIn("ax_unavailable_reason", agent_doc)


class TestMCPServerAndLoopbackHTTP(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.runs_root = self.tmp_path / "runs"
        self.store = RunStore(self.runs_root)
        self.driver = FakeIDBDriver()
        self.teardown_calls: list[dict[str, Any]] = []
        self.provision_calls: list[dict[str, Any]] = []
        self.provision_exc: BaseException | None = None

        async def fake_provision(
            run_id: str,
            persona: dict[str, Any],
            *,
            app_path: str | None = None,
            bundle_id: str | None = None,
            record_video: bool = False,
            video_path: Path | None = None,
            keep_simulator: bool = False,
        ) -> dict[str, Any]:
            if self.provision_exc is not None:
                raise self.provision_exc
            self.provision_calls.append(
                {
                    "run_id": run_id,
                    "persona_id": persona["id"],
                    "bundle_id": bundle_id,
                    "keep_simulator": keep_simulator,
                }
            )
            return {
                "udid": f"SIM-UDID-{persona['id'].upper()}",
                "device_label": f"{persona['device']['simulator']}, iOS 27.0",
                "runtime_version": "27.0",
                "bundle_id": bundle_id or "com.example.demo",
                "executable_name": "DemoApp",
                "ephemeral": True,
                "video_pid": None,
                "video_path": str(video_path) if record_video and video_path else None,
                "fallback_reason": None,
            }

        async def fake_teardown(
            udid: str,
            *,
            ephemeral: bool = True,
            delete_simulator: bool = True,
            video_pid: int | None = None,
        ) -> dict[str, Any]:
            self.teardown_calls.append(
                {
                    "udid": udid,
                    "ephemeral": ephemeral,
                    "delete_simulator": delete_simulator,
                }
            )
            return {"shutdown": True, "deleted": bool(ephemeral and delete_simulator)}

        self.server = MCPQATestersServer(
            store=self.store,
            driver=self.driver,
            provision_fn=fake_provision,
            teardown_fn=fake_teardown,
        )
        await self.server.http_server.start()

    async def asyncTearDown(self) -> None:
        await self.server.http_server.stop()
        self._tmpdir.cleanup()

    async def _rpc(self, method: str, params: dict[str, Any] | None = None, req_id: int = 1) -> dict[str, Any]:
        req: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            req["params"] = params
        resp = await self.server.handle_rpc_request(req)
        assert resp is not None
        return resp

    async def _call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        self.assertIn("result", resp, f"RPC error for {name}: {resp}")
        return resp["result"]

    async def test_initialize_tools_list_visibility_and_zero_collision(self) -> None:
        init_resp = await self._rpc("initialize", {"protocolVersion": "2025-06-18"})
        res = init_resp["result"]
        self.assertEqual(res["protocolVersion"], "2025-06-18")
        self.assertEqual(res["serverInfo"]["name"], "ios-agent-testers")
        self.assertIn("tools", res["capabilities"])
        self.assertIn("resources", res["capabilities"])

        notif = await self.server.handle_rpc_request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        self.assertIsNone(notif)

        tools_resp = await self._rpc("tools/list")
        tools = tools_resp["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertEqual(len(names), len(set(names)), "Duplicate tool names")

        # Zero collision with ios-phone-pane and terminal-phone
        self.assertEqual(set(names) & IOS_PHONE_PANE_TOOLS, set())
        self.assertEqual(set(names) & TERMINAL_PHONE_TOOLS, set())

        model_tools = [
            t for t in tools if not ((t.get("_meta") or {}).get("ui") or {}).get("visibility")
        ]
        app_only_tools = [
            t
            for t in tools
            if ((t.get("_meta") or {}).get("ui") or {}).get("visibility") == ["app"]
        ]
        self.assertEqual(len(model_tools), 11)
        self.assertEqual(len(app_only_tools), 12)
        self.assertTrue(all(t["name"].startswith("app_qa_") for t in app_only_tools))

        dash = next(t for t in tools if t["name"] == "open_qa_dashboard")
        self.assertEqual(dash["_meta"]["ui"]["resourceUri"], APP_RESOURCE_URI)
        entrypoints = dash["_meta"]["com.google.antigravity/ui"]["entrypoints"]
        self.assertEqual(
            {(e["type"], e["title"]) for e in entrypoints},
            {("aux_pane", "iOS QA 測試台"), ("global", "iOS QA 測試台")},
        )
        # Entrypoint launches invoke with {} -> no required params allowed.
        self.assertFalse(dash["inputSchema"].get("required"))

    async def test_resources_read_csp_html_hot_reload_and_run_artifacts(self) -> None:
        custom_html = self.tmp_path / "custom_app.html"
        custom_html.write_text("<html><body>QA Dashboard Live</body></html>", encoding="utf-8")

        old_env = os.environ.get("IOS_AGENT_TESTERS_HTML")
        os.environ["IOS_AGENT_TESTERS_HTML"] = str(custom_html)
        try:
            list_resp = await self._rpc("resources/list")
            res_list = list_resp["result"]["resources"]
            self.assertEqual(res_list[0]["uri"], APP_RESOURCE_URI)
            self.assertEqual(res_list[0]["mimeType"], APP_MIME_TYPE)

            read_resp = await self._rpc("resources/read", {"uri": APP_RESOURCE_URI})
            item = read_resp["result"]["contents"][0]
            self.assertEqual(item["mimeType"], APP_MIME_TYPE)
            self.assertIn("QA Dashboard Live", item["text"])
            csp = item["_meta"]["ui"]["csp"]
            expected_origin = f"http://127.0.0.1:{self.server.http_server.port}"
            self.assertEqual(csp["resourceDomains"], [expected_origin])
            self.assertEqual(csp["connectDomains"], [expected_origin])

            # Hot reload verification
            custom_html.write_text("<html><body>Hot Reloaded v2</body></html>", encoding="utf-8")
            read_resp2 = await self._rpc("resources/read", {"uri": APP_RESOURCE_URI})
            self.assertIn("Hot Reloaded v2", read_resp2["result"]["contents"][0]["text"])
        finally:
            if old_env is None:
                os.environ.pop("IOS_AGENT_TESTERS_HTML", None)
            else:
                os.environ["IOS_AGENT_TESTERS_HTML"] = old_env

        # Test ios-testers://run/{run_id}/{persona_id}/{file}
        create_res = await self._call(
            "qa_create_run", {"bundle_id": "com.example.demo", "personas": ["clara"]}
        )
        run_id = create_res["structuredContent"]["run_id"]
        pdir = self.store.persona_dir(run_id, "clara")
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "01.png").write_bytes(TINY_PNG)
        (pdir / "report.md").write_text("# Clara Report\nAll good.", encoding="utf-8")

        png_resp = await self._rpc(
            "resources/read", {"uri": f"ios-testers://run/{run_id}/clara/01.png"}
        )
        png_item = png_resp["result"]["contents"][0]
        self.assertEqual(png_item["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(png_item["blob"]), TINY_PNG)

        md_resp = await self._rpc(
            "resources/read", {"uri": f"ios-testers://run/{run_id}/clara/report.md"}
        )
        md_item = md_resp["result"]["contents"][0]
        self.assertEqual(md_item["mimeType"], "text/markdown")
        self.assertIn("Clara Report", md_item["text"])

        # Path traversal over MCP resource URI must fail with -32602
        trav_resp = await self._rpc(
            "resources/read", {"uri": f"ios-testers://run/{run_id}/clara/../../etc/passwd"}
        )
        self.assertIn("error", trav_resp)
        self.assertEqual(trav_resp["error"]["code"], -32602)

    async def test_loopback_http_auth_streaming_and_path_traversal(self) -> None:
        port = self.server.http_server.port
        tok = self.server.http_server.token
        self.assertGreaterEqual(len(tok), 32)  # 192-bit base64url token

        # Health works with no token or valid token, rejects wrong token
        st, _, body = await _raw_http_request(port, "GET", "/health")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body), {"ok": True})

        st, _, _ = await _raw_http_request(port, "GET", "/health?token=wrongtoken")
        self.assertEqual(st, 403)

        # Method restriction
        st, _, _ = await _raw_http_request(port, "POST", "/health")
        self.assertEqual(st, 405)

        # Auth required on /snapshot, /stream, /artifact
        for path in [
            "/snapshot?udid=SIM-1",
            "/snapshot?udid=SIM-1&token=bad",
            "/stream?udid=SIM-1",
            "/stream?udid=SIM-1&token=bad",
            "/artifact?run=r1&persona=clara&file=01.png",
            "/artifact?run=r1&persona=clara&file=01.png&token=bad",
        ]:
            st, _, _ = await _raw_http_request(port, "GET", path)
            self.assertEqual(st, 403, f"Expected 403 for {path}, got {st}")

        # Valid /snapshot
        st, hdrs, body = await _raw_http_request(
            port, "GET", f"/snapshot?udid=SIM-1&max_width=320&quality=0.7&token={tok}"
        )
        self.assertEqual(st, 200)
        self.assertEqual(hdrs["content-type"], "image/jpeg")
        self.assertEqual(body, TINY_JPEG)

        # Valid /stream MJPEG
        st, hdrs, body = await _raw_http_request(
            port, "GET", f"/stream?udid=SIM-1&fps=12&scale=0.5&token={tok}"
        )
        self.assertEqual(st, 200)
        self.assertIn("multipart/x-mixed-replace", hdrs["content-type"])
        self.assertIn(b"--frame\r\n", body)
        self.assertIn(TINY_JPEG, body)

        # Failing /stream returns 503 cleanly
        self.driver.fail_stream_udids.add("SIM-OFFLINE")
        st, _, _ = await _raw_http_request(
            port, "GET", f"/stream?udid=SIM-OFFLINE&token={tok}"
        )
        self.assertEqual(st, 503)

        # Create real run + artifacts for /artifact tests
        create_res = await self._call(
            "qa_create_run", {"bundle_id": "com.example.demo", "personas": ["clara"]}
        )
        run_id = create_res["structuredContent"]["run_id"]
        pdir = self.store.persona_dir(run_id, "clara")
        (pdir / "video").mkdir(parents=True, exist_ok=True)
        (pdir / "01.png").write_bytes(TINY_PNG)
        (pdir / "video" / "rec_01.mp4").write_bytes(b"0123456789ABCDEF")
        (pdir / "findings.json").write_text('{"score": 8}', encoding="utf-8")
        (pdir / "report.md").write_text("# Report", encoding="utf-8")
        (pdir / "logs.txt").write_text("hello log", encoding="utf-8")

        # Secret file outside runs_root that traversal attempts try to read
        secret_file = self.tmp_path / "secret.txt"
        secret_file.write_text("TOP_SECRET", encoding="utf-8")
        symlink_escape = pdir / "evil_link.txt"
        symlink_escape.symlink_to(secret_file)

        # Valid MIME types
        cases = [
            ("01.png", "image/png", TINY_PNG),
            ("video/rec_01.mp4", "video/mp4", b"0123456789ABCDEF"),
            ("findings.json", "application/json; charset=utf-8", b'{"score": 8}'),
            ("report.md", "text/markdown; charset=utf-8", b"# Report"),
            ("logs.txt", "text/plain; charset=utf-8", b"hello log"),
        ]
        for rel_file, expected_mime, expected_body in cases:
            st, hdrs, body = await _raw_http_request(
                port,
                "GET",
                f"/artifact?run={quote(run_id)}&persona=clara&file={quote(rel_file, safe='/')}&token={tok}",
            )
            self.assertEqual(st, 200, f"Failed for {rel_file}")
            self.assertEqual(hdrs["content-type"], expected_mime)
            self.assertEqual(body, expected_body)

        # MP4 Range request -> 206 Partial Content
        st, hdrs, body = await _raw_http_request(
            port,
            "GET",
            f"/artifact?run={quote(run_id)}&persona=clara&file=video/rec_01.mp4&token={tok}",
            headers={"Range": "bytes=2-7"},
        )
        self.assertEqual(st, 206)
        self.assertEqual(hdrs["content-range"], "bytes 2-7/16")
        self.assertEqual(body, b"234567")

        # Strict path traversal & symlink escape rejections (403/400)
        attacks = [
            f"/artifact?run={quote(run_id)}&persona=clara&file=../../secret.txt&token={tok}",
            f"/artifact?run={quote(run_id)}&persona=clara&file=%2e%2e%2f%2e%2e%2fsecret.txt&token={tok}",
            f"/artifact?run={quote(run_id)}&persona=clara&file=../manifest.json&token={tok}",
            f"/artifact?run={quote(run_id)}&persona=clara&file=/etc/passwd&token={tok}",
            f"/artifact?run={quote(run_id)}&persona=..&file=secret.txt&token={tok}",
            f"/artifact?run=..&persona=clara&file=secret.txt&token={tok}",
            f"/artifact?run={quote(run_id)}&persona=clara&file=..\\..\\secret.txt&token={tok}",
            f"/artifact?run={quote(run_id)}&persona=clara&file=evil_link.txt&token={tok}",
        ]
        for attack_url in attacks:
            st, _, body = await _raw_http_request(port, "GET", attack_url)
            self.assertIn(
                st,
                (400, 403),
                f"Path traversal not rejected with 400/403 for {attack_url}: got {st}",
            )
            self.assertNotIn(b"TOP_SECRET", body)

        # Missing file -> 404
        st, _, _ = await _raw_http_request(
            port,
            "GET",
            f"/artifact?run={quote(run_id)}&persona=clara&file=99.png&token={tok}",
        )
        self.assertEqual(st, 404)

    async def test_end_to_end_lifecycle_takeover_and_app_tools(self) -> None:
        # 1. List personas
        lp = await self._call("qa_list_personas", {"selector": "smoke"})
        self.assertFalse(lp["isError"])
        self.assertEqual(
            [p["id"] for p in lp["structuredContent"]["personas"]],
            ["clara", "amira", "sven"],
        )

        # 2. Create Run A
        c1 = await self._call(
            "qa_create_run",
            {
                "bundle_id": "com.example.demo",
                "personas": ["clara", "sven"],
                "mode": "explore",
                "max_concurrency": 2,
            },
        )
        self.assertFalse(c1["isError"])
        run_a = c1["structuredContent"]["run_id"]

        # 3. open_qa_dashboard: token ONLY in structuredContent, NEVER in text content!
        dash = await self._call("open_qa_dashboard", {})
        self.assertFalse(dash["isError"])
        sc = dash["structuredContent"]
        self.assertEqual(sc["activeRunId"], run_a)
        self.assertEqual(sc["streamToken"], self.server.http_server.token)
        self.assertEqual(sc["httpBaseUrl"], self.server.http_server.origin)
        all_text = " ".join(
            blk.get("text", "") for blk in dash["content"] if blk.get("type") == "text"
        )
        self.assertNotIn(self.server.http_server.token, all_text)

        # 4. Provision persona clara
        prov = await self._call(
            "qa_provision_persona", {"run_id": run_a, "persona_id": "clara"}
        )
        self.assertFalse(prov["isError"])
        self.assertEqual(prov["structuredContent"]["udid"], "SIM-UDID-CLARA")
        self.assertIn("clara", prov["structuredContent"]["persona_prompt"].lower())

        # 5. Observe step 1
        obs1 = await self._call(
            "qa_observe", {"run_id": run_a, "persona_id": "clara", "include_image": True}
        )
        self.assertFalse(obs1["isError"])
        self.assertEqual(obs1["structuredContent"]["step"], 1)
        self.assertFalse(obs1["structuredContent"]["paused_by_human"])
        self.assertEqual(len(self.driver.observe_calls), 1)
        self.assertTrue(any(b.get("type") == "image" for b in obs1["content"]))

        # 6. Act (tap e1) -> observes step 2
        act1 = await self._call(
            "qa_act",
            {
                "run_id": run_a,
                "persona_id": "clara",
                "action": {"kind": "tap", "ref": "e1"},
                "observe_after": True,
            },
        )
        self.assertFalse(act1["isError"])
        self.assertEqual(act1["structuredContent"]["step"], 2)
        self.assertEqual(len(self.driver.act_calls), 1)
        self.assertEqual(len(self.driver.observe_calls), 2)

        # 7. HUMAN TAKEOVER: pause agent with note
        tk_pause = await self._call(
            "app_qa_toggle_takeover",
            {
                "run_id": run_a,
                "persona_id": "clara",
                "paused": True,
                "note": "Hold on, checking login",
            },
        )
        self.assertFalse(tk_pause["isError"])
        self.assertTrue(tk_pause["structuredContent"]["paused"])
        self.assertEqual(tk_pause["structuredContent"]["note"], "Hold on, checking login")

        # While paused, qa_observe and qa_act MUST NOT touch the simulator!
        obs_paused = await self._call("qa_observe", {"run_id": run_a, "persona_id": "clara"})
        self.assertFalse(obs_paused["isError"])
        self.assertTrue(obs_paused["structuredContent"]["paused_by_human"])
        self.assertEqual(obs_paused["structuredContent"]["human_note"], "Hold on, checking login")
        self.assertEqual(len(self.driver.observe_calls), 2)

        act_paused = await self._call(
            "qa_act",
            {
                "run_id": run_a,
                "persona_id": "clara",
                "action": {"kind": "tap", "x": 100, "y": 200},
            },
        )
        self.assertFalse(act_paused["isError"])
        self.assertTrue(act_paused["structuredContent"]["paused_by_human"])
        self.assertFalse(act_paused["structuredContent"]["action_executed"])
        self.assertEqual(len(self.driver.act_calls), 1)

        # Human directly operates simulator via app_qa_* controls
        udid = "SIM-UDID-CLARA"
        for tool_name, targs in [
            ("app_qa_tap", {"udid": udid, "x": 50, "y": 80}),
            ("app_qa_swipe", {"udid": udid, "x1": 10, "y1": 300, "x2": 10, "y2": 100}),
            ("app_qa_type", {"udid": udid, "text": "hello"}),
            ("app_qa_key", {"udid": udid, "key": "return"}),
            ("app_qa_button", {"udid": udid, "button": "HOME"}),
            ("app_qa_device_action", {"udid": udid, "action": "dark"}),
        ]:
            r = await self._call(tool_name, targs)
            self.assertFalse(r["isError"], f"{tool_name} failed: {r}")
            self.assertTrue(r["structuredContent"]["ok"])

        frame_res = await self._call("app_qa_frame", {"udid": udid})
        self.assertFalse(frame_res["isError"])
        self.assertEqual(frame_res["structuredContent"]["mimeType"], "image/jpeg")
        self.assertEqual(base64.b64decode(frame_res["structuredContent"]["data"]), TINY_JPEG)
        self.assertEqual(frame_res["structuredContent"]["width_points"], 375)

        # Human resumes with steering note
        tk_resume = await self._call(
            "app_qa_toggle_takeover",
            {
                "run_id": run_a,
                "persona_id": "clara",
                "paused": False,
                "note": "Signed in, please test Settings now",
            },
        )
        self.assertFalse(tk_resume["isError"])
        self.assertFalse(tk_resume["structuredContent"]["paused"])

        # Next qa_observe receives and consumes queued human steering note(s) ONCE
        obs_resumed = await self._call("qa_observe", {"run_id": run_a, "persona_id": "clara"})
        self.assertFalse(obs_resumed["isError"])
        self.assertFalse(obs_resumed["structuredContent"]["paused_by_human"])
        delivered_note = obs_resumed["structuredContent"]["human_note"] or ""
        self.assertIn("Hold on, checking login", delivered_note)
        self.assertIn("Signed in, please test Settings now", delivered_note)
        self.assertIn("Signed in, please test Settings now", obs_resumed["content"][0]["text"])

        obs_after = await self._call("qa_observe", {"run_id": run_a, "persona_id": "clara"})
        self.assertIsNone(obs_after["structuredContent"]["human_note"])

        # 8. Record findings (invalid rejected, valid accepted)
        bad_find = await self._call(
            "qa_record_findings",
            {
                "run_id": run_a,
                "persona_id": "clara",
                "report_md": "bad",
                "findings_json": {"score": 99, "issues": [{"severity": "nope"}]},
            },
        )
        self.assertTrue(bad_find["isError"])

        clara_findings = {
            "score": 7,
            "issues": [
                {
                    "severity": "major",
                    "screen": "Settings",
                    "screens": ["02"],
                    "title": "Settings icon button is too small and unlabeled",
                    "details": "28x28pt button lacks accessibility label.",
                }
            ],
        }
        good_find = await self._call(
            "qa_record_findings",
            {
                "run_id": run_a,
                "persona_id": "clara",
                "report_md": "# Clara Report\nFound an unlabeled small button.",
                "findings_json": clara_findings,
            },
        )
        self.assertFalse(good_find["isError"])

        sven_findings = {
            "score": 6,
            "issues": [
                {
                    "severity": "major",
                    "screen": "Settings",
                    "screens": ["01"],
                    "title": "Icon button missing label in Settings",
                    "details": "VoiceOver cannot read the icon button.",
                }
            ],
        }
        good_find_sven = await self._call(
            "qa_record_findings",
            {
                "run_id": run_a,
                "persona_id": "sven",
                "report_md": "# Sven Report\nUnlabeled icon button.",
                "findings_json": sven_findings,
            },
        )
        self.assertFalse(good_find_sven["isError"])

        # 9. Finish persona clara -> simulator torn down & deleted
        fin = await self._call(
            "qa_finish_persona",
            {"run_id": run_a, "persona_id": "clara", "status": "done", "delete_simulator": True},
        )
        self.assertFalse(fin["isError"])
        self.assertTrue(fin["structuredContent"]["simulator_deleted"])
        self.assertEqual(self.teardown_calls[-1]["udid"], "SIM-UDID-CLARA")
        self.assertTrue(self.teardown_calls[-1]["delete_simulator"])

        # 10. Summarize run with semantic deduplication group
        sum_res = await self._call(
            "qa_summarize_run",
            {
                "run_id": run_a,
                "groups_json": {
                    "groups": [
                        {
                            "title": "Unlabeled undersized Settings icon button",
                            "class": "accessibility",
                            "ids": [1, 2],
                        }
                    ]
                },
            },
        )
        self.assertFalse(sum_res["isError"])
        summary = sum_res["structuredContent"]["summary"]
        self.assertEqual(summary["persona_count"], 2)
        self.assertEqual(summary["distinct_issue_count"], 1)
        self.assertTrue(summary["grouped"])
        self.assertTrue(Path(sum_res["structuredContent"]["summary_path"]).is_file())

        # 11. App-only inspection tools reflect run state, summary, and persona detail
        runs_res = await self._call("app_qa_list_runs", {})
        self.assertEqual(len(runs_res["structuredContent"]["runs"]), 1)
        self.assertEqual(runs_res["structuredContent"]["streamToken"], self.server.http_server.token)

        state_res = await self._call("app_qa_get_run_state", {"run_id": run_a})
        state_sc = state_res["structuredContent"]
        self.assertEqual(state_sc["run_id"], run_a)
        self.assertEqual([p["id"] for p in state_sc["personas"]], ["clara", "sven"])
        clara_state = state_sc["personas"][0]
        self.assertEqual(clara_state["status"], "done")
        self.assertIsNone(clara_state["udid"])
        self.assertEqual(clara_state["score"], 7)
        self.assertIsNotNone(state_sc["summary"])
        self.assertTrue(state_sc["summary_path"].endswith("SUMMARY.md"))

        detail_res = await self._call(
            "app_qa_get_persona_detail", {"run_id": run_a, "persona_id": "clara"}
        )
        detail_sc = detail_res["structuredContent"]
        self.assertEqual(detail_sc["persona_id"], "clara")
        self.assertGreaterEqual(len(detail_sc["steps"]), 4)
        self.assertIn("Clara Report", detail_sc["report_md"])
        self.assertEqual(detail_sc["findings"]["score"], 7)

        # 12. Create Run B & compare via qa_compare_runs and app_qa_compare
        c2 = await self._call(
            "qa_create_run",
            {"bundle_id": "com.example.demo", "personas": ["clara", "sven"], "mode": "explore"},
        )
        run_b = c2["structuredContent"]["run_id"]
        await self._call(
            "qa_record_findings",
            {
                "run_id": run_b,
                "persona_id": "clara",
                "report_md": "# Clara v2\nFixed!",
                "findings_json": {"score": 9, "issues": []},
            },
        )
        await self._call(
            "qa_record_findings",
            {
                "run_id": run_b,
                "persona_id": "sven",
                "report_md": "# Sven v2\nFixed!",
                "findings_json": {"score": 8, "issues": []},
            },
        )

        cmp_model = await self._call(
            "qa_compare_runs",
            {"run_a": run_a, "run_b": run_b, "name_a": "v1", "name_b": "v2"},
        )
        self.assertFalse(cmp_model["isError"])
        self.assertTrue(Path(cmp_model["structuredContent"]["compare_path"]).is_file())
        self.assertEqual(cmp_model["structuredContent"]["compare"]["score_summary"]["higher"], 2)

        cmp_app = await self._call(
            "app_qa_compare",
            {"run_a": run_a, "run_b": run_b, "name_a": "v1", "name_b": "v2"},
        )
        self.assertFalse(cmp_app["isError"])
        self.assertIn("markdown", cmp_app["structuredContent"])
        self.assertEqual(cmp_app["structuredContent"]["compare"]["name_b"], "v2")

        # 13. app_qa_device_action("stop") updates RunStore so UI stops streaming
        await self._call(
            "qa_provision_persona", {"run_id": run_b, "persona_id": "clara"}
        )
        await self._call(
            "app_qa_toggle_takeover",
            {"run_id": run_b, "persona_id": "clara", "paused": True},
        )
        stop_res = await self._call(
            "app_qa_device_action", {"udid": "SIM-UDID-CLARA", "action": "stop"}
        )
        self.assertFalse(stop_res["isError"])
        state_b = await self._call("app_qa_get_run_state", {"run_id": run_b})
        clara_b = next(p for p in state_b["structuredContent"]["personas"] if p["id"] == "clara")
        self.assertIsNone(clara_b["udid"])
        self.assertFalse(clara_b["paused_by_human"])
        self.assertEqual(clara_b["status"], "done")

    def test_one_mib_result_guardrail(self) -> None:
        huge = {
            "content": [
                {"type": "text", "text": "Observation ok"},
                {"type": "image", "mimeType": "image/png", "data": "A" * 1_200_000},
            ],
            "structuredContent": {"ok": True},
            "isError": False,
        }
        trimmed = _enforce_result_size_limit(huge)
        size = len(json.dumps(trimmed).encode("utf-8"))
        self.assertLessEqual(size, MAX_CALL_TOOL_RESULT_BYTES)
        self.assertFalse(any(b.get("type") == "image" for b in trimmed["content"]))

    def test_stdio_subprocess_roundtrip(self) -> None:
        """Spawn the real launcher over stdio and verify clean JSON-RPC framing."""
        launcher = PROJECT_ROOT / "server" / "bin" / "ios-agent-testers-server"
        env = dict(os.environ)
        env["IOS_AGENT_TESTERS_RUNS_DIR"] = str(self.tmp_path / "stdio_runs")
        env["IOS_AGENT_TESTERS_PYTHON"] = sys.executable

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": APP_RESOURCE_URI}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "open_qa_dashboard", "arguments": {}}},
        ]
        stdin_payload = "".join(json.dumps(r) + "\n" for r in requests)

        proc = subprocess.run(
            ["bash", str(launcher)],
            input=stdin_payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"Server stderr:\n{proc.stderr}")

        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 4, f"Expected 4 JSON-RPC responses on stdout, got: {lines}")
        by_id = {json.loads(ln)["id"]: json.loads(ln) for ln in lines}

        self.assertEqual(by_id[1]["result"]["serverInfo"]["name"], "ios-agent-testers")
        self.assertEqual(len(by_id[2]["result"]["tools"]), 23)
        self.assertEqual(by_id[3]["result"]["contents"][0]["mimeType"], APP_MIME_TYPE)

        dash_res = by_id[4]["result"]
        tok = dash_res["structuredContent"]["streamToken"]
        self.assertGreaterEqual(len(tok), 32)
        self.assertNotIn(tok, dash_res["content"][0]["text"])

    async def test_format_exc_and_blank_timeout_error_handling(self) -> None:
        self.assertEqual(_format_exc(TimeoutError()), "TimeoutError: operation timed out")
        self.assertEqual(_format_exc(TimeoutError("   ")), "TimeoutError: operation timed out")
        self.assertEqual(_format_exc(TimeoutError("custom timeout")), "custom timeout")
        self.assertEqual(_format_exc(RuntimeError()), "RuntimeError")

        create_res = await self._call(
            "qa_create_run", {"bundle_id": "com.example.demo", "personas": ["clara"]}
        )
        run_id = create_res["structuredContent"]["run_id"]

        # Argless TimeoutError in qa_provision_persona must never produce blank error messages
        self.provision_exc = TimeoutError()
        try:
            prov_err = await self._call(
                "qa_provision_persona", {"run_id": run_id, "persona_id": "clara"}
            )
        finally:
            self.provision_exc = None

        self.assertTrue(prov_err["isError"])
        self.assertEqual(
            prov_err["structuredContent"]["error"], "TimeoutError: operation timed out"
        )
        self.assertEqual(
            prov_err["content"][0]["text"], "TimeoutError: operation timed out"
        )
        state_res = await self._call("app_qa_get_run_state", {"run_id": run_id})
        clara_state = state_res["structuredContent"]["personas"][0]
        self.assertEqual(clara_state["status"], "error")
        self.assertEqual(clara_state["error"], "TimeoutError: operation timed out")

        # Loopback HTTP /snapshot and /stream on argless TimeoutError must return non-empty 503 error
        port = self.server.http_server.port
        tok = self.server.http_server.token
        self.driver.timeout_udids.add("SIM-TIMEOUT")

        st_snap, _, body_snap = await _raw_http_request(
            port, "GET", f"/snapshot?udid=SIM-TIMEOUT&token={tok}"
        )
        self.assertEqual(st_snap, 503)
        snap_err = json.loads(body_snap)
        self.assertFalse(snap_err["ok"])
        self.assertIn("TimeoutError", snap_err["error"])

        st_stream, _, body_stream = await _raw_http_request(
            port, "GET", f"/stream?udid=SIM-TIMEOUT&token={tok}"
        )
        self.assertEqual(st_stream, 503)
        stream_err = json.loads(body_stream)
        self.assertFalse(stream_err["ok"])
        self.assertIn("TimeoutError", stream_err["error"])

        # Unhandled argless TimeoutError in handle_rpc_request also formats non-empty message
        orig_call_tool = self.server._call_tool

        async def _raise_blank_timeout(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            raise TimeoutError()

        self.server._call_tool = _raise_blank_timeout  # type: ignore[method-assign]
        try:
            rpc_err = await self._rpc("tools/call", {"name": "qa_list_personas", "arguments": {}})
        finally:
            self.server._call_tool = orig_call_tool  # type: ignore[method-assign]
        self.assertIn("error", rpc_err)
        self.assertEqual(
            rpc_err["error"]["message"], "Internal error: TimeoutError: operation timed out"
        )

    async def test_executable_name_wired_to_start_log_tail_and_state(self) -> None:
        create_res = await self._call(
            "qa_create_run", {"bundle_id": "app.daytrace.recorder", "personas": ["clara"]}
        )
        run_id = create_res["structuredContent"]["run_id"]
        prov = await self._call(
            "qa_provision_persona", {"run_id": run_id, "persona_id": "clara"}
        )
        self.assertFalse(prov["isError"])
        self.assertEqual(prov["structuredContent"]["executable_name"], "DemoApp")
        self.assertEqual(len(self.driver.log_tail_calls), 1)
        self.assertEqual(self.driver.log_tail_calls[0]["process_name"], "DemoApp")
        self.assertEqual(
            self.server._persona_runtime[(run_id, "clara")]["executable_name"], "DemoApp"
        )
        state_res = await self._call("app_qa_get_run_state", {"run_id": run_id})
        self.assertEqual(
            state_res["structuredContent"]["personas"][0]["executable_name"], "DemoApp"
        )

    async def test_qa_act_observe_after_false_persists_step_and_preserves_last_elements(
        self,
    ) -> None:
        create_res = await self._call(
            "qa_create_run",
            {"bundle_id": "com.example.demo", "personas": ["clara"], "max_actions": 20},
        )
        run_id = create_res["structuredContent"]["run_id"]
        await self._call("qa_provision_persona", {"run_id": run_id, "persona_id": "clara"})

        # Step 1: pure observe -> action_count=0, observation_count=1, saves last_elements
        obs1 = await self._call("qa_observe", {"run_id": run_id, "persona_id": "clara"})
        self.assertFalse(obs1["isError"])
        self.assertEqual(obs1["structuredContent"]["step"], 1)
        self.assertEqual(obs1["structuredContent"]["action_count"], 0)
        self.assertEqual(obs1["structuredContent"]["observation_count"], 1)
        self.assertEqual(obs1["structuredContent"]["max_actions"], 20)
        self.assertIn("Action 0/20 · Obs 1", obs1["content"][0]["text"])
        elements_before = self.store.get_last_elements(run_id, "clara")
        self.assertEqual(len(elements_before), 2)

        # Step 2: action-only qa_act(observe_after=False)
        act1 = await self._call(
            "qa_act",
            {
                "run_id": run_id,
                "persona_id": "clara",
                "action": {"kind": "tap", "ref": "e1"},
                "observe_after": False,
            },
        )
        self.assertFalse(act1["isError"])
        sc1 = act1["structuredContent"]
        self.assertEqual(sc1["step"], 2)
        self.assertEqual(sc1["action_count"], 1)
        self.assertEqual(sc1["observation_count"], 1)
        self.assertEqual(sc1["max_actions"], 20)
        self.assertIn("Action 1/20 · Obs 1", act1["content"][0]["text"])
        self.assertEqual(len(self.driver.observe_calls), 1)

        # last_elements and last_screenshot MUST remain intact
        self.assertEqual(self.store.get_last_elements(run_id, "clara"), elements_before)

        detail = await self._call(
            "app_qa_get_persona_detail", {"run_id": run_id, "persona_id": "clara"}
        )
        detail_sc = detail["structuredContent"]
        self.assertEqual(len(detail_sc["steps"]), 2)
        self.assertIsNone(detail_sc["steps"][1]["screenshot_file"])
        self.assertEqual(detail_sc["steps"][1]["action"]["kind"], "tap")
        self.assertEqual(detail_sc["state"]["last_screenshot"], "01.png")
        self.assertEqual(detail_sc["state"]["action_count"], 1)
        self.assertEqual(detail_sc["state"]["observation_count"], 1)

        # Follow-up action-only call still receives preserved last_elements for ref resolution
        act2 = await self._call(
            "qa_act",
            {
                "run_id": run_id,
                "persona_id": "clara",
                "action": {"kind": "tap", "ref": "e1"},
                "observe_after": False,
            },
        )
        self.assertEqual(act2["structuredContent"]["step"], 3)
        self.assertEqual(act2["structuredContent"]["action_count"], 2)
        self.assertEqual(act2["structuredContent"]["observation_count"], 1)
        self.assertEqual(len(self.driver.act_calls[-1]["last_elements"] or []), 2)

    async def test_default_include_image_false_and_absolute_screenshot_path(self) -> None:
        create_res = await self._call(
            "qa_create_run", {"bundle_id": "com.example.demo", "personas": ["clara"]}
        )
        run_id = create_res["structuredContent"]["run_id"]
        await self._call("qa_provision_persona", {"run_id": run_id, "persona_id": "clara"})

        # qa_observe without include_image omits image block and prints absolute screenshot_path
        obs = await self._call("qa_observe", {"run_id": run_id, "persona_id": "clara"})
        self.assertFalse(obs["isError"])
        self.assertFalse(any(b.get("type") == "image" for b in obs["content"]))
        obs_path = obs["structuredContent"]["screenshot_path"]
        self.assertTrue(Path(obs_path).is_absolute())
        self.assertTrue(Path(obs_path).is_file())
        self.assertIn(
            f"Screenshot: {obs_path} (inspect with read_file)",
            obs["content"][0]["text"],
        )

        # qa_act(observe_after=True) without include_image also omits image block and prints screenshot_path
        act = await self._call(
            "qa_act",
            {
                "run_id": run_id,
                "persona_id": "clara",
                "action": {"kind": "tap", "ref": "e1"},
                "observe_after": True,
            },
        )
        self.assertFalse(act["isError"])
        self.assertFalse(any(b.get("type") == "image" for b in act["content"]))
        act_path = act["structuredContent"]["screenshot_path"]
        self.assertTrue(Path(act_path).is_absolute())
        self.assertIn(
            f"Screenshot: {act_path} (inspect with read_file)",
            act["content"][0]["text"],
        )

    async def test_ax_unavailable_reason_surfaced_and_persisted(self) -> None:
        create_res = await self._call(
            "qa_create_run", {"bundle_id": "com.example.demo", "personas": ["clara"]}
        )
        run_id = create_res["structuredContent"]["run_id"]
        await self._call("qa_provision_persona", {"run_id": run_id, "persona_id": "clara"})

        reason = "accessibility_info timed out after 8.0s (possible SpringBoard alert)"
        self.driver.next_ax_unavailable_reason = reason
        try:
            obs = await self._call("qa_observe", {"run_id": run_id, "persona_id": "clara"})
        finally:
            self.driver.next_ax_unavailable_reason = None

        self.assertFalse(obs["isError"])
        self.assertEqual(obs["structuredContent"]["ax_unavailable_reason"], reason)
        text = obs["content"][0]["text"]
        self.assertIn(reason, text)
        self.assertIn("read_file", text)
        self.assertIn("coordinates", text)

        detail = await self._call(
            "app_qa_get_persona_detail", {"run_id": run_id, "persona_id": "clara"}
        )
        self.assertEqual(
            detail["structuredContent"]["steps"][0].get("ax_unavailable_reason"), reason
        )

    async def test_max_actions_propagation_to_run_state_and_prompt(self) -> None:
        create_res = await self._call(
            "qa_create_run",
            {
                "bundle_id": "com.example.demo",
                "personas": ["clara"],
                "mode": "explore",
                "max_actions": 15,
            },
        )
        self.assertFalse(create_res["isError"])
        sc = create_res["structuredContent"]
        run_id = sc["run_id"]
        self.assertEqual(sc["max_actions"], 15)
        self.assertEqual(sc["manifest"]["max_actions"], 15)

        state_res = await self._call("app_qa_get_run_state", {"run_id": run_id})
        state_sc = state_res["structuredContent"]
        self.assertEqual(state_sc["manifest"]["max_actions"], 15)
        clara_state = state_sc["personas"][0]
        self.assertEqual(clara_state["max_actions"], 15)
        self.assertEqual(clara_state["max_steps"], 15)
        self.assertEqual(clara_state["action_count"], 0)
        self.assertEqual(clara_state["observation_count"], 0)

        prov = await self._call(
            "qa_provision_persona", {"run_id": run_id, "persona_id": "clara"}
        )
        self.assertFalse(prov["isError"])
        self.assertEqual(prov["structuredContent"]["max_actions"], 15)
        self.assertIn("15", prov["structuredContent"]["persona_prompt"])

    async def test_qa_summarize_run_returns_run_dir_and_grouping_items(self) -> None:
        c = await self._call(
            "qa_create_run",
            {"bundle_id": "com.example.demo", "personas": ["clara", "sven"]},
        )
        run_id = c["structuredContent"]["run_id"]
        expected_run_dir = str(self.store.run_dir(run_id).resolve())

        await self._call(
            "qa_record_findings",
            {
                "run_id": run_id,
                "persona_id": "clara",
                "report_md": "# Clara",
                "findings_json": {
                    "score": 7,
                    "issues": [
                        {
                            "severity": "major",
                            "screen": "Settings",
                            "screens": ["01"],
                            "title": "Appearance picker missing labels",
                            "details": "Segmented picker lacks labels.",
                        }
                    ],
                },
            },
        )
        await self._call(
            "qa_record_findings",
            {
                "run_id": run_id,
                "persona_id": "sven",
                "report_md": "# Sven",
                "findings_json": {
                    "score": 6,
                    "issues": [
                        {
                            "severity": "minor",
                            "screen": "Home",
                            "screens": ["02"],
                            "title": "Headline clips at AX3",
                            "details": "Truncated title text.",
                        }
                    ],
                },
            },
        )

        # Pass 1 (without groups_json): returns run_dir + grouping_items in text & structuredContent
        raw_sum = await self._call("qa_summarize_run", {"run_id": run_id})
        self.assertFalse(raw_sum["isError"])
        raw_sc = raw_sum["structuredContent"]
        raw_text = raw_sum["content"][0]["text"]
        self.assertEqual(raw_sc["run_dir"], expected_run_dir)
        self.assertIn(expected_run_dir, raw_text)
        items = raw_sc["grouping_items"]
        self.assertEqual(len(items), 2)
        self.assertEqual([it["id"] for it in items], [1, 2])
        self.assertIn("1. [clara, major, Settings] Appearance picker missing labels", raw_text)
        self.assertIn("2. [sven, minor, Home] Headline clips at AX3", raw_text)

        # Pass 2 (with groups_json): still includes run_dir, omits grouping_items
        grouped_sum = await self._call(
            "qa_summarize_run",
            {
                "run_id": run_id,
                "groups_json": {
                    "groups": [
                        {"title": "Appearance picker missing labels", "class": "accessibility", "ids": [1]},
                        {"title": "Headline clips at AX3", "class": "layout and text size", "ids": [2]},
                    ]
                },
            },
        )
        self.assertFalse(grouped_sum["isError"])
        self.assertEqual(grouped_sum["structuredContent"]["run_dir"], expected_run_dir)
        self.assertIn(expected_run_dir, grouped_sum["content"][0]["text"])
        self.assertNotIn("grouping_items", grouped_sum["structuredContent"])

        # Empty run (0 issues) returns grouping_items=[]
        c_empty = await self._call(
            "qa_create_run",
            {"bundle_id": "com.example.demo", "personas": ["clara"]},
        )
        empty_id = c_empty["structuredContent"]["run_id"]
        await self._call(
            "qa_record_findings",
            {
                "run_id": empty_id,
                "persona_id": "clara",
                "report_md": "# Clean",
                "findings_json": {"score": 10, "issues": []},
            },
        )
        empty_sum = await self._call("qa_summarize_run", {"run_id": empty_id})
        self.assertEqual(empty_sum["structuredContent"]["grouping_items"], [])
        self.assertIn("no second grouping call needed", empty_sum["content"][0]["text"])

    async def test_qa_build_app_clean_flag_and_derived_data_dir(self) -> None:
        build_calls: list[dict[str, Any]] = []

        async def fake_build(
            project_path: str,
            *,
            scheme: str | None = None,
            configuration: str = "Debug",
            build_args: str | None = None,
            clean: bool = False,
        ) -> dict[str, Any]:
            build_calls.append(
                {
                    "project_path": project_path,
                    "scheme": scheme,
                    "configuration": configuration,
                    "build_args": build_args,
                    "clean": clean,
                }
            )
            return {
                "framework": "native",
                "app_path": "/tmp/Demo.app",
                "bundle_id": "com.example.demo",
                "scheme": scheme or "Demo",
                "derived_data_dir": "/tmp/cache/demo-1234567890ab",
            }

        self.server._build_app_fn = fake_build
        res_clean = await self._call(
            "qa_build_app",
            {"project_path": "/tmp/proj", "scheme": "Demo", "clean": True},
        )
        self.assertFalse(res_clean["isError"])
        self.assertTrue(build_calls[-1]["clean"])
        self.assertEqual(
            res_clean["structuredContent"]["derived_data_dir"],
            "/tmp/cache/demo-1234567890ab",
        )
        self.assertIn("/tmp/cache/demo-1234567890ab", res_clean["content"][0]["text"])

        res_default = await self._call("qa_build_app", {"project_path": "/tmp/proj"})
        self.assertFalse(res_default["isError"])
        self.assertFalse(build_calls[-1]["clean"])


if __name__ == "__main__":
    unittest.main()
