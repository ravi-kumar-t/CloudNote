import os
import sqlite3
import json
from datetime import datetime
from .logger import logger
from .config import settings

from typing import Optional, Union, List, Dict, Any

DB_PATH = os.environ.get("DB_PATH", "logs/cloudnote.db")

def get_db_connection():
    """Establishes connection to the shared SQLite database with concurrency hardening."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
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
def _classes_match_identity(c1: dict, c2: dict) -> bool:
    """
    Precise class identity matching to distinguish separate occurrences of the same subject.
    Uses subject_code plus timings/start_time.
    """
    s1 = str(c1.get("subject_code") or c1.get("subject") or "").strip()
    s2 = str(c2.get("subject_code") or c2.get("subject") or "").strip()
    if not s1 or not s2 or s1 != s2:
        return False

    # Check timings / start_time
    t1_timings = c1.get("timings")
    t1_start = c1.get("start_time")
    t2_timings = c2.get("timings")
    t2_start = c2.get("start_time")

    # If both have explicit start_time, match on start_time
    if t1_start and t2_start:
        return str(t1_start).strip() == str(t2_start).strip()

    # If both have explicit timings, match on timings
    if t1_timings and t2_timings:
        return str(t1_timings).strip() == str(t2_timings).strip()

    # Cross-match if one has timings and the other has start_time
    if t1_start and t2_timings:
        if str(t1_start).strip() == str(t2_timings).strip():
            return True
    if t1_timings and t2_start:
        if str(t1_timings).strip() == str(t2_start).strip():
            return True

    # If neither has timing metadata (fallback), match on subject_code alone
    if not (t1_start or t1_timings) and not (t2_start or t2_timings):
        return True

    return False

def save_user_timetable(
    user_id: int,
    date_str: str,
    classes: list,
    no_classes_screenshot: Optional[str] = None,
    verified_at: Optional[str] = None,
    reconcile: bool = True
) -> int:
    """
    Saves or updates today's timetable for a specific user with state-reconciliation defense.
    Preserves in-flight and terminal statuses (COMPLETED, FACULTY_NOT_STARTED, FAILED, MISSED, QUEUED)
    for existing classes when a fresh portal timetable sync is ingested.
    Includes bounded retry (3 attempts, 0.1s backoff) for transient SQLite 'database is locked'.
    """
    import time
    max_retries = 3
    backoff = 0.1
    terminal_statuses = {"COMPLETED", "FACULTY_NOT_STARTED", "FAILED", "MISSED"}
    preserved_statuses = terminal_statuses | {"QUEUED"}

    for attempt in range(1, max_retries + 1):
        try:
            if reconcile:
                existing_tt = get_user_timetable(user_id, date_str)
                existing_classes = (existing_tt.get("classes") or []) if existing_tt else []

                if existing_classes:
                    reconciled = []
                    matched_existing_indices = set()

                    if classes:
                        for incoming in classes:
                            c_dict = dict(incoming)
                            matched_existing = None
                            for idx, ec in enumerate(existing_classes):
                                if _classes_match_identity(ec, c_dict):
                                    matched_existing = ec
                                    matched_existing_indices.add(idx)
                                    break

                            if matched_existing:
                                existing_status = matched_existing.get("status")
                                incoming_status = c_dict.get("status") or "UPCOMING"
                                if existing_status in terminal_statuses:
                                    c_dict["status"] = existing_status
                                elif existing_status == "QUEUED":
                                    if incoming_status not in terminal_statuses:
                                        c_dict["status"] = "QUEUED"
                            reconciled.append(c_dict)

                    # Retain any existing executed/in-flight classes omitted from incoming portal data
                    for idx, ec in enumerate(existing_classes):
                        if idx not in matched_existing_indices:
                            ec_status = ec.get("status")
                            if ec_status in preserved_statuses:
                                reconciled.append(dict(ec))

                    classes_to_save = reconciled
                else:
                    classes_to_save = classes
            else:
                classes_to_save = classes

            conn = get_db_connection()
            cursor = conn.cursor()
            from datetime import timezone, timedelta
            IST = timezone(timedelta(hours=5, minutes=30))
            updated_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            classes_json = json.dumps(classes_to_save)

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

        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < max_retries:
                logger.warning(
                    f"Database: SQLite locked in save_user_timetable (attempt {attempt}/{max_retries}), "
                    f"retrying in {backoff}s..."
                )
                time.sleep(backoff)
                continue
            logger.error(f"Database: OperationalError in save_user_timetable: {e}")
            raise e
        except Exception as e:
            logger.error(f"Database: Error in save_user_timetable: {e}")
            raise e

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
    """
    Inserts a persistent attendance history record for a user.
    Includes bounded retry (3 attempts, 0.1s backoff) for transient SQLite 'database is locked'.
    """
    import time
    import sqlite3
    max_retries = 3
    backoff = 0.1

    for attempt in range(1, max_retries + 1):
        conn = None
        try:
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
            return row_id
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < max_retries:
                logger.warning(
                    f"Database: SQLite locked in record_attendance_session (attempt {attempt}/{max_retries}), "
                    f"retrying in {backoff}s..."
                )
                time.sleep(backoff)
                continue
            logger.error(f"Database: OperationalError in record_attendance_session: {e}")
            raise e
        except Exception as e:
            logger.error(f"Database: Error in record_attendance_session: {e}")
            raise e
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

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

def get_active_portal_users() -> List[dict]:
    """
    Retrieves all active registered users configured with encrypted portal credentials.

    Policy:
    - IF registered users with encrypted credentials exist in SQLite, process those
      database users ONLY. Environment credentials are NOT used or merged.
    - IF ZERO registered portal users exist in SQLite, fall back to LPU_USERNAME / LPU_PASSWORD
      environment variables (if provided) and auto-provision the legacy ingestion account.
    """
    # Note: SQLite concurrency remains a known limitation and is deferred to a later database/concurrency step.
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, username, lpu_username, encrypted_portal_password
        FROM users
        WHERE encrypted_portal_password IS NOT NULL AND encrypted_portal_password != ''
        ORDER BY id ASC
    """)
    rows = cursor.fetchall()
    conn.close()

    if rows:
        users = []
        for r in rows:
            users.append({
                "id": r["id"],
                "username": r["username"],
                "lpu_username": r["lpu_username"] or r["username"],
                "encrypted_portal_password": r["encrypted_portal_password"]
            })
        return users

    # Fallback to legacy environment credentials only if ZERO registered portal users exist in DB
    if settings.LPU_USERNAME and settings.LPU_PASSWORD:
        legacy_uid = get_or_create_ingestion_user(settings.LPU_USERNAME)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, username, lpu_username, encrypted_portal_password FROM users WHERE id = ?",
            (legacy_uid,)
        )
        legacy_row = cursor.fetchone()
        conn.close()
        if legacy_row and legacy_row["encrypted_portal_password"]:
            return [{
                "id": legacy_row["id"],
                "username": legacy_row["username"],
                "lpu_username": legacy_row["lpu_username"] or legacy_row["username"],
                "encrypted_portal_password": legacy_row["encrypted_portal_password"]
            }]

    return []

def update_user_class_status(
    user_id: int,
    date_str: str,
    subject_code: str,
    timings: Optional[str] = None,
    new_status: str = "QUEUED"
) -> bool:
    """
    Updates the status of a matching class in user_timetables(user_id, date) to new_status.
    Enforces terminal-state invariance (COMPLETED, FACULTY_NOT_STARTED, FAILED, MISSED are immutable).
    Includes bounded retry (3 attempts, 0.1s backoff) for transient SQLite 'database is locked'.
    Returns True if successfully matched and persisted, False otherwise.
    """
    import time
    import sqlite3
    max_retries = 3
    backoff = 0.1
    terminal_statuses = {"COMPLETED", "FACULTY_NOT_STARTED", "FAILED", "MISSED"}

    for attempt in range(1, max_retries + 1):
        try:
            tt = get_user_timetable(user_id, date_str)
            if not tt or not tt.get("classes"):
                return False

            classes = tt["classes"]
            updated = False
            for c in classes:
                match_subject = c.get("subject_code") == subject_code
                match_timings = True
                if timings:
                    match_timings = (c.get("timings") == timings or c.get("start_time") == timings)
                if match_subject and match_timings:
                    current_status = c.get("status")
                    if current_status in terminal_statuses and new_status != current_status:
                        logger.warning(
                            f"Database: Refusing to overwrite terminal status '{current_status}' "
                            f"with '{new_status}' for user {user_id}, {subject_code}. "
                            f"Continuing search for later occurrences."
                        )
                        # FIX 4 (Step 3F.10): continue instead of return False so subsequent
                        # occurrences of the same subject (e.g. same subject at 14:00 when
                        # 09:00 is already COMPLETED) are still inspected and updated.
                        continue
                    c["status"] = new_status
                    updated = True
                    break

            if updated:
                save_user_timetable(
                    user_id=user_id,
                    date_str=date_str,
                    classes=classes,
                    no_classes_screenshot=tt.get("no_classes_screenshot"),
                    verified_at=tt.get("verified_at"),
                    reconcile=True
                )
                return True
            return False
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < max_retries:
                logger.warning(
                    f"Database: SQLite locked in update_user_class_status (attempt {attempt}/{max_retries}), "
                    f"retrying in {backoff}s..."
                )
                time.sleep(backoff)
                continue
            logger.error(f"Database: OperationalError in update_user_class_status: {e}")
            return False
        except Exception as e:
            logger.error(f"Database: Error in update_user_class_status: {e}")
            return False

    return False

def update_ingestion_status(
    status: str,
    details: str = "",
    subject: str = "",
    error: str = "",
    user_id: Optional[Union[int, str]] = None
):
    """Updates ingestion status with user partition support and global legacy compatibility."""
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))

    data = {
        "status": status,
        "details": details,
        "subject": subject,
        "error": error,
        "user_id": user_id,
        "timestamp": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    }

    # 1. User partition if user_id given
    if user_id is not None:
        try:
            user_status_file = os.path.join(get_user_logs_dir(user_id), "ingestion_status.json")
            os.makedirs(os.path.dirname(user_status_file), exist_ok=True)
            with open(user_status_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to update user ingestion status file: {e}")

    # 2. Global legacy file for backward compatibility
    status_file = "logs/ingestion_status.json"
    os.makedirs(os.path.dirname(status_file), exist_ok=True)
    try:
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to update ingestion status file: {e}")


def get_user_ingestion_status(user_id: Optional[Union[int, str]] = None) -> dict:
    """
    Retrieves ingestion status for a specific user or the legacy global state.

    Multi-user isolation policy (FIX 1, Step 3F.10):
    - If user_id is provided: read ONLY the user-scoped logs/{user_id}/ingestion_status.json.
      If it does not exist or cannot be read, return default_status immediately.
      NEVER fall back to the global logs/ingestion_status.json when a user context is given,
      to prevent cross-user status leakage.
    - If user_id is None: use the legacy global logs/ingestion_status.json (single-user
      backward compatibility path).
    """
    default_status = {
        "status": "idle",
        "details": "No active scraper processes registered yet.",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    if user_id is not None:
        # User-scoped path: strict isolation — no global fallback
        user_status_file = os.path.join(get_user_logs_dir(user_id), "ingestion_status.json")
        if os.path.exists(user_status_file) and os.path.getsize(user_status_file) > 0:
            try:
                with open(user_status_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        # User-scoped file absent or unreadable — return clean default, do NOT leak global state
        return default_status

    # Legacy global path: only reached when user_id is None
    status_file = "logs/ingestion_status.json"
    if os.path.exists(status_file) and os.path.getsize(status_file) > 0:
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    return default_status


# Auto-initialize database when module loaded
init_db()
