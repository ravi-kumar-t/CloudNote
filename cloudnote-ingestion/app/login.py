from playwright.async_api import Page
from .config import settings
from .logger import logger
from .selectors import LoginSelectors

async def perform_login(page: Page):
    """Handles the login flow for LPU MyClass."""
    logger.info(f"Navigating to {settings.BASE_URL}")
    await page.goto(settings.BASE_URL, wait_until="networkidle")
    
    logger.info("Filling login credentials...")
    try:
        await page.wait_for_selector(LoginSelectors.USERNAME_INPUT, timeout=settings.BROWSER_TIMEOUT)
        await page.fill(LoginSelectors.USERNAME_INPUT, settings.LPU_USERNAME)
        await page.fill(LoginSelectors.PASSWORD_INPUT, settings.LPU_PASSWORD)
        
        logger.info("Clicking login button...")
        await page.click(LoginSelectors.LOGIN_BUTTON)
        
        # Verify login success by waiting for a post-login element
        # Here we can wait for the 'View Classes' button or similar dashboard element
        await page.wait_for_load_state("networkidle")
        logger.info("Login process completed.")
        
    except Exception as e:
        logger.error(f"Login failed: {str(e)}")
        raise
