"""
Step 3A Test Suite — Redis Job Queue Infrastructure
Tests Redis Streams, Consumer Groups, Non-Secret Serialization,
Idempotency, Credential Rejection, and Error Handling.
"""

import os
import sys
import uuid
import logging

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.job_queue import (
    IngestionJob,
    enqueue_job,
    consume_jobs,
    acknowledge_job,
    get_pending_jobs,
    ensure_consumer_group,
    get_redis_client,
    JobQueueSecurityError,
    JobQueueMalformedError,
    DEFAULT_STREAM_NAME,
    DEFAULT_CONSUMER_GROUP,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Step3ATests")


def run_tests():
    logger.info("=================================================================")
    logger.info("=== STARTING STEP 3A REDIS JOB QUEUE TESTS ===")
    logger.info("=================================================================")

    # Use isolated test stream & group to avoid collision
    test_run_id = uuid.uuid4().hex[:8]
    test_stream = f"cloudnote:test:jobs:{test_run_id}"
    test_group = f"cloudnote:test:group:{test_run_id}"
    test_consumer = f"worker-{test_run_id}"

    r = get_redis_client()
    # 1. Connectivity Check
    assert r.ping(), "Failed to ping Redis instance!"
    logger.info("PASS [1/7]: Redis connectivity verified via REDIS_URL.")

    # 2. Enqueue Fake Job
    fake_user_id = 9901
    fake_metadata = {
        "subject_code": "TEST101",
        "subject_name": "Distributed Systems Testing",
        "faculty": "Prof. Test",
        "timings": "09:00 AM - 10:00 AM",
        "start_time": "09:00",
        "end_time": "10:00",
        "join_url": "https://test.example.com/join/mock",
    }
    fake_idempotency_key = f"user:{fake_user_id}:TEST101:2026-09-17:09:00"

    job, enqueued = enqueue_job(
        user_id=fake_user_id,
        class_metadata=fake_metadata,
        idempotency_key=fake_idempotency_key,
        client=r,
        stream_name=test_stream,
    )

    assert enqueued is True, "Expected job to be freshly enqueued."
    assert job is not None
    assert job.user_id == fake_user_id
    assert job.class_metadata["subject_code"] == "TEST101"
    assert job.idempotency_key == fake_idempotency_key
    assert job.stream_message_id is not None
    logger.info(f"PASS [2/7]: Successfully enqueued fake job (job_id={job.job_id}, stream_msg_id={job.stream_message_id}).")

    # 3. Read Job Through Consumer Group
    consumed_jobs = consume_jobs(
        consumer_name=test_consumer,
        count=5,
        block_ms=1000,
        client=r,
        stream_name=test_stream,
        group_name=test_group,
    )

    assert len(consumed_jobs) == 1, f"Expected 1 job consumed, got {len(consumed_jobs)}"
    read_job = consumed_jobs[0]
    assert read_job.job_id == job.job_id
    assert read_job.user_id == fake_user_id
    assert read_job.class_metadata == fake_metadata
    assert read_job.stream_message_id == job.stream_message_id
    logger.info(f"PASS [3/7]: Consumed job from group '{test_group}' and verified payload fidelity.")

    # 4. Pending Inspection (PEL) & Acknowledgment
    pending_before_ack = get_pending_jobs(
        client=r,
        stream_name=test_stream,
        group_name=test_group,
        count=10,
    )
    assert len(pending_before_ack) == 1, f"Expected 1 pending unacknowledged message, got {len(pending_before_ack)}"
    assert pending_before_ack[0]["message_id"] == read_job.stream_message_id
    logger.info(f"PASS [4a/7]: Verified unacknowledged job {read_job.stream_message_id} is in PEL.")

    ack_ok = acknowledge_job(
        message_id=read_job.stream_message_id,
        client=r,
        stream_name=test_stream,
        group_name=test_group,
    )
    assert ack_ok is True, "Expected acknowledge_job to return True."

    pending_after_ack = get_pending_jobs(
        client=r,
        stream_name=test_stream,
        group_name=test_group,
        count=10,
    )
    assert len(pending_after_ack) == 0, f"Expected 0 pending messages after XACK, got {len(pending_after_ack)}"
    logger.info("PASS [4b/7]: Successfully acknowledged job; verified PEL is cleared.")

    # 5. Idempotency Verification (Duplicate Protection)
    duplicate_job, dup_enqueued = enqueue_job(
        user_id=fake_user_id,
        class_metadata=fake_metadata,
        idempotency_key=fake_idempotency_key,  # Same idempotency key!
        client=r,
        stream_name=test_stream,
    )
    assert dup_enqueued is False, "Duplicate job with same idempotency key was NOT rejected!"
    assert duplicate_job is None
    logger.info(f"PASS [5/7]: Idempotency key duplicate protection successfully suppressed duplicate enqueue.")

    # 6. Security Audit: Rejection of Secret / Credential Fields
    forbidden_samples = [
        {"lpu_password": "RealSecretPassword123"},
        {"encrypted_portal_password": "enc_data_blob"},
        {"jwt_secret": "my_jwt_key"},
        {"nested": {"auth_token": "bearer xyz"}},
        {"gemini_api_key": "AIzaSy..."},
    ]

    for forbidden_dict in forbidden_samples:
        bad_meta = dict(fake_metadata)
        bad_meta.update(forbidden_dict)
        try:
            enqueue_job(
                user_id=fake_user_id,
                class_metadata=bad_meta,
                client=r,
                stream_name=test_stream,
            )
            assert False, f"FAILED: Enqueue permitted forbidden payload containing: {forbidden_dict}"
        except JobQueueSecurityError as sec_err:
            logger.debug(f"Expected security block: {sec_err}")

    logger.info("PASS [6/7]: Strict security validator blocked all payloads containing sensitive/credential keys.")

    # 7. Malformed / Invalid Job Handling
    invalid_cases = [
        (-5, fake_metadata),          # Negative user_id
        ("not-an-int", fake_metadata), # String user_id
        (fake_user_id, "string_not_dict"), # Bad metadata type
    ]

    for bad_uid, bad_meta in invalid_cases:
        try:
            enqueue_job(
                user_id=bad_uid,
                class_metadata=bad_meta,
                client=r,
                stream_name=test_stream,
            )
            assert False, f"FAILED: Permitted invalid arguments: uid={bad_uid}, meta={bad_meta}"
        except JobQueueMalformedError:
            pass

    # Malformed JSON handling during consumption
    r.xadd(test_stream, {"payload": "NOT_VALID_JSON{}"})
    bad_consumed = consume_jobs(
        consumer_name=test_consumer,
        count=5,
        block_ms=500,
        client=r,
        stream_name=test_stream,
        group_name=test_group,
    )
    # The malformed message should be logged and skipped, not crash the consumer
    assert len(bad_consumed) == 0, "Expected malformed JSON to be skipped."
    logger.info("PASS [7/7]: Malformed payloads and invalid arguments handled gracefully without crashes.")

    # Cleanup test stream
    try:
        r.delete(test_stream)
        r.delete(f"cloudnote:job:idempotency:{fake_idempotency_key}")
    except Exception:
        pass

    logger.info("=================================================================")
    logger.info("=== ALL STEP 3A REDIS QUEUE TESTS PASSED SUCCESSFULLY (7/7) ===")
    logger.info("=================================================================")


if __name__ == "__main__":
    run_tests()
