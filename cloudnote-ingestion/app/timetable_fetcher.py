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
        # Focus strictly on top-level FullCalendar event cards (anchors or divs)
        event_selector = "a.fc-event, div.fc-event"
        locators = await page.locator(event_selector).all()
        logger.info(f"Timetable Scraper: Detected {len(locators)} top-level fc-event cards for {base_date.strftime('%Y-%m-%d')}.")
        
        for i, loc in enumerate(locators):
            try:
                classes_attr = await loc.get_attribute("class") or ""
                is_visible = await loc.is_visible()
                box = await loc.bounding_box()
                
                if not is_visible or not box:
                    continue
                if "fc-mirror-container" in classes_attr or "placeholder" in classes_attr.lower() or "fc-bgevent" in classes_attr:
                    continue
                if box["width"] == 0 or box["height"] == 0:
                    continue
                
                # Retrieve timing information from the data-full attribute inside the fc-time sub-container
                time_el = loc.locator("div.fc-time, .fc-time")
                timings_str = ""
                if await time_el.count() > 0:
                    data_full = await time_el.first.get_attribute("data-full")
                    if data_full:
                        timings_str = data_full.strip()
                    else:
                        timings_str = (await time_el.first.inner_text()).strip()
                
                # Fallback to card text parsing if timings_str is not resolved
                if not timings_str:
                    card_text = await loc.inner_text() or ""
                    lines = [l.strip() for l in card_text.split("\n") if l.strip()]
                    if lines:
                        timings_str = lines[0]
                
                # Retrieve course details (from fc-title or overall card text)
                title_el = loc.locator("div.fc-title, .fc-title")
                title_text = ""
                if await title_el.count() > 0:
                    title_text = (await title_el.first.inner_text()).strip()
                else:
                    title_text = (await loc.inner_text()).strip()
                
                logger.info(f"Timetable Scraper: Processing raw event timings: {timings_str} | Title: {title_text}")
                
                parsed = parse_event_card(f"{timings_str}\n{title_text}")
                if parsed:
                    # Retain the exact parsed 12-hour formatted timings
                    parsed["timings"] = timings_str
                    
                    # Resolve start and end datetimes based on target base_date
                    start_dt, end_dt = parse_class_times(timings_str, base_date)
                    parsed["start_time"] = start_dt.strftime("%Y-%m-%d %H:%M:%S") if start_dt else ""
                    parsed["end_time"] = end_dt.strftime("%Y-%m-%d %H:%M:%S") if end_dt else ""
                    
                    # Store direct join url if present in href
                    href = await loc.get_attribute("href")
                    if href:
                        parsed["join_url"] = href
                        
                    # Ensure subject_code unique index key
                    parsed_key = f"{parsed['subject_code']}_{parsed['start_time']}"
                    # Avoid duplicates
                    if not any(c.get("key") == parsed_key for c in classes):
                        parsed["key"] = parsed_key
                        classes.append(parsed)
                        logger.info(f"Timetable Scraper: Parsed class: {parsed['subject_code']} | Timings: {parsed['timings']} | Status: {parsed['status']}")
            except Exception as loc_e:
                logger.warning(f"Timetable Scraper: Failed to parse locator {i}: {loc_e}")

    # Step A: Extract Today's classes
    today = date.today()
    await extract_visible_cards(today)
        
    logger.info(f"Timetable Scraper: Extraction complete. Collected {len(classes)} unique classes.")
    return classes
