#!/bin/bash
# The weekly update: scrape -> extract with Claude -> build page -> publish. Run by launchd (or by hand).
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs build
LOG="logs/run-$(date +%Y-%m-%d).log"
exec > >(tee -a "$LOG") 2>&1
echo "=== Run started $(date)"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
caffeinate -i -w $$ &   # keep the Mac awake until this finishes

notify() { osascript -e "display notification \"$1\" with title \"Grantee events update\"" >/dev/null 2>&1; }

export ANTHROPIC_API_KEY="$(security find-generic-password -a "$USER" -s nyc-grantee-events-anthropic -w 2>/dev/null)"
if [ -z "$ANTHROPIC_API_KEY" ]; then echo "No API key in Keychain."; notify "No API key found. Re-run setup."; exit 1; fi

PY=.venv/bin/python
rm -f build/warnings.txt
$PY scripts/scrape.py   || echo "scrape reported an error"
$PY scripts/extract.py  || echo "extract reported an error"
$PY scripts/build.py    || { echo "build failed"; notify "Build failed. See logs in ~/nyc-events/logs"; exit 1; }

PUSH_NOTE=""
if git remote get-url origin >/dev/null 2>&1; then
  git add docs/events.json docs/events.js
  if git diff --cached --quiet; then
    echo "No changes to publish."
  else
    git commit -q -m "Update performances $(date +%Y-%m-%d)"
    git pull -q --rebase origin main 2>/dev/null
    git push -q origin main && echo "Published." || { echo "Push failed"; PUSH_NOTE=" Publishing failed (GitHub sign-in?)."; }
  fi
fi

WARN="$(cat build/warnings.txt 2>/dev/null)"
if [ -n "$WARN" ] || [ -n "$PUSH_NOTE" ]; then
  N=$(printf '%s\n' "$WARN" | grep -c .)
  notify "$N organization(s) need attention.$PUSH_NOTE See ~/nyc-events/build/warnings.txt"
  echo "=== Finished with warnings $(date)"
else
  notify "Updated. All organizations OK."
  echo "=== Finished OK $(date)"
fi
find logs -name 'run-*.log' -mtime +60 -delete 2>/dev/null
