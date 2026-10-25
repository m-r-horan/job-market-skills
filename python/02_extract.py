"""
Full extraction run: one Gemini call per posting, everything done by the LLM.

- Reads data/raw_postings.json
- Appends one JSON line per posting to data/extracted.jsonl, tagged with job_id
- Resumable: skips job_ids already in extracted.jsonl
- Retries on 429/5xx with backoff; sleeps between calls to stay under RPM
- Stores the raw LLM output; cleaning/normalization happens in a later step
"""

import os
import json
import time
import requests
from dotenv import load_dotenv

from common import INDUSTRY_OPTIONS, FUNCTION_OPTIONS

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise SystemExit("Missing GEMINI_API_KEY. Check our .env file.")

GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

RAW_PATH = os.path.join("data", "raw_postings.json")
OUT_PATH = os.path.join("data", "extracted.jsonl")

SLEEP_SECONDS = 5  # ~12 requests/min, under the 15 RPM free-tier limit
MAX_RETRIES = 5
LIMIT = None  # set to an int (e.g. 20) to do a partial run

SYSTEM_PROMPT = f"""You extract structured information from internship/co-op job postings
for a skills-discovery dashboard.

Return ONLY a JSON object matching this exact schema:

{{
  "industry": one of {INDUSTRY_OPTIONS},
  "function": one of {FUNCTION_OPTIONS},
  "is_internship": true | false,
  "skills": [list of short strings],
  "tools_technologies": [list of short strings],
  "degree_level": "Associate's" | "Bachelor's" | "Master's" | "PhD" | "High school" | "Not stated",
  "compensation": {{
    "text": exact pay string from the posting, or "Not stated",
    "min": number or null,
    "max": number or null,
    "period": "hour" | "week" | "month" | "year" | "stipend" | null
  }}
}}

RULES:
1. "industry" and "function": pick exactly one value from the given lists.
   Use "Other" if nothing fits. Never invent a category.
   "industry" is the EMPLOYER's primary business: what the organization mainly
   makes, sells, or does. It is NOT the type of work the intern does (that is
   "function"). Use these definitions:
   - Technology: software, internet, semiconductors, IT products and services.
   - Healthcare: hospitals, clinics, health insurers, biotech, pharma, medical devices.
   - Finance/Banking: banks, insurers, investment firms, payments, accounting-driven finance.
   - Retail/Consumer: stores, e-commerce, consumer brands sold to the public.
   - Government/Public Sector: the employer itself is a government body
     (federal, state, local agency, or government-run lab or program).
     Private companies that merely work for the government are not included.
   - Nonprofit: charities, foundations, advocacy and mission-driven organizations.
   - Education: schools, universities, education services.
   - Engineering: organizations whose main business is engineering R&D or
     building technical systems (aerospace, defense systems, robotics, research labs).
   - Manufacturing: companies that mainly produce physical goods in plants.
   - Media/Entertainment: publishing, film, TV, music, games, art, advertising.
   - Professional Services/Consulting: firms that sell expertise or
     services to other organizations (consulting, legal, design/engineering
     services, staffing, accounting firms).
   - Hospitality/Food Service: hotels, restaurants, travel, events.
   - Transportation/Logistics: shipping, freight, airlines, delivery, supply chain services.
   - Energy/Utilities: power, oil and gas, renewables, water.
   - Real Estate/Construction: property, homebuilding, construction contractors.
   If two fit, pick the one describing what the employer mainly sells or produces.
2. "skills": short phrases, 1-5 words each, worded as in the posting.
   NEVER full sentences or copied bullet text. Boil long bullets down to the
   skill itself (e.g. "Ability to work independently as well as part of a team"
   -> "working independently", "teamwork"). No duplicates, and no overlapping
   entries that mean the same thing (pick one phrasing). Skip vague traits and
   requirements that are not learnable skills ("hands-on experience",
   "work ethic", "self-motivated", "fast-paced environment").
   Skills = general, soft, or domain abilities ("patient scheduling",
   "stakeholder communication", "financial modeling").
3. "tools_technologies": specific named software, tools, languages, or
   platforms only ("Python", "Salesforce", "Epic", "MS Excel"). Keep the name as
   written in the posting, but drop trademark symbols (®, ™). Do not put
   skills here and do not put tools in skills.
4. NEVER infer or guess. If something isn't explicitly stated, use "Not stated"
   (degree_level) or null/[] as shown. An empty list is correct if none are mentioned.
5. "degree_level": the minimum degree the posting asks for or says the candidate
   must be pursuing. "Currently pursuing a bachelor's" -> "Bachelor's".
   If several levels are listed, pick the lowest. Otherwise "Not stated".
6. "compensation": only pay explicitly stated for this role. Assume USD.
   - "text": the pay as written, e.g. "$20.00/hr - $24.00/hr".
   - "min"/"max": plain numbers, no currency symbols or commas. If a single
     value, set both to that value. If a range, min and max.
   - "period": the unit the numbers are in. "stipend" for a flat lump sum
     with no time unit.
   - If pay isn't stated: text "Not stated", min null, max null, period null.
   - Do NOT treat vague statements like "competitive pay" or "paid internship"
     as compensation; those are "Not stated".
7. "is_internship": true if the role is an internship, co-op, or similar
   student/early-program placement (summer analyst, fellowship for students,
   apprenticeship). false if it is a regular full-time or part-time job
   (e.g. "Associate Analyst", "Coordinator") with no internship/co-op framing.
8. Return valid JSON only. No commentary, no markdown, no code fences.
"""


def build_user_content(job):
    highlights_text = ""
    for h in job.get("job_highlights") or []:
        highlights_text += f"\n{h.get('title', '')}:\n"
        for item in h.get("items", []):
            highlights_text += f"- {item}\n"

    return f"""Title: {job.get('title', '')}
Company: {job.get('company_name', '')}

Description:
{job.get('description', '')}
{highlights_text}"""


def call_gemini(user_content):
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": user_content}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(
                GEMINI_URL,
                headers={"Content-Type": "application/json"},
                params={"key": GEMINI_API_KEY},
                json=payload,
                timeout=60,
            )
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 10 * attempt
                print(f"    HTTP {r.status_code}, retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            r.raise_for_status()
            result = r.json()
            text = result["candidates"][0]["content"]["parts"][0]["text"]
            meta = result.get("usageMetadata", {})
            usage = {
                "input_tokens": meta.get("promptTokenCount", 0),
                "output_tokens": meta.get("candidatesTokenCount", 0),
            }
            return json.loads(text), usage
        except (requests.RequestException, KeyError, json.JSONDecodeError) as e:
            if attempt == MAX_RETRIES:
                raise
            wait = 5 * attempt
            print(f"    {type(e).__name__}: {e} -- retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError("Exhausted retries")


def load_done_ids():
    done = set()
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        done.add(json.loads(line)["job_id"])
                    except (json.JSONDecodeError, KeyError):
                        pass
    return done


def main():
    with open(RAW_PATH, "r", encoding="utf-8") as f:
        jobs = json.load(f)

    done = load_done_ids()
    todo = [j for j in jobs if j.get("job_id") and j["job_id"] not in done]
    if LIMIT:
        todo = todo[:LIMIT]

    print(f"{len(jobs)} postings total, {len(done)} already done, {len(todo)} to run.")

    total_in = total_out = ok = failed = 0
    with open(OUT_PATH, "a", encoding="utf-8") as out:
        for i, job in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {job.get('title', '')[:60]} — {job.get('company_name', '')}")
            try:
                extracted, usage = call_gemini(build_user_content(job))
            except Exception as e:
                failed += 1
                print(f"    FAILED: {e}")
                continue

            record = {"job_id": job["job_id"], "extracted": extracted, "usage": usage}
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()

            total_in += usage["input_tokens"]
            total_out += usage["output_tokens"]
            ok += 1
            time.sleep(SLEEP_SECONDS)

    print(f"\nDone. ok={ok}, failed={failed}")
    print(f"Tokens this run — input: {total_in}, output: {total_out}")
    print("Rerun the script to retry any failures; finished postings are skipped.")


if __name__ == "__main__":
    main()