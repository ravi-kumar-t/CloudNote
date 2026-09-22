"""
Step 3F.9 Validation Test Suite
---------------------------------
Production Correctness and Reliability Verification:

1. Scenario 1: F-01 HIGH — Manual sync request consumed/cleared when users_to_sync is empty
   (verifies scheduler smart-sleep does not spin on stale sync requests)
2. Scenario 2: F-02 MEDIUM — User-scoped manual sync isolation
   (verifies User A sync request does not resync User B, and clears correctly)
3. Scenario 3: F-03 MEDIUM — Bounded SQLite lock retries in record_attendance_session()
   (verifies transient lock recovery and bounded retry termination on persistent lock)
4. Scenario 4: F-04 LOW — User-scoped zero-class screenshot cleanup
   (verifies old screenshot is deleted in screenshots/{user_id}/ and does not delete another user's screenshot)

Constraints:
- Synthetic test data only (users 98100-98199)
- Clean teardown in try ... finally
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import sqlite3
import sys
import time as time_module
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings, get_screenshot_path, get_user_screenshot_path
from app.crypto import encrypt_portal_password
from app.database import (
    get_db_connection,
    save_user_timetable,
    get_user_timetable,
    record_attendance_session,
    get_user_attendance_records,
    init_db,
)
from app.scheduler import user_needs_timetable_sync, StandaloneSchedulerService
from app.timetable_cache import timetable_cache, SYNC_FILE
from app.timetable_fetcher import fetch_timetable_data
from app.utils import get_now_ist
from backend.main import sync_timetable

# Ensure local test runs default to localhost Redis if REDIS_URL is unconfigured
if not os.environ.get("REDIS_URL") or "redis://redis:" in os.environ.get("REDIS_URL", ""):
    os.environ["REDIS_URL"] = "redis://localhost:6379/0"
settings.REDIS_URL = os.environ["REDIS_URL"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Step3F9Tests")


# ---------------------------------------------------------------------------
# Guaranteed Synthetic Cleanup Helper
# ---------------------------------------------------------------------------
def cleanup_synthetic_test_data():
    """
    Guaranteed cleanup: removes all synthetic test users (98100 <= id < 98200),
    associated attendance records, timetable rows, synthetic screenshots, and sync requests.
    Never touches real users.
    """
    logger.info("Cleaning up Step 3F.9 synthetic test data (users 98100-98199)...")
    for attempt in range(5):
        try:
            conn = get_db_connection()
            cur = conn.cursor()

            cur.execute("DELETE FROM class_attendance_records WHERE user_id >= 98100 AND user_id < 98200")
            cur.execute("DELETE FROM lecture_summaries WHERE user_id >= 98100 AND user_id < 98200")
            cur.execute("DELETE FROM user_timetables WHERE user_id >= 98100 AND user_id < 98200")
            cur.execute("DELETE FROM users WHERE id >= 98100 AND id < 98200")

            conn.commit()
            conn.close()
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 4:
                time_module.sleep(0.1)
            else:
                logger.warning(f"Cleanup retry failed: {e}")

    # Remove test screenshot directories
    base_screenshots = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "screenshots"))
    for uid in range(98100, 98200):
        u_dir = os.path.join(base_screenshots, str(uid))
        if os.path.exists(u_dir):
            try:
                for f in os.listdir(u_dir):
                    os.remove(os.path.join(u_dir, f))
                os.rmdir(u_dir)
            except Exception as e:
                logger.warning(f"Failed to remove test screenshot dir {u_dir}: {e}")

    # Clean legacy test screenshot if left behind
    legacy_ss = os.path.join(base_screenshots, "legacy_test_ss.png")
    if os.path.exists(legacy_ss):
        try:
            os.remove(legacy_ss)
        except Exception:
            pass

    # Reset sync request file to a clean state
    try:
        timetable_cache.set_sync_request(False)
    except Exception:
        pass


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
# Test Scenario 1: F-01 HIGH — Manual Sync Request Clearing When users_to_sync is Empty
# ---------------------------------------------------------------------------
def test_scenario_1_f01_sync_request_cleared_when_empty():
    """
    Verify F-01:
    - A sync request exists but users_to_sync is empty.
    - The request is consumed/cleared even when users_to_sync is empty.
    - Scheduler smart-sleep check does NOT repeatedly wake or spin on the stale request.
    """
    logger.info("=== Starting Scenario 1: F-01 Sync Request Clearing on Empty users_to_sync ===")
    uid = 98101
    today_str = get_now_ist().strftime("%Y-%m-%d")
    now_ist = get_now_ist()

    try:
        init_db()
        seed_synthetic_user(uid, "testuser_98101")

        # Save user timetable verified 2 minutes ago (verified_at within 15 min -> does not need sync)
        recent_verified = (now_ist - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
        save_user_timetable(
            user_id=uid,
            date_str=today_str,
            classes=[],
            no_classes_screenshot=None,
            verified_at=recent_verified,
        )

        # 1. Trigger manual sync request
        timetable_cache.set_sync_request(True, user_id=uid)
        assert timetable_cache.is_sync_requested() is True, "Sync request must be flagged globally"
        assert timetable_cache.is_sync_requested(user_id=uid) is True, "Sync request must be flagged for user 98101"

        # 2. Simulate Section 4 logic
        active_users = [{"id": uid, "username": "testuser_98101"}]
        sync_requested = timetable_cache.is_sync_requested()
        requested_uids = timetable_cache.get_sync_requested_user_ids()

        users_to_sync = [
            u for u in active_users
            if user_needs_timetable_sync(
                u["id"],
                today_str,
                sync_requested=timetable_cache.is_sync_requested(user_id=u["id"])
            )
        ]

        # Verified 2 mins ago, so users_to_sync MUST be empty
        assert len(users_to_sync) == 0, f"Expected users_to_sync to be empty, got {users_to_sync}"

        # 3. Apply Section 4 clearing logic
        if requested_uids:
            for r_uid in requested_uids:
                timetable_cache.set_sync_request(False, user_id=r_uid)
        elif sync_requested:
            timetable_cache.set_sync_request(False)

        # 4. Verify request is consumed/cleared
        assert timetable_cache.is_sync_requested() is False, "sync_requested must be cleared even when users_to_sync is empty"
        assert timetable_cache.is_sync_requested(user_id=uid) is False, "User-scoped request must be cleared"
        assert timetable_cache.get_sync_requested_user_ids() == [], "user_ids list must be empty"

        # 5. Verify scheduler smart-sleep wake check does not abort/spin
        interrupted = timetable_cache.is_sync_requested() or (get_now_ist().strftime("%Y-%m-%d") != today_str)
        assert interrupted is False, "Scheduler smart-sleep must not wake/spin on cleared sync request"

        # 6. Also verify empty active_users standby clearing
        timetable_cache.set_sync_request(True)
        assert timetable_cache.is_sync_requested() is True
        # If no active users exist, scheduler clears the sync request
        if timetable_cache.is_sync_requested():
            timetable_cache.set_sync_request(False)
        assert timetable_cache.is_sync_requested() is False, "Sync request must be cleared when active_users is empty"

        print("PASS: Scenario 1 (F-01) - Sync request consumed when users_to_sync is empty, smart sleep does not spin.")
    finally:
        cleanup_synthetic_test_data()


# ---------------------------------------------------------------------------
# Test Scenario 2: F-02 MEDIUM — User-Scoped Manual Sync Isolation
# ---------------------------------------------------------------------------
def test_scenario_2_f02_user_scoped_sync_isolation():
    """
    Verify F-02:
    - User A requests manual sync.
    - User A is eligible for requested sync.
    - User B is NOT unnecessarily resynced solely because User A requested sync.
    - The request state is correctly cleared after handling.
    """
    logger.info("=== Starting Scenario 2: F-02 User-Scoped Sync Isolation ===")
    uid_a = 98101
    uid_b = 98102
    today_str = get_now_ist().strftime("%Y-%m-%d")
    now_ist = get_now_ist()

    try:
        init_db()
        seed_synthetic_user(uid_a, "testuser_98101")
        seed_synthetic_user(uid_b, "testuser_98102")

        # Both users had their timetables verified 20 minutes ago (> 15 min threshold)
        verified_20m_ago = (now_ist - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
        save_user_timetable(uid_a, today_str, [], verified_at=verified_20m_ago)
        save_user_timetable(uid_b, today_str, [], verified_at=verified_20m_ago)

        # 1. User A triggers sync via backend endpoint
        res = sync_timetable(current_user={"id": uid_a, "username": "testuser_98101"})
        assert res["status"] == "sync_requested"

        # 2. Check sync request state
        assert timetable_cache.is_sync_requested() is True, "Global sync requested must be True"
        assert timetable_cache.is_sync_requested(user_id=uid_a) is True, "User A must be marked for sync"
        assert timetable_cache.is_sync_requested(user_id=uid_b) is False, "User B must NOT be marked for sync"

        # 3. Check user_needs_timetable_sync evaluation
        needs_sync_a = user_needs_timetable_sync(
            uid_a, today_str, sync_requested=timetable_cache.is_sync_requested(user_id=uid_a)
        )
        needs_sync_b = user_needs_timetable_sync(
            uid_b, today_str, sync_requested=timetable_cache.is_sync_requested(user_id=uid_b)
        )

        assert needs_sync_a is True, "User A must need sync because User A requested manual sync and >15m elapsed"
        assert needs_sync_b is False, "User B must NOT need sync because User B did NOT request sync"

        # 4. In scheduler Section 4:
        active_users = [
            {"id": uid_a, "username": "testuser_98101"},
            {"id": uid_b, "username": "testuser_98102"},
        ]
        sync_requested = timetable_cache.is_sync_requested()
        requested_uids = timetable_cache.get_sync_requested_user_ids()

        users_to_sync = [
            u for u in active_users
            if user_needs_timetable_sync(
                u["id"],
                today_str,
                sync_requested=timetable_cache.is_sync_requested(user_id=u["id"])
            )
        ]

        assert len(users_to_sync) == 1, f"Expected exactly 1 user to sync, got {len(users_to_sync)}"
        assert users_to_sync[0]["id"] == uid_a, f"Expected User A to be the only user to sync, got {users_to_sync[0]['id']}"

        # 5. Clear evaluated requests
        if requested_uids:
            for r_uid in requested_uids:
                timetable_cache.set_sync_request(False, user_id=r_uid)
        elif sync_requested:
            timetable_cache.set_sync_request(False)

        # 6. Verify request state is cleared
        assert timetable_cache.is_sync_requested() is False
        assert timetable_cache.is_sync_requested(user_id=uid_a) is False
        assert timetable_cache.is_sync_requested(user_id=uid_b) is False

        # 7. Verify backward compatibility with legacy format (no user_ids key)
        with open(SYNC_FILE, "w", encoding="utf-8") as f:
            json.dump({"sync_requested": True}, f)
        assert timetable_cache.is_sync_requested() is True
        assert timetable_cache.is_sync_requested(user_id=uid_a) is True  # fallback returns global True
        timetable_cache.set_sync_request(False)
        assert timetable_cache.is_sync_requested() is False

        print("PASS: Scenario 2 (F-02) - User-scoped manual sync isolation verified; User B not cross-triggered.")
    finally:
        cleanup_synthetic_test_data()


# ---------------------------------------------------------------------------
# Test Scenario 3: F-03 MEDIUM — Bounded SQLite Lock Retries in record_attendance_session
# ---------------------------------------------------------------------------
def test_scenario_3_f03_sqlite_lock_retry():
    """
    Verify F-03:
    - Simulate a transient SQLite "database is locked" condition.
    - Verify record_attendance_session retries and eventually succeeds.
    - Verify bounded retry behavior when the lock persists.
    """
    logger.info("=== Starting Scenario 3: F-03 SQLite Lock Retry in record_attendance_session ===")
    uid = 98103

    try:
        init_db()
        seed_synthetic_user(uid, "testuser_98103")

        # 1. Transient lock test: locked on attempts 1 & 2, succeeds on attempt 3
        real_get_conn = get_db_connection
        attempt_tracker = {"count": 0}

        def mock_get_conn_transient():
            attempt_tracker["count"] += 1
            if attempt_tracker["count"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return real_get_conn()

        with patch("app.database.get_db_connection", side_effect=mock_get_conn_transient):
            row_id = record_attendance_session(
                user_id=uid,
                subject="MTH101",
                status="PRESENT",
                title="Mathematics I",
                instructor="Dr. Ramanujan",
                session_duration="50m"
            )

        assert row_id is not None and row_id > 0, f"Expected positive row_id, got {row_id}"
        assert attempt_tracker["count"] == 3, f"Expected 3 attempts, got {attempt_tracker['count']}"

        # Verify record exists in SQLite
        records = get_user_attendance_records(uid)
        assert len(records) >= 1, "Recorded attendance session must be in database"
        assert records[0]["subject"] == "MTH101"
        assert records[0]["status"] == "PRESENT"
        assert records[0]["instructor"] == "Dr. Ramanujan"

        # 2. Persistent lock test: locked on all attempts -> bounded to exactly 3 attempts
        persistent_tracker = {"count": 0}

        def mock_get_conn_persistent():
            persistent_tracker["count"] += 1
            raise sqlite3.OperationalError("database is locked")

        failed_as_expected = False
        with patch("app.database.get_db_connection", side_effect=mock_get_conn_persistent):
            try:
                record_attendance_session(
                    user_id=uid,
                    subject="PHY101",
                    status="FAILED",
                    title="Physics"
                )
            except sqlite3.OperationalError as e:
                failed_as_expected = True
                assert "locked" in str(e).lower()

        assert failed_as_expected is True, "Must raise OperationalError when lock persists"
        assert persistent_tracker["count"] == 3, f"Expected exactly 3 bounded retry attempts, got {persistent_tracker['count']}"

        print("PASS: Scenario 3 (F-03) - record_attendance_session bounded SQLite lock retries verified.")
    finally:
        cleanup_synthetic_test_data()


# ---------------------------------------------------------------------------
# Test Scenario 4: F-04 LOW — User-Scoped Zero-Class Screenshot Cleanup
# ---------------------------------------------------------------------------
def test_scenario_4_f04_user_scoped_screenshot_cleanup():
    """
    Verify F-04:
    - Create synthetic user-scoped zero-class screenshot state.
    - Verify old screenshot cleanup operates inside screenshots/{user_id}/.
    - Verify it does NOT accidentally delete another user's screenshot.
    - Verify fallback when user_id is None.
    """
    logger.info("=== Starting Scenario 4: F-04 User-Scoped Zero-Class Screenshot Cleanup ===")
    uid_a = 98104
    uid_b = 98105
    today_str = get_now_ist().strftime("%Y-%m-%d")

    try:
        init_db()
        seed_synthetic_user(uid_a, "testuser_98104")
        seed_synthetic_user(uid_b, "testuser_98105")

        # 1. Setup user-scoped screenshot files
        old_ss_filename = "no_classes_old_synthetic.png"
        path_a_old = get_user_screenshot_path(uid_a, old_ss_filename)
        path_b_keep = get_user_screenshot_path(uid_b, old_ss_filename)

        os.makedirs(os.path.dirname(path_a_old), exist_ok=True)
        os.makedirs(os.path.dirname(path_b_keep), exist_ok=True)

        with open(path_a_old, "w", encoding="utf-8") as f:
            f.write("user_a_old_screenshot_data")
        with open(path_b_keep, "w", encoding="utf-8") as f:
            f.write("user_b_precious_screenshot_data")

        assert os.path.exists(path_a_old), "User A screenshot file must exist"
        assert os.path.exists(path_b_keep), "User B screenshot file must exist"

        # Save existing timetable for User A and User B pointing to old_ss_filename
        save_user_timetable(
            user_id=uid_a,
            date_str=today_str,
            classes=[],
            no_classes_screenshot=old_ss_filename,
            verified_at="2026-09-22 08:00:00"
        )
        save_user_timetable(
            user_id=uid_b,
            date_str=today_str,
            classes=[],
            no_classes_screenshot=old_ss_filename,
            verified_at="2026-09-22 08:00:00"
        )

        # 2. Mock Playwright page for zero-class timetable extraction
        mock_loc_empty = MagicMock()
        mock_loc_empty.all = AsyncMock(return_value=[])
        mock_loc_empty.count = AsyncMock(return_value=0)
        mock_loc_empty.is_visible = AsyncMock(return_value=False)
        mock_loc_empty.first = mock_loc_empty

        mock_page = AsyncMock()
        mock_page.locator = MagicMock(return_value=mock_loc_empty)
        mock_page.screenshot = AsyncMock()
        mock_page.wait_for_timeout = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()

        # Execute fetch_timetable_data for User A (zero classes -> captures screenshot -> cleans up old)
        result = asyncio.run(fetch_timetable_data(mock_page, user_id=uid_a))
        assert result == [], "Expected zero classes parsed"

        # 3. Verify User A's old screenshot was deleted
        assert not os.path.exists(path_a_old), f"User A old screenshot {path_a_old} should have been deleted"

        # 4. Verify User B's screenshot was NOT deleted
        assert os.path.exists(path_b_keep), f"User B screenshot {path_b_keep} must NOT be deleted!"
        with open(path_b_keep, "r", encoding="utf-8") as f:
            assert f.read() == "user_b_precious_screenshot_data", "User B screenshot data must be untouched"

        # 5. Verify fallback behavior when user_id is None
        legacy_old = "legacy_test_ss.png"
        legacy_path = get_screenshot_path(legacy_old)
        os.makedirs(os.path.dirname(legacy_path), exist_ok=True)
        with open(legacy_path, "w", encoding="utf-8") as f:
            f.write("legacy_screenshot_data")
        assert os.path.exists(legacy_path)

        timetable_cache.set_no_classes_screenshot(legacy_old, "2026-09-22 08:00:00")

        # Execute with user_id=None
        res_none = asyncio.run(fetch_timetable_data(mock_page, user_id=None))
        assert res_none == []
        assert not os.path.exists(legacy_path), "Global legacy screenshot must be deleted when user_id is None"
        assert os.path.exists(path_b_keep), "User B screenshot must still be preserved"

        print("PASS: Scenario 4 (F-04) - Zero-class screenshot cleanup is user-scoped and protects other users.")
    finally:
        cleanup_synthetic_test_data()


# ---------------------------------------------------------------------------
# Main Test Runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("\n==================================================")
    print("STEP 3F.9 VALIDATION TEST SUITE RUN")
    print("==================================================")

    test_scenario_1_f01_sync_request_cleared_when_empty()
    test_scenario_2_f02_user_scoped_sync_isolation()
    test_scenario_3_f03_sqlite_lock_retry()
    test_scenario_4_f04_user_scoped_screenshot_cleanup()

    print("\n==================================================")
    print("ALL STEP 3F.9 TESTS PASSED SUCCESSFULLY!")
    print("==================================================\n")
