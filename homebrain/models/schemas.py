"""Pydantic models for API requests and responses."""
from pydantic import BaseModel, Field
from datetime import date, datetime, time
from typing import Literal, Optional


class DeviceBase(BaseModel):
    """Base device model."""
    name: str = Field(..., min_length=1, max_length=200)
    brand: str = Field(..., min_length=1, max_length=100)
    model: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=1000)
    serial_number: Optional[str] = Field(None, max_length=100)
    product_number: Optional[str] = Field(None, max_length=100)
    purchase_date: Optional[date] = Field(None, description="Date the device was purchased")
    warranty_length: Optional[int] = Field(None, ge=0, description="Warranty length value (with unit)")
    warranty_unit: Optional[str] = Field(None, pattern="^(days|months|years)$", description="Warranty length unit")
    warranty_end: Optional[date] = Field(
        None,
        description="Warranty expiry; auto-computed from purchase_date + length when omitted",
    )


class DeviceCreate(DeviceBase):
    """Model for creating a new device."""
    pass


class Device(DeviceBase):
    """Full device model with database fields.

    ``created_at`` / ``updated_at`` are system-managed audit timestamps and
    are intentionally absent from ``DeviceCreate`` so clients cannot set them.
    """
    id: int
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class DeviceAttribute(BaseModel):
    """Device attribute model for custom user-defined fields."""
    id: int
    device_id: int
    attribute_name: str
    attribute_value: str
    created_at: datetime
    
    class Config:
        from_attributes = True


class DeviceResponse(BaseModel):
    """Device response with additional metadata."""
    id: int
    name: str
    brand: str
    model: str
    description: Optional[str]
    serial_number: Optional[str]
    product_number: Optional[str]
    purchase_date: Optional[date] = None
    warranty_length: Optional[int] = None
    warranty_unit: Optional[str] = None
    warranty_end: Optional[date] = None
    created_at: datetime = Field(..., description="When the device was added (read-only)")
    updated_at: datetime = Field(..., description="When the device was last modified (read-only)")
    manual_count: int = 0
    attributes: list[DeviceAttribute] = []
    
    class Config:
        from_attributes = True


class ManualBase(BaseModel):
    """Base manual model."""
    filename: str
    filepath: str
    page_count: Optional[int] = None


class Manual(ManualBase):
    """Full manual model with database fields."""
    id: int
    device_id: int
    indexed_at: datetime
    
    class Config:
        from_attributes = True


class SearchRequest(BaseModel):
    """Search request model."""
    query: str = Field(..., min_length=1)
    device_id: Optional[int] = Field(None, description="Optional device filter")
    limit: int = Field(10, ge=1, le=50)


class SearchResult(BaseModel):
    """Individual search result."""
    manual_id: int
    device_id: int
    filename: str
    page_number: int
    snippet: str
    score: float


class SearchResponse(BaseModel):
    """Search response with results."""
    query: str
    total_results: int
    results: list[SearchResult]


class DownloadTriggerRequest(BaseModel):
    """Request model for triggering manual download."""
    device_id: int = Field(..., description="Device ID to download manuals for")


class ChatMessage(BaseModel):
    """Chat message model."""
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str = Field(..., min_length=1)


class ChatRequest(BaseModel):
    """Chat request model."""
    messages: list[ChatMessage]
    device_id: Optional[int] = Field(None, description="Optional device context")
    stream: bool = False


class ChatResponse(BaseModel):
    """Chat response model."""
    message: str
    used_search: bool = False
    search_results_count: int = 0


class DownloadRequest(BaseModel):
    """Manual download request."""
    device_id: int


class DownloadStatus(BaseModel):
    """Download status response."""
    success: bool
    message: str
    downloaded_count: int = 0
    error_detail: str | None = None


class SettingsResponse(BaseModel):
    """Current application settings (secrets are never included)."""
    theme: str = Field(..., description="UI theme preference: auto | light | dark")
    llm_base_url: str
    llm_model: str
    llm_api_key_set: bool = Field(
        ..., description="True if an LLM API key is configured (value not exposed)"
    )
    chat_system_prompt: str = Field(
        ..., description="System prompt sent with every chat request"
    )
    search_tool_description: str = Field(
        ..., description="Description of the search_manuals tool shown to the LLM"
    )
    calendar_tool_description: str = Field(
        ..., description="Description of the manage_calendar tool shown to the LLM"
    )
    chat_system_prompt_default: str = Field(
        ..., description="Built-in default for chat_system_prompt (Restore Default)"
    )
    search_tool_description_default: str = Field(
        ..., description="Built-in default for search_tool_description (Restore Default)"
    )
    calendar_tool_description_default: str = Field(
        ..., description="Built-in default for calendar_tool_description (Restore Default)"
    )


class SettingsUpdate(BaseModel):
    """Update application settings. Omitted/blank fields are left unchanged."""
    theme: Optional[Literal["auto", "light", "dark"]] = Field(
        None, description="UI theme preference: auto follows the OS; light/dark pin it"
    )
    llm_base_url: Optional[str] = Field(None, max_length=500)
    llm_api_key: Optional[str] = Field(None, max_length=500)
    llm_model: Optional[str] = Field(None, min_length=1, max_length=200)
    chat_system_prompt: Optional[str] = Field(
        None, max_length=8000, description="Custom chat system prompt"
    )
    search_tool_description: Optional[str] = Field(
        None, max_length=4000, description="Custom search_manuals tool description"
    )
    calendar_tool_description: Optional[str] = Field(
        None, max_length=4000, description="Custom manage_calendar tool description"
    )


class ModelListRequest(BaseModel):
    """Optionally override base URL / API key when probing the LLM server.

    Blank/omitted values fall back to the currently saved settings.
    """
    llm_base_url: Optional[str] = Field(None, max_length=500)
    llm_api_key: Optional[str] = Field(None, max_length=500)


class ModelListResponse(BaseModel):
    """Model ids reported by the LLM server's /models endpoint."""
    models: list[str]


class ModelStatusResponse(BaseModel):
    """Result of probing the saved LLM settings for chat readiness.

    ``reachable`` is False when the server's model list could not be fetched;
    in that case ``error`` explains why and ``available_models_count`` is None.
    When reachable, ``model_available`` says whether the currently selected
    model appears in the server's list.
    """
    reachable: bool
    model_configured: bool = Field(
        ..., description="True if an LLM model name is configured in settings"
    )
    model_available: Optional[bool] = None
    llm_base_url: str
    llm_model: str
    available_models_count: Optional[int] = None
    available_models: Optional[list[str]] = Field(
        None,
        description="Model ids offered by the server (None when unreachable)",
    )
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Calendar events (device maintenance reminders)
# ---------------------------------------------------------------------------

class CalendarEventBase(BaseModel):
    """Base calendar event model."""
    title: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    device_id: Optional[int] = Field(
        None, description="Device this event is tied to (optional)"
    )
    start_date: date = Field(..., description="Anchor / first due date")
    start_time: Optional[time] = Field(None, description="Optional time of day")
    recurrence_type: str = Field(
        "none", pattern="^(none|daily|weekly|monthly|yearly)$",
        description="Repeat interval unit; 'none' = one-time event",
    )
    interval: int = Field(1, ge=1, le=3650, description="Every N units")


class CalendarEventCreate(CalendarEventBase):
    """Model for creating a calendar event."""
    pass


class CalendarEventUpdate(BaseModel):
    """Partial update — only provided fields change."""
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    device_id: Optional[int] = None
    start_date: Optional[date] = None
    start_time: Optional[time] = None
    recurrence_type: Optional[str] = Field(
        None, pattern="^(none|daily|weekly|monthly|yearly)$"
    )
    interval: Optional[int] = Field(None, ge=1, le=3650)


class CalendarEventResponse(BaseModel):
    """Calendar event with computed schedule fields."""
    id: int
    device_id: Optional[int]
    device_name: Optional[str] = None
    title: str
    description: Optional[str]
    start_date: date
    start_time: Optional[time]
    recurrence_type: str
    interval: int
    recurrence_label: str
    last_completed_at: Optional[datetime]
    created_at: datetime
    next_due_date: Optional[date] = Field(
        None, description="Computed next occurrence; None when a one-time event is done/past"
    )
    status: str = Field(..., pattern="^(overdue|today|upcoming|done)$")

    class Config:
        from_attributes = True


class UpcomingEventsResponse(BaseModel):
    """Events due within a day-window, for the sidebar notification area."""
    days: int
    events: list[CalendarEventResponse]
