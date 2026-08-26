"""
daily_ipo_update.py  (v8 — investorgain REMOVED, FinAPI + one-time cached
Fresh Issue/OFS enrichment, everything else from trynarada)
------------------------------------------------------------------------
DATA SOURCES (v8):
  - trynarada.com          → everything: list, prices, GMP, subscription
                             (shares + applications), timeline, application
                             sizes, intermediaries, listing/current prices.
  - FinAPI (fna_live key)  → ONLY Fresh Issue / OFS, matched by symbol/name.
                             NOTE: verified 2026-08-26 — their /api/ipo does
                             NOT expose fresh/ofs fields yet, so the call is
                             made at most ONCE PER RUN (quota-safe, ~120/day
                             limit) and values flow automatically if/when the
                             provider adds those fields.
  - IPO Central (fallback) → Fresh Issue / OFS when FinAPI has none, fetched
                             at most ONCE per new IPO name and cached forever
                             in fresh_ofs_cache.json (negatives retried only
                             after 3 days). Running extra times per day never
                             re-fetches old names — daily limits stay safe.

STILL TRUE FROM v6/v7: run the script once every hour (Windows Task Scheduler,
9 AM-10 PM) for the GMP twice-daily average and the hourly subscription log on
closing days. The script decides what to record from the current time + each
IPO's own dates; extra runs are harmless.

Mainboard only (SME auto-skipped). Independent sources, not official NSE/BSE
feeds — sanity-check occasionally.

SETUP (once): pip install requests beautifulsoup4 python-dateutil
"""

import json
import os
import re
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

BASE_URL = "https://trynarada.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal IPO tracking script)"}
IPOC_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) personal IPO tracker"}
REQUEST_DELAY_SECONDS = 1.0

GMP_SESSION_LOG_PATH = Path("gmp_session_log.json")   # raw morning/evening readings
OLD_GMP_LOG_PATH = Path("gmp_log.json")                # earlier script version's log file
QIB_INTRADAY_LOG_PATH = Path("qib_intraday_log.json")  # hourly readings on closing day
OUTPUT_JSON_PATH = Path("ipo_data.json")
FRESH_OFS_CACHE_PATH = Path("fresh_ofs_cache.json")    # name -> split, fetched ONCE ever
NOW = datetime.now()
TODAY = NOW.date()
CURRENT_HOUR = NOW.hour

# ----------------- FinAPI (your fna_live key) ----------------- #
# The key is read from (in order): the FNA_API_KEY environment variable, or a
# local fna_key.txt file next to this script. Never hardcode it here — this
# script may live in a public GitHub repo (the cloud auto-scrape setup), where
# the key must come from a repository SECRET instead.
FNA_API_BASE = "https://api.finapi.upvaly.com/api"


def _load_fna_key():
    v = os.environ.get("FNA_API_KEY", "").strip()
    if v:
        return v
    p = Path("fna_key.txt")
    if p.exists():
        return p.read_text().strip()
    return ""


FNA_API_KEY = _load_fna_key()

# ----------------- IPO Central (one-time Fresh/OFS fallback) ----------------- #
IPOC_WP_SEARCH = "https://ipocentral.in/wp-json/wp/v2/posts"
# verified 2026-08: every main IPO page (the one carrying the "IPO Details"
# table with Fresh Issue / OFS) contains "-ipo-gmp-price" in its slug
# (…-ipo-gmp-price-allotment / -gmp-price-date-details / -allotment-profit …).
# Everything else on their site is news articles and never contains this marker.
IPOC_MAIN_MARKER = "-ipo-gmp-price"
IPOC_NEGATIVE_RETRY_DAYS = 3   # only re-attempt a failed lookup after this many days


def to_float(s):
    if s is None:
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(s).replace(",", ""))
    return float(m.group()) if m else None


def to_int(s):
    f = to_float(s)
    return int(f) if f is not None else None


# ------------------------------------------------------------------ #
# FRESH ISSUE / OFS  —  FinAPI first (1 call/run), IPO Central fallback
# (one page-fetch per NEW IPO name, permanently cached). Nothing else is
# ever taken from these two — all other data comes from trynarada.
# ------------------------------------------------------------------ #
def fetch_fna_issue_map():
    """ONE keyed FinAPI call per run (NOT per IPO) so the daily limit
    (~120 endpoint / 300 global) is never at risk. Returns {SYMBOL: record}.
    As of 2026-08-26 the records contain schedule/GMP only — fresh/ofs fields
    don't exist yet — but if the provider adds them, they flow in with no
    code change (see split_from_fna_record)."""
    if not FNA_API_KEY:
        print("  [i] No FinAPI key found (set FNA_API_KEY env var or fna_key.txt) — using cache/fallback only")
        return {}
    try:
        resp = requests.get(f"{FNA_API_BASE}/ipo",
                            headers={"X-API-Key": FNA_API_KEY, **HEADERS}, timeout=20)
        if resp.status_code == 429:
            print("  [i] FinAPI daily limit reached — skipping API this run (cache/fallback still apply)")
            return {}
        resp.raise_for_status()
        data = resp.json().get("data") or []
        print(f"  [i] FinAPI: {len(data)} IPO records (1 keyed call)")
        return {(r.get("symbol") or "").upper(): r for r in data}
    except requests.RequestException as e:
        print(f"  [i] FinAPI call failed ({e}) — continuing with cache/fallback")
        return {}
    except ValueError as e:
        print(f"  [i] FinAPI returned non-JSON ({e}) — continuing with cache/fallback")
        return {}


def split_from_fna_record(rec):
    """Dig {fresh, ofs} out of a FinAPI record if such fields exist
    (case-insensitive scan; works whether values are numbers or strings
    like '₹150 crore'). Returns (fresh, ofs) — (None, None) when absent."""
    found = {"fresh": None, "ofs": None}

    def scan(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str):
                    kl, val = k.lower(), to_float(v)
                    if val is None or val == 0:
                        continue
                    if found["fresh"] is None and "fresh" in kl:
                        found["fresh"] = val
                    elif found["ofs"] is None and ("ofs" in kl or "offerforsale" in re.sub(r"[^a-z]", "", kl)):
                        found["ofs"] = val
                else:
                    scan(v)
        elif isinstance(node, list):
            for x in node:
                scan(x)

    scan(rec)
    return found["fresh"], found["ofs"]


def clean_company_query(name):
    """'Symbiotec Pharmalab Limited' -> 'Symbiotec Pharmalab' (for searching)."""
    q = re.sub(r"\s*\(.*?\)\s*", " ", name)              # drop parenthesised bits
    q = re.sub(r"\b(Private|Pvt\.?|Limited|Ltd\.?|India)\b\s*", " ", q, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", q).strip()


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def search_ipocentral_url(query_name):
    """Find the main IPO page on IPO Central via their WordPress REST search.
    Their slugs are often SHORTER than the company name (e.g. company
    'Milky Mist Dairy Food' -> slug 'milky-mist-ipo-gmp-price-date-allotment'),
    so we score candidates by token overlap and only accept the strict
    main-page suffix shapes. Returns the URL or None."""
    q = clean_company_query(query_name)
    qtokens = set(q.lower().split())

    def find_in(posts):
        best = None  # ((overlap, base_len, -slug_len), link)
        for p in posts:
            link = p.get("link") or ""
            slug = urlparse(link).path.strip("/")
            if not slug:
                continue
            idx = slug.find(IPOC_MAIN_MARKER)
            if idx == -1:
                continue  # news article, not a main IPO page
            base = slug[:idx]
            base_tokens = set(t for t in base.split("-") if t)
            overlap = len(base_tokens & qtokens)
            if overlap < 2 and overlap < 0.4 * len(qtokens):
                continue  # too little in common — an unrelated article
            score = (overlap, len(base_tokens), -len(slug))
            if best is None or score > best[0]:
                best = (score, link)
        return best[1] if best else None

    for query in (f"{q} ipo", q):
        try:
            resp = requests.get(IPOC_WP_SEARCH, params={"per_page": 30, "search": query},
                                headers=IPOC_HEADERS, timeout=20)
            if resp.status_code != 200:
                print(f"  [!] IPO Central search HTTP {resp.status_code} for '{query}'")
                continue
            url = find_in(resp.json() or [])
            if url:
                return url
        except (requests.RequestException, ValueError) as e:
            print(f"  [!] IPO Central search failed for '{query}': {e}")
    return None


def cell_to_crore(text):
    """Extract the issue value in ₹ crore from cells like:
      'INR 720 crore'                         -> 720.0
      '9,12,99,203 shares (INR 839.95 – 885.6 crore)' -> 885.6 (cap price)
      '4,57,50,000 shares'                    -> None (share count, no ₹ unit)
      'Nil'                                   -> None
    Only numbers DIRECTLY followed by a unit count, so a share count with a
    parenthetical ₹ range can never be mistaken for the value. When a range
    is given, the LAST unit-attached number is used (the cap-price value,
    which is how trynarada reports issue size too)."""
    t = str(text).lower()
    matches = re.findall(r"([\d,]+(?:\.\d+)?)\s*(crore|\bcr\b|lakh|\blac\b|million)", t)
    if not matches:
        return None
    num, unit = matches[-1]
    val = to_float(num)
    if val is None:
        return None
    if unit in ("crore", "cr"):
        return val
    if unit in ("lakh", "lac"):
        return val / 100.0
    return val / 10.0  # million


def parse_split_from_page(html):
    """Parse the 'IPO Details' table: rows like
      <td>Fresh Issue</td><td>INR 150 crore</td>
      <td>Offer For Sale</td><td>INR 1,607 crore</td>
    Only accepts values stated in crore/lakh/million — never bare share
    counts. Returns (fresh_cr, ofs_cr, total_cr) — any may be None."""
    soup = BeautifulSoup(html, "html.parser")
    fresh = ofs = total = None
    for td in soup.find_all("td"):
        label = td.get_text(" ", strip=True).lower().rstrip(": ")
        val_td = td.find_next_sibling("td")
        if val_td is None:
            continue
        val = cell_to_crore(val_td.get_text(" ", strip=True))
        if val is None:
            continue
        if fresh is None and label in ("fresh issue", "fresh issue component", "fresh"):
            fresh = val
        elif ofs is None and label in ("offer for sale", "ofs", "offer for sale (ofs)"):
            ofs = val
        elif total is None and label in ("total ipo size", "total issue size", "issue size",
                                         "total issue size (fresh + ofs)"):
            total = val
    flat_text = soup.get_text(" ", strip=True)
    # Regex fallback ONLY when the table parse produced nothing at all —
    # otherwise loose text like "Offer For Sale Nil … Fresh Issue INR 720 crore"
    # poisons an otherwise-correct table parse.
    if fresh is None and ofs is None:
        m = re.search(r"Fresh Issue[^0-9]{0,40}(?:INR|₹|Rs\.?)\s*([\d,]+(?:\.\d+)?)\s*(?:crore|cr\b)",
                      flat_text, re.IGNORECASE)
        if m:
            fresh = to_float(m.group(1))
        m = re.search(r"Offer For Sale[^0-9]{0,40}(?:INR|₹|Rs\.?)\s*([\d,]+(?:\.\d+)?)\s*(?:crore|cr\b)",
                      flat_text, re.IGNORECASE)
        if m:
            ofs = to_float(m.group(1))
    return fresh, ofs, total


def load_split_cache():
    if FRESH_OFS_CACHE_PATH.exists():
        try:
            return json.loads(FRESH_OFS_CACHE_PATH.read_text())
        except ValueError:
            print("  [!] fresh_ofs_cache.json is corrupt — starting a fresh cache")
    return {}


def enrich_issue_splits(ipos_out):
    """Attach fresh_issue_cr / ofs_cr to every IPO. Order: permanent cache →
    FinAPI (one call per run, by symbol) → IPO Central (one fetch per new
    name, then cached forever). Never re-fetches an already-cached name."""
    cache = load_split_cache()
    fna_map = fetch_fna_issue_map()

    from_cache = via_api = via_web = still_missing = 0
    cache_dirty = False

    for ipo in ipos_out:
        name = ipo["name"]
        entry = cache.get(name)

        # 1) permanent cache — the "fetch only once" guarantee
        if entry and entry.get("status") == "done":
            if entry.get("fresh_cr") is not None:
                ipo["fresh_issue_cr"] = entry["fresh_cr"]
            if entry.get("ofs_cr") is not None:
                ipo["ofs_cr"] = entry["ofs_cr"]
            ipo["issue_split_source"] = entry.get("source")
            ipo["issue_split_url"] = entry.get("url")
            ipo["issue_split_fetched_at"] = entry.get("fetched_at")
            from_cache += 1
            continue
        # negative entries: silently retry only after the back-off window
        if entry and entry.get("status") == "miss":
            age_days = (NOW - dtparser.parse(entry["last_attempt"])).days if entry.get("last_attempt") else 999
            if age_days < IPOC_NEGATIVE_RETRY_DAYS:
                still_missing += 1
                continue

        # 2) FinAPI record matched by trynarada slug (== NSE symbol)
        rec = fna_map.get((ipo.get("slug") or "").upper())
        if rec:
            fresh, ofs = split_from_fna_record(rec)
            if fresh is not None or ofs is not None:
                cache[name] = {"status": "done", "source": "FinAPI", "url": None,
                               "fresh_cr": fresh, "ofs_cr": ofs, "fetched_at": NOW.isoformat()}
                cache_dirty = True
                if fresh is not None:
                    ipo["fresh_issue_cr"] = fresh
                if ofs is not None:
                    ipo["ofs_cr"] = ofs
                ipo["issue_split_source"] = "FinAPI"
                ipo["issue_split_fetched_at"] = NOW.isoformat()
                via_api += 1
                continue

        # 3) IPO Central — one search + one page fetch, then cached forever
        url = search_ipocentral_url(name)
        fresh = ofs = total = None
        if url:
            try:
                page = requests.get(url, headers=IPOC_HEADERS, timeout=20)
                page.raise_for_status()
                fresh, ofs, total = parse_split_from_page(page.text)
                time.sleep(REQUEST_DELAY_SECONDS)
            except requests.RequestException as e:
                print(f"  [!] IPO Central fetch failed for '{name}': {e}")
        if fresh is not None or ofs is not None:
            cache[name] = {"status": "done", "source": "IPO Central", "url": url,
                           "fresh_cr": fresh, "ofs_cr": ofs, "fetched_at": NOW.isoformat()}
            cache_dirty = True
            if fresh is not None:
                ipo["fresh_issue_cr"] = fresh
            if ofs is not None:
                ipo["ofs_cr"] = ofs
            ipo["issue_split_source"] = "IPO Central"
            ipo["issue_split_url"] = url
            ipo["issue_split_fetched_at"] = NOW.isoformat()
            via_web += 1
        else:
            cache[name] = {"status": "miss", "url": url, "last_attempt": NOW.isoformat()}
            cache_dirty = True
            still_missing += 1
            print(f"  [i] No Fresh/OFS found for '{name}' (will retry in {IPOC_NEGATIVE_RETRY_DAYS} days)")

    if cache_dirty:
        FRESH_OFS_CACHE_PATH.write_text(json.dumps(cache, indent=2))
    print(f"  -> Issue split: {from_cache} from cache, {via_api} via FinAPI, "
          f"{via_web} via IPO Central, {still_missing} without data")


# ------------------------------------------------------------------ #
def migrate_old_gmp_log():
    """A previous version of this script logged to gmp_log.json (no session
    field). If that file exists, fold anything not already present into the
    new gmp_session_log.json, so past-IPO history isn't silently lost just
    because the filename changed between versions."""
    if not OLD_GMP_LOG_PATH.exists():
        return
    old = json.loads(OLD_GMP_LOG_PATH.read_text())
    new = json.loads(GMP_SESSION_LOG_PATH.read_text()) if GMP_SESSION_LOG_PATH.exists() else {}
    migrated = 0
    for name, entries in old.items():
        new.setdefault(name, [])
        existing_dates = {e["date"] for e in new[name]}
        for e in entries:
            if e["date"] not in existing_dates:
                new[name].append({"date": e["date"], "session": "legacy", "value": e["value"]})
                migrated += 1
    if migrated:
        GMP_SESSION_LOG_PATH.write_text(json.dumps(new, indent=2))
        print(f"  [i] Migrated {migrated} historical GMP reading(s) from gmp_log.json into gmp_session_log.json")


# ------------------------------------------------------------------ #
# STEP 1 — homepage list
# ------------------------------------------------------------------ #
def fetch_ipo_list():
    resp = requests.get(f"{BASE_URL}/", headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    ipos, seen = [], set()
    for h2 in soup.find_all("h2"):
        status = h2.get_text(strip=True).upper()
        if status not in ("UPCOMING", "OPEN", "CLOSED", "ALLOTTED", "LISTED"):
            continue
        container = h2.find_parent().find_next_sibling()
        if container is None:
            continue
        for a in container.find_all("a", href=re.compile(r"^/ipos/[A-Za-z0-9]+/?$")):
            href = a.get("href", "")
            slug = href.strip("/").split("/")[-1]
            if slug in seen:
                continue
            seen.add(slug)
            text = a.get_text(" ", strip=True)
            gmp_match = re.search(r"GMP\s*([+\-]?₹?[\d,.]+)", text)
            ipos.append({
                "slug": slug,
                "status": status,
                "gmp_snapshot": to_float(gmp_match.group(1)) if gmp_match else None,
            })
    return ipos


# ------------------------------------------------------------------ #
# STEP 2 — detail page (tables + timeline + price + application size)
# ------------------------------------------------------------------ #
def parse_tables(soup):
    parsed = []
    for t in soup.find_all("table"):
        ths = t.find_all("th")
        header_row = [th.get_text(" ", strip=True) for th in ths] if ths else None
        rows = []
        for tr in t.find_all("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue
            rows.append([c.get_text(" ", strip=True) for c in cells])
        if header_row is None and rows:
            header_row, rows = rows[0], rows[1:]
        parsed.append({"headers": header_row or [], "rows": rows})
    return parsed


def find_table(tables, must_contain):
    for t in tables:
        header_text = " ".join(t["headers"]).lower()
        if all(s.lower() in header_text for s in must_contain):
            return t
    return None


def parse_application_size_cell(text):
    """'50 Shares  1 Lot · ₹15,000' -> {'shares':50, 'lots':1, 'amount':15000, 'raw': text}
    Case-insensitive (site may use 'Shares'/'Lot' capitalized), accepts ₹ or Rs."""
    shares_m = re.search(r"([\d,]+)\s*shares?", text, re.IGNORECASE)
    lots_m = re.search(r"(\d+)\s*lots?", text, re.IGNORECASE)
    amount_m = re.search(r"(?:₹|rs\.?)\s*([\d,]+)", text, re.IGNORECASE)
    out = {"raw": text}
    if shares_m:
        out["shares"] = to_int(shares_m.group(1))
    if lots_m:
        out["lots"] = to_int(lots_m.group(1))
    if amount_m:
        out["amount"] = to_int(amount_m.group(1))
    return out


def fetch_ipo_detail(slug, item_status):
    resp = requests.get(f"{BASE_URL}/ipos/{slug}/", headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    h1 = soup.find("h1")
    h1_text = h1.get_text(" ", strip=True) if h1 else ""
    if re.search(r"\bSME\b", h1_text):
        return None  # mainboard-only

    name = re.sub(r"\s*\bSME\b\s*", "", h1_text).strip()
    flat = soup.get_text("\n", strip=True)
    detail = {"name": name}

    m = re.search(r"Price range\s*\n?₹?([\d,]+)\D+₹?([\d,]+)", flat, re.IGNORECASE)
    if m:
        detail["price_low"] = to_float(m.group(1))
        detail["price_high"] = to_float(m.group(2))
    else:
        m = re.search(r"Issue price\s*\n?₹?([\d,]+)", flat, re.IGNORECASE)
        if m:
            detail["price_low"] = detail["price_high"] = to_float(m.group(1))

    m = re.search(r"Lot size\s*\n?([\d,]+)\s*shares", flat, re.IGNORECASE)
    if m:
        detail["lot_size"] = to_int(m.group(1))
    m = re.search(r"Issue size\s*\n?₹?([\d,.]+)\s*Cr", flat, re.IGNORECASE)
    if m:
        detail["issue_size_cr"] = to_float(m.group(1))
    # NOTE: trynarada detail pages do NOT contain Fresh Issue / OFS at all
    # (verified 2026-08). Those two values come from the FinAPI/IPO Central
    # enrichment step in main() — cached once per IPO.

    detail["timeline"] = {}
    for label, key in [("Opens", "open"), ("Closes", "close"), ("Allotment", "allotment"),
                        ("Settlement", "settlement"), ("Listing", "listing"),
                        ("Mandate end", "mandate_end"), ("Anchor 50%", "anchor_50"),
                        ("Anchor 100%", "anchor_100")]:
        m = re.search(rf"\n{re.escape(label)}\s*\n([A-Za-z]{{3}},\s*\d{{1,2}}\s*[A-Za-z]{{3}}\s*\d{{4}})", flat)
        if m:
            try:
                detail["timeline"][key] = dtparser.parse(m.group(1)).date().isoformat()
            except ValueError:
                pass

    # Site layout (verified 2026-08): value on its own line, % in parens on the
    # line AFTER the value, and a "% vs. issue price" hint line between the
    # label and the value:
    #   ... | Issue price | ₹807 | Listing price | % vs. issue price | ₹980 | (+21.4%) | Current price | ...
    def parse_price_block(label):
        m = re.search(
            rf"{label}\s*\n(?:% vs\.? issue price\s*\n)?₹?\s*([\d,.]+)(?:\s*\n?\s*\(([+-]?[\d.]+)%\))?",
            flat, re.IGNORECASE)
        if m:
            return to_float(m.group(1)), to_float(m.group(2))
        return None, None

    listing_price, listing_gain = parse_price_block("Listing price")
    if listing_price is not None:
        detail["listing_price"] = listing_price
        if listing_gain is not None:
            detail["listing_gain_pct"] = listing_gain
    elif item_status == "LISTED":
        i = flat.lower().find("listing")
        print(f"  [!] '{name}' is LISTED but Listing price not found. Raw snippet: {flat[max(0, i - 20):i + 150] if i >= 0 else 'no *listing* text found at all'}")

    current_price, current_gain = parse_price_block("Current price")
    if current_price is not None:
        detail["current_price"] = current_price
        if current_gain is not None:
            detail["current_gain_pct"] = current_gain
    elif item_status == "LISTED":
        i = flat.lower().find("current price")
        print(f"  [!] '{name}' is LISTED but Current price not found. Raw snippet: {flat[max(0, i - 20):i + 150] if i >= 0 else 'no *current price* text found at all'}")

    # Resilient fallback: if we still don't have the issue price (e.g. the
    # "Price range" / "Issue price" label wasn't found in the expected form),
    # derive it mathematically from listing price + its % gain — same number,
    # more robust since it doesn't depend on a second label matching too.
    if not detail.get("price_high"):
        if detail.get("listing_price") is not None and detail.get("listing_gain_pct") is not None:
            derived = round(detail["listing_price"] / (1 + detail["listing_gain_pct"] / 100), 2)
            detail["price_low"] = detail["price_high"] = derived
        elif detail.get("current_price") is not None and detail.get("current_gain_pct") is not None:
            derived = round(detail["current_price"] / (1 + detail["current_gain_pct"] / 100), 2)
            detail["price_low"] = detail["price_high"] = derived

    tables = parse_tables(soup)
    shares_tbl = find_table(tables, ["shares offered"]) or find_table(tables, ["quota", "shares"])
    apps_tbl = find_table(tables, ["applications reserved"]) or find_table(tables, ["applications received"])
    appsize_tbl = find_table(tables, ["min", "max"])

    def sub_table_to_dict(tbl):
        out = {}
        if not tbl:
            return out
        for row in tbl["rows"]:
            if len(row) < 4:
                continue
            label = row[0].split()[0]
            out[label] = {"offered": row[1], "applied": row[2], "times": to_float(row[3])}
        return out

    detail["subscription_shares"] = sub_table_to_dict(shares_tbl)
    detail["subscription_applications"] = sub_table_to_dict(apps_tbl)

    app_size = {}
    if appsize_tbl:
        for row in appsize_tbl["rows"]:
            if len(row) < 2:
                continue
            label = row[0].split()[0]
            parsed_min = parse_application_size_cell(row[1])
            if parsed_min.get("shares") or parsed_min.get("amount"):
                app_size[label] = parsed_min

    # Fallback: if table parsing found nothing usable, the "Application size"
    # section may not be a real <table> on this site — try matching it as
    # flattened text instead (label followed by a nearby line containing "shares").
    if not app_size:
        for label in ["RII", "sNII", "bNII", "NII", "QIB", "Employee", "Retail"]:
            m = re.search(rf"{label}\b[^\n]{{0,40}}\n([^\n]*shares[^\n]*)", flat, re.IGNORECASE)
            if m:
                parsed = parse_application_size_cell(m.group(1))
                if parsed.get("shares") or parsed.get("amount"):
                    app_size[label] = parsed
        if app_size:
            print(f"  [i] '{name}': Application size matched via text fallback, not a <table> — table parsing may need adjustment.")

    detail["application_size"] = app_size

    if not app_size:
        # nothing worked — dump the raw area so we can see the real format
        idx = flat.lower().find("application size")
        snippet = flat[idx:idx+400] if idx >= 0 else "('Application size' heading not found on page at all)"
        print(f"  [!] Could not parse Application size for '{name}' ({slug}). Raw snippet:\n      {snippet}")

    if not detail.get("subscription_shares"):
        print(f"  [!] Could not parse subscription table for '{name}' ({slug}) — page structure may differ from expected.")

    # Freshness of the subscription numbers, e.g. "just now" / "2 hours ago" —
    # the dashboard shows this so you know how live the live data is.
    m = re.search(r"Last updated:\s*\n([^\n]+)", flat, re.IGNORECASE)
    if m:
        detail["subscription_updated"] = m.group(1).strip()

    # Lead managers + registrar, parsed as lines between the fixed labels:
    #   ... | Lead managers | IIFL Capital Services | Motilal Oswal ... | Registrar | KFin Technologies | ...
    lines = flat.split("\n")

    def grab_after(label, stop_labels, max_items):
        if label in lines:
            i = lines.index(label)
            out = []
            for ln in lines[i + 1: i + 1 + max_items + 2]:
                if ln in stop_labels or len(out) >= max_items:
                    break
                out.append(ln)
            return out
        return []

    lead_managers = grab_after("Lead managers", {"Registrar", "Registrars", "Intermediaries"}, 8)
    registrars = grab_after("Registrar", {"Intermediaries", "Lead managers", "Registrars"}, 2)
    if lead_managers or registrars:
        detail["intermediaries"] = {}
        if lead_managers:
            detail["intermediaries"]["lead_managers"] = lead_managers
        if registrars:
            detail["intermediaries"]["registrar"] = registrars[0]

    return detail


# ------------------------------------------------------------------ #
# STEP 3 — GMP twice-daily session log -> daily average
# ------------------------------------------------------------------ #
def update_gmp_session_log(entries):
    """entries: list of (name, gmp_value). Bucket into 'morning' (before 3pm)
    or 'evening' (3pm+) session for today; overwrite same session if re-run."""
    log = json.loads(GMP_SESSION_LOG_PATH.read_text()) if GMP_SESSION_LOG_PATH.exists() else {}
    session = "morning" if CURRENT_HOUR < 15 else "evening"
    for name, val in entries:
        if val is None:
            continue
        log.setdefault(name, [])
        log[name] = [e for e in log[name] if not (e["date"] == str(TODAY) and e["session"] == session)]
        log[name].append({"date": str(TODAY), "session": session, "value": val})
    GMP_SESSION_LOG_PATH.write_text(json.dumps(log, indent=2))
    return log


def compute_daily_averages(session_log_for_ipo):
    by_date = {}
    for e in session_log_for_ipo:
        by_date.setdefault(e["date"], []).append(e["value"])
    return [{"date": d, "avg": round(sum(vals) / len(vals), 2)} for d, vals in sorted(by_date.items())]


# ------------------------------------------------------------------ #
# STEP 4 — hourly subscription log, only on an IPO's own closing day, 9am-6pm
# ------------------------------------------------------------------ #
def update_qib_intraday_log(ipos_detail):
    log = json.loads(QIB_INTRADAY_LOG_PATH.read_text()) if QIB_INTRADAY_LOG_PATH.exists() else {}
    if not (9 <= CURRENT_HOUR <= 18):
        return log  # outside the tracked window, nothing to log this run
    for detail in ipos_detail:
        close_date = detail.get("timeline", {}).get("close")
        if close_date != str(TODAY):
            continue  # only log on the actual closing day
        subs = detail.get("subscription_shares") or {}
        name = detail["name"]
        log.setdefault(name, [])
        log[name] = [e for e in log[name] if not (e["date"] == str(TODAY) and e["hour"] == CURRENT_HOUR)]
        log[name].append({
            "date": str(TODAY),
            "hour": CURRENT_HOUR,
            "qib_times": subs.get("QIB", {}).get("times"),
            "nii_times": subs.get("NII", {}).get("times"),
            "rii_times": subs.get("RII", {}).get("times"),
            "total_times": subs.get("Total", {}).get("times"),
        })
    QIB_INTRADAY_LOG_PATH.write_text(json.dumps(log, indent=2))
    return log


# ------------------------------------------------------------------ #
def main():
    migrate_old_gmp_log()

    print(f"[{NOW.isoformat(timespec='minutes')}] Fetching IPO list from trynarada.com...")
    listing = fetch_ipo_list()
    print(f"  -> {len(listing)} total entries across all statuses (mainboard + SME)")

    ipos_out = []
    gmp_entries = []
    mainboard_count = skipped_sme = failed = 0

    for item in listing:
        try:
            detail = fetch_ipo_detail(item["slug"], item["status"])
        except requests.RequestException as e:
            print(f"  [!] Failed to fetch {item['slug']}: {e}")
            failed += 1
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        if detail is None:
            skipped_sme += 1
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        mainboard_count += 1
        detail["slug"] = item["slug"]
        detail["status"] = item["status"]
        detail["gmp_latest"] = item["gmp_snapshot"]
        gmp_entries.append((detail["name"], item["gmp_snapshot"]))
        ipos_out.append(detail)
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"  -> {mainboard_count} mainboard, {skipped_sme} SME skipped, {failed} failed")

    gmp_session_log = update_gmp_session_log(gmp_entries)
    for ipo in ipos_out:
        ipo["gmp_daily_avg"] = compute_daily_averages(gmp_session_log.get(ipo["name"], []))

    qib_log = update_qib_intraday_log(ipos_out)
    for ipo in ipos_out:
        ipo["qib_intraday"] = qib_log.get(ipo["name"], [])

    print("Enriching Fresh Issue / OFS (FinAPI once per run + one-time cached lookups)...")
    enrich_issue_splits(ipos_out)

    OUTPUT_JSON_PATH.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "ipos": ipos_out,
    }, indent=2))
    print(f"Done. Wrote {len(ipos_out)} mainboard IPOs to {OUTPUT_JSON_PATH}")
    print("Live view:  python -m http.server 8000  ->  open http://localhost:8000/ipo_desk.html")
    print("(serving over HTTP lets ipo_desk.html auto-load AND auto-refresh the JSON — no manual file loading)")


if __name__ == "__main__":
    main()
