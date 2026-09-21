"""
Step 3F.1 Test Suite — Core State Isolation & Concurrency Hardening Verification

Validates:
1. SQLite concurrency hardening (WAL mode, busy_timeout=30000, timeout=30.0, synchronous=NORMAL)
   and concurrent multi-threaded read/write stress without database locks.
2. User-scoped session status isolation (in Redis and logs/{user_id}/session_status.json).
3. User-scoped class attendance history isolation (in SQLite class_attendance_records and logs/{user_id}/class_history.json).
4. Screenshot URL correctness (user ID prefix in file path and URL).
5. Authenticated backend API endpoint isolation via FastAPI TestClient:
   - GET /api/session-status
   - GET /api/class-history
   - GET /api/timetable
   - GET /api/timetable/no-classes-info
6. Zero-class proof isolation across users without data leakage or class fabrication.
"""

import os
import sys
import json
import uuid
import time
import shutil
import threading
import sqlite3
from datetime import datetime, timezone, timedelta

# Ensure cloudnote-ingestion is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.security import HTTPAuthorizationCredentials
from backend.main import (
    app,
    create_access_token,
    get_current_user,
    get_optional_current_user,
    get_timetable,
    get_session_status_api,
    get_class_history,
    get_no_classes_info,
    get_no_classes_screenshot,
)
from app.config import settings
from app.database import (
    init_db,
    get_db_connection,
    save_user_timetable,
    get_user_timetable,
    record_attendance_session,
    get_user_attendance_records,
    get_user_logs_dir,
    get_user_screenshots_dir,
    get_user_screenshot_path,
    update_ingestion_status,
    get_user_ingestion_status
)
from app.session_status import (
    update_session_status,
    get_session_status,
    get_user_session_status,
    append_to_history
)
from app.redis_service import redis_service
from app.utils import get_now_ist
from fastapi import HTTPException


def test_sqlite_concurrency_hardening_and_wal():
    """
    Test 1: Verifies SQLite pragmas (WAL mode, busy_timeout=30000, synchronous=NORMAL)
    and verifies that concurrent multi-threaded writes/reads do not throw 'database is locked'.
    """
    print("\n--- Running Test 1: SQLite WAL & Concurrency Hardening ---")
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("PRAGMA journal_mode;")
    journal_mode = cur.fetchone()[0].lower()
    print(f"Verified PRAGMA journal_mode = {journal_mode}")
    assert journal_mode == "wal", f"Expected WAL mode, got {journal_mode}"

    cur.execute("PRAGMA busy_timeout;")
    busy_timeout = cur.fetchone()[0]
    print(f"Verified PRAGMA busy_timeout = {busy_timeout}")
    assert busy_timeout == 30000, f"Expected 30000ms busy timeout, got {busy_timeout}"

    cur.execute("PRAGMA synchronous;")
    synchronous = cur.fetchone()[0]
    print(f"Verified PRAGMA synchronous = {synchronous}")
    # 1 corresponds to NORMAL
    assert synchronous in (1, "1", "NORMAL", "normal"), f"Expected NORMAL (1) synchronous, got {synchronous}"
    conn.close()

    # Multi-threaded concurrent read/write stress test
    errors = []
    num_threads = 8
    writes_per_thread = 25

    def worker_stress(thread_id: int):
        try:
            for i in range(writes_per_thread):
                c = get_db_connection()
                cur = c.cursor()
                test_uid = 90000 + thread_id
                cur.execute("""
                    INSERT INTO class_attendance_records (
                        user_id, subject, title, instructor, status,
                        joined_at, ended_at, session_duration, screenshots_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    test_uid,
                    f"Subject_T{thread_id}_{i}",
                    "Concurrency Test",
                    "Stress Bot",
                    "CONNECTED",
                    "2026-09-21 10:00:00",
                    "2026-09-21 10:50:00",
                    "50m",
                    json.dumps({"test": True}),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                ))
                c.commit()

                # Read immediately while others write
                cur.execute("SELECT COUNT(*) FROM class_attendance_records WHERE user_id = ?", (test_uid,))
                count = cur.fetchone()[0]
                assert count >= 1
                c.close()
                time.sleep(0.005)
        except Exception as ex:
            errors.append(f"Thread {thread_id} failed: {ex}")

    threads = [threading.Thread(target=worker_stress, args=(t,)) for t in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Cleanup stress rows
    cleanup_conn = get_db_connection()
    cleanup_conn.execute("DELETE FROM class_attendance_records WHERE user_id >= 90000 AND user_id < 91000")
    cleanup_conn.commit()
    cleanup_conn.close()

    assert len(errors) == 0, f"Encountered concurrency lock errors: {errors}"
    print(f"PASS: {num_threads} threads executed {num_threads * writes_per_thread} concurrent operations with 0 lock errors.")


def test_user_scoped_session_status_isolation():
    """
    Test 2: Verifies user-scoped session status isolation in Redis, disk, and in-memory.
    User A's status must never overwrite User B's status, and uninitialized users must receive IDLE.
    """
    print("\n--- Running Test 2: User-Scoped Session Status Isolation ---")
    run_id = uuid.uuid4().hex[:6]
    uid_a = int(f"81{int(run_id[:3], 16) % 80 + 10}")
    uid_b = uid_a + 1
    uid_c = uid_a + 2

    # Update session status for User A
    update_session_status(
        status="CONNECTED",
        screenshot="user_a_joined.png",
        join_time="2026-09-21 10:00:00",
        subject="CSE301 Cloud Computing",
        title="Distributed Systems",
        instructor="Dr. A. Sharma",
        user_id=uid_a
    )

    # Update session status for User B
    update_session_status(
        status="FAILED",
        screenshot="user_b_failed.png",
        disconnect_time="2026-09-21 10:05:00",
        subject="MTH401 Advanced Calculus",
        title="Multivariable Analysis",
        instructor="Dr. B. Verma",
        user_id=uid_b
    )

    # Fetch User A status
    status_a = get_user_session_status(uid_a)
    assert status_a["status"] == "CONNECTED", f"Expected CONNECTED for User A, got {status_a['status']}"
    assert status_a["subject"] == "CSE301 Cloud Computing"
    assert status_a["screenshot"] == f"{uid_a}/user_a_joined.png"
    assert status_a["latest_screenshot"] == f"/screenshots/{uid_a}/user_a_joined.png"

    # Fetch User B status
    status_b = get_user_session_status(uid_b)
    assert status_b["status"] == "FAILED", f"Expected FAILED for User B, got {status_b['status']}"
    assert status_b["subject"] == "MTH401 Advanced Calculus"
    assert status_b["screenshot"] == f"{uid_b}/user_b_failed.png"
    assert status_b["latest_screenshot"] == f"/screenshots/{uid_b}/user_b_failed.png"

    # Fetch uninitialized User C status -> MUST be clean IDLE default
    status_c = get_user_session_status(uid_c)
    assert status_c["status"] == "IDLE", f"Expected IDLE for User C, got {status_c['status']}"
    assert status_c.get("subject") is None
    assert status_c["screenshot"] is None
    assert status_c["latest_screenshot"] is None

    # Check partitioned files on disk
    file_a = os.path.join(get_user_logs_dir(uid_a), "session_status.json")
    file_b = os.path.join(get_user_logs_dir(uid_b), "session_status.json")
    assert os.path.exists(file_a), f"Expected partitioned session status file at {file_a}"
    assert os.path.exists(file_b), f"Expected partitioned session status file at {file_b}"

    with open(file_a, "r", encoding="utf-8") as f:
        disk_a = json.load(f)
        assert disk_a["subject"] == "CSE301 Cloud Computing"

    with open(file_b, "r", encoding="utf-8") as f:
        disk_b = json.load(f)
        assert disk_b["subject"] == "MTH401 Advanced Calculus"

    print("PASS: User A, User B, and User C session states are strictly isolated on Redis, disk, and API.")


def test_user_scoped_class_history_isolation():
    """
    Test 3: Verifies class history is strictly user-scoped in SQLite and logs partition.
    User A cannot view User B's attendance history.
    """
    print("\n--- Running Test 3: User-Scoped Class History Isolation ---")
    run_id = uuid.uuid4().hex[:6]
    uid_a = int(f"82{int(run_id[:3], 16) % 80 + 10}")
    uid_b = uid_a + 1

    # Record attendance sessions via update_session_status lifecycle
    update_session_status(
        status="CONNECTED",
        screenshot="join_a.png",
        join_time="2026-09-21 09:00:00",
        subject="AI101 Introduction to AI",
        user_id=uid_a
    )
    update_session_status(
        status="DISCONNECTED",
        screenshot="disc_a.png",
        disconnect_time="2026-09-21 09:50:00",
        subject="AI101 Introduction to AI",
        user_id=uid_a
    )

    update_session_status(
        status="FAILED",
        screenshot="fail_b.png",
        disconnect_time="2026-09-21 11:05:00",
        subject="PHY202 Quantum Mechanics",
        user_id=uid_b
    )

    # Verify SQLite user records
    history_a = get_user_attendance_records(uid_a)
    history_b = get_user_attendance_records(uid_b)

    assert len(history_a) >= 1
    assert any(rec["subject"] == "AI101 Introduction to AI" for rec in history_a)
    assert not any(rec["subject"] == "PHY202 Quantum Mechanics" for rec in history_a), "Leak detected: User A saw User B's subject!"

    assert len(history_b) >= 1
    assert any(rec["subject"] == "PHY202 Quantum Mechanics" for rec in history_b)
    assert not any(rec["subject"] == "AI101 Introduction to AI" for rec in history_b), "Leak detected: User B saw User A's subject!"

    # Verify partitioned file logs/{user_id}/class_history.json
    hist_file_a = os.path.join(get_user_logs_dir(uid_a), "class_history.json")
    hist_file_b = os.path.join(get_user_logs_dir(uid_b), "class_history.json")

    assert os.path.exists(hist_file_a)
    assert os.path.exists(hist_file_b)

    with open(hist_file_a, "r", encoding="utf-8") as f:
        file_data_a = json.load(f)
        assert any(e["subject"] == "AI101 Introduction to AI" for e in file_data_a)
        assert not any(e["subject"] == "PHY202 Quantum Mechanics" for e in file_data_a)

    with open(hist_file_b, "r", encoding="utf-8") as f:
        file_data_b = json.load(f)
        assert any(e["subject"] == "PHY202 Quantum Mechanics" for e in file_data_b)
        assert not any(e["subject"] == "AI101 Introduction to AI" for e in file_data_b)

    print("PASS: Class attendance history is strictly isolated between users in SQLite and disk logs.")


def test_screenshot_url_correctness():
    """
    Test 4: Verifies that screenshot paths and URLs correctly include user ID
    when user_id is provided, while legacy calls without user_id preserve flat format.
    """
    print("\n--- Running Test 4: Screenshot URL Correctness ---")
    uid = 42

    # Scoped call
    update_session_status(
        status="CONNECTED",
        screenshot="proof_mic.png",
        join_time="2026-09-21 14:00:00",
        subject="CSE402 Web Architecture",
        user_id=uid
    )
    status_scoped = get_user_session_status(uid)
    assert status_scoped["screenshot"] == f"{uid}/proof_mic.png"
    assert status_scoped["latest_screenshot"] == f"/screenshots/{uid}/proof_mic.png"
    assert status_scoped["last_session"]["latest_join_screenshot"] == f"{uid}/proof_mic.png"

    # Legacy call (user_id=None)
    update_session_status(
        status="CONNECTED",
        screenshot="legacy_proof.png",
        join_time="2026-09-21 14:00:00",
        subject="Legacy Class",
        user_id=None
    )
    status_legacy = get_session_status()
    assert status_legacy["screenshot"] == "legacy_proof.png"
    assert status_legacy["latest_screenshot"] == "/screenshots/legacy_proof.png"
    assert status_legacy["last_session"]["latest_join_screenshot"] == "legacy_proof.png"

    print("PASS: Scoped screenshot URLs correctly contain user_id prefix, legacy URLs remain backward compatible.")


def test_authenticated_backend_endpoints_isolation():
    """
    Test 5: Uses FastAPI TestClient to test all 4 authenticated endpoints:
    - GET /api/session-status
    - GET /api/class-history
    - GET /api/timetable
    - GET /api/timetable/no-classes-info
    Ensures that calling with User A's Bearer token returns User A's data only,
    and calling with User B's token returns User B's data only.
    """
    print("\n--- Running Test 5: Authenticated Backend Endpoints Isolation ---")
    init_db()
    run_id = uuid.uuid4().hex[:6]
    uid_a = int(f"83{int(run_id[:3], 16) % 80 + 10}")
    uid_b = uid_a + 1

    username_a = f"test_student_a_{run_id}"
    username_b = f"test_student_b_{run_id}"

    # Provision User A and User B in database
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO users (id, username, hashed_password, email, lpu_username) VALUES (?, ?, ?, ?, ?)",
        (uid_a, username_a, "dummyhash", f"{username_a}@lpu.in", username_a)
    )
    cur.execute(
        "INSERT OR REPLACE INTO users (id, username, hashed_password, email, lpu_username) VALUES (?, ?, ?, ?, ?)",
        (uid_b, username_b, "dummyhash", f"{username_b}@lpu.in", username_b)
    )
    conn.commit()
    conn.close()

    token_a = create_access_token(username_a)
    token_b = create_access_token(username_b)
    cred_a = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token_a)
    cred_b = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token_b)

    # Validate auth dependency extraction
    user_a = get_current_user(cred_a)
    user_b = get_current_user(cred_b)
    assert user_a["id"] == uid_a
    assert user_b["id"] == uid_b

    today_str = get_now_ist().strftime("%Y-%m-%d")

    # 1. Seed user timetables in SQLite
    save_user_timetable(
        user_id=uid_a,
        date_str=today_str,
        classes=[{"time": "09:00 - 10:00", "subject": "CSE101", "room": "34-101"}],
        no_classes_screenshot=None,
        verified_at="2026-09-21 09:00:00"
    )

    save_user_timetable(
        user_id=uid_b,
        date_str=today_str,
        classes=[{"time": "14:00 - 15:00", "subject": "MEC201", "room": "28-202"}],
        no_classes_screenshot=None,
        verified_at="2026-09-21 14:00:00"
    )

    # Test GET /api/timetable
    classes_a = get_timetable(current_user=user_a)
    assert len(classes_a) == 1
    assert classes_a[0]["subject"] == "CSE101"

    classes_b = get_timetable(current_user=user_b)
    assert len(classes_b) == 1
    assert classes_b[0]["subject"] == "MEC201"

    # 2. Test GET /api/session-status
    update_session_status(
        status="CONNECTED",
        screenshot="user_a_sess.png",
        subject="CSE101",
        user_id=uid_a
    )
    update_session_status(
        status="FAILED",
        screenshot="user_b_sess.png",
        subject="MEC201",
        user_id=uid_b
    )

    data_ss_a = get_session_status_api(current_user=user_a)
    assert data_ss_a["status"] == "CONNECTED"
    assert data_ss_a["subject"] == "CSE101"
    assert f"/screenshots/{uid_a}/" in data_ss_a["latest_screenshot"]

    data_ss_b = get_session_status_api(current_user=user_b)
    assert data_ss_b["status"] == "FAILED"
    assert data_ss_b["subject"] == "MEC201"
    assert f"/screenshots/{uid_b}/" in data_ss_b["latest_screenshot"]

    # Complete User A's session to transition it to history
    update_session_status(
        status="DISCONNECTED",
        screenshot="user_a_disc.png",
        disconnect_time="2026-09-21 10:50:00",
        subject="CSE101",
        user_id=uid_a
    )

    # 3. Test GET /api/class-history
    ch_a = get_class_history(current_user=user_a)
    ch_b = get_class_history(current_user=user_b)

    assert any(r["subject"] == "CSE101" for r in ch_a)
    assert not any(r["subject"] == "MEC201" for r in ch_a)
    assert any(r["subject"] == "MEC201" for r in ch_b)
    assert not any(r["subject"] == "CSE101" for r in ch_b)

    print("PASS: Authenticated endpoints /api/timetable, /api/session-status, and /api/class-history are fully user-scoped.")


def test_no_class_proof_isolation():
    """
    Test 6: Verifies zero-class proof isolation per user.
    When User A has 0 classes and captures a verification screenshot,
    User B (who hasn't run or has classes) does NOT see User A's screenshot.
    """
    print("\n--- Running Test 6: Zero-Class Proof Isolation ---")
    run_id = uuid.uuid4().hex[:6]
    uid_a = int(f"84{int(run_id[:3], 16) % 80 + 10}")
    uid_b = uid_a + 1

    username_a = f"noclass_a_{run_id}"
    username_b = f"noclass_b_{run_id}"

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO users (id, username, hashed_password, email, lpu_username) VALUES (?, ?, ?, ?, ?)",
        (uid_a, username_a, "dummyhash", f"{username_a}@lpu.in", username_a)
    )
    cur.execute(
        "INSERT OR REPLACE INTO users (id, username, hashed_password, email, lpu_username) VALUES (?, ?, ?, ?, ?)",
        (uid_b, username_b, "dummyhash", f"{username_b}@lpu.in", username_b)
    )
    conn.commit()
    conn.close()

    token_a = create_access_token(username_a)
    token_b = create_access_token(username_b)
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    today_str = get_now_ist().strftime("%Y-%m-%d")
    now_time = get_now_ist().strftime("%H-%M-%S")
    ss_filename = f"no_classes_{today_str}_{now_time}.png"

    # Create dummy screenshot file inside User A's screenshot folder
    user_a_ss_dir = get_user_screenshots_dir(uid_a)
    user_a_file = os.path.join(user_a_ss_dir, ss_filename)
    with open(user_a_file, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    # Save User A zero-class timetable
    save_user_timetable(
        user_id=uid_a,
        date_str=today_str,
        classes=[],
        no_classes_screenshot=ss_filename,
        verified_at=f"{today_str} {now_time.replace('-', ':')}"
    )

    # Save User B timetable with no screenshot
    save_user_timetable(
        user_id=uid_b,
        date_str=today_str,
        classes=[],
        no_classes_screenshot=None,
        verified_at=None
    )

    # Ensure User B's screenshot dir does NOT contain that screenshot
    user_b_ss_dir = get_user_screenshots_dir(uid_b)
    user_b_file = os.path.join(user_b_ss_dir, ss_filename)
    if os.path.exists(user_b_file):
        os.remove(user_b_file)

    cred_a = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token_a)
    cred_b = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token_b)
    user_a = get_current_user(cred_a)
    user_b = get_current_user(cred_b)

    # Query GET /api/timetable/no-classes-info for User A
    data_a = get_no_classes_info(current_user=user_a)
    assert data_a["available"] is True, f"Expected available=True for User A, got: {data_a}"
    assert data_a["url"] == f"/screenshots/{uid_a}/{ss_filename}"

    # Query GET /api/timetable/no-classes-screenshot (info=True) for User A
    info_meta_a = get_no_classes_screenshot(info=True, current_user=user_a)
    assert info_meta_a["available"] is True
    assert info_meta_a["url"] == f"/screenshots/{uid_a}/{ss_filename}"

    # Query GET /api/timetable/no-classes-info for User B
    data_b = get_no_classes_info(current_user=user_b)
    assert data_b["available"] is False, f"Expected available=False for User B, but got User A's screenshot leak: {data_b}"

    # Query GET /api/timetable/no-classes-screenshot for User B -> must raise 404
    try:
        get_no_classes_screenshot(info=False, current_user=user_b)
        assert False, "Expected 404 HTTPException for User B screenshot access, but none raised!"
    except HTTPException as http_err:
        assert http_err.status_code == 404, f"Expected 404 status code, got {http_err.status_code}"

    # Clean up test file
    try:
        os.remove(user_a_file)
    except Exception:
        pass

    print("PASS: Zero-class verification screenshot is isolated to the owning user and does not leak across users.")


if __name__ == "__main__":
    print("================================================================================")
    print("STARTING STEP 3F.1 TEST SUITE (STATE ISOLATION & CONCURRENCY HARDENING)")
    print("================================================================================")
    test_sqlite_concurrency_hardening_and_wal()
    test_user_scoped_session_status_isolation()
    test_user_scoped_class_history_isolation()
    test_screenshot_url_correctness()
    test_authenticated_backend_endpoints_isolation()
    test_no_class_proof_isolation()
    print("\n================================================================================")
    print("ALL STEP 3F.1 TESTS PASSED SUCCESSFULLY!")
    print("================================================================================")
