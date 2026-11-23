"""Persona catalog loader, schema validator, and selector presets."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

DEFAULT_PERSONAS_PATH = Path(__file__).resolve().parents[2] / "personas.json"

ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
LOCALE_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]+)*$")

VALID_THEMES: tuple[str, ...] = ("light", "dark")

VALID_TEXT_SIZES: tuple[str, ...] = (
    "extra-small",
    "small",
    "medium",
    "large",
    "extra-large",
    "extra-extra-large",
    "extra-extra-extra-large",
    "accessibility-medium",
    "accessibility-large",
    "accessibility-extra-large",
    "accessibility-extra-extra-large",
    "accessibility-extra-extra-extra-large",
)

PRESETS: dict[str, list[str]] = {
    "smoke": ["clara", "amira", "sven"],
    "iphone": ["clara", "noah", "yuki", "amira", "sven", "giulia", "wei"],
    "ipad": ["jonas", "mateo", "minjun"],
    "a11y": ["sven", "jonas", "giulia", "noah"],
    "all": [
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
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_personas(personas: list[dict]) -> list[str]:
    """Validate a list of persona definitions against upstream invariants.

    Returns an empty list if valid, or a list of human-readable error strings.
    """
    errors: list[str] = []
    if not isinstance(personas, list):
        return ["personas must be a JSON array"]
    if not personas:
        return ["personas list must not be empty"]

    seen_ids: set[str] = set()
    for idx, entry in enumerate(personas):
        prefix = f"personas[{idx}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: entry must be an object")
            continue

        pid = entry.get("id")
        if not isinstance(pid, str) or not ID_RE.match(pid):
            errors.append(
                f"{prefix}.id: must match ^[a-z][a-z0-9_-]*$ (got {pid!r})"
            )
        else:
            prefix = f"personas[{idx}]({pid})"
            if pid in seen_ids:
                errors.append(f"{prefix}.id: duplicate persona id {pid!r}")
            seen_ids.add(pid)

        platform = entry.get("platform")
        if platform != "ios":
            errors.append(
                f"{prefix}.platform: must be 'ios' (got {platform!r})"
            )

        profile = entry.get("profile")
        if not isinstance(profile, str) or not profile.strip():
            errors.append(f"{prefix}.profile: must be a non-empty string")

        goal = entry.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            errors.append(f"{prefix}.goal: must be a non-empty string")

        locale = entry.get("locale")
        if not isinstance(locale, str) or not LOCALE_RE.match(locale):
            errors.append(
                f"{prefix}.locale: must match ^[a-z]{{2,3}}(-[A-Za-z0-9]+)*$ (got {locale!r})"
            )

        geo = entry.get("geo")
        if (
            not isinstance(geo, (list, tuple))
            or len(geo) != 2
            or not _is_number(geo[0])
            or not _is_number(geo[1])
        ):
            errors.append(f"{prefix}.geo: must be [lat, lon] numbers (got {geo!r})")
        else:
            lat, lon = float(geo[0]), float(geo[1])
            if not (-90.0 <= lat <= 90.0):
                errors.append(
                    f"{prefix}.geo[0]: latitude {lat} out of range [-90, 90]"
                )
            if not (-180.0 <= lon <= 180.0):
                errors.append(
                    f"{prefix}.geo[1]: longitude {lon} out of range [-180, 180]"
                )

        if "email" in entry and entry["email"] is not None:
            email = entry["email"]
            if not isinstance(email, str) or "@" not in email or not email.strip():
                errors.append(
                    f"{prefix}.email: optional email must be a valid email string"
                )

        device = entry.get("device")
        if not isinstance(device, dict):
            errors.append(f"{prefix}.device: must be an object")
            continue

        sim = device.get("simulator")
        if not isinstance(sim, str) or not (
            sim.startswith("iPhone") or sim.startswith("iPad")
        ):
            errors.append(
                f"{prefix}.device.simulator: must start with 'iPhone' or 'iPad' (got {sim!r})"
            )

        ios_ver = device.get("ios")
        if not isinstance(ios_ver, int) or isinstance(ios_ver, bool) or ios_ver <= 0:
            errors.append(
                f"{prefix}.device.ios: must be a positive integer (got {ios_ver!r})"
            )

        theme = device.get("theme")
        if theme not in VALID_THEMES:
            errors.append(
                f"{prefix}.device.theme: must be one of {VALID_THEMES} (got {theme!r})"
            )

        text_size = device.get("text_size")
        if text_size not in VALID_TEXT_SIZES:
            errors.append(
                f"{prefix}.device.text_size: must be one of {VALID_TEXT_SIZES} (got {text_size!r})"
            )

    return errors


def load_personas(path: str | Path | None = None) -> list[dict]:
    """Load and validate personas JSON from `path` (or bundled `personas.json`)."""
    target = Path(path) if path is not None else DEFAULT_PERSONAS_PATH
    if not target.is_file():
        raise FileNotFoundError(f"Personas file not found: {target}")

    raw_text = target.read_text(encoding="utf-8")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in personas file {target}: {exc}") from exc

    errors = validate_personas(data)
    if errors:
        raise ValueError(
            f"Invalid personas definition in {target}:\n- " + "\n- ".join(errors)
        )
    return copy.deepcopy(data)


def select_personas(
    selector: str = "smoke",
    custom_path: str | Path | None = None,
) -> list[dict]:
    """Resolve a preset (`smoke`, `iphone`, `ipad`, `a11y`, `all`) or CSV of IDs."""
    personas = load_personas(custom_path)
    by_id = {p["id"]: p for p in personas}

    raw = (selector or "smoke").strip()
    lower = raw.lower()

    if lower == "all":
        return copy.deepcopy(personas)

    if lower in PRESETS:
        preset_ids = PRESETS[lower]
        matched = [copy.deepcopy(by_id[pid]) for pid in preset_ids if pid in by_id]
        if matched:
            return matched
        # Graceful fallback for custom persona catalogs using family/a11y presets
        if lower == "iphone":
            return [
                copy.deepcopy(p)
                for p in personas
                if str(p.get("device", {}).get("simulator", "")).startswith("iPhone")
            ]
        if lower == "ipad":
            return [
                copy.deepcopy(p)
                for p in personas
                if str(p.get("device", {}).get("simulator", "")).startswith("iPad")
            ]
        if lower == "a11y":
            return [
                copy.deepcopy(p)
                for p in personas
                if str(p.get("device", {}).get("text_size", "")).startswith(
                    ("accessibility-", "extra-extra-extra-large")
                )
            ]
        if lower == "smoke":
            return copy.deepcopy(personas[:3])

    tokens = [tok.strip() for tok in raw.split(",") if tok.strip()]
    if not tokens:
        raise ValueError("Persona selector must not be empty")

    selected: list[dict] = []
    seen: set[str] = set()
    unknown: list[str] = []
    for tok in tokens:
        if tok in by_id:
            if tok not in seen:
                selected.append(copy.deepcopy(by_id[tok]))
                seen.add(tok)
        elif tok.lower() in PRESETS:
            for item in select_personas(tok.lower(), custom_path=custom_path):
                if item["id"] not in seen:
                    selected.append(item)
                    seen.add(item["id"])
        else:
            unknown.append(tok)

    if unknown:
        valid_ids = ", ".join(by_id.keys())
        valid_presets = ", ".join(PRESETS.keys())
        raise ValueError(
            f"Unknown persona(s): {', '.join(unknown)}. "
            f"Valid IDs: [{valid_ids}]. Presets: [{valid_presets}]."
        )
    return selected


def get_persona(persona_id: str, custom_path: str | Path | None = None) -> dict:
    """Return a single persona dict by ID, raising ValueError if not found."""
    personas = load_personas(custom_path)
    target = (persona_id or "").strip()
    for p in personas:
        if p["id"] == target:
            return copy.deepcopy(p)
    valid_ids = ", ".join(p["id"] for p in personas)
    raise ValueError(f"Unknown persona id {persona_id!r}. Valid IDs: [{valid_ids}]")
