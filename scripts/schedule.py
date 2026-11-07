"""Step 2b: give every show in a run an entry on EVERY day it plays.

Calendars often list a theater production once ("Oct 15 - Dec 6") or only its first date. This step opens such a
show's own page (and, when needed, the ticketing/showtimes page it links to), finds the real performance schedule, and
expands it into one entry per performance. The model only READS the schedule off the page; Python does all date math.

Order of trust, per show:
  1. machine-readable dates on the page (JSON-LD "startDate" entries): no model needed
  2. the model reads the page text and returns explicit dates and/or a weekly pattern with run dates and dark days
Nothing is invented: if the page does not state a schedule, the original entry stays as it was.

Usage: schedule.py [slug ...]    (no slugs = every organization with run-type events)
Needs ANTHROPIC_API_KEY. Model: SCHEDULE_MODEL (default claude-sonnet-5-5).
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

OUT = ROOT / "build" / "extracted"
MODEL = os.environ.get("SCHEDULE_MODEL", "claude-sonnet-5-5")  # reading a schedule off a page needs the stronger model
MAX_SHOWS_PER_ORG = int(os.environ.get("SCHEDULE_MAX_SHOWS", "25"))
PAGE_CHARS = 16_000
MAX_DAYS = 200
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

RUN_NOTE = re.compile(r"\brun\b|runs\b|showtimes?|schedule|next occurrence|multiple|various|through|performances|preview", re.I)
TICKET_LINK = re.compile(r"ticket|showtime|calendar|performances|schedule|buy|book|select", re.I)

TOOL = {
    "name": "record_schedule",
    "description": "Record the performance schedule stated on the page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["stated", "partial", "not_stated"],
                       "description": "stated = the page gives dates/days and times; partial = only some; not_stated = no schedule"},
            "explicit": {"type": "array", "description": "Individual performances the page lists date by date",
                         "items": {"type": "object", "properties": {
                             "date": {"type": "string", "description": "YYYY-MM-DD"},
                             "time": {"type": "string", "description": "e.g. 7:30 PM; empty if not stated"}},
                             "required": ["date", "time"]}},
            "weekly": {"type": "array", "description": "Repeating weekly pattern, only if the page states it",
                       "items": {"type": "object", "properties": {
                           "days": {"type": "array", "items": {"type": "string", "enum": WEEKDAYS}},
                           "times": {"type": "array", "items": {"type": "string"}},
                           "from": {"type": "string", "description": "YYYY-MM-DD this pattern starts; empty = run start"},
                           "to": {"type": "string", "description": "YYYY-MM-DD this pattern ends; empty = run end"},
                           "assumed": {"type": "boolean", "description": "true if the page shows this pattern without saying which dates it covers and you are applying it to the whole run"}},
                           "required": ["days", "times"]}},
            "run_start": {"type": "string", "description": "First performance (include previews) YYYY-MM-DD; empty if unknown"},
            "run_end": {"type": "string", "description": "Last performance YYYY-MM-DD; empty if unknown"},
            "no_performance": {"type": "array", "items": {"type": "string"},
                               "description": "YYYY-MM-DD dates the page says there is NO performance (dark days, holidays)"},
            "year": {"type": "integer", "description": "Year the dates refer to"},
        },
        "required": ["status", "explicit", "weekly", "run_start", "run_end", "no_performance"],
    },
}

PROMPT = """Read the performance schedule for ONE production from its own web page.

Production: {title}
Today: {today}.  Only dates from {today} to {end} matter.

Rules:
- Use only what the page states. Never guess a day, time or date.
- If the page lists performances date by date (a calendar, a list of dates and times), return them in "explicit".
- If it states a repeating pattern ("Tue-Sat 7:30pm, Sat & Sun 2pm, no performance Mon") with run dates, return it in "weekly" with run_start and run_end. Use separate weekly entries when the pattern changes (previews vs. regular run; "Week 1" vs "Week 2"), setting from/to.
- Example: a block reading "ENTIRE RUN Oct 15-Nov 22 / PREVIEWS Oct 15-21 / Thu-Sat 7:00 PM / Sat & Sun 3:00 PM matinee / SPECIAL WEEKDAY MATINEES Wed Oct 28 11:00 AM" means: weekly = [Thu,Fri,Sat 7:00 PM; Sat,Sun 3:00 PM] for the whole run (assumed=true), run_start Oct 15, run_end Nov 22, plus an explicit Oct 28 11:00 AM. Previews are part of the run, not a separate schedule.
- A weekly pattern shown in the run/schedule block WITHOUT its own date range (even if it sits near a "previews" date) is the production's regular schedule: apply it to the whole run and set assumed=true. Only restrict it to a sub-range when the page explicitly says the pattern applies just to those dates. Extra one-off performances (special matinees) go in "explicit" in addition.
- List dark days or cancelled dates stated on the page in "no_performance".
- Previews, opening night and press performances are performances: include them.
- Ignore box-office hours, gallery hours, talkbacks, pre-show events and streaming-only dates.
- If dates give no year, use the one that makes the date fall on or after {today} within a year.
- If nothing usable is stated, status = "not_stated" and leave arrays empty.
You must answer by calling record_schedule.

PAGE TEXT:
{text}"""

_TIME = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*$", re.I)


def norm_time(s):
    m = _TIME.match(s or "")
    if not m:
        return ""
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).upper()
    return f"{h}:{mi:02d} {ap}M" if 1 <= h <= 12 and 0 <= mi < 60 else ""


def pdate(s):
    try:
        return dt.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def expand(sched, lo, hi, assumed_out=None):
    """Turn a model-read schedule into a sorted list of (date, time) inside [lo, hi]. Pure date math, no model."""
    if sched.get("status") == "not_stated":
        return []
    out = set()
    for x in sched.get("explicit") or []:
        d = pdate(x.get("date"))
        if d and lo <= d <= hi:
            out.add((d, norm_time(x.get("time"))))
    rs, re_ = pdate(sched.get("run_start")), pdate(sched.get("run_end"))
    for w in sched.get("weekly") or []:
        days = {WEEKDAYS.index(d) for d in (w.get("days") or []) if d in WEEKDAYS}
        times = [norm_time(t) for t in (w.get("times") or [])] or [""]
        a = pdate(w.get("from")) or rs
        b = pdate(w.get("to")) or re_
        if not (a and b) or b < a:
            continue  # a pattern without a stated start and end is not expanded: never run it forever
        d = max(a, lo)
        end = min(b, hi)
        if (end - d).days > MAX_DAYS:
            continue
        while d <= end:
            if d.weekday() in days:
                for t in times:
                    out.add((d, t))
                    if w.get("assumed") and assumed_out is not None:
                        assumed_out.add((d, t))
            d += dt.timedelta(days=1)
    dark = {pdate(x) for x in sched.get("no_performance") or []}
    return sorted(p for p in out if p[0] not in dark)


def jsonld_dates(html):
    """Performance dates from schema.org markup: [(date, time)], empty when fewer than 2 are present."""
    found = set()
    for m in re.finditer(r'"startDate"\s*:\s*"(\d{4}-\d{2}-\d{2})(?:T(\d{2}):(\d{2}))?', html):
        d, hh, mm = m.group(1), m.group(2), m.group(3)
        t = ""
        if hh is not None and not (hh == "00" and mm == "00"):
            h = int(hh)
            t = f"{(h % 12) or 12}:{mm} {'AM' if h < 12 else 'PM'}"
        found.add((pdate(d), t))
    return sorted(p for p in found if p[0]) if len(found) >= 2 else []


def is_run_event(e, listing_urls, site_domains, reg_domain):
    u = (e.get("url") or "").split("#")[0]
    if not u or u.rstrip("/") in listing_urls or reg_domain(u) not in site_domains:
        return False
    if e.get("discipline") not in ("Theater", "Opera", "Multidisciplinary", "Dance", "Family/Education", "Other", "Music"):
        return False
    return bool(RUN_NOTE.search(e.get("notes") or "")) or e.get("discipline") == "Theater"


ASSUMED_NOTE = "weekly schedule assumed from the show page; confirm before attending"


def build_events(proto, pairs, assumed=()):
    base = {k: v for k, v in proto.items() if k not in ("date", "start_time")}
    base["notes"] = "; ".join(x.strip() for x in (proto.get("notes") or "").split(";")
                              if x.strip() and not RUN_NOTE.search(x))
    out = []
    for d, t in pairs:
        e = dict(base, date=d.isoformat(), start_time=t)
        if (d, t) in assumed:
            e["notes"] = "; ".join(x for x in (e["notes"], ASSUMED_NOTE) if x)
        out.append(e)
    return out


def apply(events, groups_result):
    """groups_result: {url: [(date, time), ...]} -> new event list. Originals of expanded shows are replaced."""
    new, replaced = [], set()
    for e in events:
        u = (e.get("url") or "").split("#")[0]
        pairs = groups_result.get(u)
        if pairs and e.get("title") == groups_result.get(("title", u)):
            replaced.add(id(e))
    out, seen = [], set()
    for e in events:
        if id(e) in replaced:
            continue
        out.append(e)
        seen.add((e["title"], e["date"], e.get("start_time", "")))
    protos = {}
    for e in events:
        if id(e) in replaced:
            protos.setdefault((e.get("url") or "").split("#")[0], e)
    for u, proto in protos.items():
        for ne in build_events(proto, groups_result[u], groups_result.get(("assumed", u), ())):
            k = (ne["title"], ne["date"], ne["start_time"])
            if k not in seen:
                seen.add(k)
                out.append(ne)
    return out


def ask(client, title, today, end, text):
    msg = client.messages.create(
        model=MODEL, max_tokens=3000, tools=[TOOL], tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": PROMPT.format(title=title, today=today, end=end, text=text[:PAGE_CHARS])}])
    for b in msg.content:
        if b.type == "tool_use":
            return b.input
    return {"status": "not_stated", "explicit": [], "weekly": [], "run_start": "", "run_end": "", "no_performance": []}


def read_page(page, url, allowed, is_bot_wall, PWTimeout):
    """Rendered text plus raw html of a page; ('', '') if disallowed or walled."""
    if not allowed(url):
        return "", ""
    try:
        page.goto(url, wait_until="networkidle", timeout=25_000)
    except PWTimeout:
        pass
    except Exception:
        return "", ""
    page.wait_for_timeout(800)
    text = page.evaluate("() => (document.querySelector('main') || document.body).innerText")
    if is_bot_wall(text):
        return "", ""
    return text, page.content()


def ticket_links(page, base_domain, reg_domain):
    """Up to 2 same-site or ticketing-vendor links that likely show the performance list."""
    links = page.evaluate("""() => [...document.querySelectorAll('a[href]')].map(a => [a.innerText.trim().slice(0,60), a.href])""")
    vendors = ("ovationtix", "audienceview", "tessitura", "spektrix", "etix", "eventbrite", "tix", "universe.com",
               "ticketleap", "showclix", "secure.", "tickets.", "boxoffice.", "my.")
    out = []
    for text, href in links:
        if not href.startswith("http") or not TICKET_LINK.search(text):
            continue
        host = href.split("/")[2].lower()
        if reg_domain(href) == base_domain or any(v in host for v in vendors):
            if href not in out and "ovationtix" not in host:  # OvationTix sits behind a bot wall: never attempted
                out.append(href)
    return out[:2]


def run_one(slug):
    import anthropic
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
    from scrape import UA, allowed, is_bot_wall
    from sources_io import load_sources
    from times import reg_domain

    src = next(s for s in load_sources()["sources"] if s["slug"] == slug)
    path = OUT / f"{slug}.json"
    data = json.loads(path.read_text())
    events = data["events"]
    domains = {reg_domain(w) for w in (src.get("websites") or [])}
    listing = {p["url"].split("{")[0].rstrip("/") for p in (src.get("pages") or [])}
    cfg_days = load_sources().get("window_days", 60)
    today = dt.date.today()
    end = today + dt.timedelta(days=cfg_days)
    shows = {}
    per_url = {}
    for e in events:
        per_url.setdefault((e.get("url") or "").split("#")[0], []).append(e)
    for e in events:
        if is_run_event(e, listing, domains, reg_domain):
            u = e["url"].split("#")[0]
            group = per_url[u]
            # Already listed day by day (with times) by a real calendar: that is authoritative, leave it alone.
            if len(group) >= 3 and all((x.get("start_time") or "").strip() for x in group):
                continue
            shows.setdefault(u, e)
    if not shows or src.get("manual"):
        return "0/0"
    client = anthropic.Anthropic(max_retries=5)
    result, stats = {}, {"stated": 0, "none": 0}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000}, user_agent=UA)
        page = ctx.new_page()
        for url, proto in list(shows.items())[:MAX_SHOWS_PER_ORG]:
            try:
                text, html = read_page(page, url, allowed, is_bot_wall, PWTimeout)
                if not text:
                    stats["none"] += 1
                    continue
                pairs = jsonld_dates(html)
                if not pairs:
                    extra = ""
                    sched = ask(client, proto["title"], today, end, text)
                    if sched.get("status") != "stated":
                        for link in ticket_links(page, reg_domain(url), reg_domain):
                            t2, _ = read_page(page, link, allowed, is_bot_wall, PWTimeout)
                            if t2:
                                extra += "\n\n--- TICKETING PAGE ---\n" + t2
                        if extra:
                            sched = ask(client, proto["title"], today, end, text[:6000] + extra)
                    assumed = set()
                    pairs = expand(sched, today, end, assumed)
                    if assumed:
                        result[("assumed", url)] = assumed
                else:
                    pairs = [pr for pr in pairs if today <= pr[0] <= end]
                if pairs and len(pairs) > 1 or (pairs and pairs[0][0] != pdate(proto["date"])):
                    result[url] = pairs
                    result[("title", url)] = proto["title"]
                    stats["stated"] += 1
                else:
                    stats["none"] += 1
            except Exception:
                stats["none"] += 1  # one bad page never sinks the organization
        browser.close()
    if result:
        before = len(events)
        data["events"] = apply(events, result)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        tmp.replace(path)
        return f"{stats['stated']} shows expanded, {stats['none']} left as listed, events {before} -> {len(data['events'])}"
    return f"0 expanded, {stats['none']} left as listed"


def main(argv):
    from sources_io import load_sources
    from times import reg_domain

    only = set(argv)
    todo = []
    for src in load_sources()["sources"]:
        f = OUT / f"{src['slug']}.json"
        if not f.exists() or src.get("manual") or (only and src["slug"] not in only):
            continue
        domains = {reg_domain(w) for w in (src.get("websites") or [])}
        listing = {p["url"].split("{")[0].rstrip("/") for p in (src.get("pages") or [])}
        if any(is_run_event(e, listing, domains, reg_domain) for e in json.loads(f.read_text()).get("events", [])):  # run_one skips shows a calendar already lists day by day
            todo.append(src["slug"])

    def job(slug):
        try:
            r = subprocess.run([sys.executable, __file__, "--one", slug], capture_output=True, text=True, timeout=900)
            return f"{slug}: {(r.stdout.strip().splitlines() or ['no output'])[-1]}"
        except subprocess.TimeoutExpired:
            return f"{slug}: timed out"

    with cf.ThreadPoolExecutor(max_workers=int(os.environ.get("SCHEDULE_WORKERS", "4"))) as ex:
        for line in ex.map(job, todo):
            print(line, flush=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--one":
        print(run_one(sys.argv[2]))
    else:
        sys.exit(main(sys.argv[1:]))
