import os
import re
import asyncio
from datetime import datetime, timedelta, date
from playwright.async_api import Page
from .logger import logger
from .config import settings
from .timetable_cache import timetable_cache
from .timetable_parser import parse_event_card, parse_class_times
from .selectors import LoginSelectors, CalendarSelectors

async def fetch_timetable_data(page: Page) -> list:
    """
    Scrapes today's (and tomorrow's) classes from the CodeTantra calendar page.
    Returns a unified list of parsed class objects.
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
    today = date.today()
    await extract_visible_cards(today)
        
    logger.info(f"Timetable Scraper: Extraction complete. Collected {len(classes)} unique classes.")
    return classes
