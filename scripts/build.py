"""Step 3: merge, validate and publish docs/events.json.

- Keeps only events inside the date window, de-duplicates, sorts.
- If an org returns zero events or its extraction failed, keeps that org's previous
  events (still in the window) and records a warning, so the page never goes blank
  just because one site changed. The workflow turns warnings into a failed run so
  you get GitHub's failure email.
"""
import datetime as dt
import difflib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sources_io import load_sources  # noqa: E402
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


# Notes the extractor writes when it only had a run or a first date. Once an entry has its own date and time they are
# misleading, so they are dropped (the rest of the note, e.g. "free", "sold out", stays).
_STALE = re.compile(r"showtimes? (is |are )?not stated|no individual|individual (show)?times? not|only the (first|next|start)|"
                    r"first date in the window|next occurrence|run continues|only the dates|listed separately|"
                    r"page gives (no|the run)|^run\b|world premiere run|runs? (nov|dec|jan|oct)", re.I)


def tidy_notes(notes, has_time):
    if not has_time or not notes:
        return notes
    parts = [x.strip() for x in re.split(r"(?<=[.;])\s+|;\s*", notes) if x.strip()]
    keep = [x for x in parts if not _STALE.search(x)]
    return "; ".join(x.rstrip(".") for x in keep).strip()


def clean(ev, org, default_discipline, start, end):
    try:
        date = dt.date.fromisoformat(ev["date"])
    except Exception:
        return None
    if not (start <= date <= end):
        return None
    title = (ev.get("title") or "").strip()
    # Some calendars append the date to the title ("Open Performance | Nov 17, 2026").
    title = re.sub(r"\s*[|\u2013\u2014-]\s*(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\.?,?\s+)?(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.? \d{1,2}(?:,?\s*20\d\d)?\s*$", "", title).strip()
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
        "notes": tidy_notes((ev.get("notes") or "").strip(), bool(t)),
        "_sort": (date.isoformat(), parse_time(t) if parse_time(t) is not None else 9999),
    }


_STOP = {"the", "and", "with", "live", "concert", "performs", "presents", "featuring", "tour", "night", "new", "york",
         "hall", "theater", "theatre", "dance", "music", "part", "show", "from", "that", "this", "your"}


def _tokens(title):
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) >= 4 and w not in _STOP}


def _same_event(a, b, org_names):
    """Two listings of the same performance by different organizations."""
    if a["org"] == b["org"] or a["date"] != b["date"]:
        return False
    ta, tb = a["time"].lower().strip(), b["time"].lower().strip()
    if ta and tb and ta != tb:
        return False
    x, y = a["title"].lower(), b["title"].lower()
    if x == y or difflib.SequenceMatcher(None, x, y).ratio() >= 0.86:
        return True
    if len(_tokens(a["title"]) & _tokens(b["title"])) >= 2:
        return True
    # One title is the other plus a subtitle or a presenter ("adaku, part 2" / "adaku, part 2: she shivers with it").
    nx, ny = re.sub(r"[^a-z0-9]+", " ", x).strip(), re.sub(r"[^a-z0-9]+", " ", y).strip()
    short, long_ = sorted((nx, ny), key=len)
    if len(short) >= 5 and (long_.startswith(short + " ") or long_.endswith(" " + short)):
        return True
    # One organization presenting at the other's venue (e.g. a Philharmonic concert listed by Carnegie Hall too):
    # same date and time, and one listing's location names the other organization.
    if ta and tb:
        for p, q in ((a, b), (b, a)):
            if q["org"].lower() in p["location"].lower() and (_tokens(a["title"]) & _tokens(b["title"])):
                return True
    return False


def merge_cross_org(events, own_only, org_names, generic_urls):
    """Keep one listing when several organizations list the same performance. Prefer an organization that is
    not a community calendar, then a listing with its own link, and say who else listed it."""
    def rank(e):
        generic = generic_urls.get(e["org"]) and e["url"] == generic_urls[e["org"]]
        return (e["org"] in own_only, not e["url"], bool(generic))

    by_date = {}
    for e in events:
        by_date.setdefault(e["date"], []).append(e)
    out = []
    for group in by_date.values():
        group = sorted(group, key=rank)
        kept = []
        for e in group:
            dup = next((k for k in kept if _same_event(k, e, org_names)), None)
            if dup:
                also = dup.setdefault("_also", [])
                if e["org"] not in also:
                    also.append(e["org"])
            else:
                kept.append(e)
        out += kept
    for e in out:
        also = e.pop("_also", None)
        if also:
            e["notes"] = (e["notes"] + "; " if e["notes"] else "") + "also listed by " + ", ".join(also)
            e["also"] = sorted(also)  # the page's organization filter also matches these
    return out


def write_internal(public, sources):
    """Internal copy of the page data with each grantee's program officer. Written only if
    private/officers.json exists. Stays in internal/ (not in git); optional private/config.json
    {"copy_to": "/some/shared/folder"} also copies it there."""
    import shutil

    officers = read_json(ROOT / "private" / "officers.json", {})
    if not officers:
        return
    po_by_name = {s["name"]: officers.get(s["slug"], "") for s in sources}
    data = json.loads(json.dumps(public))
    for e in data["events"]:
        e["officer"] = po_by_name.get(e["org"], "")
    for items in data["coverage_orgs"].values():
        for o in items:
            o["officer"] = po_by_name.get(o["name"], "")
    data["officers"] = sorted({v for v in po_by_name.values() if v})
    out = ROOT / "internal"
    out.mkdir(exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=1)
    (out / "events.json").write_text(text)
    (out / "events.js").write_text("window.EVENTS = " + text.replace("</", "<\\/") + ";\n")
    shutil.copy(DOCS / "index.html", out / "index.html")
    cfg = read_json(ROOT / "private" / "config.json", {})
    if cfg.get("copy_to"):
        try:
            dest = Path(cfg["copy_to"]).expanduser()
            dest.mkdir(parents=True, exist_ok=True)
            for name in ("index.html", "events.js"):
                shutil.copy(out / name, dest / name)
        except Exception as ex:
            print("WARNING: could not copy the internal page to", cfg["copy_to"], ex, file=sys.stderr)


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def main():
    cfg = load_sources()
    days = cfg.get("window_days", 60)
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

        if not (src.get("pages") or src.get("manual")):
            reason = src.get("discovery", {}).get("status", "not_checked")
            note = src.get("discovery", {}).get("note", "")
            firewall = reason == "unreachable" and bool(re.search(
                r"ERR_HTTP2_PROTOCOL_ERROR|ERR_CONNECTION_RESET|ERR_EMPTY_RESPONSE|ERR_CONNECTION_CLOSED", note))
            blocked = reason in ("blocked_bot", "blocked_robots") or firewall
            health[org] = {"status": "blocked" if blocked else "no_source", "events": 0, "reason": reason}
            cov["blocked" if blocked else "no_source"].append(
                {"name": org, "site": site, "reason": reason, "note": note})
            continue

        path = EXTRACTED / f"{src['slug']}.json"
        raw = read_json(RAW / f"{src['slug']}.json", None)
        extracted = path.exists()
        fresh = []
        if extracted:
            fresh = [e for e in (clean(x, org, default, start, end)
                                 for x in json.loads(path.read_text()).get("events", [])) if e]
        age_limit = (src.get("manual") or {}).get("max_age_days", 45)
        if src.get("manual") and raw and raw.get("manual_age_days", 0) > age_limit:
            warnings.append(f"{org}: its saved calendar file is {raw['manual_age_days']} days old, so listings may be out of date.")
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
        walled = bool(raw) and not raw.get("pages") and any("bot protection" in e for e in raw.get("errors", []))

        # Own-events-only organizations (service orgs that list many others' events) legitimately have none some weeks,
        # and anything saved before the filter existed must not be carried forward.
        if extracted and (not kept or src.get("own_events_only")):
            # The page was read and truly lists nothing in the window. Normal for small organizations.
            health[org] = {"status": "empty", "events": 0, "reason": "no performances listed in the window"}
            cov["no_events"].append({"name": org, "site": site})
            continue

        if robots or walled:
            status = "blocked"
            reason = ("its website asks automated tools not to read it (robots.txt)" if robots
                      else "its calendar is behind bot protection, which we do not bypass")
        elif raw and any("manual file missing" in e for e in raw.get("errors", [])):
            status, reason = "failed", "no saved calendar file was found in the manual folder"
        elif extracted:
            status, reason = "empty", "the page was read but no performances were found"
        else:
            status, reason = "failed", "the page did not load or could not be read this week"
        counts[org] = len(kept)
        events += kept
        health[org] = {"status": status, "events": len(kept), "reason": reason}
        if kept or src.get("verified"):
            tail = "" if (robots or walled or src.get("manual")) else " The site layout may have changed."
            warnings.append(f"{org}: {reason}; showing {len(kept)} events from the last successful update.{tail}")
        else:
            cov["blocked" if (robots or walled) else "failed"].append({"name": org, "site": site, "reason": reason})

    seen, unique = set(), []
    for e in events:
        key = (e["org"], e["date"], e["time"].lower(), e["title"].lower())
        if key not in seen:
            seen.add(key)
            unique.append(e)
    own_only = {s["name"] for s in sources if s.get("own_events_only")}
    unique = merge_cross_org(unique, own_only, [s["name"] for s in sources],
                             {s["name"]: (s.get("manual") or {}).get("url", "") for s in sources})
    unique.sort(key=lambda e: (e["_sort"], e["org"]))
    for e in unique:
        e.pop("_sort")

    listed = sorted({e["org"] for e in unique} | {o for e in unique for o in e.get("also", [])}, key=str.lower)
    coverage = {
        "total": len(sources),
        "with_events": len(listed),
        "no_events": len(cov["no_events"]),
        "no_source": len(cov["no_source"]),
        "blocked": len(cov["blocked"]),
        "failed": len(cov["failed"]),
    }
    DOCS.mkdir(exist_ok=True)
    payload_dict = {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "orgs": listed,
            "counts": counts,
            "health": health,
            "coverage": coverage,
            "coverage_orgs": cov,
            "warnings": warnings,
            "events": unique,
    }
    payload = json.dumps(payload_dict, ensure_ascii=False, indent=1)
    OUT.write_text(payload)
    # Same data as a script file, so index.html also works when opened straight from disk
    # (browsers block fetch() of local files). "</" is escaped so no event text can close the tag.
    (DOCS / "events.js").write_text("window.EVENTS = " + payload.replace("</", "<\\/") + ";\n")
    print(json.dumps({"events": len(unique), **coverage}))
    for w in warnings:
        print("WARNING:", w, file=sys.stderr)
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build" / "warnings.txt").write_text("\n".join(warnings))
    write_internal(payload_dict, sources)
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
