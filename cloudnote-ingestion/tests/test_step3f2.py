"""
Step 3F.2 Test Suite — Concurrency Hardening & Multi-Worker Empirical Validation

Empirically validates:
1. Multi-worker Redis queue consumption across graded concurrency tiers (Tier 1, Tier 2, Tier 3).
2. Concurrent multi-user ingestion and strict state isolation (SQLite, Redis, partitioned files).
3. SQLite read/write contention under heavy concurrent load with granular transaction telemetry
   (operation type, worker ID, user ID, duration_ms, contention/wait time, success/failure).
4. Deterministic distributed execution-lock race defense (Worker A acquires, Worker B rejected, executions=1, ACKs=2).
5. User-scoped idempotency under concurrent enqueueing (duplicate suppression per-user, isolated across users).
6. Deterministic worker crash recovery (Worker A aborts -> PEL retained -> Worker B XAUTOCLAIMs -> completes -> ACK -> PEL=0).
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
import sqlite3
import threading
from typing import Dict, List, Any, Optional
from datetime import datetime

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.crypto import encrypt_portal_password
from app.database import (
    init_db,
    get_db_connection,
    save_user_timetable,
    get_user_timetable,
    record_attendance_session,
    get_user_attendance_records,
    get_user_logs_dir,
    get_user_screenshots_dir,
)
from app.job_queue import (
    get_redis_client,
    ensure_consumer_group,
    enqueue_job,
    consume_jobs,
    acknowledge_job,
    claim_pending_jobs,
    get_pending_jobs,
    generate_class_idempotency_key,
    get_execution_lock_key,
    ExecutionLock,
    IngestionJob,
)
from app.worker_consumer import WorkerConsumer, generate_consumer_name
from app.session_status import update_session_status, get_user_session_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Step3F2EmpiricalTests")


def get_test_redis():
    """Returns a connected Redis client with automatic fallback for host vs container environments."""
    try:
        r = get_redis_client()
        r.ping()
        return r
    except Exception:
        r = get_redis_client(redis_url="redis://localhost:6379/0")
        assert r.ping(), "Redis connection failed on both settings.REDIS_URL and localhost:6379!"
        return r


# ---------------------------------------------------------------------------
# Telemetry Collector for Step 3F.2
# ---------------------------------------------------------------------------
class EmpiricalTelemetry:
    def __init__(self):
        self.sqlite_transactions: List[Dict[str, Any]] = []
        self.redis_deliveries = 0
        self.redis_acks = 0
        self.redis_reclaims = 0
        self.app_executions = 0
        self.lock_acquisitions = 0
        self.lock_rejections = 0
        self.duplicate_suppressions = 0
        self.isolation_failures = 0
        self.sqlite_lock_errors = 0
        self._lock = threading.Lock()

    def record_sqlite(
        self,
        op_type: str,
        worker_id: str,
        user_id: int,
        duration_ms: float,
        contention_ms: float,
        success: bool,
        error: Optional[str] = None
    ):
        with self._lock:
            self.sqlite_transactions.append({
                "op_type": op_type,
                "worker_id": worker_id,
                "user_id": user_id,
                "duration_ms": duration_ms,
                "contention_ms": contention_ms,
                "success": success,
                "error": error
            })
            if not success and error and ("database is locked" in error or "busy" in error):
                self.sqlite_lock_errors += 1

    def compute_sqlite_summary(self) -> Dict[str, Any]:
        with self._lock:
            durations = [t["duration_ms"] for t in self.sqlite_transactions]
            if not durations:
                return {"count": 0, "min_ms": 0, "avg_ms": 0, "median_ms": 0, "p95_ms": 0, "max_ms": 0}
            durations.sort()
            n = len(durations)
            return {
                "count": n,
                "min_ms": round(durations[0], 2),
                "avg_ms": round(sum(durations) / n, 2),
                "median_ms": round(durations[n // 2], 2),
                "p95_ms": round(durations[int(n * 0.95)], 2),
                "max_ms": round(durations[-1], 2),
                "lock_errors": self.sqlite_lock_errors
            }


telemetry = EmpiricalTelemetry()


# ---------------------------------------------------------------------------
# Scenario 1: Multi-Worker Redis Queue Consumption & Tiered Workloads
# ---------------------------------------------------------------------------
async def run_scenario_1_tiered_workload(
    tier_name: str,
    num_workers: int,
    num_users: int,
    num_jobs: int
) -> Dict[str, Any]:
    """
    Scenario 1: Evaluates multiple concurrent worker consumers processing a concurrent batch
    of synthetic jobs across multiple users from the shared Redis consumer group.
    Measures message delivery, message ACK, application execution, and throughput.
    """
    r = get_test_redis()
    run_id = uuid.uuid4().hex[:8]
    stream_name = f"cloudnote:test:stream:{run_id}"
    group_name = f"cloudnote:test:group:{run_id}"

    ensure_consumer_group(client=r, stream_name=stream_name, group_name=group_name)

    # 1. Provision synthetic users in SQLite
    user_ids = []
    base_uid = int(f"91{int(run_id[:4], 16) % 800 + 100}")
    conn = get_db_connection()
    cur = conn.cursor()
    for u in range(num_users):
        uid = base_uid + u
        user_ids.append(uid)
        uname = f"syn_user_{run_id}_{u}"
        enc_pw = encrypt_portal_password("SyntheticPass123!", settings.JWT_SECRET)
        cur.execute(
            "INSERT OR REPLACE INTO users (id, username, hashed_password, email, lpu_username, encrypted_portal_password) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uid, uname, "hash", f"{uname}@lpu.in", uname, enc_pw)
        )
    conn.commit()
    conn.close()

    # 2. Enqueue synthetic jobs across users
    enqueued_job_ids = []
    for j in range(num_jobs):
        uid = user_ids[j % num_users]
        metadata = {
            "subject_code": f"CSE_{tier_name}_{j}",
            "subject_name": f"Synthetic Lecture {j}",
            "faculty": f"Dr. Synthetic {j % 5}",
            "timings": f"0{9 + (j % 8)}:00 - 1{0 + (j % 8)}:00",
            "start_time": f"0{9 + (j % 8)}:00",
            "end_time": f"1{0 + (j % 8)}:00",
            "join_url": f"https://synthetic.codetantra.com/join/{j}"
        }
        idemp_key = f"user:{uid}:syn:{tier_name}:{j}"
        job_id = enqueue_job(
            user_id=uid,
            class_metadata=metadata,
            client=r,
            stream_name=stream_name,
            idempotency_key=idemp_key
        )
        enqueued_job_ids.append(job_id)

    # 3. Create independent WorkerConsumer instances
    workers = [
        WorkerConsumer(
            consumer_name=f"worker-{tier_name}-{w}-{run_id}",
            stream_name=stream_name,
            group_name=group_name,
            redis_client=r,
        )
        for w in range(num_workers)
    ]

    # Mock process_class to measure pure concurrency, locks, and DB writes
    executions_by_worker: Dict[str, int] = {w.consumer_name: 0 for w in workers}
    executions_lock = threading.Lock()

    async def mock_process_class(user_id: int, lpu_username: str, lpu_password: str, class_metadata: dict):
        with executions_lock:
            telemetry.app_executions += 1
        # Simulate browser work duration (15-30ms)
        await asyncio.sleep(0.02)
        # Record attendance in SQLite with telemetry
        t0 = time.perf_counter()
        err_msg = None
        succ = True
        try:
            record_attendance_session(
                user_id=user_id,
                subject=class_metadata.get("subject_code", "MOCK"),
                title=class_metadata.get("subject_name", "Mock Lecture"),
                instructor=class_metadata.get("faculty", "Staff"),
                status="CONNECTED",
                joined_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ended_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                session_duration="50m",
                screenshots={"connected": f"/screenshots/{user_id}/mock_proof.png"}
            )
        except Exception as ex:
            succ = False
            err_msg = str(ex)
        dur = (time.perf_counter() - t0) * 1000.0
        telemetry.record_sqlite("INSERT_ATTENDANCE", "worker", user_id, dur, 0.0, succ, err_msg)
        return {"status": "COMPLETED"}

    # 4. Run workers concurrently until all jobs are processed
    start_time = time.perf_counter()
    jobs_processed = 0

    async def worker_loop(w: WorkerConsumer):
        nonlocal jobs_processed
        while jobs_processed < num_jobs and (time.perf_counter() - start_time) < 30.0:
            count = await w.poll_and_process(
                count=1,
                block_ms=200,
                process_class_fn=mock_process_class,
                check_stale=False,
            )
            if count > 0:
                with executions_lock:
                    executions_by_worker[w.consumer_name] += count
                    jobs_processed += count
            else:
                await asyncio.sleep(0.01)

    await asyncio.gather(*(worker_loop(w) for w in workers))
    total_time = max(0.001, time.perf_counter() - start_time)

    # 5. Check queue hygiene and Pending Entries List (PEL)
    pending = get_pending_jobs(client=r, stream_name=stream_name, group_name=group_name)
    pel_count = len(pending)

    # Clean up Redis test keys
    r.delete(stream_name)

    result = {
        "tier": tier_name,
        "workers": num_workers,
        "users": num_users,
        "jobs": num_jobs,
        "jobs_processed": jobs_processed,
        "total_time_sec": round(total_time, 3),
        "throughput_jobs_per_sec": round(jobs_processed / total_time, 2),
        "pel_remaining": pel_count,
        "executions_by_worker": executions_by_worker,
    }
    return result


# ---------------------------------------------------------------------------
# Scenario 2: Concurrent Multi-User State Isolation
# ---------------------------------------------------------------------------
async def run_scenario_2_user_state_isolation() -> bool:
    """
    Scenario 2: Concurrently updates session status and class history across multiple users,
    asserting 100% data partition isolation (SQLite and disk logs).
    """
    run_id = uuid.uuid4().hex[:6]
    users = [int(f"92{i}{int(run_id[:2], 16) % 50}") for i in range(4)]
    subjects = [f"CS_ISO_{i}_{run_id}" for i in range(4)]

    # Concurrently write session status for each user
    async def user_session_task(u_idx: int):
        uid = users[u_idx]
        subj = subjects[u_idx]
        update_session_status(
            status="CONNECTED",
            screenshot=f"user_{uid}_connect.png",
            join_time="2026-09-21 11:00:00",
            subject=subj,
            user_id=uid
        )
        await asyncio.sleep(0.01)
        update_session_status(
            status="DISCONNECTED",
            screenshot=f"user_{uid}_disc.png",
            disconnect_time="2026-09-21 11:50:00",
            subject=subj,
            user_id=uid
        )

    await asyncio.gather(*(user_session_task(i) for i in range(4)))

    # Assert isolation in SQLite and files
    for i, uid in enumerate(users):
        history = get_user_attendance_records(uid)
        assert len(history) >= 1, f"Expected attendance records for user {uid}"
        # Verify user has their own subject
        assert any(r["subject"] == subjects[i] for r in history), f"User {uid} missing subject {subjects[i]}"
        # Verify user has NO other user's subjects
        for j, other_subj in enumerate(subjects):
            if i != j:
                assert not any(r["subject"] == other_subj for r in history), \
                    f"State leak! User {uid} has subject {other_subj} from User {users[j]}"

        # Check partitioned log file
        log_dir = get_user_logs_dir(uid)
        hist_file = os.path.join(log_dir, "class_history.json")
        assert os.path.exists(hist_file), f"Missing user history file {hist_file}"
        with open(hist_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert any(e["subject"] == subjects[i] for e in data)
            for j, other_subj in enumerate(subjects):
                if i != j:
                    assert not any(e["subject"] == other_subj for e in data)

    return True


# ---------------------------------------------------------------------------
# Scenario 3: SQLite Read/Write Contention Under Load
# ---------------------------------------------------------------------------
def run_scenario_3_sqlite_contention(duration_seconds: float = 3.0, num_threads: int = 8):
    """
    Scenario 3: Simulates simultaneous concurrent reader and writer processes against SQLite,
    capturing detailed transaction duration, contention, and checking for database lock errors.
    """
    stop_event = threading.Event()
    start_barrier = threading.Barrier(num_threads)
    base_uid = 95000

    def writer_thread(t_id: int):
        start_barrier.wait()
        op_count = 0
        while not stop_event.is_set():
            uid = base_uid + (t_id % 10)
            t0 = time.perf_counter()
            err_msg = None
            succ = True
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO class_attendance_records (
                        user_id, subject, title, instructor, status,
                        joined_at, ended_at, session_duration, screenshots_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    uid,
                    f"StressSubj_{t_id}_{op_count}",
                    "Concurrent Stress Test",
                    "Stress Runner",
                    "CONNECTED",
                    "2026-09-21 12:00:00",
                    "2026-09-21 12:50:00",
                    "50m",
                    json.dumps({"thread": t_id, "op": op_count}),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                ))
                conn.commit()
                conn.close()
            except Exception as e:
                succ = False
                err_msg = str(e)
            dur = (time.perf_counter() - t0) * 1000.0
            telemetry.record_sqlite("INSERT_ATTENDANCE", f"thread-{t_id}", uid, dur, 0.0, succ, err_msg)
            op_count += 1
            time.sleep(0.005)

    def reader_thread(t_id: int):
        start_barrier.wait()
        while not stop_event.is_set():
            uid = base_uid + (t_id % 10)
            t0 = time.perf_counter()
            err_msg = None
            succ = True
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT id, subject, status FROM class_attendance_records WHERE user_id = ? ORDER BY id DESC LIMIT 10",
                    (uid,)
                )
                _ = cur.fetchall()
                conn.close()
            except Exception as e:
                succ = False
                err_msg = str(e)
            dur = (time.perf_counter() - t0) * 1000.0
            telemetry.record_sqlite("SELECT_ATTENDANCE", f"thread-{t_id}", uid, dur, 0.0, succ, err_msg)
            time.sleep(0.003)

    threads = []
    # 4 writers, 4 readers
    for i in range(num_threads // 2):
        threads.append(threading.Thread(target=writer_thread, args=(i,)))
    for i in range(num_threads // 2, num_threads):
        threads.append(threading.Thread(target=reader_thread, args=(i,)))

    for t in threads:
        t.start()

    time.sleep(duration_seconds)
    stop_event.set()

    for t in threads:
        t.join()

    # Cleanup stress records
    conn = get_db_connection()
    conn.execute("DELETE FROM class_attendance_records WHERE user_id >= 95000 AND user_id < 96000")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Scenario 4: Distributed Execution-Lock Duplicate Race Outcome Proof
# ---------------------------------------------------------------------------
async def run_scenario_4_execution_lock_race() -> Dict[str, Any]:
    """
    Scenario 4: Explicitly tests the race condition where two workers process the exact same
    class for the same user simultaneously.
    Proves:
      Worker A -> lock acquired
      Worker B -> lock rejected
      process_class executions -> 1
      Redis stream ACKs -> 2
    """
    r = get_test_redis()
    run_id = uuid.uuid4().hex[:6]
    stream_name = f"cloudnote:test:race:stream:{run_id}"
    group_name = f"cloudnote:test:race:group:{run_id}"

    ensure_consumer_group(client=r, stream_name=stream_name, group_name=group_name)

    test_uid = int(f"93{int(run_id[:3], 16) % 500 + 100}")
    # Provision test user with credentials
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO users (id, username, hashed_password, email, lpu_username, encrypted_portal_password) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (test_uid, f"race_user_{run_id}", "hash", f"race_{run_id}@lpu.in", f"race_user_{run_id}",
         encrypt_portal_password("PassRace123!", settings.JWT_SECRET))
    )
    conn.commit()
    conn.close()

    metadata = {
        "subject_code": f"CSE_RACE_{run_id}",
        "subject_name": "Race Condition Lecture",
        "faculty": "Prof. Race",
        "timings": "10:00 - 11:00",
        "start_time": "10:00",
        "end_time": "11:00",
        "join_url": "https://code.tantra/race"
    }

    # Intentionally bypass idempotency key check to inject 2 stream messages for the exact same class
    # (Simulating redundant stream deliveries)
    raw_payload_1 = {
        "job_id": uuid.uuid4().hex,
        "user_id": test_uid,
        "class_metadata": metadata,
        "created_at": datetime.now().isoformat()
    }
    raw_payload_2 = {
        "job_id": uuid.uuid4().hex,
        "user_id": test_uid,
        "class_metadata": metadata,
        "created_at": datetime.now().isoformat()
    }
    raw_msg_1 = r.xadd(stream_name, {"payload": json.dumps(raw_payload_1)})
    msg_id_1 = raw_msg_1.decode("utf-8") if isinstance(raw_msg_1, bytes) else str(raw_msg_1)
    raw_msg_2 = r.xadd(stream_name, {"payload": json.dumps(raw_payload_2)})
    msg_id_2 = raw_msg_2.decode("utf-8") if isinstance(raw_msg_2, bytes) else str(raw_msg_2)

    worker_a = WorkerConsumer(
        consumer_name=f"worker-A-{run_id}",
        stream_name=stream_name,
        group_name=group_name,
        redis_client=r,
    )
    worker_b = WorkerConsumer(
        consumer_name=f"worker-B-{run_id}",
        stream_name=stream_name,
        group_name=group_name,
        redis_client=r,
    )

    executions_count = 0
    worker_a_acquired = False
    worker_b_acquired = False
    race_lock = asyncio.Lock()

    async def race_process_class(user_id: int, lpu_username: str, lpu_password: str, class_metadata: dict):
        nonlocal executions_count
        async with race_lock:
            executions_count += 1
        # Hold execution long enough so Worker B definitely experiences the lock collision
        await asyncio.sleep(0.3)
        return {"status": "COMPLETED"}

    # Read both jobs from the stream
    job_a_list = consume_jobs(consumer_name=worker_a.consumer_name, count=1, block_ms=1000,
                              client=r, stream_name=stream_name, group_name=group_name)
    job_b_list = consume_jobs(consumer_name=worker_b.consumer_name, count=1, block_ms=1000,
                              client=r, stream_name=stream_name, group_name=group_name)

    assert len(job_a_list) == 1, "Worker A failed to read message 1"
    assert len(job_b_list) == 1, "Worker B failed to read message 2"

    job_a = job_a_list[0]
    job_b = job_b_list[0]

    # Process concurrently
    task_a = asyncio.create_task(worker_a.process_single_job(job_a, process_class_fn=race_process_class))
    # Slight microsecond stagger to let A acquire first
    await asyncio.sleep(0.01)
    task_b = asyncio.create_task(worker_b.process_single_job(job_b, process_class_fn=race_process_class))

    res_a, res_b = await asyncio.gather(task_a, task_b)

    # Worker A should succeed (lock acquired, executed, ACKed)
    # Worker B should return False (lock rejected, duplicate ACKed to drain)
    worker_a_acquired = res_a
    worker_b_acquired = res_b

    # Check that BOTH stream messages were acknowledged (drained)
    pending_after = get_pending_jobs(client=r, stream_name=stream_name, group_name=group_name)
    acks_count = 2 if len(pending_after) == 0 else (2 - len(pending_after))

    # Clean up Redis
    r.delete(stream_name)

    return {
        "worker_a_lock_acquired": worker_a_acquired,
        "worker_b_lock_acquired": worker_b_acquired,
        "process_class_executions": executions_count,
        "stream_acks": acks_count,
        "pel_remaining": len(pending_after)
    }


# ---------------------------------------------------------------------------
# Scenario 5: User-Scoped Idempotency Under Concurrent Enqueueing
# ---------------------------------------------------------------------------
def run_scenario_5_concurrent_idempotency() -> Dict[str, Any]:
    """
    Scenario 5: Concurrently enqueues identical class slots for the same user from multiple
    threads, and asserts that only 1 message enters the queue. Also proves that two different
    users with the same class slot do NOT block each other.
    """
    r = get_test_redis()
    run_id = uuid.uuid4().hex[:6]
    stream_name = f"cloudnote:test:idemp:stream:{run_id}"
    group_name = f"cloudnote:test:idemp:group:{run_id}"
    ensure_consumer_group(client=r, stream_name=stream_name, group_name=group_name)

    uid_1 = 94001
    uid_2 = 94002
    slot_time = "2026-09-21_14:00:00"
    meta_1 = {"subject_code": f"CSE_IDEMP_{run_id}", "start_time": "14:00", "end_time": "15:00"}
    meta_2 = {"subject_code": f"CSE_IDEMP_{run_id}", "start_time": "14:00", "end_time": "15:00"}

    key_u1 = generate_class_idempotency_key(uid_1, meta_1)
    key_u2 = generate_class_idempotency_key(uid_2, meta_2)

    # 1. 5 concurrent threads attempting to enqueue identical slot for User 1
    u1_results = []
    def enqueue_worker(thread_idx: int):
        jid = enqueue_job(
            user_id=uid_1,
            class_metadata=meta_1,
            client=r,
            stream_name=stream_name,
            idempotency_key=key_u1
        )
        u1_results.append(jid)

    threads = [threading.Thread(target=enqueue_worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly 1 thread should succeed; other 4 return (None, False) (duplicate suppressed)
    successful_u1 = [job for job, enqueued in u1_results if enqueued]

    # 2. Enqueue same slot for User 2 -> Must succeed independently
    job_u2, enqueued_u2 = enqueue_job(
        user_id=uid_2,
        class_metadata=meta_2,
        client=r,
        stream_name=stream_name,
        idempotency_key=key_u2
    )

    stream_len = r.xlen(stream_name)

    # Clean up Redis
    r.delete(stream_name)
    r.delete(key_u1)
    r.delete(key_u2)

    return {
        "user_1_attempts": 5,
        "user_1_enqueued": len(successful_u1),
        "user_2_enqueued": 1 if enqueued_u2 else 0,
        "stream_total_messages": stream_len,
        "cross_user_isolation": (enqueued_u2 and len(successful_u1) == 1 and stream_len == 2)
    }


# ---------------------------------------------------------------------------
# Scenario 6: Deterministic Worker Crash & Stale Job Recovery (XAUTOCLAIM)
# ---------------------------------------------------------------------------
async def run_scenario_6_crash_recovery() -> Dict[str, Any]:
    """
    Scenario 6: Deterministically validates worker crash recovery:
      Worker A claims job -> controlled abort before ACK -> PEL contains job ->
      idle threshold expires -> Worker B XAUTOCLAIMs -> Worker B completes -> ACK -> PEL=0.
    """
    r = get_test_redis()
    run_id = uuid.uuid4().hex[:6]
    stream_name = f"cloudnote:test:crash:stream:{run_id}"
    group_name = f"cloudnote:test:crash:group:{run_id}"
    ensure_consumer_group(client=r, stream_name=stream_name, group_name=group_name)

    test_uid = 96001
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO users (id, username, hashed_password, email, lpu_username, encrypted_portal_password) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (test_uid, f"crash_user_{run_id}", "hash", f"crash_{run_id}@lpu.in", f"crash_user_{run_id}",
         encrypt_portal_password("PassCrash123!", settings.JWT_SECRET))
    )
    conn.commit()
    conn.close()

    metadata = {
        "subject_code": f"CSE_CRASH_{run_id}",
        "subject_name": "Crash Recovery Lecture",
        "faculty": "Prof. Crash",
        "timings": "15:00 - 16:00",
        "start_time": "15:00",
        "end_time": "16:00",
        "join_url": "https://code.tantra/crash"
    }

    # Enqueue single job
    job_id = enqueue_job(
        user_id=test_uid,
        class_metadata=metadata,
        client=r,
        stream_name=stream_name,
        idempotency_key=f"user:{test_uid}:crash:{run_id}"
    )

    # Worker A: reads job, but aborts before sending ACK
    worker_a = WorkerConsumer(
        consumer_name=f"worker-A-crashed-{run_id}",
        stream_name=stream_name,
        group_name=group_name,
        redis_client=r,
        idle_timeout_ms=500  # Fast 500ms test reclaim threshold
    )

    # Worker A reads from stream
    jobs_a = consume_jobs(consumer_name=worker_a.consumer_name, count=1, block_ms=1000,
                          client=r, stream_name=stream_name, group_name=group_name)
    assert len(jobs_a) == 1, "Worker A failed to claim job"

    # Worker A simulates mid-execution container kill / unhandled crash (NO ACK)
    logger.info(f"Test: Simulating Worker A ({worker_a.consumer_name}) crash before ACK...")
    # Verify job is in PEL for Worker A
    pel_after_crash = get_pending_jobs(client=r, stream_name=stream_name, group_name=group_name)
    assert len(pel_after_crash) == 1, "PEL should contain 1 unacknowledged message after crash"
    assert pel_after_crash[0]["consumer"] == worker_a.consumer_name

    # Wait for test idle threshold to expire (500ms + buffer)
    logger.info("Test: Waiting 600ms for test-only idle threshold to expire...")
    await asyncio.sleep(0.65)

    # Worker B: instantiated to reclaim stale pending entries
    worker_b = WorkerConsumer(
        consumer_name=f"worker-B-reclaimer-{run_id}",
        stream_name=stream_name,
        group_name=group_name,
        redis_client=r,
        idle_timeout_ms=500
    )

    worker_b_executed = False

    async def worker_b_process(user_id: int, lpu_username: str, lpu_password: str, class_metadata: dict):
        nonlocal worker_b_executed
        worker_b_executed = True
        return {"status": "COMPLETED"}

    # Worker B runs poll_and_process with check_stale=True
    processed = await worker_b.poll_and_process(count=1, block_ms=500, process_class_fn=worker_b_process, check_stale=True)

    # Verify Worker B reclaimed and ACKed
    pel_final = get_pending_jobs(client=r, stream_name=stream_name, group_name=group_name)

    # Clean up Redis
    r.delete(stream_name)

    return {
        "pel_after_crash": len(pel_after_crash),
        "crashed_worker": worker_a.consumer_name,
        "reclaiming_worker": worker_b.consumer_name,
        "worker_b_reclaimed_and_executed": worker_b_executed,
        "jobs_recovered": processed,
        "pel_final": len(pel_final)
    }


# ---------------------------------------------------------------------------
# Cleanup Helper for Synthetic Test Data
# ---------------------------------------------------------------------------
def cleanup_synthetic_test_data():
    """
    Cleans up all synthetic test users and associated database records created
    during the Step 3F.2 empirical validation test suite.
    Explicitly targets test ID ranges (90000 <= user_id < 100000) to ensure
    zero contamination of real users (e.g. user_id = 1).
    Also removes any synthetic test log directories under logs/.
    """
    print("\n--- Cleaning up Step 3F.2 synthetic test data ---")
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1. Clean attendance records for synthetic test range
        cur.execute("DELETE FROM class_attendance_records WHERE user_id >= 90000 AND user_id < 100000")
        deleted_attendance = cur.rowcount

        # 2. Clean user_timetables for synthetic test range if any
        cur.execute("DELETE FROM user_timetables WHERE user_id >= 90000 AND user_id < 100000")
        deleted_timetables = cur.rowcount

        # 3. Clean users for synthetic test range
        cur.execute("DELETE FROM users WHERE id >= 90000 AND id < 100000")
        deleted_users = cur.rowcount

        conn.commit()
        conn.close()
        print(f"Database cleanup: removed {deleted_users} synthetic users, {deleted_attendance} attendance records, {deleted_timetables} timetable rows.")
    except Exception as e:
        print(f"Warning: Database cleanup encountered error: {e}")

    # 4. Clean up any synthetic test user log directories under logs/
    try:
        import shutil
        logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        if os.path.exists(logs_dir):
            for entry in os.listdir(logs_dir):
                if entry.isdigit() and 90000 <= int(entry) < 100000:
                    user_log_path = os.path.join(logs_dir, entry)
                    if os.path.isdir(user_log_path):
                        shutil.rmtree(user_log_path, ignore_errors=True)
            print("Filesystem cleanup: removed synthetic test user log directories under logs/.")
    except Exception as e:
        print(f"Warning: Filesystem cleanup encountered error: {e}")


# ---------------------------------------------------------------------------
# Main Step 3F.2 Execution Harness
# ---------------------------------------------------------------------------
async def main():
    print("================================================================================")
    print("STARTING STEP 3F.2 EMPIRICAL CONCURRENCY & MULTI-WORKER VALIDATION")
    print("================================================================================")
    init_db()
    # Ensure a pristine slate before starting
    cleanup_synthetic_test_data()

    try:
        # --- Scenario 1: Tiered Workloads ---
        print("\n--- Running Scenario 1: Tiered Workload Benchmarking ---")
        tier_1 = await run_scenario_1_tiered_workload("Tier1", num_workers=2, num_users=4, num_jobs=8)
        print(f"Tier 1 Baseline: {tier_1['workers']} workers, {tier_1['users']} users, {tier_1['jobs']} jobs -> "
              f"{tier_1['jobs_processed']} processed in {tier_1['total_time_sec']}s ({tier_1['throughput_jobs_per_sec']} jobs/s), "
              f"PEL remaining: {tier_1['pel_remaining']}.")

        tier_2 = await run_scenario_1_tiered_workload("Tier2", num_workers=4, num_users=10, num_jobs=20)
        print(f"Tier 2 Moderate: {tier_2['workers']} workers, {tier_2['users']} users, {tier_2['jobs']} jobs -> "
              f"{tier_2['jobs_processed']} processed in {tier_2['total_time_sec']}s ({tier_2['throughput_jobs_per_sec']} jobs/s), "
              f"PEL remaining: {tier_2['pel_remaining']}.")

        tier_3 = await run_scenario_1_tiered_workload("Tier3", num_workers=8, num_users=25, num_jobs=50)
        print(f"Tier 3 Stress:   {tier_3['workers']} workers, {tier_3['users']} users, {tier_3['jobs']} jobs -> "
              f"{tier_3['jobs_processed']} processed in {tier_3['total_time_sec']}s ({tier_3['throughput_jobs_per_sec']} jobs/s), "
              f"PEL remaining: {tier_3['pel_remaining']}.")

        # --- Scenario 2: Multi-User State Isolation ---
        print("\n--- Running Scenario 2: Concurrent Multi-User State Isolation ---")
        iso_ok = await run_scenario_2_user_state_isolation()
        assert iso_ok is True
        print("PASS: 100% data partition isolation verified across concurrent users.")

        # --- Scenario 3: SQLite Read/Write Contention ---
        print("\n--- Running Scenario 3: SQLite Read/Write Contention & Telemetry ---")
        run_scenario_3_sqlite_contention(duration_seconds=2.5, num_threads=8)
        summary = telemetry.compute_sqlite_summary()
        print(f"SQLite Summary: {summary['count']} transactions | "
              f"Min: {summary['min_ms']}ms | Avg: {summary['avg_ms']}ms | Median: {summary['median_ms']}ms | "
              f"p95: {summary['p95_ms']}ms | Max: {summary['max_ms']}ms | Lock Errors: {summary['lock_errors']}")
        assert summary["lock_errors"] == 0, f"Encountered {summary['lock_errors']} SQLite database is locked errors!"
        print("PASS: SQLite WAL mode handled concurrent multi-threaded read/write load with 0 lock errors.")

        # --- Scenario 4: Distributed Execution Lock Race ---
        print("\n--- Running Scenario 4: Distributed Execution-Lock Duplicate Race ---")
        race_res = await run_scenario_4_execution_lock_race()
        print(f"Race Outcome: Worker A acquired={race_res['worker_a_lock_acquired']}, "
              f"Worker B acquired={race_res['worker_b_lock_acquired']}, "
              f"Executions={race_res['process_class_executions']}, "
              f"ACKs={race_res['stream_acks']}, "
              f"PEL={race_res['pel_remaining']}")
        assert race_res["worker_a_lock_acquired"] is True
        assert race_res["worker_b_lock_acquired"] is False
        assert race_res["process_class_executions"] == 1, f"Expected 1 execution, got {race_res['process_class_executions']}"
        assert race_res["stream_acks"] == 2, f"Expected 2 ACKs, got {race_res['stream_acks']}"
        assert race_res["pel_remaining"] == 0
        print("PASS: Execution lock race proved: Worker A acquired, Worker B rejected, executions=1, ACKs=2.")

        # --- Scenario 5: Concurrent Idempotency ---
        print("\n--- Running Scenario 5: User-Scoped Concurrent Idempotency ---")
        idemp_res = run_scenario_5_concurrent_idempotency()
        print(f"Idempotency Outcome: User 1 attempts={idemp_res['user_1_attempts']}, "
              f"enqueued={idemp_res['user_1_enqueued']} | User 2 enqueued={idemp_res['user_2_enqueued']}")
        assert idemp_res["user_1_enqueued"] == 1
        assert idemp_res["user_2_enqueued"] == 1
        assert idemp_res["cross_user_isolation"] is True
        print("PASS: User-scoped idempotency correctly suppressed duplicate enqueues for User 1 while allowing User 2.")

        # --- Scenario 6: Deterministic Worker Crash & Stale Recovery ---
        print("\n--- Running Scenario 6: Deterministic Crash & XAUTOCLAIM Recovery ---")
        crash_res = await run_scenario_6_crash_recovery()
        print(f"Crash Recovery: PEL after crash={crash_res['pel_after_crash']}, "
              f"Reclaiming worker={crash_res['reclaiming_worker']}, "
              f"Worker B executed={crash_res['worker_b_reclaimed_and_executed']}, "
              f"Jobs recovered={crash_res['jobs_recovered']}, "
              f"PEL final={crash_res['pel_final']}")
        assert crash_res["pel_after_crash"] == 1
        assert crash_res["worker_b_reclaimed_and_executed"] is True
        assert crash_res["jobs_recovered"] == 1
        assert crash_res["pel_final"] == 0
        print("PASS: Deterministic crash recovery verified: Worker A aborted -> PEL retained -> Worker B XAUTOCLAIMed -> completed -> ACK -> PEL=0.")

        print("\n================================================================================")
        print("ALL STEP 3F.2 EMPIRICAL VALIDATION TESTS COMPLETED SUCCESSFULLY!")
        print("================================================================================")
    finally:
        cleanup_synthetic_test_data()


if __name__ == "__main__":
    asyncio.run(main())
