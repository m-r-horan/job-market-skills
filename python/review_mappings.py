"""
Review helper: writes data/mappings/review.txt so you can check the mappings without
opening the big JSON files. It only reads; it changes nothing.

It shows the parts where a bad call is most likely:
  - tools/skills the model dropped as "not real" (did it drop anything valid?)
  - the names that absorbed the most variants (did it merge things that should stay apart?)
  - cities where the model changed the input
  - every title keyword, with counts (are any too generic to be useful?)

Run from the project root:  python python/review_mappings.py
To fix anything, edit the JSON file in data/mappings/ and rerun 04_build_tables.py.
"""

import os
from collections import Counter, defaultdict

from common import MAP_DIR, load_mapping

TOP_GROUPS = 25


def main():
    lines = []

    def add(text=""):
        lines.append(text)

    for stage, label in (("tools", "TOOLS"), ("skills", "SKILLS")):
        mapping = load_mapping(stage)
        if not mapping:
            add(f"{label}: no mapping file yet (run 03_normalize.py)\n")
            continue

        dropped = sorted(k for k, v in mapping.items() if v is None)
        add("=" * 72)
        what = "that are not tools (counted as skills instead)" if stage == "tools" else "dropped entirely"
        add(f"{label}: {len(dropped)} strings {what}")
        add("=" * 72)
        add("; ".join(dropped) if dropped else "(none)")
        add()

        groups = defaultdict(list)
        for raw, canon in mapping.items():
            if canon is not None:
                groups[canon].append(raw)
        biggest = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:TOP_GROUPS]
        add(f"{label}: the {TOP_GROUPS} names that absorbed the most variants (look for over-merging)")
        add("-" * 72)
        for canon, raws in biggest:
            add(f"{canon}  <-  {len(raws)} variants")
            add("    " + "; ".join(sorted(raws)))
        add()

    cities = load_mapping("cities")
    add("=" * 72)
    add("CITIES: entries where the model changed what the posting said")
    add("=" * 72)
    if cities:
        changed = [(k, v) for k, v in cities.items() if k != f"{v['city']}|{v['state']}"]
        add(f"{len(changed)} of {len(cities)} entries changed")
        for k, v in sorted(changed):
            add(f"  {k!r}  ->  {v['city']}, {v['state']}")
    else:
        add("no mapping file yet (run 03_normalize.py)")
    add()

    titles = load_mapping("titles")
    add("=" * 72)
    add("TITLE KEYWORDS: every keyword, by number of unique titles it appears in")
    add("=" * 72)
    if titles:
        counts = Counter(k for v in titles.values() for k in v)
        add(f"{len(counts)} keywords; {sum(1 for v in titles.values() if not v)} titles have none")
        for kw, c in counts.most_common():
            add(f"  {c:>4}  {kw}")
    else:
        add("no mapping file yet (run 03_normalize.py)")

    out_path = os.path.join(MAP_DIR, "review.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Wrote {out_path}  ({len(lines)} lines). Open it in any text editor.")


if __name__ == "__main__":
    main()