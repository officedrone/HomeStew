"""Tests for background search-index jobs and the Settings > Search options.

Covers the feature set added around the shared indexing progress state:

* ``GET /api/search/index-status`` reports the current/last job (running,
  done, total, failed, current_file) from the single module-global in
  ``services/indexer.py`` that every long-running index job shares.
* ``POST /api/search/reindex`` upserts every registered manual and returns
  202 immediately; a second request while one is in flight gets 409.
* ``POST /api/search/rebuild`` drops + recreates the FTS table first, so it
  also purges stale rows left behind by deleted manuals (reindex does not).
* The two new Settings > Search options round-trip through GET/PUT and are
  persisted to settings.json: ``index_max_chunk_size`` (applied live by the
  indexer) and ``index_auto_on_upload`` (gates upload/fetch indexing).

Fixture pattern is copied from test_search_pagination.py / test_auth.py: a
signed-in TestClient runs the lifespan (init_db) on a throwaway DATA_DIR, so
the auth-gated endpoints are reachable. There is no async pytest plugin in
this repo - async code runs through ``asyncio.run()`` and background jobs are
polled to completion with short sleeps while TestClient's portal loop keeps
running in its own thread.

Run from the repo root:  pytest tests/test_search_index_jobs.py -q
"""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from homestew import config as cfg
from homestew.api import devices as devices_api
from homestew.api import search as search_api
from homestew.db import get_db_context, init_db
from homestew.services import auth, indexer


@pytest.fixture(autouse=True)
def _reset_index_state():
    """Isolate the shared progress dict so tests never see a stale job."""
    indexer._INDEX_STATE.update(
        {"running": False, "done": 0, "total": 0, "failed": 0, "current_file": ""}
    )
    yield
    indexer._INDEX_STATE.update(
        {"running": False, "done": 0, "total": 0, "failed": 0, "current_file": ""}
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated data dir + no password + empty auth caches (test_auth pattern)."""
    monkeypatch.setattr(cfg.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg.settings, "DEVICES_DIR", tmp_path / "devices")
    monkeypatch.setattr(cfg.settings, "PASSWORD_HASH", "")
    # Some tests assign the index options straight on the live singleton
    # (or PUT them through the API, which applies changes globally), so
    # snapshot and restore them here - otherwise a later test asserting the
    # defaults fails depending on execution order.
    orig_chunk = cfg.settings.INDEX_MAX_CHUNK_SIZE
    orig_auto = cfg.settings.INDEX_AUTO_ON_UPLOAD
    auth._failures.clear()
    auth._pw_cache.update({"mtime": None, "hash": ""})
    yield tmp_path
    auth._failures.clear()
    cfg.settings.INDEX_MAX_CHUNK_SIZE = orig_chunk
    cfg.settings.INDEX_AUTO_ON_UPLOAD = orig_auto
    auth._pw_cache.update({"mtime": None, "hash": ""})


@pytest.fixture()
def db_env(env):
    """Create the schema (incl. pdf_index) on the throwaway DATA_DIR."""
    asyncio.run(init_db())
    return env


@pytest.fixture()
def client(env):
    """Signed-in TestClient (forced account creation unlocks the API)."""
    from homestew.main import app

    with TestClient(app) as c:
        r = c.post("/api/auth/setup", json={
            "password": "index-test-password",
            "confirm_password": "index-test-password",
        })
        assert r.status_code == 201
        yield c


def _wait_idle(client, timeout=5.0):
    """Poll index-status until no job is running (portal loop runs in a thread)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get("/api/search/index-status").json()["running"] is False:
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# index-status endpoint shape (idle on a fresh install)
# ---------------------------------------------------------------------------

def test_index_status_reports_idle_state(client):
    st = client.get("/api/search/index-status").json()
    assert set(st) == {"running", "done", "total", "failed", "current_file"}
    assert st["running"] is False
    assert st["done"] == 0 and st["total"] == 0 and st["failed"] == 0
    assert st["current_file"] == ""


# ---------------------------------------------------------------------------
# reindex: queue -> background job -> progress reaches completion, searchable
# ---------------------------------------------------------------------------

def test_reindex_processes_manuals_and_reports_progress(client, monkeypatch):
    async def seed():
        async with get_db_context() as db:
            cur = await db.execute(
                "INSERT INTO manuals (device_id, filename, filepath) "
                "VALUES (1, 'm.pdf', '/nope/m.pdf') RETURNING id"
            )
            mid = (await cur.fetchone())["id"]
            await db.commit()
        return mid

    asyncio.run(seed())
    # The queued job reads the manual's PDF; stub extraction so no real file
    # is needed and the page text is distinctive for the follow-up search.
    monkeypatch.setattr(
        indexer, "extract_pages_from_pdf", lambda p: ["calibration valve torque spec"]
    )

    r = client.post("/api/search/reindex")
    assert r.status_code == 202
    body = r.json()
    assert body["queued"] == 1 and body["rebuild"] is False

    assert _wait_idle(client), "background re-index did not finish"
    st = client.get("/api/search/index-status").json()
    assert st["running"] is False
    assert st["done"] == 1 and st["failed"] == 0

    # The manual's content is now searchable.
    s = client.post("/api/search", json={"query": "calibration"})
    assert s.status_code == 200
    results = s.json()["results"]
    assert any(res.get("filename") == "m.pdf" for res in results)


# ---------------------------------------------------------------------------
# rebuild: drops the table, purging rows of manuals that no longer exist
# ---------------------------------------------------------------------------

def test_rebuild_purges_rows_of_deleted_manuals(client, monkeypatch):
    monkeypatch.setattr(
        indexer, "extract_pages_from_pdf", lambda p: ["widgetron flux capacitor"]
    )

    async def seed_index_then_delete():
        async with get_db_context() as db:
            cur = await db.execute(
                "INSERT INTO manuals (device_id, filename, filepath) "
                "VALUES (1, 'w.pdf', '/nope/w.pdf') RETURNING id"
            )
            mid = (await cur.fetchone())["id"]
            await db.commit()
        await indexer.index_manual(
            manual_id=mid, device_id=1, pdf_path="/nope/w.pdf", filename="w.pdf"
        )
        # Manual deleted but its indexed rows remain behind (stale).
        async with get_db_context() as db:
            await db.execute("DELETE FROM manuals WHERE id = ?", (mid,))
            await db.commit()

    asyncio.run(seed_index_then_delete())

    # The orphaned row still matches before the rebuild.
    assert client.post("/api/search", json={"query": "widgetron"}).json()["total_results"] >= 1

    r = client.post("/api/search/rebuild")
    assert r.status_code == 202
    assert r.json()["rebuild"] is True
    assert _wait_idle(client)

    # After a rebuild the orphaned rows are gone (nothing left to re-index).
    assert client.post("/api/search", json={"query": "widgetron"}).json()["total_results"] == 0


# ---------------------------------------------------------------------------
# concurrency: a second request while one is in flight is refused (409)
# ---------------------------------------------------------------------------

def test_reindex_conflict_when_job_running(client, monkeypatch):
    # Force the queue helper to report "already running" so both endpoints
    # take their 409 branch deterministically.
    monkeypatch.setattr(search_api, "start_background_reindex", lambda *a, **k: -1)

    r = client.post("/api/search/reindex")
    assert r.status_code == 409
    r2 = client.post("/api/search/rebuild")
    assert r2.status_code == 409


def test_concurrent_index_requests_are_refused():
    # The slot is claimed synchronously (no await between check and set), so a
    # second call in the same tick cannot also start. Empty items => no DB use.
    async def scenario():
        n1 = indexer.start_background_reindex(items=[])   # claims the slot
        n2 = indexer.start_background_reindex(items=[])   # must be refused
        running_during = indexer.get_index_status()["running"]
        await asyncio.sleep(0.05)                          # let the empty job clear it
        return n1, n2, running_during, indexer.get_index_status()["running"]

    n1, n2, during, after = asyncio.run(scenario())
    assert n1 == 0 and n2 == -1
    assert during is True and after is False


# ---------------------------------------------------------------------------
# Settings > Search: chunk size applied live by the indexer
# ---------------------------------------------------------------------------

def test_index_chunk_size_setting_controls_row_count(db_env, monkeypatch):
    # One long page; a small chunk size splits it into many indexed rows, a
    # large one keeps it as a single row. index_manual reads the setting at run
    # time (max(200, ...)), so no restart is needed for a save to apply.
    long_page = "calibration procedure step detail " * 200  # ~7k chars, one page
    monkeypatch.setattr(indexer, "extract_pages_from_pdf", lambda p: [long_page])

    async def run(chunk_size):
        cfg.settings.INDEX_MAX_CHUNK_SIZE = chunk_size
        assert await indexer.index_manual(
            manual_id=1, device_id=7, pdf_path="ignored.pdf", filename="chunky.pdf"
        ) is True
        async with get_db_context() as db:
            cur = await db.execute(
                "SELECT COUNT(*) AS c FROM pdf_index WHERE manual_id = 1"
            )
            return (await cur.fetchone())["c"]

    small = asyncio.run(run(200))   # floor clamp -> many chunks
    large = asyncio.run(run(8000))  # whole page fits one chunk
    assert large == 1
    assert small > large


# ---------------------------------------------------------------------------
# Settings > Search: options round-trip through GET/PUT and persist
# ---------------------------------------------------------------------------

def test_settings_expose_and_persist_index_options(client):
    g = client.get("/api/settings").json()
    assert g["index_max_chunk_size"] == 4000
    assert g["index_auto_on_upload"] is True

    u = client.put(
        "/api/settings",
        json={"index_max_chunk_size": 1500, "index_auto_on_upload": False},
    )
    assert u.status_code == 200
    body = u.json()
    assert body["index_max_chunk_size"] == 1500
    assert body["index_auto_on_upload"] is False

    # Persisted to settings.json on the data volume (survives restart).
    saved = json.loads((cfg.settings.DATA_DIR / "settings.json").read_text(encoding="utf-8"))
    assert saved["INDEX_MAX_CHUNK_SIZE"] == 1500
    assert saved["INDEX_AUTO_ON_UPLOAD"] is False

    # Live singleton updated too (the indexer reads it at run time).
    assert cfg.settings.INDEX_MAX_CHUNK_SIZE == 1500
    assert cfg.settings.INDEX_AUTO_ON_UPLOAD is False


def test_index_chunk_size_out_of_range_rejected(client):
    assert client.put("/api/settings", json={"index_max_chunk_size": 50}).status_code == 422
    assert client.put("/api/settings", json={"index_max_chunk_size": 99999}).status_code == 422


# ---------------------------------------------------------------------------
# auto-index toggle gates whether an upload indexes the manual
# ---------------------------------------------------------------------------

def test_upload_skips_indexing_when_auto_off(client, monkeypatch):
    calls = []

    async def fake_index(**kwargs):
        calls.append(kwargs)
        return True

    # devices.py imports index_manual directly; patch that reference so we can
    # observe whether the upload path invoked it (no real PDF parsing).
    monkeypatch.setattr(devices_api, "index_manual", fake_index)

    dev = client.post(
        "/api/devices", json={"name": "Boiler", "brand": "Bosch", "model": "GB1"}
    )
    assert dev.status_code == 201
    did = dev.json()["id"]

    # Auto-index ON (default): the upload indexes the manual.
    r_on = client.post(
        f"/api/devices/{did}/manuals/upload",
        files={"file": ("m1.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")},
    )
    assert r_on.status_code == 201
    assert len(calls) == 1

    # Auto-index OFF: the upload stores the manual but does not index it.
    s = client.put("/api/settings", json={"index_auto_on_upload": False})
    assert s.status_code == 200 and s.json()["index_auto_on_upload"] is False

    r_off = client.post(
        f"/api/devices/{did}/manuals/upload",
        files={"file": ("m2.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")},
    )
    assert r_off.status_code == 201
    assert len(calls) == 1  # no additional index call
