"""Application settings API endpoints (LLM configuration)."""
import logging

import requests
from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from homebrain.config import save_settings_overrides, settings
from homebrain.models.schemas import (
    ModelListRequest,
    ModelListResponse,
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
    """
    return SettingsResponse(
        llm_base_url=settings.LLM_BASE_URL,
        llm_model=settings.LLM_MODEL,
        llm_api_key_set=bool(settings.LLM_API_KEY),
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
