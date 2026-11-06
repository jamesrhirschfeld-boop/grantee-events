"""Small pure helpers shared by the pipeline (no browser or API needed, so they are easy to test)."""
import datetime as dt
import re
import statistics

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MON = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"

# "Nov 5", "November 5, 2026", "Nov. 5th 2026"
_MDY = re.compile(_MON + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?\b", re.I)
# "5 November", "5 Nov 2026"
_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r"\b\.?(?:,?\s+(20\d{2}))?", re.I)
# "2026-11-05" and "11/05/2026"
_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_US = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")


def _month(name):
    return _MONTHS.index(name[:3].lower()) + 1


def _resolve(month, day, year, today):
    """Build a date; with no year, pick the year that puts it within the last ~60 days or later."""
    try:
        if year:
            return dt.date(year, month, day)
        d = dt.date(today.year, month, day)
        if d < today - dt.timedelta(days=60):
            d = dt.date(today.year + 1, month, day)
        return d
    except ValueError:
        return None


def find_dates(text, today=None):
    """Dates mentioned in text, in order of appearance."""
    today = today or dt.date.today()
    found = []  # (position, date)
    for m in _MDY.finditer(text):
        d = _resolve(_month(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None, today)
        if d:
            found.append((m.start(), d))
    for m in _DMY.finditer(text):
        d = _resolve(_month(m.group(2)), int(m.group(1)), int(m.group(3)) if m.group(3) else None, today)
        if d:
            found.append((m.start(), d))
    for m in _ISO.finditer(text):
        try:
            found.append((m.start(), dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))))
        except ValueError:
            pass
    for m in _US.finditer(text):
        try:
            found.append((m.start(), dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))))
        except ValueError:
            pass
    found.sort(key=lambda x: x[0])
    return [d for _, d in found]


def median_date(text, today=None):
    """Median of the dates in text, or None. The median shrugs off stray dates such as 'on sale Mar 10'."""
    ds = find_dates(text, today)
    if not ds:
        return None
    return dt.date.fromordinal(int(statistics.median(d.toordinal() for d in ds)))


def chunk_text(text, max_chars=70_000, overlap=2_000):
    """Split text on line boundaries into parts of at most max_chars, each overlapping the previous
    by about `overlap` characters so a listing cut at an edge appears whole in one part."""
    if len(text) <= max_chars:
        return [text]
    lines = text.split("\n")
    # Hard-split any single line that is itself too long.
    pieces = []
    for ln in lines:
        while len(ln) > max_chars:
            pieces.append(ln[:max_chars])
            ln = ln[max_chars:]
        pieces.append(ln)
    chunks, cur, size = [], [], 0
    for ln in pieces:
        if size + len(ln) + 1 > max_chars and cur:
            chunks.append("\n".join(cur))
            # carry the tail of this chunk into the next one
            tail, tsize = [], 0
            for prev in reversed(cur):
                if tsize + len(prev) + 1 > overlap:
                    break
                tail.insert(0, prev)
                tsize += len(prev) + 1
            cur, size = tail, tsize
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks
