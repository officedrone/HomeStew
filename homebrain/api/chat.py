"""Chat API endpoints with LLM integration."""
import json
import logging
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from typing import Any, AsyncGenerator, Dict, Optional

from homebrain.config import settings
from homebrain.db import get_db_context
from homebrain.default_prompts import DEFAULT_CHAT_SYSTEM_PROMPT
from homebrain.models.schemas import ChatRequest, ChatMessage, ChatResponse
from homebrain.services.llm_client import (
    LLMClient,
    chat_with_tool_support,
    chat_with_tool_events,
)
from homebrain.services.search_engine import search_manuals

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
            "SELECT id, name, brand, model FROM devices WHERE id = ?",
            (device_id,),
        )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device {device_id} not found",
        )
    return dict(row)


def device_filter_note(device: Dict[str, Any]) -> str:
    """System-prompt addition telling the LLM which device is in scope."""
    return (
        f"\n\nThe user has filtered this conversation to one specific device: "
        f"{device['name']} ({device['brand']} {device['model']}), "
        f"device_id={device['id']}. Treat every question as being about this "
        f"device and search only its manuals."
    )


def scoped_search_func(device_id: Optional[int]):
    """Search callback with the chat device filter applied.

    When a device is selected, it overrides whatever device_id the LLM puts
    into a tool call so results can never leak from other devices' manuals.
    """

    async def _search(query: str, device_id_arg: Optional[int] = None):
        return await search_manuals(query, device_id or device_id_arg, limit=10)

    return _search


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

        if messages[0]['role'] != 'system':
            messages.insert(0, {"role": "system", "content": system_prompt})

        # Chat with tool support
        response_text, used_search, search_count = await chat_with_tool_support(
            client=client,
            messages=messages,
            search_func=scoped_search_func(request.device_id),
            max_tool_calls=3
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

            if messages[0]['role'] != 'system':
                messages.insert(0, {"role": "system", "content": system_prompt})

            async for event in chat_with_tool_events(
                client=client,
                messages=messages,
                search_func=scoped_search_func(request.device_id),
                max_tool_calls=3
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
