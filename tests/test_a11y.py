"""Unit tests for ios_agent_testers.core.a11y (upstream parity + structured overlays)."""

from __future__ import annotations

import unittest

from ios_agent_testers.core.a11y import (
    audit_ios,
    extract_elements,
    find_opaque_containers,
    format_elements_text,
    graft_elements_in_boxes,
    ios,
)


def _node(
    kind: str,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    label: str = "",
    title: str = "",
    value: str = "",
    uid: str = "",
    enabled: bool = True,
    children: list | None = None,
) -> dict:
    d: dict = {
        "type": kind,
        "frame": {"x": x, "y": y, "width": w, "height": h},
        "enabled": enabled,
    }
    if label:
        d["AXLabel"] = label
    if title:
        d["title"] = title
    if value:
        d["AXValue"] = value
    if uid:
        d["AXUniqueId"] = uid
    if children:
        d["children"] = children
    return d


class TestA11y(unittest.TestCase):
    def test_clean_tree_returns_none(self) -> None:
        tree = [
            _node(
                "Application",
                0,
                0,
                390,
                844,
                children=[
                    _node("Button", 20, 100, 120, 48, label="Continue"),
                    _node("Switch", 300, 200, 51, 31, label="Dark mode", value="1"),
                ],
            )
        ]
        res = audit_ios(tree, 390, 844)
        self.assertIsNone(res.summary_line)
        self.assertIsNone(ios(tree, 390, 844))
        self.assertEqual(res.unnamed, [])
        self.assertEqual(res.undersized, [])

    def test_switch_with_axvalue_only_is_unnamed_and_size_exempt(self) -> None:
        # Upstream critical case: AXValue="1" is NOT a label, and Switch is SIZE_EXEMPT (<44pt height ignored)
        tree = [_node("Switch", 300, 200, 51, 31, label="", value="1")]
        res = audit_ios(tree, 390, 844)
        self.assertEqual(len(res.unnamed), 1)
        self.assertEqual(res.unnamed[0].kind, "Switch")
        self.assertEqual((res.unnamed[0].cx, res.unnamed[0].cy), (326, 216))
        self.assertEqual(res.undersized, [])
        self.assertEqual(
            res.summary_line,
            "accessibility check (confirm on the screenshot): 1 control with no name at (326,216)",
        )

    def test_size_exempt_cell_and_slider(self) -> None:
        tree = [
            _node("Cell", 10, 100, 370, 32, label="Row 1"),
            _node("Slider", 20, 200, 200, 28, label="Volume", value="50%"),
        ]
        res = audit_ios(tree, 390, 844)
        self.assertIsNone(res.summary_line)

    def test_recursive_label_from_descendant(self) -> None:
        tree = [
            _node(
                "Button",
                50,
                200,
                32,
                32,
                children=[_node("StaticText", 55, 205, 20, 20, label="Info")],
            )
        ]
        res = audit_ios(tree, 390, 844)
        # Named via child -> NOT unnamed, but 32x32 IS undersized with name "Info"
        self.assertEqual(res.unnamed, [])
        self.assertEqual(len(res.undersized), 1)
        self.assertEqual(res.undersized[0].name, "Info")
        self.assertEqual(
            res.summary_line,
            'accessibility check (confirm on the screenshot): 1 touch target under 44 pt: "Info" 32x32 pt at (66,216)',
        )

    def test_big_container_exclusion_over_40_percent(self) -> None:
        # 390 * 844 = 329,160; 40% = 131,664. A 350x500=175,000 unnamed Button is skipped as backdrop.
        tree = [_node("Button", 20, 100, 350, 500, label="")]
        res = audit_ios(tree, 390, 844)
        self.assertEqual(res.unnamed, [])
        self.assertEqual(res.undersized, [])
        self.assertIsNone(res.summary_line)

    def test_disabled_control_skipped(self) -> None:
        tree = [_node("Button", 50, 100, 24, 24, label="", enabled=False)]
        res = audit_ios(tree, 390, 844)
        self.assertIsNone(res.summary_line)

    def test_keyboard_subtree_pruned(self) -> None:
        tree = [
            _node(
                "Keyboard",
                0,
                550,
                390,
                294,
                children=[
                    _node("Key", 10, 560, 30, 38, label="q"),
                    _node("Button", 50, 600, 28, 28, label=""),
                ],
            )
        ]
        res = audit_ios(tree, 390, 844)
        self.assertIsNone(res.summary_line)

    def test_edge_clipping_scroll_container_and_screen(self) -> None:
        tree = [
            _node(
                "ScrollView",
                0,
                100,
                390,
                500,
                children=[
                    # Clipped at top of ScrollView (y1=100 <= fy1=100, hd=28 <= wd=120) -> NOT flagged
                    _node("Button", 20, 100, 120, 28, label="TopClipped"),
                    # Clipped at bottom of ScrollView (y2=605 >= fy2=600) -> NOT flagged
                    _node("Button", 20, 575, 120, 30, label="BottomClipped"),
                    # Strictly inside ScrollView -> IS flagged
                    _node("Button", 20, 250, 120, 30, label="TrulySmall"),
                ],
            ),
            # Narrow horizontally (wd=30 < hd=80), clipped at left screen edge x1=0 -> NOT flagged
            _node("Button", 0, 650, 30, 80, label="LeftEdgeClipped"),
            # Narrow horizontally, strictly inside screen -> IS flagged
            _node("Button", 50, 650, 30, 80, label="NarrowMid"),
        ]
        res = audit_ios(tree, 390, 844)
        self.assertEqual([f.name for f in res.undersized], ["TrulySmall", "NarrowMid"])

    def test_max_listed_truncation(self) -> None:
        children = [
            _node("Button", 20, 100 + i * 60, 30, 30, label="") for i in range(6)
        ]
        res = audit_ios(children, 390, 844)
        self.assertEqual(len(res.unnamed), 6)
        self.assertEqual(len(res.undersized), 6)
        assert res.summary_line is not None
        self.assertIn("6 controls with no name at", res.summary_line)
        self.assertIn("and 2 more", res.summary_line)
        self.assertIn("6 touch targets under 44 pt:", res.summary_line)

    def test_fb_idb_axframe_and_axrole_compatibility(self) -> None:
        # fb-idb flat/nested format using AXFrame string and AXButton role
        fb_tree = [
            {
                "role": "AXButton",
                "AXLabel": "Close",
                "AXUniqueId": "close_btn",
                "AXFrame": "{{320, 60}, {32, 32}}",
                "enabled": True,
            },
            {
                "role": "AXButton",
                "AXLabel": None,
                "AXFrame": "{{20, 60}, {48, 48}}",
                "enabled": True,
            },
        ]
        res = audit_ios(fb_tree, 390, 844)
        self.assertEqual(len(res.unnamed), 1)
        self.assertEqual(len(res.undersized), 1)
        self.assertEqual(res.undersized[0].name, "Close")
        d = res.to_dict()
        self.assertEqual(d["undersized"][0]["x"], 320.0)
        self.assertEqual(d["undersized"][0]["width"], 32.0)

    def test_extract_elements_and_format(self) -> None:
        tree = [
            _node(
                "Application",
                0,
                0,
                390,
                844,
                children=[
                    _node("StaticText", 20, 50, 200, 30, label="Sign In"),
                    _node(
                        "TextField",
                        20,
                        120,
                        350,
                        44,
                        label="Email",
                        value="clara@example.com",
                        uid="email_input",
                    ),
                    _node(
                        "Button",
                        20,
                        200,
                        350,
                        50,
                        uid="submit_btn",
                        children=[_node("StaticText", 20, 200, 350, 50, label="Submit")],
                    ),
                    # Offscreen element should be excluded
                    _node("Button", 20, 1200, 100, 44, label="Offscreen"),
                ],
            )
        ]
        els = extract_elements(tree, 390, 844)
        self.assertEqual(len(els), 3)
        self.assertEqual([e["ref"] for e in els], ["e1", "e2", "e3"])
        self.assertEqual([e["kind"] for e in els], ["text", "field", "button"])
        self.assertEqual(els[1]["identifier"], "email_input")
        self.assertEqual(els[2]["label"], "Submit")

        txt = format_elements_text(els)
        self.assertIn('[e1] text "Sign In"', txt)
        self.assertIn('[e2] field "Email" id="email_input" value="clara@example.com"', txt)
        self.assertIn('[e3] button "Submit" id="submit_btn" centre=(195,225)', txt)

    def test_role_precedence_over_localized_role_description(self) -> None:
        # Localized role_description ("bouton", "按鈕") must NOT override role="AXButton"
        tree = [
            {
                "role": "AXButton",
                "role_description": "bouton",
                "AXLabel": "Continuer",
                "frame": {"x": 20, "y": 100, "width": 32, "height": 32},
                "enabled": True,
            },
            {
                "role": "AXButton",
                "role_description": "按鈕",
                "AXLabel": "",
                "frame": {"x": 100, "y": 100, "width": 48, "height": 48},
                "enabled": True,
            },
        ]
        els = extract_elements(tree, 390, 844)
        self.assertEqual([e["kind"] for e in els], ["button", "button"])
        self.assertEqual([e["role"] for e in els], ["Button", "Button"])
        res = audit_ios(tree, 390, 844)
        self.assertEqual(len(res.undersized), 1)
        self.assertEqual(res.undersized[0].name, "Continuer")
        self.assertEqual(len(res.unnamed), 1)

    def test_checkbox_switch_normalization_and_size_exempt(self) -> None:
        # Under iOS 27 AX, switches report type="CheckBox", role="AXCheckBox", role_description="switch"
        tree = [
            {
                "type": "CheckBox",
                "role": "AXCheckBox",
                "role_description": "switch",
                "AXLabel": "Auto-Correction",
                "AXValue": "0",
                "frame": {"x": 20, "y": 120, "width": 350, "height": 31},
                "enabled": True,
            },
            {
                "type": "CheckBox",
                "role": "AXCheckBox",
                "role_description": "開關",
                "AXLabel": "深色模式",
                "AXValue": "1",
                "frame": {"x": 20, "y": 180, "width": 51, "height": 31},
                "enabled": True,
            },
            # COMPLETE format omits `role` and provides type="CheckBox" + role_description="switch"
            {
                "type": "CheckBox",
                "role_description": "switch",
                "label": "Spell Check",
                "frame": {"x": 20, "y": 240, "width": 51, "height": 31},
                "enabled": True,
            },
        ]
        els = extract_elements(tree, 390, 844)
        self.assertEqual(len(els), 3)
        self.assertTrue(all(e["kind"] == "button" for e in els))
        self.assertTrue(all(e["role"] == "Switch" for e in els))

        # Switch is SIZE_EXEMPT -> 31pt height yields 0 undersized and 0 unnamed
        res = audit_ios(tree, 390, 844)
        self.assertEqual(res.unnamed, [])
        self.assertEqual(res.undersized, [])
        self.assertIsNone(res.summary_line)

    def test_popup_radio_back_button_kinds(self) -> None:
        tree = [
            {
                "type": "PopUpButton",
                "role": "AXPopUpButton",
                "AXLabel": "光環特效色盤",
                "frame": {"x": 20, "y": 300, "width": 350, "height": 44},
            },
            {
                "role": "AXRadioButton",
                "AXLabel": "Dark",
                "frame": {"x": 32, "y": 170, "width": 112, "height": 32},
            },
            {
                "role": "AXBackButton",
                "AXLabel": "Settings",
                "frame": {"x": 16, "y": 62, "width": 44, "height": 44},
            },
        ]
        els = extract_elements(tree, 402, 874)
        self.assertEqual([e["kind"] for e in els], ["button", "button", "button"])
        self.assertEqual(
            [e["role"] for e in els], ["PopUpButton", "RadioButton", "BackButton"]
        )

    def test_axtitle_label_and_axenabled_fallback(self) -> None:
        tree = [
            {
                "role": "AXButton",
                "AXTitle": "Save Changes",
                "frame": {"x": 20, "y": 100, "width": 120, "height": 48},
                "AXEnabled": True,
            },
            {
                "role": "AXButton",
                "AXLabel": "",
                "frame": {"x": 20, "y": 200, "width": 24, "height": 24},
                "AXEnabled": False,
            },
            # AXValue must remain strictly excluded from label (still unnamed)
            {
                "role": "AXCheckBox",
                "AXValue": "1",
                "frame": {"x": 300, "y": 300, "width": 51, "height": 31},
                "AXEnabled": True,
            },
        ]
        els = extract_elements(tree, 390, 844)
        self.assertEqual(els[0]["label"], "Save Changes")
        self.assertTrue(els[0]["enabled"])
        self.assertFalse(els[1]["enabled"])

        res = audit_ios(tree, 390, 844)
        # Disabled 24x24 button skipped; AXTitle button named & >=44pt; AXValue-only switch unnamed & size-exempt
        self.assertEqual(len(res.unnamed), 1)
        self.assertEqual(res.unnamed[0].kind, "Switch")
        self.assertEqual(res.undersized, [])

    def test_complete_wrapper_dict_unwrapped(self) -> None:
        complete_payload = {
            "backend": "ax",
            "coverage": 0.95,
            "elements": [
                {
                    "type": "Button",
                    "label": "Done",
                    "frame": {"x": 300, "y": 60, "width": 60, "height": 44},
                    "enabled": True,
                },
                {
                    "type": "TabGroup",
                    "label": "",
                    "frame": {"x": 32, "y": 171, "width": 338, "height": 31},
                    "enabled": True,
                },
            ],
        }
        els = extract_elements(complete_payload, 402, 874)
        self.assertEqual(len(els), 1)
        self.assertEqual(els[0]["label"], "Done")
        self.assertIsNone(audit_ios(complete_payload, 402, 874).summary_line)
        boxes = find_opaque_containers(complete_payload, 402, 874)
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0]["kind"], "TabGroup")

    def test_find_opaque_containers_and_graft_elements_in_boxes(self) -> None:
        # Realistic DayTrace Settings -> Appearance AX tree (childless unlabeled TabGroup)
        ax_tree = [
            _node(
                "Application",
                0,
                0,
                402,
                874,
                children=[
                    _node("Button", 16, 62, 44, 44, label="設定", uid="BackButton"),
                    _node("StaticText", 160, 72, 80, 24, label="外觀"),
                    # Opaque childless unlabeled TabGroup -> MUST be detected
                    {
                        "type": "TabGroup",
                        "role": "AXTabGroup",
                        "role_description": "tab group",
                        "AXLabel": "",
                        "frame": {"x": 32, "y": 171.33, "width": 338, "height": 31},
                        "enabled": True,
                    },
                    # Non-opaque TabGroup (has children) -> must NOT be flagged
                    _node(
                        "TabGroup",
                        0,
                        800,
                        402,
                        50,
                        children=[_node("Tab", 20, 805, 80, 44, label="Home")],
                    ),
                ],
            )
        ]

        # Realistic AXBRIDGE_PERSISTENT tree containing segments + noisy/occluded elements outside box
        bridge_tree = [
            _node(
                "Application",
                0,
                0,
                402,
                874,
                children=[
                    # Noisy occluded/undersized buttons outside the TabGroup box (must be excluded!)
                    _node("Button", 50, 500, 12, 12, label=""),
                    _node("Button", 150, 700, 120, 44, label="開始錄音"),
                    # Duplicate BackButton already in base_elements
                    _node("Button", 16, 62, 44, 44, label="設定", uid="BackButton"),
                    # SegmentedControl matching TabGroup frame with 3 real SwiftUI segment buttons
                    _node(
                        "SegmentedControl",
                        32,
                        171.33,
                        338,
                        31,
                        children=[
                            _node(
                                "Button",
                                32,
                                171.33,
                                112,
                                32,
                                label="深色",
                                children=[_node("StaticText", 56, 178, 64, 18, label="深色")],
                            ),
                            _node("Button", 144, 171.33, 113, 32, label="淺色"),
                            _node("Button", 257, 171.33, 113, 32, label="跟隨系統"),
                        ],
                    ),
                ],
            )
        ]

        base_els = extract_elements(ax_tree, 402, 874)
        boxes = find_opaque_containers(ax_tree, 402, 874)
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0]["kind"], "TabGroup")
        self.assertEqual(boxes[0]["x"], 32.0)

        grafted = graft_elements_in_boxes(base_els, bridge_tree, boxes, 402, 874)
        labels = [e["label"] for e in grafted]
        self.assertIn("深色", labels)
        self.assertIn("淺色", labels)
        self.assertIn("跟隨系統", labels)
        # Inner duplicate StaticText "深色" and unlabeled SegmentedControl wrapper skipped
        self.assertEqual(labels.count("深色"), 1)
        self.assertNotIn("", labels)
        # Occluded element outside box MUST NOT leak in
        self.assertNotIn("開始錄音", labels)
        # Sequential e1..eN numbering preserved
        self.assertEqual([e["ref"] for e in grafted], [f"e{i}" for i in range(1, len(grafted) + 1)])

        # audit_ios strictly on ax_tree has 0 false positives (unlike bridge_tree!)
        ax_audit = audit_ios(ax_tree, 402, 874)
        self.assertEqual(ax_audit.unnamed, [])
        self.assertEqual(ax_audit.undersized, [])
        bridge_audit = audit_ios(bridge_tree, 402, 874)
        self.assertGreater(len(bridge_audit.unnamed) + len(bridge_audit.undersized), 0)


if __name__ == "__main__":
    unittest.main()
