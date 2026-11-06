"""Reads sources.json and applies overrides.json on top.

sources.json is edited by the tools themselves (discovery saves what it learns), and each computer keeps its own
copy. overrides.json is shipped with the project and ALWAYS wins, so a setting such as "only list this
organization's own events" or "read NY Phil from a saved file" reaches every copy. A value of null removes a key.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_raw():
    return json.loads((ROOT / "sources.json").read_text())


def load_sources():
    cfg = read_raw()
    p = ROOT / "overrides.json"
    ov = json.loads(p.read_text()) if p.exists() else {}
    for s in cfg["sources"]:
        for k, v in ov.get(s["slug"], {}).items():
            if v is None:
                s.pop(k, None)
            else:
                s[k] = v
    return cfg
