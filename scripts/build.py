"""Step 3: merge, validate and publish docs/events.json.

- Keeps only events inside the date window, de-duplicates, sorts.
- If an org returns zero events or its extraction failed, keeps that org's previous
  events (still in the window) and records a warning, so the page never goes blank
  just because one site changed. The workflow turns warnings into a failed run so
  you get GitHub's failure email.
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRACTED = ROOT / "build" / "extracted"
DOCS = ROOT / "docs"
OUT = DOCS / "events.json"
DISCIPLINES = {"Music", "Theater", "Dance", "Opera", "Multidisciplinary", "Family/Education", "Other"}


def parse_time(s):
    m = re.match(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m", (s or "").lower())
    if not m:
        return None
    h, mi = int(m.group(1)) % 12, int(m.group(2) or 0)
    if m.group(3) == "p":
        h += 12
    return h * 60 + mi


def clean(ev, org, default_discipline, start, end):
    try:
        date = dt.date.fromisoformat(ev["date"])
    except Exception:
        return None
    if not (start <= date <= end):
        return None
    title = (ev.get("title") or "").strip()
    if not title:
        return None
    url = (ev.get("url") or "").strip()
    if url and not re.match(r"^https?://", url):
        url = ""
    disc = ev.get("discipline")
    if disc not in DISCIPLINES:
        disc = default_discipline
    # Fresh extractions use "start_time"; events carried over from events.json use "time".
    t = (ev.get("start_time") or ev.get("time") or "").strip()
    return {
        "date": date.isoformat(),
        "time": t,
        "title": title,
        "org": org,
        "location": (ev.get("location") or "").strip(),
        "description": (ev.get("description") or "").strip()[:300],
        "discipline": disc,
        "genre": (ev.get("genre") or "").strip(),
        "url": url,
        "notes": (ev.get("notes") or "").strip(),
        "_sort": (date.isoformat(), parse_time(t) if parse_time(t) is not None else 9999),
    }


def main():
    cfg = json.loads((ROOT / "sources.json").read_text())
    days = cfg.get("window_days", 31)
    today = dt.date.today()
    start, end = today, today + dt.timedelta(days=days)

    previous = {}
    if OUT.exists():
        try:
            for ev in json.loads(OUT.read_text()).get("events", []):
                previous.setdefault(ev["org"], []).append(ev)
        except Exception:
            pass

    events, warnings, counts, health = [], [], {}, {}
    for src in cfg["sources"]:
        org = src["name"]
        path = EXTRACTED / f"{src['slug']}.json"
        fresh = []
        if path.exists():
            raw = json.loads(path.read_text()).get("events", [])
            fresh = [e for e in (clean(x, org, src.get("default_discipline", "Other"), start, end) for x in raw) if e]
        if fresh:
            counts[org] = len(fresh)
            health[org] = {"status": "ok", "events": len(fresh)}
            events += fresh
            continue
        # No fresh data: fall back to last known events that are still in the window.
        kept = []
        for ev in previous.get(org, []):
            c = clean(ev, org, src.get("default_discipline", "Other"), start, end)
            if c:
                base = re.sub(r"(;\s*)?from previous update", "", c["notes"]).strip("; ").strip()
                c["notes"] = (base + "; " if base else "") + "from previous update"
                kept.append(c)
        counts[org] = len(kept)
        events += kept
        reason = "extraction failed or page did not render" if not path.exists() else "no events found"
        health[org] = {"status": "no_data" if not path.exists() else "empty", "events": len(kept), "reason": reason}
        warnings.append(
            f"{org}: {reason}; showing {len(kept)} events from the last successful update. "
            "The site layout may have changed."
        )

    seen, unique = set(), []
    for e in events:
        key = (e["org"], e["date"], e["time"].lower(), e["title"].lower())
        if key not in seen:
            seen.add(key)
            unique.append(e)
    unique.sort(key=lambda e: (e["_sort"], e["org"]))
    for e in unique:
        e.pop("_sort")

    DOCS.mkdir(exist_ok=True)
    payload = json.dumps(
        {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "orgs": [s["name"] for s in cfg["sources"]],
            "counts": counts,
            "health": health,
            "warnings": warnings,
            "events": unique,
        },
        ensure_ascii=False,
        indent=1,
    )
    OUT.write_text(payload)
    # Same data as a script file, so index.html also works when opened straight from disk
    # (browsers block fetch() of local files). "</" is escaped so no event text can close the tag.
    (DOCS / "events.js").write_text("window.EVENTS = " + payload.replace("</", "<\\/") + ";\n")
    print(json.dumps(counts, ensure_ascii=False))
    for w in warnings:
        print("WARNING:", w, file=sys.stderr)
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build" / "warnings.txt").write_text("\n".join(warnings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
