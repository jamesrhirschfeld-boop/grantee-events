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
RAW = ROOT / "build" / "raw"
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


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def main():
    cfg = json.loads((ROOT / "sources.json").read_text())
    days = cfg.get("window_days", 31)
    today = dt.date.today()
    start, end = today, today + dt.timedelta(days=days)
    sources = [s for s in cfg["sources"] if s.get("active", True)]

    previous = {}
    for ev in read_json(OUT, {}).get("events", []):
        previous.setdefault(ev["org"], []).append(ev)

    events, warnings, counts, health = [], [], {}, {}
    cov = {"no_events": [], "no_source": [], "blocked": [], "failed": []}
    for src in sources:
        org, default = src["name"], src.get("default_discipline", "Other")
        site = (src.get("websites") or [""])[0]
        prev = list(previous.get(org, []))
        for old_name in src.get("previous_names", []):
            prev += previous.get(old_name, [])

        if not src.get("pages"):
            reason = src.get("discovery", {}).get("status", "not_checked")
            health[org] = {"status": "no_source", "events": 0, "reason": reason}
            cov["no_source"].append({"name": org, "site": site, "reason": reason,
                                     "note": src.get("discovery", {}).get("note", "")})
            continue

        path = EXTRACTED / f"{src['slug']}.json"
        raw = read_json(RAW / f"{src['slug']}.json", None)
        extracted = path.exists()
        fresh = []
        if extracted:
            fresh = [e for e in (clean(x, org, default, start, end)
                                 for x in json.loads(path.read_text()).get("events", [])) if e]
        if fresh:
            counts[org] = len(fresh)
            health[org] = {"status": "ok", "events": len(fresh)}
            events += fresh
            continue

        # No fresh events. Last known events that are still in the window are kept, with a note.
        kept = []
        for ev in prev:
            c = clean(ev, org, default, start, end)
            if c:
                base = re.sub(r"(;\s*)?from previous update", "", c["notes"]).strip("; ").strip()
                c["notes"] = (base + "; " if base else "") + "from previous update"
                kept.append(c)
        robots = bool(raw) and not raw.get("pages") and any("robots.txt" in e for e in raw.get("errors", []))

        if extracted and not kept:
            # The page was read and truly lists nothing in the window. Normal for small organizations.
            health[org] = {"status": "empty", "events": 0, "reason": "no performances listed in the window"}
            cov["no_events"].append({"name": org, "site": site})
            continue

        if robots:
            status = "blocked"
            reason = "its website asks automated tools not to read it (robots.txt)"
        elif extracted:
            status, reason = "empty", "the page was read but no performances were found"
        else:
            status, reason = "failed", "the page did not load or could not be read this week"
        counts[org] = len(kept)
        events += kept
        health[org] = {"status": status, "events": len(kept), "reason": reason}
        if kept or src.get("verified"):
            tail = "" if robots else " The site layout may have changed."
            warnings.append(f"{org}: {reason}; showing {len(kept)} events from the last successful update.{tail}")
        else:
            cov["blocked" if robots else "failed"].append({"name": org, "site": site, "reason": reason})

    seen, unique = set(), []
    for e in events:
        key = (e["org"], e["date"], e["time"].lower(), e["title"].lower())
        if key not in seen:
            seen.add(key)
            unique.append(e)
    unique.sort(key=lambda e: (e["_sort"], e["org"]))
    for e in unique:
        e.pop("_sort")

    listed = sorted({e["org"] for e in unique}, key=str.lower)
    coverage = {
        "total": len(sources),
        "with_events": len(listed),
        "no_events": len(cov["no_events"]),
        "no_source": len(cov["no_source"]),
        "blocked": len(cov["blocked"]),
        "failed": len(cov["failed"]),
    }
    DOCS.mkdir(exist_ok=True)
    payload = json.dumps(
        {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "orgs": listed,
            "counts": counts,
            "health": health,
            "coverage": coverage,
            "coverage_orgs": cov,
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
    print(json.dumps({"events": len(unique), **coverage}))
    for w in warnings:
        print("WARNING:", w, file=sys.stderr)
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build" / "warnings.txt").write_text("\n".join(warnings))
    # A plain-text coverage report for the person running this (not published).
    lines = [f"{coverage['with_events']} of {coverage['total']} organizations have listings; "
             f"{coverage['no_events']} have none in the window; {coverage['no_source']} have no calendar source; "
             f"{coverage['blocked']} blocked; {coverage['failed']} failed."]
    for k in ("no_source", "blocked", "failed"):
        for o in cov[k]:
            lines.append(f"{k}: {o['name']} | {o['site']} | {o.get('reason','')} {o.get('note','')}".strip())
    (ROOT / "build" / "coverage.txt").write_text("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
