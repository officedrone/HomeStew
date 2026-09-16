"""LLM client supporting OpenAI-compatible APIs with tool calling."""
import asyncio
import logging
import re
from typing import Optional, List, Dict, Any, AsyncGenerator
import json

from homestew.config import settings
from homestew.default_prompts import (
    DEFAULT_CALENDAR_TOOL_DESCRIPTION,
    DEFAULT_SEARCH_TOOL_DESCRIPTION,
)
from homestew.services import calendar_tool

logger = logging.getLogger(__name__)


class LLMClient:
    """Client for OpenAI-compatible LLM APIs."""
    
    def __init__(self, base_url: str, api_key: str, model: str):
        """
        Initialize LLM client.
        
        Args:
            base_url: Base URL of the API (e.g., http://localhost:11434/v1)
            api_key: API key (can be dummy for local servers)
            model: Model name to use
        """
        self.base_url = base_url.rstrip('/')
        # OpenAI-compatible clients require a non-empty key even when the
        # server ignores it (local Ollama etc.) — substitute a dummy.
        self.api_key = api_key if api_key and api_key.strip() else "not-needed"
        self.model = model
        
        # Try to import openai library, fallback to requests if not available
        try:
            from openai import OpenAI
            self._use_openai = True
            self.client = OpenAI(
                base_url=self.base_url,
                api_key=self.api_key
            )
            logger.info("Using OpenAI library for LLM client")
        except ImportError:
            self._use_openai = False
            logger.warning("OpenAI library not available, using requests fallback")
    
    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        stream: bool = False
    ) -> Any:
        """
        Send a chat completion request.
        
        Args:
            messages: List of message dicts with 'role' and 'content'
            tools: Optional list of tool definitions
            stream: Whether to stream the response
            
        Returns:
            Response object or generator if streaming
        """
        if self._use_openai:
            return self._openai_chat_completion(messages, tools, stream)
        else:
            return self._requests_chat_completion(messages, tools, stream)
    
    def _openai_chat_completion(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        stream: bool = False
    ) -> Any:
        """Chat completion using OpenAI library."""
        kwargs = {
            "model": self.model,
            "messages": messages,
            "stream": stream
        }
        
        if tools:
            kwargs["tools"] = tools
        
        return self.client.chat.completions.create(**kwargs)
    
    def _requests_chat_completion(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        stream: bool = False
    ) -> Any:
        """Chat completion using requests (fallback)."""
        import requests
        
        url = f"{self.base_url}/chat/completions"
        
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": stream
        }
        
        if tools:
            payload["tools"] = tools
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        
        response = requests.post(url, json=payload, headers=headers, stream=stream)
        response.raise_for_status()
        
        if stream:
            return self._parse_stream(response.iter_lines())
        
        return response.json()
    
    def _parse_stream(self, lines) -> AsyncGenerator[str, None]:
        """Parse streaming response."""
        for line in lines:
            if line:
                line = line.decode('utf-8')
                if line.startswith('data: '):
                    data = line[6:]
                    if data == '[DONE]':
                        break
                    try:
                        chunk = json.loads(data)
                        content = chunk['choices'][0]['delta'].get('content', '')
                        if content:
                            yield content
                    except (json.JSONDecodeError, KeyError):
                        continue

    def stream_chat_completion(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None
    ):
        """Open a streaming chat completion and return an event iterator.

        Blocking call: returns a (lazy) generator yielding normalised dicts:
          - {"type": "reasoning", "delta": str}  — model thinking tokens
          - {"type": "content", "delta": str}    — answer text tokens
          - {"type": "tool_call", "call": {"id","name","arguments"}}
          - {"type": "finish", "reason": str}

        Reasoning deltas come from the non-standard ``reasoning`` /
        ``reasoning_content`` delta fields that Ollama, llama.cpp and vLLM
        expose for thinking models; servers without them simply never emit
        reasoning events.

        Returns a ``(events, close)`` tuple: ``events`` is the lazy generator
        of normalised dicts and ``close`` tears down the underlying HTTP
        response (safe to call from another thread — it unblocks a pending
        read so generation can be interrupted).
        """
        if self._use_openai:
            kwargs = {
                "model": self.model,
                "messages": messages,
                "stream": True,
            }
            if tools:
                kwargs["tools"] = tools
            raw_chunks = self.client.chat.completions.create(**kwargs)

            def _close():
                # The SDK Stream exposes close(); guard for backends that don't.
                try:
                    raw_chunks.close()
                except Exception:  # noqa: BLE001 - teardown is best-effort
                    pass
        else:
            import requests

            url = f"{self.base_url}/chat/completions"
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": True,
            }
            if tools:
                payload["tools"] = tools
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
            response = requests.post(url, json=payload, headers=headers, stream=True)
            response.raise_for_status()
            raw_chunks = self._iter_sse_json_lines(response.iter_lines())

            def _close():
                try:
                    response.close()
                except Exception:  # noqa: BLE001 - teardown is best-effort
                    pass

        return _normalize_stream(raw_chunks), _close

    @staticmethod
    def _iter_sse_json_lines(lines):
        """Yield parsed JSON chunks from an SSE byte-line iterator."""
        for line in lines:
            if not line:
                continue
            text = line.decode('utf-8')
            if not text.startswith('data: '):
                continue
            data = text[6:]
            if data == '[DONE]':
                break
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                continue


# Delta fields carrying model "thinking" output across common servers.
_REASONING_KEYS = ("reasoning", "reasoning_content")


def _pick(obj, key):
    """Read a field from either an SDK object or a plain dict chunk."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    value = getattr(obj, key, None)
    if value is None:
        # OpenAI SDK stores non-standard fields (reasoning etc.) here.
        extra = getattr(obj, "model_extra", None) or {}
        value = extra.get(key)
    return value


def _flush_tool_calls(acc: Dict[int, Dict[str, Any]]):
    """Emit accumulated streamed tool-call fragments as completed calls."""
    for index in sorted(acc):
        call = acc[index]
        if call["id"] or call["name"] or call["arguments"]:
            yield {
                "type": "tool_call",
                "call": {
                    "id": call["id"],
                    "name": call["name"] or "search_manuals",
                    "arguments": call["arguments"] or "{}",
                },
            }
    acc.clear()


def _normalize_stream(raw_chunks):
    """Translate raw stream chunks (SDK objects or dicts) into events."""
    tool_calls_acc: Dict[int, Dict[str, Any]] = {}

    for chunk in raw_chunks:
        choices = _pick(chunk, "choices")
        if not choices:
            continue
        choice = choices[0]
        delta = _pick(choice, "delta")

        reasoning = None
        if delta is not None:
            for key in _REASONING_KEYS:
                reasoning = _pick(delta, key)
                if reasoning:
                    break

        # Accumulate tool-call fragments across chunks (arguments stream in
        # pieces; id/name usually arrive with the first fragment).
        for frag in (_pick(delta, "tool_calls") or []):
            index = _pick(frag, "index") or 0
            slot = tool_calls_acc.setdefault(
                index, {"id": None, "name": "", "arguments": ""}
            )
            call_id = _pick(frag, "id")
            if call_id:
                slot["id"] = call_id
            fn = _pick(frag, "function")
            if fn is not None:
                name = _pick(fn, "name")
                if name:
                    slot["name"] += name
                arguments = _pick(fn, "arguments")
                if arguments:
                    slot["arguments"] += arguments

        if reasoning:
            yield {"type": "reasoning", "delta": reasoning}
        content = _pick(delta, "content")
        if content:
            yield {"type": "content", "delta": content}

        finish_reason = _pick(choice, "finish_reason")
        if finish_reason is not None:
            yield from _flush_tool_calls(tool_calls_acc)
            yield {"type": "finish", "reason": finish_reason}

    # Servers that never send a finish_reason still get their calls flushed.
    yield from _flush_tool_calls(tool_calls_acc)


async def _aiter_stream(client: LLMClient, messages, tools):
    """Bridge the blocking stream iterator to async event generation.

    Each ``next()`` on the underlying HTTP stream blocks; running them in
    worker threads keeps the event loop free so SSE events flush promptly.

    When the consumer goes away — e.g. the browser aborts the request and
    Starlette cancels this generator — ``close()`` is invoked to tear down
    the upstream HTTP response, which unblocks the pending read in the
    worker thread so generation actually stops instead of running on until
    the LLM finishes.
    """
    stream_iter, close = await asyncio.to_thread(
        client.stream_chat_completion, messages=messages, tools=tools
    )
    sentinel = object()
    try:
        while True:
            item = await asyncio.to_thread(next, stream_iter, sentinel)
            if item is sentinel:
                break
            yield item
    finally:
        # Don't await here: during cancellation a plain await could be
        # swallowed; firing close() on the executor guarantees the socket
        # gets closed even while this generator unwinds.
        try:
            asyncio.get_running_loop().run_in_executor(None, close)
        except RuntimeError:  # no running loop (sync test contexts)
            close()


def create_search_tool() -> Dict[str, Any]:
    """Create the search_manuals tool definition."""
    return {
        "type": "function",
        "function": {
            "name": "search_manuals",
            # User-editable via Settings > Advanced Settings; falls back to the
            # built-in default when blank (defaults are kept non-empty anyway).
            "description": settings.SEARCH_TOOL_DESCRIPTION
            or DEFAULT_SEARCH_TOOL_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query - what information are you looking for?"
                    },
                    "device_id": {
                        "type": "integer",
                        "description": "Optional: Filter search to a specific device ID. Only provide if the question is about a specific device."
                    }
                },
                "required": ["query"]
            }
        }
    }


def create_calendar_tool() -> Dict[str, Any]:
    """Create the manage_calendar tool definition.

    One tool covers every calendar mutation (create/update/delete/complete)
    plus a list action used to look up event ids — see calendar_tool.py for
    why a single action-based schema works better with small local models.
    """
    return {
        "type": "function",
        "function": {
            "name": "manage_calendar",
            # User-editable via Settings > Advanced Settings; falls back to
            # the built-in default when blank.
            "description": settings.CALENDAR_TOOL_DESCRIPTION
            or DEFAULT_CALENDAR_TOOL_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "delete", "complete", "list"],
                        "description": (
                            "What to do: create a new event, update/delete/"
                            "complete an existing one (needs event_id), or "
                            "list events (to find ids / check the schedule)."
                        ),
                    },
                    "event_id": {
                        "type": "integer",
                        "description": (
                            "The id of the event to update/delete/complete. "
                            "Required for those actions; get it from action='list'."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": "Event title, e.g. 'Replace HVAC filter'. Required for create.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional longer note for the event.",
                    },
                    "device_id": {
                        "type": "integer",
                        "description": (
                            "Optional: link the event to a device. Only use an "
                            "id from the conversation's device list. For update, "
                            "pass -1 to unlink the device."
                        ),
                    },
                    "start_date": {
                        "type": "string",
                        "description": (
                            "First/anchor due date as ISO YYYY-MM-DD (resolve "
                            "relative dates against today). Required for create."
                        ),
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Optional time of day HH:MM (24h), e.g. '08:30'. Empty string clears it on update.",
                    },
                    "recurrence_type": {
                        "type": "string",
                        "enum": ["none", "daily", "weekly", "monthly", "yearly"],
                        "description": "Repeat unit; 'none' = one-time reminder. Default 'none'.",
                    },
                    "interval": {
                        "type": "integer",
                        "description": "Repeat every N units (e.g. monthly + 3 = every 3 months). Default 1.",
                    },
                    "within_days": {
                        "type": "integer",
                        "description": (
                            "For action='list' only: restrict to events due "
                            "within N days (overdue included). Omit for all events."
                        ),
                    },
                },
                "required": ["action"],
            },
        },
    }


async def _execute_calendar_tool(
    tool_call,
    calendar_func,
) -> tuple[Dict[str, Any], bool, str]:
    """Execute a manage_calendar tool call.

    Returns (tool response message, ok, short summary). The summary is a
    one-line outcome ('Created event id=4') surfaced to the UI's trace and
    taken from the report itself so model and user always see the same thing.
    """
    args = calendar_tool.parse_tool_arguments(tool_call)
    tc_id = tool_call.id if hasattr(tool_call, 'id') else tool_call.get('id')
    try:
        report = await calendar_func(args)
        ok = not report.startswith("Error")
        # First line of the report doubles as the UI summary.
        summary = report.splitlines()[0][:120] if report else "Done"
        tool_message = {"role": "tool", "content": report, "tool_call_id": tc_id}
        return tool_message, ok, summary
    except Exception as e:  # noqa: BLE001 - keep the conversation alive
        logger.error(f"Calendar tool call failed: {e}")
        tool_message = {
            "role": "tool",
            "content": f"Error executing calendar action: {str(e)}",
            "tool_call_id": tc_id,
        }
        return tool_message, False, "Calendar action failed"


async def _execute_search_tool(
    tool_call,
    search_func
) -> tuple[Dict[str, Any], Optional[int]]:
    """
    Execute a search_manuals tool call.

    Returns:
        Tuple of (tool response message, result count). The count is None
        when the search itself failed, so callers can tell "no results"
        apart from "search errored".
    """
    try:
        # Parse arguments
        if hasattr(tool_call, 'function'):
            args = json.loads(tool_call.function.arguments)
        else:
            args = json.loads(tool_call['function']['arguments'])

        query = args.get('query', '')
        device_id = args.get('device_id')

        logger.info(f"Executing search tool: '{query}' (device={device_id})")

        # Execute search. The callback may return either a plain result list
        # or a (results, note) tuple — the note carries warnings the model
        # must see (e.g. an invented device id whose filter was dropped).
        results = await search_func(query, device_id)
        note = ""
        if isinstance(results, tuple):
            results, note = results

        # Format results for LLM
        if not results:
            content = (
                f"No manuals found matching '{query}'. Retry with fewer or "
                "different keywords (drop generic words like 'connection', "
                "'port' or 'laptop'; keep the device's model number and one "
                "distinctive spec word), and if you passed a device_id try "
                "again without it. If retries also fail, tell the user the "
                "manuals do not cover this instead of answering from your own "
                "knowledge."
            )
            # A dropped/invalid device filter means nothing was actually
            # searched in that device's manuals — the model must retry
            # without it rather than conclude the manuals are silent.
            if note:
                content = f"{note}\n\n{content}"
        else:
            formatted_results = []
            for i, result in enumerate(results[:8], 1):
                # Snippets wrap hits in <mark> tags for the web UI; here we
                # want plain text. Remove the tags themselves — stripping
                # only the angle brackets would leave stray "mark" words
                # that the model then quotes verbatim ("markUSBmark").
                snippet_clean = re.sub(r"</?mark>", "", result.snippet)
                if result.manual_id == 0:
                    # Hit inside a device's own record (details / custom
                    # attributes), not a PDF page — no file link exists.
                    formatted_results.append(
                        f"Result {i} — Device entry: {result.filename}\n"
                        f'   Entry text: "{snippet_clean[:600]}"'
                    )
                    continue
                # Markdown link opening the exact PDF page in the browser —
                # the same URL scheme the Search tab uses for its results.
                file_url = f"/api/downloads/manuals/{result.manual_id}/file#page={result.page_number}"
                formatted_results.append(
                    f"Result {i} — {result.filename}, Page {result.page_number}\n"
                    f"   Reference link: [Page {result.page_number}]({file_url})\n"
                    f'   Snippet from this page: "{snippet_clean[:600]}"'
                )

            # Same as the no-results case: a dropped bogus device_id means
            # these hits may come from other devices' manuals — say so.
            prefix = f"{note}\n\n" if note else ""
            content = (
                prefix +
                f"Found {len(results)} relevant sections:\n\n" +
                "\n\n".join(formatted_results) +
                "\n\nUse ONLY these snippets to answer. Each snippet is the "
                "only verified evidence for its page: cite a page only for "
                "facts that are actually visible in that page's snippet, and "
                "reuse that page's Reference link verbatim (keep the URL, "
                "manual id and page number exactly as given). A \"Device "
                "entry\" result is the user's own record of that device "
                "(its details and custom attributes): it is a valid source, "
                "but cite it as the device entry, not as a manual page. "
                "Facts not visible in any snippet or entry are NOT covered "
                "by the manuals — say so instead of inventing them or citing "
                "a page for them."
            )

        tool_message = {
            "role": "tool",
            "content": content,
            "tool_call_id": tool_call.id if hasattr(tool_call, 'id') else tool_call.get('id')
        }
        return tool_message, len(results)

    except Exception as e:
        logger.error(f"Tool call failed: {e}")
        tool_message = {
            "role": "tool",
            "content": f"Error executing search: {str(e)}",
            "tool_call_id": tool_call.id if hasattr(tool_call, 'id') else tool_call.get('id')
        }
        return tool_message, None


async def handle_tool_call(
    tool_call,
    search_func
) -> Dict[str, Any]:
    """
    Handle a tool call from the LLM.
    
    Args:
        tool_call: Tool call object from LLM response
        search_func: Async function to execute search
        
    Returns:
        Tool response message
    """
    tool_message, _ = await _execute_search_tool(tool_call, search_func)
    return tool_message


async def chat_with_tool_events(
    client: LLMClient,
    messages: List[Dict[str, str]],
    search_func,
    max_tool_calls: int = 3,
    calendar_func=None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Chat with tool calling, streaming the model's work as typed SSE events.

    Event shapes (JSON-serialisable dicts, consumed by the SSE endpoint):
      - {"type": "thinking_start"}                — a thinking block begins
      - {"type": "thinking_delta", "delta": str}  — streamed reasoning tokens
      - {"type": "thinking_end"}                  — the thinking block closed
      - {"type": "content_delta", "delta": str}   — streamed answer text
      - {"type": "tool_call", "id", "name", "arguments"}     — full call once
        its streamed argument fragments are complete
      - {"type": "tool_result", "id", "ok", "result_count",
         "summary"?}  — execution outcome (summary: calendar calls only)
      - {"type": "message", "content": str, "used_search": bool,
         "search_results_count": int}             — final answer (terminal)
      - {"type": "done"}                          — stream finished

    Thinking events only appear when the server exposes reasoning deltas
    (Ollama / llama.cpp thinking models etc.); other servers just skip them.

    Args:
        client: LLMClient instance
        messages: Conversation messages
        search_func: Async function for searching manuals
        max_tool_calls: Maximum number of tool calls in one conversation
        calendar_func: Optional async callable(args) -> str executing the
            manage_calendar tool; when None the tool is not offered at all.
    """
    tools = [create_search_tool()]
    if calendar_func is not None:
        tools.append(create_calendar_tool())
    current_messages = list(messages)
    tool_call_count = 0
    # Thinking models occasionally end a turn with only reasoning tokens and
    # no answer text; nudge once for an explicit answer before giving up.
    nudged = False

    while True:
        thinking_open = False
        content_parts: List[str] = []
        pending_tool_calls: List[Dict[str, Any]] = []
        turn_finish: Optional[str] = None

        # The underlying SDK stream is blocking; _aiter_stream runs each read
        # in a worker thread so events flush to the client as they arrive.
        async for ev in _aiter_stream(client, current_messages, tools):
            if ev["type"] == "reasoning":
                if not thinking_open:
                    thinking_open = True
                    yield {"type": "thinking_start"}
                yield {"type": "thinking_delta", "delta": ev["delta"]}
            elif ev["type"] == "content":
                content_parts.append(ev["delta"])
                yield {"type": "content_delta", "delta": ev["delta"]}
            elif ev["type"] == "tool_call":
                pending_tool_calls.append(ev["call"])
            elif ev["type"] == "finish":
                turn_finish = ev["reason"]

        if thinking_open:
            yield {"type": "thinking_end"}

        # The LLM wants to call tools: execute them and loop for the answer.
        if pending_tool_calls:
            tool_call_count += len(pending_tool_calls)

            for call in pending_tool_calls:
                tc_id = call["id"]
                tc_name = call["name"] or "search_manuals"
                tc_args = call["arguments"] or "{}"

                # Full arguments are known now, so the UI can show a summary.
                yield {
                    "type": "tool_call",
                    "id": tc_id,
                    "name": tc_name,
                    "arguments": tc_args,
                }

                # _execute_search_tool accepts plain dicts shaped like this.
                call_obj = {"id": tc_id, "function": {"name": tc_name, "arguments": tc_args}}
                result_event: Dict[str, Any]
                if tc_name == "manage_calendar" and calendar_func is not None:
                    tool_response, ok, summary = await _execute_calendar_tool(
                        call_obj, calendar_func
                    )
                    result_event = {
                        "type": "tool_result",
                        "id": tc_id,
                        "name": tc_name,
                        "ok": ok,
                        "result_count": None,
                        "summary": summary,
                    }
                else:
                    tool_response, result_count = await _execute_search_tool(
                        call_obj, search_func
                    )
                    result_event = {
                        "type": "tool_result",
                        "id": tc_id,
                        "name": tc_name,
                        "ok": result_count is not None,
                        "result_count": result_count,
                    }

                yield result_event

                # Echo metadata (strict OpenAI-compatible servers, e.g.
                # llama.cpp, match the tool reply against this call and
                # reject partial tool_call objects).
                current_messages.append({
                    "role": "assistant",
                    # Some OpenAI-compatible servers (llama.cpp) reject a null
                    # content; an empty string is accepted everywhere.
                    "content": "",
                    "tool_calls": [{
                        "id": tc_id,
                        "type": "function",
                        "function": {"name": tc_name, "arguments": tc_args},
                    }],
                })
                current_messages.append(tool_response)

            # Continue conversation with tool results
            if tool_call_count >= max_tool_calls:
                # Max tool calls reached without a final answer.
                yield {
                    "type": "message",
                    "content": "I've searched the manuals multiple times but couldn't find a clear answer. Could you rephrase your question?",
                    "used_search": True,
                    "search_results_count": max_tool_calls,
                }
                yield {"type": "done"}
                return
            continue

        final_content = "".join(content_parts)
        if not final_content.strip():
            logger.warning(
                f"LLM turn produced no answer text "
                f"(finish={turn_finish}, tool_calls={tool_call_count})"
            )
            if not nudged:
                # No content_delta events were emitted (content was empty),
                # so asking again cannot duplicate streamed text in the UI.
                nudged = True
                current_messages.append({
                    "role": "user",
                    "content": (
                        "Your previous reply contained no visible answer. "
                        "Answer the user's question now, following the "
                        "system rules (search the manuals first if you have "
                        "not already)."
                    ),
                })
                continue
            final_content = "I'm sorry, I couldn't process that request."

        # Final response (authoritative content; UI replaces streamed text).
        yield {
            "type": "message",
            "content": final_content,
            "used_search": tool_call_count > 0,
            "search_results_count": tool_call_count,
        }
        yield {"type": "done"}
        return


async def chat_with_tool_support(
    client: LLMClient,
    messages: List[Dict[str, str]],
    search_func,
    max_tool_calls: int = 3,
    calendar_func=None,
) -> tuple[str, bool, int]:
    """
    Chat with automatic tool calling support.

    Thin wrapper around chat_with_tool_events for non-streaming callers:
    drains the event stream and returns only the final answer.

    Args:
        client: LLMClient instance
        messages: Conversation messages
        search_func: Async function for searching manuals
        max_tool_calls: Maximum number of tool calls in one conversation
        
    Returns:
        Tuple of (response_text, used_search, search_results_count)
    """
    content = "I'm sorry, I couldn't process that request."
    used_search = False
    result_count = 0

    async for event in chat_with_tool_events(
        client, messages, search_func, max_tool_calls, calendar_func=calendar_func
    ):
        if event.get("type") == "message":
            content = event["content"]
            used_search = event.get("used_search", False)
            result_count = event.get("search_results_count", 0)

    return content, used_search, result_count
