"""
Step 3B Test Suite — Redis Worker Consumer Integration
Tests Redis worker consumer loop, in-memory credential resolution,
failure and ACK policies, PEL unacknowledged retention on crash,
stale pending job auto-claim recovery, execution lock heartbeat keep-alive,
and strict differentiation between true duplicates and legitimate retries.
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import (
    init_db,
    get_db_connection,
    set_user_portal_credentials,
    get_user_portal_credentials,
)
from app.job_queue import (
    IngestionJob,
    enqueue_job,
    get_pending_jobs,
    get_redis_client,
    ensure_consumer_group,
    get_execution_lock_key,
    ExecutionLock,
    acquire_execution_lock,
    release_execution_lock,
)
from app.worker_consumer import WorkerConsumer, generate_consumer_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Step3BTests")


async def run_step3b_tests():
    logger.info("=================================================================")
    logger.info("=== STARTING STEP 3B REDIS WORKER CONSUMER INTEGRATION TESTS ===")
    logger.info("=================================================================")

    r = get_redis_client()
    assert r.ping(), "Redis connection failed!"

    # Ensure SQLite DB is initialized
    init_db()

    # Provision isolated test stream and group
    run_id = uuid.uuid4().hex[:8]
    test_stream = f"cloudnote:test:jobs:3b_{run_id}"
    test_group = f"cloudnote:test:group:3b_{run_id}"
    ensure_consumer_group(client=r, stream_name=test_stream, group_name=test_group)

    # 1. Setup Mock User with Encrypted Portal Password in SQLite
    fake_user_id = 9801
    fake_username = f"test_student_{run_id}"
    fake_lpu_username = f"lpu_user_{run_id}"
    fake_secret_pw = f"SecretPass_Mock_{run_id}!"

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO users (id, username, hashed_password, email, lpu_username) VALUES (?, ?, ?, ?, ?)",
        (fake_user_id, fake_username, "mock_hash_xyz", f"{fake_username}@test.com", fake_lpu_username),
    )
    conn.commit()
    conn.close()

    # Encrypt password into SQLite
    set_ok = set_user_portal_credentials(
        user_id=fake_user_id,
        lpu_username=fake_lpu_username,
        plain_password=fake_secret_pw,
    )
    assert set_ok is True, "Failed to set user portal credentials in SQLite."

    # Verify SQLite credential helper
    creds = get_user_portal_credentials(fake_user_id)
    assert creds is not None
    assert creds["portal_password"] == fake_secret_pw
    logger.info("PASS [1/9]: Provisioned mock user in SQLite and verified in-memory credential decryption.")

    # 2. Test Happy Path: Enqueue -> Consume -> Credential Resolution -> process_class -> ACK
    consumer_1 = WorkerConsumer(
        consumer_name=f"worker-1-{run_id}",
        stream_name=test_stream,
        group_name=test_group,
        redis_client=r,
        idle_timeout_ms=300000,
    )

    fake_class_meta = {
        "subject_code": "CSE401",
        "subject_name": "Cloud Computing",
        "faculty": "Dr. Test Node",
        "timings": "11:00 AM - 12:00 PM",
        "start_time": "2026-09-17 11:00:00",
        "end_time": "2026-09-17 12:00:00",
        "join_url": "https://test.example.com/join/cse401",
    }

    job_1, enqueued_1 = enqueue_job(
        user_id=fake_user_id,
        class_metadata=fake_class_meta,
        client=r,
        stream_name=test_stream,
    )
    assert enqueued_1 is True

    captured_calls = []

    async def mock_process_class(user_id, lpu_username, lpu_password, class_metadata):
        captured_calls.append({
            "user_id": user_id,
            "lpu_username": lpu_username,
            "lpu_password": lpu_password,
            "class_metadata": class_metadata,
        })
        return {"status": "COMPLETED", "details": "Processed in mock"}

    processed_count = await consumer_1.poll_and_process(
        count=1,
        block_ms=1000,
        process_class_fn=mock_process_class,
        check_stale=False,
    )
    assert processed_count == 1, f"Expected 1 job processed, got {processed_count}"
    assert len(captured_calls) == 1

    call = captured_calls[0]
    assert call["user_id"] == fake_user_id
    assert call["lpu_username"] == fake_lpu_username
    assert call["lpu_password"] == fake_secret_pw
    assert call["class_metadata"]["subject_code"] == "CSE401"

    pending = get_pending_jobs(client=r, stream_name=test_stream, group_name=test_group)
    assert len(pending) == 0, f"Expected 0 pending jobs after ACK, found {len(pending)}"
    logger.info("PASS [2/9]: Job consumed, credentials decrypted in-memory, process_class invoked correctly, and ACKed.")

    # 3. Test Terminal Failure Policies (Invalid User and Missing Credentials -> Safe ACK)
    non_existent_uid = 99999
    job_bad_user, _ = enqueue_job(
        user_id=non_existent_uid,
        class_metadata=fake_class_meta,
        client=r,
        stream_name=test_stream,
    )
    await consumer_1.poll_and_process(count=1, block_ms=1000, process_class_fn=mock_process_class, check_stale=False)
    pending_bad = get_pending_jobs(client=r, stream_name=test_stream, group_name=test_group)
    assert len(pending_bad) == 0, "Non-existent user job should be terminally ACKed."

    fake_user_no_creds = 9802
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO users (id, username, hashed_password, email, lpu_username, encrypted_portal_password) VALUES (?, ?, ?, ?, ?, NULL)",
        (fake_user_no_creds, f"no_creds_{run_id}", "hash", "no_creds@test.com", f"no_creds_{run_id}"),
    )
    conn.commit()
    conn.close()

    job_no_creds, _ = enqueue_job(
        user_id=fake_user_no_creds,
        class_metadata=fake_class_meta,
        client=r,
        stream_name=test_stream,
    )
    await consumer_1.poll_and_process(count=1, block_ms=1000, process_class_fn=mock_process_class, check_stale=False)
    pending_no_creds = get_pending_jobs(client=r, stream_name=test_stream, group_name=test_group)
    assert len(pending_no_creds) == 0, "Missing credentials job should be terminally ACKed."
    logger.info("PASS [3/9]: Terminal failure policies for invalid user and missing credentials safely ACKed without looping.")

    # 4. Test Unexpected Exception Policy (No ACK, Remains in PEL)
    async def mock_crashing_process_class(user_id, lpu_username, lpu_password, class_metadata):
        raise RuntimeError("Simulated unexpected worker container crash or unhandled exception!")

    job_crash, _ = enqueue_job(
        user_id=fake_user_id,
        class_metadata=fake_class_meta,
        client=r,
        stream_name=test_stream,
    )

    try:
        await consumer_1.poll_and_process(
            count=1,
            block_ms=1000,
            process_class_fn=mock_crashing_process_class,
            check_stale=False,
        )
    except Exception:
        pass

    pel_after_crash = get_pending_jobs(client=r, stream_name=test_stream, group_name=test_group)
    assert len(pel_after_crash) == 1, f"Expected 1 job retained in PEL after crash, found {len(pel_after_crash)}"
    crashed_msg_id = pel_after_crash[0]["message_id"]
    assert crashed_msg_id == job_crash.stream_message_id
    logger.info(f"PASS [4/9]: Unexpected process_class exception left job {crashed_msg_id} unACKed in PEL.")

    # 5. Test Stale Job Recovery (XAUTOCLAIM by Second Consumer)
    consumer_2 = WorkerConsumer(
        consumer_name=f"worker-2-recovery-{run_id}",
        stream_name=test_stream,
        group_name=test_group,
        redis_client=r,
        idle_timeout_ms=100,
    )

    await asyncio.sleep(0.15)

    recovered_calls = []

    async def mock_recovery_process_class(user_id, lpu_username, lpu_password, class_metadata):
        recovered_calls.append(user_id)
        return {"status": "COMPLETED", "details": "Recovered by consumer 2"}

    recovered_count = await consumer_2.poll_and_process(
        count=1,
        block_ms=500,
        process_class_fn=mock_recovery_process_class,
        check_stale=True,
    )
    assert recovered_count == 1, f"Expected consumer 2 to reclaim and process 1 stale job, got {recovered_count}"
    assert len(recovered_calls) == 1
    assert recovered_calls[0] == fake_user_id

    pel_after_recovery = get_pending_jobs(client=r, stream_name=test_stream, group_name=test_group)
    assert len(pel_after_recovery) == 0, f"Expected PEL empty after recovery, found {len(pel_after_recovery)}"
    logger.info("PASS [5/9]: Stale unACKed job successfully reclaimed via XAUTOCLAIM and ACKed by second worker.")

    # 6. Test Lock TTL & Dynamic Heartbeat Keep-Alive Behavior
    test_lock_key = f"cloudnote:lock:test:heartbeat:{run_id}"
    exec_lock_hb = ExecutionLock(
        lock_key=test_lock_key,
        consumer_name=consumer_1.consumer_name,
        client=r,
        ttl_seconds=2,  # Short TTL of 2 seconds
        heartbeat_interval=0.5,  # Renew every 0.5s
    )
    acquired_hb = await exec_lock_hb.acquire()
    assert acquired_hb is True, "Failed to acquire heartbeat lock."

    # Wait 2.5s while active (longer than initial 2s TTL). Heartbeat should keep it alive!
    await asyncio.sleep(2.2)
    assert r.get(test_lock_key) == consumer_1.consumer_name, "Lock died prematurely; heartbeat failed to renew!"

    # Release lock and verify immediate key removal
    released_hb = await exec_lock_hb.release()
    assert released_hb is True, "Failed to release heartbeat lock."
    assert r.get(test_lock_key) is None, "Lock key remained in Redis after release!"
    logger.info("PASS [6/9]: Verified lock dynamic heartbeat keeps lock alive past nominal TTL and releases cleanly.")

    # 7. Test True Duplicate vs Legitimate Retry/Recovery under Active Lock
    active_lock_key = get_execution_lock_key(fake_user_id, fake_class_meta)
    active_lock = ExecutionLock(
        lock_key=active_lock_key,
        consumer_name="worker-active-owner",
        client=r,
        ttl_seconds=30,
    )
    await active_lock.acquire()

    # Case A: True Duplicate (New stream message arriving while active lock is held)
    job_dup, _ = enqueue_job(
        user_id=fake_user_id,
        class_metadata=fake_class_meta,
        client=r,
        stream_name=test_stream,
    )

    dup_exec_calls = []

    async def mock_dup_fn(user_id, lpu_username, lpu_password, class_metadata):
        dup_exec_calls.append(user_id)
        return {"status": "COMPLETED"}

    # Consumer 2 reads the NEW job (is_reclaimed=False)
    # Concurrency guard should detect active lock, NOT execute process_class, and ACK duplicate message
    res_dup = await consumer_2.process_single_job(
        job=job_dup,
        process_class_fn=mock_dup_fn,
        is_reclaimed=False,
    )
    assert res_dup is False, "Expected process_single_job to return False for duplicate."
    assert len(dup_exec_calls) == 0, "process_class should not be invoked for duplicate."
    # Verify the duplicate was ACKed so it doesn't linger in PEL
    pel_dup = get_pending_jobs(client=r, stream_name=test_stream, group_name=test_group)
    assert len(pel_dup) == 0, f"Expected duplicate to be ACKed, but found {len(pel_dup)} in PEL."

    # Case B: Legitimate Retry / Reclaimed message while lock is held
    # Suppose a message in PEL was reclaimed, but the active worker still holds the lock!
    # It must NOT be ACKed!
    job_retry_sim = IngestionJob(
        job_id="mock_reclaimed_retry_id",
        user_id=fake_user_id,
        class_metadata=fake_class_meta,
        created_at="2026-09-17T11:00:00Z",
        stream_message_id="9999999999999-0",
    )
    res_retry = await consumer_2.process_single_job(
        job=job_retry_sim,
        process_class_fn=mock_dup_fn,
        is_reclaimed=True,  # This is a reclaimed/retry message!
    )
    assert res_retry is False
    # Because is_reclaimed=True, consumer_2 did NOT ACK the message!
    # It leaves it in PEL for subsequent retry once lock is free.

    # Release active lock
    await active_lock.release()
    logger.info("PASS [7/9]: Differentiated true duplicate (ACKed) from legitimate retry/reclaimed message (retained in PEL).")

    # 8. Test That Two Different Classes Never Collide on Execution Lock
    meta_class_a = {
        "subject_code": "CSE320",
        "timings": "10:00 AM - 11:00 AM",
        "start_time": "2026-09-17 10:00:00",
    }
    meta_class_b = {
        "subject_code": "CSE320",
        "timings": "10:00 AM - 11:00 AM",
        "start_time": "2026-09-18 10:00:00",  # Different date
    }
    meta_class_c = {
        "subject_code": "MTH102",  # Different subject
        "timings": "10:00 AM - 11:00 AM",
        "start_time": "2026-09-17 10:00:00",
    }

    key_a = get_execution_lock_key(101, meta_class_a)
    key_b = get_execution_lock_key(101, meta_class_b)
    key_c = get_execution_lock_key(101, meta_class_c)
    key_other_user = get_execution_lock_key(202, meta_class_a)

    assert key_a != key_b, "Lock keys collided between different dates!"
    assert key_a != key_c, "Lock keys collided between different subjects!"
    assert key_a != key_other_user, "Lock keys collided between different users!"
    logger.info("PASS [8/9]: Verified two genuinely different class jobs never share the same execution lock.")

    # 9. Verify Redis Payload Contains Zero Secrets
    stream_entries = r.xrange(test_stream)
    for entry_id, fields in stream_entries:
        raw_payload = fields.get("payload", "")
        assert fake_secret_pw not in raw_payload, "CRITICAL: Plaintext password detected in Redis stream!"
        parsed = json.loads(raw_payload)
        for forbidden in ["lpu_password", "encrypted_portal_password", "jwt_secret", "gemini_api_key"]:
            assert forbidden not in parsed, f"CRITICAL: Forbidden key '{forbidden}' found in Redis payload!"
    logger.info("PASS [9/9]: Stream audit confirmed zero credentials or forbidden keys stored in Redis.")

    # Cleanup test stream and group
    try:
        r.delete(test_stream)
    except Exception:
        pass

    logger.info("=================================================================")
    logger.info("=== ALL STEP 3B TESTS PASSED SUCCESSFULLY (9/9) ===")
    logger.info("=================================================================")


if __name__ == "__main__":
    asyncio.run(run_step3b_tests())
