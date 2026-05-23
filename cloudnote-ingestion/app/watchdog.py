import asyncio
from .logger import logger
from .selectors import MeetingSelectors
from .config import settings

async def check_session_health(page, browser) -> bool:
    """Evaluates the health of the current browser session using post-join indicators."""
    try:
        if not browser or not browser.is_connected():
            logger.warning("Watchdog Health Check: Browser is disconnected.")
            return False
            
        if not page or page.is_closed():
            logger.warning("Watchdog Health Check: Page is closed or crashed.")
            return False
            
        # 1. Verify meeting iframe is still attached
        iframe_element = page.locator(MeetingSelectors.IFRAME).first
        if await iframe_element.count() == 0:
            logger.warning("Watchdog Health Check: Meeting iframe not found in the DOM.")
            return False
            
        # 2. Verify active post-join meeting/participant/chat container exists in any frame
        container_found = False
        post_join_indicators = [
            "#app", "main#app", ".main-layout", "#meeting-layout", 
            "[aria-label='Users list']", "#users-list", "#user-list",
            "#chat-area", "#message-list", ".chat-area",
            "#presentation-container", ".presentation-container"
        ]
        
        for frame in page.frames:
            try:
                for selector in post_join_indicators:
                    if await frame.query_selector(selector):
                        container_found = True
                        break
                if container_found:
                    logger.info("[WATCHDOG] Chat/presentation containers detected")
                    break
            except Exception:
                continue
                
        if not container_found:
            logger.warning("Watchdog Health Check: Steady-state post-join container not found in any frame.")
            return False
            
        # 3. Check for disconnection or error overlays inside the main page and all frames
        disconnect_signals = [
            "websocket disconnected", 
            "connection lost", 
            "disconnected from server", 
            "click to reconnect",
            "something went wrong"
        ]
        
        # Check main page body text
        try:
            body_text = (await page.locator("body").inner_text()).lower()
            for signal in disconnect_signals:
                if signal in body_text:
                    logger.warning(f"Watchdog Health Check: Disconnect signal '{signal}' detected in main page text.")
                    return False
        except Exception as body_err:
            logger.warning(f"Watchdog Health Check: Failed to read page body text: {body_err}")
            return False
            
        # Check iframe body text
        for frame in page.frames:
            try:
                frame_text = (await frame.locator("body").inner_text()).lower()
                for signal in disconnect_signals:
                    if signal in frame_text:
                        logger.warning(f"Watchdog Health Check: Disconnect signal '{signal}' detected in iframe text.")
                        return False
            except Exception:
                continue
                
        logger.info("[WATCHDOG] Connection verified")
        logger.info("[WATCHDOG] Meeting session healthy")
        return True
    except Exception as e:
        logger.warning(f"Watchdog Health Check: Exception encountered: {e}")
        return False

async def attempt_recovery(page, browser, context, p, join_url: str = None):
    """Executes the 3-step recovery watchdog pipeline."""
    logger.info("[RECOVERY] Starting session recovery watchdog process...")
    
    # Transition session status to RECOVERING
    try:
        from .session_status import update_session_status
        update_session_status("RECOVERING")
    except Exception as status_err:
        logger.error(f"[RECOVERY] Failed to update session status to RECOVERING: {status_err}")
    
    # Step 1: Reconnect / Reload
    if browser and browser.is_connected() and page and not page.is_closed():
        logger.info("[RECOVERY] Watchdog Recovery Step 1: Attempting Reconnect via Page Reload...")
        try:
            await page.reload()
            await page.wait_for_load_state("networkidle", timeout=30000)
            
            # Check if reload fixed it and we are back in the meeting room
            mic_found = False
            for frame in page.frames:
                if await frame.query_selector(MeetingSelectors.MICROPHONE_BUTTON):
                    mic_found = True
                    break
            
            if mic_found:
                logger.info("[RECOVERY] Watchdog Recovery Step 1: Meeting room found. Completing echo test flow...")
                from .joiner import handle_meeting_room
                await handle_meeting_room(page)
                logger.info("[RECOVERY] Watchdog Step 1 Success: Reconnected successfully via reload.")
                
                # Restore status to CONNECTED
                update_session_status("CONNECTED")
                return page, browser, context
            else:
                logger.warning("[RECOVERY] Watchdog Step 1 Failed: Page reloaded but meeting room is not present.")
        except Exception as e:
            logger.warning(f"[RECOVERY] Watchdog Step 1 Exception: Reconnect reload failed: {e}")
            
    # Step 2: Re-login
    logger.info("[RECOVERY] Watchdog Recovery Step 2: Attempting Re-login...")
    new_browser, new_context, new_page = None, None, None
    try:
        # First close old resources if alive to clean up gracefully
        try:
            if page and not page.is_closed():
                await page.close()
            if context:
                await context.close()
            if browser and browser.is_connected():
                await browser.close()
        except Exception as close_err:
            logger.warning(f"[RECOVERY] Error cleaning up resources during recovery: {close_err}")
            
        # Recreate browser, context, and page
        from .main import create_browser_and_page
        new_browser, new_context, new_page = await create_browser_and_page(p)
        
        # Perform login
        from .login import perform_login
        await perform_login(new_page)
        logger.info("[RECOVERY] Watchdog Step 2 Success: Re-login succeeded.")
        
    except Exception as e:
        log_err_msg = f"[RECOVERY] Watchdog Step 2 Failed: Re-login failed: {e}"
        logger.error(log_err_msg)
        # Clean up any partially created resources
        try:
            if new_page and not new_page.is_closed():
                await new_page.close()
            if new_context:
                await new_context.close()
            if new_browser and new_browser.is_connected():
                await new_browser.close()
        except:
            pass
        return None
        
    # Step 3: Re-open lecture
    logger.info("[RECOVERY] Watchdog Recovery Step 3: Attempting to Re-open Lecture...")
    try:
        from .joiner import join_class_pipeline
        joined = await join_class_pipeline(new_page, join_url=join_url)
        if joined:
            logger.info("[RECOVERY] Watchdog Step 3 Success: Re-opened and re-joined lecture successfully.")
            # Restore status to CONNECTED
            update_session_status("CONNECTED")
            return new_page, new_browser, new_context
        else:
            logger.error("[RECOVERY] Watchdog Step 3 Failed: Could not re-join lecture.")
            await new_page.close()
            await new_context.close()
            await new_browser.close()
            return None
    except Exception as e:
        logger.error(f"[RECOVERY] Watchdog Step 3 Exception: Failed to re-open lecture: {e}")
        try:
            await new_page.close()
            await new_context.close()
            await new_browser.close()
        except:
            pass
        return None
