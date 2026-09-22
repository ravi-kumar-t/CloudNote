"""
CloudNote Autonomous Multi-User Timetable Scheduler
---------------------------------------------------
Dedicated runtime entrypoint for multi-user timetable tracking and Redis job queueing.
Discovers active registered student accounts from SQLite, synchronizes per-user
timetables using isolated browser contexts, monitors upcoming class countdowns across
all active users, and enqueues active class jobs to the Redis Stream ('cloudnote:jobs:stream').
Strictly decoupled: Never instantiates or executes WorkerConsumer or browser meeting joining.
"""

import asyncio
from datetime import datetime, time, timedelta
import logging
import re
import signal
import sys
from typing import Optional, List, Dict, Any

from playwright.async_api import async_playwright

from .config import settings
from .logger import logger
from .utils import get_now_ist
from .database import (
    update_ingestion_status,
    get_or_create_ingestion_user,
    get_active_portal_users,
    get_user_portal_credentials,
    get_user_timetable,
    update_user_class_status,
    save_user_timetable,
)
from .timetable_cache import timetable_cache
from .timetable_fetcher import fetch_timetable_data
from .login import perform_login
from .job_queue import (
    schedule_class_to_queue,
    get_execution_lock_key,
    get_redis_client,
)

TERMINAL_STATUSES = {"COMPLETED", "FACULTY_NOT_STARTED", "FAILED", "MISSED"}


async def wait_for_countdown(page, countdown_selector: str = ""):
    """Parses countdown text inside the lecture modal and waits accordingly."""
    try:
        # Targeted modal parsing
        modal = page.locator("div[role='dialog'], div.modal, .modal-content, .ui-dialog").first

        # Scoped selector only inside lecture modal/popup
        if await modal.count() > 0:
            logger.info("Found lecture modal, scoping countdown search to modal.")
            modal_text = await modal.inner_text()
            text = modal_text.strip().lower()

            h_match = re.search(r'(\d+)\s*h', text)
            m_match = re.search(r'(\d+)\s*m', text)
            s_match = re.search(r'(\d+)\s*s', text)

            if not (h_match or m_match or s_match):
                logger.info("No countdown pattern found in modal. Proceeding to join.")
                return
        else:
            logger.info("No modal found, skipping countdown.")
            return

        logger.info(f"Detected countdown components in modal: h={h_match.group(1) if h_match else 0}, m={m_match.group(1) if m_match else 0}, s={s_match.group(1) if s_match else 0}")

        hours = int(h_match.group(1)) if h_match else 0
        minutes = int(m_match.group(1)) if m_match else 0
        seconds = int(s_match.group(1)) if s_match else 0

        total_seconds = hours * 3600 + minutes * 60 + seconds

        if total_seconds > 0:
            wait_time = max(total_seconds - 120, 30)
            logger.info(f"Waiting for {wait_time} seconds (wake-up safety buffer applied; total countdown: {total_seconds}s) until class starts...")
            await asyncio.sleep(wait_time)
        else:
            logger.info("Class is ready to join.")

    except Exception as e:
        logger.warning(f"Error parsing countdown: {str(e)}. Proceeding anyway.")


async def sync_user_timetable_in_context(
    browser,
    user: dict,
    today_str: str,
) -> list:
    """
    Performs an isolated headless timetable synchronization for a single user
    using a dedicated Playwright BrowserContext. Guarantees complete session,
    cookie, and storage isolation from all other users.
    """
    user_id = user["id"]
    username = user["username"]
    lpu_user = user.get("lpu_username") or username

    user_creds = get_user_portal_credentials(user_id)
    if not user_creds or not user_creds.get("portal_password"):
        logger.warning(
            f"Scheduler: Missing or undecryptable portal credentials for user {user_id} ({lpu_user}). Skipping sync."
        )
        return []

    portal_password = user_creds["portal_password"]

    # Isolated incognito browser context per user
    context = await browser.new_context(
        ignore_https_errors=True,
        viewport={'width': 1920, 'height': 1080},
        timezone_id="Asia/Kolkata",
        locale="en-IN",
    )
    page = await context.new_page()
    page.set_default_timeout(settings.BROWSER_TIMEOUT)
    page.set_default_navigation_timeout(settings.NAVIGATION_TIMEOUT)

    try:
        logger.info(f"Scheduler: Syncing timetable for user {user_id} ({lpu_user}) in isolated context...")
        await perform_login(page, username=lpu_user, password=portal_password, user_id=user_id)
        fetched_classes = await fetch_timetable_data(page, user_id=user_id)
        logger.info(f"Scheduler: Synced {len(fetched_classes)} classes for user {user_id} ({lpu_user}).")
        return fetched_classes
    except Exception as sync_err:
        logger.error(f"Scheduler: Timetable sync failed for user {user_id} ({lpu_user}): {sync_err}. Continuing with remaining users.")
        return []
    finally:
        del portal_password
        try:
            await context.close()
        except Exception:
            pass


def user_needs_timetable_sync(user_id: int, today_str: str, sync_requested: bool = False) -> bool:
    """
    Determines whether a user requires a browser timetable sync.
    Avoids unnecessary browser launches if today's timetable is already verified and valid.
    """
    # Note: SQLite concurrency remains a known limitation and is deferred to a later database/concurrency step.
    tt = get_user_timetable(user_id, today_str)
    if not tt:
        return True

    verified_at = tt.get("verified_at")
    if not verified_at or today_str not in str(verified_at):
        return True

    if sync_requested:
        try:
            v_dt = datetime.strptime(str(verified_at), "%Y-%m-%d %H:%M:%S")
            # If verified more than 15 minutes ago, allow manual refresh
            if (get_now_ist() - v_dt).total_seconds() > 900:
                return True
        except Exception:
            return True

    return False


class StandaloneSchedulerService:
    """Manages the autonomous multi-user timetable sync and class queueing scheduler loop."""

    def __init__(self):
        self._is_running = True
        self._shutdown_event = asyncio.Event()

    def stop(self):
        """Signals the scheduler loop to stop."""
        self._is_running = False
        self._shutdown_event.set()

    def handle_signal(self, sig, frame=None):
        """Signal handler for graceful scheduler termination."""
        sig_name = signal.Signals(sig).name if hasattr(signal, "Signals") else str(sig)
        logger.info(f"SchedulerService: Received signal {sig_name}. Stopping scheduler...")
        self.stop()

    async def run(self):
        """Main autonomous multi-user scheduler loop."""
        logger.info("==========================================================")
        logger.info("Starting CloudNote Autonomous Multi-User Timetable Scheduler")
        logger.info("==========================================================")

        # Reset persistent session status to clean IDLE state on application startup
        try:
            from .session_status import reset_session_status
            reset_session_status()
        except Exception as reset_err:
            logger.error(f"SchedulerService: Failed to reset session status on startup: {reset_err}")

        # Register signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.handle_signal, sig)
            except (NotImplementedError, AttributeError):
                signal.signal(sig, self.handle_signal)

        try:
            while self._is_running:
                try:
                    # 1. Resilient scheduler heartbeat update (Sidecar Integration)
                    try:
                        from .redis_service import redis_service
                        redis_service.set_scheduler_heartbeat()
                    except Exception:
                        pass

                    today_str = get_now_ist().strftime("%Y-%m-%d")

                    # 2. Discover active registered portal users
                    # Note: SQLite concurrency remains a known limitation and is deferred to a later database/concurrency step.
                    active_users = get_active_portal_users()
                    if not active_users:
                        logger.info("Scheduler: No active registered portal users found in database. Waiting 30s...")
                        update_ingestion_status("idle", details="No active registered portal users found. Waiting for registrations.")

                        target_time = asyncio.get_event_loop().time() + 30
                        while asyncio.get_event_loop().time() < target_time and self._is_running:
                            try:
                                from .redis_service import redis_service
                                redis_service.set_scheduler_heartbeat()
                            except Exception:
                                pass
                            if timetable_cache.is_sync_requested():
                                break
                            await asyncio.sleep(5)
                        continue

                    logger.info(f"Scheduler: Discovered {len(active_users)} active portal user(s).")

                    # 3. Handle DEBUG_MODE simulation if active
                    if settings.DEBUG_MODE:
                        logger.info("=== DEBUG_MODE SIMULATION ACTIVE ===")
                        now = get_now_ist()
                        if settings.DEBUG_SIMULATION_MODE.upper() == "FAILURE":
                            join_url = f"{settings.BACKEND_INTERNAL_URL}/debug-meeting-fail"
                            subject = "DEBUG_FAIL"
                        else:
                            join_url = f"{settings.BACKEND_INTERNAL_URL}/debug-meeting"
                            subject = "DEBUG_SUCCESS"

                        active_class = {
                            "subject_code": subject,
                            "subject_name": "Debug Automation Class",
                            "faculty": "Dr. Playwright Tester",
                            "timings": "10:00 AM - 11:00 AM",
                            "start_time": (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
                            "end_time": (now + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S"),
                            "join_url": join_url,
                            "status": "UPCOMING"
                        }
                        for u in active_users:
                            user_tt = get_user_timetable(u["id"], today_str)
                            if not user_tt:
                                save_user_timetable(u["id"], today_str, [active_class], verified_at=now.strftime("%Y-%m-%d %H:%M:%S"))

                    # 4. Multi-User Timetable Sync Check
                    sync_requested = timetable_cache.is_sync_requested()
                    users_to_sync = [
                        u for u in active_users
                        if user_needs_timetable_sync(u["id"], today_str, sync_requested=sync_requested)
                    ]

                    if users_to_sync and not settings.DEBUG_MODE:
                        logger.info(f"Scheduler: {len(users_to_sync)} user(s) require timetable sync. Launching browser...")
                        update_ingestion_status(
                            "processing",
                            details=f"Performing headless timetable sync for {len(users_to_sync)} user(s)"
                        )

                        async with async_playwright() as p:
                            browser = await p.chromium.launch(
                                headless=settings.HEADLESS,
                                args=["--start-maximized"]
                            )
                            try:
                                for user in users_to_sync:
                                    await sync_user_timetable_in_context(browser, user, today_str)
                            finally:
                                await browser.close()
                                logger.info("Scheduler: Headless browser closed cleanly.")

                        timetable_cache.set_sync_request(False)
                        update_ingestion_status("idle", details="Multi-user timetable sync pass complete.")

                    # 5. Process timetables across all active users & enqueue active classes
                    now = get_now_ist()
                    total_enqueued = 0

                    for user in active_users:
                        uid = user["id"]
                        user_tt = get_user_timetable(uid, today_str)
                        if not user_tt or not user_tt.get("classes"):
                            continue

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
                                            r = get_redis_client()
                                            lock_key = get_execution_lock_key(uid, c)
                                            if r.exists(lock_key):
                                                lock_held = True
                                        except Exception as lk_err:
                                            logger.debug(f"Scheduler: Error checking lock for user {uid}: {lk_err}")

                                    if lock_held:
                                        logger.info(
                                            f"Scheduler: Class {subject} for user {uid} is past end_time + 5m, "
                                            f"but active ExecutionLock is held. Leaving QUEUED for worker completion."
                                        )
                                    else:
                                        logger.info(
                                            f"Scheduler: Expired class detected for user {uid}: {subject} "
                                            f"(end_time {end_str} + 5m passed, no active lock). Transitioning to MISSED."
                                        )
                                        update_user_class_status(
                                            uid, today_str, subject, timings=c.get("timings"), new_status="MISSED"
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
                                logger.info(f"Scheduler: Active class found for user {uid}: {subject}. Enqueueing to Redis...")

                                if settings.REDIS_URL:
                                    try:
                                        job, enqueued = schedule_class_to_queue(c, user_id=uid)
                                        if enqueued and job:
                                            logger.info(
                                                f"Scheduler: Successfully enqueued job {job.job_id} "
                                                f"({job.idempotency_key}) for user {uid}."
                                            )
                                            update_user_class_status(
                                                uid, today_str, subject, timings=c.get("timings"), new_status="QUEUED"
                                            )
                                            total_enqueued += 1
                                        elif not enqueued:
                                            logger.info(
                                                f"Scheduler: Job for user {uid}, {subject} already enqueued/deduplicated."
                                            )
                                            # Non-destructive deduplication: NEVER overwrite terminal status
                                            if c.get("status") not in TERMINAL_STATUSES:
                                                update_user_class_status(
                                                    uid, today_str, subject, timings=c.get("timings"), new_status="QUEUED"
                                                )
                                    except Exception as q_err:
                                        logger.error(
                                            f"Scheduler: Failed to enqueue class job to Redis for user {uid}: {q_err}. "
                                            f"Will retry on next tick."
                                        )
                                else:
                                    logger.error("Scheduler: REDIS_URL not configured. Cannot enqueue class job to Redis.")

                    if settings.DEMO_MODE and total_enqueued > 0:
                        logger.info("=== DEMO_MODE SINGLE RUN COMPLETE, GOING IDLE ===")
                        while self._is_running:
                            await asyncio.sleep(3600)
                    elif settings.DEBUG_MODE and total_enqueued > 0:
                        logger.info("=== DEBUG_MODE SINGLE RUN COMPLETE, BREAKING LOOP ===")
                        break

                    # 6. Unified Multi-User Smart Sleep Calculation
                    all_upcoming = []
                    total_classes_today = 0
                    now = get_now_ist()

                    for user in active_users:
                        uid = user["id"]
                        user_tt = get_user_timetable(uid, today_str)
                        if not user_tt or not user_tt.get("classes"):
                            continue
                        classes = user_tt["classes"]
                        total_classes_today += len(classes)
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

                    if all_upcoming:
                        all_upcoming.sort(key=lambda x: x[0])
                        earliest_dt, earliest_uid, earliest_class = all_upcoming[0]
                        seconds_remaining = (earliest_dt - now).total_seconds()
                        sleep_duration = max(seconds_remaining - 300, 30)

                        logger.info(
                            f"Scheduler: Next upcoming class across all users is {earliest_class.get('subject_code')} "
                            f"(user {earliest_uid}) starting at {earliest_dt.strftime('%Y-%m-%d %H:%M:%S')}."
                        )
                        logger.info(f"Scheduler: Entering smart sleep for {sleep_duration:.0f} seconds (waking 5m before start)...")
                        update_ingestion_status(
                            "idle",
                            details=f"Next class: {earliest_class.get('subject_code')} (user {earliest_uid}) at {earliest_dt.strftime('%I:%M %p')}"
                        )

                        target_time = asyncio.get_event_loop().time() + sleep_duration
                        while asyncio.get_event_loop().time() < target_time and self._is_running:
                            try:
                                from .redis_service import redis_service
                                redis_service.set_scheduler_heartbeat()
                            except Exception:
                                pass
                            if timetable_cache.is_sync_requested() or get_now_ist().strftime("%Y-%m-%d") != today_str:
                                logger.info("Scheduler: Sleep interrupted by sync request or date rollover. Waking up!")
                                break
                            await asyncio.sleep(10)

                    else:
                        if total_classes_today == 0:
                            logger.info("Scheduler: No classes scheduled for any active users today.")
                            status_details = "No classes scheduled for today across active users. Resting until tomorrow."
                        else:
                            logger.info("Scheduler: All scheduled classes for today have been queued or finalized across all active users.")
                            status_details = "All classes queued/finalized for today. Resting until tomorrow."

                        current_ist = get_now_ist()
                        if current_ist.time() < time(8, 0):
                            target_wake = datetime.combine(current_ist.date(), time(8, 0))
                            target_desc = "today 8:00 AM"
                        else:
                            target_wake = datetime.combine(current_ist.date() + timedelta(days=1), time(8, 0))
                            target_desc = "tomorrow 8:00 AM"
                        sleep_duration = max((target_wake - current_ist).total_seconds(), 30)

                        logger.info(f"Scheduler: Entering smart sleep until {target_desc} ({sleep_duration:.0f} seconds)...")
                        update_ingestion_status("idle", details=status_details)

                        target_time = asyncio.get_event_loop().time() + sleep_duration
                        while asyncio.get_event_loop().time() < target_time and self._is_running:
                            try:
                                from .redis_service import redis_service
                                redis_service.set_scheduler_heartbeat()
                            except Exception:
                                pass
                            if timetable_cache.is_sync_requested() or get_now_ist().strftime("%Y-%m-%d") != today_str:
                                logger.info("Scheduler: Sleep interrupted by sync request or date rollover. Waking up!")
                                break
                            await asyncio.sleep(10)

                except asyncio.CancelledError:
                    raise
                except Exception as loop_err:
                    from .worker_consumer import sanitize_log_message
                    err_msg = sanitize_log_message(str(loop_err))[:300]
                    logger.error(
                        f"Scheduler: Unexpected error in scheduler loop: {type(loop_err).__name__}: {err_msg}. "
                        "Backing off for 5s before next iteration."
                    )
                    if not self._is_running:
                        break
                    try:
                        await asyncio.sleep(5)
                    except asyncio.CancelledError:
                        raise

        except asyncio.CancelledError:
            logger.info("SchedulerService: Cancelled. Shutting down scheduler gracefully...")
        finally:
            self._is_running = False
            logger.info("SchedulerService: Stopped cleanly.")


async def run_scheduler():
    service = StandaloneSchedulerService()
    await service.run()


if __name__ == "__main__":
    asyncio.run(run_scheduler())
