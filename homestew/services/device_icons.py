"""Canonical device-icon key list shared by the API, the LLM tool and tests.

The glyphs themselves live in ``frontend/app.js`` (the ``DEVICE_ICONS`` map,
which renders Lucide icons via ``data-lucide`` attributes). Devices store only
a key string; a NULL/unknown key renders the default appliance glyph.

``DEVICE_ICON_KEYS`` MUST mirror the frontend map's key order exactly —
``tests/test_device_tool.py::test_icon_keys_match_frontend_picker`` parses
app.js and compares the tuples, so adding an icon means editing BOTH lists
(frontend first, then append the key here).
"""

#: Key rendered when a device has no icon chosen (NULL in the database).
DEFAULT_DEVICE_ICON = "appliance"

#: Every selectable icon key, in the same order as ``DEVICE_ICONS`` in
#: frontend/app.js. Keep both lists in sync (see module docstring).
DEVICE_ICON_KEYS: tuple[str, ...] = (
    "appliance",
    "fridge",
    "freezer",
    "oven",
    "dishwasher",
    "microwave",
    "washer",
    "coffee_maker",
    "blender",
    "water_dispenser",
    "air_conditioner",
    "air_purifier",
    "humidifier",
    "water_heater",
    "thermostat",
    "fan",
    "vacuum",
    "grill",
    "tv",
    "computer",
    "laptop",
    "tablet",
    "phone",
    "smartwatch",
    "headphones",
    "keyboard",
    "mouse",
    "gaming_console",
    "printer",
    "projector",
    "camera",
    "security_camera",
    "smart_speaker",
    "router",
    "nas_drive",
    "server_rack",
    "smart_lock",
    "lighting",
    "generator",
    "lawn_garden",
)
