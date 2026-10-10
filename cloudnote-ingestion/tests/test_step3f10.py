"""
Step 3F.10 – Production-Correctness Tests
==========================================
Covers all four confirmed findings from the 3F.10 audit:

  Scenario 1 (F-01): Ingestion Status User Isolation
      get_user_ingestion_status(user_id) must never fall back to the global
      logs/ingestion_status.json when a user-scoped context is provided.

  Scenario 2 (F-02): Zero-Class Screenshot User Isolation
      resolve_latest_no_classes_screenshot(user_id) must not serve global
      timetable_cache.json / root screenshots/ data to authenticated users
      who have no own zero-class screenshot.
      API get_no_classes_info reports available=False, and
      get_no_classes_screenshot raises 404.

  Scenario 3 (F-03): Atomic Concurrent Sync Writes
      Concurrent set_sync_request() calls must never leave logs/sync_request.json
      in a corrupted / partially-written state.

  Scenario 4 (F-04): Same-Subject Multi-Occurrence Status Matching
      update_user_class_status() with timings=None must NOT stop at the first
      occurrence of a subject that already has a terminal status; it must
      continue and update the next non-terminal matching occurrence.

Test rules:
  - Uses only synthetic users (never touches real LPU_USERNAME account).
  - Cleans up all SQLite rows, status files, and screenshots in try/finally.
  - No network or Redis calls required.
"""

import json
import os
import sys
import shutil
import threading
import time
import uuid
from typing import Optional

# --------------------------------------------------------------------------- #
# Path setup
# --------------------------------------------------------------------------- #
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from fastapi import HTTPException
from app.database import (
    get_db_connection,
    get_user_logs_dir,
    get_user_ingestion_status,
    update_ingestion_status,
    save_user_timetable,
    update_user_class_status,
    get_user_timetable,
    init_db,
)
from app.timetable_cache import timetable_cache, SYNC_FILE
from app.utils import get_now_ist
from backend.main import (
    resolve_latest_no_classes_screenshot,
    get_no_classes_info,
    get_no_classes_screenshot,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

SYNTHETIC_BASE_ID = 9_200_000


def _create_synthetic_user(suffix: str) -> tuple[int, str]:
    """Inserts a synthetic user and returns (user_id, username)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    run_id = uuid.uuid4().hex[:6]
    username = f"test_3f10_{suffix}_{run_id}"
    try:
        cursor.execute(
            "INSERT INTO users "
            "(username, hashed_password, email, lpu_username, encrypted_portal_password) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, "hashed_test", f"{username}@test.invalid", username, "enc_test"),
        )
        conn.commit()
        cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        return row["id"], username
    finally:
        conn.close()


def _delete_synthetic_user(user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        cursor.execute("DELETE FROM user_timetables WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM class_attendance_records WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM lecture_summaries WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def _cleanup_user_files(user_id: int):
    """Remove logs/{user_id}/ and screenshots/{user_id}/ trees if present."""
    logs_base = os.path.join(REPO_ROOT, "logs", str(user_id))
    ss_base = os.path.join(REPO_ROOT, "screenshots", str(user_id))
    for p in (logs_base, ss_base):
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Scenario 1: F-01 — Ingestion Status User Isolation
# --------------------------------------------------------------------------- #

def test_scenario_1_ingestion_status_isolation():
    """
    User A has an active ingestion status file.
    User B has NO scoped ingestion status file.
    get_user_ingestion_status(user_b_id) must return the clean default
    and must NOT contain any of User A's data.
    """
    print("\n" + "=" * 65)
    print("SCENARIO 1 (F-01): Ingestion Status User Isolation")
    print("=" * 65)

    uid_a, user_a = _create_synthetic_user("f01_user_a")
    uid_b, user_b = _create_synthetic_user("f01_user_b")

    try:
        # Give User A an active ingestion status
        update_ingestion_status(
            "processing",
            details="Joining lecture now",
            subject="CSE_ISOLATION_TEST",
            user_id=uid_a,
        )

        # Verify User A's own status reads back correctly
        status_a = get_user_ingestion_status(uid_a)
        assert status_a.get("status") == "processing", (
            f"FAIL: User A status should be 'processing', got {status_a.get('status')!r}"
        )
        assert status_a.get("subject") == "CSE_ISOLATION_TEST", (
            f"FAIL: User A subject mismatch: {status_a.get('subject')!r}"
        )
        print(f"  User A status verified: status={status_a['status']}, subject={status_a['subject']}")

        # Ensure User B has NO scoped status file
        user_b_logs = get_user_logs_dir(uid_b)
        user_b_status_file = os.path.join(user_b_logs, "ingestion_status.json")
        if os.path.exists(user_b_status_file):
            os.remove(user_b_status_file)

        # Key assertion: User B must receive a clean default — not User A's data
        status_b = get_user_ingestion_status(uid_b)

        assert status_b.get("status") == "idle", (
            f"FAIL: User B must get 'idle', got {status_b.get('status')!r}"
        )
        assert status_b.get("subject", "") in ("", None), (
            f"FAIL: User B must NOT see User A's subject, got {status_b.get('subject')!r}"
        )
        assert str(status_b.get("user_id", "")) != str(uid_a), (
            f"FAIL: User B must NOT see User A's user_id ({uid_a})"
        )
        # details must be the default message (no processing detail from User A)
        assert "CSE_ISOLATION" not in str(status_b.get("details", "")), (
            f"FAIL: User A's details leaked into User B response: {status_b.get('details')!r}"
        )

        print(f"  User B status verified (isolated): status={status_b['status']}, no User A data.")
        print("PASS [1/4]: Ingestion status user isolation is correct.")

    finally:
        _delete_synthetic_user(uid_a)
        _delete_synthetic_user(uid_b)
        _cleanup_user_files(uid_a)
        _cleanup_user_files(uid_b)


# --------------------------------------------------------------------------- #
# Scenario 2: F-02 — Zero-Class Screenshot User Isolation
# --------------------------------------------------------------------------- #

def test_scenario_2_zero_class_screenshot_isolation():
    """
    Global timetable_cache.json contains a zero-class screenshot entry.
    User B has NO own scoped zero-class screenshot.
    resolve_latest_no_classes_screenshot(user_b_id) must return (None, None, None).
    API get_no_classes_info reports available=False.
    API get_no_classes_screenshot raises 404.
    """
    print("\n" + "=" * 65)
    print("SCENARIO 2 (F-02): Zero-Class Screenshot User Isolation")
    print("=" * 65)

    uid_b, username_b = _create_synthetic_user("f02_user_b")
    current_user_b = {"id": uid_b, "username": username_b}

    logs_dir = os.path.join(REPO_ROOT, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    cache_file = os.path.join(logs_dir, "timetable_cache.json")

    today_str = get_now_ist().strftime("%Y-%m-%d")
    fake_ss_filename = f"no_classes_{today_str}_12-00-00.png"

    ss_root = os.path.join(REPO_ROOT, "screenshots")
    fake_ss_path = os.path.join(ss_root, fake_ss_filename)
    cache_backup = None
    ss_created = False

    try:
        # Backup existing timetable_cache.json if present
        if os.path.exists(cache_file):
            with open(cache_file, "r", encoding="utf-8") as f:
                cache_backup = f.read()

        # Write a fake global zero-class screenshot file & cache entry
        os.makedirs(ss_root, exist_ok=True)
        with open(fake_ss_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        ss_created = True

        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({
                "date": today_str,
                "no_classes_screenshot": fake_ss_filename,
                "verified_at": f"{today_str} 12:00:00",
            }, f)

        # Ensure User B has no scoped zero-class screenshot directory or files
        user_b_ss_dir = os.path.join(ss_root, str(uid_b))
        if os.path.isdir(user_b_ss_dir):
            shutil.rmtree(user_b_ss_dir, ignore_errors=True)

        # 1. Direct function check: User B must receive (None, None, None)
        result = resolve_latest_no_classes_screenshot(user_id=uid_b)
        assert result == (None, None, None), (
            f"FAIL: Expected (None, None, None) for user {uid_b}, got {result}"
        )
        print(f"  User B (uid={uid_b}) resolve result: {result} — strictly isolated.")

        # 2. Endpoint check: get_no_classes_info for User B
        info_resp = get_no_classes_info(current_user=current_user_b)
        assert info_resp.get("available") is False, (
            f"FAIL: Expected available=False for User B, got {info_resp}"
        )
        print(f"  User B get_no_classes_info response: available={info_resp.get('available')}")

        # 3. Endpoint check: get_no_classes_screenshot for User B -> must raise 404
        raised_404 = False
        try:
            get_no_classes_screenshot(info=False, current_user=current_user_b)
        except HTTPException as exc:
            if exc.status_code == 404:
                raised_404 = True
            else:
                raise AssertionError(f"Expected 404 status code, got {exc.status_code}")
        assert raised_404, "FAIL: get_no_classes_screenshot did not raise HTTPException(404) for User B"
        print("  User B get_no_classes_screenshot raised 404 as expected.")

        # 4. Verify legacy fallback works for unauthenticated / legacy context (user_id=None)
        result_legacy = resolve_latest_no_classes_screenshot(user_id=None)
        assert result_legacy[0] is not None, (
            "FAIL: Legacy path (user_id=None) should find the global screenshot but got None"
        )
        print(f"  Legacy path (user_id=None) resolved global screenshot: {result_legacy[1]}")

        print("PASS [2/4]: Zero-class screenshot user isolation and endpoints are correct.")

    finally:
        if ss_created and os.path.exists(fake_ss_path):
            try:
                os.remove(fake_ss_path)
            except OSError:
                pass
        if cache_backup is not None:
            with open(cache_file, "w", encoding="utf-8") as f:
                f.write(cache_backup)
        elif os.path.exists(cache_file):
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump({}, f)
            except Exception:
                pass
        _delete_synthetic_user(uid_b)
        _cleanup_user_files(uid_b)


# --------------------------------------------------------------------------- #
# Scenario 3: F-03 — Atomic Concurrent Sync Writes
# --------------------------------------------------------------------------- #

def test_scenario_3_concurrent_sync_writes():
    """
    20 threads each call set_sync_request(True, user_id=<unique_id>) concurrently.
    The resulting logs/sync_request.json must be valid JSON at all times and must
    contain no corrupted / truncated state.

    Note on expected semantics:
    Because set_sync_request() performs read-modify-write (read current user_ids, add
    new id, write back), concurrent writers may overtake each other. The key invariant
    is that no write leaves the file in invalid JSON state — not that every single
    user_id is present in the final file (a full CRDT-style merge is not guaranteed
    by the current lock-free design).
    """
    print("\n" + "=" * 65)
    print("SCENARIO 3 (F-03): Atomic Concurrent Sync Writes")
    print("=" * 65)

    WRITERS = 20
    synthetic_ids = list(range(SYNTHETIC_BASE_ID, SYNTHETIC_BASE_ID + WRITERS))

    sync_backup = None
    if os.path.exists(SYNC_FILE):
        with open(SYNC_FILE, "r", encoding="utf-8") as f:
            sync_backup = f.read()
        timetable_cache.set_sync_request(False)

    errors: list[Exception] = []
    observed_corruptions: list[str] = []

    def writer(uid: int):
        try:
            timetable_cache.set_sync_request(True, user_id=uid)
        except Exception as exc:
            errors.append(exc)

    def reader_verifier():
        """Continuously reads SYNC_FILE and checks JSON validity during writes."""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if os.path.exists(SYNC_FILE):
                try:
                    with open(SYNC_FILE, "r", encoding="utf-8") as f:
                        content = f.read()
                    if content.strip():
                        json.loads(content)  # raises if corrupt
                except json.JSONDecodeError as jde:
                    observed_corruptions.append(f"JSONDecodeError: {jde} | content={content[:200]!r}")
                except Exception:
                    pass
            time.sleep(0.001)

    verifier_thread = threading.Thread(target=reader_verifier, daemon=True)
    verifier_thread.start()

    threads = [threading.Thread(target=writer, args=(uid,)) for uid in synthetic_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    verifier_thread.join(timeout=6)

    # Invariant assertions:
    assert not errors, f"FAIL: {len(errors)} writer thread(s) raised exceptions: {errors[:3]}"
    assert not observed_corruptions, (
        f"FAIL: {len(observed_corruptions)} JSON corruption(s) observed during concurrent writes:\n"
        + "\n".join(observed_corruptions[:3])
    )

    # The final file must be valid JSON
    final_data = None
    if os.path.exists(SYNC_FILE) and os.path.getsize(SYNC_FILE) > 0:
        with open(SYNC_FILE, "r", encoding="utf-8") as f:
            final_data = json.load(f)

    assert final_data is not None, "FAIL: SYNC_FILE missing or empty after concurrent writes"
    assert "sync_requested" in final_data, "FAIL: 'sync_requested' key missing from final payload"
    assert final_data["sync_requested"] is True, "FAIL: 'sync_requested' should be True"
    assert isinstance(final_data.get("user_ids"), list), "FAIL: 'user_ids' is not a list"
    assert len(final_data["user_ids"]) >= 1, (
        "FAIL: No user_ids registered — all concurrent writes failed or were dropped"
    )

    print(f"  No JSON corruption observed across {WRITERS} concurrent writers.")
    print(f"  Final user_ids count: {len(final_data['user_ids'])} (partial merge expected due to RMW).")
    print("PASS [3/4]: Concurrent atomic sync writes produce no corrupted JSON.")

    # Restore
    try:
        timetable_cache.set_sync_request(False)
        if sync_backup is not None:
            with open(SYNC_FILE, "w", encoding="utf-8") as f:
                f.write(sync_backup)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Scenario 4: F-04 — Same-Subject Multi-Occurrence Status Matching
# --------------------------------------------------------------------------- #

def test_scenario_4_multi_occurrence_status_matching():
    """
    A user has two occurrences of CSE_MULTIOCCUR on the same date:
      - 09:00 AM  already COMPLETED  (terminal)
      - 14:00     UPCOMING           (should be updated to QUEUED)

    Calling update_user_class_status(... timings=None, new_status="QUEUED")
    must:
      - Leave the 09:00 occurrence at COMPLETED (terminal-state invariance preserved).
      - Update the 14:00 occurrence to QUEUED.
    """
    print("\n" + "=" * 65)
    print("SCENARIO 4 (F-04): Same-Subject Multi-Occurrence Status Matching")
    print("=" * 65)

    uid, _ = _create_synthetic_user("f04_multi_occur")
    today_str = get_now_ist().strftime("%Y-%m-%d")

    classes = [
        {
            "subject_code": "CSE_MULTIOCCUR",
            "subject_name": "Multi Occurrence Test",
            "faculty": "Dr. Test",
            "timings": "09:00 AM - 10:00 AM",
            "start_time": f"{today_str} 09:00:00",
            "end_time": f"{today_str} 10:00:00",
            "status": "COMPLETED",   # already terminal
            "join_url": "",
        },
        {
            "subject_code": "CSE_MULTIOCCUR",
            "subject_name": "Multi Occurrence Test",
            "faculty": "Dr. Test",
            "timings": "14:00 PM - 15:00 PM",
            "start_time": f"{today_str} 14:00:00",
            "end_time": f"{today_str} 15:00:00",
            "status": "UPCOMING",   # should become QUEUED
            "join_url": "",
        },
    ]

    try:
        save_user_timetable(uid, today_str, classes)

        # Verify initial state
        tt_before = get_user_timetable(uid, today_str)
        assert tt_before is not None, "FAIL: Timetable not saved"
        cls_before = tt_before["classes"]
        assert cls_before[0]["status"] == "COMPLETED", "FAIL: 09:00 occurrence should start as COMPLETED"
        assert cls_before[1]["status"] == "UPCOMING",  "FAIL: 14:00 occurrence should start as UPCOMING"
        print(f"  Initial state: 09:00={cls_before[0]['status']}, 14:00={cls_before[1]['status']}")

        # Call with timings=None (subject-only match — triggers the multi-occurrence path)
        result = update_user_class_status(
            uid, today_str, "CSE_MULTIOCCUR", timings=None, new_status="QUEUED"
        )

        assert result is True, (
            "FAIL: update_user_class_status returned False — did not update any occurrence"
        )

        tt_after = get_user_timetable(uid, today_str)
        cls_after = tt_after["classes"]

        status_09 = cls_after[0]["status"]
        status_14 = cls_after[1]["status"]
        print(f"  After update:  09:00={status_09}, 14:00={status_14}")

        assert status_09 == "COMPLETED", (
            f"FAIL: Terminal status must be preserved. 09:00 is {status_09!r} (expected 'COMPLETED')"
        )
        assert status_14 == "QUEUED", (
            f"FAIL: Second occurrence must be updated. 14:00 is {status_14!r} (expected 'QUEUED')"
        )

        print("PASS [4/4]: Same-subject multi-occurrence terminal preservation is correct.")

    finally:
        _delete_synthetic_user(uid)
        _cleanup_user_files(uid)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def run_all():
    init_db()
    scenarios = [
        ("Scenario 1 — Ingestion Status Isolation (F-01)",    test_scenario_1_ingestion_status_isolation),
        ("Scenario 2 — Zero-Class Screenshot Isolation (F-02)", test_scenario_2_zero_class_screenshot_isolation),
        ("Scenario 3 — Concurrent Sync Writes (F-03)",        test_scenario_3_concurrent_sync_writes),
        ("Scenario 4 — Multi-Occurrence Status Matching (F-04)", test_scenario_4_multi_occurrence_status_matching),
    ]

    passed = 0
    failed = 0
    for name, fn in scenarios:
        try:
            fn()
            passed += 1
        except Exception as exc:
            import traceback
            print(f"\nFAIL [{name}]: {exc}")
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 65)
    print(f"Step 3F.10 Results: {passed}/{passed + failed} passed, {failed} failed")
    print("=" * 65)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
