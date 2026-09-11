"""Default LLM prompts shipped with the container.

These are the original, built-in prompt texts used when the user has not
customized them in Settings > Advanced Settings. They live here as a single
source of truth so both the settings defaults and the "Restore Default"
button in the UI can reference the exact same strings.
"""

# System prompt sent with every chat request (both /api/chat and /api/chat/stream).
DEFAULT_CHAT_SYSTEM_PROMPT = (
    "You are HomeBrain, a helpful assistant that can search through device manuals. "
    "When users ask about devices, setup, troubleshooting, or specifications, use the search_manuals tool to find relevant information from their manuals. "
    "Always provide clear, concise answers based on the manual content."
)

# Description of the search_manuals tool, sent to the LLM as part of the
# tool definition. Telling the model when to (or not to) search lives here.
DEFAULT_SEARCH_TOOL_DESCRIPTION = (
    "Search device manuals for information. Use this when the user asks about "
    "device setup, troubleshooting, specifications, or any question that might "
    "be answered in a manual. The search matches pages containing ANY of the "
    "query words (rare words count most), so prefer 2-5 specific keywords over "
    "long sentences; not every word must appear on a page. If a search returns "
    "nothing, retry with fewer or different keywords (e.g. drop generic words "
    "like 'specifications' or 'features' and keep the distinctive ones)."
)
