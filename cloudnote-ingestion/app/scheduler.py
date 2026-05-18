import asyncio
import re
from .logger import logger

async def wait_for_countdown(page, countdown_selector: str):
    """Parses countdown text inside the lecture modal and waits accordingly."""
    try:
        # Targeted modal parsing
        modal = page.locator("div[role='dialog'], div.modal, .modal-content, .ui-dialog").first
        
        # Scoped selector only inside lecture modal/popup
        if await modal.count() > 0:
            logger.info("Found lecture modal, scoping countdown search to modal.")
            modal_text = await modal.inner_text()
            text = modal_text.strip().lower()
            
            h_match = re.search(r'(\d+)\s*h', text)
            m_match = re.search(r'(\d+)\s*m', text)
            s_match = re.search(r'(\d+)\s*s', text)
            
            if not (h_match or m_match or s_match):
                logger.info("No countdown pattern found in modal. Proceeding to join.")
                return
        else:
            logger.info("No modal found, skipping countdown.")
            return

        logger.info(f"Detected countdown components in modal: h={h_match.group(1) if h_match else 0}, m={m_match.group(1) if m_match else 0}, s={s_match.group(1) if s_match else 0}")
        
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
