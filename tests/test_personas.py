"""Unit tests for ios_agent_testers.core.personas and bundled personas.json."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from ios_agent_testers.core.personas import (
    DEFAULT_PERSONAS_PATH,
    PRESETS,
    VALID_TEXT_SIZES,
    get_persona,
    load_personas,
    select_personas,
    validate_personas,
)


class TestPersonas(unittest.TestCase):
    def test_bundled_personas_valid_and_complete(self) -> None:
        self.assertTrue(DEFAULT_PERSONAS_PATH.is_file())
        personas = load_personas()
        self.assertEqual(len(personas), 10)
        self.assertEqual(validate_personas(personas), [])

        ids = [p["id"] for p in personas]
        self.assertEqual(
            ids,
            [
                "clara",
                "jonas",
                "noah",
                "yuki",
                "amira",
                "mateo",
                "sven",
                "giulia",
                "minjun",
                "wei",
            ],
        )

        # Spot-check key persona properties (RTL Arabic, max accessibility text, iPad Pro)
        by_id = {p["id"]: p for p in personas}
        self.assertEqual(by_id["amira"]["locale"], "ar-SA")
        self.assertEqual(by_id["amira"]["device"]["simulator"], "iPhone 16 Pro")
        self.assertEqual(
            by_id["sven"]["device"]["text_size"],
            "accessibility-extra-extra-extra-large",
        )
        self.assertEqual(by_id["sven"]["device"]["theme"], "dark")
        self.assertEqual(by_id["minjun"]["device"]["simulator"], "iPad Pro 13-inch (M4)")
        self.assertEqual(by_id["wei"]["locale"], "zh-CN")
        self.assertEqual(by_id["wei"]["device"]["simulator"], "iPhone 16e")

    def test_valid_text_sizes_count(self) -> None:
        self.assertEqual(len(VALID_TEXT_SIZES), 12)
        self.assertIn("extra-small", VALID_TEXT_SIZES)
        self.assertIn("accessibility-extra-extra-extra-large", VALID_TEXT_SIZES)

    def test_presets_selection(self) -> None:
        for preset_name, expected_ids in PRESETS.items():
            selected = select_personas(preset_name)
            self.assertEqual([p["id"] for p in selected], expected_ids, msg=preset_name)

    def test_csv_selection_and_dedup(self) -> None:
        selected = select_personas("sven, clara, sven, amira")
        self.assertEqual([p["id"] for p in selected], ["sven", "clara", "amira"])

    def test_unknown_selector_raises(self) -> None:
        with self.assertRaises(ValueError):
            select_personas("clara,nonexistent_persona")

    def test_get_persona(self) -> None:
        clara = get_persona("clara")
        self.assertEqual(clara["locale"], "fr-FR")
        self.assertEqual(clara["device"]["simulator"], "iPhone SE (3rd generation)")
        with self.assertRaises(ValueError):
            get_persona("nobody")

    def test_validation_catches_invariants(self) -> None:
        base = load_personas()[0]

        # Empty list
        self.assertTrue(validate_personas([]))

        # Invalid id format
        bad = copy.deepcopy(base)
        bad["id"] = "Clara!"
        self.assertTrue(any("id" in e for e in validate_personas([bad])))

        # Duplicate ids
        dup1 = copy.deepcopy(base)
        dup2 = copy.deepcopy(base)
        self.assertTrue(any("duplicate" in e for e in validate_personas([dup1, dup2])))

        # Wrong platform
        bad = copy.deepcopy(base)
        bad["platform"] = "android"
        self.assertTrue(any("platform" in e for e in validate_personas([bad])))

        # Bad locale
        bad = copy.deepcopy(base)
        bad["locale"] = "FR_fr"
        self.assertTrue(any("locale" in e for e in validate_personas([bad])))

        # Geo out of range & bool
        bad = copy.deepcopy(base)
        bad["geo"] = [95.0, 0.0]
        self.assertTrue(any("latitude" in e for e in validate_personas([bad])))

        bad = copy.deepcopy(base)
        bad["geo"] = [0.0, -181.0]
        self.assertTrue(any("longitude" in e for e in validate_personas([bad])))

        bad = copy.deepcopy(base)
        bad["geo"] = [True, 0.0]
        self.assertTrue(any("geo" in e for e in validate_personas([bad])))

        # Invalid simulator prefix
        bad = copy.deepcopy(base)
        bad["device"]["simulator"] = "Pixel 9"
        self.assertTrue(any("simulator" in e for e in validate_personas([bad])))

        # Bool ios version
        bad = copy.deepcopy(base)
        bad["device"]["ios"] = True
        self.assertTrue(any("ios" in e for e in validate_personas([bad])))

        # Invalid theme
        bad = copy.deepcopy(base)
        bad["device"]["theme"] = "sepia"
        self.assertTrue(any("theme" in e for e in validate_personas([bad])))

        # Invalid text_size
        bad = copy.deepcopy(base)
        bad["device"]["text_size"] = "huge"
        self.assertTrue(any("text_size" in e for e in validate_personas([bad])))

        # Invalid optional email
        bad = copy.deepcopy(base)
        bad["email"] = "not-an-email"
        self.assertTrue(any("email" in e for e in validate_personas([bad])))

        # Valid optional email passes
        good = copy.deepcopy(base)
        good["email"] = "qa+clara@example.com"
        self.assertEqual(validate_personas([good]), [])

    def test_custom_personas_file_and_family_presets(self) -> None:
        custom = [
            {
                "id": "custom_phone",
                "platform": "ios",
                "profile": "Custom phone tester",
                "goal": "Test login flow",
                "locale": "en-US",
                "geo": [37.7749, -122.4194],
                "device": {
                    "simulator": "iPhone 16",
                    "ios": 18,
                    "text_size": "large",
                    "theme": "light",
                },
            },
            {
                "id": "custom_tablet",
                "platform": "ios",
                "profile": "Custom iPad tester",
                "goal": "Test split view",
                "locale": "ja-JP",
                "geo": [35.6762, 139.6503],
                "device": {
                    "simulator": "iPad Pro 13-inch (M4)",
                    "ios": 18,
                    "text_size": "accessibility-extra-large",
                    "theme": "dark",
                },
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "custom_personas.json"
            p.write_text(json.dumps(custom), encoding="utf-8")

            self.assertEqual(len(load_personas(p)), 2)
            self.assertEqual(
                [x["id"] for x in select_personas("all", custom_path=p)],
                ["custom_phone", "custom_tablet"],
            )
            self.assertEqual(
                [x["id"] for x in select_personas("iphone", custom_path=p)],
                ["custom_phone"],
            )
            self.assertEqual(
                [x["id"] for x in select_personas("ipad", custom_path=p)],
                ["custom_tablet"],
            )
            self.assertEqual(
                [x["id"] for x in select_personas("a11y", custom_path=p)],
                ["custom_tablet"],
            )


if __name__ == "__main__":
    unittest.main()
