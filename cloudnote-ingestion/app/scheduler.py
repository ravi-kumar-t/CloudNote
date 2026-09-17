"""
CloudNote Autonomous Timetable Scheduler
-----------------------------------------
Dedicated runtime entrypoint for timetable schedule tracking and Redis job queueing.
Fetches daily timetable data, monitors upcoming class countdowns, and enqueues
active class jobs to the Redis Stream ('cloudnote:jobs:stream').
Strictly decoupled: Never instantiates or executes WorkerConsumer or browser joining.
"""

import asyncio
from datetime import datetime, time, timedelta
import logging
import signal
import sys
from typing import Optional

from playwright.async_api import async_playwright

from .config import settings
from .logger import logger
from .utils import get_now_ist
from .database import update_ingestion_status, get_or_create_ingestion_user
from .timetable_cache import timetable_cache
from .timetable_fetcher import fetch_timetable_data
from .login import perform_login
from .job_queue import schedule_class_to_queue


class StandaloneSchedulerService:
    """Manages the autonomous timetable sync and class queueing scheduler loop."""

    def __init__(self):
        self._is_running = True
        self._shutdown_event = asyncio.Event()

    def handle_signal(self, sig, frame=None):
        """Signal handler for graceful scheduler termination."""
        sig_name = signal.Signals(sig).name if hasattr(signal, "Signals") else str(sig)
        logger.info(f"SchedulerService: Received signal {sig_name}. Stopping scheduler...")
        self._is_running = False
        self._shutdown_event.set()

    async def run(self):
        """Main autonomous scheduler loop."""
        logger.info("==========================================================")
        logger.info("Starting Dedicated CloudNote Autonomous Timetable Scheduler")
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

        if not settings.DEBUG_MODE:
            if any(c.get("subject_code", "").startswith("DEBUG_") for c in timetable_cache.classes):
                logger.info("Scheduler: Found debug data in timetable cache. Wiping cache for REAL MODE.")
                timetable_cache.mark_stale()

        try:
            while self._is_running:
                # 1. Resilient scheduler heartbeat update (Sidecar Integration)
                try:
                    from .redis_service import redis_service
                    redis_service.set_scheduler_heartbeat()
                except Exception:
                    pass

                # 2. Check cache state
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
                    classes = [active_class]
                    cache_valid = True
                    sync_requested = False
                else:
                    classes = timetable_cache.get_timetable()
                    cache_valid = timetable_cache.is_valid_for_today()
                    sync_requested = timetable_cache.is_sync_requested()

                logger.info(f"DEBUG CACHE CLASS COUNT: {len(classes)}")
                logger.info(f"DEBUG CACHE VALID: {cache_valid}")

                # 3. If cache is missing, stale, or sync is manually requested, run a Headless Fetch
                if not cache_valid or sync_requested:
                    logger.info("Scheduler: Timetable cache is stale, missing, or sync requested. Performing headless sync...")
                    update_ingestion_status("processing", details="Performing headless timetable sync session")
                    timetable_cache.set_sync_request(False)

                    async with async_playwright() as p:
                        logger.info("Scheduler: Launching headless browser for timetable fetch...")
                        browser = await p.chromium.launch(headless=settings.HEADLESS, args=["--start-maximized"])
                        context = await browser.new_context(
                            ignore_https_errors=True,
                            viewport={'width': 1920, 'height': 1080},
                            timezone_id="Asia/Kolkata",
                            locale="en-IN",
                        )
                        page = await context.new_page()
                        page.set_default_timeout(settings.BROWSER_TIMEOUT)

                        try:
                            await perform_login(page)
                            fetched_classes = await fetch_timetable_data(page)
                            timetable_cache.set_timetable(fetched_classes)
                            classes = fetched_classes
                            update_ingestion_status("idle", details=f"Timetable sync complete. Found {len(classes)} classes for today.")
                        except Exception as sync_err:
                            logger.error(f"Scheduler: Headless sync session failed: {sync_err}")
                            update_ingestion_status("failed", error=f"Timetable sync failed: {str(sync_err)}")
                        finally:
                            await context.close()
                            await browser.close()
                            logger.info("Scheduler: Headless browser closed.")

                # 4. Process timetable to compute next execution window
                now = get_now_ist()
                active_class = None
                next_upcoming_class = None
                min_seconds_to_start = None

                for c in classes:
                    start_str = c.get("start_time")
                    end_str = c.get("end_time")
                    if not start_str or not end_str:
                        continue

                    try:
                        start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
                        end_dt = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        continue

                    should_join = False
                    if start_dt <= now <= end_dt:
                        should_join = True
                    elif settings.DEBUG_MODE:
                        logger.info("DEBUG_MODE active: Bypassing scheduler gating to force join flow.")
                        should_join = True

                    if should_join:
                        if c.get("status") not in ("COMPLETED", "QUEUED"):
                            active_class = c
                            break

                    elif start_dt > now:
                        seconds_to_start = (start_dt - now).total_seconds()
                        if min_seconds_to_start is None or seconds_to_start < min_seconds_to_start:
                            min_seconds_to_start = seconds_to_start
                            next_upcoming_class = c

                # 5. Decision Orchestrator
                if active_class:
                    if settings.DEMO_MODE:
                        logger.info("DEMO_MODE active: Waiting 15 seconds for backend to stabilize inside Docker network...")
                        await asyncio.sleep(15)

                    subject = active_class.get("subject_code", "Lecture")
                    logger.info(f"Scheduler: Active class found: {subject}. Enqueueing job to Redis stream...")

                    enqueued_to_redis = False
                    if settings.REDIS_URL:
                        try:
                            job, enqueued = schedule_class_to_queue(active_class)
                            if enqueued and job:
                                enqueued_to_redis = True
                                logger.info(f"Scheduler: Successfully enqueued class job {job.job_id} ({job.idempotency_key}) to Redis stream.")
                                update_ingestion_status("processing", details=f"Class job queued for worker: {subject}", subject=subject)
                                active_class["status"] = "QUEUED"
                                timetable_cache.set_timetable(classes)
                            elif not enqueued:
                                enqueued_to_redis = True
                                logger.info(f"Scheduler: Class job for {subject} already enqueued/deduplicated via idempotency key.")
                                active_class["status"] = "QUEUED"
                                timetable_cache.set_timetable(classes)
                        except Exception as q_err:
                            logger.error(f"Scheduler: Failed to enqueue class job to Redis: {q_err}. Scheduler will retry on next tick.")
                            enqueued_to_redis = False
                    else:
                        logger.error("Scheduler: REDIS_URL not configured. Cannot enqueue class job to Redis.")

                    if not enqueued_to_redis:
                        logger.warning(f"Scheduler: Class {subject} could not be enqueued. Waiting cooldown before retry...")
                        await asyncio.sleep(10)

                    if settings.DEMO_MODE:
                        logger.info("=== DEMO_MODE SINGLE RUN COMPLETE, GOING IDLE ===")
                        while True:
                            await asyncio.sleep(3600)
                    elif settings.DEBUG_MODE:
                        logger.info("=== DEBUG_MODE SINGLE RUN COMPLETE, BREAKING LOOP ===")
                        break

                elif next_upcoming_class:
                    start_str = next_upcoming_class.get("start_time")
                    start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")

                    current_time = get_now_ist()
                    seconds_remaining = (start_dt - current_time).total_seconds()
                    sleep_duration = max(seconds_remaining - 300, 30)

                    logger.info(f"Scheduler: Next upcoming class {next_upcoming_class.get('subject_code')} starts at {start_str}.")
                    logger.info(f"Scheduler: Entering smart sleep for {sleep_duration} seconds (waking up 5 minutes before start)...")
                    update_ingestion_status("idle", details=f"Next class: {next_upcoming_class.get('subject_code')} at {start_dt.strftime('%I:%M %p')}")

                    target_time = asyncio.get_event_loop().time() + sleep_duration
                    while asyncio.get_event_loop().time() < target_time and self._is_running:
                        try:
                            from .redis_service import redis_service
                            redis_service.set_scheduler_heartbeat()
                        except Exception:
                            pass
                        if timetable_cache.is_sync_requested() or not timetable_cache.is_valid_for_today():
                            logger.info("Scheduler: Sleep interrupted by sync request or date rollover. Waking up!")
                            break
                        await asyncio.sleep(10)

                else:
                    if len(classes) == 0:
                        logger.info("Scheduler: No classes scheduled for today.")
                        status_details = "No classes scheduled for today. Resting until tomorrow."
                    else:
                        logger.info("Scheduler: All scheduled classes for today have been queued or completed.")
                        status_details = "All classes queued/completed for today. Resting until tomorrow."

                    current_ist = get_now_ist()
                    tomorrow_8am = datetime.combine(current_ist.date() + timedelta(days=1), time(8, 0))
                    sleep_duration = (tomorrow_8am - current_ist).total_seconds()

                    logger.info(f"Scheduler: Entering smart sleep until tomorrow 8:00 AM ({sleep_duration} seconds)...")
                    update_ingestion_status("idle", details=status_details)

                    target_time = asyncio.get_event_loop().time() + sleep_duration
                    while asyncio.get_event_loop().time() < target_time and self._is_running:
                        try:
                            from .redis_service import redis_service
                            redis_service.set_scheduler_heartbeat()
                        except Exception:
                            pass
                        if timetable_cache.is_sync_requested() or not timetable_cache.is_valid_for_today():
                            logger.info("Scheduler: Sleep interrupted by sync request or date rollover. Waking up!")
                            break
                        await asyncio.sleep(10)

        except asyncio.CancelledError:
            logger.info("SchedulerService: Cancelled. Shutting down scheduler gracefully...")
        finally:
            logger.info("SchedulerService: Stopped cleanly.")


async def run_scheduler():
    service = StandaloneSchedulerService()
    await service.run()


if __name__ == "__main__":
    asyncio.run(run_scheduler())
