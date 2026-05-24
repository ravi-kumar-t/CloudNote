import os
import json
from datetime import datetime
from .logger import logger

STATUS_FILE = "logs/session_status.json"

def get_session_status():
    """Retrieves the persisted class session connection status and screenshot metadata."""
    default_state = {
        "status": "IDLE",
        "last_join_time": None,
        "disconnect_time": None,
        "screenshot": None,
        "last_session": {
            "latest_join_screenshot": None,
            "latest_disconnect_screenshot": None,
            "latest_failure_screenshot": None,
            "joined_at": None,
            "disconnected_at": None,
            "session_duration": None,
            "last_completed_class": None,
            "final_session_state": None
        }
    }
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Safeguard: merge with default_state to ensure all keys exist
                if "last_session" not in data:
                    data["last_session"] = default_state["last_session"]
                else:
                    # Deep merge default keys to prevent any missing keys inside last_session
                    for k, v in default_state["last_session"].items():
                        if k not in data["last_session"]:
                            data["last_session"][k] = v
                return data
        except Exception as e:
            logger.warning(f"SessionStatus: Failed to load cache file: {e}")
            
    return default_state

def reset_session_status():
    """Resets the persistent session status to a clean IDLE state on application startup while preserving last_session history."""
    try:
        current = get_session_status()
        current["status"] = "IDLE"
        current["last_join_time"] = None
        current["disconnect_time"] = None
        current["screenshot"] = None
        # Keep current["last_session"] completely untouched!
        
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        logger.info("SessionStatus: Reset persistent session connection status to IDLE (preserved last session proof artifacts).")
    except Exception as e:
        logger.error(f"SessionStatus: Failed to reset session status to IDLE: {e}")

def update_session_status(
    status: str, 
    screenshot: str = None, 
    join_time: str = None, 
    disconnect_time: str = None,
    subject: str = None
):
    """Updates and commits the runtime session status and persistent historical proof to logs/session_status.json."""
    current = get_session_status()
    current["status"] = status
    
    # If transitioning to IDLE, reset active state variables but preserve history
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

    # Update historical "last_session" proof metadata persistently
    ls = current.get("last_session")
    if not ls:
        ls = {
            "latest_join_screenshot": None,
            "latest_disconnect_screenshot": None,
            "latest_failure_screenshot": None,
            "joined_at": None,
            "disconnected_at": None,
            "session_duration": None,
            "last_completed_class": None,
            "final_session_state": None
        }
        current["last_session"] = ls
        
    if status == "CONNECTED":
        if join_time:
            ls["joined_at"] = join_time
        if screenshot:
            ls["latest_join_screenshot"] = screenshot
        ls["latest_disconnect_screenshot"] = None
        ls["latest_failure_screenshot"] = None
        ls["disconnected_at"] = None
        ls["session_duration"] = "Active"
        ls["final_session_state"] = "CONNECTED"
        if subject:
            ls["last_completed_class"] = subject
            
    elif status == "FAILED":
        ls["latest_join_screenshot"] = None
        ls["latest_disconnect_screenshot"] = None
        ls["latest_failure_screenshot"] = screenshot
        ls["joined_at"] = None
        ls["disconnected_at"] = disconnect_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ls["session_duration"] = "N/A"
        ls["final_session_state"] = "FAILED"
        if subject:
            ls["last_completed_class"] = subject

    elif status == "DISCONNECTED":
        if disconnect_time:
            ls["disconnected_at"] = disconnect_time
        if screenshot:
            ls["latest_disconnect_screenshot"] = screenshot
        ls["final_session_state"] = "COMPLETED"
        
        # Calculate session duration
        joined_at = ls.get("joined_at")
        disconnected_at = ls.get("disconnected_at")
        if joined_at and disconnected_at:
            try:
                j_dt = datetime.strptime(joined_at, "%Y-%m-%d %H:%M:%S")
                d_dt = datetime.strptime(disconnected_at, "%Y-%m-%d %H:%M:%S")
                diff = d_dt - j_dt
                hours = diff.seconds // 3600
                minutes = (diff.seconds % 3600) // 60
                ls["session_duration"] = f"{hours}h {minutes}m"
            except Exception as calc_err:
                logger.warning(f"SessionStatus: Failed to calculate duration: {calc_err}")
                ls["session_duration"] = "N/A"
        else:
            ls["session_duration"] = "N/A"
            
    try:
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        logger.info(f"SessionStatus: Updated connection status to {status} (screenshot: {screenshot})")
    except Exception as e:
        logger.error(f"SessionStatus: Failed to persist session status to disk: {e}")
