"""MCP App stdio server + loopback streaming/artifact server for ios-agent-testers.

Implements JSON-RPC 2.0 over newline-delimited stdio with zero third-party
dependencies outside terminal-phone's existing idb-venv (Python 3.13 + fb-idb).
All logs go strictly to stderr; stdout carries only MCP JSON-RPC frames.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

# Ensure PLUGIN_ROOT is importable whether invoked as `-m server.main` or `server/main.py`.
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from server.http_server import LoopbackQAServer, _format_exc, guess_mime_type  # noqa: E402

# Strictly log only to stderr — stdout is exclusively MCP JSON-RPC frames.
logging.basicConfig(
    level=os.environ.get("IOS_AGENT_TESTERS_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("ios-agent-testers.server")

SERVER_NAME = "ios-agent-testers"
SERVER_VERSION = "0.2.0"
APP_RESOURCE_URI = "ui://ios-agent-testers/app.html"
APP_MIME_TYPE = "text/html;profile=mcp-app"
RUN_RESOURCE_PREFIX = "ios-testers://run/"

# Guardrails to stay well under Antigravity's 1 MiB trajectory CallToolResult cap.
MAX_INLINE_IMAGE_BYTES = 540_000
MAX_CALL_TOOL_RESULT_BYTES = 950_000

TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {".md", ".markdown", ".json", ".jsonl", ".txt", ".log", ".ips"}
)

PLACEHOLDER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>iOS QA 測試台</title>
  <style>
    :root { color-scheme: light dark; }
    body {
      margin: 0;
      min-height: 100dvh;
      display: grid;
      place-items: center;
      background: var(--color-background-primary, #0f172a);
      color: var(--color-text-primary, #f8fafc);
      font-family: var(--font-sans, -apple-system, BlinkMacSystemFont, sans-serif);
      padding: 24px;
      box-sizing: border-box;
    }
    .card {
      max-width: 400px;
      padding: 22px 26px;
      border-radius: 14px;
      background: var(--color-background-secondary, rgba(255,255,255,0.06));
      border: 1px solid var(--color-border-primary, rgba(255,255,255,0.14));
      text-align: center;
    }
    h1 { font-size: 16px; margin: 0 0 8px; }
    p { font-size: 13px; margin: 0; color: var(--color-text-secondary, #94a3b8); line-height: 1.5; }
    code { font-size: 12px; opacity: 0.9; }
  </style>
</head>
<body>
  <div class="card">
    <h1>iOS QA 測試台建置中</h1>
    <p>找不到前端檔案 <code>dist/app.html</code>。完成 <code>web/</code> 編譯後重新開啟此面板即可自動載入。</p>
  </div>
</body>
</html>
"""

DASHBOARD_META: dict[str, Any] = {
    "ui": {"resourceUri": APP_RESOURCE_URI},
    "com.google.antigravity/ui": {
        "entrypoints": [
            {"type": "aux_pane", "title": "iOS QA 測試台"},
            {"type": "global", "title": "iOS QA 測試台"},
        ]
    },
}

APP_ONLY_META: dict[str, Any] = {
    "ui": {"visibility": ["app"]},
}

ACTION_KINDS: list[str] = [
    "tap",
    "long_press",
    "multi_tap",
    "swipe",
    "scroll",
    "back",
    "dismiss_sheet",
    "type",
    "replace_text",
    "enter",
    "key",
    "button",
    "open",
    "relaunch",
    "open_url",
    "rotate",
    "shake",
    "set_appearance",
    "set_text_size",
    "set_location",
    "permission",
    "wait",
]

TOOLS_SCHEMA: list[dict[str, Any]] = [
    # ------------------------------------------------------------------
    # Model-visible tools (11)
    # ------------------------------------------------------------------
    {
        "name": "open_qa_dashboard",
        "title": "Open iOS QA Dashboard",
        "description": (
            "Open the live iOS Agent Testers QA Dashboard in Antigravity's side pane or "
            "full-page sidebar view to monitor running personas, inspect step screenshots "
            "and A11y overlays, pause/take over a simulator, triage SUMMARY reports, and compare runs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Optional run ID to focus (defaults to the most recent run).",
                },
                "persona_id": {
                    "type": "string",
                    "description": "Optional persona ID to pre-select in the Stage Inspector.",
                },
            },
            "additionalProperties": False,
        },
        "_meta": DASHBOARD_META,
    },
    {
        "name": "qa_list_personas",
        "title": "List iOS QA Personas",
        "description": (
            "List built-in or custom iOS QA tester personas and presets "
            "(smoke, all, iphone, ipad, a11y, or comma-separated persona IDs)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "Preset name (all, smoke, iphone, ipad, a11y) or comma-separated IDs.",
                },
                "custom_personas_path": {
                    "type": "string",
                    "description": "Optional path to a custom personas JSON file.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "qa_build_app",
        "title": "Build iOS Simulator App",
        "description": (
            "Auto-detect the mobile framework (flutter, capacitor, expo, react-native, maui, kmp, native) "
            "and build an unsigned arm64 iOS Simulator .app bundle, returning app_path and bundle_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {
                    "type": "string",
                    "description": "Absolute path to the mobile project directory.",
                },
                "scheme": {
                    "type": "string",
                    "description": "Optional Xcode scheme name override.",
                },
                "configuration": {
                    "type": "string",
                    "description": "Build configuration (default: Debug).",
                },
                "build_args": {
                    "type": "string",
                    "description": "Optional extra build arguments parsed safely with shlex.",
                },
                "clean": {
                    "type": "boolean",
                    "description": "Clean cached DerivedData before building for a fresh rebuild (default: false).",
                },
            },
            "required": ["project_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "qa_create_run",
        "title": "Create iOS QA Run",
        "description": (
            "Initialize a new multi-persona iOS QA run directory and manifest for explore, "
            "reproduce, improve, or targeted testing. Optionally fetches a GitHub issue for reproduce mode."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app_path": {
                    "type": "string",
                    "description": "Path to a built iOS Simulator .app bundle.",
                },
                "bundle_id": {
                    "type": "string",
                    "description": "App CFBundleIdentifier (extracted from app_path if omitted, or use a built-in bundle ID like com.apple.Preferences).",
                },
                "personas": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "Preset (smoke, all, iphone, ipad, a11y), comma-separated IDs, or array of IDs (default: smoke).",
                },
                "custom_personas_path": {
                    "type": "string",
                    "description": "Optional path to a custom personas JSON file.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["explore", "reproduce", "improve", "targeted"],
                    "description": "QA test mode (default: explore).",
                },
                "brief_path": {
                    "type": "string",
                    "description": "Optional path to a custom brief markdown file.",
                },
                "bug_report": {
                    "type": "string",
                    "description": "GitHub issue URL, owner/repo#N reference, or raw bug text (for reproduce mode).",
                },
                "custom_goal": {
                    "type": "string",
                    "description": "Optional targeted scenario or focus goal.",
                },
                "record_video": {
                    "type": "boolean",
                    "description": "Whether to record H.264 simulator video per persona (default: false).",
                },
                "max_concurrency": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 4,
                    "description": "Recommended concurrent simulator cap on this Mac (1..4, default: 2).",
                },
                "max_actions": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Action budget per persona (1..100, default: 30).",
                },
                "runs_dir": {
                    "type": "string",
                    "description": "Optional custom base runs directory.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "qa_provision_persona",
        "title": "Provision Persona Simulator",
        "description": (
            "Create, configure (locale/RTL, US QWERTY keyboard, autocorrect off, theme, Dynamic Type, GPS), "
            "install, and launch the target app on an ephemeral iOS Simulator for one persona. "
            "Returns the simulator UDID and the full rendered persona brief."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "persona_id": {"type": "string"},
                "keep_simulator": {
                    "type": "boolean",
                    "description": "Keep simulator after qa_finish_persona instead of deleting (default: false).",
                },
            },
            "required": ["run_id", "persona_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "qa_observe",
        "title": "Observe Persona Simulator",
        "description": (
            "Capture a point-scaled PNG screenshot, nested accessibility elements with semantic refs (e1..eN), "
            "deterministic A11y audit (unnamed active controls & <44pt targets), crash/process check, and recent app logs. "
            "Respects human takeover pause state cleanly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "persona_id": {"type": "string"},
                "include_image": {
                    "type": "boolean",
                    "description": "Include inline screenshot image block (default: false; inspect the returned screenshot_path via read_file instead).",
                },
                "max_elements": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": 400,
                    "description": "Maximum accessibility elements to return (default: 120).",
                },
            },
            "required": ["run_id", "persona_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "qa_act",
        "title": "Perform Simulator Action",
        "description": (
            "Execute a UI/device action (tap, long_press, multi_tap, swipe, scroll, back, dismiss_sheet, "
            "type, replace_text, enter, key, button, open, relaunch, open_url, rotate, shake, set_appearance, "
            "set_text_size, set_location, permission, wait) and optionally observe immediately afterwards. "
            "Respects human takeover pause state without interfering."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "persona_id": {"type": "string"},
                "action": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ACTION_KINDS},
                        "ref": {"type": "string"},
                        "identifier": {"type": "string"},
                        "label": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "x1": {"type": "number"},
                        "y1": {"type": "number"},
                        "x2": {"type": "number"},
                        "y2": {"type": "number"},
                        "direction": {"type": "string"},
                        "duration": {"type": "number"},
                        "count": {"type": "integer"},
                        "text": {"type": "string"},
                        "key": {"type": "string"},
                        "button": {"type": "string"},
                        "url": {"type": "string"},
                        "bundle_id": {"type": "string"},
                        "orientation": {"type": "string"},
                        "appearance": {"type": "string"},
                        "text_size": {"type": "string"},
                        "lat": {"type": "number"},
                        "lon": {"type": "number"},
                        "service": {"type": "string"},
                        "grant": {"type": "boolean"},
                        "timeout": {"type": "number"},
                        "absent": {"type": "boolean"},
                    },
                    "required": ["kind"],
                    "additionalProperties": True,
                },
                "observe_after": {
                    "type": "boolean",
                    "description": "Automatically capture an observation after the action (default: true).",
                },
                "include_image": {
                    "type": "boolean",
                    "description": "Include inline screenshot image block when observe_after is true (default: false; inspect the returned screenshot_path via read_file instead).",
                },
            },
            "required": ["run_id", "persona_id", "action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "qa_record_findings",
        "title": "Record Persona QA Findings",
        "description": (
            "Validate and persist a persona's narrative report.md and structured findings.json "
            "for the run's test mode (explore, reproduce, improve, targeted)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "persona_id": {"type": "string"},
                "report_md": {
                    "type": "string",
                    "description": "Full markdown narrative report written by the persona tester.",
                },
                "findings_json": {
                    "oneOf": [
                        {"type": "object"},
                        {"type": "string"},
                    ],
                    "description": "Structured findings JSON object (or JSON string) matching the run's mode schema.",
                },
            },
            "required": ["run_id", "persona_id", "report_md", "findings_json"],
            "additionalProperties": False,
        },
    },
    {
        "name": "qa_finish_persona",
        "title": "Finish Persona & Tear Down Simulator",
        "description": (
            "Stop video recording and log tailing, close idb connections, mark the persona finished, "
            "and shut down + delete its ephemeral simulator unless keep_simulator or delete_simulator=false is set."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "persona_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["done", "crashed", "error"],
                    "description": "Final persona status (default: done).",
                },
                "delete_simulator": {
                    "type": "boolean",
                    "description": "Delete the ephemeral simulator after shutdown (default: true).",
                },
            },
            "required": ["run_id", "persona_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "qa_summarize_run",
        "title": "Summarize QA Run",
        "description": (
            "Aggregate all persona findings in a run into SUMMARY.md and summary.json, returning run_dir. "
            "When groups_json is omitted, returns compact grouping_items in both text and structuredContent "
            "for one-step semantic deduplication across the 11 issue classes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "groups_json": {
                    "oneOf": [
                        {"type": "object"},
                        {"type": "array"},
                        {"type": "string"},
                    ],
                    "description": "Optional semantic issue grouping payload ({groups: [{title, class, ids}]} or list).",
                },
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "qa_compare_runs",
        "title": "Compare Two QA Runs",
        "description": (
            "Compare Run A vs Run B (by run_id or directory path) and generate COMPARE.md and compare.json "
            "with overview KPI deltas, persona-by-persona score diffs, unique issues, and improvement area shifts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_a": {"type": "string", "description": "Baseline run_id or run directory path."},
                "run_b": {"type": "string", "description": "Candidate run_id or run directory path."},
                "name_a": {"type": "string", "description": "Display label for Run A."},
                "name_b": {"type": "string", "description": "Display label for Run B."},
            },
            "required": ["run_a", "run_b"],
            "additionalProperties": False,
        },
    },
    # ------------------------------------------------------------------
    # App-only tools (12) — invisible to the model (_meta.ui.visibility: ["app"])
    # ------------------------------------------------------------------
    {
        "name": "app_qa_list_runs",
        "title": "List QA runs (UI)",
        "description": "List available QA runs and loopback HTTP connection info for the dashboard UI.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "_meta": APP_ONLY_META,
    },
    {
        "name": "app_qa_get_run_state",
        "title": "Get QA run state (UI)",
        "description": "Fetch full live state, persona matrix entries, and summary for a QA run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "_meta": APP_ONLY_META,
    },
    {
        "name": "app_qa_get_persona_detail",
        "title": "Get persona detail (UI)",
        "description": "Fetch step filmstrip, A11y findings, logs, crashes, report.md, and findings.json for one persona.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "persona_id": {"type": "string"},
            },
            "required": ["run_id", "persona_id"],
            "additionalProperties": False,
        },
        "_meta": APP_ONLY_META,
    },
    {
        "name": "app_qa_frame",
        "title": "Capture JPEG frame (UI)",
        "description": "Capture a fallback JPEG frame for a running simulator UDID when MJPEG is unavailable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "udid": {"type": "string"},
                "maxWidth": {"type": "number"},
                "quality": {"type": "number"},
            },
            "required": ["udid"],
            "additionalProperties": False,
        },
        "_meta": APP_ONLY_META,
    },
    {
        "name": "app_qa_tap",
        "title": "Tap simulator (UI)",
        "description": "Direct human takeover tap at display points (x, y).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "udid": {"type": "string"},
                "x": {"type": "number"},
                "y": {"type": "number"},
            },
            "required": ["udid", "x", "y"],
            "additionalProperties": False,
        },
        "_meta": APP_ONLY_META,
    },
    {
        "name": "app_qa_swipe",
        "title": "Swipe simulator (UI)",
        "description": "Direct human takeover swipe between display points.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "udid": {"type": "string"},
                "x1": {"type": "number"},
                "y1": {"type": "number"},
                "x2": {"type": "number"},
                "y2": {"type": "number"},
                "duration": {"type": "number"},
            },
            "required": ["udid", "x1", "y1", "x2", "y2"],
            "additionalProperties": False,
        },
        "_meta": APP_ONLY_META,
    },
    {
        "name": "app_qa_type",
        "title": "Type text into simulator (UI)",
        "description": "Direct human takeover text input (ASCII & Unicode).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "udid": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["udid", "text"],
            "additionalProperties": False,
        },
        "_meta": APP_ONLY_META,
    },
    {
        "name": "app_qa_key",
        "title": "Press special key (UI)",
        "description": "Send return, backspace, escape, or tab key to simulator.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "udid": {"type": "string"},
                "key": {
                    "type": "string",
                    "enum": ["return", "backspace", "escape", "tab"],
                },
            },
            "required": ["udid", "key"],
            "additionalProperties": False,
        },
        "_meta": APP_ONLY_META,
    },
    {
        "name": "app_qa_button",
        "title": "Press hardware button (UI)",
        "description": "Press simulator hardware button (HOME, LOCK, SIRI, SIDE_BUTTON).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "udid": {"type": "string"},
                "button": {
                    "type": "string",
                    "enum": ["HOME", "LOCK", "SIRI", "SIDE_BUTTON"],
                },
            },
            "required": ["udid", "button"],
            "additionalProperties": False,
        },
        "_meta": APP_ONLY_META,
    },
    {
        "name": "app_qa_device_action",
        "title": "Perform device action (UI)",
        "description": "Toggle dark/light theme, save a manual screenshot, or stop the simulator.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "udid": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["dark", "light", "save_screenshot", "stop"],
                },
            },
            "required": ["udid", "action"],
            "additionalProperties": False,
        },
        "_meta": APP_ONLY_META,
    },
    {
        "name": "app_qa_toggle_takeover",
        "title": "Toggle human takeover (UI)",
        "description": "Pause or resume the persona agent for direct human control, with an optional steering note.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "persona_id": {"type": "string"},
                "paused": {"type": "boolean"},
                "note": {"type": "string"},
            },
            "required": ["run_id", "persona_id", "paused"],
            "additionalProperties": False,
        },
        "_meta": APP_ONLY_META,
    },
    {
        "name": "app_qa_compare",
        "title": "Compare two QA runs (UI)",
        "description": "Compute Run A vs Run B comparison for the dashboard Compare view.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_a": {"type": "string"},
                "run_b": {"type": "string"},
                "name_a": {"type": "string"},
                "name_b": {"type": "string"},
            },
            "required": ["run_a", "run_b"],
            "additionalProperties": False,
        },
        "_meta": APP_ONLY_META,
    },
]


def _default_runs_root() -> Path:
    override = os.environ.get("IOS_AGENT_TESTERS_RUNS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    data_dir = os.environ.get("IOS_AGENT_TESTERS_DATA", "").strip()
    if data_dir:
        return Path(data_dir).expanduser() / "runs"
    return (
        Path.home()
        / ".gemini"
        / "antigravity"
        / "plugin_data"
        / "ios-agent-testers"
        / "runs"
    )


def _extract_bug_report_title(run_dir: Path) -> str | None:
    bug_path = run_dir / "bug_report.md"
    if not bug_path.is_file():
        return None
    try:
        for line in bug_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped:
                return stripped.lstrip("#").strip() or None
    except Exception:
        return None
    return None


def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("Failed reading %s: %s", path, exc)
        return None


def _default_issue_counts() -> dict[str, int]:
    return {"blocker": 0, "major": 0, "minor": 0, "cosmetic": 0}


def _normalize_persona_entry(
    manifest_persona: dict[str, Any],
    state_entry: dict[str, Any],
) -> dict[str, Any]:
    """Merge static manifest persona metadata with live RunStore state into exact UI schema."""
    pid = str(manifest_persona.get("id") or state_entry.get("id") or "")
    raw_counts = state_entry.get("issue_counts")
    counts = _default_issue_counts()
    if isinstance(raw_counts, dict):
        for sev in ("blocker", "major", "minor", "cosmetic"):
            val = raw_counts.get(sev, 0)
            counts[sev] = int(val) if isinstance(val, (int, float)) else 0

    paused = bool(state_entry.get("paused_by_human", False))
    status = str(state_entry.get("status") or "pending")
    if paused and status == "running":
        status = "paused"

    step_count = int(state_entry.get("step_count") or 0)
    max_actions = int(
        state_entry.get("max_actions") or state_entry.get("max_steps") or 30
    )
    max_steps = int(state_entry.get("max_steps") or max_actions)
    raw_action_count = state_entry.get("action_count")
    action_count = (
        int(raw_action_count)
        if isinstance(raw_action_count, (int, float))
        and not isinstance(raw_action_count, bool)
        else step_count
    )
    raw_obs_count = state_entry.get("observation_count")
    observation_count = (
        int(raw_obs_count)
        if isinstance(raw_obs_count, (int, float))
        and not isinstance(raw_obs_count, bool)
        else step_count
    )

    return {
        "id": pid,
        "profile": str(state_entry.get("profile") or manifest_persona.get("profile") or ""),
        "goal": str(state_entry.get("goal") or manifest_persona.get("goal") or ""),
        "locale": str(state_entry.get("locale") or manifest_persona.get("locale") or "en-US"),
        "device": state_entry.get("device") or manifest_persona.get("device") or {},
        "status": status,
        "udid": state_entry.get("udid"),
        "device_label": state_entry.get("device_label"),
        "executable_name": state_entry.get("executable_name"),
        "width_points": int(state_entry.get("width_points") or 390),
        "height_points": int(state_entry.get("height_points") or 844),
        "step_count": step_count,
        "max_steps": max_steps,
        "action_count": action_count,
        "observation_count": observation_count,
        "max_actions": max_actions,
        "paused_by_human": paused,
        "human_note": state_entry.get("human_note"),
        "issue_counts": counts,
        "score": state_entry.get("score"),
        "reproduced": state_entry.get("reproduced"),
        "goal_reached": state_entry.get("goal_reached"),
        "crash_count": int(state_entry.get("crash_count") or 0),
        "has_video": bool(state_entry.get("has_video", False)),
        "last_screenshot": state_entry.get("last_screenshot"),
        "error": state_entry.get("error"),
    }


def _normalize_personas_list(
    manifest: dict[str, Any], state: dict[str, Any]
) -> list[dict[str, Any]]:
    """Build ordered persona list whether RunStore stores state['personas'] as dict or list."""
    manifest_personas = manifest.get("personas") or []
    manifest_by_id: dict[str, dict[str, Any]] = {}
    manifest_order: list[str] = []
    for item in manifest_personas:
        if isinstance(item, dict) and item.get("id"):
            pid = str(item["id"])
            manifest_by_id[pid] = item
            manifest_order.append(pid)

    raw_state_personas = state.get("personas")
    state_by_id: dict[str, dict[str, Any]] = {}
    state_order: list[str] = []

    if isinstance(raw_state_personas, dict):
        order_hint = state.get("persona_order")
        if isinstance(order_hint, list) and order_hint:
            state_order = [str(x) for x in order_hint if str(x) in raw_state_personas]
        for k, v in raw_state_personas.items():
            if isinstance(v, dict):
                state_by_id[str(k)] = v
                if str(k) not in state_order:
                    state_order.append(str(k))
    elif isinstance(raw_state_personas, list):
        for item in raw_state_personas:
            if isinstance(item, dict) and item.get("id"):
                pid = str(item["id"])
                state_by_id[pid] = item
                state_order.append(pid)

    ordered_ids: list[str] = []
    for pid in manifest_order + state_order:
        if pid not in ordered_ids:
            ordered_ids.append(pid)

    return [
        _normalize_persona_entry(
            manifest_by_id.get(pid, {"id": pid}),
            state_by_id.get(pid, {"id": pid}),
        )
        for pid in ordered_ids
    ]


def _serialized_size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _enforce_result_size_limit(result: dict[str, Any]) -> dict[str, Any]:
    """Ensure CallToolResult stays under Antigravity's 1 MiB trajectory cap."""
    if _serialized_size(result) <= MAX_CALL_TOOL_RESULT_BYTES:
        return result

    # Pass 1: drop inline image blocks (the PNG is already saved on disk and in the UI).
    content = result.get("content")
    if isinstance(content, list):
        new_content = []
        dropped_img = False
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "image":
                dropped_img = True
                continue
            new_content.append(blk)
        if dropped_img:
            new_content.append(
                {
                    "type": "text",
                    "text": "[Inline image omitted to stay under 1 MiB MCP CallToolResult limit; view saved screenshot via artifact/dashboard.]",
                }
            )
            result["content"] = new_content

    if _serialized_size(result) <= MAX_CALL_TOOL_RESULT_BYTES:
        return result

    # Pass 2: trim bulky fields in structuredContent safely.
    sc = result.get("structuredContent")
    if isinstance(sc, dict):
        if isinstance(sc.get("markdown"), str) and len(sc["markdown"]) > 120_000:
            sc["markdown"] = sc["markdown"][:120_000] + "\n\n...[truncated; read full file on disk]"
        if isinstance(sc.get("report_md"), str) and len(sc["report_md"]) > 120_000:
            sc["report_md"] = sc["report_md"][:120_000] + "\n\n...[truncated]"
        if isinstance(sc.get("logs_tail"), list) and len(sc["logs_tail"]) > 40:
            sc["logs_tail"] = sc["logs_tail"][-40:]
        if isinstance(sc.get("elements"), list) and len(sc["elements"]) > 80:
            sc["elements"] = sc["elements"][:80]
        if isinstance(sc.get("data"), str) and len(sc["data"]) > 700_000:
            # Extremely rare for JPEG frames; leave error indicator rather than blowing 1 MiB.
            sc["truncated"] = True

    # Pass 3: truncate oversized text content blocks if still above threshold.
    if _serialized_size(result) > MAX_CALL_TOOL_RESULT_BYTES and isinstance(
        result.get("content"), list
    ):
        for blk in result["content"]:
            if (
                isinstance(blk, dict)
                and blk.get("type") == "text"
                and isinstance(blk.get("text"), str)
                and len(blk["text"]) > 100_000
            ):
                blk["text"] = blk["text"][:100_000] + "\n...[truncated]"

    return result


class MCPQATestersServer:
    """Async JSON-RPC 2.0 MCP App server for ios-agent-testers."""

    def __init__(
        self,
        *,
        store: Any | None = None,
        driver: Any | None = None,
        http_server: LoopbackQAServer | None = None,
        runs_root: Path | None = None,
        # Optional dependency-injection hooks for clean unit testing
        provision_fn: Callable[..., Any] | None = None,
        teardown_fn: Callable[..., Any] | None = None,
        extract_bundle_id_fn: Callable[..., str] | None = None,
        build_app_fn: Callable[..., Any] | None = None,
        select_personas_fn: Callable[..., list[dict[str, Any]]] | None = None,
        bug_report_fn: Callable[..., str] | None = None,
        load_brief_fn: Callable[..., str] | None = None,
        render_prompt_fn: Callable[..., str] | None = None,
        summarize_fn: Callable[..., tuple[str, dict[str, Any]]] | None = None,
        compare_fn: Callable[..., tuple[str, dict[str, Any]]] | None = None,
        validate_findings_fn: Callable[..., list[str]] | None = None,
    ) -> None:
        if store is None:
            from ios_agent_testers.core.run_store import RunStore

            store = RunStore(runs_root or _default_runs_root())
        if driver is None:
            from ios_agent_testers.core.idb_driver import IDBDriver

            driver = IDBDriver()

        self.store = store
        self.driver = driver
        # Maps run_id -> custom RunStore when qa_create_run uses a custom runs_dir.
        self._custom_run_stores: dict[str, Any] = {}
        # Maps (run_id, persona_id) -> ephemeral runtime metadata (run_start_ts, video_pid, etc.)
        self._persona_runtime: dict[tuple[str, str], dict[str, Any]] = {}

        self.http_server = http_server or LoopbackQAServer(
            self.driver,
            self.store,
            store_resolver=self._get_store_for_run,
        )

        self._provision_fn = provision_fn
        self._teardown_fn = teardown_fn
        self._extract_bundle_id_fn = extract_bundle_id_fn
        self._build_app_fn = build_app_fn
        self._select_personas_fn = select_personas_fn
        self._bug_report_fn = bug_report_fn
        self._load_brief_fn = load_brief_fn
        self._render_prompt_fn = render_prompt_fn
        self._summarize_fn = summarize_fn
        self._compare_fn = compare_fn
        self._validate_findings_fn = validate_findings_fn

        self._stdout_lock = asyncio.Lock()
        self._inflight: dict[Any, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------
    # Core module resolution helpers
    # ------------------------------------------------------------------

    def _get_store_for_run(self, run_id: str | None) -> Any:
        if run_id and run_id in self._custom_run_stores:
            return self._custom_run_stores[run_id]
        return self.store

    def _list_all_runs(self) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for row in self.store.list_runs():
            rid = str(row.get("run_id") or "")
            if rid:
                seen[rid] = row
        for custom_store in set(self._custom_run_stores.values()):
            if custom_store is self.store:
                continue
            for row in custom_store.list_runs():
                rid = str(row.get("run_id") or "")
                if rid and rid not in seen:
                    seen[rid] = row
        return sorted(
            seen.values(),
            key=lambda r: (str(r.get("created_at") or ""), str(r.get("run_id") or "")),
            reverse=True,
        )

    def _resolve_run_dir(self, run_ref: str) -> Path:
        ref = run_ref.strip()
        if not ref:
            raise ValueError("Run reference cannot be empty")
        p = Path(ref).expanduser()
        if p.is_dir() and (p / "manifest.json").is_file():
            return p.resolve()
        store = self._get_store_for_run(ref)
        candidate = Path(store.run_dir(ref))
        if candidate.is_dir():
            return candidate.resolve()
        if p.is_dir():
            return p.resolve()
        raise FileNotFoundError(f"Run not found: {run_ref}")

    def _get_select_personas(self) -> Callable[..., list[dict[str, Any]]]:
        if self._select_personas_fn is not None:
            return self._select_personas_fn
        from ios_agent_testers.core.personas import select_personas

        return select_personas

    def _get_presets(self) -> dict[str, list[str]]:
        from ios_agent_testers.core.personas import PRESETS

        return dict(PRESETS)

    def _get_provision_fn(self) -> Callable[..., Any]:
        if self._provision_fn is not None:
            return self._provision_fn
        from ios_agent_testers.core.sim_provisioner import provision_persona_sim

        return provision_persona_sim

    def _get_teardown_fn(self) -> Callable[..., Any]:
        if self._teardown_fn is not None:
            return self._teardown_fn
        from ios_agent_testers.core.sim_provisioner import teardown_persona_sim

        return teardown_persona_sim

    def _get_extract_bundle_id_fn(self) -> Callable[..., str]:
        if self._extract_bundle_id_fn is not None:
            return self._extract_bundle_id_fn
        from ios_agent_testers.core.sim_provisioner import extract_bundle_id

        return extract_bundle_id

    def _get_build_app_fn(self) -> Callable[..., Any]:
        if self._build_app_fn is not None:
            return self._build_app_fn
        from ios_agent_testers.core.builder import build_ios_simulator_app

        return build_ios_simulator_app

    def _get_bug_report_fn(self) -> Callable[..., str]:
        if self._bug_report_fn is not None:
            return self._bug_report_fn
        from ios_agent_testers.core.bug_report import fetch_or_format_bug_report

        return fetch_or_format_bug_report

    def _get_load_brief_fn(self) -> Callable[..., str]:
        if self._load_brief_fn is not None:
            return self._load_brief_fn
        from ios_agent_testers.core.briefs import load_brief

        return load_brief

    def _get_render_prompt_fn(self) -> Callable[..., str]:
        if self._render_prompt_fn is not None:
            return self._render_prompt_fn
        from ios_agent_testers.core.briefs import render_persona_prompt

        return render_persona_prompt

    def _get_summarize_fn(self) -> Callable[..., tuple[str, dict[str, Any]]]:
        if self._summarize_fn is not None:
            return self._summarize_fn
        from ios_agent_testers.core.summarize import summarize_run

        return summarize_run

    def _get_compare_fn(self) -> Callable[..., tuple[str, dict[str, Any]]]:
        if self._compare_fn is not None:
            return self._compare_fn
        from ios_agent_testers.core.compare import compare_runs

        return compare_runs

    def _get_validate_findings_fn(self) -> Callable[..., list[str]]:
        if self._validate_findings_fn is not None:
            return self._validate_findings_fn
        from ios_agent_testers.core.run_store import validate_findings

        return validate_findings

    # ------------------------------------------------------------------
    # MCP UI Resource helpers
    # ------------------------------------------------------------------

    def _ui_resource_meta(self) -> dict[str, Any]:
        return {
            "ui": {
                "prefersBorder": False,
                "csp": {
                    "resourceDomains": [self.http_server.origin],
                    "connectDomains": [self.http_server.origin],
                },
            }
        }

    def _read_app_html(self) -> str:
        html_path_str = os.environ.get("IOS_AGENT_TESTERS_HTML") or str(
            PLUGIN_ROOT / "dist" / "app.html"
        )
        html_path = Path(html_path_str)
        if html_path.is_file():
            try:
                text = html_path.read_text(encoding="utf-8")
                if text.strip():
                    return text
            except Exception as exc:
                logger.warning("Failed reading %s: %s", html_path, exc)
        return PLACEHOLDER_HTML

    # ------------------------------------------------------------------
    # State lookup helpers
    # ------------------------------------------------------------------

    def _find_persona_in_state(
        self, store: Any, run_id: str, persona_id: str
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        manifest = store.get_manifest(run_id)
        state = store.get_state(run_id)
        personas_list = _normalize_personas_list(manifest, state)
        persona_state = next((p for p in personas_list if p["id"] == persona_id), None)
        if persona_state is None:
            raise ValueError(f"Persona '{persona_id}' not found in run '{run_id}'")
        manifest_persona = next(
            (
                p
                for p in (manifest.get("personas") or [])
                if isinstance(p, dict) and p.get("id") == persona_id
            ),
            {"id": persona_id},
        )
        return manifest, manifest_persona, persona_state

    def _find_active_persona_by_udid(self, udid: str) -> tuple[Any, str, str] | None:
        for row in self._list_all_runs():
            rid = str(row.get("run_id") or "")
            if not rid:
                continue
            store = self._get_store_for_run(rid)
            try:
                manifest = store.get_manifest(rid)
                state = store.get_state(rid)
            except Exception:
                continue
            for p in _normalize_personas_list(manifest, state):
                if p.get("udid") == udid:
                    return store, rid, p["id"]
        return None

    async def _build_image_block(
        self, udid: str, png_path: Path
    ) -> dict[str, Any] | None:
        if not png_path.is_file():
            return None
        png_bytes = await asyncio.to_thread(png_path.read_bytes)
        if len(png_bytes) <= MAX_INLINE_IMAGE_BYTES:
            return {
                "type": "image",
                "data": base64.b64encode(png_bytes).decode("ascii"),
                "mimeType": "image/png",
            }
        # Fall back to compressed JPEG frame so we stay well below the 1 MiB CallToolResult limit.
        with contextlib.suppress(Exception):
            jpeg_bytes = await self.driver.get_jpeg_frame(
                udid, max_width=480, quality=0.72
            )
            if len(jpeg_bytes) <= MAX_INLINE_IMAGE_BYTES:
                return {
                    "type": "image",
                    "data": base64.b64encode(jpeg_bytes).decode("ascii"),
                    "mimeType": "image/jpeg",
                }
        return None

    def _format_observation_text(
        self,
        *,
        run_id: str,
        persona_id: str,
        step: int,
        screenshot_file: str,
        screenshot_path: str | None = None,
        obs: dict[str, Any],
        action_res: dict[str, Any] | None = None,
        human_note: str | None = None,
        action_count: int | None = None,
        observation_count: int | None = None,
        max_actions: int | None = None,
    ) -> str:
        header = f"Run {run_id} / {persona_id} — Step {step:02d} ({screenshot_file})"
        if (
            action_count is not None
            and max_actions is not None
            and observation_count is not None
        ):
            header += f" | Action {action_count}/{max_actions} · Obs {observation_count}"
        eff_screenshot_path = obs.get("screenshot_path") or screenshot_path or screenshot_file
        lines: list[str] = [
            header,
            f"Screenshot: {eff_screenshot_path} (inspect with read_file)",
            f"Screen: {obs.get('width_points')}x{obs.get('height_points')} pt | app_running={obs.get('app_running')}",
        ]
        if human_note:
            lines.append(f"HUMAN STEERING NOTE (from operator after resume): {human_note}")
        if action_res is not None:
            lines.append(
                f"Action: {action_res.get('kind')} ok={action_res.get('ok')} detail={action_res.get('detail', '')}"
            )
        if obs.get("app_running") is False:
            lines.append("WARNING: Target app process is NOT running (possible crash or termination).")
        crashes = obs.get("crashes") or []
        if crashes:
            names = ", ".join(str(c.get("name") or "crash") for c in crashes[:5])
            lines.append(f"CRASHES DETECTED ({len(crashes)}): {names}")
        ax_unavailable_reason = obs.get("ax_unavailable_reason")
        if ax_unavailable_reason:
            lines.append(
                f"WARNING: Accessibility tree unavailable ({ax_unavailable_reason}) — "
                "inspect screenshot_path via read_file and tap by display-point coordinates (x, y) if needed."
            )
        a11y_line = obs.get("a11y_line")
        if a11y_line:
            lines.append(f"A11y: {a11y_line}")
        recent_logs = obs.get("recent_logs") or []
        if recent_logs:
            lines.append("Recent logs:\n" + "\n".join(str(x) for x in recent_logs[-10:]))
        elements_text = obs.get("elements_text") or ""
        if elements_text:
            lines.append("Elements:\n" + str(elements_text))
        return "\n".join(lines)

    async def _perform_observation(
        self,
        *,
        store: Any,
        run_id: str,
        persona_id: str,
        udid: str,
        bundle_id: str | None,
        action_for_record: dict[str, Any] | None,
        action_res: dict[str, Any] | None,
        include_image: bool,
        max_elements: int,
        human_note: str | None,
    ) -> dict[str, Any]:
        persona_dir = Path(store.persona_dir(run_id, persona_id))
        persona_dir.mkdir(parents=True, exist_ok=True)
        crashes_dir = persona_dir / "crashes"
        step = int(store.next_step_number(run_id, persona_id))
        screenshot_file = f"{step:02d}.png"
        output_png = persona_dir / screenshot_file

        runtime = self._persona_runtime.get((run_id, persona_id)) or {}
        run_start_ts = float(runtime.get("run_start_ts") or time.time() - 60.0)

        obs = await self.driver.observe(
            udid,
            bundle_id=bundle_id,
            output_png=output_png,
            run_start_ts=run_start_ts,
            crashes_dir=crashes_dir,
            max_elements=max_elements,
        )

        crashes = obs.get("crashes") or []
        recent_logs = obs.get("recent_logs") or []
        elements = obs.get("elements") or []
        app_running = obs.get("app_running")
        ax_unavailable_reason = obs.get("ax_unavailable_reason")

        store.record_step(
            run_id,
            persona_id,
            step,
            screenshot_file=screenshot_file,
            action=action_for_record,
            a11y=obs.get("a11y"),
            app_running=app_running,
            crashes=crashes,
            recent_logs=recent_logs,
            elements=elements,
            ax_unavailable_reason=ax_unavailable_reason,
        )

        state_updates: dict[str, Any] = {
            "width_points": int(obs.get("width_points") or 390),
            "height_points": int(obs.get("height_points") or 844),
            "last_screenshot": screenshot_file,
        }
        if app_running is False or crashes:
            state_updates["status"] = "crashed"
        else:
            state_updates["status"] = "running"
        store.update_persona_state(run_id, persona_id, **state_updates)

        _, _, updated_pstate = self._find_persona_in_state(store, run_id, persona_id)
        action_count = int(updated_pstate["action_count"])
        observation_count = int(updated_pstate["observation_count"])
        max_actions = int(updated_pstate["max_actions"])

        text = self._format_observation_text(
            run_id=run_id,
            persona_id=persona_id,
            step=step,
            screenshot_file=screenshot_file,
            screenshot_path=str(output_png),
            obs=obs,
            action_res=action_res,
            human_note=human_note,
            action_count=action_count,
            observation_count=observation_count,
            max_actions=max_actions,
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if include_image:
            img_block = await self._build_image_block(udid, output_png)
            if img_block is not None:
                content.append(img_block)

        structured: dict[str, Any] = {
            "ok": True,
            "paused_by_human": False,
            "human_note": human_note,
            "run_id": run_id,
            "persona_id": persona_id,
            "step": step,
            "action_count": action_count,
            "observation_count": observation_count,
            "max_actions": max_actions,
            "screenshot_file": screenshot_file,
            "screenshot_path": str(obs.get("screenshot_path") or output_png),
            "width_points": int(obs.get("width_points") or 390),
            "height_points": int(obs.get("height_points") or 844),
            "elements": elements,
            "a11y": obs.get("a11y"),
            "a11y_line": obs.get("a11y_line"),
            "ax_unavailable_reason": ax_unavailable_reason,
            "app_running": app_running,
            "crashes": crashes,
            "recent_logs": recent_logs,
        }
        if action_res is not None:
            structured["action_executed"] = True
            structured["action_result"] = action_res

        return {
            "content": content,
            "structuredContent": structured,
            "isError": False,
        }

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = arguments if isinstance(arguments, dict) else {}
        try:
            res = await self._dispatch_tool(name, args)
            return _enforce_result_size_limit(res)
        except KeyError as exc:
            msg = f"Missing required parameter: {exc}"
            return {
                "content": [{"type": "text", "text": msg}],
                "structuredContent": {"ok": False, "error": msg},
                "isError": True,
            }
        except Exception as exc:
            msg = _format_exc(exc)
            logger.warning("Tool %s error: %s", name, msg)
            return {
                "content": [{"type": "text", "text": msg}],
                "structuredContent": {"ok": False, "error": msg},
                "isError": True,
            }

    async def _dispatch_tool(
        self, name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        # 1. open_qa_dashboard
        if name == "open_qa_dashboard":
            runs = self._list_all_runs()
            requested_run = str(args.get("run_id") or "").strip() or None
            requested_persona = str(args.get("persona_id") or "").strip() or None

            active_run_id: str | None = None
            if requested_run:
                active_run_id = requested_run
            elif runs:
                active_run_id = str(runs[0].get("run_id") or "") or None

            active_persona_id = requested_persona
            if active_run_id and not active_persona_id:
                store = self._get_store_for_run(active_run_id)
                with contextlib.suppress(Exception):
                    manifest = store.get_manifest(active_run_id)
                    state = store.get_state(active_run_id)
                    plist = _normalize_personas_list(manifest, state)
                    running = next(
                        (p["id"] for p in plist if p["status"] in ("running", "paused")),
                        None,
                    )
                    active_persona_id = running or (plist[0]["id"] if plist else None)

            # CRITICAL: streamToken MUST ONLY live in structuredContent, NEVER in text content!
            structured = {
                "runs": runs,
                "activeRunId": active_run_id,
                "activePersonaId": active_persona_id,
                "httpBaseUrl": self.http_server.origin,
                "streamToken": self.http_server.token,
            }
            if active_run_id:
                text = (
                    f"Opened iOS QA Dashboard ({len(runs)} run(s) available; "
                    f"active run: {active_run_id}"
                    + (f", persona: {active_persona_id}" if active_persona_id else "")
                    + ")."
                )
            else:
                text = (
                    "Opened iOS QA Dashboard. No QA runs exist yet — call qa_create_run "
                    "to start a multi-persona iOS test run."
                )
            return {
                "content": [{"type": "text", "text": text}],
                "structuredContent": structured,
                "isError": False,
            }

        # 2. qa_list_personas
        if name == "qa_list_personas":
            selector = str(args.get("selector") or "all").strip() or "all"
            custom_path = args.get("custom_personas_path")
            select_fn = self._get_select_personas()
            personas = select_fn(selector=selector, custom_path=custom_path)
            presets = self._get_presets()
            lines = [f"Selected {len(personas)} persona(s) for selector '{selector}':"]
            for p in personas:
                dev = p.get("device") or {}
                lines.append(
                    f"- {p.get('id')}: {dev.get('simulator')} | {p.get('locale')} | "
                    f"{dev.get('text_size')} | {dev.get('theme')} — {p.get('profile')}"
                )
            return {
                "content": [{"type": "text", "text": "\n".join(lines)}],
                "structuredContent": {
                    "ok": True,
                    "selector": selector,
                    "count": len(personas),
                    "presets": presets,
                    "personas": personas,
                },
                "isError": False,
            }

        # 3. qa_build_app
        if name == "qa_build_app":
            project_path = str(args["project_path"]).strip()
            if not project_path:
                raise ValueError("project_path is required")
            scheme = args.get("scheme")
            configuration = str(args.get("configuration") or "Debug")
            build_args = args.get("build_args")
            clean = bool(args.get("clean", False))
            build_fn = self._get_build_app_fn()
            res = await build_fn(
                project_path,
                scheme=scheme,
                configuration=configuration,
                build_args=build_args,
                clean=clean,
            )
            derived_data_dir = res.get("derived_data_dir")
            text = (
                f"Built iOS Simulator app ({res.get('framework')}): "
                f"app_path={res.get('app_path')} bundle_id={res.get('bundle_id')} "
                f"scheme={res.get('scheme')}"
                + (f" derived_data_dir={derived_data_dir}" if derived_data_dir else "")
            )
            structured: dict[str, Any] = {"ok": True, **res}
            if "derived_data_dir" not in structured:
                structured["derived_data_dir"] = None
            return {
                "content": [{"type": "text", "text": text}],
                "structuredContent": structured,
                "isError": False,
            }

        # 4. qa_create_run
        if name == "qa_create_run":
            mode = str(args.get("mode") or "explore").strip()
            from ios_agent_testers.core.run_store import VALID_MODES, RunStore

            if mode not in VALID_MODES:
                raise ValueError(
                    f"Invalid mode '{mode}'; expected one of {', '.join(VALID_MODES)}"
                )

            raw_personas = args.get("personas")
            if isinstance(raw_personas, list):
                selector_str = ",".join(str(x).strip() for x in raw_personas if str(x).strip())
            elif isinstance(raw_personas, str) and raw_personas.strip():
                selector_str = raw_personas.strip()
            else:
                selector_str = "smoke"

            select_fn = self._get_select_personas()
            personas = select_fn(
                selector=selector_str,
                custom_path=args.get("custom_personas_path"),
            )
            if not personas:
                raise ValueError(f"No personas resolved for selector '{selector_str}'")

            app_path = str(args.get("app_path") or "").strip() or None
            bundle_id = str(args.get("bundle_id") or "").strip() or None
            if app_path and not bundle_id:
                app_p = Path(app_path).expanduser()
                if app_p.exists():
                    bundle_id = self._get_extract_bundle_id_fn()(app_p)

            bug_report_raw = args.get("bug_report")
            bug_report_md: str | None = None
            if isinstance(bug_report_raw, str) and bug_report_raw.strip():
                bug_report_md = await asyncio.to_thread(
                    self._get_bug_report_fn(), bug_report_raw.strip()
                )

            max_concurrency = max(1, min(4, int(args.get("max_concurrency", 2))))
            max_actions = max(1, min(100, int(args.get("max_actions", 30))))
            record_video = bool(args.get("record_video", False))
            brief_path = str(args.get("brief_path") or "").strip() or None
            custom_goal = str(args.get("custom_goal") or "").strip() or None

            runs_dir_arg = str(args.get("runs_dir") or "").strip()
            if runs_dir_arg:
                target_store = RunStore(Path(runs_dir_arg).expanduser())
            else:
                target_store = self.store

            manifest = target_store.create_run(
                app_path=app_path,
                bundle_id=bundle_id,
                personas=personas,
                mode=mode,
                brief_path=brief_path,
                bug_report_md=bug_report_md,
                custom_goal=custom_goal,
                record_video=record_video,
                max_concurrency=max_concurrency,
                max_actions=max_actions,
            )
            run_id = str(manifest["run_id"])
            if target_store is not self.store:
                self._custom_run_stores[run_id] = target_store

            run_dir = Path(target_store.run_dir(run_id))
            persona_ids = [str(p["id"]) for p in personas]
            text = (
                f"Created QA run {run_id} (mode={mode}, personas={', '.join(persona_ids)}, "
                f"max_concurrency={max_concurrency}, max_actions={max_actions}, "
                f"bundle_id={bundle_id or 'unspecified'}). "
                f"Directory: {run_dir}"
            )
            return {
                "content": [{"type": "text", "text": text}],
                "structuredContent": {
                    "ok": True,
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                    "mode": mode,
                    "app_path": app_path,
                    "bundle_id": bundle_id,
                    "personas": persona_ids,
                    "max_concurrency": max_concurrency,
                    "max_actions": max_actions,
                    "record_video": record_video,
                    "bug_report_saved": bool(bug_report_md),
                    "manifest": manifest,
                },
                "isError": False,
            }

        # 5. qa_provision_persona
        if name == "qa_provision_persona":
            run_id = str(args["run_id"]).strip()
            persona_id = str(args["persona_id"]).strip()
            keep_simulator = bool(args.get("keep_simulator", False))

            store = self._get_store_for_run(run_id)
            manifest, manifest_persona, _ = self._find_persona_in_state(
                store, run_id, persona_id
            )

            store.update_persona_state(
                run_id,
                persona_id,
                status="provisioning",
                error=None,
            )

            record_video = bool(manifest.get("record_video", False))
            video_path: Path | None = None
            if record_video:
                video_dir = Path(store.persona_dir(run_id, persona_id)) / "video"
                video_dir.mkdir(parents=True, exist_ok=True)
                video_path = video_dir / "rec_01.mp4"

            try:
                prov = await self._get_provision_fn()(
                    run_id,
                    manifest_persona,
                    app_path=manifest.get("app_path"),
                    bundle_id=manifest.get("bundle_id") or manifest.get("app_id"),
                    record_video=record_video,
                    video_path=video_path,
                    keep_simulator=keep_simulator,
                )
            except Exception as exc:
                store.update_persona_state(
                    run_id,
                    persona_id,
                    status="error",
                    error=_format_exc(exc),
                )
                raise

            udid = str(prov["udid"])
            ephemeral = bool(prov.get("ephemeral", True))
            bundle_id = prov.get("bundle_id") or manifest.get("bundle_id") or manifest.get("app_id")
            exec_name = str(prov.get("executable_name") or prov.get("process_name") or "").strip() or None

            await self.driver.connect(udid, ephemeral=ephemeral)
            try:
                dims = await self.driver.describe_screen(udid)
                width_points = int(dims.get("width_points") or 390)
                height_points = int(dims.get("height_points") or 844)
            except Exception as exc:
                logger.warning("describe_screen failed for %s: %s", udid, _format_exc(exc))
                width_points, height_points = 390, 844

            log_file = Path(store.persona_dir(run_id, persona_id)) / "logs.txt"
            with contextlib.suppress(Exception):
                await self.driver.start_log_tail(
                    udid,
                    log_file,
                    bundle_id=bundle_id,
                    process_name=exec_name,
                )

            run_start_ts = time.time()
            self._persona_runtime[(run_id, persona_id)] = {
                "run_start_ts": run_start_ts,
                "video_pid": prov.get("video_pid"),
                "ephemeral": ephemeral,
                "keep_simulator": keep_simulator,
                "bundle_id": bundle_id,
                "executable_name": exec_name,
            }

            device_label = str(prov.get("device_label") or "")
            store.update_persona_state(
                run_id,
                persona_id,
                status="running",
                udid=udid,
                device_label=device_label,
                executable_name=exec_name,
                width_points=width_points,
                height_points=height_points,
                has_video=bool(prov.get("video_path")),
                video_pid=prov.get("video_pid"),
                ephemeral=ephemeral,
                keep_simulator=keep_simulator,
                bundle_id=bundle_id,
                error=None,
            )

            mode = str(manifest.get("mode") or "explore")
            max_actions = int(manifest.get("max_actions") or 30)
            brief_text = self._get_load_brief_fn()(
                mode=mode, brief_path=manifest.get("brief_path")
            )
            bug_report_path = Path(store.run_dir(run_id)) / "bug_report.md"
            bug_report_md = (
                bug_report_path.read_text(encoding="utf-8", errors="replace")
                if bug_report_path.is_file()
                else None
            )
            persona_prompt = self._get_render_prompt_fn()(
                manifest_persona,
                run_id=run_id,
                mode=mode,
                bundle_id=str(bundle_id or ""),
                device_label=device_label,
                brief_text=brief_text,
                bug_report_md=bug_report_md,
                custom_goal=manifest.get("custom_goal"),
                max_actions=max_actions,
            )

            fallback_note = (
                f" (fallback: {prov['fallback_reason']})"
                if prov.get("fallback_reason")
                else ""
            )
            text = (
                f"Provisioned simulator for {persona_id} in run {run_id}: "
                f"udid={udid}, {device_label}{fallback_note}, {width_points}x{height_points}pt.\n\n"
                f"--- PERSONA BRIEF ---\n{persona_prompt}"
            )
            return {
                "content": [{"type": "text", "text": text}],
                "structuredContent": {
                    "ok": True,
                    "run_id": run_id,
                    "persona_id": persona_id,
                    "udid": udid,
                    "device_label": device_label,
                    "runtime_version": prov.get("runtime_version"),
                    "bundle_id": bundle_id,
                    "executable_name": exec_name,
                    "width_points": width_points,
                    "height_points": height_points,
                    "ephemeral": ephemeral,
                    "has_video": bool(prov.get("video_path")),
                    "fallback_reason": prov.get("fallback_reason"),
                    "max_actions": max_actions,
                    "persona_prompt": persona_prompt,
                },
                "isError": False,
            }

        # 6. qa_observe
        if name == "qa_observe":
            run_id = str(args["run_id"]).strip()
            persona_id = str(args["persona_id"]).strip()
            include_image = bool(args.get("include_image", False))
            max_elements = max(10, min(400, int(args.get("max_elements", 120))))

            store = self._get_store_for_run(run_id)
            manifest, _, pstate = self._find_persona_in_state(store, run_id, persona_id)

            # Respect human takeover pause state without fighting the human.
            takeover = store.get_takeover(run_id, persona_id) or {}
            if takeover.get("paused_by_human"):
                note = takeover.get("human_note")
                msg = (
                    f"Persona '{persona_id}' is currently paused by the human operator "
                    f"in the QA Dashboard. Wait briefly and call qa_observe again until resumed."
                )
                if note:
                    msg += f" Current human note: {note}"
                return {
                    "content": [{"type": "text", "text": msg}],
                    "structuredContent": {
                        "ok": True,
                        "paused_by_human": True,
                        "human_note": note,
                        "run_id": run_id,
                        "persona_id": persona_id,
                    },
                    "isError": False,
                }

            # Consume any steering note left when the human resumed control.
            human_note = store.consume_human_note(run_id, persona_id)

            udid = pstate.get("udid")
            if not udid:
                raise RuntimeError(
                    f"Persona '{persona_id}' in run '{run_id}' has no provisioned simulator; call qa_provision_persona first."
                )
            runtime = self._persona_runtime.get((run_id, persona_id)) or {}
            bundle_id = runtime.get("bundle_id") or manifest.get("bundle_id") or manifest.get("app_id")

            return await self._perform_observation(
                store=store,
                run_id=run_id,
                persona_id=persona_id,
                udid=str(udid),
                bundle_id=bundle_id,
                action_for_record=None,
                action_res=None,
                include_image=include_image,
                max_elements=max_elements,
                human_note=human_note,
            )

        # 7. qa_act
        if name == "qa_act":
            run_id = str(args["run_id"]).strip()
            persona_id = str(args["persona_id"]).strip()
            action = args["action"]
            if not isinstance(action, dict) or not action.get("kind"):
                raise ValueError("action must be an object with a non-empty 'kind'")
            observe_after = bool(args.get("observe_after", True))
            include_image = bool(args.get("include_image", False))

            store = self._get_store_for_run(run_id)
            manifest, _, pstate = self._find_persona_in_state(store, run_id, persona_id)

            # Respect human takeover pause state without touching the simulator.
            takeover = store.get_takeover(run_id, persona_id) or {}
            if takeover.get("paused_by_human"):
                note = takeover.get("human_note")
                msg = (
                    f"Persona '{persona_id}' is paused by the human operator; "
                    f"skipped action '{action.get('kind')}'. Wait briefly and poll qa_observe until resumed."
                )
                if note:
                    msg += f" Current human note: {note}"
                return {
                    "content": [{"type": "text", "text": msg}],
                    "structuredContent": {
                        "ok": True,
                        "paused_by_human": True,
                        "human_note": note,
                        "action_executed": False,
                        "run_id": run_id,
                        "persona_id": persona_id,
                    },
                    "isError": False,
                }

            human_note = store.consume_human_note(run_id, persona_id)

            udid = pstate.get("udid")
            if not udid:
                raise RuntimeError(
                    f"Persona '{persona_id}' in run '{run_id}' has no provisioned simulator; call qa_provision_persona first."
                )
            runtime = self._persona_runtime.get((run_id, persona_id)) or {}
            bundle_id = runtime.get("bundle_id") or manifest.get("bundle_id") or manifest.get("app_id")
            last_elements = store.get_last_elements(run_id, persona_id)

            act_res = await self.driver.act(
                str(udid),
                action,
                bundle_id=bundle_id,
                last_elements=last_elements,
            )

            action_record = {
                "kind": act_res.get("kind") or action.get("kind"),
                "detail": act_res.get("detail") or "",
                "ok": bool(act_res.get("ok", True)),
                "resolved_point": act_res.get("resolved_point"),
            }

            if observe_after:
                return await self._perform_observation(
                    store=store,
                    run_id=run_id,
                    persona_id=persona_id,
                    udid=str(udid),
                    bundle_id=bundle_id,
                    action_for_record=action_record,
                    action_res=act_res,
                    include_image=include_image,
                    max_elements=120,
                    human_note=human_note,
                )

            step = int(store.next_step_number(run_id, persona_id))
            store.record_step(
                run_id,
                persona_id,
                step,
                screenshot_file=None,
                action=action_record,
                a11y=None,
                app_running=None,
                crashes=(),
                recent_logs=(),
                elements=None,
            )
            _, _, updated_pstate = self._find_persona_in_state(store, run_id, persona_id)
            action_count = int(updated_pstate["action_count"])
            observation_count = int(updated_pstate["observation_count"])
            max_actions = int(updated_pstate["max_actions"])

            note_line = (
                f"\nHUMAN STEERING NOTE (from operator after resume): {human_note}"
                if human_note
                else ""
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Run {run_id} / {persona_id} — Step {step:02d} (action-only) | "
                            f"Action {action_count}/{max_actions} · Obs {observation_count}\n"
                            f"Action '{action_record['kind']}' ok={action_record['ok']}: "
                            f"{action_record['detail']}{note_line}"
                        ),
                    }
                ],
                "structuredContent": {
                    "ok": bool(act_res.get("ok", True)),
                    "paused_by_human": False,
                    "human_note": human_note,
                    "run_id": run_id,
                    "persona_id": persona_id,
                    "step": step,
                    "action_count": action_count,
                    "observation_count": observation_count,
                    "max_actions": max_actions,
                    "action_executed": True,
                    "action_result": act_res,
                },
                "isError": not bool(act_res.get("ok", True)),
            }

        # 8. qa_record_findings
        if name == "qa_record_findings":
            run_id = str(args["run_id"]).strip()
            persona_id = str(args["persona_id"]).strip()
            report_md = str(args["report_md"])
            raw_findings = args["findings_json"]

            if isinstance(raw_findings, str):
                findings = json.loads(raw_findings)
            elif isinstance(raw_findings, dict):
                findings = raw_findings
            else:
                raise ValueError("findings_json must be a JSON object or JSON string")

            store = self._get_store_for_run(run_id)
            manifest, _, _ = self._find_persona_in_state(store, run_id, persona_id)
            mode = str(manifest.get("mode") or "explore")

            errors = self._get_validate_findings_fn()(findings, mode=mode)
            if errors:
                err_text = "findings_json validation failed:\n- " + "\n- ".join(errors)
                return {
                    "content": [{"type": "text", "text": err_text}],
                    "structuredContent": {
                        "ok": False,
                        "errors": errors,
                        "run_id": run_id,
                        "persona_id": persona_id,
                    },
                    "isError": True,
                }

            saved = store.save_findings(run_id, persona_id, report_md, findings)
            issue_count = len(findings.get("issues") or [])
            score = findings.get("score")
            text = (
                f"Recorded findings for {persona_id} in run {run_id}: "
                f"score={score}, issues={issue_count}."
            )
            return {
                "content": [{"type": "text", "text": text}],
                "structuredContent": {
                    "ok": True,
                    "run_id": run_id,
                    "persona_id": persona_id,
                    **(saved if isinstance(saved, dict) else {}),
                },
                "isError": False,
            }

        # 9. qa_finish_persona
        if name == "qa_finish_persona":
            run_id = str(args["run_id"]).strip()
            persona_id = str(args["persona_id"]).strip()
            final_status = str(args.get("status") or "done").strip()
            if final_status not in ("done", "crashed", "error"):
                raise ValueError(f"Invalid finish status: {final_status}")
            delete_simulator = bool(args.get("delete_simulator", True))

            store = self._get_store_for_run(run_id)
            _, _, pstate = self._find_persona_in_state(store, run_id, persona_id)

            raw_state = store.get_state(run_id)
            raw_personas = raw_state.get("personas")
            raw_entry: dict[str, Any] = {}
            if isinstance(raw_personas, dict):
                raw_entry = raw_personas.get(persona_id) or {}
            elif isinstance(raw_personas, list):
                raw_entry = next(
                    (x for x in raw_personas if isinstance(x, dict) and x.get("id") == persona_id),
                    {},
                )

            runtime = self._persona_runtime.pop((run_id, persona_id), {})
            udid = pstate.get("udid")
            ephemeral = bool(runtime.get("ephemeral", raw_entry.get("ephemeral", True)))
            keep_sim = bool(runtime.get("keep_simulator", raw_entry.get("keep_simulator", False)))
            video_pid = runtime.get("video_pid", raw_entry.get("video_pid"))
            should_delete = delete_simulator and not keep_sim

            teardown_res: dict[str, Any] = {"deleted": False, "shutdown": False}
            if udid:
                with contextlib.suppress(Exception):
                    await self.driver.stop_log_tail(str(udid))
                with contextlib.suppress(Exception):
                    await self.driver.close_udid(str(udid))
                teardown_res = await self._get_teardown_fn()(
                    str(udid),
                    ephemeral=ephemeral,
                    delete_simulator=should_delete,
                    video_pid=video_pid,
                )

            deleted = bool(teardown_res.get("deleted", False))
            store.update_persona_state(
                run_id,
                persona_id,
                status=final_status,
                paused_by_human=False,
                udid=None if deleted else udid,
                video_pid=None,
            )

            text = (
                f"Finished persona '{persona_id}' in run {run_id} "
                f"(status={final_status}, simulator_deleted={deleted})."
            )
            return {
                "content": [{"type": "text", "text": text}],
                "structuredContent": {
                    "ok": True,
                    "run_id": run_id,
                    "persona_id": persona_id,
                    "status": final_status,
                    "simulator_deleted": deleted,
                    "teardown": teardown_res,
                },
                "isError": False,
            }

        # 10. qa_summarize_run
        if name == "qa_summarize_run":
            run_id = str(args["run_id"]).strip()
            groups_json = args.get("groups_json")
            run_dir = self._resolve_run_dir(run_id)
            md, summary_dict = await asyncio.to_thread(
                self._get_summarize_fn(),
                run_dir,
                groups_json=groups_json,
                use_external_llm=False,
            )
            summary_md_path = run_dir / "SUMMARY.md"
            summary_json_path = run_dir / "summary.json"
            lines = [
                f"Summarized run {run_id}: {summary_dict.get('persona_count', 0)} persona(s), "
                f"{summary_dict.get('distinct_issue_count', 0)} distinct issue(s) "
                f"({summary_dict.get('report_count', 0)} total reports), "
                f"avg_score={summary_dict.get('avg_score')}.",
                f"Run directory: {run_dir}",
                f"Saved to: {summary_md_path}",
            ]
            structured_sum: dict[str, Any] = {
                "ok": True,
                "run_id": run_id,
                "run_dir": str(run_dir),
                "summary_path": str(summary_md_path),
                "summary_json_path": str(summary_json_path),
                "summary": summary_dict,
                "markdown": md,
            }
            if groups_json is None:
                from ios_agent_testers.core.summarize import extract_grouping_items

                grouping_items = extract_grouping_items(summary_dict)
                structured_sum["grouping_items"] = grouping_items
                if grouping_items:
                    lines.append("")
                    lines.append(f"Grouping items ({len(grouping_items)}):")
                    for item in grouping_items:
                        lines.append(
                            f"{item['id']}. [{item['persona']}, {item['severity']}, {item['screen']}] {item['title']}"
                        )
                else:
                    lines.append(
                        "Grouping items: none (0 issue reports — no second grouping call needed)."
                    )
            return {
                "content": [{"type": "text", "text": "\n".join(lines)}],
                "structuredContent": structured_sum,
                "isError": False,
            }

        # 11. qa_compare_runs
        if name == "qa_compare_runs":
            run_a = str(args["run_a"]).strip()
            run_b = str(args["run_b"]).strip()
            name_a = str(args.get("name_a") or run_a).strip()
            name_b = str(args.get("name_b") or run_b).strip()
            dir_a = self._resolve_run_dir(run_a)
            dir_b = self._resolve_run_dir(run_b)
            md, compare_dict = await asyncio.to_thread(
                self._get_compare_fn(),
                dir_a,
                dir_b,
                name_a=name_a,
                name_b=name_b,
            )
            compare_md_path = dir_b / "COMPARE.md"
            compare_json_path = dir_b / "compare.json"
            text = f"Compared '{name_a}' vs '{name_b}'. Saved report to {compare_md_path}."
            return {
                "content": [{"type": "text", "text": text}],
                "structuredContent": {
                    "ok": True,
                    "compare_path": str(compare_md_path),
                    "compare_json_path": str(compare_json_path),
                    "compare": compare_dict,
                    "markdown": md,
                },
                "isError": False,
            }

        # ------------------------------------------------------------------
        # App-only tools (12)
        # ------------------------------------------------------------------

        # 12. app_qa_list_runs
        if name == "app_qa_list_runs":
            runs = self._list_all_runs()
            return {
                "content": [{"type": "text", "text": f"{len(runs)} run(s)"}],
                "structuredContent": {
                    "ok": True,
                    "runs": runs,
                    "httpBaseUrl": self.http_server.origin,
                    "streamToken": self.http_server.token,
                },
                "isError": False,
            }

        # 13. app_qa_get_run_state
        if name == "app_qa_get_run_state":
            requested = str(args.get("run_id") or "").strip() or None
            if not requested:
                runs = self._list_all_runs()
                requested = str(runs[0].get("run_id") or "") if runs else None

            if not requested:
                return {
                    "content": [{"type": "text", "text": "No QA runs found"}],
                    "structuredContent": {
                        "ok": True,
                        "run_id": None,
                        "manifest": None,
                        "personas": [],
                        "summary": None,
                        "summary_path": None,
                        "httpBaseUrl": self.http_server.origin,
                        "streamToken": self.http_server.token,
                    },
                    "isError": False,
                }

            store = self._get_store_for_run(requested)
            manifest_raw = store.get_manifest(requested)
            state_raw = store.get_state(requested)
            run_dir = Path(store.run_dir(requested))

            summary = await asyncio.to_thread(_read_json_if_exists, run_dir / "summary.json")
            summary_md_file = run_dir / "SUMMARY.md"
            summary_path = str(summary_md_file) if summary_md_file.is_file() else None
            bug_report_title = (
                manifest_raw.get("bug_report_title")
                or _extract_bug_report_title(run_dir)
            )

            manifest_out = {
                "run_id": str(manifest_raw.get("run_id") or requested),
                "mode": str(manifest_raw.get("mode") or "explore"),
                "app_id": manifest_raw.get("app_id") or manifest_raw.get("bundle_id"),
                "app_path": manifest_raw.get("app_path"),
                "created_at": manifest_raw.get("created_at"),
                "record_video": bool(manifest_raw.get("record_video", False)),
                "max_concurrency": int(manifest_raw.get("max_concurrency") or 2),
                "max_actions": int(manifest_raw.get("max_actions") or 30),
                "bug_report_title": bug_report_title,
            }
            personas_out = _normalize_personas_list(manifest_raw, state_raw)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Run {requested}: {len(personas_out)} persona(s)",
                    }
                ],
                "structuredContent": {
                    "ok": True,
                    "run_id": requested,
                    "manifest": manifest_out,
                    "personas": personas_out,
                    "summary": summary,
                    "summary_path": summary_path,
                    "httpBaseUrl": self.http_server.origin,
                    "streamToken": self.http_server.token,
                },
                "isError": False,
            }

        # 14. app_qa_get_persona_detail
        if name == "app_qa_get_persona_detail":
            run_id = str(args["run_id"]).strip()
            persona_id = str(args["persona_id"]).strip()
            store = self._get_store_for_run(run_id)
            detail = store.get_persona_detail(run_id, persona_id)
            manifest, manifest_persona, normalized_state = self._find_persona_in_state(
                store, run_id, persona_id
            )
            logs_tail = list(detail.get("logs_tail") or [])
            udid = normalized_state.get("udid")
            if not logs_tail and udid:
                with contextlib.suppress(Exception):
                    logs_tail = list(self.driver.get_recent_logs(str(udid), limit=50))

            structured = {
                "ok": True,
                "run_id": run_id,
                "persona_id": persona_id,
                "state": normalized_state,
                "steps": detail.get("steps") or [],
                "logs_tail": logs_tail,
                "crashes": detail.get("crashes") or [],
                "report_md": detail.get("report_md"),
                "report_path": detail.get("report_path"),
                "findings": detail.get("findings"),
            }
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Persona {persona_id} ({len(structured['steps'])} step(s))",
                    }
                ],
                "structuredContent": structured,
                "isError": False,
            }

        # 15. app_qa_frame
        if name == "app_qa_frame":
            udid = str(args["udid"]).strip()
            if not udid:
                raise ValueError("udid is required")
            max_width = max(120, min(1200, int(args.get("maxWidth", 480))))
            quality = max(0.2, min(0.95, float(args.get("quality", 0.75))))
            jpeg_bytes = await self.driver.get_jpeg_frame(
                udid, max_width=max_width, quality=quality
            )
            try:
                dims = await self.driver.describe_screen(udid)
                wpt = int(dims.get("width_points") or 390)
                hpt = int(dims.get("height_points") or 844)
            except Exception:
                wpt, hpt = 390, 844
            b64 = base64.b64encode(jpeg_bytes).decode("ascii")
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"frame {wpt}x{hpt} ({len(jpeg_bytes)} bytes)",
                    }
                ],
                "structuredContent": {
                    "ok": True,
                    "mimeType": "image/jpeg",
                    "data": b64,
                    "width_points": wpt,
                    "height_points": hpt,
                },
                "isError": False,
            }

        # 16. app_qa_tap
        if name == "app_qa_tap":
            udid = str(args["udid"]).strip()
            res = await self.driver.act(
                udid, {"kind": "tap", "x": float(args["x"]), "y": float(args["y"])}
            )
            ok = bool(res.get("ok", True))
            return {
                "content": [{"type": "text", "text": "tap ok" if ok else str(res.get("detail", "tap failed"))}],
                "structuredContent": {"ok": ok},
                "isError": not ok,
            }

        # 17. app_qa_swipe
        if name == "app_qa_swipe":
            udid = str(args["udid"]).strip()
            res = await self.driver.act(
                udid,
                {
                    "kind": "swipe",
                    "x1": float(args["x1"]),
                    "y1": float(args["y1"]),
                    "x2": float(args["x2"]),
                    "y2": float(args["y2"]),
                    "duration": float(args.get("duration", 0.25)),
                },
            )
            ok = bool(res.get("ok", True))
            return {
                "content": [{"type": "text", "text": "swipe ok" if ok else str(res.get("detail", "swipe failed"))}],
                "structuredContent": {"ok": ok},
                "isError": not ok,
            }

        # 18. app_qa_type
        if name == "app_qa_type":
            udid = str(args["udid"]).strip()
            res = await self.driver.act(
                udid, {"kind": "type", "text": str(args.get("text", ""))}
            )
            ok = bool(res.get("ok", True))
            return {
                "content": [{"type": "text", "text": "type ok" if ok else str(res.get("detail", "type failed"))}],
                "structuredContent": {"ok": ok},
                "isError": not ok,
            }

        # 19. app_qa_key
        if name == "app_qa_key":
            udid = str(args["udid"]).strip()
            key = str(args["key"]).strip()
            res = await self.driver.act(udid, {"kind": "key", "key": key})
            ok = bool(res.get("ok", True))
            return {
                "content": [{"type": "text", "text": "key ok" if ok else str(res.get("detail", "key failed"))}],
                "structuredContent": {"ok": ok},
                "isError": not ok,
            }

        # 20. app_qa_button
        if name == "app_qa_button":
            udid = str(args["udid"]).strip()
            button = str(args["button"]).strip()
            res = await self.driver.act(udid, {"kind": "button", "button": button})
            ok = bool(res.get("ok", True))
            return {
                "content": [{"type": "text", "text": "button ok" if ok else str(res.get("detail", "button failed"))}],
                "structuredContent": {"ok": ok},
                "isError": not ok,
            }

        # 21. app_qa_device_action
        if name == "app_qa_device_action":
            udid = str(args["udid"]).strip()
            action = str(args["action"]).strip()
            if action in ("dark", "light"):
                res = await self.driver.act(
                    udid, {"kind": "set_appearance", "appearance": action}
                )
                ok = bool(res.get("ok", True))
                return {
                    "content": [{"type": "text", "text": f"appearance={action}"}],
                    "structuredContent": {"ok": ok, "action": action},
                    "isError": not ok,
                }

            if action == "save_screenshot":
                ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                owner = self._find_active_persona_by_udid(udid)
                resource_uri: str | None = None
                if owner is not None:
                    owner_store, rid, pid = owner
                    out_dir = Path(owner_store.persona_dir(rid, pid))
                    out_dir.mkdir(parents=True, exist_ok=True)
                    filename = f"manual_{ts}.png"
                    out_path = out_dir / filename
                    resource_uri = f"{RUN_RESOURCE_PREFIX}{rid}/{pid}/{filename}"
                else:
                    out_dir = Path(self.store.runs_root) / "_snapshots"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    filename = f"snapshot_{ts}.png"
                    out_path = out_dir / filename

                saved_ok = False
                proc = await asyncio.create_subprocess_exec(
                    "xcrun",
                    "simctl",
                    "io",
                    udid,
                    "screenshot",
                    str(out_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                if proc.returncode == 0 and out_path.is_file():
                    saved_ok = True
                else:
                    # Fallback via driver frame capture (e.g. mocked tests or offline simctl).
                    jpeg_bytes = await self.driver.get_jpeg_frame(
                        udid, max_width=800, quality=0.9
                    )
                    out_path = out_path.with_suffix(".jpg")
                    await asyncio.to_thread(out_path.write_bytes, jpeg_bytes)
                    if owner is not None:
                        _, rid, pid = owner
                        resource_uri = f"{RUN_RESOURCE_PREFIX}{rid}/{pid}/{out_path.name}"
                    saved_ok = True

                structured: dict[str, Any] = {
                    "ok": saved_ok,
                    "action": "save_screenshot",
                    "path": str(out_path),
                    "screenshot_path": str(out_path),
                    "filename": out_path.name,
                }
                if resource_uri:
                    structured["resourceUri"] = resource_uri
                    structured["screenshot_uri"] = resource_uri
                return {
                    "content": [{"type": "text", "text": f"Saved screenshot to {out_path}"}],
                    "structuredContent": structured,
                    "isError": not saved_ok,
                }

            if action == "stop":
                owner = self._find_active_persona_by_udid(udid)
                video_pid: int | None = None
                if owner is not None:
                    owner_store, rid, pid = owner
                    runtime = self._persona_runtime.pop((rid, pid), {})
                    video_pid = runtime.get("video_pid")
                    if video_pid is None:
                        with contextlib.suppress(Exception):
                            raw_state = owner_store.get_state(rid)
                            raw_personas = raw_state.get("personas")
                            if isinstance(raw_personas, dict):
                                video_pid = (raw_personas.get(pid) or {}).get("video_pid")

                with contextlib.suppress(Exception):
                    await self.driver.stop_log_tail(udid)
                with contextlib.suppress(Exception):
                    await self.driver.close_udid(udid)
                teardown_res = await self._get_teardown_fn()(
                    udid, ephemeral=False, delete_simulator=False, video_pid=video_pid
                )

                if owner is not None:
                    owner_store, rid, pid = owner
                    with contextlib.suppress(Exception):
                        _, _, pstate = self._find_persona_in_state(owner_store, rid, pid)
                        updates: dict[str, Any] = {
                            "udid": None,
                            "paused_by_human": False,
                            "video_pid": None,
                        }
                        if pstate.get("status") in (
                            "pending",
                            "provisioning",
                            "running",
                            "paused",
                        ):
                            updates["status"] = "done"
                        owner_store.update_persona_state(rid, pid, **updates)

                return {
                    "content": [{"type": "text", "text": f"Stopped simulator {udid}"}],
                    "structuredContent": {
                        "ok": True,
                        "action": "stop",
                        "teardown": teardown_res,
                    },
                    "isError": False,
                }

            raise ValueError(f"Unsupported device action: {action}")

        # 22. app_qa_toggle_takeover
        if name == "app_qa_toggle_takeover":
            run_id = str(args["run_id"]).strip()
            persona_id = str(args["persona_id"]).strip()
            paused = bool(args["paused"])
            note = args.get("note")
            if note is not None:
                note = str(note).strip() or None

            store = self._get_store_for_run(run_id)
            res = store.set_takeover(run_id, persona_id, paused, note=note) or {}
            effective_paused = bool(res.get("paused_by_human", paused))
            effective_note = res.get("human_note", note)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Takeover {'paused' if effective_paused else 'resumed'} "
                            f"for {persona_id} in run {run_id}"
                        ),
                    }
                ],
                "structuredContent": {
                    "ok": True,
                    "paused": effective_paused,
                    "note": effective_note,
                },
                "isError": False,
            }

        # 23. app_qa_compare
        if name == "app_qa_compare":
            run_a = str(args["run_a"]).strip()
            run_b = str(args["run_b"]).strip()
            name_a = str(args.get("name_a") or run_a).strip()
            name_b = str(args.get("name_b") or run_b).strip()
            dir_a = self._resolve_run_dir(run_a)
            dir_b = self._resolve_run_dir(run_b)
            md, compare_dict = await asyncio.to_thread(
                self._get_compare_fn(),
                dir_a,
                dir_b,
                name_a=name_a,
                name_b=name_b,
            )
            compare_path = str(dir_b / "COMPARE.md")
            return {
                "content": [
                    {"type": "text", "text": f"Compared {name_a} vs {name_b}"}
                ],
                "structuredContent": {
                    "ok": True,
                    "markdown": md,
                    "compare": compare_dict,
                    "compare_path": compare_path,
                },
                "isError": False,
            }

        return {
            "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
            "structuredContent": {"ok": False, "error": f"unknown tool: {name}"},
            "isError": True,
        }

    # ------------------------------------------------------------------
    # JSON-RPC 2.0 transport & request router
    # ------------------------------------------------------------------

    async def _send_message(self, msg: dict[str, Any]) -> None:
        line = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        async with self._stdout_lock:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()

    async def _send_result(self, req_id: Any, result: Any) -> None:
        await self._send_message({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def _send_error(
        self, req_id: Any, code: int, message: str, data: Any = None
    ) -> None:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        await self._send_message({"jsonrpc": "2.0", "id": req_id, "error": err})

    async def handle_rpc_request(self, req: dict[str, Any]) -> dict[str, Any] | None:
        """Process one JSON-RPC request and return its response object (or None for notifications)."""
        req_id = req.get("id")
        has_id = "id" in req
        method = req.get("method")
        params = req.get("params") or {}

        if not isinstance(method, str):
            if has_id:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32600, "message": "Invalid Request"},
                }
            return None

        if not has_id:
            if method == "notifications/initialized":
                logger.debug("MCP client initialized")
            elif method == "notifications/cancelled":
                target_id = params.get("requestId")
                task = self._inflight.get(target_id)
                if task is not None and not task.done():
                    task.cancel()
            return None

        try:
            if method == "initialize":
                client_proto = params.get("protocolVersion")
                protocol_version = (
                    client_proto
                    if isinstance(client_proto, str) and client_proto
                    else "2025-03-26"
                )
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": protocol_version,
                        "capabilities": {
                            "tools": {},
                            "resources": {},
                        },
                        "serverInfo": {
                            "name": SERVER_NAME,
                            "version": SERVER_VERSION,
                        },
                    },
                }

            if method == "ping":
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}

            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS_SCHEMA}}

            if method == "tools/call":
                tool_name = params.get("name")
                tool_args = params.get("arguments") or {}
                if not isinstance(tool_name, str):
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": "Invalid params: missing tool name"},
                    }
                result = await self._call_tool(tool_name, tool_args)
                return {"jsonrpc": "2.0", "id": req_id, "result": result}

            if method == "resources/list":
                resources: list[dict[str, Any]] = [
                    {
                        "uri": APP_RESOURCE_URI,
                        "name": "iOS QA 測試台",
                        "description": "Antigravity iOS Agent Testers live QA dashboard MCP App",
                        "mimeType": APP_MIME_TYPE,
                        "_meta": self._ui_resource_meta(),
                    }
                ]
                return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": resources}}

            if method == "resources/templates/list":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "resourceTemplates": [
                            {
                                "uriTemplate": f"{RUN_RESOURCE_PREFIX}{{run_id}}/{{persona_id}}/{{file}}",
                                "name": "iOS QA Run Artifact",
                                "description": "Screenshot, report.md, findings.json, or log artifact from a QA run",
                                "mimeType": "application/octet-stream",
                            }
                        ]
                    },
                }

            if method == "resources/read":
                uri = params.get("uri")
                if not isinstance(uri, str) or not uri:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": "Invalid params: missing uri"},
                    }

                if uri == APP_RESOURCE_URI:
                    html = await asyncio.to_thread(self._read_app_html)
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "contents": [
                                {
                                    "uri": APP_RESOURCE_URI,
                                    "mimeType": APP_MIME_TYPE,
                                    "text": html,
                                    "_meta": self._ui_resource_meta(),
                                }
                            ]
                        },
                    }

                if uri.startswith(RUN_RESOURCE_PREFIX):
                    rest = uri[len(RUN_RESOURCE_PREFIX) :]
                    parts = rest.split("/", 2)
                    if len(parts) != 3 or not all(parts):
                        return {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {
                                "code": -32602,
                                "message": f"Invalid run artifact URI: {uri}",
                            },
                        }
                    run_id = unquote(parts[0])
                    persona_id = unquote(parts[1])
                    rel_file = unquote(parts[2])
                    store = self._get_store_for_run(run_id)
                    try:
                        resolved = await asyncio.to_thread(
                            store.resolve_artifact_path, run_id, persona_id, rel_file
                        )
                        resolved = Path(resolved).resolve()
                        if not resolved.is_relative_to(Path(store.runs_root).resolve()):
                            raise ValueError("artifact escapes runs_root")
                    except ValueError as exc:
                        return {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {"code": -32602, "message": f"Invalid artifact path: {exc}"},
                        }
                    except FileNotFoundError:
                        return {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {"code": -32002, "message": f"Resource not found: {uri}"},
                        }

                    mime = guess_mime_type(resolved).split(";", 1)[0]
                    if resolved.suffix.lower() in TEXT_EXTENSIONS:
                        text_content = await asyncio.to_thread(
                            resolved.read_text, "utf-8", "replace"
                        )
                        content_item: dict[str, Any] = {
                            "uri": uri,
                            "mimeType": mime,
                            "text": text_content,
                        }
                    else:
                        raw_bytes = await asyncio.to_thread(resolved.read_bytes)
                        content_item = {
                            "uri": uri,
                            "mimeType": mime,
                            "blob": base64.b64encode(raw_bytes).decode("ascii"),
                        }
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"contents": [content_item]},
                    }

                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32002, "message": f"Resource not found: {uri}"},
                }

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Unhandled error for method %s", method)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Internal error: {_format_exc(exc)}"},
            }

    async def _handle_request(self, req: dict[str, Any]) -> None:
        resp = await self.handle_rpc_request(req)
        if resp is not None:
            await self._send_message(resp)

    async def run(self) -> None:
        await self.http_server.start()

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)

        reader = asyncio.StreamReader(limit=4 * 1024 * 1024)
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)

        async def read_loop() -> None:
            while not stop_event.is_set():
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError as exc:
                    await self._send_error(None, -32700, f"Parse error: {exc}")
                    continue

                if not isinstance(msg, dict):
                    await self._send_error(None, -32600, "Invalid Request")
                    continue

                req_id = msg.get("id")
                has_id = "id" in msg

                if has_id:
                    task = asyncio.create_task(self._handle_request(msg))
                    self._inflight[req_id] = task

                    def _cleanup(t: asyncio.Task[None], rid: Any = req_id) -> None:
                        if self._inflight.get(rid) is t:
                            self._inflight.pop(rid, None)
                        with contextlib.suppress(asyncio.CancelledError):
                            exc = t.exception()
                            if exc is not None:
                                logger.error("Request task failed: %s", _format_exc(exc))

                    task.add_done_callback(_cleanup)
                else:
                    await self._handle_request(msg)

        read_task = asyncio.create_task(read_loop(), name="mcp-stdin-reader")
        stop_task = asyncio.create_task(stop_event.wait(), name="mcp-stop-signal")
        try:
            await asyncio.wait(
                [read_task, stop_task], return_when=asyncio.FIRST_COMPLETED
            )
            # On clean stdin EOF (not SIGTERM/SIGINT), drain pending in-flight
            # requests so queued responses (such as reading dist/app.html) flush cleanly.
            if read_task.done() and not stop_event.is_set():
                pending = [t for t in self._inflight.values() if not t.done()]
                if pending:
                    with contextlib.suppress(TimeoutError, Exception):
                        await asyncio.wait_for(
                            asyncio.gather(*pending, return_exceptions=True),
                            timeout=10.0,
                        )
        finally:
            for t in (read_task, stop_task):
                if not t.done():
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await t
            for task in list(self._inflight.values()):
                if not task.done():
                    task.cancel()
            for task in list(self._inflight.values()):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(task, timeout=2.0)
            self._inflight.clear()
            await self.http_server.stop()
            with contextlib.suppress(Exception):
                await self.driver.close_all()


def main() -> None:
    server = MCPQATestersServer()
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
