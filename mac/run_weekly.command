#!/bin/bash
# Double-click to run the full weekly update now (scrape, extract, build, publish). Takes a while.
cd "$(dirname "$0")" || exit 1
bash ./run_weekly.sh
echo
read -n 1 -s -r -p "Done. Press any key to close."
