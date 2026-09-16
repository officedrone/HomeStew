"""Notification API endpoints (webhook test sends)."""
import logging

import requests
from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from homestew.config import settings
from homestew.models.schemas import WebhookTestRequest
from homestew.services.notifier import build_test_payload
from homestew.services.notify_channels import send_test_webhook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.post("/test")
async def test_webhook(test: WebhookTestRequest):
    """Send a sample payload to the webhook so the user can verify it works.

    Mirrors the settings model-probe pattern: URL/token from the request body
    win over saved values (so unsaved edits are testable), connection errors
    and non-2xx responses become a friendly 502. This never writes the dedupe
    ledger and does not require notifications to be enabled — testing before
    switching the feature on is expected.
    """
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

    wtype = test.webhook_type or getattr(settings, "NOTIFY_WEBHOOK_TYPE", "generic")

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
