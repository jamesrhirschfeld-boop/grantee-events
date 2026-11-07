"""Keep the organization list in step with my_orgs.csv, and regenerate slugs.txt.

my_orgs.csv is YOUR list for organizations that are not (yet) in the Fluxx export. Open it in Excel, Numbers or any
text editor; one organization per row:   Organization,Website,Discipline   (Discipline is optional:
Theater, Music, Dance, Opera, Multidisciplinary or Other). Add a row, save, and run any command: the organization is
added to sources.json (discovery will find its calendar on the next run). Removing a row you added turns it off again; changing the website of an existing row re-checks that organization.
Fluxx imports never deactivate organizations that come from this file.

slugs.txt (name -> short code used with one_org.command) is rewritten every time.
"""
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_fluxx import DISC, domain, norm_url, slugify  # noqa: E402

TAG = "my_orgs.csv"


def read_rows():
    p = ROOT / "my_orgs.csv"
    if not p.exists():
        return []
    rows = []
    with open(p, newline="", encoding="utf-8-sig") as f:
        for r in csv.reader(f):
            if len(r) >= 2 and r[0].strip() and r[1].strip() and r[0].strip().lower() != "organization":
                rows.append((r[0].strip(), norm_url(r[1].split()[0]), (r[2].strip() if len(r) > 2 else "")))
    return rows


def main():
    path = ROOT / "sources.json"
    cfg = json.loads(path.read_text())
    srcs = cfg["sources"]
    by_dom = {domain(w): s for s in srcs for w in s.get("websites", [])}
    by_name = {s["name"].lower(): s for s in srcs}
    used = {s["slug"] for s in srcs}
    wanted, added, changed = set(), [], False
    for name, site, disc in read_rows():
        s = by_name.get(name.lower()) or by_dom.get(domain(site))
        if s is not None and s["websites"][:1] != [site] and by_name.get(name.lower()) is s:
            s["websites"] = [site] + [w for w in s["websites"][1:] if domain(w) != domain(site)]  # edited address
            s.pop("discovery", None)  # re-find the calendar on the new address
            changed = True
        if s is None:
            s = {"slug": slugify(name, used), "name": name, "websites": [site],
                 "default_discipline": DISC.get(disc.lower(), "Other"), "borough": "",
                 "performing_arts_majority": True, "active": True, "source": TAG}
            srcs.append(s)
            by_dom[domain(site)] = s
            added.append(name)
            changed = True
        elif s.get("source") == TAG and not s.get("active", True):
            s["active"] = True
            changed = True
        if s.get("source") == TAG:
            wanted.add(s["slug"])
    for s in srcs:  # a row deleted from the file switches that organization off
        if s.get("source") == TAG and s["slug"] not in wanted and s.get("active", True):
            s["active"] = False
            changed = True
    if changed:
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n")
    rows = sorted((s["name"], s["slug"]) for s in srcs if s.get("active", True))
    (ROOT / "slugs.txt").write_text("Organization  ->  slug (use the slug after one_org.command)\n\n"
                                    + "\n".join(f"{n}  ->  {sl}" for n, sl in rows) + "\n")
    if added:
        print(f"Added from my_orgs.csv: {', '.join(added)}")
    print(f"{len(rows)} active organizations; slugs.txt updated")


if __name__ == "__main__":
    main()
