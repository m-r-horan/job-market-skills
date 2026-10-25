"""
Normalization: builds the lookup tables that merge messy variants into clean names.

Reads (never modifies):
  data/extracted.jsonl      LLM output from extract_all.py
  data/raw_postings.json    for each posting's title and location

Writes to data/mappings/ (one JSON file each; open them and hand-fix any bad merge):
  tools.json           "excel" / "ms excel" / "microsoft excel"  ->  "Microsoft Excel"
  skills.json          "problem-solving" / "problem solving"     ->  "Problem solving"
  cities.json          "SF|CA" / "San Francisco|"                ->  {"city": "San Francisco", "state": "CA"}
  title_keywords.json  "Product Manager Intern - Summer 2027"    ->  ["product manager"]
                       (keywords taken from the title itself)

Only unique strings are sent to the model (not whole postings), in batches, so a full
run is roughly 25 API calls.

Resumable and safe to rerun: anything already in a mapping file is kept (including your
hand edits) and only new strings are sent to the model. A string the model failed to
answer for is left out of the file and retried on the next run.

Usage (from the project root):
  python python/03_normalize.py                # all four
  python python/03_normalize.py tools cities   # only those
  python python/03_normalize.py --redo tools skills   # ask again for everything in those stages
                                                       # (the old file is kept as *.previous.json)
"""

import os
import sys
import json
import shutil
import time
import requests
from collections import Counter
from dotenv import load_dotenv

from common import (MAP_DIR, MAP_FILES, clean_text, load_extracted, load_mapping,
                    load_raw_by_id, parse_location)

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise SystemExit("Missing GEMINI_API_KEY. Check our .env file.")

GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

SLEEP_SECONDS = 5        # stays under the 15 requests/minute free-tier limit
MAX_RETRIES = 5
MIN_SKILL_COUNT = 1      # 1 = map every skill, so one-off variants still merge into common names
CONTEXT_NAMES = 300      # how many already-chosen names to show the model for consistency

STAGES = ["tools", "skills", "cities", "titles"]
BATCH_SIZES = {"tools": 150, "skills": 150, "cities": 100, "titles": 80}
# ----------------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------------

TOOLS_SYSTEM = """You clean up a list of tools and technologies taken from internship postings.

INPUT is a JSON object of index -> raw tool name (lowercase). Return a JSON object with the
same indexes, each mapped to its canonical name.

A TOOL is something a person uses to do the work: named software, programming languages,
platforms, hardware, lab equipment and instruments, and categories of software ("CRM",
"ERP systems", "accounting software"). A skill, method, technique, field of study or concept
is NOT a tool ("machine learning", "agile methodology", "flow cytometry", "hydrology"):
map those to null so they can be handled as skills.

RULES:
1. Merge only true variants of the SAME thing: spelling, spacing, abbreviations,
   plural/singular, versions, and "Microsoft"/"MS" prefixes. Examples: "excel", "ms excel",
   "microsoft excel" -> "Microsoft Excel"; "microsoft office suite", "ms office" ->
   "Microsoft Office"; "powerbi" -> "Power BI"; "oscilloscope", "oscilloscopes" ->
   "Oscilloscope"; "erp", "erp systems" -> "ERP".
2. Do NOT merge different things. A generic category is not a specific product ("spreadsheet
   software" stays "Spreadsheet software"; it is not "Microsoft Excel"), and a specific tool
   is not a broader suite (Microsoft Excel and Microsoft Office stay separate; Java and
   JavaScript stay separate).
3. Use the standard proper-case name (Python, SQL, C++, AutoCAD, Oscilloscope, CRM). If the
   input is a long spelled-out name with a well-known short form, use the short form.
4. If an ALREADY USED NAMES list is given, reuse a name from it whenever the input is a
   variant of that thing.
5. If something is a specific named product or instrument, keep it as a tool even if you do
   not recognise it.
6. Map to null only when the input is a skill, method, technique, field of study or concept,
   and not something a person uses.

Return ONLY a JSON object like {"0": "Microsoft Excel", "1": null}. Every index must appear."""

SKILLS_SYSTEM = """You clean up a list of skills taken from internship postings.

INPUT is a JSON object of index -> raw skill phrase (lowercase). Return a JSON object with
the same indexes, each mapped to a canonical skill name.

RULES:
1. Merge two phrases only when one is just a reworded version of the other: the same skill,
   with nothing added or lost. That covers spelling and hyphenation, plural/singular, word
   order, a trailing "skills" or "abilities", and filler words such as "strong" or
   "effective". Examples: "problem-solving", "problem solving" -> "Problem solving";
   "strong communication skills", "communication skills" -> "Communication".
2. If a phrase adds a domain, an object or a method, it is a different skill. Keep the added
   detail and do NOT fold it into the bare general term ("circuit testing" is not "Testing";
   "tax consulting" is not "Consulting"; "written communication" is not "Communication").
3. Use sentence case ("Data analysis", "Project management"); keep acronyms upper-case
   ("CAD design", "SQL querying"). Tidy the wording, but keep the meaning.
4. If an ALREADY USED NAMES list is given, reuse a name from it whenever the input is just a
   reworded version of that skill.
5. Never map to null. Every phrase here has already been accepted as a skill: soft traits
   (initiative, judgment), domain knowledge (calculus, reactor physics), techniques,
   languages and methods all stay. If unsure, keep the phrase as it is, in sentence case.

Return ONLY a JSON object like {"0": "Problem solving", "1": "Circuit testing"}. Every index must appear."""

CITIES_SYSTEM = """You clean up place names taken from job postings.

INPUT is a JSON object of index -> "city|state" exactly as written on the posting. The state
may be blank, and the city may be an abbreviation, nickname or misspelling. Return a JSON
object with the same indexes, each mapped to {"city": ..., "state": ...}.

RULES:
1. Use the full standard city name and the 2-letter US state code. Examples: "SF|CA",
   "San Francisco|", "San Francisco|California" -> {"city": "San Francisco", "state": "CA"};
   "NYC|NY", "New York City|NY" -> {"city": "New York", "state": "NY"}.
   A bare "Washington" with no state means Washington, DC.
2. Fill in a missing state only when the city is unambiguous. Otherwise leave state "".
3. Do not merge different places: suburbs and neighboring cities stay separate (Cambridge is
   not Boston). Merge only names that refer to the same place.
4. "Remote / unspecified" and "Unknown" stay exactly as they are, with state "".
5. If an ALREADY USED NAMES list is given, reuse those spellings.

Return ONLY a JSON object. Every index must appear."""

TITLES_SYSTEM = """You pull search keywords out of internship job titles, for students deciding
what words to type into a job board.

INPUT is a JSON object of index -> job title. Return a JSON object with the same indexes,
each mapped to a list of 1 to 3 lowercase keywords or short phrases TAKEN FROM THE TITLE.

RULES:
1. Use the title's own words. Do not rename, translate or expand them, and do not add words the
   title does not contain ("swe" stays "swe"; "analyst" stays "analyst").
2. Keep the words that name the role or field: "product manager", "data analyst", "software
   engineer", "associate", "marketing", "supply chain". When a role and its field sit together in
   the title, keep them as one phrase ("product manager", not "product" and "manager").
3. Drop words that are not searchable role or field terms: "intern", "internship", "co-op",
   "coop", seasons, years, city and state names, company names, "hybrid", "remote",
   "now hiring", "opportunity", "program", and degree or level words such as "undergraduate",
   "graduate", "bs/ms", "phd".
4. Lowercase everything. The only changes allowed are removing a plural "s" or fixing hyphen and
   spacing differences ("data-analyst" and "data analysts" -> "data analyst").
5. If an ALREADY USED NAMES list is given, reuse one of those spellings when it is the same
   keyword.
6. If nothing in the title names a role or field (for example just "Summer Internship"), return [].

Return ONLY a JSON object like {"0": ["software engineer"], "1": []}. Every index must appear."""


# ----------------------------------------------------------------------------
# Counting the unique strings to normalize
# ----------------------------------------------------------------------------

def load_postings():
    """Returns [(raw_posting, extracted_fields)] for postings flagged as internships."""
    raw_by_id = load_raw_by_id()
    records, _, _ = load_extracted()
    return [
        (raw_by_id[jid], ex) for jid, ex in records.items()
        if jid in raw_by_id and ex.get("is_internship") is not False
    ]


def count_terms(postings, field):
    """How many postings mention each (cleaned, lowercased) term. Once per posting."""
    counts = Counter()
    for _, ex in postings:
        seen = set()
        for item in ex.get(field) or []:
            t = clean_text(item).lower()
            if t and t not in seen:
                seen.add(t)
                counts[t] += 1
    return counts


def count_cities(postings):
    counts = Counter()
    for raw, _ in postings:
        city, state, _ = parse_location(clean_text(raw.get("location", "")))
        counts[f"{city}|{state}"] += 1
    return counts


def count_titles(postings):
    counts = Counter()
    for raw, _ in postings:
        title = clean_text(raw.get("title", ""))
        if title and not title.lower().startswith("test"):
            counts[title] += 1
    return counts


# ----------------------------------------------------------------------------
# Mapping files
# ----------------------------------------------------------------------------

def map_path(stage):
    return os.path.join(MAP_DIR, MAP_FILES[stage])


def save_mapping(stage, mapping):
    """Sorted by canonical name so variants of the same thing sit together when you edit."""
    os.makedirs(MAP_DIR, exist_ok=True)
    ordered = dict(sorted(
        mapping.items(),
        key=lambda kv: (json.dumps(kv[1], ensure_ascii=False).lower(), kv[0]),
    ))
    tmp = map_path(stage) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=1)
    os.replace(tmp, map_path(stage))   # a Ctrl+C mid-write can't corrupt the real file


# ----------------------------------------------------------------------------
# Model call
# ----------------------------------------------------------------------------

def call_model(system_prompt, user_text):
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    # The key goes in a header, not the URL, so error messages never print it.
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=120)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 10 * attempt
                print(f"    HTTP {r.status_code}, retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError) as e:
            if attempt == MAX_RETRIES:
                raise
            wait = 5 * attempt
            print(f"    {type(e).__name__} -- retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError("Exhausted retries")


# ----------------------------------------------------------------------------
# Answer checks: each returns (ok, value). Not ok -> left unmapped and retried next run.
# ----------------------------------------------------------------------------

def check_term(has, v, key=None):
    if not has:
        return False, None
    if v is None:
        return True, None            # model says: not a real tool/skill
    if isinstance(v, str) and clean_text(v):
        return True, clean_text(v)
    return False, None


def check_skill(has, v, key):
    """Skills are never dropped: a null answer keeps the original phrase."""
    if not has:
        return False, None
    if isinstance(v, str) and clean_text(v):
        return True, clean_text(v)
    if v is None:
        return True, key
    return False, None


def check_city(has, v, key=None):
    if not has or not isinstance(v, dict):
        return False, None
    city = clean_text(v.get("city") or "")
    state = clean_text(v.get("state") or "").upper().rstrip(".")
    if not city:
        return False, None
    return True, {"city": city, "state": state}


def check_keywords(has, v, key=None):
    if not has or not isinstance(v, list):
        return False, None
    out = []
    for k in v:
        if isinstance(k, str):
            k = clean_text(k).lower()
            if k and k not in out:
                out.append(k)
    return True, out[:3]


# ----------------------------------------------------------------------------
# Context shown to the model so names stay consistent from batch to batch
# ----------------------------------------------------------------------------

def context_terms(mapping):
    return [n for n, _ in Counter(v for v in mapping.values() if v).most_common(CONTEXT_NAMES)]


def context_cities(mapping):
    names = Counter(
        f"{v['city']}, {v['state']}" if v["state"] else v["city"] for v in mapping.values()
    )
    return [n for n, _ in names.most_common(CONTEXT_NAMES)]


def context_keywords(mapping):
    kw = Counter(k for v in mapping.values() for k in v)
    return [n for n, _ in kw.most_common(CONTEXT_NAMES)]


SPECS = {
    "tools":  (TOOLS_SYSTEM, check_term, context_terms),
    "skills": (SKILLS_SYSTEM, check_skill, context_terms),
    "cities": (CITIES_SYSTEM, check_city, context_cities),
    "titles": (TITLES_SYSTEM, check_keywords, context_keywords),
}


def run_stage(stage, keys, redo=False):
    system_prompt, check, context_fn = SPECS[stage]
    batch_size = BATCH_SIZES[stage]

    mapping = load_mapping(stage)
    if redo and mapping:
        backup = map_path(stage).replace(".json", ".previous.json")
        shutil.copyfile(map_path(stage), backup)
        print(f"\n[{stage}] --redo: old mapping kept as {backup}")
        mapping = {}
    todo = [k for k in keys if k not in mapping]
    print(f"\n[{stage}] {len(keys)} unique strings, {len(mapping)} already mapped, {len(todo)} to map")

    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        numbered = {str(i): k for i, k in enumerate(batch)}

        names = context_fn(mapping)
        user_text = ""
        if names:
            user_text += ("ALREADY USED NAMES (reuse these when the input is a variant):\n"
                          + json.dumps(names, ensure_ascii=False) + "\n\n")
        user_text += "INPUT:\n" + json.dumps(numbered, ensure_ascii=False)

        try:
            result = call_model(system_prompt, user_text)
        except Exception as e:
            print(f"  batch {start // batch_size + 1} FAILED ({type(e).__name__}); will retry on the next run")
            continue

        if not isinstance(result, dict):
            print(f"  batch {start // batch_size + 1}: unexpected answer shape; will retry on the next run")
            continue

        added = missing = 0
        for i, k in numbered.items():
            ok, value = check(i in result, result.get(i), k)
            if ok:
                mapping[k] = value
                added += 1
            else:
                missing += 1
        save_mapping(stage, mapping)
        note = f", {missing} unanswered (retried next run)" if missing else ""
        print(f"  batch {start // batch_size + 1}/{(len(todo) + batch_size - 1) // batch_size}: "
              f"{added} mapped{note}")

        if start + batch_size < len(todo):
            time.sleep(SLEEP_SECONDS)

    return mapping


# ----------------------------------------------------------------------------
# Summaries: a preview of what the dashboard's charts will show
# ----------------------------------------------------------------------------

def summarize_terms(label, counts, mapping, dropped_note="dropped"):
    mentions, variants, dropped = Counter(), Counter(), 0
    for raw, c in counts.items():
        canon = mapping.get(raw, raw)      # unmapped strings pass through unchanged
        if canon is None:
            dropped += c
            continue
        mentions[canon] += c
        variants[canon] += 1
    extra = f", {dropped} mentions {dropped_note}" if dropped else ""
    print(f"\n{label}: {len(counts)} raw strings -> {len(mentions)} after merging{extra}")
    print("  Top 12 (approx. postings; variants merged):")
    for canon, m in mentions.most_common(12):
        print(f"   {m:>4}  {canon}  ({variants[canon]} variants)")


def summarize_cities(counts, mapping):
    places, unmapped = Counter(), 0
    for raw, c in counts.items():
        v = mapping.get(raw)
        if v is None:
            unmapped += 1
            city, _, state = raw.partition("|")
            v = {"city": city, "state": state}
        places[f"{v['city']}, {v['state']}" if v["state"] else v["city"]] += c
    print(f"\nCities: {len(counts)} raw city/state pairs -> {len(places)} places"
          + (f" ({unmapped} left unmapped)" if unmapped else ""))
    print("  Top 12:")
    for p, c in places.most_common(12):
        print(f"   {c:>4}  {p}")


def summarize_titles(counts, mapping):
    kw, empty = Counter(), 0
    for title, c in counts.items():
        words = mapping.get(title)
        if words is None:
            continue
        if not words:
            empty += c
        for k in words:
            kw[k] += c
    print(f"\nTitle keywords: {len(counts)} unique titles -> {len(kw)} keywords"
          f" ({empty} postings have no role keyword)")
    print("  Top 15 (postings):")
    for k, c in kw.most_common(15):
        print(f"   {c:>4}  {k}")


def main():
    args = sys.argv[1:]
    flags = [a for a in args if a.startswith("--")]
    named = [a for a in args if not a.startswith("--")]
    if [f for f in flags if f != "--redo"]:
        raise SystemExit("Only one option exists: --redo (name the stages, e.g. --redo tools skills)")
    redo = "--redo" in flags
    if redo and not named:
        raise SystemExit("--redo needs stage names so nothing is redone by accident, "
                         "e.g.: python python/03_normalize.py --redo tools skills")
    wanted = named or STAGES
    bad = [w for w in wanted if w not in STAGES]
    if bad:
        raise SystemExit(f"Unknown stage(s): {', '.join(bad)}. Choose from: {', '.join(STAGES)}")

    postings = load_postings()
    print(f"{len(postings)} internship postings in the extracted data.")

    if "tools" in wanted:
        counts = count_terms(postings, "tools_technologies")
        mapping = run_stage("tools", [k for k, _ in counts.most_common()], redo=redo)
        summarize_terms("Tools", counts, mapping, "are not tools (counted as skills)")

    if "skills" in wanted:
        counts = count_terms(postings, "skills")
        # Strings the tools stage said are not tools (machine learning, flow cytometry...) are skills too.
        tool_map = load_mapping("tools")
        moved = 0
        for k, c in count_terms(postings, "tools_technologies").items():
            if k in tool_map and tool_map[k] is None:
                counts[k] += c
                moved += 1
        if moved:
            print(f"\n({moved} strings from the tools list are not tools; they are treated as skills.)")
        keys = [k for k, c in counts.most_common() if c >= MIN_SKILL_COUNT]
        singles = sum(1 for c in counts.values() if c < MIN_SKILL_COUNT)
        if singles:
            print(f"\n({singles} skills appear in too few postings; they pass through unmerged.)")
        mapping = run_stage("skills", keys, redo=redo)
        summarize_terms("Skills", counts, mapping)

    if "cities" in wanted:
        counts = count_cities(postings)
        mapping = run_stage("cities", [k for k, _ in counts.most_common()], redo=redo)
        summarize_cities(counts, mapping)

    if "titles" in wanted:
        counts = count_titles(postings)
        mapping = run_stage("titles", [k for k, _ in counts.most_common()], redo=redo)
        summarize_titles(counts, mapping)

    print(f"\nMappings are in {MAP_DIR}/. Open any file to fix a bad merge by hand; "
          "your edits are kept on rerun.")


if __name__ == "__main__":
    main()