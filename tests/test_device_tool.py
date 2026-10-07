"""Tests for the manage_devices LLM tool (services/device_tool.py).

There is no async pytest runner in this repo, so each test drives the async
executor through asyncio.run() inside a sync function. The db_env fixture
points the settings singleton at a throwaway DATA_DIR and creates a fresh
database - mirroring the isolated-fixture pattern from test_setup_wizard.py.

Run from the repo root:  pytest tests/test_device_tool.py -q
"""
import asyncio

import pytest

from homestew import config as cfg
from homestew.db import get_db_context, init_db
from homestew.services.device_tool import execute_device_tool


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """Fresh database in a temp DATA_DIR; settings restored afterwards."""
    saved_dir = cfg.settings.DATA_DIR
    saved_devices_dir = cfg.settings.DEVICES_DIR
    monkeypatch.setattr(cfg.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg.settings, "DEVICES_DIR", tmp_path / "devices")
    asyncio.run(init_db())
    yield tmp_path
    setattr(cfg.settings, "DATA_DIR", saved_dir)
    setattr(cfg.settings, "DEVICES_DIR", saved_devices_dir)


def run(args, chat_device_id=None):
    return asyncio.run(execute_device_tool(args, chat_device_id))


async def _fetch(device_id):
    async with get_db_context() as db:
        cur = await db.execute("SELECT * FROM devices WHERE id = ?", (device_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


def fetch(device_id):
    return asyncio.run(_fetch(device_id))


# --- create ---------------------------------------------------------------

def test_create_requires_name_brand_model(db_env):
    report = run({"action": "create", "name": "TV"})
    assert report.startswith("Error")
    assert "brand" in report and "model" in report


def test_create_returns_id_and_line(db_env):
    report = run({
        "action": "create", "name": "Living Room TV",
        "brand": "Sony", "model": "XR-65X90J",
    })
    assert report.startswith("Created device id=1:")
    assert '"Living Room TV" (Sony XR-65X90J)' in report
    # A freshly created device comes with a one-click manuals link whose
    # visible text is the device name, not its id.
    assert "[Living Room TV](#fetch-manuals-1)" in report
    row = fetch(1)
    assert row["name"] == "Living Room TV"


def test_create_computes_warranty_end(db_env):
    # Jan 31 + 1 month clamps to Feb 28 (non-leap), same rule as the REST API.
    report = run({
        "action": "create", "name": "Laptop", "brand": "Lenovo", "model": "X1",
        "purchase_date": "2026-01-31", "warranty_length": 1, "warranty_unit": "months",
    })
    assert "ends 2026-02-28" in report


def test_create_rejects_bad_dates_and_units(db_env):
    r = run({"action": "create", "name": "A", "brand": "B", "model": "C",
             "purchase_date": "last tuesday"})
    assert r.startswith("Error") and "purchase_date" in r
    r = run({"action": "create", "name": "A", "brand": "B", "model": "C",
             "warranty_length": 2, "warranty_unit": "decades"})
    assert r.startswith("Error") and "warranty_unit" in r


def test_create_refuses_exact_twin(db_env):
    run({"action": "create", "name": "TV", "brand": "Sony", "model": "X1"})
    report = run({"action": "create", "name": "tv", "brand": "SONY", "model": "x1"})
    assert "Nothing created" in report and "id=1" in report


# --- update ---------------------------------------------------------------

def test_update_partial_and_clear(db_env):
    run({"action": "create", "name": "TV", "brand": "Sony", "model": "X1",
         "serial_number": "AAA"})
    report = run({"action": "update", "device_id": 1,
                  "serial_number": "", "description": "bedroom unit"})
    assert report.startswith("Updated device id=1")
    row = fetch(1)
    assert row["serial_number"] is None
    assert row["description"] == "bedroom unit"


def test_update_recomputes_warranty_end(db_env):
    run({"action": "create", "name": "Drill", "brand": "Bosch", "model": "GSR",
         "purchase_date": "2025-03-10", "warranty_length": 2,
         "warranty_unit": "years"})
    assert fetch(1)["warranty_end"] == "2027-03-10"
    run({"action": "update", "device_id": 1, "warranty_length": 3})
    row = fetch(1)
    assert row["warranty_end"] == "2028-03-10"


def test_update_requires_field_and_real_id(db_env):
    run({"action": "create", "name": "TV", "brand": "Sony", "model": "X1"})
    r = run({"action": "update", "device_id": 1})
    assert r.startswith("Nothing to change")
    r = run({"action": "update", "device_id": 99, "name": "Ghost"})
    assert r.startswith("Error") and "no device with id=99" in r


def test_update_cannot_clear_required_name(db_env):
    run({"action": "create", "name": "TV", "brand": "Sony", "model": "X1"})
    # Empty name is ignored as "not provided" rather than clearing the column.
    r = run({"action": "update", "device_id": 1, "name": "", "description": "x"})
    assert r.startswith("Updated") and fetch(1)["name"] == "TV"


# --- delete (refused) -----------------------------------------------------

def test_delete_is_refused_with_editor_link(db_env):
    run({"action": "create", "name": "TV", "brand": "Sony", "model": "X1"})
    report = run({"action": "delete", "device_id": 1})
    assert "was NOT deleted" in report
    # The link's visible text is the device name, never "Edit Device #<id>".
    assert "[TV](#edit-device-1)" in report
    # The device must still be there.
    assert fetch(1) is not None


def test_delete_unknown_device_errors(db_env):
    r = run({"action": "delete", "device_id": 42})
    assert r.startswith("Error") and "no device with id=42" in r


# --- attributes -----------------------------------------------------------

def test_add_and_remove_attribute_by_name(db_env):
    run({"action": "create", "name": "PC", "brand": "Dell", "model": "OptiPlex"})
    report = run({"action": "add_attribute", "device_id": 1,
                  "attribute_name": "RAM", "attribute_value": "16GB"})
    assert report.startswith("Added attribute id=1")
    r = run({"action": "remove_attribute", "device_id": 1, "attribute_name": "ram"})
    assert r.startswith('Removed attribute "RAM"')


def test_remove_attribute_requires_known_name(db_env):
    run({"action": "create", "name": "PC", "brand": "Dell", "model": "OptiPlex"})
    r = run({"action": "remove_attribute", "device_id": 1, "attribute_name": "GPU"})
    assert r.startswith("Error") and "no attribute named" in r


def test_add_attribute_needs_both_parts(db_env):
    run({"action": "create", "name": "PC", "brand": "Dell", "model": "OptiPlex"})
    r = run({"action": "add_attribute", "device_id": 1, "attribute_name": "RAM"})
    assert r.startswith("Error")


# --- search_devices (device resolution) -----------------------------------

def test_search_devices_unique_match_rewrites_with_brand_model(db_env):
    run({"action": "create", "name": "Work Laptop", "brand": "HP",
         "model": "EliteBook 840"})
    run({"action": "create", "name": "Kitchen Fridge", "brand": "Bosch",
         "model": "KAD93"})
    # The user's nickname 'work laptop' matches exactly one device; the report
    # must tell the model to re-search using that device's real brand+model.
    report = run({"action": "search_devices", "query": "what memory does my work laptop support"})
    assert "exactly ONE device" in report
    assert "HP EliteBook 840" in report
    # The rewritten-query instruction carries the concrete brand/model.
    assert 'HP EliteBook 840 memory' in report
    # And a clickable link to the resolved device.
    assert "[Work Laptop](#edit-device-1)" in report


def test_search_devices_ambiguous_asks_user(db_env):
    run({"action": "create", "name": "Living Room TV", "brand": "Sony", "model": "A"})
    run({"action": "create", "name": "Bedroom TV", "brand": "LG", "model": "B"})
    report = run({"action": "search_devices", "query": "the tv"})
    assert "AMBIGUOUS" in report
    assert "Ask the user which" in report
    # Both candidates get a link so the user can pick.
    assert "[Living Room TV](#edit-device-1)" in report
    assert "[Bedroom TV](#edit-device-2)" in report


def test_search_devices_matches_custom_attribute(db_env):
    run({"action": "create", "name": "Desktop", "brand": "Dell", "model": "X"})
    run({"action": "add_attribute", "device_id": 1,
         "attribute_name": "Nickname", "attribute_value": "render box"})
    report = run({"action": "search_devices", "query": "render box"})
    assert "exactly ONE device" in report and "device_id=1" in report


def test_search_devices_no_match_falls_back(db_env):
    run({"action": "create", "name": "TV", "brand": "Sony", "model": "X1"})
    report = run({"action": "search_devices", "query": "espresso machine"})
    assert "No registered device matches" in report


def test_search_devices_needs_query(db_env):
    r = run({"action": "search_devices", "query": "what is it"})
    assert r.startswith("Error")


# --- mutation reports always carry a device link --------------------------

def test_update_and_attribute_reports_include_device_link(db_env):
    run({"action": "create", "name": "Router", "brand": "Ubiquiti", "model": "U7"})
    upd = run({"action": "update", "device_id": 1, "description": "office"})
    assert "[Router](#edit-device-1)" in upd
    add = run({"action": "add_attribute", "device_id": 1,
               "attribute_name": "Ports", "attribute_value": "4x GbE"})
    assert "[Router](#edit-device-1)" in add
    rem = run({"action": "remove_attribute", "device_id": 1,
               "attribute_name": "Ports"})
    assert "[Router](#edit-device-1)" in rem


def test_scoped_search_devices_only_sees_own_device(db_env):
    run({"action": "create", "name": "Work Laptop", "brand": "HP", "model": "E"})
    run({"action": "create", "name": "Home Laptop", "brand": "Apple", "model": "M"})
    # In a chat filtered to device 2, only that device can match.
    report = run({"action": "search_devices", "query": "laptop"}, chat_device_id=2)
    assert '"Home Laptop"' in report and '"Work Laptop"' not in report


# --- list -----------------------------------------------------------------

def test_list_shows_ids_manuals_and_attributes(db_env):
    run({"action": "create", "name": "TV", "brand": "Sony", "model": "X1"})
    run({"action": "add_attribute", "device_id": 1,
         "attribute_name": "Room", "attribute_value": "Living"})
    async def seed_manual():
        async with get_db_context() as db:
            await db.execute(
                "INSERT INTO manuals (device_id, filename, filepath) "
                "VALUES (1, 'm.pdf', '/data/devices/1/manuals/m.pdf')"
            )
            await db.commit()
    asyncio.run(seed_manual())

    report = run({"action": "list"})
    assert "1 device(s):" in report
    assert "- device_id=1:" in report
    assert "1 manual(s)" in report
    assert "Room: Living" in report


def test_list_empty_registry(db_env):
    assert "No devices are registered yet" in run({"action": "list"})


# --- scope enforcement ----------------------------------------------------

def test_scoped_chat_cannot_touch_other_devices(db_env):
    run({"action": "create", "name": "TV", "brand": "Sony", "model": "X1"})
    run({"action": "create", "name": "PC", "brand": "Dell", "model": "OptiPlex"})
    r = run({"action": "update", "device_id": 2, "description": "hack"},
            chat_device_id=1)
    assert r.startswith("Error") and "filtered to" in r
    # delete/add_attribute share the same guard.
    assert run({"action": "delete", "device_id": 2}, chat_device_id=1).startswith("Error")
    assert run({"action": "add_attribute", "device_id": 2,
                "attribute_name": "a", "attribute_value": "b"},
               chat_device_id=1).startswith("Error")


def test_scoped_chat_targets_its_device_implicitly(db_env):
    run({"action": "create", "name": "TV", "brand": "Sony", "model": "X1"})
    # No device_id given: the filter is the implicit target.
    r = run({"action": "update", "description": "scoped"}, chat_device_id=1)
    assert r.startswith("Updated device id=1")
    # Creating a NEW device stays allowed in a filtered chat.
    r = run({"action": "create", "name": "New", "brand": "B", "model": "M"},
            chat_device_id=1)
    assert r.startswith("Created device id=2:")


def test_scoped_list_only_shows_own_device(db_env):
    run({"action": "create", "name": "TV", "brand": "Sony", "model": "X1"})
    run({"action": "create", "name": "PC", "brand": "Dell", "model": "OptiPlex"})
    report = run({"action": "list"}, chat_device_id=2)
    assert '"PC"' in report and '"TV"' not in report


# --- misc -----------------------------------------------------------------

def test_unknown_action_lists_valid_ones(db_env):
    r = run({"action": "teleport"})
    assert r.startswith("Error") and "create" in r and "list" in r


def test_bad_device_id_reports_how_to_find_ids(db_env):
    r = run({"action": "update", "device_id": "the tv", "name": "X"})
    assert r.startswith("Error") and "action='list'" in r


# --- icons ----------------------------------------------------------------

def test_create_with_valid_icon_stores_and_reports_it(db_env):
    report = run({
        "action": "create", "name": "Fridge", "brand": "LG", "model": "LFX",
        "icon": "refrigerator",
    })
    assert report.startswith("Created device id=1:")
    assert "icon=refrigerator" in report
    assert fetch(1)["icon"] == "refrigerator"


def test_create_rejects_unknown_icon_and_points_at_search(db_env):
    r = run({"action": "create", "name": "A", "brand": "B", "model": "C",
             "icon": "toaster"})
    assert r.startswith("Error") and "icon" in r
    # The report teaches the model how to find real keys.
    assert "search_icons" in r


def test_update_sets_icon(db_env):
    run({"action": "create", "name": "TV", "brand": "Sony", "model": "X1"})
    assert fetch(1)["icon"] is None
    report = run({"action": "update", "device_id": 1, "icon": "tv"})
    assert report.startswith("Updated device id=1") and "icon" in report
    assert fetch(1)["icon"] == "tv"


def test_update_empty_icon_resets_to_default(db_env):
    run({"action": "create", "name": "TV", "brand": "Sony", "model": "X1",
         "icon": "tv"})
    report = run({"action": "update", "device_id": 1, "icon": ""})
    assert report.startswith("Updated device id=1")
    # Cleared -> NULL, which the frontend renders as the default glyph.
    assert fetch(1)["icon"] is None


def test_update_rejects_unknown_icon(db_env):
    run({"action": "create", "name": "TV", "brand": "Sony", "model": "X1"})
    r = run({"action": "update", "device_id": 1, "icon": "spaceship"})
    assert r.startswith("Error") and "icon" in r
    # The rejected value must not have been written.
    assert fetch(1)["icon"] is None


def test_icon_is_case_insensitive_and_normalises_underscores(db_env):
    run({"action": "create", "name": "TV", "brand": "Sony", "model": "X1"})
    # Models habitually write snake_case; canonical Lucide names are kebab.
    r = run({"action": "update", "device_id": 1, "icon": "  Washing_Machine "})
    assert r.startswith("Updated device id=1")
    assert fetch(1)["icon"] == "washing-machine"


def test_list_reports_icon(db_env):
    run({"action": "create", "name": "Kettle", "brand": "Breville",
         "model": "BKE", "icon": "coffee"})
    report = run({"action": "list"})
    assert "icon=coffee" in report


def test_icon_names_match_frontend_list():
    """Backend lucide_icon_names.txt must mirror frontend/lucide-icon-names.js.

    Both files are generated from the vendored lucide.min.js by
    _gen_icon_names.py; a drift means one side offers names the other cannot
    render. Also checks every curated DEFAULT_ICON_PICKER entry is valid and
    that all names survive the API's stored-key pattern.
    """
    import re
    from pathlib import Path

    from homestew.services.device_icons import (
        DEFAULT_ICON_PICKER,
        LUCIDE_ICON_NAMES,
    )

    root = Path(__file__).resolve().parent.parent
    js = (root / "frontend" / "lucide-icon-names.js").read_text(encoding="utf-8")
    frontend_names = set(re.findall(r'"([a-z0-9][a-z0-9-]*)"', js))
    assert frontend_names == set(LUCIDE_ICON_NAMES)

    # Curated picker defaults must all be renderable names.
    assert len(DEFAULT_ICON_PICKER) == len(set(DEFAULT_ICON_PICKER))
    for name in DEFAULT_ICON_PICKER:
        assert name in LUCIDE_ICON_NAMES, f"DEFAULT_ICON_PICKER has {name}"

    # Every stored name fits the API schema pattern (^[a-z0-9_-]+$).
    for name in LUCIDE_ICON_NAMES:
        assert re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,49}", name), name


# --- icon search (name matcher + tool action) ------------------------------

def test_search_icon_keys_matches_name_substrings():
    from homestew.services.device_icons import search_icon_keys

    keys = [k for k, _l in search_icon_keys("coffee")]
    assert "coffee" in keys
    # Partial match: the query need not be the full name.
    assert "washing-machine" in [k for k, _l in search_icon_keys("washing")]
    # Multi-word AND semantics across words.
    assert "lamp-ceiling" in [k for k, _l in search_icon_keys("lamp ceiling")]
    # Substring tolerance mid-name.
    assert "air-vent" in [k for k, _l in search_icon_keys("vent")]


def test_search_icon_keys_and_semantics_reject_partial_queries():
    from homestew.services.device_icons import search_icon_keys

    # 'wifi' matches many names but the AND with 'washing' must empty it.
    assert search_icon_keys("wifi washing") == []


def test_search_icon_keys_blank_returns_curated_picker():
    from homestew.services.device_icons import (
        DEFAULT_ICON_PICKER,
        search_icon_keys,
    )

    # A blank query returns the curated initial view, not all ~2,100 names.
    assert [k for k, _l in search_icon_keys("")] == list(DEFAULT_ICON_PICKER)
    # Punctuation-only queries carry no words: no matches (not the picker).
    assert search_icon_keys("!!!") == []


def test_search_icon_keys_ranks_exact_name_first():
    from homestew.services.device_icons import search_icon_keys

    keys = [k for k, _l in search_icon_keys("fan")]
    assert keys[0] == "fan"  # exact name beats fan-tastic longer names


def test_search_icons_action_reports_matches_for_the_model(db_env):
    r = run({"action": "search_icons", "query": "coffee"})
    assert "icon(s) match" in r
    assert "coffee" in r


def test_search_icons_action_finds_partial_names(db_env):
    r = run({"action": "search_icons", "query": "washing"})
    assert "washing-machine" in r


def test_search_icons_action_no_match_points_at_simpler_words(db_env):
    r = run({"action": "search_icons", "query": "spaceship"})
    assert "No icon matches" in r
    # The fallback teaches the model how to search again.
    assert "Lucide" in r


def test_search_icons_action_empty_query_asks_for_words(db_env):
    r = run({"action": "search_icons"})
    assert r.startswith("Pass a 'query'")
