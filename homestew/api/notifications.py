"""Notification API endpoints: webhook test sends and the in-app toast feed."""
import logging
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from homestew.config import settings
from homestew.models.schemas import (
    UINotificationActionResponse,
    UINotificationListResponse,
    UINotificationTestRequest,
    UINotificationTestResponse,
    WebhookTestRequest,
)
from homestew.services import dispatch, ui_notifications
from homestew.services.notify_channels import send_test_webhook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])


# ---------------------------------------------------------------------------
# In-app notifications (toast framework). The frontend polls /ui; Dismiss
# silences the current occurrence of a condition, /check forces one immediate
# re-evaluation (used by Settings > Advanced "Check now" and handy for tests).
# ---------------------------------------------------------------------------

@router.get("/ui", response_model=UINotificationListResponse)
async def get_ui_notifications():
    """Active in-app notifications: health episodes + calendar due/overdue.

    Read-only - evaluation happens in the background notifier loop
    (services/ui_notifications.run_due_checks) - so the per-minute poll
    never runs condition queries and costs two cheap SELECTs plus, for toast-
    subscribed calendar reminders, one candidate scan.
    """
    return UINotificationListResponse(**await ui_notifications.collect())


@router.post("/ui/{key}/dismiss", response_model=UINotificationActionResponse)
async def dismiss_ui_notification(key: str):
    """Dismiss one occurrence - a check episode or a calendar card.

    Accepts both plain check keys (``unindexed_manuals``) and calendar dedup
    keys (``calendar:{event_id}:{due}:{kind}``), i.e. whatever the item's
    ``dedup_key`` field carries. Server-side and permanent for that
    occurrence: a check stays silent while the condition holds (returning
    only on a new episode); a calendar card writes a 'ui' ledger row so it
    never resurrects, while the NEXT occurrence of a recurring event still
    notifies. Unknown key -> 404; nothing active to dismiss -> ok:false (a
    benign race with another tab closing the episode).
    """
    if not ui_notifications.is_dismissable_key(key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown notification: {key}",
        )
    return UINotificationActionResponse(ok=await ui_notifications.dismiss(key))


@router.post("/ui/{key}/check", response_model=UINotificationListResponse)
async def check_ui_notification(key: str):
    """Force one immediate evaluation of a single event, ignoring its interval.

    Condition checks run their evaluator (and deliver a new episode to
    subscribed external channels, like the background loop would); the
    calendar rule event runs one notifier scan (delivering due/overdue
    occurrences to its subscribed external channels - the ledger still
    dedupes occurrences already sent). Either way the response is the full
    active list (same shape as GET /ui, including per-check statuses) so the
    caller can repaint without another request. Unknown key -> 404.
    """
    if key in ui_notifications.CHECKS:
        await ui_notifications.run_due_checks(force_key=key)
    elif key == dispatch.CALENDAR_EVENT_KEY:
        from homestew.services import notifier

        await notifier.run_tick()
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown notification: {key}",
        )
    return UINotificationListResponse(**await ui_notifications.collect())


@router.post("/ui/{key}/test", response_model=UINotificationTestResponse)
async def test_ui_notification(
    key: str, body: Optional[UINotificationTestRequest] = None
):
    """Preview one event AND fire it through its real subscribed channels.

    Two things happen, mirroring what a live alert would do (decision: Test
    must exercise the actual delivery configuration):

    1. A sample item is returned for a local preview toast - built from the
       event's registered sample data without evaluating anything or touching
       state, so it works even when the condition does not currently hold.
    2. The same payload (marked ``test: true``) is delivered to every
       non-UI channel subscribed for the event; per-channel success comes
       back in ``deliveries`` so a broken webhook fails visibly instead of
       only previewing happily. No ledger or episode state is written.

    Works even with no channel subscribed yet (the preview always renders;
    verifying before subscribing is the point). Unknown key -> 404.

    An optional JSON body ``{"channels": [...]}`` overrides which channels to
    fire through - the Settings UI sends the checkboxes exactly as currently
    shown, so a freshly ticked (unsaved) Webhook still gets the test POST.
    Without a body the saved subscription is used, unchanged.
    """
    if key not in dispatch.all_event_defs():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown notification: {key}",
        )
    deliveries = await ui_notifications.test_event(
        key, channels=body.channels if body else None
    )
    return UINotificationTestResponse(
        item=ui_notifications.sample_item(key), deliveries=deliveries
    )


@router.post("/test")
async def test_webhook(test: WebhookTestRequest):
    """Send a sample payload to the webhook so the user can verify it works.

    Mirrors the settings model-probe pattern: URL/token from the request body
    win over saved values (so unsaved edits are testable), connection errors
    and non-2xx responses become a friendly 502. This never writes the dedupe
    ledger and does not require notifications to be enabled - testing before
    switching the feature on is expected.
    """
    wtype = (test.webhook_type
             or getattr(settings, "NOTIFY_WEBHOOK_TYPE", "none") or "none")
    if wtype == "none":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook Type is set to None - choose Generic or Synology Chat first",
        )

    url = (test.webhook_url or "").strip() or settings.NOTIFY_WEBHOOK_URL
    if not url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No webhook URL configured",
        )
    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook URL must start with http:// or https://",
        )

    token = (test.webhook_token or "").strip() or settings.NOTIFY_WEBHOOK_TOKEN
    payload = dispatch.build_calendar_test_payload()

    try:
        await run_in_threadpool(
            lambda: send_test_webhook(payload, url, token, test.verify_ssl, wtype)
        )
    except requests.RequestException as exc:
        logger.warning("Webhook test to %s failed: %s", url, exc)
        # Synology Chat answers HTTP 200 with {"success": false} on rejection;
        # send_test_webhook turns that into an exception whose message names
        # the reason (e.g. an invalid token). When the server responded at all,
        # pass its explanation through; pure connection errors stay generic.
        detail = "The webhook could not be reached or rejected the request"
        if getattr(exc, "response", None) is not None:
            detail = str(exc)[:300] or detail
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)

    return {"ok": True}
