#!/bin/bash
# One-time setup for running the weekly NYC grantee events update on this Mac.
# Double-click this file (or run it in Terminal). Safe to run again.
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/nyc-events"
LABEL="org.nyc-grantee-events.weekly"
KEYSVC="nyc-grantee-events-anthropic"
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31m%s\033[0m\n' "$*"; read -n 1 -s -r -p "Press any key to close."; exit 1; }

say "1/7  Checking Python"
if ! python3 --version >/dev/null 2>&1; then
  die "Python 3 isn't installed yet. macOS should be showing a window offering to install the Command Line Tools: click Install, wait for it to finish, then double-click this file again."
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)' || die "Python 3.9 or newer is needed (found $(python3 --version))."
git --version >/dev/null 2>&1 || die "Git isn't available yet. Install the Command Line Tools when macOS offers, then run this again."

say "2/7  Copying the project to $DEST"
# The weekly job runs from your home folder, because macOS blocks background jobs from reading Documents.
mkdir -p "$DEST"
if [ "$SRC" != "$DEST" ]; then
  if [ ! -f "$DEST/sources.json" ]; then
    rsync -a --exclude build --exclude .venv --exclude logs "$SRC/" "$DEST/"
  else
    # Re-run: update the code, but keep your sources.json and the latest published data.
    rsync -a "$SRC/scripts/" "$DEST/scripts/"
    rsync -a "$SRC/mac/" "$DEST/mac/"
    cp "$SRC/docs/index.html" "$DEST/docs/index.html"
    cp "$SRC/requirements.txt" "$DEST/requirements.txt"
  fi
fi
# One-time switch to the full grantee list (schema 2). After that, your copy is kept, because it holds what discovery learns.
if ! grep -q '"schema": 2' "$DEST/sources.json" 2>/dev/null && grep -q '"schema": 2' "$SRC/sources.json" 2>/dev/null; then
  cp "$DEST/sources.json" "$DEST/sources.json.before-full-list" 2>/dev/null
  cp "$SRC/sources.json" "$DEST/sources.json"
  echo "Loaded the full grantee list into sources.json."
fi
chmod +x "$DEST"/mac/*.sh "$DEST"/mac/*.command 2>/dev/null
cd "$DEST" || die "Could not enter $DEST"

say "3/7  Installing Python packages and the browser (a few minutes the first time)"
[ -d .venv ] || python3 -m venv .venv || die "Could not create the Python environment."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt || die "Package install failed."
.venv/bin/python -m playwright install chromium || die "Browser install failed."

say "4/7  Anthropic API key"
if security find-generic-password -a "$USER" -s "$KEYSVC" -w >/dev/null 2>&1; then
  echo "A key is already saved in your Mac's Keychain. Keeping it."
  echo "(To replace it, run:  security delete-generic-password -a \"\$USER\" -s $KEYSVC   and run this setup again.)"
else
  echo "Paste your Anthropic API key and press Return. Nothing will show as you paste."
  read -s -r -p "Key: " KEY; echo
  [ -n "$KEY" ] || die "No key entered."
  security add-generic-password -a "$USER" -s "$KEYSVC" -w "$KEY" -U || die "Could not save the key to Keychain."
  unset KEY
  echo "Saved in your Keychain (not in any file)."
fi
export ANTHROPIC_API_KEY="$(security find-generic-password -a "$USER" -s "$KEYSVC" -w)"
.venv/bin/python - <<'PY' || die "The API key did not work. Check it at console.anthropic.com and try again."
import os, anthropic
c = anthropic.Anthropic()
c.messages.create(model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"), max_tokens=8,
                  messages=[{"role": "user", "content": "Reply with OK"}])
print("API key works.")
PY

say "5/7  GitHub (where the shared page is published)"
# GitHub Actions is replaced by this Mac, so the cloud workflow is set aside to avoid duplicate/failing runs.
if [ -f .github/workflows/update.yml ]; then
  mkdir -p extras && mv .github/workflows/update.yml extras/update.yml.cloud-version
  rmdir .github/workflows .github 2>/dev/null
fi
printf 'build/\n.venv/\nlogs/\n__pycache__/\n.DS_Store\n*.xlsx\n*.csv\nsources.json.before-full-list\n' > .gitignore
if [ ! -d .git ]; then git init -q -b main; fi
git config user.name  >/dev/null 2>&1 || git config user.name  "Grantee events updater"
git config user.email >/dev/null 2>&1 || git config user.email "events-updater@users.noreply.github.com"
if ! git remote get-url origin >/dev/null 2>&1; then
  echo "Create an EMPTY repository on github.com (no README), then paste its web address here."
  echo "Example: https://github.com/yourname/grantee-events"
  read -r -p "Repository address (or press Return to skip publishing for now): " REPO
  [ -n "$REPO" ] && git remote add origin "$REPO"
fi
if git remote get-url origin >/dev/null 2>&1; then
  git add -A && git commit -q -m "Initial project" 2>/dev/null
  echo "Pushing once now. If a sign-in window or prompt appears, sign in with GitHub"
  echo "(use a Personal Access Token as the password if asked); the Mac remembers it afterward."
  git push -u origin main || echo "Push did not finish. Fix the sign-in, then run: cd ~/nyc-events && git push -u origin main"
else
  echo "Skipped. The page will still be built locally in $DEST/docs."
fi

say "6/7  Scheduling every Monday at 7:17 AM"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$DEST/logs"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DEST/mac/run_weekly.sh</string></array>
  <key>StartCalendarInterval</key><dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>17</integer></dict>
  <key>StandardOutPath</key><string>$DEST/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$DEST/logs/launchd.err</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST" && echo "Scheduled. If the Mac is asleep Monday morning, it runs when the Mac next wakes." || echo "Could not register the schedule."

say "7/7  First run"
read -r -p "Run the first full update now? Takes a few minutes and uses a little API credit. (y/n) " GO
if [ "$GO" = "y" ] || [ "$GO" = "Y" ]; then bash "$DEST/mac/run_weekly.sh"; fi

say "Done. Project: $DEST   Logs: $DEST/logs"
read -n 1 -s -r -p "Press any key to close."
