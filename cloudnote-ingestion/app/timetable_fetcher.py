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
        event_selector = "div.fc-event, div.fc-event div, div.calendar-event, div[class*='event'], a.fc-event, a.fc-time-grid-event"
        locators = await page.locator(event_selector).all()
        logger.info(f"Timetable Scraper: Detected {len(locators)} raw event cards for {base_date.strftime('%Y-%m-%d')}.")
        
        for i, loc in enumerate(locators):
            try:
                classes_attr = await loc.get_attribute("class") or ""
                text_content = await loc.inner_text() or ""
                is_visible = await loc.is_visible()
                box = await loc.bounding_box()
                
                if not is_visible or not text_content.strip() or not box:
                    continue
                if "fc-mirror-container" in classes_attr or "placeholder" in classes_attr.lower() or "fc-bgevent" in classes_attr:
                    continue
                if box["width"] == 0 or box["height"] == 0:
                    continue
                    
                parsed = parse_event_card(text_content)
                if parsed:
                    # Resolve start and end datetimes based on target base_date
                    start_dt, end_dt = parse_class_times(parsed["timings"], base_date)
                    parsed["start_time"] = start_dt.strftime("%Y-%m-%d %H:%M:%S") if start_dt else ""
                    parsed["end_time"] = end_dt.strftime("%Y-%m-%d %H:%M:%S") if end_dt else ""
                    
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
    
    # Step B: Extract Tomorrow's classes for rollover safety
    try:
        next_button = page.locator("button.fc-next-button, .fc-button-next, button:has(span.fc-icon-right-single-arrow), button:has-text('Next')").first
        if await next_button.count() > 0 and await next_button.is_visible():
            logger.info("Timetable Scraper: Navigating to Tomorrow's schedule...")
            await next_button.click()
            await page.wait_for_timeout(1000)
            tomorrow = today + timedelta(days=1)
            await extract_visible_cards(tomorrow)
            
            # Navigate back to Today for UI state continuity
            prev_button = page.locator("button.fc-prev-button, .fc-button-prev, button:has(span.fc-icon-left-single-arrow), button:has-text('Prev')").first
            if await prev_button.count() > 0 and await prev_button.is_visible():
                await prev_button.click()
                await page.wait_for_timeout(1000)
    except Exception as nav_e:
        logger.debug(f"Timetable Scraper: Could not navigate to tomorrow's schedule: {nav_e}")
        
    logger.info(f"Timetable Scraper: Extraction complete. Collected {len(classes)} unique classes.")
    return classes
