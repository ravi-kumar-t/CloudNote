import os
import re
import asyncio
from datetime import datetime, timedelta, date
from typing import Optional, Union
from playwright.async_api import Page
from .logger import logger
from .config import settings, get_screenshot_path, get_user_screenshot_path
from .timetable_cache import timetable_cache
from .timetable_parser import parse_event_card, parse_class_times
from .selectors import LoginSelectors, CalendarSelectors

async def fetch_timetable_data(page: Page, user_id: Optional[Union[int, str]] = None) -> list:
    """
    Scrapes today's (and tomorrow's) classes from the CodeTantra calendar page.
    Returns a unified list of parsed class objects.
    Supports user-scoped screenshot storage and database persistence when user_id is provided.
    """
    logger.info("Timetable Scraper: Starting timetable data extraction...")
    
    # 1. Ensure we are on the calendar
    from .joiner import open_calendar
    await open_calendar(page)
    
    # 2. Try to select "Day" view or standard day view if available
    try:
        day_button = page.locator("button.fc-agendaDay-button, button.fc-day-button, button:has-text('Day'), .fc-button-group button:has-text('day')").first
        if await day_button.count() > 0 and await day_button.is_visible():
            logger.info("Timetable Scraper: Switching calendar to Day view...")
            await day_button.click()
            await page.wait_for_timeout(1000)
    except Exception as e:
        logger.debug(f"Timetable Scraper: Could not switch to Day view (falling back to current view): {e}")

    classes = []
    
    async def extract_visible_cards(base_date: date):
        event_selector = "a.fc-time-grid-event.fc-event"
        
        # 1. Snapshot all visible event data FIRST before opening any modal
        logger.info(f"Timetable Scraper: Snapshotting visible cards for {base_date.strftime('%Y-%m-%d')}...")
        try:
            await page.wait_for_selector(event_selector, timeout=5000)
        except Exception:
            pass
            
        event_cards = await page.locator("a.fc-time-grid-event.fc-event").all()
        valid_cards = []
        for card in event_cards:
            try:
                box = await card.bounding_box()
                if not box:
                    continue
                # ignore invisible / collapsed / recycled cards
                if box["width"] < 50 or box["height"] < 20:
                    continue
                text = (await card.inner_text()).strip()
                if not text:
                    continue
                valid_cards.append({
                    "text": text,
                    "box": box,
                    "html": await card.evaluate("(e) => e.outerHTML")
                })
            except Exception:
                continue
                
        logger.info(f"VISIBLE VALID EVENT COUNT: {len(valid_cards)}")
        
        # 2. Iterate over valid_cards
        for i, item in enumerate(valid_cards):
            card_text = item["text"]
            box = item["box"]
            card_html = item["html"]
            
            logger.info(f"VISIBLE EVENT HTML: {card_html}")
            
            try:
                # 3. Re-query the DOM fresh inside each iteration using only strict selector
                loc = page.locator(event_selector).nth(i)
                
                is_visible = await loc.is_visible()
                box = await loc.bounding_box()
                
                if not is_visible or not box:
                    logger.warning(f"Timetable Scraper: Re-queried card [{i}] is no longer visible or present.")
                    continue
                
                # Snapshot details BEFORE click to avoid stale locator exceptions after DOM re-renders
                card_href = (await loc.get_attribute("href")) or ""
                
                # Requested Diagnostics logs
                logger.info(f"VISIBLE EVENT COUNT: {len(valid_cards)}")
                logger.info(f"EVENT TEXT: {card_text.strip()}")
                logger.info(f"EVENT BBOX: {box}")
                logger.info(f"EVENT HTML: {card_html[:1000]}")
                
                # --- NEW EXTRACTION FLOW: CLICK & EXPAND MODAL ---
                logger.info(f"Timetable Scraper: Clicking event card [{i}] to expand details modal...")
                await loc.click()
                
                # Wait for expanded modal/panel/details container
                try:
                    await page.wait_for_selector("text=/Class Timings|Status|Not started yet/i", timeout=5000)
                except Exception as wait_e:
                    logger.warning(f"Timetable Scraper: Modal didn't show expected text: {wait_e}")
                
                # Extract timings ONLY from expanded content
                modal_selectors = ["div[role='dialog']", "div.modal", ".modal-content", ".ui-dialog", "body"]
                modal_text = ""
                for selector in modal_selectors:
                    modal_el = page.locator(selector).first
                    if await modal_el.count() > 0 and await modal_el.is_visible():
                        modal_text = await modal_el.inner_text()
                        break
                if not modal_text:
                    modal_text = await page.inner_text("body")
                    
                logger.debug(f"Timetable Scraper: Modal raw inner text (first 1000 chars): {modal_text[:1000]}")
                
                # Regex parsing of real timings
                import re
                timings_str = ""
                # Try Pattern A: "26 May 20:00 - 26 May 21:00"
                match_a = re.search(r'\b\d{1,2}\s+[a-zA-Z]+\s+(\d{1,2}:\d{2})\s*-\s*\d{1,2}\s+[a-zA-Z]+\s+(\d{1,2}:\d{2})\b', modal_text)
                if match_a:
                    timings_str = f"{match_a.group(1)} - {match_a.group(2)}"
                    logger.info(f"Timetable Scraper: Timing extracted using Pattern A (real modal): {timings_str}")
                else:
                    # Fallback Pattern B: "12:00 PM - 01:00 PM"
                    match_b = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM)?\s*-\s*\d{1,2}:\d{2}\s*(?:AM|PM)?)', modal_text, re.IGNORECASE)
                    if match_b:
                        timings_str = match_b.group(1).strip()
                        logger.info(f"Timetable Scraper: Timing extracted using Pattern B (real modal): {timings_str}")
                
                # Close the modal dialog
                try:
                    close_btn = page.locator("button[aria-label='Close'], .close, .ui-dialog-titlebar-close, button:has-text('Close'), a:has-text('Close')").first
                    if await close_btn.count() > 0 and await close_btn.is_visible():
                        await close_btn.click()
                    else:
                        await page.keyboard.press("Escape")
                    await page.wait_for_timeout(1000)
                except Exception as close_err:
                    logger.warning(f"Timetable Scraper: Failed to close modal: {close_err}")
                
                # --- Layered Defense Timing Extraction (Fallback) ---
                if not timings_str:
                    logger.warning("Timetable Scraper: Could not resolve timings from modal. Using collapsed tile fallbacks.")
                    time_match = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM)?\s*-\s*\d{1,2}:\d{2}\s*(?:AM|PM)?)', card_text, re.IGNORECASE)
                    if time_match:
                        timings_str = time_match.group(1).strip()
                    else:
                        lines = [l.strip() for l in card_text.split("\n") if l.strip()]
                        if lines:
                            timings_str = lines[0]
                
                # Retrieve course details (from overall card text)
                title_text = card_text.strip()
                    
                logger.info(f"Timetable Scraper: Processing raw event timings: {timings_str} | Title: {title_text}")
                
                parsed = parse_event_card(f"{timings_str}\n{title_text}")
                if parsed:
                    # Retain the exact parsed 12-hour formatted timings
                    parsed["timings"] = timings_str
                    
                    # Resolve start and end datetimes based on target base_date
                    start_dt, end_dt = parse_class_times(timings_str, base_date)
                    parsed["start_time"] = start_dt.strftime("%Y-%m-%d %H:%M:%S") if start_dt else ""
                    parsed["end_time"] = end_dt.strftime("%Y-%m-%d %H:%M:%S") if end_dt else ""
                    
                    # Store direct join url if present in card_href
                    if card_href:
                        parsed["join_url"] = card_href
                        
                    # Ensure subject_code unique index key
                    parsed_key = f"{parsed['subject_code']}_{parsed['start_time']}"
                    # Avoid duplicates
                    if not any(c.get("key") == parsed_key for c in classes):
                        parsed["key"] = parsed_key
                        classes.append(parsed)
                        logger.info(f"Timetable Scraper: Parsed class: {parsed['subject_code']} | Timings: {parsed['timings']} | Status: {parsed['status']}")
            except Exception as loc_e:
                logger.warning(f"Timetable Scraper: Failed to process card {i}: {loc_e}")

    # Step A: Extract Today's classes
    from .utils import get_now_ist
    today = get_now_ist().date()
    await extract_visible_cards(today)
        
    logger.info(f"Timetable Scraper: Extraction complete. Collected {len(classes)} unique classes.")

    if len(classes) == 0:
        logger.info("No classes detected for today. Capturing timetable screenshot...")
        from .utils import get_now_ist
        timestamp = get_now_ist().strftime("%Y-%m-%d_%H-%M-%S")
        ss_filename = f"no_classes_{timestamp}.png"
        if user_id is not None:
            ss_path = get_user_screenshot_path(user_id, ss_filename)
        else:
            ss_path = get_screenshot_path(ss_filename)

        try:
            await page.wait_for_timeout(1000)
            await page.screenshot(path=ss_path, full_page=True)
            logger.info(f"Timetable screenshot saved: {ss_path}")

            # Clean up prior zero-class screenshot for today to avoid accumulating duplicates
            old_ss = None
            if user_id is not None:
                try:
                    from .database import get_user_timetable
                    today_str = get_now_ist().strftime("%Y-%m-%d")
                    existing_tt = get_user_timetable(int(user_id), today_str)
                    if existing_tt:
                        old_ss = existing_tt.get("no_classes_screenshot")
                except Exception:
                    pass
            if not old_ss:
                old_ss = getattr(timetable_cache, "no_classes_screenshot", None)

            if old_ss and old_ss != ss_filename:
                try:
                    if user_id is not None:
                        old_path = get_user_screenshot_path(user_id, old_ss)
                    else:
                        old_path = get_screenshot_path(old_ss)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                except Exception as rm_err:
                    logger.debug(f"Could not remove older verification screenshot {old_ss}: {rm_err}")

            verified_at = get_now_ist().strftime("%Y-%m-%d %H:%M:%S")
            timetable_cache.set_no_classes_screenshot(ss_filename, verified_at)

            # Persist to user_timetables table if user_id provided
            if user_id is not None:
                try:
                    from .database import save_user_timetable
                    today_str = get_now_ist().strftime("%Y-%m-%d")
                    save_user_timetable(
                        user_id=int(user_id),
                        date_str=today_str,
                        classes=[],
                        no_classes_screenshot=ss_filename,
                        verified_at=verified_at
                    )
                except Exception as db_err:
                    logger.warning(f"Timetable Scraper: Failed to persist zero-class timetable to DB for user {user_id}: {db_err}")

        except Exception as ss_e:
            logger.error(f"Failed to capture timetable screenshot: {ss_e}")
    else:
        # Classes detected: persist to user_timetables table if user_id provided
        if user_id is not None:
            try:
                from .database import save_user_timetable
                from .utils import get_now_ist
                today_str = get_now_ist().strftime("%Y-%m-%d")
                verified_at = get_now_ist().strftime("%Y-%m-%d %H:%M:%S")
                save_user_timetable(
                    user_id=int(user_id),
                    date_str=today_str,
                    classes=classes,
                    no_classes_screenshot=None,
                    verified_at=verified_at
                )
            except Exception as db_err:
                logger.warning(f"Timetable Scraper: Failed to persist timetable classes to DB for user {user_id}: {db_err}")

    return classes

async def fetch_user_timetable_headless(
    user_id: int,
    lpu_username: str,
    lpu_password: str
) -> list:
    """
    Performs an isolated headless timetable synchronization for an explicit user.
    Creates an independent browser context, logs in with supplied credentials,
    extracts the calendar timetable, commits it to user_timetables, and cleanly exits.
    """
    from playwright.async_api import async_playwright
    from .login import perform_login

    async with async_playwright() as p:
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
        page.set_default_navigation_timeout(settings.NAVIGATION_TIMEOUT)

        try:
            await perform_login(page, username=lpu_username, password=lpu_password, user_id=user_id)
            classes = await fetch_timetable_data(page, user_id=user_id)
            return classes
        finally:
            await context.close()
            await browser.close()
