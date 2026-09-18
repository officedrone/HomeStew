"""Database access for calendar events.

Kept separate from the API router so the chat endpoints can reuse the same
queries (e.g. injecting upcoming maintenance into the system prompt) without
importing FastAPI code paths.
"""
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from homestew.db import get_db_context
from homestew.services.calendar_engine import (
    describe_schedule,
    event_status,
    next_due_date,
)

logger = logging.getLogger(__name__)


def _parse_date(value: Any) -> Optional[date]:
    """Coerce a SQLite DATE/TIMESTAMP value to a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_datetime(value: Any) -> Optional[datetime]:
    """Coerce a SQLite TIMESTAMP value to a datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _row_to_event(row: Any, today: date) -> Dict[str, Any]:
    """Expand a calendar_events row with computed schedule fields."""
    start_date = _parse_date(row["start_date"]) or today
    completed_at = _parse_datetime(
        row["last_completed_at"] if "last_completed_at" in row.keys() else None
    )
    recurrence_type = row["recurrence_type"] or "none"
    interval = row["interval"] or 1

    due = next_due_date(
        start_date=start_date,
        recurrence_type=recurrence_type,
        interval=interval,
        today=today,
        completed_on=completed_at.date() if completed_at else None,
    )

    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "device_name": row["device_name"] if "device_name" in row.keys() else None,
        "title": row["title"],
        "description": row["description"],
        "start_date": start_date,
        "start_time": _parse_time(row["start_time"]) if "start_time" in row.keys() else None,
        "recurrence_type": recurrence_type,
        "interval": interval,
        "recurrence_label": describe_schedule(recurrence_type, interval),
        "last_completed_at": completed_at,
        "created_at": _parse_datetime(row["created_at"]) or datetime.now(),
        "next_due_date": due,
        "status": event_status(due, today),
    }


def _parse_time(value: Any):
    """Coerce a SQLite TIME value to a time object."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.time()
    text = str(value).strip()
    try:
        from datetime import time as _time

        parts = text.split(":")
        return _time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return None


_SELECT = """
    SELECT e.id, e.device_id, e.title, e.description, e.start_date, e.start_time,
           e.recurrence_type, e.interval, e.last_completed_at, e.created_at,
           d.name AS device_name
    FROM calendar_events e
    LEFT JOIN devices d ON d.id = e.device_id
"""


async def list_events(
    device_id: Optional[int] = None,
    within_days: Optional[int] = None,
    today: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """All events with computed due dates, sorted by soonest first.

    ``within_days`` keeps only overdue-or-upcoming events whose next due date
    falls on/before today + N days; completed one-time events are always kept
    when no window is requested so the Calendar tab can still show them.
    """
    today = today or date.today()

    async with get_db_context() as db:
        sql = _SELECT
        params: List[Any] = []
        if device_id is not None:
            sql += " WHERE e.device_id = ?"
            params.append(device_id)
        sql += " ORDER BY e.start_date ASC, e.id ASC"
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()

    events = [_row_to_event(r, today) for r in rows]

    if within_days is not None:
        horizon = today.toordinal() + within_days
        events = [
            e for e in events
            if e["next_due_date"] is not None and e["next_due_date"].toordinal() <= horizon
        ]

    # Soonest first; finished one-time events sink to the bottom.
    events.sort(key=lambda e: (e["next_due_date"] or date.max, e["id"]))
    return events


async def get_event(event_id: int) -> Optional[Dict[str, Any]]:
    """One event by id with computed fields, or None."""
    today = date.today()
    async with get_db_context() as db:
        cursor = await db.execute(_SELECT + " WHERE e.id = ?", (event_id,))
        row = await cursor.fetchone()
    return _row_to_event(row, today) if row else None


async def upcoming_for_device(
    device_id: Optional[int], days: int = 7
) -> List[Dict[str, Any]]:
    """Events due within ``days`` (overdue included), optionally per device."""
    return await list_events(device_id=device_id, within_days=days)


async def list_notify_candidates(
    within_days: int,
    today: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Events eligible for a due/overdue notification alert.

    Same shape as ``list_events`` rows except that an UNCOMPLETED one-time
    event whose date has passed keeps its (past) start_date as next_due_date
    with status 'overdue'. The regular list_events treats such events as done
    - which is right for the Calendar tab, but would make a missed one-time
    reminder invisible to the notifier. Completed events stay excluded.
    """
    today = today or date.today()
    async with get_db_context() as db:
        cursor = await db.execute(_SELECT + " ORDER BY e.start_date ASC, e.id ASC")
        rows = await cursor.fetchall()

    horizon = (today + timedelta(days=within_days)).toordinal()
    events: List[Dict[str, Any]] = []
    for row in rows:
        event = _row_to_event(row, today)
        if (
            event["next_due_date"] is None
            and event["recurrence_type"] == "none"
            and event["last_completed_at"] is None
        ):
            # Missed one-time occurrence: still pending, just overdue.
            event["next_due_date"] = event["start_date"]
            event["status"] = event_status(event["next_due_date"], today)
        if (
            event["next_due_date"] is not None
            and event["next_due_date"].toordinal() <= horizon
        ):
            events.append(event)

    events.sort(key=lambda e: (e["next_due_date"] or date.max, e["id"]))
    return events
