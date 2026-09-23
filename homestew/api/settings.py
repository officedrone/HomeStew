"""Application settings API endpoints (LLM configuration)."""
import logging
from urllib.parse import urlsplit, urlunsplit

import requests
from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from homestew.config import (
    create_master_key_runtime,
    delete_master_key_runtime,
    llm_looks_configured,
    normalize_setup_steps,
    resolve_setup_step,
    save_settings_overrides,
    settings,
)
from homestew.services import auth
from homestew.default_prompts import (
    DEFAULT_CALENDAR_TOOL_DESCRIPTION,
    DEFAULT_CHAT_SYSTEM_PROMPT,
    DEFAULT_DEVICE_TOOL_DESCRIPTION,
    DEFAULT_SEARCH_TOOL_DESCRIPTION,
)
from homestew.models.schemas import (
    ModelListRequest,
    ModelListResponse,
    ModelStatusResponse,
    SecretsKeyActionResponse,
    SettingsResponse,
    SettingsUpdate,
    SetupStepResolveRequest,
    SetupStepResolveResponse,
)

logger = logging.getLogger(__name__)

# How long to wait for the LLM server when probing its model list.
MODEL_LIST_TIMEOUT = 10

# Maps clear_secrets request values to settings attribute names.
_CLEARABLE_SECRETS = {
    "llm_api_key": "LLM_API_KEY",
    "notify_webhook_url": "NOTIFY_WEBHOOK_URL",
    "notify_webhook_token": "NOTIFY_WEBHOOK_TOKEN",
}

router = APIRouter(prefix="/settings", tags=["settings"])


def _normalize_base_url(base_url: str) -> str:
    """Canonical form of an LLM base URL for equality checks.

    Lowercases scheme+host and strips a trailing slash (and a trailing "/v1"
    is kept - servers are addressed consistently enough that this matters
    only to decide whether the saved API key may be sent).
    """
    base_url = base_url.strip().rstrip("/")
    try:
        parts = urlsplit(base_url)
    except ValueError:
        return base_url.lower()
    if not parts.netloc:
        return base_url.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def _current_settings() -> SettingsResponse:
    """Build the response from the live settings singleton.

    Secret values (API key, webhook URL and token - Synology webhook URLs
    embed their token) are never returned - only whether each is configured.
    The built-in prompt defaults are included so the UI's "Restore Default"
    buttons can repopulate them without shipping a second copy of the text.
    """
    return SettingsResponse(
        theme=settings.THEME,
        llm_base_url=settings.LLM_BASE_URL,
        llm_model=settings.LLM_MODEL,
        llm_supports_vision=bool(getattr(settings, "LLM_SUPPORTS_VISION", False)),
        llm_api_key_set=bool(settings.LLM_API_KEY),
        chat_system_prompt=settings.CHAT_SYSTEM_PROMPT,
        search_tool_description=settings.SEARCH_TOOL_DESCRIPTION,
        calendar_tool_description=settings.CALENDAR_TOOL_DESCRIPTION,
        device_tool_description=settings.DEVICE_TOOL_DESCRIPTION,
        chat_system_prompt_default=DEFAULT_CHAT_SYSTEM_PROMPT,
        search_tool_description_default=DEFAULT_SEARCH_TOOL_DESCRIPTION,
        calendar_tool_description_default=DEFAULT_CALENDAR_TOOL_DESCRIPTION,
        device_tool_description_default=DEFAULT_DEVICE_TOOL_DESCRIPTION,
        notify_enabled=settings.NOTIFY_ENABLED,
        notify_check_interval_minutes=settings.NOTIFY_CHECK_INTERVAL_MINUTES,
        notify_lead_value=settings.NOTIFY_LEAD_VALUE,
        notify_lead_unit=settings.NOTIFY_LEAD_UNIT,
        notify_webhook_enabled=settings.NOTIFY_WEBHOOK_ENABLED,
        notify_webhook_type=getattr(settings, "NOTIFY_WEBHOOK_TYPE", "generic"),
        notify_webhook_url_set=bool(settings.NOTIFY_WEBHOOK_URL),
        notify_webhook_token_set=bool(settings.NOTIFY_WEBHOOK_TOKEN),
        notify_webhook_verify_ssl=settings.NOTIFY_WEBHOOK_VERIFY_SSL,
        secrets_encrypted=settings.secrets_encrypted,
        secrets_key_source=_secrets_key_source(),
        secrets_key_deletable=_secrets_key_source() == "generated",
        unreadable_secrets=list(settings.unreadable_secrets),
        # First-run wizard: which steps were already skipped/saved (never
        # re-prompted) and whether the LLM looks configured, so the UI can
        # decide which steps still apply.
        setup_steps=normalize_setup_steps(settings.SETUP_STEPS),
        llm_configured=llm_looks_configured(),
        # Account status for Settings > Advanced. The hash itself is never
        # exposed - only whether one exists (every caller reaching this
        # endpoint is authenticated anyway). Read through auth so a CLI reset
        # from another process is reflected without a restart.
        password_configured=auth.password_configured(),
        auth_max_failed_attempts=settings.AUTH_MAX_FAILED_ATTEMPTS,
        auth_lockout_minutes=settings.AUTH_LOCKOUT_MINUTES,
    )


def _secrets_key_source() -> str:
    """Where the master key comes from: 'mounted' | 'generated' | 'none'.

    Read through the config module's store attribute (not imported once) so
    runtime create/delete of the managed key is reflected immediately.
    """
    from homestew import config as app_config

    return getattr(app_config._secret_store, "key_source", "none")


@router.get("", response_model=SettingsResponse)
async def get_settings():
    """Get current LLM settings (API key value is never exposed)."""
    return _current_settings()


@router.put("", response_model=SettingsResponse)
async def update_settings(update: SettingsUpdate):
    """Update LLM settings, persist them and apply immediately.

    Blank/omitted secret fields are left unchanged - in particular an empty
    llm_api_key means "keep the existing key". Use ``clear_secrets`` to
    remove a stored secret (the UI's Remove buttons).
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

    # Vision toggle: a plain boolean, so False must be applied too (None
    # means "unchanged", which the Optional schema already gives us).
    if update.llm_supports_vision is not None:
        changes["LLM_SUPPORTS_VISION"] = update.llm_supports_vision

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
    if update.device_tool_description is not None:
        changes["DEVICE_TOOL_DESCRIPTION"] = (
            update.device_tool_description.strip() or DEFAULT_DEVICE_TOOL_DESCRIPTION
        )

    # Notifications: the notifier loop re-reads these each tick, so saving is
    # enough - no restart or client reset needed.
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

    # Webhook URL: like the API key it is write-only, so a blank value keeps
    # the stored URL; removal goes through clear_secrets.
    if update.notify_webhook_url is not None and update.notify_webhook_url.strip():
        url = update.notify_webhook_url.strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook URL must start with http:// or https://"
            )
        changes["NOTIFY_WEBHOOK_URL"] = url

    # Webhook bearer token: blank/omitted keeps the existing token, exactly
    # like llm_api_key above - its value is never returned by GET.
    if update.notify_webhook_token is not None and update.notify_webhook_token.strip():
        changes["NOTIFY_WEBHOOK_TOKEN"] = update.notify_webhook_token.strip()

    # SSL validation toggle: a plain boolean, so False must be applied too
    # (None means "unchanged", which the Optional schema already gives us).
    if update.notify_webhook_verify_ssl is not None:
        changes["NOTIFY_WEBHOOK_VERIFY_SSL"] = update.notify_webhook_verify_ssl

    # Login lockout tuning (Settings > Advanced). The password itself never
    # travels through this endpoint - it has its own PUT /api/auth/password
    # so changing it goes through current-password verification.
    if update.auth_max_failed_attempts is not None:
        changes["AUTH_MAX_FAILED_ATTEMPTS"] = update.auth_max_failed_attempts
    if update.auth_lockout_minutes is not None:
        changes["AUTH_LOCKOUT_MINUTES"] = update.auth_lockout_minutes

    # Explicit secret removal (UI Remove buttons). Applied after the updates
    # above; an empty string persists as "cleared" (load treats it as unset).
    for name in update.clear_secrets or []:
        changes[_CLEARABLE_SECRETS[name]] = ""

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
    from homestew.api.chat import reset_llm_client
    reset_llm_client()

    logger.info("Settings updated: %s", sorted(changes.keys()))
    return _current_settings()


@router.post("/secrets-key/create", response_model=SecretsKeyActionResponse)
async def create_secrets_key():
    """Create the HomeStew-managed secrets master key (one-time setup).

    Called by the first-run wizard and Settings > Advanced when no key is
    available. A mounted key file always wins - this only manages the key
    inside the data volume, never overwrites an existing one.
    """
    ok, reason = create_master_key_runtime()
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "exists": "A usable master key is already in place.",
                "mounted": "The mounted key file exists but could not be "
                "read - fix the file at SECRETS_KEY_FILE instead of creating "
                "a second key.",
                "failed": "The data directory rejected the new key file. "
                "Check that it is writable, or mount a key file at "
                "SECRETS_KEY_FILE.",
            }.get(reason, reason),
        )
    logger.info("Secrets master key created via API.")
    return SecretsKeyActionResponse(
        ok=True,
        reason=reason,
        secrets_key_source=_secrets_key_source(),
        secrets_encrypted=settings.secrets_encrypted,
    )


@router.post("/secrets-key/delete", response_model=SecretsKeyActionResponse)
async def delete_secrets_key():
    """Delete the HomeStew-managed master key (Settings > Advanced).

    Troubleshooting / rotation aid: afterwards stored secrets can no longer
    be decrypted - they are reported as unreadable and treated as unset
    until a new key is created and the secrets are re-entered. A mounted
    key file is outside HomeStew's management and must be removed via the
    container's secret mount.
    """
    try:
        ok, reason = delete_master_key_runtime()
    except OSError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The key file could not be deleted - check the data volume.",
        )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "mounted": "The active key comes from a mounted key file. "
                "Remove the mount (docker-compose 'secrets') to delete it.",
                "none": "There is no HomeStew-managed key to delete.",
            }.get(reason, reason),
        )
    logger.warning("Secrets master key deleted via API.")
    return SecretsKeyActionResponse(
        ok=True,
        reason=reason,
        secrets_key_source=_secrets_key_source(),
        secrets_encrypted=settings.secrets_encrypted,
    )


@router.post("/setup-steps/resolve", response_model=SetupStepResolveResponse)
async def resolve_setup_wizard_step(body: SetupStepResolveRequest):
    """Record that a first-run wizard step was skipped or completed.

    The resolution is persisted to the data volume, so a step the user
    skipped is not offered again on the next launch - skipping is a choice,
    not just a dismissal. Steps whose condition no longer applies (a key
    exists, an LLM is configured, devices were added) drop out of the wizard
    by themselves and need no resolution here.
    """
    steps = resolve_setup_step(body.step, body.resolution)
    return SetupStepResolveResponse(setup_steps=steps)


@router.post("/models", response_model=ModelListResponse)
async def list_llm_models(probe: ModelListRequest):
    """Query the LLM server's OpenAI-compatible ``/models`` endpoint.

    Uses the base URL / API key from the request body when provided (so a
    freshly typed, unsaved configuration can be probed), otherwise falls back
    to the currently saved settings. The key is only ever sent to the server
    itself and never logged or returned.

    Security: the *saved* key is attached only when the effective URL matches
    the saved base URL. A different (typed) URL can be probed with a freshly
    typed key, but never silently carries the stored one - otherwise this
    endpoint would let any caller ship the saved credential to an arbitrary
    host.
    """
    base_url = (probe.llm_base_url or "").strip() or settings.LLM_BASE_URL
    base_url = base_url.rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LLM base URL must start with http:// or https://"
        )

    typed_key = (probe.llm_api_key or "").strip()
    if typed_key:
        api_key = typed_key
    elif _normalize_base_url(base_url) == _normalize_base_url(settings.LLM_BASE_URL):
        api_key = settings.LLM_API_KEY
    else:
        api_key = ""
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
    available. Never raises on a connection failure - the caller (the chat
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
            llm_supports_vision=bool(getattr(settings, "LLM_SUPPORTS_VISION", False)),
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
        llm_supports_vision=bool(getattr(settings, "LLM_SUPPORTS_VISION", False)),
        available_models_count=len(result.models),
        available_models=result.models,
    )
