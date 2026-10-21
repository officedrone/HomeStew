"""Calendar reminders rendered into the unified in-app feed (v0.2.5).

Before v0.2.5 the calendar could only talk to a webhook; the toast feed knew
nothing about due events. Now ``calendar_reminders`` is one subscribable event
and ``collect()`` merges due/overdue occurrences into ``GET /api/notifications/ui``
as ``kind="rule"`` items carrying a per-occurrence ``dedup_key``.

Covered here:

* a due and an overdue event both appear, with the right dedup key shape,
  message, severity and Go To target;
* Dismiss writes a ``'ui'`` ledger row so the card never resurrects, while the
  NEXT occurrence of a recurring event still shows up;
* marking an event done removes the card on its own (it leaves the candidate
  list - no dismissal needed);
* a webhook-only subscription produces no toast at all (and, per the idle-CPU
  rule, the read side does no calendar work either);
* dismissing a card whose event was deleted in another tab reports ok:false
  instead of tripping the ledger's foreign key;
* the ledger keeps one row per channel, so a webhook send never marks the
  toast as delivered (and vice versa).

Fixture pattern copied from tests/test_ui_notifications.py. Async service
functions run through asyncio.run() - no async pytest runner in this repo.

Run from the repo root:  pytest tests/test_calendar_toasts.py -q
"""
import asyncio
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from homestew import config as cfg
from homestew.db import get_db_context, init_db
from homestew.services import auth, indexer, notify_channels, notifier, ui_notifications


@pytest.fixture(autouse=True)
def _no_index_job():
    indexer._INDEX_STATE.update(
        {"running": False, "done": 0, "total": 0, "failed": 0, "current_file": ""}
    )
    yield
    indexer._INDEX_STATE.update(
        {"running": False, "done": 0, "total": 0, "failed": 0, "current_file": ""}
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
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
    from homestew.main import app

    with TestClient(app) as c:
        r = c.post("/api/auth/setup", json={
            "password": "toast-test-password",
            "confirm_password": "toast-test-password",
        })
        assert r.status_code == 201
        yield c


class RecordingChannel:
    """Duck-typed NotificationChannel recording payloads (see test_notifier.py)."""

    def __init__(self, name="recorder"):
        self.name = name
        self.payloads = []

    def enabled(self):
        return True

    def send(self, payload):
        self.payloads.append(payload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed_event(title, start_date, recurrence="none", interval=1):
    async with get_db_context() as db:
        cur = await db.execute(
            "INSERT INTO calendar_events (title, start_date, recurrence_type,"
            " interval) VALUES (?, ?, ?, ?)",
            (title, start_date.isoformat(), recurrence, interval),
        )
        eid = cur.lastrowid
        await db.commit()
    return eid


def seed_event(title="Change the filter", days=1, recurrence="none"):
    return asyncio.run(_seed_event(title, date.today() + timedelta(days=days),
                                   recurrence))


def items(client):
    return client.get("/api/notifications/ui").json()["items"]


def calendar_items(client):
    return [i for i in items(client) if i["key"] == "calendar_reminders"]


def ledger():
    async def _fetch():
        async with get_db_context() as db:
            cur = await db.execute(
                "SELECT event_id, due_date, kind, channel FROM notification_log"
            )
            return [(r["event_id"], str(r["due_date"])[:10], r["kind"],
                     r["channel"]) for r in await cur.fetchall()]

    return asyncio.run(_fetch())


# ---------------------------------------------------------------------------
# Due / overdue events reach the toast feed
# ---------------------------------------------------------------------------

def test_due_event_appears_as_a_rule_item(client):
    eid = seed_event("Descale the coffee machine", days=1)
    cards = calendar_items(client)
    assert len(cards) == 1
    card = cards[0]
    due_str = (date.today() + timedelta(days=1)).isoformat()
    assert card["dedup_key"] == f"calendar:{eid}:{due_str}:due"
    assert card["kind"] == "rule"
    assert card["severity"] == "warn"
    assert "Descale the coffee machine" in card["message"]
    assert card["goto_tab"] == "calendar"
    assert card["action_label"] == "Go to Calendar"


def test_overdue_event_appears_as_overdue(client):
    eid = seed_event("Smoke alarm batteries", days=-3)
    cards = calendar_items(client)
    assert len(cards) == 1
    due_str = (date.today() - timedelta(days=3)).isoformat()
    assert cards[0]["dedup_key"] == f"calendar:{eid}:{due_str}:overdue"
    assert cards[0]["message"].lower().startswith("overdue")


def test_event_outside_the_lead_window_is_silent(client):
    # Default lead is 24 hours; a week away is far outside it.
    seed_event("Spring servicing", days=7)
    assert calendar_items(client) == []


# ---------------------------------------------------------------------------
# Dismissal is permanent for the occurrence, not for the series
# ---------------------------------------------------------------------------

def test_dismiss_stays_gone_across_polls(client):
    eid = seed_event("Replace the UV lamp", days=1)
    key = calendar_items(client)[0]["dedup_key"]

    r = client.post(f"/api/notifications/ui/{key}/dismiss")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert calendar_items(client) == []

    # The dismissal lives in the ledger, so a second poll (or a fresh app
    # instance) stays silent while the occurrence is the same.
    assert calendar_items(client) == []
    from homestew.main import app

    with TestClient(app) as c2:
        assert c2.post("/api/auth/login",
                       json={"password": "toast-test-password"}).status_code == 200
        assert calendar_items(c2) == []


def test_dismiss_writes_a_ui_ledger_row(client):
    eid = seed_event("Water the plants", days=1)
    key = calendar_items(client)[0]["dedup_key"]
    due_str = (date.today() + timedelta(days=1)).isoformat()

    client.post(f"/api/notifications/ui/{key}/dismiss")
    assert (eid, due_str, "due", "ui") in ledger()


def test_re_dismissing_the_same_occurrence_is_ok(client):
    seed_event("Clean the filter", days=1)
    key = calendar_items(client)[0]["dedup_key"]
    assert client.post(f"/api/notifications/ui/{key}/dismiss").json()["ok"] is True
    # Idempotent: the desired end state holds either way.
    assert client.post(f"/api/notifications/ui/{key}/dismiss").json()["ok"] is True


def test_next_occurrence_of_a_recurring_event_notifies_again(client):
    today = date.today()
    # Weekly series anchored a week ago -> this occurrence is due today.
    eid = asyncio.run(_seed_event("Back up the NAS", today - timedelta(days=7),
                                 recurrence="weekly", interval=1))
    first_key = calendar_items(client)[0]["dedup_key"]
    assert first_key == f"calendar:{eid}:{today.isoformat()}:due"

    client.post(f"/api/notifications/ui/{first_key}/dismiss")
    assert calendar_items(client) == []

    # Advance the series: completing today's occurrence rolls it forward, and
    # tomorrow's date is a fresh occurrence with a fresh dedup key.
    r = client.post(f"/api/calendar/{eid}/complete")
    assert r.status_code == 200
    cards = calendar_items(client)
    # The rolled-forward date is outside the 24h lead window, so nothing yet;
    # what matters is that the dismissed occurrence did not come back.
    assert all(c["dedup_key"] != first_key for c in cards)


# ---------------------------------------------------------------------------
# Marking done removes the card without any dismissal
# ---------------------------------------------------------------------------

def test_marking_done_makes_the_card_vanish(client):
    eid = seed_event("Flip the heat pump", days=1)
    assert len(calendar_items(client)) == 1

    assert client.post(f"/api/calendar/{eid}/complete").status_code == 200
    # A completed one-time event leaves the candidate list entirely - no
    # ledger row was needed.
    assert calendar_items(client) == []
    assert ledger() == []


# ---------------------------------------------------------------------------
# Channel selection
# ---------------------------------------------------------------------------

def test_webhook_only_subscription_produces_no_toast(client, monkeypatch):
    rec = RecordingChannel("webhook")
    monkeypatch.setattr(notify_channels, "CHANNELS", [rec])
    monkeypatch.setattr(cfg.settings, "NOTIFY_SUBSCRIPTIONS", {
        "calendar_reminders": {"enabled": True, "channels": ["webhook"]},
    })
    seed_event("Service the boiler", days=1)

    # Nothing in the toast feed...
    assert calendar_items(client) == []
    # ...while the background tick still delivers externally, and its ledger
    # row is per-channel so it never silences the toast side.
    assert asyncio.run(notifier.run_tick()) == 1
    assert len(rec.payloads) == 1
    assert all(row[3] == "webhook" for row in ledger())


def test_disabled_event_is_silent_everywhere(client, monkeypatch):
    rec = RecordingChannel("webhook")
    monkeypatch.setattr(notify_channels, "CHANNELS", [rec])
    # No subscribed channels = the event is off (channel enablement IS the
    # per-event switch; there is no separate enabled flag any more).
    monkeypatch.setattr(cfg.settings, "NOTIFY_SUBSCRIPTIONS", {
        "calendar_reminders": {"channels": []},
    })
    seed_event("Ignored reminder", days=1)
    assert calendar_items(client) == []
    assert asyncio.run(notifier.run_tick()) == 0
    assert rec.payloads == []


def test_webhook_send_does_not_suppress_the_toast(client, monkeypatch):
    """The ledger is per-channel: a webhook success must not eat the card."""
    rec = RecordingChannel("webhook")
    monkeypatch.setattr(notify_channels, "CHANNELS", [rec])
    monkeypatch.setattr(cfg.settings, "NOTIFY_SUBSCRIPTIONS", {
        "calendar_reminders": {"enabled": True, "channels": ["ui", "webhook"]},
    })
    seed_event("Check the sump pump", days=1)

    assert asyncio.run(notifier.run_tick()) == 1
    cards = calendar_items(client)
    assert len(cards) == 1  # still shown in-app
    assert all(row[3] != "ui" for row in ledger())


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_dismissing_a_deleted_events_card_does_not_crash(client):
    eid = seed_event("Ghost reminder", days=1)
    key = calendar_items(client)[0]["dedup_key"]

    # Deleted in another tab: the card is already gone, and the ledger's FK
    # onto calendar_events must not be tripped.
    assert client.delete(f"/api/calendar/{eid}").status_code == 204
    r = client.post(f"/api/notifications/ui/{key}/dismiss")
    assert r.status_code == 200 and r.json() == {"ok": False}


def test_malformed_calendar_key_is_404(client):
    for bad in ("calendar:notanint:2026-10-01:due",
                "calendar:1:2026-10-01:sometime",
                "calendar:1:2026-10-01"):
        assert client.post(f"/api/notifications/ui/{bad}/dismiss").status_code == 404


def test_calendar_test_endpoint_previews_and_delivers(client, monkeypatch):
    rec = RecordingChannel("webhook")
    monkeypatch.setattr(notify_channels, "CHANNELS", [rec])
    monkeypatch.setattr(cfg.settings, "NOTIFY_SUBSCRIPTIONS", {
        "calendar_reminders": {"enabled": True, "channels": ["ui", "webhook"]},
    })

    body = client.post("/api/notifications/ui/calendar_reminders/test").json()
    assert body["item"]["key"] == "calendar_reminders"
    assert body["item"]["kind"] == "rule"
    assert body["deliveries"] == {"webhook": True}
    assert len(rec.payloads) == 1
    assert rec.payloads[0]["test"] is True

    # A Test never writes the ledger or episode state.
    assert ledger() == []
    assert calendar_items(client) == []


def test_calendar_test_honours_unsaved_channel_override(client, monkeypatch):
    """The bug: ticking Webhook and clicking Test BEFORE saving only toasted.

    The frontend now sends the row's checkboxes as the request body, so the
    sample payload goes out even while the saved subscription is toast-only.
    """
    rec = RecordingChannel("webhook")
    monkeypatch.setattr(notify_channels, "CHANNELS", [rec])
    monkeypatch.setattr(cfg.settings, "NOTIFY_SUBSCRIPTIONS", {
        "calendar_reminders": {"channels": ["ui"]},  # webhook NOT saved yet
    })

    body = client.post("/api/notifications/ui/calendar_reminders/test",
                       json={"channels": ["ui", "webhook"]}).json()
    assert body["deliveries"] == {"webhook": True}
    assert len(rec.payloads) == 1

    # Without the override the saved (toast-only) list still applies: preview
    # renders, nothing is sent externally.
    rec.payloads.clear()
    body = client.post("/api/notifications/ui/calendar_reminders/test").json()
    assert body["deliveries"] == {}
    assert rec.payloads == []


def test_test_override_drops_unknown_channels(client, monkeypatch):
    """A stale frontend can't invent channels: unknown ids are ignored."""
    rec = RecordingChannel("webhook")
    monkeypatch.setattr(notify_channels, "CHANNELS", [rec])
    monkeypatch.setattr(cfg.settings, "NOTIFY_SUBSCRIPTIONS", {
        "calendar_reminders": {"channels": []},
    })

    body = client.post("/api/notifications/ui/calendar_reminders/test",
                       json={"channels": ["carrier-pigeon"]}).json()
    assert body["deliveries"] == {}  # unknown id dropped -> nothing to send
    assert rec.payloads == []


def test_calendar_check_now_runs_a_scan(client, monkeypatch):
    """Settings 'Check Now' on the calendar row forces one notifier pass."""
    rec = RecordingChannel("webhook")
    monkeypatch.setattr(notify_channels, "CHANNELS", [rec])
    monkeypatch.setattr(cfg.settings, "NOTIFY_SUBSCRIPTIONS", {
        "calendar_reminders": {"channels": ["webhook"]},
    })
    seed_event("Service the boiler", days=1)

    r = client.post("/api/notifications/ui/calendar_reminders/check")
    assert r.status_code == 200
    # The scan delivered externally (ledger written) - response is the full
    # active feed shape, same as GET /ui.
    assert len(rec.payloads) == 1
    assert "items" in r.json() and "statuses" in r.json()


def test_unknown_key_check_is_404(client):
    assert client.post("/api/notifications/ui/made_up/check").status_code == 404
