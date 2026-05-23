import asyncio
import os
import json
from playwright.async_api import async_playwright
from .config import settings
from .logger import logger
from datetime import datetime, timezone, timedelta, date, time
import sys
from .extractor import LectureExtractor

async def create_browser_and_page(p):
    """Launches browser, context, and page with settings-defined defaults and microphone permissions."""
    logger.info("Launching browser...")
    browser = await p.chromium.launch(
        headless=settings.HEADLESS,
        args=[
            "--use-fake-ui-for-media-stream", 
            "--use-fake-device-for-media-stream",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu"
        ]
    )
    
    # Create a browser context with permissions for microphone
    logger.info("Initializing browser context with HTTPS certificate bypass enabled...")
    context = await browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        permissions=['microphone'],
        ignore_https_errors=True
    )
    
    page = await context.new_page()
    page.set_default_timeout(settings.BROWSER_TIMEOUT)
    page.set_default_navigation_timeout(settings.NAVIGATION_TIMEOUT)
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

async def run_ingestion_core(session_state, p):
    extractor = None
    try:
        # Phase 1: Login
        from .login import perform_login
        await perform_login(session_state["page"])
        
        # Phase 2: Joining Workflow
        from .joiner import join_class_pipeline
        joined = await join_class_pipeline(session_state["page"])
        
        if joined:
            logger.info("Successfully joined the class session.")
            logger.info("Session is now ACTIVE. Monitoring lecture state...")
            
            from .watchdog import check_session_health, attempt_recovery
            
            extractor = LectureExtractor()
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
                        p
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
                summarize_lecture(raw_content)
            except Exception as ai_err:
                logger.warning(f"Failed to execute AI summarization pipeline during shutdown: {ai_err}")

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
    
    join_attempts_limit = 3
    join_failure_cooldown = 180  # 3 minutes cooldown if join/login fails
    
    while True:
        # Check cache state
        classes = timetable_cache.get_timetable()
        sync_requested = timetable_cache.is_sync_requested()
        
        # If cache is missing, stale, or sync is manually requested, run a Headless Unified Fetch
        if not classes or sync_requested:
            logger.info("Unified Loop: Timetable cache is stale, missing, or sync was requested. Performing headless sync...")
            update_ingestion_status("processing", details="Performing headless timetable sync session")
            
            # Clear manual sync flag immediately so we don't loop on it
            timetable_cache.set_sync_request(False)
            
            async with async_playwright() as p:
                logger.info("Unified Loop: Launching headless browser for sync session...")
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                )
                context = await browser.new_context(
                    ignore_https_errors=True,
                    viewport={'width': 1920, 'height': 1080}
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
                    logger.error(f"Unified Loop: Headless sync session failed: {sync_err}")
                    update_ingestion_status("failed", error=f"Timetable sync failed: {str(sync_err)}")
                finally:
                    await context.close()
                    await browser.close()
                    logger.info("Unified Loop: Headless browser session closed successfully.")
                    
        # Process timetable to compute next execution window
        now = datetime.now()
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
            
            # Case 1: Active class (currently between start and end times)
            if start_dt <= now <= end_dt:
                # Skip if already completed (though normally handled by loop exit)
                if c.get("status") != "COMPLETED":
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
            # We have an active class! Launch browser and execute targeted join & monitor session
            subject = active_class.get("subject_code", "Lecture")
            end_str = active_class.get("end_time")
            try:
                end_dt = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")
            except Exception as parse_err:
                logger.error(f"Failed to parse active class end_time '{end_str}': {parse_err}")
                end_dt = datetime.now() + timedelta(hours=1)
            logger.info(f"Unified Loop: Active class found: {subject} (Ends: {end_str}). Triggering targeted headed join...")
            update_ingestion_status("processing", details=f"Launching targeted browser to join class: {subject}", subject=subject)
            
            # Transition session status to CONNECTING
            try:
                from .session_status import update_session_status
                update_session_status("CONNECTING")
            except Exception as status_err:
                logger.error(f"Failed to update session status to CONNECTING: {status_err}")
            
            async with async_playwright() as p:
                logger.info("[JOIN_PHASE] Launching browser...")
                browser, context, page = await create_browser_and_page(p)
                
                try:
                    await perform_login(page)
                    logger.info("[JOIN_PHASE] Login successful")
                    
                    # Target join success loop
                    join_success = False
                    for attempt in range(1, join_attempts_limit + 1):
                        logger.info(f"Unified Loop: Attempting to join {subject} (Attempt {attempt}/{join_attempts_limit})...")
                        try:
                            from .joiner import join_class_pipeline
                            join_url = active_class.get("join_url")
                            join_success = await join_class_pipeline(page, join_url=join_url)
                            if join_success:
                                break
                        except Exception as join_err:
                            logger.error(f"Unified Loop: Join attempt {attempt} failed: {join_err}")
                            if attempt < join_attempts_limit:
                                await asyncio.sleep(join_failure_cooldown)
                                
                    if not join_success:
                        logger.error("Unified Loop: Failed to join class after maximum attempts.")
                        update_ingestion_status("failed", error="Failed to join active lecture")
                    else:
                        logger.info("Unified Loop: Successfully joined active session. Monitoring active stream...")
                        update_ingestion_status("processing", details="Monitoring active lecture stream", subject=subject)
                        
                        # Wait an extra 5 seconds for UI elements/layout to stabilize
                        await asyncio.sleep(5)
                        
                        # Capture join success screenshot
                        now_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
                        ss_filename = f"join_success_{now_str}.png"
                        ss_path = os.path.join(settings.SCREENSHOTS_DIR, ss_filename)
                        try:
                            await page.screenshot(path=ss_path)
                            logger.info(f"Join Success: Saved validation screenshot to {ss_path}")
                            logger.info("[JOIN_PHASE] Join success screenshot captured")
                            from .session_status import update_session_status
                            update_session_status(
                                status="CONNECTED",
                                screenshot=ss_filename,
                                join_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                subject=subject
                            )
                        except Exception as ss_e:
                            logger.error(f"Join Success: Failed to capture validation screenshot: {ss_e}")
                        
                        # Active monitoring and passive text extraction
                        extractor = LectureExtractor()
                        rejoin_attempts = 0
                        start_time = asyncio.get_event_loop().time()
                        
                        while True:
                            await asyncio.sleep(30)
                            await extractor.extract_content(page)
                            
                            # 1. Scheduled session cutoff logic (with configurable 2 minutes grace buffer)
                            now_time = datetime.now()
                            grace_buffer = timedelta(minutes=2)
                            cutoff_time = end_dt + grace_buffer
                            
                            if now_time >= cutoff_time:
                                logger.info(f"[SESSION_END] Scheduled lecture end reached (End: {end_dt.strftime('%Y-%m-%d %H:%M:%S')}, Cutoff: {cutoff_time.strftime('%Y-%m-%d %H:%M:%S')})")
                                logger.info("[SESSION_END] Initiating graceful shutdown")
                                break
                            
                            # End detection
                            if await check_lecture_completion(page):
                                logger.info("Unified Loop: Lecture completion detected.")
                                break
                                
                            # Watchdog health checks
                            from .watchdog import check_session_health, attempt_recovery
                            if not await check_session_health(page, browser):
                                logger.warning("Unified Loop: Watchdog health failure. Recovering...")
                                recovery = await attempt_recovery(page, browser, context, p, join_url=join_url)
                                if recovery:
                                    page, browser, context = recovery
                                    rejoin_attempts += 1
                                else:
                                    logger.error("Unified Loop: Watchdog recovery failed.")
                                    break
                                    
                        # Summarization & Persistence on session completion
                        if extractor.get_buffer_size() > 0:
                            logger.info("Unified Loop: Triggering Gemini AI summarization pipeline...")
                            update_ingestion_status("processing", details="Generating AI lecture summary", subject=subject)
                            try:
                                from .gemini_service import summarize_lecture
                                summarize_lecture(extractor.get_raw_text())
                                update_ingestion_status("completed", details="AI summary generated and persisted successfully.")
                            except Exception as sum_e:
                                logger.error(f"Unified Loop: Summarization failed: {sum_e}")
                        
                        # Set this class as completed in cache to avoid re-joining
                        active_class["status"] = "COMPLETED"
                        timetable_cache.set_timetable(classes)
                        
                except Exception as e:
                    logger.error(f"Unified Loop: targeted class session execution failed: {e}")
                    update_ingestion_status("failed", error=str(e))
                finally:
                    # Capture disconnect screenshot if we were previously connected
                    try:
                        from .session_status import get_session_status, update_session_status
                        current_status = get_session_status()
                        if current_status.get("status") == "CONNECTED":
                            now_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
                            ss_filename = f"disconnect_{now_str}.png"
                            ss_path = os.path.join(settings.SCREENSHOTS_DIR, ss_filename)
                            try:
                                if page and not page.is_closed():
                                    await page.screenshot(path=ss_path)
                                    logger.info(f"Disconnect Check: Captured validation screenshot at {ss_path}.")
                            except Exception as ss_e:
                                logger.error(f"Disconnect Check: Failed to capture validation screenshot: {ss_e}")
                            
                            update_session_status(
                                status="DISCONNECTED",
                                screenshot=ss_filename,
                                disconnect_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            )
                    except Exception as status_err:
                        logger.error(f"Failed to execute status monitoring update on disconnect: {status_err}")
                        
                    await context.close()
                    await browser.close()
                    logger.info("Unified Loop: Targeted browser closed, releasing system memory.")
                    logger.info("[SESSION_END] Browser context closed")
                    logger.info("[SESSION_END] Returning to scheduler standby")
                    
        elif next_upcoming_class:
            # We have an upcoming class. Determine smart sleep duration (until 5 minutes before start)
            start_str = next_upcoming_class.get("start_time")
            start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
            seconds_remaining = (start_dt - datetime.now()).total_seconds()
            
            # Buffer of 5 minutes (300 seconds)
            sleep_duration = max(seconds_remaining - 300, 30)
            
            logger.info(f"Unified Loop: Next upcoming class {next_upcoming_class.get('subject_code')} starts at {start_str}.")
            logger.info(f"Unified Loop: Entering smart sleep for {sleep_duration} seconds (waking up 5 minutes before start)...")
            update_ingestion_status("idle", details=f"Next class: {next_upcoming_class.get('subject_code')} at {start_dt.strftime('%I:%M %p')}")
            
            # Execute smart sleep
            target_time = asyncio.get_event_loop().time() + sleep_duration
            while asyncio.get_event_loop().time() < target_time:
                # Check for calendar date rollovers or manual sync requests
                if timetable_cache.is_sync_requested() or timetable_cache.get_timetable() == []:
                    logger.info("Unified Loop: Sleep interrupted by sync request or date rollover. Waking up!")
                    break
                await asyncio.sleep(10)
                
        else:
            # All scheduled classes today are finished. Sleep until the next day's refresh window (e.g. 8:00 AM)
            logger.info("Unified Loop: All scheduled classes for today have been completed successfully.")
            tomorrow_8am = datetime.combine(date.today() + timedelta(days=1), time(8, 0))
            sleep_duration = (tomorrow_8am - datetime.now()).total_seconds()
            
            logger.info(f"Unified Loop: Entering smart sleep until tomorrow 8:00 AM ({sleep_duration} seconds) for new day rollover...")
            update_ingestion_status("idle", details="All classes completed for today. Resting until tomorrow.")
            
            target_time = asyncio.get_event_loop().time() + sleep_duration
            while asyncio.get_event_loop().time() < target_time:
                if timetable_cache.is_sync_requested() or timetable_cache.get_timetable() == []:
                    logger.info("Unified Loop: Sleep interrupted by sync request or date rollover. Waking up!")
                    break
                await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(run_ingestion())
