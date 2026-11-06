#!/bin/bash
# Finds each grantee's calendar page automatically, starting from the website in sources.json.
# Safe to stop and re-run: it keeps what it has learned and skips organizations already checked.
cd "$HOME/nyc-events" || { echo "Run setup.command first."; read -n 1 -s -r; exit 1; }
export ANTHROPIC_API_KEY="$(security find-generic-password -a "$USER" -s nyc-grantee-events-anthropic -w 2>/dev/null)"
[ -z "$ANTHROPIC_API_KEY" ] && { echo "No API key in Keychain. Run setup.command."; read -n 1 -s -r; exit 1; }
caffeinate -i -w $$ &
echo "How many organizations? Press Return for a trial of 25 random ones, type a number, or 'all'."
echo "Or type specific names from sources.json (slugs), comma-separated, to check them again, e.g. a-better-jamaica"
read -r N
if [ -z "$N" ]; then ARGS="--sample 25"
elif [ "$N" = "all" ]; then ARGS=""
elif [[ "$N" =~ ^[0-9]+$ ]]; then ARGS="--limit $N"
else ARGS="--slugs $N"; fi
.venv/bin/python scripts/discover.py $ARGS | tee "logs/discover-$(date +%Y-%m-%d-%H%M).log"
echo
echo "Next: run the weekly update (mac/run_weekly.sh) to scrape and publish, or send me the summary above."
read -n 1 -s -r -p "Press any key to close."
