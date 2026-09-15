"""Configuration management for HomeBrain."""
import json
import logging
import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings

from homebrain.default_prompts import (
    DEFAULT_CALENDAR_TOOL_DESCRIPTION,
    DEFAULT_CHAT_SYSTEM_PROMPT,
    DEFAULT_SEARCH_TOOL_DESCRIPTION,
)

logger = logging.getLogger(__name__)

# Settings that can be changed at runtime via the UI and persisted to disk.
EDITABLE_SETTINGS = (
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "CHAT_SYSTEM_PROMPT",
    "SEARCH_TOOL_DESCRIPTION",
    "CALENDAR_TOOL_DESCRIPTION",
)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # Application
    APP_NAME: str = "HomeBrain"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    
    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    
    # LLM Configuration
    LLM_BASE_URL: str = "http://localhost:11434/v1"
    # Empty by default — local servers (Ollama etc.) ignore the key, and an
    # empty value means "no API key configured" in the Settings UI.
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "llama3.2"

    # LLM prompts (editable under Settings > Advanced Settings). Defaults are
    # the built-in prompt texts from homebrain.default_prompts.
    CHAT_SYSTEM_PROMPT: str = DEFAULT_CHAT_SYSTEM_PROMPT
    SEARCH_TOOL_DESCRIPTION: str = DEFAULT_SEARCH_TOOL_DESCRIPTION
    CALENDAR_TOOL_DESCRIPTION: str = DEFAULT_CALENDAR_TOOL_DESCRIPTION
    
    # Data directories
    DATA_DIR: Path = Path("/data")
    DEVICES_DIR: Optional[Path] = None
    
    class Config:
        env_file = ".env"
        case_sensitive = True
    
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


def _overrides_path() -> Path:
    """Path to the persisted settings-override file (inside the /data volume)."""
    return settings.DATA_DIR / "settings.json"


def load_settings_overrides() -> None:
    """Apply persisted UI overrides on top of env/default values.

    Precedence: settings.json > environment variables > defaults.
    Called once at import time so overrides survive container restarts.
    """
    path = _overrides_path()
    if not path.exists():
        return
    try:
        # utf-8-sig also decodes plain UTF-8; it additionally strips a BOM,
        # which Windows editors/tools like to add and json.loads rejects.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read settings overrides from %s: %s", path, exc)
        return
    for key in EDITABLE_SETTINGS:
        if key in data and data[key] is not None:
            setattr(settings, key, data[key])
    logger.info("Loaded settings overrides from %s", path)


def save_settings_overrides(values: dict) -> None:
    """Merge the given values into the persisted override file.

    Only keys present in EDITABLE_SETTINGS are written. Existing stored
    values for keys not included here are preserved.
    """
    path = _overrides_path()
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    for key in EDITABLE_SETTINGS:
        if key in values and values[key] is not None:
            existing[key] = values[key]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


# Apply any persisted overrides immediately after the singleton is created.
load_settings_overrides()
