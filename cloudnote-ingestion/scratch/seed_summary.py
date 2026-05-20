import os
import sys
import json
import sqlite3
from datetime import datetime

# Adjust sys.path to support importing app and backend modules from the root directory
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from app.database import get_db_connection, init_db
from backend.main import hash_password

def seed_database():
    print("=== Offline Dashboard Seeding Utility ===")
    
    # 1. Initialize SQLite tables if they do not exist
    print("Database: Initializing SQLite tables...")
    init_db()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Enable foreign key constraint support for this connection
        cursor.execute("PRAGMA foreign_keys = ON;")
        
        # 2. Reset database: Delete existing records in child first then parent
        print("Database: Clearing existing lecture summaries...")
        cursor.execute("DELETE FROM lecture_summaries")
        
        print("Database: Clearing existing users...")
        cursor.execute("DELETE FROM users")
        
        # Commit cleanup before inserting new records
        conn.commit()
        print("Database: Cleanup committed successfully.")
        
        # 3. Create mock test user 'dashboard_test_student'
        username = "dashboard_test_student"
        password = "password123"
        email = "dashboard_test_student@lpu.in"
        hashed = hash_password(password)
        
        print(f"Database: Provisioning student account '{username}'...")
        cursor.execute(
            "INSERT INTO users (username, hashed_password, email) VALUES (?, ?, ?)",
            (username, hashed, email)
        )
        user_id = cursor.lastrowid
        print(f"Database: Successfully created test user '{username}' (User ID: {user_id}).")
        
        # 4. Define realistic lecture summaries
        summaries = [
            {
                "subject": "INT219: Front-End Web Development",
                "timestamp": "2026-05-20 10:15:00",
                "summary": "This session focused on the fundamentals of the React component lifecycle and modern state management. The instructor introduced the concept of functional components vs. class components, followed by an in-depth dive into the standard state management hook, useState, and the side-effect handler hook, useEffect. Special emphasis was placed on proper dependency arrays to prevent infinite re-rendering loops during state mutations.",
                "topics": ["React Lifecycle", "useState Hook", "useEffect Hook", "Dependency Arrays"],
                "key_points": [
                    "Functional components are preferred in modern React over legacy class components",
                    "useState returns a state value and a state updater function",
                    "useEffect runs asynchronously after the render cycle completes",
                    "Omitting or incorrectly populating the dependency array is the leading cause of memory leaks and infinite rendering loops"
                ]
            },
            {
                "subject": "CSE325: Operating Systems",
                "timestamp": "2026-05-20 12:30:00",
                "summary": "Today's lecture covered CPU scheduling algorithms, focusing primarily on preemptive and non-preemptive strategies. We analyzed First-Come-First-Serve (FCFS), Shortest Job First (SJF), and Round Robin (RR) algorithms. The class engaged in a step-by-step trace of process execution tables to calculate average waiting times, turnaround times, and context switching overhead in time-sliced execution environments.",
                "topics": ["CPU Scheduling", "Round Robin", "Context Switching", "Turnaround Time"],
                "key_points": [
                    "Scheduling is critical for maintaining high CPU utilization and fairness",
                    "Round Robin uses a fixed time quantum to achieve fair preemption",
                    "Context switching introduces CPU overhead that must be balanced with the time quantum length",
                    "Average waiting time is typically minimized by Shortest Job First (SJF) but prone to starvation"
                ]
            },
            {
                "subject": "INT306: Database Management Systems",
                "timestamp": "2026-05-20 15:45:00",
                "summary": "In this database session, we explored the principles of relational database normalization up to Third Normal Form (3NF) and Boyce-Codd Normal Form (BCNF). The lecture walked through real-world scenarios where un-normalized schemas lead to insertion, deletion, and update anomalies. We practiced decomposing complex relations into smaller tables while maintaining dependency preservation and lossless join properties.",
                "topics": ["Database Normalization", "First Normal Form (1NF)", "Second Normal Form (2NF)", "Third Normal Form (3NF)", "Boyce-Codd Normal Form (BCNF)"],
                "key_points": [
                    "Normalization reduces data redundancy and eliminates data anomalies",
                    "1NF requires atomicity of attributes; 2NF requires full functional dependency on the primary key",
                    "3NF eliminates transitive dependencies; BCNF requires that every determinant be a superkey",
                    "Decomposition must always be lossless to ensure no data is lost during structural transformations"
                ]
            }
        ]
        
        # 5. Insert summaries linked to the test user ID
        for s in summaries:
            cursor.execute("""
                INSERT INTO lecture_summaries (user_id, timestamp, subject, summary, topics_json, key_points_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                s["timestamp"],
                s["subject"],
                s["summary"],
                json.dumps(s["topics"]),
                json.dumps(s["key_points"])
            ))
            summary_id = cursor.lastrowid
            print(f"Database: Inserted summary ID {summary_id} for '{s['subject']}'")
            
        conn.commit()
        print("\nDatabase: Seeding completed successfully. 3 mock summaries inserted.")
        print(f"Login Credentials:\n - Username: {username}\n - Password: {password}\n")
        
    except Exception as e:
        conn.rollback()
        print(f"Database: Error during seeding process: {e}")
        raise e
    finally:
        conn.close()

if __name__ == "__main__":
    seed_database()
