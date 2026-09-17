import os
import sqlite3
import json
from datetime import datetime
from .logger import logger
from .config import settings

from typing import Optional, Union, List, Dict, Any

DB_PATH = "logs/cloudnote.db"

def get_db_connection():
    """Establishes connection to the shared SQLite database."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# --- Storage Partitioning Helpers ---
def get_user_screenshots_dir(user_id: Union[int, str]) -> str:
    """Returns absolute path to user-scoped screenshots directory: screenshots/{user_id}."""
    base_dir = "/app/screenshots" if os.path.exists("/app/screenshots") else os.path.abspath(settings.SCREENSHOTS_DIR)
    path = os.path.join(base_dir, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path

def get_user_screenshot_path(user_id: Union[int, str], filename: str) -> str:
    """Returns absolute path to a specific screenshot inside user's directory."""
    user_dir = get_user_screenshots_dir(user_id)
    return os.path.join(user_dir, filename)

def get_user_logs_dir(user_id: Union[int, str]) -> str:
    """Returns absolute path to user-scoped logs directory: logs/{user_id}."""
    base_dir = "/app/logs" if os.path.exists("/app/logs") else os.path.abspath(settings.LOGS_DIR)
    path = os.path.join(base_dir, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path

def init_db():
    """Initializes the database schema and performs non-destructive migrations."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. Create users table if not exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                hashed_password TEXT NOT NULL,
                email TEXT NOT NULL,
                lpu_username TEXT,
                encrypted_portal_password TEXT
            )
        """)
        
        # 1b. Non-destructive migration for existing users table
        cursor.execute("PRAGMA table_info(users)")
        columns = [row["name"] for row in cursor.fetchall()]
        if "lpu_username" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN lpu_username TEXT")
            logger.info("Database: Migrated 'users' table -> added column 'lpu_username'.")
        if "encrypted_portal_password" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN encrypted_portal_password TEXT")
            logger.info("Database: Migrated 'users' table -> added column 'encrypted_portal_password'.")

        # Backfill lpu_username = username for existing records if null
        cursor.execute("UPDATE users SET lpu_username = username WHERE lpu_username IS NULL OR lpu_username = ''")

        # 2. Create lecture_summaries table with user_id foreign key
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lecture_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                subject TEXT NOT NULL,
                summary TEXT NOT NULL,
                topics_json TEXT NOT NULL,
                key_points_json TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """)

        # 3. Create user_timetables table (user-scoped timetable persistence)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_timetables (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                classes_json TEXT NOT NULL DEFAULT '[]',
                no_classes_screenshot TEXT,
                verified_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
                UNIQUE(user_id, date)
            )
        """)

        # 4. Create class_attendance_records table (user-scoped session history)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS class_attendance_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                subject TEXT NOT NULL,
                title TEXT,
                instructor TEXT,
                status TEXT NOT NULL,
                joined_at TEXT,
                ended_at TEXT,
                session_duration TEXT,
                screenshots_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """)

        conn.commit()
        conn.close()
        logger.info("Database: Successfully initialized SQLite tables.")
    except Exception as e:
        logger.error(f"Database: Failed to initialize SQLite schema: {e}")

def get_or_create_ingestion_user(username: str) -> int:
    """
    Returns the user ID corresponding to the given username.
    Provisions a default placeholder student account if it doesn't already exist.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT id, lpu_username, encrypted_portal_password FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        if row:
            user_id = row["id"]
            # Non-destructive backfill for encrypted password if settings match
            if not row["encrypted_portal_password"] and username == settings.LPU_USERNAME and settings.LPU_PASSWORD:
                try:
                    from .crypto import encrypt_portal_password
                    enc_pw = encrypt_portal_password(settings.LPU_PASSWORD, settings.JWT_SECRET)
                    cursor.execute("UPDATE users SET encrypted_portal_password = ? WHERE id = ?", (enc_pw, user_id))
                    conn.commit()
                except Exception:
                    pass
            conn.close()
            return user_id
            
        # Create default placeholder hashed password: pbkdf2 hash of username for MVP safety
        import hashlib
        salt = b"cloudnote_salt_123"
        hashed = hashlib.pbkdf2_hmac("sha256", username.encode("utf-8"), salt, 100000).hex()

        encrypted_pw = None
        if username == settings.LPU_USERNAME and settings.LPU_PASSWORD:
            try:
                from .crypto import encrypt_portal_password
                encrypted_pw = encrypt_portal_password(settings.LPU_PASSWORD, settings.JWT_SECRET)
            except Exception:
                pass

        cursor.execute(
            "INSERT INTO users (username, hashed_password, email, lpu_username, encrypted_portal_password) VALUES (?, ?, ?, ?, ?)",
            (username, hashed, f"{username}@lpu.in", username, encrypted_pw)
        )
        conn.commit()
        user_id = cursor.lastrowid
        conn.close()
        logger.info(f"Database: Auto-provisioned student account for username '{username}' (User ID: {user_id}).")
        return user_id
    except Exception as e:
        logger.error(f"Database: Failed to get/create student account: {e}")
        return 1 # Fallback to user ID 1


def save_summary_to_db(username: str, summary_data: dict, user_id: Optional[int] = None) -> int:
    """
    Persists a generated Gemini lecture summary into SQLite under the active user account.
    Returns the newly inserted summary row ID.
    Supports direct user_id specification or resolves from username.
    """
    try:
        resolved_user_id = user_id if user_id is not None else get_or_create_ingestion_user(username)

        conn = get_db_connection()
        cursor = conn.cursor()

        from datetime import timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        timestamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        subject = summary_data.get("subject", "Ingested Lecture")
        summary = summary_data.get("summary", "No summary text generated.")

        # Serialize arrays to JSON strings
        topics_json = json.dumps(summary_data.get("topics", []))
        key_points_json = json.dumps(summary_data.get("key_points", []))

        # Prevent duplicate insertions for same user, subject, and calendar day
        today_date = timestamp[0:10]
        cursor.execute("""
            SELECT id FROM lecture_summaries 
            WHERE user_id = ? AND subject = ? AND substr(timestamp, 1, 10) = ?
        """, (resolved_user_id, subject, today_date))
        existing_row = cursor.fetchone()

        if existing_row:
            row_id = existing_row["id"]
            cursor.execute("""
                UPDATE lecture_summaries 
                SET timestamp = ?, summary = ?, topics_json = ?, key_points_json = ?
                WHERE id = ?
            """, (timestamp, summary, topics_json, key_points_json, row_id))
            logger.info(f"Database: Found existing summary for '{subject}' on {today_date}. Updated row {row_id} to prevent duplicate insertion.")
        else:
            cursor.execute("""
                INSERT INTO lecture_summaries (user_id, timestamp, subject, summary, topics_json, key_points_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (resolved_user_id, timestamp, subject, summary, topics_json, key_points_json))
            row_id = cursor.lastrowid

        conn.commit()
        conn.close()

        # Structured log for DB write success
        structured_payload = {
            "event": "db_write_success",
            "username": username,
            "user_id": resolved_user_id,
            "row_id": row_id,
            "timestamp": datetime.now(IST).isoformat()
        }
        logger.info(f"[STRUCTURED] {json.dumps(structured_payload)}")
        return row_id
    except Exception as db_err:
        from datetime import timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        # Structured log for DB write failure
        structured_payload = {
            "event": "db_write_failed",
            "username": username,
            "error": str(db_err),
            "timestamp": datetime.now(IST).isoformat()
        }
        logger.error(f"[STRUCTURED] {json.dumps(structured_payload)}")
        raise db_err

# --- Multi-User Timetable & Session Data Helpers ---
def save_user_timetable(
    user_id: int,
    date_str: str,
    classes: list,
    no_classes_screenshot: Optional[str] = None,
    verified_at: Optional[str] = None
) -> int:
    """Saves or updates today's timetable for a specific user."""
    conn = get_db_connection()
    cursor = conn.cursor()
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    updated_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    classes_json = json.dumps(classes)

    cursor.execute("""
        INSERT INTO user_timetables (user_id, date, classes_json, no_classes_screenshot, verified_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, date) DO UPDATE SET
            classes_json = excluded.classes_json,
            no_classes_screenshot = excluded.no_classes_screenshot,
            verified_at = excluded.verified_at,
            updated_at = excluded.updated_at
    """, (user_id, date_str, classes_json, no_classes_screenshot, verified_at, updated_at))
    row_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return row_id

def get_user_timetable(user_id: int, date_str: str) -> Optional[dict]:
    """Retrieves timetable for a specific user and date."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, user_id, date, classes_json, no_classes_screenshot, verified_at, updated_at
        FROM user_timetables
        WHERE user_id = ? AND date = ?
    """, (user_id, date_str))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "date": row["date"],
        "classes": json.loads(row["classes_json"]),
        "no_classes_screenshot": row["no_classes_screenshot"],
        "verified_at": row["verified_at"],
        "updated_at": row["updated_at"]
    }

def record_attendance_session(
    user_id: int,
    subject: str,
    status: str,
    title: Optional[str] = None,
    instructor: Optional[str] = None,
    joined_at: Optional[str] = None,
    ended_at: Optional[str] = None,
    session_duration: Optional[str] = None,
    screenshots: Optional[dict] = None
) -> int:
    """Inserts a persistent attendance history record for a user."""
    conn = get_db_connection()
    cursor = conn.cursor()
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    created_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    screenshots_json = json.dumps(screenshots or {})

    cursor.execute("""
        INSERT INTO class_attendance_records (
            user_id, subject, title, instructor, status,
            joined_at, ended_at, session_duration, screenshots_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, subject, title, instructor, status, joined_at, ended_at, session_duration, screenshots_json, created_at))
    row_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return row_id

def get_user_attendance_records(user_id: int, limit: int = 50) -> list:
    """Retrieves attendance history records for a user, sorted newest first."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, user_id, subject, title, instructor, status,
               joined_at, ended_at, session_duration, screenshots_json, created_at
        FROM class_attendance_records
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (user_id, limit))
    rows = cursor.fetchall()
    conn.close()
    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "user_id": r["user_id"],
            "subject": r["subject"],
            "title": r["title"],
            "instructor": r["instructor"],
            "status": r["status"],
            "joined_at": r["joined_at"],
            "ended_at": r["ended_at"],
            "session_duration": r["session_duration"],
            "screenshots": json.loads(r["screenshots_json"]),
            "created_at": r["created_at"]
        })
    return result

def get_user_portal_credentials(user_id: int) -> Optional[dict]:
    """
    Retrieves user profile and decrypted LPU portal credentials for worker session.
    Returns {'user_id': int, 'username': str, 'lpu_username': str, 'portal_password': Optional[str]} or None.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, lpu_username, encrypted_portal_password FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None

    lpu_user = row["lpu_username"] or row["username"]
    enc_pw = row["encrypted_portal_password"]
    decrypted_pw = None
    if enc_pw:
        try:
            from .crypto import decrypt_portal_password
            decrypted_pw = decrypt_portal_password(enc_pw, settings.JWT_SECRET)
        except Exception as e:
            logger.warning(f"Database: Failed to decrypt portal password for user {user_id}: {e}")

    return {
        "user_id": row["id"],
        "username": row["username"],
        "lpu_username": lpu_user,
        "portal_password": decrypted_pw
    }

def set_user_portal_credentials(user_id: int, lpu_username: str, plain_password: str) -> bool:
    """Updates the user's LPU portal username and encrypted portal password."""
    try:
        from .crypto import encrypt_portal_password
        enc_pw = encrypt_portal_password(plain_password, settings.JWT_SECRET)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE users
            SET lpu_username = ?, encrypted_portal_password = ?
            WHERE id = ?
        """, (lpu_username, enc_pw, user_id))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Database: Failed to set portal credentials for user {user_id}: {e}")
        return False

def update_ingestion_status(status: str, details: str = "", subject: str = "", error: str = ""):
    """Updates logs/ingestion_status.json with the current worker state."""
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    status_file = "logs/ingestion_status.json"
    os.makedirs(os.path.dirname(status_file), exist_ok=True)
    
    data = {
        "status": status,
        "details": details,
        "subject": subject,
        "error": error,
        "timestamp": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    }
    
    try:
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to update ingestion status file: {e}")

# Auto-initialize database when module loaded
init_db()
