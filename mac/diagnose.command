#!/bin/bash
# Find out why sites could not be read (no API cost). Usage: double-click, or: diagnose.command [slug ...]
# Default: every organization whose last discovery said "unreachable" or "error". Takes a few minutes.
cd "$(dirname "$0")/.." || exit 1
# If this copy has no Python environment (e.g. it was opened from Documents), work in the real one in the home folder.
if [ ! -x .venv/bin/python ] && [ -x "$HOME/nyc-events/.venv/bin/python" ]; then cd "$HOME/nyc-events" || exit 1; fi
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
.venv/bin/python scripts/diagnose.py "$@"
# Leave a copy beside the project in Documents so it can be shared back easily
DOCS="$HOME/Documents/My Files/04 Work/Grantee Performance Listings/nyc-events"
if [ -d "$DOCS" ]; then cp build/diagnose.txt "$DOCS/diagnose.txt" 2>/dev/null && echo "Copy saved to: $DOCS/diagnose.txt"; fi
echo
read -r -p "Done. Press Return to close."
