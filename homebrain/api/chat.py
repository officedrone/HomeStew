"""Chat API endpoints with LLM integration."""
import json
import logging
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from typing import List, AsyncGenerator

from homebrain.config import settings
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


@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Send a chat message and get LLM response with manual search capability."""
    if not request.messages or not request.messages[-1].content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message content cannot be empty"
        )
    
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

        if messages[0]['role'] != 'system':
            messages.insert(0, {"role": "system", "content": system_prompt})
        
        # Chat with tool support
        response_text, used_search, search_count = await chat_with_tool_support(
            client=client,
            messages=messages,
            search_func=lambda q, d=None: search_manuals(q, d, limit=10),
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
    """Stream typed SSE status events (thinking / tool calls) plus the final answer.

    Event frames are JSON objects: {"type":"status",...}, {"type":"message",...},
    {"type":"done"} or {"type":"error",...}.
    """
    if not request.messages or not request.messages[-1].content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message content cannot be empty"
        )
    
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

            if messages[0]['role'] != 'system':
                messages.insert(0, {"role": "system", "content": system_prompt})
            
            async for event in chat_with_tool_events(
                client=client,
                messages=messages,
                search_func=lambda q, d=None: search_manuals(q, d, limit=10),
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
