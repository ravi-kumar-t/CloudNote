"""
Step 3F.4 Validation Test Suite
---------------------------------
Dead-Letter Queue (DLQ), Poison Pill Quarantine & Timetable State Finalization:
1. Scenario 1: Transient failure recovery (< max delivery attempts)
2. Scenario 2: Poison pill DLQ quarantine (authoritative delivery count & exact execution count)
3. Scenario 3: Timetable terminal status preservation (COMPLETED, FACULTY_NOT_STARTED, FAILED)
4. Scenario 4: DLQ payload schema & zero-credential security audit
5. Scenario 5: Stream capacity defense & bounded trimming (QUEUE_STREAM_MAXLEN)
6. Scenario 6: Guaranteed synthetic test data cleanup

Constraints:
- Synthetic test data only (users 90050-90060)
- Authoritative delivery count based on Redis PEL `times_delivered`
- Exact execution count proven before quarantine (zero off-by-one)
- Zero credentials in DLQ payloads
- Clean teardown in try ... finally
"""

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
import uuid
from typing import Any, Optional

import redis

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.crypto import encrypt_portal_password
from app.database import (
    get_db_connection,
    save_user_timetable,
    get_user_timetable,
)
from app.job_queue import (
    enqueue_job,
    consume_jobs,
    acknowledge_job,
    claim_pending_jobs,
    route_to_dead_letter_queue,
    get_dead_letter_jobs,
    ensure_consumer_group,
    get_redis_client,
    IngestionJob,
    JobQueueSecurityError,
    FORBIDDEN_PAYLOAD_KEYS,
    DEFAULT_DLQ_STREAM_NAME,
)
from app.worker_consumer import WorkerConsumer, generate_consumer_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Step3F4DLQTests")


def get_test_redis():
    """Returns a connected Redis client with localhost fallback."""
    try:
        r = get_redis_client()
        r.ping()
        return r
    except Exception:
        r = get_redis_client(redis_url="redis://localhost:6379/0")
        assert r.ping(), "Redis connection failed on both settings.REDIS_URL and localhost:6379!"
        return r


# ---------------------------------------------------------------------------
# Guaranteed Synthetic Cleanup Helper
# ---------------------------------------------------------------------------
def cleanup_synthetic_test_data():
    """
    Guaranteed cleanup: removes all synthetic test users (90000 <= id < 100000),
    associated attendance records, timetable rows, synthetic user log directories,
    and Redis test stream keys. Never modifies real users.
    """
    print("\n--- Cleaning up Step 3F.4 synthetic test data ---")
    for attempt in range(5):
        try:
            conn = get_db_connection()
            cur = conn.cursor()

            cur.execute("DELETE FROM class_attendance_records WHERE user_id >= 90000 AND user_id < 100000")
            del_attendance = cur.rowcount

            cur.execute("DELETE FROM user_timetables WHERE user_id >= 90000 AND user_id < 100000")
            del_timetables = cur.rowcount

            cur.execute("DELETE FROM users WHERE id >= 90000 AND id < 100000")
            del_users = cur.rowcount

            conn.commit()
            conn.close()
            print(f"Database cleanup: removed {del_users} users, {del_attendance} attendance records, {del_timetables} timetable rows.")
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < 4:
                time.sleep(0.2)
                continue
            print(f"Warning: Database cleanup error: {e}")
            break
        except Exception as e:
            print(f"Warning: Database cleanup error: {e}")
            break

    # Remove synthetic log directories
    try:
        logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        if os.path.exists(logs_dir):
            for entry in os.listdir(logs_dir):
                if entry.isdigit() and 90000 <= int(entry) < 100000:
                    shutil.rmtree(os.path.join(logs_dir, entry), ignore_errors=True)
            print("Filesystem cleanup: removed synthetic test user directories under logs/.")
    except Exception as e:
        print(f"Warning: Filesystem cleanup error: {e}")

    # Clean up Redis test keys
    try:
        r = get_test_redis()
        keys = r.keys("cloudnote:test:step3f4:*")
        if keys:
            r.delete(*keys)
            print(f"Redis cleanup: removed {len(keys)} test keys.")
    except Exception as e:
        print(f"Warning: Redis cleanup error: {e}")


def insert_synthetic_user(user_id: int, username: str, password_plaintext: Optional[str] = "TestPass123!"):
    """Inserts a synthetic user record into SQLite with optional encrypted portal credentials."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    enc_pw = encrypt_portal_password(password_plaintext, settings.JWT_SECRET) if password_plaintext else None
    cur.execute(
        """
        INSERT INTO users (id, username, email, hashed_password, lpu_username, encrypted_portal_password)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, username, f"{username}@example.com", "hash_placeholder", username, enc_pw),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Scenario 1: Transient Failure Recovery (< max delivery attempts)
# ---------------------------------------------------------------------------
async def test_scenario_1_transient_failure_recovery():
    """
    Scenario 1: Transient failure recovery (< max delivery attempts).
    - Worker 1 attempts execution on delivery 1 (times_delivered=1), simulates failure (unhandled crash).
    - Message is NOT acknowledged; remains in Redis PEL with times_delivered=1.
    - Timetable remains QUEUED.
    - Worker 2 reclaims job via claim_pending_jobs (XAUTOCLAIM increments times_delivered to 2).
    - Reclaimed job has delivery_count=2 (<= max_attempts=3).
    - Worker 2 completes processing successfully, updates timetable to COMPLETED, and ACKs message.
    - Main PEL is drained (0 pending), DLQ count is 0.
    """
    print("\n--- Scenario 1: Transient Failure Recovery (< Max Delivery Attempts) ---")
    r = get_test_redis()
    uid = uuid.uuid4().hex[:8]
    stream = f"cloudnote:test:step3f4:s1:stream:{uid}"
    group = f"group:s1:{uid}"
    dlq = f"cloudnote:test:step3f4:s1:dlq:{uid}"
    ensure_consumer_group(client=r, stream_name=stream, group_name=group)

    user_id = 90051
    today_str = "2026-09-21"
    insert_synthetic_user(user_id, "std_90051")

    # Setup initial timetable with QUEUED status
    class_meta = {
        "subject_code": "CSE_RECOVER_101",
        "subject_name": "Distributed Algorithms",
        "faculty": "Dr. Leslie Lamport",
        "timings": "09:00 AM - 10:00 AM",
        "date": today_str,
        "status": "QUEUED",
    }
    save_user_timetable(user_id=user_id, date_str=today_str, classes=[class_meta])

    # Enqueue job to stream
    job, enqueued = enqueue_job(
        user_id=user_id,
        class_metadata=class_meta,
        idempotency_key=f"s1_test_{uid}",
        client=r,
        stream_name=stream,
    )
    assert enqueued and job is not None
    assert job.stream_message_id is not None

    # Step A: Worker 1 consumes and crashes on delivery 1
    worker_1 = WorkerConsumer(
        consumer_name=f"worker-s1-1-{uid}",
        stream_name=stream,
        group_name=group,
        dlq_stream_name=dlq,
        redis_client=r,
    )

    jobs = consume_jobs(consumer_name=worker_1.consumer_name, count=1, block_ms=1000, client=r, stream_name=stream, group_name=group)
    assert len(jobs) == 1
    consumed_job = jobs[0]
    assert consumed_job.delivery_count == 1

    async def fail_delivery_1(**kwargs):
        raise RuntimeError("Simulated transient connection failure on delivery 1")

    # Attempt execution under Worker 1: should raise and NOT ACK
    task_success = await worker_1._safe_job_task(consumed_job, process_class_fn=fail_delivery_1, is_reclaimed=False)
    assert task_success is False, "Expected task to fail on transient error."

    # Verify Redis PEL: message must still be in PEL with times_delivered=1
    pel_entries = r.xpending_range(name=stream, groupname=group, min="-", max="+", count=10)
    assert len(pel_entries) == 1, f"Expected 1 entry in PEL, found {len(pel_entries)}"
    assert pel_entries[0]["times_delivered"] == 1, f"Expected times_delivered=1, got {pel_entries[0]['times_delivered']}"

    # Verify timetable is still QUEUED
    tt = get_user_timetable(user_id, today_str)
    assert tt is not None and tt["classes"][0]["status"] == "QUEUED", "Timetable should still be QUEUED after transient failure."

    # Step B: Worker 2 reclaims and succeeds on delivery 2
    worker_2 = WorkerConsumer(
        consumer_name=f"worker-s1-2-{uid}",
        stream_name=stream,
        group_name=group,
        dlq_stream_name=dlq,
        redis_client=r,
    )

    # Reclaim immediately (min_idle_time_ms=0)
    reclaimed_jobs = claim_pending_jobs(
        consumer_name=worker_2.consumer_name,
        min_idle_time_ms=0,
        count=1,
        client=r,
        stream_name=stream,
        group_name=group,
    )
    assert len(reclaimed_jobs) == 1, "Expected Worker 2 to reclaim 1 job."
    reclaimed_job = reclaimed_jobs[0]

    # Authoritative delivery count from Redis PEL must be 2
    assert reclaimed_job.delivery_count == 2, f"Expected delivery_count=2, got {reclaimed_job.delivery_count}"

    async def succeed_delivery_2(**kwargs):
        return {"status": "COMPLETED", "details": "Ingestion completed on attempt 2."}

    # Worker 2 executes successfully
    success = await worker_2._safe_job_task(reclaimed_job, process_class_fn=succeed_delivery_2, is_reclaimed=True)
    assert success is True, "Worker 2 should successfully process reclaimed job."

    # Verify PEL is now completely drained (0 pending)
    pel_after = r.xpending_range(name=stream, groupname=group, min="-", max="+", count=10)
    assert len(pel_after) == 0, f"Expected PEL to be empty after ACK, found {len(pel_after)}."

    # Verify DLQ received 0 messages
    dlq_entries = get_dead_letter_jobs(client=r, dlq_stream_name=dlq)
    assert len(dlq_entries) == 0, f"Expected 0 DLQ entries for recovered job, found {len(dlq_entries)}."

    # Verify timetable updated to COMPLETED
    tt_final = get_user_timetable(user_id, today_str)
    assert tt_final is not None and tt_final["classes"][0]["status"] == "COMPLETED", (
        f"Expected timetable status COMPLETED, got {tt_final['classes'][0]['status'] if tt_final else None}"
    )

    print("PASS: Scenario 1 — Transient failure successfully recovered on attempt 2 (< max), ACKed, timetable finalized to COMPLETED.")


# ---------------------------------------------------------------------------
# Scenario 2: Poison Pill DLQ Quarantine (Authoritative Count & Exact Executions)
# ---------------------------------------------------------------------------
async def test_scenario_2_poison_pill_dlq_quarantine():
    """
    Scenario 2: Poison pill DLQ quarantine (authoritative delivery count & exact execution count).
    - QUEUE_MAX_DELIVERY_ATTEMPTS = 3.
    - Job executes on delivery 1 (times_delivered=1 <= 3) -> fails.
    - Job executes on delivery 2 (times_delivered=2 <= 3) -> fails.
    - Job executes on delivery 3 (times_delivered=3 <= 3) -> fails.
    - On delivery 4 (times_delivered=4 > 3):
      Worker intercepts poison pill BEFORE executing process_class again.
      Routes to DLQ, updates timetable to FAILED, emits XACK to clear main stream PEL.
    - Explicitly proves:
      1. Exactly 3 executions took place before quarantine (zero off-by-one).
      2. Redis PEL times_delivered is 4 upon quarantine.
      3. Main stream PEL is 0 (drained).
      4. DLQ stream count is exactly 1.
      5. Timetable status is FAILED.
    """
    print("\n--- Scenario 2: Poison Pill DLQ Quarantine (Authoritative Count & Exact Executions) ---")
    r = get_test_redis()
    uid = uuid.uuid4().hex[:8]
    stream = f"cloudnote:test:step3f4:s2:stream:{uid}"
    group = f"group:s2:{uid}"
    dlq = f"cloudnote:test:step3f4:s2:dlq:{uid}"
    ensure_consumer_group(client=r, stream_name=stream, group_name=group)

    user_id = 90052
    today_str = "2026-09-21"
    insert_synthetic_user(user_id, "std_90052")

    class_meta = {
        "subject_code": "CSE_POISON_999",
        "subject_name": "Fault Tolerance",
        "faculty": "Dr. Jim Gray",
        "timings": "11:00 AM - 12:00 PM",
        "date": today_str,
        "status": "QUEUED",
    }
    save_user_timetable(user_id=user_id, date_str=today_str, classes=[class_meta])

    job, enqueued = enqueue_job(
        user_id=user_id,
        class_metadata=class_meta,
        idempotency_key=f"s2_test_{uid}",
        client=r,
        stream_name=stream,
    )
    assert enqueued and job is not None

    execution_counter = 0

    async def poison_process_class(**kwargs):
        nonlocal execution_counter
        execution_counter += 1
        raise RuntimeError(f"Crashing poison job on execution #{execution_counter}")

    max_attempts = getattr(settings, "QUEUE_MAX_DELIVERY_ATTEMPTS", 3)
    assert max_attempts == 3, f"Expected default QUEUE_MAX_DELIVERY_ATTEMPTS=3, got {max_attempts}"

    # Delivery 1: initial read
    worker_a = WorkerConsumer(consumer_name=f"worker-s2-a-{uid}", stream_name=stream, group_name=group, dlq_stream_name=dlq, redis_client=r)
    jobs_1 = consume_jobs(consumer_name=worker_a.consumer_name, count=1, block_ms=1000, client=r, stream_name=stream, group_name=group)
    assert len(jobs_1) == 1
    assert jobs_1[0].delivery_count == 1
    await worker_a._safe_job_task(jobs_1[0], process_class_fn=poison_process_class, is_reclaimed=False)
    assert execution_counter == 1, "Execution #1 should have completed."

    # Delivery 2: first reclaim
    worker_b = WorkerConsumer(consumer_name=f"worker-s2-b-{uid}", stream_name=stream, group_name=group, dlq_stream_name=dlq, redis_client=r)
    jobs_2 = claim_pending_jobs(consumer_name=worker_b.consumer_name, min_idle_time_ms=0, count=1, client=r, stream_name=stream, group_name=group)
    assert len(jobs_2) == 1
    assert jobs_2[0].delivery_count == 2, f"Expected Redis PEL delivery_count=2, got {jobs_2[0].delivery_count}"
    await worker_b._safe_job_task(jobs_2[0], process_class_fn=poison_process_class, is_reclaimed=True)
    assert execution_counter == 2, "Execution #2 should have completed."

    # Delivery 3: second reclaim (at max delivery attempts)
    worker_c = WorkerConsumer(consumer_name=f"worker-s2-c-{uid}", stream_name=stream, group_name=group, dlq_stream_name=dlq, redis_client=r)
    jobs_3 = claim_pending_jobs(consumer_name=worker_c.consumer_name, min_idle_time_ms=0, count=1, client=r, stream_name=stream, group_name=group)
    assert len(jobs_3) == 1
    assert jobs_3[0].delivery_count == 3, f"Expected Redis PEL delivery_count=3, got {jobs_3[0].delivery_count}"
    await worker_c._safe_job_task(jobs_3[0], process_class_fn=poison_process_class, is_reclaimed=True)
    assert execution_counter == 3, "Execution #3 should have completed."

    # Verify state before Delivery 4:
    # 3 executions done, job still in main PEL, 0 in DLQ, timetable still QUEUED
    pel_before_4 = r.xpending_range(name=stream, groupname=group, min="-", max="+", count=10)
    assert len(pel_before_4) == 1
    assert pel_before_4[0]["times_delivered"] == 3
    assert len(get_dead_letter_jobs(client=r, dlq_stream_name=dlq)) == 0
    tt_before_4 = get_user_timetable(user_id, today_str)
    assert tt_before_4["classes"][0]["status"] == "QUEUED"

    # Delivery 4: third reclaim (exceeds max_attempts: 4 > 3)
    worker_d = WorkerConsumer(consumer_name=f"worker-s2-d-{uid}", stream_name=stream, group_name=group, dlq_stream_name=dlq, redis_client=r)
    jobs_4 = claim_pending_jobs(consumer_name=worker_d.consumer_name, min_idle_time_ms=0, count=1, client=r, stream_name=stream, group_name=group)
    assert len(jobs_4) == 1
    assert jobs_4[0].delivery_count == 4, f"Authoritative Redis PEL times_delivered must be 4, got {jobs_4[0].delivery_count}"

    # Execute Worker D: Must detect delivery_count (4) > max_attempts (3), quarantine to DLQ without executing poison_process_class!
    task_res = await worker_d._safe_job_task(jobs_4[0], process_class_fn=poison_process_class, is_reclaimed=True)
    assert task_res is False, "Quarantine should return False without raising."

    # EMPIRICAL VALIDATIONS:
    # 1. Exact execution count assertion: poison_process_class must have executed exactly 3 times!
    assert execution_counter == 3, (
        f"OFF-BY-ONE DETECTED: Expected exactly {max_attempts} executions before quarantine, "
        f"but poison_process_class was executed {execution_counter} times!"
    )

    # 2. DLQ stream validation
    dlq_entries = get_dead_letter_jobs(client=r, dlq_stream_name=dlq)
    assert len(dlq_entries) == 1, f"Expected exactly 1 DLQ entry, found {len(dlq_entries)}."
    quarantined = dlq_entries[0]
    assert quarantined["user_id"] == str(user_id)
    assert quarantined["delivery_count"] == "4"
    assert "Exceeded maximum delivery attempts (4 > 3)" in quarantined["error_message"]
    assert quarantined["failed_by_consumer"] == worker_d.consumer_name
    assert quarantined["original_stream_id"] == job.stream_message_id

    # 3. Main PEL validation: Poison message must be cleared from PEL via XACK
    pel_after_quarantine = r.xpending_range(name=stream, groupname=group, min="-", max="+", count=10)
    assert len(pel_after_quarantine) == 0, (
        f"Poison message was not cleared from main stream PEL! Pending: {len(pel_after_quarantine)}"
    )

    # 4. Timetable status validation: Finalized to FAILED
    tt_after = get_user_timetable(user_id, today_str)
    assert tt_after is not None and tt_after["classes"][0]["status"] == "FAILED", (
        f"Expected timetable status FAILED, got {tt_after['classes'][0]['status'] if tt_after else None}"
    )

    print("PASS: Scenario 2 — Poison pill quarantined to DLQ after exactly 3 executions (times_delivered=4 > 3). Main PEL drained, timetable finalized to FAILED.")


# ---------------------------------------------------------------------------
# Scenario 3: Timetable Terminal Status Preservation
# ---------------------------------------------------------------------------
async def test_scenario_3_timetable_terminal_status_preservation():
    """
    Scenario 3: Timetable terminal status preservation.
    Verifies that the worker preserves the exact terminal outcome returned by the pipeline:
    - COMPLETED
    - FACULTY_NOT_STARTED (must NOT be overwritten to COMPLETED)
    - FAILED (terminal failure / Policy C missing credentials)
    Without altering schema or inventing new statuses.
    """
    print("\n--- Scenario 3: Timetable Terminal Status Preservation ---")
    r = get_test_redis()
    uid = uuid.uuid4().hex[:8]
    stream = f"cloudnote:test:step3f4:s3:stream:{uid}"
    group = f"group:s3:{uid}"
    dlq = f"cloudnote:test:step3f4:s3:dlq:{uid}"
    ensure_consumer_group(client=r, stream_name=stream, group_name=group)

    user_id = 90053
    today_str = "2026-09-21"
    insert_synthetic_user(user_id, "std_90053")

    worker = WorkerConsumer(consumer_name=f"worker-s3-{uid}", stream_name=stream, group_name=group, dlq_stream_name=dlq, redis_client=r)

    # Subtest 3A: COMPLETED
    class_completed = {
        "subject_code": "CSE_STATUS_COMPLETED",
        "subject_name": "Completed Lecture",
        "timings": "09:00 AM - 10:00 AM",
        "date": today_str,
        "status": "QUEUED",
    }
    save_user_timetable(user_id, today_str, [class_completed])
    job_a, _ = enqueue_job(user_id=user_id, class_metadata=class_completed, client=r, stream_name=stream)
    jobs_a = consume_jobs(consumer_name=worker.consumer_name, count=1, client=r, stream_name=stream, group_name=group)

    async def mock_completed(**kwargs):
        return {"status": "COMPLETED", "summary_id": 101}

    await worker._safe_job_task(jobs_a[0], process_class_fn=mock_completed)
    tt_a = get_user_timetable(user_id, today_str)
    assert tt_a["classes"][0]["status"] == "COMPLETED", f"Expected COMPLETED, got {tt_a['classes'][0]['status']}"

    # Subtest 3B: FACULTY_NOT_STARTED
    class_fns = {
        "subject_code": "CSE_STATUS_FNS",
        "subject_name": "Waiting for Faculty",
        "timings": "10:00 AM - 11:00 AM",
        "date": today_str,
        "status": "QUEUED",
    }
    save_user_timetable(user_id, today_str, [class_fns])
    job_b, _ = enqueue_job(user_id=user_id, class_metadata=class_fns, client=r, stream_name=stream)
    jobs_b = consume_jobs(consumer_name=worker.consumer_name, count=1, client=r, stream_name=stream, group_name=group)

    async def mock_fns(**kwargs):
        return {"status": "FACULTY_NOT_STARTED", "details": "Faculty did not start class."}

    await worker._safe_job_task(jobs_b[0], process_class_fn=mock_fns)
    tt_b = get_user_timetable(user_id, today_str)
    assert tt_b["classes"][0]["status"] == "FACULTY_NOT_STARTED", (
        f"Expected FACULTY_NOT_STARTED, got {tt_b['classes'][0]['status']} (must NOT be mapped to COMPLETED!)"
    )

    # Subtest 3C: FAILED (Pipeline outcome)
    class_failed = {
        "subject_code": "CSE_STATUS_FAILED",
        "subject_name": "Join Failure Class",
        "timings": "11:00 AM - 12:00 PM",
        "date": today_str,
        "status": "QUEUED",
    }
    save_user_timetable(user_id, today_str, [class_failed])
    job_c, _ = enqueue_job(user_id=user_id, class_metadata=class_failed, client=r, stream_name=stream)
    jobs_c = consume_jobs(consumer_name=worker.consumer_name, count=1, client=r, stream_name=stream, group_name=group)

    async def mock_failed(**kwargs):
        return {"status": "FAILED", "details": "Failed to join active lecture"}

    await worker._safe_job_task(jobs_c[0], process_class_fn=mock_failed)
    tt_c = get_user_timetable(user_id, today_str)
    assert tt_c["classes"][0]["status"] == "FAILED", f"Expected FAILED, got {tt_c['classes'][0]['status']}"

    # Subtest 3D: Policy C (Terminal failure due to missing credentials)
    user_no_creds = 90054
    insert_synthetic_user(user_no_creds, "std_90054_nocreds", password_plaintext=None)
    class_nocreds = {
        "subject_code": "CSE_STATUS_NOCREDS",
        "subject_name": "No Creds Lecture",
        "timings": "01:00 PM - 02:00 PM",
        "date": today_str,
        "status": "QUEUED",
    }
    save_user_timetable(user_no_creds, today_str, [class_nocreds])
    job_d, _ = enqueue_job(user_id=user_no_creds, class_metadata=class_nocreds, client=r, stream_name=stream)
    jobs_d = consume_jobs(consumer_name=worker.consumer_name, count=1, client=r, stream_name=stream, group_name=group)
    res_d = await worker._safe_job_task(jobs_d[0])
    assert res_d is False, "Policy C should return False for unauthenticated user."
    tt_d = get_user_timetable(user_no_creds, today_str)
    assert tt_d["classes"][0]["status"] == "FAILED", f"Expected FAILED for missing credentials, got {tt_d['classes'][0]['status']}"

    print("PASS: Scenario 3 — All terminal states (COMPLETED, FACULTY_NOT_STARTED, FAILED, Policy C) accurately finalized in user_timetables.")


# ---------------------------------------------------------------------------
# Scenario 4: DLQ Payload Schema & Zero-Credential Security Audit
# ---------------------------------------------------------------------------
async def test_scenario_4_dlq_security_audit():
    """
    Scenario 4: DLQ payload schema & zero-credential security audit.
    - Inspects DLQ records and verifies exact required fields:
      job_id, user_id, class_metadata, delivery_count, error_message, failed_by_consumer,
      original_stream_id, quarantined_at, dlq_message_id.
    - Verifies zero credentials or forbidden keys anywhere in the DLQ record.
    - Verifies that attempting to route an IngestionJob containing forbidden credentials
      raises JobQueueSecurityError before touching Redis.
    """
    print("\n--- Scenario 4: DLQ Payload Schema & Zero-Credential Security Audit ---")
    r = get_test_redis()
    uid = uuid.uuid4().hex[:8]
    stream = f"cloudnote:test:step3f4:s4:stream:{uid}"
    group = f"group:s4:{uid}"
    dlq = f"cloudnote:test:step3f4:s4:dlq:{uid}"

    # 1. Route a valid job to DLQ
    safe_metadata = {
        "subject_code": "CSE_SECURITY_AUDIT",
        "subject_name": "Cryptography & Systems Security",
        "faculty": "Dr. Ronald Rivest",
        "timings": "02:00 PM - 03:00 PM",
    }
    safe_job = IngestionJob(
        job_id=f"job_sec_{uid}",
        user_id=90055,
        class_metadata=safe_metadata,
        created_at="2026-09-21T10:00:00Z",
        stream_message_id="1790000000000-0",
        delivery_count=4,
    )

    dlq_id = route_to_dead_letter_queue(
        job=safe_job,
        error_message="Test poison pill with password=secret123 in error trace",
        failed_by_consumer=f"worker-sec-{uid}",
        client=r,
        dlq_stream_name=dlq,
        main_stream_name=stream,
        group_name=group,
    )
    assert dlq_id is not None, "DLQ routing should succeed."

    # 2. Inspect stored DLQ payload
    records = get_dead_letter_jobs(client=r, dlq_stream_name=dlq)
    assert len(records) == 1, "Expected 1 DLQ record."
    record = records[0]

    # Verify all expected schema fields are present
    required_keys = [
        "job_id",
        "user_id",
        "class_metadata",
        "delivery_count",
        "error_message",
        "failed_by_consumer",
        "original_stream_id",
        "quarantined_at",
        "dlq_message_id",
    ]
    for k in required_keys:
        assert k in record, f"Missing required field '{k}' in DLQ schema."

    # Verify error message was sanitized
    assert "secret123" not in record["error_message"], "Plaintext secret leaked in DLQ error_message!"
    assert "[REDACTED]" in record["error_message"], "Expected redacted token in DLQ error message."

    # Verify zero forbidden keys anywhere in payload
    raw_json_str = json.dumps(record).lower()
    for forbidden in FORBIDDEN_PAYLOAD_KEYS:
        # Check that forbidden key does not appear as a json key
        assert f'"{forbidden}"' not in raw_json_str, f"Forbidden credential key '{forbidden}' found in DLQ payload!"

    # 3. Malicious payload rejection: Attempting to quarantine a job containing credentials
    malicious_job = IngestionJob(
        job_id=f"job_bad_{uid}",
        user_id=90055,
        class_metadata={"subject_code": "CSE_HACK", "lpu_password": "PlaintextPassword123!"},
        created_at="2026-09-21T10:00:00Z",
        stream_message_id="1790000000001-0",
        delivery_count=4,
    )
    security_error_raised = False
    try:
        route_to_dead_letter_queue(
            job=malicious_job,
            error_message="Poison pill attempt",
            failed_by_consumer=f"worker-sec-{uid}",
            client=r,
            dlq_stream_name=dlq,
            main_stream_name=stream,
            group_name=group,
        )
    except JobQueueSecurityError as sec_err:
        security_error_raised = True
        assert "lpu_password" in str(sec_err), f"Expected offending key in error: {sec_err}"

    assert security_error_raised is True, "Security Violation: route_to_dead_letter_queue must reject credentials!"

    # Verify DLQ still has only 1 record (malicious was rejected before write)
    records_after = get_dead_letter_jobs(client=r, dlq_stream_name=dlq)
    assert len(records_after) == 1, "Malicious record must not be written to DLQ."

    print("PASS: Scenario 4 — DLQ payload schema matches specification, error sanitized, zero credentials stored, and forbidden payloads strictly rejected.")


# ---------------------------------------------------------------------------
# Scenario 5: Stream Capacity Defense & Bounded Trimming
# ---------------------------------------------------------------------------
async def test_scenario_5_stream_capacity_trimming():
    """
    Scenario 5: Stream capacity defense & bounded trimming.
    - Proves enqueue_job adheres to maxlen configuration (approximate=False for exact test).
    - Enqueues 15 jobs into a stream with maxlen=10.
    - Asserts stream length does not exceed 10.
    - Verifies oldest jobs (1-5) were evicted and newest jobs (6-15) are preserved.
    """
    print("\n--- Scenario 5: Stream Capacity Defense & Bounded Trimming ---")
    r = get_test_redis()
    uid = uuid.uuid4().hex[:8]
    stream = f"cloudnote:test:step3f4:s5:stream:{uid}"

    max_len = 10
    total_to_enqueue = 15

    enqueued_ids = []
    for i in range(1, total_to_enqueue + 1):
        job, enqueued = enqueue_job(
            user_id=90056,
            class_metadata={"subject_code": f"CSE_STREAM_{i}", "slot": str(i)},
            idempotency_key=f"s5_job_{i}_{uid}",
            client=r,
            stream_name=stream,
            maxlen=max_len,
            approximate=False,
        )
        assert enqueued and job is not None
        enqueued_ids.append(job.stream_message_id)

    # Verify Redis stream length is capped at 10
    stream_len = r.xlen(stream)
    assert stream_len == max_len, f"Expected stream length {max_len}, got {stream_len}."

    # Read all remaining messages
    remaining = r.xrange(stream, min="-", max="+")
    remaining_msg_ids = [msg_id for msg_id, _ in remaining]
    assert len(remaining_msg_ids) == max_len

    # Verify oldest 5 were trimmed and newest 10 match exactly
    expected_ids = enqueued_ids[total_to_enqueue - max_len:]
    assert remaining_msg_ids == expected_ids, "Expected stream to preserve the newest 10 messages."

    # Test default maxlen behavior uses settings.QUEUE_STREAM_MAXLEN
    stream_default = f"cloudnote:test:step3f4:s5:default:{uid}"
    job_def, enq_def = enqueue_job(
        user_id=90056,
        class_metadata={"subject_code": "CSE_DEFAULT_MAXLEN"},
        idempotency_key=f"s5_def_{uid}",
        client=r,
        stream_name=stream_default,
    )
    assert enq_def and job_def is not None
    assert r.xlen(stream_default) == 1

    print(f"PASS: Scenario 5 — Stream trimmed cleanly at maxlen={max_len} (evicted oldest 5, retained newest 10).")


# ---------------------------------------------------------------------------
# Scenario 6: Guaranteed Synthetic Cleanup
# ---------------------------------------------------------------------------
def test_scenario_6_guaranteed_cleanup():
    """
    Scenario 6: Guaranteed synthetic cleanup validation.
    Runs the cleanup helper and explicitly asserts that zero synthetic users,
    timetables, attendance records, or Redis test keys remain.
    """
    print("\n--- Scenario 6: Guaranteed Synthetic Cleanup Validation ---")
    cleanup_synthetic_test_data()

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM users WHERE id >= 90000 AND id < 100000")
    user_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM user_timetables WHERE user_id >= 90000 AND user_id < 100000")
    timetable_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM class_attendance_records WHERE user_id >= 90000 AND user_id < 100000")
    attendance_count = cur.fetchone()[0]

    conn.close()

    r = get_test_redis()
    redis_keys = r.keys("cloudnote:test:step3f4:*")

    assert user_count == 0, f"Cleanup incomplete: {user_count} synthetic users remaining!"
    assert timetable_count == 0, f"Cleanup incomplete: {timetable_count} synthetic timetables remaining!"
    assert attendance_count == 0, f"Cleanup incomplete: {attendance_count} synthetic attendance records remaining!"
    assert len(redis_keys) == 0, f"Cleanup incomplete: {len(redis_keys)} Redis test keys remaining!"

    print("PASS: Scenario 6 — All synthetic users, timetables, attendance records, and Redis keys completely cleaned up.")


# ---------------------------------------------------------------------------
# Main Test Suite Runner
# ---------------------------------------------------------------------------
async def run_all_tests():
    print("================================================================================")
    print("STEP 3F.4 VALIDATION: DEAD-LETTER QUEUE (DLQ) & TIMETABLE FINALIZATION")
    print("================================================================================")

    # Initial cleanup to ensure clean state
    cleanup_synthetic_test_data()

    passed = 0
    total = 6

    try:
        await test_scenario_1_transient_failure_recovery()
        passed += 1

        await test_scenario_2_poison_pill_dlq_quarantine()
        passed += 1

        await test_scenario_3_timetable_terminal_status_preservation()
        passed += 1

        await test_scenario_4_dlq_security_audit()
        passed += 1

        await test_scenario_5_stream_capacity_trimming()
        passed += 1

        test_scenario_6_guaranteed_cleanup()
        passed += 1

    except Exception as e:
        logger.error(f"Test suite encountered an error: {e}", exc_info=True)
        raise
    finally:
        # Guaranteed cleanup runs even if test fails
        cleanup_synthetic_test_data()

    print("\n================================================================================")
    print(f"STEP 3F.4 RESULTS: {passed}/{total} SCENARIOS PASSED SUCCESSFULLY")
    print("================================================================================")


if __name__ == "__main__":
    asyncio.run(run_all_tests())
