"""
Multi-city internship/co-op pull from SerpApi Google Jobs, built to hit ~1,000
postings without blowing the monthly search quota.

Changes from v1:
- Multiple query phrasings per city (Google Jobs caps how deep one query paginates,
  so different phrasings surface different postings).
- Hard SEARCH_BUDGET: every API call is counted, including retries. Script stops
  before overspending.
- Stops globally once TARGET_TOTAL unique postings are reached.
- No more "skip city if any postings exist" - cities that stalled early get topped
  up by the new queries (dedup by job_id keeps it clean).
- Removed empty-page retries (each retry burns a search and a real end-of-results
  is empty anyway).
- Removed no_cache (cached repeats are free; fresh ones cost a search).
- Resumable: completed (city, query) pairs saved to data/pull_progress.json.
"""

import os
import json
import time
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("SERPAPI_KEY")
if not API_KEY:
    raise SystemExit("Missing SERPAPI_KEY. Check your .env file.")

SEARCH_URL = "https://serpapi.com/search"
OUTPUT_PATH = os.path.join("data", "raw_postings.json")
PROGRESS_PATH = os.path.join("data", "pull_progress.json")

# Verify any new location string against https://serpapi.com/locations.json?q=<city>
CITIES = {
    "Boston": "Boston,Massachusetts,United States",
    "New York": "New York,New York,United States",
    "San Francisco": "San Francisco,California,United States",
    "Chicago": "Chicago,Illinois,United States",
    "Washington DC": "Washington,District of Columbia,United States",
    "Seattle": "Seattle,Washington,United States",
    "Austin": "Austin,Texas,United States",
    "Los Angeles": "Los Angeles,California,United States",
    "Atlanta": "Atlanta,Georgia,United States",
    "Philadelphia": "Philadelphia,Pennsylvania,United States",
}

QUERIES = ["internship", "co-op", "summer 2027 intern"]

TARGET_TOTAL = 1000
SEARCH_BUDGET = 150          # max searches THIS RUN (you have ~186 left; keep headroom)
CITY_CAP = 100               # max postings per city, across all queries
MAX_PAGES_PER_COMBO = 10     # safety limit per city/query; CITY_CAP usually stops it first
MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 5
PAUSE_BETWEEN_PAGES_SECONDS = 2

searches_used = 0


class BudgetExhausted(Exception):
    pass


def fetch_page(params):
    global searches_used
    for attempt in range(1, MAX_RETRIES + 1):
        if searches_used >= SEARCH_BUDGET:
            raise BudgetExhausted
        searches_used += 1
        try:
            response = requests.get(SEARCH_URL, params=params, timeout=20)
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            print(f"    Attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
    return None


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    os.makedirs("data", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


def city_count(all_jobs, city):
    return sum(1 for j in all_jobs if j.get("search_city") == city)


def pull_combo(city, location, query, all_jobs, seen_ids):
    """Pull up to MAX_PAGES_PER_COMBO pages for one city + query."""
    params = {
        "engine": "google_jobs",
        "api_key": API_KEY,
        "q": query,
        "location": location,
        "lrad": 25,
        "hl": "en",
        "gl": "us",
    }
    added_total = 0
    for page in range(1, MAX_PAGES_PER_COMBO + 1):
        if len(all_jobs) >= TARGET_TOTAL or city_count(all_jobs, city) >= CITY_CAP:
            break
        data = fetch_page(params)
        if data is None:
            print("    Giving up on this combo after retries.")
            break

        jobs = data.get("jobs_results", [])
        if not jobs:
            print(f"    Page {page}: empty, end of results.")
            break

        new = 0
        for job in jobs:
            if city_count(all_jobs, city) >= CITY_CAP:
                break
            jid = job.get("job_id")
            if jid and jid in seen_ids:
                continue
            job["search_city"] = city
            job["search_query"] = query
            all_jobs.append(job)
            if jid:
                seen_ids.add(jid)
            new += 1
        added_total += new
        print(f"    Page {page}: {len(jobs)} results, {new} new "
              f"(total {len(all_jobs)}, searches used {searches_used}/{SEARCH_BUDGET})")
        save_json(OUTPUT_PATH, all_jobs)

        token = data.get("serpapi_pagination", {}).get("next_page_token")
        if not token:
            print("    No next page token, end of results.")
            break
        params["next_page_token"] = token

        # Pages that are almost all duplicates aren't worth the searches.
        if new == 0:
            print("    Page was all duplicates, moving on.")
            break
        time.sleep(PAUSE_BETWEEN_PAGES_SECONDS)
    return added_total


def main():
    all_jobs = load_json(OUTPUT_PATH, [])
    seen_ids = {j.get("job_id") for j in all_jobs if j.get("job_id")}
    done = set(tuple(x) for x in load_json(PROGRESS_PATH, []))
    print(f"Starting with {len(all_jobs)} postings. Budget this run: {SEARCH_BUDGET} searches.")

    try:
        # Query-major order: run one query across ALL cities before the next query,
        # so if the budget runs out, coverage is spread evenly across cities.
        for query in QUERIES:
            for city, location in CITIES.items():
                if len(all_jobs) >= TARGET_TOTAL:
                    raise StopIteration
                if (city, query) in done:
                    continue
                if city_count(all_jobs, city) >= CITY_CAP:
                    print(f"\n{city}: already at {CITY_CAP}, skipping '{query}'.")
                    continue
                print(f"\n=== {city} | '{query}' ===")
                pull_combo(city, location, query, all_jobs, seen_ids)
                done.add((city, query))
                save_json(PROGRESS_PATH, sorted(done))
    except BudgetExhausted:
        print("\nSearch budget reached, stopping. Re-run to continue (progress is saved).")
    except StopIteration:
        print(f"\nHit target of {TARGET_TOTAL} postings.")

    print(f"\n=== Done. {len(all_jobs)} postings, {searches_used} searches used this run. ===")
    for city in CITIES:
        print(f"  {city}: {sum(1 for j in all_jobs if j.get('search_city') == city)}")


if __name__ == "__main__":
    main()