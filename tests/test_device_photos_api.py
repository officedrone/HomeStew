"""Tests for the device-photos API: upload validation, CRUD, file serving.

Photos are stored under ``data/devices/<id>/photos/``; uploads are validated
by magic bytes (no Pillow), dimension/size limits come from settings, and
deleting a device removes its whole directory. Fixture pattern copied from
test_devices_api.py.

Run from the repo root:  pytest tests/test_device_photos_api.py -q
"""
import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from homestew import config as cfg
from homestew.services.system import auth


def _png(width=8, height=6):
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture()
def env(tmp_path, monkeypatch):
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
    from homestew.main import app

    with TestClient(app) as c:
        r = c.post("/api/auth/setup", json={
            "password": "photo-test-password",
            "confirm_password": "photo-test-password",
        })
        assert r.status_code == 201
        yield c


def test_photo_lifecycle(client):
    dev = client.post("/api/devices", json={"name": "HVAC", "brand": "Trane", "model": "X"}).json()
    did = dev["id"]
    assert dev["photo_count"] == 0 and dev["unanalyzed_photo_count"] == 0

    # upload
    r = client.post(
        f"/api/devices/{did}/photos",
        files={"file": ("Filter Photo.png", _png(), "image/png")},
        data={"label": "Filter", "notes": "20x25x1", "analyze": "false"},
    )
    assert r.status_code == 201, r.text
    photo = r.json()
    assert photo["mime_type"] == "image/png"
    assert photo["width"] == 8 and photo["height"] == 6
    assert photo["label"] == "Filter"
    pid = photo["id"]

    # counts on device
    dev2 = client.get(f"/api/devices/{did}").json()
    assert dev2["photo_count"] == 1 and dev2["unanalyzed_photo_count"] == 1
    lst = client.get("/api/devices").json()
    assert [d for d in lst if d["id"] == did][0]["photo_count"] == 1

    # list
    photos = client.get(f"/api/devices/{did}/photos").json()
    assert len(photos) == 1 and photos[0]["id"] == pid

    # file serving
    fr = client.get(f"/api/devices/{did}/photos/{pid}/file")
    assert fr.status_code == 200
    assert fr.headers["content-type"] == "image/png"
    assert fr.content.startswith(b"\x89PNG")

    # update label/notes
    ur = client.put(f"/api/devices/{did}/photos/{pid}", json={"label": "Return filter", "notes": "MERV 13"})
    assert ur.status_code == 200 and ur.json()["label"] == "Return filter"

    # suggestion decision with nothing pending -> 404
    sr = client.post(f"/api/devices/{did}/photos/{pid}/suggestion", json={"accept": True})
    assert sr.status_code == 404

    # reject non-image
    bad = client.post(
        f"/api/devices/{did}/photos",
        files={"file": ("evil.txt", b"just text", "image/png")},
    )
    assert bad.status_code == 400

    # delete photo removes file + row
    dr = client.delete(f"/api/devices/{did}/photos/{pid}")
    assert dr.status_code == 204
    assert client.get(f"/api/devices/{did}/photos").json() == []
    assert client.get(f"/api/devices/{did}/photos/{pid}/file").status_code == 404


def test_delete_device_removes_dir(env, client):
    dev = client.post("/api/devices", json={"name": "Dryer", "brand": "LG", "model": "D1"}).json()
    did = dev["id"]
    r = client.post(
        f"/api/devices/{did}/photos",
        files={"file": ("lint.jpg", _png(), "image/jpeg")},
    )
    assert r.status_code == 201, r.text
    device_dir = env / "devices" / str(did)
    assert device_dir.is_dir()
    assert client.delete(f"/api/devices/{did}").status_code == 204
    assert not device_dir.exists()
