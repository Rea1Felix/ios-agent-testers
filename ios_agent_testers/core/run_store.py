"""Atomic filesystem state store, locking, takeover control, and findings validation."""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ios_agent_testers.core.bug_report import extract_bug_report_title

VALID_MODES: tuple[str, ...] = ("explore", "reproduce", "improve", "targeted")
VALID_SEVERITIES: tuple[str, ...] = ("blocker", "major", "minor", "cosmetic")
VALID_IMPROVEMENT_AREAS: tuple[str, ...] = (
    "onboarding",
    "navigation",
    "copy",
    "layout",
    "feedback",
    "trust",
    "performance",
    "feature",
    "accessibility",
    "translation",
)
VALID_REPRODUCED: tuple[str, ...] = ("yes", "no", "unclear")
VALID_GOAL_REACHED: tuple[str, ...] = ("yes", "partly", "no")
VALID_IMPACTS: tuple[str, ...] = ("high", "medium", "low")
VALID_STATUSES: tuple[str, ...] = (
    "pending",
    "provisioning",
    "running",
    "paused",
    "crashed",
    "done",
    "error",
)
DEFAULT_MAX_ACTIONS = 30
DEFAULT_MAX_STEPS = DEFAULT_MAX_ACTIONS
MAX_IMPROVEMENTS = 7

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def default_runs_root() -> Path:
    """Return the default runs root directory from env vars or plugin data dir."""
    explicit = os.environ.get("IOS_AGENT_TESTERS_RUNS_DIR")
    if explicit and explicit.strip():
        return Path(explicit).expanduser()
    data_dir = os.environ.get("IOS_AGENT_TESTERS_DATA")
    if data_dir and data_dir.strip():
        return Path(data_dir).expanduser() / "runs"
    return (
        Path.home()
        / ".gemini"
        / "antigravity"
        / "plugin_data"
        / "ios-agent-testers"
        / "runs"
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _validate_safe_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    if (
        "/" in value
        or "\\" in value
        or "\x00" in value
        or ".." in value
        or not _SAFE_ID_RE.fullmatch(value)
    ):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _write_json_atomic(path: Path, payload: Any) -> None:
    _write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _clean_persona_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _empty_issue_counts() -> dict[str, int]:
    return {sev: 0 for sev in VALID_SEVERITIES}


def validate_findings(findings: dict, mode: str = "explore") -> list[str]:
    """Validate structured `findings.json` payload for the given test mode.

    Returns a list of error messages (empty list means valid).
    """
    errors: list[str] = []
    if mode not in VALID_MODES:
        errors.append(f"Invalid mode {mode!r}; expected one of {VALID_MODES}")
        return errors

    if not isinstance(findings, dict):
        return ["findings must be a JSON object"]

    # Score validation (required in explore/targeted/improve; optional but validated in reproduce)
    if "score" in findings and findings["score"] is not None:
        score = findings["score"]
        if not _is_number(score) or not (0 <= float(score) <= 10):
            errors.append("score must be a number between 0 and 10")
    elif mode in ("explore", "targeted", "improve"):
        errors.append("score is required and must be a number between 0 and 10")

    # Issues list validation (required across all modes)
    issues = findings.get("issues")
    if not isinstance(issues, list):
        errors.append("issues must be a list")
    else:
        for idx, item in enumerate(issues):
            prefix = f"issues[{idx}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix} must be an object")
                continue
            title = item.get("title")
            if not isinstance(title, str) or not title.strip():
                errors.append(f"{prefix}.title must be a non-empty string")
            severity = item.get("severity")
            if not isinstance(severity, str) or severity not in VALID_SEVERITIES:
                errors.append(
                    f"{prefix}.severity must be one of {VALID_SEVERITIES}, got {severity!r}"
                )
            if "screen" in item and item["screen"] is not None:
                if not isinstance(item["screen"], str):
                    errors.append(f"{prefix}.screen must be a string")
            if "screens" in item and item["screens"] is not None:
                screens = item["screens"]
                if not isinstance(screens, list) or not all(
                    isinstance(s, str) or (isinstance(s, int) and not isinstance(s, bool))
                    for s in screens
                ):
                    errors.append(f"{prefix}.screens must be a list of screenshot numbers/strings")

    if mode == "reproduce":
        reproduced = findings.get("reproduced")
        if not isinstance(reproduced, str) or reproduced not in VALID_REPRODUCED:
            errors.append(
                f"reproduced is required and must be one of {VALID_REPRODUCED}, got {reproduced!r}"
            )
        evidence = findings.get("evidence")
        if not isinstance(evidence, list) or not all(
            isinstance(e, str) or (isinstance(e, int) and not isinstance(e, bool))
            for e in evidence
        ):
            errors.append("evidence is required and must be a list of screenshot strings")
        note = findings.get("note")
        if not isinstance(note, str) or not note.strip():
            errors.append("note is required and must be a non-empty string")

    elif mode == "improve":
        goal_reached = findings.get("goal_reached")
        if not isinstance(goal_reached, str) or goal_reached not in VALID_GOAL_REACHED:
            errors.append(
                f"goal_reached is required and must be one of {VALID_GOAL_REACHED}, got {goal_reached!r}"
            )
        actions = findings.get("actions")
        if not isinstance(actions, int) or isinstance(actions, bool) or actions < 0:
            errors.append("actions is required and must be a non-negative integer")
        improvements = findings.get("improvements")
        if not isinstance(improvements, list):
            errors.append("improvements is required and must be a list")
        else:
            if len(improvements) > MAX_IMPROVEMENTS:
                errors.append(
                    f"improvements must have at most {MAX_IMPROVEMENTS} items, got {len(improvements)}"
                )
            for idx, imp in enumerate(improvements):
                prefix = f"improvements[{idx}]"
                if not isinstance(imp, dict):
                    errors.append(f"{prefix} must be an object")
                    continue
                title = imp.get("title")
                if not isinstance(title, str) or not title.strip():
                    errors.append(f"{prefix}.title must be a non-empty string")
                area = imp.get("area")
                if not isinstance(area, str) or area not in VALID_IMPROVEMENT_AREAS:
                    errors.append(
                        f"{prefix}.area must be one of {VALID_IMPROVEMENT_AREAS}, got {area!r}"
                    )
                impact = imp.get("impact")
                if not isinstance(impact, str) or impact not in VALID_IMPACTS:
                    errors.append(
                        f"{prefix}.impact must be one of {VALID_IMPACTS}, got {impact!r}"
                    )
                why = imp.get("why")
                if not isinstance(why, str) or not why.strip():
                    errors.append(f"{prefix}.why must be a non-empty string")
                if "screen" in imp and imp["screen"] is not None:
                    if not isinstance(imp["screen"], str):
                        errors.append(f"{prefix}.screen must be a string")
                if "screens" in imp and imp["screens"] is not None:
                    screens = imp["screens"]
                    if not isinstance(screens, list) or not all(
                        isinstance(s, str) or (isinstance(s, int) and not isinstance(s, bool))
                        for s in screens
                    ):
                        errors.append(f"{prefix}.screens must be a list of screenshot strings")

    return errors


class RunStore:
    """Thread-safe and process-safe atomic run state & artifact manager."""

    _thread_locks_guard = threading.Lock()
    _thread_locks: dict[str, threading.RLock] = {}
    _lock_depths: dict[tuple[int, str], int] = {}

    def __init__(self, runs_root: Path | None = None) -> None:
        base = Path(runs_root).expanduser() if runs_root is not None else default_runs_root()
        base.mkdir(parents=True, exist_ok=True)
        self._runs_root = base.resolve()

    @property
    def runs_root(self) -> Path:
        return self._runs_root

    def run_dir(self, run_id: str) -> Path:
        safe_run = _validate_safe_id(run_id, "run_id")
        return self._runs_root / safe_run

    def persona_dir(self, run_id: str, persona_id: str) -> Path:
        safe_persona = _validate_safe_id(persona_id, "persona_id")
        return self.run_dir(run_id) / safe_persona

    @classmethod
    def _get_thread_lock(cls, key: str) -> threading.RLock:
        with cls._thread_locks_guard:
            lock = cls._thread_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                cls._thread_locks[key] = lock
            return lock

    @contextlib.contextmanager
    def _run_lock(self, run_id: str) -> Iterator[None]:
        rdir = self.run_dir(run_id)
        rdir.mkdir(parents=True, exist_ok=True)
        key = str(rdir.resolve())
        tlock = self._get_thread_lock(key)
        depth_key = (threading.get_ident(), key)

        tlock.acquire()
        lock_file = None
        acquired_flock = False
        try:
            with self._thread_locks_guard:
                depth = self._lock_depths.get(depth_key, 0)
                self._lock_depths[depth_key] = depth + 1

            if depth == 0:
                lock_path = rdir / ".lock"
                lock_file = open(lock_path, "a+b")
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                acquired_flock = True
            yield
        finally:
            if acquired_flock and lock_file is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                with contextlib.suppress(OSError):
                    lock_file.close()
            with self._thread_locks_guard:
                remaining = self._lock_depths.get(depth_key, 1) - 1
                if remaining <= 0:
                    self._lock_depths.pop(depth_key, None)
                else:
                    self._lock_depths[depth_key] = remaining
            tlock.release()

    def _state_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "state.json"

    def _manifest_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "manifest.json"

    def _load_state_unlocked(self, run_id: str) -> dict[str, Any]:
        spath = self._state_path(run_id)
        if not spath.is_file():
            raise FileNotFoundError(f"Run state not found for run_id={run_id!r}")
        data = _read_json(spath)
        if not isinstance(data, dict):
            raise ValueError(f"Corrupted state.json for run_id={run_id!r}")
        return data

    def create_run(
        self,
        *,
        app_path: str | None = None,
        bundle_id: str | None = None,
        personas: list[dict],
        mode: str = "explore",
        brief_path: str | None = None,
        bug_report_md: str | None = None,
        custom_goal: str | None = None,
        record_video: bool = False,
        max_concurrency: int = 2,
        max_actions: int = DEFAULT_MAX_ACTIONS,
        run_id: str | None = None,
    ) -> dict:
        """Create a new QA run directory with manifest.json and initial state.json."""
        if mode not in VALID_MODES:
            raise ValueError(f"Invalid mode {mode!r}; expected one of {VALID_MODES}")
        if not isinstance(personas, list) or not personas:
            raise ValueError("personas must be a non-empty list")
        if max_actions is None:
            clamped_max_actions = DEFAULT_MAX_ACTIONS
        elif isinstance(max_actions, bool):
            raise ValueError("max_actions must be an integer")
        else:
            clamped_max_actions = max(1, min(100, int(max_actions)))

        seen_ids: set[str] = set()
        normalized_personas: list[dict[str, Any]] = []
        for entry in personas:
            if not isinstance(entry, dict) or not entry.get("id"):
                raise ValueError("Every persona must be a dict with a non-empty 'id'")
            pid = _validate_safe_id(str(entry["id"]), "persona_id")
            if pid in seen_ids:
                raise ValueError(f"Duplicate persona id in run: {pid!r}")
            seen_ids.add(pid)
            normalized_personas.append(dict(entry))

        if run_id is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            run_id = f"{stamp}-{secrets.token_hex(3)}"
        else:
            run_id = _validate_safe_id(run_id, "run_id")

        rdir = self.run_dir(run_id)
        if (rdir / "manifest.json").exists():
            raise FileExistsError(f"Run {run_id!r} already exists")

        created_at = _utc_now_iso()
        now_ts = time.time()
        bug_report_title = extract_bug_report_title(bug_report_md)

        manifest: dict[str, Any] = {
            "run_id": run_id,
            "mode": mode,
            "app_id": bundle_id,
            "bundle_id": bundle_id,
            "app_path": str(app_path) if app_path else None,
            "brief_path": str(brief_path) if brief_path else None,
            "custom_goal": custom_goal,
            "created_at": created_at,
            "record_video": bool(record_video),
            "max_concurrency": max(1, int(max_concurrency)),
            "max_actions": clamped_max_actions,
            "bug_report_title": bug_report_title,
            "personas": normalized_personas,
        }

        persona_states: dict[str, dict[str, Any]] = {}
        for p in normalized_personas:
            pid = p["id"]
            device = dict(p.get("device") or {})
            effective_goal = (
                custom_goal.strip()
                if (custom_goal and custom_goal.strip() and mode == "targeted")
                else str(p.get("goal") or "")
            )
            persona_states[pid] = {
                "id": pid,
                "profile": str(p.get("profile") or ""),
                "goal": effective_goal,
                "locale": str(p.get("locale") or "en-US"),
                "device": device,
                "status": "pending",
                "udid": None,
                "device_label": None,
                "ephemeral": True,
                "video_pid": None,
                "video_path": None,
                "width_points": None,
                "height_points": None,
                "step_count": 0,
                "action_count": 0,
                "observation_count": 0,
                "max_actions": clamped_max_actions,
                "max_steps": clamped_max_actions,
                "paused_by_human": False,
                "human_note": None,
                "issue_counts": _empty_issue_counts(),
                "score": None,
                "reproduced": None,
                "goal_reached": None,
                "crash_count": 0,
                "has_video": False,
                "last_screenshot": None,
                "error": None,
                "run_start_ts": now_ts,
                "started_at": None,
                "finished_at": None,
                "updated_at": created_at,
            }

        state: dict[str, Any] = {
            "run_id": run_id,
            "mode": mode,
            "created_at": created_at,
            "updated_at": created_at,
            "persona_order": [p["id"] for p in normalized_personas],
            "personas": persona_states,
        }

        with self._run_lock(run_id):
            if (rdir / "manifest.json").exists():
                raise FileExistsError(f"Run {run_id!r} already exists")
            for p in normalized_personas:
                pdir = self.persona_dir(run_id, p["id"])
                (pdir / "crashes").mkdir(parents=True, exist_ok=True)
                if record_video:
                    (pdir / "video").mkdir(parents=True, exist_ok=True)
            if bug_report_md is not None and bug_report_md.strip():
                text = bug_report_md if bug_report_md.endswith("\n") else bug_report_md + "\n"
                _write_text_atomic(rdir / "bug_report.md", text)
            _write_json_atomic(self._manifest_path(run_id), manifest)
            _write_json_atomic(self._state_path(run_id), state)

        return manifest

    def list_runs(self) -> list[dict]:
        """Return summary rows for all runs under runs_root, sorted newest first."""
        rows: list[dict[str, Any]] = []
        if not self._runs_root.is_dir():
            return rows

        for entry in self._runs_root.iterdir():
            if not entry.is_dir():
                continue
            mpath = entry / "manifest.json"
            if not mpath.is_file():
                continue
            try:
                manifest = _read_json(mpath)
                if not isinstance(manifest, dict):
                    continue
                spath = entry / "state.json"
                state = _read_json(spath) if spath.is_file() else {}
            except (OSError, ValueError):
                continue

            run_id = str(manifest.get("run_id") or entry.name)
            personas_raw = state.get("personas") if isinstance(state, dict) else {}
            if isinstance(personas_raw, dict):
                persona_items = list(personas_raw.values())
            elif isinstance(personas_raw, list):
                persona_items = personas_raw
            else:
                persona_items = []

            persona_count = len(persona_items) or len(manifest.get("personas") or [])
            active_count = sum(
                1
                for p in persona_items
                if isinstance(p, dict)
                and p.get("status") in ("provisioning", "running", "paused")
            )
            done_count = sum(
                1
                for p in persona_items
                if isinstance(p, dict) and p.get("status") == "done"
            )

            issue_count = 0
            scores: list[float] = []
            for p in persona_items:
                if not isinstance(p, dict):
                    continue
                counts = p.get("issue_counts")
                if isinstance(counts, dict):
                    issue_count += sum(
                        int(v)
                        for v in counts.values()
                        if isinstance(v, (int, float)) and not isinstance(v, bool)
                    )
                score = p.get("score")
                if _is_number(score):
                    scores.append(float(score))

            avg_score = round(sum(scores) / len(scores), 1) if scores else None
            has_summary = (entry / "SUMMARY.md").is_file() or (entry / "summary.json").is_file()

            try:
                mtime_ns = mpath.stat().st_mtime_ns
            except OSError:
                mtime_ns = 0

            rows.append(
                (
                    (str(manifest.get("created_at") or ""), mtime_ns, run_id),
                    {
                        "run_id": run_id,
                        "mode": str(manifest.get("mode") or "explore"),
                        "created_at": str(manifest.get("created_at") or ""),
                        "app_id": manifest.get("app_id") or manifest.get("bundle_id"),
                        "persona_count": persona_count,
                        "active_count": active_count,
                        "done_count": done_count,
                        "issue_count": issue_count,
                        "avg_score": avg_score,
                        "has_summary": has_summary,
                    },
                )
            )

        rows.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in rows]

    def get_manifest(self, run_id: str) -> dict:
        mpath = self._manifest_path(run_id)
        if not mpath.is_file():
            raise FileNotFoundError(f"Run manifest not found for run_id={run_id!r}")
        data = _read_json(mpath)
        if not isinstance(data, dict):
            raise ValueError(f"Corrupted manifest.json for run_id={run_id!r}")
        return data

    def get_state(self, run_id: str) -> dict:
        with self._run_lock(run_id):
            state = self._load_state_unlocked(run_id)
        cleaned_personas: dict[str, dict[str, Any]] = {}
        raw_personas = state.get("personas") or {}
        if isinstance(raw_personas, dict):
            for pid, pdata in raw_personas.items():
                cleaned_personas[pid] = (
                    _clean_persona_dict(pdata) if isinstance(pdata, dict) else {}
                )
        result = dict(state)
        result["personas"] = cleaned_personas
        return result

    def update_persona_state(self, run_id: str, persona_id: str, **updates: Any) -> dict:
        safe_pid = _validate_safe_id(persona_id, "persona_id")
        now = _utc_now_iso()
        with self._run_lock(run_id):
            state = self._load_state_unlocked(run_id)
            personas = state.setdefault("personas", {})
            if safe_pid not in personas:
                raise KeyError(f"Persona {safe_pid!r} not found in run {run_id!r}")
            p = personas[safe_pid]

            if "status" in updates and updates["status"] is not None:
                status = str(updates["status"])
                if status not in VALID_STATUSES:
                    raise ValueError(
                        f"Invalid persona status {status!r}; expected one of {VALID_STATUSES}"
                    )
                if status in ("provisioning", "running") and not p.get("started_at"):
                    p["started_at"] = now
                if status in ("done", "error") and not p.get("finished_at"):
                    p["finished_at"] = now

            for key, value in updates.items():
                p[key] = value

            if updates.get("video_path") and "has_video" not in updates:
                p["has_video"] = True

            p["updated_at"] = now
            state["updated_at"] = now
            _write_json_atomic(self._state_path(run_id), state)
            return _clean_persona_dict(p)

    def set_takeover(
        self,
        run_id: str,
        persona_id: str,
        paused: bool,
        note: str | None = None,
    ) -> dict:
        """Pause or resume human takeover and optionally queue a steering note."""
        safe_pid = _validate_safe_id(persona_id, "persona_id")
        now = _utc_now_iso()
        with self._run_lock(run_id):
            state = self._load_state_unlocked(run_id)
            personas = state.get("personas") or {}
            if safe_pid not in personas:
                raise KeyError(f"Persona {safe_pid!r} not found in run {run_id!r}")
            p = personas[safe_pid]

            paused_bool = bool(paused)
            p["paused_by_human"] = paused_bool

            if paused_bool:
                if p.get("status") != "paused":
                    p["_pre_pause_status"] = p.get("status", "running")
                if p.get("status") in ("pending", "provisioning", "running"):
                    p["status"] = "paused"
            else:
                if p.get("status") == "paused":
                    prev = p.pop("_pre_pause_status", None)
                    p["status"] = prev if prev and prev != "paused" else "running"
                else:
                    p.pop("_pre_pause_status", None)

            if note is not None:
                cleaned = str(note).strip()
                if cleaned:
                    queue = list(p.get("_pending_notes") or [])
                    if not queue or queue[-1] != cleaned:
                        queue.append(cleaned)
                    p["_pending_notes"] = queue
                    p["human_note"] = "\n".join(queue)
                else:
                    p.pop("_pending_notes", None)
                    p["human_note"] = None

            p["updated_at"] = now
            state["updated_at"] = now
            _write_json_atomic(self._state_path(run_id), state)

            return {
                "paused_by_human": paused_bool,
                "paused": paused_bool,
                "human_note": p.get("human_note"),
                "note": p.get("human_note"),
                "status": p.get("status"),
            }

    def get_takeover(self, run_id: str, persona_id: str) -> dict:
        safe_pid = _validate_safe_id(persona_id, "persona_id")
        with self._run_lock(run_id):
            state = self._load_state_unlocked(run_id)
            personas = state.get("personas") or {}
            if safe_pid not in personas:
                raise KeyError(f"Persona {safe_pid!r} not found in run {run_id!r}")
            p = personas[safe_pid]
            paused = bool(p.get("paused_by_human", False))
            note = p.get("human_note")
            return {
                "paused_by_human": paused,
                "paused": paused,
                "human_note": note,
                "note": note,
                "status": p.get("status", "pending"),
            }

    def consume_human_note(self, run_id: str, persona_id: str) -> str | None:
        """Atomically pop and return any pending steering note(s) for the persona."""
        safe_pid = _validate_safe_id(persona_id, "persona_id")
        now = _utc_now_iso()
        with self._run_lock(run_id):
            state = self._load_state_unlocked(run_id)
            personas = state.get("personas") or {}
            if safe_pid not in personas:
                raise KeyError(f"Persona {safe_pid!r} not found in run {run_id!r}")
            p = personas[safe_pid]
            note = p.get("human_note")
            if note is None and "_pending_notes" not in p:
                return None
            p["human_note"] = None
            p.pop("_pending_notes", None)
            p["updated_at"] = now
            state["updated_at"] = now
            _write_json_atomic(self._state_path(run_id), state)
            return str(note) if note else None

    def next_step_number(self, run_id: str, persona_id: str) -> int:
        """Return the next 1-based step index for this persona."""
        safe_pid = _validate_safe_id(persona_id, "persona_id")
        with self._run_lock(run_id):
            state = self._load_state_unlocked(run_id)
            personas = state.get("personas") or {}
            if safe_pid not in personas:
                raise KeyError(f"Persona {safe_pid!r} not found in run {run_id!r}")
            step_count = int(personas[safe_pid].get("step_count") or 0)
            actions_path = self.persona_dir(run_id, safe_pid) / "actions.jsonl"
            if actions_path.is_file():
                for line in actions_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                        if isinstance(rec, dict) and isinstance(rec.get("step"), int):
                            step_count = max(step_count, rec["step"])
                    except ValueError:
                        continue
            return step_count + 1

    def record_step(
        self,
        run_id: str,
        persona_id: str,
        step: int,
        *,
        screenshot_file: str | None = None,
        action: dict | None = None,
        a11y: dict | None = None,
        app_running: bool | None = None,
        crashes: list[dict] | tuple[dict, ...] = (),
        recent_logs: list[str] | tuple[str, ...] = (),
        elements: list[dict] | tuple[dict, ...] | None = None,
        ax_unavailable_reason: str | None = None,
    ) -> dict:
        """Persist one observed/acted step to actions.jsonl, a11y.json, last_elements.json, and state.json."""
        safe_pid = _validate_safe_id(persona_id, "persona_id")
        if not isinstance(step, int) or isinstance(step, bool) or step < 1:
            raise ValueError(f"step must be a positive integer, got {step!r}")

        now = _utc_now_iso()
        pdir = self.persona_dir(run_id, safe_pid)
        screenshot_str = str(screenshot_file) if screenshot_file else None
        elements_list = list(elements) if elements is not None else None
        crashes_list = list(crashes or [])
        logs_list = [str(line) for line in (recent_logs or [])]
        ax_reason = str(ax_unavailable_reason) if ax_unavailable_reason else None

        entry: dict[str, Any] = {
            "step": step,
            "timestamp": now,
            "screenshot_file": screenshot_str,
            "action": dict(action) if isinstance(action, dict) else None,
            "a11y": a11y if isinstance(a11y, dict) else None,
            "app_running": app_running,
            "crashes": crashes_list,
            "recent_logs": logs_list,
            "elements_count": len(elements_list) if elements_list is not None else 0,
            "ax_unavailable_reason": ax_reason,
        }

        with self._run_lock(run_id):
            pdir.mkdir(parents=True, exist_ok=True)
            state = self._load_state_unlocked(run_id)
            personas = state.get("personas") or {}
            if safe_pid not in personas:
                raise KeyError(f"Persona {safe_pid!r} not found in run {run_id!r}")
            p = personas[safe_pid]

            # 1. Append to actions.jsonl
            actions_path = pdir / "actions.jsonl"
            with open(actions_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

            # 2. Update a11y.json atomically
            a11y_path = pdir / "a11y.json"
            a11y_store: dict[str, Any] = {"steps": {}, "by_screenshot": {}}
            if a11y_path.is_file():
                try:
                    loaded = _read_json(a11y_path)
                    if isinstance(loaded, dict):
                        a11y_store["steps"] = dict(loaded.get("steps") or {})
                        a11y_store["by_screenshot"] = dict(loaded.get("by_screenshot") or {})
                except (OSError, ValueError):
                    pass
            if isinstance(a11y, dict):
                a11y_store["steps"][str(step)] = a11y
                if screenshot_str:
                    a11y_store["by_screenshot"][screenshot_str] = a11y
                _write_json_atomic(a11y_path, a11y_store)

            # 3. Write last_elements.json atomically ONLY when elements is explicitly provided
            if elements_list is not None:
                _write_json_atomic(pdir / "last_elements.json", elements_list)

            # 4. Update live state.json atomically
            p["step_count"] = int(p.get("step_count") or 0) + 1
            p["action_count"] = int(p.get("action_count") or 0) + (
                1 if isinstance(action, dict) else 0
            )
            p["observation_count"] = int(p.get("observation_count") or 0) + (
                1 if screenshot_str else 0
            )
            if screenshot_str:
                p["last_screenshot"] = screenshot_str

            if crashes_list:
                seen_crashes = set(p.get("_seen_crashes") or [])
                new_count = 0
                for c in crashes_list:
                    cname = str(c.get("name") or "") if isinstance(c, dict) else str(c)
                    if cname:
                        if cname not in seen_crashes:
                            seen_crashes.add(cname)
                            new_count += 1
                    else:
                        new_count += 1
                p["_seen_crashes"] = sorted(seen_crashes)
                p["crash_count"] = int(p.get("crash_count") or 0) + new_count
                if app_running is False and p.get("status") not in ("done", "paused"):
                    p["status"] = "crashed"
            elif app_running is False and p.get("status") not in ("done", "paused", "error"):
                # Process died even if no .ips file landed yet
                p["status"] = "crashed"
            elif p.get("status") in ("pending", "provisioning"):
                p["status"] = "running"
                if not p.get("started_at"):
                    p["started_at"] = now

            p["updated_at"] = now
            state["updated_at"] = now
            _write_json_atomic(self._state_path(run_id), state)

        return entry

    def get_last_elements(self, run_id: str, persona_id: str) -> list[dict]:
        pdir = self.persona_dir(run_id, persona_id)
        epath = pdir / "last_elements.json"
        if not epath.is_file():
            return []
        try:
            data = _read_json(epath)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def save_findings(
        self,
        run_id: str,
        persona_id: str,
        report_md: str,
        findings: dict,
    ) -> dict:
        """Validate and atomically persist report.md and findings.json for one persona."""
        safe_pid = _validate_safe_id(persona_id, "persona_id")
        if not isinstance(report_md, str) or not report_md.strip():
            raise ValueError("report_md must be a non-empty string")

        manifest = self.get_manifest(run_id)
        mode = str(manifest.get("mode") or "explore")
        errors = validate_findings(findings, mode=mode)
        if errors:
            raise ValueError("Invalid findings.json: " + "; ".join(errors))

        pdir = self.persona_dir(run_id, safe_pid)
        report_path = pdir / "report.md"
        findings_path = pdir / "findings.json"
        now = _utc_now_iso()

        counts = _empty_issue_counts()
        for issue in findings.get("issues") or []:
            sev = issue.get("severity")
            if sev in counts:
                counts[sev] += 1

        with self._run_lock(run_id):
            state = self._load_state_unlocked(run_id)
            personas = state.get("personas") or {}
            if safe_pid not in personas:
                raise KeyError(f"Persona {safe_pid!r} not found in run {run_id!r}")
            p = personas[safe_pid]

            text = report_md if report_md.endswith("\n") else report_md + "\n"
            _write_text_atomic(report_path, text)
            _write_json_atomic(findings_path, findings)

            p["issue_counts"] = counts
            if _is_number(findings.get("score")):
                p["score"] = findings["score"]
            if "reproduced" in findings and findings["reproduced"] is not None:
                p["reproduced"] = findings["reproduced"]
            if "goal_reached" in findings and findings["goal_reached"] is not None:
                p["goal_reached"] = findings["goal_reached"]
            p["updated_at"] = now
            state["updated_at"] = now
            _write_json_atomic(self._state_path(run_id), state)

        return {
            "report_path": str(report_path),
            "findings_path": str(findings_path),
            "issue_counts": counts,
            "score": findings.get("score"),
        }

    def get_persona_detail(self, run_id: str, persona_id: str) -> dict:
        """Return full persona inspection payload matching `app_qa_get_persona_detail`."""
        safe_pid = _validate_safe_id(persona_id, "persona_id")
        pdir = self.persona_dir(run_id, safe_pid)

        with self._run_lock(run_id):
            state = self._load_state_unlocked(run_id)
            personas = state.get("personas") or {}
            if safe_pid not in personas:
                raise KeyError(f"Persona {safe_pid!r} not found in run {run_id!r}")
            pstate = _clean_persona_dict(personas[safe_pid])

        # Parse steps from actions.jsonl
        steps: list[dict[str, Any]] = []
        step_crashes_by_name: dict[str, dict[str, Any]] = {}
        fallback_logs: list[str] = []
        actions_path = pdir / "actions.jsonl"
        if actions_path.is_file():
            for line in actions_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                steps.append(
                    {
                        "step": rec.get("step"),
                        "timestamp": rec.get("timestamp"),
                        "screenshot_file": rec.get("screenshot_file"),
                        "action": rec.get("action"),
                        "a11y": rec.get("a11y"),
                        "app_running": rec.get("app_running"),
                        "crashes": rec.get("crashes") or [],
                        "elements_count": rec.get("elements_count", 0),
                        "ax_unavailable_reason": rec.get("ax_unavailable_reason"),
                    }
                )
                for c in rec.get("crashes") or []:
                    if isinstance(c, dict):
                        cname = str(c.get("name") or f"crash-step-{rec.get('step')}")
                        step_crashes_by_name[cname] = {
                            "name": cname,
                            "excerpt": str(c.get("excerpt") or ""),
                        }
                if isinstance(rec.get("recent_logs"), list) and rec["recent_logs"]:
                    fallback_logs = [str(x) for x in rec["recent_logs"]]

        # Tail logs.txt (fallback to last step's recent_logs)
        logs_path = pdir / "logs.txt"
        if logs_path.is_file():
            all_lines = logs_path.read_text(encoding="utf-8", errors="replace").splitlines()
            logs_tail = all_lines[-100:]
        else:
            logs_tail = fallback_logs[-100:]

        # Collect crash files from crashes/ directory + merge step-reported crashes
        crashes_dir = pdir / "crashes"
        crashes_map: dict[str, dict[str, Any]] = dict(step_crashes_by_name)
        if crashes_dir.is_dir():
            for cfile in sorted(crashes_dir.iterdir()):
                if not cfile.is_file():
                    continue
                try:
                    raw_excerpt = cfile.read_text(encoding="utf-8", errors="replace")[:4000]
                except OSError:
                    raw_excerpt = ""
                crashes_map[cfile.name] = {
                    "name": cfile.name,
                    "excerpt": raw_excerpt,
                }
        crashes = [crashes_map[k] for k in sorted(crashes_map)]

        report_path = pdir / "report.md"
        report_md = (
            report_path.read_text(encoding="utf-8", errors="replace")
            if report_path.is_file()
            else None
        )

        findings_path = pdir / "findings.json"
        findings: dict[str, Any] | None = None
        if findings_path.is_file():
            try:
                loaded = _read_json(findings_path)
                if isinstance(loaded, dict):
                    findings = loaded
            except (OSError, ValueError):
                findings = None

        return {
            "run_id": run_id,
            "persona_id": safe_pid,
            "state": pstate,
            "steps": steps,
            "logs_tail": logs_tail,
            "crashes": crashes,
            "report_md": report_md,
            "report_path": str(report_path) if report_path.is_file() else None,
            "findings": findings,
        }

    def resolve_artifact_path(self, run_id: str, persona_id: str, rel_file: str) -> Path:
        """Strictly resolve an artifact inside `<runs_root>/<run_id>/<persona_id>/`.

        Raises:
            ValueError: on any path traversal, absolute path, symlink escape, or invalid ID.
            FileNotFoundError: when the validated path does not exist or is not a file.
        """
        safe_run = _validate_safe_id(run_id, "run_id")
        safe_persona = _validate_safe_id(persona_id, "persona_id")

        if not isinstance(rel_file, str) or not rel_file.strip():
            raise ValueError("rel_file must be a non-empty relative path")
        if "\x00" in rel_file or "\\" in rel_file:
            raise ValueError(f"Illegal characters in artifact path: {rel_file!r}")

        rel_path = Path(rel_file)
        if rel_path.is_absolute():
            raise ValueError(f"Absolute artifact paths are forbidden: {rel_file!r}")
        for part in rel_path.parts:
            if part in ("", ".", ".."):
                raise ValueError(f"Path traversal segment forbidden: {rel_file!r}")

        base_dir = (self._runs_root / safe_run / safe_persona).resolve()
        if not base_dir.is_relative_to(self._runs_root):
            raise ValueError("Resolved persona directory escapes runs_root")

        candidate = (base_dir / rel_path).resolve()
        if candidate == base_dir or not candidate.is_relative_to(base_dir):
            raise ValueError(f"Artifact path escapes persona directory: {rel_file!r}")

        if not candidate.exists() or not candidate.is_file():
            raise FileNotFoundError(
                f"Artifact not found: {safe_run}/{safe_persona}/{rel_file}"
            )
        return candidate
