import os
import json
import tempfile
import time
from datetime import datetime
from typing import Optional, Union
from .logger import logger

CACHE_FILE = "logs/timetable_cache.json"
SYNC_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs", "sync_request.json"))

class TimetableCache:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(TimetableCache, cls).__new__(cls, *args, **kwargs)
            cls._instance.classes = []
            cls._instance.last_fetch_date = None
            cls._instance.no_classes_screenshot = None
            cls._instance.verified_at = None
            cls._instance.load_from_disk()
        return cls._instance

    def load_from_disk(self):
        """Loads cached classes from the JSON file if available."""
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.last_fetch_date = data.get("date")
                    self.classes = data.get("classes", [])
                    self.no_classes_screenshot = data.get("no_classes_screenshot")
                    self.verified_at = data.get("verified_at")
                    logger.info(f"Cache: Loaded {len(self.classes)} classes from disk for date {self.last_fetch_date}.")
            except Exception as e:
                logger.warning(f"Cache: Failed to load from disk due to corruption: {e}. Deleting corrupted cache and regenerating a clean state.")
                try:
                    if os.path.exists(CACHE_FILE):
                        os.remove(CACHE_FILE)
                except Exception as del_e:
                    logger.error(f"Cache: Failed to delete corrupted cache file: {del_e}")
                self.last_fetch_date = None
                self.classes = []
                self.no_classes_screenshot = None
                self.verified_at = None
                self.save_to_disk()

    def save_to_disk(self):
        """Persists today's class list and verification screenshot metadata to disk."""
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "date": self.last_fetch_date,
                    "classes": self.classes,
                    "no_classes_screenshot": getattr(self, "no_classes_screenshot", None),
                    "verified_at": getattr(self, "verified_at", None)
                }, f, indent=2)
            logger.info("Cache: Persisted active schedule to disk.")
        except Exception as e:
            logger.error(f"Cache: Failed to save to disk: {e}")

    def is_valid_for_today(self) -> bool:
        """Checks if the cached data is fresh and corresponds to today's date."""
        from datetime import timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        return self.last_fetch_date == today_str

    def get_timetable(self):
        """Returns the cached timetable if it is valid for today, otherwise returns empty list."""
        from datetime import timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        if not self.is_valid_for_today():
            logger.info(f"Cache: Data is stale or missing (last fetched: {self.last_fetch_date}, today: {datetime.now(IST).strftime('%Y-%m-%d')}).")
            return []
        return self.classes

    def set_timetable(self, classes_list, no_classes_screenshot=None, verified_at=None):
        """Sets today's class list and immediately flushes to disk."""
        from datetime import timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        self.classes = classes_list
        self.last_fetch_date = datetime.now(IST).strftime("%Y-%m-%d")
        if no_classes_screenshot is not None:
            self.no_classes_screenshot = no_classes_screenshot
            self.verified_at = verified_at
        elif len(classes_list) > 0:
            self.no_classes_screenshot = None
            self.verified_at = None
        self.save_to_disk()

    def set_no_classes_screenshot(self, filename: str, verified_at: str):
        """Sets the zero-class verification screenshot filename and timestamp."""
        self.no_classes_screenshot = filename
        self.verified_at = verified_at
        self.save_to_disk()

    def get_no_classes_screenshot(self):
        """Returns (filename, verified_at) if valid for today, else (None, None)."""
        if not self.is_valid_for_today():
            return None, None
        return self.no_classes_screenshot, self.verified_at

    def mark_stale(self):
        """Forcibly voids the cache date to trigger a new unified fetch session."""
        self.last_fetch_date = None
        self.classes = []
        self.no_classes_screenshot = None
        self.verified_at = None
        if os.path.exists(CACHE_FILE):
            try:
                os.remove(CACHE_FILE)
            except Exception:
                pass
        logger.info("Cache: Marked as stale/void.")

    @staticmethod
    def get_sync_requested_user_ids() -> list:
        """Returns list of user IDs that have requested a manual sync."""
        if os.path.exists(SYNC_FILE):
            try:
                with open(SYNC_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    uids = data.get("user_ids", [])
                    if isinstance(uids, list):
                        result = []
                        for u in uids:
                            try:
                                result.append(int(u))
                            except (ValueError, TypeError):
                                pass
                        return result
            except Exception:
                pass
        return []

    @staticmethod
    def is_sync_requested(user_id: Optional[Union[int, str]] = None) -> bool:
        """
        Checks if a user triggered a manual refresh request from the dashboard.
        If user_id is provided, checks if that specific user requested a sync.
        Falls back to global sync_requested for backward compatibility if user_ids key is not present.
        If user_id is None, checks if any sync request is pending globally.
        """
        if os.path.exists(SYNC_FILE):
            try:
                with open(SYNC_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if not data.get("sync_requested", False):
                        return False
                    if user_id is None:
                        return True
                    # user_id is provided
                    if "user_ids" in data:
                        uids = data.get("user_ids", [])
                        if isinstance(uids, list):
                            try:
                                return int(user_id) in [int(x) for x in uids]
                            except (ValueError, TypeError):
                                return False
                        return False
                    # Fallback for backward compatibility if user_ids key absent
                    return True
            except Exception:
                pass
        return False

    @staticmethod
    def set_sync_request(requested: bool, user_id: Optional[Union[int, str]] = None):
        """
        Toggles or updates the cross-process manual sync refresh flag.
        If user_id is provided, tracks or removes the user from pending sync requests.
        Preserves backward compatibility with sync_requested boolean.

        Atomic write (FIX 3, Step 3F.10):
        The payload is assembled in memory and written to a temporary file in the same
        directory as SYNC_FILE, then atomically replaced via os.replace().
        This eliminates the race window where open(SYNC_FILE, 'w') truncates the file
        to 0 bytes before the write completes, which could expose concurrent readers
        to empty or partially-written JSON.
        """
        try:
            sync_dir = os.path.dirname(SYNC_FILE)
            os.makedirs(sync_dir, exist_ok=True)
            existing_data = {}
            if os.path.exists(SYNC_FILE):
                try:
                    with open(SYNC_FILE, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)
                except Exception:
                    existing_data = {}

            existing_uids = []
            if isinstance(existing_data.get("user_ids"), list):
                for x in existing_data["user_ids"]:
                    try:
                        uid_int = int(x)
                        if uid_int not in existing_uids:
                            existing_uids.append(uid_int)
                    except (ValueError, TypeError):
                        pass

            if requested:
                if user_id is not None:
                    try:
                        uid_int = int(user_id)
                        if uid_int not in existing_uids:
                            existing_uids.append(uid_int)
                    except (ValueError, TypeError):
                        pass
                payload = {"sync_requested": True, "user_ids": existing_uids}
            else:
                if user_id is not None:
                    try:
                        uid_int = int(user_id)
                        existing_uids = [x for x in existing_uids if x != uid_int]
                    except (ValueError, TypeError):
                        pass
                    still_requested = len(existing_uids) > 0
                    payload = {"sync_requested": still_requested, "user_ids": existing_uids}
                else:
                    payload = {"sync_requested": False, "user_ids": []}

            # Atomic write: build JSON in memory -> temp file -> os.replace()
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(
                    dir=sync_dir,
                    prefix=".sync_request_tmp_",
                    suffix=".json"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as tmp_f:
                        json.dump(payload, tmp_f)
                        tmp_f.flush()
                        os.fsync(tmp_f.fileno())
                except Exception:
                    # fd still open if fdopen raised; close it before cleanup
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                # Atomically replace SYNC_FILE with bounded retry for Windows file-locking contention
                replaced = False
                last_err = None
                for attempt in range(10):
                    try:
                        os.replace(tmp_path, SYNC_FILE)
                        replaced = True
                        tmp_path = None  # replaced successfully; no cleanup needed
                        break
                    except OSError as err:
                        last_err = err
                        time.sleep(0.015 * (attempt + 1))
                if not replaced and last_err:
                    raise last_err
            except Exception as atomic_err:
                logger.error(f"Cache: Atomic sync request write failed: {atomic_err}")
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                raise
        except Exception as e:
            logger.error(f"Cache: Failed to write sync request flag: {e}")

    @staticmethod
    def request_sync(user_id: Optional[Union[int, str]] = None):
        """Convenience method to request a manual sync for a user (or globally)."""
        TimetableCache.set_sync_request(True, user_id=user_id)

# Global instance for thread-safe ease of use
timetable_cache = TimetableCache()
