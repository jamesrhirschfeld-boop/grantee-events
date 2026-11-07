#!/bin/bash
# Opens your organization list (my_orgs.csv). Add rows (Organization, Website, Discipline), save, close it,
# then come back here: new organizations are added, their calendars are found, and slugs.txt is refreshed.
cd "$(dirname "$0")/.." || exit 1
if [ ! -x .venv/bin/python ] && [ -x "$HOME/nyc-events/.venv/bin/python" ]; then cd "$HOME/nyc-events" || exit 1; fi
[ -x .venv/bin/python ] || { echo "Run setup.command first."; read -n 1 -s -r; exit 1; }
open -a TextEdit my_orgs.csv 2>/dev/null || open my_orgs.csv
read -n 1 -s -r -p "Save my_orgs.csv, then press any key here to add the new organizations..."
echo
.venv/bin/python scripts/sync_orgs.py
.venv/bin/python scripts/discover.py --limit 20 | tail -15
cp slugs.txt "$HOME/Documents/My Files/04 Work/Grantee Performance Listings/nyc-events/slugs.txt" 2>/dev/null
echo "Done. New organizations appear on the page after the next weekly run (or run one_org.command <slug>)."
read -n 1 -s -r -p "Press any key to close."
