"""Notification API endpoints: webhook test sends and the in-app toast feed."""
import logging

import requests
from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from homestew.config import settings
from homestew.models.schemas import (
    UINotificationActionResponse,
    UINotificationListResponse,
    WebhookTestRequest,
)
from homestew.services import ui_notifications
from homestew.services.notifier import build_test_payload
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
    """Active in-app notifications, evaluating any check whose interval elapsed.

    Cheap by design: a check only re-runs its condition query once its own
    configured interval has passed, so the per-minute poll costs almost
    nothing the rest of the time.
    """
    return UINotificationListResponse(**await ui_notifications.collect())


@router.post("/ui/{key}/dismiss", response_model=UINotificationActionResponse)
async def dismiss_ui_notification(key: str):
    """Dismiss the current occurrence of one notification.

    Server-side and permanent for that occurrence: the item never returns
    while the condition keeps holding, and comes back only if the problem is
    resolved and later recurs. Unknown key -> 404; nothing active to dismiss
    -> ok:false (a benign race with another tab closing the episode).
    """
    if key not in ui_notifications.CHECKS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown notification: {key}",
        )
    return UINotificationActionResponse(ok=await ui_notifications.dismiss(key))


@router.post("/ui/{key}/check", response_model=UINotificationListResponse)
async def check_ui_notification(key: str):
    """Force one immediate evaluation of a single check, ignoring its interval.

    Returns the full active list (same shape as GET /ui) so the caller can
    repaint without another request. Unknown key -> 404.
    """
    if key not in ui_notifications.CHECKS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown notification: {key}",
        )
    return UINotificationListResponse(
        **await ui_notifications.collect(force_key=key)
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
    payload = build_test_payload()

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
