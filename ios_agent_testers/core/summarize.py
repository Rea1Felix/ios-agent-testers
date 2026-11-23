"""Cross-persona QA findings summarizer (SUMMARY.md + summary.json).

Full behavioral parity with upstream `mobile-agent-testers/scripts/summarize.py`,
upgraded with structured `SummaryDict` output for the Antigravity QA Dashboard
and native support for externally supplied semantic groups.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Iterator

ISSUE_CLASSES: tuple[str, ...] = (
    "layout and text size",
    "feedback and errors",
    "accessibility",
    "translation",
    "right to left",
    "tablet",
    "system bars",
    "copy",
    "visual polish",
    "crash",
    "likely test artefact",
)

SEVERITY_RANK: dict[str, int] = {
    "blocker": 0,
    "major": 1,
    "minor": 2,
    "cosmetic": 3,
}

SEVERITIES: tuple[str, ...] = ("blocker", "major", "minor", "cosmetic")
RESULTS: tuple[str, ...] = ("yes", "unclear", "no")
GOALS: tuple[str, ...] = ("yes", "partly", "no")
IMPACTS: tuple[str, ...] = ("high", "medium", "low")
AREAS: tuple[str, ...] = (
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

_GROUPING_PROMPT_TEMPLATE = """Below are bug reports from AI testers. Each tester used the same app on a
different phone, often in a different language. Group the reports that describe
the same underlying bug: same screen or element and same cause, even when the
wording or the language differs. Different elements, strings or screens are
different bugs, even when they look alike (two mistranslated labels are two bugs).
For each group, write a short English title and pick one class from this list:
{classes}.
Answer with JSON only, in this shape:
{{"groups": [{{"title": "...", "class": "...", "ids": [1, 4]}}]}}
Every report id must be in exactly one group.

Reports (id. [persona, severity, screen] title):
{reports}
"""


def number(value: Any) -> int | float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    ):
        return value
    return None


def cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r\n", " ").replace("\n", " ")


def fmt_num(value: int | float | None) -> str:
    return "" if value is None else f"{value:g}"


def _as_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


def per_persona(run_dir: Path | str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield `(persona_id, findings_dict)` from a run directory.

    Supports both local RunStore layout (`<run_dir>/<persona>/findings.json`)
    and upstream collected-artifact layouts (`<run_dir>/results/<persona>/findings.json`
    and `<run_dir>/*/results/<persona>/findings.json`).
    """
    root = Path(run_dir)
    candidates: list[Path] = []
    if root.is_dir():
        # 1. Local RunStore layout: <run_dir>/<persona>/findings.json
        for f in sorted(root.glob("*/findings.json")):
            if f.parent.name.startswith(".") or f.parent.name == "results":
                continue
            candidates.append(f)
        # 2. Direct results/ layout: <run_dir>/results/<persona>/findings.json
        for f in sorted(root.glob("results/*/findings.json")):
            candidates.append(f)
        # 3. Upstream CI layout: <run_dir>/*/results/<persona>/findings.json
        for f in sorted(root.glob("*/results/*/findings.json")):
            candidates.append(f)

    seen_personas: set[str] = set()
    for fpath in candidates:
        persona = fpath.parent.name
        if persona in seen_personas:
            continue
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            seen_personas.add(persona)
            yield persona, data


def _load_default_personas_map() -> dict[str, dict[str, Any]]:
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "personas.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, list):
            return {
                str(p["id"]): p
                for p in data
                if isinstance(p, dict) and p.get("id")
            }
    return {}


def _load_run_personas_map(run_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Build persona metadata map combining personas.json, manifest.json, and state.json."""
    personas_map = _load_default_personas_map()
    manifest: dict[str, Any] = {}

    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded
                for p in loaded.get("personas") or []:
                    if isinstance(p, dict) and p.get("id"):
                        personas_map[str(p["id"])] = dict(p)
        except (OSError, ValueError):
            pass

    state_path = run_dir / "state.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and isinstance(state.get("personas"), dict):
                for pid, pstate in state["personas"].items():
                    if not isinstance(pstate, dict):
                        continue
                    merged = dict(personas_map.get(pid) or {})
                    if pstate.get("locale"):
                        merged["locale"] = pstate["locale"]
                    if isinstance(pstate.get("device"), dict) and pstate["device"]:
                        merged["device"] = dict(pstate["device"])
                    if pstate.get("device_label"):
                        merged["device_label"] = pstate["device_label"]
                    personas_map[pid] = merged
        except (OSError, ValueError):
            pass

    return personas_map, manifest


def phone_info(persona: dict[str, Any] | None) -> tuple[str, str, str]:
    """Return `(device, locale, text_size)` for summary and comparison tables."""
    if not isinstance(persona, dict):
        return "", "", ""
    device_label = str(persona.get("device_label") or "").strip()
    d = persona.get("device") if isinstance(persona.get("device"), dict) else {}
    if device_label:
        name = device_label
    elif d.get("name"):
        name = str(d["name"])
    elif "simulator" in d:
        name = f'{d["simulator"]}, iOS {d.get("ios", "?")}'
    else:
        name = ""
    size = f'x{d["font_scale"]}' if "font_scale" in d else str(d.get("text_size") or "")
    locale = str(persona.get("locale") or "")
    return name, locale, size


def _find_bug_report_title(run_dir: Path, manifest: dict[str, Any]) -> str | None:
    if manifest.get("bug_report_title"):
        return str(manifest["bug_report_title"]).strip() or None
    candidates = [
        run_dir / "bug_report.md",
        run_dir / "results" / "bug_report.md",
        *sorted(run_dir.glob("*/results/bug_report.md")),
    ]
    for path in candidates:
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        if lines:
            title = lines[0].lstrip("#").strip()
            if title:
                return title
    return None


def collect_run_findings(run_dir: Path) -> dict[str, Any]:
    """Collect findings, scores, reproduction verdicts, and walkthrough data from `run_dir`."""
    rdir = Path(run_dir)
    personas_map, manifest = _load_run_personas_map(rdir)
    run_id = str(manifest.get("run_id") or rdir.name)
    bug_report_title = _find_bug_report_title(rdir, manifest)

    items: list[dict[str, Any]] = []
    scores: list[int | float] = []
    reproduction_rows: list[dict[str, Any]] = []
    walkthrough_rows: list[dict[str, Any]] = []
    persona_ids: list[str] = []

    for persona, data in per_persona(rdir):
        persona_ids.append(persona)
        sc = number(data.get("score"))
        if sc is not None:
            scores.append(sc)

        for raw_issue in data.get("issues") or []:
            if not isinstance(raw_issue, dict):
                continue
            screens_raw = raw_issue.get("screens")
            screens = (
                [str(s) for s in screens_raw]
                if isinstance(screens_raw, list)
                else []
            )
            items.append(
                {
                    "id": len(items) + 1,
                    "persona": persona,
                    "severity": str(raw_issue.get("severity") or "?").strip().lower(),
                    "title": str(raw_issue.get("title") or "?").strip(),
                    "screen": str(raw_issue.get("screen") or "").strip(),
                    "screens": screens,
                }
            )

        repro_ans = str(data.get("reproduced") or "").strip().lower()
        if repro_ans:
            ev_raw = data.get("evidence")
            evidence = [str(e) for e in ev_raw] if isinstance(ev_raw, list) else []
            reproduction_rows.append(
                {
                    "persona": persona,
                    "result": repro_ans if repro_ans in RESULTS else "unclear",
                    "evidence": evidence,
                    "note": str(data.get("note") or "").strip(),
                }
            )

        goal = str(data.get("goal_reached") or "").strip().lower()
        ideas: list[dict[str, Any]] = []
        for raw_imp in data.get("improvements") or []:
            if not isinstance(raw_imp, dict) or not str(raw_imp.get("title") or "").strip():
                continue
            area = str(raw_imp.get("area") or "").strip().lower()
            impact = str(raw_imp.get("impact") or "").strip().lower()
            screens_raw = raw_imp.get("screens")
            screens = (
                [str(s) for s in screens_raw]
                if isinstance(screens_raw, list)
                else []
            )
            ideas.append(
                {
                    "title": str(raw_imp["title"]).strip(),
                    "area": area if area in AREAS else "other",
                    "impact": impact if impact in IMPACTS else "?",
                    "why": str(raw_imp.get("why") or "").strip(),
                    "screen": str(raw_imp.get("screen") or "").strip(),
                    "screens": screens,
                }
            )
        if goal or ideas:
            walkthrough_rows.append(
                {
                    "persona": persona,
                    "goal": goal if goal in GOALS else ("?" if goal else ""),
                    "actions": number(data.get("actions")),
                    "score": sc,
                    "ideas": ideas,
                }
            )

    mode = str(manifest.get("mode") or "").strip().lower()
    if not mode:
        if reproduction_rows:
            mode = "reproduce"
        elif walkthrough_rows:
            mode = "improve"
        else:
            mode = "explore"

    return {
        "run_id": run_id,
        "mode": mode,
        "bug_report_title": bug_report_title,
        "personas_map": personas_map,
        "persona_ids": persona_ids,
        "scores": scores,
        "items": items,
        "reproduction_rows": reproduction_rows,
        "walkthrough_rows": walkthrough_rows,
    }


def group_by_title(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic fallback grouping: merge identical titles case-insensitively."""
    groups: dict[str, dict[str, Any]] = {}
    for item in items:
        key = item["title"].lower()
        g = groups.setdefault(
            key,
            {"title": item["title"], "class": "", "ids": []},
        )
        g["ids"].append(item["id"])
    return list(groups.values())


def build_grouping_prompt(items: list[dict[str, Any]]) -> str:
    """Build the semantic-grouping prompt sending only `(id, persona, severity, screen, title)`."""
    reports = "\n".join(
        f'{item["id"]}. [{item["persona"]}, {item["severity"]}, {item["screen"]}] {item["title"]}'
        for item in items
    )
    return _GROUPING_PROMPT_TEMPLATE.format(
        classes=", ".join(ISSUE_CLASSES),
        reports=reports,
    )


def extract_grouping_items(summary_dict: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten `summary_dict['issues'][*]['reports']` into compact grouping items sorted by `id`.

    Returns `[{"id": int, "persona": str, "severity": str, "screen": str, "title": str}]`
    deduplicated by `id` (first occurrence wins), matching the exact `1..N` IDs accepted by
    `validate_and_repair_groups`.
    """
    if not isinstance(summary_dict, dict):
        return []
    issues = summary_dict.get("issues")
    if not isinstance(issues, list):
        return []

    seen: set[int] = set()
    flattened: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        reports = issue.get("reports")
        if not isinstance(reports, list):
            continue
        for report in reports:
            if not isinstance(report, dict):
                continue
            parsed_id = _as_id(report.get("id"))
            if parsed_id is None or parsed_id in seen:
                continue
            seen.add(parsed_id)
            flattened.append(
                {
                    "id": parsed_id,
                    "persona": str(report.get("persona") or "").strip(),
                    "severity": str(report.get("severity") or "?").strip().lower(),
                    "screen": str(report.get("screen") or "").strip(),
                    "title": str(report.get("title") or "?").strip(),
                }
            )

    flattened.sort(key=lambda item: item["id"])
    return flattened


def _extract_groups_list(raw_groups: dict | list | str) -> list[Any] | None:
    obj: Any = raw_groups
    if isinstance(obj, str):
        text = obj.strip()
        if not text:
            return None
        parsed: Any = None
        try:
            parsed = json.loads(text)
        except ValueError:
            match = re.search(r"\{.*\}", text, re.S)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                except ValueError:
                    parsed = None
            if parsed is None:
                arr_match = re.search(r"\[.*\]", text, re.S)
                if arr_match:
                    try:
                        parsed = json.loads(arr_match.group(0))
                    except ValueError:
                        parsed = None
        if parsed is None:
            return None
        obj = parsed

    # Handle CLI wrapper envelope such as `{"result": "{\"groups\": [...]}"}`
    if isinstance(obj, dict) and "groups" not in obj and isinstance(obj.get("result"), str):
        return _extract_groups_list(obj["result"])

    if isinstance(obj, dict):
        groups = obj.get("groups")
        return groups if isinstance(groups, list) else None
    if isinstance(obj, list):
        return obj
    return None


def _validate_and_repair_groups_internal(
    raw_groups: dict | list | str,
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    if not items:
        return [], False

    parsed_groups = _extract_groups_list(raw_groups)
    if not parsed_groups:
        return group_by_title(items), False

    valid = {item["id"] for item in items}
    by_id = {item["id"]: item for item in items}
    seen: set[int] = set()
    repaired: list[dict[str, Any]] = []

    for g in parsed_groups:
        if not isinstance(g, dict):
            continue
        raw_ids = g.get("ids")
        if not isinstance(raw_ids, list):
            continue
        ids: list[int] = []
        for raw_id in raw_ids:
            parsed_id = _as_id(raw_id)
            if parsed_id is not None and parsed_id in valid and parsed_id not in seen:
                seen.add(parsed_id)
                ids.append(parsed_id)
        if not ids:
            continue
        title = str(g.get("title") or "").strip() or by_id[ids[0]]["title"]
        raw_class = g.get("class")
        cls = raw_class if raw_class in ISSUE_CLASSES else ""
        repaired.append({"title": title, "class": cls, "ids": ids})

    if not repaired:
        return group_by_title(items), False

    for missing_id in sorted(valid - seen):
        repaired.append(
            {
                "title": by_id[missing_id]["title"],
                "class": "",
                "ids": [missing_id],
            }
        )

    return repaired, True


def validate_and_repair_groups(
    raw_groups: dict | list | str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate and repair semantic issue groups.

    Strips unknown or duplicate IDs (first group wins), blanks unknown classes,
    and appends any ungrouped report IDs as singleton groups. Falls back to
    deterministic title grouping if `raw_groups` is unusable.
    """
    repaired, _ = _validate_and_repair_groups_internal(raw_groups, items)
    return repaired


def _group_with_external_cli(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Optional CLI hook for terminal users (`SUMMARY_LLM_CMD` or `claude -p`)."""
    if os.environ.get("SUMMARY_GROUPING", "1") == "0" or len(items) < 2:
        return group_by_title(items), False

    custom_cmd = os.environ.get("SUMMARY_LLM_CMD", "").strip()
    if custom_cmd:
        cmd = shlex.split(custom_cmd)
    else:
        claude_exe = shutil.which("claude")
        if not claude_exe:
            return group_by_title(items), False
        cmd = [
            claude_exe,
            "-p",
            "--output-format",
            "json",
            "--model",
            os.environ.get("SUMMARY_MODEL", "sonnet"),
        ]

    prompt = build_grouping_prompt(items)
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return group_by_title(items), False
        return _validate_and_repair_groups_internal(proc.stdout, items)
    except Exception as exc:
        print(
            f"External LLM grouping skipped ({type(exc).__name__}): identical titles only",
            file=sys.stderr,
        )
        return group_by_title(items), False


def _render_reproduction(
    rows: list[dict[str, Any]],
    bug_title: str | None,
    personas_map: dict[str, dict[str, Any]],
) -> tuple[list[str], dict[str, Any]]:
    counts = {r: sum(x["result"] == r for x in rows) for r in RESULTS}
    lines = ["## Reproduction", ""]
    if bug_title:
        lines.append(f"Bug: {cell(bug_title)}")
    lines.append(
        f"Reproduced on {counts['yes']} of {len(rows)} devices, not reproduced on {counts['no']}, "
        f"unclear on {counts['unclear']}. Open the evidence screenshots before you trust a row."
    )
    lines += [
        "",
        "| Reproduced | Persona | Device | Language | Text size | Evidence | Note |",
        "|---|---|---|---|---|---|---|",
    ]
    sorted_rows = sorted(rows, key=lambda x: (RESULTS.index(x["result"]), x["persona"]))
    json_rows: list[dict[str, Any]] = []
    for r in sorted_rows:
        device, locale, size = phone_info(personas_map.get(r["persona"]))
        lines.append(
            f"| {r['result']} | {r['persona']} | {cell(device)} | {locale} | {size} "
            f"| {cell(', '.join(r['evidence']))} | {cell(r['note'])} |"
        )
        json_rows.append(
            {
                "persona": r["persona"],
                "reproduced": r["result"],
                "device": device,
                "locale": locale,
                "text_size": size,
                "evidence": list(r["evidence"]),
                "note": r["note"],
            }
        )
    return lines + [""], {"counts": counts, "rows": json_rows}


def _render_walkthrough(
    rows: list[dict[str, Any]],
    personas_map: dict[str, dict[str, Any]],
) -> tuple[list[str], dict[str, Any]]:
    goal_counts = {g: sum(r["goal"] == g for r in rows) for g in GOALS}
    actions_vals = [r["actions"] for r in rows if r["actions"] is not None]
    med_raw = median(actions_vals) if actions_vals else None
    med_actions: int | float | None = (
        int(med_raw)
        if isinstance(med_raw, float) and med_raw.is_integer()
        else med_raw
    )

    head: list[str] = []
    if any(r["goal"] for r in rows):
        head.append(
            f"Goal reached: yes for {goal_counts['yes']}, partly for {goal_counts['partly']}, "
            f"no for {goal_counts['no']} of {len(rows)} personas."
        )
    if med_actions is not None:
        head.append(f"Median: {med_actions:g} actions.")

    lines = [
        "## Walkthrough",
        "",
        " ".join(head),
        "",
        "| Persona | Device | Language | Text size | Goal reached | Actions | Score |",
        "|---|---|---|---|---|---|---|",
    ]
    sorted_rows = sorted(
        rows,
        key=lambda x: (GOALS.index(x["goal"]) if x["goal"] in GOALS else 3, x["persona"]),
    )
    json_walk_rows: list[dict[str, Any]] = []
    for r in sorted_rows:
        device, locale, size = phone_info(personas_map.get(r["persona"]))
        goal_disp = r["goal"] or "?"
        lines.append(
            f"| {r['persona']} | {cell(device)} | {locale} | {size} | {goal_disp} "
            f"| {fmt_num(r['actions'])} | {fmt_num(r['score'])} |"
        )
        json_walk_rows.append(
            {
                "persona": r["persona"],
                "goal_reached": goal_disp,
                "actions": r["actions"],
                "score": r["score"],
                "device": device,
            }
        )

    ideas = [dict(imp, persona=r["persona"]) for r in rows for imp in r["ideas"]]
    json_areas: list[dict[str, Any]] = []
    json_improvements: list[dict[str, Any]] = []

    if ideas:
        areas_map: dict[str, list[Any]] = defaultdict(lambda: [0, set()])
        for imp in ideas:
            areas_map[imp["area"]][0] += 1
            areas_map[imp["area"]][1].add(imp["persona"])
        ranked = sorted(
            areas_map,
            key=lambda a: (-len(areas_map[a][1]), -areas_map[a][0], a),
        )
        lines += [
            "",
            "## Improvements",
            "",
            f"{len(ideas)} suggestions from {len({i['persona'] for i in ideas})} personas. "
            "The same idea often comes back in other words: read each area together, and open the screenshots.",
            "",
            "| Area | Suggestions | Personas |",
            "|---|---|---|",
        ]
        for a in ranked:
            count, ps = areas_map[a][0], areas_map[a][1]
            lines.append(f"| {a} | {count} | {len(ps)} ({', '.join(sorted(ps))}) |")
            json_areas.append(
                {
                    "area": a,
                    "persona_count": len(ps),
                    "count": count,
                }
            )

        lines += [
            "",
            "| Impact | Area | Improvement | Persona | Screen | Why |",
            "|---|---|---|---|---|---|",
        ]
        sorted_ideas = sorted(
            ideas,
            key=lambda i: (
                IMPACTS.index(i["impact"]) if i["impact"] in IMPACTS else 3,
                ranked.index(i["area"]),
                i["persona"],
            ),
        )
        for imp in sorted_ideas:
            where = imp["screen"] + (
                f" ({', '.join(imp['screens'])})" if imp["screens"] else ""
            )
            lines.append(
                f"| {imp['impact']} | {imp['area']} | {cell(imp['title'])} | {imp['persona']} "
                f"| {cell(where.strip())} | {cell(imp['why'])} |"
            )
            json_improvements.append(
                {
                    "impact": imp["impact"],
                    "area": imp["area"],
                    "title": imp["title"],
                    "persona": imp["persona"],
                    "screen": imp["screen"],
                    "screens": list(imp["screens"]),
                    "why": imp["why"],
                }
            )

    walk_dict = {
        "goal_counts": goal_counts,
        "median_actions": med_actions,
        "rows": json_walk_rows,
        "areas": json_areas,
        "improvements": json_improvements,
    }
    return lines + [""], walk_dict


def summarize_run(
    run_dir: Path,
    *,
    groups_json: dict | list | str | None = None,
    use_external_llm: bool = False,
) -> tuple[str, dict]:
    """Summarize all persona findings under `run_dir`, writing `SUMMARY.md` & `summary.json`.

    Returns `(markdown_text, summary_dict)`.
    """
    rdir = Path(run_dir)
    collected = collect_run_findings(rdir)
    items: list[dict[str, Any]] = collected["items"]
    scores: list[int | float] = collected["scores"]
    repro_rows: list[dict[str, Any]] = collected["reproduction_rows"]
    walk_rows: list[dict[str, Any]] = collected["walkthrough_rows"]
    personas_map: dict[str, dict[str, Any]] = collected["personas_map"]

    if groups_json is not None:
        groups, grouped = _validate_and_repair_groups_internal(groups_json, items)
    elif use_external_llm:
        groups, grouped = _group_with_external_cli(items)
    else:
        groups, grouped = group_by_title(items), False

    by_id = {item["id"]: item for item in items}
    issue_rows: list[dict[str, Any]] = []
    for g in groups:
        fs = [by_id[i] for i in g["ids"]]
        issue_rows.append(
            {
                "title": g["title"] or fs[0]["title"],
                "class": g["class"] or "other",
                "reports": fs,
                "personas": sorted({f["persona"] for f in fs}),
                "sev": min(
                    (f["severity"] for f in fs),
                    key=lambda s: SEVERITY_RANK.get(s, 9),
                ),
                "screens": sorted({f["screen"] for f in fs if f["screen"]}),
            }
        )
    issue_rows.sort(
        key=lambda r: (
            SEVERITY_RANK.get(r["sev"], 9),
            -len(r["personas"]),
            r["title"].lower(),
        )
    )

    severity_counts = {sev: 0 for sev in SEVERITIES}
    for item in items:
        if item["severity"] in severity_counts:
            severity_counts[item["severity"]] += 1

    reporting_personas = sorted({item["persona"] for item in items})
    all_persona_count = len(collected["persona_ids"])
    avg_score = round(sum(scores) / len(scores), 1) if scores else None

    lines: list[str] = ["# Agent test summary", ""]
    repro_dict: dict[str, Any] | None = None
    walk_dict: dict[str, Any] | None = None

    has_repro = bool(repro_rows)
    has_walk = bool(walk_rows)

    if has_repro:
        repro_lines, repro_dict = _render_reproduction(
            repro_rows, collected["bug_report_title"], personas_map
        )
        lines += repro_lines

    if has_walk:
        walk_lines, walk_dict = _render_walkthrough(walk_rows, personas_map)
        lines += walk_lines

    classes_json: list[dict[str, Any]] = []

    if has_repro or has_walk:
        lines += [
            "## Everything else the agents reported" if has_repro else "## Issues",
            "",
        ]
        if not items:
            lines.append("Nothing else." if has_repro else "No issues reported.")
            md_text = "\n".join(lines) + "\n"
            summary_dict = _build_summary_dict(
                collected=collected,
                grouped=grouped,
                persona_count=all_persona_count,
                items=items,
                issue_rows=issue_rows,
                avg_score=avg_score,
                severity_counts=severity_counts,
                repro_dict=repro_dict,
                walk_dict=walk_dict,
                classes_json=classes_json,
            )
            _write_text_atomic(rdir / "SUMMARY.md", md_text)
            _write_text_atomic(
                rdir / "summary.json",
                json.dumps(summary_dict, ensure_ascii=False, indent=2) + "\n",
            )
            return md_text, summary_dict

    head = f"{len(items)} reports from {len(reporting_personas)} personas"
    if scores:
        head += f", average score {sum(scores) / len(scores):.1f}/10"
    lines.append(head + ".")

    if grouped:
        alone = sum(len(r["personas"]) == 1 for r in issue_rows)
        lines.append(
            f"Grouped into {len(issue_rows)} distinct issues: {alone} found by one persona only, "
            f"{len(issue_rows) - alone} by several. Open the screenshots before you trust a group."
        )
    elif items:
        lines.append(
            "Only identical titles are merged: supply semantic groups (or enable LLM grouping) "
            "to merge reports describing the same bug in different words."
        )

    extra = (" Class |", "---|") if grouped else ("", "")
    lines += [
        "",
        "| Severity | Issue | Personas | Screen |" + extra[0],
        "|---|---|---|---|" + extra[1],
    ]
    for r in issue_rows:
        lines.append(
            f"| {r['sev']} | {cell(r['title'])} | {len(r['personas'])} ({', '.join(r['personas'])}) "
            f"| {cell(', '.join(r['screens']))} |"
            + (f" {r['class']} |" if grouped else "")
        )

    if grouped:
        classes_agg: dict[str, list[Any]] = defaultdict(lambda: [0, set()])
        for r in issue_rows:
            classes_agg[r["class"]][0] += 1
            classes_agg[r["class"]][1].update(r["personas"])
        lines += [
            "",
            "## Which personas found each class of issue",
            "",
            "| Class | Issues | Personas |",
            "|---|---|---|",
        ]
        for cls_name, (cnt, ps) in sorted(
            classes_agg.items(), key=lambda kv: (-len(kv[1][1]), kv[0])
        ):
            sorted_ps = sorted(ps)
            lines.append(f"| {cls_name} | {cnt} | {len(sorted_ps)} ({', '.join(sorted_ps)}) |")
            classes_json.append(
                {
                    "class": cls_name,
                    "personas": sorted_ps,
                    "count": cnt,
                }
            )

        merged = [r for r in issue_rows if len(r["reports"]) > 1]
        if merged:
            lines += ["", "## Reports behind each grouped issue", ""]
            for r in merged:
                lines.append(f"- **{cell(r['title'])}**")
                lines += [
                    f"  - {f['persona']} ({f['severity']}): {f['title']}"
                    for f in r["reports"]
                ]

    md_text = "\n".join(lines) + "\n"
    summary_dict = _build_summary_dict(
        collected=collected,
        grouped=grouped,
        persona_count=all_persona_count,
        items=items,
        issue_rows=issue_rows,
        avg_score=avg_score,
        severity_counts=severity_counts,
        repro_dict=repro_dict,
        walk_dict=walk_dict,
        classes_json=classes_json,
    )

    _write_text_atomic(rdir / "SUMMARY.md", md_text)
    _write_text_atomic(
        rdir / "summary.json",
        json.dumps(summary_dict, ensure_ascii=False, indent=2) + "\n",
    )
    return md_text, summary_dict


def _build_summary_dict(
    *,
    collected: dict[str, Any],
    grouped: bool,
    persona_count: int,
    items: list[dict[str, Any]],
    issue_rows: list[dict[str, Any]],
    avg_score: float | None,
    severity_counts: dict[str, int],
    repro_dict: dict[str, Any] | None,
    walk_dict: dict[str, Any] | None,
    classes_json: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "run_id": collected["run_id"],
        "mode": collected["mode"],
        "bug_report_title": collected["bug_report_title"],
        "grouped": bool(grouped),
        "persona_count": persona_count,
        "report_count": len(items),
        "distinct_issue_count": len(issue_rows),
        "avg_score": avg_score,
        "severity_counts": severity_counts,
        "reproduction": repro_dict,
        "walkthrough": walk_dict,
        "issues": [
            {
                "title": r["title"],
                "severity": r["sev"],
                "class": r["class"],
                "personas": list(r["personas"]),
                "screens": list(r["screens"]),
                "reports": [
                    {
                        "id": f["id"],
                        "persona": f["persona"],
                        "severity": f["severity"],
                        "screen": f["screen"],
                        "screens": list(f.get("screens") or []),
                        "title": f["title"],
                    }
                    for f in r["reports"]
                ],
            }
            for r in issue_rows
        ],
        "classes": classes_json,
    }
