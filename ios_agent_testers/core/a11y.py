"""Deterministic iOS accessibility auditor and element extractor.

Maintains 100% behavioral parity with mobile-agent-testers' `scripts/a11y.py`
while supporting both `fb-idb` nested AX JSON and `axe` JSON, and emitting
structured bounding-box findings for the QA Dashboard overlay.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

ACTIVE: tuple[str, ...] = (
    "Button",
    "Link",
    "Tab",
    "Switch",
    "Slider",
    "Cell",
    "MenuItem",
    "Stepper",
    "SegmentedControl",
)

SCROLLING: tuple[str, ...] = (
    "ScrollView",
    "Table",
    "CollectionView",
    "List",
    "WebView",
)

SIZE_EXEMPT: tuple[str, ...] = (
    "Switch",
    "Cell",
    "Slider",
)

MAX_LISTED = 4
MIN_PT = 44
BIG_AREA_RATIO = 0.4

_AX_FRAME_RE = re.compile(
    r"\{\{\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\}\s*,\s*"
    r"\{\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\}\}"
)

_AX_ROLE_MAP: dict[str, str] = {
    "AXButton": "Button",
    "AXBackButton": "BackButton",
    "BackButton": "BackButton",
    "AXPopUpButton": "PopUpButton",
    "PopUpButton": "PopUpButton",
    "AXRadioButton": "RadioButton",
    "RadioButton": "RadioButton",
    "AXLink": "Link",
    "AXTab": "Tab",
    "AXTabGroup": "TabGroup",
    "TabGroup": "TabGroup",
    "AXSwitch": "Switch",
    "AXCheckBox": "Switch",
    "AXSlider": "Slider",
    "AXCell": "Cell",
    "AXMenuItem": "MenuItem",
    "AXStepper": "Stepper",
    "AXSegmentedControl": "SegmentedControl",
    "AXScrollView": "ScrollView",
    "AXScrollArea": "ScrollView",
    "AXTable": "Table",
    "AXCollectionView": "CollectionView",
    "AXList": "List",
    "AXWebView": "WebView",
    "AXWebArea": "WebView",
    "AXKeyboard": "Keyboard",
    "AXKey": "Key",
    "AXStaticText": "StaticText",
    "AXTextField": "TextField",
    "AXSecureTextField": "SecureTextField",
    "AXSearchField": "SearchField",
    "AXTextArea": "TextArea",
    "AXApplication": "Application",
    "AXWindow": "Window",
    "AXScrollBar": "ScrollBar",
}

_SKIP_ELEMENT_KINDS = frozenset({"Application", "Window", "ScrollBar"})
_OPAQUE_CONTAINER_KINDS = frozenset({"TabGroup", "SegmentedControl"})
_BUTTON_ELEMENT_KINDS = frozenset(
    {
        "Button",
        "BackButton",
        "PopUpButton",
        "RadioButton",
        "Link",
        "Switch",
        "Cell",
        "Tab",
        "Slider",
        "Stepper",
        "SegmentedControl",
        "MenuItem",
    }
)
_RECURSIVE_LABEL_KINDS = frozenset(
    {
        "Button",
        "BackButton",
        "PopUpButton",
        "RadioButton",
        "Link",
        "Tab",
        "Switch",
        "MenuItem",
        "Stepper",
    }
)


@dataclass
class A11yFinding:
    category: str  # "unnamed" | "undersized"
    kind: str
    name: str
    x: float
    y: float
    width: float
    height: float
    cx: int
    cy: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "kind": self.kind,
            "name": self.name,
            "x": round(float(self.x), 2),
            "y": round(float(self.y), 2),
            "width": round(float(self.width), 2),
            "height": round(float(self.height), 2),
            "cx": int(self.cx),
            "cy": int(self.cy),
        }


@dataclass
class A11yAuditResult:
    summary_line: str | None
    unnamed: list[A11yFinding]
    undersized: list[A11yFinding]

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary_line": self.summary_line,
            "unnamed": [f.to_dict() for f in self.unnamed],
            "undersized": [f.to_dict() for f in self.undersized],
        }


def _clean_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    return str(val).strip()


def _ios_kind(node: dict[str, Any]) -> str:
    """Resolve canonical iOS AX kind: `type` -> `role` -> `role_description` (last)."""
    raw_type = _clean_str(node.get("type"))
    role = _clean_str(node.get("role"))
    role_desc = _clean_str(node.get("role_description"))

    if raw_type:
        if raw_type == "CheckBox" and (
            role_desc.lower() == "switch" or role == "AXCheckBox"
        ):
            return "Switch"
        if raw_type in _AX_ROLE_MAP:
            return _AX_ROLE_MAP[raw_type]
        return raw_type

    if role:
        if role in _AX_ROLE_MAP:
            return _AX_ROLE_MAP[role]
        if role.startswith("AX") and len(role) > 2:
            return role[2:]
        return role

    if role_desc:
        return role_desc
    return ""


def _is_enabled(node: dict[str, Any]) -> bool:
    if "enabled" in node and node["enabled"] is not None:
        return node["enabled"] is not False
    if "AXEnabled" in node and node["AXEnabled"] is not None:
        return node["AXEnabled"] is not False
    return True


def _direct_label(node: dict[str, Any]) -> str:
    """Return node's own accessibility label/title, intentionally excluding AXValue."""
    for key in ("AXLabel", "AXTitle", "title", "label"):
        text = _clean_str(node.get(key))
        if text:
            return text
    return ""


def _ios_name(node: dict[str, Any]) -> str:
    """Recursively look up accessibility name across descendants (excluding AXValue)."""
    direct = _direct_label(node)
    if direct:
        return direct
    for child in node.get("children") or []:
        if isinstance(child, dict):
            found = _ios_name(child)
            if found:
                return found
    return ""


def _ios_frame(node: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """Parse node frame into (x1, y1, x2, y2) logical points, or None if invalid/empty."""
    frame = node.get("frame")
    if isinstance(frame, dict):
        if "width" in frame and "height" in frame:
            try:
                x = float(frame.get("x", 0.0))
                y = float(frame.get("y", 0.0))
                w = float(frame["width"])
                h = float(frame["height"])
                if w > 0 and h > 0:
                    return (x, y, x + w, y + h)
            except (TypeError, ValueError):
                pass
        elif isinstance(frame.get("origin"), dict) and isinstance(frame.get("size"), dict):
            try:
                x = float(frame["origin"].get("x", 0.0))
                y = float(frame["origin"].get("y", 0.0))
                w = float(frame["size"].get("width", 0.0))
                h = float(frame["size"].get("height", 0.0))
                if w > 0 and h > 0:
                    return (x, y, x + w, y + h)
            except (TypeError, ValueError):
                pass

    ax_frame = node.get("AXFrame")
    if isinstance(ax_frame, str):
        m = _AX_FRAME_RE.search(ax_frame)
        if m:
            x, y, w, h = (float(g) for g in m.groups())
            if w > 0 and h > 0:
                return (x, y, x + w, y + h)
    return None


def _iter_roots(tree: list | dict | None) -> list[dict[str, Any]]:
    if isinstance(tree, dict):
        elems = tree.get("elements")
        if isinstance(elems, list):
            return [n for n in elems if isinstance(n, dict)]
        return [tree]
    if isinstance(tree, list):
        return [n for n in tree if isinstance(n, dict)]
    return []


def _cut(
    box: tuple[float, float, float, float],
    frames: list[tuple[float, float, float, float]],
    small_is_height: bool,
) -> bool:
    """Return True if `box` is clipped at 0pt tolerance along its smaller dimension."""
    x1, y1, x2, y2 = box
    for fx1, fy1, fx2, fy2 in frames:
        if small_is_height:
            if y1 <= fy1 or y2 >= fy2:
                return True
        else:
            if x1 <= fx1 or x2 >= fx2:
                return True
    return False


def _format_summary_line(
    unnamed: list[A11yFinding],
    undersized: list[A11yFinding],
) -> str | None:
    if not unnamed and not undersized:
        return None

    parts: list[str] = []
    if unnamed:
        count = len(unnamed)
        coords = " ".join(f"({f.cx},{f.cy})" for f in unnamed[:MAX_LISTED])
        extra = f" and {count - MAX_LISTED} more" if count > MAX_LISTED else ""
        plural = "s" if count > 1 else ""
        parts.append(f"{count} control{plural} with no name at {coords}{extra}")

    if undersized:
        count = len(undersized)
        items: list[str] = []
        for f in undersized[:MAX_LISTED]:
            wd = round(f.width)
            hd = round(f.height)
            prefix = f'"{f.name}" ' if f.name else ""
            items.append(f"{prefix}{wd}x{hd} pt at ({f.cx},{f.cy})")
        extra = f" and {count - MAX_LISTED} more" if count > MAX_LISTED else ""
        plural = "s" if count > 1 else ""
        parts.append(
            f"{count} touch target{plural} under 44 pt: {', '.join(items)}{extra}"
        )

    return "accessibility check (confirm on the screenshot): " + "; ".join(parts)


def audit_ios(tree: list | dict, wpt: float, hpt: float) -> A11yAuditResult:
    """Run deterministic iOS accessibility audit on a nested AX tree."""
    wpt_f = float(wpt)
    hpt_f = float(hpt)
    screen_frame = (0.0, 0.0, wpt_f, hpt_f)
    screen_area = max(0.0, wpt_f * hpt_f)

    unnamed: list[A11yFinding] = []
    undersized: list[A11yFinding] = []
    seen: set[tuple[int, int, str]] = set()

    def walk(
        node: dict[str, Any],
        scrolls: tuple[tuple[float, float, float, float], ...],
    ) -> None:
        kind = _ios_kind(node)
        if "Keyboard" in kind or kind == "Key":
            return

        box = _ios_frame(node)
        next_scrolls = (
            scrolls + (box,) if (box is not None and kind in SCROLLING) else scrolls
        )

        if box is not None and kind in ACTIVE and _is_enabled(node):
            x1, y1, x2, y2 = box
            w = x2 - x1
            h = y2 - y1
            cx = round((x1 + x2) / 2.0)
            cy = round((y1 + y2) / 2.0)

            if 0 <= cx <= wpt_f and 0 <= cy <= hpt_f:
                dedup_key = (cx, cy, kind)
                if dedup_key not in seen:
                    seen.add(dedup_key)
                    name = _ios_name(node)
                    big = screen_area > 0 and (w * h) > (BIG_AREA_RATIO * screen_area)
                    if not name and not big:
                        unnamed.append(
                            A11yFinding(
                                category="unnamed",
                                kind=kind,
                                name="",
                                x=x1,
                                y=y1,
                                width=w,
                                height=h,
                                cx=cx,
                                cy=cy,
                            )
                        )

                    wd = round(w)
                    hd = round(h)
                    if (
                        kind not in SIZE_EXEMPT
                        and min(wd, hd) < MIN_PT
                        and not _cut(box, list(scrolls) + [screen_frame], hd <= wd)
                    ):
                        undersized.append(
                            A11yFinding(
                                category="undersized",
                                kind=kind,
                                name=name[:30],
                                x=x1,
                                y=y1,
                                width=w,
                                height=h,
                                cx=cx,
                                cy=cy,
                            )
                        )

        for child in node.get("children") or []:
            if isinstance(child, dict):
                walk(child, next_scrolls)

    for root in _iter_roots(tree):
        walk(root, ())

    summary_line = _format_summary_line(unnamed, undersized)
    return A11yAuditResult(
        summary_line=summary_line,
        unnamed=unnamed,
        undersized=undersized,
    )


def ios(tree: list | dict, wpt: float, hpt: float) -> str | None:
    """Upstream-compatible entry point returning only the summary line (or None)."""
    return audit_ios(tree, wpt, hpt).summary_line


def _element_display_kind(kind: str) -> str:
    if "Field" in kind or kind == "TextArea":
        return "field"
    if kind in _BUTTON_ELEMENT_KINDS:
        return "button"
    return "text"


def extract_elements(
    tree: list | dict,
    wpt: float,
    hpt: float,
    max_elements: int = 120,
) -> list[dict[str, Any]]:
    """Extract compact, on-screen interactive & textual elements with refs (`e1`, ...)."""
    wpt_f = float(wpt)
    hpt_f = float(hpt)
    elements: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, int]] = set()
    limit = max(1, int(max_elements))

    def walk(node: dict[str, Any]) -> None:
        if len(elements) >= limit:
            return

        kind = _ios_kind(node)
        box = _ios_frame(node)

        if box is not None and kind not in _SKIP_ELEMENT_KINDS:
            x1, y1, x2, y2 = box
            w = x2 - x1
            h = y2 - y1
            cx = round((x1 + x2) / 2.0)
            cy = round((y1 + y2) / 2.0)

            if w > 0 and h > 0 and 0 <= cx <= wpt_f and 0 <= cy <= hpt_f:
                label = _direct_label(node)
                if not label and kind in _RECURSIVE_LABEL_KINDS:
                    label = _ios_name(node)

                value = _clean_str(node.get("AXValue")) or _clean_str(node.get("value"))
                identifier = (
                    _clean_str(node.get("AXUniqueId"))
                    or _clean_str(node.get("identifier"))
                    or _clean_str(node.get("AXIdentifier"))
                )

                is_interesting = bool(
                    label
                    or value
                    or identifier
                    or "Field" in kind
                    or kind
                    in (
                        "Button",
                        "BackButton",
                        "PopUpButton",
                        "RadioButton",
                        "TextArea",
                        "Switch",
                        "Slider",
                        "Tab",
                        "Link",
                        "Stepper",
                        "SegmentedControl",
                    )
                )

                if is_interesting:
                    dedup_key = (label, value, cx, cy)
                    if dedup_key not in seen:
                        seen.add(dedup_key)
                        elements.append(
                            {
                                "ref": f"e{len(elements) + 1}",
                                "kind": _element_display_kind(kind),
                                "role": kind or "Unknown",
                                "label": label,
                                "identifier": identifier,
                                "value": value,
                                "cx": cx,
                                "cy": cy,
                                "frame": {
                                    "x": round(x1, 2),
                                    "y": round(y1, 2),
                                    "width": round(w, 2),
                                    "height": round(h, 2),
                                },
                                "enabled": _is_enabled(node),
                            }
                        )

        for child in node.get("children") or []:
            if len(elements) >= limit:
                return
            if isinstance(child, dict):
                walk(child)

    for root in _iter_roots(tree):
        walk(root)

    return elements


def find_opaque_containers(
    tree: list | dict | None,
    wpt: float,
    hpt: float,
) -> list[dict[str, Any]]:
    """Detect childless, unlabeled on-screen `TabGroup` / `SegmentedControl` frames.

    On iOS 27 `fb-idb` `AX`, SwiftUI segmented pickers collapse into an empty,
    unlabeled `TabGroup` leaf with zero children; returning their bounding boxes
    lets the driver run targeted `AXBRIDGE_PERSISTENT` grafting only inside those
    regions without polluting `audit_ios()`.
    """
    wpt_f = float(wpt)
    hpt_f = float(hpt)
    boxes: list[dict[str, Any]] = []
    seen: set[tuple[float, float, float, float, str]] = set()

    def walk(node: dict[str, Any]) -> None:
        kind = _ios_kind(node)
        dict_children = [c for c in (node.get("children") or []) if isinstance(c, dict)]

        if (
            kind in _OPAQUE_CONTAINER_KINDS
            and not dict_children
            and not _direct_label(node)
            and _is_enabled(node)
        ):
            box = _ios_frame(node)
            if box is not None:
                x1, y1, x2, y2 = box
                w = x2 - x1
                h = y2 - y1
                cx = round((x1 + x2) / 2.0)
                cy = round((y1 + y2) / 2.0)
                if w > 0 and h > 0 and 0 <= cx <= wpt_f and 0 <= cy <= hpt_f:
                    entry_key = (
                        round(x1, 1),
                        round(y1, 1),
                        round(w, 1),
                        round(h, 1),
                        kind,
                    )
                    if entry_key not in seen:
                        seen.add(entry_key)
                        boxes.append(
                            {
                                "x": round(x1, 2),
                                "y": round(y1, 2),
                                "width": round(w, 2),
                                "height": round(h, 2),
                                "kind": kind,
                            }
                        )

        for child in dict_children:
            walk(child)

    for root in _iter_roots(tree):
        walk(root)

    return boxes


def _point_in_any_box(
    cx: float,
    cy: float,
    boxes: list[dict[str, Any]],
    *,
    tolerance: float = 2.0,
) -> dict[str, Any] | None:
    for box in boxes:
        try:
            bx = float(box["x"])
            by = float(box["y"])
            bw = float(box["width"])
            bh = float(box["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if bw <= 0 or bh <= 0:
            continue
        if (bx - tolerance) <= cx <= (bx + bw + tolerance) and (by - tolerance) <= cy <= (
            by + bh + tolerance
        ):
            return box
    return None


def _is_duplicate_element(
    cand: dict[str, Any],
    existing: list[dict[str, Any]],
) -> bool:
    c_label = _clean_str(cand.get("label"))
    c_label_low = c_label.lower()
    c_val = _clean_str(cand.get("value"))
    c_kind = _clean_str(cand.get("kind"))
    c_cx = int(cand.get("cx", 0))
    c_cy = int(cand.get("cy", 0))

    for ex in existing:
        e_label = _clean_str(ex.get("label"))
        e_val = _clean_str(ex.get("value"))
        e_cx = int(ex.get("cx", 0))
        e_cy = int(ex.get("cy", 0))

        if (c_label, c_val, c_cx, c_cy) == (e_label, e_val, e_cx, e_cy):
            return True
        if (
            c_label_low
            and c_label_low == e_label.lower()
            and abs(c_cx - e_cx) <= 12
            and abs(c_cy - e_cy) <= 12
        ):
            return True
        if (
            not c_label_low
            and not e_label
            and c_kind == _clean_str(ex.get("kind"))
            and abs(c_cx - e_cx) <= 4
            and abs(c_cy - e_cy) <= 4
        ):
            return True
    return False


def graft_elements_in_boxes(
    base_elements: list[dict[str, Any]],
    bridge_tree: list | dict | None,
    boxes: list[dict[str, Any]],
    wpt: float,
    hpt: float,
    max_elements: int = 120,
) -> list[dict[str, Any]]:
    """Graft controls from `bridge_tree` whose centers fall inside `boxes` into `base_elements`.

    Appends non-duplicate controls (up to `max_elements`) and re-numbers refs `e1..eN`.
    """
    limit = max(1, int(max_elements))
    merged: list[dict[str, Any]] = [dict(el) for el in (base_elements or [])[:limit]]

    if boxes and bridge_tree:
        # Use a generous cap when scanning bridge_tree since AXBRIDGE trees are deep
        bridge_candidates = extract_elements(
            bridge_tree, wpt, hpt, max_elements=max(500, limit * 4)
        )
        matched: list[dict[str, Any]] = []
        has_non_container_in_opaque = False
        for cand in bridge_candidates:
            cx = float(cand.get("cx", -1))
            cy = float(cand.get("cy", -1))
            hit_box = _point_in_any_box(cx, cy, boxes)
            if hit_box is None:
                continue

            role = _clean_str(cand.get("role"))
            label = _clean_str(cand.get("label"))
            value = _clean_str(cand.get("value"))
            identifier = _clean_str(cand.get("identifier"))

            # Never graft unlabeled opaque container wrappers back in
            if role in _OPAQUE_CONTAINER_KINDS and not label and not value and not identifier:
                continue
            # Inside opaque picker containers, ignore decorative unlabeled text
            if (
                hit_box.get("kind") in _OPAQUE_CONTAINER_KINDS
                and cand.get("kind") == "text"
                and not label
                and not value
            ):
                continue

            if (
                hit_box.get("kind") in _OPAQUE_CONTAINER_KINDS
                and role not in _OPAQUE_CONTAINER_KINDS
            ):
                has_non_container_in_opaque = True

            matched.append(cand)

        if has_non_container_in_opaque:
            matched = [
                el
                for el in matched
                if _clean_str(el.get("role")) not in _OPAQUE_CONTAINER_KINDS
            ]

        # Prefer interactive controls (button/field) over co-located child StaticText
        matched.sort(key=lambda el: 0 if el.get("kind") in ("button", "field") else 1)

        for cand in matched:
            if len(merged) >= limit:
                break
            if _is_duplicate_element(cand, merged):
                continue
            merged.append(dict(cand))

    for idx, el in enumerate(merged, start=1):
        el["ref"] = f"e{idx}"
    return merged


def format_elements_text(elements: list[dict[str, Any]]) -> str:
    """Format extracted elements into compact agent-readable lines."""
    lines: list[str] = []
    for el in elements:
        ref = el.get("ref") or ""
        kind = el.get("kind") or "text"
        label = (el.get("label") or "").replace("\n", " ").strip()
        value = (el.get("value") or "").replace("\n", " ").strip()
        identifier = (el.get("identifier") or "").strip()
        cx = el.get("cx", 0)
        cy = el.get("cy", 0)

        primary = (label or value)[:70]
        parts = [f"- [{ref}] {kind}" if ref else f"- {kind}"]
        if primary:
            parts.append(f'"{primary}"')
        elif identifier:
            parts.append(f'id="{identifier[:50]}"')
        else:
            parts.append('""')

        if identifier and primary:
            parts.append(f'id="{identifier[:40]}"')
        if value and value != label:
            parts.append(f'value="{value[:40]}"')
        if el.get("enabled") is False:
            parts.append("[disabled]")
        parts.append(f"centre=({cx},{cy})")
        lines.append(" ".join(parts))

    return "\n".join(lines)
