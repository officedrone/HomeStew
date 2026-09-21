"""Chat API endpoints with LLM integration."""
import json
import logging
from datetime import date as _date

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from homestew.config import settings
from homestew.db import get_db_context
from homestew.default_prompts import DEFAULT_CHAT_SYSTEM_PROMPT
from homestew.models.schemas import ChatRequest, ChatMessage, ChatResponse
from homestew.services.llm_client import (
    LLMClient,
    ToolBinding,
    build_calendar_binding,
    build_device_binding,
    build_search_binding,
    chat_with_tool_support,
    chat_with_tool_events,
)
from homestew.services.search_engine import search_manuals
from homestew.services import calendar_service, calendar_tool, device_tool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

# Global LLM client (initialized on first request)
_llm_client: LLMClient = None


def get_llm_client() -> LLMClient:
    """Get or create LLM client."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient(
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
            model=settings.LLM_MODEL
        )
    return _llm_client


def reset_llm_client() -> None:
    """Drop the cached client so updated settings take effect on next request."""
    global _llm_client
    _llm_client = None


async def resolve_device_filter(device_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """Validate the optional chat device filter and return the device row.

    Returns None when no filter is set; raises 404 for an unknown id so the
    UI gets a clear error instead of silently searching every manual.
    """
    if device_id is None:
        return None

    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT id, name, brand, model, description, serial_number, "
            "product_number, purchase_date, warranty_length, warranty_unit, "
            "warranty_end FROM devices WHERE id = ?",
            (device_id,),
        )
        row = await cursor.fetchone()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {device_id} not found",
            )
        device = dict(row)

        # Custom user-defined fields are part of the device entry too, so the
        # model can answer from them without inventing anything.
        attr_cursor = await db.execute(
            "SELECT attribute_name, attribute_value FROM device_attributes "
            "WHERE device_id = ?",
            (device_id,),
        )
        device["attributes"] = [dict(a) for a in await attr_cursor.fetchall()]
    return device


async def list_all_device_ids() -> List[int]:
    """Every registered device id - used to validate LLM-supplied filters."""
    async with get_db_context() as db:
        cursor = await db.execute("SELECT id FROM devices")
        return [row["id"] for row in await cursor.fetchall()]


async def device_roster_note() -> str:
    """System-prompt section listing every device and its real id.

    Without this the model invents plausible-looking device ids (it called
    search with device_id=202 for a laptop whose real id is 1), and every
    search scoped to a nonexistent id returns nothing - which the model then
    (correctly!) reports as "the manuals do not cover this".
    """
    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT id, name, brand, model, purchase_date, warranty_length, "
            "warranty_unit, warranty_end FROM devices ORDER BY id"
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        # Custom attributes belong to the device entry just as much as the
        # columns do - without them here, an unscoped chat cannot see e.g. a
        # warranty status the user typed into the device form.
        attr_cursor = await db.execute(
            "SELECT device_id, attribute_name, attribute_value "
            "FROM device_attributes ORDER BY id"
        )
        attrs: Dict[int, List[str]] = {}
        for a in await attr_cursor.fetchall():
            attrs.setdefault(a["device_id"], []).append(
                f"{a['attribute_name']}: {a['attribute_value']}"
            )

    if not rows:
        return ""

    lines = []
    for r in rows:
        parts = [f"- device_id={r['id']}: {r['name']} ({r['brand']} {r['model']})"]
        warranty = _warranty_phrase(r)
        if warranty:
            parts.append(warranty)
        if attrs.get(r["id"]):
            parts.append("attributes: " + "; ".join(attrs[r["id"]]))
        lines.append(" | ".join(parts))
    return (
        "\n\nThe user's registered devices (these are the ONLY valid "
        "device_id values - never invent or guess an id). The warranty and "
        "attributes shown here are facts the user recorded themselves: they "
        "are authoritative and answer questions like 'is my X still under "
        "warranty' directly - no manual search needed (a manual can never "
        "know when this specific unit was bought).\n"
        "When a question refers to a device by nickname or description "
        "('my work laptop') rather than an exact name/brand/model, you MUST "
        "call manage_devices with action='search_devices' and the user's own "
        "words as query BEFORE answering - never pick a device from this "
        "list yourself and never ask the user which device they mean unless "
        "that tool reports AMBIGUOUS:\n" + "\n".join(lines)
    )


def current_date_note() -> str:
    """System-prompt line with today's date and weekday.

    The manage_calendar tool needs absolute ISO dates, but users speak in
    relative ones ("tomorrow", "next Friday"). Without the real date in the
    prompt the model cannot resolve them - it either asks the user or invents
    a wrong anchor. The weekday is included so "next Friday" resolves without
    arithmetic.
    """
    today = _date.today()
    return (
        f"\n\nToday is {today.isoformat()} ({today.strftime('%A')}). Use this "
        "to resolve relative dates like 'tomorrow' or 'next Friday' into the "
        "ISO YYYY-MM-DD values calendar tool calls require."
    )


async def calendar_reminders_note(
    device_id: Optional[int], days: int = 7
) -> str:
    """System-prompt section with maintenance events due within the next days.

    When the chat is scoped to a device only that device's (and unassigned)
    events are listed; otherwise every event in the window is included so the
    model can proactively nudge about upcoming upkeep. The data is computed
    server-side and injected directly - small local models reliably mention it
    without needing an extra tool call.
    """
    # Device-scoped chats still see unassigned events (they may concern the
    # device even though nobody linked them), merged with that device's own.
    window = await calendar_service.list_events(within_days=days)
    if device_id is not None:
        events = [e for e in window if e["device_id"] in (None, device_id)]
    else:
        events = window
    if not events:
        return ""

    lines = []
    for e in events:
        when = e["next_due_date"].isoformat()
        status_word = {"overdue": "OVERDUE", "today": "due TODAY"}.get(
            e["status"], f"due {when}"
        )
        device_part = (
            f" (device_id={e['device_id']}: {e['device_name']})"
            if e.get("device_id")
            else " (no device)"
        )
        lines.append(
            f'- id={e["id"]}: "{e["title"]}" - {status_word}, repeats: '
            f"{e['recurrence_label']}{device_part}"
        )

    return (
        "\n\nThe user's maintenance calendar (events due within the next "
        f"{days} days, including overdue ones):\n"
        + "\n".join(lines)
        + "\nThe id= values are for the manage_calendar tool: use them to "
        "update, delete or complete these events without listing again. If a "
        "question concerns a device with one of these events - or the topic "
        "matches one (filters, cleaning, maintenance) - briefly remind the "
        "user about it after answering. Never invent calendar events that "
        "are not listed here."
    )


def _warranty_phrase(device: Dict[str, Any]) -> str:
    """One-line warranty summary with ACTIVE/EXPIRED status for the prompt.

    The status is derived server-side against today's date - small models
    reliably report 'expired' when told so explicitly, but misjudge a bare
    ISO date. Returns '' when nothing warranty-related is recorded.
    """
    parts = []
    if device.get("purchase_date"):
        parts.append(f"purchased {device['purchase_date']}")
    if device.get("warranty_length") is not None and device.get("warranty_unit"):
        parts.append(
            f"{device['warranty_length']} {device['warranty_unit']} warranty"
        )
    if device.get("warranty_end"):
        end_raw = str(device["warranty_end"])
        try:
            status = (
                "ACTIVE"
                if _date.fromisoformat(end_raw[:10]) >= _date.today()
                else "EXPIRED"
            )
        except ValueError:
            status = ""
        parts.append(f"warranty ends {end_raw} ({status})")
    return ("Warranty: " + ", ".join(parts)) if parts else ""


def device_filter_note(device: Dict[str, Any]) -> str:
    """System-prompt addition with the full device entry currently in scope.

    Everything the user typed into the device form is included so the model
    can legitimately answer from it; anything not listed here still has to
    come from a manual search result.
    """
    lines = [
        f"- Name: {device['name']}",
        f"- Brand: {device['brand']}",
        f"- Model: {device['model']}",
    ]
    if device.get("description"):
        lines.append(f"- Description: {device['description']}")
    if device.get("serial_number"):
        lines.append(f"- Serial number: {device['serial_number']}")
    if device.get("product_number"):
        lines.append(f"- Product number: {device['product_number']}")
    warranty = _warranty_phrase(device)
    if warranty:
        lines.append(f"- {warranty}")
    for attr in device.get("attributes", []):
        lines.append(f"- {attr['attribute_name']}: {attr['attribute_value']}")

    return (
        "\n\nThe user has filtered this conversation to one specific device, "
        f"device_id={device['id']}. Treat every question as being about this "
        "device and search only its manuals.\n"
        "Device entry - the custom attributes and warranty fields are facts "
        "the user recorded themselves and are just as important as the "
        "manuals. Check them FIRST: if one of them answers the question "
        "(warranty status, purchase date, serial number, a spec the user "
        "typed in), answer from it directly without searching - manuals can "
        "never know when this specific unit was bought or what the user "
        "noted about it. Facts here may be used directly; anything not "
        "listed here still requires a manual search result:\n"
        + "\n".join(lines)
    )


def scoped_search_func(
    device_id: Optional[int],
    valid_ids: List[int],
):
    """Search callback with the chat device filter applied.

    When a device is selected, it overrides whatever device_id the LLM puts
    into a tool call so results can never leak from other devices' manuals.

    A device id coming from the LLM that does not exist (small local models
    happily invent them) would silently scope the search to zero documents;
    instead of returning an empty result set - which the model reads as "the
    manuals say nothing" - the bogus filter is dropped and the note explains
    what happened so the model can retry with a real id.

    Returns either a plain result list or a (results, note) tuple.
    """

    async def _search(
        query: str, device_id_arg: Optional[int] = None
    ) -> Tuple[List[Any], str]:
        note = ""
        if device_id is None and device_id_arg is not None:
            try:
                device_id_arg = int(device_id_arg)
            except (TypeError, ValueError):
                device_id_arg = None
            if device_id_arg is not None and device_id_arg not in valid_ids:
                note = (
                    f"device_id={device_id_arg} does not match any registered "
                    "device, so the filter was dropped and this search covered "
                    "ALL devices. Use one of the real device ids from the "
                    "system prompt (or none) on retry."
                )
                device_id_arg = None

        results = await search_manuals(query, device_id or device_id_arg, limit=10)
        return results, note

    return _search


def scoped_calendar_func(device_id: Optional[int]):
    """Calendar-tool callback with the chat's device filter applied.

    The same rule as search: in a device-filtered conversation the model can
    only see (and touch) that device's events plus unassigned ones - the
    filter overrides any device_id from the tool call and _load_scoped_event
    rejects foreign event ids. See calendar_tool for the action semantics.
    """

    async def _calendar(args: Dict[str, Any]) -> str:
        return await calendar_tool.execute_calendar_tool(args, device_id)

    return _calendar


def scoped_device_func(device_id: Optional[int]):
    """Device-tool callback with the chat's device filter applied.

    Same scope rule as the other tools: in a device-filtered conversation
    only that device may be updated or annotated (creating stays allowed -
    it touches no existing record). See device_tool for the action
    semantics, incl. why deletion is refused with an editor link instead.
    """

    async def _device(args: Dict[str, Any]) -> str:
        return await device_tool.execute_device_tool(args, device_id)

    return _device


def build_tool_bindings(
    device_id: Optional[int], valid_ids: List[int]
) -> List[ToolBinding]:
    """Assemble the tools offered to the LLM for one conversation.

    This is THE registration point: adding a tool means appending one
    binding here, removing it means deleting that line - nothing else in
    the chat pipeline knows individual tool names. Every callback closes
    over the request's device filter so scope can never be widened by a
    tool argument.
    """
    return [
        build_search_binding(scoped_search_func(device_id, valid_ids)),
        build_calendar_binding(scoped_calendar_func(device_id)),
        build_device_binding(scoped_device_func(device_id)),
    ]


@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Send a chat message and get LLM response with manual search capability."""
    if not request.messages or not request.messages[-1].content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message content cannot be empty"
        )
    
    # Resolve before the try so a 404 for an unknown device isn't swallowed
    # into a generic 500.
    device = await resolve_device_filter(request.device_id)
    valid_ids = await list_all_device_ids()

    try:
        client = get_llm_client()

        # Convert to OpenAI message format
        messages = [
            {"role": msg.role, "content": msg.content}
            for msg in request.messages
        ]

        # Add system prompt if needed (user-editable via Settings > Advanced
        # Settings; a blank value falls back to the built-in default).
        system_prompt = settings.CHAT_SYSTEM_PROMPT.strip() or DEFAULT_CHAT_SYSTEM_PROMPT
        if device:
            system_prompt += device_filter_note(device)
        else:
            # No filter: tell the model which device ids actually exist so it
            # can scope per-device searches instead of inventing ids.
            system_prompt += await device_roster_note()
        # Today's date for relative-date resolution in calendar calls.
        system_prompt += current_date_note()
        # Upcoming maintenance nudges (see calendar_reminders_note).
        system_prompt += await calendar_reminders_note(request.device_id)

        if messages[0]['role'] != 'system':
            messages.insert(0, {"role": "system", "content": system_prompt})

        # Chat with tool support (tools assembled in build_tool_bindings).
        response_text, used_search, search_count = await chat_with_tool_support(
            client=client,
            messages=messages,
            bindings=build_tool_bindings(request.device_id, valid_ids),
            max_tool_calls=8
        )
        
        return ChatResponse(
            message=response_text,
            used_search=used_search,
            search_results_count=search_count
        )
        
    except Exception as e:
        logger.error(f"Chat failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Chat processing failed: {str(e)}"
        )


@router.post("/stream")
async def chat_stream(request: ChatRequest):
    """Stream typed SSE events (thinking deltas / tool calls) plus the answer.

    Event frames are JSON objects: {"type":"thinking_start"|"thinking_delta"|
    "thinking_end"|"content_delta"|"tool_call"|"tool_result",...},
    {"type":"message",...}, {"type":"done"} or {"type":"error",...}.
    """
    if not request.messages or not request.messages[-1].content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message content cannot be empty"
        )

    # Resolve before streaming starts so an unknown device is a plain 404.
    device = await resolve_device_filter(request.device_id)
    valid_ids = await list_all_device_ids()

    async def generate_response() -> AsyncGenerator[str, None]:
        try:
            client = get_llm_client()

            messages = [
                {"role": msg.role, "content": msg.content}
                for msg in request.messages
            ]

            # Add system prompt (user-editable via Settings > Advanced
            # Settings; a blank value falls back to the built-in default).
            system_prompt = settings.CHAT_SYSTEM_PROMPT.strip() or DEFAULT_CHAT_SYSTEM_PROMPT
            if device:
                system_prompt += device_filter_note(device)
            else:
                # No filter: give the model the real device ids (see above).
                system_prompt += await device_roster_note()
            # Today's date for relative-date resolution in calendar calls.
            system_prompt += current_date_note()
            # Upcoming maintenance nudges (see calendar_reminders_note).
            system_prompt += await calendar_reminders_note(request.device_id)

            if messages[0]['role'] != 'system':
                messages.insert(0, {"role": "system", "content": system_prompt})

            async for event in chat_with_tool_events(
                client=client,
                messages=messages,
                bindings=build_tool_bindings(request.device_id, valid_ids),
                max_tool_calls=8
            ):
                # json.dumps keeps each frame on a single line, as SSE requires.
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            
        except Exception as e:
            # Headers are already sent, so report the failure as an event.
            logger.error(f"Streaming chat failed: {e}")
            error_event = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"
    
    return StreamingResponse(
        generate_response(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )
