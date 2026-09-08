"""Configuration management for HomeBrain."""
import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings


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
    LLM_API_KEY: str = "changeme"
    LLM_MODEL: str = "llama3.2"
    
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
