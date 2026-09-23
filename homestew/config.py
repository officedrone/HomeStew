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
    DEFAULT_DEVICE_TOOL_DESCRIPTION,
    DEFAULT_SEARCH_TOOL_DESCRIPTION,
)
from homestew.services.secret_store import (
    AUTO_KEY_FILENAME,
    SecretError,
    SecretStore,
    create_generated_master_key,
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
    # Whether the selected model can read images. Gates the chat attach /
    # camera controls (and image uploads server-side) so a text-only model
    # never receives an image it would reject.
    "LLM_SUPPORTS_VISION",
    "CHAT_SYSTEM_PROMPT",
    "SEARCH_TOOL_DESCRIPTION",
    "CALENDAR_TOOL_DESCRIPTION",
    "DEVICE_TOOL_DESCRIPTION",
    "NOTIFY_ENABLED",
    "NOTIFY_CHECK_INTERVAL_MINUTES",
    "NOTIFY_LEAD_VALUE",
    "NOTIFY_LEAD_UNIT",
    "NOTIFY_WEBHOOK_ENABLED",
    "NOTIFY_WEBHOOK_TYPE",
    "NOTIFY_WEBHOOK_URL",
    "NOTIFY_WEBHOOK_TOKEN",
    "NOTIFY_WEBHOOK_VERIFY_SSL",
    # First-run wizard bookkeeping: which steps were skipped/saved, so a
    # skipped step is never re-prompted on the next launch.
    "SETUP_STEPS",
    # Single-user account: scrypt hash of the password (services/auth.py).
    # NOT in SECRET_SETTINGS on purpose - those are encrypted with the
    # master key, and deleting that key would silently disable login. The
    # hash itself is one-way, so plaintext storage adds no exposure.
    "PASSWORD_HASH",
    # Login brute-force lockout tuning (in-memory, per client IP).
    "AUTH_MAX_FAILED_ATTEMPTS",
    "AUTH_LOCKOUT_MINUTES",
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
    # (e.g. a separate frontend on another port). Empty - the default - means
    # same-origin only: no CORS headers are emitted, so arbitrary web pages
    # cannot read responses or push settings (incl. secrets) from a browser.
    EXTRA_ALLOWED_ORIGINS: str = ""

    # UI theme: 'auto' follows the OS preference; 'light'/'dark' pin it.
    THEME: str = "auto"
    
    # LLM Configuration
    LLM_BASE_URL: str = "http://localhost:11434/v1"
    # Empty by default - local servers (Ollama etc.) ignore the key, and an
    # empty value means "no API key configured" in the Settings UI.
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "llama3.2"
    # Whether the selected model can read images (vision). Off by default so
    # a text-only model never gets offered image input; the checkbox under
    # Settings > AI (and in the first-run wizard) turns the chat photo
    # controls on. The chat API rejects images while this is off, so the UI
    # gate is cosmetic as well as defensive.
    LLM_SUPPORTS_VISION: bool = False

    # LLM prompts (editable under Settings > Advanced Settings). Defaults are
    # the built-in prompt texts from homestew.default_prompts.
    CHAT_SYSTEM_PROMPT: str = DEFAULT_CHAT_SYSTEM_PROMPT
    SEARCH_TOOL_DESCRIPTION: str = DEFAULT_SEARCH_TOOL_DESCRIPTION
    CALENDAR_TOOL_DESCRIPTION: str = DEFAULT_CALENDAR_TOOL_DESCRIPTION
    DEVICE_TOOL_DESCRIPTION: str = DEFAULT_DEVICE_TOOL_DESCRIPTION

    # Notifications - a background loop (services/notifier.py) checks calendar
    # events every NOTIFY_CHECK_INTERVAL_MINUTES minutes and alerts when one is
    # overdue or due within the lead time (value + hours/days unit). The loop
    # re-reads these values each tick, so UI changes apply without a restart.
    NOTIFY_ENABLED: bool = False
    NOTIFY_CHECK_INTERVAL_MINUTES: int = 15
    NOTIFY_LEAD_VALUE: int = 24
    NOTIFY_LEAD_UNIT: str = "hours"  # 'hours' | 'days'

    # Webhook notification channel - generic JSON POST to the user's URL.
    # The token is an optional bearer secret; like the LLM API key its value
    # is never returned by the settings API, only whether one is configured.
    NOTIFY_WEBHOOK_ENABLED: bool = True
    # Request shape for the webhook URL: 'generic' = raw JSON body, 'synology'
    # = Synology Chat incoming webhook (form-encoded payload={"text": ...}).
    NOTIFY_WEBHOOK_TYPE: str = "generic"
    NOTIFY_WEBHOOK_URL: str = ""
    NOTIFY_WEBHOOK_TOKEN: str = ""
    # When False, webhook POSTs skip TLS certificate verification - needed for
    # receivers with self-signed certs (e.g. a LAN Synology DSM webhook).
    NOTIFY_WEBHOOK_VERIFY_SSL: bool = True

    # First-run setup wizard bookkeeping: step name -> resolution
    # ('skipped' | 'saved'), e.g. {"secrets_key": "skipped", "llm": "saved"}.
    # Persisted to settings.json so a step the user skipped (or completed)
    # is never re-prompted on a later launch; empty dict = nothing resolved
    # yet, and steps whose condition still applies are offered again.
    SETUP_STEPS: dict = {}

    # Single-user account. Empty = no password yet: every page load then
    # shows the forced create-account screen (first run AND upgrades from
    # pre-auth installs - there is deliberately no way to opt out).
    # scrypt hash string, see services/auth.py; never returned by any API.
    PASSWORD_HASH: str = ""
    # Failed-login lockout: consecutive failures per client IP before that
    # address is refused for AUTH_LOCKOUT_MINUTES minutes. Editable under
    # Settings > Advanced so a household can tune (or effectively widen) it.
    AUTH_MAX_FAILED_ATTEMPTS: int = 8
    AUTH_LOCKOUT_MINUTES: int = 5
    
    # Manual fetching (services/manual_finder.py + manual_downloader.py).
    # Env-only knobs (not UI-editable): MANUAL_SEARCH_BACKENDS is a comma-list
    # of ddgs engines ("auto" when empty - the library picks and rotates, so a
    # single blocked engine no longer fails the search); MANUAL_SEARCH_REGION
    # e.g. "us-en"/"de-de" (library default when empty); caps for results,
    # downloads and per-file size (the container is memory-limited).
    MANUAL_SEARCH_BACKENDS: str = ""
    MANUAL_SEARCH_REGION: str = ""
    MANUAL_MAX_RESULTS: int = 15
    MANUAL_MAX_DOWNLOADS: int = 5
    MANUAL_SEARCH_TIMEOUT: int = 10
    MANUAL_DOWNLOAD_TIMEOUT: int = 30
    MANUAL_MAX_PDF_MB: int = 50
    # Optional http/socks5 proxy for search + PDF downloads, e.g.
    # "http://user:pass@example.com:3128" or the ddgs alias "tb" (Tor).
    MANUAL_PROXY: str = ""

    # Data directories
    DATA_DIR: Path = Path("/data")
    DEVICES_DIR: Optional[Path] = None

    # Optional master-key file for encrypting the secrets in settings.json,
    # e.g. a read-only Docker secret mount (see README "Secrets"). When the
    # file is absent HomeStew uses a managed key at <DATA_DIR>/.secrets_key,
    # created once via the first-run wizard / Settings > Advanced; this path
    # only lets you manage the key yourself (kept outside the volume).
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

# Secret encryption for persisted settings; built once at import from the
# key resolved at boot (mounted file or an existing managed key - a fresh
# install starts without one and creates it via the wizard / Advanced
# settings). The two functions below are the only paths through which
# secrets reach the disk.
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
    # Wizard bookkeeping must be a str->str map; anything else on disk is
    # ignored rather than crashing the load of every other setting.
    if "SETUP_STEPS" in data:
        data["SETUP_STEPS"] = normalize_setup_steps(data["SETUP_STEPS"])
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
                        "treated as unset - re-enter it in the Settings UI.",
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
    saved - encrypted secrets keep their existing ciphertext tokens).

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
                    "No secrets master key available - storing %s "
                    "UNENCRYPTED. See README 'Secrets'.", key,
                )
        existing[key] = value

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    try:
        # Restrict to the owner. On Windows chmod only toggles the read-only
        # bit - real protection there comes from NTFS ACLs / volume choice.
        os.chmod(path, 0o600)
    except OSError:
        pass


def normalize_setup_steps(value) -> dict:
    """Coerce a stored SETUP_STEPS value to a plain ``{step: resolution}`` map.

    Only string keys with 'skipped'/'saved' values survive, so a hand-edited
    or corrupted settings.json can never smuggle junk into the wizard logic.
    """
    if not isinstance(value, dict):
        return {}
    return {
        str(k): v
        for k, v in value.items()
        if isinstance(k, str) and v in ("skipped", "saved")
    }


def resolve_setup_step(step: str, resolution: str) -> dict:
    """Record how a first-run wizard step ended and persist it.

    Called by ``POST /api/settings/setup-steps/resolve`` when the user skips
    or saves a step. Persisting means the step is never re-prompted on a
    later launch, even after a container restart; the in-memory singleton is
    updated too so GET /api/settings agrees without a reload.
    """
    merged = {**normalize_setup_steps(settings.SETUP_STEPS), step: resolution}
    # Persist first: if the data volume rejects the write the in-memory state
    # must not claim a step is resolved (it would silently vanish on restart).
    save_settings_overrides({"SETUP_STEPS": merged})
    settings.SETUP_STEPS = merged
    logger.info("Setup wizard step %r resolved as %r", step, resolution)
    return merged


def llm_looks_configured() -> bool:
    """Whether the LLM integration appears set up already.

    True when a key is configured, the wizard's AI step was saved once, or
    the base URL / model differ from the built-in defaults - i.e. someone
    (env vars, compose, Settings) deliberately pointed HomeStew at an LLM
    server and the first-run wizard should not offer to do it again.
    """
    if (settings.LLM_API_KEY or "").strip():
        return True
    if (settings.SETUP_STEPS or {}).get("llm") == "saved":
        return True
    fields = Settings.model_fields
    if str(settings.LLM_BASE_URL).strip() != str(fields["LLM_BASE_URL"].default):
        return True
    if str(settings.LLM_MODEL).strip() != str(fields["LLM_MODEL"].default):
        return True
    return False


# Apply any persisted overrides immediately after the singleton is created.
# Must come after normalize_setup_steps(), which load_settings_overrides()
# uses to sanitize a stored SETUP_STEPS value at boot.
load_settings_overrides()


def _reload_secrets_from_disk() -> None:
    """Re-read settings.json with the current store after a key change.

    The in-memory secret values were produced by the previous store, so a
    create/delete must not leave them stale: re-running the load path swaps
    in what the new key can actually read (plaintext secrets when there is
    no key; nothing decryptable after deleting one) and refreshes the
    ``secrets_encrypted`` / ``unreadable_secrets`` status flags the UI shows.
    """
    for key in SECRET_SETTINGS:
        setattr(settings, key, "")
    # The early-return paths of load_settings_overrides (no settings.json)
    # keep the flags as they are, so reset them here first.
    settings.unreadable_secrets = ()
    load_settings_overrides()


def create_master_key_runtime() -> tuple[bool, str]:
    """Create the HomeStew-managed master key while running (wizard / UI).

    Returns ``(ok, reason)``: ``reason`` is ``"created"`` when a fresh key
    was written and picked up, ``"exists"`` when a usable key is already in
    place (mounted or managed - never overwritten), ``"mounted"`` when a
    mounted key file exists but is unreadable (fix the file, not the app),
    or ``"failed"`` when the data directory rejected the write.
    """
    global _secret_store
    if _secret_store.available:
        return False, "exists"
    auto_path = settings.DATA_DIR / AUTO_KEY_FILENAME
    if auto_path.exists():
        # A managed key file is there but unreadable/corrupt - do not touch
        # it; deleting it is an explicit Advanced action.
        return False, "failed"
    if settings.SECRETS_KEY_FILE and Path(settings.SECRETS_KEY_FILE).exists():
        # The mount exists but the content was rejected at boot: creating a
        # second key would silently bypass the user's own key management.
        return False, "mounted"
    key = create_generated_master_key(settings.DATA_DIR)
    if key is None:
        return False, "failed"
    _secret_store = SecretStore(key)
    _secret_store.key_source = "generated"
    # Secrets saved while keyless sit on disk as plaintext: the reload picks
    # them up and load_settings_overrides() re-encrypts them with the new key.
    _reload_secrets_from_disk()
    logger.info("Master key created at %s; secrets are encrypted at rest.", auto_path)
    return True, "created"


def delete_master_key_runtime() -> tuple[bool, str]:
    """Delete the HomeStew-managed master key while running (Advanced UI).

    Troubleshooting/rotation aid: after deleting, stored ciphertext can no
    longer be decrypted - reload treats it as unset and the Settings banner
    reports it until a new key is created and secrets are re-entered. A
    mounted key file (SECRETS_KEY_FILE) is outside HomeStew's management and
    never removed here.

    Returns ``(ok, reason)``: ``"deleted"``, ``"mounted"`` (a mounted key
    file is in use - remove the mount instead), or ``"none"`` (no managed
    key to delete).
    """
    global _secret_store
    if _secret_store.key_source == "mounted":
        return False, "mounted"
    auto_path = settings.DATA_DIR / AUTO_KEY_FILENAME
    if not auto_path.exists():
        # Nothing on disk: make sure the runtime state agrees anyway.
        if _secret_store.available:
            _secret_store = SecretStore(None)
            _reload_secrets_from_disk()
        return False, "none"
    try:
        auto_path.unlink()
    except OSError as exc:
        logger.error("Could not delete the master key at %s: %s", auto_path, exc)
        raise OSError(exc)
    _secret_store = SecretStore(None)
    _reload_secrets_from_disk()
    logger.warning(
        "Master key deleted (%s). Secrets are UNENCRYPTED and previously "
        "stored ones are unreadable until a new key is created.", auto_path,
    )
    return True, "deleted"
