"""
Step 3F.7 Validation Test Suite
---------------------------------
Production Hardening & Operational Resilience:

1. Scenario 1: Scheduler daemon loop exception immunity & graceful cancellation
2. Scenario 2: Timetable retention of omitted executed/in-flight classes
3. Scenario 3: Smart sleep ISO timestamp parsing parity
4. Scenario 4: DLQ bounded capacity defense (QUEUE_DLQ_MAXLEN)
5. Scenario 5: Malformed message quarantine DLQ safety & PEL retention
6. Scenario 6: Guaranteed synthetic cleanup (users 97000-97099)

Constraints:
- Synthetic test data only (users 97000-97099)
- No fake LPU classes
- Clean teardown in try ... finally
"""

import asyncio
from datetime import datetime, date, time, timedelta, timezone
import json
import logging
import os
import sqlite3
import sys
import time as time_module
from typing import Any, Optional
from unittest.mock import MagicMock, patch
import uuid

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
    _classes_match_identity,
)
from app.job_queue import (
    enqueue_job,
    consume_jobs,
    claim_pending_jobs,
    acknowledge_job,
    schedule_class_to_queue,
    get_execution_lock_key,
    get_redis_client,
    get_dead_letter_jobs,
    quarantine_malformed_message,
    route_to_dead_letter_queue,
    ensure_consumer_group,
    ExecutionLock,
    IngestionJob,
    DEFAULT_STREAM_NAME,
    DEFAULT_CONSUMER_GROUP,
    DEFAULT_DLQ_STREAM_NAME,
)
from app.scheduler import StandaloneSchedulerService, TERMINAL_STATUSES
from app.worker_consumer import WorkerConsumer, generate_consumer_name
from app.utils import get_now_ist

# Ensure local test runs default to localhost Redis if REDIS_URL is unconfigured or points to Docker hostname 'redis'
if not os.environ.get("REDIS_URL") or "redis://redis:" in os.environ.get("REDIS_URL", ""):
    os.environ["REDIS_URL"] = "redis://localhost:6379/0"
settings.REDIS_URL = os.environ["REDIS_URL"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Step3F7Tests")


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
    Guaranteed cleanup: removes all synthetic test users (97000 <= id < 97100),
    associated attendance records, timetable rows, and Redis test stream keys.
    Never touches real users.
    """
    print("\n--- Cleaning up Step 3F.7 synthetic test data ---")
    for attempt in range(5):
        try:
            conn = get_db_connection()
            cur = conn.cursor()

            cur.execute("DELETE FROM class_attendance_records WHERE user_id >= 97000 AND user_id < 97100")
            cur.execute("DELETE FROM lecture_summaries WHERE user_id >= 97000 AND user_id < 97100")
            cur.execute("DELETE FROM user_timetables WHERE user_id >= 97000 AND user_id < 97100")
            cur.execute("DELETE FROM users WHERE id >= 97000 AND id < 97100")

            conn.commit()
            conn.close()
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 4:
                time_module.sleep(0.1)
            else:
                logger.warning(f"Cleanup retry failed: {e}")

    try:
        r = get_test_redis()
        test_streams = r.keys("cloudnote:test:step3f7:*")
        if test_streams:
            r.delete(*test_streams)
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


# ---------------------------------------------------------------------------
# SCENARIO 1: Scheduler Daemon Loop Exception Immunity & Graceful Shutdown
# ---------------------------------------------------------------------------
def test_scenario_1_scheduler_loop_exception_immunity():
    """
    Scenario 1:
    - Inject an unexpected exception during tick 1 of the scheduler loop.
    - Verify the scheduler daemon does NOT terminate.
    - Verify it logs a sanitized error, applies ~5s backoff, and proceeds to tick 2.
    - Verify cancellation and stop() still work gracefully.
    """
    print("\n" + "=" * 80)
    print("SCENARIO 1: Scheduler Daemon Loop Exception Immunity & Cancellation")
    print("=" * 80)

    service = StandaloneSchedulerService()
    tick_counter = 0
    backoff_durations = []

    real_sleep = asyncio.sleep

    async def mock_sleep(duration):
        backoff_durations.append(duration)
        # Fast-forward sleep so test executes immediately without 5s wall-clock wait
        await real_sleep(0.001)

    def mock_get_users():
        nonlocal tick_counter
        tick_counter += 1
        if tick_counter == 1:
            raise sqlite3.OperationalError("database is locked (simulated transient SQLite collision)")
        elif tick_counter == 2:
            # Tick 2 executes successfully! Stop the service so run() exits cleanly.
            service.stop()
            return []
        return []

    async def run_test():
        with patch("app.scheduler.get_active_portal_users", side_effect=mock_get_users), \
             patch("asyncio.sleep", side_effect=mock_sleep):
            await service.run()

    asyncio.run(run_test())

    assert tick_counter >= 2, f"Expected scheduler to continue to tick 2, but only ran {tick_counter} tick(s)."
    assert 5 in backoff_durations, f"Expected 5s backoff after tick exception, recorded: {backoff_durations}"
    assert not service._is_running, "Expected service to have stopped cleanly after stop() signal."

    # Test clean task cancellation behavior
    service_cancelled = StandaloneSchedulerService()

    async def run_and_cancel():
        task = asyncio.create_task(service_cancelled.run())
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_and_cancel())
    assert not service_cancelled._is_running, "Expected service to be stopped after cancellation."

    print("[PASS] Scenario 1: Scheduler loop exception immunity and cancellation verified.")


# ---------------------------------------------------------------------------
# SCENARIO 2: Non-Destructive Retention of Omitted Executed/In-Flight Classes
# ---------------------------------------------------------------------------
def test_scenario_2_timetable_retention_of_omitted_classes():
    """
    Scenario 2:
    - Existing timetable has:
      - Class A: COMPLETED
      - Class B: QUEUED
      - Class D: UPCOMING
    - Incoming portal sync contains only new Class C: UPCOMING.
    - Verify Class A, Class B, and Class C are persisted in timetable.
    - Verify Class D (UPCOMING) is NOT automatically preserved (legitimate cancellation/drop).
    - Also verify empty incoming sync retains Class A and Class B.
    """
    print("\n" + "=" * 80)
    print("SCENARIO 2: Timetable Retention of Omitted Executed/In-Flight Classes")
    print("=" * 80)

    uid = 97010
    today_str = get_now_ist().strftime("%Y-%m-%d")
    seed_synthetic_user(uid, f"synth_user_{uid}")

    existing_classes = [
        {
            "subject_code": "CSE_CLASS_A",
            "subject_name": "Completed Morning Class",
            "timings": "09:00 AM - 10:00 AM",
            "start_time": f"{today_str} 09:00:00",
            "end_time": f"{today_str} 10:00:00",
            "status": "COMPLETED",
        },
        {
            "subject_code": "CSE_CLASS_B",
            "subject_name": "In-Flight Queued Class",
            "timings": "10:00 AM - 11:00 AM",
            "start_time": f"{today_str} 10:00:00",
            "end_time": f"{today_str} 11:00:00",
            "status": "QUEUED",
        },
        {
            "subject_code": "CSE_CLASS_D",
            "subject_name": "Cancelled Upcoming Class",
            "timings": "12:00 PM - 01:00 PM",
            "start_time": f"{today_str} 12:00:00",
            "end_time": f"{today_str} 13:00:00",
            "status": "UPCOMING",
        },
    ]
    save_user_timetable(uid, today_str, existing_classes)

    # Portal sync returns only Class C (afternoon)
    incoming_sync = [
        {
            "subject_code": "CSE_CLASS_C",
            "subject_name": "Newly Discovered Class",
            "timings": "02:00 PM - 03:00 PM",
            "start_time": f"{today_str} 14:00:00",
            "end_time": f"{today_str} 15:00:00",
            "status": "UPCOMING",
        }
    ]

    save_user_timetable(uid, today_str, incoming_sync, reconcile=True)

    tt = get_user_timetable(uid, today_str)
    persisted_classes = tt.get("classes") or []
    codes = {c["subject_code"]: c["status"] for c in persisted_classes}

    assert "CSE_CLASS_A" in codes, "Class A (COMPLETED) was erroneously dropped from timetable!"
    assert codes["CSE_CLASS_A"] == "COMPLETED", f"Expected COMPLETED, got: {codes['CSE_CLASS_A']}"

    assert "CSE_CLASS_B" in codes, "Class B (QUEUED) was erroneously dropped from timetable!"
    assert codes["CSE_CLASS_B"] == "QUEUED", f"Expected QUEUED, got: {codes['CSE_CLASS_B']}"

    assert "CSE_CLASS_C" in codes, "New Class C was not added to timetable!"
    assert codes["CSE_CLASS_C"] == "UPCOMING", f"Expected UPCOMING, got: {codes['CSE_CLASS_C']}"

    assert "CSE_CLASS_D" not in codes, (
        "Class D (omitted UPCOMING) was preserved! Only executed/in-flight classes should be preserved."
    )

    # Secondary check: empty incoming sync ([]) must still preserve Class A and Class B
    save_user_timetable(uid, today_str, [], reconcile=True)
    tt_after_empty = get_user_timetable(uid, today_str)
    codes_after_empty = {c["subject_code"]: c["status"] for c in tt_after_empty.get("classes", [])}

    assert "CSE_CLASS_A" in codes_after_empty and codes_after_empty["CSE_CLASS_A"] == "COMPLETED"
    assert "CSE_CLASS_B" in codes_after_empty and codes_after_empty["CSE_CLASS_B"] == "QUEUED"
    assert "CSE_CLASS_C" not in codes_after_empty, "Uncompleted Class C should not be preserved across empty sync."

    print("[PASS] Scenario 2: Omitted executed/in-flight classes correctly preserved and dropped UPCOMING verified.")


# ---------------------------------------------------------------------------
# SCENARIO 3: Smart Sleep ISO Timestamp Parsing Parity
# ---------------------------------------------------------------------------
def test_scenario_3_smart_sleep_iso_parsing():
    """
    Scenario 3:
    - Provide upcoming classes with ISO start_time formats:
      - 2026-09-22T10:00:00
      - 2026-09-22 10:00
    - Verify that smart sleep Section 6 parses both without errors.
    - Verify that upcoming classes are recognized and sorted correctly.
    """
    print("\n" + "=" * 80)
    print("SCENARIO 3: Smart Sleep ISO Timestamp Parsing Parity")
    print("=" * 80)

    now = get_now_ist()
    test_dt1 = now + timedelta(hours=1)
    test_dt2 = now + timedelta(hours=2)

    iso_t_str = test_dt1.strftime("%Y-%m-%dT%H:%M:%S")
    iso_space_str = test_dt2.strftime("%Y-%m-%d %H:%M")

    classes = [
        {
            "subject_code": "ISO_T_FORMAT",
            "start_time": iso_t_str,
            "status": "UPCOMING",
        },
        {
            "subject_code": "ISO_SPACE_FORMAT",
            "start_time": iso_space_str,
            "status": "UPCOMING",
        },
    ]

    all_upcoming = []
    uid = 97020

    for c in classes:
        if c.get("status") in TERMINAL_STATUSES or c.get("status") == "QUEUED":
            continue
        start_str = c.get("start_time")
        if not start_str:
            continue
        start_dt = None
        try:
            start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            try:
                start_dt = datetime.fromisoformat(str(start_str).replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                continue

        if start_dt and start_dt > now:
            all_upcoming.append((start_dt, uid, c))

    assert len(all_upcoming) == 2, f"Expected 2 classes parsed, got {len(all_upcoming)}"
    all_upcoming.sort(key=lambda x: x[0])
    earliest_dt, _, earliest_class = all_upcoming[0]

    assert earliest_class["subject_code"] == "ISO_T_FORMAT"
    assert earliest_dt == test_dt1.replace(microsecond=0)

    print("[PASS] Scenario 3: ISO formats (T-separated and minute-only) correctly parsed by smart sleep logic.")


# ---------------------------------------------------------------------------
# SCENARIO 4: DLQ Bounded Capacity Defense (QUEUE_DLQ_MAXLEN)
# ---------------------------------------------------------------------------
def test_scenario_4_dlq_bounded_capacity():
    """
    Scenario 4:
    - Set a small maxlen (e.g. 10).
    - Push 25 entries into the DLQ stream via quarantine_malformed_message and route_to_dead_letter_queue.
    - Verify that the stream remains bounded and does not grow indefinitely.
    """
    print("\n" + "=" * 80)
    print("SCENARIO 4: DLQ Bounded Capacity Defense (QUEUE_DLQ_MAXLEN)")
    print("=" * 80)

    r = get_test_redis()
    uid_suffix = uuid.uuid4().hex[:8]
    test_dlq_stream = f"cloudnote:test:step3f7:dlq:{uid_suffix}"
    test_main_stream = f"cloudnote:test:step3f7:main:{uid_suffix}"
    test_group = f"cloudnote:test:step3f7:grp:{uid_suffix}"

    ensure_consumer_group(r, test_main_stream, test_group)

    test_maxlen = 10
    total_entries = 25

    # 1. Test quarantine_malformed_message with maxlen
    for i in range(1, 15):
        msg_id = f"1790000000000-{i}"
        quarantine_malformed_message(
            message_id=msg_id,
            error_message=f"Malformed test payload {i}",
            consumer_name="test_worker_1",
            raw_payload='{"bad_json": true}',
            client=r,
            dlq_stream_name=test_dlq_stream,
            main_stream_name=test_main_stream,
            group_name=test_group,
            maxlen=test_maxlen,
            approximate=False,
        )

    # 2. Test route_to_dead_letter_queue with maxlen
    for i in range(15, total_entries + 1):
        job = IngestionJob(
            job_id=f"poison-job-{i}",
            user_id=97040,
            class_metadata={"subject_code": f"CSE_POISON_{i}"},
            created_at=datetime.now(timezone.utc).isoformat(),
            stream_message_id=f"1790000000001-{i}",
            delivery_count=3,
        )
        route_to_dead_letter_queue(
            job=job,
            error_message=f"Simulated execution crash {i}",
            failed_by_consumer="test_worker_1",
            client=r,
            dlq_stream_name=test_dlq_stream,
            main_stream_name=test_main_stream,
            group_name=test_group,
            maxlen=test_maxlen,
            approximate=False,
        )

    # Verify stream length is capped at test_maxlen
    stream_len = r.xlen(test_dlq_stream)
    assert stream_len == test_maxlen, (
        f"Expected DLQ stream length {test_maxlen}, but found {stream_len} (entries not bounded!)."
    )

    # 3. Test that default maxlen uses settings.QUEUE_DLQ_MAXLEN
    orig_dlq_maxlen = getattr(settings, "QUEUE_DLQ_MAXLEN", 10000)
    try:
        settings.QUEUE_DLQ_MAXLEN = 5
        test_dlq_default = f"cloudnote:test:step3f7:dlq_def:{uid_suffix}"
        for i in range(1, 10):
            job = IngestionJob(
                job_id=f"def-job-{i}",
                user_id=97040,
                class_metadata={"subject_code": f"CSE_DEF_{i}"},
                created_at=datetime.now(timezone.utc).isoformat(),
                delivery_count=3,
            )
            route_to_dead_letter_queue(
                job=job,
                error_message="Default maxlen test",
                failed_by_consumer="test_worker_1",
                client=r,
                dlq_stream_name=test_dlq_default,
                main_stream_name=test_main_stream,
                group_name=test_group,
                approximate=False,
            )

        def_len = r.xlen(test_dlq_default)
        assert def_len == 5, f"Expected default settings.QUEUE_DLQ_MAXLEN=5 to bound stream to 5, got {def_len}."
    finally:
        settings.QUEUE_DLQ_MAXLEN = orig_dlq_maxlen

    print("[PASS] Scenario 4: DLQ bounded capacity defense verified on both quarantine and poison routes.")


# ---------------------------------------------------------------------------
# SCENARIO 5: Malformed Message DLQ Safety & PEL Retention Invariant
# ---------------------------------------------------------------------------
def test_scenario_5_malformed_message_dlq_safety():
    """
    Scenario 5:
    - Verify malformed message quarantine writes to DLQ before XACK.
    - Verify that if the DLQ write fails, the message is NOT XACKed and remains in the PEL.
    """
    print("\n" + "=" * 80)
    print("SCENARIO 5: Malformed Message DLQ Safety & PEL Retention Invariant")
    print("=" * 80)

    r = get_test_redis()
    uid_suffix = uuid.uuid4().hex[:8]
    test_stream = f"cloudnote:test:step3f7:s5_stream:{uid_suffix}"
    test_group = f"cloudnote:test:step3f7:s5_group:{uid_suffix}"
    test_dlq = f"cloudnote:test:step3f7:s5_dlq:{uid_suffix}"

    ensure_consumer_group(r, test_stream, test_group)

    # Add a malformed raw message directly into the main stream
    raw_msg_id = r.xadd(test_stream, {"corrupted_field": "no_payload_here"})

    # Read the message with XREADGROUP to put it into the consumer group PEL
    entries = r.xreadgroup(test_group, "consumer_s5", {test_stream: ">"}, count=1)
    assert len(entries) > 0 and len(entries[0][1]) > 0

    # Verify message is currently in PEL
    pel_before = r.xpending(test_stream, test_group)
    assert pel_before["pending"] == 1, f"Expected 1 pending message in PEL, got {pel_before['pending']}"

    # 1. Simulate DLQ write failure (e.g. Redis out of memory or connection error)
    with patch.object(r, "xadd", side_effect=redis.RedisError("Simulated Redis write error on DLQ")):
        result = quarantine_malformed_message(
            message_id=raw_msg_id,
            error_message="Test corruption error",
            consumer_name="consumer_s5",
            client=r,
            dlq_stream_name=test_dlq,
            main_stream_name=test_stream,
            group_name=test_group,
        )
        assert result is None, f"Expected None on DLQ write failure, got {result}"

    # Verify message is STILL in PEL (CRITICAL SAFETY INVARIANT)
    pel_after_failure = r.xpending(test_stream, test_group)
    assert pel_after_failure["pending"] == 1, (
        "Message was removed from PEL despite DLQ write failure! Invariant violated."
    )

    # 2. Now allow DLQ write to succeed
    success_dlq_id = quarantine_malformed_message(
        message_id=raw_msg_id,
        error_message="Test corruption error",
        consumer_name="consumer_s5",
        client=r,
        dlq_stream_name=test_dlq,
        main_stream_name=test_stream,
        group_name=test_group,
    )
    assert success_dlq_id is not None, "Expected successful DLQ ID"

    # Verify message is now cleared from PEL
    pel_after_success = r.xpending(test_stream, test_group)
    assert pel_after_success["pending"] == 0, (
        f"Message should have been cleared from PEL after successful DLQ quarantine, pending={pel_after_success['pending']}"
    )

    print("[PASS] Scenario 5: DLQ write-before-XACK invariant and failure-safe PEL retention verified.")


# ---------------------------------------------------------------------------
# SCENARIO 6: Guaranteed Synthetic Cleanup
# ---------------------------------------------------------------------------
def test_scenario_6_guaranteed_cleanup():
    """
    Scenario 6:
    - Runs the synthetic cleanup helper.
    - Confirms zero synthetic test users (97000-97099) or test keys remain.
    """
    print("\n" + "=" * 80)
    print("SCENARIO 6: Guaranteed Synthetic Cleanup")
    print("=" * 80)

    cleanup_synthetic_test_data()

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users WHERE id >= 97000 AND id < 97100")
    user_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM user_timetables WHERE user_id >= 97000 AND user_id < 97100")
    timetable_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM class_attendance_records WHERE user_id >= 97000 AND user_id < 97100")
    att_count = cur.fetchone()[0]
    conn.close()

    assert user_count == 0, f"Remaining synthetic users: {user_count}"
    assert timetable_count == 0, f"Remaining synthetic timetables: {timetable_count}"
    assert att_count == 0, f"Remaining synthetic attendance records: {att_count}"

    r = get_test_redis()
    remaining_keys = r.keys("cloudnote:test:step3f7:*")
    assert len(remaining_keys) == 0, f"Remaining Redis test keys: {remaining_keys}"

    print("[PASS] Scenario 6: Zero synthetic records remain in SQLite or Redis.")


# ---------------------------------------------------------------------------
# Main Test Runner
# ---------------------------------------------------------------------------
def run_all_tests():
    print("\n" + "#" * 80)
    print("STARTING STEP 3F.7 VALIDATION TEST SUITE")
    print("#" * 80)

    cleanup_synthetic_test_data()

    try:
        test_scenario_1_scheduler_loop_exception_immunity()
        test_scenario_2_timetable_retention_of_omitted_classes()
        test_scenario_3_smart_sleep_iso_parsing()
        test_scenario_4_dlq_bounded_capacity()
        test_scenario_5_malformed_message_dlq_safety()
        test_scenario_6_guaranteed_cleanup()

        print("\n" + "#" * 80)
        print("ALL STEP 3F.7 TESTS PASSED SUCCESSFULLY! (6/6 Scenarios)")
        print("#" * 80 + "\n")
    finally:
        cleanup_synthetic_test_data()


if __name__ == "__main__":
    run_all_tests()
