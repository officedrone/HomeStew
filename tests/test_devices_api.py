"""Tests for the devices API's ``icon`` field (per-device icons feature).

Covers the HTTP layer that test_device_tool.py does not: the icon key
round-trips through POST/GET/PUT, absent = null (default glyph), invalid
keys are rejected by the schema pattern, and ``init_db()`` migrates a
legacy devices table that predates the column.

Fixture pattern copied from test_search_index_jobs.py: a signed-in
TestClient runs the lifespan (init_db) on a throwaway DATA_DIR so the
auth-gated endpoints are reachable.

Run from the repo root:  pytest tests/test_devices_api.py -q
"""
import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from homestew import config as cfg
from homestew.db import init_db
from homestew.services import auth


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated data dir + no password + empty auth caches (test_auth pattern)."""
    monkeypatch.setattr(cfg.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg.settings, "DEVICES_DIR", tmp_path / "devices")
    monkeypatch.setattr(cfg.settings, "PASSWORD_HASH", "")
    auth._failures.clear()
    auth._pw_cache.update({"mtime": None, "hash": ""})
    yield tmp_path
    auth._failures.clear()
    auth._pw_cache.update({"mtime": None, "hash": ""})


@pytest.fixture()
def client(env):
    """Signed-in TestClient (forced account creation unlocks the API)."""
    from homestew.main import app

    with TestClient(app) as c:
        r = c.post("/api/auth/setup", json={
            "password": "icon-test-password",
            "confirm_password": "icon-test-password",
        })
        assert r.status_code == 201
        yield c


def _device_payload(**overrides):
    payload = {"name": "Fridge", "brand": "LG", "model": "GML18"}
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# icon round-trip through the REST API
# ---------------------------------------------------------------------------

def test_create_with_icon_round_trips_in_list_and_detail(client):
    r = client.post("/api/devices", json=_device_payload(icon="refrigerator"))
    assert r.status_code == 201
    created = r.json()
    assert created["icon"] == "refrigerator"

    listed = client.get("/api/devices").json()
    assert [d["icon"] for d in listed if d["id"] == created["id"]] == ["refrigerator"]

    detail = client.get(f"/api/devices/{created['id']}")
    assert detail.status_code == 200
    assert detail.json()["icon"] == "refrigerator"


def test_create_without_icon_defaults_to_null(client):
    r = client.post("/api/devices", json=_device_payload())
    assert r.status_code == 201
    assert r.json()["icon"] is None
    # The frontend renders the default appliance glyph for null.
    listed = client.get("/api/devices").json()
    assert listed[0]["icon"] is None


def test_update_sets_and_clears_icon(client):
    did = client.post("/api/devices", json=_device_payload()).json()["id"]

    r = client.put(f"/api/devices/{did}", json=_device_payload(icon="tv"))
    assert r.status_code == 200
    assert r.json()["icon"] == "tv"

    # Omitting the field (null) clears it back to the default glyph.
    r = client.put(f"/api/devices/{did}", json=_device_payload())
    assert r.status_code == 200
    assert r.json()["icon"] is None


def test_update_icon_does_not_disturb_other_fields(client):
    did = client.post(
        "/api/devices",
        json=_device_payload(icon="laptop", serial_number="SN-42"),
    ).json()["id"]

    # PUT is a full replace (the frontend rebuilds the whole payload from the
    # cached device when only the icon changes), so resend the other fields.
    r = client.put(
        f"/api/devices/{did}",
        json=_device_payload(icon="phone", serial_number="SN-42"),
    )
    body = r.json()
    assert body["icon"] == "phone"
    assert body["serial_number"] == "SN-42"


# ---------------------------------------------------------------------------
# schema validation (pattern ^[a-z0-9_-]+$)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["Fridge", "bad icon", "fridge!", "a/b"])
def test_invalid_icon_keys_rejected_with_422(client, bad):
    r = client.post("/api/devices", json=_device_payload(icon=bad))
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/devices/icons - the selectable icon catalog + search
# ---------------------------------------------------------------------------

def test_icons_endpoint_lists_curated_picker_when_blank(client):
    r = client.get("/api/devices/icons")
    assert r.status_code == 200
    icons = r.json()
    # A blank query returns the curated initial view (not all ~2,100 names).
    keys = [i["key"] for i in icons]
    assert len(keys) == len(set(keys))
    assert {"key": "plug", "label": "Plug"} in icons
    assert "refrigerator" in keys and "washing-machine" in keys


def test_icons_endpoint_filters_by_query(client):
    r = client.get("/api/devices/icons", params={"q": "coffee"})
    assert r.status_code == 200
    keys = [i["key"] for i in r.json()]
    assert "coffee" in keys
    # The AND semantics keep unrelated icons out.
    assert "refrigerator" not in keys


def test_icons_endpoint_matches_partial_names(client):
    # Partial match: 'washing' is a substring of washing-machine's name, so
    # users never need the exact icon name.
    keys = [i["key"] for i in client.get("/api/devices/icons", params={"q": "washing"}).json()]
    assert "washing-machine" in keys


def test_icons_endpoint_unknown_query_returns_empty_list(client):
    r = client.get("/api/devices/icons", params={"q": "spaceship"})
    assert r.status_code == 200
    assert r.json() == []


def test_icons_route_wins_over_device_id_route(client):
    # "/icons" must resolve to the catalog, not to get_device(device_id="icons").
    r = client.get("/api/devices/icons")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ---------------------------------------------------------------------------
# init_db migration: legacy devices table without the icon column
# ---------------------------------------------------------------------------

def test_init_db_adds_icon_column_to_legacy_table(env):
    db_path = env / "homestew.db"
    conn = sqlite3.connect(db_path)
    # Pre-feature schema: no icon (and no warranty/audit columns either).
    conn.execute(
        """
        CREATE TABLE devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            brand TEXT NOT NULL,
            model TEXT NOT NULL,
            description TEXT,
            serial_number TEXT,
            product_number TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO devices (name, brand, model) VALUES ('Old', 'Brand', 'M1')"
    )
    conn.commit()
    conn.close()

    asyncio.run(init_db())

    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    assert "icon" in cols
    row = conn.execute("SELECT icon FROM devices WHERE name = 'Old'").fetchone()
    assert row == (None,)  # existing rows keep NULL = default glyph
    conn.close()


# ---------------------------------------------------------------------------
# init_db migration: orphaned pre-Lucide icon keys are cleared
# ---------------------------------------------------------------------------

def test_init_db_clears_orphaned_icon_keys(env):
    """Curated keys ('fridge', 'washer') from before the Lucide switch are
    not renderable, so a startup migration nulls them -> default glyph.
    Valid Lucide names survive untouched."""
    db_path = env / "homestew.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            brand TEXT NOT NULL,
            model TEXT NOT NULL,
            description TEXT,
            serial_number TEXT,
            product_number TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            icon TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO devices (name, brand, model, icon) VALUES "
        "('Old', 'B', 'M1', 'fridge'), ('New', 'B', 'M2', 'washing-machine')"
    )
    conn.commit()
    conn.close()

    asyncio.run(init_db())

    conn = sqlite3.connect(db_path)
    rows = dict(conn.execute("SELECT name, icon FROM devices").fetchall())
    assert rows["Old"] is None            # orphaned curated key cleared
    assert rows["New"] == "washing-machine"  # valid Lucide name kept
    conn.close()

    # Idempotent: a second init_db changes nothing.
    asyncio.run(init_db())
    conn = sqlite3.connect(db_path)
    rows = dict(conn.execute("SELECT name, icon FROM devices").fetchall())
    assert rows["Old"] is None
    assert rows["New"] == "washing-machine"
    conn.close()
