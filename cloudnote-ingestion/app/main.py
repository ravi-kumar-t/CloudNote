import asyncio
import os
import json
from typing import Optional, Union, Dict, Any
from playwright.async_api import async_playwright
from .config import settings, get_screenshot_path, get_user_screenshot_path
from .logger import logger
from datetime import datetime, timezone, timedelta, date, time
import sys
from .extractor import LectureExtractor
from .utils import get_now_ist
async def create_browser_and_page(p):
    """Launches browser, context, and page with settings-defined defaults and microphone permissions."""
    logger.info("Launching browser...")
    logger.info(f"PLAYWRIGHT HEADLESS MODE: {settings.HEADLESS}")
    browser = await p.chromium.launch(
        headless=settings.HEADLESS,
        args=["--start-maximized"]
    )
    
    # Create a browser context with permissions for microphone
    logger.info("Initializing browser context with HTTPS certificate bypass enabled...")
    context = await browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        permissions=['microphone'],
        ignore_https_errors=True,
        timezone_id="Asia/Kolkata",
        locale="en-IN"
    )
    
    page = await context.new_page()
    page.set_default_timeout(settings.BROWSER_TIMEOUT)
    page.set_default_navigation_timeout(settings.NAVIGATION_TIMEOUT)
    
    logger.info(
        f"PLAYWRIGHT TIMEZONE: {await page.evaluate('Intl.DateTimeFormat().resolvedOptions().timeZone')}"
    )
    logger.info(
        f"PLAYWRIGHT DATE: {await page.evaluate('new Date().toString()')}"
    )
    
    return browser, context, page

async def check_lecture_completion(page) -> bool:
    """
    Scans the page and its sub-frames to detect if the BigBlueButton lecture has completed.
    Returns True if completion is confirmed, False otherwise.
    """
    if not page or page.is_closed():
        return True
        
    try:
        main_url = page.url.lower()
        
        # 1. Text signatures representing termination
        text_signatures = [
            "meeting has ended",
            "session ended",
            "you have been logged out",
            "conference has ended",
            "disconnected",
            "meeting ended",
            "class completed",
            "class has ended",
            "class ended",
            "thank you"
        ]
        
        # 2. Check main page URL and body text
        if "/html5client/" not in main_url and "myclass.lpu.in" in main_url:
            # We navigated away from the BBB URL but are still on LPU domains (meaning we were redirected back)
            logger.info("Lecture Completion: Main page URL has redirected away from BigBlueButton (/html5client/).")
            return True
            
        try:
            main_text = (await page.locator("body").inner_text()).lower()
            if any(sig in main_text for sig in text_signatures):
                logger.info("Lecture Completion: Direct completion signature found in main page text.")
                return True
        except:
            pass
            
        # 3. Check all active frames
        has_bbb_frame = False
        has_core_containers = False
        bbb_frame_completed = False
        
        for frame in page.frames:
            url = (frame.url or "").lower()
            if "/html5client/" in url:
                has_bbb_frame = True
                
                # Check presence of BBB core components in this frame
                try:
                    app_exists = await frame.locator("#app").count() > 0
                    pres_exists = await frame.locator("#presentation-container").count() > 0
                    users_exists = await frame.locator('[aria-label="Users list"]').count() > 0
                    
                    if app_exists or pres_exists or users_exists:
                        has_core_containers = True
                except:
                    pass
                    
            # Check frame text for termination signals
            try:
                frame_text = (await frame.locator("body").inner_text()).lower()
                if any(sig in frame_text for sig in text_signatures):
                    logger.info("Lecture Completion: Direct completion signature found inside frame text.")
                    bbb_frame_completed = True
                    break
            except:
                pass
                
        if bbb_frame_completed:
            return True
            
        # 4. Enforce BBB frame presence & container checks
        # If we successfully joined previously, but now no frame has '/html5client/' in its URL
        if not has_bbb_frame:
            logger.info("Lecture Completion: Meeting iframe containing '/html5client/' is no longer present.")
            return True
            
        # If the frame exists but all BBB core elements (#app, #presentation-container, users list) have disappeared
        if not has_core_containers:
            logger.info("Lecture Completion: All BBB core containers (#app, #presentation-container, users list) have disappeared.")
            return True
            
    except Exception as e:
        logger.debug(f"Error checking lecture completion: {e}")
        
    return False

async def run_ingestion_core(
    session_state,
    p,
    username: Optional[str] = None,
    password: Optional[str] = None,
    user_id: Optional[Union[int, str]] = None
):
    extractor = None
    try:
        # Phase 1: Login
        from .login import perform_login
        await perform_login(session_state["page"], username=username, password=password, user_id=user_id)

        # Phase 2: Joining Workflow
        from .joiner import join_class_pipeline
        joined = await join_class_pipeline(session_state["page"], user_id=user_id)

        if joined:
            logger.info("Successfully joined the class session.")
            logger.info("Session is now ACTIVE. Monitoring lecture state...")

            from .watchdog import check_session_health, attempt_recovery

            extractor = LectureExtractor(user_id=user_id)
            rejoin_attempts = 0
            start_time = asyncio.get_event_loop().time()

            while True:
                # Active session check every 30 seconds
                await asyncio.sleep(30)

                # 1. Passive text extraction
                await extractor.extract_content(session_state["page"])

                # 2. Check for lecture-ended state
                lecture_ended = await check_lecture_completion(session_state["page"])
                if lecture_ended:
                    logger.info("Lecture completion detected on-screen. Initiating graceful class-end sequence.")
                    # Emit final lifecycle metrics
                    elapsed_seconds = int(asyncio.get_event_loop().time() - start_time)
                    final_metrics = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_seconds": elapsed_seconds,
                        "status": "COMPLETED",
                        "rejoin_attempts": rejoin_attempts,
                        "metrics": {
                            "buffer_lines": extractor.get_buffer_size(),
                            "iframe_count": len(session_state["page"].frames)
                        }
                    }
                    logger.info(f"LIFECYCLE_COMPLETED_METRICS: {json.dumps(final_metrics)}")
                    break

                # 3. Verify session health
                is_healthy = await check_session_health(session_state["page"], session_state["browser"])

                # 4. Emit structured ACTIVE session metrics logs
                elapsed_seconds = int(asyncio.get_event_loop().time() - start_time)
                metrics = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "elapsed_seconds": elapsed_seconds,
                    "status": "HEALTHY" if is_healthy else "UNHEALTHY",
                    "rejoin_attempts": rejoin_attempts,
                    "metrics": {
                        "buffer_lines": extractor.get_buffer_size(),
                        "iframe_count": len(session_state["page"].frames)
                    }
                }
                logger.info(f"ACTIVE_SESSION_METRICS: {json.dumps(metrics)}")

                if not is_healthy:
                    logger.warning("Session health check failed! Activating watchdog recovery process...")
                    recovery_result = await attempt_recovery(
                        session_state["page"], 
                        session_state["browser"], 
                        session_state["context"], 
                        p,
                        username=username,
                        password=password,
                        user_id=user_id
                    )
                    if recovery_result:
                        session_state["page"], session_state["browser"], session_state["context"] = recovery_result
                        rejoin_attempts += 1
                        logger.info(f"Session successfully recovered by watchdog. Total rejoins: {rejoin_attempts}/{settings.MAX_REJOIN_ATTEMPTS}")

                        if rejoin_attempts > settings.MAX_REJOIN_ATTEMPTS:
                            logger.error("Too many watchdog recovery attempts. Bailing out to prevent infinite rejoin loop.")
                            raise Exception("Runaway watchdog loop protection triggered.")
                    else:
                        logger.error("Session recovery watchdog failed all attempts. Terminating session.")
                        raise Exception("Session health recovery failed.")
                else:
                    logger.info("Worker Status: HEALTHY | Session: ACTIVE (In Lecture)")
        else:
            logger.info("No class was joined. Exiting gracefully to end the session.")

    except asyncio.CancelledError:
        logger.info("Shutdown signal received. Closing session gracefully...")
    except Exception as e:
        logger.error(f"Execution error: {str(e)}", exc_info=True)
        try:
            if user_id is not None:
                screenshot_path = get_user_screenshot_path(user_id, "error_screenshot.png")
            else:
                screenshot_path = os.path.join(settings.SCREENSHOTS_DIR, "error_screenshot.png")
            await session_state["page"].screenshot(path=screenshot_path)
            logger.info(f"Error screenshot saved to {screenshot_path}.")
        except Exception as ss_err:
            logger.error(f"Failed to capture screenshot: {str(ss_err)}")
    finally:
        # Safe trigger AI summarization with strict failure isolation
        if extractor and extractor.get_buffer_size() > 0:
            logger.info("Executing final lecture content summarization during graceful worker shutdown...")
            try:
                raw_content = extractor.get_raw_text()
                from .gemini_service import summarize_lecture
                summarize_lecture(raw_content, username=username, user_id=int(user_id) if user_id else None)
            except Exception as ai_err:
                logger.warning(f"Failed to execute AI summarization pipeline during shutdown: {ai_err}")

async def process_class(
    user_id: int,
    lpu_username: str,
    lpu_password: str,
    class_metadata: dict
) -> dict:
    """
    Targeted orchestration function for processing an explicitly specified user's class session.
    Flow: login -> navigation/join -> extraction -> summary -> persistence.
    Provisions an independent, isolated BrowserContext and Page for strict session isolation.
    Persists artifacts authoritatively under user-scoped paths (screenshots/{user_id}/, logs/{user_id}/,
    user_timetables, and class_attendance_records).
    Plaintext credentials flow strictly in-memory and are never logged, printed, or returned.
    """
    subject = class_metadata.get("subject_code", "Lecture")
    join_url = class_metadata.get("join_url")
    subject_name = class_metadata.get("subject_name", "Class Lecture")
    instructor = class_metadata.get("faculty", "N/A")
    end_str = class_metadata.get("end_time")

    from datetime import datetime, timedelta
    from .utils import get_now_ist
    from .session_status import update_session_status
    from .extractor import LectureExtractor
    from .watchdog import check_session_health, attempt_recovery
    from .gemini_service import summarize_lecture

    try:
        end_dt = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        end_dt = get_now_ist() + timedelta(hours=1)

    logger.info(f"Targeted Ingestion: Starting process_class for user_id={user_id}, subject='{subject}'")

    # Authoritatively set user session status
    update_session_status(
        "CONNECTING",
        subject=subject,
        title=subject_name,
        instructor=instructor,
        user_id=user_id
    )

    result = {
        "user_id": user_id,
        "subject": subject,
        "status": "FAILED",
        "details": "",
        "summary_id": None
    }

    async with async_playwright() as p:
        browser, context, page = await create_browser_and_page(p)
        try:
            # 1. Capture before_join screenshot in user-scoped partition
            timestamp = get_now_ist().strftime("%Y-%m-%d_%H-%M-%S")
            before_ss = get_user_screenshot_path(user_id, f"before_join_{timestamp}.png")
            try:
                await page.screenshot(path=before_ss, full_page=True)
                logger.info(f"Targeted Ingestion: Saved before_join screenshot to {before_ss}")
            except Exception as ss_e:
                logger.warning(f"Targeted Ingestion: Before-join screenshot failed: {ss_e}")

            # 2. Perform Login with explicit user credentials
            from .login import perform_login
            if not settings.DEBUG_MODE:
                await perform_login(page, username=lpu_username, password=lpu_password, user_id=user_id)
                logger.info(f"Targeted Ingestion: User {user_id} login complete.")
            else:
                logger.info("Targeted Ingestion: DEBUG_MODE active: bypassing real login flow.")

            # 3. Join Class Pipeline
            from .joiner import join_class_pipeline
            join_attempts_limit = 3 if not settings.DEBUG_MODE else 1
            join_failure_cooldown = 180 if not settings.DEBUG_MODE else 1
            join_success = False

            for attempt in range(1, join_attempts_limit + 1):
                logger.info(f"Targeted Ingestion: Join attempt {attempt}/{join_attempts_limit} for user {user_id}...")
                try:
                    join_success = await join_class_pipeline(page, join_url=join_url, user_id=user_id)
                    if join_success:
                        break
                except Exception as join_err:
                    logger.error(f"Targeted Ingestion: Join attempt {attempt} failed: {join_err}")
                    if attempt < join_attempts_limit:
                        await asyncio.sleep(join_failure_cooldown)

            if join_success == "FACULTY_NOT_STARTED":
                logger.info(f"Targeted Ingestion: Faculty did not start class for user {user_id}.")
                result["status"] = "FACULTY_NOT_STARTED"
                result["details"] = "Faculty did not start the class."
                return result

            elif not join_success:
                logger.error(f"Targeted Ingestion: Failed to join class for user {user_id}.")
                timestamp = get_now_ist().strftime("%Y-%m-%d_%H-%M-%S")
                ss_filename = f"failure_{timestamp}.png"
                ss_path = get_user_screenshot_path(user_id, ss_filename)
                try:
                    if page and not page.is_closed():
                        await page.screenshot(path=ss_path, full_page=True)
                        update_session_status(
                            status="FAILED",
                            screenshot=ss_filename,
                            disconnect_time=get_now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                            subject=subject,
                            title=subject_name,
                            instructor=instructor,
                            user_id=user_id
                        )
                except Exception as ss_err:
                    logger.error(f"Targeted Ingestion: Failed to capture failure screenshot: {ss_err}")

                result["status"] = "FAILED"
                result["details"] = "Failed to join active lecture"
                return result

            # 4. Connected State
            logger.info(f"Targeted Ingestion: Successfully joined active session for user {user_id}.")
            await asyncio.sleep(5)

            timestamp = get_now_ist().strftime("%Y-%m-%d_%H-%M-%S")
            conn_ss = f"connected_{timestamp}.png"
            conn_path = get_user_screenshot_path(user_id, conn_ss)
            try:
                await page.screenshot(path=conn_path, full_page=True)
                update_session_status(
                    status="CONNECTED",
                    screenshot=conn_ss,
                    join_time=get_now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                    subject=subject,
                    title=subject_name,
                    instructor=instructor,
                    user_id=user_id
                )
            except Exception as ss_err:
                logger.error(f"Targeted Ingestion: Failed to capture connected screenshot: {ss_err}")

            # 5. Extraction & Monitoring Loop
            extractor = LectureExtractor(user_id=user_id)
            rejoin_attempts = 0

            while True:
                await asyncio.sleep(30)
                await extractor.extract_content(page)

                now_time = get_now_ist()
                grace_buffer = timedelta(minutes=2)
                cutoff_time = end_dt + grace_buffer

                if now_time >= cutoff_time:
                    logger.info(f"Targeted Ingestion: Scheduled lecture end cutoff reached for user {user_id}.")
                    break

                if await check_lecture_completion(page):
                    logger.info(f"Targeted Ingestion: Lecture completion detected on-screen for user {user_id}.")
                    break

                if not await check_session_health(page, browser):
                    logger.warning(f"Targeted Ingestion: Watchdog health failure for user {user_id}. Recovering...")
                    recovery = await attempt_recovery(
                        page, browser, context, p,
                        join_url=join_url,
                        username=lpu_username,
                        password=lpu_password,
                        user_id=user_id
                    )
                    if recovery:
                        page, browser, context = recovery
                        rejoin_attempts += 1
                    else:
                        logger.error(f"Targeted Ingestion: Watchdog recovery failed for user {user_id}.")
                        break

            # 6. Disconnect screenshot & attendance persistence
            timestamp = get_now_ist().strftime("%Y-%m-%d_%H-%M-%S")
            disc_ss = f"disconnect_{timestamp}.png"
            disc_path = get_user_screenshot_path(user_id, disc_ss)
            try:
                if page and not page.is_closed():
                    await page.screenshot(path=disc_path, full_page=True)
            except Exception:
                pass

            update_session_status(
                status="DISCONNECTED",
                screenshot=disc_ss,
                disconnect_time=get_now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                subject=subject,
                title=subject_name,
                instructor=instructor,
                user_id=user_id
            )

            # 7. Summarization & user-scoped DB persistence
            summary_id = None
            if extractor.get_buffer_size() > 0:
                logger.info(f"Targeted Ingestion: Triggering Gemini AI summarization for user {user_id}...")
                try:
                    summary_dict = summarize_lecture(
                        extractor.get_raw_text(),
                        subject=subject,
                        username=lpu_username,
                        user_id=user_id
                    )
                    summary_id = summary_dict.get("id")
                except Exception as sum_err:
                    logger.error(f"Targeted Ingestion: Summarization failed for user {user_id}: {sum_err}")

            result["status"] = "COMPLETED"
            result["details"] = "Lecture session ingested and processed successfully."
            result["summary_id"] = summary_id
            return result

        except Exception as e:
            logger.error(f"Targeted Ingestion: Unhandled error processing class for user {user_id}: {e}")
            result["status"] = "FAILED"
            result["details"] = str(e)
            return result
        finally:
            try:
                if page and not page.is_closed():
                    await page.close()
                if context:
                    await context.close()
                if browser and browser.is_connected():
                    await browser.close()
            except Exception as close_err:
                logger.debug(f"Targeted Ingestion: Cleanup error: {close_err}")


async def process_class_direct(active_class: dict, classes: list) -> bool:
    """
    Legacy direct-processing fallback for headed browser session.
    Preserved for backward compatibility when Redis queue is unavailable or bypassed.
    """
    subject = active_class.get("subject_code", "Lecture")
    end_str = active_class.get("end_time")
    try:
        end_dt = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")
    except Exception as parse_err:
        logger.error(f"Failed to parse active class end_time '{end_str}': {parse_err}")
        end_dt = datetime.now() + timedelta(hours=1)
    logger.info(f"Direct Fallback: Active class found: {subject} (Ends: {end_str}). Triggering targeted headed join...")
    update_ingestion_status("processing", details=f"Launching targeted browser to join class: {subject}", subject=subject)

    try:
        from .session_status import update_session_status
        update_session_status(
            "CONNECTING",
            subject=active_class.get("subject_code"),
            title=active_class.get("subject_name"),
            instructor=active_class.get("faculty")
        )
    except Exception as status_err:
        logger.error(f"Failed to update session status to CONNECTING: {status_err}")

    join_attempts_limit = 3 if not settings.DEBUG_MODE else 1
    join_failure_cooldown = 180 if not settings.DEBUG_MODE else 1

    async with async_playwright() as p:
        logger.info("[JOIN_PHASE] Launching browser...")
        browser, context, page = await create_browser_and_page(p)

        try:
            try:
                timestamp = get_now_ist().strftime("%Y-%m-%d_%H-%M-%S")
                screenshot_path = get_screenshot_path(f"before_join_{timestamp}.png")
                await page.screenshot(path=screenshot_path, full_page=True)
                logger.info(f"Screenshot saved: {screenshot_path}")
            except Exception as ss_e:
                logger.error(f"Screenshot failed at before_join: {ss_e}")

            if not settings.DEBUG_MODE:
                await perform_login(page)
                logger.info("[JOIN_PHASE] Login successful")
            else:
                logger.info("[JOIN_PHASE] DEBUG_MODE active: bypassing real login flow.")

            join_success = False
            for attempt in range(1, join_attempts_limit + 1):
                logger.info(f"Direct Fallback: Attempting to join {subject} (Attempt {attempt}/{join_attempts_limit})...")
                try:
                    from .joiner import join_class_pipeline
                    join_url = active_class.get("join_url")
                    join_success = await join_class_pipeline(page, join_url=join_url)
                    if join_success:
                        break
                except Exception as join_err:
                    logger.error(f"Direct Fallback: Join attempt {attempt} failed: {join_err}")
                    if attempt < join_attempts_limit:
                        await asyncio.sleep(join_failure_cooldown)

            if join_success == "FACULTY_NOT_STARTED":
                logger.info("Direct Fallback: Faculty did not start the class. Completing lifecycle immediately.")
                update_ingestion_status("idle", details="Faculty did not start the class.")
                active_class["status"] = "COMPLETED"
                timetable_cache.set_timetable(classes)
                return True

            elif not join_success:
                logger.error("Direct Fallback: Failed to join class after maximum attempts.")
                update_ingestion_status("failed", error="Failed to join active lecture")
                timestamp = get_now_ist().strftime("%Y-%m-%d_%H-%M-%S")
                ss_filename = f"failure_{timestamp}.png"
                ss_path = get_screenshot_path(ss_filename)
                try:
                    if page and not page.is_closed():
                        await page.screenshot(path=ss_path, full_page=True)
                        logger.info(f"Join Failed: Saved validation screenshot to {ss_path}")
                        from .session_status import update_session_status
                        update_session_status(
                            status="FAILED",
                            screenshot=ss_filename,
                            disconnect_time=get_now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                            subject=subject
                        )
                except Exception as ss_e:
                    logger.error(f"Join Failed: Failed to capture validation screenshot: {ss_e}")
                return False

            else:
                logger.info("Direct Fallback: Successfully joined active session. Monitoring active stream...")
                update_ingestion_status("processing", details="Monitoring active lecture stream", subject=subject)
                await asyncio.sleep(5)
                timestamp = get_now_ist().strftime("%Y-%m-%d_%H-%M-%S")
                ss_filename = f"connected_{timestamp}.png"
                ss_path = get_screenshot_path(ss_filename)
                try:
                    await page.screenshot(path=ss_path, full_page=True)
                    logger.info(f"Join Success (Connected): Saved validation screenshot to {ss_path}")
                    from .session_status import update_session_status
                    update_session_status(
                        status="CONNECTED",
                        screenshot=ss_filename,
                        join_time=get_now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                        subject=subject
                    )
                except Exception as ss_e:
                    logger.error(f"Join Success: Failed to capture validation screenshot: {ss_e}")

                extractor = LectureExtractor()
                rejoin_attempts = 0
                while True:
                    try:
                        from .redis_service import redis_service
                        redis_service.set_scheduler_heartbeat()
                    except Exception:
                        pass
                    await asyncio.sleep(30)
                    await extractor.extract_content(page)
                    now_time = get_now_ist()
                    grace_buffer = timedelta(minutes=2)
                    cutoff_time = end_dt + grace_buffer
                    if now_time >= cutoff_time:
                        logger.info(f"[SESSION_END] Scheduled lecture end reached")
                        break
                    if await check_lecture_completion(page):
                        logger.info("Direct Fallback: Lecture completion detected.")
                        break
                    from .watchdog import check_session_health, attempt_recovery
                    if not await check_session_health(page, browser):
                        logger.warning("Direct Fallback: Watchdog health failure. Recovering...")
                        recovery = await attempt_recovery(page, browser, context, p, join_url=join_url)
                        if recovery:
                            page, browser, context = recovery
                            rejoin_attempts += 1
                        else:
                            logger.error("Direct Fallback: Watchdog recovery failed.")
                            break

                if extractor.get_buffer_size() > 0:
                    logger.info("Direct Fallback: Triggering Gemini AI summarization pipeline...")
                    update_ingestion_status("processing", details="Generating AI lecture summary", subject=subject)
                    try:
                        from .gemini_service import summarize_lecture
                        summarize_lecture(extractor.get_raw_text(), subject=subject)
                        update_ingestion_status("completed", details="AI summary generated and persisted successfully.")
                    except Exception as sum_e:
                        logger.error(f"Direct Fallback: Summarization failed: {sum_e}")

                active_class["status"] = "COMPLETED"
                timetable_cache.set_timetable(classes)
                return True

        except Exception as e:
            logger.error(f"Direct Fallback: targeted class session execution failed: {e}")
            update_ingestion_status("failed", error=str(e))
            return False
        finally:
            try:
                from .session_status import get_session_status, update_session_status
                current_status = get_session_status()
                if current_status.get("status") == "CONNECTED":
                    timestamp = get_now_ist().strftime("%Y-%m-%d_%H-%M-%S")
                    ss_filename = f"disconnect_{timestamp}.png"
                    ss_path = get_screenshot_path(ss_filename)
                    try:
                        if page and not page.is_closed():
                            await page.screenshot(path=ss_path, full_page=True)
                    except Exception:
                        pass
                    update_session_status(
                        status="DISCONNECTED",
                        screenshot=ss_filename,
                        disconnect_time=get_now_ist().strftime("%Y-%m-%d %H:%M:%S")
                    )
            except Exception:
                pass
            await context.close()
            await browser.close()
            logger.info("Direct Fallback: Targeted browser closed.")


async def run_ingestion():
    logger.info("Starting CloudNote Continuous Ingestion Worker...")
    
    # Reset persistent session status to clean IDLE state on application startup
    try:
        from .session_status import reset_session_status
        reset_session_status()
    except Exception as reset_err:
        logger.error(f"Failed to reset session status on worker startup: {reset_err}")
        
    from .database import update_ingestion_status
    from .timetable_cache import timetable_cache
    from .timetable_fetcher import fetch_timetable_data
    from .timetable_parser import parse_class_times
    from .login import perform_login
    
    if not settings.DEBUG_MODE:
        if any(c.get("subject_code", "").startswith("DEBUG_") for c in timetable_cache.classes):
            logger.info("Found debug data in timetable cache. Wiping cache for REAL MODE.")
            timetable_cache.mark_stale()

    # Step 3C: Launch background WorkerConsumer task if Redis is configured
    consumer = None
    consumer_task = None
    if settings.REDIS_URL and not settings.DEMO_MODE:
        try:
            from .worker_consumer import WorkerConsumer
            consumer = WorkerConsumer()
            consumer_task = asyncio.create_task(consumer.run())
            logger.info("Unified Loop: Started background WorkerConsumer task for Redis stream jobs.")
        except Exception as c_err:
            logger.warning(f"Unified Loop: Could not start background WorkerConsumer: {c_err}")

    join_attempts_limit = 3 if not settings.DEBUG_MODE else 1
    join_failure_cooldown = 180 if not settings.DEBUG_MODE else 1

    try:
        while True:
            # Resilient scheduler heartbeat update (Sidecar Integration)
            try:
                from .redis_service import redis_service
                redis_service.set_scheduler_heartbeat()
            except Exception:
                pass
            # Check cache state
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
        
            # If cache is missing, stale, or sync is manually requested, run a Headless Unified Fetch
            if not cache_valid or sync_requested:
                logger.info("Unified Loop: Timetable cache is stale, missing, or sync was requested. Performing headless sync...")
                update_ingestion_status("processing", details="Performing headless timetable sync session")
            
                # Clear manual sync flag immediately so we don't loop on it
                timetable_cache.set_sync_request(False)
            
                async with async_playwright() as p:
                    logger.info("Unified Loop: Launching headless browser for sync session...")
                    logger.info(f"PLAYWRIGHT HEADLESS MODE: {settings.HEADLESS}")
                    browser = await p.chromium.launch(
                        headless=settings.HEADLESS,
                        args=["--start-maximized"]
                    )
                    context = await browser.new_context(
                        ignore_https_errors=True,
                        viewport={'width': 1920, 'height': 1080},
                        timezone_id="Asia/Kolkata",
                        locale="en-IN"
                    )
                    page = await context.new_page()
                    page.set_default_timeout(settings.BROWSER_TIMEOUT)
                
                    logger.info(
                        f"PLAYWRIGHT TIMEZONE: {await page.evaluate('Intl.DateTimeFormat().resolvedOptions().timeZone')}"
                    )
                    logger.info(
                        f"PLAYWRIGHT DATE: {await page.evaluate('new Date().toString()')}"
                    )
                
                    try:
                        await perform_login(page)
                        fetched_classes = await fetch_timetable_data(page)
                        timetable_cache.set_timetable(fetched_classes)
                        classes = fetched_classes
                        update_ingestion_status("idle", details=f"Timetable sync complete. Found {len(classes)} classes for today.")
                    except Exception as sync_err:
                        logger.error(f"Unified Loop: Headless sync session failed: {sync_err}")
                        update_ingestion_status("failed", error=f"Timetable sync failed: {str(sync_err)}")
                    finally:
                        await context.close()
                        await browser.close()
                        logger.info("Unified Loop: Headless browser session closed successfully.")
                    
            # Process timetable to compute next execution window
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
            
                logger.info(f"DEBUG CURRENT IST TIME: {now}")
                logger.info(f"DEBUG CLASS START: {start_dt}")
                logger.info(f"DEBUG CLASS END: {end_dt}")
                logger.info(f"DEBUG CLASS STATUS: {c}")
            
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
            
                # Case 2: Upcoming class today or tomorrow
                elif start_dt > now:
                    seconds_to_start = (start_dt - now).total_seconds()
                    if min_seconds_to_start is None or seconds_to_start < min_seconds_to_start:
                        min_seconds_to_start = seconds_to_start
                        next_upcoming_class = c
                    
            # Decision Orchestrator
            if active_class:
                if settings.DEMO_MODE:
                    logger.info("DEMO_MODE active: Waiting 15 seconds for backend to stabilize inside Docker network...")
                    await asyncio.sleep(15)

                if settings.DEBUG_MODE:
                    logger.info(f"DEBUG BACKEND URL: {settings.BACKEND_INTERNAL_URL}")
                    try:
                        import aiohttp
                        async with aiohttp.ClientSession() as session:
                            test_url = f"{settings.BACKEND_INTERNAL_URL}/debug-meeting"
                            async with session.get(test_url, timeout=10) as resp:
                                if resp.status == 200:
                                    logger.info(f"Backend connectivity verification SUCCESS against {test_url}")
                                else:
                                    logger.error(f"Backend connectivity verification FAILED. Status: {resp.status}")
                                    if settings.DEMO_MODE:
                                        logger.info("=== DEMO_MODE ABORTING DUE TO BACKEND UNAVAILABILITY ===")
                                        while True:
                                            await asyncio.sleep(3600)
                    except Exception as conn_err:
                        logger.error(f"Backend connectivity verification FAILED with exception: {conn_err}")
                        if settings.DEMO_MODE:
                            logger.info("=== DEMO_MODE ABORTING DUE TO BACKEND UNAVAILABILITY ===")
                            while True:
                                await asyncio.sleep(3600)

                # Active class found: Evaluate queue-based execution pathway or direct fallback
                subject = active_class.get("subject_code", "Lecture")
                logger.info(f"Unified Loop: Active class found: {subject}. Evaluating execution pathway...")

                enqueued_to_redis = False
                if settings.REDIS_URL:
                    try:
                        from .job_queue import schedule_class_to_queue
                        job, enqueued = schedule_class_to_queue(active_class)
                        if enqueued and job:
                            enqueued_to_redis = True
                            logger.info(f"Unified Loop: Successfully enqueued class job {job.job_id} ({job.idempotency_key}) to Redis stream.")
                            update_ingestion_status("processing", details=f"Class job queued for worker: {subject}", subject=subject)
                            active_class["status"] = "QUEUED"
                            timetable_cache.set_timetable(classes)
                        elif not enqueued:
                            enqueued_to_redis = True
                            logger.info(f"Unified Loop: Class job for {subject} already enqueued/deduplicated via idempotency key.")
                            active_class["status"] = "QUEUED"
                            timetable_cache.set_timetable(classes)
                    except Exception as q_err:
                        logger.warning(f"Unified Loop: Failed to enqueue class job to Redis: {q_err}. Falling back to direct execution...")
                        enqueued_to_redis = False

                # Fallback path: Direct execution if Redis is unavailable or enqueue failed
                if not enqueued_to_redis:
                    logger.info(f"Unified Loop: Using direct processing fallback for {subject}...")
                    await process_class_direct(active_class, classes)

                if settings.DEMO_MODE:
                    logger.info("=== DEMO_MODE SINGLE RUN COMPLETE, GOING IDLE ===")
                    while True:
                        await asyncio.sleep(3600)
                elif settings.DEBUG_MODE:
                    logger.info("=== DEBUG_MODE SINGLE RUN COMPLETE, BREAKING LOOP ===")
                    break

            elif next_upcoming_class:
                # We have an upcoming class. Determine smart sleep duration (until 5 minutes before start)
                start_str = next_upcoming_class.get("start_time")
                start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
            
                # Phase 3 — Trace Sleep Calculation Temporary Instrumenting
                current_time = get_now_ist()
                seconds_remaining = (start_dt - current_time).total_seconds()
                sleep_duration = max(seconds_remaining - 300, 30)
                wake_time = start_dt - timedelta(minutes=5)
            
                logger.info("=== SCHEDULER DEBUG SLEEP TRACE ===")
                logger.info(f"  current_time: {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
                logger.info(f"  class_start:  {start_str}")
                logger.info(f"  wake_time:    {wake_time.strftime('%Y-%m-%d %H:%M:%S')}")
                logger.info(f"  computed_sleep_seconds: {sleep_duration} seconds")
                logger.info("===================================")
            
                logger.info(f"Unified Loop: Next upcoming class {next_upcoming_class.get('subject_code')} starts at {start_str}.")
                logger.info(f"Unified Loop: Entering smart sleep for {sleep_duration} seconds (waking up 5 minutes before start)...")
                update_ingestion_status("idle", details=f"Next class: {next_upcoming_class.get('subject_code')} at {start_dt.strftime('%I:%M %p')}")
            
                # Execute smart sleep
                target_time = asyncio.get_event_loop().time() + sleep_duration
                while asyncio.get_event_loop().time() < target_time:
                    # Resilient scheduler heartbeat update (Sidecar Integration)
                    try:
                        from .redis_service import redis_service
                        redis_service.set_scheduler_heartbeat()
                    except Exception:
                        pass
                    # Check for calendar date rollovers or manual sync requests
                    if timetable_cache.is_sync_requested() or not timetable_cache.is_valid_for_today():
                        logger.info("Unified Loop: Sleep interrupted by sync request or date rollover. Waking up!")
                        break
                    await asyncio.sleep(10)
                
            else:
                if len(classes) == 0:
                    logger.info("Unified Loop: No classes scheduled for today.")
                    status_details = "No classes scheduled for today. Resting until tomorrow."
                else:
                    logger.info("Unified Loop: All scheduled classes for today have been completed successfully.")
                    status_details = "All classes completed for today. Resting until tomorrow."
                current_ist = get_now_ist()
                tomorrow_8am = datetime.combine(current_ist.date() + timedelta(days=1), time(8, 0))
                sleep_duration = (tomorrow_8am - current_ist).total_seconds()
            
                logger.info(f"Unified Loop: Entering smart sleep until tomorrow 8:00 AM ({sleep_duration} seconds) for new day rollover...")
                update_ingestion_status("idle", details=status_details)
            
                target_time = asyncio.get_event_loop().time() + sleep_duration
                while asyncio.get_event_loop().time() < target_time:
                    # Resilient scheduler heartbeat update (Sidecar Integration)
                    try:
                        from .redis_service import redis_service
                        redis_service.set_scheduler_heartbeat()
                    except Exception:
                        pass
                    if timetable_cache.is_sync_requested() or not timetable_cache.is_valid_for_today():
                        logger.info("Unified Loop: Sleep interrupted by sync request or date rollover. Waking up!")
                        break
                    await asyncio.sleep(10)

    finally:
        if consumer:
            try:
                consumer.stop()
            except Exception:
                pass
        if consumer_task:
            try:
                await asyncio.wait_for(consumer_task, timeout=5.0)
            except Exception:
                pass

if __name__ == "__main__":
    asyncio.run(run_ingestion())
