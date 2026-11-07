#!/bin/bash
# Retry only the organizations whose sites failed to load (or whose check errored), read the ones that now work,
# and publish. The other organizations keep their results from the last full run.
cd "$(dirname "$0")/.." || exit 1
if [ ! -x .venv/bin/python ] && [ -x "$HOME/nyc-events/.venv/bin/python" ]; then cd "$HOME/nyc-events" || exit 1; fi
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export ANTHROPIC_API_KEY="$(security find-generic-password -a "$USER" -s nyc-grantee-events-anthropic -w 2>/dev/null)"
[ -z "$ANTHROPIC_API_KEY" ] && { echo "No API key in Keychain. Re-run setup.command."; read -r -p "Press Return"; exit 1; }
PY=.venv/bin/python
caffeinate -i -w $$ &

FAILED=$($PY - <<'PYEOF'
import sys; sys.path.insert(0, "scripts")
from sources_io import load_sources
print(",".join(s["slug"] for s in load_sources()["sources"]
      if s.get("active", True) and not s.get("manual") and not s.get("verified") and not s.get("pages")
      and (s.get("discovery") or {}).get("status") in ("unreachable", "error")))
PYEOF
)
if [ -z "$FAILED" ]; then echo "Nothing to retry: no organizations are marked unreachable or error."; read -r -p "Press Return"; exit 0; fi
echo "Retrying $(echo "$FAILED" | tr ',' '\n' | wc -l | tr -d ' ') organizations..."
$PY scripts/discover.py --slugs "$FAILED" --workers 4

# which of them now have a calendar page?
FOUND=$($PY - "$FAILED" <<'PYEOF'
import sys; sys.path.insert(0, "scripts")
from sources_io import load_sources
import os
want = set(sys.argv[1].split(","))
# the retried organizations that now have pages, plus any organization with a calendar set up but never read yet
print(" ".join(s["slug"] for s in load_sources()["sources"]
               if s.get("active", True) and (s.get("pages") or s.get("manual"))
               and (s["slug"] in want or not os.path.exists("build/extracted/%s.json" % s["slug"]))))
PYEOF
)
echo
echo "Now readable: $(echo $FOUND | wc -w | tr -d ' ') organizations"
if [ -n "$FOUND" ]; then
  $PY scripts/scrape.py $FOUND
  $PY scripts/extract.py $FOUND
  $PY scripts/schedule.py $FOUND
  $PY scripts/times.py $FOUND
fi

echo; echo "Rebuilding the page..."
$PY scripts/build.py || { echo "Build failed."; read -r -p "Press Return to close."; exit 1; }
cat build/coverage.txt 2>/dev/null | head -12
if git remote get-url origin >/dev/null 2>&1; then
  git add docs/index.html docs/events.json docs/events.js sources.json
  if git diff --cached --quiet; then echo "Nothing changed, nothing to publish."
  else
    git commit -q -m "Retry failed organizations $(date +%Y-%m-%d)"
    git pull -q --rebase origin main 2>/dev/null
    git push -q origin main && echo "Published. GitHub takes 1-3 minutes to show it." || echo "Publishing failed (GitHub sign-in?)."
  fi
fi
echo; read -r -p "Done. Press Return to close."
