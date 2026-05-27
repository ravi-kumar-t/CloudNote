import os
import sys
import json
import sqlite3
import argparse
from datetime import datetime, timezone, timedelta

def get_now_ist():
    # Returns the current timezone-naive datetime representing India Standard Time (UTC+5:30).
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).replace(tzinfo=None)

def main():
    parser = argparse.ArgumentParser(description="Reset CloudNote local demo state.")
    parser.add_argument(
        "--state", "-s",
        choices=["connected", "failed", "idle"],
        default="connected",
        help="Target active session state to simulate (default: connected)"
    )
    args = parser.parse_args()
    
    today_date = get_now_ist().strftime("%Y-%m-%d")
    print(f"Resetting demo state to: {args.state.upper()} for date: {today_date}")
    
    logs_dir = "logs"
    demo_dir = os.path.join(logs_dir, "demo")
    
    # 1. Update session status JSON
    status_src = os.path.join(demo_dir, f"{args.state}_session.json")
    status_dest = os.path.join(logs_dir, "session_status.json")
    
    if not os.path.exists(status_src):
        print(f"Error: Seed file not found at {status_src}")
        sys.exit(1)
        
    with open(status_src, "r", encoding="utf-8") as f:
        status_data = json.load(f)
        
    # Dynamically update dates to today
    def replace_dates(obj):
        if isinstance(obj, dict):
            return {k: replace_dates(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [replace_dates(x) for x in obj]
        elif isinstance(obj, str):
            # Replace placeholder templates and hardcoded 2026-05-27 string
            val = obj.replace("{date}", today_date).replace("2026-05-27", today_date)
            return val
        return obj

    status_data = replace_dates(status_data)
    with open(status_dest, "w", encoding="utf-8") as f:
        json.dump(status_data, f, indent=2)
    print(f"Updated session status at: {status_dest}")

    # 2. Update class history list
    history_src = os.path.join(demo_dir, "history_demo.json")
    history_dest = os.path.join(logs_dir, "class_history.json")
    
    if os.path.exists(history_src):
        with open(history_src, "r", encoding="utf-8") as f:
            history_data = json.load(f)
        history_data = replace_dates(history_data)
        with open(history_dest, "w", encoding="utf-8") as f:
            json.dump(history_data, f, indent=2)
        print(f"Updated class history at: {history_dest}")
    else:
        print(f"Warning: History seed file not found at {history_src}")

    # 3. Update timetable cache
    timetable_src = os.path.join(demo_dir, "timetable_demo.json")
    timetable_dest = os.path.join(logs_dir, "timetable_cache.json")
    
    if os.path.exists(timetable_src):
        with open(timetable_src, "r", encoding="utf-8") as f:
            timetable_data = json.load(f)
        timetable_data = replace_dates(timetable_data)
        with open(timetable_dest, "w", encoding="utf-8") as f:
            json.dump(timetable_data, f, indent=2)
        print(f"Updated timetable cache at: {timetable_dest}")
    else:
        print(f"Warning: Timetable seed file not found at {timetable_src}")

    # 4. Update ingestion status
    ingestion_src = os.path.join(demo_dir, "ingestion_demo.json")
    ingestion_dest = os.path.join(logs_dir, "ingestion_status.json")
    
    if os.path.exists(ingestion_src):
        with open(ingestion_src, "r", encoding="utf-8") as f:
            ingestion_data = json.load(f)
        ingestion_data = replace_dates(ingestion_data)
        with open(ingestion_dest, "w", encoding="utf-8") as f:
            json.dump(ingestion_data, f, indent=2)
        print(f"Updated ingestion status at: {ingestion_dest}")
    else:
        print(f"Warning: Ingestion seed file not found at {ingestion_src}")

    # 5. Populate SQLite Database with beautiful mock summaries for all users
    db_path = os.path.join(logs_dir, "cloudnote.db")
    # Initialize DB first if it doesn't exist
    sys.path.append(os.path.abspath(os.path.dirname(__file__)))
    from app.database import init_db, get_db_connection, get_or_create_ingestion_user
    init_db()
    
    # Auto-provision some default users if none exist to make sure we can log in
    get_or_create_ingestion_user("teststudent")
    get_or_create_ingestion_user("12316665")
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get all users
        cursor.execute("SELECT id, username FROM users")
        users = cursor.fetchall()
        
        # Seed summaries for each user
        mock_summaries = [
            {
                "subject": "CSE316",
                "summary": "A detailed discussion on CPU Scheduling algorithms, process synchronization using semaphores, and memory management basics. Discussed FCFS, SJF, and Round Robin scheduling schemes, showing their advantages and tradeoffs.",
                "topics": ["CPU Scheduling", "Process Synchronization", "Semaphores"],
                "key_points": [
                    "FCFS is simple but suffers from convoy effect.",
                    "SJF is optimal but requires future CPU burst prediction.",
                    "Round Robin is pre-emptive and uses time slices."
                ]
            },
            {
                "subject": "CSE408",
                "summary": "Introduction to Dynamic Programming versus Divide and Conquer paradigms. Solved the 0/1 Knapsack problem using dynamic programming memoization and bottom-up tabulation approach. Discussed time and space complexity differences.",
                "topics": ["Dynamic Programming", "Knapsack Problem", "Time Complexity"],
                "key_points": [
                    "DP solves subproblems once and stores results in a table.",
                    "0/1 Knapsack has a pseudo-polynomial time complexity of O(nW).",
                    "Tabulation is bottom-up while memoization is top-down."
                ]
            }
        ]
        
        for u in users:
            user_id = u["id"]
            username = u["username"]
            for s in mock_summaries:
                timestamp = f"{today_date} 10:30:00" if s["subject"] == "CSE316" else f"{today_date} 12:45:00"
                
                # Check if it already exists
                cursor.execute(
                    "SELECT id FROM lecture_summaries WHERE user_id = ? AND subject = ?",
                    (user_id, s["subject"])
                )
                row = cursor.fetchone()
                
                topics_str = json.dumps(s["topics"])
                key_points_str = json.dumps(s["key_points"])
                
                if row:
                    cursor.execute(
                        "UPDATE lecture_summaries SET timestamp = ?, summary = ?, topics_json = ?, key_points_json = ? WHERE id = ?",
                        (timestamp, s["summary"], topics_str, key_points_str, row["id"])
                    )
                else:
                    cursor.execute(
                        "INSERT INTO lecture_summaries (user_id, timestamp, subject, summary, topics_json, key_points_json) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (user_id, timestamp, s["subject"], s["summary"], topics_str, key_points_str)
                    )
        
        conn.commit()
        conn.close()
        print("Successfully seeded mock summaries in SQLite database.")
    except Exception as db_err:
        print(f"Error seeding SQLite database: {db_err}")

    print("Demo reset complete! Ready for presentations.")

if __name__ == "__main__":
    main()
