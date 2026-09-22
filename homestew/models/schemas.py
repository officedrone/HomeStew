"""Pydantic models for API requests and responses."""
import re
from pydantic import BaseModel, Field, model_validator
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


class ManualCandidate(BaseModel):
    """A found PDF manual offered to the user for approval (not downloaded)."""
    name: str = Field(..., description="PDF file name shown in the table")
    title: str = Field("", description="Search result title (tooltip)")
    url: str = Field(..., description="Direct URL of the PDF")
    domain: str = Field("", description="Top-level domain the PDF comes from")
    already_downloaded: bool = Field(
        False,
        description="True when this exact URL is already stored for the device",
    )


class ManualSearchResponse(BaseModel):
    """Candidate list returned by the manual search endpoint."""
    candidates: list[ManualCandidate] = []
    error_detail: Optional[str] = None


# Vision support: user messages may carry inline images as data URLs.
# The frontend downscales photos before sending (canvas -> JPEG q~0.85),
# so a few MB of base64 per image is the realistic worst case; anything
# bigger is rejected instead of bloating every LLM request.
MAX_CHAT_IMAGES = 4
MAX_IMAGE_DATA_URL_CHARS = 4_000_000
_IMAGE_DATA_URL_RE = re.compile(
    r"^data:image/(?:png|jpe?g|webp|gif);base64,[A-Za-z0-9+/=\s]+$"
)


class ChatMessage(BaseModel):
    """Chat message model.

    ``images`` carries optional data URLs (``data:image/jpeg;base64,...``)
    for vision models. Only the newest user turn's images are forwarded to
    the LLM - older ones collapse to a text placeholder in api/chat.py so
    conversation history stays small for local models with tight context.
    """
    role: str = Field(..., pattern="^(user|assistant|system)$")
    # No min_length: an image-only message has empty text. The validator
    # below guarantees at least one of text/images is present.
    content: str = Field("", max_length=20000)
    images: list[str] = Field(
        default_factory=list,
        description="Optional inline images as data:image/...;base64 URLs",
    )

    @model_validator(mode="after")
    def _validate_text_or_images(self):
        # An empty message is only meaningful when it carries an image.
        if not self.content.strip() and not self.images:
            raise ValueError("Message must contain text or at least one image")
        if self.images and self.role != "user":
            raise ValueError("Only user messages may carry images")
        if len(self.images) > MAX_CHAT_IMAGES:
            raise ValueError(f"At most {MAX_CHAT_IMAGES} images per message")
        for url in self.images:
            if len(url) > MAX_IMAGE_DATA_URL_CHARS:
                raise ValueError("Attached image is too large")
            # Reject anything that is not an inline raster data URL: no
            # remote URLs (the LLM server may not fetch them, and it would
            # turn the chat into an SSRF vector), no non-image types.
            if not _IMAGE_DATA_URL_RE.match(url):
                raise ValueError(
                    "Images must be inline data:image/(png|jpeg|webp|gif);base64 URLs"
                )
        return self


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


class StoreManualRequest(BaseModel):
    """Ask the server to download ONE user-approved candidate manual.

    ``replace`` must be set by the client only after the user confirmed that
    an already-stored manual may be overwritten; without it a known URL is
    rejected with 409 instead of touching disk.
    """
    device_id: int
    url: str = Field(..., min_length=1)
    title: Optional[str] = None
    replace: bool = False


class StoreManualResponse(BaseModel):
    """Result of storing one approved manual."""
    manual_id: int
    filename: str
    replaced: bool = False
    indexed: bool = False


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
    device_tool_description: str = Field(
        ..., description="Description of the manage_devices tool shown to the LLM"
    )
    device_tool_description_default: str = Field(
        ..., description="Built-in default for device_tool_description (Restore Default)"
    )
    notify_enabled: bool = Field(..., description="Background due-event notifier on/off")
    notify_check_interval_minutes: int = Field(
        ..., description="How often the notifier loop scans the calendar"
    )
    notify_lead_value: int = Field(..., description="Lead-time magnitude before due")
    notify_lead_unit: str = Field(..., description="Lead-time unit: hours | days")
    notify_webhook_enabled: bool = Field(
        ..., description="Webhook channel on/off (requires a URL below)"
    )
    notify_webhook_type: Literal["generic", "synology"] = Field(
        ...,
        description="Webhook request shape: generic JSON or Synology Chat incoming webhook",
    )
    notify_webhook_url_set: bool = Field(
        ...,
        description="True if a webhook URL is configured (value not exposed - "
        "Synology URLs embed their secret token, so the URL is write-only too)",
    )
    notify_webhook_token_set: bool = Field(
        ..., description="True if a webhook bearer token is configured (value not exposed)"
    )
    notify_webhook_verify_ssl: bool = Field(
        ...,
        description="Verify TLS certificates when POSTing to the webhook URL",
    )
    secrets_encrypted: bool = Field(
        ...,
        description="False when no master key file is mounted and secrets are "
        "stored as plaintext (the UI shows a warning banner)",
    )
    secrets_key_source: Literal["mounted", "generated", "none"] = Field(
        "none",
        description="Where the secrets master key comes from: 'mounted' key "
        "file, HomeStew-managed key in the data volume, or 'none' (the UI's "
        "first-run wizard offers a one-time creation)",
    )
    secrets_key_deletable: bool = Field(
        False,
        description="True when a HomeStew-managed key exists that Settings > "
        "Advanced can delete (a mounted key file is not deletable)",
    )
    unreadable_secrets: list[str] = Field(
        default_factory=list,
        description="Names of stored secrets that could not be decrypted "
        "(wrong key file or tampered data); they are treated as unset",
    )
    setup_steps: dict[str, Literal["skipped", "saved"]] = Field(
        default_factory=dict,
        description="First-run wizard steps already resolved (skipped or "
        "saved), keyed by step name. Persisted so a skipped step is never "
        "re-prompted on the next launch.",
    )
    llm_configured: bool = Field(
        False,
        description="True when the LLM integration looks set up already "
        "(API key saved, AI step previously saved in the wizard, or base "
        "URL/model changed from the built-in defaults)",
    )
    password_configured: bool = Field(
        False,
        description="True once the single-user account password exists. The "
        "hash itself is never returned by any endpoint.",
    )
    auth_max_failed_attempts: int = Field(
        8, ge=1, le=100,
        description="Consecutive failed logins from one client IP before it "
        "is locked out (Settings > Advanced)",
    )
    auth_lockout_minutes: int = Field(
        5, ge=1, le=240,
        description="How long a locked-out client IP must wait before it may "
        "try logging in again",
    )


class SecretsKeyActionResponse(BaseModel):
    """Result of the create/delete master-key endpoints."""
    ok: bool = Field(..., description="Whether the action changed anything")
    reason: str = Field(
        ...,
        description="Machine-readable outcome: created | exists | mounted | "
        "failed for create; deleted | mounted | none for delete",
    )
    secrets_key_source: Literal["mounted", "generated", "none"] = Field(
        ..., description="Key source after the action"
    )
    secrets_encrypted: bool = Field(
        ..., description="Secrets-at-rest state after the action"
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
    device_tool_description: Optional[str] = Field(
        None, max_length=4000, description="Custom manage_devices tool description"
    )
    notify_enabled: Optional[bool] = None
    notify_check_interval_minutes: Optional[int] = Field(
        None, ge=1, le=1440, description="Notifier scan cadence in minutes"
    )
    notify_lead_value: Optional[int] = Field(
        None, ge=1, le=365, description="Notify this many hours/days before due"
    )
    notify_lead_unit: Optional[Literal["hours", "days"]] = None
    notify_webhook_enabled: Optional[bool] = None
    notify_webhook_type: Optional[Literal["generic", "synology"]] = Field(
        None, description="Webhook kind: generic JSON POST or Synology Chat incoming webhook"
    )
    notify_webhook_url: Optional[str] = Field(
        None,
        max_length=500,
        description="Webhook URL; blank keeps the existing one (use "
        "clear_secrets to remove it)",
    )
    notify_webhook_token: Optional[str] = Field(
        None,
        max_length=500,
        description="Bearer token; blank keeps the existing token",
    )
    notify_webhook_verify_ssl: Optional[bool] = Field(
        None,
        description="Validate TLS certificates for webhook POSTs (uncheck for self-signed certs)",
    )
    clear_secrets: Optional[list[Literal[
        "llm_api_key", "notify_webhook_url", "notify_webhook_token"
    ]]] = Field(
        None,
        description="Secrets to remove (the UI's Remove buttons). Applied "
        "after the updates above, so a field and its clear entry must not "
        "be combined in one request.",
    )
    auth_max_failed_attempts: Optional[int] = Field(
        None, ge=1, le=100,
        description="Failed logins per client IP before the lockout applies",
    )
    auth_lockout_minutes: Optional[int] = Field(
        None, ge=1, le=240,
        description="Lockout duration in minutes once the limit is reached",
    )


class WebhookTestRequest(BaseModel):
    """Optionally override webhook URL / token when sending a test payload.

    Blank/omitted values fall back to the currently saved settings, so the
    Settings UI can test a freshly typed (not yet saved) configuration.
    """
    webhook_url: Optional[str] = Field(None, max_length=500)
    webhook_token: Optional[str] = Field(None, max_length=500)
    verify_ssl: Optional[bool] = Field(
        None,
        description="TLS verification for this test; omitted uses the saved setting",
    )
    webhook_type: Optional[Literal["generic", "synology"]] = Field(
        None, description="Request shape for this test; omitted uses the saved setting",
    )


class SetupStepResolveRequest(BaseModel):
    """Record how a first-run wizard step ended (skip or save).

    Persisted server-side so the step is not offered again on later page
    loads - skipping is a deliberate choice, not just a dismissal.
    """
    step: Literal["secrets_key", "llm", "device"] = Field(
        ..., description="The wizard step being resolved"
    )
    resolution: Literal["skipped", "saved"] = Field(
        ..., description="How the step ended for this user"
    )


class SetupStepResolveResponse(BaseModel):
    """All wizard step resolutions after the recorded one."""
    setup_steps: dict[str, Literal["skipped", "saved"]]


class AuthStatusResponse(BaseModel):
    """The only pre-login information HomeStew reveals (two booleans)."""
    password_configured: bool = Field(
        ..., description="False on first run -> the UI shows create-account"
    )
    authenticated: bool = Field(
        ..., description="Whether the caller's session cookie is valid"
    )


class PasswordSetupRequest(BaseModel):
    """First-run account creation (POST /api/auth/setup)."""
    password: str = Field(..., min_length=8, max_length=256)
    confirm_password: str = Field(..., min_length=8, max_length=256)


class PasswordLoginRequest(BaseModel):
    """Password login (POST /api/auth/login)."""
    password: str = Field(..., min_length=1, max_length=256)
    remember_me: bool = Field(
        False,
        description="True -> persistent 30-day cookie; false -> the session "
        "ends when the browser closes (and the token expires after 12 h)",
    )


class PasswordChangeRequest(BaseModel):
    """Password change from Settings > Advanced (PUT /api/auth/password)."""
    current_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=8, max_length=256)
    confirm_password: str = Field(..., min_length=8, max_length=256)


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
    """Partial update - only provided fields change."""
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


# ---------------------------------------------------------------------------
# Backup / restore (Settings > General)
#
# The archive stores raw database column values (including ids and audit
# timestamps) so a restore can reproduce the original rows as faithfully as
# possible. Dates/times/datetimes are plain ISO strings in the JSON; Pydantic
# parses them back on restore. Manuals reference their PDF inside the archive
# via ``archive_path`` instead of the absolute ``filepath`` column, which is
# machine-specific and must be rewritten against the current DEVICES_DIR.
# ---------------------------------------------------------------------------

# Temporal columns are carried as verbatim ISO strings (exactly what SQLite
# stores) rather than parsed date/datetime objects, so a restore re-inserts the
# original text. This keeps audit timestamps byte-identical and, critically,
# preserves notification-ledger dedupe keys that compare due_date as TEXT.
class BackupDevice(BaseModel):
    """A device row exactly as stored, for backup/restore round-tripping."""
    id: int
    name: str
    brand: str
    model: str
    description: Optional[str] = None
    serial_number: Optional[str] = None
    product_number: Optional[str] = None
    purchase_date: Optional[str] = None
    warranty_length: Optional[int] = None
    warranty_unit: Optional[str] = None
    warranty_end: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BackupAttribute(BaseModel):
    """A custom device attribute row."""
    id: int
    device_id: int
    attribute_name: str
    attribute_value: str
    created_at: Optional[str] = None


class BackupManual(BaseModel):
    """A manual row; the PDF itself lives at ``archive_path`` in the archive."""
    id: int
    device_id: int
    filename: str
    page_count: Optional[int] = None
    source_url: Optional[str] = None
    indexed_at: Optional[str] = None
    archive_path: Optional[str] = Field(
        None, description="Relative path of the PDF inside the archive (manuals/<id>.pdf)"
    )


class BackupCalendarEvent(BaseModel):
    """A calendar event row exactly as stored."""
    id: int
    device_id: Optional[int] = None
    title: str
    description: Optional[str] = None
    start_date: date = Field(..., description="Anchor / first due date (validated)")
    start_time: Optional[str] = None
    recurrence_type: str
    interval: int
    last_completed_at: Optional[str] = None
    created_at: Optional[str] = None


class BackupNotification(BaseModel):
    """A notification-ledger row, so restored events don't re-alert.

    ``due_date`` stays a verbatim string to match the UNIQUE(event_id,
    due_date, kind) dedupe key exactly on restore.
    """
    event_id: int
    due_date: str
    kind: str
    sent_at: Optional[str] = None


class BackupData(BaseModel):
    """Everything captured in a backup's data.json member."""
    devices: list[BackupDevice] = []
    device_attributes: list[BackupAttribute] = []
    manuals: list[BackupManual] = []
    calendar_events: list[BackupCalendarEvent] = []
    notification_log: list[BackupNotification] = []


class BackupCreateResponse(BaseModel):
    """Result of building a backup file, before it is downloaded."""
    filename: str = Field(..., description="Archive name to download")
    url: str = Field(..., description="One-shot download URL for the archive")
    devices: int
    manuals: int
    calendar_events: int
    missing_files: list[str] = Field(
        default_factory=list,
        description="Manuals whose PDF was absent on disk and could not be archived",
    )


class RestoreResponse(BaseModel):
    """Summary of a restore operation (search re-indexing runs in background)."""
    mode: str = Field(..., pattern="^(merge|replace)$")
    devices_created: int
    devices_merged: int
    manuals_restored: int
    events_restored: int
    skipped: int = Field(0, description="Rows already present and left untouched (merge)")
    index_total: int = Field(..., description="Manuals queued for search re-indexing")


class RestoreIndexStatus(BaseModel):
    """Progress of the background search re-index that follows a restore."""
    running: bool
    done: int
    total: int
