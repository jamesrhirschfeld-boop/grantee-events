#!/bin/bash
# Try one or more organizations without a full run: read, extract, rebuild the page locally. Does NOT publish.
# Usage: double-click and type slugs (e.g. new-york-city-center), or: one_org.command slug [slug ...]
cd "$(dirname "$0")/.." || exit 1
# If this copy has no Python environment (e.g. it was opened from Documents), work in the real one in the home folder.
if [ ! -x .venv/bin/python ] && [ -x "$HOME/nyc-events/.venv/bin/python" ]; then cd "$HOME/nyc-events" || exit 1; fi
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export ANTHROPIC_API_KEY="$(security find-generic-password -a "$USER" -s nyc-grantee-events-anthropic -w 2>/dev/null)"
[ -z "$ANTHROPIC_API_KEY" ] && { echo "No API key in Keychain. Re-run setup.command."; read -r -p "Press Return"; exit 1; }
SLUGS="$*"
if [ -z "$SLUGS" ]; then read -r -p "Organization slug(s), e.g. new-york-city-center: " SLUGS; fi
PY=.venv/bin/python
# Stop early on a slug that matches nothing (the slug is not always the organization's name; see slugs.txt).
for s in $SLUGS; do
  if ! $PY -c "import json,sys; sys.exit(0 if any(x['slug']==sys.argv[1] for x in json.load(open('sources.json'))['sources']) else 1)" "$s" 2>/dev/null; then
    echo "No organization has the slug '$s'. Closest matches in slugs.txt:"
    grep -i -- "${s%%-*}" slugs.txt | head -8
    read -r -p "Press Return to close."; exit 1
  fi
done
$PY scripts/sync_orgs.py
$PY scripts/scrape.py $SLUGS
$PY scripts/extract.py $SLUGS
$PY scripts/schedule.py $SLUGS
$PY scripts/times.py $SLUGS
echo
for s in $SLUGS; do
  echo "--- $s"
  $PY - "$s" <<'PYEOF'
import json, sys
f = "build/extracted/%s.json" % sys.argv[1]
try:
    ev = json.load(open(f))["events"]
except Exception as e:
    print("no result:", e); sys.exit()
import collections
print(len(ev), "events")
for t, n in collections.Counter(e.get("title") for e in ev).most_common():
    print("  %3d  %s" % (n, t))
print()
for e in sorted(ev, key=lambda e: (e.get("date") or "", e.get("start_time") or "")):
    print(" ", e.get("date"), e.get("start_time") or e.get("time") or "Time TBA", "|", e.get("title"), "|", e.get("location"))
PYEOF
done
echo
read -r -p "Done. Press Return to close."
