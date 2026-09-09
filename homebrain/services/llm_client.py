"""LLM client supporting OpenAI-compatible APIs with tool calling."""
import logging
from typing import Optional, List, Dict, Any, AsyncGenerator
import json

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


def create_search_tool() -> Dict[str, Any]:
    """Create the search_manuals tool definition."""
    return {
        "type": "function",
        "function": {
            "name": "search_manuals",
            "description": "Search device manuals for information. Use this when the user asks about device setup, troubleshooting, specifications, or any question that might be answered in a manual.",
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
    try:
        # Parse arguments
        if hasattr(tool_call, 'function'):
            args = json.loads(tool_call.function.arguments)
        else:
            args = json.loads(tool_call['function']['arguments'])
        
        query = args.get('query', '')
        device_id = args.get('device_id')
        
        logger.info(f"Executing search tool: '{query}' (device={device_id})")
        
        # Execute search
        results = await search_func(query, device_id)
        
        # Format results for LLM
        if not results:
            content = f"No manuals found matching '{query}'. Try different keywords."
        else:
            formatted_results = []
            for i, result in enumerate(results[:5], 1):
                snippet_clean = ''.join(c for c in result.snippet if c not in '<>')
                formatted_results.append(
                    f"{i}. [{result.filename}, Page {result.page_number}]\n"
                    f"   {snippet_clean[:150]}..."
                )
            
            content = (
                f"Found {len(results)} relevant sections:\n\n" + 
                "\n\n".join(formatted_results) +
                "\n\nUse this information to answer the user's question."
            )
        
        return {
            "role": "tool",
            "content": content,
            "tool_call_id": tool_call.id if hasattr(tool_call, 'id') else tool_call.get('id')
        }
        
    except Exception as e:
        logger.error(f"Tool call failed: {e}")
        return {
            "role": "tool",
            "content": f"Error executing search: {str(e)}",
            "tool_call_id": tool_call.id if hasattr(tool_call, 'id') else tool_call.get('id')
        }


async def chat_with_tool_support(
    client: LLMClient,
    messages: List[Dict[str, str]],
    search_func,
    max_tool_calls: int = 3
) -> tuple[str, bool, int]:
    """
    Chat with automatic tool calling support.
    
    Args:
        client: LLMClient instance
        messages: Conversation messages
        search_func: Async function for searching manuals
        max_tool_calls: Maximum number of tool calls in one conversation
        
    Returns:
        Tuple of (response_text, used_search, search_results_count)
    """
    tools = [create_search_tool()]
    current_messages = messages.copy()
    tool_call_count = 0
    
    while tool_call_count < max_tool_calls:
        # Get response from LLM
        response = client.chat_completion(
            messages=current_messages,
            tools=tools,
            stream=False
        )
        
        choice = response.choices[0]
        message = choice.message
        
        # Check if LLM wants to call a tool
        if hasattr(message, 'tool_calls') and message.tool_calls:
            tool_call_count += 1
            
            # Handle each tool call
            for tool_call in message.tool_calls:
                tool_response = await handle_tool_call(tool_call, search_func)
                current_messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": tool_call.id, "function": {"name": "search_manuals"}}]
                })
                current_messages.append(tool_response)
            
            # Continue conversation with tool results
            continue
        
        elif hasattr(message, 'content') and message.content:
            # Final response
            return message.content, tool_call_count > 0, tool_call_count
        
        else:
            logger.warning("Unexpected LLM response format")
            return "I'm sorry, I couldn't process that request.", False, 0
    
    # Max tool calls reached without final answer
    return "I've searched the manuals multiple times but couldn't find a clear answer. Could you rephrase your question?", True, max_tool_calls
