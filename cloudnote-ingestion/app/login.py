from playwright.async_api import Page
from .config import settings, get_screenshot_path
from .logger import logger
from .selectors import LoginSelectors
from datetime import datetime, timezone, timedelta
from .utils import get_now_ist
async def perform_login(page: Page):
    """Handles the login flow for LPU MyClass."""
    logger.info(f"Navigating to {settings.BASE_URL}")
    await page.goto(
        settings.BASE_URL,
        wait_until="commit",
        timeout=180000
    )
    await page.wait_for_timeout(3000)
    logger.info(f"PLAYWRIGHT URL AFTER GOTO: {page.url}")
    logger.info(f"PLAYWRIGHT TITLE: {await page.title()}")
    
    logger.info("Filling login credentials...")
    try:
        timestamp_start = get_now_ist().strftime("%Y-%m-%d_%H-%M-%S")
        screenshot_path_start = get_screenshot_path(f"login_start_{timestamp_start}.png")
        try:
            await page.screenshot(path=screenshot_path_start, full_page=True)
            logger.info(f"Screenshot saved: {screenshot_path_start}")
        except Exception as e:
            logger.error(f"Screenshot failed at login_start: {e}")

        await page.locator(LoginSelectors.USERNAME_INPUT).fill(settings.LPU_USERNAME)
        await page.locator(LoginSelectors.PASSWORD_INPUT).fill(settings.LPU_PASSWORD)
        
        logger.info("Clicking login button...")
        
        login_button = page.locator(
            "button[type='submit'], button:has-text('Login'), button"
        ).first
        
        await login_button.dispatch_event("click")
        
        logger.info("Login click dispatched.")
        
        await page.wait_for_timeout(3000)
        
        await page.wait_for_load_state("domcontentloaded", timeout=60000)
        
        logger.info(f"POST LOGIN URL: {page.url}")
        
        try:
            logger.info(f"POST LOGIN TITLE: {await page.title()}")
        except Exception:
            logger.info("POST LOGIN TITLE unavailable due to navigation transition.")
            
        timestamp_success = get_now_ist().strftime("%Y-%m-%d_%H-%M-%S")
        screenshot_path_success = get_screenshot_path(f"login_success_{timestamp_success}.png")
        try:
            await page.screenshot(path=screenshot_path_success, full_page=True)
            logger.info(f"Screenshot saved: {screenshot_path_success}")
        except Exception as e:
            logger.error(f"Screenshot failed at login_success: {e}")

        logger.info("Login process completed.")
        
    except Exception as e:
        logger.error(f"Login failed: {str(e)}")
        raise
