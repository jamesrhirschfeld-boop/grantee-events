#!/bin/bash
# Copy what the tool saw for one or more organizations into your Documents folder so it can be shared back.
# Usage: double-click and type slugs, or: share.command slug [slug ...]
cd "$(dirname "$0")/.." || exit 1
if [ ! -x .venv/bin/python ] && [ -x "$HOME/nyc-events/.venv/bin/python" ]; then cd "$HOME/nyc-events" || exit 1; fi
SLUGS="$*"
if [ -z "$SLUGS" ]; then read -r -p "Organization slug(s): " SLUGS; fi
DEST="$HOME/Documents/My Files/04 Work/Grantee Performance Listings/nyc-events/share"
mkdir -p "$DEST" || { echo "Could not create $DEST"; read -r -p "Press Return"; exit 1; }
for s in $SLUGS; do
  for kind in raw extracted; do
    [ -f "build/$kind/$s.json" ] && cp "build/$kind/$s.json" "$DEST/$s.$kind.json" && echo "copied $kind/$s"
  done
done
echo
read -r -p "Done. Press Return to close."
