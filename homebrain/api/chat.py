"""Chat API endpoints with LLM integration."""
import logging
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from typing import List, AsyncGenerator

from homebrain.config import settings
from homebrain.models.schemas import ChatRequest, ChatMessage, ChatResponse
from homebrain.services.llm_client import LLMClient, chat_with_tool_support
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
        
        # Add system prompt if needed
        system_prompt = (
            "You are HomeBrain, a helpful assistant that can search through device manuals. "
            "When users ask about devices, setup, troubleshooting, or specifications, use the search_manuals tool to find relevant information from their manuals. "
            "Always provide clear, concise answers based on the manual content."
        )
        
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
    """Stream chat response with tool calling support."""
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
            
            # Add system prompt
            system_prompt = (
                "You are HomeBrain, a helpful assistant that can search through device manuals. "
                "When users ask about devices, setup, troubleshooting, or specifications, use the search_manuals tool to find relevant information from their manuals."
            )
            
            if messages[0]['role'] != 'system':
                messages.insert(0, {"role": "system", "content": system_prompt})
            
            # For streaming with tools, we'll do non-streaming tool calls
            # then stream the final response
            from homebrain.services.llm_client import create_search_tool
            
            tools = [create_search_tool()]
            current_messages = messages.copy()
            
            # Execute tool calls if needed (non-streaming)
            max_calls = 3
            for _ in range(max_calls):
                response = client.chat_completion(
                    messages=current_messages,
                    tools=tools,
                    stream=False
                )
                
                choice = response.choices[0]
                message = choice.message
                
                if hasattr(message, 'tool_calls') and message.tool_calls:
                    # Handle tool calls (simplified)
                    from homebrain.services.llm_client import handle_tool_call
                    
                    for tool_call in message.tool_calls:
                        tool_response = await handle_tool_call(tool_call, search_manuals)
                        
                        current_messages.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{"id": tool_call.id}]
                        })
                        current_messages.append(tool_response)
                    
                    continue
                
                # Got final response, stream it
                if hasattr(message, 'content') and message.content:
                    # Stream the response character by character for effect
                    for i in range(0, len(message.content), 50):
                        chunk = message.content[i:i+50]
                        yield f"data: {chunk}\n\n"
                    
                    break
            
            yield "data: [DONE]\n\n"
            
        except Exception as e:
            logger.error(f"Streaming chat failed: {e}")
            yield f'data: Error: {str(e)}\n\n'
    
    return StreamingResponse(
        generate_response(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive"
        }
    )
