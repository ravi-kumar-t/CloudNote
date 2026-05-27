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
        "latest_screenshot": None,
        "latest_event": "IDLE",
        "timestamp": None,
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
    # 1. Local JSON File first (Core MVP Stability & Manual Testing Support)
    if not os.path.exists(STATUS_FILE):
        return default_state

    if os.path.getsize(STATUS_FILE) == 0:
        return default_state

    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"SessionStatus: Failed to load cache file: {e}")
        return default_state

    # Safeguard: merge with default_state to ensure all keys exist
    for k, v in default_state.items():
        if k not in data:
            data[k] = v
    if "last_session" not in data:
        data["last_session"] = default_state["last_session"]
    else:
        # Deep merge default keys to prevent any missing keys inside last_session
        for k, v in default_state["last_session"].items():
            if k not in data["last_session"]:
                data["last_session"][k] = v
    return data

def reset_session_status():
    """Resets the persistent session status to a clean IDLE state on application startup while preserving last_session history."""
    try:
        current = get_session_status()
        current["status"] = "IDLE"
        current["last_join_time"] = None
        current["disconnect_time"] = None
        current["screenshot"] = None
        current["latest_screenshot"] = None
        current["latest_event"] = "IDLE"
        current["timestamp"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        # Keep current["last_session"] completely untouched!
        
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        logger.info("SessionStatus: Reset persistent session connection status to IDLE (preserved last session proof artifacts).")
        
        # Resilient Redis cache update (Sidecar Integration)
        try:
            from .redis_service import redis_service
            redis_service.set_session_status(current)
        except Exception as redis_err:
            logger.debug(f"SessionStatus: Failed to sync reset status to Redis: {redis_err}")
    except Exception as e:
        logger.error(f"SessionStatus: Failed to reset session status to IDLE: {e}")

HISTORY_FILE = "logs/class_history.json"

def append_to_history(entry: dict):
    history = []
    if not os.path.exists(HISTORY_FILE):
        history = []
    elif os.path.getsize(HISTORY_FILE) == 0:
        history = []
    else:
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
                if not isinstance(history, list):
                    history = []
        except Exception as e:
            logger.warning(f"SessionStatus: Failed to load history file: {e}")
            history = []
            
    # Prevent duplicate identical entries
    if history and history[-1].get("subject") == entry.get("subject") and history[-1].get("ended_at") == entry.get("ended_at"):
        logger.debug("SessionStatus: History entry already exists, skipping duplicate append.")
        return
        
    history.append(entry)
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
        logger.info(f"SessionStatus: Appended class to history: {entry.get('subject')} (status: {entry.get('status')})")
    except Exception as e:
        logger.error(f"SessionStatus: Failed to append to history log: {e}")

def update_session_status(
    status: str, 
    screenshot: str = None, 
    join_time: str = None, 
    disconnect_time: str = None,
    subject: str = None,
    title: str = None,
    instructor: str = None
):
    """Updates and commits the runtime session status and persistent historical proof to logs/session_status.json."""
    current = get_session_status()
    current["status"] = status
    
    # Track class metadata
    if subject:
        current["subject"] = subject
    if title:
        current["title"] = title
    if instructor:
        current["instructor"] = instructor
    
    # Map status to latest_event directly
    latest_event = "IDLE"
    if status == "CONNECTED":
        latest_event = "CONNECTED"
    elif status == "FAILED":
        latest_event = "FAILED"
    elif status == "DISCONNECTED":
        latest_event = "DISCONNECTED"
    elif status == "CONNECTING":
        latest_event = "CONNECTING"
    elif status == "FACULTY_NOT_STARTED":
        latest_event = "FACULTY_NOT_STARTED"
        
    current["latest_event"] = latest_event
    current["timestamp"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    
    # If transitioning to IDLE, reset active state variables but preserve history
    if status == "IDLE":
        current["last_join_time"] = None
        current["disconnect_time"] = None
        current["screenshot"] = None
        current["latest_screenshot"] = None
        current["subject"] = None
        current["title"] = None
        current["instructor"] = None
        current["joined_at"] = None
    else:
        if screenshot:
            current["screenshot"] = screenshot
            current["latest_screenshot"] = f"/screenshots/{screenshot}"
        if join_time:
            current["last_join_time"] = join_time
            current["joined_at"] = join_time.replace(" ", "T")
            current["disconnect_time"] = None  # Reset disconnect time on new join
        if disconnect_time:
            current["disconnect_time"] = disconnect_time

    # Persistent log appender for completed lifecycles
    if status == "FAILED":
        entry = {
            "subject": current.get("subject") or subject or "Lecture",
            "title": current.get("title") or "Class Lecture",
            "instructor": current.get("instructor") or "N/A",
            "status": "FAILED",
            "joined_at": None,
            "ended_at": current["timestamp"],
            "screenshots": {
                "failure": f"/screenshots/{screenshot}" if screenshot else None
            }
        }
        append_to_history(entry)
    elif status == "FACULTY_NOT_STARTED":
        entry = {
            "subject": current.get("subject") or subject or "Lecture",
            "title": current.get("title") or "Class Lecture",
            "instructor": current.get("instructor") or "N/A",
            "status": "FACULTY_NOT_STARTED",
            "joined_at": None,
            "ended_at": current["timestamp"],
            "screenshots": {
                "connected": f"/screenshots/{screenshot}" if screenshot else None
            }
        }
        append_to_history(entry)
    elif status == "DISCONNECTED":
        entry = {
            "subject": current.get("subject") or "Lecture",
            "title": current.get("title") or "Class Lecture",
            "instructor": current.get("instructor") or "N/A",
            "status": "CONNECTED", # status remains CONNECTED as per spec
            "joined_at": current.get("joined_at") or (current.get("last_join_time").replace(" ", "T") if current.get("last_join_time") else None),
            "ended_at": current["timestamp"],
            "screenshots": {
                "connected": current.get("latest_screenshot") if current.get("latest_screenshot") else None,
                "disconnect": f"/screenshots/{screenshot}" if screenshot else None
            }
        }
        append_to_history(entry)

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
        logger.info(f"SessionStatus: Updated connection status to {status} (screenshot: {screenshot}, latest_event: {latest_event}, latest_screenshot: {current['latest_screenshot']})")
    except Exception as e:
        logger.error(f"SessionStatus: Failed to persist session status to disk: {e}")

    # Resilient Redis cache update (Sidecar Integration)
    try:
        from .redis_service import redis_service
        redis_service.set_session_status(current)
    except Exception as redis_err:
        logger.debug(f"SessionStatus: Failed to cache update in Redis: {redis_err}")
