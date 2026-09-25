"""Tests for search results: unbounded fetch + device labelling.

The Search UI paginates client-side, so POST /api/search must return EVERY
match (no limit) with a truthful total_results, and every result must carry
the name of the device it belongs to ("Relevant Device URL:" footer). The
chat tool keeps its fixed budget by calling search_manuals(limit=10)
directly - covered here too.

Same fixture pattern as test_auth.py: TestClient runs the lifespan (init_db)
on a throwaway DATA_DIR; /api/search is auth-gated, so the client signs up
through the forced-setup endpoint first. pdf_index rows are inserted
directly (same columns indexer.py writes) - no real PDFs needed.

Run from the repo root:  pytest tests/test_search_pagination.py -q
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from homestew import config as cfg
from homestew.db import get_db_context, init_db
from homestew.services import auth
from homestew.services.search_engine import search_manuals

# Distinctive whole-word term: qualifies under the trigram + word-boundary
# rules without colliding with filler text.
TERM = "calibration"


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
            "password": "search-test-password",
            "confirm_password": "search-test-password",
        })
        assert r.status_code == 201
        yield c


async def _seed():
    """Two devices; device 1 owns a manual with many TERM-bearing pages.

    Device 2 is named so that its own record matches the query too, which
    produces a device-entry hit (manual_id=0) alongside the page results.
    """
    async with get_db_context() as db:
        await db.execute(
            "INSERT INTO devices (name, brand, model) VALUES (?, ?, ?)",
            ("Oven", "Acme", "OV-100"),
        )
        await db.execute(
            "INSERT INTO devices (name, brand, model) VALUES (?, ?, ?)",
            ("Calibration Probe", "Beta", "CP-2"),
        )
        await db.execute(
            "INSERT INTO manuals (device_id, filename, filepath, page_count) "
            "VALUES (?, ?, ?, ?)",
            (1, "oven-manual.pdf", "/data/devices/1/manuals/oven-manual.pdf", 14),
        )
        for page in range(1, 15):
            await db.execute(
                "INSERT INTO pdf_index "
                "(device_id, manual_id, filename, page_number, content) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    1, 1, "oven-manual.pdf", page,
                    f"Page {page} of the oven manual. The {TERM} procedure "
                    f"starts by unplugging the unit and removing the rear panel.",
                ),
            )
        # Filler pages without the term keep its document frequency well
        # under COMMON_TERM_DF_RATIO - a term on every page would be treated
        # as filler noise instead of a real match.
        for page in range(15, 45):
            await db.execute(
                "INSERT INTO pdf_index "
                "(device_id, manual_id, filename, page_number, content) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    1, 1, "oven-manual.pdf", page,
                    f"Page {page} of the oven manual. Cleaning instructions: "
                    "wipe the interior with a damp cloth after it cools down.",
                ),
            )
        await db.commit()


def seed():
    asyncio.run(_seed())


def search(client, **body):
    r = client.post("/api/search", json={"query": TERM, **body})
    assert r.status_code == 200
    return r.json()


# ---------------------------------------------------------------------------
# API: unbounded results + truthful totals (client-side pagination contract)
# ---------------------------------------------------------------------------

def test_search_without_limit_returns_every_match(client):
    seed()
    data = search(client)
    # 14 manual pages + the "Calibration Probe" device entry.
    assert data["total_results"] == len(data["results"])
    assert data["total_results"] >= 12


def test_explicit_limit_still_caps(client):
    seed()
    data = search(client, limit=3)
    assert len(data["results"]) == 3


def test_every_result_carries_its_device_name(client):
    seed()
    data = search(client)
    names = {r["device_id"]: r["device_name"] for r in data["results"]}
    assert names[1] == "Oven"
    # Device-entry hits (manual_id=0) are labelled too.
    entries = [r for r in data["results"] if r["manual_id"] == 0]
    assert entries and all(r["device_name"] == "Calibration Probe" for r in entries)


def test_device_filter_scopes_results_and_names(client):
    seed()
    data = search(client, device_id=1)
    assert all(r["device_id"] == 1 for r in data["results"])
    assert all(r["device_name"] == "Oven" for r in data["results"])


def test_empty_query_is_rejected(client):
    seed()
    assert client.post("/api/search", json={"query": "   "}).status_code == 400


# ---------------------------------------------------------------------------
# Service: the chat tool's fixed budget is unaffected by the API change
# ---------------------------------------------------------------------------

def test_service_limit_default_and_none(env):
    # No TestClient here, so the lifespan never ran - create the schema.
    asyncio.run(init_db())
    seed()
    capped = asyncio.run(search_manuals(TERM, None, limit=10))
    assert len(capped) == 10
    everything = asyncio.run(search_manuals(TERM, None, limit=None))
    assert len(everything) > len(capped)
    assert all(r.device_name for r in everything)
