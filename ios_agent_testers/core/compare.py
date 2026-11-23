"""A/B QA run comparator (COMPARE.md + compare.json).

Full behavioral parity with upstream `mobile-agent-testers/scripts/compare.py`,
with structured `CompareDict` JSON output and optional semantic-group diffing
when both runs contain grouped `summary.json` files.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any

from ios_agent_testers.core.summarize import (
    AREAS,
    GOALS,
    SEVERITIES,
    SEVERITY_RANK,
    _load_run_personas_map,
    _write_text_atomic,
    cell,
    fmt_num,
    number,
    per_persona,
    phone_info,
)


def _load_run(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Load per-persona score, goal, actions, issues, and improvement areas."""
    run: dict[str, dict[str, Any]] = {}
    for persona, data in per_persona(run_dir):
        goal = str(data.get("goal_reached") or "").strip().lower()
        issues = [
            {
                "severity": str(i.get("severity") or "?").strip().lower(),
                "title": str(i.get("title") or "?").strip(),
            }
            for i in data.get("issues") or []
            if isinstance(i, dict)
        ]
        areas = [
            str(i.get("area") or "").strip().lower()
            for i in data.get("improvements") or []
            if isinstance(i, dict) and str(i.get("title") or "").strip()
        ]
        run[persona] = {
            "score": number(data.get("score")),
            "actions": number(data.get("actions")),
            "goal": goal if goal in GOALS else "",
            "issues": issues,
            "areas": [a if a in AREAS else "other" for a in areas],
        }
    return run


def _load_grouped_summary(run_dir: Path) -> dict[str, Any] | None:
    spath = run_dir / "summary.json"
    if not spath.is_file():
        return None
    try:
        data = json.loads(spath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        isinstance(data, dict)
        and data.get("grouped") is True
        and isinstance(data.get("issues"), list)
    ):
        return data
    return None


def _run_overview_struct(run: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ps = list(run.values())
    goals = [p["goal"] for p in ps if p["goal"]]
    actions = [p["actions"] for p in ps if p["actions"] is not None]
    scores = [p["score"] for p in ps if p["score"] is not None]
    sev_counts = {s: 0 for s in SEVERITIES}
    for p in ps:
        for i in p["issues"]:
            if i["severity"] in sev_counts:
                sev_counts[i["severity"]] += 1

    goal_counts = {g: goals.count(g) for g in GOALS}
    med_raw = median(actions) if actions else None
    med_actions: int | float | None = (
        int(med_raw)
        if isinstance(med_raw, float) and med_raw.is_integer()
        else med_raw
    )
    avg_score = round(sum(scores) / len(scores), 1) if scores else None
    return {
        "persona_count": len(ps),
        "has_goals": bool(goals),
        "goal_counts": goal_counts,
        "median_actions": med_actions,
        "avg_score": avg_score,
        "raw_avg_score": (sum(scores) / len(scores)) if scores else None,
        "severity_counts": sev_counts,
    }


def _overview_markdown_cells(ov: dict[str, Any]) -> list[str]:
    return [
        str(ov["persona_count"]),
        (
            " / ".join(str(ov["goal_counts"][g]) for g in GOALS)
            if ov["has_goals"]
            else ""
        ),
        fmt_num(ov["median_actions"]) if ov["median_actions"] is not None else "",
        f"{ov['raw_avg_score']:.1f}/10" if ov["raw_avg_score"] is not None else "",
        " / ".join(str(ov["severity_counts"][s]) for s in SEVERITIES),
    ]


def _only_in_raw(
    run: dict[str, dict[str, Any]],
    other: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Issues whose title (case-insensitive) never appears in `other`, most severe first."""
    seen = {i["title"].lower() for p in other.values() for i in p["issues"]}
    found: dict[str, dict[str, Any]] = {}
    for persona, p in run.items():
        for i in p["issues"]:
            key = i["title"].lower()
            if key not in seen:
                entry = found.setdefault(
                    key,
                    {
                        "title": i["title"],
                        "severity": i["severity"],
                        "personas": set(),
                    },
                )
                entry["personas"].add(persona)
                if SEVERITY_RANK.get(i["severity"], 9) < SEVERITY_RANK.get(
                    entry["severity"], 9
                ):
                    entry["severity"] = i["severity"]
    rows = sorted(
        found.values(),
        key=lambda f: (
            SEVERITY_RANK.get(f["severity"], 9),
            -len(f["personas"]),
            f["title"].lower(),
        ),
    )
    return [
        {
            "title": r["title"],
            "severity": r["severity"],
            "personas": sorted(r["personas"]),
        }
        for r in rows
    ]


def _group_signature_titles(group: dict[str, Any]) -> set[str]:
    titles: set[str] = set()
    gtitle = str(group.get("title") or "").strip().lower()
    if gtitle:
        titles.add(gtitle)
    for rep in group.get("reports") or []:
        if isinstance(rep, dict):
            rt = str(rep.get("title") or "").strip().lower()
            if rt:
                titles.add(rt)
    return titles


def _only_in_semantic(
    summary_run: dict[str, Any],
    summary_other: dict[str, Any],
) -> list[dict[str, Any]]:
    """Semantic-group diff when both runs have grouped `summary.json` files."""
    other_titles: set[str] = set()
    for g in summary_other.get("issues") or []:
        if isinstance(g, dict):
            other_titles.update(_group_signature_titles(g))

    unique_rows: list[dict[str, Any]] = []
    for g in summary_run.get("issues") or []:
        if not isinstance(g, dict):
            continue
        sig = _group_signature_titles(g)
        if sig and not (sig & other_titles):
            personas = sorted(
                {str(p) for p in (g.get("personas") or []) if str(p).strip()}
            )
            unique_rows.append(
                {
                    "title": str(g.get("title") or "?").strip(),
                    "severity": str(g.get("severity") or "?").strip().lower(),
                    "personas": personas,
                }
            )

    unique_rows.sort(
        key=lambda f: (
            SEVERITY_RANK.get(f["severity"], 9),
            -len(f["personas"]),
            f["title"].lower(),
        )
    )
    return unique_rows


def _score_delta(
    score_a: int | float | None,
    score_b: int | float | None,
) -> int | float | None:
    if score_a is None or score_b is None:
        return None
    diff = score_b - score_a
    if isinstance(score_a, int) and isinstance(score_b, int):
        return int(diff)
    return round(float(diff), 2)


def compare_runs(
    run_a_dir: Path,
    run_b_dir: Path,
    *,
    name_a: str = "A",
    name_b: str = "B",
) -> tuple[str, dict]:
    """Compare two QA runs, writing `COMPARE.md` & `compare.json` inside `run_b_dir`.

    Returns `(markdown_text, compare_dict)`.
    """
    dir_a = Path(run_a_dir)
    dir_b = Path(run_b_dir)
    na = str(name_a or "A")
    nb = str(name_b or "B")

    run_a = _load_run(dir_a)
    run_b = _load_run(dir_b)

    personas_a_map, _ = _load_run_personas_map(dir_a)
    personas_b_map, _ = _load_run_personas_map(dir_b)
    personas_map = {**personas_a_map, **personas_b_map}

    both = sorted(set(run_a) & set(run_b))
    only_a_personas = sorted(set(run_a) - set(run_b))
    only_b_personas = sorted(set(run_b) - set(run_a))
    alone = sorted(set(run_a) ^ set(run_b))

    ov_a = _run_overview_struct(run_a)
    ov_b = _run_overview_struct(run_b)

    lines: list[str] = [
        f"# {na} vs {nb}",
        "",
        f"The same personas on two versions of the app ({na}: {dir_a}, {nb}: {dir_b}). "
        f"{len(both)} personas ran both.",
    ]
    if alone:
        lines.append(
            f"Only on one version, so not in the persona table: {', '.join(alone)}."
        )
    lines += [
        "Simulated users show what blocks or confuses them on each version. They do not predict "
        "which version converts better: confirm with real users before you decide.",
        "",
        f"| | {cell(na)} | {cell(nb)} |",
        "|---|---|---|",
    ]
    labels = [
        "Personas",
        "Goal reached (yes / partly / no)",
        "Median actions",
        "Average score",
        "Issues (blocker / major / minor / cosmetic)",
    ]
    for label, x_cell, y_cell in zip(
        labels, _overview_markdown_cells(ov_a), _overview_markdown_cells(ov_b)
    ):
        lines.append(f"| {label} | {x_cell} | {y_cell} |")

    lines += [
        "",
        "## Persona by persona",
        "",
        f"| Persona | Device | Language | Goal {cell(na)} | Goal {cell(nb)} "
        f"| Actions {cell(na)} | Actions {cell(nb)} | Score {cell(na)} | Score {cell(nb)} "
        f"| Issues {cell(na)} | Issues {cell(nb)} |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    up = down = same = 0
    persona_rows: list[dict[str, Any]] = []
    for p in both:
        x, y = run_a[p], run_b[p]
        device, locale, _ = phone_info(personas_map.get(p))
        lines.append(
            f"| {p} | {cell(device)} | {locale} | {x['goal']} | {y['goal']} "
            f"| {fmt_num(x['actions'])} | {fmt_num(y['actions'])} "
            f"| {fmt_num(x['score'])} | {fmt_num(y['score'])} "
            f"| {len(x['issues'])} | {len(y['issues'])} |"
        )
        if x["score"] is not None and y["score"] is not None:
            if y["score"] > x["score"]:
                up += 1
            elif y["score"] < x["score"]:
                down += 1
            else:
                same += 1
        persona_rows.append(
            {
                "persona": p,
                "score_a": x["score"],
                "score_b": y["score"],
                "delta": _score_delta(x["score"], y["score"]),
                "actions_a": x["actions"],
                "actions_b": y["actions"],
                "goal_a": x["goal"] or None,
                "goal_b": y["goal"] or None,
                "issues_a": len(x["issues"]),
                "issues_b": len(y["issues"]),
            }
        )

    total_scored = up + down + same
    if total_scored:
        lines += [
            "",
            f"{nb} scored higher than {na} for {up} of {total_scored} personas, "
            f"lower for {down}, the same for {same}. "
            "With ten personas, a gap of one or two is noise.",
        ]

    # Issue diff: use semantic groups when both runs have grouped summary.json, else exact title diff
    sum_a = _load_grouped_summary(dir_a)
    sum_b = _load_grouped_summary(dir_b)
    used_semantic = sum_a is not None and sum_b is not None
    if used_semantic:
        assert sum_a is not None and sum_b is not None
        only_in_a = _only_in_semantic(sum_a, sum_b)
        only_in_b = _only_in_semantic(sum_b, sum_a)
    else:
        only_in_a = _only_in_raw(run_a, run_b)
        only_in_b = _only_in_raw(run_b, run_a)

    for label_name, found in ((na, only_in_a), (nb, only_in_b)):
        lines += ["", f"## Issues seen only on {label_name}", ""]
        if not found:
            lines.append("None.")
        else:
            for item in found[:20]:
                lines.append(
                    f"- {item['severity']}: {cell(item['title'])} "
                    f"({', '.join(item['personas'])})"
                )
            if len(found) > 20:
                lines.append(f"- and {len(found) - 20} more")

    if used_semantic:
        lines += [
            "",
            "Compared using semantic issue groups from summary.json. "
            "Confirm subtle wording differences by reading both lists side by side.",
        ]
    else:
        lines += [
            "",
            "The same bug described in other words appears on both lists: read them side by side.",
        ]

    areas_a: dict[str, int] = defaultdict(int)
    areas_b: dict[str, int] = defaultdict(int)
    for p in run_a.values():
        for area in p["areas"]:
            areas_a[area] += 1
    for p in run_b.values():
        for area in p["areas"]:
            areas_b[area] += 1

    all_areas = sorted(
        set(areas_a) | set(areas_b),
        key=lambda k: (-(areas_a[k] + areas_b[k]), k),
    )
    improvements_by_area: list[dict[str, Any]] = []
    if all_areas:
        lines += [
            "",
            "## Improvements suggested, by area",
            "",
            f"| Area | {cell(na)} | {cell(nb)} |",
            "|---|---|---|",
        ]
        for area in all_areas:
            ca, cb = areas_a[area], areas_b[area]
            lines.append(f"| {area} | {ca} | {cb} |")
            improvements_by_area.append(
                {"area": area, "count_a": ca, "count_b": cb}
            )

    md_text = "\n".join(lines) + "\n"
    compare_dict: dict[str, Any] = {
        "name_a": na,
        "name_b": nb,
        "overview": {
            "personas": [ov_a["persona_count"], ov_b["persona_count"]],
            "goal_reached": [ov_a["goal_counts"], ov_b["goal_counts"]],
            "median_actions": [ov_a["median_actions"], ov_b["median_actions"]],
            "avg_score": [ov_a["avg_score"], ov_b["avg_score"]],
            "issues": [ov_a["severity_counts"], ov_b["severity_counts"]],
        },
        "personas": persona_rows,
        "only_a_personas": only_a_personas,
        "only_b_personas": only_b_personas,
        "score_summary": {"higher": up, "lower": down, "same": same},
        "only_in_a": only_in_a,
        "only_in_b": only_in_b,
        "improvements_by_area": improvements_by_area,
    }

    _write_text_atomic(dir_b / "COMPARE.md", md_text)
    _write_text_atomic(
        dir_b / "compare.json",
        json.dumps(compare_dict, ensure_ascii=False, indent=2) + "\n",
    )
    return md_text, compare_dict
