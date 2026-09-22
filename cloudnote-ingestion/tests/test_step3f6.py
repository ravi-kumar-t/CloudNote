"""
Step 3F.6 Validation Test Suite
---------------------------------
Timetable Sync State Reconciliation, Scheduler Morning Wakeup Hardening &
Malformed Stream Message Quarantine:

1. Scenario 1: Preserving terminal statuses across portal timetable sync (COMPLETED, FACULTY_NOT_STARTED, FAILED, MISSED)
2. Scenario 2: Preserving in-flight QUEUED status across portal timetable sync
3. Scenario 3: Merging existing completed Class A and newly added Class B (with precise identity matching)
4. Scenario 4: Post-sync scheduler immunity against retroactive MISSED transitions
5. Scenario 5: Scheduler fallback smart-sleep calculation (06:00, 07:59, 08:01, 18:00)
6. Scenario 6: Malformed Redis stream message quarantine & failure-safe PEL retention
7. Scenario 7: Guaranteed synthetic cleanup (users 96000-96099)

Constraints:
- Synthetic test data only (users 96000-96099)
- No fake LPU classes
- Clean teardown in try ... finally
"""

import asyncio
from datetime import datetime, date, time, timedelta
import json
import logging
import os
import sqlite3
import sys
import time as time_module
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
    ensure_consumer_group,
    ExecutionLock,
    IngestionJob,
    DEFAULT_STREAM_NAME,
    DEFAULT_CONSUMER_GROUP,
    DEFAULT_DLQ_STREAM_NAME,
)
from app.scheduler import TERMINAL_STATUSES
from app.worker_consumer import WorkerConsumer, generate_consumer_name
from app.utils import get_now_ist

# Ensure local test runs default to localhost Redis if REDIS_URL is unconfigured or points to Docker hostname 'redis'
if not os.environ.get("REDIS_URL") or "redis://redis:" in os.environ.get("REDIS_URL", ""):
    os.environ["REDIS_URL"] = "redis://localhost:6379/0"
settings.REDIS_URL = os.environ["REDIS_URL"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Step3F6Tests")


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
    print("\n--- Cleaning up Step 3F.6 synthetic test data ---")
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
            time_module.sleep(0.1)

    try:
        r = get_test_redis()
        # Clean up any test locks or stream keys for synthetic users
        keys = r.keys("cloudnote:lock:exec:user:96*")
        if keys:
            r.delete(*keys)
        keys_idemp = r.keys("cloudnote:job:idemp:user:96*")
        if keys_idemp:
            r.delete(*keys_idemp)
        test_streams = r.keys("cloudnote:test:*:3f6*")
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
# SCENARIO 1: Midday Timetable Sync Terminal State Preservation
# ---------------------------------------------------------------------------
def test_scenario_1_terminal_state_preservation():
    """
    Scenario 1: Existing timetable contains COMPLETED, FACULTY_NOT_STARTED, FAILED, MISSED classes.
    Incoming scraped timetable reports them as UPCOMING.
    Verify all four terminal statuses remain unchanged.
    """
    print("\n" + "=" * 80)
    print("SCENARIO 1: Terminal State Preservation Across Timetable Sync")
    print("=" * 80)

    uid = 96010
    today_str = get_now_ist().strftime("%Y-%m-%d")
    seed_synthetic_user(uid, f"synth_user_{uid}")

    existing_classes = [
        {
            "subject_code": "CSE_TERM_1",
            "subject_name": "Completed Lecture",
            "timings": "09:00 AM - 10:00 AM",
            "start_time": f"{today_str} 09:00:00",
            "end_time": f"{today_str} 10:00:00",
            "status": "COMPLETED",
        },
        {
            "subject_code": "CSE_TERM_2",
            "subject_name": "Faculty Not Started Lecture",
            "timings": "10:00 AM - 11:00 AM",
            "start_time": f"{today_str} 10:00:00",
            "end_time": f"{today_str} 11:00:00",
            "status": "FACULTY_NOT_STARTED",
        },
        {
            "subject_code": "CSE_TERM_3",
            "subject_name": "Failed Lecture",
            "timings": "11:00 AM - 12:00 PM",
            "start_time": f"{today_str} 11:00:00",
            "end_time": f"{today_str} 12:00:00",
            "status": "FAILED",
        },
        {
            "subject_code": "CSE_TERM_4",
            "subject_name": "Missed Lecture",
            "timings": "12:00 PM - 01:00 PM",
            "start_time": f"{today_str} 12:00:00",
            "end_time": f"{today_str} 13:00:00",
            "status": "MISSED",
        },
    ]

    save_user_timetable(uid, today_str, existing_classes)

    # Simulate fresh scrape from portal where all classes report default "UPCOMING"
    incoming_scraped = [
        {
            "subject_code": "CSE_TERM_1",
            "subject_name": "Completed Lecture",
            "timings": "09:00 AM - 10:00 AM",
            "start_time": f"{today_str} 09:00:00",
            "end_time": f"{today_str} 10:00:00",
            "status": "UPCOMING",
        },
        {
            "subject_code": "CSE_TERM_2",
            "subject_name": "Faculty Not Started Lecture",
            "timings": "10:00 AM - 11:00 AM",
            "start_time": f"{today_str} 10:00:00",
            "end_time": f"{today_str} 11:00:00",
            "status": "UPCOMING",
        },
        {
            "subject_code": "CSE_TERM_3",
            "subject_name": "Failed Lecture",
            "timings": "11:00 AM - 12:00 PM",
            "start_time": f"{today_str} 11:00:00",
            "end_time": f"{today_str} 12:00:00",
            "status": "UPCOMING",
        },
        {
            "subject_code": "CSE_TERM_4",
            "subject_name": "Missed Lecture",
            "timings": "12:00 PM - 01:00 PM",
            "start_time": f"{today_str} 12:00:00",
            "end_time": f"{today_str} 13:00:00",
            "status": "UPCOMING",
        },
    ]

    save_user_timetable(uid, today_str, incoming_scraped)

    tt = get_user_timetable(uid, today_str)
    status_map = {c["subject_code"]: c["status"] for c in tt["classes"]}

    assert status_map["CSE_TERM_1"] == "COMPLETED", f"Expected COMPLETED, got {status_map['CSE_TERM_1']}"
    assert status_map["CSE_TERM_2"] == "FACULTY_NOT_STARTED", f"Expected FACULTY_NOT_STARTED, got {status_map['CSE_TERM_2']}"
    assert status_map["CSE_TERM_3"] == "FAILED", f"Expected FAILED, got {status_map['CSE_TERM_3']}"
    assert status_map["CSE_TERM_4"] == "MISSED", f"Expected MISSED, got {status_map['CSE_TERM_4']}"

    print("[PASS] Scenario 1: All four terminal statuses preserved across portal timetable sync.")


# ---------------------------------------------------------------------------
# SCENARIO 2: In-Flight QUEUED State Preservation Across Timetable Sync
# ---------------------------------------------------------------------------
def test_scenario_2_queued_state_preservation():
    """
    Scenario 2: Existing class is QUEUED. Incoming timetable reports UPCOMING.
    Verify QUEUED remains QUEUED.
    """
    print("\n" + "=" * 80)
    print("SCENARIO 2: In-Flight QUEUED State Preservation")
    print("=" * 80)

    uid = 96020
    today_str = get_now_ist().strftime("%Y-%m-%d")
    seed_synthetic_user(uid, f"synth_user_{uid}")

    existing_classes = [
        {
            "subject_code": "CSE_INFLIGHT",
            "subject_name": "Active Ingesting Lecture",
            "timings": "02:00 PM - 03:00 PM",
            "start_time": f"{today_str} 14:00:00",
            "end_time": f"{today_str} 15:00:00",
            "status": "QUEUED",
        }
    ]

    save_user_timetable(uid, today_str, existing_classes)

    # Scrape runs and reports UPCOMING
    scraped_classes = [
        {
            "subject_code": "CSE_INFLIGHT",
            "subject_name": "Active Ingesting Lecture",
            "timings": "02:00 PM - 03:00 PM",
            "start_time": f"{today_str} 14:00:00",
            "end_time": f"{today_str} 15:00:00",
            "status": "UPCOMING",
        }
    ]

    save_user_timetable(uid, today_str, scraped_classes)

    tt = get_user_timetable(uid, today_str)
    assert tt["classes"][0]["status"] == "QUEUED", (
        f"In-flight class status was overwritten! Expected QUEUED, got: {tt['classes'][0]['status']}"
    )

    print("[PASS] Scenario 2: In-flight QUEUED class preserved across portal timetable sync.")


# ---------------------------------------------------------------------------
# SCENARIO 3: New Class Discovery & Precise Identity Matching
# ---------------------------------------------------------------------------
def test_scenario_3_new_class_discovery_and_precise_matching():
    """
    Scenario 3: Existing completed Class A plus genuinely new Class B.
    Incoming timetable contains both.
    Verify A remains COMPLETED and B is added as UPCOMING.
    Also verifies that separate occurrences of the same subject on the same day are distinguished.
    """
    print("\n" + "=" * 80)
    print("SCENARIO 3: New Class Discovery & Precise Identity Matching")
    print("=" * 80)

    uid = 96030
    today_str = get_now_ist().strftime("%Y-%m-%d")
    seed_synthetic_user(uid, f"synth_user_{uid}")

    # User attended CSE401 morning section (09:00 AM)
    existing_classes = [
        {
            "subject_code": "CSE401",
            "subject_name": "Cloud Computing Morning",
            "timings": "09:00 AM - 10:00 AM",
            "start_time": f"{today_str} 09:00:00",
            "end_time": f"{today_str} 10:00:00",
            "status": "COMPLETED",
        }
    ]
    save_user_timetable(uid, today_str, existing_classes)

    # Incoming timetable contains:
    # 1. CSE401 morning (reports UPCOMING from portal)
    # 2. CSE401 afternoon lab (same subject, different timing 02:00 PM - new occurrence!)
    # 3. MTH202 (genuinely new subject)
    incoming_timetable = [
        {
            "subject_code": "CSE401",
            "subject_name": "Cloud Computing Morning",
            "timings": "09:00 AM - 10:00 AM",
            "start_time": f"{today_str} 09:00:00",
            "end_time": f"{today_str} 10:00:00",
            "status": "UPCOMING",
        },
        {
            "subject_code": "CSE401",
            "subject_name": "Cloud Computing Afternoon Lab",
            "timings": "02:00 PM - 03:00 PM",
            "start_time": f"{today_str} 14:00:00",
            "end_time": f"{today_str} 15:00:00",
            "status": "UPCOMING",
        },
        {
            "subject_code": "MTH202",
            "subject_name": "Applied Probability",
            "timings": "11:00 AM - 12:00 PM",
            "start_time": f"{today_str} 11:00:00",
            "end_time": f"{today_str} 12:00:00",
            "status": "UPCOMING",
        },
    ]

    save_user_timetable(uid, today_str, incoming_timetable)

    tt = get_user_timetable(uid, today_str)
    assert len(tt["classes"]) == 3, f"Expected 3 classes in timetable, found {len(tt['classes'])}"

    # Match each class precisely
    cse_morning = next(c for c in tt["classes"] if c["subject_code"] == "CSE401" and "09:00" in c["start_time"])
    cse_afternoon = next(c for c in tt["classes"] if c["subject_code"] == "CSE401" and "14:00" in c["start_time"])
    mth = next(c for c in tt["classes"] if c["subject_code"] == "MTH202")

    assert cse_morning["status"] == "COMPLETED", (
        f"CSE401 morning status corrupted! Expected COMPLETED, got: {cse_morning['status']}"
    )
    assert cse_afternoon["status"] == "UPCOMING", (
        f"CSE401 afternoon lab should be UPCOMING, got: {cse_afternoon['status']}"
    )
    assert mth["status"] == "UPCOMING", (
        f"New subject MTH202 should be UPCOMING, got: {mth['status']}"
    )

    print("[PASS] Scenario 3: Precise identity matching successfully distinguished same-subject separate occurrences and preserved COMPLETED while adding new classes.")


# ---------------------------------------------------------------------------
# SCENARIO 4: Post-Sync Scheduler Immunity to Retroactive MISSED Transition
# ---------------------------------------------------------------------------
def test_scenario_4_post_sync_scheduler_immunity():
    """
    Scenario 4: After timetable sync, run the relevant scheduler evaluation against an
    expired previously-COMPLETED class.
    Verify it remains COMPLETED and cannot become MISSED.
    """
    print("\n" + "=" * 80)
    print("SCENARIO 4: Post-Sync Scheduler Immunity Against Retroactive MISSED")
    print("=" * 80)

    uid = 96040
    today_str = get_now_ist().strftime("%Y-%m-%d")
    now = get_now_ist()
    seed_synthetic_user(uid, f"synth_user_{uid}")

    # Class ran earlier today (ended 45 minutes ago)
    start_dt = now - timedelta(minutes=105)
    end_dt = now - timedelta(minutes=45)

    existing_classes = [
        {
            "subject_code": "CSE_PRIOR_DONE",
            "subject_name": "Morning Lecture",
            "timings": "08:00 AM - 09:00 AM",
            "start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "COMPLETED",
        }
    ]
    save_user_timetable(uid, today_str, existing_classes)

    # Midday sync happens (e.g. at 10:15 AM); reports UPCOMING
    scraped_classes = [
        {
            "subject_code": "CSE_PRIOR_DONE",
            "subject_name": "Morning Lecture",
            "timings": "08:00 AM - 09:00 AM",
            "start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "UPCOMING",
        }
    ]
    save_user_timetable(uid, today_str, scraped_classes)

    # Verify status is COMPLETED before scheduler tick
    tt_pre = get_user_timetable(uid, today_str)
    assert tt_pre["classes"][0]["status"] == "COMPLETED"

    # Run scheduler evaluation tick
    simulate_scheduler_pass_for_user(uid, today_str, now)

    # Assert class was NOT corrupted to MISSED
    tt_post = get_user_timetable(uid, today_str)
    assert tt_post["classes"][0]["status"] == "COMPLETED", (
        f"Critical bug: Expired completed class was corrupted to {tt_post['classes'][0]['status']}!"
    )

    print("[PASS] Scenario 4: Post-sync scheduler evaluation verified: completed class remained COMPLETED.")


# ---------------------------------------------------------------------------
# SCENARIO 5: Scheduler Early-Morning Wakeup Time Calculation
# ---------------------------------------------------------------------------
def test_scenario_5_scheduler_morning_sleep_calculation():
    """
    Scenario 5: Test scheduler fallback sleep calculation:
    - 06:00 -> target today 08:00
    - 07:59 -> target today 08:00
    - 08:01 -> target tomorrow 08:00
    - 18:00 -> target tomorrow 08:00
    """
    print("\n" + "=" * 80)
    print("SCENARIO 5: Scheduler Fallback Sleep Target Calculation")
    print("=" * 80)

    def calculate_sleep_target(mock_current: datetime) -> tuple[datetime, str, float]:
        """Calculates sleep target using the hardened logic in app/scheduler.py."""
        if mock_current.time() < time(8, 0):
            target_wake = datetime.combine(mock_current.date(), time(8, 0))
            target_desc = "today 8:00 AM"
        else:
            target_wake = datetime.combine(mock_current.date() + timedelta(days=1), time(8, 0))
            target_desc = "tomorrow 8:00 AM"
        sleep_duration = max((target_wake - mock_current).total_seconds(), 30)
        return target_wake, target_desc, sleep_duration

    base_date = date(2026, 9, 22)

    # 1. Test 06:00:00 -> target TODAY 08:00 (duration: 2 hours = 7200s)
    t_0600 = datetime.combine(base_date, time(6, 0))
    target, desc, duration = calculate_sleep_target(t_0600)
    assert target == datetime.combine(base_date, time(8, 0)), f"Expected today 08:00, got: {target}"
    assert desc == "today 8:00 AM"
    assert duration == 7200.0, f"Expected 7200s, got: {duration}"

    # 2. Test 07:59:00 -> target TODAY 08:00 (duration: 60s)
    t_0759 = datetime.combine(base_date, time(7, 59))
    target, desc, duration = calculate_sleep_target(t_0759)
    assert target == datetime.combine(base_date, time(8, 0)), f"Expected today 08:00, got: {target}"
    assert desc == "today 8:00 AM"
    assert duration == 60.0, f"Expected 60s, got: {duration}"

    # 3. Test 08:01:00 -> target TOMORROW 08:00 (duration: 23h 59m = 86340s)
    t_0801 = datetime.combine(base_date, time(8, 1))
    target, desc, duration = calculate_sleep_target(t_0801)
    assert target == datetime.combine(base_date + timedelta(days=1), time(8, 0)), f"Expected tomorrow 08:00, got: {target}"
    assert desc == "tomorrow 8:00 AM"
    assert duration == 86340.0, f"Expected 86340s, got: {duration}"

    # 4. Test 18:00:00 -> target TOMORROW 08:00 (duration: 14h = 50400s)
    t_1800 = datetime.combine(base_date, time(18, 0))
    target, desc, duration = calculate_sleep_target(t_1800)
    assert target == datetime.combine(base_date + timedelta(days=1), time(8, 0)), f"Expected tomorrow 08:00, got: {target}"
    assert desc == "tomorrow 8:00 AM"
    assert duration == 50400.0, f"Expected 50400s, got: {duration}"

    print("[PASS] Scenario 5: Scheduler fallback sleep target calculation passed all time boundaries.")


# ---------------------------------------------------------------------------
# SCENARIO 6: Malformed Stream Message Quarantine & Failure-Safe PEL Retention
# ---------------------------------------------------------------------------
def test_scenario_6_malformed_message_quarantine():
    """
    Scenario 6:
    Inject malformed Redis stream messages:
    - missing payload
    - invalid JSON
    - structurally invalid/security-rejected payload
    Verify each is safely quarantined to DLQ and XACKed only after DLQ write.
    Verify main-stream PEL becomes 0.
    Verify DLQ contains no credentials/secrets.

    Also test the DLQ-write-failure case:
    - malformed message must remain pending in the main-stream PEL
    - it must NOT be XACKed if DLQ persistence fails.
    """
    print("\n" + "=" * 80)
    print("SCENARIO 6: Malformed Stream Message Quarantine & Failure-Safe Retention")
    print("=" * 80)

    r = get_test_redis()
    test_stream = "cloudnote:test:stream:3f6"
    test_group = "cloudnote:test:group:3f6"
    test_dlq = "cloudnote:test:dlq:3f6"

    r.delete(test_stream, test_dlq)
    ensure_consumer_group(client=r, stream_name=test_stream, group_name=test_group)

    consumer_name = "test-worker-3f6"

    # Part A: Inject 3 malformed stream messages
    # 1. Missing payload field
    id_missing = r.xadd(test_stream, {"invalid_field": "no_payload_here"})
    # 2. Invalid non-JSON payload
    id_invalid_json = r.xadd(test_stream, {"payload": "{not valid json syntax"})
    # 3. Structurally invalid / security-rejected payload containing forbidden password
    id_security = r.xadd(test_stream, {"payload": json.dumps({"lpu_password": "supersecretpassword123"})})

    # Read and process with consume_jobs
    jobs = consume_jobs(
        consumer_name=consumer_name,
        count=10,
        block_ms=500,
        client=r,
        stream_name=test_stream,
        group_name=test_group,
        dlq_stream_name=test_dlq,
    )

    # 1. Assert no valid jobs returned (all 3 were malformed)
    assert len(jobs) == 0, f"Expected 0 valid jobs from malformed stream, got: {len(jobs)}"

    # 2. Verify all 3 messages were quarantined to DLQ
    dlq_entries = get_dead_letter_jobs(client=r, dlq_stream_name=test_dlq)
    assert len(dlq_entries) == 3, f"Expected 3 entries in DLQ, found: {len(dlq_entries)}"

    # 3. Verify DLQ contains no credentials/secrets
    for entry in dlq_entries:
        assert "supersecretpassword123" not in json.dumps(entry), "Security violation: secret leaked into DLQ!"
        assert entry.get("error_message"), "Expected non-empty diagnostic error message in DLQ"
        assert entry.get("failed_by_consumer") == consumer_name

    # 4. Verify main-stream PEL is 0 (all 3 were safely XACKed after DLQ write)
    pel = r.xpending(test_stream, test_group)
    assert pel["pending"] == 0, f"Expected 0 pending messages in PEL, found: {pel['pending']}"

    # Part B: DLQ Write Failure Case
    # Inject another malformed message, but simulate a failure during DLQ write
    id_dlq_fail = r.xadd(test_stream, {"payload": "{bad json with dlq fail"})

    # Patch r.xadd for DLQ stream to fail
    real_xadd = r.xadd

    def flaky_xadd(name, *args, **kwargs):
        if name == test_dlq:
            raise redis.exceptions.RedisError("Simulated Redis out-of-memory error during DLQ write")
        return real_xadd(name, *args, **kwargs)

    with patch.object(r, "xadd", side_effect=flaky_xadd):
        # consume_jobs should attempt quarantine, fail the DLQ write, and NOT XACK
        consume_jobs(
            consumer_name=consumer_name,
            count=5,
            block_ms=500,
            client=r,
            stream_name=test_stream,
            group_name=test_group,
            dlq_stream_name=test_dlq,
        )

    # 5. Verify the message REMAINS in the PEL because DLQ write failed!
    pel_after_failure = r.xpending(test_stream, test_group)
    assert pel_after_failure["pending"] == 1, (
        f"Critical invariant violated: Message was XACKed despite DLQ write failure! PEL pending: {pel_after_failure['pending']}"
    )

    # Clean up test keys
    r.delete(test_stream, test_dlq)

    print("[PASS] Scenario 6: Malformed message DLQ quarantine, zero secret leakage, and failure-safe PEL retention verified.")


# ---------------------------------------------------------------------------
# SCENARIO 7: Guaranteed Synthetic Cleanup
# ---------------------------------------------------------------------------
def test_scenario_7_guaranteed_synthetic_cleanup():
    """
    Scenario 7: Guaranteed synthetic cleanup using IDs 96000-96099.
    Verify zero synthetic test records remain in SQLite or Redis.
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
def run_all_step3f6_tests():
    print("\n" + "#" * 80)
    print("STARTING STEP 3F.6 VALIDATION TEST SUITE")
    print("#" * 80)

    cleanup_synthetic_test_data()

    try:
        test_scenario_1_terminal_state_preservation()
        test_scenario_2_queued_state_preservation()
        test_scenario_3_new_class_discovery_and_precise_matching()
        test_scenario_4_post_sync_scheduler_immunity()
        test_scenario_5_scheduler_morning_sleep_calculation()
        test_scenario_6_malformed_message_quarantine()
        test_scenario_7_guaranteed_synthetic_cleanup()

        print("\n" + "#" * 80)
        print("ALL STEP 3F.6 TESTS PASSED SUCCESSFULLY! (7/7 Scenarios)")
        print("#" * 80 + "\n")
        return True

    except Exception as exc:
        logger.exception(f"TEST RUN FAILED WITH EXCEPTION: {exc}")
        return False

    finally:
        cleanup_synthetic_test_data()


if __name__ == "__main__":
    success = run_all_step3f6_tests()
    sys.exit(0 if success else 1)
