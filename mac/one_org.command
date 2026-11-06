#!/bin/bash
# Try one or more organizations without a full run: read, extract, rebuild the page locally. Does NOT publish.
# Usage: double-click and type slugs (e.g. new-york-city-center), or: one_org.command slug [slug ...]
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export ANTHROPIC_API_KEY="$(security find-generic-password -a "$USER" -s nyc-grantee-events-anthropic -w 2>/dev/null)"
[ -z "$ANTHROPIC_API_KEY" ] && { echo "No API key in Keychain. Re-run setup.command."; read -r -p "Press Return"; exit 1; }
SLUGS="$*"
if [ -z "$SLUGS" ]; then read -r -p "Organization slug(s), e.g. new-york-city-center: " SLUGS; fi
PY=.venv/bin/python
$PY scripts/scrape.py $SLUGS
$PY scripts/extract.py $SLUGS
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
print(len(ev), "events")
for e in ev[:60]:
    print(" ", e.get("date"), e.get("start_time") or e.get("time") or "Time TBA", "|", e.get("title"), "|", e.get("location"))
PYEOF
done
echo
read -r -p "Done. Press Return to close."
