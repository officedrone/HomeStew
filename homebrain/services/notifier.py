"""Background notifications: watch calendar events and dispatch alerts.

A single asyncio task (started from the FastAPI lifespan in main.py) wakes up
every NOTIFY_CHECK_INTERVAL_MINUTES minutes, looks for calendar events that
are overdue or coming due within the configured lead time, and sends one
notification per event through every enabled channel.

Dedupe: after all channels succeed, a row is written to notification_log
keyed on (event_id, due_date, kind), so each occurrence of an event alerts at
most once for "due" and once for "overdue". Recurring events re-alert on their
next occurrence because the due date changes. A failed send writes no ledger
row, so it is retried automatically on the following tick.

All settings are read from the live singleton every tick, so changes made in
Settings > Notifications apply without restarting the container.
"""
import asyncio
import logging
import math
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi.concurrency import run_in_threadpool

from homebrain.config import settings
from homebrain.db import get_db_context
from homebrain.services import calendar_service
from homebrain.services.notify_channels import CHANNELS

logger = logging.getLogger(__name__)


def lead_timedelta() -> timedelta:
    """The configured lead time (value + hours/days unit) as a timedelta."""
    value = max(1, int(settings.NOTIFY_LEAD_VALUE or 1))
    if (settings.NOTIFY_LEAD_UNIT or "hours").lower() == "days":
        return timedelta(days=value)
    return timedelta(hours=value)


def _due_datetime(event: Dict[str, Any]) -> Optional[datetime]:
    """Combine an event's next due date with its optional time of day."""
    due = event.get("next_due_date")
    if due is None:
        return None
    return datetime.combine(due, event.get("start_time") or time.min)


def build_payload(
    event: Dict[str, Any], kind: str, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Generic JSON notification body for one calendar event.

    ``kind`` is "due" (coming up within the lead window) or "overdue". The
    shape is deliberately plain so any webhook receiver can pick the fields
    it cares about without a per-service formatter.
    """
    now = now or datetime.now()
    due_dt = _due_datetime(event)
    hours_until_due: Optional[float] = None
    if due_dt is not None:
        hours_until_due = round((due_dt - now).total_seconds() / 3600.0, 2)

    return {
        "source": settings.APP_NAME,
        "type": "calendar_overdue" if kind == "overdue" else "calendar_due",
        "event_id": event.get("id"),
        "title": event.get("title"),
        "description": event.get("description"),
        "device_name": event.get("device_name"),
        "due_date": due_dt.date().isoformat() if due_dt else None,
        "due_time": (
            due_dt.strftime("%H:%M") if due_dt and event.get("start_time") else None
        ),
        "recurrence": event.get("recurrence_label"),
        "status": event.get("status"),
        "hours_until_due": hours_until_due,
        "sent_at": now.isoformat(timespec="seconds"),
    }


def build_test_payload() -> Dict[str, Any]:
    """Sample payload for the Settings > Notifications "Send Test" button."""
    now = datetime.now()
    sample = {
        "id": 0,
        "title": "Test notification",
        "description": "This is a test of the HomeBrain webhook channel.",
        "device_name": None,
        "next_due_date": (now + timedelta(days=1)).date(),
        "start_time": time(9, 0),
        "recurrence_label": "One-time",
        "status": "upcoming",
    }
    payload = build_payload(sample, kind="due", now=now)
    payload["test"] = True
    return payload


async def _already_sent() -> Set[Tuple[int, str, str]]:
    """Ledger of (event_id, due_date, kind) tuples already notified."""
    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT event_id, due_date, kind FROM notification_log"
        )
        rows = await cursor.fetchall()
    return {(r["event_id"], str(r["due_date"])[:10], r["kind"]) for r in rows}


async def run_tick(now: Optional[datetime] = None) -> int:
    """One scan-and-notify pass. Returns the number of notifications sent."""
    now = now or datetime.now()
    lead = lead_timedelta()

    # list_notify_candidates keeps overdue + due-on-or-before today+N (and,
    # unlike the calendar tab's view, also missed one-time events — they are
    # pending, just overdue). ceil-ing the lead to whole days is a cheap
    # pre-filter; the exact cutoff incl. start_time is applied below.
    window_days = max(1, math.ceil(lead.total_seconds() / 86400))
    events = await calendar_service.list_notify_candidates(window_days)

    channels: List[Any] = [c for c in CHANNELS if c.enabled()]
    if not channels:
        return 0

    sent = await _already_sent()
    fired = 0
    for event in events:
        due_dt = _due_datetime(event)
        if due_dt is None:
            continue

        kind = "overdue" if event.get("status") == "overdue" else "due"
        if kind == "due" and (due_dt - now) > lead:
            continue  # still outside the lead window

        key = (event["id"], due_dt.date().isoformat(), kind)
        if key in sent:
            continue  # this occurrence already alerted

        payload = build_payload(event, kind, now)
        delivered = True
        for channel in channels:
            try:
                await run_in_threadpool(channel.send, payload)
            except Exception as exc:
                delivered = False
                logger.warning(
                    "Notification channel %s failed for event %s (%s): %s",
                    channel.name, event["id"], kind, exc,
                )
                break  # no ledger row -> the whole alert retries next tick
        if not delivered:
            continue

        async with get_db_context() as db:
            await db.execute(
                "INSERT OR IGNORE INTO notification_log (event_id, due_date, kind)"
                " VALUES (?, ?, ?)",
                key,
            )
            await db.commit()
        sent.add(key)
        fired += 1
        logger.info(
            "Notified (%s): %r due %s", kind, event["title"], due_dt.date()
        )

    return fired


async def notification_loop() -> None:
    """Long-running background task; runs until the app shuts down.

    Sleeps first so a container restart never fires an immediate burst (the
    ledger would dedupe it anyway). The interval is re-read every iteration,
    and a failing tick is logged but never kills the loop.
    """
    logger.info("Notification loop started")
    while True:
        interval = min(max(int(settings.NOTIFY_CHECK_INTERVAL_MINUTES or 15), 1), 1440)
        await asyncio.sleep(interval * 60)
        try:
            if not settings.NOTIFY_ENABLED:
                continue
            count = await run_tick()
            if count:
                logger.info("Notification tick sent %d alert(s)", count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Notification tick failed")
