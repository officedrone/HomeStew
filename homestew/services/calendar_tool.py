"""Execution of the manage_calendar tool call made by the LLM.

The chat exposes one tool for the whole calendar (create / update / delete /
complete / list) instead of five separate ones: small local models pick the
right action from a single well-described schema far more reliably than they
juggle several tools, and one call keeps the trace readable in the UI.

Device scoping mirrors the search tool: when the chat is filtered to a device
(``chat_device_id``), every action is forced onto that device - an event can
be created for it, but events belonging to other devices are invisible and
untouchable from this conversation. The LLM can never widen that scope.

Every handler returns a plain-text report aimed at the model: what changed,
the ids involved and enough schedule detail (next due date, recurrence) for
the model to confirm the change without guessing. Errors come back as text
too - raising here would surface a generic "tool failed" message instead of
something the model can explain or retry from.
"""
import json
import logging
from datetime import date, datetime, time as dtime
from typing import Any, Dict, Optional, Tuple

from homestew.db import get_db_context
from homestew.services import calendar_service
from homestew.services.device_tool import device_link_note

logger = logging.getLogger(__name__)

_ACTIONS = ("create", "update", "delete", "complete", "list")
_RECURRENCE = ("none", "daily", "weekly", "monthly", "yearly")


def _parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _parse_time(value: Any) -> Tuple[Optional[dtime], bool]:
    """Parse HH:MM (or HH:MM:SS); returns (value, ok). ok=False = bad format."""
    if value is None or str(value).strip() == "":
        return None, True
    text = str(value).strip().replace("T", " ")[:8]
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time(), True
        except ValueError:
            continue
    return None, False


def _device_note(device_id: Optional[int], device_name: Optional[str]) -> str:
    if not device_id:
        return " (no device)"
    return f" (device_id={device_id}{': ' + device_name if device_name else ''})"


def _event_line(event: Dict[str, Any]) -> str:
    """One-line summary of an event with everything the model needs.

    The id is NOT included - callers prefix it (``id=7: ...``) so list and
    mutation reports share one format.
    """
    due = event["next_due_date"]
    status_word = {"overdue": "OVERDUE", "today": "due TODAY", "done": "done"}.get(
        event["status"], f"due {due.isoformat()}" if due else "no further date"
    )
    when = f", time {event['start_time'].strftime('%H:%M')}" if event.get("start_time") else ""
    return (
        f'"{event["title"]}" - {event["recurrence_label"]}'
        f"{when}, {status_word}{_device_note(event.get('device_id'), event.get('device_name'))}"
        + (f" - {event['description']}" if event.get("description") else "")
    )


def _device_link_for(event: Dict[str, Any]) -> str:
    """Device-link suffix for a mutation report ('' when no device linked).

    Every calendar change touching a device ends with that device's chat
    link so the user can jump straight to it - same rule as manage_devices.
    The event row must still carry device_id/device_name (deleted events do,
    they are reported from the pre-delete fetch).
    """
    if not event.get("device_id") or not event.get("device_name"):
        return ""
    return device_link_note(event["device_name"], event["device_id"])


async def _device_exists(db, device_id: int) -> bool:
    cursor = await db.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,))
    return await cursor.fetchone() is not None


def _coerce_device_id(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1  # sentinel: caller reports a bad id


async def _create(args: Dict[str, Any], chat_device_id: Optional[int]) -> str:
    title = str(args.get("title") or "").strip()
    if not title:
        return "Error: 'title' is required to create a calendar event."

    start_date = _parse_date(args.get("start_date"))
    if start_date is None:
        return (
            "Error: 'start_date' is required and must be an ISO date "
            "(YYYY-MM-DD). Resolve relative dates against today before retrying."
        )

    recurrence = str(args.get("recurrence_type") or "none").strip().lower()
    if recurrence not in _RECURRENCE:
        return (
            f"Error: unknown recurrence_type '{recurrence}'. Use one of: "
            + ", ".join(_RECURRENCE)
            + "."
        )

    try:
        interval = int(args.get("interval") or 1)
    except (TypeError, ValueError):
        interval = 1
    interval = max(1, min(interval, 3650))

    start_time, ok = _parse_time(args.get("start_time"))
    if not ok:
        return "Error: 'start_time' must be HH:MM (24h), e.g. 08:30."

    # The chat's device filter always wins over whatever id the model sends.
    device_id = chat_device_id
    if device_id is None and args.get("device_id") not in (None, ""):
        device_id = _coerce_device_id(args.get("device_id"))
        if device_id == -1:
            return "Error: 'device_id' must be an integer from the device list."

    async with get_db_context() as db:
        if device_id is not None and not await _device_exists(db, device_id):
            return (
                f"Error: device_id={device_id} does not exist. Use a real id "
                "from the conversation's device list, or omit device_id."
            )
        cursor = await db.execute(
            """
            INSERT INTO calendar_events
                (device_id, title, description, start_date, start_time,
                 recurrence_type, interval)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                device_id,
                title[:200],
                str(args.get("description") or "")[:2000],
                start_date.isoformat(),
                start_time.strftime("%H:%M") if start_time else None,
                recurrence,
                interval,
            ),
        )
        await db.commit()
        new_id = cursor.lastrowid

    created = await calendar_service.get_event(new_id)
    if not created:
        return f"Created event id={new_id}."
    return f"Created event id={new_id}: {_event_line(created)}" + _device_link_for(created)


async def _load_scoped_event(
    event_id: int, chat_device_id: Optional[int]
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Fetch an event and enforce the chat's device scope."""
    try:
        event_id = int(event_id)
    except (TypeError, ValueError):
        return None, "Error: 'event_id' must be a number. Use action='list' to find ids."

    event = await calendar_service.get_event(event_id)
    if not event:
        return None, (
            f"Error: no calendar event with id={event_id}. Call action='list' "
            "to see the real event ids."
        )
    # In a device-filtered chat, events of other devices must stay invisible.
    if chat_device_id is not None and event["device_id"] not in (None, chat_device_id):
        return None, (
            f"Error: event id={event_id} belongs to another device, not the "
            "one this conversation is filtered to."
        )
    return event, ""


async def _update(args: Dict[str, Any], chat_device_id: Optional[int]) -> str:
    if args.get("event_id") in (None, ""):
        return "Error: 'event_id' is required. Use action='list' to find it."
    event, err = await _load_scoped_event(args["event_id"], chat_device_id)
    if err:
        return err

    fields: Dict[str, Any] = {}

    if args.get("title") not in (None, ""):
        fields["title"] = str(args["title"]).strip()[:200]
    if args.get("description") is not None:
        fields["description"] = str(args["description"])[:2000]

    if args.get("recurrence_type") not in (None, ""):
        recurrence = str(args["recurrence_type"]).strip().lower()
        if recurrence not in _RECURRENCE:
            return (
                f"Error: unknown recurrence_type '{recurrence}'. Use one of: "
                + ", ".join(_RECURRENCE)
                + "."
            )
        fields["recurrence_type"] = recurrence

    if args.get("interval") not in (None, ""):
        try:
            fields["interval"] = max(1, min(int(args["interval"]), 3650))
        except (TypeError, ValueError):
            return "Error: 'interval' must be a positive integer."

    if args.get("start_date") not in (None, ""):
        start_date = _parse_date(args["start_date"])
        if start_date is None:
            return "Error: 'start_date' must be an ISO date (YYYY-MM-DD)."
        fields["start_date"] = start_date

    if args.get("start_time") is not None:
        # Empty string clears the time; a bad format errors.
        if str(args["start_time"]).strip() == "":
            fields["start_time"] = None
        else:
            parsed, ok = _parse_time(args["start_time"])
            if not ok:
                return "Error: 'start_time' must be HH:MM (24h) or empty to clear."
            fields["start_time"] = parsed

    # device_id: only honoured in an unfiltered chat (a filtered conversation
    # cannot move events between devices). -1 clears the link.
    if args.get("device_id") is not None and chat_device_id is None:
        raw = args["device_id"]
        if str(raw).strip() in ("", "-1"):
            fields["device_id"] = None
        else:
            device_id = _coerce_device_id(raw)
            async with get_db_context() as db:
                if device_id == -1 or not await _device_exists(db, device_id):
                    return (
                        f"Error: device_id={raw} does not exist. Use a real id "
                        "from the conversation's device list."
                    )
            fields["device_id"] = device_id

    if not fields:
        return (
            "Nothing to change: no updatable fields were given. Pass at least "
            "one of title, description, start_date, start_time, "
            "recurrence_type, interval, device_id."
        )

    updates, params = [], []
    for key in ("title", "description", "recurrence_type"):
        if key in fields:
            updates.append(f"{key} = ?")
            params.append(fields[key])
    if "device_id" in fields:
        updates.append("device_id = ?")
        params.append(fields["device_id"])
    if "start_date" in fields:
        updates.append("start_date = ?")
        params.append(fields["start_date"].isoformat())
    if "start_time" in fields:
        updates.append("start_time = ?")
        params.append(
            fields["start_time"].strftime("%H:%M") if fields["start_time"] else None
        )
    if "interval" in fields:
        updates.append("interval = ?")
        params.append(fields["interval"])

    params.append(event["id"])
    async with get_db_context() as db:
        await db.execute(
            f"UPDATE calendar_events SET {', '.join(updates)} WHERE id = ?", params
        )
        # Same rule as the REST API: a changed schedule restarts completion
        # tracking so the new first occurrence is not hidden.
        if {"start_date", "recurrence_type", "interval"} & set(fields):
            await db.execute(
                "UPDATE calendar_events SET last_completed_at = NULL WHERE id = ?",
                (event["id"],),
            )
        await db.commit()

    updated = await calendar_service.get_event(event["id"])
    changed = ", ".join(sorted(fields.keys()))
    return (
        f"Updated event id={event['id']} ({changed}): {_event_line(updated)}"
        + _device_link_for(updated)
    )


async def _delete(args: Dict[str, Any], chat_device_id: Optional[int]) -> str:
    if args.get("event_id") in (None, ""):
        return "Error: 'event_id' is required. Use action='list' to find it."
    event, err = await _load_scoped_event(args["event_id"], chat_device_id)
    if err:
        return err

    async with get_db_context() as db:
        await db.execute("DELETE FROM calendar_events WHERE id = ?", (event["id"],))
        await db.commit()
    return f'Deleted event "{event["title"]}" (id={event["id"]}).' + _device_link_for(event)


async def _complete(args: Dict[str, Any], chat_device_id: Optional[int]) -> str:
    if args.get("event_id") in (None, ""):
        return "Error: 'event_id' is required. Use action='list' to find it."
    event, err = await _load_scoped_event(args["event_id"], chat_device_id)
    if err:
        return err

    async with get_db_context() as db:
        await db.execute(
            "UPDATE calendar_events SET last_completed_at = ? WHERE id = ?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), event["id"]),
        )
        await db.commit()

    updated = await calendar_service.get_event(event["id"])
    return (
        f'Marked "{updated["title"]}" (id={event["id"]}) done: '
        f"{_event_line(updated)}" + _device_link_for(updated)
    )


async def _list(args: Dict[str, Any], chat_device_id: Optional[int]) -> str:
    """List events; in a filtered chat only that device's (+ unassigned)."""
    within_days = None
    if args.get("within_days") not in (None, ""):
        try:
            within_days = max(1, min(int(args["within_days"]), 365))
        except (TypeError, ValueError):
            return "Error: 'within_days' must be a positive integer."

    filter_device = None if chat_device_id is not None else args.get("device_id") or None
    if filter_device is not None:
        filter_device = _coerce_device_id(filter_device)
        async with get_db_context() as db:
            # Small models invent ids; an unknown one would just list nothing
            # and the model would report an empty calendar. Say what happened.
            if filter_device == -1 or not await _device_exists(db, filter_device):
                return (
                    f"Error: device_id={args.get('device_id')} does not exist. "
                    "Use a real id from the conversation's device list, or omit it."
                )

    if chat_device_id is not None:
        events = [
            e
            for e in await calendar_service.list_events(within_days=within_days)
            if e["device_id"] in (None, chat_device_id)
        ]
    else:
        events = await calendar_service.list_events(
            device_id=filter_device, within_days=within_days
        )

    if not events:
        return "The calendar has no matching events."
    lines = [f'- id={e["id"]}: {_event_line(e)}' for e in events[:30]]
    return f"{len(events)} calendar event(s):\n" + "\n".join(lines)


async def execute_calendar_tool(
    args: Dict[str, Any], chat_device_id: Optional[int]
) -> str:
    """Run one manage_calendar call and return the text report for the LLM."""
    action = str(args.get("action") or "").strip().lower()
    if action not in _ACTIONS:
        return (
            f"Error: unknown action '{action}'. Use one of: "
            + ", ".join(_ACTIONS)
            + "."
        )

    logger.info(
        "Executing calendar tool: %s (device filter=%s)", action, chat_device_id
    )
    try:
        if action == "create":
            return await _create(args, chat_device_id)
        if action == "update":
            return await _update(args, chat_device_id)
        if action == "delete":
            return await _delete(args, chat_device_id)
        if action == "complete":
            return await _complete(args, chat_device_id)
        return await _list(args, chat_device_id)
    except Exception as exc:  # noqa: BLE001 - report to the model, not a crash
        logger.error("Calendar tool call failed: %s", exc)
        return f"Error executing calendar action '{action}': {exc}"


def parse_tool_arguments(tool_call: Any) -> Dict[str, Any]:
    """Extract an argument dict from an SDK object or plain-dict tool call."""
    if hasattr(tool_call, "function"):
        raw = tool_call.function.arguments
    else:
        raw = tool_call["function"]["arguments"]
    try:
        args = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return args if isinstance(args, dict) else {}
