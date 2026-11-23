"""Unit tests for A/B QA run comparator (COMPARE.md + compare.json)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ios_agent_testers.core.compare import compare_runs
from ios_agent_testers.core.summarize import summarize_run


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class TestCompareRuns(unittest.TestCase):
    def test_compare_raw_runs_exact_schema_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dir_a = Path(td) / "run-a"
            dir_b = Path(td) / "run-b"

            _write_json(
                dir_a / "clara" / "findings.json",
                {
                    "score": 6,
                    "goal_reached": "partly",
                    "actions": 24,
                    "issues": [
                        {"title": "Crash on Save", "severity": "blocker"},
                        {"title": "Minor | spacing", "severity": "minor"},
                    ],
                    "improvements": [
                        {"title": "Add spacing", "area": "layout", "impact": "medium", "why": "Tight"}
                    ],
                },
            )
            _write_json(
                dir_a / "sven" / "findings.json",
                {
                    "score": 5,
                    "goal_reached": "no",
                    "actions": 30,
                    "issues": [{"title": "Crash on Save", "severity": "major"}],
                    "improvements": [
                        {"title": "Larger buttons", "area": "layout", "impact": "high", "why": "Clipped"}
                    ],
                },
            )
            _write_json(
                dir_a / "yuki" / "findings.json",
                {"score": 8, "issues": []},
            )

            _write_json(
                dir_b / "clara" / "findings.json",
                {
                    "score": 8,
                    "goal_reached": "yes",
                    "actions": 16,
                    "issues": [
                        {"title": "minor | spacing", "severity": "minor"},
                        {"title": "New dark mode glitch", "severity": "major"},
                    ],
                    "improvements": [
                        {"title": "Better onboarding copy", "area": "copy", "impact": "low", "why": "Vague"}
                    ],
                },
            )
            _write_json(
                dir_b / "sven" / "findings.json",
                {
                    "score": 5,
                    "goal_reached": "partly",
                    "actions": 20,
                    "issues": [],
                    "improvements": [],
                },
            )
            _write_json(
                dir_b / "amira" / "findings.json",
                {"score": 9, "issues": []},
            )

            md, comp = compare_runs(dir_a, dir_b, name_a="v1.0", name_b="v1.1")

            self.assertTrue((dir_b / "COMPARE.md").is_file())
            self.assertTrue((dir_b / "compare.json").is_file())

            self.assertEqual(comp["name_a"], "v1.0")
            self.assertEqual(comp["name_b"], "v1.1")
            self.assertEqual(comp["overview"]["personas"], [3, 3])
            self.assertEqual(
                comp["overview"]["goal_reached"],
                [{"yes": 0, "partly": 1, "no": 1}, {"yes": 1, "partly": 1, "no": 0}],
            )
            self.assertEqual(comp["overview"]["median_actions"], [27, 18])
            self.assertEqual(comp["overview"]["avg_score"], [6.3, 7.3])
            self.assertEqual(
                comp["overview"]["issues"],
                [
                    {"blocker": 1, "major": 1, "minor": 1, "cosmetic": 0},
                    {"blocker": 0, "major": 1, "minor": 1, "cosmetic": 0},
                ],
            )

            # Personas intersection = clara, sven
            self.assertEqual([p["persona"] for p in comp["personas"]], ["clara", "sven"])
            self.assertEqual(comp["personas"][0]["delta"], 2)
            self.assertEqual(comp["personas"][1]["delta"], 0)
            self.assertEqual(comp["only_a_personas"], ["yuki"])
            self.assertEqual(comp["only_b_personas"], ["amira"])
            self.assertEqual(comp["score_summary"], {"higher": 1, "lower": 0, "same": 1})

            # "Minor | spacing" matches case-insensitively across A and B, so excluded from both
            self.assertEqual(
                comp["only_in_a"],
                [{"title": "Crash on Save", "severity": "blocker", "personas": ["clara", "sven"]}],
            )
            self.assertEqual(
                comp["only_in_b"],
                [{"title": "New dark mode glitch", "severity": "major", "personas": ["clara"]}],
            )

            # Improvements by area sorted by -(count_a + count_b), area
            self.assertEqual(
                comp["improvements_by_area"],
                [
                    {"area": "layout", "count_a": 2, "count_b": 0},
                    {"area": "copy", "count_a": 0, "count_b": 1},
                ],
            )

            # Markdown checks
            self.assertIn("# v1.0 vs v1.1", md)
            self.assertIn("## Persona by persona", md)
            self.assertIn("Only on one version, so not in the persona table: amira, yuki.", md)
            self.assertIn("v1.1 scored higher than v1.0 for 1 of 2 personas, lower for 0, the same for 1.", md)
            self.assertIn("## Issues seen only on v1.0", md)
            self.assertIn("- blocker: Crash on Save (clara, sven)", md)
            self.assertIn("## Issues seen only on v1.1", md)
            self.assertIn("- major: New dark mode glitch (clara)", md)
            self.assertIn("## Improvements suggested, by area", md)

    def test_compare_caps_only_in_list_at_20(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dir_a = Path(td) / "run-a"
            dir_b = Path(td) / "run-b"
            _write_json(
                dir_a / "clara" / "findings.json",
                {
                    "score": 5,
                    "issues": [
                        {"title": f"Issue {i:02d}", "severity": "minor"} for i in range(25)
                    ],
                },
            )
            _write_json(dir_b / "clara" / "findings.json", {"score": 9, "issues": []})

            md, comp = compare_runs(dir_a, dir_b)
            self.assertEqual(len(comp["only_in_a"]), 25)
            self.assertEqual(comp["only_in_b"], [])
            self.assertIn("- and 5 more", md)
            self.assertIn("## Issues seen only on B\n\nNone.", md)

    def test_compare_uses_semantic_groups_when_both_summaries_grouped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dir_a = Path(td) / "run-a"
            dir_b = Path(td) / "run-b"

            _write_json(
                dir_a / "clara" / "findings.json",
                {
                    "score": 6,
                    "issues": [
                        {"title": "Bouton Envoyer coupé", "severity": "major", "screen": "Form"},
                        {"title": "Crash à l'ouverture", "severity": "blocker", "screen": "Launch"},
                    ],
                },
            )
            _write_json(
                dir_b / "sven" / "findings.json",
                {
                    "score": 8,
                    "issues": [
                        {"title": "Skicka-knapp klippt", "severity": "major", "screen": "Form"},
                        {"title": "Ny ikon saknar etikett", "severity": "minor", "screen": "Home"},
                    ],
                },
            )

            summarize_run(
                dir_a,
                groups_json={
                    "groups": [
                        {"title": "Submit button clipped", "class": "layout and text size", "ids": [1]},
                        {"title": "Crash on launch", "class": "crash", "ids": [2]},
                    ]
                },
            )
            summarize_run(
                dir_b,
                groups_json={
                    "groups": [
                        {"title": "Submit button clipped", "class": "layout and text size", "ids": [1]},
                        {"title": "Home icon missing label", "class": "accessibility", "ids": [2]},
                    ]
                },
            )

            md, comp = compare_runs(dir_a, dir_b, name_a="Before", name_b="After")
            # "Submit button clipped" matched semantically across French and Swedish!
            self.assertEqual(
                comp["only_in_a"],
                [{"title": "Crash on launch", "severity": "blocker", "personas": ["clara"]}],
            )
            self.assertEqual(
                comp["only_in_b"],
                [{"title": "Home icon missing label", "severity": "minor", "personas": ["sven"]}],
            )
            self.assertIn("semantic issue groups from summary.json", md)


if __name__ == "__main__":
    unittest.main()
