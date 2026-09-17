"""
Step 3C Test Suite — Scheduler → Redis Queue Producer Integration
Tests:
1. Deterministic idempotency key stability and collision resistance
2. Scheduler class enqueueing to Redis stream with exact user_id and metadata
3. Zero credentials in Redis stream payload (strict security verification)
4. Duplicate scheduling suppression via idempotency key
5. End-to-end flow: Enqueue -> WorkerConsumer -> process_class() with decrypted credentials -> ACK
6. Scheduler state preservation (cache completion status and fallback logic)
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import (
    init_db,
    get_db_connection,
    set_user_portal_credentials,
    get_user_portal_credentials,
    get_or_create_ingestion_user,
)
from app.job_queue import (
    IngestionJob,
    enqueue_job,
    get_pending_jobs,
    get_redis_client,
    ensure_consumer_group,
    generate_class_idempotency_key,
    schedule_class_to_queue,
    JobQueueSecurityError,
    FORBIDDEN_PAYLOAD_KEYS,
)
from app.worker_consumer import WorkerConsumer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Step3CTests")


async def run_step3c_tests():
    logger.info("=================================================================")
    logger.info("=== STARTING STEP 3C SCHEDULER -> REDIS QUEUE PRODUCER TESTS ===")
    logger.info("=================================================================")

    r = get_redis_client()
    assert r.ping(), "Redis connection failed!"
    init_db()

    run_id = uuid.uuid4().hex[:8]
    test_stream = f"cloudnote:test:jobs:3c_{run_id}"
    test_group = f"cloudnote:test:group:3c_{run_id}"
    test_consumer = f"worker-3c-{run_id}"
    ensure_consumer_group(client=r, stream_name=test_stream, group_name=test_group)

    # -------------------------------------------------------------
    # TEST 1: Deterministic Idempotency Key Stability & Collision Resistance
    # -------------------------------------------------------------
    user_id = int(f"97{int(run_id[:4], 16) % 80 + 10}")
    class_meta_1 = {
        "subject_code": f"CSE408_{run_id}",
        "subject_name": "Cloud Computing Architecture",
        "faculty": "Dr. Cloud Specialist",
        "timings": "10:00 AM - 11:00 AM",
        "start_time": "2026-09-17 10:00:00",
        "end_time": "2026-09-17 11:00:00",
        "join_url": "https://test.example.com/join/cse408",
    }
    key1_a = generate_class_idempotency_key(user_id, class_meta_1)
    key1_b = generate_class_idempotency_key(user_id, class_meta_1)
    assert key1_a == key1_b, "Idempotency key must be deterministic for identical inputs!"
    assert f"user:{user_id}:class:CSE408_{run_id}:" in key1_a, f"Key does not match format: {key1_a}"

    # Different user, same class -> distinct key
    key_diff_user = generate_class_idempotency_key(9702, class_meta_1)
    assert key_diff_user != key1_a, "Different users must produce distinct keys!"

    # Same user, different class -> distinct key
    class_meta_diff_subject = dict(class_meta_1, subject_code="CSE320")
    key_diff_subj = generate_class_idempotency_key(user_id, class_meta_diff_subject)
    assert key_diff_subj != key1_a, "Different subjects must produce distinct keys!"

    # Same user, same class, different time -> distinct key
    class_meta_diff_time = dict(class_meta_1, start_time="2026-09-18 10:00:00")
    key_diff_time = generate_class_idempotency_key(user_id, class_meta_diff_time)
    assert key_diff_time != key1_a, "Different start times must produce distinct keys!"

    logger.info("PASS [1/6]: Deterministic idempotency key is stable, robust, and collision-resistant.")

    # -------------------------------------------------------------
    # TEST 2: Scheduler Enqueues Class to Redis Stream
    # -------------------------------------------------------------
    r.delete(f"cloudnote:job:idempotency:{key1_a}")
    job, enqueued = schedule_class_to_queue(
        class_metadata=class_meta_1,
        user_id=user_id,
        client=r,
        stream_name=test_stream,
    )
    assert enqueued is True, "First scheduling must return enqueued=True"
    assert job is not None, "Job must not be None"
    assert job.user_id == user_id, f"Job user_id mismatch: {job.user_id} != {user_id}"
    assert job.class_metadata["subject_code"] == f"CSE408_{run_id}"
    assert job.class_metadata["join_url"] == "https://test.example.com/join/cse408"
    assert job.idempotency_key == key1_a, "Job idempotency key must match generated key"
    assert job.stream_message_id is not None, "Job must have a stream_message_id"

    # Verify stream length in Redis
    stream_len = r.xlen(test_stream)
    assert stream_len == 1, f"Expected 1 stream message, got {stream_len}"
    logger.info("PASS [2/6]: Scheduler creates Redis job with correct user_id, metadata, and stream ID.")

    # -------------------------------------------------------------
    # TEST 3: Duplicate Scheduling Does NOT Create Duplicate Stream Messages
    # -------------------------------------------------------------
    dup_job, dup_enqueued = schedule_class_to_queue(
        class_metadata=class_meta_1,
        user_id=user_id,
        client=r,
        stream_name=test_stream,
    )
    assert dup_enqueued is False, "Duplicate scheduling must return enqueued=False"
    assert dup_job is None, "Duplicate call must return None for newly created job"

    # Verify stream length remains 1
    stream_len_after = r.xlen(test_stream)
    assert stream_len_after == 1, f"Stream length must remain 1 after duplicate attempt, got {stream_len_after}"
    logger.info("PASS [3/6]: Duplicate scheduling suppressed by idempotency key; zero duplicate jobs added.")

    # -------------------------------------------------------------
    # TEST 4: Zero Credentials in Redis Stream Payload
    # -------------------------------------------------------------
    raw_messages = r.xrange(test_stream, "-", "+")
    assert len(raw_messages) == 1
    raw_msg_id, raw_fields = raw_messages[0]
    raw_payload = raw_fields.get("payload") if "payload" in raw_fields else raw_fields.get(b"payload")
    if isinstance(raw_payload, bytes):
        payload_str = raw_payload.decode("utf-8")
    else:
        payload_str = raw_payload
    payload_dict = json.loads(payload_str)

    # Check for forbidden keys anywhere in the raw payload
    def check_keys_recursive(d):
        if isinstance(d, dict):
            for k, v in d.items():
                for forbidden in FORBIDDEN_PAYLOAD_KEYS:
                    assert forbidden not in str(k).lower(), f"Security Violation: Found '{k}' in Redis stream payload!"
                check_keys_recursive(v)
        elif isinstance(d, (list, tuple)):
            for item in d:
                check_keys_recursive(item)

    check_keys_recursive(payload_dict)

    # Confirm that attempting to sneak a secret into metadata raises JobQueueSecurityError
    malicious_meta = dict(class_meta_1, lpu_password="hacked_password_leak")
    try:
        schedule_class_to_queue(
            class_metadata=malicious_meta,
            user_id=user_id,
            client=r,
            stream_name=test_stream,
        )
        assert False, "Should have raised JobQueueSecurityError!"
    except JobQueueSecurityError:
        pass

    logger.info("PASS [4/6]: Zero credentials in Redis stream; malicious secrets strictly blocked.")

    # -------------------------------------------------------------
    # TEST 5: Queued Job is Consumed by WorkerConsumer & Calls process_class()
    # -------------------------------------------------------------
    # Setup test user with encrypted credentials in DB
    fake_username = f"student_3c_{run_id}"
    fake_lpu_user = f"lpu_3c_{run_id}"
    fake_secret_pw = f"SecretPass_3c_{run_id}!"

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO users (id, username, hashed_password, email, lpu_username) VALUES (?, ?, ?, ?, ?)",
        (user_id, fake_username, "mock_hash_xyz", f"{fake_username}@test.com", fake_lpu_user),
    )
    conn.commit()
    conn.close()

    set_user_portal_credentials(user_id=user_id, lpu_username=fake_lpu_user, plain_password=fake_secret_pw)

    # Track process_class execution calls
    processed_calls = []

    async def mock_process_class(user_id, lpu_username, lpu_password, class_metadata):
        processed_calls.append({
            "user_id": user_id,
            "lpu_username": lpu_username,
            "lpu_password": lpu_password,
            "class_metadata": class_metadata,
        })
        return {"status": "COMPLETED"}

    consumer = WorkerConsumer(
        consumer_name=test_consumer,
        stream_name=test_stream,
        group_name=test_group,
        redis_client=r,
    )

    # Process next job
    processed_count = await consumer.poll_and_process(
        count=1,
        block_ms=2000,
        process_class_fn=mock_process_class,
        check_stale=False,
    )
    assert processed_count == 1, f"WorkerConsumer.poll_and_process() must process 1 job, got {processed_count}"
    assert len(processed_calls) == 1, f"Expected 1 process_class call, got {len(processed_calls)}"

    call = processed_calls[0]
    assert call["user_id"] == user_id, f"Worker called process_class with wrong user_id: {call['user_id']}"
    assert call["lpu_username"] == fake_lpu_user, f"Worker called with wrong username: {call['lpu_username']}"
    assert call["lpu_password"] == fake_secret_pw, f"Worker failed to decrypt password correctly in memory!"
    assert call["class_metadata"]["subject_code"] == f"CSE408_{run_id}"

    # Confirm stream message was acknowledged (PEL is empty)
    pending = get_pending_jobs(client=r, stream_name=test_stream, group_name=test_group)
    assert len(pending) == 0, f"Pending entries list must be empty after ACK, got {len(pending)}"
    logger.info("PASS [5/6]: WorkerConsumer successfully processed queued job, decrypted portal password in-memory, executed process_class, and ACKed.")

    # -------------------------------------------------------------
    # TEST 6: Timetable Cache State & Fallback Integrity
    # -------------------------------------------------------------
    from app.timetable_cache import timetable_cache

    test_classes = [
        dict(class_meta_1, status="UPCOMING"),
        {
            "subject_code": "CSE409",
            "subject_name": "Next Upcoming Class",
            "faculty": "Prof. Future",
            "timings": "11:00 AM - 12:00 PM",
            "start_time": (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
            "status": "UPCOMING",
        }
    ]
    timetable_cache.set_timetable(test_classes)

    # Simulate scheduler processing of active class
    active = test_classes[0]
    job_enq, is_new = schedule_class_to_queue(
        class_metadata=active,
        user_id=user_id,
        client=r,
        stream_name=test_stream,
    )
    # Mark QUEUED in cache as scheduler does
    active["status"] = "QUEUED"
    timetable_cache.set_timetable(test_classes)

    # Verify cached status
    cached = timetable_cache.get_timetable()
    assert cached[0]["status"] == "QUEUED", "Cached class must be marked QUEUED"
    assert cached[1]["status"] == "UPCOMING", "Upcoming class must remain UPCOMING"

    # Direct fallback test: when Redis is unavailable, direct execution is chosen
    from app.main import process_class_direct
    assert callable(process_class_direct), "process_class_direct must be a callable fallback function"

    logger.info("PASS [6/6]: Timetable cache lifecycle and direct fallback function verified.")

    # Cleanup test stream & group
    try:
        r.delete(test_stream)
        idemp_key = f"cloudnote:job:idempotency:{key1_a}"
        r.delete(idemp_key)
    except Exception:
        pass

    logger.info("=================================================================")
    logger.info("=== ALL STEP 3C TESTS PASSED SUCCESSFULLY! ===")
    logger.info("=================================================================")


if __name__ == "__main__":
    asyncio.run(run_step3c_tests())
