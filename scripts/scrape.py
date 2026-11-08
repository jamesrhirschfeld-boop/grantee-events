"""Step 1: render each grantee's calendar page in a headless browser and save its text.

Every link on the page is written inline, right after its anchor text, as <url>
in angle-bracket-like markers (⟨url⟩). That keeps each event's link next to its
own title, which matters on pages where every link is just "More info".

Output: build/raw/<slug>.json
"""
import datetime as dt
import json
import re
import sys
import urllib.request
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from sources_io import load_sources
from textutil import median_date

UA = "Mozilla/5.0 (compatible; NYCGranteeEventsBot/1.0)"
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "build" / "raw"
MAX_CHARS = 150_000

BOT_WALL = re.compile(r"access denied|captcha|verify you are (a )?human|just a moment|attention required|"
                      r"enable javascript and cookies|request blocked|pardon our interruption|security verification|"
                      r"protect against malicious bots|checking your browser|unusual traffic", re.I)


def is_bot_wall(text):
    """A short page that is a bot-protection screen instead of content. We never try to get past these."""
    return len(text) < 800 and bool(BOT_WALL.search(text))


TAG_LINKS_JS = """
() => {
  document.querySelectorAll('a[href]').forEach(a => {
    if (a.dataset.tagged) return;
    a.dataset.tagged = '1';
    if (!/^https?:/i.test(a.href)) return;
    const s = document.createElement('span');
    s.style.textTransform = 'none';  // links inside ALL-CAPS styled text must keep their real case
    s.textContent = ' ⟨' + a.href + '⟩ ';
    a.appendChild(s);
  });
  const root = document.querySelector('main') || document.body;
  return root.innerText;
}
"""

# Opt-in per page (reveal_hidden): some sites keep their calendar in hidden tabs or in month panels switched by a
# dropdown. Show everything that is hidden, and label each revealed block with a month/year found in its attributes
# (e.g. data-link="#calendar-November-2026") so the grids are not anonymous.
REVEAL_JS = r"""
() => {
  const mon = /(January|February|March|April|May|June|July|August|September|October|November|December)[-_ ,]*(20\d\d)/i;
  document.querySelectorAll('[hidden], .hidden, [aria-hidden="true"], [style*="display: none"], [style*="display:none"]').forEach(e => {
    if (['SCRIPT','STYLE','TEMPLATE','NOSCRIPT','SELECT','OPTION'].includes(e.tagName)) return;
    let label = '';
    for (const a of e.getAttributeNames()) { const m = mon.exec(e.getAttribute(a) || ''); if (m) { label = m[1] + ' ' + m[2]; break; } }
    e.hidden = false; e.removeAttribute('hidden'); e.classList.remove('hidden'); e.style.display = 'block';
    if (label && !e.dataset.labelled) {
      e.dataset.labelled = '1';
      const h = document.createElement('div'); h.textContent = 'MONTH: ' + label; e.insertBefore(h, e.firstChild);
    }
  });
}
"""

LINKS_JS = "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"


def months_in_window(days):
    today = dt.date.today()
    end = today + dt.timedelta(days=days)
    y, m = today.year, today.month
    out = []
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def expand_urls(url, days):
    if "{ym}" in url:
        return [url.replace("{ym}", ym) for ym in months_in_window(days)]
    return [url]


def load(page, url):
    try:
        page.goto(url, wait_until="networkidle", timeout=40_000)
    except PWTimeout:
        # Some sites never go idle (analytics, chat widgets). The DOM is usually fine.
        pass
    page.wait_for_timeout(1500)


# Text of a button or link that means "reveal more of this list". No per-site setup needed:
# "Show More", "Load more events", "Show 129 more events", "View all", ...
MORE_SRC = (
    r"^(show|load|view|see)\s+(\d+\s+)?(more|additional)\b"
    r"|^more\s+(events|results|dates|shows|performances)\b"
    r"|^(show|load|view)\s+all\b"
)

FIND_MORE_JS = r"""(src) => {
  const rx = new RegExp(src, 'i');
  for (const el of document.querySelectorAll('button,[role=button],a')) {
    const t = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
    if (!t || t.length > 60 || !rx.test(t)) continue;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (el.tagName === 'A') {  // only links that act as buttons, never ones that navigate away
      const h = el.getAttribute('href') || '';
      if (h && !h.startsWith('#') && !h.startsWith('javascript')) continue;
    }
    return el;
  }
  return null;
}"""

BODY_TEXT_JS = "() => (document.querySelector('main') || document.body).innerText"


def expand(page, spec, end):
    """Reveal the rest of a list that loads in pieces, without per-site configuration.

    Clicks any "show more" style control, then scrolls for infinite-scroll lists. Stops when nothing new
    appears, when the newly loaded entries are dated well past the window, or at a safety cap.
    A page can still override with click_text / max_clicks / max_scrolls in sources.json.
    """
    src = spec.get("click_text") or MORE_SRC
    prev = len(page.evaluate(BODY_TEXT_JS))
    past_window = False

    def grew():
        nonlocal prev, past_window
        now = page.evaluate(BODY_TEXT_JS)
        if len(now) <= prev:
            return False
        d = median_date(now[prev:])
        prev = len(now)
        if d and d > end + dt.timedelta(days=7):
            past_window = True
        return True

    for _ in range(spec.get("max_clicks", 25)):
        try:
            el = page.evaluate_handle(FIND_MORE_JS, src).as_element()
            if el is None:
                break
            el.click(timeout=5000)
        except Exception:
            break
        page.wait_for_timeout(1500)
        if not grew() or past_window:
            break

    if past_window:
        return
    for _ in range(spec.get("max_scrolls", 8)):
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1200)
        if not grew() or past_window:
            break


_robots = {}


def allowed(url):
    """Respect robots.txt. A missing, unreadable or error-returning robots.txt counts as allowed."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                rp.parse(r.read().decode("utf-8", "replace").splitlines())
            _robots[base] = rp
        except Exception:
            _robots[base] = None
    rp = _robots[base]
    return True if rp is None else rp.can_fetch("NYCGranteeEventsBot", url)


def grab(page, spec, end):
    load(page, spec["url"])
    if spec.get("expand", True):
        expand(page, spec, end)
    if spec.get("reveal_hidden"):
        page.evaluate(REVEAL_JS)
    text = page.evaluate(TAG_LINKS_JS)
    sel = spec.get("next_selector")  # month-by-month calendars: click "next" once per further month in the window
    if sel:
        today = dt.date.today()
        for _ in range((end.year - today.year) * 12 + end.month - today.month):
            try:
                btn = page.locator(sel).locator("visible=true").first
                btn.click(timeout=5000)
            except Exception:
                break
            page.wait_for_timeout(1500)
            text += "\n\n=== NEXT MONTH ===\n" + page.evaluate(TAG_LINKS_JS)
    return text[:MAX_CHARS * 3] if sel else text[:MAX_CHARS]


def read_manual(path):
    """Text of a file dropped in manual/: .pdf (via pypdf) or .txt/.md (pasted page text)."""
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        text = "\n\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
        # some PDFs encode the "ti" ligature as a stray character
        return text.replace("\u018f", "ti").replace("\u019f", "ti").replace("Ɵ", "ti")
    return path.read_text(errors="replace")


def manual_source(src):
    """Organizations that block automated tools: the person running this saves their published calendar
    (a PDF they publish, or pasted page text) into manual/, and it is read exactly like a scraped page."""
    import time

    m = src["manual"]
    f = ROOT / "manual" / m["file"]
    result = {"slug": src["slug"], "name": src["name"], "pages": [], "errors": []}
    if not f.exists():
        result["errors"].append(f"manual file missing: manual/{m['file']}")
        return result
    age = int((time.time() - f.stat().st_mtime) // 86400)
    result["manual_age_days"] = age
    text = read_manual(f)
    if m.get("url"):
        text = (f"(Note: this document has no links for individual events. For every event use this page as its url: "
                f"{m['url']})\n\n" + text)
    result["pages"].append({"url": m.get("url") or f"file:{m['file']}", "text": text})
    return result


def scrape_source(ctx, src, end, days):
    """Render one organization's pages. Returns the result dict that is saved to build/raw/<slug>.json."""
    result = {"slug": src["slug"], "name": src["name"], "pages": [], "errors": []}
    page = ctx.new_page()
    for spec in src["pages"]:
        for url in expand_urls(spec["url"], days):
            one = dict(spec, url=url)
            if not allowed(url):
                result["errors"].append(f"{url}: disallowed by robots.txt, skipped")
                continue
            try:
                text = grab(page, one, end)
                if is_bot_wall(text):
                    result["errors"].append(f"{url}: bot protection page, skipped")
                    continue
                result["pages"].append({"url": url, "text": text})
                if spec.get("follow_links_matching"):
                    base = urlparse(url).netloc
                    seen, follow = set(), []
                    for href in page.evaluate(LINKS_JS):
                        href = urljoin(url, href).split("#")[0]
                        pats = spec["follow_links_matching"]
                        pats = [pats] if isinstance(pats, str) else pats
                        if (
                            any(pt in href for pt in pats)
                            and not any(x in href for x in (spec.get("follow_links_exclude") or []))
                            and urlparse(href).netloc == base
                            and href != url
                            and href not in seen
                        ):
                            seen.add(href)
                            follow.append(href)
                    for href in follow[: spec.get("max_follow", 8)]:
                        if not allowed(href):
                            result["errors"].append(f"{href}: disallowed by robots.txt, skipped")
                            continue
                        try:
                            load(page, href)
                            if spec.get("reveal_hidden"):
                                page.evaluate(REVEAL_JS)
                            result["pages"].append({"url": href, "text": page.evaluate(TAG_LINKS_JS)[:30_000]})
                        except Exception as e:  # one bad detail page shouldn't sink the org
                            result["errors"].append(f"{href}: {e}")
            except Exception as e:
                result["errors"].append(f"{url}: {e}")
    page.close()
    return result


def run_one(slug):
    """Child-process entry point: scrape a single organization and write its raw file."""
    cfg = load_sources()
    days = cfg.get("window_days", 60)
    end = dt.date.today() + dt.timedelta(days=days)
    src = next(s for s in cfg["sources"] if s["slug"] == slug)
    RAW.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000}, user_agent=UA)
        try:
            result = scrape_source(ctx, src, end, days)
        finally:
            browser.close()
    (RAW / f"{slug}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))


def main(argv):
    """Scrape every active organization that has calendar pages, several at a time.

    Each organization runs in its own child process with a hard time limit, so one stuck site cannot
    stall the whole run. Usage: scrape.py [slug ...]   (no slugs = all)
    """
    import concurrent.futures as cf
    import os
    import subprocess

    cfg = load_sources()
    only = set(argv)
    todo = [s for s in cfg["sources"]
            if s.get("active", True) and (s.get("pages") or s.get("manual")) and (not only or s["slug"] in only)]
    RAW.mkdir(parents=True, exist_ok=True)
    if not only:  # a full run: drop results left over from organizations that are no longer scraped
        for f in RAW.glob("*.json"):
            f.unlink()
    workers = int(os.environ.get("SCRAPE_WORKERS", "4"))
    limit = int(os.environ.get("SCRAPE_TIMEOUT", "420"))

    def job(src):
        if src.get("manual"):
            r = manual_source(src)
            (RAW / f"{src['slug']}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))
            return f"{src['slug']}: manual file, {len(r['pages'])} pages, {len(r['errors'])} errors"
        try:
            own_limit = int(src.get("scrape_timeout", limit))  # organizations that follow many pages can ask for longer
            subprocess.run([sys.executable, str(Path(__file__)), "--one", src["slug"]],
                           timeout=own_limit, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            (RAW / f"{src['slug']}.json").write_text(json.dumps(
                {"slug": src["slug"], "name": src["name"], "pages": [], "errors": [f"timed out after {own_limit}s"]}))
        f = RAW / f"{src['slug']}.json"
        if not f.exists():
            f.write_text(json.dumps({"slug": src["slug"], "name": src["name"], "pages": [],
                                     "errors": ["scraper crashed before saving"]}))
        r = json.loads(f.read_text())
        return f"{src['slug']}: {len(r['pages'])} pages, {len(r['errors'])} errors"

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for line in ex.map(job, todo):
            print(line, flush=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--one":
        run_one(sys.argv[2])
    else:
        sys.exit(main(sys.argv[1:]))
