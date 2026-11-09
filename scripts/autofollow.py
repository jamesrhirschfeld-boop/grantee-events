"""Generic "follow the right links" logic, so organizations need no hand-written setup.

Many calendars list only titles or date ranges; the real dates, times and venue are on each show's own page. Others are
season pages made of image tiles. When a page looks thin on dates, the scraper asks this module which of the page's links
lead to individual shows or events, and reads those pages too.

Pure functions (no browser, no API): easy to test. Scoring is rule-based and deterministic; an optional model pass
(see rank_with_model) can re-order when there are many candidates.
"""
import re
from urllib.parse import urlparse, urljoin

from textutil import find_dates

LINK_INFO_JS = r"""
() => {
  const region = (a) => {
    if (a.closest('nav, [role=navigation]')) return 'nav';
    if (a.closest('footer, [role=contentinfo]')) return 'footer';
    if (a.closest('header, [role=banner]')) return 'nav';
    if (a.closest('main, [role=main], article')) return 'main';
    return 'other';
  };
  return Array.from(document.querySelectorAll('a[href]')).map(a => ({
    href: a.href,
    text: (a.innerText || a.getAttribute('aria-label') || a.title || '').trim().replace(/\s+/g, ' ').slice(0, 80),
    ctx: ((a.closest('li, article, section, div') || a).innerText || '').trim().replace(/\s+/g, ' ').slice(0, 160),
    region: region(a),
    imgonly: !(a.innerText || '').trim() && !!a.querySelector('img'),
  }));
}
"""

# Pages that are never a show: skipped outright.
SKIP_WORDS = re.compile(
    r"(?:^|[/-])(?:production-history|past-seasons?|season-archive)(?:/|$)|/(about|aboutus|team|staff|board|careers?|jobs?|press|donate|donation|give|giving|support|membership|member|friend|"
    r"volunteer|contact|privacy|terms|faq|faqs|shop|store|cart|checkout|account|login|signin|newsletter|mailing|subscribe|"
    r"accessibility|history|mission|rentals?|rent|sponsor|sponsors|supporters|reports?|policies|policy|search|tag|category|"
    r"author|feed|wp-admin|wp-json|cdn-cgi|gift-?cards?|group-?tickets?|group-?sales|visit|directions|parking|"
    r"jobs|employment|internships?|apply|applications?|resources|archive|archives|past-?events?|history|people|person|bios?|biography|"
    r"artists?-bios?|cast|creative-team|donors?|partners?|press-?releases?|newsroom|blog|news)(/|$|-)", re.I)
FILE_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|zip|docx?|xlsx?|mp3|mp4|ics)(\?|$)", re.I)

# Hosts whose pages sit behind bot protection: never followed (we don't bypass it).
WALLED_HOSTS = ("ovationtix.com", "telecharge.com", "ticketmaster.com", "livenation.com", "axs.com", "seetickets.com",
                "audienceview.com", "tix.com")

GOOD_PATH = re.compile(r"(event|show|perform|production|season|whats-?on|calendar|series|festival|concert|play|"
                       r"program|residency|premiere|tour|live|dance|music|theat)", re.I)
GOOD_TEXT = re.compile(r"(learn more|more info|details|read more|buy|ticket|info|view|see |watch|rsvp|reserve|get )", re.I)
TICKET_TEXT = re.compile(r"(ticket|buy|rsvp|reserve|book)", re.I)
MONTH_IN_URL = re.compile(r"(20\d\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.I)


def reg_domain(url):
    host = (urlparse(url).hostname or "").lower()
    parts = host.split(".")
    return ".".join(parts[-3:]) if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "ac") else ".".join(parts[-2:])


def is_thin(text, start, end, min_dates=4):
    """True when the page shows fewer than min_dates distinct dates inside the window: it is a menu, tiles or a list of
    titles, and the real information is one click further."""
    ds = {d for d in find_dates(text, start) if start <= d <= end}
    return len(ds) < min_dates


def candidate_links(infos, page_url, already, site_domains, max_external=3):
    """Ordered list of links worth following from this page (best first)."""
    base = urlparse(page_url)
    seen, scored = set(already), []
    external = 0
    for i, a in enumerate(infos):
        href = urljoin(page_url, a.get("href", "")).split("#")[0]
        u = urlparse(href)
        if u.scheme not in ("http", "https") or not u.netloc:
            continue
        if href.rstrip("/") == page_url.rstrip("/") or href in seen:
            continue
        if FILE_EXT.search(u.path) or SKIP_WORDS.search(u.path + "/") or u.path in ("", "/"):
            continue
        if any(u.netloc.lower().endswith(h) for h in WALLED_HOSTS):
            continue
        text = a.get("text", "")
        same = reg_domain(href) in site_domains
        if not same:
            # An outside page is followed only when the link is a ticket/details link and the host is not walled.
            if not TICKET_TEXT.search(text) or external >= max_external:
                continue
        region = a.get("region", "other")
        score = {"main": 3, "other": 2, "nav": 1, "footer": -5}.get(region, 1)
        if region == "footer":
            continue
        if GOOD_PATH.search(u.path):
            score += 2
        if GOOD_TEXT.search(text):
            score += 2
        if MONTH_IN_URL.search(u.path):
            score += 1
        if a.get("imgonly"):
            score += 1  # tile images are how many small sites link their shows
        if len([p for p in u.path.split("/") if p]) >= 2:
            score += 1
        if u.query:
            score -= 2
        if not same:
            external += 1
            score -= 1
        seen.add(href)
        scored.append((-score, i, href))
    scored.sort()
    return [h for _, _, h in scored]


MODEL = "claude-haiku-5-5"


def rank_with_model(client, org, page_url, infos, cands, keep):
    """Optional: ask a small model which candidate links are individual shows/events. Returns a sub-list of cands
    (in the model's order); falls back to cands[:keep] on any problem."""
    by_href = {}
    for a in infos:
        by_href.setdefault(urljoin(page_url, a["href"]).split("#")[0], a)
    lines = []
    for n, h in enumerate(cands[:40]):
        a = by_href.get(h, {})
        lines.append(f"{n}. {h} | text: {a.get('text', '')} | near: {a.get('ctx', '')[:100]}")
    tool = {"name": "pick", "description": "Pick the links that lead to individual performances or events.",
            "input_schema": {"type": "object", "properties": {"numbers": {"type": "array", "items": {"type": "integer"}}},
                             "required": ["numbers"]}}
    prompt = (f"Organization: {org}. Page: {page_url}\nBelow are links found on this page. Pick up to {keep} that most likely "
              "lead to the page of ONE specific upcoming performance, show, concert, production or festival (a page that would state "
              "dates and times). Skip general pages (about, support, donate, education, news, staff), past-season archives, "
              "and links that look like site navigation. Answer by calling pick.\n\n" + "\n".join(lines))
    try:
        r = client.messages.create(model=MODEL, max_tokens=300, tools=[tool], tool_choice={"type": "tool", "name": "pick"},
                                   messages=[{"role": "user", "content": prompt}])
        for b in r.content:
            if b.type == "tool_use":
                nums = [n for n in b.input.get("numbers", []) if isinstance(n, int) and 0 <= n < len(cands[:40])]
                if nums:
                    return [cands[n] for n in nums][:keep]
    except Exception:
        pass
    return cands[:keep]
