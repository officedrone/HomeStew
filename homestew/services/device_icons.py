"""Canonical device-icon catalog shared by the API, the LLM tool and tests.

Since v0.1.9 the selectable icons are the ENTIRE Lucide set (~2,100 glyphs)
that ships in ``frontend/vendor/lucide.min.js`` — there is no hand-curated
list anymore. A device's ``icon`` column stores a kebab-case Lucide icon name
directly (e.g. ``"washing-machine"``); NULL or an unknown name renders the
default glyph (``DEFAULT_DEVICE_ICON``).

The authoritative name list lives in ``lucide_icon_names.txt`` next to this
module: every line is one kebab-case name, generated from the vendored bundle
(see ``_gen_icon_names.py`` at the repo root) and round-trip-verified against
Lucide's own ``data-lucide`` name parser, so every listed name is guaranteed
to render. Regenerate that file after upgrading lucide.min.js.

``search_icon_keys()`` is the single search implementation behind both the
``GET /api/devices/icons`` endpoint and the manage_devices tool's
``action='search_icons'``; the frontend implements the same matcher over the
same list (fetched from the endpoint's ``/static`` copy) so typing in the
picker feels instant. Matching is substring AND over words — users never have
to know an icon's exact name.

``DEFAULT_ICON_PICKER`` is the curated *initial view* of the picker (and the
result of a blank search). It is a browse convenience, not a constraint: any
valid Lucide name can be stored once found via search.
"""
import re
from pathlib import Path

#: Key rendered when a device has no icon chosen (NULL in the database).
DEFAULT_DEVICE_ICON = "plug"

#: Names that read better as acronyms/stylised words in picker labels. The
#: frontend mirrors this map verbatim in frontend/app.js.
_ICON_LABEL_OVERRIDES = {
    "cctv": "CCTV",
    "tv": "TV",
    "wifi": "WiFi",
    "led": "LED",
    "dvd": "DVD",
    "usb": "USB",
    "pc": "PC",
    "hd": "HD",
    "id": "ID",
    "ai": "AI",
    "3d": "3D",
}

#: All selectable Lucide icon names, loaded from the generated list file.
LUCIDE_ICON_NAMES: frozenset[str] = frozenset(
    (Path(__file__).parent / "lucide_icon_names.txt")
    .read_text(encoding="utf-8")
    .split()
)

#: Curated common household/device glyphs shown first in the picker and
#: returned for a blank search. Every entry MUST be in LUCIDE_ICON_NAMES
#: (tests/test_device_tool.py enforces it). Order = display order.
DEFAULT_ICON_PICKER: tuple[str, ...] = (
    "plug",
    "refrigerator",
    "snowflake",
    "cooking-pot",
    "droplets",
    "microwave",
    "washing-machine",
    "coffee",
    "blender",
    "glass-water",
    "air-vent",
    "wind",
    "cloud-rain",
    "heater",
    "thermometer",
    "fan",
    "robot-vacuum",
    "flame",
    "tv",
    "monitor",
    "laptop",
    "tablet",
    "smartphone",
    "watch",
    "headphones",
    "keyboard",
    "mouse",
    "gamepad2",
    "printer",
    "projector",
    "camera",
    "cctv",
    "speaker",
    "router",
    "hard-drive",
    "server",
    "lock",
    "lightbulb",
    "zap",
    "plant-pot",
)


def is_valid_icon_key(key: str) -> bool:
    """True when *key* is a Lucide icon name the frontend can render."""
    return key in LUCIDE_ICON_NAMES


def icon_label(name: str) -> str:
    """Human-readable picker label for a kebab-case icon name.

    'washing-machine' -> 'Washing Machine'. Hyphen words are capitalised;
    a few acronyms use ``_ICON_LABEL_OVERRIDES``. The frontend derives the
    same labels client-side (iconLabel() in app.js) — keep both in sync.
    """
    words = name.split("-")
    return " ".join(
        _ICON_LABEL_OVERRIDES.get(w, w.capitalize()) for w in words
    )


def search_icon_keys(query: str, limit: int = 12) -> list[tuple[str, str]]:
    """Return ``(name, label)`` pairs whose name matches *query*.

    Matching is AND over the query words (every word must appear as a
    substring of the icon name, case-insensitive), so 'washing' finds
    washing-machine and 'water' finds glass-water without knowing exact
    names. Ranking: a full phrase hit on the name beats a per-word hit, plus
    bonuses for prefix matches and exact words; shorter names win ties so
    'coffee' ranks coffee above coffee-bean. An empty/blank query returns
    ``DEFAULT_ICON_PICKER`` (the picker's initial view), not all ~2,100 names.
    """
    text = str(query or "").strip().lower()
    if not text:
        return [(name, icon_label(name)) for name in DEFAULT_ICON_PICKER]
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w]
    if not words:
        return []
    phrase = " ".join(words)

    scored: list[tuple[int, int, int, str]] = []
    for idx, name in enumerate(sorted(LUCIDE_ICON_NAMES)):
        # AND semantics: a word found nowhere disqualifies the icon.
        if not all(w in name for w in words):
            continue
        score = 0
        if phrase in name:
            score += 8
        if name.startswith(phrase):
            score += 4
        elif any(name.startswith(w) for w in words):
            score += 2
        exact_pool = set(name.split("-"))
        for word in words:
            if word in exact_pool:
                score += 3
        # Shorter names win ties (coffee before coffee-bean); sorted() keeps
        # the whole scan deterministic.
        scored.append((-score, len(name), idx, name))

    scored.sort()
    return [(name, icon_label(name)) for _s, _l, _i, name in scored[:limit]]
