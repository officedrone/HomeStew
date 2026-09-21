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
    assert "(#edit-device-1)" in report
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
