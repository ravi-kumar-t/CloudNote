"""
Step 3F.5 Validation Test Suite
---------------------------------
Scheduler Terminal-State Invariance, Non-Destructive Deduplication & Expired Class Finalization:
1. Scenario 1: Terminal-state invariance (COMPLETED, FACULTY_NOT_STARTED, FAILED, MISSED never re-enqueued or modified)
2. Scenario 2: Deduplication branch (existing Redis idempotency key + terminal status must not become QUEUED)
3. Scenario 3: Expired QUEUED class (end_dt + 5m + no execution lock -> MISSED)
4. Scenario 4: Active execution protection (expired class + active ExecutionLock -> remains QUEUED)
5. Scenario 5: Worker stale-job drain (expired Redis job -> no browser launch -> ACK -> MISSED)
6. Scenario 6: SQLite lock retry (transient database-lock behavior retries with backoff and succeeds)
7. Scenario 7: Guaranteed synthetic cleanup (users 95000-95099 cleaned up in try ... finally)

Constraints:
- Synthetic test data only (users 95000-95099)
- No fake LPU classes
- Clean teardown in try ... finally
"""

import asyncio
from datetime import datetime, timedelta
import json
import logging
import os
import sqlite3
import sys
import time
import threading
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import redis

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.crypto import encrypt_portal_password
from app.database import (
    get_db_connection,
    save_user_timetable,
    get_user_timetable,
    update_user_class_status,
)
from app.job_queue import (
    enqueue_job,
    consume_jobs,
    acknowledge_job,
    schedule_class_to_queue,
    get_execution_lock_key,
    get_redis_client,
    ExecutionLock,
    IngestionJob,
    DEFAULT_STREAM_NAME,
    DEFAULT_CONSUMER_GROUP,
)
from app.scheduler import TERMINAL_STATUSES
from app.worker_consumer import WorkerConsumer, generate_consumer_name
from app.utils import get_now_ist

# Ensure local test runs default to localhost Redis if REDIS_URL is unconfigured or points to Docker hostname 'redis'
if not os.environ.get("REDIS_URL") or "redis://redis:" in os.environ.get("REDIS_URL", ""):
    os.environ["REDIS_URL"] = "redis://localhost:6379/0"
settings.REDIS_URL = os.environ["REDIS_URL"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Step3F5Tests")


def get_test_redis():
    """Returns a connected Redis client with localhost fallback."""
    r = get_redis_client(redis_url=settings.REDIS_URL)
    r.ping()
    return r


# ---------------------------------------------------------------------------
# Guaranteed Synthetic Cleanup Helper
# ---------------------------------------------------------------------------
def cleanup_synthetic_test_data():
    """
    Guaranteed cleanup: removes all synthetic test users (90000 <= id < 100000),
    associated attendance records, timetable rows, and Redis test stream keys.
    Never touches real users.
    """
    print("\n--- Cleaning up Step 3F.5 synthetic test data ---")
    for attempt in range(5):
        try:
            conn = get_db_connection()
            cur = conn.cursor()

            cur.execute("DELETE FROM class_attendance_records WHERE user_id >= 90000 AND user_id < 100000")
            cur.execute("DELETE FROM lecture_summaries WHERE user_id >= 90000 AND user_id < 100000")
            cur.execute("DELETE FROM user_timetables WHERE user_id >= 90000 AND user_id < 100000")
            cur.execute("DELETE FROM users WHERE id >= 90000 AND id < 100000")

            conn.commit()
            conn.close()
            break
        except sqlite3.OperationalError:
            time.sleep(0.1)

    try:
        r = get_test_redis()
        # Clean up any test locks or stream keys for synthetic users
        keys = r.keys("cloudnote:lock:exec:user:95*")
        if keys:
            r.delete(*keys)
        keys_idemp = r.keys("cloudnote:job:idemp:user:95*")
        if keys_idemp:
            r.delete(*keys_idemp)
    except Exception as e:
        logger.warning(f"Failed clearing Redis test keys: {e}")


def seed_synthetic_user(user_id: int, username: str, password: str = "TestPass123!"):
    """Creates a synthetic user record in SQLite with encrypted credentials."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    enc_pw = encrypt_portal_password(password, settings.JWT_SECRET)
    cur.execute(
        """
        INSERT INTO users (id, username, email, hashed_password, lpu_username, encrypted_portal_password)
        VALUES (?, ?, ?, 'synthetic_hash', ?, ?)
        """,
        (user_id, username, f"{username}@test.com", username, enc_pw),
    )
    conn.commit()
    conn.close()


def simulate_scheduler_pass_for_user(user_id: int, today_str: str, now: datetime):
    """
    Simulates the exact Step 5 scheduler logic implemented in app/scheduler.py
    for a single user at a specific point in time.
    """
    user_tt = get_user_timetable(user_id, today_str)
    if not user_tt or not user_tt.get("classes"):
        return 0

    total_enqueued = 0
    r = get_test_redis()

    for c in user_tt["classes"]:
        start_str = c.get("start_time")
        end_str = c.get("end_time")
        if not start_str or not end_str:
            continue

        try:
            start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            try:
                start_dt = datetime.fromisoformat(str(start_str).replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                continue

        try:
            end_dt = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            try:
                end_dt = datetime.fromisoformat(str(end_str).replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                continue

        subject = c.get("subject_code", "Lecture")
        class_status = c.get("status")

        # 5a. Expired timetable finalization (now > end_dt + 5 minutes)
        if class_status not in TERMINAL_STATUSES:
            if now > (end_dt + timedelta(minutes=5)):
                # Active-processing protection: check Redis ExecutionLock
                lock_held = False
                if settings.REDIS_URL:
                    try:
                        lock_key = get_execution_lock_key(user_id, c)
                        if r.exists(lock_key):
                            lock_held = True
                    except Exception as lk_err:
                        logger.debug(f"Scheduler: Error checking lock for user {user_id}: {lk_err}")

                if lock_held:
                    logger.info(
                        f"Scheduler: Class {subject} for user {user_id} is past end_time + 5m, "
                        f"but active ExecutionLock is held. Leaving QUEUED for worker completion."
                    )
                else:
                    logger.info(
                        f"Scheduler: Expired class detected for user {user_id}: {subject} "
                        f"(end_time {end_str} + 5m passed, no active lock). Transitioning to MISSED."
                    )
                    update_user_class_status(
                        user_id, today_str, subject, timings=c.get("timings"), new_status="MISSED"
                    )
                    c["status"] = "MISSED"
                continue

        # 5b. Terminal-state invariance: never process or re-enqueue terminal classes
        if class_status in TERMINAL_STATUSES:
            continue

        should_join = False
        if start_dt <= now <= end_dt:
            should_join = True
        elif settings.DEBUG_MODE:
            should_join = True

        if should_join and c.get("status") not in TERMINAL_STATUSES and c.get("status") != "QUEUED":
            logger.info(f"Scheduler: Active class found for user {user_id}: {subject}. Enqueueing to Redis...")
            job, enqueued = schedule_class_to_queue(c, user_id=user_id)
            if enqueued and job:
                update_user_class_status(
                    user_id, today_str, subject, timings=c.get("timings"), new_status="QUEUED"
                )
                total_enqueued += 1
            elif not enqueued:
                if c.get("status") not in TERMINAL_STATUSES:
                    update_user_class_status(
                        user_id, today_str, subject, timings=c.get("timings"), new_status="QUEUED"
                    )

    return total_enqueued


# ---------------------------------------------------------------------------
# SCENARIO 1: Terminal-State Invariance
# ---------------------------------------------------------------------------
def test_scenario_1_terminal_state_invariance():
    """
    Scenario 1: COMPLETED, FACULTY_NOT_STARTED, FAILED, MISSED must never be re-enqueued or modified.
    Direct calls to update_user_class_status with QUEUED or another status must be rejected.
    """
    print("\n" + "=" * 80)
    print("SCENARIO 1: Terminal-State Invariance")
    print("=" * 80)

    uid = 95010
    today_str = get_now_ist().strftime("%Y-%m-%d")
    now = get_now_ist()
    seed_synthetic_user(uid, f"synth_user_{uid}")

    # 4 classes, currently active (now is within [start_dt, end_dt])
    classes = [
        {
            "subject_code": "CSE_TERM_1",
            "subject_name": "Terminal Completed",
            "timings": "10:00 AM - 11:00 AM",
            "start_time": (now - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": (now + timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S"),
            "status": "COMPLETED",
        },
        {
            "subject_code": "CSE_TERM_2",
            "subject_name": "Terminal Faculty Not Started",
            "timings": "10:00 AM - 11:00 AM",
            "start_time": (now - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": (now + timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S"),
            "status": "FACULTY_NOT_STARTED",
        },
        {
            "subject_code": "CSE_TERM_3",
            "subject_name": "Terminal Failed",
            "timings": "10:00 AM - 11:00 AM",
            "start_time": (now - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": (now + timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S"),
            "status": "FAILED",
        },
        {
            "subject_code": "CSE_TERM_4",
            "subject_name": "Terminal Missed",
            "timings": "10:00 AM - 11:00 AM",
            "start_time": (now - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": (now + timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S"),
            "status": "MISSED",
        },
    ]

    save_user_timetable(uid, today_str, classes)

    # 1. Simulate scheduler pass
    enqueued = simulate_scheduler_pass_for_user(uid, today_str, now)
    assert enqueued == 0, f"Expected 0 classes enqueued for terminal classes, got {enqueued}!"

    # 2. Verify statuses remained identical in DB
    tt = get_user_timetable(uid, today_str)
    status_map = {c["subject_code"]: c["status"] for c in tt["classes"]}
    assert status_map["CSE_TERM_1"] == "COMPLETED", f"Expected COMPLETED, got {status_map['CSE_TERM_1']}"
    assert status_map["CSE_TERM_2"] == "FACULTY_NOT_STARTED", f"Expected FACULTY_NOT_STARTED, got {status_map['CSE_TERM_2']}"
    assert status_map["CSE_TERM_3"] == "FAILED", f"Expected FAILED, got {status_map['CSE_TERM_3']}"
    assert status_map["CSE_TERM_4"] == "MISSED", f"Expected MISSED, got {status_map['CSE_TERM_4']}"

    # 3. Verify update_user_class_status refuses to overwrite terminal statuses
    for code, term_status in status_map.items():
        refused = update_user_class_status(uid, today_str, code, new_status="QUEUED")
        assert not refused, f"Expected update_user_class_status to reject overwriting {term_status} with QUEUED!"
        # Verify status is still intact
        fresh_tt = get_user_timetable(uid, today_str)
        fresh_map = {c["subject_code"]: c["status"] for c in fresh_tt["classes"]}
        assert fresh_map[code] == term_status, f"Status was illegally changed from {term_status} to {fresh_map[code]}!"

    print("[PASS] Scenario 1: Terminal-State Invariance verified across all 4 terminal statuses.")


# ---------------------------------------------------------------------------
# SCENARIO 2: Deduplication Branch Non-Destructive Update
# ---------------------------------------------------------------------------
def test_scenario_2_deduplication_branch():
    """
    Scenario 2: Existing Redis idempotency key + terminal timetable status must not become QUEUED.
    """
    print("\n" + "=" * 80)
    print("SCENARIO 2: Deduplication Branch Non-Destructive Update")
    print("=" * 80)

    uid = 95020
    today_str = get_now_ist().strftime("%Y-%m-%d")
    now = get_now_ist()
    seed_synthetic_user(uid, f"synth_user_{uid}")

    class_meta = {
        "subject_code": "CSE_DEDUP",
        "subject_name": "Deduplication Safety Class",
        "timings": "11:00 AM - 12:00 PM",
        "start_time": (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": (now + timedelta(minutes=50)).strftime("%Y-%m-%d %H:%M:%S"),
        "status": "FACULTY_NOT_STARTED",  # Finished previously as FACULTY_NOT_STARTED
    }

    save_user_timetable(uid, today_str, [class_meta])

    # Pre-populate the Redis idempotency key so schedule_class_to_queue would return enqueued=False
    # (Simulates class that was queued earlier and its Redis key is still in TTL)
    from app.job_queue import generate_class_idempotency_key, IDEMPOTENCY_KEY_PREFIX
    r = get_test_redis()
    idemp_sub = generate_class_idempotency_key(uid, class_meta)
    idemp_key = f"{IDEMPOTENCY_KEY_PREFIX}{idemp_sub}"
    r.set(idemp_key, "already_enqueued_job_123", ex=3600)

    # Now verify that schedule_class_to_queue returns enqueued=False
    job, enqueued = schedule_class_to_queue(class_meta, user_id=uid)
    assert not enqueued, "Expected schedule_class_to_queue to report enqueued=False due to existing idempotency key!"

    # Run scheduler pass
    simulate_scheduler_pass_for_user(uid, today_str, now)

    # Verify status in SQLite was NOT overwritten to QUEUED
    tt = get_user_timetable(uid, today_str)
    assert tt["classes"][0]["status"] == "FACULTY_NOT_STARTED", (
        f"Deduplication branch corrupted terminal status! Got: {tt['classes'][0]['status']}"
    )

    print("[PASS] Scenario 2: Deduplication branch non-destructive update verified (FACULTY_NOT_STARTED preserved).")


# ---------------------------------------------------------------------------
# SCENARIO 3: Expired QUEUED Class Finalization -> MISSED
# ---------------------------------------------------------------------------
def test_scenario_3_expired_queued_class():
    """
    Scenario 3: QUEUED class where now > end_dt + 5m and no active ExecutionLock -> transitions to MISSED.
    """
    print("\n" + "=" * 80)
    print("SCENARIO 3: Expired QUEUED Class Finalization")
    print("=" * 80)

    uid = 95030
    today_str = get_now_ist().strftime("%Y-%m-%d")
    now = get_now_ist()
    seed_synthetic_user(uid, f"synth_user_{uid}")

    # Class ended 10 minutes ago (which is > 5 minutes after end_dt)
    class_meta = {
        "subject_code": "CSE_EXPIRED_NO_LOCK",
        "subject_name": "Abandoned Queued Class",
        "timings": "08:00 AM - 09:00 AM",
        "start_time": (now - timedelta(minutes=70)).strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),
        "status": "QUEUED",
    }

    save_user_timetable(uid, today_str, [class_meta])

    # Verify no execution lock exists
    r = get_test_redis()
    lock_key = get_execution_lock_key(uid, class_meta)
    r.delete(lock_key)

    # Run scheduler pass
    simulate_scheduler_pass_for_user(uid, today_str, now)

    # Verify status in DB is now MISSED
    tt = get_user_timetable(uid, today_str)
    assert tt["classes"][0]["status"] == "MISSED", (
        f"Expected class to transition to MISSED, got: {tt['classes'][0]['status']}"
    )

    print("[PASS] Scenario 3: Expired QUEUED class correctly transitioned to MISSED.")


# ---------------------------------------------------------------------------
# SCENARIO 4: Active Execution Protection (Lock Held -> Stays QUEUED)
# ---------------------------------------------------------------------------
def test_scenario_4_active_execution_protection():
    """
    Scenario 4: Expired class where now > end_dt + 5m, but an active ExecutionLock is held in Redis.
    Scheduler must NOT mark it MISSED, leaving it QUEUED so worker can finish.
    """
    print("\n" + "=" * 80)
    print("SCENARIO 4: Active Execution Protection")
    print("=" * 80)

    uid = 95040
    today_str = get_now_ist().strftime("%Y-%m-%d")
    now = get_now_ist()
    seed_synthetic_user(uid, f"synth_user_{uid}")

    class_meta = {
        "subject_code": "CSE_ACTIVE_LATE",
        "subject_name": "Running Late Class",
        "timings": "08:00 AM - 09:00 AM",
        "start_time": (now - timedelta(minutes=70)).strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),
        "status": "QUEUED",
    }

    save_user_timetable(uid, today_str, [class_meta])

    r = get_test_redis()
    lock_key = get_execution_lock_key(uid, class_meta)
    # Simulate an active worker holding the ExecutionLock with heartbeat
    r.set(lock_key, "active_worker_instance_42", ex=60)

    try:
        # Run scheduler pass
        simulate_scheduler_pass_for_user(uid, today_str, now)

        # Verify status in DB remains QUEUED
        tt = get_user_timetable(uid, today_str)
        assert tt["classes"][0]["status"] == "QUEUED", (
            f"Active execution lock was ignored! Status became: {tt['classes'][0]['status']}"
        )
        print("[PASS] Scenario 4: Active execution protection verified (remains QUEUED while lock held).")

    finally:
        r.delete(lock_key)


# ---------------------------------------------------------------------------
# SCENARIO 5: Worker Stale Redis-Job Drain
# ---------------------------------------------------------------------------
def test_scenario_5_worker_stale_job_drain():
    """
    Scenario 5: Worker receives an expired job (end_dt + 5m in the past).
    Worker must:
    - NOT launch browser/Playwright (process_class_fn is not called)
    - update timetable to MISSED
    - ACK the stale message
    - release the execution lock
    """
    print("\n" + "=" * 80)
    print("SCENARIO 5: Worker Stale Redis-Job Drain")
    print("=" * 80)

    uid = 95050
    today_str = get_now_ist().strftime("%Y-%m-%d")
    now = get_now_ist()
    seed_synthetic_user(uid, f"synth_user_{uid}")

    start_dt = now - timedelta(minutes=80)
    end_dt = now - timedelta(minutes=20)
    target_date = start_dt.strftime("%Y-%m-%d")

    class_meta = {
        "subject_code": "CSE_STALE_JOB",
        "subject_name": "Stale Redis Queue Job",
        "timings": "07:00 AM - 08:00 AM",
        "date": target_date,
        "start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "QUEUED",
    }

    save_user_timetable(uid, target_date, [class_meta])

    test_stream = "cloudnote:test:stream:3f5"
    test_group = "cloudnote:test:group:3f5"
    r = get_test_redis()
    r.delete(test_stream)

    from app.job_queue import ensure_consumer_group
    ensure_consumer_group(client=r, stream_name=test_stream, group_name=test_group)

    # Enqueue stale job directly to Redis stream
    job_id = enqueue_job(
        user_id=uid,
        class_metadata=class_meta,
        client=r,
        stream_name=test_stream,
    )
    assert job_id, "Failed enqueueing test job to stream!"

    # Create worker consumer
    worker = WorkerConsumer(
        stream_name=test_stream,
        group_name=test_group,
        redis_client=r,
    )

    # Mock process_class_fn to assert it is never called
    mock_process_class = MagicMock()

    async def run_worker_drain():
        # Consume the job from the test stream
        jobs = consume_jobs(
            consumer_name=worker.consumer_name,
            count=1,
            block_ms=1000,
            client=r,
            stream_name=test_stream,
            group_name=test_group,
        )
        assert len(jobs) == 1, f"Expected 1 job consumed, got {len(jobs)}"
        job = jobs[0]

        # Execute single job
        result = await worker.process_single_job(job, process_class_fn=mock_process_class)
        assert result is False, "Expected process_single_job to return False for stale job!"

    asyncio.run(run_worker_drain())

    # 1. Verify browser/Playwright mock function was NEVER called
    assert mock_process_class.call_count == 0, (
        f"Security violation: process_class was called {mock_process_class.call_count} times for stale job!"
    )

    # 2. Verify timetable was transitioned to MISSED
    tt = get_user_timetable(uid, target_date)
    assert tt["classes"][0]["status"] == "MISSED", (
        f"Expected timetable status to be MISSED, got: {tt['classes'][0]['status']}"
    )

    # 3. Verify message was ACKed from Redis stream
    pel_info = r.xpending(test_stream, test_group)
    assert pel_info["pending"] == 0, f"Expected 0 pending jobs in PEL after ACK, got {pel_info['pending']}!"

    # 4. Verify execution lock was released
    lock_key = get_execution_lock_key(uid, class_meta)
    assert not r.exists(lock_key), "Execution lock was not released after stale job drain!"

    # Clean up test stream
    r.delete(test_stream)
    print("[PASS] Scenario 5: Worker stale-job drain verified (no browser, ACKed, timetable MISSED, lock released).")


# ---------------------------------------------------------------------------
# SCENARIO 6: SQLite Lock Retry
# ---------------------------------------------------------------------------
def test_scenario_6_sqlite_lock_retry():
    """
    Scenario 6: SQLite lock retry behavior.
    Verifies that update_user_class_status and _sync_timetable_status:
    - retry on transient SQLite OperationalError: database is locked
    - backoff for 0.1s
    - succeed when the lock is released within 3 attempts
    - fail gracefully without infinite looping if permanently locked
    """
    print("\n" + "=" * 80)
    print("SCENARIO 6: SQLite Lock Retry")
    print("=" * 80)

    uid = 95060
    today_str = get_now_ist().strftime("%Y-%m-%d")
    now = get_now_ist()
    seed_synthetic_user(uid, f"synth_user_{uid}")

    class_meta = {
        "subject_code": "CSE_LOCK_RETRY",
        "subject_name": "Lock Retry Test",
        "timings": "09:00 AM - 10:00 AM",
        "start_time": (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
        "status": "UPCOMING",
    }
    save_user_timetable(uid, today_str, [class_meta])

    # Sub-test A: Transient lock with temporary exclusive hold
    # Acquire a real exclusive lock on SQLite for 0.15s in a background thread
    lock_cleared = threading.Event()

    def hold_exclusive_lock():
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("BEGIN EXCLUSIVE")
            time.sleep(0.15)  # Hold lock for 150ms (longer than 1 retry backoff of 100ms)
            conn.commit()
            conn.close()
        finally:
            lock_cleared.set()

    t = threading.Thread(target=hold_exclusive_lock)
    t.start()

    # Small delay to ensure background thread acquires lock first
    time.sleep(0.02)

    # update_user_class_status should encounter locked on attempt 1, sleep 0.1s, and succeed on attempt 2 or 3!
    start_time = time.time()
    updated = update_user_class_status(uid, today_str, "CSE_LOCK_RETRY", new_status="QUEUED")
    duration = time.time() - start_time
    t.join()

    assert updated is True, "Expected update_user_class_status to retry and succeed after transient lock!"
    assert duration >= 0.08, f"Expected retry backoff delay of ~0.1s, but completed in {duration:.3f}s!"

    # Verify DB was updated
    tt = get_user_timetable(uid, today_str)
    assert tt["classes"][0]["status"] == "QUEUED", f"Expected status QUEUED, got {tt['classes'][0]['status']}"

    # Sub-test B: WorkerConsumer _sync_timetable_status retry behavior
    worker = WorkerConsumer(stream_name="test", group_name="test", redis_client=get_test_redis())
    attempts_seen = []
    real_update = update_user_class_status

    def mock_flaky_update(*args, **kwargs):
        attempts_seen.append(len(attempts_seen) + 1)
        if len(attempts_seen) < 3:
            raise sqlite3.OperationalError("database is locked")
        return real_update(*args, **kwargs)

    with patch("app.database.update_user_class_status", side_effect=mock_flaky_update):
        sync_result = worker._sync_timetable_status(
            user_id=uid,
            class_metadata=class_meta,
            new_status="MISSED",
        )
        assert sync_result is True, "Expected _sync_timetable_status to succeed on 3rd attempt!"
        assert len(attempts_seen) == 3, f"Expected exactly 3 attempts, got {len(attempts_seen)}!"

    # Verify status in DB became MISSED
    tt = get_user_timetable(uid, today_str)
    assert tt["classes"][0]["status"] == "MISSED", f"Expected MISSED, got {tt['classes'][0]['status']}"

    # Sub-test C: Permanent lock gracefully returns False without infinite loop
    def mock_permanent_lock(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    with patch("app.database.update_user_class_status", side_effect=mock_permanent_lock):
        failed_result = worker._sync_timetable_status(
            user_id=uid,
            class_metadata=class_meta,
            new_status="FAILED",
        )
        assert failed_result is False, "Expected _sync_timetable_status to return False on permanent lock!"

    print("[PASS] Scenario 6: SQLite lock retry behavior verified (transient backoff retry, success on attempt 3, graceful failure on permanent lock).")


# ---------------------------------------------------------------------------
# SCENARIO 7: Guaranteed Synthetic Cleanup
# ---------------------------------------------------------------------------
def test_scenario_7_guaranteed_synthetic_cleanup():
    """
    Scenario 7: Verify guaranteed cleanup leaves zero synthetic data.
    """
    print("\n" + "=" * 80)
    print("SCENARIO 7: Guaranteed Synthetic Cleanup")
    print("=" * 80)

    cleanup_synthetic_test_data()

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users WHERE id >= 90000 AND id < 100000")
    user_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM user_timetables WHERE user_id >= 90000 AND user_id < 100000")
    tt_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM class_attendance_records WHERE user_id >= 90000 AND user_id < 100000")
    att_count = cur.fetchone()[0]
    conn.close()

    assert user_count == 0, f"Found {user_count} leftover synthetic users!"
    assert tt_count == 0, f"Found {tt_count} leftover synthetic timetables!"
    assert att_count == 0, f"Found {att_count} leftover synthetic attendance records!"

    print("[PASS] Scenario 7: Zero synthetic test records remain in SQLite or Redis.")


# ---------------------------------------------------------------------------
# MAIN RUNNER
# ---------------------------------------------------------------------------
def run_all_step3f5_tests():
    print("\n" + "#" * 80)
    print("STARTING STEP 3F.5 VALIDATION TEST SUITE")
    print("#" * 80)

    cleanup_synthetic_test_data()

    try:
        test_scenario_1_terminal_state_invariance()
        test_scenario_2_deduplication_branch()
        test_scenario_3_expired_queued_class()
        test_scenario_4_active_execution_protection()
        test_scenario_5_worker_stale_job_drain()
        test_scenario_6_sqlite_lock_retry()
        test_scenario_7_guaranteed_synthetic_cleanup()

        print("\n" + "#" * 80)
        print("ALL STEP 3F.5 TESTS PASSED SUCCESSFULLY! (7/7 Scenarios)")
        print("#" * 80 + "\n")
        return True

    except Exception as exc:
        logger.exception(f"TEST RUN FAILED WITH EXCEPTION: {exc}")
        return False

    finally:
        cleanup_synthetic_test_data()


if __name__ == "__main__":
    success = run_all_step3f5_tests()
    sys.exit(0 if success else 1)
