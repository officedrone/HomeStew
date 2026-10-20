"""In-app notification framework: condition checks piped to frontend toasts.

The frontend polls ``GET /api/notifications/ui`` (roughly once a minute); this
module decides which checks are *due* and evaluates them, applying an episode
state machine so each continuous stretch where a condition holds counts as one
occurrence:

* condition flips false -> true   : new episode (``episode_started_at`` set,
  any previous dismissal cleared) - the toast appears;
* condition stays true            : the episode persists - a Dismissed check
  stays silent, an undismissed one keeps showing;
* condition flips true -> false   : the episode closes silently, so a later
  recurrence notifies again.

Dismissal is therefore "never show *this occurrence* again": it survives page
reloads and container restarts (state lives in ``ui_notification_state``) but
never suppresses a genuinely new problem.

Checks are evaluated lazily on the poll - there is deliberately no background
task here (idle-CPU lesson from issue #9: nothing runs while no browser is
open). Each check has its own interval (Settings > Advanced, minutes); a check
only re-evaluates once ``interval_minutes`` elapsed since its own
``last_checked_at``. While a search-index job is running, evaluation is
skipped *without advancing* ``last_checked_at``: a rebuild drops the FTS table,
so every manual would momentarily look un-indexed.

Adding a new notification event = one evaluator function + one entry in
``CHECKS`` below. The frontend renders whatever the endpoint returns, and
Settings > Advanced builds its rows from the registry too, so no other file
needs to change.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from homestew.config import (
    UI_NOTIFICATION_INTERVAL_MAX,
    UI_NOTIFICATION_INTERVAL_MIN,
    normalize_ui_notifications,
    save_settings_overrides,
    settings,
)
from homestew.db import get_db_context

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
    default_enabled: bool = True


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
        ),
    )
}


# ---------------------------------------------------------------------------
# Per-check effective configuration (registry defaults + user overrides)
# ---------------------------------------------------------------------------

def effective_setting(key: str) -> Dict[str, Any]:
    """``{enabled, interval_minutes}`` for one check, merged from overrides."""
    check = CHECKS[key]
    overrides = normalize_ui_notifications(settings.UI_NOTIFICATIONS).get(key, {})
    interval = overrides.get("interval_minutes", check.default_interval_minutes)
    interval = min(max(int(interval), UI_NOTIFICATION_INTERVAL_MIN),
                   UI_NOTIFICATION_INTERVAL_MAX)
    enabled = overrides.get("enabled", check.default_enabled)
    return {"enabled": bool(enabled), "interval_minutes": interval}


def settings_payload() -> Dict[str, Dict[str, Any]]:
    """All registered checks with their effective settings + labels.

    Feeds GET /api/settings so Settings > Advanced can render one row per
    check without the frontend knowing any defaults.
    """
    return {
        key: {
            "enabled": eff["enabled"],
            "interval_minutes": eff["interval_minutes"],
            "label": check.label,
        }
        for key, check in CHECKS.items()
        for eff in (effective_setting(key),)
    }


def update_settings(updates: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Merge per-check updates into settings.UI_NOTIFICATIONS and persist.

    Raises ValueError on an unknown check key - the route turns that into a
    422 so a stale frontend can never invent checks. Returns the new payload.
    """
    merged = normalize_ui_notifications(settings.UI_NOTIFICATIONS)
    for key, entry in updates.items():
        if key not in CHECKS:
            raise ValueError(f"Unknown notification check: {key}")
        current = dict(merged.get(key, {}))
        if entry.get("enabled") is not None:
            current["enabled"] = bool(entry["enabled"])
        if entry.get("interval_minutes") is not None:
            current["interval_minutes"] = int(entry["interval_minutes"])
        merged[key] = current
    save_settings_overrides({"UI_NOTIFICATIONS": merged})
    settings.UI_NOTIFICATIONS = merged
    return settings_payload()


# ---------------------------------------------------------------------------
# State access (ui_notification_state table, one row per check)
# ---------------------------------------------------------------------------

async def _load_states() -> Dict[str, Dict[str, Any]]:
    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT check_key, last_checked_at, condition_true, "
            "episode_started_at, dismissed_at, details FROM ui_notification_state"
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
) -> None:
    async with get_db_context() as db:
        await db.execute(
            """
            INSERT INTO ui_notification_state (
                check_key, last_checked_at, condition_true,
                episode_started_at, dismissed_at, details
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(check_key) DO UPDATE SET
                last_checked_at = excluded.last_checked_at,
                condition_true = excluded.condition_true,
                episode_started_at = excluded.episode_started_at,
                dismissed_at = excluded.dismissed_at,
                details = excluded.details
            """,
            (key, last_checked_at, int(condition_true),
             episode_started_at, dismissed_at, json.dumps(details)),
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


# ---------------------------------------------------------------------------
# Poll entry point + dismissal
# ---------------------------------------------------------------------------

async def collect(
    now: Optional[datetime] = None, force_key: Optional[str] = None
) -> Dict[str, Any]:
    """Evaluate due checks and return the active notifications.

    Called by ``GET /api/notifications/ui`` (and by the force-check endpoint
    with ``force_key``). A failing evaluator is logged and leaves the row
    untouched - one broken check must never blank the whole response.
    """
    now = now or _utcnow()
    states = await _load_states()

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

        if details is not None:
            # Condition holds: start an episode, or keep the current one
            # (its dismissal and start time survive a re-evaluation).
            was_true = bool(row and row.get("condition_true"))
            await _write_state(
                key,
                last_checked_at=_iso(now),
                condition_true=True,
                episode_started_at=(row.get("episode_started_at") if was_true
                                    else _iso(now)),
                dismissed_at=(row.get("dismissed_at") if was_true else None),
                details=details,
            )
        else:
            # Condition gone: close the episode so a future recurrence fires.
            await _write_state(
                key,
                last_checked_at=_iso(now),
                condition_true=False,
                episode_started_at=None,
                dismissed_at=None,
                details={},
            )

    # Re-read after the writes so emitted items reflect this poll exactly.
    states = await _load_states()
    items = []
    for key, check in CHECKS.items():
        # A check switched off mid-episode must go silent immediately, not
        # keep emitting from its last stored state.
        if not effective_setting(key)["enabled"]:
            continue
        row = states.get(key)
        if not row or not row.get("condition_true") or row.get("dismissed_at"):
            continue
        try:
            details = json.loads(row.get("details") or "{}")
        except json.JSONDecodeError:
            details = {}
        items.append({
            "key": key,
            "severity": check.severity,
            "message": check.message(details),
            "count": details.get("count"),
            "goto_tab": check.goto_tab,
            "goto_section": check.goto_section,
            "action_label": check.action_label,
            "episode_started_at": row.get("episode_started_at"),
        })
    return {"items": items, "checked_at": _iso(now)}


async def dismiss(key: str, now: Optional[datetime] = None) -> bool:
    """Silence the current episode of one check. Returns False if unknown/idle.

    Only an active (true, undismissed) episode can be dismissed; dismissing a
    check whose condition no longer holds is a no-op - the episode already
    closed and a future recurrence must notify.
    """
    if key not in CHECKS:
        return False
    now = now or _utcnow()
    states = await _load_states()
    row = states.get(key)
    if not row or not row.get("condition_true") or row.get("dismissed_at"):
        return False
    await _write_state(
        key,
        last_checked_at=row.get("last_checked_at"),
        condition_true=True,
        episode_started_at=row.get("episode_started_at"),
        dismissed_at=_iso(now),
        details=json.loads(row.get("details") or "{}"),
    )
    logger.info("UI notification %r dismissed for episode %s", key,
                row.get("episode_started_at"))
    return True
