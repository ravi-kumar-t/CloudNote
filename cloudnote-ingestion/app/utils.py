from datetime import datetime, timezone, timedelta

def get_now_ist() -> datetime:
    """Returns the current timezone-naive datetime representing India Standard Time (UTC+5:30)."""
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).replace(tzinfo=None)
