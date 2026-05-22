import re
from datetime import datetime, date, time
from .logger import logger

def parse_event_card(text_content: str) -> dict:
    """
    Parses CodeTantra calendar event card inner text content.
    Extracts subject_code, subject_name, faculty, timings, status, and join_availability.
    """
    lines = [line.strip() for line in text_content.split("\n") if line.strip()]
    if not lines:
        return {}

    subject_code = ""
    subject_name = ""
    faculty = ""
    timings = ""
    status = "UPCOMING"
    join_availability = False

    time_pattern = r'\d{1,2}:\d{2}\s*(?:AM|PM)?\s*-\s*\d{1,2}:\d{2}\s*(?:AM|PM)?'

    # Known subject mappings fallback for clean subject names
    subject_mappings = {
        "CSES009": "Specialization Lecture",
        "CSE326": "Internet Web Programming",
        "MTH102": "Applied Mathematics",
        "CSE408": "Design & Analysis of Algorithms"
    }

    for line in lines:
        # Check if it matches timing pattern
        if re.search(time_pattern, line, re.IGNORECASE):
            timings = line
            continue

        # Check if this line is "Lecture by ..." representing faculty
        if "lecture by" in line.lower():
            parts = line.split(":", 1)
            raw_fac = parts[1].strip() if len(parts) > 1 else line.replace("Lecture by", "").strip(" :")
            
            # Clean up numbers, IDs, and parentheses
            raw_fac = re.sub(r'\(.*?\)', '', raw_fac).strip()
            names = re.split(r'[/,]', raw_fac)
            cleaned_names = []
            for n in names:
                cleaned_n = re.sub(r'^\s*\d+\s*:\s*', '', n).strip()
                cleaned_n = re.sub(r'\b\d+\b', '', cleaned_n).strip()
                if cleaned_n:
                    cleaned_names.append(cleaned_n)
            faculty = " / ".join(cleaned_names)
            continue

        # Look for subject code (e.g. CSE326, CSE 326, CSES009)
        code_match = re.search(r'\b([A-Z]{2,4})[\s-]?(\d{3,4})\b', line, re.IGNORECASE)
        if code_match:
            subject_code = f"{code_match.group(1).upper()}{code_match.group(2)}"
            # Extracted name is the rest of the line stripped
            name_part = re.sub(r'\b[A-Z]{2,4}[\s-]?\d{3,4}\b', '', line, flags=re.IGNORECASE).strip(" :-\t")
            if name_part and "lecture" not in name_part.lower():
                subject_name = name_part
            continue

        # Check status indicators
        if "join" in line.lower() and "not" not in line.lower():
            join_availability = True
            status = "JOINABLE_ACTIVE"
        elif "completed" in line.lower() or "ended" in line.lower():
            status = "COMPLETED"
        elif "started" in line.lower():
            status = "LIVE"

    # Fallback default values
    if not subject_code and lines:
        first_line = lines[0]
        code_match = re.search(r'\b([A-Z]{2,4})[\s-]?(\d{3,4})\b', first_line, re.IGNORECASE)
        if code_match:
            subject_code = f"{code_match.group(1).upper()}{code_match.group(2)}"
            subject_name = re.sub(r'\b[A-Z]{2,4}[\s-]?\d{3,4}\b', '', first_line, flags=re.IGNORECASE).strip(" :-\t")

    if not subject_name and subject_code:
        subject_name = subject_mappings.get(subject_code, f"{subject_code} Lecture")
    elif subject_name and "lecture by" in subject_name.lower():
        subject_name = subject_mappings.get(subject_code, f"{subject_code} Lecture")

    return {
        "subject_code": subject_code or "GEN",
        "subject_name": subject_name or "General Lecture",
        "faculty": faculty or "Faculty Staff",
        "timings": timings or "Unknown timings",
        "status": status,
        "join_availability": join_availability
    }

def parse_class_times(timings_str: str, base_date: date = None):
    """
    Parses timing strings e.g. "06:00 PM - 07:30 PM" or "06:00 - 07:30 PM"
    Returns a tuple of (start_datetime, end_datetime). Returns (None, None) if parsing fails.
    """
    if not base_date:
        base_date = date.today()

    try:
        parts = timings_str.split("-")
        if len(parts) != 2:
            return None, None

        start_part = parts[0].strip()
        end_part = parts[1].strip()

        # Handle boundary cases where only the end time has the AM/PM tag
        if "pm" in end_part.lower() and "am" not in start_part.lower() and "pm" not in start_part.lower():
            start_part += " PM"
        elif "am" in end_part.lower() and "am" not in start_part.lower() and "pm" not in start_part.lower():
            start_part += " AM"

        # Safe parsing fallback patterns
        start_dt = None
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
            try:
                start_dt = datetime.strptime(start_part, fmt)
                break
            except ValueError:
                continue

        end_dt = None
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
            try:
                end_dt = datetime.strptime(end_part, fmt)
                break
            except ValueError:
                continue

        if not start_dt or not end_dt:
            return None, None

        # Build datetime combined with active date context
        start_time = datetime.combine(base_date, start_dt.time())
        end_time = datetime.combine(base_date, end_dt.time())
        return start_time, end_time

    except Exception as e:
        logger.warning(f"Failed to parse class timings '{timings_str}': {e}")
        return None, None
