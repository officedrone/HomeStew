"""Calendar event API endpoints — device maintenance reminders."""
import logging
from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from homebrain.db import get_db_context
from homebrain.models.schemas import (
    CalendarEventCreate,
    CalendarEventResponse,
    CalendarEventUpdate,
    UpcomingEventsResponse,
)
from homebrain.services import calendar_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calendar", tags=["calendar"])


async def _ensure_device_exists(device_id: int) -> None:
    async with get_db_context() as db:
        cursor = await db.execute("SELECT id FROM devices WHERE id = ?", (device_id,))
        if not await cursor.fetchone():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {device_id} not found",
            )


@router.post("", response_model=CalendarEventResponse, status_code=status.HTTP_201_CREATED)
async def create_event(event: CalendarEventCreate):
    """Create a calendar event, optionally tied to a device."""
    if event.device_id is not None:
        await _ensure_device_exists(event.device_id)

    async with get_db_context() as db:
        cursor = await db.execute(
            """
            INSERT INTO calendar_events
                (device_id, title, description, start_date, start_time,
                 recurrence_type, interval)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.device_id,
                event.title,
                event.description or "",
                event.start_date.isoformat(),
                event.start_time.strftime("%H:%M") if event.start_time else None,
                event.recurrence_type,
                max(1, event.interval),
            ),
        )
        await db.commit()
        new_id = cursor.lastrowid

    created = await calendar_service.get_event(new_id)
    return CalendarEventResponse(**created)


@router.get("", response_model=List[CalendarEventResponse])
async def list_events(
    device_id: Optional[int] = Query(None, description="Filter by device"),
    within_days: Optional[int] = Query(
        None, ge=1, le=365,
        description="Only events due within N days (overdue included)",
    ),
):
    """List calendar events with their computed next due dates."""
    events = await calendar_service.list_events(
        device_id=device_id, within_days=within_days
    )
    return [CalendarEventResponse(**e) for e in events]


@router.get("/upcoming", response_model=UpcomingEventsResponse)
async def upcoming_events(days: int = Query(7, ge=1, le=90)):
    """Events due within the next N days (overdue included) — sidebar feed."""
    events = await calendar_service.list_events(within_days=days)
    return UpcomingEventsResponse(days=days, events=[CalendarEventResponse(**e) for e in events])


@router.get("/{event_id}", response_model=CalendarEventResponse)
async def get_event(event_id: int):
    """Get one event with its computed schedule."""
    event = await calendar_service.get_event(event_id)
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Event {event_id} not found"
        )
    return CalendarEventResponse(**event)


@router.put("/{event_id}", response_model=CalendarEventResponse)
async def update_event(event_id: int, update: CalendarEventUpdate):
    """Update an event's fields (title, schedule, device link, ...)."""
    existing = await calendar_service.get_event(event_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Event {event_id} not found"
        )

    fields = update.model_dump(exclude_unset=True)
    if fields.get("device_id") is not None:
        await _ensure_device_exists(fields["device_id"])

    updates, params = [], []
    for key in ("title", "description", "recurrence_type"):
        if key in fields:
            updates.append(f"{key} = ?")
            params.append(fields[key] if fields[key] is not None else "")
    if "device_id" in fields:
        updates.append("device_id = ?")
        params.append(fields["device_id"])
    if "start_date" in fields and fields["start_date"] is not None:
        updates.append("start_date = ?")
        params.append(fields["start_date"].isoformat())
    if "start_time" in fields:
        updates.append("start_time = ?")
        params.append(
            fields["start_time"].strftime("%H:%M") if fields["start_time"] else None
        )
    if "interval" in fields and fields["interval"] is not None:
        updates.append("interval = ?")
        params.append(max(1, fields["interval"]))

    if updates:
        params.append(event_id)
        async with get_db_context() as db:
            await db.execute(
                f"UPDATE calendar_events SET {', '.join(updates)} WHERE id = ?", params
            )
            # Editing the schedule restarts completion tracking, otherwise a
            # just-completed event would hide its new first occurrence.
            if {"start_date", "recurrence_type", "interval"} & set(fields):
                await db.execute(
                    "UPDATE calendar_events SET last_completed_at = NULL WHERE id = ?",
                    (event_id,),
                )
            await db.commit()

    updated = await calendar_service.get_event(event_id)
    return CalendarEventResponse(**updated)


@router.post("/{event_id}/complete", response_model=CalendarEventResponse)
async def complete_event(event_id: int):
    """Mark the current occurrence as done.

    Recurring events roll forward to their next occurrence; one-time events
    become 'done'.
    """
    existing = await calendar_service.get_event(event_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Event {event_id} not found"
        )

    async with get_db_context() as db:
        await db.execute(
            "UPDATE calendar_events SET last_completed_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), event_id),
        )
        await db.commit()

    updated = await calendar_service.get_event(event_id)
    return CalendarEventResponse(**updated)


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(event_id: int):
    """Delete a calendar event."""
    async with get_db_context() as db:
        cursor = await db.execute("SELECT id FROM calendar_events WHERE id = ?", (event_id,))
        if not await cursor.fetchone():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"Event {event_id} not found"
            )
        await db.execute("DELETE FROM calendar_events WHERE id = ?", (event_id,))
        await db.commit()
