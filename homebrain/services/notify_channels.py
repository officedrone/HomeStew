"""Pluggable notification channels.

A channel knows how to deliver one notification payload somewhere. The
notifier loop (services/notifier.py) iterates the CHANNELS registry and sends
through every enabled channel, so adding a new delivery method (ntfy, email,
Home Assistant service call, ...) means subclassing NotificationChannel and
appending an instance to CHANNELS — nothing else changes.

``send()`` is intentionally synchronous/blocking: callers run it through
FastAPI's ``run_in_threadpool`` so the event loop stays responsive, mirroring
how the settings API probes the LLM server with ``requests``.

Channels read their configuration from the live ``settings`` singleton at call
time (not import time), so edits made in Settings > Notifications take effect
on the very next send without restarting the container.
"""
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
    """Generic JSON webhook: POSTs the payload as application/json.

    Works with any receiver that accepts a JSON body — custom scripts,
    Node-RED, Home Assistant webhook triggers, etc. An optional bearer token
    is sent as an ``Authorization`` header for receivers that need auth.
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
        resp = requests.post(
            url,
            json=payload,
            headers=self._build_headers(settings.NOTIFY_WEBHOOK_TOKEN),
            timeout=WEBHOOK_TIMEOUT,
            verify=_verify_ssl(),
        )
        resp.raise_for_status()


def send_test_webhook(
    payload: Dict[str, Any],
    url: Optional[str] = None,
    token: Optional[str] = None,
    verify_ssl: Optional[bool] = None,
) -> None:
    """One-off webhook POST for the Settings "Send Test" button.

    Unlike ``WebhookChannel.send`` this takes an explicit URL/token so a
    freshly typed (not yet saved) configuration can be tested; blank/None
    falls back to the currently saved settings. Raises on connection errors
    or non-2xx responses so the API can surface a friendly 502.
    """
    target = (url if url is not None else settings.NOTIFY_WEBHOOK_URL or "").strip()
    # The Settings UI passes the checkbox state so an unsaved change can be
    # tested before saving; blank/None falls back to the saved setting.
    verify = _verify_ssl() if verify_ssl is None else bool(verify_ssl)
    resp = requests.post(
        target,
        json=payload,
        headers=WebhookChannel._build_headers(
            token if token is not None else settings.NOTIFY_WEBHOOK_TOKEN
        ),
        timeout=WEBHOOK_TIMEOUT,
        verify=verify,
    )
    resp.raise_for_status()


#: Registry of all delivery channels the notifier will use. Extension point:
#: add new channel instances here and they are picked up automatically.
CHANNELS: List[NotificationChannel] = [WebhookChannel()]
