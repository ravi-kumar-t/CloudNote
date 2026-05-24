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
    
    # Runtime window (IST timezone assumed in app)
    ACTIVE_HOURS_START: int = 18  # 6:00 PM
    ACTIVE_HOURS_END: int = 23    # 11:00 PM
    MAX_SESSION_DURATION_SECONDS: int = 14400  # 4 hours
    
    # Testing/Debug
    DEBUG_SLEEP_OVERRIDE_SECONDS: Optional[int] = None
    DEBUG_MODE: bool = False
    
    # Gemini AI & Extraction Settings
    GEMINI_API_KEY: Optional[str] = None
    RAW_LECTURE_FILE: str = "logs/raw_lecture.txt"
    AI_SUMMARY_FILE: str = "logs/ai_summary.json"
    MAX_BUFFER_LINES: int = 500
    MAX_REJOIN_ATTEMPTS: int = 3
    REDIS_URL: Optional[str] = "redis://redis:6379/0"


    
settings = Settings()

# Ensure directories exist
os.makedirs(settings.LOGS_DIR, exist_ok=True)
os.makedirs(settings.SCREENSHOTS_DIR, exist_ok=True)
