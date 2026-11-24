"""Background notifications: the single tick that drives every event source.

One asyncio task (started from the FastAPI lifespan in main.py) wakes up
every minute and runs two passes:

1. Calendar reminders (this module): once per the calendar event's own
   "Interval (min)" (Settings > Notifications table), scan for events
   overdue or coming due within the event's "Notify before due (hrs)" lead
   window and deliver each occurrence to the channels SUBSCRIBED for the
   ``calendar_reminders`` event (services/notifications/dispatch.py). An event with no
   subscribed channel is simply off - enablement is per event via its
   channel checkboxes, there is no master switch any more.
2. Health checks (services/ui_notifications.run_due_checks): evaluate every
   condition check whose own interval elapsed, maintaining the episode state
   machine and delivering new/undelivered episodes to their subscribed
   channels (webhook etc.). Runs on every wake-up - each check's own
   interval gates it, exactly as before.

Dedupe: a notification_log row is written per (event_id, due_date, kind,
channel) after that channel's send succeeds, so each occurrence alerts at
most once PER CHANNEL. Recurring events re-alert on their next occurrence
because the due date changes. A failed send writes no ledger row for that
channel, so it is retried automatically on the following tick without
re-sending to channels that already succeeded.

The idle-CPU guarantee (issue #9) survives the fan-out: a tick resolves the
subscribed-and-available channels BEFORE touching the database, so an install
that only subscribes events to in-app toasts does zero calendar work per tick
(the frontend poll remains the sole driver).

All settings are read from the live singleton every tick, so changes made in
Settings > Notifications apply without restarting the container.
"""
import asyncio
import logging
import math
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from homestew.db import get_db_context
from homestew.services.calendar import calendar_service
from homestew.services.notifications import dispatch
from homestew.services.notifications import ui_notifications
from homestew.services.notifications.dispatch import CALENDAR_EVENT_KEY, UI_CHANNEL

logger = logging.getLogger(__name__)


async def _already_sent() -> Set[Tuple[int, str, str, str]]:
    """Ledger of (event_id, due_date, kind, channel) tuples already notified."""
    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT event_id, due_date, kind, channel FROM notification_log"
        )
        rows = await cursor.fetchall()
    return {
        (r["event_id"], str(r["due_date"])[:10], r["kind"], r["channel"])
        for r in rows
    }


def subscribed_external_channels(sub: Dict[str, Any]) -> List[str]:
    """Subscribed non-UI channels that could deliver right now.

    The UI pseudo-channel needs no send (the frontend poll renders it), so a
    subscription list of just ["ui"] means this tick has nothing to push and
    must not touch the database at all - preserving the idle-CPU guarantee.
    """
    return [
        name for name in sub["channels"]
        if name != UI_CHANNEL and dispatch.channel_available(name)
    ]


async def run_tick(now: Optional[datetime] = None) -> int:
    """One calendar scan-and-notify pass. Returns occurrences newly delivered.

    Delivers each due/overdue occurrence to the external channels subscribed
    for the calendar event (services/notifications/dispatch.py), writing one ledger row per
    successfully delivered channel. Channels that already have a ledger row
    for this occurrence are skipped, so a partial failure retries only the
    failed channel on the next tick. Also backs the Settings "Check Now"
    button for the calendar row (POST /ui/calendar_reminders/check).
    """
    # Resolve channels FIRST (issue #9): with nothing subscribed-and-enabled
    # no alert can be pushed, so return before touching the database at all.
    sub = dispatch.effective_subscription(CALENDAR_EVENT_KEY)
    if not sub["enabled"]:
        return 0
    channels = subscribed_external_channels(sub)
    if not channels:
        return 0

    now = now or datetime.now()
    lead = dispatch.lead_timedelta()

    # list_notify_candidates keeps overdue + due-on-or-before today+N (and,
    # unlike the calendar tab's view, also missed one-time events - they are
    # pending, just overdue). ceil-ing the lead to whole days is a cheap
    # pre-filter; the exact cutoff incl. start_time is applied below.
    window_days = max(1, math.ceil(lead.total_seconds() / 86400))
    events = await calendar_service.list_notify_candidates(window_days)

    sent = await _already_sent()
    fired = 0
    for event in events:
        due_dt = dispatch.due_datetime(event)
        if due_dt is None:
            continue

        kind = "overdue" if event.get("status") == "overdue" else "due"
        if kind == "due" and (due_dt - now) > lead:
            continue  # still outside the lead window

        due_str = due_dt.date().isoformat()
        todo = [c for c in channels if (event["id"], due_str, kind, c) not in sent]
        if not todo:
            continue  # every subscribed channel already delivered this occurrence

        payload = dispatch.build_calendar_payload(event, kind, now)
        results = await dispatch.deliver(payload, todo)

        newly = False
        for name, ok in results.items():
            if not ok:
                continue  # no ledger row -> only this channel retries next tick
            async with get_db_context() as db:
                await db.execute(
                    "INSERT OR IGNORE INTO notification_log"
                    " (event_id, due_date, kind, channel) VALUES (?, ?, ?, ?)",
                    (event["id"], due_str, kind, name),
                )
                await db.commit()
            sent.add((event["id"], due_str, kind, name))
            newly = True
        if newly:
            fired += 1
            logger.info(
                "Notified (%s) via %s: %r due %s",
                kind, ",".join(n for n, ok in results.items() if ok),
                event["title"], due_dt.date(),
            )

    return fired


#: How often the loop wakes to compare per-event intervals (seconds). The
#: events' own "Interval (min)" values do the real gating; this only sets
#: their resolution, and a wake with nothing due costs no database work.
_LOOP_WAKE_SECONDS = 60


async def notification_loop() -> None:
    """Long-running background task; runs until the app shuts down.

    Sleeps first so a container restart never fires an immediate burst (the
    ledger would dedupe it anyway). Every wake compares the calendar event's
    per-event interval against the last scan, so changing "Interval (min)"
    in Settings applies without a restart; a failing pass is logged but
    never kills the loop. Health checks run on every wake-up (their own
    per-event interval gates them).
    """
    logger.info("Notification loop started")
    # Seed with "now" so the first calendar scan happens one full interval
    # after startup, mirroring the old sleep-first behaviour.
    last_calendar_scan = datetime.now()
    while True:
        await asyncio.sleep(_LOOP_WAKE_SECONDS)
        try:
            interval = (
                dispatch.effective_subscription(CALENDAR_EVENT_KEY)[
                    "interval_minutes"
                ]
                or 15
            )
            now = datetime.now()
            if (now - last_calendar_scan).total_seconds() >= interval * 60:
                last_calendar_scan = now
                count = await run_tick()
                if count:
                    logger.info("Notification tick sent %d alert(s)", count)
            # Health checks: evaluate due conditions and push new episodes to
            # their subscribed channels. Cheap by design - each check only
            # re-evaluates once its own interval elapsed.
            await ui_notifications.run_due_checks()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Notification tick failed")
