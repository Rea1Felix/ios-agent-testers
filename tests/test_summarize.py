"""Unit tests for cross-persona QA summarizer (SUMMARY.md + summary.json)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ios_agent_testers.core.summarize import (
    ISSUE_CLASSES,
    SEVERITY_RANK,
    build_grouping_prompt,
    collect_run_findings,
    extract_grouping_items,
    summarize_run,
    validate_and_repair_groups,
)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class TestSummarizeGrouping(unittest.TestCase):
    def test_constants_exact_upstream_parity(self) -> None:
        self.assertEqual(len(ISSUE_CLASSES), 11)
        self.assertEqual(
            ISSUE_CLASSES,
            (
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
            ),
        )
        self.assertEqual(
            SEVERITY_RANK,
            {"blocker": 0, "major": 1, "minor": 2, "cosmetic": 3},
        )

    def test_build_grouping_prompt(self) -> None:
        items = [
            {"id": 1, "persona": "clara", "severity": "major", "screen": "Login", "title": "Button cut off"},
            {"id": 2, "persona": "sven", "severity": "blocker", "screen": "Login", "title": "Knappen klippt"},
        ]
        prompt = build_grouping_prompt(items)
        self.assertIn("1. [clara, major, Login] Button cut off", prompt)
        self.assertIn("2. [sven, blocker, Login] Knappen klippt", prompt)
        for cls in ISSUE_CLASSES:
            self.assertIn(cls, prompt)

    def test_validate_and_repair_groups_dedups_and_fills_missing(self) -> None:
        items = [
            {"id": 1, "persona": "clara", "severity": "major", "screen": "A", "title": "Issue 1"},
            {"id": 2, "persona": "sven", "severity": "minor", "screen": "A", "title": "Issue 2"},
            {"id": 3, "persona": "amira", "severity": "cosmetic", "screen": "B", "title": "Issue 3"},
        ]
        raw = {
            "groups": [
                {
                    "title": "Merged A",
                    "class": "layout and text size",
                    "ids": [1, "1", True, 2, 999],
                },
                {
                    # ID 2 already claimed by group 1 (first group wins); invalid class -> ""
                    "title": "Duplicate only",
                    "class": "not-a-valid-class",
                    "ids": [2],
                },
            ]
        }
        repaired = validate_and_repair_groups(raw, items)
        self.assertEqual(len(repaired), 2)
        self.assertEqual(repaired[0], {"title": "Merged A", "class": "layout and text size", "ids": [1, 2]})
        # Missing ID 3 appended as singleton
        self.assertEqual(repaired[1], {"title": "Issue 3", "class": "", "ids": [3]})

    def test_validate_and_repair_handles_claude_envelope_and_corrupt_fallback(self) -> None:
        items = [
            {"id": 1, "persona": "clara", "severity": "major", "screen": "A", "title": "Same Title"},
            {"id": 2, "persona": "sven", "severity": "minor", "screen": "A", "title": "same title"},
        ]
        envelope = json.dumps(
            {
                "result": 'Here is JSON:\n```json\n{"groups": [{"title": "Canonical", "class": "unknown_cls", "ids": [1, 2]}]}\n```'
            }
        )
        repaired = validate_and_repair_groups(envelope, items)
        self.assertEqual(repaired, [{"title": "Canonical", "class": "", "ids": [1, 2]}])

        # Corrupt input falls back to deterministic case-insensitive title grouping
        fallback = validate_and_repair_groups("not valid json at all", items)
        self.assertEqual(fallback, [{"title": "Same Title", "class": "", "ids": [1, 2]}])

    def test_extract_grouping_items_ordering_dedup_and_empty(self) -> None:
        self.assertEqual(extract_grouping_items({}), [])
        self.assertEqual(extract_grouping_items(None), [])
        self.assertEqual(extract_grouping_items({"issues": []}), [])
        self.assertEqual(extract_grouping_items({"issues": "not-a-list"}), [])

        summary_dict = {
            "issues": [
                {
                    "title": "Issue B",
                    "reports": [
                        {
                            "id": 3,
                            "persona": "sven",
                            "severity": "BLOCKER",
                            "screen": "Settings",
                            "screens": ["05"],
                            "title": "Spara-knapp klippt",
                        },
                        {
                            "id": 1,
                            "persona": "clara",
                            "severity": "major",
                            "screen": "Settings",
                            "screens": ["03"],
                            "title": "Save button clipped",
                        },
                    ],
                },
                {
                    "title": "Issue A",
                    "reports": [
                        {
                            "id": "2",
                            "persona": "amira",
                            "severity": "minor",
                            "screen": "Home",
                            "title": "Icon missing label",
                        },
                        # Duplicate id=1 must be ignored (first occurrence wins)
                        {
                            "id": 1,
                            "persona": "duplicate-persona",
                            "severity": "cosmetic",
                            "screen": "Other",
                            "title": "Duplicate report",
                        },
                        # Invalid / boolean IDs skipped cleanly
                        {"id": True, "persona": "bad", "severity": "minor", "screen": "X", "title": "Bool id"},
                        {"id": "not-an-int", "persona": "bad", "severity": "minor", "screen": "X", "title": "Bad id"},
                    ],
                },
            ]
        }
        items = extract_grouping_items(summary_dict)
        self.assertEqual(
            items,
            [
                {
                    "id": 1,
                    "persona": "clara",
                    "severity": "major",
                    "screen": "Settings",
                    "title": "Save button clipped",
                },
                {
                    "id": 2,
                    "persona": "amira",
                    "severity": "minor",
                    "screen": "Home",
                    "title": "Icon missing label",
                },
                {
                    "id": 3,
                    "persona": "sven",
                    "severity": "blocker",
                    "screen": "Settings",
                    "title": "Spara-knapp klippt",
                },
            ],
        )
        # Ensure compact shape (exact 5 keys, no screens list bloat)
        for item in items:
            self.assertEqual(set(item.keys()), {"id", "persona", "severity", "screen", "title"})

    def test_extract_grouping_items_exact_parity_with_summarize_and_validate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rdir = Path(td) / "run-parity"
            _write_json(
                rdir / "clara" / "findings.json",
                {
                    "score": 6,
                    "issues": [
                        {"title": "Icon has no label", "severity": "minor", "screen": "Home", "screens": ["01"]},
                        {"title": "Save clipped", "severity": "major", "screen": "Settings", "screens": ["03"]},
                    ],
                },
            )
            _write_json(
                rdir / "sven" / "findings.json",
                {
                    "score": 5,
                    "issues": [
                        {"title": "Spara-knapp klippt", "severity": "blocker", "screen": "Settings", "screens": ["04"]},
                    ],
                },
            )

            # First pass: un-grouped summary (issues sorted by severity -> sven's id=3 comes first!)
            _, ungrouped_summary = summarize_run(rdir)
            self.assertEqual(ungrouped_summary["issues"][0]["reports"][0]["id"], 3)

            collected_items = collect_run_findings(rdir)["items"]
            extracted = extract_grouping_items(ungrouped_summary)

            self.assertEqual([x["id"] for x in extracted], [1, 2, 3])
            self.assertEqual(
                extracted,
                [
                    {k: item[k] for k in ("id", "persona", "severity", "screen", "title")}
                    for item in collected_items
                ],
            )

            # Build grouping prompt and feed groups back into validate_and_repair_groups & summarize_run
            prompt = build_grouping_prompt(extracted)
            self.assertIn("1. [clara, minor, Home] Icon has no label", prompt)
            self.assertIn("2. [clara, major, Settings] Save clipped", prompt)
            self.assertIn("3. [sven, blocker, Settings] Spara-knapp klippt", prompt)

            raw_groups = {
                "groups": [
                    {"title": "Save button clipped", "class": "layout and text size", "ids": [2, 3]},
                ]
            }
            repaired = validate_and_repair_groups(raw_groups, extracted)
            self.assertEqual(
                repaired,
                [
                    {"title": "Save button clipped", "class": "layout and text size", "ids": [2, 3]},
                    {"title": "Icon has no label", "class": "", "ids": [1]},
                ],
            )
            _, grouped_summary = summarize_run(rdir, groups_json=raw_groups)
            self.assertTrue(grouped_summary["grouped"])
            self.assertEqual(grouped_summary["distinct_issue_count"], 2)
            self.assertEqual(grouped_summary["issues"][0]["personas"], ["clara", "sven"])


class TestSummarizeRun(unittest.TestCase):
    def test_explore_mode_grouped_and_markdown_escaping(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rdir = Path(td) / "run-explore"
            _write_json(
                rdir / "manifest.json",
                {
                    "run_id": "run-explore",
                    "mode": "explore",
                    "personas": [
                        {"id": "clara", "locale": "fr-FR", "device": {"simulator": "iPhone SE (3rd generation)", "ios": 18, "text_size": "large"}},
                        {"id": "sven", "locale": "sv-SE", "device": {"simulator": "iPhone SE (3rd generation)", "ios": 18, "text_size": "accessibility-extra-extra-extra-large"}},
                    ],
                },
            )
            _write_json(
                rdir / "clara" / "findings.json",
                {
                    "score": 6,
                    "issues": [
                        {"title": "Save | Confirm\nclipped", "severity": "major", "screen": "Settings", "screens": ["03"]},
                        {"title": "Icon has no label", "severity": "minor", "screen": "Home", "screens": ["01"]},
                    ],
                },
            )
            _write_json(
                rdir / "sven" / "findings.json",
                {
                    "score": 8,
                    "issues": [
                        {"title": "Spara-knapp klippt", "severity": "blocker", "screen": "Settings", "screens": ["05", "06"]},
                    ],
                },
            )
            # Broken JSON file must be ignored cleanly
            (rdir / "broken").mkdir(parents=True)
            (rdir / "broken" / "findings.json").write_text("{broken json", encoding="utf-8")

            groups = {
                "groups": [
                    {"title": "Save button clipped | narrow\nscreen", "class": "layout and text size", "ids": [1, 3]},
                    {"title": "Unnamed icon", "class": "accessibility", "ids": [2]},
                ]
            }
            md, summary = summarize_run(rdir, groups_json=groups)

            self.assertTrue((rdir / "SUMMARY.md").is_file())
            self.assertTrue((rdir / "summary.json").is_file())
            self.assertTrue(summary["grouped"])
            self.assertEqual(summary["run_id"], "run-explore")
            self.assertEqual(summary["mode"], "explore")
            self.assertEqual(summary["persona_count"], 2)
            self.assertEqual(summary["report_count"], 3)
            self.assertEqual(summary["distinct_issue_count"], 2)
            self.assertEqual(summary["avg_score"], 7.0)
            self.assertEqual(
                summary["severity_counts"],
                {"blocker": 1, "major": 1, "minor": 1, "cosmetic": 0},
            )
            # First issue should be worst severity ("blocker") from merged group
            self.assertEqual(summary["issues"][0]["severity"], "blocker")
            self.assertEqual(summary["issues"][0]["personas"], ["clara", "sven"])
            self.assertEqual(len(summary["issues"][0]["reports"]), 2)
            self.assertEqual(summary["issues"][0]["reports"][1]["screens"], ["05", "06"])

            # Pipe & newline escaped in markdown table
            self.assertIn("Save button clipped \\| narrow screen", md)
            self.assertIn("## Which personas found each class of issue", md)
            self.assertIn("## Reports behind each grouped issue", md)

    def test_reproduce_mode_upstream_layout_and_zero_side_issues(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rdir = Path(td) / "run-repro"
            # Test upstream CI collected artifact layout: */results/<persona>/findings.json
            (rdir / "job-1" / "results").mkdir(parents=True)
            (rdir / "job-1" / "results" / "bug_report.md").write_text(
                "# Crash on export | CSV\n\nDetails...\n", encoding="utf-8"
            )
            _write_json(
                rdir / "job-1" / "results" / "amira" / "findings.json",
                {"reproduced": "no", "evidence": ["04"], "note": "Worked fine", "issues": []},
            )
            _write_json(
                rdir / "job-2" / "results" / "clara" / "findings.json",
                {"reproduced": "yes", "evidence": ["02", "03"], "note": "Crashed\nimmediately", "issues": []},
            )
            _write_json(
                rdir / "job-3" / "results" / "sven" / "findings.json",
                {"reproduced": "unclear", "evidence": ["05"], "note": "Button unreachable", "issues": []},
            )

            md, summary = summarize_run(rdir)
            self.assertEqual(summary["mode"], "reproduce")
            self.assertEqual(summary["bug_report_title"], "Crash on export | CSV")
            self.assertEqual(summary["reproduction"]["counts"], {"yes": 1, "unclear": 1, "no": 1})
            # Table order: yes -> unclear -> no
            self.assertEqual(
                [r["persona"] for r in summary["reproduction"]["rows"]],
                ["clara", "sven", "amira"],
            )
            self.assertIn("## Reproduction", md)
            self.assertIn("Bug: Crash on export \\| CSV", md)
            self.assertIn("## Everything else the agents reported", md)
            self.assertIn("Nothing else.", md)
            self.assertIn("Crashed immediately", md)

    def test_improve_mode_walkthrough_and_area_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rdir = Path(td) / "run-improve"
            _write_json(
                rdir / "manifest.json",
                {"run_id": "run-improve", "mode": "improve", "personas": []},
            )
            _write_json(
                rdir / "clara" / "findings.json",
                {
                    "score": 7,
                    "goal_reached": "yes",
                    "actions": 18,
                    "issues": [],
                    "improvements": [
                        {"title": "Clarify pricing", "area": "trust", "impact": "medium", "screen": "Paywall", "screens": ["03"], "why": "Unclear renewal"},
                        {"title": "Explain permission", "area": "onboarding", "impact": "high", "screen": "Intro", "screens": ["01"], "why": "Abrupt dialog"},
                    ],
                },
            )
            _write_json(
                rdir / "sven" / "findings.json",
                {
                    "score": 5,
                    "goal_reached": "partly",
                    "actions": 26,
                    "issues": [],
                    "improvements": [
                        {"title": "Bigger intro button", "area": "onboarding", "impact": "high", "screen": "Intro", "screens": ["02"], "why": "Clipped at 3XL"},
                        {"title": "Custom area item", "area": "weird-area", "impact": "low", "screen": "Home", "screens": [], "why": "Mapped to other"},
                    ],
                },
            )

            md, summary = summarize_run(rdir)
            walk = summary["walkthrough"]
            self.assertIsNotNone(walk)
            self.assertEqual(walk["goal_counts"], {"yes": 1, "partly": 1, "no": 0})
            self.assertEqual(walk["median_actions"], 22)
            # onboarding has 2 personas -> ranked first
            self.assertEqual(walk["areas"][0], {"area": "onboarding", "persona_count": 2, "count": 2})
            # unknown area mapped to "other"
            self.assertIn("other", [a["area"] for a in walk["areas"]])
            self.assertIn("## Walkthrough", md)
            self.assertIn("## Improvements", md)
            self.assertIn("No issues reported.", md)

    def test_external_cli_hook_via_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rdir = Path(td) / "run-cli-hook"
            _write_json(
                rdir / "clara" / "findings.json",
                {"score": 7, "issues": [{"title": "Cut label", "severity": "minor", "screen": "A"}]},
            )
            _write_json(
                rdir / "sven" / "findings.json",
                {"score": 6, "issues": [{"title": "Truncated label", "severity": "major", "screen": "A"}]},
            )
            completed = subprocess.CompletedProcess(
                args=["mock-llm"],
                returncode=0,
                stdout=json.dumps(
                    {"groups": [{"title": "Label truncated", "class": "layout and text size", "ids": [1, 2]}]}
                ),
                stderr="",
            )
            with mock.patch.dict("os.environ", {"SUMMARY_LLM_CMD": "mock-llm", "SUMMARY_GROUPING": "1"}):
                with mock.patch("subprocess.run", return_value=completed):
                    _, summary = summarize_run(rdir, use_external_llm=True)
            self.assertTrue(summary["grouped"])
            self.assertEqual(summary["distinct_issue_count"], 1)
            self.assertEqual(summary["issues"][0]["class"], "layout and text size")

            # SUMMARY_GROUPING=0 skips external CLI
            with mock.patch.dict("os.environ", {"SUMMARY_LLM_CMD": "mock-llm", "SUMMARY_GROUPING": "0"}):
                with mock.patch("subprocess.run") as run_mock:
                    _, summary2 = summarize_run(rdir, use_external_llm=True)
            run_mock.assert_not_called()
            self.assertFalse(summary2["grouped"])


if __name__ == "__main__":
    unittest.main()
