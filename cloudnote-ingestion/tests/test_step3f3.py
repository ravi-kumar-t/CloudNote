"""
Step 3F.3 Test Suite — Native Multi-Worker Scaling & Bounded Asynchronous Concurrency

Empirically validates:
1. Scenario 1: Multi-worker concurrency (two independent WorkerConsumer instances on the same stream/group
   process distinct synthetic jobs concurrently within 200ms start delta; both ACK, PEL=0).
2. Scenario 2: Single-worker bounded concurrency (WorkerConsumer with max_concurrent_jobs=2 processes 5 jobs;
   peak concurrent executions bounded at <= 2; all complete and ACK, PEL=0).
3. Scenario 3: Execution-lock duplicate race under bounded async worker (2 duplicate stream messages dispatched;
   exactly 1 executes; 2nd detects lock held, safely ACKs; total executions=1, ACKs=2, PEL=0).
4. Scenario 4: Worker crash & XAUTOCLAIM recovery (Worker A fails unhandled -> PEL retained -> Worker B
   reclaims via XAUTOCLAIM -> completes -> ACK -> PEL=0).
5. Scenario 5: Multi-user state isolation under concurrent processing (4 concurrent synthetic users; 100% data
   partition isolation across SQLite attendance records, logs, and credentials).
6. Scenario 6: Graceful shutdown recovery semantics (long-running job exceeding shutdown timeout is cancelled
   without ACK, execution lock released in finally, retained in PEL for recovery).
7. Guaranteed cleanup: wipes synthetic users [90000, 100000), attendance records, logs/9xxxx, and Redis test keys.
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
from typing import Dict, List, Any, Optional

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.crypto import encrypt_portal_password
from app.database import (
    init_db,
    get_db_connection,
    record_attendance_session,
    get_user_attendance_records,
    get_user_logs_dir,
)
from app.job_queue import (
    get_redis_client,
    ensure_consumer_group,
    enqueue_job,
    acknowledge_job,
    claim_pending_jobs,
    get_pending_jobs,
    generate_class_idempotency_key,
    get_execution_lock_key,
    IngestionJob,
)
from app.worker_consumer import WorkerConsumer, generate_consumer_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Step3F3ScalingTests")


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
    print("\n--- Cleaning up Step 3F.3 synthetic test data ---")
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
        keys = r.keys("cloudnote:test:step3f3:*")
        if keys:
            r.delete(*keys)
            print(f"Redis cleanup: removed {len(keys)} test keys.")
    except Exception as e:
        print(f"Warning: Redis cleanup error: {e}")


def insert_synthetic_user(user_id: int, username: str, password_plaintext: str = "TestPass123!"):
    """Inserts a synthetic user record into SQLite with encrypted portal credentials."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    enc_pw = encrypt_portal_password(password_plaintext, settings.JWT_SECRET)
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
# Scenario 1: Multi-Worker Concurrency
# ---------------------------------------------------------------------------
async def test_scenario_1_multi_worker_concurrency():
    """
    Two independent WorkerConsumer instances on the same stream/group consume two synthetic jobs.
    Proves true concurrent start (< 200ms delta), both complete, both ACK, PEL=0.
    """
    print("\n--- Scenario 1: Multi-Worker Concurrent Processing ---")
    r = get_test_redis()
    uid = uuid.uuid4().hex[:8]
    stream = f"cloudnote:test:step3f3:s1:{uid}"
    group = f"group:s1:{uid}"
    ensure_consumer_group(client=r, stream_name=stream, group_name=group)

    user_a = 91001
    user_b = 91002
    insert_synthetic_user(user_a, "std_91001")
    insert_synthetic_user(user_b, "std_91002")

    job_a_id = enqueue_job(
        user_id=user_a,
        class_metadata={"subject_code": "CSE101_S1", "room": "38-601", "start_time": "09:00", "end_time": "10:00"},
        client=r, stream_name=stream,
    )
    job_b_id = enqueue_job(
        user_id=user_b,
        class_metadata={"subject_code": "CSE102_S1", "room": "38-602", "start_time": "09:00", "end_time": "10:00"},
        client=r, stream_name=stream,
    )

    starts: Dict[str, float] = {}
    finishes: Dict[str, float] = {}

    async def mock_process_class(user_id: int, lpu_username: str, lpu_password: str, class_metadata: dict):
        subject = class_metadata["subject_code"]
        starts[subject] = time.perf_counter()
        await asyncio.sleep(0.3)
        finishes[subject] = time.perf_counter()
        return {"status": "COMPLETED", "subject": subject}

    worker_a = WorkerConsumer(consumer_name=f"worker-3f3-A-{uid}", stream_name=stream, group_name=group, redis_client=r)
    worker_b = WorkerConsumer(consumer_name=f"worker-3f3-B-{uid}", stream_name=stream, group_name=group, redis_client=r)

    # Run both workers simultaneously
    results = await asyncio.gather(
        worker_a.poll_and_process(count=1, block_ms=1000, process_class_fn=mock_process_class),
        worker_b.poll_and_process(count=1, block_ms=1000, process_class_fn=mock_process_class),
    )

    assert sum(results) == 2, f"Expected 2 jobs processed, got {results}"
    assert "CSE101_S1" in starts and "CSE102_S1" in starts, "Both jobs must have started"

    start_delta_ms = abs(starts["CSE101_S1"] - starts["CSE102_S1"]) * 1000.0
    pel = get_pending_jobs(client=r, stream_name=stream, group_name=group)
    pel_count = len(pel)

    print(f"Scenario 1: Start time delta between Worker A and Worker B: {start_delta_ms:.1f}ms")
    print(f"Scenario 1: Both jobs completed. PEL remaining: {pel_count}")

    assert start_delta_ms < 200.0, f"Workers did not start concurrently! Delta: {start_delta_ms:.1f}ms (threshold 200ms)"
    assert pel_count == 0, f"PEL must be 0 after successful ACKs, got {pel_count}"
    print("PASS: Scenario 1 — Multi-worker concurrent consumption verified.")
    return True


# ---------------------------------------------------------------------------
# Scenario 2: Single-Worker Bounded Concurrency (max_concurrent_jobs=2)
# ---------------------------------------------------------------------------
async def test_scenario_2_single_worker_bounded_concurrency():
    """
    Single WorkerConsumer with max_concurrent_jobs=2 processes 5 synthetic jobs across 5 users.
    Validates bounded concurrency (peak active tasks <= 2), capacity awareness, and clean completion.
    """
    print("\n--- Scenario 2: Single-Worker Bounded Concurrency (max_concurrent_jobs=2) ---")
    r = get_test_redis()
    uid = uuid.uuid4().hex[:8]
    stream = f"cloudnote:test:step3f3:s2:{uid}"
    group = f"group:s2:{uid}"
    ensure_consumer_group(client=r, stream_name=stream, group_name=group)

    users = [91010 + i for i in range(5)]
    for u in users:
        insert_synthetic_user(u, f"std_{u}")
        enqueue_job(
            user_id=u,
            class_metadata={"subject_code": f"CSE20{u % 10}", "room": "38-601", "start_time": "10:00", "end_time": "11:00"},
            client=r, stream_name=stream,
        )

    active_count = 0
    max_observed_active = 0
    completed_jobs = 0
    lock = asyncio.Lock()

    async def mock_process_class(user_id: int, lpu_username: str, lpu_password: str, class_metadata: dict):
        nonlocal active_count, max_observed_active, completed_jobs
        async with lock:
            active_count += 1
            if active_count > max_observed_active:
                max_observed_active = active_count

        # Simulate execution work
        await asyncio.sleep(0.2)

        async with lock:
            active_count -= 1
            completed_jobs += 1
        return {"status": "COMPLETED"}

    worker = WorkerConsumer(
        consumer_name=f"worker-bounded-{uid}",
        stream_name=stream,
        group_name=group,
        redis_client=r,
        max_concurrent_jobs=2,
    )

    # Poll until all 5 jobs are dispatched/processed
    dispatched_total = 0
    for _ in range(10):
        dispatched = await worker.poll_and_process(count=2, block_ms=500, process_class_fn=mock_process_class)
        dispatched_total += dispatched
        if completed_jobs >= 5:
            break
        await asyncio.sleep(0.05)

    # Await any remaining active background tasks
    await worker.wait_for_active_tasks(timeout=5.0)

    pel = get_pending_jobs(client=r, stream_name=stream, group_name=group)
    pel_count = len(pel)

    print(f"Scenario 2: Completed {completed_jobs}/5 jobs. Peak concurrent active: {max_observed_active}. PEL: {pel_count}")

    assert completed_jobs == 5, f"Expected 5 completed jobs, got {completed_jobs}"
    assert max_observed_active <= 2, f"Concurrency limit violated! Peak active: {max_observed_active} > 2"
    assert max_observed_active == 2, f"Expected 2 concurrent slots to be utilized, got peak {max_observed_active}"
    assert pel_count == 0, f"Expected PEL count 0, got {pel_count}"
    print("PASS: Scenario 2 — Single-worker bounded concurrency verified (peak <= 2).")
    return True


# ---------------------------------------------------------------------------
# Scenario 3: Execution-Lock Duplicate Race Under Async Worker
# ---------------------------------------------------------------------------
async def test_scenario_3_execution_lock_race():
    """
    Inject 2 duplicate stream messages for the exact same user and class.
    Dispatched concurrently into WorkerConsumer(max_concurrent_jobs=2).
    Proves: 1 acquires lock and executes; 2nd detects lock held, safely ACKs (is_reclaimed=False);
    Total executions=1, ACKs=2, PEL=0.
    """
    print("\n--- Scenario 3: Execution-Lock Duplicate Race Under Async Worker ---")
    r = get_test_redis()
    uid = uuid.uuid4().hex[:8]
    stream = f"cloudnote:test:step3f3:s3:{uid}"
    group = f"group:s3:{uid}"
    ensure_consumer_group(client=r, stream_name=stream, group_name=group)

    user_id = 91020
    insert_synthetic_user(user_id, f"std_{user_id}")
    class_meta = {"subject_code": "CSE301_RACE", "room": "38-603", "start_time": "11:00", "end_time": "12:00"}

    # Inject 2 duplicate messages directly to stream (bypassing client idempotency key to test worker guard)
    job_1 = IngestionJob(
        job_id=f"job-dup-1-{uid}",
        user_id=user_id,
        class_metadata=class_meta,
        created_at="2026-09-21T10:00:00Z",
    )
    job_2 = IngestionJob(
        job_id=f"job-dup-2-{uid}",
        user_id=user_id,
        class_metadata=class_meta,
        created_at="2026-09-21T10:00:00Z",
    )
    r.xadd(stream, {"payload": job_1.to_json()})
    r.xadd(stream, {"payload": job_2.to_json()})

    executions = 0
    lock = asyncio.Lock()

    async def mock_process_class(user_id: int, lpu_username: str, lpu_password: str, class_metadata: dict):
        nonlocal executions
        async with lock:
            executions += 1
        await asyncio.sleep(0.3)
        return {"status": "COMPLETED"}

    worker = WorkerConsumer(
        consumer_name=f"worker-race-{uid}",
        stream_name=stream,
        group_name=group,
        redis_client=r,
        max_concurrent_jobs=2,
    )

    # Poll both messages in a single iteration
    dispatched = await worker.poll_and_process(count=2, block_ms=500, process_class_fn=mock_process_class)
    await worker.wait_for_active_tasks(timeout=5.0)

    pel = get_pending_jobs(client=r, stream_name=stream, group_name=group)
    pel_count = len(pel)

    print(f"Scenario 3: Dispatched: {dispatched}, Executions: {executions}, PEL remaining: {pel_count}")

    assert executions == 1, f"Expected exactly 1 execution, got {executions}"
    assert pel_count == 0, f"Expected both messages ACKed (active job + duplicate drained), PEL: {pel_count}"
    print("PASS: Scenario 3 — Execution-lock duplicate race defended cleanly (1 execution, 2 ACKs).")
    return True


# ---------------------------------------------------------------------------
# Scenario 4: Worker Crash & XAUTOCLAIM Stale Recovery
# ---------------------------------------------------------------------------
async def test_scenario_4_worker_crash_and_xautoclaim_recovery():
    """
    Worker A crashes unexpectedly during job processing -> message left unACKed in PEL.
    After idle timeout, Worker B reclaims via XAUTOCLAIM -> completes -> ACKs -> PEL=0.
    """
    print("\n--- Scenario 4: Worker Crash & XAUTOCLAIM Recovery ---")
    r = get_test_redis()
    uid = uuid.uuid4().hex[:8]
    stream = f"cloudnote:test:step3f3:s4:{uid}"
    group = f"group:s4:{uid}"
    ensure_consumer_group(client=r, stream_name=stream, group_name=group)

    user_id = 91030
    insert_synthetic_user(user_id, f"std_{user_id}")
    job_id = enqueue_job(
        user_id=user_id,
        class_metadata={"subject_code": "CSE401_CRASH", "room": "38-604", "start_time": "12:00", "end_time": "13:00"},
        client=r, stream_name=stream,
    )

    # Worker A: crashes unexpectedly
    worker_a = WorkerConsumer(
        consumer_name=f"worker-crash-A-{uid}",
        stream_name=stream,
        group_name=group,
        redis_client=r,
        idle_timeout_ms=500,
        max_concurrent_jobs=1,
    )

    async def mock_crash_fn(user_id: int, lpu_username: str, lpu_password: str, class_metadata: dict):
        raise RuntimeError("Simulated unhandled worker crash during processing")

    try:
        await worker_a.poll_and_process(count=1, block_ms=500, process_class_fn=mock_crash_fn)
    except RuntimeError:
        pass

    # Verify message is still pending in PEL
    pel_after_crash = get_pending_jobs(client=r, stream_name=stream, group_name=group)
    pending_count = len(pel_after_crash)
    print(f"Scenario 4: PEL count after Worker A crash: {pending_count}")
    assert pending_count == 1, f"Job was lost! Expected 1 pending message in PEL, got {pending_count}"

    # Wait for idle timeout (500ms)
    await asyncio.sleep(0.7)

    # Worker B: reclaims and finishes cleanly
    worker_b_executed = False
    async def mock_recovery_fn(user_id: int, lpu_username: str, lpu_password: str, class_metadata: dict):
        nonlocal worker_b_executed
        worker_b_executed = True
        return {"status": "COMPLETED"}

    worker_b = WorkerConsumer(
        consumer_name=f"worker-recovery-B-{uid}",
        stream_name=stream,
        group_name=group,
        redis_client=r,
        idle_timeout_ms=500,
        max_concurrent_jobs=1,
    )

    recovered = await worker_b.poll_and_process(count=1, block_ms=500, process_class_fn=mock_recovery_fn, check_stale=True)
    pel_after_recovery = get_pending_jobs(client=r, stream_name=stream, group_name=group)
    final_pel = len(pel_after_recovery)

    print(f"Scenario 4: Worker B recovered: {recovered}, Executed: {worker_b_executed}, Final PEL: {final_pel}")

    assert recovered == 1, f"Worker B failed to reclaim job, recovered count: {recovered}"
    assert worker_b_executed is True, "Worker B did not execute recovery function"
    assert final_pel == 0, f"Expected final PEL 0, got {final_pel}"
    print("PASS: Scenario 4 — Crash & XAUTOCLAIM recovery verified cleanly.")
    return True


# ---------------------------------------------------------------------------
# Scenario 5: Multi-User State Isolation Under Concurrent Execution
# ---------------------------------------------------------------------------
async def test_scenario_5_multi_user_isolation():
    """
    4 synthetic users execute concurrent ingestion jobs via WorkerConsumer(max_concurrent_jobs=4).
    Verifies 100% data partition isolation:
    - User credentials securely resolved in-memory per user
    - Scoped attendance records in SQLite (1 per user)
    - User-partitioned log directories (logs/<user_id>)
    - Zero cross-user record leakage
    """
    print("\n--- Scenario 5: Multi-User State Isolation Under Concurrency ---")
    r = get_test_redis()
    uid = uuid.uuid4().hex[:8]
    stream = f"cloudnote:test:step3f3:s5:{uid}"
    group = f"group:s5:{uid}"
    ensure_consumer_group(client=r, stream_name=stream, group_name=group)

    users = [92001, 92002, 92003, 92004]
    passwords = {
        92001: "SecretAlpha_92001!",
        92002: "SecretBeta_92002!",
        92003: "SecretGamma_92003!",
        92004: "SecretDelta_92004!",
    }

    for u in users:
        insert_synthetic_user(u, f"std_{u}", passwords[u])
        enqueue_job(
            user_id=u,
            class_metadata={"subject_code": f"CSE_ISO_{u}", "room": "38-605", "start_time": "13:00", "end_time": "14:00"},
            client=r, stream_name=stream,
        )

    isolation_violations = []

    async def mock_isolated_process(user_id: int, lpu_username: str, lpu_password: str, class_metadata: dict):
        # 1. Verify credential isolation
        if lpu_password != passwords.get(user_id):
            isolation_violations.append(f"Credential mismatch for user {user_id}: got wrong password")
        if lpu_username != f"std_{user_id}":
            isolation_violations.append(f"Username mismatch for user {user_id}: {lpu_username}")

        # 2. Write attendance record to SQLite
        record_attendance_session(
            user_id=user_id,
            subject=class_metadata["subject_code"],
            status="COMPLETED",
            title="Lecture",
            session_duration="50m",
        )

        # 3. Write user-partitioned log file
        log_dir = get_user_logs_dir(user_id)
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"session_{user_id}.log")
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(f"User {user_id} session logged successfully.\n")

        # 4. Verify SQLite isolation: only this user's records should appear in scoped query
        records = get_user_attendance_records(user_id)
        for rec in records:
            if rec.get("user_id") != user_id:
                isolation_violations.append(f"Cross-user record contamination! User {user_id} saw record for {rec.get('user_id')}")

        await asyncio.sleep(0.1)
        return {"status": "COMPLETED"}

    worker = WorkerConsumer(
        consumer_name=f"worker-iso-{uid}",
        stream_name=stream,
        group_name=group,
        redis_client=r,
        max_concurrent_jobs=4,
    )

    # Process all 4 jobs
    dispatched = 0
    for _ in range(5):
        dispatched += await worker.poll_and_process(count=4, block_ms=500, process_class_fn=mock_isolated_process)
        if dispatched >= 4:
            break
        await asyncio.sleep(0.05)

    await worker.wait_for_active_tasks(timeout=5.0)

    pel = get_pending_jobs(client=r, stream_name=stream, group_name=group)
    pel_count = len(pel)

    print(f"Scenario 5: Dispatched: {dispatched}, Isolation violations: {len(isolation_violations)}, PEL: {pel_count}")

    assert len(isolation_violations) == 0, f"Isolation failures: {isolation_violations}"
    assert pel_count == 0, f"Expected PEL 0, got {pel_count}"

    # Verify each user has exactly 1 attendance record
    for u in users:
        records = get_user_attendance_records(u)
        assert len(records) == 1, f"Expected 1 record for user {u}, got {len(records)}"

    print("PASS: Scenario 5 — 100% data partition isolation verified across concurrent users.")
    return True


# ---------------------------------------------------------------------------
# Scenario 6: Graceful Shutdown Timeout / Cancelled Task Recovery Semantics
# ---------------------------------------------------------------------------
async def test_scenario_6_shutdown_recovery_semantics():
    """
    Simulates a long-running job exceeding the graceful shutdown timeout.
    Verifies:
    1. Active task is cancelled without emitting ACK.
    2. Execution lock is released in finally block.
    3. Job remains unacknowledged in PEL.
    4. Second worker can reclaim the unACKed job once idle.
    """
    print("\n--- Scenario 6: Shutdown Timeout & Recovery Semantics ---")
    r = get_test_redis()
    uid = uuid.uuid4().hex[:8]
    stream = f"cloudnote:test:step3f3:s6:{uid}"
    group = f"group:s6:{uid}"
    ensure_consumer_group(client=r, stream_name=stream, group_name=group)

    user_id = 91040
    insert_synthetic_user(user_id, f"std_{user_id}")
    job_id = enqueue_job(
        user_id=user_id,
        class_metadata={"subject_code": "CSE501_LONG", "room": "38-606", "start_time": "14:00", "end_time": "15:00"},
        client=r, stream_name=stream,
    )

    started_evt = asyncio.Event()

    async def mock_long_lecture(user_id: int, lpu_username: str, lpu_password: str, class_metadata: dict):
        started_evt.set()
        await asyncio.sleep(10.0)  # Long-running job
        return {"status": "COMPLETED"}

    worker_1 = WorkerConsumer(
        consumer_name=f"worker-shutdown-{uid}",
        stream_name=stream,
        group_name=group,
        redis_client=r,
        max_concurrent_jobs=1,
        idle_timeout_ms=500,
    )

    # Dispatch with async_dispatch=True
    await worker_1.poll_and_process(count=1, block_ms=500, process_class_fn=mock_long_lecture, async_dispatch=True)
    await started_evt.wait()

    # Verify lock is held
    lock_key = get_execution_lock_key(user_id, {"subject_code": "CSE501_LONG", "start_time": "14:00", "end_time": "15:00"})
    lock_holder = r.get(lock_key)
    assert lock_holder is not None, "Execution lock should be held during processing"

    # Trigger shutdown with small timeout (0.3s)
    await worker_1.wait_for_active_tasks(timeout=0.3)

    # Verify: lock was released in finally block
    lock_after = r.get(lock_key)
    print(f"Scenario 6: Lock after shutdown cancellation: {lock_after}")
    assert lock_after is None, "Execution lock must be released in finally block upon cancellation!"

    # Verify: job was NOT ACKed, remains in PEL
    pel_after = get_pending_jobs(client=r, stream_name=stream, group_name=group)
    pel_count = len(pel_after)
    print(f"Scenario 6: PEL count after shutdown cancellation: {pel_count}")
    assert pel_count == 1, f"Cancelled job must NOT be ACKed! Expected PEL=1, got {pel_count}"

    # Wait for idle timeout
    await asyncio.sleep(0.7)

    # Worker 2 reclaims and completes
    worker_2_completed = False
    async def mock_w2(user_id: int, lpu_username: str, lpu_password: str, class_metadata: dict):
        nonlocal worker_2_completed
        worker_2_completed = True
        return {"status": "COMPLETED"}

    worker_2 = WorkerConsumer(
        consumer_name=f"worker-reclaim-{uid}",
        stream_name=stream,
        group_name=group,
        redis_client=r,
        max_concurrent_jobs=1,
        idle_timeout_ms=500,
    )

    recovered = await worker_2.poll_and_process(count=1, block_ms=500, process_class_fn=mock_w2, check_stale=True)
    pel_final = get_pending_jobs(client=r, stream_name=stream, group_name=group)
    final_count = len(pel_final)

    print(f"Scenario 6: Worker 2 recovered: {recovered}, Completed: {worker_2_completed}, Final PEL: {final_count}")
    assert recovered == 1
    assert worker_2_completed is True
    assert final_count == 0
    print("PASS: Scenario 6 — Shutdown timeout preserved PEL and permitted clean reclamation.")
    return True


# ---------------------------------------------------------------------------
# Main Runner
# ---------------------------------------------------------------------------
async def main():
    print("================================================================================")
    print("STARTING STEP 3F.3 NATIVE MULTI-WORKER SCALING & CONCURRENCY VALIDATION")
    print("================================================================================")
    init_db()
    cleanup_synthetic_test_data()

    try:
        await test_scenario_1_multi_worker_concurrency()
        await test_scenario_2_single_worker_bounded_concurrency()
        await test_scenario_3_execution_lock_race()
        await test_scenario_4_worker_crash_and_xautoclaim_recovery()
        await test_scenario_5_multi_user_isolation()
        await test_scenario_6_shutdown_recovery_semantics()

        print("\n================================================================================")
        print("ALL STEP 3F.3 VALIDATION SCENARIOS PASSED CLEANLY!")
        print("================================================================================")
    finally:
        cleanup_synthetic_test_data()


if __name__ == "__main__":
    asyncio.run(main())
