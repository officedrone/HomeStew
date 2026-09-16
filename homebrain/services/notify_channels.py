"""Pluggable notification channels.

A channel knows how to deliver one notification payload somewhere. The
notifier loop (services/notifier.py) iterates the CHANNELS registry and sends
through every enabled channel, so adding a new delivery method (ntfy, email,
Home Assistant service call, ...) means subclassing NotificationChannel and
appending an instance to CHANNELS — nothing else changes.

The webhook channel supports two request shapes, selected by
NOTIFY_WEBHOOK_TYPE: ``generic`` POSTs the payload as application/json, while
``synology`` renders it into Synology Chat's incoming-webhook format — a form-
encoded ``payload={"text": ...}`` body whose auth token lives in the URL.

``send()`` is intentionally synchronous/blocking: callers run it through
FastAPI's ``run_in_threadpool`` so the event loop stays responsive, mirroring
how the settings API probes the LLM server with ``requests``.

Channels read their configuration from the live ``settings`` singleton at call
time (not import time), so edits made in Settings > Notifications take effect
on the very next send without restarting the container.
"""
import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import requests

from homebrain.config import settings

logger = logging.getLogger(__name__)

# How long to wait for a webhook endpoint to accept the payload.
WEBHOOK_TIMEOUT = 10


def _verify_ssl() -> bool:
    """Whether webhook TLS certificates must be verified (read live, per send).

    Off means ``requests`` accepts self-signed / internal-CA certs; the caller
    is opted in from Settings > Notifications > Validate SSL.
    """
    return bool(settings.NOTIFY_WEBHOOK_VERIFY_SSL)


#: Supported webhook request shapes (kept in sync with the Settings dropdown).
WEBHOOK_TYPES = ("generic", "synology")


def _format_synology_text(payload: Dict[str, Any]) -> str:
    """Render a generic notification payload as Synology Chat message text."""
    kind = "overdue" if payload.get("type") == "calendar_overdue" else "due soon"
    source = payload.get("source") or settings.APP_NAME
    lines = [f"**{source}** — {payload.get('title') or 'Notification'} ({kind})"]
    if payload.get("device_name"):
        lines.append(f"Device: {payload['device_name']}")
    due = payload.get("due_date")
    if due:
        when = f"{due} {payload.get('due_time') or ''}".strip()
        hours = payload.get("hours_until_due")
        if hours is not None and hours >= 0:
            when += f" (in {hours:g} h)"
        lines.append(f"Due: {when}")
    if payload.get("description"):
        lines.append(str(payload["description"]))
    return "\n".join(lines)


def _post_webhook(
    url: str,
    payload: Dict[str, Any],
    token: Optional[str],
    verify: bool,
    webhook_type: str,
) -> None:
    """POST one notification in the shape selected by ``webhook_type``.

    Raises requests exceptions on connection errors or rejected deliveries so
    callers (notifier retries, API 502s) can react uniformly.
    """
    if webhook_type == "synology":
        # Synology Chat incoming webhooks want application/x-www-form-urlencoded
        # with a JSON string in the `payload` field; auth is the token= query
        # param already baked into the URL, so no Authorization header here.
        body = {"payload": json.dumps({"text": _format_synology_text(payload)})}
        resp = requests.post(url, data=body, timeout=WEBHOOK_TIMEOUT, verify=verify)
        resp.raise_for_status()
        # DSM answers HTTP 200 even when it rejects the call (bad token etc.),
        # reporting the failure in a {"success": false} body — surface that as
        # an error instead of silently "succeeding".
        try:
            data = resp.json()
        except ValueError:
            return  # non-JSON body: nothing better to check than the status
        if isinstance(data, dict) and data.get("success") is False:
            err = data.get("error") or {}
            raise requests.HTTPError(
                "Synology Chat rejected the webhook call"
                f" (code {err.get('code')}: {err.get('errors') or 'unknown error'})",
                response=resp,
            )
    else:
        resp = requests.post(
            url,
            json=payload,
            headers=WebhookChannel._build_headers(token),
            timeout=WEBHOOK_TIMEOUT,
            verify=verify,
        )
        resp.raise_for_status()


class NotificationChannel(ABC):
    """Base class for notification delivery methods."""

    #: Stable identifier, used in logs (and future per-channel ledgers).
    name: str = ""

    @abstractmethod
    def enabled(self) -> bool:
        """Whether this channel is configured and switched on right now."""

    @abstractmethod
    def send(self, payload: Dict[str, Any]) -> None:
        """Deliver one notification. Raise on failure (caller retries)."""


class WebhookChannel(NotificationChannel):
    """Webhook delivery in either the generic JSON or Synology Chat shape.

    Generic works with any receiver that accepts a JSON body — custom scripts,
    Node-RED, Home Assistant webhook triggers, etc. An optional bearer token
    is sent as an ``Authorization`` header for receivers that need auth.
    Synology Chat mode renders the payload into Chat's form-encoded
    ``payload={"text": ...}`` format (token comes from the webhook URL).
    """

    name = "webhook"

    def enabled(self) -> bool:
        return bool(
            settings.NOTIFY_WEBHOOK_ENABLED
            and (settings.NOTIFY_WEBHOOK_URL or "").strip()
        )

    @staticmethod
    def _build_headers(token: Optional[str]) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        bearer = (token or "").strip()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return headers

    def send(self, payload: Dict[str, Any]) -> None:
        url = (settings.NOTIFY_WEBHOOK_URL or "").strip()
        _post_webhook(
            url,
            payload,
            settings.NOTIFY_WEBHOOK_TOKEN,
            _verify_ssl(),
            getattr(settings, "NOTIFY_WEBHOOK_TYPE", "generic"),
        )


def send_test_webhook(
    payload: Dict[str, Any],
    url: Optional[str] = None,
    token: Optional[str] = None,
    verify_ssl: Optional[bool] = None,
    webhook_type: Optional[str] = None,
) -> None:
    """One-off webhook POST for the Settings "Send Test" button.

    Unlike ``WebhookChannel.send`` this takes an explicit URL/token/type so a
    freshly typed (not yet saved) configuration can be tested; blank/None
    falls back to the currently saved settings. Raises on connection errors
    or non-2xx responses so the API can surface a friendly 502.
    """
    target = (url if url is not None else settings.NOTIFY_WEBHOOK_URL or "").strip()
    # The Settings UI passes the checkbox/dropdown state so unsaved changes can
    # be tested before saving; blank/None falls back to the saved settings.
    verify = _verify_ssl() if verify_ssl is None else bool(verify_ssl)
    wtype = webhook_type or getattr(settings, "NOTIFY_WEBHOOK_TYPE", "generic")
    _post_webhook(
        target,
        payload,
        token if token is not None else settings.NOTIFY_WEBHOOK_TOKEN,
        verify,
        wtype,
    )


#: Registry of all delivery channels the notifier will use. Extension point:
#: add new channel instances here and they are picked up automatically.
CHANNELS: List[NotificationChannel] = [WebhookChannel()]
