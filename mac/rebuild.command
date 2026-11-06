#!/bin/bash
# Rebuild the page from the results already saved on this Mac and publish it. No sites are read and no API is used.
# Use after changing settings, or after trying a few organizations with one_org.command.
cd "$(dirname "$0")/.." || exit 1
# If this copy has no Python environment (e.g. it was opened from Documents), work in the real one in the home folder.
if [ ! -x .venv/bin/python ] && [ -x "$HOME/nyc-events/.venv/bin/python" ]; then cd "$HOME/nyc-events" || exit 1; fi
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
echo "Rebuilding the page..."
.venv/bin/python scripts/build.py || { echo "Build failed."; read -r -p "Press Return to close."; exit 1; }
if git remote get-url origin >/dev/null 2>&1; then
  git add docs/index.html docs/events.json docs/events.js sources.json
  if git diff --cached --quiet; then
    echo "Nothing changed, so nothing to publish."
  else
    git commit -q -m "Rebuild $(date +%Y-%m-%d\ %H:%M)"
    git pull -q --rebase origin main 2>/dev/null
    if git push -q origin main; then
      echo "Published. GitHub takes 1-3 minutes to show it. If you just published, wait for that to finish before publishing again."
    else
      echo "Publishing failed (GitHub sign-in?)."
    fi
  fi
fi
echo
read -r -p "Done. Press Return to close."
