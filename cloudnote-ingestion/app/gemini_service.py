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
        save_summary_to_disk(fallback)
        return fallback
        
    if not lecture_text.strip():
        logger.warning("Gemini Service: Extracted lecture text is empty. Summarization skipped.")
        fallback = generate_fallback_summary("No lecture content was captured.")
        save_summary_to_disk(fallback)
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
            
            # Save the final summary output
            save_summary_to_disk(structured_data)
            return structured_data
            
    except urllib.error.HTTPError as http_err:
        logger.warning(f"Gemini Service: API HTTP error (Code {http_err.code}): {http_err.read().decode('utf-8', errors='ignore')}")
    except Exception as e:
        logger.warning(f"Gemini Service: API execution failed gracefully: {e}")
        
    # 4. Return fallback JSON on any failure to isolate issues
    fallback = generate_fallback_summary("AI generation encountered an unexpected API or network failure.")
    save_summary_to_disk(fallback)
    return fallback

def generate_fallback_summary(reason: str) -> dict:
    """Generates a placeholder JSON structure when AI summarization is unavailable."""
    return {
        "summary": f"Lecture summary unavailable. Reason: {reason}",
        "topics": ["Lecture Ingestion Completed"],
        "key_points": ["Review logs/raw_lecture.txt for the raw captured text stream."]
    }

def save_summary_to_disk(summary_data: dict):
    """Saves the structured JSON summary to logs/ai_summary.json and timestamped file."""
    try:
        os.makedirs(os.path.dirname(settings.AI_SUMMARY_FILE), exist_ok=True)
        
        # Save standard logs/ai_summary.json
        with open(settings.AI_SUMMARY_FILE, "w", encoding="utf-8") as f:
            json.dump(summary_data, f, indent=2)
            
        # Also save a timestamped summary for audit history
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        timestamped_file = os.path.join(
            os.path.dirname(settings.AI_SUMMARY_FILE),
            f"ai_summary_{timestamp}.json"
        )
        with open(timestamped_file, "w", encoding="utf-8") as f:
            json.dump(summary_data, f, indent=2)
            
        logger.info(f"Gemini Service: Saved persistent AI summary to {settings.AI_SUMMARY_FILE} and {timestamped_file}")
    except Exception as save_err:
        logger.error(f"Gemini Service: Failed to persist AI summary outputs: {save_err}")
