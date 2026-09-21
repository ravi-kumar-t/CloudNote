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
    DEBUG_SIMULATION_MODE: str = "SUCCESS"
    DEMO_MODE: bool = False
    BACKEND_INTERNAL_URL: str = "http://backend:8000"
    
    # Gemini AI & Extraction Settings
    GEMINI_API_KEY: Optional[str] = None
    RAW_LECTURE_FILE: str = "logs/raw_lecture.txt"
    AI_SUMMARY_FILE: str = "logs/ai_summary.json"
    MAX_BUFFER_LINES: int = 500
    MAX_REJOIN_ATTEMPTS: int = 3
    REDIS_URL: Optional[str] = "redis://redis:6379/0"

    # JWT Authentication Settings
    JWT_SECRET: str = "your_jwt_secret"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_HOURS: int = 24

    # Worker Concurrency Settings
    WORKER_MAX_CONCURRENT_JOBS: int = 1

    # Queue Retry & Capacity Settings
    QUEUE_MAX_DELIVERY_ATTEMPTS: int = 3
    QUEUE_STREAM_MAXLEN: int = 10000

    # CORS Settings
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000,http://127.0.0.1:5173"

    
settings = Settings()

# Ensure directories exist
os.makedirs(settings.LOGS_DIR, exist_ok=True)
os.makedirs(settings.SCREENSHOTS_DIR, exist_ok=True)

def get_screenshot_path(filename: str) -> str:
    """Returns absolute path to screenshot folder inside docker context (/app/screenshots) or local workspace fallback."""
    if os.path.exists("/app"):
        return f"/app/screenshots/{filename}"
    return os.path.abspath(os.path.join(settings.SCREENSHOTS_DIR, filename))

def get_user_screenshots_dir(user_id: int | str) -> str:
    """Returns absolute path to user-scoped screenshots folder (/app/screenshots/{user_id} or local)."""
    base = "/app/screenshots" if os.path.exists("/app") else os.path.abspath(settings.SCREENSHOTS_DIR)
    path = os.path.join(base, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path

def get_user_screenshot_path(user_id: int | str, filename: str) -> str:
    """Returns absolute path to a user-scoped screenshot file."""
    return os.path.join(get_user_screenshots_dir(user_id), filename)

def get_user_logs_dir(user_id: int | str) -> str:
    """Returns absolute path to user-scoped logs folder (/app/logs/{user_id} or local)."""
    base = "/app/logs" if os.path.exists("/app") else os.path.abspath(settings.LOGS_DIR)
    path = os.path.join(base, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path
