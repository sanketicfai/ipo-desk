"""Shared helpers: IST time, number/date parsing, Next.js RSC decoding, HTML cleaning, name matching."""
import html as _html
import json
import re
from functools import lru_cache
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher

IST = timezone(timedelta(hours=5, minutes=30))   # India has no DST -> fixed offset is exact


def now_ist():
    return datetime.now(IST)


def today_ist():
    return now_ist().date()


def iso_now():
    return now_ist().isoformat(timespec="seconds")


def is_market_hours(dt=None):
    dt = dt or now_ist()
    if dt.weekday() >= 5:
        return False
    t = dt.hour * 60 + dt.minute
    return 9 * 60 <= t <= 15 * 60 + 45


# ----------------------------------------------------------------------------- numbers
_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def strip_tags(s):
    if s is None:
        return ""
    s = re.sub(r"<[^>]+>", " ", str(s))
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def num(v):
    """'₹1,000.50 Cr' -> 1000.5 ; '18.08x' -> 18.08 ; '-3' -> -3 ; '--'/'-'/'' -> None"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = strip_tags(v).replace(",", "").replace("\u2212", "-")
    s = s.replace("₹", " ").replace("Rs.", " ").replace("Rs", " ")
    m = _NUM.search(s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def pct(a, b):
    """% change from b to a"""
    try:
        if a is None or b in (None, 0):
            return None
        return round((a - b) / b * 100.0, 2)
    except Exception:
        return None


def rnd(v, n=2):
    return None if v is None else round(v, n)


# ----------------------------------------------------------------------------- dates
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _mk(y, m, d):
    try:
        return date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None


def infer_year(day, month, ref=None, mode="nearest"):
    """Pick a year for a day/month without year. mode: nearest | past (<= ref) | future (>= ref)"""
    ref = ref or today_ist()
    cands = [d for d in (_mk(ref.year + k, month, day) for k in (-1, 0, 1)) if d]
    if not cands:
        return None
    if mode == "past":
        ok = [d for d in cands if d <= ref]
        return max(ok) if ok else min(cands)
    if mode == "future":
        ok = [d for d in cands if d >= ref]
        return min(ok) if ok else max(cands)
    return min(cands, key=lambda d: abs((d - ref).days))


def parse_date(s, ref=None, mode="nearest"):
    """Understands: 2026-09-16, 2026-09-16T00:00:00.000Z, 16th Sep 2026, Wed, 16 Sep 2026,
    16-Sep-2026, 25-Sep-26, 23-09-2026, Sep 18, 2026, 'Tue, 29 Sep' (year inferred)."""
    if not s:
        return None
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    if isinstance(s, datetime):
        return s.date()
    s = strip_tags(s)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return _mk(*m.groups())
    m = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})", s)
    if m:
        return _mk(m.group(3), m.group(2), m.group(1))
    s2 = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", s)
    m = re.search(r"(\d{1,2})[\s\-]+([A-Za-z]{3})[A-Za-z]*[\s\-,]+(\d{4}|\d{2})\b", s2)
    if m:
        d, mon, y = m.groups()
        mo = MONTHS.get(mon.lower()[:3])
        if mo:
            y = int(y)
            if y < 100:
                y += 2000
            return _mk(y, mo, d)
    m = re.search(r"([A-Za-z]{3})[A-Za-z]*\s+(\d{1,2}),?\s+(\d{4})", s2)
    if m:
        mo = MONTHS.get(m.group(1).lower()[:3])
        if mo:
            return _mk(m.group(3), mo, m.group(2))
    m = re.search(r"(\d{1,2})[\s\-]+([A-Za-z]{3})", s2)
    if m:
        mo = MONTHS.get(m.group(2).lower()[:3])
        if mo:
            return infer_year(int(m.group(1)), mo, ref, mode)
    return None


def dstr(d):
    return d.isoformat() if d else None


# ----------------------------------------------------------------------------- Next.js RSC
def rsc_bytes(html):
    """Concatenate self.__next_f.push([1,"..."]) chunks of a Next.js app-router page."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', html, re.S)
    out = []
    for c in chunks:
        try:
            out.append(json.loads('"' + c + '"'))
        except Exception:
            out.append(c)
    return "".join(out).encode("utf-8")


_ROW = re.compile(rb"([0-9a-f]{1,6}):")


def rsc_rows(b):
    """Parse the RSC flight stream -> {row_id: payload}. T-rows (text blobs) are length-prefixed."""
    rows, pos, n = {}, 0, len(b)
    while pos < n:
        m = _ROW.match(b, pos)
        if not m:
            nl = b.find(b"\n", pos)
            if nl < 0:
                break
            pos = nl + 1
            continue
        rid, p = m.group(1).decode(), m.end()
        if b[p:p + 1] == b"T":
            comma = b.find(b",", p)
            try:
                ln = int(b[p + 1:comma], 16)
            except ValueError:
                nl = b.find(b"\n", p)
                pos = n if nl < 0 else nl + 1
                continue
            rows[rid] = b[comma + 1:comma + 1 + ln].decode("utf-8", "replace")
            pos = comma + 1 + ln
        else:
            nl = b.find(b"\n", p)
            nl = n if nl < 0 else nl
            rows[rid] = b[p:nl].decode("utf-8", "replace")
            pos = nl + 1
    return rows


def json_after(text, key, start=0):
    """Decode the JSON value that follows the first occurrence of `key` (e.g. '"ipoData":')."""
    i = text.find(key, start)
    if i < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text, i + len(key))[0]
    except Exception:
        return None


def resolve_ref(v, rows):
    if isinstance(v, str) and re.fullmatch(r"\$[0-9a-f]+", v):
        return rows.get(v[1:], "")
    return v


# ----------------------------------------------------------------------------- HTML cleaning
_ALLOWED = {"p", "br", "b", "strong", "i", "em", "u", "ul", "ol", "li", "table", "thead", "tbody",
            "tfoot", "tr", "td", "th", "span", "div", "h2", "h3", "h4", "h5", "a", "small", "sup", "sub"}


def clean_html(h, max_len=60000):
    """Allow-list sanitiser so third-party HTML can be shown safely in the dashboard."""
    if not h or not isinstance(h, str):
        return ""
    return _clean_html_cached(h, max_len)


@lru_cache(maxsize=6000)
def _clean_html_cached(h, max_len):
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return _html.escape(strip_tags(h))
    soup = BeautifulSoup(h, "html.parser")
    for t in soup.find_all(["script", "style", "iframe", "img", "button", "form", "input", "svg",
                            "noscript", "link", "meta", "object", "embed", "video", "audio"]):
        t.decompose()
    for t in soup.find_all(True):
        if t.name not in _ALLOWED:
            t.unwrap()
            continue
        attrs = {}
        if t.name == "a":
            href = t.get("href", "")
            if href.startswith("http"):
                attrs = {"href": href, "target": "_blank", "rel": "noopener noreferrer"}
            else:
                t.unwrap()
                continue
        if t.name in ("td", "th"):
            for a in ("colspan", "rowspan"):
                if t.get(a) and str(t.get(a)).isdigit():
                    attrs[a] = t.get(a)
        t.attrs = attrs
    out = str(soup)
    out = re.sub(r"(<br\s*/?>\s*){3,}", "<br><br>", out)
    return out[:max_len]


def html_table_rows(h):
    """Extract a simple matrix from the first <table> in an HTML string."""
    if not h or not isinstance(h, str):
        return []
    return [list(r) for r in _table_rows_cached(h)]


@lru_cache(maxsize=3000)
def _table_rows_cached(h):
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    soup = BeautifulSoup(h, "html.parser")
    tbl = soup.find("table")
    if not tbl:
        return ()
    out = []
    for tr in tbl.find_all("tr"):
        cells = tuple(c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"]))
        if any(cells):
            out.append(cells)
    return tuple(out)


# ----------------------------------------------------------------------------- name matching
_STOP = {"limited", "ltd", "the", "pvt", "private", "co", "company", "corp", "corporation", "inc",
         "and", "ipo", "sme", "reit", "invit"}


def norm_name(s):
    s = _html.unescape(s or "").lower().replace("&", " and ")
    s = re.sub(r"\((?:india|i)\)", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(t for t in s.split() if t not in _STOP)


def compact(s):
    return norm_name(s).replace(" ", "")


def name_score(a, b):
    ca, cb = compact(a), compact(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    short, long_ = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
    if len(short) >= 6 and long_.startswith(short):
        return 0.92
    # also try without trailing 'india'
    a2, b2 = re.sub(r"india$", "", ca), re.sub(r"india$", "", cb)
    if a2 and a2 == b2:
        return 0.97
    return SequenceMatcher(None, ca, cb).ratio()


def clean_company_name(s):
    s = strip_tags(s)
    return re.sub(r"\s+", " ", s).strip()
