"""Unified event -> channel notification dispatch.

One notification *event* (e.g. ``unindexed_manuals``, ``calendar_reminders``)
can be *subscribed* to any number of delivery *channels* (in-app toast,
webhook, and whatever is added later). The subscriptions live in the
``NOTIFY_SUBSCRIPTIONS`` setting as a per-event map::

    {"unindexed_manuals": {"interval_minutes": 1440,
                           "channels": ["ui", "webhook"]}}

and are edited from Settings > Notifications via checkboxes - one row per
event, one checkbox per channel. An event is *enabled* exactly when its
channel list is non-empty: ticking a "Notify via" checkbox IS the enable-
ment (there is no separate per-event or global switch any more). This module owns:

* the CHANNEL registry (``CHANNEL_DEFS`` for display + instance lookup into
  ``notify_channels.CHANNELS``; ``ui`` is a pseudo-channel meaning "the
  frontend poll renders it", so delivering to it is a no-op here);
* the EVENT registry (``EVENT_DEFS``): rule-based events are declared here,
  condition-check events register themselves from services/ui_notifications.py
  at import time (dispatch never imports that module - no circular import);
* subscription config merge/validation (registry defaults + user overrides,
  persisted through ``save_settings_overrides``);
* ``deliver()`` - fan one payload out to a list of channel names and report
  per-channel success. Callers own their ledger semantics: the notifier
  records notification_log rows per channel, health checks track delivered
  channels per episode in ui_notification_state.

Adding a new CHANNEL = subclass NotificationChannel in notify_channels.py,
append it to CHANNELS, add one ChannelDef here. Adding a new EVENT = one
EventDef (rule) or one check entry + register_event_def call (condition).
The Settings table and the frontend build themselves from these registries.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi.concurrency import run_in_threadpool

from homestew.config import (
    NOTIFY_LEAD_HOURS_MAX,
    NOTIFY_LEAD_HOURS_MIN,
    UI_NOTIFICATION_INTERVAL_MAX,
    UI_NOTIFICATION_INTERVAL_MIN,
    normalize_notify_subscriptions,
    save_settings_overrides,
    settings,
)
from homestew.services import notify_channels

logger = logging.getLogger(__name__)

#: Pseudo-channel name for the in-app toast feed. "Delivering" to it means
#: nothing here - the frontend picks the event up on its next poll of
#: GET /api/notifications/ui (or the notifier records a ledger row so a
#: Dismissed calendar card never resurrects).
UI_CHANNEL = "ui"

#: The one rule-based (non-check) event shipped today: due/overdue calendar
#: reminders. Covers both the "due" and "overdue" kinds - the subscription
#: chooses channels, the ledger keeps the kind for per-occurrence dedupe.
CALENDAR_EVENT_KEY = "calendar_reminders"


@dataclass(frozen=True)
class ChannelDef:
    """Display metadata for one delivery channel (Settings checkboxes)."""
    name: str
    label: str


#: Registry of selectable channels, in display order. The ``webhook`` entry
#: mirrors notify_channels.WebhookChannel.name; new channels appended here
#: (plus an instance in notify_channels.CHANNELS) appear as a checkbox on
#: every event row without any other change.
CHANNEL_DEFS: Tuple[ChannelDef, ...] = (
    ChannelDef(name=UI_CHANNEL, label="Toast"),
    ChannelDef(name="webhook", label="Webhook"),
)


@dataclass(frozen=True)
class EventDef:
    """Definition of one subscribable notification event.

    ``kind`` is ``"check"`` for condition-evaluation events (owned by
    services/ui_notifications.py) or ``"rule"`` for rule-driven events like
    calendar reminders. Both kinds carry their own scan interval now - the
    Settings table edits it per row - and rule events additionally carry the
    "notify before due" lead in hours (checks have no due date, so theirs is
    None and the UI shows a dash).
    """
    key: str
    label: str
    kind: str  # "check" | "rule"
    default_channels: Tuple[str, ...] = (UI_CHANNEL,)
    default_interval_minutes: Optional[int] = None
    default_notify_before_due_hours: Optional[int] = None


#: Rule-based events declared here. Condition-check events live in
#: services/ui_notifications.py and contribute their EventDefs through a
#: provider callback (registered at import time), so dispatch never imports
#: that module - no circular import - and the merged view always agrees with
#: the live CHECKS registry, even when tests monkeypatch it.
EVENT_DEFS: Dict[str, EventDef] = {
    CALENDAR_EVENT_KEY: EventDef(
        key=CALENDAR_EVENT_KEY,
        label="Calendar due & overdue",
        kind="rule",
        # Toast on by default (the in-app feed is free); webhook stays off so
        # enabling calendar notifications never surprises an external receiver.
        default_channels=(UI_CHANNEL,),
        # The notifier loop's calendar cadence + lead window, both editable
        # per row in Settings > Notifications (moved out of the old global
        # NOTIFY_CHECK_INTERVAL_MINUTES / NOTIFY_LEAD_* settings).
        default_interval_minutes=15,
        default_notify_before_due_hours=24,
    ),
}

_EVENT_DEF_PROVIDERS: List[Any] = []


def register_event_def_provider(provider) -> None:
    """Register a callable returning extra ``{key: EventDef}`` entries.

    Called once by services/ui_notifications.py at import time. Idempotent:
    re-registering the same callable (module reload in tests) is a no-op.
    """
    if provider not in _EVENT_DEF_PROVIDERS:
        _EVENT_DEF_PROVIDERS.append(provider)


def all_event_defs() -> Dict[str, EventDef]:
    """The full subscribable-event registry (rule events + providers)."""
    out = dict(EVENT_DEFS)
    for provider in _EVENT_DEF_PROVIDERS:
        out.update(provider())
    return out


def lead_timedelta() -> timedelta:
    """The calendar event's configured lead window, in hours.

    Read from the ``calendar_reminders`` subscription's
    ``notify_before_due_hours`` (Settings > Notifications table), so the
    notifier and ui_notifications apply the same window without either
    module importing the other. 0 means "alert only once actually due"; a
    corrupt/missing value falls back to the registry default.
    """
    hours = effective_subscription(CALENDAR_EVENT_KEY)[
        "notify_before_due_hours"
    ]
    if hours is None:
        hours = EVENT_DEFS[CALENDAR_EVENT_KEY].default_notify_before_due_hours
    return timedelta(hours=max(NOTIFY_LEAD_HOURS_MIN, int(hours)))


def due_datetime(event: Dict[str, Any]) -> Optional[datetime]:
    """Combine an event's next due date with its optional time of day."""
    due = event.get("next_due_date")
    if due is None:
        return None
    return datetime.combine(due, event.get("start_time") or time.min)


def calendar_dedup_key(event_id: Any, due_str: str, kind: str) -> str:
    """Stable id for one occurrence of one calendar event.

    Doubles as the in-app card's reconcile/dismiss key (frontend treats it
    opaquely) and matches the notification_log UNIQUE key components.
    """
    return f"calendar:{event_id}:{due_str}:{kind}"


def calendar_message(event: Dict[str, Any], kind: str) -> str:
    """One-line summary used as the toast text / generic webhook message."""
    title = event.get("title") or "Calendar event"
    due = event.get("next_due_date")
    when = f" on {due}" if due else ""
    if kind == "overdue":
        return f"Overdue: {title}{when}"
    return f"Due soon: {title}{when}"


def build_calendar_payload(
    event: Dict[str, Any], kind: str, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Generic JSON notification body for one calendar event occurrence.

    ``kind`` is "due" (coming up within the lead window) or "overdue". The
    shape is deliberately plain so any webhook receiver can pick the fields
    it cares about without a per-service formatter; the unified envelope
    (severity/message/dedup_key) rides along for generic receivers.
    """
    now = now or datetime.now()
    due_dt = due_datetime(event)
    hours_until_due: Optional[float] = None
    if due_dt is not None:
        hours_until_due = round((due_dt - now).total_seconds() / 3600.0, 2)

    return enrich_payload({
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
        "severity": "warn",
        "message": calendar_message(event, kind),
        "dedup_key": calendar_dedup_key(
            event.get("id"), due_dt.date().isoformat() if due_dt else "", kind
        ),
        "sent_at": now.isoformat(timespec="seconds"),
    })


def build_calendar_test_payload() -> Dict[str, Any]:
    """Sample calendar payload for the Settings "Send Test" buttons."""
    now = datetime.now()
    sample = {
        "id": 0,
        "title": "Test notification",
        "description": "This is a test of the HomeStew webhook channel.",
        "device_name": None,
        "next_due_date": (now + timedelta(days=1)).date(),
        "start_time": time(9, 0),
        "recurrence_label": "One-time",
        "status": "upcoming",
    }
    payload = build_calendar_payload(sample, kind="due", now=now)
    payload["test"] = True
    return payload


def build_check_payload(
    key: str, message: str, details: Dict[str, Any], severity: str,
    test: bool = False,
) -> Dict[str, Any]:
    """Generic JSON body for one health-check event episode.

    ``type`` mirrors the event key so a receiver can route on it; ``details``
    carries the evaluator's payload (e.g. {"count": 3}). The dedup_key is
    episode-scoped: receivers may treat repeats of the same key as one alert.
    """
    return enrich_payload({
        "source": settings.APP_NAME,
        "type": key,
        "event_key": key,
        "severity": severity,
        "message": message,
        "details": details,
        "dedup_key": f"check:{key}",
        "test": bool(test),
    })


def channel_names() -> List[str]:
    return [c.name for c in CHANNEL_DEFS]


def _channel_instance(name: str):
    """The live NotificationChannel object for a name, or None.

    Looked up on notify_channels.CHANNELS at call time (not a snapshot), so
    tests can monkeypatch the registry and future channels are picked up.
    """
    for channel in notify_channels.CHANNELS:
        if channel.name == name:
            return channel
    return None


def channel_available(name: str) -> bool:
    """Whether a channel could deliver right now (configured + switched on).

    The UI pseudo-channel is always available; real channels defer to their
    own ``enabled()`` (webhook needs type != none, enabled flag, URL...).
    """
    if name == UI_CHANNEL:
        return True
    instance = _channel_instance(name)
    return bool(instance and instance.enabled())


# ---------------------------------------------------------------------------
# Subscription configuration (registry defaults + NOTIFY_SUBSCRIPTIONS)
# ---------------------------------------------------------------------------

def effective_subscription(key: str) -> Dict[str, Any]:
    """``{enabled, interval_minutes, notify_before_due_hours, channels}``.

    Unknown events raise KeyError - callers iterate EVENT_DEFS or validate
    first. The stored channel list is taken as-is (an explicitly emptied list
    means "deliver nowhere", distinct from "unset" = registry default); the
    API boundary rejects unknown channel names before they get persisted.
    ``enabled`` is DERIVED: an event fires exactly when it has at least one
    subscribed channel, so there is no separate enable flag to keep in sync.
    """
    definition = all_event_defs()[key]
    overrides = normalize_notify_subscriptions(
        settings.NOTIFY_SUBSCRIPTIONS
    ).get(key, {})
    channels = overrides.get("channels")
    if channels is None:
        channels = list(definition.default_channels)
    interval = overrides.get("interval_minutes", definition.default_interval_minutes)
    if interval is not None:
        interval = min(max(int(interval), UI_NOTIFICATION_INTERVAL_MIN),
                       UI_NOTIFICATION_INTERVAL_MAX)
    lead = overrides.get(
        "notify_before_due_hours",
        definition.default_notify_before_due_hours,
    )
    if lead is not None:
        lead = min(max(int(lead), NOTIFY_LEAD_HOURS_MIN), NOTIFY_LEAD_HOURS_MAX)
    return {"enabled": bool(channels), "interval_minutes": interval,
            "notify_before_due_hours": lead, "channels": list(channels)}


def subscriptions_payload() -> Dict[str, Dict[str, Any]]:
    """Every registered event with effective settings + labels.

    Feeds GET /api/settings (``notify_subscriptions``) so the Settings table
    renders one row per event with one channel checkbox each, without the
    frontend knowing any defaults. ``interval_minutes`` is editable for both
    kinds now; ``notify_before_due_hours`` is None for condition checks (no
    due date to lead), which the UI renders as a dash.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for key, definition in all_event_defs().items():
        eff = effective_subscription(key)
        out[key] = {
            "enabled": eff["enabled"],
            "interval_minutes": eff["interval_minutes"],
            "notify_before_due_hours": eff["notify_before_due_hours"],
            "channels": eff["channels"],
            "label": definition.label,
            "kind": definition.kind,
        }
    return out


def update_subscriptions(updates: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Merge per-event updates into settings.NOTIFY_SUBSCRIPTIONS and persist.

    Raises ValueError on an unknown event key or channel name - the route
    turns that into a 422 so a stale frontend can never invent events or
    channels. Returns the new payload.
    """
    known_channels = set(channel_names())
    known_events = all_event_defs()
    merged = normalize_notify_subscriptions(settings.NOTIFY_SUBSCRIPTIONS)
    for key, entry in updates.items():
        if key not in known_events:
            raise ValueError(f"Unknown notification event: {key}")
        current = dict(merged.get(key, {}))
        # No "enabled" field any more: enablement is the channel list, which
        # the block below replaces wholesale (an emptied list = disabled).
        if entry.get("interval_minutes") is not None:
            current["interval_minutes"] = int(entry["interval_minutes"])
        if entry.get("notify_before_due_hours") is not None:
            current["notify_before_due_hours"] = int(
                entry["notify_before_due_hours"]
            )
        if entry.get("channels") is not None:
            names = list(entry["channels"])
            unknown = [n for n in names if n not in known_channels]
            if unknown:
                raise ValueError(f"Unknown notification channel(s): {unknown}")
            current["channels"] = names
        merged[key] = current
    save_settings_overrides({"NOTIFY_SUBSCRIPTIONS": merged})
    settings.NOTIFY_SUBSCRIPTIONS = merged
    return subscriptions_payload()


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def enrich_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Add the unified envelope fields shared by every event's payload.

    Existing per-event fields are untouched (webhook receivers built against
    the calendar payload keep working); ``severity``/``message`` give generic
    receivers a renderable summary without per-type formatting, and
    ``dedup_key`` identifies the exact occurrence for receivers that track
    state themselves.
    """
    enriched = dict(payload)
    enriched.setdefault("source", settings.APP_NAME)
    enriched.setdefault("severity", "warn")
    enriched.setdefault(
        "sent_at", datetime.now().isoformat(timespec="seconds")
    )
    return enriched


async def deliver(
    payload: Dict[str, Any], channel_names_: List[str]
) -> Dict[str, bool]:
    """Send one payload through every named channel; report per-channel result.

    The UI pseudo-channel always "succeeds" (visibility is derived from state
    at poll time). Unavailable channels (disabled/unconfigured) report False
    without an attempt - the caller decides whether that means retry. Real
    channels run their blocking send() in a threadpool, mirroring how every
    other outbound HTTP call in the app avoids stalling the event loop. A
    failing channel never aborts the fan-out: each result is independent so
    callers can ledger successful channels and retry only failed ones.
    """
    results: Dict[str, bool] = {}
    for name in channel_names_:
        if name == UI_CHANNEL:
            results[name] = True
            continue
        instance = _channel_instance(name)
        if instance is None or not instance.enabled():
            results[name] = False
            continue
        try:
            await run_in_threadpool(instance.send, payload)
            results[name] = True
        except Exception as exc:
            logger.warning(
                "Notification channel %s failed (%s): %s",
                name, payload.get("type"), exc,
            )
            results[name] = False
    return results
