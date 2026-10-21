"""Condition-check framework: health events piped to subscribed channels.

This module owns the *sources*: condition checks like "manuals are missing
from the search index". Each check runs an evaluator on its own interval and
maintains an episode state machine so each continuous stretch where a
condition holds counts as one occurrence:

* condition flips false -> true   : new episode (``episode_started_at`` set,
  any previous dismissal cleared) - the toast appears;
* condition stays true            : the episode persists - a Dismissed check
  stays silent, an undismissed one keeps showing;
* condition flips true -> false   : the episode closes silently, so a later
  recurrence notifies again.

Dismissal is therefore "never show *this occurrence* again": it survives page
reloads and container restarts (state lives in ``ui_notification_state``) but
never suppresses a genuinely new problem.

The delivery side is unified: every check registers an EventDef with
services/dispatch.py, so Settings > Notifications shows one row per event
with channel checkboxes (Toast / Webhook / future channels). Evaluation
happens in ``run_due_checks()``, driven by the background notifier loop -
webhook-subscribed health alerts therefore fire even with no browser open.
The frontend poll (``collect()``) is a READ-ONLY snapshot of persisted state
plus live calendar candidates, so polling costs nothing beyond two queries.
While a search-index job is running, evaluation is skipped *without
advancing* ``last_checked_at``: a rebuild drops the FTS table, so every manual
would momentarily look un-indexed.

Adding a new health event = one evaluator function + one entry in ``CHECKS``
below. The EventDef registration, the Settings row, and the frontend all
build themselves from the registry, so no other file needs to change.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from homestew.config import settings
from homestew.db import get_db_context
from homestew.services import calendar_service, dispatch
from homestew.services.dispatch import CALENDAR_EVENT_KEY, UI_CHANNEL, EventDef

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse a stored timestamp; naive strings are assumed UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Check registry - the extension point of this framework
# ---------------------------------------------------------------------------

# An evaluator returns a dict of details when the condition currently holds,
# or None when it does not. The dict is persisted (JSON) and handed back to
# the message builder on every poll until the episode ends.
Evaluator = Callable[[], Awaitable[Optional[Dict[str, Any]]]]
MessageBuilder = Callable[[Dict[str, Any]], str]


@dataclass(frozen=True)
class UINotificationCheck:
    """Definition of one condition check."""
    key: str
    label: str
    default_interval_minutes: int
    evaluate: Evaluator
    message: MessageBuilder
    severity: str = "warn"
    goto_tab: str = "settings"
    goto_section: Optional[str] = None
    action_label: str = "Go to Settings"
    # Details handed to the message builder by the Settings "Test" button so
    # the preview toast reads like a real one (never persisted or evaluated).
    sample_details: Dict[str, Any] = field(default_factory=dict)


async def _count_unindexed_manuals() -> Optional[Dict[str, Any]]:
    """Manuals that exist but have zero rows in the search index.

    Same "0 pages" definition the Settings > Search table shows per manual;
    NOT EXISTS short-circuits once a page row is found for a manual.
    """
    async with get_db_context() as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS n FROM manuals m
            WHERE NOT EXISTS (
                SELECT 1 FROM pdf_index p WHERE p.manual_id = m.id
            )
            """
        )
        row = await cursor.fetchone()
    count = int(row["n"]) if row else 0
    return {"count": count} if count > 0 else None


def _unindexed_message(details: Dict[str, Any]) -> str:
    n = int(details.get("count", 0))
    plural = "manuals are" if n != 1 else "manual is"
    return f"{n} {plural} not in the search index yet."


CHECKS: Dict[str, UINotificationCheck] = {
    c.key: c
    for c in (
        UINotificationCheck(
            key="unindexed_manuals",
            label="Un-indexed manuals",
            default_interval_minutes=1440,  # once a day
            evaluate=_count_unindexed_manuals,
            message=_unindexed_message,
            severity="warn",
            goto_tab="settings",
            goto_section="search",
            action_label="Go to Search settings",
            sample_details={"count": 1},
        ),
    )
}


def sample_item(key: str) -> Dict[str, Any]:
    """A preview notification for the Settings "Test" button.

    Built from the event's registered sample data - no evaluation, no DB
    read and no state write - so the toast can be previewed even when the
    condition does not currently hold. The frontend renders it exactly like
    a real card but never offers Dismiss persistence for it (a Test card is
    session-local: ✕ or a reload clears it). Calendar previews mirror the
    sample payload dispatch builds for webhook test sends.
    """
    if key == CALENDAR_EVENT_KEY:
        return {
            "key": CALENDAR_EVENT_KEY,
            "dedup_key": "calendar:test",
            "kind": "rule",
            "severity": "warn",
            "message": "Due soon: Test notification",
            "count": None,
            "goto_tab": "calendar",
            "goto_section": None,
            "action_label": "Go to Calendar",
            "episode_started_at": None,
        }
    check = CHECKS[key]
    details = dict(check.sample_details)
    return {
        "key": key,
        "dedup_key": f"check:{key}",
        "kind": "check",
        "severity": check.severity,
        "message": check.message(details),
        "count": details.get("count"),
        "goto_tab": check.goto_tab,
        "goto_section": check.goto_section,
        "action_label": check.action_label,
        "episode_started_at": None,
    }


# ---------------------------------------------------------------------------
# Event registry bridge (per-event config lives in services/dispatch.py)
# ---------------------------------------------------------------------------

def _event_defs() -> Dict[str, EventDef]:
    """EventDefs for every condition check, derived from the CHECKS registry.

    Registered with dispatch at import time so Settings > Notifications lists
    one row per subscribable event - condition checks and rule events alike -
    and each check inherits the channel checkboxes from CHANNEL_DEFS. The
    label / interval / enable defaults stay in the check definitions above;
    this only mirrors them into the unified event registry.
    """
    return {
        key: EventDef(
            key=key,
            label=check.label,
            kind="check",
            default_channels=(UI_CHANNEL,),
            default_interval_minutes=check.default_interval_minutes,
        )
        for key, check in CHECKS.items()
    }


dispatch.register_event_def_provider(_event_defs)


def effective_setting(key: str) -> Dict[str, Any]:
    """``{enabled, interval_minutes}`` for one check, merged from overrides.

    Thin shim over ``dispatch.effective_subscription`` (the unified
    NOTIFY_SUBSCRIPTIONS store), kept because the evaluation loop reads a
    check's own interval through it.
    """
    sub = dispatch.effective_subscription(key)
    return {"enabled": sub["enabled"], "interval_minutes": sub["interval_minutes"]}


# ---------------------------------------------------------------------------
# State access (ui_notification_state table, one row per check)
# ---------------------------------------------------------------------------

async def _load_states() -> Dict[str, Dict[str, Any]]:
    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT check_key, last_checked_at, condition_true, "
            "episode_started_at, dismissed_at, details, notified_channels "
            "FROM ui_notification_state"
        )
        rows = await cursor.fetchall()
    return {r["check_key"]: dict(r) for r in rows}


async def _write_state(
    key: str,
    *,
    last_checked_at: str,
    condition_true: bool,
    episode_started_at: Optional[str],
    dismissed_at: Optional[str],
    details: Dict[str, Any],
    notified_channels: List[str],
) -> None:
    async with get_db_context() as db:
        await db.execute(
            """
            INSERT INTO ui_notification_state (
                check_key, last_checked_at, condition_true,
                episode_started_at, dismissed_at, details, notified_channels
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(check_key) DO UPDATE SET
                last_checked_at = excluded.last_checked_at,
                condition_true = excluded.condition_true,
                episode_started_at = excluded.episode_started_at,
                dismissed_at = excluded.dismissed_at,
                details = excluded.details,
                notified_channels = excluded.notified_channels
            """,
            (key, last_checked_at, int(condition_true),
             episode_started_at, dismissed_at, json.dumps(details),
             json.dumps(sorted(set(notified_channels)))),
        )
        await db.commit()


def _index_job_running() -> bool:
    """True while any background indexing job is in flight.

    Imported lazily: indexer imports nothing from this module, but keeping
    the import here also avoids pulling it at app-config time.
    """
    from homestew.services.indexer import get_index_status

    return bool(get_index_status().get("running"))


def _due(row: Optional[Dict[str, Any]], interval_minutes: int,
         now: datetime) -> bool:
    """Whether a check's own interval has elapsed (no row = never checked)."""
    if row is None:
        return True
    last = _parse_iso(row.get("last_checked_at"))
    if last is None:
        return True
    return (now - last).total_seconds() >= interval_minutes * 60


def _external_targets(key: str) -> List[str]:
    """Non-UI channels subscribed for one event right now.

    Availability is NOT filtered here: a subscribed-but-currently-unavailable
    channel (webhook URL not set yet, transient outage) stays in the episode's
    frozen target list so ``deliver()`` retries it on later ticks while the
    episode lasts - without ever re-notifying channels that already succeeded.
    """
    sub = dispatch.effective_subscription(key)
    if not sub["enabled"]:
        return []
    return [name for name in sub["channels"] if name != UI_CHANNEL]


def _parse_delivery(value: Optional[str]) -> Tuple[List[str], List[str]]:
    """Decode persisted delivery state into ``(targets, done)``.

    ``notified_channels`` holds a JSON object ``{"targets": [...], "done":
    [...]}``: the channel list frozen when the current episode started, and
    the subset already delivered successfully. Accepting a bare list (the
    column default ``'[]'``, or a value written by an earlier build) keeps old
    rows readable: they simply carry no pending targets.
    """
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return [], []
    if isinstance(parsed, list):
        return [], [c for c in parsed if isinstance(c, str)]
    if not isinstance(parsed, dict):
        return [], []

    def _names(raw) -> List[str]:
        return [c for c in raw if isinstance(c, str)] if isinstance(raw, list) else []

    return _names(parsed.get("targets")), _names(parsed.get("done"))


def _dump_delivery(targets: List[str], done: List[str]) -> Dict[str, Any]:
    """Encode delivery state for ``_write_state`` (which JSON-dumps it)."""
    return {"targets": sorted(set(targets)), "done": sorted(set(done))}


async def _deliver_check_episode(
    key: str, check: UINotificationCheck, details: Dict[str, Any],
    targets: List[str], done: List[str], last_checked_at: str,
    episode_started_at: str, dismissed_at: Optional[str],
) -> None:
    """Push one active health episode to its not-yet-delivered channels.

    Called right after the state row is written for a due evaluation. Only
    channels in the episode's frozen target list that have not succeeded yet
    are attempted; successes merge into ``done`` so each occurrence alerts at
    most once per channel, while failures retry on the next tick (the same
    partial-failure semantics as the calendar ledger).
    """
    pending = [name for name in targets if name not in done]
    if not pending:
        return
    payload = dispatch.build_check_payload(
        key=key,
        message=check.message(details),
        details=details,
        severity=check.severity,
    )
    results = await dispatch.deliver(payload, pending)
    newly_done = [name for name, ok in results.items() if ok]
    if not newly_done:
        return  # row keeps the old done list; every target retries next tick
    await _write_state(
        key,
        last_checked_at=last_checked_at,
        condition_true=True,
        episode_started_at=episode_started_at,
        dismissed_at=dismissed_at,
        details=details,
        notified_channels=_dump_delivery(targets, done + newly_done),
    )
    logger.info(
        "Health event %r delivered via %s (episode %s)",
        key, ",".join(newly_done), episode_started_at,
    )


# ---------------------------------------------------------------------------
# Evaluation (background loop) - the ONLY writer of check state
# ---------------------------------------------------------------------------

async def run_due_checks(
    now: Optional[datetime] = None, force_key: Optional[str] = None
) -> int:
    """Evaluate every due condition check and deliver new episodes.

    Driven by the notifier loop on each wake-up (so webhook-subscribed health
    alerts fire with no browser open), and by ``POST /ui/{key}/check`` with
    ``force_key`` for Settings "Check now". A failing evaluator is logged and
    leaves the row untouched - one broken check must never blank the feed.
    Returns the number of checks evaluated (useful for tests/logging).
    """
    now = now or _utcnow()
    states = await _load_states()
    evaluated = 0

    for key, check in CHECKS.items():
        eff = effective_setting(key)
        if not eff["enabled"]:
            continue
        row = states.get(key)
        forced = key == force_key
        if not forced and not _due(row, eff["interval_minutes"], now):
            continue
        # Never evaluate mid-index-job, and do NOT advance last_checked_at:
        # the check stays due so it re-evaluates right after the job ends.
        if _index_job_running():
            continue
        try:
            details = await check.evaluate()
        except Exception:
            logger.exception("UI notification check %r failed", key)
            continue
        evaluated += 1

        if details is not None:
            # Condition holds: start an episode, or keep the current one
            # (its dismissal, start time and delivery progress survive a
            # re-evaluation).
            was_true = bool(row and row.get("condition_true"))
            prev_targets, prev_done = _parse_delivery(
                row.get("notified_channels") if row else None
            )
            # A NEW episode freezes its channel targets from the subscription
            # as of now; a continuing episode keeps the frozen list, so
            # enabling a channel mid-episode waits for the next occurrence.
            targets = prev_targets if was_true else _external_targets(key)
            done = prev_done if was_true else []
            episode_started = (row.get("episode_started_at") if was_true
                               else _iso(now))
            dismissed = row.get("dismissed_at") if was_true else None
            await _write_state(
                key,
                last_checked_at=_iso(now),
                condition_true=True,
                episode_started_at=episode_started,
                dismissed_at=dismissed,
                details=details,
                notified_channels=_dump_delivery(targets, done),
            )
            # A dismissed episode stays silent on every channel, not just the
            # toast feed: dismissal means "this occurrence, nowhere".
            if dismissed is None and targets:
                await _deliver_check_episode(
                    key, check, details, targets, done, _iso(now),
                    episode_started, dismissed,
                )
        else:
            # Condition gone: close the episode so a future recurrence fires
            # (with a fresh target snapshot from then-current subscriptions).
            await _write_state(
                key,
                last_checked_at=_iso(now),
                condition_true=False,
                episode_started_at=None,
                dismissed_at=None,
                details={},
                notified_channels=_dump_delivery([], []),
            )
    return evaluated


# ---------------------------------------------------------------------------
# Calendar inbox items (rule event rendered into the toast feed)
# ---------------------------------------------------------------------------

async def _calendar_inbox_items(now: datetime) -> List[Dict[str, Any]]:
    """Due/overdue calendar events not yet delivered to the ``ui`` channel.

    Mirrors notifier.run_tick's filtering (same lead window, same kind rules)
    but reads instead of sends: an occurrence disappears from the feed when a
    ledger row with channel='ui' exists - written either by Dismiss or, later,
    by an actual UI delivery ack - and reappears on the next occurrence
    because the due date changes. Marking the event done takes it out of the
    candidate list entirely, so the card vanishes without any dismissal.

    Costs two queries per poll and only when the calendar event is enabled
    AND subscribed to the toast channel - a webhook-only or disabled install
    keeps the old zero-work poll (idle-CPU lesson #9).
    """
    sub = dispatch.effective_subscription(CALENDAR_EVENT_KEY)
    if not sub["enabled"] or UI_CHANNEL not in sub["channels"]:
        return []

    lead = dispatch.lead_timedelta()
    window_days = max(1, math.ceil(lead.total_seconds() / 86400))
    events = await calendar_service.list_notify_candidates(window_days)

    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT event_id, due_date, kind FROM notification_log "
            "WHERE channel = 'ui'"
        )
        rows = await cursor.fetchall()
    sent = {(r["event_id"], str(r["due_date"])[:10], r["kind"]) for r in rows}

    # due_datetime() is naive LOCAL time (date + wall-clock time of day), so
    # the lead-window comparison must use local now, exactly like notifier.
    local_now = datetime.now()
    items: List[Dict[str, Any]] = []
    for event in events:
        due_dt = dispatch.due_datetime(event)
        if due_dt is None:
            continue
        kind = "overdue" if event.get("status") == "overdue" else "due"
        if kind == "due" and (due_dt - local_now) > lead:
            continue  # still outside the lead window
        due_str = due_dt.date().isoformat()
        if (event["id"], due_str, kind) in sent:
            continue  # this occurrence was dismissed from the toast feed
        items.append({
            "key": CALENDAR_EVENT_KEY,
            "dedup_key": dispatch.calendar_dedup_key(event["id"], due_str, kind),
            "kind": "rule",
            "severity": "warn",
            "message": dispatch.calendar_message(event, kind),
            "count": None,
            "goto_tab": "calendar",
            "goto_section": None,
            "action_label": "Go to Calendar",
            "episode_started_at": due_dt.isoformat(timespec="seconds"),
        })
    return items


# ---------------------------------------------------------------------------
# Poll entry point (READ-ONLY) + dismissal
# ---------------------------------------------------------------------------

async def collect(now: Optional[datetime] = None) -> Dict[str, Any]:
    """The active in-app feed: persisted check state + calendar candidates.

    Called by ``GET /api/notifications/ui``. Deliberately read-only -
    evaluation lives in ``run_due_checks()`` (background loop / force-check)
    - so the per-minute poll never runs condition queries and cannot race
    with the loop. Items carry a ``dedup_key`` the frontend uses
    to reconcile cards and route Dismiss for both check episodes and calendar
    occurrences.
    """
    now = now or _utcnow()
    states = await _load_states()

    items: List[Dict[str, Any]] = []
    statuses: Dict[str, Dict[str, Any]] = {}
    for key, check in CHECKS.items():
        row = states.get(key)
        # Status label for the Settings events table: reported for EVERY
        # registered check (even disabled or never-run), so each row always
        # has something to show. condition_true stays None until first eval.
        statuses[key] = {
            "condition_true": (bool(row.get("condition_true")) if row else None),
            "dismissed": bool(row and row.get("dismissed_at")),
            "last_checked_at": (row.get("last_checked_at") if row else None),
        }
        # A check switched off (or with the Toast channel unchecked) mid-
        # episode must go silent immediately, not keep emitting from its last
        # stored state. External channels are unaffected - they already fired
        # during run_due_checks; this list is what the toast feed renders.
        sub = dispatch.effective_subscription(key)
        if not sub["enabled"] or UI_CHANNEL not in sub["channels"]:
            continue
        if not row or not row.get("condition_true") or row.get("dismissed_at"):
            continue
        try:
            details = json.loads(row.get("details") or "{}")
        except json.JSONDecodeError:
            details = {}
        items.append({
            "key": key,
            "dedup_key": f"check:{key}",
            "kind": "check",
            "severity": check.severity,
            "message": check.message(details),
            "count": details.get("count"),
            "goto_tab": check.goto_tab,
            "goto_section": check.goto_section,
            "action_label": check.action_label,
            "episode_started_at": row.get("episode_started_at"),
        })

    try:
        items.extend(await _calendar_inbox_items(now))
    except Exception:
        # A broken calendar read must never blank the health-check cards.
        logger.exception("Calendar inbox rendering failed")

    return {"items": items, "checked_at": _iso(now), "statuses": statuses}


def parse_calendar_dedup_key(key: str) -> Optional[Tuple[int, str, str]]:
    """Split ``calendar:{event_id}:{due_date}:{kind}`` into its parts.

    Returns None for anything malformed (including plain check keys), so the
    dismiss route can tell "unknown key" (404) from "nothing to dismiss".
    """
    if not key.startswith("calendar:"):
        return None
    parts = key.split(":")
    if len(parts) != 4:
        return None
    try:
        event_id = int(parts[1])
    except ValueError:
        return None
    due, kind = parts[2], parts[3]
    if not due or kind not in ("due", "overdue"):
        return None
    return event_id, due, kind


def normalize_dismiss_key(key: str) -> str:
    """Map a dedup-shaped key back to its check key (``check:x`` -> ``x``).

    The frontend reconciles cards by ``dedup_key`` and posts that value to
    Dismiss, so both ``unindexed_manuals`` and ``check:unindexed_manuals``
    must work; calendar dedup keys pass through untouched.
    """
    if key.startswith("check:") and key[6:] in CHECKS:
        return key[6:]
    return key


def is_dismissable_key(key: str) -> bool:
    """Whether a dismiss target is a known check key or calendar dedup key."""
    key = normalize_dismiss_key(key)
    return key in CHECKS or parse_calendar_dedup_key(key) is not None


async def dismiss(key: str, now: Optional[datetime] = None) -> bool:
    """Silence one occurrence - a check episode or a calendar card.

    Checks: only an active (true, undismissed) episode can be dismissed;
    dismissing one whose condition no longer holds is a no-op - the episode
    already closed and a future recurrence must notify.

    Calendar dedup keys: writes a ``'ui'`` ledger row for that exact
    occurrence (INSERT OR IGNORE), which is what keeps the card from
    resurrecting on the next poll while still letting the NEXT occurrence of
    a recurring event notify. Re-dismissing an already-ledgered occurrence
    reports success - the desired end state holds either way.
    """
    parsed = parse_calendar_dedup_key(key)
    if parsed is not None:
        event_id, due_str, kind = parsed
        async with get_db_context() as db:
            # The ledger has an FK onto calendar_events: a card whose event
            # was deleted in another tab can never be dismissed (and does not
            # need to be - it is already gone). Report ok:false instead of
            # tripping the constraint.
            cursor = await db.execute(
                "SELECT 1 FROM calendar_events WHERE id = ?", (event_id,)
            )
            if await cursor.fetchone() is None:
                return False
            await db.execute(
                "INSERT OR IGNORE INTO notification_log"
                " (event_id, due_date, kind, channel) VALUES (?, ?, ?, 'ui')",
                (event_id, due_str, kind),
            )
            await db.commit()
        logger.info("Calendar notification %r dismissed from toast feed", key)
        return True

    key = normalize_dismiss_key(key)
    if key not in CHECKS:
        return False
    now = now or _utcnow()
    states = await _load_states()
    row = states.get(key)
    if not row or not row.get("condition_true") or row.get("dismissed_at"):
        return False
    targets, done = _parse_delivery(row.get("notified_channels"))
    await _write_state(
        key,
        last_checked_at=row.get("last_checked_at"),
        condition_true=True,
        episode_started_at=row.get("episode_started_at"),
        dismissed_at=_iso(now),
        details=json.loads(row.get("details") or "{}"),
        notified_channels=_dump_delivery(targets, done),
    )
    logger.info("UI notification %r dismissed for episode %s", key,
                row.get("episode_started_at"))
    return True


async def test_event(
    key: str, channels: Optional[List[str]] = None
) -> Dict[str, bool]:
    """Fire a sample payload through the event's real subscribed channels.

    The Settings "Test" button: builds the same payload a live alert would
    carry (marked ``test: true``) and delivers it to every non-UI channel
    subscribed for the event - so testing actually exercises the webhook
    configuration instead of only previewing the toast locally. No ledger or
    episode state is written; per-channel results come back for the UI to
    report. Works whatever the subscription currently is (verifying a
    channel before relying on it is the point), but sends nowhere when no
    external channel is subscribed.

    ``channels`` overrides which channels to test - the frontend passes the
    checkboxes exactly as currently shown, so ticking "Webhook" and clicking
    Test fires the webhook even before Save persists it. Unknown ids are
    dropped; None (older clients / plain POST) falls back to the saved
    subscription unchanged.
    """
    definition = dispatch.all_event_defs().get(key)
    if definition is None:
        raise KeyError(key)
    sub = dispatch.effective_subscription(key)
    if channels is None:
        source_channels = sub["channels"]
    else:
        known = set(dispatch.channel_names())
        source_channels = [c for c in channels if c in known]
    targets = [name for name in source_channels if name != UI_CHANNEL]
    if not targets:
        return {}

    if key == CALENDAR_EVENT_KEY:
        payload = dispatch.build_calendar_test_payload()
    else:
        check = CHECKS[key]
        details = dict(check.sample_details)
        payload = dispatch.build_check_payload(
            key=key,
            message=check.message(details),
            details=details,
            severity=check.severity,
            test=True,
        )
    return await dispatch.deliver(payload, targets)
