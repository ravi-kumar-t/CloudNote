import asyncio
import os
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
    page_title = await page.title()
    logger.info(f"Page title before calendar detection: {page_title}")
    
    # Capture screenshot before event parsing
    if not os.path.exists(settings.SCREENSHOTS_DIR):
        os.makedirs(settings.SCREENSHOTS_DIR)
    screenshot_path = os.path.join(settings.SCREENSHOTS_DIR, "pre_event_parsing.png")
    await page.screenshot(path=screenshot_path)
    logger.info(f"Captured pre-parsing screenshot at {screenshot_path}")
    
    # Capture targeted DOM debugging information for timetable section
    try:
        timetable_container = await page.query_selector("div.fc-view-container, div.calendar-container, #calendar, div.fc")
        if timetable_container:
            timetable_html = await timetable_container.inner_html()
            logger.debug(f"Timetable DOM Snippet (First 2000 chars): {timetable_html[:2000]}")
        else:
            logger.debug("Could not find main timetable container for DOM snapshot.")
    except Exception as dom_e:
        logger.warning(f"Failed to capture DOM snippet: {dom_e}")
    
    valid_events = []
    for attempt in range(3):
        try:
            await page.wait_for_selector(CalendarSelectors.EVENT_BOX, timeout=30000)
            events = await page.query_selector_all(CalendarSelectors.EVENT_BOX)
            
            logger.info(f"Attempt {attempt+1}: Detected {len(events)} raw elements matching selector.")
            
            valid_events = []
            for i, ev in enumerate(events):
                try:
                    classes = await ev.get_attribute("class") or ""
                    text_content = await ev.inner_text() or ""
                    is_visible = await ev.is_visible()
                    
                    logger.info(f"Element {i} Diagnostics -> Classes: '{classes}' | Visible: {is_visible} | Text: '{text_content.strip()}'")
                    
                    if not is_visible:
                        continue
                        
                    if "fc-mirror-container" in classes or "placeholder" in classes.lower() or "fc-bgevent" in classes:
                        continue
                        
                    # Some empty containers might still render
                    if not text_content.strip():
                        continue
                        
                    valid_events.append(ev)
                except Exception as eval_err:
                    logger.warning(f"Failed to evaluate element {i}: {eval_err}")
            
            if valid_events:
                logger.info(f"Filtered down to {len(valid_events)} valid lecture events.")
                break
            else:
                logger.info("No valid lecture events found after filtering. Retrying...")
                await asyncio.sleep(5)
                
        except Exception as e:
            logger.warning(f"Attempt {attempt+1}: Failed to find events - {e}")
            await asyncio.sleep(5)
            
    if not valid_events:
        logger.warning("No active lecture events found.")
        html_content = await page.content()
        logger.debug(f"Page HTML when no events found: {html_content[:1000]}...")
        
        screenshot_path_fail = os.path.join(settings.SCREENSHOTS_DIR, "no_events_found.png")
        await page.screenshot(path=screenshot_path_fail)
        logger.info(f"Captured targeted debugging screenshot at {screenshot_path_fail}")
        
        return False
    
    logger.info(f"Selecting the first valid event...")
    # Click the first event found (usually the active one)
    # Using JavaScript click to avoid intersection issues common in these portals
    clickable = await valid_events[0].query_selector("a, div")
    if clickable:
        await clickable.evaluate("node => node.click()")
    else:
        await valid_events[0].evaluate("node => node.click()")
    
    await asyncio.sleep(2) # Wait for event details to pop up
    return True

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
        has_events = await select_latest_event(page)
        if not has_events:
            logger.info("Pipeline finishing gracefully because no active classes exist.")
            return False
        
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
