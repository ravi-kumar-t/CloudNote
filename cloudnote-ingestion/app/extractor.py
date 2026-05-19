import os
import json
import asyncio
from collections import deque
from datetime import datetime
from playwright.async_api import Page
from .logger import logger
from .config import settings

class LectureExtractor:
    """Advanced, passive, read-only extractor of lecture content with deep diagnostics and fallback pipelines."""
    
    def __init__(self):
        self.buffer = deque(maxlen=settings.MAX_BUFFER_LINES)
        self.seen_lines = set()
        
    async def extract_content(self, page: Page) -> int:
        """
        Runs the extraction pipeline, gathering detailed diagnostics, selector telemetry, 
        specific content, and generic body text fallbacks without interacting with the DOM.
        """
        if not page or page.is_closed():
            return 0
            
        timestamp_str = datetime.now().isoformat()
        new_lines_captured = 0
        
        # Diagnostics and telemetry storage
        frame_diagnostics = []
        selector_hits = {}
        raw_extracted_blocks = []
        
        # 1. Broad set of specific and generic selectors
        chat_selectors = [
            "[data-test='chatMessageText']",
            ".chat-message",
            ".message-content",
            ".msg-text",
            ".chat-area p"
        ]
        
        presentation_selectors = [
            "#presentation-container svg text",
            ".text-layer span",
            ".presentation text",
            ".presentation p"
        ]
        
        generic_fallback_selectors = [
            "p",
            "h1", "h2", "h3", "h4", "h5", "h6",
            "li",
            "[aria-live]",
            "[class*='caption']",
            "[class*='transcript']",
            "span[class*='message']",
            "div[class*='message']",
            "div[class*='chat']"
        ]
        
        all_selectors = chat_selectors + presentation_selectors + generic_fallback_selectors
        for selector in all_selectors:
            selector_hits[selector] = 0
            
        try:
            # 2. Enumerate and analyze the entire frame tree (Iframe Tree Diagnostics)
            frames = page.frames
            logger.info(f"Extractor Diagnostics: Scanning {len(frames)} total active frames.")
            
            for index, frame in enumerate(frames):
                try:
                    # Gather metadata safely
                    url = frame.url
                    if not isinstance(url, str):
                        url = str(url) if url else ""
                        
                    name = frame.name
                    if not isinstance(name, str):
                        name = str(name) if name else ""
                    
                    # Safely evaluate title and body text length
                    title = ""
                    try:
                        title = await frame.evaluate("document.title")
                    except:
                        pass
                        
                    body_text = ""
                    try:
                        body_text = await frame.locator("body").inner_text()
                    except:
                        pass
                        
                    body_len = len(body_text)
                    
                    # Log diagnostics per frame
                    logger.info(f"Frame [{index}] -> URL: '{url}' | Name: '{name}' | Title: '{title}' | Body Length: {body_len} chars")
                    
                    frame_diagnostics.append({
                        "index": index,
                        "url": url,
                        "name": name,
                        "title": title,
                        "body_length": body_len
                    })
                    
                    if body_len == 0:
                        continue
                        
                    # 3. Specific element queries & selector hits telemetry
                    for selector in all_selectors:
                        try:
                            elements = await frame.query_selector_all(selector)
                            for el in elements:
                                try:
                                    if await el.is_visible():
                                        text = (await el.inner_text()).strip()
                                        if text and len(text) > 2:
                                            raw_extracted_blocks.append(text)
                                            selector_hits[selector] += 1
                                except:
                                    continue
                        except:
                            continue
                            
                    # 4. Generic Fallback: Extract all lines from frame body text directly
                    # This guarantees that if text exists in the DOM, it is captured
                    try:
                        body_lines = body_text.split("\n")
                        for line in body_lines:
                            line = line.strip()
                            if line and len(line) > 5:
                                # We capture it as a raw candidate
                                raw_extracted_blocks.append(line)
                    except:
                        pass
                        
                except Exception as frame_err:
                    logger.debug(f"Failed to scan frame [{index}]: {frame_err}")
                    continue
            
            # 5. Determine if BBB lecture exposes textual DOM content
            max_body_len = max([f["body_length"] for f in frame_diagnostics]) if frame_diagnostics else 0
            if max_body_len < 100:
                logger.warning(
                    f"ANALYSIS: All frames expose very low textual content (Max length: {max_body_len} chars). "
                    "The lecture session might be rendered inside a video stream or HTML5 Canvas without exposing text in the DOM."
                )
            else:
                logger.info(f"ANALYSIS: Accessible textual DOM content exists (Max frame body: {max_body_len} chars).")
                
            # 6. Normalization, deduplication, and buffer queuing
            total_blocks_found = len(raw_extracted_blocks)
            extracted_chars = sum([len(b) for b in raw_extracted_blocks])
            
            for line in raw_extracted_blocks:
                norm_line = " ".join(line.lower().split())
                # Skip duplicate lines and common short interface text (e.g. "mute", "unmute")
                if norm_line not in self.seen_lines and len(norm_line) > 3:
                    self.seen_lines.add(norm_line)
                    
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    formatted_line = f"[{timestamp}] {line}"
                    
                    self.buffer.append(formatted_line)
                    new_lines_captured += 1
                    
            # 7. Telemetry report
            logger.info(
                f"Extractor Telemetry: Blocks Found: {total_blocks_found} | "
                f"Extracted Characters: {extracted_chars} | "
                f"New Unique Lines: {new_lines_captured} | "
                f"Buffer Size: {len(self.buffer)}"
            )
            
            # 8. Persist raw lecture lines
            if new_lines_captured > 0:
                await self.save_to_disk()
                
            # 9. Save detailed JSON diagnostic snapshot
            await self.save_diagnostics(timestamp_str, frame_diagnostics, selector_hits, raw_extracted_blocks, {
                "total_blocks_found": total_blocks_found,
                "extracted_chars": extracted_chars,
                "new_deduped_lines": new_lines_captured
            })
            
        except Exception as e:
            logger.warning(f"Extractor: Diagnostic scan encountered top-level error: {e}")
            
        return new_lines_captured
        
    async def save_to_disk(self):
        """Asynchronously writes the rolling buffer to logs/raw_lecture.txt."""
        try:
            os.makedirs(os.path.dirname(settings.RAW_LECTURE_FILE), exist_ok=True)
            with open(settings.RAW_LECTURE_FILE, "w", encoding="utf-8") as f:
                for line in self.buffer:
                    f.write(line + "\n")
        except Exception as err:
            logger.error(f"Extractor: Failed to save raw lecture buffer: {err}")
            
    async def save_diagnostics(self, timestamp: str, frames: list, selector_hits: dict, raw_blocks: list, telemetry: dict):
        """Saves a detailed extraction snapshot to logs/extraction_diagnostics.json."""
        try:
            diag_file = "logs/extraction_diagnostics.json"
            os.makedirs(os.path.dirname(diag_file), exist_ok=True)
            
            snapshot = {
                "timestamp": timestamp,
                "telemetry": telemetry,
                "frame_diagnostics": frames,
                "selector_hit_counts": {k: v for k, v in selector_hits.items() if v > 0},
                "raw_extracted_blocks": list(set(raw_blocks))[:200]  # Cap list to prevent massive files
            }
            
            with open(diag_file, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2)
                
            logger.info(f"Extractor: Saved diagnostic extraction snapshot to {diag_file}")
        except Exception as diag_err:
            logger.error(f"Extractor: Failed to save diagnostic snapshot: {diag_err}")

    def get_raw_text(self) -> str:
        """Retrieves the complete content of the buffer as a single text block."""
        return "\n".join(list(self.buffer))

    def get_buffer_size(self) -> int:
        """Returns the current number of lines inside the rolling buffer."""
        return len(self.buffer)
