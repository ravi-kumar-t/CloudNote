import json
import os
import urllib.request
import urllib.error
from datetime import datetime
from .logger import logger
from .config import settings

def summarize_lecture(lecture_text: str) -> dict:
    """
    Summarizes lecture text using the Gemini API.
    Zero-dependency implementation using built-in urllib.
    Isolates all failures to prevent blocking the worker's graceful shutdown.
    """
    # 1. Verification and API Key check
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        logger.warning("Gemini Service: GEMINI_API_KEY is not configured. Summarization skipped.")
        fallback = generate_fallback_summary("Gemini API Key missing.")
        try:
            save_summary_atomically(settings.LPU_USERNAME, fallback)
        except Exception:
            pass
        return fallback
        
    if not lecture_text.strip():
        logger.warning("Gemini Service: Extracted lecture text is empty. Summarization skipped.")
        fallback = generate_fallback_summary("No lecture content was captured.")
        try:
            save_summary_atomically(settings.LPU_USERNAME, fallback)
        except Exception:
            pass
        return fallback

    logger.info("Gemini Service: Submitting lecture text for AI summarization...")
    
    # 2. Build the lightweight REST API URL and JSON payload
    # Using gemini-2.5-flash for speed and premium JSON structuring
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    
    prompt = (
        "You are an expert academic summarizer. Please analyze the following chronologically logged "
        "lecture content (chat messages, slide texts, and interactive notes) and provide a structured "
        "JSON summary matching the schema below. Do not include any Markdown wrappers like ```json. "
        "Strictly output the plain raw JSON.\n\n"
        "Schema:\n"
        "{\n"
        '  "summary": "A concise paragraph summarizing the core concepts covered during the lecture.",\n'
        '  "topics": ["Major Topic 1", "Major Topic 2", ...],\n'
        '  "key_points": ["Key technical detail or takeaway 1", "Key technical detail 2", ...]\n'
        "}\n\n"
        f"Lecture Log Content:\n{lecture_text}"
    )
    
    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }
    
    logger.info(f"[STRUCTURED] {json.dumps({'event': 'gemini_request_start', 'prompt_length': len(prompt), 'timestamp': datetime.now().isoformat()})}")
    
    try:
        data = json.dumps(payload).encode("utf-8")
        
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        
        # 3. Execute the request safely with strict timeout bounds
        with urllib.request.urlopen(req, timeout=30) as response:
            res_body = response.read().decode("utf-8")
            res_json = json.loads(res_body)
            
            # Extract generated text block from Gemini's standard response structure
            ai_text = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
            
            # Parse the text as JSON to verify and format correctly
            structured_data = json.loads(ai_text)
            logger.info("Gemini Service: Successfully generated and parsed lecture summary.")
            logger.info(f"[STRUCTURED] {json.dumps({'event': 'gemini_request_success', 'response_length': len(ai_text), 'timestamp': datetime.now().isoformat()})}")
            
            # Save the final summary output
            try:
                save_summary_atomically(settings.LPU_USERNAME, structured_data)
            except Exception:
                pass
            return structured_data
            
    except urllib.error.HTTPError as http_err:
        err_body = http_err.read().decode('utf-8', errors='ignore')
        logger.warning(f"Gemini Service: API HTTP error (Code {http_err.code}): {err_body}")
        logger.error(f"[STRUCTURED] {json.dumps({'event': 'gemini_request_failed', 'error': f'HTTP {http_err.code}: {err_body}', 'timestamp': datetime.now().isoformat()})}")
    except Exception as e:
        logger.warning(f"Gemini Service: API execution failed gracefully: {e}")
        logger.error(f"[STRUCTURED] {json.dumps({'event': 'gemini_request_failed', 'error': str(e), 'timestamp': datetime.now().isoformat()})}")
        
    # 4. Return fallback JSON on any failure to isolate issues
    fallback = generate_fallback_summary("AI generation encountered an unexpected API or network failure.")
    try:
        save_summary_atomically(settings.LPU_USERNAME, fallback)
    except Exception:
        pass
    return fallback

def generate_fallback_summary(reason: str) -> dict:
    """Generates a placeholder JSON structure when AI summarization is unavailable."""
    return {
        "summary": f"Lecture summary unavailable. Reason: {reason}",
        "topics": ["Lecture Ingestion Completed"],
        "key_points": ["Review logs/raw_lecture.txt for the raw captured text stream."]
    }

def save_summary_atomically(username: str, summary_data: dict) -> int:
    """
    Saves the structured JSON summary to logs/ai_summary.json and a timestamped file on disk,
    and inserts it into the SQLite database. Ensures atomic persistence: if either step fails,
    both files on disk are cleaned up to guarantee consistency.
    Returns the database row ID.
    """
    files_written = []
    db_written = False
    
    ai_summary_path = settings.AI_SUMMARY_FILE
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped_file = os.path.join(
        os.path.dirname(settings.AI_SUMMARY_FILE),
        f"ai_summary_{timestamp}.json"
    )
    
    try:
        # 1. Create directory and save to disk
        os.makedirs(os.path.dirname(ai_summary_path), exist_ok=True)
        
        with open(ai_summary_path, "w", encoding="utf-8") as f:
            json.dump(summary_data, f, indent=2)
        files_written.append(ai_summary_path)
            
        with open(timestamped_file, "w", encoding="utf-8") as f:
            json.dump(summary_data, f, indent=2)
        files_written.append(timestamped_file)
        
        logger.info(f"Gemini Service: Saved persistent AI summary to disk: {files_written}")
        
        # 2. Persist to SQLite DB under the LPU account
        from .database import save_summary_to_db
        row_id = save_summary_to_db(username, summary_data)
        db_written = True
        
        return row_id
        
    except Exception as save_err:
        # Atomic consistency rollback: clean up written files if DB write failed
        if len(files_written) > 0 and not db_written:
            logger.warning("Atomicity Enforcement: Database write failed. Rolling back written JSON files from disk.")
            for file_path in files_written:
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        logger.info(f"Atomicity Enforcement: Rolled back file: {file_path}")
                    except Exception as rm_err:
                        logger.error(f"Failed to remove file during rollback: {rm_err}")
                        
        logger.error(f"Gemini Service: Failed to persist AI summary outputs atomically: {save_err}")
        raise save_err
