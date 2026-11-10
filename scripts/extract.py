"""Step 2: ask Claude to turn each org's rendered page text into structured events.

Reads build/raw/<slug>.json, writes build/extracted/<slug>.json.
Needs ANTHROPIC_API_KEY in the environment. The key is never written to disk or to the page.
"""
import datetime as dt
import json
import re
import os
import sys
import time
from pathlib import Path

import anthropic

from sources_io import load_sources
from textutil import chunk_text

CHUNK_CHARS = 70_000
CACHE = None  # set below once ROOT is known
CACHE_MAX_AGE_DAYS = 10  # an unchanged page is not re-read for a week; after that it is read again regardless
CACHE_EXTRA_DAYS = 14   # extract a little past the page's window so a reused result still covers the following week
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "build" / "raw"
CACHE = ROOT / "build" / "cache"
OUT = ROOT / "build" / "extracted"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

DISCIPLINES = ["Music", "Theater", "Dance", "Opera", "Multidisciplinary", "Family/Education", "Other"]

TOOL = {
    "name": "record_events",
    "description": "Record every qualifying NYC performance found in the page text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "YYYY-MM-DD"},
                        "start_time": {"type": "string", "description": "e.g. '7:30 PM'; empty string if not stated"},
                        "title": {"type": "string", "description": "The event name only: no date, time or venue appended"},
                        "location": {"type": "string", "description": "Venue name, plus street address only if the page gives it"},
                        "description": {"type": "string", "description": "One or two sentences in your own words, max 220 characters"},
                        "discipline": {"type": "string", "enum": DISCIPLINES},
                        "genre": {"type": "string", "description": "Short style label, e.g. 'classical', 'Afro Latin jazz', 'new play'; may be empty"},
                        "url": {"type": "string", "description": "Performance page URL taken from the page text; empty if none"},
                        "notes": {"type": "string", "description": "Caveats: family concert, donor rehearsal, sold out, etc. (do not write 'free' here; use the free fields); may be empty"},
                        "free": {"type": "boolean", "description": "true ONLY if the page explicitly says this performance is free to attend. False if the price is not stated, is unclear, or any ticket is required to be bought."},
                        "free_evidence": {"type": "string", "description": "When free is true: the exact words from the page text that say it is free (copy them verbatim, max 120 characters). Empty otherwise."},
                    },
                    "required": ["date", "start_time", "title", "location", "description", "discipline", "url"],
                },
            }
        },
        "required": ["events"],
    },
}

INSTRUCTIONS = """You extract performance listings from the rendered text of an arts organization's own website.

Organization: {org}
Today: {today}
Window: {start} to {end} inclusive.

Rules:
- Include only LIVE PERFORMANCES open to the public that take place in New York City (the five boroughs) and fall inside the window: concerts, recitals, plays and musicals, dance, opera, readings and staged readings, cabaret, performance art, parades and festival performances, open rehearsals or previews that the public can attend, and film screenings only when live performers are part of the event.
- Exclude everything else, even if the organization offers it: talks, lectures, panels, conversations, book events, classes, workshops, masterclasses, camps, auditions, exhibitions and gallery openings, film-only screenings, guided tours, fundraisers and galas, networking, meetings, volunteer days, membership or ticket packages, wellness sessions, participatory classes even when they include live music, and online-only events. Exclude anything outside the five boroughs (tour dates elsewhere) and past dates. When unsure whether something is a performance or a talk or class, leave it out.
- One entry per performance (a separate entry for each date and showtime). If the page shows a recurring series (e.g. "Every Tuesday" or "Saturdays, listed dates"), expand it into individual dates only when the page text states the days, the start date and either an end date or an explicit list of dates; otherwise list the next occurrence only and say so in notes.
- Use only what the page says. If a time, venue or description is not stated, leave it empty or say so in notes. Never guess.
- Links appear inline as ⟨https://...⟩ immediately after the link text they belong to. Use the link that belongs to that performance's own page (not a generic ticketing or home page). If there is none, leave url empty.
- Write descriptions in your own words; do not copy marketing text.
- A donor rehearsal or open jam that is not a ticketed concert may be included if it is a performance open to the public or with reserved passes; say so in notes.
- Free admission: set free to true only when the page text explicitly says that this performance is free (for example "Free", "free admission", "free and open to the public", "no charge"; a free reservation or RSVP still counts). Copy the exact words into free_evidence. Do not infer free from a missing price, from "pay what you wish", "suggested donation", "free with museum admission", "free parking", a free event elsewhere on the page, or a general statement about the organization. If you are not certain, set free to false.
- If nothing qualifies, return an empty list.
You must return the result by calling the record_events tool (even if the list is empty). Do not reply with prose."""


def window(days):
    today = dt.date.today()
    return today, today, today + dt.timedelta(days=days)


def _squash(t):
    return re.sub(r"\s+", " ", re.sub(r"⟨[^⟩]*⟩", " ", t or "")).strip().lower()


def ground_free(events, pages_text):
    """A performance is tagged free only if the words the model quoted really appear in the page text.
    Anything else (no quote, quote not found, quote that does not say free) is treated as price unknown."""
    hay = _squash(pages_text)
    for e in events:
        ev = _squash(e.pop("free_evidence", ""))
        ok = bool(e.get("free")) and len(ev) >= 4 and ev in hay and re.search(r"\bfree\b|no charge|no cost|complimentary", ev)
        e["free"] = bool(ok)
    return events


def dedupe(events):
    """The same performance often appears on several pages (festival page, artist page, calendar). Keep one entry per
    title, date and start time: the one with the fullest location, a link and the longest description."""
    best = {}
    for e in events:
        k = ((e.get("title") or "").strip().lower(), e.get("date"), (e.get("start_time") or "").strip().lower())
        score = (len(e.get("location") or ""), bool(e.get("url")), len(e.get("description") or ""))
        if k not in best or score > best[k][0]:
            best[k] = (score, e)
    return [v[1] for v in best.values()]


def cache_key(raw, scope, strict):
    """Fingerprint of everything the answer depends on except the date: the page text, the org's rules and the prompt."""
    import hashlib
    h = hashlib.sha256()
    h.update(INSTRUCTIONS.encode())
    h.update(MODEL.encode())
    h.update(f"{scope}|{strict}|{raw.get('name')}".encode())
    for p in raw["pages"]:
        h.update(p["url"].encode())
        h.update(p["text"].encode())
    return h.hexdigest()


def cached_events(slug, key):
    f = CACHE / f"{slug}.json"
    try:
        c = json.loads(f.read_text())
        age = (dt.date.today() - dt.date.fromisoformat(c["saved"])).days
        if c["key"] == key and 0 <= age <= CACHE_MAX_AGE_DAYS:
            return c["events"]
    except Exception:
        pass
    return None


def save_cache(slug, key, events):
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / f"{slug}.json").write_text(json.dumps({"key": key, "saved": dt.date.today().isoformat(), "events": events},
                                                   ensure_ascii=False))


def extract_org(client, raw, days, scope="", strict=False):
    today, start, end = window(days)
    pages = "\n\n".join(f"=== PAGE {p['url']} ===\n{p['text']}" for p in raw["pages"])
    prompt = INSTRUCTIONS.format(org=raw["name"], today=today, start=start, end=end)
    if scope:
        prompt += (
            "\n\nScope rule for this organization (it narrows the rules above and takes precedence over them): "
            + scope
        )
    if strict:
        prompt += (
            "\n\nThis organization does much more than perform (talks, classes, exhibitions, community programs). "
            "Be strict: list an entry only if it is clearly a live performance."
        )
    # Big calendars (a venue with hundreds of listings) are read in overlapping parts so no single
    # response runs out of room. Duplicates from the overlap are removed in build.py.
    chunks = chunk_text(pages, CHUNK_CHARS)
    events = []
    for i, chunk in enumerate(chunks, 1):
        note = ""
        if len(chunks) > 1:
            note = (
                f"\n\n(This is part {i} of {len(chunks)} of the page text. Parts overlap slightly: include a "
                "listing only if it appears complete in this part.)"
            )
        events += extract_chunk(client, prompt + note, chunk)
    return dedupe(ground_free(events, pages))


class Truncated(Exception):
    """The model ran out of output room: the text holds more events than fit in one answer."""


def split_text(text):
    """Cut text in two at a page boundary near the middle (or at a line break if it is a single page)."""
    marks = [m.start() for m in re.finditer(r"^=== PAGE ", text, re.M)][1:]
    if marks:
        cut = min(marks, key=lambda i: abs(i - len(text) // 2))
        if 0 < cut < len(text):
            return text[:cut], text[cut:]
    mid = text.rfind("\n", 0, len(text) // 2 + 1)
    mid = mid if mid > 0 else len(text) // 2
    return text[:mid], text[mid:]


def extract_chunk(client, prompt, pages):
    """Extract from text; if the answer would not fit, split the text in half and combine both answers."""
    try:
        return extract_once(client, prompt, pages)
    except Truncated:
        if len(pages) < 1500:
            raise RuntimeError("output truncated (max_tokens) even on a small piece of text")
        a, b = split_text(pages)
        note = "\n\n(This is one piece of a longer text: list only the events that are complete in this piece.)"
        return extract_chunk(client, prompt + note, a) + extract_chunk(client, prompt + note, b)


def extract_once(client, prompt, pages):
    last = None
    for attempt in range(3):
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=16_000,
                tools=[TOOL],
                tool_choice={"type": "auto"},  # forcing a specific tool is not supported by this model
                messages=[{"role": "user", "content": f"{prompt}\n\n{pages}"}],
            )
            if msg.stop_reason == "max_tokens":
                raise Truncated()
            for block in msg.content:
                if block.type == "tool_use":
                    return block.input.get("events", [])
            raise RuntimeError("no tool_use block in response")
        except Truncated:
            raise  # retrying the same text cannot help; the caller splits it
        except Exception as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"extraction failed after retries: {last}")


def main():
    import concurrent.futures as cf

    cfg = load_sources()
    days = cfg.get("window_days", 60)
    OUT.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[1:])  # optional: extract just these organizations (slugs) and leave the rest alone
    if not only:
        for f in OUT.glob("*.json"):
            f.unlink()  # never reuse a stale result from an earlier run
    client = anthropic.Anthropic(max_retries=5)  # reads ANTHROPIC_API_KEY
    todo = []
    for src in cfg["sources"]:
        if not src.get("active", True) or not (src.get("pages") or src.get("manual")):
            continue
        if only and src["slug"] not in only:
            continue
        path = RAW / f"{src['slug']}.json"
        if not path.exists():
            print(f"{src['slug']}: no raw page data, skipping")
            continue
        raw = json.loads(path.read_text())
        if not raw["pages"]:
            print(f"{src['slug']}: render produced no pages, skipping")
            continue
        todo.append((src, raw))

    def job(item):
        src, raw = item
        try:
            scope, strict = src.get("scope", ""), not src.get("performing_arts_majority", True)
            use_cache = not os.environ.get("EXTRACT_NO_CACHE") and not only
            key = cache_key(raw, scope, strict)
            events = cached_events(src["slug"], key) if use_cache else None
            note = " (unchanged page: reused last result, no API call)" if events is not None else ""
            if events is None:
                events = extract_org(client, raw, days + CACHE_EXTRA_DAYS, scope, strict)
                save_cache(src["slug"], key, events)
            (OUT / f"{src['slug']}.json").write_text(
                json.dumps({"slug": src["slug"], "events": events}, ensure_ascii=False, indent=1))
            return f"{src['slug']}: {len(events)} events{note}"
        except Exception as e:
            return f"{src['slug']}: FAILED {e}"

    with cf.ThreadPoolExecutor(max_workers=int(os.environ.get("EXTRACT_WORKERS", "4"))) as ex:
        for line in ex.map(job, todo):
            print(line, file=sys.stderr if "FAILED" in line else sys.stdout, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
