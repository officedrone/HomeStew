"""Configuration management for HomeStew."""
import json
import logging
import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings

from homestew.default_prompts import (
    DEFAULT_CALENDAR_TOOL_DESCRIPTION,
    DEFAULT_CHAT_SYSTEM_PROMPT,
    DEFAULT_SEARCH_TOOL_DESCRIPTION,
)
from homestew.services.secret_store import (
    SecretError,
    SecretStore,
    get_secret_store,
    is_encrypted,
)

logger = logging.getLogger(__name__)

# Settings that can be changed at runtime via the UI and persisted to disk.
EDITABLE_SETTINGS = (
    "THEME",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "CHAT_SYSTEM_PROMPT",
    "SEARCH_TOOL_DESCRIPTION",
    "CALENDAR_TOOL_DESCRIPTION",
    "NOTIFY_ENABLED",
    "NOTIFY_CHECK_INTERVAL_MINUTES",
    "NOTIFY_LEAD_VALUE",
    "NOTIFY_LEAD_UNIT",
    "NOTIFY_WEBHOOK_ENABLED",
    "NOTIFY_WEBHOOK_TYPE",
    "NOTIFY_WEBHOOK_URL",
    "NOTIFY_WEBHOOK_TOKEN",
    "NOTIFY_WEBHOOK_VERIFY_SSL",
)

# Subset of EDITABLE_SETTINGS holding credentials. These are encrypted with
# AES-256-GCM (services/secret_store.py) before being written to
# settings.json and are never returned in plaintext by the settings API.
# The webhook URL counts because Synology-style webhooks embed their secret
# token in the URL query string.
SECRET_SETTINGS = (
    "LLM_API_KEY",
    "NOTIFY_WEBHOOK_URL",
    "NOTIFY_WEBHOOK_TOKEN",
)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # Application
    APP_NAME: str = "HomeStew"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    
    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # Comma-separated extra origins allowed to call the API cross-origin
    # (e.g. a separate frontend on another port). Empty — the default — means
    # same-origin only: no CORS headers are emitted, so arbitrary web pages
    # cannot read responses or push settings (incl. secrets) from a browser.
    EXTRA_ALLOWED_ORIGINS: str = ""

    # UI theme: 'auto' follows the OS preference; 'light'/'dark' pin it.
    THEME: str = "auto"
    
    # LLM Configuration
    LLM_BASE_URL: str = "http://localhost:11434/v1"
    # Empty by default — local servers (Ollama etc.) ignore the key, and an
    # empty value means "no API key configured" in the Settings UI.
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "llama3.2"

    # LLM prompts (editable under Settings > Advanced Settings). Defaults are
    # the built-in prompt texts from homestew.default_prompts.
    CHAT_SYSTEM_PROMPT: str = DEFAULT_CHAT_SYSTEM_PROMPT
    SEARCH_TOOL_DESCRIPTION: str = DEFAULT_SEARCH_TOOL_DESCRIPTION
    CALENDAR_TOOL_DESCRIPTION: str = DEFAULT_CALENDAR_TOOL_DESCRIPTION

    # Notifications — a background loop (services/notifier.py) checks calendar
    # events every NOTIFY_CHECK_INTERVAL_MINUTES minutes and alerts when one is
    # overdue or due within the lead time (value + hours/days unit). The loop
    # re-reads these values each tick, so UI changes apply without a restart.
    NOTIFY_ENABLED: bool = False
    NOTIFY_CHECK_INTERVAL_MINUTES: int = 15
    NOTIFY_LEAD_VALUE: int = 24
    NOTIFY_LEAD_UNIT: str = "hours"  # 'hours' | 'days'

    # Webhook notification channel — generic JSON POST to the user's URL.
    # The token is an optional bearer secret; like the LLM API key its value
    # is never returned by the settings API, only whether one is configured.
    NOTIFY_WEBHOOK_ENABLED: bool = True
    # Request shape for the webhook URL: 'generic' = raw JSON body, 'synology'
    # = Synology Chat incoming webhook (form-encoded payload={"text": ...}).
    NOTIFY_WEBHOOK_TYPE: str = "generic"
    NOTIFY_WEBHOOK_URL: str = ""
    NOTIFY_WEBHOOK_TOKEN: str = ""
    # When False, webhook POSTs skip TLS certificate verification — needed for
    # receivers with self-signed certs (e.g. a LAN Synology DSM webhook).
    NOTIFY_WEBHOOK_VERIFY_SSL: bool = True
    
    # Data directories
    DATA_DIR: Path = Path("/data")
    DEVICES_DIR: Optional[Path] = None

    # Optional master-key file for encrypting the secrets in settings.json,
    # e.g. a read-only Docker secret mount (see README "Secrets"). When the
    # file is absent HomeStew auto-generates a key on first boot and keeps it
    # at <DATA_DIR>/.secrets_key, so encryption works out of the box; this
    # path only lets you manage the key yourself (kept outside the volume).
    SECRETS_KEY_FILE: Optional[Path] = Path("/run/secrets/homestew_secret_key")
    
    class Config:
        env_file = ".env"
        case_sensitive = True
    
    # Runtime-only status flags (never persisted): whether secret values are
    # encrypted at rest, and which stored secrets could not be decrypted
    # (wrong/rotated key file or tampered data) and were treated as unset.
    secrets_encrypted: bool = False
    unreadable_secrets: tuple[str, ...] = ()

    def model_post_init(self, __context):
        """Set computed paths after initialization."""
        if self.DEVICES_DIR is None:
            object.__setattr__(self, 'DEVICES_DIR', self.DATA_DIR / "devices")

    @property
    def devices_dir(self) -> Path:
        """Get the devices directory path."""
        return self.DEVICES_DIR


# Global settings instance
settings = Settings()

# Secret encryption for persisted settings; built once at import (this is
# where a first-boot master key gets generated into DATA_DIR). The two
# functions below are the only paths through which secrets reach the disk.
_secret_store: SecretStore = get_secret_store(settings)


def _overrides_path() -> Path:
    """Path to the persisted settings-override file (inside the /data volume)."""
    return settings.DATA_DIR / "settings.json"


def load_settings_overrides() -> None:
    """Apply persisted UI overrides on top of env/default values.

    Precedence: settings.json > environment variables > defaults.
    Called once at import time so overrides survive container restarts.

    Secret values (SECRET_SETTINGS) are decrypted back into the in-memory
    singleton; a secret that cannot be decrypted (wrong key file, tampered
    data, or ciphertext stored while no key was mounted) is treated as unset
    and recorded in ``settings.unreadable_secrets`` for the Settings UI.
    Legacy plaintext secrets are migrated to encrypted form in place when a
    master key is available.
    """
    path = _overrides_path()
    if not path.exists():
        settings.secrets_encrypted = _secret_store.available
        return
    try:
        # utf-8-sig also decodes plain UTF-8; it additionally strips a BOM,
        # which Windows editors/tools like to add and json.loads rejects.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read settings overrides from %s: %s", path, exc)
        settings.secrets_encrypted = _secret_store.available
        return
    unreadable: list[str] = []
    migrate = False  # plaintext secrets found while a key is available
    for key in EDITABLE_SETTINGS:
        if key not in data or data[key] is None:
            continue
        value = data[key]
        if key in SECRET_SETTINGS and isinstance(value, str) and value:
            if is_encrypted(value):
                try:
                    value = _secret_store.decrypt(key, value)
                except SecretError as exc:
                    logger.error(
                        "Stored secret %s could not be decrypted (%s). It is "
                        "treated as unset — re-enter it in the Settings UI.",
                        key, exc,
                    )
                    unreadable.append(key)
                    value = ""
            elif _secret_store.available:
                migrate = True
        setattr(settings, key, value)
    settings.unreadable_secrets = tuple(unreadable)
    settings.secrets_encrypted = _secret_store.available
    logger.info("Loaded settings overrides from %s", path)
    if migrate:
        # Re-encrypt the plaintext secrets already held in memory and rewrite
        # the file, so old installs upgrade transparently on first boot with
        # a key file present.
        migrated = {
            key: getattr(settings, key)
            for key in SECRET_SETTINGS
            if getattr(settings, key) and not is_encrypted(getattr(settings, key))
        }
        try:
            save_settings_overrides(migrated)
            logger.info(
                "Migrated %d plaintext secret(s) to encrypted form", len(migrated)
            )
        except OSError as exc:
            logger.warning("Could not migrate plaintext secrets: %s", exc)


def save_settings_overrides(values: dict) -> None:
    """Merge the given values into the persisted override file.

    Only keys present in EDITABLE_SETTINGS are written. Existing stored
    values for keys not included here are preserved (they stay as they were
    saved — encrypted secrets keep their existing ciphertext tokens).

    Secret values (SECRET_SETTINGS) are encrypted before writing when a
    master key is available; without one they fall back to plaintext (the
    Settings UI warns about this). The file is chmod-ed to 0600 best-effort.
    """
    path = _overrides_path()
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    for key in EDITABLE_SETTINGS:
        if key not in values or values[key] is None:
            continue
        value = values[key]
        if (
            key in SECRET_SETTINGS
            and isinstance(value, str)
            and value
            and not is_encrypted(value)
        ):
            if _secret_store.available:
                try:
                    value = _secret_store.encrypt(key, value)
                except SecretError as exc:  # defensive: never lose the write
                    logger.error("Could not encrypt %s (%s); storing plaintext", key, exc)
            else:
                logger.warning(
                    "No secrets master key available — storing %s "
                    "UNENCRYPTED. See README 'Secrets'.", key,
                )
        existing[key] = value

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    try:
        # Restrict to the owner. On Windows chmod only toggles the read-only
        # bit — real protection there comes from NTFS ACLs / volume choice.
        os.chmod(path, 0o600)
    except OSError:
        pass


# Apply any persisted overrides immediately after the singleton is created.
load_settings_overrides()
