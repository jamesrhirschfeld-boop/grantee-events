#!/bin/bash
# Test only: loads each site in a browser on THIS Mac and reports how much it saw. Uses no API key.
# Usage: double-click (checks Carnegie Hall), or in Terminal: check_sites.command carnegie nyphil signature alja
cd "$HOME/nyc-events" || { echo "Run setup.command first."; read -n 1 -s -r; exit 1; }
SLUGS="${@:-carnegie}"
.venv/bin/python scripts/scrape.py $SLUGS
.venv/bin/python - $SLUGS <<'PY'
import json, sys, datetime as dt
sys.path.insert(0, "scripts")
from textutil import find_dates
for slug in sys.argv[1:]:
    try:
        r = json.load(open(f"build/raw/{slug}.json"))
    except Exception:
        print(f"{slug}: no result"); continue
    print(f"\n{slug}: {len(r['pages'])} page(s)")
    for e in r["errors"]: print("   error:", e[:200])
    for p in r["pages"]:
        try:
            ds = sorted(find_dates(p["text"], dt.date.today()))
            span = f"{ds[0]} to {ds[-1]}" if ds else "no dates recognised"
        except Exception:
            span = "?"
        print(f"   {len(p['text']):>7} chars  {span}  {p['url'][:70]}")
PY
read -n 1 -s -r -p "Press any key to close."
