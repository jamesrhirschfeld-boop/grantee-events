"""Step 2: ask Claude to turn each org's rendered page text into structured events.

Reads build/raw/<slug>.json, writes build/extracted/<slug>.json.
Needs ANTHROPIC_API_KEY in the environment. The key is never written to disk or to the page.
"""
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import anthropic

from textutil import chunk_text

CHUNK_CHARS = 70_000
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "build" / "raw"
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
                        "title": {"type": "string"},
                        "location": {"type": "string", "description": "Venue name, plus street address only if the page gives it"},
                        "description": {"type": "string", "description": "One or two sentences in your own words, max 220 characters"},
                        "discipline": {"type": "string", "enum": DISCIPLINES},
                        "genre": {"type": "string", "description": "Short style label, e.g. 'classical', 'Afro Latin jazz', 'new play'; may be empty"},
                        "url": {"type": "string", "description": "Performance page URL taken from the page text; empty if none"},
                        "notes": {"type": "string", "description": "Caveats: free event, family concert, donor rehearsal, sold out, etc.; may be empty"},
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
- Include only performances (concerts, plays, dance, readings, parades or open rehearsals open to the public) that take place in New York City (the five boroughs) and fall inside the window. Skip everything else, including tour dates elsewhere, classes, guided building tours, fundraisers, ticket packages and past dates.
- One entry per performance (a separate entry for each date and showtime). If the page shows a recurring series (e.g. "Every Tuesday" or "Saturdays, listed dates"), expand it into individual dates only when the page text states the days, the start date and either an end date or an explicit list of dates; otherwise list the next occurrence only and say so in notes.
- Use only what the page says. If a time, venue or description is not stated, leave it empty or say so in notes. Never guess.
- Links appear inline as ⟨https://...⟩ immediately after the link text they belong to. Use the link that belongs to that performance's own page (not a generic ticketing or home page). If there is none, leave url empty.
- Write descriptions in your own words; do not copy marketing text.
- If a donor rehearsal, workshop or open jam is not a ticketed concert, still include it if it is open to the public or has reserved passes, and say so in notes.
- If nothing qualifies, return an empty list.
You must return the result by calling the record_events tool (even if the list is empty). Do not reply with prose."""


def window(days):
    today = dt.date.today()
    return today, today, today + dt.timedelta(days=days)


def extract_org(client, raw, days, scope=""):
    today, start, end = window(days)
    pages = "\n\n".join(f"=== PAGE {p['url']} ===\n{p['text']}" for p in raw["pages"])
    prompt = INSTRUCTIONS.format(org=raw["name"], today=today, start=start, end=end)
    if scope:
        prompt += (
            "\n\nScope rule for this organization (it narrows the rules above and takes precedence over them): "
            + scope
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
    return events


def extract_chunk(client, prompt, pages):
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
                raise RuntimeError("output truncated (max_tokens); too many events in one call")
            for block in msg.content:
                if block.type == "tool_use":
                    return block.input.get("events", [])
            raise RuntimeError("no tool_use block in response")
        except Exception as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"extraction failed after retries: {last}")


def main():
    cfg = json.loads((ROOT / "sources.json").read_text())
    days = cfg.get("window_days", 31)
    OUT.mkdir(parents=True, exist_ok=True)
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    for src in cfg["sources"]:
        path = RAW / f"{src['slug']}.json"
        out = OUT / f"{src['slug']}.json"
        if out.exists():
            out.unlink()  # never reuse a stale result from an earlier run
        if not path.exists():
            print(f"{src['slug']}: no raw page data, skipping")
            continue
        raw = json.loads(path.read_text())
        if not raw["pages"]:
            print(f"{src['slug']}: render produced no pages, skipping")
            continue
        try:
            events = extract_org(client, raw, days, src.get("scope", ""))
            out.write_text(json.dumps({"slug": src["slug"], "events": events}, ensure_ascii=False, indent=1))
            print(f"{src['slug']}: {len(events)} events")
        except Exception as e:
            print(f"{src['slug']}: FAILED {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
