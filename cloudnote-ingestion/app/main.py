import asyncio
import os
from playwright.async_api import async_playwright
from .config import settings
from .logger import logger
from datetime import datetime, timezone, timedelta
import sys

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

async def run_ingestion_core(session_state, p):
    try:
        # Phase 1: Login
        from .login import perform_login
        await perform_login(session_state["page"])
        
        # Phase 2: Joining Workflow
        from .joiner import join_class_pipeline
        joined = await join_class_pipeline(session_state["page"])
        
        if joined:
            logger.info("Successfully joined the class session.")
            # Keep session alive during lecture
            logger.info("Session is now ACTIVE. Monitoring lecture state...")
            
            from .watchdog import check_session_health, attempt_recovery
            
            while True:
                # Active session health check every 30 seconds
                await asyncio.sleep(30)
                
                is_healthy = await check_session_health(session_state["page"], session_state["browser"])
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
                        logger.info("Session successfully recovered by watchdog.")
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
