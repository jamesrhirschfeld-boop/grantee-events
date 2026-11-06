#!/bin/bash
# Refresh the grantee list from a new Fluxx export. Keeps everything already learned about each organization.
cd "$HOME/nyc-events" || { echo "Run setup.command first."; read -n 1 -s -r; exit 1; }
echo "Drag the Fluxx .xlsx file onto this window, then press Return."
read -r F
F="${F%\"}"; F="${F#\"}"; F="${F//\\ / }"
.venv/bin/python scripts/import_fluxx.py "$F"
read -n 1 -s -r -p "Press any key to close."
