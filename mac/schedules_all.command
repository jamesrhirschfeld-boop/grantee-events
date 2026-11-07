#!/bin/bash
# Expand theater runs to every performance day for ALL organizations, using the results already saved on this Mac
# (no sites re-scraped), then rebuild and publish the page. Uses the API (a few dollars) and takes 15-40 minutes.
cd "$(dirname "$0")/.." || exit 1
if [ ! -x .venv/bin/python ] && [ -x "$HOME/nyc-events/.venv/bin/python" ]; then cd "$HOME/nyc-events" || exit 1; fi
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export ANTHROPIC_API_KEY="$(security find-generic-password -a "$USER" -s nyc-grantee-events-anthropic -w 2>/dev/null)"
[ -z "$ANTHROPIC_API_KEY" ] && { echo "No API key in Keychain. Re-run setup.command."; read -r -p "Press Return"; exit 1; }
mkdir -p logs
.venv/bin/python scripts/schedule.py "$@" | tee "logs/schedule-$(date +%Y-%m-%d-%H%M).log"
echo
echo "Rebuilding and publishing..."
"$(dirname "$0")/rebuild.command"
