"""Application settings API endpoints (LLM configuration)."""
import logging

import requests
from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from homebrain.config import save_settings_overrides, settings
from homebrain.default_prompts import (
    DEFAULT_CALENDAR_TOOL_DESCRIPTION,
    DEFAULT_CHAT_SYSTEM_PROMPT,
    DEFAULT_SEARCH_TOOL_DESCRIPTION,
)
from homebrain.models.schemas import (
    ModelListRequest,
    ModelListResponse,
    ModelStatusResponse,
    SettingsResponse,
    SettingsUpdate,
)

logger = logging.getLogger(__name__)

# How long to wait for the LLM server when probing its model list.
MODEL_LIST_TIMEOUT = 10

router = APIRouter(prefix="/settings", tags=["settings"])


def _current_settings() -> SettingsResponse:
    """Build the response from the live settings singleton.

    The API key value is never returned — only whether one is configured.
    The built-in prompt defaults are included so the UI's "Restore Default"
    buttons can repopulate them without shipping a second copy of the text.
    """
    return SettingsResponse(
        theme=settings.THEME,
        llm_base_url=settings.LLM_BASE_URL,
        llm_model=settings.LLM_MODEL,
        llm_api_key_set=bool(settings.LLM_API_KEY),
        chat_system_prompt=settings.CHAT_SYSTEM_PROMPT,
        search_tool_description=settings.SEARCH_TOOL_DESCRIPTION,
        calendar_tool_description=settings.CALENDAR_TOOL_DESCRIPTION,
        chat_system_prompt_default=DEFAULT_CHAT_SYSTEM_PROMPT,
        search_tool_description_default=DEFAULT_SEARCH_TOOL_DESCRIPTION,
        calendar_tool_description_default=DEFAULT_CALENDAR_TOOL_DESCRIPTION,
        notify_enabled=settings.NOTIFY_ENABLED,
        notify_check_interval_minutes=settings.NOTIFY_CHECK_INTERVAL_MINUTES,
        notify_lead_value=settings.NOTIFY_LEAD_VALUE,
        notify_lead_unit=settings.NOTIFY_LEAD_UNIT,
        notify_webhook_enabled=settings.NOTIFY_WEBHOOK_ENABLED,
        notify_webhook_type=getattr(settings, "NOTIFY_WEBHOOK_TYPE", "generic"),
        notify_webhook_url=settings.NOTIFY_WEBHOOK_URL,
        notify_webhook_token_set=bool(settings.NOTIFY_WEBHOOK_TOKEN),
        notify_webhook_verify_ssl=settings.NOTIFY_WEBHOOK_VERIFY_SSL,
    )


@router.get("", response_model=SettingsResponse)
async def get_settings():
    """Get current LLM settings (API key value is never exposed)."""
    return _current_settings()


@router.put("", response_model=SettingsResponse)
async def update_settings(update: SettingsUpdate):
    """Update LLM settings, persist them and apply immediately.

    Blank/omitted fields are left unchanged — in particular an empty
    llm_api_key means "keep the existing key".
    """
    changes = {}

    # UI theme: validated by the schema (Literal), so any value here is valid.
    if update.theme is not None:
        changes["THEME"] = update.theme

    if update.llm_base_url is not None and update.llm_base_url.strip():
        base_url = update.llm_base_url.strip()
        if not base_url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="LLM base URL must start with http:// or https://"
            )
        changes["LLM_BASE_URL"] = base_url

    if update.llm_model is not None and update.llm_model.strip():
        changes["LLM_MODEL"] = update.llm_model.strip()

    if update.llm_api_key is not None and update.llm_api_key.strip():
        changes["LLM_API_KEY"] = update.llm_api_key.strip()

    # LLM prompts: a blank value means "use the built-in default", so a user
    # who clears (or goofs up) a prompt can always get back to a working one.
    if update.chat_system_prompt is not None:
        changes["CHAT_SYSTEM_PROMPT"] = (
            update.chat_system_prompt.strip() or DEFAULT_CHAT_SYSTEM_PROMPT
        )
    if update.search_tool_description is not None:
        changes["SEARCH_TOOL_DESCRIPTION"] = (
            update.search_tool_description.strip() or DEFAULT_SEARCH_TOOL_DESCRIPTION
        )
    if update.calendar_tool_description is not None:
        changes["CALENDAR_TOOL_DESCRIPTION"] = (
            update.calendar_tool_description.strip() or DEFAULT_CALENDAR_TOOL_DESCRIPTION
        )

    # Notifications: the notifier loop re-reads these each tick, so saving is
    # enough — no restart or client reset needed.
    if update.notify_enabled is not None:
        changes["NOTIFY_ENABLED"] = update.notify_enabled
    if update.notify_check_interval_minutes is not None:
        changes["NOTIFY_CHECK_INTERVAL_MINUTES"] = update.notify_check_interval_minutes
    if update.notify_lead_value is not None:
        changes["NOTIFY_LEAD_VALUE"] = update.notify_lead_value
    if update.notify_lead_unit is not None:
        changes["NOTIFY_LEAD_UNIT"] = update.notify_lead_unit
    if update.notify_webhook_enabled is not None:
        changes["NOTIFY_WEBHOOK_ENABLED"] = update.notify_webhook_enabled
    # Webhook kind (generic JSON vs Synology Chat): validated by the schema.
    if update.notify_webhook_type is not None:
        changes["NOTIFY_WEBHOOK_TYPE"] = update.notify_webhook_type

    # Webhook URL: unlike the API key, a blank value intentionally clears it
    # (the UI round-trips the real URL from GET, so blank only means "remove").
    if update.notify_webhook_url is not None:
        url = update.notify_webhook_url.strip()
        if url and not url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook URL must start with http:// or https://"
            )
        changes["NOTIFY_WEBHOOK_URL"] = url

    # Webhook bearer token: blank/omitted keeps the existing token, exactly
    # like llm_api_key above — its value is never returned by GET.
    if update.notify_webhook_token is not None and update.notify_webhook_token.strip():
        changes["NOTIFY_WEBHOOK_TOKEN"] = update.notify_webhook_token.strip()

    # SSL validation toggle: a plain boolean, so False must be applied too
    # (None means "unchanged", which the Optional schema already gives us).
    if update.notify_webhook_verify_ssl is not None:
        changes["NOTIFY_WEBHOOK_VERIFY_SSL"] = update.notify_webhook_verify_ssl

    if not changes:
        return _current_settings()

    # Apply to the live singleton so changes take effect immediately.
    for key, value in changes.items():
        setattr(settings, key, value)

    # Persist to the data volume so values survive container restarts.
    try:
        save_settings_overrides(changes)
    except OSError as exc:
        logger.error("Failed to persist settings overrides: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Settings could not be saved to disk"
        )

    # Rebuild the cached LLM client so new connection settings are used.
    from homebrain.api.chat import reset_llm_client
    reset_llm_client()

    logger.info("Settings updated: %s", sorted(changes.keys()))
    return _current_settings()


@router.post("/models", response_model=ModelListResponse)
async def list_llm_models(probe: ModelListRequest):
    """Query the LLM server's OpenAI-compatible ``/models`` endpoint.

    Uses the base URL / API key from the request body when provided (so a
    freshly typed, unsaved configuration can be probed), otherwise falls back
    to the currently saved settings. The key is only ever sent to the server
    itself and never logged or returned.
    """
    base_url = (probe.llm_base_url or "").strip() or settings.LLM_BASE_URL
    base_url = base_url.rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LLM base URL must start with http:// or https://"
        )

    api_key = (probe.llm_api_key or "").strip() or settings.LLM_API_KEY
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    try:
        resp = await run_in_threadpool(
            lambda: requests.get(
                f"{base_url}/models", headers=headers, timeout=MODEL_LIST_TIMEOUT
            )
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        logger.warning("Could not list LLM models from %s: %s", base_url, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach the model list at {base_url}/models"
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Model list endpoint returned an unexpected response"
        )

    models = []
    for item in payload.get("data", []) if isinstance(payload, dict) else []:
        model_id = item.get("id") if isinstance(item, dict) else None
        if isinstance(model_id, str) and model_id and model_id not in models:
            models.append(model_id)
    models.sort(key=str.lower)

    return ModelListResponse(models=models)


@router.get("/model-status", response_model=ModelStatusResponse)
async def get_model_status():
    """Check whether the saved LLM settings are usable for chat.

    Probes the server's model list with the currently saved configuration
    and reports whether it is reachable and whether the selected model is
    available. Never raises on a connection failure — the caller (the chat
    tab) needs a structured answer to show the user, not an HTTP error.
    """
    base_url = settings.LLM_BASE_URL
    model_selected = bool((settings.LLM_MODEL or "").strip())

    try:
        result = await list_llm_models(ModelListRequest())
    except HTTPException as exc:
        return ModelStatusResponse(
            reachable=False,
            model_configured=model_selected,
            llm_base_url=base_url,
            llm_model=settings.LLM_MODEL,
            error=str(exc.detail),
        )

    selected = (settings.LLM_MODEL or "").strip()
    available = any(m.lower() == selected.lower() for m in result.models)
    return ModelStatusResponse(
        reachable=True,
        model_configured=model_selected,
        model_available=available,
        llm_base_url=base_url,
        llm_model=settings.LLM_MODEL,
        available_models_count=len(result.models),
        available_models=result.models,
    )
