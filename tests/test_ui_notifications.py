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
from homestew.services import auth, dispatch, indexer, ui_notifications


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
    orig = dict(cfg.settings.NOTIFY_SUBSCRIPTIONS)
    auth._failures.clear()
    auth._pw_cache.update({"mtime": None, "hash": ""})
    yield tmp_path
    cfg.settings.NOTIFY_SUBSCRIPTIONS = orig
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


async def _seed_pending_photo_suggestion(pending="Filter looks grey"):
    async with get_db_context() as db:
        cur = await db.execute(
            "INSERT INTO devices (name, brand, model) VALUES ('Furnace', 'Trane', 'XR14')"
        )
        await db.execute(
            "INSERT INTO device_photos (device_id, filename, filepath, "
            "mime_type, pending_ai_notes) VALUES (?, 'f.png', '/nope/f.png', ?, ?)",
            (cur.lastrowid, "image/png", pending),
        )
        await db.commit()


def seed_pending_photo(**kw):
    asyncio.run(_seed_pending_photo_suggestion(**kw))


def run_eval():
    """Simulate one background-loop pass (collect() itself is read-only)."""
    return asyncio.run(ui_notifications.run_due_checks())


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
    run_eval()
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
    run_eval()
    body = client.get("/api/notifications/ui").json()
    assert body["items"][0]["count"] == 2
    # Message pluralises on the count.
    assert body["items"][0]["message"].startswith("2 ")


# ---------------------------------------------------------------------------
# Photo AI suggestions pending
# ---------------------------------------------------------------------------

def test_pending_photo_suggestion_triggers_notification(client):
    seed_pending_photo()
    run_eval()
    body = client.get("/api/notifications/ui").json()
    assert _keys(body) == ["photo_suggestions"]
    item = body["items"][0]
    assert item["count"] == 1
    assert item["severity"] == "info"
    assert item["goto_tab"] == "devices"
    assert "photo" in item["message"].lower()


def test_accepted_suggestion_clears_the_notification(client):
    seed_pending_photo()
    run_eval()
    assert _keys(client.get("/api/notifications/ui").json()) == ["photo_suggestions"]

    async def accept():
        async with get_db_context() as db:
            await db.execute(
                "UPDATE device_photos SET ai_notes = pending_ai_notes, "
                "pending_ai_notes = NULL"
            )
            await db.commit()

    asyncio.run(accept())
    body = client.post("/api/notifications/ui/photo_suggestions/check").json()
    assert body["items"] == []


def test_empty_pending_string_is_not_a_suggestion(client):
    seed_pending_photo(pending="")
    run_eval()
    assert "photo_suggestions" not in _keys(client.get("/api/notifications/ui").json())


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

    run_eval()   # never checked -> evaluates
    run_eval()   # interval not elapsed -> cached
    run_eval()
    assert calls["n"] == 1

    # A short interval makes the next pass evaluate again.
    dispatch.update_subscriptions(
        {"unindexed_manuals": {"interval_minutes": 5}}
    )
    asyncio.run(_backdate("unindexed_manuals", minutes_ago=30))
    run_eval()
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
    run_eval()
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
    run_eval()
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
    run_eval()
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
    run_eval()  # skipped mid-job, last_checked_at must NOT advance
    assert client.get("/api/notifications/ui").json()["items"] == []

    # Job finishes -> the check was left due (last_checked_at never advanced),
    # so the next pass evaluates and the poll reports it.
    indexer._INDEX_STATE["running"] = False
    run_eval()
    assert _keys(client.get("/api/notifications/ui").json()) == ["unindexed_manuals"]


# ---------------------------------------------------------------------------
# Unknown keys
# ---------------------------------------------------------------------------

def test_unknown_notification_key_is_404(client):
    assert client.post("/api/notifications/ui/nope/dismiss").status_code == 404
    assert client.post("/api/notifications/ui/nope/check").status_code == 404
    assert client.post("/api/notifications/ui/nope/test").status_code == 404


# ---------------------------------------------------------------------------
# Per-check statuses (Settings > Advanced status labels)
# ---------------------------------------------------------------------------

def test_statuses_report_every_registered_check(client):
    # A pass evaluates the (due) check, so every registered key has a status
    # row and the empty DB reads as a clean result.
    run_eval()
    statuses = client.get("/api/notifications/ui").json()["statuses"]
    assert set(statuses) == set(ui_notifications.CHECKS)
    st = statuses["unindexed_manuals"]
    assert st["condition_true"] is False  # evaluated, no manuals -> false
    assert st["dismissed"] is False
    assert st["last_checked_at"]  # evaluation stamps the time


def test_status_tracks_problem_then_ok(client):
    seed_manual()
    run_eval()
    statuses = client.get("/api/notifications/ui").json()["statuses"]
    st = statuses["unindexed_manuals"]
    assert st["condition_true"] is True
    assert st["dismissed"] is False
    assert st["last_checked_at"]  # set once evaluated

    # Dismiss the episode -> still true, now flagged dismissed.
    client.post("/api/notifications/ui/unindexed_manuals/dismiss")
    st = client.get("/api/notifications/ui").json()["statuses"]["unindexed_manuals"]
    assert st["condition_true"] is True and st["dismissed"] is True


# ---------------------------------------------------------------------------
# "Test" preview: a sample item even when the condition does NOT hold
# ---------------------------------------------------------------------------

def test_test_endpoint_returns_sample_when_all_clear(client):
    # No manuals at all -> no active notification, but Test still previews one.
    assert client.get("/api/notifications/ui").json()["items"] == []
    body = client.post("/api/notifications/ui/unindexed_manuals/test").json()
    item = body["item"]
    assert item["key"] == "unindexed_manuals"
    assert item["severity"] == "warn"
    assert item["goto_tab"] == "settings" and item["goto_section"] == "search"
    assert "search index" in item["message"]
    # Only the toast channel is subscribed by default -> nothing dispatched.
    assert body["deliveries"] == {}
    # A preview never mutates state: still nothing active.
    assert client.get("/api/notifications/ui").json()["items"] == []


def test_test_endpoint_does_not_persist_state(client):
    # Previewing must not create or alter a ui_notification_state row.
    client.post("/api/notifications/ui/unindexed_manuals/test")

    async def rows():
        async with get_db_context() as db:
            cur = await db.execute("SELECT COUNT(*) AS n FROM ui_notification_state")
            return (await cur.fetchone())["n"]

    assert asyncio.run(rows()) == 0


# ---------------------------------------------------------------------------
# Settings > Advanced: per-check interval / enabled round-trip
# ---------------------------------------------------------------------------

def test_settings_expose_checks_with_labels(client):
    checks = client.get("/api/settings").json()["notify_subscriptions"]
    assert "unindexed_manuals" in checks
    row = checks["unindexed_manuals"]
    assert row["enabled"] is True
    assert row["interval_minutes"] == 1440
    # Condition checks have no due date, so no lead window (UI shows a dash).
    assert row["notify_before_due_hours"] is None
    assert row["label"]


def test_settings_round_trip_and_persist(client, env):
    # Disabling = an empty channel list (there is no separate enabled field).
    r = client.put("/api/settings", json={
        "notify_subscriptions": {
            "unindexed_manuals": {"channels": [], "interval_minutes": 60}
        }
    })
    assert r.status_code == 200
    row = r.json()["notify_subscriptions"]["unindexed_manuals"]
    assert row["enabled"] is False and row["interval_minutes"] == 60

    persisted = json.loads((env / "settings.json").read_text(encoding="utf-8-sig"))
    assert persisted["NOTIFY_SUBSCRIPTIONS"]["unindexed_manuals"] == {
        "channels": [], "interval_minutes": 60,
    }


def test_disabled_check_never_fires(client):
    seed_manual()
    client.put("/api/settings", json={
        "notify_subscriptions": {"unindexed_manuals": {"channels": []}}
    })
    assert client.get("/api/notifications/ui").json()["items"] == []
    # Even forcing is refused while the check has no channel subscribed.
    assert client.post(
        "/api/notifications/ui/unindexed_manuals/check"
    ).json()["items"] == []


def test_partial_update_keeps_the_other_field(client):
    client.put("/api/settings", json={
        "notify_subscriptions": {"unindexed_manuals": {"interval_minutes": 120}}
    })
    row = client.get("/api/settings").json()["notify_subscriptions"]["unindexed_manuals"]
    assert row["interval_minutes"] == 120 and row["enabled"] is True


def test_out_of_range_interval_is_422(client):
    for bad in (1, 99999):
        r = client.put("/api/settings", json={
            "notify_subscriptions": {"unindexed_manuals": {"interval_minutes": bad}}
        })
        assert r.status_code == 422


def test_unknown_check_key_in_settings_is_422(client):
    r = client.put("/api/settings", json={
        "notify_subscriptions": {"made_up_check": {"enabled": True}}
    })
    assert r.status_code == 422


def test_unknown_channel_in_settings_is_422(client):
    r = client.put("/api/settings", json={
        "notify_subscriptions": {"unindexed_manuals": {"channels": ["carrier-pigeon"]}}
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# settings.json hardening (normalize_notify_subscriptions)
# ---------------------------------------------------------------------------

def test_normalize_drops_junk_and_clamps():
    out = cfg.normalize_notify_subscriptions({
        "unindexed_manuals": {"channels": ["ui"], "interval_minutes": 1},
        "bogus": "not-a-dict",
        7: {"channels": ["ui"]},
        "partial": {"nonsense": 3},
    })
    assert out["unindexed_manuals"]["interval_minutes"] == cfg.UI_NOTIFICATION_INTERVAL_MIN
    assert out["unindexed_manuals"]["channels"] == ["ui"]
    # The enabled flag is no longer a stored field (derived from channels).
    assert "enabled" not in out["unindexed_manuals"]
    assert "bogus" not in out and 7 not in out
    assert "partial" not in out
    assert cfg.normalize_notify_subscriptions(None) == {}


def test_normalize_clamps_lead_hours():
    out = cfg.normalize_notify_subscriptions({
        "calendar_reminders": {"notify_before_due_hours": 999999},
    })
    assert out["calendar_reminders"]["notify_before_due_hours"] == cfg.NOTIFY_LEAD_HOURS_MAX
    out = cfg.normalize_notify_subscriptions({
        "calendar_reminders": {"notify_before_due_hours": -5},
    })
    assert out["calendar_reminders"]["notify_before_due_hours"] == cfg.NOTIFY_LEAD_HOURS_MIN


def test_normalize_rejects_bool_as_interval():
    # bool is an int subclass; True must not become interval 1 (and with no
    # other valid field the whole entry drops away).
    out = cfg.normalize_notify_subscriptions({"x": {"interval_minutes": True}})
    assert out == {}
