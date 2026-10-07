"""Tests for the "none" webhook type (Settings > Notifications).

"None" is the default Webhook Type: no webhook delivery happens even while a
URL/token from an earlier generic/synology configuration is still stored. The
type flows through three layers, each pinned here:

1. WebhookChannel.enabled() -> False when the type is "none", regardless of
   NOTIFY_WEBHOOK_ENABLED / NOTIFY_WEBHOOK_URL (so the notifier tick skips it).
2. Settings API round-trip: PUT notify_webhook_type="none" persists and GET
   reports it back; junk values are rejected by the schema Literal (422).
3. POST /api/notifications/test -> 400 while the type is none, whether passed
   explicitly or taken from the saved setting.

Run from the repo root:  pytest tests/test_notify_webhook_none.py -q
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from homestew import config as cfg
from homestew.services import auth, notify_channels


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated data dir + no password; webhook settings restored afterwards."""
    monkeypatch.setattr(cfg.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg.settings, "DEVICES_DIR", tmp_path / "devices")
    monkeypatch.setattr(cfg.settings, "PASSWORD_HASH", "")
    snapshot = {
        key: getattr(cfg.settings, key)
        for key in (
            "NOTIFY_WEBHOOK_TYPE",
            "NOTIFY_WEBHOOK_ENABLED",
            "NOTIFY_WEBHOOK_URL",
            "NOTIFY_WEBHOOK_TOKEN",
        )
    }
    auth._failures.clear()
    auth._pw_cache.update({"mtime": None, "hash": ""})
    yield tmp_path
    for key, value in snapshot.items():
        setattr(cfg.settings, key, value)
    auth._failures.clear()
    auth._pw_cache.update({"mtime": None, "hash": ""})


@pytest.fixture()
def client(env):
    """Signed-in TestClient (forced account creation unlocks the API)."""
    from homestew.main import app

    with TestClient(app) as c:
        r = c.post("/api/auth/setup", json={
            "password": "webhook-none-test",
            "confirm_password": "webhook-none-test",
        })
        assert r.status_code == 201
        yield c


class TestWebhookChannelEnabled:
    def setup_method(self):
        # A fully configured channel - only the type decides here.
        for key, value in {
            "NOTIFY_WEBHOOK_ENABLED": True,
            "NOTIFY_WEBHOOK_URL": "https://example.com/hooks/homestew",
        }.items():
            setattr(cfg.settings, key, value)

    def teardown_method(self):
        cfg.settings.NOTIFY_WEBHOOK_ENABLED = True
        cfg.settings.NOTIFY_WEBHOOK_URL = ""
        cfg.settings.NOTIFY_WEBHOOK_TOKEN = ""

    def test_none_disables_channel_even_with_url_and_enabled_flag(self):
        cfg.settings.NOTIFY_WEBHOOK_TYPE = "none"
        assert notify_channels.WebhookChannel().enabled() is False

    def test_generic_still_enabled_with_url(self):
        cfg.settings.NOTIFY_WEBHOOK_TYPE = "generic"
        assert notify_channels.WebhookChannel().enabled() is True

    def test_synology_still_enabled_with_url(self):
        cfg.settings.NOTIFY_WEBHOOK_TYPE = "synology"
        assert notify_channels.WebhookChannel().enabled() is True

    def test_missing_type_attribute_defaults_to_none(self):
        # Old pickles / stripped settings must fail closed (no delivery).
        del cfg.settings.NOTIFY_WEBHOOK_TYPE
        try:
            assert notify_channels.WebhookChannel().enabled() is False
        finally:
            cfg.settings.NOTIFY_WEBHOOK_TYPE = "none"


class TestSendTestWebhookGuard:
    def test_send_test_webhook_refuses_none_type(self):
        with pytest.raises(ValueError):
            notify_channels.send_test_webhook(
                {"title": "test"},
                url="https://example.com/hooks/homestew",
                webhook_type="none",
            )

    def test_send_test_webhook_falls_back_to_saved_none(self, env):
        cfg.settings.NOTIFY_WEBHOOK_TYPE = "none"
        with pytest.raises(ValueError):
            notify_channels.send_test_webhook(
                {"title": "test"}, url="https://example.com/hooks/homestew"
            )


class TestSettingsApi:
    def test_default_type_is_none(self, client):
        cfg.settings.NOTIFY_WEBHOOK_TYPE = "none"
        assert client.get("/api/settings").json()["notify_webhook_type"] == "none"

    def test_round_trip_none_generic_none(self, client):
        for value in ("generic", "synology", "none"):
            r = client.put("/api/settings", json={"notify_webhook_type": value})
            assert r.status_code == 200
            assert r.json()["notify_webhook_type"] == value
            assert client.get("/api/settings").json()["notify_webhook_type"] == value
            # And it survives to the persisted overrides file.
            persisted = (cfg.settings.DATA_DIR / "settings.json").read_text(
                encoding="utf-8-sig"
            )
            assert f'"{value}"' in persisted

    def test_unknown_type_rejected(self, client):
        assert client.put(
            "/api/settings", json={"notify_webhook_type": "slack"}
        ).status_code == 422


class TestNotificationsTestEndpoint:
    def test_explicit_none_type_is_400(self, client):
        r = client.post("/api/notifications/test", json={
            "webhook_url": "https://example.com/hooks/homestew",
            "webhook_type": "none",
        })
        assert r.status_code == 400
        assert "None" in r.json()["detail"]

    def test_saved_none_type_is_400(self, client):
        cfg.settings.NOTIFY_WEBHOOK_TYPE = "none"
        cfg.settings.NOTIFY_WEBHOOK_URL = "https://example.com/hooks/homestew"
        r = client.post("/api/notifications/test", json={})
        assert r.status_code == 400

    def test_generic_type_still_reaches_send(self, client, monkeypatch):
        # Non-none types must keep flowing to send_test_webhook unchanged.
        # The endpoint imports the symbol by name, so patch it there.
        calls = []
        monkeypatch.setattr(
            "homestew.api.notifications.send_test_webhook",
            lambda *a, **k: calls.append(a),
        )
        r = client.post("/api/notifications/test", json={
            "webhook_url": "https://example.com/hooks/homestew",
            "webhook_type": "generic",
        })
        assert r.status_code == 200
        assert calls and calls[0][4] == "generic"
