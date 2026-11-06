"""Step 2b: fill in missing start times from each event's own page.

Many calendars list only a date and title; the time is on the event's detail page. For every extracted event that has
no start time but has its own page on the organization's own website, this opens that page (respecting robots.txt),
asks a small model for the show's start time, and writes it back into build/extracted/<slug>.json.

Safe by design: a time is only filled when the page states it; anything unclear stays "Time TBA".
Usage: times.py [slug ...]   (no slugs = every organization with gaps)
Needs ANTHROPIC_API_KEY. Uses the small model (TIMES_MODEL, default claude-haiku-5-5).
"""
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

OUT = ROOT / "build" / "extracted"
MODEL = os.environ.get("TIMES_MODEL", "claude-haiku-5-5")
MAX_PAGES_PER_ORG = int(os.environ.get("TIMES_MAX_PAGES", "60"))
PAGE_CHARS = 12_000

TOOL = {
    "name": "record_times",
    "description": "Record the start time of each requested performance date.",
    "input_schema": {
        "type": "object",
        "properties": {
            "times": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "YYYY-MM-DD, exactly as requested"},
                        "start_time": {"type": "string",
                                       "description": "Curtain/show start such as '7:30 PM'; empty if the page does not state it"},
                    },
                    "required": ["date", "start_time"],
                },
            }
        },
        "required": ["times"],
    },
}

PROMPT = """From the text of an event's own web page, find the START time of the performance on each requested date.

Event: {title}
Requested dates (YYYY-MM-DD): {dates}

Rules:
- Give the time the performance itself begins (curtain, "show", "start", "performance begins"). Do NOT give doors, box-office, \
reception, pre-show talk or lobby times unless it is the only time stated and the page calls it the start.
- If the event has an early and a late set, a matinee and an evening, or similar, use the one matching the event title \
and the date requested. If you cannot tell which applies, leave it empty.
- If the page gives a time pattern for several dates (e.g. a weekly schedule), use the entry for the requested date only.
- Use only what the page says; never guess. If a date is not on the page or no time is stated, return an empty start_time.
- Format as H:MM AM/PM, e.g. 7:30 PM or 2:00 PM.
You must answer by calling the record_times tool.

PAGE TEXT:
{text}"""

_TIME = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*$", re.I)
_TIME_NOTE = re.compile(r"\b(show)?times?\b|\bTBA\b", re.I)


def norm_time(s):
    """'7:30pm' / '7:30 p.m.' / '7 PM' -> '7:30 PM' / '7:00 PM'; anything else (noon, ranges, junk) -> ''."""
    m = _TIME.match(s or "")
    if not m:
        return ""
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).upper()
    if not (1 <= h <= 12 and 0 <= mi < 60):
        return ""
    return f"{h}:{mi:02d} {ap}M"


def reg_domain(url):
    host = (urlparse(url).hostname or "").lower()
    parts = host.split(".")
    return ".".join(parts[-3:]) if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "ac") else ".".join(parts[-2:])


def candidates(events, site_domains, listing_urls):
    """Group events needing a time by their own page: {url: [event, ...]}. Skips generic/listing pages."""
    groups = {}
    for e in events:
        if (e.get("start_time") or "").strip():
            continue
        u = (e.get("url") or "").split("#")[0]
        if not u or u.rstrip("/") in listing_urls:
            continue
        if reg_domain(u) not in site_domains:
            continue
        groups.setdefault(u, []).append(e)
    return groups


def ask_times(client, title, dates, text):
    """Returns {date: normalized time} for dates whose time the page states."""
    msg = client.messages.create(
        model=MODEL, max_tokens=1000, tools=[TOOL], tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": PROMPT.format(title=title, dates=", ".join(sorted(dates)), text=text[:PAGE_CHARS])}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            out = {}
            for t in block.input.get("times", []):
                v = norm_time(t.get("start_time", ""))
                if v and t.get("date") in dates:
                    out[t["date"]] = v
            return out
    return {}


def apply_times(events, url_times):
    """Write filled times into the events; returns how many were filled."""
    n = 0
    for e in events:
        u = (e.get("url") or "").split("#")[0]
        t = url_times.get(u, {}).get(e["date"])
        if t and not (e.get("start_time") or "").strip():
            e["start_time"] = t
            # drop note fragments that talk about the missing/ambiguous time; they no longer apply
            keep = [x.strip() for x in (e.get("notes") or "").split(";") if x.strip() and not _TIME_NOTE.search(x)]
            e["notes"] = "; ".join(keep)
            n += 1
    return n


def run_one(slug):
    import anthropic
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
    from scrape import UA, allowed
    from sources_io import load_sources

    src = next(s for s in load_sources()["sources"] if s["slug"] == slug)
    path = OUT / f"{slug}.json"
    data = json.loads(path.read_text())
    events = data["events"]
    domains = {reg_domain(w) for w in (src.get("websites") or [])}
    listing = {p["url"].split("{")[0].rstrip("/") for p in (src.get("pages") or [])}
    groups = candidates(events, domains, listing)
    if not groups or src.get("manual"):
        return "0/0"
    client = anthropic.Anthropic(max_retries=5)
    url_times, wanted = {}, sum(len(v) for v in groups.values())
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000}, user_agent=UA)
        page = ctx.new_page()
        for url, evs in list(groups.items())[:MAX_PAGES_PER_ORG]:
            if not allowed(url):
                continue
            try:
                try:
                    page.goto(url, wait_until="networkidle", timeout=25_000)
                except PWTimeout:
                    pass
                page.wait_for_timeout(800)
                text = page.evaluate("() => (document.querySelector('main') || document.body).innerText")
                got = ask_times(client, evs[0]["title"], {e["date"] for e in evs}, text)
                if got:
                    url_times[url] = got
            except Exception:
                continue  # one bad page never sinks the organization
        browser.close()
    filled = apply_times(events, url_times)
    if filled:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        tmp.replace(path)
    return f"{filled}/{wanted}"


def main(argv):
    from sources_io import load_sources

    only = set(argv)
    todo = []
    for src in load_sources()["sources"]:
        f = OUT / f"{src['slug']}.json"
        if not f.exists() or src.get("manual") or (only and src["slug"] not in only):
            continue
        domains = {reg_domain(w) for w in (src.get("websites") or [])}
        listing = {p["url"].split("{")[0].rstrip("/") for p in (src.get("pages") or [])}
        if candidates(json.loads(f.read_text()).get("events", []), domains, listing):
            todo.append(src["slug"])

    def job(slug):
        try:
            r = subprocess.run([sys.executable, __file__, "--one", slug], capture_output=True, text=True, timeout=420)
            last = (r.stdout.strip().splitlines() or ["0/0"])[-1]
            return f"{slug}: times filled {last}"
        except subprocess.TimeoutExpired:
            return f"{slug}: timed out"

    with cf.ThreadPoolExecutor(max_workers=int(os.environ.get("TIMES_WORKERS", "4"))) as ex:
        for line in ex.map(job, todo):
            print(line, flush=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--one":
        print(run_one(sys.argv[2]))
    else:
        sys.exit(main(sys.argv[1:]))
