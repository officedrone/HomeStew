"""Canonical device-icon catalog shared by the API, the LLM tool and tests.

The glyphs themselves live in ``frontend/app.js`` (the ``DEVICE_ICONS`` map,
which renders Lucide icons via ``data-lucide`` attributes). Devices store only
a key string; a NULL/unknown key renders the default appliance glyph.

Each catalog entry is ``(key, label, synonyms)``:

- ``label`` mirrors the frontend picker label verbatim;
- ``synonyms`` are extra lowercase search words (never displayed) so everyday
  wording ("boiler", "aircon", "bbq") still finds the right icon.

The catalog's key ORDER and every key/label/synonym set MUST mirror the
frontend map — ``tests/test_device_tool.py::test_icon_keys_match_frontend_picker``
parses app.js and compares all three, so adding an icon means editing BOTH
lists (frontend first, then append the entry here).

``search_icon_keys()`` is the single search implementation behind both the
``GET /api/devices/icons`` endpoint and the manage_devices tool's
``action='search_icons'``; the frontend filters its own copy of the same data
client-side so typing in the picker feels instant.
"""
import re

#: Key rendered when a device has no icon chosen (NULL in the database).
DEFAULT_DEVICE_ICON = "appliance"

#: Every selectable icon as ``(key, label, synonyms)``, in the same order as
#: ``DEVICE_ICONS`` in frontend/app.js. Keep both in sync (see docstring).
DEVICE_ICON_CATALOG: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("appliance", "Appliance (default)", ("plug", "generic", "electronics")),
    ("fridge", "Fridge", ("refrigerator", "cooler", "icebox")),
    ("freezer", "Freezer", ("freeze", "ice", "deep freezer")),
    ("oven", "Oven / Stove", ("stove", "range", "cooker")),
    ("dishwasher", "Dishwasher", ("dishes", "dish washer")),
    ("microwave", "Microwave", ("micro", "reheat")),
    ("washer", "Washing Machine", ("laundry", "washing machine")),
    ("coffee_maker", "Coffee Maker", ("coffee", "espresso", "cup")),
    ("blender", "Blender", ("smoothie", "mixer", "juicer")),
    ("water_dispenser", "Water Dispenser / Filter", ("water", "filter", "dispenser")),
    ("air_conditioner", "Air Conditioner / HVAC", ("ac", "aircon", "cooling", "hvac")),
    ("air_purifier", "Air Purifier", ("purifier", "clean air")),
    ("humidifier", "Humidifier", ("mist", "moisture", "steam")),
    ("water_heater", "Water Heater / Boiler", ("boiler", "hot water", "geyser")),
    ("thermostat", "Thermostat", ("temperature", "climate", "heating")),
    ("fan", "Fan", ("blower", "cooling")),
    ("vacuum", "Vacuum", ("robot vacuum", "cleaning", "hoover")),
    ("grill", "Grill / Fire Pit", ("bbq", "barbecue", "smoker", "fire pit")),
    ("tv", "TV", ("television", "screen", "display")),
    ("computer", "Computer", ("desktop", "pc", "monitor", "workstation")),
    ("laptop", "Laptop", ("notebook", "macbook")),
    ("tablet", "Tablet", ("ipad", "pad")),
    ("phone", "Phone", ("smartphone", "mobile", "cell")),
    ("smartwatch", "Smartwatch", ("watch", "wearable")),
    ("headphones", "Headphones", ("earphones", "headset", "audio")),
    ("keyboard", "Keyboard", ("typing", "keys")),
    ("mouse", "Mouse", ("pointer", "cursor")),
    ("gaming_console", "Gaming Console", ("console", "playstation", "xbox", "games")),
    ("printer", "Printer", ("printing", "print")),
    ("projector", "Projector", ("beamer", "cinema", "presentation")),
    ("camera", "Camera", ("photo", "photography")),
    ("security_camera", "Security Camera", ("cctv", "surveillance")),
    ("smart_speaker", "Smart Speaker", ("speaker", "alexa", "assistant")),
    ("router", "Router", ("wifi", "network", "internet")),
    ("nas_drive", "NAS / External Drive", ("storage", "disk", "backup")),
    ("server_rack", "Server", ("server", "homelab", "compute")),
    ("smart_lock", "Smart Lock", ("lock", "door", "keyless")),
    ("lighting", "Lighting", ("light", "bulb", "lamp")),
    ("generator", "Generator / Power", ("power", "electricity", "inverter")),
    ("lawn_garden", "Lawn & Garden", ("mower", "garden", "yard")),
)

#: Every selectable icon key, derived from the catalog above. Kept as a tuple
#: for the tool schema's enum and membership checks; order mirrors app.js.
DEVICE_ICON_KEYS: tuple[str, ...] = tuple(
    key for key, _label, _syn in DEVICE_ICON_CATALOG
)

#: Display label per icon key (mirrors the frontend picker labels).
DEVICE_ICON_LABELS: dict[str, str] = {
    key: label for key, label, _syn in DEVICE_ICON_CATALOG
}


def search_icon_keys(query: str, limit: int = 12) -> list[tuple[str, str]]:
    """Return ``(key, label)`` pairs whose key/label/synonyms match *query*.

    Matching is AND over the query words (every word must appear somewhere in
    the icon's key/label/synonym text) with substring tolerance, so 'coffee'
    finds coffee_maker and 'water heater' finds water_heater. Ranking: a full
    phrase hit on the key beats the label beats the synonyms, plus a bonus per
    exact word match; ties keep catalog order. An empty/blank query returns
    the WHOLE catalog (ignoring *limit*) so callers can offer everything.
    """
    text = str(query or "").strip().lower()
    if not text:
        return [(key, label) for key, label, _syn in DEVICE_ICON_CATALOG]
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w]
    if not words:
        return []
    phrase = " ".join(words)

    scored: list[tuple[int, int, str, str]] = []
    for idx, (key, label, syn) in enumerate(DEVICE_ICON_CATALOG):
        key_words = key.replace("_", " ")
        label_l = label.lower()
        syn_l = [s.lower() for s in syn]
        blob = f"{key_words} {label_l} {' '.join(syn_l)}"
        # AND semantics: a word found nowhere disqualifies the icon.
        if not all(w in blob for w in words):
            continue
        score = 0
        if phrase in key_words:
            score += 8
        elif phrase in label_l:
            score += 6
        elif any(phrase in s for s in syn_l):
            score += 4
        exact_pool = set(key_words.split())
        for s in syn_l:
            exact_pool.update(w for w in re.split(r"[^a-z0-9]+", s) if w)
        for word in words:
            if word in exact_pool:
                score += 3
        scored.append((-score, idx, key, label))

    scored.sort()
    return [(key, label) for _score, _idx, key, label in scored[:limit]]
