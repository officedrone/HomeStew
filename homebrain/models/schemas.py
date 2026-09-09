"""Pydantic models for API requests and responses."""
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


class DeviceBase(BaseModel):
    """Base device model."""
    name: str = Field(..., min_length=1, max_length=200)
    brand: str = Field(..., min_length=1, max_length=100)
    model: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=1000)
    serial_number: Optional[str] = Field(None, max_length=100)
    product_number: Optional[str] = Field(None, max_length=100)


class DeviceCreate(DeviceBase):
    """Model for creating a new device."""
    pass


class Device(DeviceBase):
    """Full device model with database fields."""
    id: int
    created_at: datetime
    
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
    created_at: datetime
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
    llm_base_url: str
    llm_model: str
    llm_api_key_set: bool = Field(
        ..., description="True if an LLM API key is configured (value not exposed)"
    )


class SettingsUpdate(BaseModel):
    """Update application settings. Omitted/blank fields are left unchanged."""
    llm_base_url: Optional[str] = Field(None, max_length=500)
    llm_api_key: Optional[str] = Field(None, max_length=500)
    llm_model: Optional[str] = Field(None, min_length=1, max_length=200)
