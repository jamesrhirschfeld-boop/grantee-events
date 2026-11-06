"""Step 0 (occasional): find each organization's calendar page from its homepage.

For every active organization that has no calendar pages yet:
  1. open the homepage and collect its links;
  2. ask Claude (a small, cheap model) which links most likely list upcoming performances;
  3. open those candidates and COUNT the distinct dates in the next window. A page only counts as a calendar
     if it really shows several upcoming dates, so a wrong guess is caught without trusting the model.
Result is saved in sources.json: `pages` plus `discovery: {status, checked, note}`.
Statuses: ok | no_events_found | no_calendar_found | blocked_robots | blocked_bot | unreachable | error

Usage: discover.py [--sample N] [--limit N] [--slugs a,b] [--retry] [--workers N]
Needs ANTHROPIC_API_KEY. Re-checks an organization only after 60 days unless --retry.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

from sources_io import load_sources, read_raw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
OUT = ROOT / "build" / "discovery"
MODEL = os.environ.get("DISCOVER_MODEL", "claude-haiku-5-5")
RECHECK_DAYS = 60
MIN_DATES = 3

KEYWORDS = re.compile(r"event|calendar|what.?s.?on|whats-on|season|perform|show|tickets|schedule|program|concert|"
                      r"live|theat|stage|music|dance|visit|see|agenda|festival", re.I)
BOT_WALL = re.compile(r"access denied|captcha|verify you are (a )?human|just a moment|attention required|"
                      r"enable javascript and cookies|request blocked|pardon our interruption", re.I)

TOOL = {
    "name": "choose_pages",
    "description": "Pick the pages most likely to list this organization's upcoming performances.",
    "input_schema": {
        "type": "object",
        "properties": {
            "urls": {"type": "array", "items": {"type": "string"}, "maxItems": 4,
                     "description": "Up to 4 URLs copied exactly from the link list, best first."},
            "reason": {"type": "string"},
        },
        "required": ["urls"],
    },
}


TICKETING = ("eventbrite.", "ovationtix.", "spektrix.", "audienceview", "tickettailor.", "universe.com", "tessitura",
             "etix.com", "showclix.", "ticketleap.", "seatengine.", "squadup.", "givebutter.", "theatermania.", "my.")


def reg_domain(url):
    host = urlparse(url).netloc.lower().split(":")[0]
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def same_site_or_ticketing(url, home_domains):
    """Accept a calendar on the organization's own domain, or on a ticketing platform. Anything else (another
    organization's site that the homepage happens to link to) is dropped: it is a different source."""
    d = reg_domain(url)
    host = urlparse(url).netloc.lower()
    return d in home_domains or any(t in host for t in TICKETING)


def choose(client, name, home, links):
    lines = "\n".join(f"{t[:70]} | {h}" for t, h in links)
    prompt = (
        f"Organization: {name}\nHomepage: {home}\n\nBelow are links found on its homepage as 'text | url'.\n"
        "Choose up to 4 URLs that most likely open a LIST or CALENDAR of upcoming performances "
        "(concerts, plays, dance, etc.): pages named like events, calendar, what's on, season, performances, "
        "shows, schedule, tickets-and-events. Prefer one page that lists many dates over single-event pages. "
        "Avoid single event pages, past/archive pages, donation, membership, jobs, news, press, login and "
        "social-media links. Only use URLs from the list. Call choose_pages.\n\n" + lines
    )
    msg = client.messages.create(model=MODEL, max_tokens=1000, tools=[TOOL],
                                 messages=[{"role": "user", "content": prompt}])
    for b in msg.content:
        if b.type == "tool_use":
            return [u for u in b.input.get("urls", []) if isinstance(u, str)][:4]
    return []


def discover_one(slug):
    """Child-process entry: explore one organization, write build/discovery/<slug>.json."""
    import anthropic
    from playwright.sync_api import sync_playwright

    import scrape
    from textutil import find_dates

    cfg = load_sources()
    src = next(s for s in cfg["sources"] if s["slug"] == slug)
    days = cfg.get("window_days", 60)
    today = dt.date.today()
    end = today + dt.timedelta(days=days)
    client = anthropic.Anthropic(max_retries=5)
    res = {"slug": slug, "status": "error", "note": "", "pages": []}

    def finish():
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{slug}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1400, "height": 1000}, user_agent=scrape.UA)
            page = ctx.new_page()
            candidates, blocked_robots, bot_wall, loaded_any = [], 0, 0, False
            for home in src["websites"]:
                if not scrape.allowed(home):
                    blocked_robots += 1
                    continue
                try:
                    scrape.load(page, home)
                    loaded_any = True
                    final = page.url
                    text = page.evaluate("() => document.body.innerText")[:3000]
                    if len(text) < 300 and BOT_WALL.search(text):
                        bot_wall += 1
                        continue
                    raw = page.evaluate("() => Array.from(document.querySelectorAll('a[href]')).map(a => "
                                        "[(a.innerText||a.getAttribute('aria-label')||'').trim().replace(/\\s+/g,' '), a.href])")
                except Exception:
                    continue
                seen, links = set(), []
                for t, h in raw:
                    h = urljoin(final, h).split("#")[0]
                    if not h.startswith("http") or h in seen or re.search(r"\.(pdf|jpg|png|zip)$", h, re.I):
                        continue
                    seen.add(h)
                    links.append((t, h))
                links.sort(key=lambda th: (not KEYWORDS.search(th[0] + " " + th[1]),))
                chosen = choose(client, src["name"], final, links[:150])
                valid = {h for _, h in links}
                home_domains = {reg_domain(w) for w in src["websites"]} | {reg_domain(final)}
                candidates += [final] + [u for u in chosen if u in valid and u != final
                                         and same_site_or_ticketing(u, home_domains)]
            if not candidates:
                res["status"] = ("blocked_robots" if blocked_robots else "blocked_bot" if bot_wall
                                 else "unreachable")
                res["note"] = {"blocked_robots": "robots.txt asks automated tools to stay out",
                               "blocked_bot": "bot protection page instead of the site",
                               "unreachable": "homepage did not load"}[res["status"]]
                finish()
                return
            scored = []
            for url in list(dict.fromkeys(candidates))[:5]:
                if not scrape.allowed(url):
                    blocked_robots += 1
                    continue
                try:
                    text = scrape.grab(page, {"url": url}, end)
                except Exception:
                    continue
                ds = [d for d in find_dates(text, today) if today <= d <= end]
                scored.append((len(set(ds)), len(find_dates(text, today)), url))
            browser.close()
        scored.sort(reverse=True)
        good = [s for s in scored if s[0] >= MIN_DATES]
        if good:
            res["status"] = "ok"
            res["pages"] = [{"url": u} for _, _, u in good[:2]]
            res["note"] = "; ".join(f"{n} dates in window at {u}" for n, _, u in good[:2])
        elif scored and scored[0][1] > 0:
            res["status"] = "no_events_found"
            res["pages"] = [{"url": scored[0][2]}]
            res["note"] = f"best page has {scored[0][0]} dates in the window ({scored[0][1]} overall): {scored[0][2]}"
        elif blocked_robots and not scored:
            res["status"] = "blocked_robots"
            res["note"] = "robots.txt asks automated tools to stay out of the event pages"
        else:
            res["status"] = "no_calendar_found"
            res["note"] = "no page with dates found; may need a manual calendar URL"
    except Exception as e:
        res["status"], res["note"] = "error", str(e)[:300]
    finish()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--slugs")
    ap.add_argument("--retry", action="store_true")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("SCRAPE_WORKERS", "4")))
    a = ap.parse_args()

    cfg_path = ROOT / "sources.json"
    cfg = load_sources()
    today = dt.date.today()

    def eligible(s):
        if not s.get("active", True) or s.get("manual") or s.get("verified"):
            return False
        if a.slugs:  # asked for by name: check again even if pages were found before
            return s["slug"] in a.slugs.split(",")
        if s.get("pages"):
            return False
        if a.retry:
            return True
        c = s.get("discovery", {}).get("checked")
        return not c or (today - dt.date.fromisoformat(c)).days >= RECHECK_DAYS

    todo = [s for s in cfg["sources"] if eligible(s)]
    if a.sample:
        todo = random.Random(1).sample(todo, min(a.sample, len(todo)))
    if a.limit:
        todo = todo[: a.limit]
    print(f"Discovering calendars for {len(todo)} organization(s)...", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)

    def job(s):
        f = OUT / f"{s['slug']}.json"
        f.unlink(missing_ok=True)
        try:
            subprocess.run([sys.executable, str(Path(__file__)), "--one", s["slug"]], timeout=360,
                           capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            pass
        r = json.loads(f.read_text()) if f.exists() else {"status": "error", "note": "timed out or crashed", "pages": []}
        return s["slug"], r

    counts = {}
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for slug, r in ex.map(job, todo):
            # Re-read before each write so a concurrent edit is not lost.
            cur = read_raw()
            tgt = next(s for s in cur["sources"] if s["slug"] == slug)
            tgt["discovery"] = {"status": r["status"], "checked": today.isoformat(), "note": r.get("note", "")}
            if r["status"] in ("ok", "no_events_found") and r.get("pages"):
                tgt["pages"] = r["pages"]
            else:
                tgt.pop("pages", None)  # a failed re-check must not leave an older, possibly wrong, page behind
            cfg_path.write_text(json.dumps(cur, ensure_ascii=False, indent=1) + "\n")
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            print(f"  {r['status']:<18} {tgt['name'][:42]:<42} {r.get('note','')[:90]}", flush=True)
    print("\nSummary:", json.dumps(counts))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--one":
        discover_one(sys.argv[2])
    else:
        main()
