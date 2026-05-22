import asyncio
import os
import json
from playwright.async_api import async_playwright
from .config import settings
from .logger import logger
from datetime import datetime, timezone, timedelta
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
        viewport={'width': 1280, 'height': 720},
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
    ist = timezone(timedelta(hours=5, minutes=30))
    from .database import update_ingestion_status
    
    join_attempts_limit = 3
    join_failure_cooldown = 180  # 3 minutes cooldown if join/login fails
    idle_sleep_duration = 300  # 5 minutes sleep if calendar is empty
    active_check_interval = 60  # Check active hours every 60 seconds
    
    while True:
        now = datetime.now(ist)
        
        # 1. Enforce active hours (6 PM - 11 PM IST)
        if not (settings.ACTIVE_HOURS_START <= now.hour < settings.ACTIVE_HOURS_END):
            logger.info(f"Current time {now.strftime('%I:%M %p %Z')} is outside active hours ({settings.ACTIVE_HOURS_START}:00 - {settings.ACTIVE_HOURS_END}:00). Worker sleeping...")
            update_ingestion_status("idle", details="Outside active hours window (6 PM - 11 PM IST)")
            await asyncio.sleep(active_check_interval)
            continue
            
        logger.info("Within active hours. Launching Playwright browser instance...")
        update_ingestion_status("processing", details="Launching browser and scanning calendar")
        
        async with async_playwright() as p:
            browser, context, page = await create_browser_and_page(p)
            session_state = {
                "browser": browser,
                "context": context,
                "page": page
            }
            
            try:
                # A. Login & calendar check
                from .login import perform_login
                await perform_login(page)
                
                from .joiner import open_calendar, select_latest_event
                await open_calendar(page)
                
                has_events = await select_latest_event(page)
                if not has_events:
                    logger.info("No active classes found on calendar. Closing browser and waiting...")
                    update_ingestion_status("idle", details="No active classes found on calendar")
                    
                    await context.close()
                    await browser.close()
                    
                    # Sleep for idle duration before re-checking
                    await asyncio.sleep(idle_sleep_duration)
                    continue
                
                # B. Class event exists! Run join pipeline with bounded retries
                join_success = False
                for attempt in range(1, join_attempts_limit + 1):
                    logger.info(f"Attempting to join session (Attempt {attempt}/{join_attempts_limit})...")
                    update_ingestion_status("processing", details=f"Attempting to join class (Attempt {attempt}/{join_attempts_limit})")
                    try:
                        from .joiner import join_class_pipeline
                        # join_class_pipeline handles upcoming count downs internally and returns True on join success
                        join_success = await join_class_pipeline(page)
                        if join_success:
                            break
                    except Exception as join_err:
                        logger.error(f"Join attempt {attempt} failed: {join_err}")
                        if attempt < join_attempts_limit:
                            logger.info(f"Sleeping {join_failure_cooldown}s before retrying join...")
                            await asyncio.sleep(join_failure_cooldown)
                
                if not join_success:
                    logger.error("Failed to join class session after maximum retry attempts.")
                    update_ingestion_status("failed", error="Failed to join class session after max retries")
                    await context.close()
                    await browser.close()
                    await asyncio.sleep(join_failure_cooldown)
                    continue
                
                # C. Successfully joined class! Monitor lifecycle
                logger.info("Session is now ACTIVE. Monitoring lecture state...")
                update_ingestion_status("processing", details="Monitoring active lecture", subject="Live Lecture")
                
                from .watchdog import check_session_health, attempt_recovery
                
                extractor = LectureExtractor()
                rejoin_attempts = 0
                start_time = asyncio.get_event_loop().time()
                
                while True:
                    await asyncio.sleep(30)
                    
                    # 1. Passive text extraction
                    await extractor.extract_content(page)
                    
                    # 2. Check for lecture-ended state
                    lecture_ended = await check_lecture_completion(page)
                    if lecture_ended:
                        logger.info("Lecture completion detected on-screen. Initiating graceful class-end sequence.")
                        elapsed_seconds = int(asyncio.get_event_loop().time() - start_time)
                        final_metrics = {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "elapsed_seconds": elapsed_seconds,
                            "status": "COMPLETED",
                            "rejoin_attempts": rejoin_attempts,
                            "metrics": {
                                "buffer_lines": extractor.get_buffer_size(),
                                "iframe_count": len(page.frames)
                            }
                        }
                        logger.info(f"LIFECYCLE_COMPLETED_METRICS: {json.dumps(final_metrics)}")
                        break
                        
                    # 3. Verify session health
                    is_healthy = await check_session_health(page, browser)
                    
                    # 4. Emit structured logs
                    elapsed_seconds = int(asyncio.get_event_loop().time() - start_time)
                    metrics = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "elapsed_seconds": elapsed_seconds,
                        "status": "HEALTHY" if is_healthy else "UNHEALTHY",
                        "rejoin_attempts": rejoin_attempts,
                        "metrics": {
                            "buffer_lines": extractor.get_buffer_size(),
                            "iframe_count": len(page.frames)
                        }
                    }
                    logger.info(f"ACTIVE_SESSION_METRICS: {json.dumps(metrics)}")
                    
                    if not is_healthy:
                        logger.warning("Session health check failed! Activating watchdog recovery process...")
                        update_ingestion_status("processing", details="Watchdog recovery in progress", subject="Live Lecture")
                        recovery_result = await attempt_recovery(page, browser, context, p)
                        if recovery_result:
                            page, browser, context = recovery_result
                            rejoin_attempts += 1
                            logger.info(f"Session successfully recovered by watchdog. Total rejoins: {rejoin_attempts}/{settings.MAX_REJOIN_ATTEMPTS}")
                            update_ingestion_status("processing", details="Monitoring active lecture (recovered)", subject="Live Lecture")
                            
                            if rejoin_attempts > settings.MAX_REJOIN_ATTEMPTS:
                                logger.error("Too many watchdog recovery attempts. Bailing out to prevent infinite rejoin loop.")
                                raise Exception("Runaway watchdog loop protection triggered.")
                        else:
                            logger.error("Session recovery watchdog failed all attempts. Terminating session.")
                            raise Exception("Session health recovery failed.")
                            
                # D. Triggers summarization and persists results atomically
                if extractor.get_buffer_size() > 0:
                    logger.info("Executing final lecture content summarization...")
                    update_ingestion_status("processing", details="Generating AI lecture summary")
                    raw_content = extractor.get_raw_text()
                    from .gemini_service import summarize_lecture
                    
                    try:
                        summarize_lecture(raw_content)
                        update_ingestion_status("completed", details="AI Summary generated and persisted successfully")
                    except Exception as sum_err:
                        logger.error(f"Failed to generate/persist summary: {sum_err}")
                        update_ingestion_status("failed", error=f"Summarization failure: {str(sum_err)}")
                else:
                    logger.info("No content extracted from lecture. Skipping summarization.")
                    update_ingestion_status("idle", details="Lecture ended with empty content")
                    
            except Exception as e:
                logger.error(f"Ingestion execution loop failed: {str(e)}", exc_info=True)
                update_ingestion_status("failed", error=str(e))
                try:
                    screenshot_path = os.path.join(settings.SCREENSHOTS_DIR, "loop_error.png")
                    await page.screenshot(path=screenshot_path)
                    logger.info(f"Error screenshot saved to {screenshot_path}.")
                except Exception as ss_err:
                    logger.error(f"Failed to capture error screenshot: {ss_err}")
            finally:
                logger.info("Releasing Playwright resources and closing browser...")
                try:
                    await context.close()
                    if browser and browser.is_connected():
                        await browser.close()
                except Exception as close_err:
                    logger.warning(f"Error during final cleanup in loop: {close_err}")
                    
        # Sleep for a bit before scanning the calendar again
        logger.info("Awaiting next run interval...")
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(run_ingestion())
