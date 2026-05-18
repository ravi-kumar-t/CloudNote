import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    LPU_USERNAME: str
    LPU_PASSWORD: str
    HEADLESS: bool = True
    BASE_URL: str = "https://myclass.lpu.in/"
    LOG_LEVEL: str = "INFO"
    
    # Paths
    LOGS_DIR: str = "logs"
    SCREENSHOTS_DIR: str = "screenshots"
    
    # Browser settings
    BROWSER_TIMEOUT: int = 30000  # ms
    NAVIGATION_TIMEOUT: int = 60000  # ms

settings = Settings()

# Ensure directories exist
os.makedirs(settings.LOGS_DIR, exist_ok=True)
os.makedirs(settings.SCREENSHOTS_DIR, exist_ok=True)
