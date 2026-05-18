import asyncio
import os
from playwright.async_api import async_playwright
from .config import settings
from .logger import logger
from .login import perform_login

async def run_ingestion():
    logger.info("Starting CloudNote Playwright Ingestion Worker...")
    logger.info(f"Environment: Cloud/Railway Validation (HEADLESS={settings.HEADLESS})")
    async with async_playwright() as p:
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
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 720},
            permissions=['microphone']
        )
        
        page = await context.new_page()
        page.set_default_timeout(settings.BROWSER_TIMEOUT)
        page.set_default_navigation_timeout(settings.NAVIGATION_TIMEOUT)
        
        try:
            # Phase 1: Login
            await perform_login(page)
            
            # Phase 2: Joining Workflow
            from .joiner import join_class_pipeline
            await join_class_pipeline(page)
            
            logger.info("Successfully joined the class session.")
            
            # Keep session alive
            logger.info("Session is now ACTIVE. Heartbeat started.")
            while True:
                logger.info("Worker Status: HEALTHY | Session: ACTIVE")
                await asyncio.sleep(300) # Every 5 minutes
                
        except asyncio.CancelledError:
            logger.info("Shutdown signal received. Closing session gracefully...")
        except Exception as e:
            logger.error(f"Execution error: {str(e)}", exc_info=True)
            try:
                screenshot_path = os.path.join(settings.SCREENSHOTS_DIR, "error_screenshot.png")
                await page.screenshot(path=screenshot_path)
                logger.info(f"Error screenshot saved to {screenshot_path}. Note: Railway uses ephemeral disks unless a volume is attached.")
            except Exception as ss_err:
                logger.error(f"Failed to capture screenshot: {str(ss_err)}")
        finally:
            logger.info("Releasing browser resources...")
            await browser.close()

if __name__ == "__main__":
    asyncio.run(run_ingestion())
