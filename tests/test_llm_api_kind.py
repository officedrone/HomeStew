"""The "API Endpoint Type" selector (Settings > AI + first-run wizard).

Covers the chat-completions vs responses surface added for
``LLM_API_KIND`` / ``LLM_REASONING_EFFORT``:

* ``_to_responses_input()`` maps the internal completions-style history
  onto Responses API input items (function_call / function_call_output,
  chat-style text/image_url parts converted to input_text/input_image —
  LM Studio rejects the completions part shapes with 400 invalid_union).
* ``_flatten_tools()`` converts nested chat-completions tool definitions
  to the flat Responses shape.
* Request shaping: with kind='responses' both streaming and non-streaming
  paths POST {base}/responses with an ``input`` body (never ``messages``)
  and carry ``reasoning.effort`` from the setting; the default kind keeps
  posting {base}/chat/completions byte-identically to before.
* ``_normalize_responses_stream()`` translates typed Responses SSE events
  into the same normalized vocabulary the tool loop consumes (content /
  reasoning deltas, one complete tool_call per function-call item, finish
  on terminal events, RuntimeError on error events).
* The settings API round-trips both fields and get_llm_client() propagates
  the kind onto the cached client.

Fixture patterns copied from test_llm_reasoning.py.

Run from repo root:  pytest tests/test_llm_api_kind.py -q
"""
import json

import pytest
from fastapi.testclient import TestClient

from homestew import config as cfg
from homestew.services import auth
from homestew.services.llm_client import (
    LLMClient,
    _flatten_tools,
    _normalize_responses_stream,
    _to_responses_input,
)


@pytest.fixture(autouse=True)
def _restore_api_kind():
    """Both settings live on the live singleton; never leak between tests."""
    orig_kind = cfg.settings.LLM_API_KIND
    orig_effort = cfg.settings.LLM_REASONING_EFFORT
    yield
    cfg.settings.LLM_API_KIND = orig_kind
    cfg.settings.LLM_REASONING_EFFORT = orig_effort


# ---------------------------------------------------------------------------
# _to_responses_input() - completions history -> Responses input items
# ---------------------------------------------------------------------------

class TestToResponsesInput:
    def test_plain_turns_become_role_content_items(self):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        assert _to_responses_input(messages) == messages

    def test_assistant_tool_calls_become_function_call_items(self):
        messages = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "search_manuals", "arguments": '{"q":"x"}'},
            }],
        }]
        assert _to_responses_input(messages) == [{
            "type": "function_call",
            "call_id": "call_1",
            "name": "search_manuals",
            "arguments": '{"q":"x"}',
        }]

    def test_assistant_text_with_tool_calls_keeps_message_item(self):
        messages = [{
            "role": "assistant",
            "content": "Let me look that up.",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "manage_calendar", "arguments": "{}"},
            }],
        }]
        items = _to_responses_input(messages)
        assert items[0] == {"role": "assistant", "content": "Let me look that up."}
        assert items[1]["type"] == "function_call"

    def test_tool_reply_becomes_function_call_output(self):
        messages = [{"role": "tool", "content": "3 results", "tool_call_id": "call_9"}]
        assert _to_responses_input(messages) == [{
            "type": "function_call_output",
            "call_id": "call_9",
            "output": "3 results",
        }]

    def test_multimodal_parts_converted_to_responses_shape(self):
        # LM Studio /responses rejects chat-style parts (400 invalid_union);
        # text -> input_text, image_url{url} -> input_image with a plain url.
        parts = [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]
        items = _to_responses_input([{"role": "user", "content": parts}])
        assert items == [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "What is this?"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
            ],
        }]

    def test_assistant_multimodal_parts_use_output_text(self):
        # A replayed assistant turn must not claim an input_* part type.
        parts = [{"type": "text", "text": "I see a cat."}]
        items = _to_responses_input([{"role": "assistant", "content": parts}])
        assert items == [{
            "role": "assistant",
            "content": [{"type": "output_text", "text": "I see a cat."}],
        }]

    def test_already_responses_parts_pass_through(self):
        parts = [
            {"type": "input_text", "text": "hi"},
            {"type": "input_image", "image_url": "data:image/png;base64,BBB"},
        ]
        items = _to_responses_input([{"role": "user", "content": parts}])
        assert items == [{"role": "user", "content": parts}]

    def test_image_url_string_form_and_empty_parts_dropped(self):
        # image_url may arrive as a bare string; blank images are skipped.
        parts = [
            {"type": "image_url", "image_url": "data:image/png;base64,CCC"},
            {"type": "image_url", "image_url": {"url": ""}},
        ]
        items = _to_responses_input([{"role": "user", "content": parts}])
        assert items == [{
            "role": "user",
            "content": [{"type": "input_image", "image_url": "data:image/png;base64,CCC"}],
        }]


# ---------------------------------------------------------------------------
# _flatten_tools() - nested completions definitions -> flat Responses items
# ---------------------------------------------------------------------------

class TestFlattenTools:
    def test_nested_definition_flattened(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "search_manuals",
                "description": "Search PDFs",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        assert _flatten_tools(tools) == [{
            "type": "function",
            "name": "search_manuals",
            "description": "Search PDFs",
            "parameters": {"type": "object", "properties": {}},
        }]

    def test_none_and_empty_return_empty_list(self):
        assert _flatten_tools(None) == []
        assert _flatten_tools([]) == []


# ---------------------------------------------------------------------------
# Request shaping per API kind (raw-requests transport captures the payload)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload=None, lines=None):
        self.payload = payload if payload is not None else {"id": "resp_1"}
        self._lines = lines or []

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload

    def iter_lines(self):
        return iter(self._lines)

    def close(self):
        pass


def _client_requests_mode(monkeypatch, api_kind="chat_completions"):
    """LLMClient forced onto the raw-requests path; captures the POST."""
    import requests

    client = LLMClient("http://fake/v1", "", "m", api_kind=api_kind)
    client._use_openai = False
    captured = {}

    def fake_post(url, json=None, headers=None, stream=False):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)
    return client, captured


class TestRequestShaping:
    def test_responses_non_stream_posts_input_to_responses_endpoint(
        self, monkeypatch
    ):
        client, captured = _client_requests_mode(monkeypatch, api_kind="responses")
        client.chat_completion([{"role": "user", "content": "hi"}])
        assert captured["url"] == "http://fake/v1/responses"
        body = captured["json"]
        assert body["input"] == [{"role": "user", "content": "hi"}]
        assert "messages" not in body

    def test_responses_payload_carries_reasoning_effort(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "LLM_REASONING_EFFORT", "high")
        client, captured = _client_requests_mode(monkeypatch, api_kind="responses")
        client.chat_completion([{"role": "user", "content": "hi"}])
        assert captured["json"]["reasoning"] == {"effort": "high"}

    def test_responses_payload_omits_reasoning_when_effort_blank(self, monkeypatch):
        monkeypatch.setattr(cfg.settings, "LLM_REASONING_EFFORT", "")
        client, captured = _client_requests_mode(monkeypatch, api_kind="responses")
        client.chat_completion([{"role": "user", "content": "hi"}])
        assert "reasoning" not in captured["json"]

    def test_responses_payload_flattens_tools(self, monkeypatch):
        client, captured = _client_requests_mode(monkeypatch, api_kind="responses")
        client.chat_completion(
            [{"role": "user", "content": "hi"}],
            tools=[{
                "type": "function",
                "function": {"name": "search_manuals", "parameters": {}},
            }],
        )
        assert captured["json"]["tools"] == [
            {"type": "function", "name": "search_manuals", "parameters": {}}
        ]

    def test_default_kind_still_posts_messages_to_chat_completions(
        self, monkeypatch
    ):
        # Regression: the historical surface is untouched by this feature.
        client, captured = _client_requests_mode(monkeypatch)
        client.chat_completion([{"role": "user", "content": "hi"}])
        assert captured["url"] == "http://fake/v1/chat/completions"
        body = captured["json"]
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        assert "input" not in body

    def test_stream_responses_posts_to_responses_with_stream_flag(self, monkeypatch):
        import requests

        client, captured = _client_requests_mode(monkeypatch, api_kind="responses")
        events, close = client.stream_chat_completion(
            [{"role": "user", "content": "hi"}]
        )
        assert captured["url"] == "http://fake/v1/responses"
        assert captured["json"]["stream"] is True
        assert list(events) == []  # empty fake stream
        close()

    def test_unknown_api_kind_falls_back_to_chat_completions(self):
        client = LLMClient("http://fake/v1", "", "m", api_kind="bogus")
        assert client.api_kind == "chat_completions"


# ---------------------------------------------------------------------------
# _normalize_responses_stream() - typed SSE events -> normalized vocabulary
# ---------------------------------------------------------------------------

def _sse(*events):
    """Return parsed event dicts (the SSE line parsing lives in the client's
    _iter_sse_json_lines; the normalizer consumes already-parsed events)."""
    return list(events)


class TestNormalizeResponsesStream:
    def test_output_text_deltas_become_content_events(self):
        raw = _sse(
            {"type": "response.created"},
            {"type": "response.output_text.delta", "delta": "Hel"},
            {"type": "response.output_text.delta", "delta": "lo"},
            {"type": "response.completed", "response": {"status": "completed"}},
        )
        events = list(_normalize_responses_stream(raw))
        assert events == [
            {"type": "content", "delta": "Hel"},
            {"type": "content", "delta": "lo"},
            {"type": "finish", "reason": "completed"},
        ]

    def test_reasoning_summary_deltas_become_reasoning_events(self):
        raw = _sse(
            {"type": "response.reasoning_summary_text.delta", "delta": "thinking..."},
            {"type": "response.completed", "response": {"status": "completed"}},
        )
        events = list(_normalize_responses_stream(raw))
        assert events[0] == {"type": "reasoning", "delta": "thinking..."}

    def test_function_call_fragments_flush_once_on_item_done(self):
        raw = _sse(
            {"type": "response.output_item.added", "item": {
                "type": "function_call", "id": "fc_1",
                "call_id": "call_1", "name": "search_manuals",
            }},
            {"type": "response.function_call_arguments.delta",
             "item_id": "fc_1", "delta": '{"qu'},
            {"type": "response.function_call_arguments.delta",
             "item_id": "fc_1", "delta": 'ery":"x"}'},
            {"type": "response.output_item.done", "item": {
                "type": "function_call", "id": "fc_1", "call_id": "call_1",
                "name": "search_manuals", "arguments": '{"query":"x"}',
            }},
            {"type": "response.completed", "response": {"status": "completed"}},
        )
        events = list(_normalize_responses_stream(raw))
        tool_calls = [e for e in events if e["type"] == "tool_call"]
        assert tool_calls == [{
            "type": "tool_call",
            "call": {
                "id": "call_1",
                "name": "search_manuals",
                "arguments": '{"query":"x"}',
            },
        }]
        assert events[-1] == {"type": "finish", "reason": "completed"}

    def test_pending_call_flushed_without_terminal_event(self):
        # Servers that close the stream without response.completed must not
        # lose the accumulated call (mirrors _normalize_stream's behaviour).
        raw = _sse(
            {"type": "response.output_item.added", "item": {
                "type": "function_call", "id": "fc_1",
                "call_id": "call_1", "name": "manage_calendar",
            }},
            {"type": "response.function_call_arguments.delta",
             "item_id": "fc_1", "delta": '{"action":"list"}'},
        )
        events = list(_normalize_responses_stream(raw))
        assert events == [{
            "type": "tool_call",
            "call": {
                "id": "call_1",
                "name": "manage_calendar",
                "arguments": '{"action":"list"}',
            },
        }]

    def test_failed_response_finishes_after_flushing_calls(self):
        raw = _sse(
            {"type": "response.output_item.added", "item": {
                "type": "function_call", "id": "fc_1",
                "call_id": "call_1", "name": "search_manuals",
                "arguments": "{}",
            }},
            {"type": "response.incomplete", "response": {"status": "incomplete"}},
        )
        events = list(_normalize_responses_stream(raw))
        assert [e["type"] for e in events] == ["tool_call", "finish"]
        assert events[-1]["reason"] == "incomplete"

    def test_error_event_raises(self):
        raw = _sse(
            {"type": "error", "error": {"message": "model does not support images"}},
        )
        with pytest.raises(RuntimeError, match="does not support images"):
            list(_normalize_responses_stream(raw))

    def test_unknown_event_types_are_ignored(self):
        raw = _sse(
            {"type": "response.created", "response": {}},
            {"type": "response.content_part.added", "part": {"type": "output_text"}},
            {"type": "some.future.event"},
            {"type": "response.completed", "response": {"status": "completed"}},
        )
        events = list(_normalize_responses_stream(raw))
        assert events == [{"type": "finish", "reason": "completed"}]


# ---------------------------------------------------------------------------
# Settings API round-trip + client propagation
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated data dir + no password (test_llm_reasoning pattern)."""
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
            "password": "api-kind-test-password",
            "confirm_password": "api-kind-test-password",
        })
        assert r.status_code == 201
        yield c


class TestSettingsApi:
    def test_get_reports_defaults(self, client):
        body = client.get("/api/settings").json()
        assert body["llm_api_kind"] == "chat_completions"
        assert body["llm_reasoning_effort"] == "medium"

    def test_put_responses_applies_and_persists(self, client, env):
        r = client.put("/api/settings", json={"llm_api_kind": "responses"})
        assert r.status_code == 200
        assert r.json()["llm_api_kind"] == "responses"
        # Live singleton updated (get_llm_client rebuilds from it)...
        assert cfg.settings.LLM_API_KIND == "responses"
        # ...and the value survives a restart via settings.json.
        raw = json.loads((env / "settings.json").read_text(encoding="utf-8-sig"))
        assert raw["LLM_API_KIND"] == "responses"

    def test_put_effort_applies_and_persists(self, client, env):
        r = client.put("/api/settings", json={"llm_reasoning_effort": "low"})
        assert r.json()["llm_reasoning_effort"] == "low"
        assert cfg.settings.LLM_REASONING_EFFORT == "low"
        raw = json.loads((env / "settings.json").read_text(encoding="utf-8-sig"))
        assert raw["LLM_REASONING_EFFORT"] == "low"

    def test_put_omitted_leaves_values_unchanged(self, client):
        client.put("/api/settings", json={
            "llm_api_kind": "responses",
            "llm_reasoning_effort": "high",
        })
        r = client.put("/api/settings", json={"llm_model": "other-model"})
        assert r.json()["llm_api_kind"] == "responses"
        assert r.json()["llm_reasoning_effort"] == "high"

    def test_put_invalid_kind_rejected_with_422(self, client):
        r = client.put("/api/settings", json={"llm_api_kind": "grpc"})
        assert r.status_code == 422

    def test_put_invalid_effort_rejected_with_422(self, client):
        r = client.put("/api/settings", json={"llm_reasoning_effort": "ultra"})
        assert r.status_code == 422


class TestClientPropagation:
    def test_get_llm_client_propagates_api_kind(self, monkeypatch):
        from homestew.api import chat

        monkeypatch.setattr(cfg.settings, "LLM_API_KIND", "responses")
        chat.reset_llm_client()
        try:
            assert chat.get_llm_client().api_kind == "responses"
            # And the default again after switching back.
            monkeypatch.setattr(cfg.settings, "LLM_API_KIND", "chat_completions")
            chat.reset_llm_client()
            assert chat.get_llm_client().api_kind == "chat_completions"
        finally:
            chat.reset_llm_client()
