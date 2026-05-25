import os
import json
from datetime import datetime
from .logger import logger

CACHE_FILE = "logs/timetable_cache.json"
SYNC_FILE = "logs/sync_request.json"

class TimetableCache:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(TimetableCache, cls).__new__(cls, *args, **kwargs)
            cls._instance.classes = []
            cls._instance.last_fetch_date = None
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
                    logger.info(f"Cache: Loaded {len(self.classes)} classes from disk for date {self.last_fetch_date}.")
            except Exception as e:
                logger.warning(f"Cache: Failed to load from disk: {e}")

    def save_to_disk(self):
        """Persists today's class list to disk."""
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "date": self.last_fetch_date,
                    "classes": self.classes
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

    def set_timetable(self, classes_list):
        """Sets today's class list and immediately flushes to disk."""
        from datetime import timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        self.classes = classes_list
        self.last_fetch_date = datetime.now(IST).strftime("%Y-%m-%d")
        self.save_to_disk()

    def mark_stale(self):
        """Forcibly voids the cache date to trigger a new unified fetch session."""
        self.last_fetch_date = None
        self.classes = []
        if os.path.exists(CACHE_FILE):
            try:
                os.remove(CACHE_FILE)
            except Exception:
                pass
        logger.info("Cache: Marked as stale/void.")

    @staticmethod
    def is_sync_requested() -> bool:
        """Checks if a user triggered a manual refresh request from the dashboard."""
        if os.path.exists(SYNC_FILE):
            try:
                with open(SYNC_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("sync_requested", False)
            except Exception:
                pass
        return False

    @staticmethod
    def set_sync_request(requested: bool):
        """Toggles the cross-process manual sync refresh flag."""
        try:
            os.makedirs(os.path.dirname(SYNC_FILE), exist_ok=True)
            with open(SYNC_FILE, "w", encoding="utf-8") as f:
                json.dump({"sync_requested": requested}, f)
        except Exception as e:
            logger.error(f"Cache: Failed to write sync request flag: {e}")

# Global instance for thread-safe ease of use
timetable_cache = TimetableCache()
