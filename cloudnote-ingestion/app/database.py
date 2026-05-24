import os
import sqlite3
import json
from datetime import datetime
from .logger import logger
from .config import settings

DB_PATH = "logs/cloudnote.db"

def get_db_connection():
    """Establishes connection to the shared SQLite database."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes the database schema if tables do not exist."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. Create users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                hashed_password TEXT NOT NULL,
                email TEXT NOT NULL
            )
        """)
        
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
        
        cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        if row:
            user_id = row["id"]
            conn.close()
            return user_id
            
        # Create default placeholder hashed password: pbkdf2 hash of username for MVP safety
        import hashlib
        salt = b"cloudnote_salt_123"
        hashed = hashlib.pbkdf2_hmac("sha256", username.encode("utf-8"), salt, 100000).hex()
        
        cursor.execute(
            "INSERT INTO users (username, hashed_password, email) VALUES (?, ?, ?)",
            (username, hashed, f"{username}@lpu.in")
        )
        conn.commit()
        user_id = cursor.lastrowid
        conn.close()
        logger.info(f"Database: Auto-provisioned student account for username '{username}' (User ID: {user_id}).")
        return user_id
    except Exception as e:
        logger.error(f"Database: Failed to get/create student account: {e}")
        return 1 # Fallback to user ID 1

def save_summary_to_db(username: str, summary_data: dict) -> int:
    """
    Persists a generated Gemini lecture summary into SQLite under the active user account.
    Returns the newly inserted summary row ID.
    """
    try:
        user_id = get_or_create_ingestion_user(username)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
        """, (user_id, subject, today_date))
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
            """, (user_id, timestamp, subject, summary, topics_json, key_points_json))
            row_id = cursor.lastrowid
        
        conn.commit()
        conn.close()
        
        # Structured log for DB write success
        structured_payload = {
            "event": "db_write_success",
            "username": username,
            "user_id": user_id,
            "row_id": row_id,
            "timestamp": datetime.now().isoformat()
        }
        logger.info(f"[STRUCTURED] {json.dumps(structured_payload)}")
        return row_id
    except Exception as db_err:
        # Structured log for DB write failure
        structured_payload = {
            "event": "db_write_failed",
            "username": username,
            "error": str(db_err),
            "timestamp": datetime.now().isoformat()
        }
        logger.error(f"[STRUCTURED] {json.dumps(structured_payload)}")
        raise db_err

def update_ingestion_status(status: str, details: str = "", subject: str = "", error: str = ""):
    """Updates logs/ingestion_status.json with the current worker state."""
    status_file = "logs/ingestion_status.json"
    os.makedirs(os.path.dirname(status_file), exist_ok=True)
    
    data = {
        "status": status,
        "details": details,
        "subject": subject,
        "error": error,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    
    try:
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to update ingestion status file: {e}")

# Auto-initialize database when module loaded
init_db()
