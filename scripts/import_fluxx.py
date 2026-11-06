"""Build/refresh sources.json from a Fluxx organization export (.xlsx).

Uses only: 'Org Common Name' (display name), 'Website', 'Discipline', 'Borough' and
'Majority of programming devoted to performing arts'. Nothing else (finances, staff, tax IDs, mission text)
is read or written, because sources.json is committed to the repository.

Re-running keeps everything already learned (discovered calendar pages, scope rules, notes) for orgs
whose website is unchanged, adds new orgs, and flags orgs that left the export (active=false).

Usage: python scripts/import_fluxx.py path/to/export.xlsx
"""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
SKIP_NAME = re.compile(r"\b(testing|test org)", re.I)
DISC = {"theatre": "Theater", "theater": "Theater", "music": "Music", "dance": "Dance",
        "multidisciplinary": "Multidisciplinary", "opera": "Opera"}


def norm_url(u):
    u = (u or "").strip().strip(";,")
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


def domain(u):
    return urlparse(u).netloc.lower().removeprefix("www.")


def slugify(s, used):
    base = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "org"
    slug, n = base, 2
    while slug in used:
        slug, n = f"{base}-{n}", n + 1
    used.add(slug)
    return slug


def main(path):
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    ix = {k: i for i, k in enumerate(rows[0])}
    need = ["Org Common Name", "Website", "Discipline", "Borough", "Majority of programming devoted to performing arts"]
    missing = [k for k in need if k not in ix]
    if missing:
        sys.exit(f"Export is missing columns: {missing}")

    cfg_path = ROOT / "sources.json"
    old = json.loads(cfg_path.read_text()) if cfg_path.exists() else {"sources": []}
    by_dom = {}
    for s in old["sources"]:
        for w in s.get("websites", []):
            by_dom[domain(w)] = s
    used = {s["slug"] for s in old["sources"]}

    seen_dom, sources, new, skipped = set(), [], 0, []
    for r in rows[1:]:
        if not any(r):
            continue
        g = lambda k: (r[ix[k]] or "")
        name = str(g("Org Common Name")).strip()
        sites = [norm_url(x) for x in re.split(r"[;,\s]+", str(g("Website"))) if x.strip()]
        if not name or not sites:
            skipped.append((name or "(no name)", "no name or website"))
            continue
        if SKIP_NAME.search(name) or sites[0].endswith(".fake") or ".fake/" in sites[0]:
            skipped.append((name, "test record"))
            continue
        prev = next((by_dom[domain(w)] for w in sites if domain(w) in by_dom), None)
        entry = dict(prev) if prev else {"slug": slugify(name, used)}
        if not prev:
            new += 1
        entry.update({
            "name": name,
            "websites": sites,
            "default_discipline": DISC.get(str(g("Discipline")).strip().lower(), "Other"),
            "borough": str(g("Borough")).strip(),
            "performing_arts_majority": str(g("Majority of programming devoted to performing arts")).strip().lower() != "no",
            "active": True,
        })
        if prev and prev.get("name") != name:
            entry["previous_names"] = sorted(set(prev.get("previous_names", []) + [prev["name"]]))
        sources.append(entry)
        seen_dom.update(domain(w) for w in sites)

    kept_old = [dict(s, active=False) for s in old["sources"]
                if not any(domain(w) in seen_dom for w in s.get("websites", []))]
    out = {"schema": 2, "window_days": old.get("window_days", 31), "sources": sources + kept_old}
    cfg_path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    print(f"{len(sources)} organizations ({new} new), {len(kept_old)} no longer in the export")
    for n, why in skipped:
        print(f"  skipped: {n} ({why})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
