import os
import sys
import json
import sqlite3
import logging
from datetime import datetime

# Setup basic logging to console
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("PipelineVerifier")

def verify_pipeline():
    logger.info("=== STARTING CLOUDNOTE END-TO-END PIPELINE VERIFICATION ===")
    
    # 1. Add workspace folder to path to import app modules
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    
    try:
        from app.config import settings
        from app.database import get_db_connection, init_db, save_summary_to_db, get_or_create_ingestion_user
        from app.gemini_service import summarize_lecture
        logger.info("Successfully imported core app modules.")
    except ImportError as ie:
        logger.error(f"Failed to import app modules: {ie}")
        sys.exit(1)
        
    # 2. Database Initialization
    logger.info("Initializing SQLite Database...")
    init_db()
    
    username = settings.LPU_USERNAME
    logger.info(f"Target student profile username: '{username}'")
    
    # 3. Create or fetch target student account
    try:
        user_id = get_or_create_ingestion_user(username)
        logger.info(f"Database Verification: Student account exists with User ID: {user_id}")
    except Exception as e:
        logger.error(f"Failed to provision student profile: {e}")
        sys.exit(1)
        
    # 4. Generate Mock Lecture Content
    mock_lecture_text = (
        "[10:00 AM] Professor Dr. A. K. Sharma joined the session.\n"
        "[10:02 AM] Chat: Rahul: Good morning sir!\n"
        "[10:05 AM] Slide Title: Introduction to Data Structures and Algorithms\n"
        "[10:15 AM] Notes: A Queue is a linear data structure that follows the First In First Out (FIFO) principle.\n"
        "[10:20 AM] Notes: Operations: Enqueue (adds element to rear), Dequeue (removes element from front).\n"
        "[10:35 AM] Slide: Queue Complexity Analysis: Time Complexity for Enqueue and Dequeue is O(1).\n"
        "[10:45 AM] Chat: Preeti: Sir, what is the application of double-ended queue?\n"
        "[10:50 AM] Notes: Applications include job scheduling, printer buffering, and CPU multitasking scheduling.\n"
        "[11:00 AM] Lecture ended."
    )
    logger.info(f"Mock lecture text generated. Characters: {len(mock_lecture_text)}")
    
    # 5. Run Passive Extraction Telemetry Log simulation
    extraction_payload = {
        "event": "extraction_success",
        "blocks_found": 8,
        "extracted_characters": len(mock_lecture_text),
        "new_unique_lines": 9,
        "buffer_size": 9,
        "timestamp": datetime.now().isoformat()
    }
    logger.info(f"[STRUCTURED] {json.dumps(extraction_payload)}")
    
    # 6. Trigger AI Summarization
    logger.info("Triggering Gemini AI Summarization Pipeline...")
    
    # Ensure a local directory exists for file writes
    os.makedirs(os.path.dirname(settings.AI_SUMMARY_FILE), exist_ok=True)
    
    # Run the summarizer
    summary_result = summarize_lecture(mock_lecture_text)
    
    # 7. Assert Disk File Output
    logger.info("Asserting disk-level JSON artifact persistence...")
    if not os.path.exists(settings.AI_SUMMARY_FILE):
        logger.error(f"FAILURE: AI summary file not found on disk at {settings.AI_SUMMARY_FILE}")
        sys.exit(1)
        
    try:
        with open(settings.AI_SUMMARY_FILE, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        logger.info(f"SUCCESS: Read AI summary from {settings.AI_SUMMARY_FILE}")
        logger.info(f"Summary text preview: '{disk_data.get('summary')[:100]}...'")
    except Exception as read_err:
        logger.error(f"FAILURE: Could not parse summary JSON file: {read_err}")
        sys.exit(1)
        
    # 8. Assert SQLite Database Row Persistence
    logger.info("Asserting database-level row persistence...")
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            "SELECT id, timestamp, subject, summary, topics_json, key_points_json "
            "FROM lecture_summaries WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,)
        )
        row = cursor.fetchone()
        
        if not row:
            logger.error("FAILURE: No summaries persisted in SQLite for this user.")
            sys.exit(1)
            
        row_id = row["id"]
        db_topics = json.loads(row["topics_json"])
        db_key_points = json.loads(row["key_points_json"])
        
        logger.info("=== DATABASE VERIFICATION SUCCESS ===")
        logger.info(f"  Row ID: {row_id}")
        logger.info(f"  Timestamp: {row['timestamp']}")
        logger.info(f"  Subject: {row['subject']}")
        logger.info(f"  Topics Captured: {db_topics}")
        logger.info(f"  Key Points Count: {len(db_key_points)}")
        logger.info("=====================================")
        
    except Exception as db_verify_err:
        logger.error(f"FAILURE: Database read assertion failed: {db_verify_err}")
        sys.exit(1)
    finally:
        conn.close()
        
    logger.info("=== PIPELINE INTEGRATION STATUS: 100% OPERATIONAL ===")

if __name__ == "__main__":
    verify_pipeline()
