"""Maintenance-calendar recurrence engine.

A deliberately small, dependency-free implementation of the recurring-schedule
rules HomeBrain needs: one-time events plus "every N days / weeks / months /
years". Monthly and yearly occurrences clamp the day-of-month (an event
anchored on Jan 31 recurs on Feb 28, then Mar 31), which matches how calendar
apps handle "last day of month" style schedules.

Events store an anchor ``start_date``; the *next due date* is always computed
from it rather than mutated, so editing or completing an event can never drift
the schedule:

- one-time events are due on their start date and stay done once completed;
- recurring events roll forward to the first occurrence after "today" — or,
  when they were just completed, strictly after the completion day, so
  clicking "Done" today never leaves the event due again today.
"""
from datetime import date, timedelta
from typing import Optional

RECURRENCE_TYPES = ("none", "daily", "weekly", "monthly", "yearly")


def add_months(d: date, months: int) -> date:
    """Add whole months to a date, clamping the day (Jan 31 +1mo -> Feb 28)."""
    total = (d.year * 12 + (d.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1
    # Highest valid day in the target month.
    if month == 12:
        last_day = 31
    else:
        last_day = (date(year, month + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(d.day, last_day))


def occurrence(start_date: date, recurrence_type: str, interval: int, k: int) -> date:
    """The k-th occurrence (k >= 0) of a schedule anchored at ``start_date``."""
    interval = max(1, interval or 1)
    step = interval * k
    if recurrence_type == "daily":
        return start_date + timedelta(days=step)
    if recurrence_type == "weekly":
        return start_date + timedelta(weeks=step)
    if recurrence_type == "monthly":
        return add_months(start_date, step)
    if recurrence_type == "yearly":
        return add_months(start_date, 12 * step)
    return start_date


def next_due_date(
    start_date: date,
    recurrence_type: str,
    interval: int,
    today: date,
    completed_on: Optional[date] = None,
) -> Optional[date]:
    """First due date on/after ``today`` (or after ``completed_on``).

    Returns None for one-time events that are already done or whose single
    occurrence lies in the past relative to the reference day.
    """
    if recurrence_type not in RECURRENCE_TYPES or recurrence_type == "none":
        # One-time: due on its date; a completion (completed_on set) means done.
        if completed_on is not None:
            return None
        return start_date if start_date >= today else None

    ref = max(today, completed_on) if completed_on else today
    # Recurring events that were just completed roll to the NEXT occurrence;
    # otherwise the current one (>= today) is still pending.
    strict = completed_on is not None and completed_on >= today

    d = start_date
    k = 0
    # Guard against pathological anchors: bail out after ~50 years of steps so
    # a bogus interval can never spin forever.
    while True:
        if (d > ref) if strict else (d >= ref):
            return d
        k += 1
        if k > 2600:  # ~50 years of weekly steps — far beyond any real use
            return None
        d = occurrence(start_date, recurrence_type, interval, k)


def describe_schedule(recurrence_type: str, interval: int) -> str:
    """Human-readable schedule text for UI and LLM prompts."""
    interval = max(1, interval or 1)
    if recurrence_type == "daily":
        return "Daily" if interval == 1 else f"Every {interval} days"
    if recurrence_type == "weekly":
        return "Weekly" if interval == 1 else f"Every {interval} weeks"
    if recurrence_type == "monthly":
        return "Monthly" if interval == 1 else f"Every {interval} months"
    if recurrence_type == "yearly":
        return "Yearly" if interval == 1 else f"Every {interval} years"
    return "One-time"


def event_status(next_due: Optional[date], today: date) -> str:
    """Classify a computed due date against today."""
    if next_due is None:
        return "done"
    if next_due < today:
        return "overdue"
    if next_due == today:
        return "today"
    return "upcoming"
