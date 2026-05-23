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
    try:
        view_classes_btn = page.locator(CalendarSelectors.VIEW_CLASSES_BUTTON)
        # Using a short timeout to prevent blocking when the button is absent in the new UI
        await view_classes_btn.wait_for(state="visible", timeout=5000)
        logger.info("Clicking 'View Classes' button to navigate to calendar...")
        await view_classes_btn.click()
        await page.wait_for_load_state("networkidle")
    except (TimeoutError, Exception) as e:
        logger.info(f"'View Classes' button not found or not visible: {e}. Assuming we are already on the calendar dashboard and proceeding.")

async def select_latest_event(page: Page):
    """Finds and clicks the most recent class event."""
    logger.info("Detecting calendar events...")
    page_title = await page.title()
    logger.info(f"Page title before calendar detection: {page_title}")
    
    # Capture screenshot before event parsing (DEBUG ONLY)
    if settings.DEBUG_MODE:
        if not os.path.exists(settings.SCREENSHOTS_DIR):
            os.makedirs(settings.SCREENSHOTS_DIR)
        screenshot_path = os.path.join(settings.SCREENSHOTS_DIR, "pre_event_parsing.png")
        await page.screenshot(path=screenshot_path)
        logger.info(f"Captured pre-parsing screenshot at {screenshot_path}")
    
        # Capture targeted DOM debugging information for timetable section
        try:
            timetable_selector = "div.fc-view-container, div.calendar-container, #calendar, div.fc"
            await page.wait_for_selector(timetable_selector, timeout=30000)
            timetable_container = await page.query_selector(timetable_selector)
            if timetable_container:
                timetable_html = await timetable_container.inner_html()
                logger.debug(f"Timetable DOM Snippet (First 2000 chars): {timetable_html[:2000]}")
            else:
                logger.debug("Could not find main timetable container for DOM snapshot.")
        except Exception as dom_e:
            logger.warning(f"Failed to capture DOM snippet or timetable container not found: {dom_e}")
    
    valid_events = []
    for attempt in range(3):
        try:
            candidate_locators = await page.locator("div.fc-event, div.calendar-event, div[class*='event'], a.fc-event, a.fc-time-grid-event").all()
            
            logger.info(f"Attempt {attempt+1}: Detected {len(candidate_locators)} raw candidate locators.")
            
            valid_events = []
            for i, loc in enumerate(candidate_locators):
                try:
                    classes = await loc.get_attribute("class") or ""
                    text_content = await loc.inner_text() or ""
                    is_visible = await loc.is_visible()
                    box = await loc.bounding_box()
                    
                    # Add diagnostic logging (DEBUG ONLY)
                    if settings.DEBUG_MODE:
                        box_str = f"x:{box['x']:.1f}, y:{box['y']:.1f}, w:{box['width']:.1f}, h:{box['height']:.1f}" if box else "None"
                        logger.info(f"Candidate {i} Diagnostics -> Classes: '{classes}' | Text: '{text_content.strip()}' | Visible: {is_visible} | BoundingBox: {box_str}")
                    
                    if not is_visible:
                        continue
                        
                    if "fc-mirror-container" in classes or "placeholder" in classes.lower() or "fc-bgevent" in classes:
                        continue
                        
                    if not text_content.strip():
                        continue
                        
                    if not box or box["width"] == 0 or box["height"] == 0:
                        continue
                        
                    valid_events.append(loc)
                except Exception as eval_err:
                    logger.warning(f"Failed to evaluate candidate {i}: {eval_err}")
            
            if valid_events:
                logger.info(f"Filtered down to {len(valid_events)} valid REAL lecture events.")
                break
            else:
                logger.info("No valid lecture events found after filtering. Retrying...")
                await asyncio.sleep(5)
                
        except Exception as e:
            logger.warning(f"Attempt {attempt+1}: Failed to find events - {e}")
            await asyncio.sleep(5)
            
    if not valid_events:
        logger.warning("No active lecture events found.")
        if settings.DEBUG_MODE:
            html_content = await page.content()
            logger.debug(f"Page HTML when no events found: {html_content[:1000]}...")
            
            screenshot_path_fail = os.path.join(settings.SCREENSHOTS_DIR, "no_events_found.png")
            await page.screenshot(path=screenshot_path_fail)
            logger.info(f"Captured targeted debugging screenshot at {screenshot_path_fail}")
        
        return False
    
    logger.info(f"Selecting the first valid REAL lecture card...")
    try:
        handle = await valid_events[0].element_handle()
        clickable = await handle.query_selector("a, div")
        if clickable:
            await clickable.evaluate("node => node.click()")
        else:
            await handle.evaluate("node => node.click()")
    except Exception as click_err:
        logger.error(f"Failed to click the lecture card: {click_err}")
        raise
    
    # Stabilization delay (shorter in production)
    await page.wait_for_timeout(1000 if not settings.DEBUG_MODE else 3000)
    
    if settings.DEBUG_MODE:
        screenshot_post_click = os.path.join(settings.SCREENSHOTS_DIR, "post_click_modal.png")
        await page.screenshot(path=screenshot_post_click)
        logger.info(f"Captured post-click modal screenshot at {screenshot_post_click}")
        
        logger.info(f"Post-click current URL: {page.url}")
        
        modals = await page.locator("div[role='dialog'], div.modal, .modal-content, .ui-dialog").all()
        logger.info(f"Detected {len(modals)} visible dialogs/modals.")
        if modals:
            modal_html = await modals[0].inner_html()
            logger.debug(f"Modal HTML Snippet: {modal_html[:2000]}")
            
        for kw in ["Join", "join", "class", "lecture", "start"]:
            blocks = await page.locator(f"text={kw}").all()
            visible_blocks = [b for b in blocks if await b.is_visible()]
            logger.info(f"Found {len(visible_blocks)} visible text blocks containing '{kw}'.")
    
    return True

async def click_join_button(page: Page):
    """Retries clicking the Join button until success or timeout."""
    logger.info("Waiting for Join button to appear...")
    
    for attempt in range(20): # Try for ~100 seconds
        try:
            # Enumerate all visible buttons after click
            buttons = await page.locator("button, a.btn, a.joinBtn, div[role='button'], .btn").all()
            join_candidates = []
            
            for i, btn in enumerate(buttons):
                try:
                    is_visible = await btn.is_visible()
                    if not is_visible:
                        continue
                    
                    text = (await btn.inner_text()).strip()
                    aria = await btn.get_attribute("aria-label") or ""
                    classes = await btn.get_attribute("class") or ""
                    
                    # Log visible buttons on the first attempt
                    if attempt == 0:
                        logger.info(f"Visible Button {i} -> Text: '{text}' | Aria: '{aria}' | Classes: '{classes}'")
                    
                    # Robust Join button candidate ranking
                    if "join" in text.lower() or "join" in aria.lower() or "joinBtn" in classes:
                        join_candidates.append(btn)
                except:
                    pass
            
            if join_candidates:
                logger.info(f"Found {len(join_candidates)} potential Join buttons. Prioritizing first visible match.")
                logger.info("[JOIN_PHASE] Join button detected")
                await join_candidates[0].click(force=True)
                logger.info("[JOIN_PHASE] Join button detected and clicked successfully!")
                return True
                
        except Exception as e:
            logger.warning(f"Error while searching for Join button: {e}")
        
        await asyncio.sleep(5)
        logger.debug(f"Join button not ready (attempt {attempt+1}/20)...")
    
    screenshot_fail = os.path.join(settings.SCREENSHOTS_DIR, "join_button_search_failure.png")
    await page.screenshot(path=screenshot_fail)
    logger.info(f"Captured Join button search failure screenshot at {screenshot_fail}")
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
    
    logger.info("[JOIN_PHASE] BBB iframe detected and switched successfully.")

    # 2. Click Microphone
    logger.info("Selecting microphone...")
    await iframe_handle.wait_for_selector(MeetingSelectors.MICROPHONE_BUTTON, state="visible", timeout=60000)
    await iframe_handle.click(MeetingSelectors.MICROPHONE_BUTTON)
    
    # 3. Handle Echo Test
    logger.info("[JOIN_PHASE] Echo test detected")
    try:
        await iframe_handle.wait_for_selector(MeetingSelectors.ECHO_YES_BUTTON, state="visible", timeout=30000)
        await iframe_handle.click(MeetingSelectors.ECHO_YES_BUTTON)
        logger.info("[JOIN_PHASE] Audio connection confirmed. Fully joined class!")
    except TimeoutError:
        logger.warning("[JOIN_PHASE] Echo test button didn't appear. Possibly auto-joined or already in.")

async def analyze_lecture_state(page: Page) -> str:
    """Analyzes the lecture page to determine its state and logs diagnostics."""
    logger.info("Analyzing lecture page state...")
    
    # Wait a bit for the lecture page to fully render
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1000 if not settings.DEBUG_MODE else 3000)
    
    # 1. Fast Path check for JOINABLE_ACTIVE (production mode only)
    if not settings.DEBUG_MODE:
        try:
            join_btn = page.locator("button:has-text('Join'), a.joinBtn, a[href*='jnr.jsp'], [class*='joinbtn']").first
            if await join_btn.is_visible() and not await join_btn.is_disabled():
                logger.info("[FAST PATH] Confidently detected visible active Join button. Skipping diagnostic overhead.")
                return {"state": "JOINABLE_ACTIVE", "wait_time": 300}
        except Exception as fast_err:
            logger.debug(f"Fast path check skipped: {fast_err}")
            
    # 2. Save lecture_page.html and lecture_state_snapshot.png (DEBUG ONLY)
    if settings.DEBUG_MODE:
        if not os.path.exists(settings.SCREENSHOTS_DIR):
            os.makedirs(settings.SCREENSHOTS_DIR)
            
        screenshot_path = os.path.join(settings.SCREENSHOTS_DIR, "lecture_state_snapshot.png")
        await page.screenshot(path=screenshot_path)
        logger.info(f"Captured lecture state snapshot at {screenshot_path}")
        
        html_content = await page.content()
        html_path = os.path.join(settings.SCREENSHOTS_DIR, "lecture_page.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        logger.info(f"Saved lecture HTML snapshot to {html_path}")
        
    # 3. Log all visible buttons/links on lecture page
    elements = await page.locator("button, a, div[role='button']").all()
    if settings.DEBUG_MODE:
        logger.info(f"Found {len(elements)} potential interactive elements on lecture page.")
        
    visible_interactive = []
    for i, el in enumerate(elements):
        try:
            if not await el.is_visible():
                continue
            text = (await el.inner_text()).strip()
            href = await el.get_attribute("href") or ""
            classes = await el.get_attribute("class") or ""
            aria = await el.get_attribute("aria-label") or ""
            is_disabled = await el.is_disabled()
            
            if settings.DEBUG_MODE:
                logger.info(f"Interactive {i} -> Text: '{text}' | Href: '{href}' | Classes: '{classes}' | Aria: '{aria}' | Disabled: {is_disabled}")
                
            visible_interactive.append({
                "text": text,
                "disabled": is_disabled,
                "el": el,
                "href": href,
                "class": classes,
                "aria": aria
            })
        except Exception:
            pass

    # Helper function to find the leaf-most visible content element containing a keyword
    async def scan_for_leaf_element(pattern: str):
        excludes = [
            "header", "footer", "nav", ".sidebar", ".navigation", "#sidebar", 
            "#header", "#footer", "#menu", ".menu", ".user-profile", 
            "li.nav-item", "a.nav-link", ".menu-item", ".dropdown"
        ]
        js_query = """
        (pattern, excludes) => {
            function isExcluded(el) {
                for (const sel of excludes) {
                    if (el.closest(sel)) return true;
                }
                return false;
            }
            
            const elements = Array.from(document.querySelectorAll('*'));
            let bestMatch = null;
            let bestDepth = -1;
            
            for (const el of elements) {
                if (isExcluded(el)) continue;
                
                // Only consider visible elements
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || el.offsetWidth === 0 || el.offsetHeight === 0) {
                    continue;
                }
                
                const text = (el.innerText || "").toLowerCase();
                if (text.includes(pattern.toLowerCase())) {
                    let depth = 0;
                    let temp = el;
                    while (temp) {
                        depth++;
                        temp = temp.parentElement;
                    }
                    
                    if (depth > bestDepth) {
                        bestDepth = depth;
                        bestMatch = el;
                    }
                }
            }
            
            if (bestMatch) {
                let path = [];
                let temp = bestMatch;
                while (temp) {
                    let name = temp.tagName.toLowerCase();
                    if (temp.id) name += '#' + temp.id;
                    if (temp.className) {
                        name += '.' + Array.from(temp.classList).join('.');
                    }
                    path.unshift(name);
                    temp = temp.parentElement;
                }
                return {
                    tag: bestMatch.tagName,
                    id: bestMatch.id,
                    classes: bestMatch.className,
                    text: bestMatch.innerText.trim(),
                    outerHTML: bestMatch.outerHTML.substring(0, 1000),
                    path: path.join(' > ')
                };
            }
            return null;
        }
        """
        try:
            return await page.evaluate(js_query, [pattern, excludes])
        except Exception as js_err:
            logger.warning(f"JS leaf-most scan for '{pattern}' failed: {js_err}")
            return None

    # Perform highly scoped content scans to prevent false positives
    not_started_match = await scan_for_leaf_element("not started yet")
    if not not_started_match:
        not_started_match = await scan_for_leaf_element("class not started")
    if not not_started_match:
        not_started_match = await scan_for_leaf_element("yet to start")
        
    join_not_avail_match = await scan_for_leaf_element("join not available")
    if not join_not_avail_match:
        join_not_avail_match = await scan_for_leaf_element("not available yet")
        
    completed_match = await scan_for_leaf_element("completed")
    if not completed_match:
        completed_match = await scan_for_leaf_element("class ended")
    if not completed_match:
        completed_match = await scan_for_leaf_element("lecture completed")
    if not completed_match:
        completed_match = await scan_for_leaf_element("ended")

    # Read page text for countdown
    page_text = (await page.inner_text("body")).lower()
    import re
    h_match = re.search(r'(\d+)\s*h', page_text)
    m_match = re.search(r'(\d+)\s*m', page_text)
    s_match = re.search(r'(\d+)\s*s', page_text)
    has_countdown = bool(h_match or m_match or s_match)

    state = "UNKNOWN"
    
    # Priority 1: JOINABLE_ACTIVE (visible enabled Join button / jnr.jsp / joinBtn / clickable)
    join_btn_found = False
    for item in visible_interactive:
        text_match = "join" in item["text"].lower()
        href_match = "jnr.jsp" in item.get("href", "").lower()
        class_match = "joinbtn" in item.get("class", "").lower()
        aria_match = "join" in item.get("aria", "").lower()
        
        if (text_match or href_match or class_match or aria_match) and not item["disabled"]:
            join_btn_found = True
            logger.info(f"Priority 1 Match - Active Join button found: Text='{item['text']}', Href='{item.get('href')}', Class='{item.get('class')}'")
            break
            
    if join_btn_found:
        state = "JOINABLE_ACTIVE"
    # Priority 2: NOT_STARTED (explicit "Not started yet" content match, taking high precedence)
    elif not_started_match:
        state = "NOT_STARTED"
        logger.info(f"Priority 2 Match - 'Not Started' text detected inside content element:")
        logger.info(f"  Matched selector/tag: {not_started_match['tag']}")
        logger.info(f"  Matched text: '{not_started_match['text']}'")
        logger.info(f"  Matched DOM node details: ID='{not_started_match['id']}', Classes='{not_started_match['classes']}'")
        logger.info(f"  Matched container path: {not_started_match['path']}")
    # Priority 3: UPCOMING (countdown timer exists, NO active Join button exists)
    elif has_countdown:
        state = "UPCOMING"
        logger.info(f"Priority 3 Match - Countdown timer found on the page.")
    # Priority 4: NOT_YET_AVAILABLE (explicit "Join not available" inside core content area)
    elif join_not_avail_match:
        state = "NOT_YET_AVAILABLE"
        logger.info(f"Priority 4 Match - 'Join Not Available' text detected inside content element:")
        logger.info(f"  Matched selector/tag: {join_not_avail_match['tag']}")
        logger.info(f"  Matched text: '{join_not_avail_match['text']}'")
        logger.info(f"  Matched container path: {join_not_avail_match['path']}")
    # Priority 5: COMPLETED (completed or ended inside core content area)
    elif completed_match:
        state = "COMPLETED"
        logger.info(f"Priority 5 Match - 'Completed/Ended' text detected inside content element:")
        logger.info(f"  Matched selector/tag: {completed_match['tag']}")
        logger.info(f"  Matched text: '{completed_match['text']}'")
        logger.info(f"  Matched DOM node details: ID='{completed_match['id']}', Classes='{completed_match['classes']}'")
        logger.info(f"  Matched container path: {completed_match['path']}")
    else:
        state = "UNKNOWN"

    # Save diagnostic artifacts if COMPLETED is detected to allow false positive inspections
    if state == "COMPLETED":
        try:
            os.makedirs("screenshots", exist_ok=True)
            os.makedirs("scratch", exist_ok=True)
            
            # Save diagnostic files as requested
            diag_ss_path = "screenshots/completed_false_positive.png"
            await page.screenshot(path=diag_ss_path)
            
            diag_html_path = "scratch/completed_false_positive.html"
            html_content = await page.content()
            with open(diag_html_path, "w", encoding="utf-8") as f:
                f.write(html_content)
                
            logger.info("Saved COMPLETED state diagnostics successfully:")
            logger.info(f"  Screenshot -> {diag_ss_path}")
            logger.info(f"  DOM HTML -> {diag_html_path}")
            if completed_match:
                logger.info(f"  Source element outerHTML (Truncated): {completed_match['outerHTML']}")
        except Exception as diag_err:
            logger.warning(f"Failed to write completed diagnostics files: {diag_err}")

    # Future-ready retry metadata
    wait_time = 300
    if has_countdown:
        hours = int(h_match.group(1)) if h_match else 0
        minutes = int(m_match.group(1)) if m_match else 0
        seconds = int(s_match.group(1)) if s_match else 0
        total_seconds = hours * 3600 + minutes * 60 + seconds
        wait_time = max(total_seconds - 120, 30)
        logger.info(f"Explicit detection: Countdown timer is present ({hours}h {minutes}m {seconds}s).")
        logger.info(f"Retry recommendation (with wake-up safety buffer): Schedule next run in ~{wait_time} seconds (total countdown: {total_seconds}s).")
    else:
        logger.info("Retry recommendation: Default polling interval.")
        
    if settings.DEBUG_SLEEP_OVERRIDE_SECONDS is not None:
        logger.warning(f"DEBUG OVERRIDE: Forcing sleep duration to {settings.DEBUG_SLEEP_OVERRIDE_SECONDS} seconds.")
        wait_time = settings.DEBUG_SLEEP_OVERRIDE_SECONDS
        
    return {"state": state, "wait_time": wait_time}

async def join_class_pipeline(page: Page):
    """Orchestrates the entire joining flow using a state-machine loop."""
    try:
        # Note: At the start of the joining pipeline, the browser is already on the lecture page (mi.jsp)
        # after select_latest_event has been executed. No need to reload calendar or search events here.
        
        # ==========================================
        # [ULTRA FAST PATH] Production Instant-Click
        # ==========================================
        if "mi.jsp" in page.url:
            join_btn = page.locator("button:has-text('Join'), a.joinBtn, a[href*='jnr.jsp'], [class*='joinbtn']").first
            if await join_btn.count() > 0 and await join_btn.is_visible() and not await join_btn.is_disabled():
                logger.info("[ULTRA FAST PATH] Active, enabled Join button detected on lecture page. Joining immediately...")
                await join_btn.click()
                await handle_meeting_room(page)
                return True
        
        had_upcoming = False
        max_iterations = 20
        for iteration in range(1, max_iterations + 1):
            logger.info(f"--- Lifecycle Iteration #{iteration} ---")
            
            # Analyze lecture state after navigation or sleep
            lecture_info = await analyze_lecture_state(page)
            lecture_state = lecture_info["state"]
            wait_time = lecture_info["wait_time"]
            
            # If we transitioned from UPCOMING/NOT_STARTED but class is not yet joinable, run pre-join polling
            if had_upcoming and lecture_state not in ["JOINABLE_ACTIVE", "UPCOMING", "NOT_STARTED"]:
                logger.info("Lecture transitioned from UPCOMING/NOT_STARTED but JOINABLE_ACTIVE is not immediately available.")
                logger.info("Entering pre-join polling mode: polling every 20 seconds for up to 10 minutes...")
                
                poll_interval = 20
                if settings.DEBUG_SLEEP_OVERRIDE_SECONDS is not None:
                    poll_interval = settings.DEBUG_SLEEP_OVERRIDE_SECONDS
                    
                max_poll_duration = 600  # 10 minutes
                poll_elapsed = 0
                
                while poll_elapsed < max_poll_duration:
                    logger.info(f"Pre-join polling: {poll_elapsed // 60}m {poll_elapsed % 60}s elapsed of {max_poll_duration // 60}m limit...")
                    
                    try:
                        logger.info("Reloading active lecture page to refresh state...")
                        await page.reload()
                        await page.wait_for_load_state("networkidle")
                        
                        # [POLLING ULTRA FAST PATH]
                        if "mi.jsp" in page.url:
                            join_btn = page.locator("button:has-text('Join'), a.joinBtn, a[href*='jnr.jsp'], [class*='joinbtn']").first
                            if await join_btn.count() > 0 and await join_btn.is_visible() and not await join_btn.is_disabled():
                                logger.info("[POLLING ULTRA FAST PATH] Active Join button detected. Joining immediately...")
                                await join_btn.click()
                                await handle_meeting_room(page)
                                return True
                        
                        # Fallback recovery: if we somehow got redirected away from the lecture page
                        if "mi.jsp" not in page.url:
                            logger.info("Redirect detected. Re-navigating to lecture page via calendar...")
                            await open_calendar(page)
                            await select_latest_event(page)
                    except Exception as reload_err:
                        logger.warning(f"Failed to refresh active page state: {reload_err}")
                        
                    lecture_info = await analyze_lecture_state(page)
                    lecture_state = lecture_info["state"]
                    
                    if lecture_state == "JOINABLE_ACTIVE":
                        logger.info("Pre-join polling: Lecture became JOINABLE_ACTIVE!")
                        await wait_for_countdown(page, CalendarSelectors.COUNTDOWN_TEXT)
                        await click_join_button(page)
                        await handle_meeting_room(page)
                        return True
                    
                    await asyncio.sleep(poll_interval)
                    poll_elapsed += poll_interval
                
                logger.warning("Pre-join polling timed out after 10 minutes without finding Join button.")
                return False
 
            if lecture_state == "JOINABLE_ACTIVE":
                logger.info("Lecture is JOINABLE_ACTIVE. Proceeding to join sequence...")
                # Handle countdown if present (though unlikely if JOINABLE_ACTIVE, kept for safety)
                await wait_for_countdown(page, CalendarSelectors.COUNTDOWN_TEXT)
                await click_join_button(page)
                await handle_meeting_room(page)
                return True
                
            elif lecture_state in ["UPCOMING", "NOT_STARTED"]:
                had_upcoming = True
                logger.info(f"Lecture is {lecture_state}. Sleeping internally for {wait_time} seconds before re-evaluating...")
                await asyncio.sleep(wait_time)
                continue
                
            elif lecture_state == "COMPLETED":
                logger.info("Lecture is COMPLETED. Gracefully shutting down.")
                return False
                
            elif lecture_state == "NOT_YET_AVAILABLE":
                short_retry = 60
                if settings.DEBUG_SLEEP_OVERRIDE_SECONDS is not None:
                    short_retry = settings.DEBUG_SLEEP_OVERRIDE_SECONDS
                logger.info(f"Lecture is NOT_YET_AVAILABLE. Sleeping for short retry interval ({short_retry}s)...")
                await asyncio.sleep(short_retry)
                continue
                
            else:
                logger.warning(f"Lecture state is {lecture_state}. Aborting join sequence gracefully.")
                return False
                
        logger.warning(f"Maximum lifecycle iterations ({max_iterations}) reached. Exiting gracefully to prevent infinite loop.")
        return False
        
    except Exception as e:
        logger.error(f"Joining pipeline failed: {str(e)}")
        failure_path = os.path.join(settings.SCREENSHOTS_DIR, "joining_failure.png")
        await page.screenshot(path=failure_path)
        raise
