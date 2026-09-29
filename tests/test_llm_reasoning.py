"""The "Reasoning" toggle (Settings > AI + first-run wizard).

Covers the whole chain added for ``LLM_REASONING_ENABLED``:

* ``llm_client._reasoning_params()`` is empty while reasoning is allowed
  (the default) and carries the explicit "do not think" switches when the
  checkbox is off.
* Both request paths of ``LLMClient`` (OpenAI SDK via ``extra_body``, raw
  requests via the JSON payload) include those fields only when reasoning
  is disabled - so API-call reasoning parameters follow the checkbox value.
* The settings API round-trips ``llm_reasoning_enabled``: GET reports it,
  PUT applies False as well as True, persists to settings.json, and an
  omitted field leaves the stored value untouched (wizard + Settings save
  both go through this endpoint).

Fixture pattern copied from test_search_index_jobs.py: a signed-in
TestClient on a throwaway DATA_DIR for the auth-gated endpoints.

Run from repo root:  pytest tests/test_llm_reasoning.py -q
"""
import json

import pytest
from fastapi.testclient import TestClient

from homestew import config as cfg
from homestew.services import auth
from homestew.services.llm_client import LLMClient, _reasoning_params


@pytest.fixture(autouse=True)
def _restore_reasoning():
    """The toggle lives on the live singleton; never leak changes between tests."""
    orig = cfg.settings.LLM_REASONING_ENABLED
    yield
    cfg.settings.LLM_REASONING_ENABLED = orig


# ---------------------------------------------------------------------------
# _reasoning_params() - the checkbox -> request-field mapping
# ---------------------------------------------------------------------------

class TestReasoningParams:
    def test_enabled_default_adds_nothing(self, monkeypatch):
        # Checked (default): the server/model decides natively.
        monkeypatch.setattr(cfg.settings, "LLM_REASONING_ENABLED", True)
        assert _reasoning_params() == {}

    def test_disabled_emits_all_common_switches(self, monkeypatch):
        # Unchecked: llama.cpp/vLLM template kwargs + Ollama's think flag.
        monkeypatch.setattr(cfg.settings, "LLM_REASONING_ENABLED", False)
        params = _reasoning_params()
        assert params["chat_template_kwargs"] == {
            "enable_thinking": False,
            "thinking": False,
        }
        assert params["think"] is False

    def test_missing_attribute_falls_back_to_enabled(self, monkeypatch):
        # Older settings objects without the key behave like a checked box.
        monkeypatch.delattr(cfg.settings, "LLM_REASONING_ENABLED", raising=False)
        assert _reasoning_params() == {}


# ---------------------------------------------------------------------------
# LLMClient request paths carry (or omit) the reasoning fields
# ---------------------------------------------------------------------------

class _CapturingCompletions:
    """Stand-in for the OpenAI SDK: records kwargs, returns a sentinel."""

    def __init__(self):
        self.kwargs = None

    class _NS:
        pass

    def create(self, **kwargs):
        self.kwargs = kwargs
        return "response"


def _client_with_fake_sdk():
    client = LLMClient("http://fake/v1", "", "m")
    client._use_openai = True
    completions = _CapturingCompletions()
    holder = _CapturingCompletions._NS()
    holder.chat = _CapturingCompletions._NS()
    holder.chat.completions = completions
    client.client = holder
    return client, completions


class TestOpenAiPath:
    def test_enabled_omits_extra_body(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "LLM_REASONING_ENABLED", True)
        client, completions = _client_with_fake_sdk()
        client.chat_completion([{"role": "user", "content": "hi"}])
        assert "extra_body" not in completions.kwargs

    def test_disabled_passes_extra_body(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "LLM_REASONING_ENABLED", False)
        client, completions = _client_with_fake_sdk()
        client.chat_completion([{"role": "user", "content": "hi"}])
        extra = completions.kwargs["extra_body"]
        assert extra["chat_template_kwargs"]["enable_thinking"] is False
        assert extra["think"] is False

    def test_streaming_disabled_passes_extra_body(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "LLM_REASONING_ENABLED", False)
        client, completions = _client_with_fake_sdk()
        events, close = client.stream_chat_completion(
            [{"role": "user", "content": "hi"}]
        )
        assert completions.kwargs["stream"] is True
        assert completions.kwargs["extra_body"]["think"] is False
        list(events)  # exhaust (the sentinel string has no choices -> no events)
        close()


class _FakeResponse:
    def __init__(self):
        self.payload = {"choices": [{"message": {"content": "ok"}}]}

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def _client_requests_mode(monkeypatch):
    """LLMClient forced onto the raw-requests path; captures the POST payload."""
    import requests

    client = LLMClient("http://fake/v1", "", "m")
    client._use_openai = False
    captured = {}

    def fake_post(url, json=None, headers=None, stream=False):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)
    return client, captured


class TestRequestsPath:
    def test_enabled_payload_has_no_reasoning_fields(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "LLM_REASONING_ENABLED", True)
        client, captured = _client_requests_mode(monkeypatch)
        client.chat_completion([{"role": "user", "content": "hi"}])
        assert "think" not in captured["json"]
        assert "chat_template_kwargs" not in captured["json"]

    def test_disabled_payload_carries_reasoning_fields(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "LLM_REASONING_ENABLED", False)
        client, captured = _client_requests_mode(monkeypatch)
        client.chat_completion([{"role": "user", "content": "hi"}])
        payload = captured["json"]
        assert payload["think"] is False
        assert payload["chat_template_kwargs"]["enable_thinking"] is False

    def test_disabled_stream_payload_carries_reasoning_fields(self, monkeypatch):
        # stream=True would make requests iterate the (fake) response; patch
        # iter_lines so the SSE parser gets an empty stream instead.
        monkeypatch.setattr(cfg.settings, "LLM_REASONING_ENABLED", False)
        client, captured = _client_requests_mode(monkeypatch)

        import requests

        def fake_post(url, json=None, headers=None, stream=False):
            resp = _FakeResponse()
            resp.iter_lines = lambda: iter([])
            resp.close = lambda: None
            captured["url"] = url
            captured["json"] = json
            return resp

        monkeypatch.setattr(requests, "post", fake_post)
        events, close = client.stream_chat_completion(
            [{"role": "user", "content": "hi"}]
        )
        assert captured["json"]["stream"] is True
        assert captured["json"]["think"] is False
        list(events)
        close()


# ---------------------------------------------------------------------------
# Settings API round-trip (Settings save + wizard both PUT here)
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated data dir + no password (test_search_index_jobs pattern)."""
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
            "password": "reasoning-test-password",
            "confirm_password": "reasoning-test-password",
        })
        assert r.status_code == 201
        yield c


class TestSettingsApi:
    def test_get_reports_default_true(self, client):
        body = client.get("/api/settings").json()
        assert body["llm_reasoning_enabled"] is True

    def test_put_false_applies_and_persists(self, client, env):
        r = client.put("/api/settings", json={"llm_reasoning_enabled": False})
        assert r.status_code == 200
        assert r.json()["llm_reasoning_enabled"] is False
        # Live singleton updated (the LLM client reads it per request)...
        assert cfg.settings.LLM_REASONING_ENABLED is False
        # ...and the value survives a restart via settings.json.
        raw = json.loads((env / "settings.json").read_text(encoding="utf-8-sig"))
        assert raw["LLM_REASONING_ENABLED"] is False

    def test_put_true_re_enables(self, client):
        client.put("/api/settings", json={"llm_reasoning_enabled": False})
        r = client.put("/api/settings", json={"llm_reasoning_enabled": True})
        assert r.json()["llm_reasoning_enabled"] is True
        assert cfg.settings.LLM_REASONING_ENABLED is True

    def test_put_omitted_leaves_value_unchanged(self, client):
        # Plain-boolean semantics: None means "unchanged", not "off".
        client.put("/api/settings", json={"llm_reasoning_enabled": False})
        r = client.put("/api/settings", json={"llm_model": "other-model"})
        assert r.json()["llm_reasoning_enabled"] is False
