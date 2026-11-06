# NYC Grantee Performances

A shared web page listing upcoming NYC performances by a chosen set of arts organizations, built only from
each organization's own website and refreshed every Monday.

## How it works

1. `scripts/scrape.py` opens each calendar page in headless Chromium (so script-driven calendars render),
   clicks "show more" where needed, and saves the page text with each link kept next to its own title.
2. `scripts/extract.py` sends that text to the Claude API, which returns structured events
   (date, time, title, org, location, description, discipline, URL).
3. `scripts/build.py` keeps NYC events inside the next 31 days, removes duplicates, and writes `docs/events.json`.
   If an org returns nothing (site changed, page failed), it keeps that org's last good events and records a warning.
4. `docs/index.html` is a static page with search and filters. It reads `docs/events.js` (the same data as
   `events.json`, written as a script so the page also works when you double-click the HTML file; browsers
   block pages opened from disk from fetching a separate JSON file). Keep `index.html` and `events.js` together.
5. `.github/workflows/update.yml` runs steps 1 to 3 every Monday and commits the new `events.json`.
   If any org fell back to old data, the run is marked failed after publishing, and GitHub emails you.

## One-time setup

1. Create a GitHub repository and push this folder to it.
2. In the repository: **Settings → Secrets and variables → Actions → New repository secret**.
   Name: `ANTHROPIC_API_KEY`. Value: your key. (Never put the key in a file or in the page.)
3. **Settings → Pages**: Source = *Deploy from a branch*, Branch = `main`, Folder = `/docs`.
4. **Actions → Update performances → Run workflow** to test the first run.

Only people with write access to the repository can change the page or the schedule.
Note that GitHub Pages sites are public to anyone with the link, and the repo must be public unless your
GitHub plan supports Pages on private repos. The page contains only information already on the orgs' sites.

## Adding a grantee

Add an entry to `sources.json`:

- `slug`, `name`, `default_discipline`
- `scope` (optional): a plain-English rule for what counts, applied by Claude, e.g. Carnegie Hall's
  "include only events labelled 'Carnegie Hall Presents'; exclude rentals and tours".
- `pages`: one or more calendar URLs. Most need nothing else:
  - "Show more" / "Load more" buttons are found and clicked automatically, and infinite-scroll lists are
    scrolled, stopping once the entries pass the date window. (`click_text`, `max_clicks`, `max_scrolls` or
    `"expand": false` override this for a stubborn page.)
  - `{ym}` in the URL expands to the current and next month (for month-at-a-time calendars).
  - `follow_links_matching` and `max_follow`: also open detail pages whose URL contains that text
    (useful when a calendar only says "Every Tuesday" and the exact times are on the event page).

Pages disallowed by the site's `robots.txt` are skipped and reported. Large calendars are read in
overlapping batches so one Claude call never has to hold hundreds of listings.

`events.json` also carries a `health` section (per org: ok, empty or no_data, with the event count), which is
the place to look when a run fails and to see at a glance which orgs need attention.

Then run the workflow. Check the output for the new org before relying on it.

## Running locally

```
pip install -r requirements.txt
python -m playwright install chromium
export ANTHROPIC_API_KEY=...   # your key
python scripts/scrape.py && python scripts/extract.py && python scripts/build.py
python -m http.server -d docs 8000   # then open http://localhost:8000
```

## Running weekly from a Mac (instead of GitHub Actions)

A Mac in your office or home avoids the bot blocking that cloud servers often hit.

1. Double-click `mac/setup.command` once. It installs everything, saves your API key in the Mac's Keychain,
   connects a GitHub repository for publishing, and schedules a run every Monday at 7:17 AM.
   (If the Mac is asleep then, the run happens when it next wakes.) It works from `~/nyc-events`.
2. `mac/check_sites.command` tests how the sites load on this Mac without using the API.
3. `mac/run_weekly.sh` is the weekly job; run it by hand any time. Logs are in `~/nyc-events/logs`.
   A macOS notification appears after each run, and again if an organization needs attention.
4. The cloud workflow is set aside in `extras/` so the two don't both run.

## Cost and limits

Each weekly run makes one Claude call per organization, with page text as input. For a few orgs this is
typically a few cents. Cost grows with the number of orgs and the size of their calendar pages.
`CLAUDE_MODEL` can override the model (default `claude-sonnet-5-5`).

## Known limits

- A site that blocks automated browsers, or that requires a login, will fall back to old data and trigger a warning.
- Claude can misread an unusual page. The page shows each event's own link so entries can be spot-checked,
  and the extraction prompt forbids guessing, but a periodic spot check is still worthwhile.
- Recurring series are expanded only when the page states the days, start and an end or explicit dates.
