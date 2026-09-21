import os
import json
from typing import Optional, Union
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

def append_to_history(entry: dict, user_id: Optional[Union[int, str]] = None):
    """Appends completed class record to user-partitioned history or legacy global history."""
    if user_id is not None:
        from .database import get_user_logs_dir
        history_file = os.path.join(get_user_logs_dir(user_id), "class_history.json")
    else:
        history_file = HISTORY_FILE

    history = []
    if os.path.exists(history_file) and os.path.getsize(history_file) > 0:
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                history = json.load(f)
                if not isinstance(history, list):
                    history = []
        except Exception as e:
            logger.warning(f"SessionStatus: Failed to load history file {history_file}: {e}")
            history = []

    # Prevent duplicate identical entries
    if history and history[-1].get("subject") == entry.get("subject") and history[-1].get("ended_at") == entry.get("ended_at"):
        logger.debug("SessionStatus: History entry already exists, skipping duplicate append.")
        return

    history.append(entry)
    try:
        os.makedirs(os.path.dirname(history_file), exist_ok=True)
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
        logger.info(f"SessionStatus: Appended class to history ({history_file}): {entry.get('subject')} (status: {entry.get('status')})")
    except Exception as e:
        logger.error(f"SessionStatus: Failed to append to history log {history_file}: {e}")

def update_session_status(
    status: str, 
    screenshot: str = None, 
    join_time: str = None, 
    disconnect_time: str = None,
    subject: str = None,
    title: str = None,
    instructor: str = None,
    user_id: Optional[Union[int, str]] = None
):
    """Updates and commits runtime session status and persistent historical proof to user partitions and Redis."""
    if user_id is not None:
        current = get_user_session_status(user_id)
    else:
        current = get_session_status()

    current["status"] = status
    if user_id is not None:
        current["user_id"] = user_id

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

    # Clean screenshot filename and prefix with user_id if present
    clean_screenshot = os.path.basename(screenshot) if screenshot else None
    if clean_screenshot and user_id is not None:
        ss_rel = f"{user_id}/{clean_screenshot}"
        ss_url = f"/screenshots/{user_id}/{clean_screenshot}"
    elif clean_screenshot:
        ss_rel = clean_screenshot
        ss_url = f"/screenshots/{clean_screenshot}"
    else:
        ss_rel = None
        ss_url = None

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
        if clean_screenshot:
            current["screenshot"] = ss_rel
            current["latest_screenshot"] = ss_url
        if join_time:
            current["last_join_time"] = join_time
            current["joined_at"] = join_time.replace(" ", "T")
            current["disconnect_time"] = None
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
        if clean_screenshot:
            ls["latest_join_screenshot"] = ss_rel
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
        ls["latest_failure_screenshot"] = ss_rel
        ls["joined_at"] = None
        ls["disconnected_at"] = disconnect_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ls["session_duration"] = "N/A"
        ls["final_session_state"] = "FAILED"
        if subject:
            ls["last_completed_class"] = subject

    elif status == "DISCONNECTED":
        if disconnect_time:
            ls["disconnected_at"] = disconnect_time
        if clean_screenshot:
            ls["latest_disconnect_screenshot"] = ss_rel
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

    # Persistent log appender and SQLite recording for completed/terminal lifecycles
    if status in ("FAILED", "FACULTY_NOT_STARTED", "DISCONNECTED"):
        rec_status = "CONNECTED" if status == "DISCONNECTED" else status
        rec_joined = ls.get("joined_at") if status == "DISCONNECTED" else None
        rec_ended = (ls.get("disconnected_at") if status == "DISCONNECTED" else disconnect_time) or current["timestamp"]
        rec_duration = ls.get("session_duration") if status == "DISCONNECTED" else "N/A"

        rec_screenshots = {}
        if status == "FAILED" and ss_url:
            rec_screenshots["failure"] = ss_url
        elif status == "FACULTY_NOT_STARTED" and ss_url:
            rec_screenshots["connected"] = ss_url
        elif status == "DISCONNECTED":
            if ls.get("latest_join_screenshot"):
                rec_screenshots["connected"] = f"/screenshots/{ls.get('latest_join_screenshot')}"
            if ss_url:
                rec_screenshots["disconnect"] = ss_url

        entry = {
            "subject": current.get("subject") or subject or "Lecture",
            "title": current.get("title") or title or "Class Lecture",
            "instructor": current.get("instructor") or instructor or "N/A",
            "status": rec_status,
            "joined_at": rec_joined,
            "ended_at": rec_ended,
            "session_duration": rec_duration,
            "screenshots": rec_screenshots
        }

        if user_id is not None:
            try:
                from .database import record_attendance_session
                record_attendance_session(
                    user_id=int(user_id),
                    subject=entry["subject"],
                    title=entry["title"],
                    instructor=entry["instructor"],
                    status=entry["status"],
                    joined_at=entry["joined_at"],
                    ended_at=entry["ended_at"],
                    session_duration=entry["session_duration"],
                    screenshots=rec_screenshots
                )
            except Exception as db_att_err:
                logger.warning(f"SessionStatus: Failed to record user attendance session: {db_att_err}")
            append_to_history(entry, user_id=user_id)
        else:
            append_to_history(entry)

    # 1. User-partitioned status JSON & Redis
    if user_id is not None:
        try:
            from .database import get_user_logs_dir
            user_status_file = os.path.join(get_user_logs_dir(user_id), "session_status.json")
            os.makedirs(os.path.dirname(user_status_file), exist_ok=True)
            with open(user_status_file, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2)
        except Exception as user_status_err:
            logger.warning(f"SessionStatus: Failed to save user session status file: {user_status_err}")

        try:
            from .redis_service import redis_service
            redis_service.set_session_status(current, user_id=user_id)
        except Exception as redis_err:
            logger.debug(f"SessionStatus: Failed to cache update in Redis for user {user_id}: {redis_err}")

    # 2. Global legacy status file for backward compatibility (only update if user_id is None)
    if user_id is None:
        try:
            os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2)
            from .redis_service import redis_service
            redis_service.set_session_status(current)
        except Exception as e:
            logger.error(f"SessionStatus: Failed to persist global session status to disk: {e}")

    logger.info(f"SessionStatus: Updated connection status to {status} (user_id: {user_id}, screenshot: {ss_rel}, latest_screenshot: {current.get('latest_screenshot')})")

def get_user_session_status(user_id: Union[int, str]) -> dict:
    """Retrieves session status for a specific user from Redis or user's logs directory."""
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
    # 1. Try Redis user key first
    try:
        from .redis_service import redis_service
        cached = redis_service.get_session_status(user_id=user_id)
        if cached and isinstance(cached, dict):
            for k, v in default_state.items():
                if k not in cached:
                    cached[k] = v
            if "last_session" not in cached:
                cached["last_session"] = default_state["last_session"]
            return cached
    except Exception:
        pass

    # 2. Try user-partitioned file
    from .database import get_user_logs_dir
    user_file = os.path.join(get_user_logs_dir(user_id), "session_status.json")
    if os.path.exists(user_file) and os.path.getsize(user_file) > 0:
        try:
            with open(user_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in default_state.items():
                    if k not in data:
                        data[k] = v
                if "last_session" not in data:
                    data["last_session"] = default_state["last_session"]
                else:
                    for k, v in default_state["last_session"].items():
                        if k not in data["last_session"]:
                            data["last_session"][k] = v
                return data
        except Exception:
            pass

    # 3. Fresh default state isolated to this user (never leak another user's global status)
    return default_state
