"""
Shared pieces used by more than one pipeline script, kept in one place so the scripts
can't drift apart. (The mapping lookups in 04_build_tables.py only work if these behave
exactly the way they did when 03_normalize.py made the mappings.)
"""

import os
import re
import json

# ---- Where things live (all paths are relative to the project root) ----
RAW_PATH = os.path.join("data", "raw_postings.json")
EXTRACTED_PATH = os.path.join("data", "extracted.jsonl")
MAP_DIR = os.path.join("data", "mappings")
CLEAN_DIR = os.path.join("data", "clean")

MAP_FILES = {
    "tools": "tools.json",
    "skills": "skills.json",
    "cities": "cities.json",
    "titles": "title_keywords.json",
}

# ---- The allowed values the extraction prompt offers the model ----
INDUSTRY_OPTIONS = [
    "Technology", "Healthcare", "Finance/Banking", "Retail/Consumer",
    "Government/Public Sector", "Nonprofit", "Education",
    "Engineering", "Manufacturing", "Media/Entertainment",
    "Professional Services/Consulting", "Hospitality/Food Service",
    "Transportation/Logistics", "Energy/Utilities", "Real Estate/Construction",
    "Other",
]

FUNCTION_OPTIONS = [
    "Engineering/Technical", "Data/Analytics", "Marketing", "Sales",
    "Operations", "Finance/Accounting", "Human Resources", "Design/Creative",
    "Research", "Legal", "Customer Service", "Administrative/Office Support",
    "Science/Lab", "Education/Teaching", "Healthcare/Clinical",
    "Product Management", "Other",
]

REMOTE_WORDS = {"anywhere", "united states", "usa", "us", "remote"}


def clean_text(s):
    """Removes trademark symbols and collapses extra whitespace. Does not change case."""
    s = re.sub(r"[®™©]", "", str(s))
    return re.sub(r"\s+", " ", s).strip()


def parse_location(loc):
    """Splits the posting's own location text. Returns (city, state, multi_location).

    "Philadelphia, PA (+1 other)" -> ("Philadelphia", "PA", True)
    "Anywhere"                    -> ("Remote / unspecified", "", False)
    Plain string splitting only; no API calls.
    """
    loc = (loc or "").strip()
    if not loc:
        return "Unknown", "", False

    multi = " (+" in loc
    base = loc.split(" (+")[0].strip()
    parts = [p.strip() for p in base.split(",") if p.strip()]

    if not parts:
        return "Unknown", "", multi
    if len(parts) == 1:
        if parts[0].lower() in REMOTE_WORDS:
            return "Remote / unspecified", "", multi
        return parts[0], "", multi
    return parts[0], parts[1], multi


def load_raw_by_id():
    """raw_postings.json as {job_id: posting}, keeping the first copy of any repeated id."""
    with open(RAW_PATH, "r", encoding="utf-8") as f:
        raw_jobs = json.load(f)
    raw_by_id = {}
    for job in raw_jobs:
        jid = job.get("job_id")
        if jid and jid not in raw_by_id:
            raw_by_id[jid] = job
    return raw_by_id


def load_extracted():
    """Reads extracted.jsonl. Returns ({job_id: extracted_fields}, unreadable_lines, duplicate_ids)."""
    records, bad_lines, dupes = {}, 0, 0
    with open(EXTRACTED_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                job_id, extracted = rec["job_id"], rec["extracted"]
            except (json.JSONDecodeError, KeyError):
                bad_lines += 1
                continue
            if job_id in records:
                dupes += 1
                continue
            records[job_id] = extracted
    return records, bad_lines, dupes


def load_mapping(stage):
    """A mapping file from data/mappings/, or {} if it doesn't exist yet."""
    path = os.path.join(MAP_DIR, MAP_FILES[stage])
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}