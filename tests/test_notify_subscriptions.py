"""Unified subscription + fan-out tests (dispatch core).

Covers services/notifications/dispatch.py and the delivery side of ui_notifications/notifier:

* deliver() semantics: the UI pseudo-channel is a no-op success, an
  unavailable channel reports False without an attempt, results are
  independent per channel;
* effective_subscription() clamps stored intervals into the configured bounds;
* a health episode fires its webhook exactly once per NEW episode (the frozen
  done-list), enabling a channel mid-episode waits for the next episode, and
  dismissing an occurrence silences external channels too - even a target
  whose first send failed is never retried after dismissal;
* the calendar tick fans out to every subscribed channel with one ledger row
  per success; a partial failure retries only the failed channel next tick;
* Settings PUT rejects unknown event keys / channel names with 422.

Fake channels are registered by patching notify_channels.CHANNELS (dispatch
looks instances up there at call time) plus settings.NOTIFY_SUBSCRIPTIONS
directly - update_subscriptions() validates names against CHANNEL_DEFS and
would reject the fake names used here (same pattern as tests/test_notifier.py).

No async pytest runner in this repo: async code runs via asyncio.run().

Run from the repo root:  pytest tests/test_notify_subscriptions.py -q
"""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from homestew import config as cfg
from homestew.db import get_db_context, init_db
from homestew.services.system import auth
from homestew.services.notifications import dispatch
from homestew.services.search import indexer
from homestew.services.notifications import notify_channels
from homestew.services.notifications import notifier
from homestew.services.notifications import ui_notifications


@pytest.fixture(autouse=True)
def _no_index_job():
    """Evaluation must not be skipped by a stale 'indexing' state."""
    indexer._INDEX_STATE.update(
        {"running": False, "done": 0, "total": 0, "failed": 0, "current_file": ""}
    )
    yield
    indexer._INDEX_STATE.update(
        {"running": False, "done": 0, "total": 0, "failed": 0, "current_file": ""}
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated data dir + no password + empty auth caches."""
    monkeypatch.setattr(cfg.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg.settings, "DEVICES_DIR", tmp_path / "devices")
    monkeypatch.setattr(cfg.settings, "PASSWORD_HASH", "")
    orig = dict(cfg.settings.NOTIFY_SUBSCRIPTIONS)
    auth._failures.clear()
    auth._pw_cache.update({"mtime": None, "hash": ""})
    yield tmp_path
    cfg.settings.NOTIFY_SUBSCRIPTIONS = orig
    auth._failures.clear()
    auth._pw_cache.update({"mtime": None, "hash": ""})


@pytest.fixture()
def db_env(env):
    asyncio.run(init_db())
    return env


@pytest.fixture()
def client(db_env):
    """Signed-in TestClient (forced account creation unlocks the API)."""
    from homestew.main import app

    with TestClient(app) as c:
        r = c.post("/api/auth/setup", json={
            "password": "sub-test-password",
            "confirm_password": "sub-test-password",
        })
        assert r.status_code == 201
        yield c


class RecordingChannel:
    """Duck-typed NotificationChannel that records every payload sent."""

    def __init__(self, name="recorder"):
        self.name = name
        self.payloads = []

    def enabled(self):
        return True

    def send(self, payload):
        self.payloads.append(payload)


class FlakyChannel:
    """Channel whose send() fails until ``ok`` is flipped to True."""

    def __init__(self, name="flaky"):
        self.name = name
        self.ok = False
        self.attempts = 0

    def enabled(self):
        return True

    def send(self, payload):
        self.attempts += 1
        if not self.ok:
            raise RuntimeError("channel down")


# ---------------------------------------------------------------------------
# Helpers (asyncio.run wrappers - no async pytest runner in this repo)
# ---------------------------------------------------------------------------

def run_eval():
    """One background-loop pass over the condition checks."""
    return asyncio.run(ui_notifications.run_due_checks())


async def _seed_manual(filename="m.pdf"):
    async with get_db_context() as db:
        cur = await db.execute(
            "INSERT INTO manuals (device_id, filename, filepath) "
            "VALUES (1, ?, '/nope/m.pdf') RETURNING id",
            (filename,),
        )
        mid = (await cur.fetchone())["id"]
        await db.commit()
    return mid


def seed_manual(filename="m.pdf"):
    return asyncio.run(_seed_manual(filename))


async def _index_manual(manual_id, filename="m.pdf"):
    async with get_db_context() as db:
        await db.execute(
            "INSERT INTO pdf_index (device_id, manual_id, filename, "
            "page_number, content) VALUES (1, ?, ?, 1, 'boiler text')",
            (manual_id, filename),
        )
        await db.commit()


async def _seed_event(start_date):
    async with get_db_context() as db:
        cur = await db.execute(
            "INSERT INTO calendar_events (title, start_date, recurrence_type)"
            " VALUES ('Lube the gears', ?, 'none') RETURNING id",
            (start_date.isoformat(),),
        )
        eid = (await cur.fetchone())["id"]
        await db.commit()
    return eid


def seed_event(start_date):
    return asyncio.run(_seed_event(start_date))


async def _backdate(key, minutes_ago):
    stamp = (datetime.now(timezone.utc)
             - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    async with get_db_context() as db:
        await db.execute(
            "UPDATE ui_notification_state SET last_checked_at = ? "
            "WHERE check_key = ?",
            (stamp, key),
        )
        await db.commit()


def backdate(key, minutes_ago):
    asyncio.run(_backdate(key, minutes_ago))


def ledger_rows():
    async def _fetch():
        async with get_db_context() as db:
            cur = await db.execute(
                "SELECT event_id, due_date, kind, channel FROM notification_log"
            )
            return [(r["event_id"], str(r["due_date"])[:10], r["kind"],
                     r["channel"]) for r in await cur.fetchall()]

    return asyncio.run(_fetch())


# ---------------------------------------------------------------------------
# deliver() semantics
# ---------------------------------------------------------------------------

def test_deliver_ui_channel_is_noop_success():
    results = asyncio.run(dispatch.deliver({"type": "x"}, ["ui"]))
    assert results == {"ui": True}


def test_deliver_unavailable_channel_reports_false_without_attempt(monkeypatch):
    monkeypatch.setattr(notify_channels, "CHANNELS", [])
    results = asyncio.run(dispatch.deliver({"type": "x"}, ["webhook"]))
    assert results == {"webhook": False}


def test_deliver_failures_are_independent_per_channel(monkeypatch):
    good = RecordingChannel("good")
    bad = FlakyChannel("bad")
    monkeypatch.setattr(notify_channels, "CHANNELS", [good, bad])
    results = asyncio.run(dispatch.deliver({"type": "x"}, ["good", "bad"]))
    assert results == {"good": True, "bad": False}
    assert len(good.payloads) == 1  # the failing sibling did not abort the fan-out


# ---------------------------------------------------------------------------
# effective_subscription merge rules
# ---------------------------------------------------------------------------

def test_effective_subscription_clamps_stored_intervals(env, monkeypatch):
    monkeypatch.setattr(cfg.settings, "NOTIFY_SUBSCRIPTIONS",
                        {"unindexed_manuals": {"interval_minutes": 1}})
    assert dispatch.effective_subscription("unindexed_manuals")["interval_minutes"] == 5

    monkeypatch.setattr(cfg.settings, "NOTIFY_SUBSCRIPTIONS",
                        {"unindexed_manuals": {"interval_minutes": 999999}})
    assert dispatch.effective_subscription("unindexed_manuals")["interval_minutes"] == 10080


def test_rule_event_defaults(client):
    row = client.get("/api/settings").json()["notify_subscriptions"]["calendar_reminders"]
    assert row["kind"] == "rule"
    # Rule events carry their own scan cadence + lead window per row.
    assert row["interval_minutes"] == 15
    assert row["notify_before_due_hours"] == 24
    # Registry defaults: toast on, webhook off. Enabled is derived from the
    # (non-empty) channel list.
    assert row["enabled"] is True
    assert row["channels"] == ["ui"]


def test_enabled_is_derived_from_channels(client):
    # Emptying the channel list disables the event; re-adding one enables it.
    r = client.put("/api/settings", json={
        "notify_subscriptions": {"calendar_reminders": {"channels": []}}
    })
    row = r.json()["notify_subscriptions"]["calendar_reminders"]
    assert row["channels"] == [] and row["enabled"] is False

    r = client.put("/api/settings", json={
        "notify_subscriptions": {"calendar_reminders": {"channels": ["ui"]}}
    })
    row = r.json()["notify_subscriptions"]["calendar_reminders"]
    assert row["enabled"] is True


def test_lead_hours_round_trip_and_clamp(client):
    r = client.put("/api/settings", json={
        "notify_subscriptions": {"calendar_reminders": {
            "notify_before_due_hours": 48, "interval_minutes": 30}}
    })
    row = r.json()["notify_subscriptions"]["calendar_reminders"]
    assert row["notify_before_due_hours"] == 48 and row["interval_minutes"] == 30

    # Out of range -> clamped by normalize on read (schema also bounds it).
    cfg.settings.NOTIFY_SUBSCRIPTIONS = {
        "calendar_reminders": {"notify_before_due_hours": 999999}
    }
    assert dispatch.effective_subscription(
        "calendar_reminders")["notify_before_due_hours"] == 8760


# ---------------------------------------------------------------------------
# Health episodes -> external channels
# ---------------------------------------------------------------------------

def test_webhook_fires_once_per_new_episode(client, monkeypatch):
    rec = RecordingChannel("recorder")
    monkeypatch.setattr(notify_channels, "CHANNELS", [rec])
    monkeypatch.setattr(cfg.settings, "NOTIFY_SUBSCRIPTIONS", {
        "unindexed_manuals": {"enabled": True, "channels": ["ui", "recorder"],
                              "interval_minutes": 5},
    })

    mid = seed_manual()
    run_eval()  # new episode -> one delivery
    assert len(rec.payloads) == 1
    payload = rec.payloads[0]
    assert payload["type"] == "unindexed_manuals"
    assert payload["dedup_key"] == "check:unindexed_manuals"
    assert payload["details"]["count"] == 1

    # Same episode continuing (interval elapsed, condition still true): the
    # done-list freezes -> no resend.
    backdate("unindexed_manuals", 60)
    run_eval()
    assert len(rec.payloads) == 1

    # Condition resolves -> episode closes; a recurrence is a NEW episode.
    asyncio.run(_index_manual(mid))
    backdate("unindexed_manuals", 60)
    run_eval()
    seed_manual("again.pdf")
    backdate("unindexed_manuals", 60)
    run_eval()
    assert len(rec.payloads) == 2


def test_mid_episode_channel_enable_waits_for_next_episode(client, monkeypatch):
    rec = RecordingChannel("recorder")
    monkeypatch.setattr(notify_channels, "CHANNELS", [rec])
    monkeypatch.setattr(cfg.settings, "NOTIFY_SUBSCRIPTIONS", {
        "unindexed_manuals": {"enabled": True, "channels": ["ui"],
                              "interval_minutes": 5},
    })

    mid = seed_manual()
    run_eval()  # episode starts with an empty frozen target list
    assert rec.payloads == []

    # Enabling the webhook mid-episode must NOT retroactively fire it.
    cfg.settings.NOTIFY_SUBSCRIPTIONS = {
        "unindexed_manuals": {"enabled": True, "channels": ["ui", "recorder"],
                              "interval_minutes": 5},
    }
    backdate("unindexed_manuals", 60)
    run_eval()
    assert rec.payloads == []

    # The next episode snapshots the new subscription and fires.
    asyncio.run(_index_manual(mid))
    backdate("unindexed_manuals", 60)
    run_eval()  # closes the episode
    seed_manual("again.pdf")
    backdate("unindexed_manuals", 60)
    run_eval()
    assert len(rec.payloads) == 1


def test_dismissed_episode_delivers_nowhere_even_after_failed_send(client, monkeypatch):
    """Dismissal means "this occurrence, nowhere" - not just "no toast"."""
    flaky = FlakyChannel("flaky")
    monkeypatch.setattr(notify_channels, "CHANNELS", [flaky])
    monkeypatch.setattr(cfg.settings, "NOTIFY_SUBSCRIPTIONS", {
        "unindexed_manuals": {"enabled": True, "channels": ["ui", "flaky"],
                              "interval_minutes": 5},
    })

    seed_manual()
    run_eval()  # episode starts; the send fails, so done stays empty
    assert flaky.attempts == 1

    client.post("/api/notifications/ui/unindexed_manuals/dismiss")
    flaky.ok = True  # a retry would succeed now...
    backdate("unindexed_manuals", 60)
    run_eval()
    assert flaky.attempts == 1  # ...but the occurrence is dismissed


# ---------------------------------------------------------------------------
# Calendar tick fan-out + per-channel ledger
# ---------------------------------------------------------------------------

def test_calendar_tick_partial_failure_retries_only_failed_channel(client, monkeypatch):
    good = RecordingChannel("recorder")
    flaky = FlakyChannel("flaky")
    monkeypatch.setattr(notify_channels, "CHANNELS", [good, flaky])
    monkeypatch.setattr(cfg.settings, "NOTIFY_SUBSCRIPTIONS", {
        "calendar_reminders": {"enabled": True,
                               "channels": ["ui", "recorder", "flaky"]},
    })

    tomorrow = date.today() + timedelta(days=1)
    eid = seed_event(tomorrow)

    assert asyncio.run(notifier.run_tick()) == 1
    assert len(good.payloads) == 1 and flaky.attempts == 1
    rows = ledger_rows()
    assert (eid, tomorrow.isoformat(), "due", "recorder") in rows
    assert not any(r[3] == "flaky" for r in rows)  # failed send -> no row

    # Next tick: the succeeded channel is skipped, only the failure retries.
    flaky.ok = True
    asyncio.run(notifier.run_tick())
    assert len(good.payloads) == 1
    assert flaky.attempts == 2
    assert (eid, tomorrow.isoformat(), "due", "flaky") in ledger_rows()


def test_calendar_tick_toast_only_subscription_skips_db_entirely(client, monkeypatch):
    """Idle-CPU guarantee survives the fan-out: no external target -> no scan."""
    monkeypatch.setattr(notify_channels, "CHANNELS", [])
    monkeypatch.setattr(cfg.settings, "NOTIFY_SUBSCRIPTIONS", {
        "calendar_reminders": {"enabled": True, "channels": ["ui"]},
    })
    seed_event(date.today() + timedelta(days=1))

    calls = []

    async def spy_candidates(*args, **kwargs):
        calls.append(1)
        return []

    monkeypatch.setattr(notifier.calendar_service, "list_notify_candidates",
                        spy_candidates)
    assert asyncio.run(notifier.run_tick()) == 0
    assert calls == []


# ---------------------------------------------------------------------------
# Settings API validation (registry-backed)
# ---------------------------------------------------------------------------

def test_settings_put_rejects_unknown_event_key(client):
    r = client.put("/api/settings", json={
        "notify_subscriptions": {"made_up_event": {"enabled": True}}
    })
    assert r.status_code == 422


def test_settings_put_rejects_unknown_channel_name(client):
    r = client.put("/api/settings", json={
        "notify_subscriptions": {"calendar_reminders": {"channels": ["carrier-pigeon"]}}
    })
    assert r.status_code == 422


def test_settings_put_round_trips_channels(client, env):
    r = client.put("/api/settings", json={
        "notify_subscriptions": {
            "calendar_reminders": {"channels": ["ui", "webhook"]}
        }
    })
    assert r.status_code == 200
    row = r.json()["notify_subscriptions"]["calendar_reminders"]
    assert row["channels"] == ["ui", "webhook"]

    import json
    persisted = json.loads((env / "settings.json").read_text(encoding="utf-8-sig"))
    assert persisted["NOTIFY_SUBSCRIPTIONS"]["calendar_reminders"]["channels"] == [
        "ui", "webhook"
    ]
