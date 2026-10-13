"""The "Provider" selector (Custom vs OpenAI Cloud) added in v0.2.1.

Covers ``LLM_PROVIDER`` / ``LLM_BASE_URL_OVERRIDE``:

* ``effective_llm_base_url()`` resolves the URL actually used: 'custom' and
  'openai'+override use the stored LLM_BASE_URL; 'openai' without an override
  ignores it and returns OpenAI's hosted endpoint.
* The settings API round-trips both fields, reports the STORED base URL (so
  switching back from the cloud provider restores the custom one), and the
  model probe / status fall back to the effective URL when none is supplied.
* ``llm_looks_configured()`` treats a deliberate 'openai' selection as set up.
* Cloud quirk: with provider='openai' the completions reasoning switches
  (chat_template_kwargs / think) are never sent - api.openai.com rejects
  unknown top-level body fields - while the Responses effort still rides along.
* get_llm_client() builds the cached client against the effective URL.

Fixture patterns copied from test_llm_api_kind.py.

Run from repo root:  pytest tests/test_llm_provider.py -q
"""
import json

import pytest
from fastapi.testclient import TestClient

from homestew import config as cfg
from homestew.config import OPENAI_CLOUD_BASE_URL, effective_llm_base_url
from homestew.services import auth
from homestew.services.llm_client import LLMClient, _reasoning_params


@pytest.fixture(autouse=True)
def _restore_provider_settings():
    """All touched settings live on the live singleton; never leak."""
    orig = {
        "LLM_PROVIDER": cfg.settings.LLM_PROVIDER,
        "LLM_BASE_URL_OVERRIDE": cfg.settings.LLM_BASE_URL_OVERRIDE,
        "LLM_BASE_URL": cfg.settings.LLM_BASE_URL,
        "LLM_API_KEY": cfg.settings.LLM_API_KEY,
        "LLM_MODEL": cfg.settings.LLM_MODEL,
        "LLM_REASONING_ENABLED": cfg.settings.LLM_REASONING_ENABLED,
    }
    yield
    for key, value in orig.items():
        setattr(cfg.settings, key, value)


# ---------------------------------------------------------------------------
# effective_llm_base_url() - provider -> URL actually used
# ---------------------------------------------------------------------------

class TestEffectiveBaseUrl:
    def test_custom_uses_stored_url(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "LLM_PROVIDER", "custom")
        monkeypatch.setattr(cfg.settings, "LLM_BASE_URL", "http://ollama:11434/v1")
        assert effective_llm_base_url() == "http://ollama:11434/v1"

    def test_openai_without_override_returns_cloud_url(self, monkeypatch):
        # The stored custom URL is ignored entirely while the cloud provider
        # is selected - it stays on disk so switching back restores it.
        monkeypatch.setattr(cfg.settings, "LLM_PROVIDER", "openai")
        monkeypatch.setattr(cfg.settings, "LLM_BASE_URL_OVERRIDE", False)
        monkeypatch.setattr(cfg.settings, "LLM_BASE_URL", "http://ollama:11434/v1")
        assert effective_llm_base_url() == OPENAI_CLOUD_BASE_URL

    def test_openai_with_override_uses_stored_url(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "LLM_PROVIDER", "openai")
        monkeypatch.setattr(cfg.settings, "LLM_BASE_URL_OVERRIDE", True)
        monkeypatch.setattr(
            cfg.settings, "LLM_BASE_URL", "https://my-proxy.example.com/v1"
        )
        assert effective_llm_base_url() == "https://my-proxy.example.com/v1"

    def test_missing_provider_attribute_defaults_to_custom(self, monkeypatch):
        # An older settings object without the attribute behaves as 'custom'.
        monkeypatch.delattr(cfg.settings, "LLM_PROVIDER", raising=False)
        monkeypatch.setattr(cfg.settings, "LLM_BASE_URL", "http://host:1/v1")
        assert effective_llm_base_url() == "http://host:1/v1"


# ---------------------------------------------------------------------------
# Cloud quirk: reasoning switches suppressed on api.openai.com
# ---------------------------------------------------------------------------

class TestReasoningCloudQuirk:
    def test_reasoning_off_still_sends_switches_on_custom(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "LLM_PROVIDER", "custom")
        monkeypatch.setattr(cfg.settings, "LLM_REASONING_ENABLED", False)
        params = _reasoning_params()
        assert params["think"] is False
        assert params["chat_template_kwargs"]["enable_thinking"] is False

    def test_reasoning_off_sends_nothing_on_openai_cloud(self, monkeypatch):
        # api.openai.com 400s on unknown top-level body fields, so the
        # local-server switches must never be sent there.
        monkeypatch.setattr(cfg.settings, "LLM_PROVIDER", "openai")
        monkeypatch.setattr(cfg.settings, "LLM_REASONING_ENABLED", False)
        assert _reasoning_params() == {}

    def test_reasoning_on_sends_nothing_either(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "LLM_PROVIDER", "custom")
        monkeypatch.setattr(cfg.settings, "LLM_REASONING_ENABLED", True)
        assert _reasoning_params() == {}


# ---------------------------------------------------------------------------
# Settings API round-trip + probe/status fallback
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated data dir + no password (test_llm_api_kind pattern)."""
    monkeypatch.setattr(cfg.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg.settings, "DEVICES_DIR", tmp_path / "devices")
    monkeypatch.setattr(cfg.settings, "PASSWORD_HASH", "")
    auth._failures.clear()
    auth._pw_cache.update({"mtime": None, "hash": ""})
    yield tmp_path
    auth._failures.clear()
    auth._pw_cache.update({"mtime": None, "hash": ""})


@pytest.fixture()
def client(env):
    """Signed-in TestClient (forced account creation unlocks the API)."""
    from homestew.main import app

    with TestClient(app) as c:
        r = c.post("/api/auth/setup", json={
            "password": "provider-test-password",
            "confirm_password": "provider-test-password",
        })
        assert r.status_code == 201
        yield c


class TestSettingsApi:
    def test_get_reports_defaults(self, client):
        body = client.get("/api/settings").json()
        assert body["llm_provider"] == "custom"
        assert body["llm_base_url_override"] is False

    def test_put_openai_applies_and_persists(self, client, env):
        r = client.put("/api/settings", json={"llm_provider": "openai"})
        assert r.status_code == 200
        assert r.json()["llm_provider"] == "openai"
        assert cfg.settings.LLM_PROVIDER == "openai"
        raw = json.loads((env / "settings.json").read_text(encoding="utf-8-sig"))
        assert raw["LLM_PROVIDER"] == "openai"

    def test_put_openai_keeps_stored_base_url(self, client):
        # Selecting the cloud provider must NOT clobber the stored custom URL.
        client.put("/api/settings", json={
            "llm_base_url": "http://ollama:11434/v1",
            "llm_model": "llama3",
        })
        r = client.put("/api/settings", json={"llm_provider": "openai"})
        assert r.json()["llm_base_url"] == "http://ollama:11434/v1"

    def test_put_override_applies_and_persists(self, client, env):
        r = client.put("/api/settings", json={
            "llm_provider": "openai",
            "llm_base_url_override": True,
            "llm_base_url": "https://proxy.example.com/v1",
        })
        assert r.json()["llm_base_url_override"] is True
        assert cfg.settings.LLM_BASE_URL_OVERRIDE is True
        raw = json.loads((env / "settings.json").read_text(encoding="utf-8-sig"))
        assert raw["LLM_BASE_URL_OVERRIDE"] is True

    def test_put_override_false_applies(self, client):
        # Unticking the checkbox (a plain boolean) must persist as False.
        client.put("/api/settings", json={
            "llm_provider": "openai",
            "llm_base_url_override": True,
        })
        r = client.put("/api/settings", json={"llm_base_url_override": False})
        assert r.json()["llm_base_url_override"] is False

    def test_put_invalid_provider_rejected_with_422(self, client):
        r = client.put("/api/settings", json={"llm_provider": "anthropic"})
        assert r.status_code == 422

    def test_llm_configured_true_when_openai_selected(self, client):
        # A deliberate cloud selection counts as configured even with no key,
        # model, or non-default URL, so the wizard stops prompting for AI.
        client.put("/api/settings", json={"llm_provider": "openai"})
        assert client.get("/api/settings").json()["llm_configured"] is True


# ---------------------------------------------------------------------------
# get_llm_client() builds against the effective URL
# ---------------------------------------------------------------------------

class TestClientPropagation:
    def test_get_llm_client_uses_cloud_url_for_openai(self, monkeypatch):
        from homestew.api import chat

        monkeypatch.setattr(cfg.settings, "LLM_PROVIDER", "openai")
        monkeypatch.setattr(cfg.settings, "LLM_BASE_URL_OVERRIDE", False)
        monkeypatch.setattr(cfg.settings, "LLM_BASE_URL", "http://ollama:11434/v1")
        chat.reset_llm_client()
        try:
            assert chat.get_llm_client().base_url == OPENAI_CLOUD_BASE_URL.rstrip("/")
        finally:
            chat.reset_llm_client()

    def test_get_llm_client_uses_stored_url_for_custom(self, monkeypatch):
        from homestew.api import chat

        monkeypatch.setattr(cfg.settings, "LLM_PROVIDER", "custom")
        monkeypatch.setattr(cfg.settings, "LLM_BASE_URL", "http://ollama:11434/v1")
        chat.reset_llm_client()
        try:
            assert chat.get_llm_client().base_url == "http://ollama:11434/v1"
        finally:
            chat.reset_llm_client()
