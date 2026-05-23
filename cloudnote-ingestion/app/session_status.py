import os
import json
from datetime import datetime
from .logger import logger

STATUS_FILE = "logs/session_status.json"

def get_session_status():
    """Retrieves the persisted class session connection status and screenshot metadata."""
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"SessionStatus: Failed to load cache file: {e}")
            
    # Default IDLE state
    return {
        "status": "IDLE",
        "last_join_time": None,
        "disconnect_time": None,
        "screenshot": None
    }

def reset_session_status():
    """Resets the persistent session status to a clean IDLE state on application startup."""
    try:
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        idle_state = {
            "status": "IDLE",
            "last_join_time": None,
            "disconnect_time": None,
            "screenshot": None
        }
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(idle_state, f, indent=2)
        logger.info("SessionStatus: Cleaned and reset persistent session state to IDLE.")
    except Exception as e:
        logger.error(f"SessionStatus: Failed to reset session status to IDLE: {e}")

def update_session_status(status: str, screenshot: str = None, join_time: str = None, disconnect_time: str = None):
    """Updates and commits the runtime session status to logs/session_status.json."""
    current = get_session_status()
    current["status"] = status
    
    # If transitioning to IDLE, reset all state variables
    if status == "IDLE":
        current["last_join_time"] = None
        current["disconnect_time"] = None
        current["screenshot"] = None
    else:
        if screenshot:
            current["screenshot"] = screenshot
        if join_time:
            current["last_join_time"] = join_time
            current["disconnect_time"] = None  # Reset disconnect time on new join
        if disconnect_time:
            current["disconnect_time"] = disconnect_time
        
    try:
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        logger.info(f"SessionStatus: Updated connection status to {status} (screenshot: {screenshot})")
    except Exception as e:
        logger.error(f"SessionStatus: Failed to persist session status to disk: {e}")
