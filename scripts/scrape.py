"""Step 1: render each grantee's calendar page in a headless browser and save its text.

Every link on the page is written inline, right after its anchor text, as <url>
in angle-bracket-like markers (⟨url⟩). That keeps each event's link next to its
own title, which matters on pages where every link is just "More info".

Output: build/raw/<slug>.json
"""
import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from textutil import median_date

UA = "Mozilla/5.0 (compatible; NYCGranteeEventsBot/1.0)"
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "build" / "raw"
MAX_CHARS = 150_000

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
        page.goto(url, wait_until="networkidle", timeout=60_000)
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
    text = page.evaluate(TAG_LINKS_JS)
    return text[:MAX_CHARS]


def main():
    cfg = json.loads((ROOT / "sources.json").read_text())
    days = cfg.get("window_days", 31)
    end = dt.date.today() + dt.timedelta(days=days)
    RAW.mkdir(parents=True, exist_ok=True)
    failures = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": 1400, "height": 1000},
            user_agent=UA,
        )
        only = set(sys.argv[1:])  # optional: scrape just these slugs, e.g. `scrape.py carnegie`
        for src in cfg["sources"]:
            if only and src["slug"] not in only:
                continue
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
                        result["pages"].append({"url": url, "text": text})
                        if spec.get("follow_links_matching"):
                            base = urlparse(url).netloc
                            seen, follow = set(), []
                            for href in page.evaluate(LINKS_JS):
                                href = urljoin(url, href).split("#")[0]
                                if (
                                    spec["follow_links_matching"] in href
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
                                    result["pages"].append(
                                        {"url": href, "text": page.evaluate(TAG_LINKS_JS)[:30_000]}
                                    )
                                except Exception as e:  # one bad detail page shouldn't sink the org
                                    result["errors"].append(f"{href}: {e}")
                    except Exception as e:
                        result["errors"].append(f"{url}: {e}")
            page.close()
            if not result["pages"]:
                failures += 1
            (RAW / f"{src['slug']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
            print(f"{src['slug']}: {len(result['pages'])} pages, {len(result['errors'])} errors")
        browser.close()

    # Don't fail the run here: build.py decides what to do about an org with no data.
    return 0


if __name__ == "__main__":
    sys.exit(main())
