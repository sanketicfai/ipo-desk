"""Data-source adapters: InvestorGain, Chittorgarh, Narada (trynarada.com), NSE and BSE.

Every public function returns plain dicts/lists and raises on failure; the scheduler records
per-source health so the dashboard can show which sources are live.
"""
import csv
import io
import json
import re
import threading
import time
import zipfile
from datetime import timedelta
from urllib.parse import urlparse

import requests

from util import (clean_company_name, clean_html, dstr, html_table_rows, json_after, now_ist, num,
                  parse_date, resolve_ref, rsc_bytes, rsc_rows, strip_tags, today_ist)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0.0.0 Safari/537.36")

IG = "https://www.investorgain.com"
IG_API = "https://webnodejs.investorgain.com"
CG = "https://www.chittorgarh.com"
ND = "https://trynarada.com"
NSE = "https://www.nseindia.com"
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_WWW = "https://www.bseindia.com"


class SourceBlocked(Exception):
    """Source refuses this network (HTTP 401/403) - skip it for a while."""


# ============================================================================ HTTP client
class Http:
    """Thread-safe requests wrapper: per-host rate limit, retries, circuit breaker."""

    MIN_INTERVAL = {           # seconds between requests to the same host (politeness)
        "www.investorgain.com": 0.25,
        "webnodejs.investorgain.com": 0.5,
        "trynarada.com": 0.35,
        "www.chittorgarh.com": 1.5,
        "www.nseindia.com": 0.4,
        "nsearchives.nseindia.com": 0.5,
        "api.bseindia.com": 0.35,
        "www.bseindia.com": 0.5,
    }

    def __init__(self):
        self._local = threading.local()
        self._host_lock = {}
        self._last = {}
        self._glock = threading.Lock()
        self.blocked_until = {}     # host -> epoch seconds
        self.fail_count = {}

    def session(self):
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9",
                              "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8"})
            self._local.s = s
        return s

    def _throttle(self, host):
        with self._glock:
            lk = self._host_lock.setdefault(host, threading.Lock())
        with lk:
            gap = self.MIN_INTERVAL.get(host, 0.3)
            wait = self._last.get(host, 0) + gap - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.time()

    def get(self, url, timeout=25, retries=2, headers=None, params=None, session=None, allow_blocked=False):
        host = urlparse(url).netloc
        if not allow_blocked and self.blocked_until.get(host, 0) > time.time():
            raise SourceBlocked(f"{host} temporarily skipped (blocked earlier)")
        s = session or self.session()
        last_err = None
        for attempt in range(retries + 1):
            self._throttle(host)
            try:
                r = s.get(url, timeout=timeout, headers=headers, params=params)
                if r.status_code in (401, 403):
                    self.blocked_until[host] = time.time() + 15 * 60
                    raise SourceBlocked(f"{host} returned HTTP {r.status_code} (blocked from this network)")
                if r.status_code in (429, 500, 502, 503, 504, 520, 521, 522, 523, 524):
                    last_err = RuntimeError(f"{host} HTTP {r.status_code}")
                    time.sleep(1.5 * (attempt + 1) + (5 if r.status_code == 429 else 0))
                    continue
                if r.status_code == 404:
                    raise LookupError(f"404 {url}")
                r.raise_for_status()
                self.fail_count[host] = 0
                return r
            except (SourceBlocked, LookupError):
                raise
            except requests.RequestException as e:
                last_err = e
                time.sleep(1.2 * (attempt + 1))
        self.fail_count[host] = self.fail_count.get(host, 0) + 1
        if self.fail_count[host] >= 6:          # circuit breaker for flaky origins
            self.blocked_until[host] = time.time() + 5 * 60
            self.fail_count[host] = 0
        raise last_err or RuntimeError(f"failed {url}")


# ============================================================================ InvestorGain
def _ig_rating(html):
    return (html or "").count("&#128293;") + (html or "").count("\U0001F525")


def _ig_slug(path):
    m = re.search(r"/(?:gmp|subscription|ipo)/([^/]+)/(\d+)/?", path or "")
    return (m.group(1), int(m.group(2))) if m else (None, None)


def _ig_exchange_badge(name_html):
    for t in re.findall(r"<span[^>]*>([^<]+)</span>", name_html or ""):
        t = t.strip()
        if t in ("BSE SME", "NSE SME", "BSE", "NSE", "BSE, NSE", "NSE, BSE", "SME", "IPO"):
            if t not in ("SME", "IPO"):
                return t
    return None


def _ig_table(http, path):
    r = http.get(IG + path, timeout=30)
    full = rsc_bytes(r.text).decode("utf-8", "replace")
    obj = json_after(full, '"initialTableResponse":') or {}
    return obj.get("reportTableData") or []


def ig_catalog(http):
    """All IPOs known to InvestorGain (id, name, slug, open/close dates)."""
    r = http.get(IG_API + "/cloud/v2/ipo/ipo-url-lists", timeout=40, retries=3,
                 headers={"Origin": IG, "Referer": IG + "/", "Accept": "application/json"})
    out = []
    for x in r.json().get("lists") or []:
        out.append({
            "ig_id": int(x["id"]),
            "name": clean_company_name(x.get("company_short_name")),
            "slug": x.get("urlrewrite_folder_name"),
            "open": dstr(parse_date(x.get("issue_open_dt"))),
            "close": dstr(parse_date(x.get("issue_end_dt"))),
            "logo": ("https://www.chittorgarh.net/images/ipo/" + x["logo_url"]) if x.get("logo_url") else None,
        })
    return out


IG_STATUS = {"U": "upcoming", "O": "open", "CT": "closing_today", "C": "closed", "BA": "allotment_today",
             "A": "allotted", "LP": "allotted", "LT": "listing_today", "LN": "listed", "L": "listed"}


def ig_live_gmp(http):
    """Live GMP report (#331): every current IPO with GMP, low/high, rating, dates, subscription."""
    out = []
    for r in _ig_table(http, "/report/live-ipo-gmp/331/"):
        slug, iid = _ig_slug(r.get("~urlrewrite_folder_name"))
        iid = int(r.get("~id") or iid or 0)
        if not iid:
            continue
        txt = strip_tags(r.get("GMP"))
        m = re.search(r"₹\s*(-?[\d.]+|--)\s*\(\s*(-?[\d.]+|-)?\s*%?\)", txt)
        gmp = num(m.group(1)) if m and m.group(1) != "--" else None
        gpct = num(m.group(2)) if m and m.group(2) not in (None, "-") and gmp is not None else None
        lh = re.search(r"(-?[\d.]+)\s*↓\s*/\s*(-?[\d.]+)\s*↑", txt)
        sub = r.get("Sub")
        out.append({
            "ig_id": iid, "slug": slug, "name": clean_company_name(r.get("~ipo_name")),
            "ig_status": r.get("~ipo_status1"), "board": "SME" if r.get("~IPO_Category") == "SME" else "Mainboard",
            "exchange": _ig_exchange_badge(r.get("Name")),
            "gmp": gmp, "gmp_pct": gpct,
            "gmp_low": num(lh.group(1)) if lh else None, "gmp_high": num(lh.group(2)) if lh else None,
            "rating": _ig_rating(r.get("Rating")),
            "sub_total": num(sub) if sub and sub.strip() not in ("-", "") else None,
            "price": num(r.get("Price (₹)")), "size_cr": num(r.get("IPO Size")), "lot": num(r.get("Lot")),
            "pe": num(r.get("~P/E")),
            "open": dstr(parse_date(r.get("~Srt_Open"))), "close": dstr(parse_date(r.get("~Srt_Close"))),
            "allotment": dstr(parse_date(r.get("~Srt_BoA_Dt"))), "listing": dstr(parse_date(r.get("~Str_Listing"))),
            "gmp_updated": strip_tags(r.get("Updated-On")) or None,
            "anchor": "✅" in (r.get("Anchor") or "") or "\u2705" in (r.get("Anchor") or ""),
        })
    return out


def ig_live_subscription(http):
    """Live subscription report (#333): category-wise times for all open IPOs in one request."""
    out = []
    for r in _ig_table(http, "/report/ipo-subscription-live/333/"):
        iid = int(r.get("~id") or 0)
        if not iid:
            continue
        tot = r.get("Total") or ""
        tm = re.search(r"<small[^>]*>.*?<b>([^<]+)</b>", tot, re.S)
        out.append({
            "ig_id": iid, "total": num(re.sub(r"<small.*", "", tot, flags=re.S)),
            "qib": num(r.get("QIB")), "snii": num(r.get("SHNI")), "bnii": num(r.get("BHNI")),
            "nii": num(r.get("NII")), "rii": num(r.get("RII")),
            "as_of": strip_tags(tm.group(1)) if tm else None,
        })
    return out


def ig_performance(http, year):
    """GMP performance tracker (#377): listing price, listing-day close, LTP, final GMP for a year."""
    out = []
    for r in _ig_table(http, f"/report/ipo-gmp-performance-tracker/377/?year={year}"):
        iid = int(r.get("~id") or 0)
        if not iid:
            continue
        name = clean_company_name(re.sub(r"<span.*", "", r.get("IPO") or "", flags=re.S))
        lp = strip_tags(r.get("Listing Price"))
        ltp = strip_tags(r.get("Closing Price (LTP)"))
        ltp_v = num(ltp.split("(")[0]) if "₹-" not in ltp else None
        out.append({
            "ig_id": iid, "name": name, "symbol": (r.get("Symbol") or "").strip() or None,
            "listing": dstr(parse_date(r.get("Listing Dt"))),
            "board": "SME" if r.get("~IPO_Category") == "SME" else "Mainboard",
            "size_cr": num(r.get("Size")), "sub_total": num(r.get("Sub")),
            "final_gmp": num(r.get("GMP")), "issue_price": num(r.get("Price")),
            "est_listing": num(strip_tags(r.get("Est Price"))),
            "listing_price": num(lp.split("(")[0]) if lp else None,
            "listing_day_close": num(strip_tags(r.get("Listing Day Close"))),
            "ltp": ltp_v if ltp_v else None,
            "listing_gain_pct": num(r.get("~str_listing_gain_in_per")),
            "ltp_gain_pct": num(r.get("~str_ltp_per_calc")) if ltp_v else None,
            "updated": r.get("~Last Updated"),
        })
    return out


_IG_HTML_FIELDS = ("about_company", "company_desc", "financial", "issue_objects", "promoters",
                   "anchor_investor_detail", "ipo_reservation_desc", "peer_analysis", "lotTableHtml")


def _ig_parse_gmp_rows(rows):
    out = []
    for g in rows or []:
        d = parse_date(g.get("gmp_date"))
        if not d:
            continue
        gv = g.get("gmp")
        gmp = num(gv) if gv not in (None, "", "--", "-") else None
        p = g.get("gmp_percent_raw")
        if p in (None, ""):
            p = g.get("gmp_percent_calc")
        out.append({
            "date": d.isoformat(), "gmp": gmp,
            "gmp_pct": num(p) if gmp is not None else None,
            "est_listing": num(g.get("estimated_listing_price")),
            "sub2": (g.get("sub2") or "").strip() if (g.get("sub2") or "").strip() not in ("--", "-") else None,
            "est_profit": num(g.get("est_profit")),
            "updated": g.get("last_updated") or g.get("last_updated_gmp"),
            "trend": g.get("trend_dir") or None,
        })
    out.sort(key=lambda x: x["date"])
    return out


def ig_gmp_page(http, slug, iid):
    """Full IPO detail + complete day-wise GMP history (InvestorGain /gmp/ page)."""
    r = http.get(f"{IG}/gmp/{slug}/{iid}/", timeout=30)
    b = rsc_bytes(r.text)
    full = b.decode("utf-8", "replace")
    rows = rsc_rows(b)
    ipo = json_after(full, '"ipoData":')
    if not ipo:
        raise ValueError("InvestorGain: ipoData not found")
    d = dict(ipo[0] if isinstance(ipo, list) else ipo)
    for k in _IG_HTML_FIELDS:
        if k in d:
            d[k] = resolve_ref(d.get(k), rows)
    gmp = _ig_parse_gmp_rows(json_after(full, '"gmpData":') or [])
    lms = json_after(full, '"ipoLeadManagersList":') or []
    reg = json_after(full, '"registrarInfo":') or []
    return {"ipo": d, "gmp": gmp, "lead_managers": [strip_tags(x.get("comp_name")) for x in lms if isinstance(x, dict)],
            "registrar": reg[0] if isinstance(reg, list) and reg else None,
            "url": f"{IG}/gmp/{slug}/{iid}/"}


def _bid_row(x):
    f = lambda k: num(x.get(k))
    return {
        "as_of": x.get("bid_date"),
        "times": {"qib": f("qib"), "nii": f("nii"), "bnii": f("nii_big"), "snii": f("nii_small"),
                  "rii": f("rii"), "emp": f("emp"), "shareholder": f("shareholder"), "other": f("other"),
                  "total": f("total")},
        "offered": {"qib": f("qib_offered"), "nii": f("nii_offered"), "bnii": f("nii_offered_big"),
                    "snii": f("nii_offered_small"), "rii": f("rii_offered"), "emp": f("emp_offered"),
                    "shareholder": f("shareholder_offered"), "other": f("other_offered"), "total": f("total_offered")},
        "bid": {"qib": f("qib_shares_bid_for"), "nii": f("nii_shares_bid_for"), "bnii": f("nii_shares_bid_for_big"),
                "snii": f("nii_shares_bid_for_small"), "rii": f("rii_shares_bid_for"), "emp": f("emp_shares_bid_for"),
                "shareholder": f("shareholder_shares_bid"), "other": f("other_shares_bid_for"),
                "total": f("total_shares_bid_for")},
        "amount_cr": {"qib": f("qib_bid_amt"), "nii": f("nii_bid_amt"), "bnii": f("nii_bid_amt_big"),
                      "snii": f("nii_bid_amt_small"), "rii": f("rii_bid_amt"), "emp": f("emp_bid_amt"),
                      "shareholder": f("shareholder_bid_amt"), "other": f("other_bid_amt"), "total": f("total_bid_amt")},
    }


def ig_subscription_page(http, slug, iid):
    """Day-wise, category-wise share subscription (InvestorGain /subscription/ page)."""
    r = http.get(f"{IG}/subscription/{slug}/{iid}/", timeout=30)
    b = rsc_bytes(r.text)
    full = b.decode("utf-8", "replace")
    rows = rsc_rows(b)
    bd = json_after(full, '"biddingData":') or {}
    bids = [_bid_row(x) for x in (bd.get("ipoBiddingData") or []) if isinstance(x, dict)]
    summary = _ig_bid_summary(resolve_ref(bd.get("sResultIPOBidding"), rows) if bd.get("sResultIPOBidding") else "")
    with_anchor = _ig_bid_summary(resolve_ref(bd.get("sResultIPOBiddingWithAnchor"), rows)
                                  if bd.get("sResultIPOBiddingWithAnchor") else "")
    return {"bids": bids, "summary": summary, "with_anchor": with_anchor, "url": f"{IG}/subscription/{slug}/{iid}/"}


_IG_SUM_LABELS = {"QIB (Total)": "qib_total", "QIB (Ex Anchor)": "qib", "QIB": "qib", "Anchor": "anchor",
                  "NII": "nii", "S-NII": "snii", "B-NII": "bnii", "RII": "rii", "Employee": "emp",
                  "Shareholder": "shareholder", "Overall (With Anchor)": "total", "Overall": "total"}


def _ig_bid_summary(h):
    """Parse InvestorGain's bidding summary cards -> [{cat, times, chance}] (chance = 'allotment 1 in N')."""
    if not h:
        return []
    toks = [t.strip() for t in _bs(h).get_text(" | ", strip=True).split(" | ")]
    out, seen = [], set()
    for i, t in enumerate(toks):
        key = _IG_SUM_LABELS.get(t)
        if not key or key in seen:
            continue
        times = chance = None
        for t2 in toks[i + 1:i + 5]:
            m = re.fullmatch(r"([\d.]+)x", t2)
            if m and times is None:
                times = float(m.group(1))
                continue
            m = re.search(r"~?1 out of ([\d,]+)", t2)
            if m:
                chance = num(m.group(1))
                break
            if t2 in _IG_SUM_LABELS:
                break
        if times is not None:
            seen.add(key)
            out.append({"cat": key, "times": times, "chance_1_in": chance})
    return out


# ============================================================================ Chittorgarh
def cg_detail(http, slug, cg_id):
    """Chittorgarh IPO page: total applications, basis-of-allotment (applications/allottees), listing close."""
    r = http.get(f"{CG}/ipo/{slug}/{cg_id}/", timeout=75, retries=1)
    full = rsc_bytes(r.text).decode("utf-8", "replace")
    sub = json_after(full, '"subscriptionDataResponse":') or {}
    listing = json_after(full, '"listing_detail":') or {}
    bids = sub.get("ipoBiddingDetails") or []
    last = bids[-1] if bids else {}
    boa = []
    for x in sub.get("ipoBoaDetails") or []:
        boa.append({
            "category": x.get("category_name"), "shares_offered": num(x.get("no_of_shares")),
            "applications": num(x.get("no_of_applications")), "shares_applied": num(x.get("shares_applied")),
            "times": num(x.get("times_subscribed")), "allottees": num(x.get("no_of_allottees")),
            "shares_allotted": num(x.get("no_of_shares_allotted")),
        })
    # Chittorgarh keeps the category-wise bidding table for old issues long after InvestorGain has
    # dropped it down to a single total line, so the same page is our fallback source for the
    # share-wise subscription table.
    cats = (("qib", "qib_offered", "qib_shares_bid_for"), ("nii", "nii_offered", "nii_shares_bid_for"),
            ("bnii", "nii_offered_big", "nii_shares_bid_for_big"), ("snii", "nii_offered_small", "nii_shares_bid_for_small"),
            ("rii", "rii_offered", "rii_shares_bid_for"), ("emp", "emp_offered", "emp_shares_bid_for"),
            ("other", "other_offered", "other_shares_bid_for"))
    bid_row = None
    if last:
        bid_row = {"as_of": last.get("bid_date"),
                   "offered": {k: num(last.get(off_k)) for k, off_k, _ in cats},
                   "bid": {k: num(last.get(bid_k)) for k, _, bid_k in cats},
                   "times": {"qib": num(last.get("qib")), "nii": num(last.get("nii")),
                             "bnii": num(last.get("nii_big")), "snii": num(last.get("nii_small")),
                             "rii": num(last.get("rii")), "emp": num(last.get("emp")), "other": num(last.get("other"))}}
        bid_row["offered"]["total"] = num(last.get("total_offered"))
        bid_row["bid"]["total"] = num(last.get("total_shares_bid_for"))
        bid_row["times"]["total"] = num(last.get("total"))
        for k in ("offered", "bid"):
            bid_row[k] = {a: b for a, b in bid_row[k].items() if b is not None}
    return {
        "url": r.url,
        "total_applications": num(last.get("total_application")),
        "bid_as_of": last.get("bid_date"),
        "bids": [bid_row] if bid_row else [],
        "times": {"qib": num(last.get("qib")), "nii": num(last.get("nii")), "bnii": num(last.get("nii_big")),
                  "snii": num(last.get("nii_small")), "rii": num(last.get("rii"))} if last else {},
        "boa": boa,
        "listing_day_close": num(listing.get("ildt_close_price")) if isinstance(listing, dict) else None,
        "locked": bool(sub.get("isLocked")),
    }


# ============================================================================ Narada
def _bs(html):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


_ND_GMP = re.compile(r"GMP\s*([+\-−]?)\s*₹\s*([\d,.]+)\s*\(\s*([+\-−]?[\d.]+)%\)")
_ND_BAND = re.compile(r"₹\s*([\d,.]+)\s*[–\-]\s*([\d,.]+)")


def _nd_signed(sign, v):
    x = num(v)
    if x is None:
        return None
    return -x if sign in ("-", "−") else x


def _nd_list_item(a):
    sym = a["href"].strip("/").split("/")[-1]
    name_el = a.find("span", class_=lambda c: c and "font-semibold" in c)
    name = clean_company_name(name_el.get_text()) if name_el else sym
    sme = any(s.get_text(strip=True) == "SME" for s in a.find_all("span"))
    text = a.get_text(" ", strip=True)
    item = {"sym": sym, "name": name, "board": "SME" if sme else "Mainboard"}
    img = a.find("img")
    if img and img.get("src", "").startswith("http"):
        item["logo"] = img["src"]
    m = _ND_BAND.search(text)
    if m and "Issued" not in text:
        item["price_low"], item["price_high"] = num(m.group(1)), num(m.group(2))
    m = _ND_GMP.search(text)
    if m:
        item["gmp"] = _nd_signed(m.group(1), m.group(2))
        item["gmp_pct"] = _nd_signed("-" if m.group(3).startswith(("-", "−")) else "", m.group(3).lstrip("+-−"))
    m = re.search(r"([\d.]+)x subscribed", text)
    if m:
        item["sub_total"] = float(m.group(1))
    m = re.search(r"Issued ₹([\d,.]+)", text)
    if m:
        item["issue_price"] = num(m.group(1))
    m = re.search(r"Listed ₹([\d,.]+)\s*\(([+\-−]?[\d.]+)%\)", text)
    if m:
        item["listing_price"], item["listing_pct"] = num(m.group(1)), num(m.group(2).replace("−", "-"))
    m = re.search(r"Now ₹([\d,.]+)\s*\(([+\-−]?[\d.]+)%\)", text)
    if m:
        item["current_price"], item["current_pct"] = num(m.group(1)), num(m.group(2).replace("−", "-"))
    m = re.search(r"(Opens|Closes|Closed|Allotment|Lists|Listing|Listed)\s+([A-Z][a-z]{2},\s*\d{1,2}\s+[A-Z][a-z]{2}(?:\s+\d{4})?)", text)
    if m:
        item["event"], item["event_text"] = m.group(1), m.group(2)
    return item


def nd_home(http):
    """Narada home: every current IPO grouped by status (UPCOMING/OPEN/CLOSED/ALLOTTED/...)."""
    r = http.get(ND + "/", timeout=30)
    soup = _bs(r.text)
    out = []
    today = today_ist()
    for sec in soup.find_all("section"):
        h2 = sec.find("h2")
        if not h2:
            continue
        label = h2.get_text(strip=True).upper()
        for a in sec.find_all("a", href=re.compile(r"^/ipos/[^/]+/$")):
            it = _nd_list_item(a)
            it["nd_section"] = label
            if it.get("event_text"):
                mode = "future" if it["event"] in ("Opens", "Closes", "Lists", "Allotment") else "nearest"
                d = parse_date(it["event_text"], ref=today, mode=mode)
                it["event_date"] = dstr(d)
            out.append(it)
    return out


def nd_listed(http, since_date, max_pages=40):
    """Narada 'listed' feed: issue -> listing -> current price for every listed IPO since `since_date`."""
    out, ref = [], today_ist()
    for page in range(1, max_pages + 1):
        r = http.get(f"{ND}/ipos/listed/?page={page}", timeout=30)
        soup = _bs(r.text)
        items = soup.find_all("a", href=re.compile(r"^/ipos/[^/]+/$"))
        if not items:
            break
        stop = False
        for a in items:
            it = _nd_list_item(a)
            if it.get("event_text"):
                d = parse_date(it["event_text"], ref=ref, mode="past")
                if d:
                    ref = d
                    it["listing_date"] = d.isoformat()
                    if d < since_date:
                        stop = True
                        continue
            it["nd_section"] = "LISTED"
            out.append(it)
        if stop or "load-more-listed" not in r.text:
            break
    return out


_ND_QUOTA = {"QIB": "qib", "FII": "fii", "DFI": "dfi", "MF": "mf", "Other": "other_qib", "Others": "other_qib",
             "NII": "nii", "bNII": "bnii", "sNII": "snii", "RII": "rii", "IND": "rii", "Individual": "rii",
             "Individuals": "rii", "EMP": "emp", "Employee": "emp",
             "Employees": "emp", "SHA": "shareholder", "Shareholder": "shareholder", "Shareholders": "shareholder",
             "Total": "total", "Anchor": "anchor", "Policyholder": "policyholder", "Policyholders": "policyholder"}


def _nd_table(tbl):
    rows = []
    if not tbl:
        return rows
    for tr in tbl.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        first = next(iter(tds[0].stripped_strings), "").strip()
        key = _ND_QUOTA.get(first, first.lower())
        vals = [tds[i].get_text(" ", strip=True) for i in (1, 2, 3)]
        rows.append({"cat": key, "label": first,
                     "a": num(vals[0]) if vals[0] not in ("-", "") else None,
                     "b": num(vals[1]) if vals[1] not in ("-", "") else None,
                     "times": num(vals[2]) if vals[2] not in ("-", "") else None})
    return rows


def nd_detail(http, sym):
    """Narada IPO page: timeline, share-wise (₹ Cr) and application-wise subscription, sizes, intermediaries."""
    r = http.get(f"{ND}/ipos/{sym}/", timeout=30)
    soup = _bs(r.text)
    main = soup.find("main") or soup
    out = {"sym": sym, "url": f"{ND}/ipos/{sym}/"}
    h1 = main.find("h1")
    if h1:
        out["name"] = clean_company_name(h1.get_text())
        meta = h1.find_next("div")
        if meta:
            mt = meta.get_text(" ", strip=True)
            parts = [p.strip() for p in mt.split("·")]
            if parts:
                out["symbol"] = parts[0] or sym
            if len(parts) > 1:
                out["exchanges"] = parts[1]
            if len(parts) > 2:
                out["nd_status"] = parts[2]
            a = meta.find("a", href=True)
            if a and a["href"].startswith("http"):
                out["website"] = a["href"]
    text = main.get_text(" | ", strip=True)
    m = re.search(r"Price range \| ₹([\d,.]+)\s*[–\-]\s*([\d,.]+)", text)
    if m:
        out["price_low"], out["price_high"] = num(m.group(1)), num(m.group(2))
    m = re.search(r"Issue price \| ₹([\d,.]+)", text)
    if m:
        out["issue_price"] = num(m.group(1))
    m = re.search(r"GMP(?: \|[^|₹]*)*? \| ([+\-−]?)₹([\d,.]+) \| \(([+\-−]?[\d.]+)%\)", text) or \
        re.search(r"GMP[^₹]{0,80}([+\-−]?)₹([\d,.]+)\s*\(([+\-−]?[\d.]+)%\)", text)
    if m:
        out["gmp"] = _nd_signed(m.group(1), m.group(2))
        out["gmp_pct"] = num(m.group(3).replace("−", "-"))
    m = re.search(r"Listing price[^₹]{0,80}₹([\d,.]+)(?: \|)? \(([+\-−]?[\d.]+)%\)", text)
    if m:
        out["listing_price"], out["listing_pct"] = num(m.group(1)), num(m.group(2).replace("−", "-"))
    m = re.search(r"Current price[^₹]{0,80}₹([\d,.]+)(?: \|)? \(([+\-−]?[\d.]+)%\)", text)
    if m:
        out["current_price"], out["current_pct"] = num(m.group(1)), num(m.group(2).replace("−", "-"))
    m = re.search(r"Lot size \| ([\d,]+) shares", text)
    if m:
        out["lot"] = num(m.group(1))
    m = re.search(r"Issue size \| ₹([\d,.]+) Cr", text)
    if m:
        out["size_cr"] = num(m.group(1))
    # timeline: <div>Label</div><a href="/ipos/calendar/?month=YYYY-MM#YYYY-MM-DD">
    tl = {}
    for a in main.find_all("a", href=re.compile(r"/ipos/calendar/\?month=")):
        lab_el = a.find_previous_sibling("div")
        frag = a["href"].split("#")[-1]
        d = parse_date(frag) or parse_date(a.get_text())
        if lab_el and d:
            tl[lab_el.get_text(strip=True)] = d.isoformat()
    out["timeline"] = tl
    out["sub_shares"] = _nd_table(main.find("table", attrs={"data-table": "shares"}))
    out["sub_apps"] = _nd_table(main.find("table", attrs={"data-table": "applications"}))
    # application size
    sizes = []
    apply_div = main.find(attrs={"data-apply": True})
    if apply_div:
        for tr in apply_div.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            row = {"quota": tds[0].get_text(strip=True)}
            for key, td in (("min", tds[1]), ("max", tds[2])):
                btn = td.find(attrs={"data-copy": True})
                t = td.get_text(" ", strip=True)
                lm = re.search(r"(\d+)\s+lots?\s*·\s*₹([\d,]+)", t)
                row[key] = {"shares": num(btn["data-copy"]) if btn else None,
                            "lots": num(lm.group(1)) if lm else None,
                            "amount": num(lm.group(2)) if lm else None,
                            "text": "No limit" if "No limit" in t else None}
            sizes.append(row)
    out["app_sizes"] = sizes
    inter = {}
    for dl in main.find_all("dl"):
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                vals = [s.get_text(" ", strip=True) for s in dd.find_all("span", recursive=False)] or \
                       [dd.get_text(" ", strip=True)]
                inter[dt.get_text(strip=True)] = [v for v in vals if v]
    out["intermediaries"] = inter
    lu = main.find(attrs={"data-last-updated": True})
    out["fetched"] = now_ist().isoformat(timespec="seconds")
    return out


# ============================================================================ NSE
class NseClient:
    """NSE needs browser-like cookies. Works from Indian residential/broadband IPs; cloud IPs get 403."""

    def __init__(self, http):
        self.http = http
        self.s = None
        self.primed_at = 0
        self.lock = threading.Lock()

    def _session(self):
        with self.lock:
            if self.s is None or time.time() - self.primed_at > 600:
                s = requests.Session()
                s.headers.update({"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
                                  "Referer": NSE + "/market-data/all-upcoming-issues-ipo"})
                self.http.get(NSE + "/market-data/all-upcoming-issues-ipo", session=s, timeout=20, retries=1,
                              headers={"Accept": "text/html,application/xhtml+xml"})
                self.s, self.primed_at = s, time.time()
            return self.s

    def api(self, path, params=None):
        for attempt in range(2):
            s = self._session()                     # raises SourceBlocked if NSE refuses this network
            try:
                r = self.http.get(NSE + "/api/" + path, session=s, params=params, timeout=20, retries=1,
                                  allow_blocked=attempt > 0)
                return r.json()
            except SourceBlocked:
                if attempt:
                    raise
                with self.lock:                     # 401/403 on the API usually = expired cookies -> re-prime
                    self.s = None
                self.http.blocked_until.pop("www.nseindia.com", None)
        return None

    def current(self):
        return self.api("ipo-current-issue")

    def upcoming(self):
        return self.api("all-upcoming-issues", {"category": "ipo"})

    def past(self, from_d, to_d):
        return self.api("public-past-issues", {"from_date": from_d.strftime("%d-%m-%Y"),
                                                "to_date": to_d.strftime("%d-%m-%Y")})

    def active_category(self, symbol):
        """Official live category-wise bids for an open IPO."""
        j = self.api("ipo-active-category", {"symbol": symbol})
        rows = j.get("dataList") if isinstance(j, dict) else j
        out = []
        for x in rows or []:
            out.append({"category": x.get("category"), "offered": num(x.get("noOfShareOffered")),
                        "bid": num(x.get("noOfSharesBid")), "times": num(x.get("noOfTotalMeant"))})
        return {"rows": out, "as_of": (j.get("updateTime") if isinstance(j, dict) else None)}

    def quote(self, symbol, series="EQ"):
        s = self._session()
        try:
            r = self.http.get(NSE + "/api/NextApi/apiClient/GetQuoteApi", session=s, timeout=20, retries=0,
                              params={"functionName": "getSymbolData", "marketType": "N",
                                      "series": series or "EQ", "symbol": symbol})
            q = r.json()["equityResponse"][0]
            md = q.get("metaData") or {}
            ltp = num((q.get("orderBook") or {}).get("lastPrice")) or num((q.get("tradeInfo") or {}).get("lastPrice"))
            if ltp:
                return {"ltp": ltp, "prev_close": num(md.get("previousClose")), "open": num(md.get("open")),
                        "high": num(md.get("dayHigh")), "low": num(md.get("dayLow")), "pchange": num(md.get("pChange")),
                        "as_of": q.get("lastUpdateTime")}
        except SourceBlocked:
            raise
        except Exception:
            pass
        r = self.http.get(NSE + "/api/quote-equity", session=s, params={"symbol": symbol}, timeout=20, retries=0)
        j = r.json()
        pi = j.get("priceInfo") or {}
        return {"ltp": num(pi.get("lastPrice")), "prev_close": num(pi.get("previousClose")), "open": num(pi.get("open")),
                "high": num((pi.get("intraDayHighLow") or {}).get("max")),
                "low": num((pi.get("intraDayHighLow") or {}).get("min")), "pchange": num(pi.get("pChange")),
                "as_of": (j.get("metadata") or {}).get("lastUpdateTime")}


def nse_parse_listing(rows, kind):
    out = []
    for x in rows or []:
        pr = x.get("issuePrice") or x.get("priceRange") or ""
        nums = [num(v) for v in re.findall(r"[\d.]+", str(pr).replace(",", ""))]
        nums = [v for v in nums if v]
        out.append({
            "symbol": x.get("symbol"), "name": clean_company_name(x.get("companyName") or x.get("company")),
            "series": x.get("series"), "kind": kind,
            "open": dstr(parse_date(x.get("issueStartDate") or x.get("ipoStartDate"))),
            "close": dstr(parse_date(x.get("issueEndDate") or x.get("ipoEndDate"))),
            "listing": dstr(parse_date(x.get("listingDate"))) if x.get("listingDate") not in (None, "-") else None,
            "price_low": nums[0] if nums else None, "price_high": nums[-1] if nums else None,
            "board": "SME" if (x.get("securityType") == "SME" or x.get("series") in ("SM", "ST")) else None,
            "sub_total": num(x.get("noOfTime")) if x.get("noOfTime") else None,
            "status": x.get("status"),
        })
    return out


# ============================================================================ BSE
def bse_quote(http, scripcode):
    r = http.get(f"{BSE_API}/getScripHeaderData/w", timeout=20, retries=1,
                 params={"Debtflag": "", "scripcode": scripcode, "seriesid": ""},
                 headers={"Accept": "application/json, text/plain, */*", "Origin": BSE_WWW, "Referer": BSE_WWW + "/"})
    j = r.json()
    h = j.get("Header") or {}
    cr = j.get("CurrRate") or {}
    ltp = num(h.get("LTP")) or num(cr.get("LTP"))
    if not ltp:
        raise ValueError("BSE quote: no LTP")
    return {"ltp": ltp, "prev_close": num(h.get("PrevClose")), "open": num(h.get("Open")), "high": num(h.get("High")),
            "low": num(h.get("Low")), "pchange": num(cr.get("PcChg")), "as_of": h.get("Ason")}


def bse_bhavcopy(http, d):
    """Official end-of-day prices for every BSE scrip (incl. BSE SME) for date d -> {scripcode: row}."""
    url = f"{BSE_WWW}/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d.strftime('%Y%m%d')}_F_0000.CSV"
    r = http.get(url, timeout=40, retries=1, headers={"Referer": BSE_WWW + "/"})
    if "TckrSymb" not in r.text[:400]:
        raise LookupError("bhavcopy not published")
    out = {}
    for x in csv.DictReader(io.StringIO(r.text)):
        code = (x.get("FinInstrmId") or "").strip()
        if not code:
            continue
        out[code] = {"symbol": x.get("TckrSymb"), "isin": x.get("ISIN"), "open": num(x.get("OpnPric")),
                     "high": num(x.get("HghPric")), "low": num(x.get("LwPric")), "close": num(x.get("ClsPric")),
                     "prev_close": num(x.get("PrvsClsgPric")), "volume": num(x.get("TtlTradgVol"))}
    return out


def nse_bhavcopy(http, d):
    """Official NSE end-of-day file (UDiFF) -> {symbol: row}. Usually blocked from cloud IPs."""
    url = f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip"
    r = http.get(url, timeout=40, retries=1, headers={"Referer": NSE + "/"})
    z = zipfile.ZipFile(io.BytesIO(r.content))
    txt = z.read(z.namelist()[0]).decode("utf-8", "replace")
    out = {}
    for x in csv.DictReader(io.StringIO(txt)):
        sym = (x.get("TckrSymb") or "").strip()
        if sym and x.get("SctySrs") in ("EQ", "BE", "BZ", "SM", "ST", "SZ"):
            out[sym] = {"series": x.get("SctySrs"), "isin": x.get("ISIN"), "open": num(x.get("OpnPric")),
                        "high": num(x.get("HghPric")), "low": num(x.get("LwPric")), "close": num(x.get("ClsPric")),
                        "prev_close": num(x.get("PrvsClsgPric"))}
    return out


def yahoo_daily(http, symbol, day, span=8):
    """Daily candles around one date from Yahoo (exchange EOD data).

    Used only for listing-day figures on exchanges whose archive we cannot reach: pass the
    exchange symbol (e.g. 'SRTL.NS'), get back [{'date','open','high','low','close','volume'}].
    """
    import datetime as _dt
    import json as _json
    p1 = int(_dt.datetime.combine(day - _dt.timedelta(days=span), _dt.time()).timestamp())
    p2 = int(_dt.datetime.combine(day + _dt.timedelta(days=4), _dt.time()).timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1={p1}&period2={p2}&interval=1d")
    r = http.get(url, timeout=25, retries=1, headers={"User-Agent": "Mozilla/5.0 (IPO Desk)"})
    j = _json.loads(r.text)
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        raise ValueError("yahoo: no chart data")
    res = res[0]
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    vol = q.get("volume") or []
    out = []
    for i, t in enumerate(ts):
        try:
            d = _dt.datetime.fromtimestamp(t, _dt.timezone.utc).date().isoformat()
        except Exception:
            continue
        row = {"date": d, "open": num(q["open"][i]), "high": num(q["high"][i]),
               "low": num(q["low"][i]), "close": num(q["close"][i]),
               "volume": num(vol[i]) if i < len(vol) else None}
        if row["close"] is not None:
            out.append(row)
    return out
