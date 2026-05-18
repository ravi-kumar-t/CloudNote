import asyncio
import re
from .logger import logger

async def wait_for_countdown(page, countdown_selector: str):
    """Parses countdown text and waits accordingly."""
    try:
        countdown_element = await page.query_selector(countdown_selector)
        if not countdown_element:
            logger.info("No countdown found. Proceeding to join.")
            return

        text = await countdown_element.inner_text()
        text = text.strip().lower()
        logger.info(f"Detected countdown: {text}")

        # Regex to find hours, minutes, seconds (e.g., 01h 05m 10s)
        h_match = re.search(r'(\d+)h', text)
        m_match = re.search(r'(\d+)m', text)
        s_match = re.search(r'(\d+)s', text)

        hours = int(h_match.group(1)) if h_match else 0
        minutes = int(m_match.group(1)) if m_match else 0
        seconds = int(s_match.group(1)) if s_match else 0

        total_seconds = hours * 3600 + minutes * 60 + seconds

        if total_seconds > 0:
            logger.info(f"Waiting for {total_seconds} seconds until class starts...")
            await asyncio.sleep(total_seconds + 5) # Buffer
        else:
            logger.info("Class is ready to join.")

    except Exception as e:
        logger.warning(f"Error parsing countdown: {str(e)}. Proceeding anyway.")
