#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
closing_day_tracker.py — Closing-day IPO subscription tracker (15-min snapshots)
================================================================================
Captures category-wise live subscription (QIB / NII-bHNI-sHNI / Retail / Total)
SHARE-WISE (offer vs applied shares), APPLICATION-WISE (reserve vs applied
applications) and ₹-demand for a list of IPOs closing today, and appends one
snapshot per 15-minute slot (:00 / :15 / :30 / :45 IST) to qib_closing_log.json
until the 17:00 IST bidding close.

Data source: IPOJi (ipoji.com) subscription pages — compiled by IPOJi from the
BSE & NSE live public-issue bidding platforms. (If you prefer the BSE "iPO"
bid book / NSE NEAPS directly, set env IPOJI_BASE to an empty value is NOT
needed — per-exchange raw feeds can be added as extra source modules; the
parser below is source-agnostic: it only needs the section tables.)

Usage
-----
  python closing_day_tracker.py                 # one capture now (for cron)
  python closing_day_tracker.py --loop          # keep running: sleep to next
                                                # quarter mark, capture, till end
  python closing_day_tracker.py --end 17:00     # closing time (IST, default 17:00)
  python closing_day_tracker.py --out qib_closing_log.json
  python closing_day_tracker.py --selftest      # run parser against embedded sample

The snapshot for a slot is keyed by the slot it was captured in; re-running
inside the same slot refreshes that slot's entry instead of duplicating it.
Only data stamped by the source as updated TODAY (IST) is logged — stale pages
are skipped so the log never records dead numbers after close.
"""

import argparse
import datetime as dt
import html as htmlmod
import json
import os
import re
import sys
import time
import urllib.request

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DEFAULT_END = "17:00"
LOG_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qib_closing_log.json")
SOURCE_BASE = "https://www.ipoji.com/ipo-subscription/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ---- The four issues closing today (edit this list for the next closing day) --
IPOS = {
    "Karamtara Engineering": {"slug": "karamtara-engineering-ipo", "type": "Mainboard"},
    "Steamhouse India":      {"slug": "steamhouse-india-ipo",      "type": "Mainboard"},
    "Rentomojo":             {"slug": "rentomojo-ipo",             "type": "Mainboard"},
    "LCC Projects":          {"slug": "lcc-projects-ipo",          "type": "Mainboard"},
}

# ----------------------------------------------------------------- utilities --
def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)

def slot_of(d: dt.datetime, minutes: int = 15) -> str:
    """Floor to the :00/:15/:30/:45 slot -> 'HH:MM'."""
    m = (d.minute // minutes) * minutes
    return f"{d.hour:02d}:{m:02d}"

def parse_int(tok):
    if tok is None:
        return None
    t = re.sub(r"[^\d]", "", str(tok))
    return int(t) if t else None

def parse_float(tok):
    if tok is None:
        return None
    t = str(tok).replace(",", "").replace("₹", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    return float(m.group(0)) if m else None

def fetch(url: str, timeout: int = 25, tries: int = 3) -> str:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-IN,en;q=0.9",
                "Cache-Control": "no-cache",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + 2 * i)
    raise RuntimeError(f"fetch failed {url}: {last}")

# ------------------------------------------------------------------- parsing --
TAG = re.compile(r"<[^>]+>")

def clean(s: str) -> str:
    s = TAG.sub(" ", s)
    s = htmlmod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()

def sections(html: str):
    """Yield (heading, table_html) pairs: each table joined to nearest heading."""
    heads = [(m.start(), clean(m.group(1))) for m in re.finditer(r"<h[12][^>]*>(.*?)</h[12]>", html, re.S)]
    out = []
    for m in re.finditer(r"<table[^>]*>(.*?)</table>", html, re.S | re.I):
        title = ""
        for pos, h in heads:
            if pos < m.start():
                title = h
            else:
                break
        out.append((title, m.group(0)))
    return out

def rows_of(table_html: str):
    """-> list of list-of-cell-strings (tags stripped, no empties at edges)."""
    rows = []
    for rm in re.finditer(r"<tr[^>]*>(.*?)</tr>", table_html, re.S | re.I):
        cells = [clean(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", rm.group(1), re.S | re.I)]
        cells = [c for c in cells if c not in ("", "&nbsp;")]
        if cells:
            rows.append(cells)
    return rows

CAT_KEY = [
    (re.compile(r"^qibs?\b", re.I), "qib"),
    (re.compile(r"^niis?\b", re.I), "nii"),
    (re.compile(r"^bniis?\b", re.I), "bnii"),
    (re.compile(r"^sniis?\b", re.I), "snii"),
    (re.compile(r"^retail", re.I), "retail"),
    (re.compile(r"^employee", re.I), "employee"),
    (re.compile(r"^fiis?\b", re.I), "qib_fii"),
    (re.compile(r"^diis?\b", re.I), "qib_dii"),
    (re.compile(r"^mutual", re.I), "qib_mf"),
    (re.compile(r"^others", re.I), "qib_others"),
    (re.compile(r"^total", re.I), "total"),
]

def cat_key(label: str):
    for rx, key in CAT_KEY:
        if rx.search(label or ""):
            return key
    return None

def parse_updated_stamp(text: str):
    """'... last updated 11 Sep 2026, 2:30 PM' -> (date, 'HH:MM') or (None, None)."""
    m = re.search(r"last updated[^0-9]{0,20}(\d{1,2})\s+([A-Za-z]+)\s+(\d{4}),?\s+(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)?", text)
    if not m:
        return None, None
    day, mon, year, hh, mm, ap = m.groups()
    months = {x.lower(): i for i, x in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
    try:
        d = dt.date(int(year), months[mon[:3].lower()], int(day))
    except Exception:  # noqa: BLE001
        return None, None
    h = int(hh) % 12 + (12 if (ap or "").upper() == "PM" else 0)
    return d, f"{h:02d}:{mm}"

def parse_ipo_page(html: str):
    """Extract one IPO's full dataset from its subscription page HTML."""
    data = {"times": {}, "shares": {}, "apps": {}, "demand_cr": {}}

    for title, table in sections(html):
        t = title.lower()
        rows = rows_of(table)
        if not rows:
            continue
        if "no. of shares" in t or ("shares" in t and "crore" not in t):
            for r in rows:
                if len(r) < 4:
                    continue
                k = cat_key(r[0])
                if not k or k == "total" and "applications" in " ".join(r).lower():
                    continue
                offer, applied, times = parse_int(r[1]), parse_int(r[2]), parse_float(r[3])
                if offer is not None and applied is not None:
                    data["shares"][k] = {"offer": offer, "applied": applied}
                    if times is not None:
                        data["times"][k] = times
        elif "application" in t and ("breakup" in t or "wise" in t):
            for r in rows:
                joined = " ".join(r).lower()
                if "total applications" in joined:
                    data["apps_total"] = parse_int(joined)
                    continue
                if len(r) < 4:
                    continue
                k = cat_key(r[0])
                if not k:
                    continue
                reserve, applied, times = parse_int(r[1]), parse_int(r[2]), parse_float(r[3])
                if applied is not None:
                    data["apps"][k] = {"reserve": reserve, "applied": applied, "times": times}
        elif "crore" in t:
            for r in rows:
                if len(r) < 4:
                    continue
                k = cat_key(r[0])
                if not k:
                    continue
                offered, demand, times = parse_float(r[1]), parse_float(r[2]), parse_float(r[3])
                if demand is not None:
                    data["demand_cr"][k] = demand
                if offered is not None and k in ("qib", "nii", "bnii", "snii", "retail", "employee", "total") and times is not None:
                    data["times"].setdefault(k, times)

        # "Total Applications" may be a colspan row inside any table
        for r in rows:
            j = " ".join(r).lower()
            if "total applications" in j and "apps_total" not in data:
                data["apps_total"] = parse_int(j)

    if "total" in data["shares"]:
        data["times"].setdefault("total", None)
        if data["times"].get("total") is None:
            so, sa = data["shares"]["total"]["offer"], data["shares"]["total"]["applied"]
            if so:
                data["times"]["total"] = round(sa / so, 2)
    # tidy: drop empty buckets
    data["times"] = {k: v for k, v in data["times"].items() if v is not None}
    if not data["apps"]:
        data.pop("apps")
    if not data["demand_cr"]:
        data.pop("demand_cr")
    data["apps_total"] = data.get("apps_total") or (data.get("apps", {}).get("retail", {}).get("applied"))
    return data

def updated_stamp_is_today(text: str, today: dt.date):
    d, tm = parse_updated_stamp(text)
    return (d == today), tm

# ------------------------------------------------------------------- capture --
def capture_one(name: str, slug: str, today: dt.date):
    html = fetch(SOURCE_BASE + slug)
    fresh, stamp = updated_stamp_is_today(html, today)
    if not fresh:
        return None, f"stale page (source not updated today)"
    data = parse_ipo_page(html)
    if not data["times"]:
        return None, "no parsable subscription table"
    data["src_updated"] = stamp
    return data, "ok"

def capture_all(ipos: dict, today: dt.date):
    out, notes = {}, {}
    for name, meta in ipos.items():
        try:
            data, note = capture_one(name, meta["slug"], today)
        except Exception as e:  # noqa: BLE001
            data, note = None, f"error: {e}"
        if data:
            out[name] = data
        notes[name] = note
    return out, notes

# ---------------------------------------------------------------- merge/store --
def load_log(path: str):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    return {"meta": {}, "snapshots": []}

def richness(entry: dict) -> int:
    d = entry.get("data", {})
    return sum(len(v.get("shares", {})) + len(v.get("apps", {})) for v in d.values())

def save_snapshot(path: str, ipos_meta: dict, snap: dict, force_final: bool = False):
    log = load_log(path)
    meta = log.get("meta") or {}
    meta.setdefault("closing_date", now_ist().strftime("%Y-%m-%d"))
    meta.setdefault("interval_minutes", 15)
    meta.setdefault("close_time_ist", DEFAULT_END)
    meta.setdefault("source", "IPOJi (ipoji.com) — compiled from BSE & NSE live public-issue bid platforms")
    meta.setdefault("source_base", SOURCE_BASE)
    meta.setdefault("schedule_note",
                    "Snapshot every 15 min (:00/:15/:30/:45 IST) until the 17:00 IST close; "
                    "automated by GitHub Actions workflow closing-day.yml.")
    meta["ipos"] = {**(meta.get("ipos") or {}), **ipos_meta}
    log["meta"] = meta

    snaps = log.setdefault("snapshots", [])
    slot = snap["slot"]
    for i, s in enumerate(snaps):
        if s.get("slot") == slot:
            if richness(snap) >= richness(s):   # same slot -> keep the richer/newer
                snaps[i] = snap
            snap = None
            break
    if snap:
        snaps.append(snap)
    snaps.sort(key=lambda s: s.get("slot", ""))
    if force_final:
        snaps[-1]["final"] = True
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(log, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)

# --------------------------------------------------------------------- modes --
def run_once(path: str, ipos: dict, end_hm: str, verbose=True):
    now = now_ist()
    today = now.date()
    end = dt.time(*map(int, end_hm.split(":")))
    after_close = (now.time() > end) or (now.time() == end and now.second > 0)
    data, notes = capture_all(ipos, today)
    if not data:
        if all(str(n).startswith("stale") for n in notes.values()):
            print("no closing-day window — source pages not updated today; no-op", flush=True)
            return 0
        print("CAPTURE FAILED:", json.dumps(notes), flush=True)
        return 1
    slot = slot_of(now)
    snap = {
        "slot": slot,
        "captured_ist": now.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
        "src_updated": max((v.get("src_updated") or "00:00" for v in data.values()), default=None),
        "data": {k: {kk: vv for kk, vv in v.items() if kk != "src_updated"} for k, v in data.items()},
    }
    if slot >= end_hm or after_close:
        snap["final"] = True   # closing snapshot
    save_snapshot(path, {k: v for k, v in ipos.items()}, snap)
    if verbose:
        summ = {n: round(v["times"].get("qib", -1), 2) for n, v in data.items()}
        print(f"[{now.strftime('%H:%M:%S')} IST] slot {slot} saved: QIB× {summ} -> {path}", flush=True)
        for n, note in notes.items():
            if note != "ok":
                print(f"  ! {n}: {note}", flush=True)
    return 0

def run_loop(path: str, ipos: dict, end_hm: str, interval: int = 15):
    end = dt.datetime.combine(now_ist().date(), dt.time(*map(int, end_hm.split(":"))), IST)
    while True:
        now = now_ist()
        if now >= end + dt.timedelta(minutes=interval):
            print("window over", flush=True)
            return 0
        run_once(path, ipos, end_hm)
        # sleep to the next quarter mark (+90 s so the source pages have fresh data)
        now = now_ist()
        secs_into = (now.minute * 60 + now.second) % (interval * 60)
        nxt = now + dt.timedelta(seconds=(interval * 60 - secs_into) + 90)
        wait = max(20, (nxt - now_ist()).total_seconds())
        if now_ist() + dt.timedelta(seconds=wait) > end + dt.timedelta(minutes=5):
            print("past close window — final capture done", flush=True)
            return 0
        print(f"sleeping {int(wait)}s -> next slot", flush=True)
        time.sleep(wait)

# ------------------------------------------------------------------ selftest --
SELFTEST_HTML = """
<html><body><div>Data sourced from BSE &amp; NSE · IPO subscription data last updated 11 Sep 2026, 2:30 PM</div>
<h2>Subscription Details (No. of Shares)</h2>
<table><tr><th>Category</th><th>Offer</th><th>Applied</th><th>Times</th></tr>
<tr><td><b>QIBs</b></td><td>68,89,765</td><td>24,89,29,142</td><td>36.13</td></tr>
<tr><td><b>NIIs</b></td><td>51,67,323</td><td>17,34,38,642</td><td>33.56</td></tr>
<tr><td><b>BNIIs</b> (&gt;10L)</td><td>34,44,882</td><td>11,59,57,774</td><td>33.66</td></tr>
<tr><td><b>SNIIs</b> (&lt;10L)</td><td>17,22,441</td><td>5,74,80,868</td><td>33.37</td></tr>
<tr><td><b>Retail</b></td><td>1,20,57,086</td><td>12,39,18,054</td><td>10.28</td></tr>
<tr><td><b>Total</b></td><td>2,41,14,174</td><td>54,62,85,838</td><td>22.65</td></tr>
<tr><td colspan=4>Total Applications<b>20,06,209</b> approx.</td></tr></table>
<h2>Application Wise Breakup (Approx.)</h2>
<table><tr><th>Category</th><th>Reserve</th><th>Applied</th><th>Times</th></tr>
<tr><td><b>BNIIs</b> (&gt;10L)</td><td>4,171</td><td>28,013</td><td>6.72</td></tr>
<tr><td><b>SNIIs</b> (&lt;10L)</td><td>2,085</td><td>68,053</td><td>32.64</td></tr>
<tr><td><b>Retail</b></td><td>2,04,357</td><td>19,09,878</td><td>9.35</td></tr>
<tr><td colspan=4>Total Applications<b>20,06,209</b> approx.</td></tr></table>
<h2>Subscription Details (In Crores)</h2>
<table><tr><th>Category</th><th>Offered</th><th>Demand</th><th>Times</th></tr>
<tr><td><b>QIBs</b></td><td>175</td><td>6322.8</td><td>36.13</td></tr>
<tr><td><b>FIIs</b></td><td>-</td><td>4455.29</td><td>-</td></tr>
<tr><td><b>NIIs</b></td><td>131.25</td><td>4405.34</td><td>33.56</td></tr>
<tr><td><b>Retail</b></td><td>306.25</td><td>3147.52</td><td>10.28</td></tr>
<tr><td><b>TOTAL</b></td><td>612.5</td><td>13875.66</td><td>22.65</td></tr></table>
</body></html>
"""

def selftest():
    fresh, stamp = updated_stamp_is_today(SELFTEST_HTML, dt.date(2026, 9, 11))
    assert fresh and stamp == "14:30", f"stamp parse failed: {fresh} {stamp}"
    fresh2, _ = updated_stamp_is_today(SELFTEST_HTML, dt.date(2026, 1, 1))
    assert fresh2 is False, "stale-date check failed"
    d = parse_ipo_page(SELFTEST_HTML)
    assert d["times"]["qib"] == 36.13 and d["times"]["total"] == 22.65, d["times"]
    assert d["shares"]["total"]["applied"] == 546285838, d["shares"]
    assert d["apps"]["retail"] == {"reserve": 204357, "applied": 1909878, "times": 9.35}, d["apps"]
    assert d["apps_total"] == 2006209, d["apps_total"]
    assert abs(d["demand_cr"]["qib_fii"] - 4455.29) < 0.01, d["demand_cr"]
    print("selftest OK:", json.dumps(d["times"]), "| apps_total", d["apps_total"])

# ----------------------------------------------------------------------- main --
def main():
    ap = argparse.ArgumentParser(description="Closing-day IPO subscription tracker")
    ap.add_argument("--out", default=os.environ.get("CLOSING_LOG", LOG_DEFAULT))
    ap.add_argument("--loop", action="store_true", help="keep capturing every quarter hour till close")
    ap.add_argument("--end", default=os.environ.get("CLOSING_END", DEFAULT_END), help="close time IST HH:MM")
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--slugs", default="", help="comma list Name=slug to override the built-in IPO list")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        selftest()
        return 0

    ipos = dict(IPOS)
    if a.slugs:
        ipos = {}
        for part in a.slugs.split(","):
            n, _, s = part.strip().partition("=")
            ipos[n] = {"slug": s or n.lower().replace(" ", "-") + "-ipo", "type": "Mainboard"}

    if a.loop:
        return run_loop(a.out, ipos, a.end, a.interval)
    return run_once(a.out, ipos, a.end)

if __name__ == "__main__":
    sys.exit(main())
