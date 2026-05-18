import asyncio
from playwright.async_api import Page, TimeoutError
from .logger import logger
from .selectors import CalendarSelectors, MeetingSelectors
from .config import settings
from .scheduler import wait_for_countdown

async def open_calendar(page: Page):
    """Navigates to the class calendar."""
    logger.info("Opening class calendar...")
    await page.wait_for_selector(CalendarSelectors.VIEW_CLASSES_BUTTON)
    await page.click(CalendarSelectors.VIEW_CLASSES_BUTTON)
    await page.wait_for_load_state("networkidle")

async def select_latest_event(page: Page):
    """Finds and clicks the most recent class event."""
    logger.info("Detecting calendar events...")
    await page.wait_for_selector(CalendarSelectors.EVENT_BOX, timeout=10000)
    
    events = await page.query_selector_all(CalendarSelectors.EVENT_BOX)
    if not events:
        raise Exception("No class events found on calendar.")
    
    logger.info(f"Found {len(events)} events. Selecting the first one...")
    # Click the first event found (usually the active one)
    # Using JavaScript click to avoid intersection issues common in these portals
    clickable = await events[0].query_selector("a, div")
    if clickable:
        await clickable.evaluate("node => node.click()")
    else:
        await events[0].evaluate("node => node.click()")
    
    await asyncio.sleep(2) # Wait for event details to pop up

async def click_join_button(page: Page):
    """Retries clicking the Join button until success or timeout."""
    logger.info("Waiting for Join button to appear...")
    
    for attempt in range(20): # Try for ~100 seconds
        try:
            join_btn = await page.query_selector(CalendarSelectors.JOIN_BUTTON)
            if join_btn and await join_btn.is_visible():
                logger.info("Join button found! Clicking...")
                await join_btn.click()
                return True
        except Exception:
            pass
        
        await asyncio.sleep(5)
        logger.debug(f"Join button not ready (attempt {attempt+1}/20)...")
    
    raise Exception("Join button did not appear in time.")

async def handle_meeting_room(page: Page):
    """Handles the meeting room interface: iframe, mic, and echo test."""
    logger.info("Handling meeting room initialization...")
    
    # 1. Handle Iframe
    logger.info("Waiting for meeting iframe...")
    iframe_handle = None
    for _ in range(10):
        iframes = page.frames
        # Look for the frame that contains meeting-related text or the mic button
        for frame in iframes:
            try:
                mic = await frame.query_selector(MeetingSelectors.MICROPHONE_BUTTON)
                if mic:
                    iframe_handle = frame
                    break
            except:
                continue
        if iframe_handle:
            break
        await asyncio.sleep(5)
    
    if not iframe_handle:
        # Fallback: check if we can just find it by tag
        iframe_element = await page.wait_for_selector(MeetingSelectors.IFRAME, timeout=30000)
        iframe_handle = await iframe_element.content_frame()
    
    if not iframe_handle:
        raise Exception("Could not locate meeting iframe.")
    
    logger.info("Successfully switched to meeting context.")

    # 2. Click Microphone
    logger.info("Selecting microphone...")
    await iframe_handle.wait_for_selector(MeetingSelectors.MICROPHONE_BUTTON, state="visible", timeout=60000)
    await iframe_handle.click(MeetingSelectors.MICROPHONE_BUTTON)
    
    # 3. Handle Echo Test
    logger.info("Waiting for echo test (YES button)...")
    try:
        await iframe_handle.wait_for_selector(MeetingSelectors.ECHO_YES_BUTTON, state="visible", timeout=30000)
        await iframe_handle.click(MeetingSelectors.ECHO_YES_BUTTON)
        logger.info("Echo test confirmed. Fully joined class!")
    except TimeoutError:
        logger.warning("Echo test button didn't appear. Possibly auto-joined or already in.")

async def join_class_pipeline(page: Page):
    """Orchestrates the entire joining flow."""
    try:
        await open_calendar(page)
        await select_latest_event(page)
        
        # Handle countdown if present
        await wait_for_countdown(page, CalendarSelectors.COUNTDOWN_TEXT)
        
        await click_join_button(page)
        await handle_meeting_room(page)
        
        return True
    except Exception as e:
        logger.error(f"Joining pipeline failed: {str(e)}")
        failure_path = os.path.join(settings.SCREENSHOTS_DIR, "joining_failure.png")
        await page.screenshot(path=failure_path)
        raise
