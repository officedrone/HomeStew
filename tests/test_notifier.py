"""Tests for the background notifier tick (services/notifier.py) and the
/health access-log filter (main.py) - issue #9, idle CPU optimization.

Two behaviours are pinned down:

1. run_tick() must resolve channels BEFORE touching the database, so a tick
   with no enabled channel does zero DB work (the old order ran a full
   calendar scan every tick even when nothing could be delivered).
2. When a channel IS enabled and an event is due, the tick still delivers,
   writes the ledger row, and dedupes on the next tick - proving the reorder
   did not break the happy path.

Plus unit tests for _HealthAccessFilter / install_health_access_log_filter.

No async pytest runner in this repo: async code runs via asyncio.run().

Run from the repo root:  pytest tests/test_notifier.py -q
"""
import asyncio
import logging

import pytest

from homestew import config as cfg
from homestew.db import get_db_context, init_db
from homestew.services import notifier


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


class RecordingChannel:
    """Minimal stand-in for a NotificationChannel (duck-typed like the ABC)."""

    name = "recorder"

    def __init__(self):
        self.payloads = []

    def enabled(self):
        return True

    def send(self, payload):
        self.payloads.append(payload)


class ExplodingChannel:
    """Enabled channel whose send() always fails - ledger must stay empty."""

    name = "exploder"

    def __init__(self):
        self.attempts = 0

    def enabled(self):
        return True

    def send(self, payload):
        self.attempts += 1
        raise RuntimeError("channel down")


async def _seed_event(title="Change the filter", start_date=None):
    """Insert a one-time event; returns its id."""
    if start_date is None:
        from datetime import date

        start_date = date.today().isoformat()
    async with get_db_context() as db:
        cur = await db.execute(
            "INSERT INTO calendar_events (title, start_date, recurrence_type)"
            " VALUES (?, ?, 'none')",
            (title, start_date),
        )
        await db.commit()
        return cur.lastrowid


def _ledger_rows():
    async def _fetch():
        async with get_db_context() as db:
            cur = await db.execute(
                "SELECT event_id, due_date, kind FROM notification_log"
            )
            return [tuple(r) for r in await cur.fetchall()]

    return asyncio.run(_fetch())


# --- run_tick ordering (issue #9) ------------------------------------------

def test_run_tick_does_no_db_work_without_enabled_channels(monkeypatch):
    """No enabled channel -> return 0 BEFORE any calendar scan."""
    monkeypatch.setattr(notifier, "CHANNELS", [])

    calls = []

    async def spy_candidates(*args, **kwargs):
        calls.append(1)
        return []

    monkeypatch.setattr(
        notifier.calendar_service, "list_notify_candidates", spy_candidates
    )

    assert asyncio.run(notifier.run_tick()) == 0
    assert calls == [], "run_tick must not scan the calendar when no channel is enabled"


def test_run_tick_does_no_db_work_when_only_disabled_channels(monkeypatch):
    """A configured-but-disabled channel behaves like none at all."""

    class DisabledChannel(RecordingChannel):
        def enabled(self):
            return False

    monkeypatch.setattr(notifier, "CHANNELS", [DisabledChannel()])

    calls = []

    async def spy_candidates(*args, **kwargs):
        calls.append(1)
        return []

    monkeypatch.setattr(
        notifier.calendar_service, "list_notify_candidates", spy_candidates
    )

    assert asyncio.run(notifier.run_tick()) == 0
    assert calls == []


# --- run_tick happy path still works after the reorder ----------------------

def test_run_tick_delivers_and_ledgers(db_env, monkeypatch):
    channel = RecordingChannel()
    monkeypatch.setattr(notifier, "CHANNELS", [channel])

    event_id = asyncio.run(_seed_event())

    assert asyncio.run(notifier.run_tick()) == 1
    assert len(channel.payloads) == 1
    payload = channel.payloads[0]
    assert payload["event_id"] == event_id
    assert payload["title"] == "Change the filter"
    # One-time event dated today fires as "due".
    assert payload["type"] == "calendar_due"

    rows = _ledger_rows()
    assert len(rows) == 1 and rows[0][0] == event_id

    # Second tick: ledger dedupe -> nothing re-sent, no new rows.
    assert asyncio.run(notifier.run_tick()) == 0
    assert len(channel.payloads) == 1
    assert len(_ledger_rows()) == 1


def test_run_tick_failed_send_writes_no_ledger(db_env, monkeypatch):
    """Delivery failure must not mark the occurrence as sent (retry next tick)."""
    channel = ExplodingChannel()
    monkeypatch.setattr(notifier, "CHANNELS", [channel])

    asyncio.run(_seed_event())

    assert asyncio.run(notifier.run_tick()) == 0
    assert channel.attempts == 1
    assert _ledger_rows() == []


# --- /health access-log filter (main.py) ------------------------------------

def _access_record(method: str, path: str) -> logging.LogRecord:
    """Build a record shaped exactly like uvicorn.access produces:
    logger.info('%s - "%s %s HTTP/%s" %d', addr, method, path, ver, status).
    """
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:54321", method, path, "1.1", 200),
        exc_info=None,
    )


def test_health_access_filter_drops_only_health():
    from homestew.main import _HealthAccessFilter

    filt = _HealthAccessFilter()
    assert filt.filter(_access_record("GET", "/health")) is False
    assert filt.filter(_access_record("HEAD", "/health")) is False
    assert filt.filter(_access_record("GET", "/health?probe=1")) is False
    assert filt.filter(_access_record("GET", "/api/devices")) is True
    # A path merely STARTING with /health (none exist today) must survive.
    assert filt.filter(_access_record("GET", "/healthz")) is True
    # Non-GET/HEAD on /health stays logged (defensive, shouldn't happen).
    assert filt.filter(_access_record("POST", "/health")) is True


def test_health_access_filter_keeps_malformed_records():
    from homestew.main import _HealthAccessFilter

    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, "plain message", None, None
    )
    assert _HealthAccessFilter().filter(rec) is True


def test_install_health_access_log_filter_is_idempotent():
    from homestew.main import _HealthAccessFilter, install_health_access_log_filter

    access_logger = logging.getLogger("uvicorn.access")
    saved_filters = list(access_logger.filters)

    def ours_count() -> int:
        return sum(isinstance(f, _HealthAccessFilter) for f in access_logger.filters)

    try:
        # A lifespan from an earlier TestClient may already have installed
        # one - the point of idempotency is that repeated installs never
        # stack duplicates.
        install_health_access_log_filter()
        assert ours_count() == 1
        install_health_access_log_filter()
        assert ours_count() == 1
    finally:
        access_logger.filters = saved_filters
