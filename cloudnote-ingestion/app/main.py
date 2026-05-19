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
    logger.info("Starting CloudNote Playwright Ingestion Worker...")
    
    # Active Window Check
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    if not (settings.ACTIVE_HOURS_START <= now.hour < settings.ACTIVE_HOURS_END):
        logger.info(f"Current time {now.strftime('%I:%M %p %Z')} is outside active hours ({settings.ACTIVE_HOURS_START}:00 - {settings.ACTIVE_HOURS_END}:00). Exiting gracefully.")
        sys.exit(0)
        
    logger.info(f"Environment: Cloud/Railway Validation (HEADLESS={settings.HEADLESS})")
    async with async_playwright() as p:
        browser, context, page = await create_browser_and_page(p)
        session_state = {
            "browser": browser,
            "context": context,
            "page": page
        }
        
        try:
            # Wrap the core logic with a session timeout
            await asyncio.wait_for(
                run_ingestion_core(session_state, p),
                timeout=settings.MAX_SESSION_DURATION_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning(f"Maximum session duration ({settings.MAX_SESSION_DURATION_SECONDS}s) reached. Forcing graceful shutdown.")
        finally:
            logger.info("Releasing Playwright resources and closing browser...")
            try:
                if session_state["context"]:
                    await session_state["context"].close()
                if session_state["browser"] and session_state["browser"].is_connected():
                    await session_state["browser"].close()
            except Exception as close_err:
                logger.warning(f"Error during final cleanup in run_ingestion: {close_err}")
            logger.info("Shutdown complete.")

if __name__ == "__main__":
    asyncio.run(run_ingestion())
