"""Tests for the device-categories feature (API + FK-nulling on delete).

Covers the HTTP surface added for the Devices-page sections: category CRUD,
the reorder endpoints (devices and categories), devices carrying their
category id/name through list/detail, moving a device between buckets, and
the load-bearing rule that deleting a category NULLs its devices instead of
deleting them (runtime connections do not enable the FK pragma, so this is
done explicitly in the endpoint - see api/categories.py).

Fixture pattern copied from test_devices_api.py: a signed-in TestClient runs
the lifespan (init_db) on a throwaway DATA_DIR.

Run from the repo root:  pytest tests/test_categories_api.py -q
"""
import pytest
from fastapi.testclient import TestClient

from homestew import config as cfg
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
            "password": "cat-test-password",
            "confirm_password": "cat-test-password",
        })
        assert r.status_code == 201
        yield c


def _mk_category(client, name):
    r = client.post("/api/categories", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _mk_device(client, name, **overrides):
    payload = {"name": name, "brand": "Acme", "model": "M1"}
    payload.update(overrides)
    r = client.post("/api/devices", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# --- category CRUD ---------------------------------------------------------

def test_create_and_list_categories_in_order(client):
    kitchen = _mk_category(client, "Kitchen")
    tools = _mk_category(client, "Tools")
    # Created at the end of the list, so sort_order ascends with creation.
    listed = client.get("/api/categories").json()
    assert [c["id"] for c in listed] == [kitchen["id"], tools["id"]]
    assert [c["name"] for c in listed] == ["Kitchen", "Tools"]
    # A brand-new category has no devices yet.
    assert all(c["device_count"] == 0 for c in listed)


def test_category_name_is_trimmed_and_blank_rejected(client):
    created = _mk_category(client, "  Garage  ")
    assert created["name"] == "Garage"
    r = client.post("/api/categories", json={"name": "   "})
    assert r.status_code == 422


def test_duplicate_category_name_conflicts(client):
    _mk_category(client, "Kitchen")
    r = client.post("/api/categories", json={"name": "Kitchen"})
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


def test_rename_category_and_conflict(client):
    cat = _mk_category(client, "Tools")
    other = _mk_category(client, "Garden")
    r = client.put(f"/api/categories/{cat['id']}", json={"name": "Workshop"})
    assert r.status_code == 200
    assert r.json()["name"] == "Workshop"
    # Renaming onto an existing name is rejected.
    r = client.put(f"/api/categories/{other['id']}", json={"name": "Workshop"})
    assert r.status_code == 409


def test_update_missing_category_is_404(client):
    r = client.put("/api/categories/999", json={"name": "Nope"})
    assert r.status_code == 404


# --- devices carry their category ------------------------------------------

def test_device_create_with_category_round_trips(client):
    cat = _mk_category(client, "Kitchen")
    dev = _mk_device(client, "Fridge", category_id=cat["id"])
    assert dev["category_id"] == cat["id"]
    assert dev["category_name"] == "Kitchen"

    listed = client.get("/api/devices").json()
    row = next(d for d in listed if d["id"] == dev["id"])
    assert row["category_id"] == cat["id"]
    assert row["category_name"] == "Kitchen"


def test_device_without_category_is_uncategorized(client):
    dev = _mk_device(client, "Mystery Box")
    assert dev["category_id"] is None
    assert dev["category_name"] is None


def test_category_device_count_reflects_membership(client):
    cat = _mk_category(client, "Kitchen")
    _mk_device(client, "Fridge", category_id=cat["id"])
    _mk_device(client, "Oven", category_id=cat["id"])
    listed = client.get("/api/categories").json()
    row = next(c for c in listed if c["id"] == cat["id"])
    assert row["device_count"] == 2


# --- moving devices between buckets ----------------------------------------

def test_move_device_category_endpoint(client):
    kitchen = _mk_category(client, "Kitchen")
    tools = _mk_category(client, "Tools")
    dev = _mk_device(client, "Drill", category_id=kitchen["id"])

    r = client.put(f"/api/devices/{dev['id']}/category", json={"category_id": tools["id"]})
    assert r.status_code == 204
    detail = client.get(f"/api/devices/{dev['id']}").json()
    assert detail["category_id"] == tools["id"]

    # Null moves it back to Uncategorized.
    r = client.put(f"/api/devices/{dev['id']}/category", json={"category_id": None})
    assert r.status_code == 204
    assert client.get(f"/api/devices/{dev['id']}").json()["category_id"] is None


def test_reorder_devices_sets_bucket_order_and_membership(client):
    cat = _mk_category(client, "Kitchen")
    a = _mk_device(client, "A", category_id=cat["id"])
    b = _mk_device(client, "B", category_id=cat["id"])
    c = _mk_device(client, "C")  # starts uncategorized

    # Drag C into the Kitchen bucket between A and B.
    r = client.put("/api/devices/reorder", json={
        "category_id": cat["id"],
        "device_ids": [a["id"], c["id"], b["id"]],
    })
    assert r.status_code == 204

    detail_c = client.get(f"/api/devices/{c['id']}").json()
    assert detail_c["category_id"] == cat["id"]

    # The Kitchen bucket now lists A, C, B in that order (sort_order asc).
    listed = [d["id"] for d in client.get("/api/devices").json()
              if d["category_id"] == cat["id"]]
    assert listed == [a["id"], c["id"], b["id"]]


def test_reorder_devices_rejects_unknown_id(client):
    r = client.put("/api/devices/reorder", json={"category_id": None, "device_ids": [999]})
    assert r.status_code == 400


def test_reorder_categories_sets_section_order(client):
    a = _mk_category(client, "A")
    b = _mk_category(client, "B")
    c = _mk_category(client, "C")
    r = client.put("/api/categories/reorder", json={"category_ids": [c["id"], a["id"], b["id"]]})
    assert r.status_code == 204
    listed = client.get("/api/categories").json()
    assert [cat["id"] for cat in listed] == [c["id"], a["id"], b["id"]]


def test_reorder_categories_rejects_unknown_id(client):
    _mk_category(client, "A")
    r = client.put("/api/categories/reorder", json={"category_ids": [999]})
    assert r.status_code == 400


# --- delete nulls devices (the FK-pragma-off rule) --------------------------

def test_delete_category_moves_devices_to_uncategorized(client):
    cat = _mk_category(client, "Kitchen")
    dev = _mk_device(client, "Fridge", category_id=cat["id"])

    r = client.delete(f"/api/categories/{cat['id']}")
    assert r.status_code == 204

    # The device survives and becomes uncategorized (never deleted).
    detail = client.get(f"/api/devices/{dev['id']}")
    assert detail.status_code == 200
    assert detail.json()["category_id"] is None
    assert detail.json()["category_name"] is None


def test_delete_missing_category_is_404(client):
    r = client.delete("/api/categories/999")
    assert r.status_code == 404
