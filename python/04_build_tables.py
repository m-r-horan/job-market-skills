"""
Step 4: build the two tables Power BI loads. No API calls; runs in seconds; safe to rerun.

Reads (never modifies):
  data/raw_postings.json    title, company, location, links
  data/extracted.jsonl      the LLM's fields for each posting
  data/mappings/*.json      the clean-name lookups from 03_normalize.py

Writes to data/clean/:
  postings.csv          one row per posting  (filters, pay, company, title, link)
  posting_terms.csv     one row per posting + skill / tool / title keyword
  validation_sample.csv random postings to hand-check extraction accuracy (not for Power BI)

If you hand-fix a mapping file, just rerun this script and the CSVs update.
A string with no mapping keeps its original wording. In tools.json a null means "not a tool"
(a method or concept), so it is counted as a skill; a null in skills.json drops the string.
"""

import os
import csv
import random
from collections import Counter

from common import (CLEAN_DIR, FUNCTION_OPTIONS, INDUSTRY_OPTIONS, MAP_FILES, clean_text,
                    load_extracted, load_mapping, load_raw_by_id, parse_location)

# Labels the model made up that clearly belong under an allowed one.
LABEL_ALIASES = {"industry": {"Automotive": "Manufacturing"}}

HOURS_PER_YEAR = 2080       # 40 hrs x 52 weeks
HOURS_PER_WEEK = 40
MIN_HOURLY = 7.25           # US federal minimum wage; outside this range we flag pay as suspicious
MAX_HOURLY = 150
VALIDATION_SAMPLE_SIZE = 25
SEED = 1

POSTING_COLUMNS = [
    "job_id", "title", "company", "url",
    "place", "city", "state", "location", "multi_location",
    "industry", "function", "degree_level",
    "pay_text", "pay_min", "pay_max", "pay_period",
    "pay_hourly_min", "pay_hourly_max", "pay_flag",
]
TERM_COLUMNS = ["job_id", "type", "term", "raw"]


def to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def get_url(job):
    apply_opts = job.get("apply_options") or []
    return (
        job.get("source_link")
        or (apply_opts[0].get("link") if apply_opts else None)
        or job.get("share_link")
        or ""
    )


def hourly_pay(comp):
    """Returns (hourly_min, hourly_max, flag).

    The stated min and max are each converted to hourly separately; no midpoint.
    Flag is ok / no_pay / stipend / out_of_range.
    """
    if not isinstance(comp, dict):
        return None, None, "no_pay"

    lo = to_float(comp.get("min"))
    hi = to_float(comp.get("max"))
    period = comp.get("period")

    # A stated pay of $0 (e.g. "$0.00 - $0.00") means the posting has no real figure.
    if lo is not None and lo <= 0:
        lo = None
    if hi is not None and hi <= 0:
        hi = None

    if lo is None and hi is None:
        return None, None, "no_pay"
    if lo is None:
        lo = hi
    if hi is None:
        hi = lo

    if period == "hour":
        factor = 1.0
    elif period == "year":
        factor = 1 / HOURS_PER_YEAR
    elif period == "month":
        factor = 12 / HOURS_PER_YEAR
    elif period == "week":
        factor = 1 / HOURS_PER_WEEK
    elif period == "stipend":
        return None, None, "stipend"   # lump sum, can't be turned into an hourly rate
    else:
        return None, None, "no_pay"

    h_lo = round(lo * factor, 2)
    h_hi = round(hi * factor, 2)

    if h_lo < MIN_HOURLY or h_hi > MAX_HOURLY:
        return h_lo, h_hi, "out_of_range"
    return h_lo, h_hi, "ok"


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig so Excel/Power BI read it cleanly
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def add_terms(rows, job_id, kind, items, mapping, stats):
    """Appends one row per distinct clean term. `items` are (lookup_key, display_text) pairs."""
    seen = set()
    for key, raw_text in items:
        if key in mapping:
            term = mapping[key]
            if term is None:                 # the model said this isn't a real tool/skill
                stats[kind]["dropped"] += 1
                continue
        else:
            term = raw_text                  # no mapping yet: keep the original wording
            stats[kind]["unmapped"].add(key)
        if term.lower() in seen:
            continue
        seen.add(term.lower())
        rows.append({"job_id": job_id, "type": kind, "term": term, "raw": raw_text})


def main():
    os.makedirs(CLEAN_DIR, exist_ok=True)

    raw_by_id = load_raw_by_id()
    extracted, bad_lines, dupe_ids = load_extracted()
    maps = {stage: load_mapping(stage) for stage in MAP_FILES}

    print(f"Raw postings: {len(raw_by_id)}")
    print(f"Extracted postings: {len(extracted)} "
          f"(skipped {bad_lines} unreadable lines, {dupe_ids} duplicate job_ids)")
    for stage, mapping in maps.items():
        if not mapping:
            print(f"Note: data/mappings/{MAP_FILES[stage]} is missing or empty "
                  f"(run 03_normalize.py). Using original wording for {stage}.")

    dropped = Counter()
    off_list = {"industry": Counter(), "function": Counter()}
    stats = {k: {"dropped": 0, "unmapped": set()} for k in ("skill", "tool", "title_keyword")}
    unmapped_cities = 0
    moved_mentions = 0
    seen_title_company = set()
    posting_rows, term_rows = [], []
    raw_terms = {}      # job_id -> (skills, tools) exactly as extracted, for the validation sheet

    for job_id, ex in extracted.items():
        raw = raw_by_id.get(job_id)
        if raw is None:
            dropped["no matching raw posting"] += 1
            continue

        title = clean_text(raw.get("title", ""))
        company = clean_text(raw.get("company_name", ""))

        if ex.get("is_internship") is False:
            dropped["not an internship"] += 1
            continue
        if title.lower().startswith("test"):
            dropped["test/junk posting"] += 1
            continue

        # Same job often appears on several job boards under different job_ids.
        key = (title.lower(), company.lower())
        if key in seen_title_company:
            dropped["duplicate title+company"] += 1
            continue
        seen_title_company.add(key)

        # The model sometimes invents a label that isn't on the allowed list.
        values = {}
        for field, allowed in (("industry", INDUSTRY_OPTIONS), ("function", FUNCTION_OPTIONS)):
            v = ex.get(field)
            v = LABEL_ALIASES.get(field, {}).get(v, v)
            if v not in allowed:
                off_list[field][str(v)] += 1
                v = "Other"
            values[field] = v

        # Place: the posting's own location text, then the cleaned name from cities.json.
        location_text = clean_text(raw.get("location", ""))
        city, state, multi_location = parse_location(location_text)
        mapped = maps["cities"].get(f"{city}|{state}")
        if mapped:
            city, state = mapped["city"], mapped["state"]
        else:
            unmapped_cities += 1
        place = f"{city}, {state}" if state else city

        comp = ex.get("compensation")
        comp = comp if isinstance(comp, dict) else {}
        hourly_min, hourly_max, pay_flag = hourly_pay(comp)

        posting_rows.append({
            "job_id": job_id,
            "title": title,
            "company": company,
            "url": get_url(raw),
            "place": place,
            "city": city,
            "state": state,
            "location": location_text,
            "multi_location": multi_location,
            "industry": values["industry"],
            "function": values["function"],
            "degree_level": ex.get("degree_level", ""),
            "pay_text": comp.get("text", "Not stated"),
            "pay_min": comp.get("min"),
            "pay_max": comp.get("max"),
            "pay_period": comp.get("period"),
            "pay_hourly_min": hourly_min,
            "pay_hourly_max": hourly_max,
            "pay_flag": pay_flag,
        })

        # Terms: skills, tools, and the keywords from this posting's title.
        skills = [(clean_text(s).lower(), clean_text(s)) for s in ex.get("skills") or [] if clean_text(s)]
        tools = [(clean_text(t).lower(), clean_text(t)) for t in ex.get("tools_technologies") or [] if clean_text(t)]
        raw_terms[job_id] = ([r for _, r in skills], [r for _, r in tools])

        # In tools.json a null means "not a tool" (a method or concept), so it is counted as a skill.
        tools_kept = [(k, r) for k, r in tools if maps["tools"].get(k, "") is not None]
        tools_as_skills = [(k, r) for k, r in tools if k in maps["tools"] and maps["tools"][k] is None]
        moved_mentions += len(tools_as_skills)
        add_terms(term_rows, job_id, "skill", skills + tools_as_skills, maps["skills"], stats)
        add_terms(term_rows, job_id, "tool", tools_kept, maps["tools"], stats)
        # A title with no entry in title_keywords.json gets no keyword rows (nothing to invent).
        seen_kw = set()
        for k in maps["titles"].get(title, []):
            if k.lower() not in seen_kw:
                seen_kw.add(k.lower())
                term_rows.append({"job_id": job_id, "type": "title_keyword", "term": k, "raw": title})

    # ---- Write outputs ----
    write_csv(os.path.join(CLEAN_DIR, "postings.csv"), POSTING_COLUMNS, posting_rows)
    write_csv(os.path.join(CLEAN_DIR, "posting_terms.csv"), TERM_COLUMNS, term_rows)

    # ---- Validation sheet: judge the extraction itself, so it shows the model's original wording ----
    random.seed(SEED)
    sample = random.sample(posting_rows, min(VALIDATION_SAMPLE_SIZE, len(posting_rows)))
    validation_rows = []
    for p in sample:
        sk, tl = raw_terms[p["job_id"]]
        validation_rows.append({
            "job_id": p["job_id"], "title": p["title"], "company": p["company"], "url": p["url"],
            "industry": p["industry"],
            "skills_extracted": "; ".join(sk), "tools_extracted": "; ".join(tl),
            "skills_correct": "", "skills_wrong": "", "skills_missed": "", "notes": "",
        })
    if validation_rows:
        write_csv(os.path.join(CLEAN_DIR, "validation_sample.csv"),
                  list(validation_rows[0].keys()), validation_rows)

    # ---- Summary ----
    n = len(posting_rows)
    if n == 0:
        raise SystemExit("No postings kept. Check data/extracted.jsonl and data/raw_postings.json.")

    print(f"\nKept {n} postings. Dropped:")
    for reason, count in dropped.most_common():
        print(f"  {count:>5}  {reason}")

    for field in ("industry", "function"):
        if off_list[field]:
            items = ", ".join(f"{k} ({v})" for k, v in off_list[field].most_common())
            print(f"\n{field.capitalize()} labels not on the allowed list, set to Other: {items}")

    flags = Counter(r["pay_flag"] for r in posting_rows)
    print("\nPay coverage:")
    for flag in ("ok", "out_of_range", "stipend", "no_pay"):
        print(f"  {flag:<13} {flags.get(flag, 0):>5}  ({flags.get(flag, 0) / n:.0%})")
    if flags.get("out_of_range"):
        print("  -> Check the out_of_range rows in postings.csv; they may be extraction errors.")

    print("\nTop 10 places:")
    for place, count in Counter(r["place"] for r in posting_rows).most_common(10):
        print(f"  {count:>5}  {place}")
    print(f"  ({sum(1 for r in posting_rows if r['multi_location'])} postings list more than one "
          f"location; only the first is used)")

    print("\nPostings by industry:")
    for industry, count in Counter(r["industry"] for r in posting_rows).most_common():
        print(f"  {count:>5}  {industry}")

    for kind, label in (("skill", "skills"), ("tool", "tools"), ("title_keyword", "title keywords")):
        per_posting = Counter(r["term"].lower() for r in term_rows if r["type"] == kind)
        display = {}
        for r in term_rows:
            if r["type"] == kind:
                display.setdefault(r["term"].lower(), r["term"])
        print(f"\nTop 15 {label} (% of postings; {len(per_posting)} distinct):")
        for term, count in per_posting.most_common(15):
            print(f"  {count / n:>5.0%}  {display[term]}")

    print("\nMapping coverage:")
    for kind, label in (("skill", "skills"), ("tool", "tools")):
        print(f"  {label}: {len(stats[kind]['unmapped'])} strings had no mapping (kept original wording), "
              f"{stats[kind]['dropped']} mentions dropped by the mapping")
    print(f"  cities: {unmapped_cities} postings had no city mapping (kept the posting's own text)")
    print(f"  tools list: {moved_mentions} mentions were not tools and are counted as skills")
    if not maps["titles"]:
        print("  title keywords: none (run 03_normalize.py to create them)")

    stale = [f for f in ("posting_skills.csv", "posting_tools.csv") if os.path.exists(os.path.join(CLEAN_DIR, f))]
    if stale:
        print(f"\nNote: {', '.join(stale)} from the older version are no longer used and can be deleted.")

    print(f"\nFiles written to {CLEAN_DIR}/  (Power BI loads postings.csv and posting_terms.csv)")


if __name__ == "__main__":
    main()