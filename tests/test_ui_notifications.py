"""Tests for the in-app notification framework (condition checks -> toasts).

Covers the feature added around ``services/ui_notifications.py``:

* ``GET /api/notifications/ui`` evaluates due checks and returns active items;
  a manual with zero ``pdf_index`` rows produces the ``unindexed_manuals`` item
  pointing at Settings > Search.
* Per-check interval gating: a check re-evaluates only once its own
  ``interval_minutes`` elapsed (verified with an evaluator call counter).
* Episode-based dismissal: Dismiss silences the current occurrence across
  forced re-evaluations, and the item returns once the condition goes false
  and later true again.
* Evaluation is suppressed while a background index job runs, without
  advancing ``last_checked_at`` (so it fires right after the job ends).
* ``POST /api/notifications/ui/{key}/check`` forces one immediate evaluation;
  unknown keys are 404.
* The per-check settings round-trip through GET/PUT ``/api/settings`` and are
  persisted to settings.json; an out-of-range interval is rejected with 422.

Fixture pattern copied from tests/test_search_index_jobs.py: a signed-in
TestClient runs the lifespan (init_db) on a throwaway DATA_DIR. Async service
functions are driven through ``asyncio.run()`` where a direct call is wanted.

Run from the repo root:  pytest tests/test_ui_notifications.py -q
"""
import asyncio
import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from homestew import config as cfg
from homestew.db import get_db_context, init_db
from homestew.services import auth, indexer, ui_notifications


@pytest.fixture(autouse=True)
def _no_index_job():
    """The shared index state must read idle, or collect() skips evaluation."""
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
    # PUT /api/settings applies changes to the live singleton globally, so
    # snapshot and restore the notification overrides - otherwise a later test
    # sees an earlier one's interval/disabled choices depending on order.
    orig = dict(cfg.settings.UI_NOTIFICATIONS)
    auth._failures.clear()
    auth._pw_cache.update({"mtime": None, "hash": ""})
    yield tmp_path
    cfg.settings.UI_NOTIFICATIONS = orig
    auth._failures.clear()
    auth._pw_cache.update({"mtime": None, "hash": ""})


@pytest.fixture()
def db_env(env):
    """Create the schema (incl. ui_notification_state) on the throwaway dir."""
    asyncio.run(init_db())
    return env


@pytest.fixture()
def client(db_env):
    """Signed-in TestClient (forced account creation unlocks the API)."""
    from homestew.main import app

    with TestClient(app) as c:
        r = c.post("/api/auth/setup", json={
            "password": "notify-test-password",
            "confirm_password": "notify-test-password",
        })
        assert r.status_code == 201
        yield c


async def _seed_manual(filename="m.pdf", indexed=False):
    """Insert a manual (optionally with one pdf_index row). Returns its id."""
    async with get_db_context() as db:
        cur = await db.execute(
            "INSERT INTO manuals (device_id, filename, filepath) "
            "VALUES (1, ?, '/nope/m.pdf') RETURNING id",
            (filename,),
        )
        mid = (await cur.fetchone())["id"]
        if indexed:
            await db.execute(
                "INSERT INTO pdf_index (device_id, manual_id, filename, "
                "page_number, content) VALUES (1, ?, ?, 1, 'boiler text')",
                (mid, filename),
            )
        await db.commit()
    return mid


def seed_manual(*a, **kw):
    return asyncio.run(_seed_manual(*a, **kw))


async def _index_manual(manual_id, filename="m.pdf"):
    """Give a manual one indexed page - the condition goes false."""
    async with get_db_context() as db:
        await db.execute(
            "INSERT INTO pdf_index (device_id, manual_id, filename, "
            "page_number, content) VALUES (1, ?, ?, 1, 'boiler text')",
            (manual_id, filename),
        )
        await db.commit()


def _keys(payload):
    return [i["key"] for i in payload["items"]]


# ---------------------------------------------------------------------------
# Fresh install: nothing to report
# ---------------------------------------------------------------------------

def test_fresh_install_has_no_notifications(client):
    body = client.get("/api/notifications/ui").json()
    assert body["items"] == []
    assert body["checked_at"]


# ---------------------------------------------------------------------------
# The first check: un-indexed manuals
# ---------------------------------------------------------------------------

def test_unindexed_manual_triggers_notification(client):
    seed_manual()
    body = client.get("/api/notifications/ui").json()
    assert _keys(body) == ["unindexed_manuals"]
    item = body["items"][0]
    assert item["count"] == 1
    assert item["severity"] == "warn"
    # Go To target: Settings > Search, where the Re-index buttons live.
    assert item["goto_tab"] == "settings"
    assert item["goto_section"] == "search"
    assert "search index" in item["message"].lower()


def test_indexed_manual_produces_nothing(client):
    seed_manual(indexed=True)
    assert client.get("/api/notifications/ui").json()["items"] == []


def test_count_tracks_number_of_unindexed_manuals(client):
    seed_manual("a.pdf")
    seed_manual("b.pdf")
    body = client.get("/api/notifications/ui").json()
    assert body["items"][0]["count"] == 2
    # Message pluralises on the count.
    assert body["items"][0]["message"].startswith("2 ")


# ---------------------------------------------------------------------------
# Interval gating (per check)
# ---------------------------------------------------------------------------

def test_check_reruns_only_after_its_interval_elapsed(client, monkeypatch):
    calls = {"n": 0}
    real = ui_notifications.CHECKS["unindexed_manuals"]

    async def counting():
        calls["n"] += 1
        return await real.evaluate()

    monkeypatch.setattr(
        ui_notifications, "CHECKS",
        {**ui_notifications.CHECKS,
         "unindexed_manuals": dataclasses.replace(real, evaluate=counting)},
    )

    client.get("/api/notifications/ui")   # never checked -> evaluates
    client.get("/api/notifications/ui")   # interval not elapsed -> cached
    client.get("/api/notifications/ui")
    assert calls["n"] == 1

    # A short interval makes the next poll evaluate again.
    ui_notifications.update_settings(
        {"unindexed_manuals": {"interval_minutes": 5}}
    )
    asyncio.run(_backdate("unindexed_manuals", minutes_ago=30))
    client.get("/api/notifications/ui")
    assert calls["n"] == 2


async def _backdate(key, minutes_ago):
    from datetime import datetime, timedelta, timezone

    stamp = (datetime.now(timezone.utc)
             - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    async with get_db_context() as db:
        await db.execute(
            "UPDATE ui_notification_state SET last_checked_at = ? "
            "WHERE check_key = ?",
            (stamp, key),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Episode-based dismissal
# ---------------------------------------------------------------------------

def test_dismiss_silences_the_occurrence(client):
    seed_manual()
    assert _keys(client.get("/api/notifications/ui").json()) == ["unindexed_manuals"]

    r = client.post("/api/notifications/ui/unindexed_manuals/dismiss")
    assert r.status_code == 200 and r.json() == {"ok": True}

    # Still un-indexed, but this occurrence is dismissed - even a forced
    # re-evaluation must not bring it back.
    assert client.get("/api/notifications/ui").json()["items"] == []
    body = client.post("/api/notifications/ui/unindexed_manuals/check").json()
    assert body["items"] == []


def test_new_occurrence_after_the_condition_resolves(client):
    mid = seed_manual()
    client.get("/api/notifications/ui")
    assert client.post(
        "/api/notifications/ui/unindexed_manuals/dismiss"
    ).json()["ok"] is True

    # Problem fixed (manual gets indexed) -> episode closes: no item and
    # nothing left to dismiss.
    asyncio.run(_index_manual(mid))
    body = client.post("/api/notifications/ui/unindexed_manuals/check").json()
    assert body["items"] == []
    assert client.post(
        "/api/notifications/ui/unindexed_manuals/dismiss"
    ).json()["ok"] is False

    # A later recurrence is a NEW occurrence and notifies again.
    seed_manual("later.pdf")
    body = client.post("/api/notifications/ui/unindexed_manuals/check").json()
    assert _keys(body) == ["unindexed_manuals"]


def test_dismissal_survives_a_restart(client):
    """Dismissal is server state, so a new app instance stays silent."""
    seed_manual()
    client.get("/api/notifications/ui")
    client.post("/api/notifications/ui/unindexed_manuals/dismiss")

    from homestew.main import app

    with TestClient(app) as c2:
        # New client = new cookie jar; sign in again (the password file
        # lives in DATA_DIR, which the fixture keeps pointed at tmp_path).
        login = c2.post("/api/auth/login", json={"password": "notify-test-password"})
        assert login.status_code == 200
        assert c2.get("/api/notifications/ui").json()["items"] == []


# ---------------------------------------------------------------------------
# Suppressed while an index job runs
# ---------------------------------------------------------------------------

def test_evaluation_suppressed_while_indexing(client):
    seed_manual()
    indexer._INDEX_STATE["running"] = True
    assert client.get("/api/notifications/ui").json()["items"] == []

    # Job finishes -> the check was left due (last_checked_at never advanced),
    # so it evaluates and reports on the very next poll.
    indexer._INDEX_STATE["running"] = False
    assert _keys(client.get("/api/notifications/ui").json()) == ["unindexed_manuals"]


# ---------------------------------------------------------------------------
# Unknown keys
# ---------------------------------------------------------------------------

def test_unknown_notification_key_is_404(client):
    assert client.post("/api/notifications/ui/nope/dismiss").status_code == 404
    assert client.post("/api/notifications/ui/nope/check").status_code == 404


# ---------------------------------------------------------------------------
# Settings > Advanced: per-check interval / enabled round-trip
# ---------------------------------------------------------------------------

def test_settings_expose_checks_with_labels(client):
    checks = client.get("/api/settings").json()["ui_notifications"]
    assert "unindexed_manuals" in checks
    row = checks["unindexed_manuals"]
    assert row["enabled"] is True
    assert row["interval_minutes"] == 1440
    assert row["label"]


def test_settings_round_trip_and_persist(client, env):
    r = client.put("/api/settings", json={
        "ui_notifications": {
            "unindexed_manuals": {"enabled": False, "interval_minutes": 60}
        }
    })
    assert r.status_code == 200
    row = r.json()["ui_notifications"]["unindexed_manuals"]
    assert row["enabled"] is False and row["interval_minutes"] == 60

    persisted = json.loads((env / "settings.json").read_text(encoding="utf-8-sig"))
    assert persisted["UI_NOTIFICATIONS"]["unindexed_manuals"] == {
        "enabled": False, "interval_minutes": 60,
    }


def test_disabled_check_never_fires(client):
    seed_manual()
    client.put("/api/settings", json={
        "ui_notifications": {"unindexed_manuals": {"enabled": False}}
    })
    assert client.get("/api/notifications/ui").json()["items"] == []
    # Even forcing is refused while the check is switched off.
    assert client.post(
        "/api/notifications/ui/unindexed_manuals/check"
    ).json()["items"] == []


def test_partial_update_keeps_the_other_field(client):
    client.put("/api/settings", json={
        "ui_notifications": {"unindexed_manuals": {"interval_minutes": 120}}
    })
    row = client.get("/api/settings").json()["ui_notifications"]["unindexed_manuals"]
    assert row["interval_minutes"] == 120 and row["enabled"] is True


def test_out_of_range_interval_is_422(client):
    for bad in (1, 99999):
        r = client.put("/api/settings", json={
            "ui_notifications": {"unindexed_manuals": {"interval_minutes": bad}}
        })
        assert r.status_code == 422


def test_unknown_check_key_in_settings_is_422(client):
    r = client.put("/api/settings", json={
        "ui_notifications": {"made_up_check": {"enabled": True}}
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# settings.json hardening (normalize_ui_notifications)
# ---------------------------------------------------------------------------

def test_normalize_drops_junk_and_clamps():
    out = cfg.normalize_ui_notifications({
        "unindexed_manuals": {"enabled": True, "interval_minutes": 1},
        "bogus": "not-a-dict",
        7: {"enabled": True},
        "partial": {"nonsense": 3},
    })
    assert out["unindexed_manuals"]["interval_minutes"] == cfg.UI_NOTIFICATION_INTERVAL_MIN
    assert out["unindexed_manuals"]["enabled"] is True
    assert "bogus" not in out and 7 not in out
    assert "partial" not in out
    assert cfg.normalize_ui_notifications(None) == {}


def test_normalize_rejects_bool_as_interval():
    # bool is an int subclass; True must not become interval 1.
    out = cfg.normalize_ui_notifications({"x": {"interval_minutes": True}})
    assert out == {}
