"""Find out WHY a site could not be read. No API calls.

For each organization it tries the address three ways (a plain web request, the headless browser, and the same address
with/without "www."), notes the HTTP status, where it redirects, whether a bot-protection page appears, and whether
robots.txt allows us, then gives a plain verdict:

  fixable   the site loads fine for the browser (a retry or a hand-set calendar page will work)
  moved     the address redirects to a different site (the Fluxx address is out of date)
  bad-url   the address does not exist (typo or dead domain)
  slow      it only loads very slowly
  blocked   the site refuses or silently drops automated visitors (we leave these out and ask the organization)
  robots    robots.txt asks automated tools to stay out

Usage: diagnose.py [slug ...]   (no slugs = every organization whose discovery status is 'unreachable' or 'error')
Writes build/diagnose.txt and prints it.
"""
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
BOT_WALL = re.compile(r"access denied|captcha|verify you are (a )?human|just a moment|attention required|"
                      r"enable javascript and cookies|request blocked|pardon our interruption|unusual traffic", re.I)


def http_probe(url, ua):
    """-> (status or None, final_url, error text)"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html,*/*"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.geturl(), ""
    except urllib.error.HTTPError as e:
        return e.code, url, f"HTTP {e.code}"
    except Exception as e:  # DNS failure, refused, reset, timeout, TLS...
        return None, url, str(getattr(e, "reason", e))[:100]


def other_host(url):
    host = urlparse(url).netloc
    return url.replace("//www.", "//", 1) if host.startswith("www.") else url.replace("//", "//www.", 1)


def same_site(a, b):
    ha = urlparse(a).netloc.lower().removeprefix("www.")
    hb = urlparse(b).netloc.lower().removeprefix("www.")
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def browser_probe(url):
    """-> dict(status, final, text_len, bot_wall, error, seconds)"""
    import time
    from playwright.sync_api import sync_playwright
    out = {"status": None, "final": url, "text_len": 0, "bot_wall": False, "error": "", "seconds": 0}
    t0 = time.time()
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1400, "height": 1000}, user_agent=BROWSER_UA)
        page = ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            out["status"] = resp.status if resp else None
            page.wait_for_timeout(3000)
            out["final"] = page.url
            text = page.evaluate("() => document.body ? document.body.innerText : ''")
            out["text_len"] = len(text)
            out["bot_wall"] = bool(len(text) < 600 and BOT_WALL.search(text))
        except Exception as e:
            out["error"] = str(e).splitlines()[0][:110]
        b.close()
    out["seconds"] = round(time.time() - t0, 1)
    return out


def verdict(url, plain, bro, alt_bro, robots_ok):
    if not robots_ok:
        return "robots", "robots.txt asks automated tools to stay out"
    for label, r in (("", bro), ("(with/without www) ", alt_bro)):
        if r and not r["error"] and r["text_len"] > 300 and not r["bot_wall"] and (r["status"] or 200) < 400:
            if not same_site(r["final"], url):
                return "moved", f"{label}redirects to {r['final']}"
            return "fixable", f"{label}loads fine in the browser ({r['text_len']} characters, {r['seconds']}s); try again or set the calendar page by hand"
    err = (bro["error"] + " " + plain[2]).lower()
    if bro["bot_wall"] or (alt_bro and alt_bro["bot_wall"]):
        return "blocked", "shows a bot-protection page"
    if (bro["status"] or 0) in (401, 403, 406, 429, 503) or (plain[0] or 0) in (401, 403, 406, 429, 503):
        return "blocked", f"refuses automated visitors (HTTP {bro['status'] or plain[0]})"
    if "name_not_resolved" in err or "name or service not known" in err or "nodename nor servname" in err:
        return "bad-url", "the domain does not exist (check the address)"
    if "err_connection_refused" in err or "connection refused" in err:
        return "bad-url", "nothing is answering at that address"
    if "certificate" in err or "ssl" in err or "cert_" in err:
        return "bad-url", "security certificate problem on the site"
    if "timeout" in err or "timed out" in err:
        if plain[0] and plain[0] < 400 and not bro["bot_wall"]:
            return "slow", "web request works but the browser load times out"
        return "blocked", "connection times out for automated visitors (often silent blocking)"
    if "reset" in err or "err_http2" in err or "empty_response" in err or "closed" in err:
        return "blocked", "connection is dropped for automated visitors"
    if bro["text_len"] and bro["text_len"] <= 300:
        return "blocked", f"returns an almost empty page ({bro['text_len']} characters), likely a block or a script-only site"
    return "unknown", (bro["error"] or plain[2] or "no clear cause")


def run_one(slug):
    from scrape import allowed
    from sources_io import load_sources

    src = next(s for s in load_sources()["sources"] if s["slug"] == slug)
    url = (src.get("websites") or [""])[0]
    robots_ok = allowed(url)
    plain = http_probe(url, BROWSER_UA)
    bro = browser_probe(url)
    alt_bro = None
    if bro["error"] or bro["text_len"] <= 300 or bro["bot_wall"]:
        alt_bro = browser_probe(other_host(url))
    kind, why = verdict(url, plain, bro, alt_bro, robots_ok)
    return {"slug": slug, "name": src["name"], "url": url, "kind": kind, "why": why,
            "http": plain[0], "browser_status": bro["status"], "browser_error": bro["error"]}


def main(argv):
    from sources_io import load_sources

    cfg = load_sources()
    if argv:
        slugs = argv
    else:
        slugs = [s["slug"] for s in cfg["sources"]
                 if s.get("active", True) and (s.get("discovery") or {}).get("status") in ("unreachable", "error")]
    print(f"Checking {len(slugs)} site(s)... (about {max(1, len(slugs) // 3)} minutes)", flush=True)

    def job(slug):
        try:
            r = subprocess.run([sys.executable, __file__, "--one", slug], capture_output=True, text=True, timeout=240)
            return json.loads(r.stdout.strip().splitlines()[-1])
        except Exception as e:
            name = next((s["name"] for s in cfg["sources"] if s["slug"] == slug), slug)
            return {"slug": slug, "name": name, "url": "", "kind": "unknown", "why": f"check itself failed: {e}"}

    with cf.ThreadPoolExecutor(max_workers=int(os.environ.get("DIAG_WORKERS", "4"))) as ex:
        results = list(ex.map(job, slugs))

    order = ["fixable", "moved", "bad-url", "slow", "unknown", "blocked", "robots"]
    results.sort(key=lambda r: (order.index(r["kind"]) if r["kind"] in order else 99, r["name"].lower()))
    lines = []
    for r in results:
        lines.append(f"{r['kind'].upper():8} {r['name']}  [{r['slug']}]\n         {r['url']}\n         {r['why']}")
    counts = {}
    for r in results:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    head = "Summary: " + ", ".join(f"{k} {counts[k]}" for k in order if k in counts)
    text = head + "\n\n" + "\n".join(lines) + "\n"
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build" / "diagnose.txt").write_text(text)
    (ROOT / "build" / "diagnose.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print(text)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--one":
        print(json.dumps(run_one(sys.argv[2])))
    else:
        sys.exit(main(sys.argv[1:]))
