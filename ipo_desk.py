#!/usr/bin/env python3
"""IPO Desk - live Indian IPO dashboard (Mainboard + SME).

Merges and cross-verifies NSE, BSE, Narada (trynarada.com), InvestorGain and Chittorgarh.
Run:  python ipo_desk.py            -> opens http://localhost:8765

Cloud / one-shot mode (used by GitHub Actions, Render cron, etc.):
      python ipo_desk.py --once --static-dir site --no-browser
"""
import argparse
import csv
import gzip
import io
import json
import shutil
import os
import sys
import threading
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import market                            # noqa: E402
import sources as S                      # noqa: E402
from merge import build_doc, STATUS_LABEL  # noqa: E402
from store import Store                  # noqa: E402
from util import (IST, compact, iso_now, is_market_hours, name_score, now_ist, parse_date,  # noqa: E402
                  today_ist)

VERSION = "2.1"
PRE_LISTING = {"upcoming", "open", "closing_today", "closed", "allotment_today", "allotted", "listing_today"}
LIVE = {"open", "closing_today"}


def log(*a):
    print(now_ist().strftime("%H:%M:%S"), *a, flush=True)


class Engine:
    def __init__(self, db_path, lookback_days=365, enable_cg=True):
        self.store = Store(db_path)
        self.http = S.Http()
        self.nse = S.NseClient(self.http)
        self.lookback = lookback_days
        self.enable_cg = enable_cg
        self.lock = threading.RLock()
        self.cache = {}                   # key -> {"doc":..., "detail":...}
        self.version = 0
        self.progress = {"phase": "starting", "done": 0, "total": 0, "note": ""}
        self.wake = threading.Event()
        self.priority = []                # keys requested from the UI
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.fail_until = {}
        self.attempted = {}
        self.catalog_ready = threading.Event()
        for row in self.store.q("SELECT key, data FROM ipo"):
            try:
                self.cache[row["key"]] = json.loads(row["data"])
            except Exception:
                pass
        if self.cache:
            self.version = 1
        self.import_seed()

    # ================================================================ history seed
    def import_seed(self, path=None):
        """Import history exported by seed_export.py (once per distinct file, then never again)."""
        path = path or os.path.join(HERE, "seed", "history.json.gz")
        if not os.path.exists(path):
            return 0
        try:
            import gzip
            import hashlib
            raw = open(path, "rb").read()
            h = hashlib.sha1(raw).hexdigest()
            if self.store.kv_get("seed_hash") == h:
                return 0
            data = json.loads(gzip.decompress(raw))
            n = self.store.put_gmp_ignore(data.get("gmp") or [])
            m = self.store.put_sub_ignore(data.get("sub") or [])
            o = self.store.put_ohlc_ignore(data.get("ohlc") or [])
            self.store.kv_set("seed_hash", h)
            log(f"seed: imported {n} GMP rows, {m} subscription snapshots and {o} daily candles "
                f"from {os.path.basename(path)}")
            return n
        except Exception:
            log("seed import failed", traceback.format_exc(limit=3))
            return 0

    # ================================================================ source health helper
    def call(self, source, fn, *a, **kw):
        try:
            v = fn(*a, **kw)
            self.store.mark(source, True)
            return v
        except LookupError:
            self.store.mark(source, True)          # reachable, page simply not there
            return None
        except Exception as e:  # noqa: BLE001
            self.store.mark(source, False, f"{type(e).__name__}: {e}")
            return None

    # ================================================================ catalog
    def cutoff(self):
        return today_ist() - timedelta(days=self.lookback)

    def sync_catalog(self, full=True):
        """Build the IPO universe and refresh list-level data from every source."""
        t0 = time.time()
        self.progress.update(phase="catalog", note="Fetching IPO lists")
        http, st = self.http, self.store
        today, cutoff = today_ist(), self.cutoff()
        cat = None
        if full or not st.kv_get("ig_catalog"):
            cat = self.call("InvestorGain", S.ig_catalog, http)
            if cat:
                st.kv_set("ig_catalog", cat)
        cat = cat or st.kv_get("ig_catalog") or []
        live = self.call("InvestorGain", S.ig_live_gmp, http) or []
        sublive = self.call("InvestorGain", S.ig_live_subscription, http) or []
        perf = []
        if full or not st.kv_get("ig_perf_rows"):
            for y in sorted({today.year, cutoff.year}):
                perf += self.call("InvestorGain", S.ig_performance, http, y) or []
            if perf:
                st.kv_set("ig_perf_rows", perf)
        perf = perf or st.kv_get("ig_perf_rows") or []
        nd_home = self.call("Narada", S.nd_home, http) or []
        nd_listed = []
        if full or not st.kv_get("nd_listed_rows"):
            nd_listed = self.call("Narada", S.nd_listed, http, cutoff) or []
            if nd_listed:
                st.kv_set("nd_listed_rows", nd_listed)
        nd_listed = nd_listed or st.kv_get("nd_listed_rows") or []
        nse_rows = []
        if full or not st.kv_get("nse_rows"):
            for kind, fn in (("current", self.nse.current), ("upcoming", self.nse.upcoming)):
                rows = self.call("NSE", fn)
                if rows:
                    nse_rows += S.nse_parse_listing(rows, kind)
            past = self.call("NSE", self.nse.past, cutoff, today)
            if past:
                nse_rows += S.nse_parse_listing(past, "past")
            if nse_rows:
                st.kv_set("nse_rows", nse_rows)
        elif not full:
            rows = self.call("NSE", self.nse.current)      # cheap live refresh of open issues
            nse_rows = S.nse_parse_listing(rows, "current") if rows else []
            old = [r for r in (st.kv_get("nse_rows") or []) if r.get("kind") != "current"]
            nse_rows = (nse_rows + old) if rows else (st.kv_get("nse_rows") or [])

        # ---------------- universe keyed by InvestorGain id
        by_id = {c["ig_id"]: c for c in cat}
        keep = set()
        for c in cat:
            d = parse_date(c.get("open"))
            if d and d >= cutoff:
                keep.add(c["ig_id"])
        live_by = {r["ig_id"]: r for r in live}
        perf_by = {}
        for r in perf:
            ld = parse_date(r.get("listing"))
            if ld and ld >= cutoff:
                perf_by[r["ig_id"]] = r
        keep |= set(live_by) | set(perf_by)
        items = []
        for iid in keep:
            c = by_id.get(iid) or {"ig_id": iid, "name": (live_by.get(iid) or perf_by.get(iid) or {}).get("name"),
                                   "slug": (live_by.get(iid) or {}).get("slug")}
            key = f"ig{iid}"
            items.append((key, "ig_cat", c))
            if iid in live_by:
                items.append((key, "ig_live", live_by[iid]))
            if iid in perf_by:
                items.append((key, "ig_perf", perf_by[iid]))
        sub_by = {r["ig_id"]: r for r in sublive}
        for iid, r in sub_by.items():
            items.append((f"ig{iid}", "ig_sublive", r))
        # clear stale live rows (IPO dropped out of the live report) - only if the report loaded
        if live:
            for k in list(st.raw_source("ig_live").keys()):
                if k.startswith("ig") and k[2:].isdigit() and int(k[2:]) not in live_by:
                    st.del_raw(k, "ig_live")
        if sublive:
            for k in list(st.raw_source("ig_sublive").keys()):
                if k.startswith("ig") and k[2:].isdigit() and int(k[2:]) not in {r["ig_id"] for r in sublive}:
                    st.del_raw(k, "ig_sublive")
        st.put_raw_many(items)
        keys = {k for k, _, _ in items}

        # ---------------- match Narada + NSE items to InvestorGain keys
        index = self._index(keys)
        nd_map = st.kv_get("nd_map", {})
        nd_items = {}
        for it in nd_listed + nd_home:            # home overrides listed (fresher)
            nd_items[it["sym"]] = {**nd_items.get(it["sym"], {}), **it}
        new_items = []
        for sym, it in nd_items.items():
            hint = parse_date(it.get("listing_date") or it.get("event_date"))
            prev = nd_map.get(sym)
            key = prev if prev in keys else None
            key = key or self._match(index, it.get("name"), sym, hint)
            if key and prev and prev.startswith("nd:") and prev != key:
                st.move_key(prev, key)              # Narada-only record now matched -> merge histories
            if not key:
                if it.get("nd_section") in ("LISTED",) and hint and hint < cutoff:
                    continue
                key = f"nd:{sym}"
                keys.add(key)
            nd_map[sym] = key
            new_items.append((key, "nd_list", it))
        nse_map = st.kv_get("nse_map", {})
        for it in nse_rows:
            sym = it.get("symbol")
            if not sym:
                continue
            hint = parse_date(it.get("open"))
            key = nse_map.get(sym) if nse_map.get(sym) in keys else None
            key = key or self._match(index, it.get("name"), sym, hint)
            if not key:
                continue
            nse_map[sym] = key
            new_items.append((key, "nse_list", it))
        st.put_raw_many(new_items)
        st.kv_set("nd_map", nd_map)
        st.kv_set("nse_map", nse_map)

        # ---------------- daily GMP snapshots (history keeps growing even after sources prune it)
        day = today.isoformat()
        for r in live:
            if r.get("gmp") is None or r.get("ig_status") in ("LN", "L"):
                continue
            est = (r["price"] + r["gmp"]) if r.get("price") else None
            st.put_gmp(f"ig{r['ig_id']}", "investorgain",
                       [{"date": day, "gmp": r["gmp"], "gmp_pct": r.get("gmp_pct"), "est_listing": est,
                         "updated": r.get("gmp_updated")}])
        for sym, it in nd_items.items():
            if it.get("gmp") is None or it.get("nd_section") not in ("UPCOMING", "OPEN", "CLOSED", "ALLOTTED"):
                continue
            st.put_gmp(nd_map[sym], "narada", [{"date": day, "gmp": it["gmp"], "gmp_pct": it.get("gmp_pct"),
                                                "updated": iso_now()}])
        stamp = now_ist().strftime("%Y-%m-%dT%H:%M")
        for r in sublive:
            st.put_sub(f"ig{r['ig_id']}", "investorgain", stamp,
                       {k: r.get(k) for k in ("total", "qib", "nii", "snii", "bnii", "rii")})
        st.kv_set("universe", sorted(keys))
        self.remerge_all(keys)
        log(f"catalog: {len(keys)} IPOs  (IG {len(cat)} catalog / {len(live)} live / {len(perf)} listed, "
            f"Narada {len(nd_items)}, NSE {len(nse_rows)})  {time.time() - t0:.1f}s")
        self.progress.update(phase="ready", note="")
        self.catalog_ready.set()

    def _index(self, keys):
        idx = {"items": [], "by_sym": {}, "by_compact": {}}
        raws = {}
        for src in ("ig_cat", "ig_live", "ig_perf", "ig_detail", "nd_list"):
            for k, d in self.store.raw_source(src).items():
                if k in keys:
                    raws.setdefault(k, {})[src] = d
        for k, r in raws.items():
            names, syms, dates = set(), set(), set()
            for src in ("ig_cat", "ig_live", "ig_perf"):
                d = r.get(src) or {}
                if d.get("name"):
                    names.add(d["name"])
                for f in ("open", "close", "listing", "allotment"):
                    dd = parse_date(d.get(f))
                    if dd:
                        dates.add(dd)
            p = r.get("ig_perf") or {}
            for s in (p.get("symbol") or "").split(","):
                if s.strip():
                    syms.add(s.strip().upper())
            ipo = (r.get("ig_detail") or {}).get("ipo") or {}
            for f in ("nse_symbol", "nse_script_symbol", "bse_script_id", "nse_cd"):
                if ipo.get(f):
                    syms.add(str(ipo[f]).upper())
            if ipo.get("company_name"):
                names.add(ipo["company_name"])
            item = {"key": k, "names": [n for n in names if n], "dates": dates}
            idx["items"].append(item)
            for s in syms:
                idx["by_sym"].setdefault(s, k)
            for n in item["names"]:
                idx["by_compact"].setdefault(compact(n), k)
        return idx

    def _match(self, idx, name, sym, hint):
        if sym and sym.upper() in idx["by_sym"]:
            return idx["by_sym"][sym.upper()]
        c = compact(name or "")
        if c and c in idx["by_compact"]:
            k = idx["by_compact"][c]
            return k
        best, best_s = None, 0.0
        for it in idx["items"]:
            if hint and it["dates"] and not any(abs((hint - d).days) <= 20 for d in it["dates"]):
                continue
            s = max((name_score(name or "", n) for n in it["names"]), default=0)
            if s > best_s:
                best, best_s = it["key"], s
        return best if best_s >= 0.86 else None

    # ================================================================ merge
    def remerge(self, key):
        self._vrows = None
        raw = self.store.raw_for(key)
        if not raw:
            return
        doc, detail = build_doc(key, raw, self.store.gmp_for(key), self.store.sub_for(key))
        entry = {"doc": doc, "detail": detail}
        self.store.put_doc(key, entry)
        with self.lock:
            self.cache[key] = entry
            self.version += 1

    def remerge_all(self, keys=None):
        keys = keys or set(self.store.kv_get("universe", []))
        raw_all = {}
        for r in self.store.q("SELECT key, source, data, fetched_at FROM raw"):
            if r["key"] in keys:
                d = json.loads(r["data"])
                if isinstance(d, dict):
                    d["_fetched"] = r["fetched_at"]
                raw_all.setdefault(r["key"], {})[r["source"]] = d
        gmp_all = {}
        for r in self.store.q("SELECT * FROM gmp_hist ORDER BY day"):
            gmp_all.setdefault(r["key"], []).append(dict(r))
        sub_all = {}
        for r in self.store.q("SELECT key, ts, source, data FROM sub_hist ORDER BY ts"):
            sub_all.setdefault(r["key"], []).append({"ts": r["ts"], "source": r["source"], **json.loads(r["data"])})
        out, today = {}, today_ist()
        for k in keys:
            if k not in raw_all:
                continue
            try:
                doc, detail = build_doc(k, raw_all[k], gmp_all.get(k, []), sub_all.get(k, []), today)
                out[k] = {"doc": doc, "detail": detail}
            except Exception:
                log("merge error", k, traceback.format_exc(limit=3))
        self.store.put_docs(list(out.items()))
        with self.lock:
            for k in list(self.cache):
                if k not in keys:
                    self.cache.pop(k, None)
            self.cache.update(out)
            self.version += 1
        if keys:
            gone = [k for k in self.store.keys() if k not in keys]
            for k in gone:
                self.store.x("DELETE FROM ipo WHERE key=?", (k,))

    # ================================================================ per-IPO detail fetch
    def fetch_details(self, key, parts=("ig", "igsub", "nd", "nse"), merge=True):
        st, http = self.store, self.http
        raw = st.raw_for(key)
        doc = (self.cache.get(key) or {}).get("doc") or {}
        c = raw.get("ig_cat") or {}
        iid = c.get("ig_id") or (raw.get("ig_live") or {}).get("ig_id")
        slug = c.get("slug") or (raw.get("ig_live") or {}).get("slug")
        status = doc.get("status")
        day, stamp = today_ist().isoformat(), now_ist().strftime("%Y-%m-%dT%H:%M")
        if "ig" in parts and iid and slug:
            d = self.call("InvestorGain", S.ig_gmp_page, http, slug, iid)
            if not d:
                self.fail_until[key] = time.time() + 3600
            if d:
                rows = d.pop("gmp", [])
                if rows:
                    st.put_gmp(key, "investorgain", rows)
                st.put_raw(key, "ig_detail", d)
        opened = bool(doc.get("dates", {}).get("open") and parse_date(doc["dates"]["open"])
                      and parse_date(doc["dates"]["open"]) <= today_ist())
        if "igsub" in parts and iid and slug and (opened or not doc):
            d = self.call("InvestorGain", S.ig_subscription_page, http, slug, iid)
            if d and d.get("bids"):
                st.put_raw(key, "ig_sub", d)
        nd_sym = (raw.get("nd_list") or {}).get("sym")
        if not nd_sym and doc.get("nse_symbol"):
            nd_sym = doc["nse_symbol"]
        if "nd" in parts and nd_sym:
            d = self.call("Narada", S.nd_detail, http, nd_sym)
            if d:
                st.put_raw(key, "nd_detail", d)
                if d.get("gmp") is not None and (status in PRE_LISTING or not status) and not d.get("listing_price"):
                    st.put_gmp(key, "narada", [{"date": day, "gmp": d["gmp"], "gmp_pct": d.get("gmp_pct"),
                                                "updated": iso_now()}])
                if status in LIVE and d.get("sub_shares"):
                    st.put_sub(key, "narada", stamp, {r["cat"]: r.get("times") for r in d["sub_shares"]
                                                      if r.get("times") is not None})
        if "nse" in parts and status in LIVE and doc.get("nse_symbol"):
            d = self.call("NSE", self.nse.active_category, doc["nse_symbol"])
            if d and d.get("rows"):
                st.put_raw(key, "nse_cat", d)
        if merge:
            self.remerge(key)

    def fetch_cg(self, key):
        raw = self.store.raw_for(key)
        ipo = (raw.get("ig_detail") or {}).get("ipo") or {}
        cg_id, slug = ipo.get("cor_id"), ipo.get("urlrewrite_folder_name") or (raw.get("ig_cat") or {}).get("slug")
        if not cg_id or not slug:
            return False
        d = self.call("Chittorgarh", S.cg_detail, self.http, slug, cg_id)
        if d:
            self.store.put_raw(key, "cg", d)
            self.remerge(key)
            return True
        return False

    # ================================================================ prices
    def refresh_quotes(self, keys):
        """Live quotes from NSE (primary) / BSE for listed IPOs."""
        for k in keys:
            doc = (self.cache.get(k) or {}).get("doc") or {}
            got = False
            if doc.get("nse_symbol"):
                series = doc.get("series") if doc.get("series") in ("EQ", "BE", "SM", "ST", "BZ") else ("SM" if doc.get("board") == "SME" else "EQ")
                q = self.call("NSE", self.nse.quote, doc["nse_symbol"], series)
                if q and q.get("ltp"):
                    self.store.put_raw(k, "nse_quote", q)
                    got = True
            if doc.get("bse_code"):
                q = self.call("BSE", S.bse_quote, self.http, doc["bse_code"])
                if q:
                    self.store.put_raw(k, "bse_quote", q)
                    got = True
            if got:
                self.remerge(k)

    def refresh_bhavcopy(self, listing_days=45):
        """Official EOD files: latest close + listing-day open/close (verifies listing price)."""
        docs = [e["doc"] for e in self.cache.values()]
        listed = [d for d in docs if d.get("status") in ("listed", "listing_today")]
        today = today_ist()
        # latest available BSE bhavcopy
        latest, ld = None, None
        start = today if now_ist().hour >= 19 else today - timedelta(days=1)
        for i in range(0, 8):
            d = start - timedelta(days=i)
            if d.weekday() >= 5:
                continue
            try:
                latest = S.bse_bhavcopy(self.http, d)
                ld = d
                self.store.mark("BSE", True)
                break
            except LookupError:
                continue
            except Exception as e:  # noqa: BLE001
                self.store.mark("BSE", False, f"bhavcopy: {e}")
                break
        items = []
        if latest:
            for doc in listed:
                row = latest.get(str(doc.get("bse_code") or ""))
                if row:
                    items.append((doc["key"], "bse_bhav_last", {**row, "date": ld.isoformat()}))
        # listing-day files for recent listings
        need = {}
        have = self.store.raw_source("bse_bhav_listing")
        for doc in listed:
            l = parse_date((doc.get("dates") or {}).get("listing"))
            if not l or not doc.get("bse_code") or doc["key"] in have or (today - l).days > listing_days:
                continue
            if l == today and now_ist().hour < 19:
                continue
            need.setdefault(l, []).append(doc)
        for l, group in sorted(need.items(), reverse=True):
            try:
                bc = S.bse_bhavcopy(self.http, l)
            except Exception:
                continue
            for doc in group:
                row = bc.get(str(doc.get("bse_code")))
                if row:
                    items.append((doc["key"], "bse_bhav_listing", {**row, "date": l.isoformat()}))
        # NSE listing-day files (works from Indian home connections; skipped quietly when blocked)
        have_n = self.store.raw_source("nse_bhav_listing")
        need_n = {}
        for doc in listed:
            l = parse_date((doc.get("dates") or {}).get("listing"))
            if not l or not doc.get("nse_symbol") or doc["key"] in have_n or (today - l).days > listing_days:
                continue
            if l == today and now_ist().hour < 19:
                continue
            need_n.setdefault(l, []).append(doc)
        for l, group in sorted(need_n.items(), reverse=True):
            try:
                bc = S.nse_bhavcopy(self.http, l)
                self.store.mark("NSE", True)
            except S.SourceBlocked as e:
                self.store.mark("NSE", False, str(e))
                break
            except Exception:
                continue
            for doc in group:
                row = bc.get(doc["nse_symbol"])
                if row:
                    items.append((doc["key"], "nse_bhav_listing", {**row, "date": l.isoformat()}))
        if items:
            self.store.put_raw_many(items)
            for k in {k for k, _, _ in items}:
                self.remerge(k)
        log(f"bhavcopy: {len(items)} price rows (latest {ld})")

    def detail_payload(self, key, entry):
        """One IPO, with the value row attached - same numbers the static export bakes in."""
        out = dict(entry)
        try:                                    # our subscription snapshots, for the day-wise tab
            rows = self.store.sub_for(key)
            if rows:
                out["detail"] = dict(out.get("detail") or {},
                                     subscription=dict((out.get("detail") or {}).get("subscription") or {},
                                                       intraday=rows[-60:]))
        except Exception:
            pass
        try:                                    # exchange daily candles, for the price chart
            cand = self.store.ohlc_for(key, limit=220)
            if cand:
                out["detail"] = dict(out.get("detail") or {},
                                     prices=dict((out.get("detail") or {}).get("prices") or {}, daily=cand))
        except Exception:
            pass
        try:
            import value
            if not getattr(self, "_vrows", None):
                self._vrows = self.value_payload()
            row = next((r for r in (self._vrows.get("rows") or []) if r.get("key") == key), None)
            if row:
                out["value"] = row
                out["detail"] = dict(out.get("detail") or {}, value=row)
        except Exception:
            pass
        return out

    def backfill_listing_days(self, max_dates=14, budget=150, tried=None):
        """Fetch the official end-of-day file for each past listing day we do not have yet.

        One request per calendar date covers every IPO that listed that day, and the file carries
        the true open / high / low / close - which is exactly the day-1 story this desk needs.
        """
        docs = [e["doc"] for e in self.cache.values()]
        have = self.store.raw_source("bse_bhav_listing") | self.store.raw_source("nse_bhav_listing")
        tried = tried if tried is not None else {}
        todo = {}
        for d in docs:
            if d.get("status") not in ("listed", "listing_today"):
                continue
            l = parse_date((d.get("dates") or {}).get("listing"))
            if not l or d["key"] in have or (today_ist() - l).days < 1:
                continue
            todo.setdefault(l, []).append(d)
        if not todo:
            return 0, 0
        dates = sorted(todo, reverse=True)[:max_dates]          # newest listings first
        t0, filled = time.time(), 0
        for idx, day in enumerate(dates):
            if time.time() - t0 > budget:
                break
            if idx:
                time.sleep(0.7)                                 # the exchange throttles bursts
            bse, tries = {}, 2
            while tries:
                try:
                    bse = S.bse_bhavcopy(self.http, day)
                    self.store.mark("BSE", True)
                    break
                except Exception:
                    bse, tries = {}, tries - 1
                    time.sleep(1.5)
            nse = {}
            try:
                nse = S.nse_bhavcopy(self.http, day)
                self.store.mark("NSE", True)
            except Exception:
                nse = {}
            by_isin = {str((r or {}).get("isin") or "").upper(): r for r in bse.values() if (r or {}).get("isin")}
            items = []
            for d in todo[day]:
                tried[d["key"]] = tried.get(d["key"], 0) + 1
                code = str(d.get("bse_code") or "")
                row = bse.get(code) if code else None
                if not row and d.get("isin"):
                    row = by_isin.get(str(d["isin"]).upper())
                if row:
                    items.append((d["key"], "bse_bhav_listing", {**row, "date": day.isoformat()}))
                    continue
                sym = (d.get("nse_symbol") or d.get("nd_sym") or "").upper()
                if sym and sym in nse:
                    items.append((d["key"], "nse_bhav_listing", {**nse[sym], "date": day.isoformat()}))
            if items:
                self.store.put_raw_many(items)
                for k in {k for k, _, _ in items}:
                    self.remerge(k)
                filled += len(items)
            log(f"listing day {day}: {len(items)}/{len(todo[day])} matched (bse {len(bse)} rows)")
        return filled, len(dates)

    def backfill_listing_eod(self, max_items=40, budget=180, per_day=6, tried=None):
        """Listing-day figures for the IPOs the official BSE file does not cover (NSE listings).

        Pulls the daily candle history around each listing date from the exchange EOD feed and
        stores open/high/low/close for the first traded day.
        """
        docs = [e["doc"] for e in self.cache.values()]
        have = self.store.raw_source("bse_bhav_listing") | self.store.raw_source("nse_bhav_listing") \
            | self.store.raw_source("nse_eod_listing")
        tried = tried if tried is not None else {}
        todo = []
        for d in docs:
            if d.get("status") not in ("listed", "listing_today") or d["key"] in have:
                continue
            l = parse_date((d.get("dates") or {}).get("listing"))
            sym = (d.get("nse_symbol") or d.get("nd_sym") or "").upper()
            if not l or (today_ist() - l).days < 1 or not sym or len(sym) > 12:
                continue
            if tried.get(d["key"], 0) >= 3:
                continue
            todo.append((l, sym, d))
        if not todo:
            return 0
        todo.sort(reverse=True)                                  # newest listings first
        t0, done, items = time.time(), 0, []
        for day, sym, d in todo[:max_items]:
            if time.time() - t0 > budget or done >= per_day:
                break
            rows = []
            for suffix in (".NS", ".BO"):
                try:
                    rows = S.yahoo_daily(self.http, sym + suffix, day)
                except Exception:
                    rows = []
                if rows:
                    break
                time.sleep(0.2)
            done += 1
            tried[d["key"]] = tried.get(d["key"], 0) + 1
            time.sleep(0.3)
            if not rows:
                continue
            want = day.isoformat()
            row = next((r for r in rows if r["date"] == want), None)
            if not row:                                          # first session on or after listing
                later = [r for r in rows if r["date"] >= want]
                row = min(later, key=lambda r: r["date"]) if later else None
            if row:
                items.append((d["key"], "nse_eod_listing", {**row, "symbol": sym}))
                self.store.put_raw_many([items[-1]])
                self.remerge(d["key"])
        return len(items)

    def day1_file(self, kind="xlsx"):
        """Build the day-1 spreadsheet / csv on demand (cached until the data version moves)."""
        with self.lock:
            ver, cached = self.version, getattr(self, "_day1_files", {})
            if cached.get("ver") == ver and cached.get(kind):
                return cached[kind]
            docs = [e["doc"] for e in self.cache.values()]
        import day1
        import tempfile
        tmp = tempfile.mkdtemp(prefix="ipodesk-day1-")
        day1.export(docs, tmp, generated=iso_now())
        with open(os.path.join(tmp, "day1." + kind), "rb") as f:
            blob = f.read()
        shutil.rmtree(tmp, ignore_errors=True)
        with self.lock:
            self._day1_files = {"ver": ver, kind: blob}
        return blob

    def day1_payload(self):
        """Listing-day rows + the day-1 study, cached against the current data version."""
        with self.lock:
            ver = self.version
            if getattr(self, "_day1_ver", None) == ver and self._day1_cache:
                return self._day1_cache
            docs = [e["doc"] for e in self.cache.values()]
        import day1
        p = day1.payload(docs, iso_now())
        with self.lock:
            self._day1_ver, self._day1_cache = ver, p
        return p

    def subhist_payload(self):
        """Our own subscription snapshots for the bidding IPOs - the last-day QIB signal."""
        out = {}
        with self.lock:
            keys = [k for k, e in self.cache.items()
                    if e["doc"].get("status") in ("open", "closing_today", "allotted", "listing_today")]
        for k in keys:
            rows = self.store.sub_for(k)
            if rows:
                out[k] = [{"ts": r["ts"], "qib": r.get("qib"), "total": r.get("total"),
                           "bnii": r.get("bnii"), "snii": r.get("snii"), "rii": r.get("rii")}
                          for r in rows[-40:]]
        return {"generated": now_ist().isoformat(timespec="seconds"), "keys": out}

    def live_payload(self, ttl=20):
        """Live prices for the shares trading today plus the market strip - server mode only.

        Everything else in this app is a snapshot that the cloud job republishes every few
        minutes. This is the one endpoint that answers "what is the price right now": it
        refetches the instruments and the shares listing today, and caches the answer for `ttl`
        seconds so a page can tick every 20-30 s without hammering the source.
        """
        now = time.time()
        with self.lock:
            cached = getattr(self, "_live_cache", None)
            if cached and now - cached[0] < ttl:
                return cached[1]
            docs = [dict(e["doc"]) for e in self.cache.values()
                    if e["doc"].get("status") == "listing_today"]
        quotes = {}
        for d in docs:
            sym = (d.get("nse_symbol") or d.get("nd_sym") or "").strip().upper()
            if not sym or len(sym) > 12:
                continue
            for suffix in (".NS", ".BO"):
                try:
                    q = market._quote(self.http, sym + suffix, timeout=10)
                except Exception:
                    continue
                if q and q.get("value"):
                    quotes[d["key"]] = dict(q, symbol=sym + suffix)
                    break
        try:
            mk = market.fetch(self.http)
        except Exception:
            mk = {"items": []}
        out = {"generated": now_ist().isoformat(timespec="seconds"), "market": mk, "quotes": quotes,
               "count": len(quotes), "listing_today": [d.get("key") for d in docs]}
        with self.lock:
            self._live_cache = (time.time(), out)
        return out

    def value_payload(self):
        """Fair-value rows for the live server (same model the static export bakes in)."""
        try:
            import value
            docs = self.list_payload()["ipos"]
            fund = {k: value.fundamentals_from_detail((e or {}).get("detail") or {})
                    for k, e in list(self.cache.items())}
            return value.build(docs, fund)
        except Exception:
            log("value check failed", traceback.format_exc(limit=2))
            return {"rows": [], "picks": [], "counts": {}, "stats": {}}

    # ================================================================ price history
    def refresh_ohlc(self, days=8):
        """Keep the daily candles fresh: read the official end-of-day file for any of the last few
        trading dates we have not stored yet, and upsert every scrip it carries. One or two requests
        a day in steady state, because a date already stored is never fetched twice."""
        from sources import bse_bhavcopy
        key = "ohlc_days"
        try:
            done = set(json.loads(self.store.kv_get(key) or "[]"))
        except Exception:
            done = set()
        by_code, by_isin = {}, {}
        for k, e in list(self.cache.items()):
            d = e["doc"]
            if d.get("status") not in ("listed", "listing_today"):
                continue
            if d.get("bse_code"):
                by_code[str(d["bse_code"]).strip()] = k
            if d.get("isin"):
                by_isin[str(d["isin"]).strip().upper()] = k
        if not by_code:
            return 0
        t, added, tried = today_ist(), 0, 0
        for i in range(days):
            d = t - timedelta(days=i)
            if d.weekday() >= 5 or d.isoformat() in done or tried >= 3:
                continue
            tried += 1
            try:
                rows = bse_bhavcopy(self.http, d)
            except Exception:
                continue
            day, bars = d.isoformat(), {}
            for code, row in rows.items():
                k = by_code.get(str(code)) or by_isin.get((row.get("isin") or "").upper())
                if not k or not row.get("close"):
                    continue
                bars.setdefault(k, []).append({"date": day, "open": row.get("open"), "high": row.get("high"),
                                               "low": row.get("low"), "close": row.get("close"), "volume": row.get("volume")})
            for k, b in bars.items():
                added += self.store.put_ohlc(k, b, src="exchange eod")
            done.add(day)
        self.store.kv_set(key, sorted(done)[-40:])          # kv_set stores JSON itself
        return added

    def record_prices(self):
        """Append one closing price per IPO per day - the fuel for the price/GMP chart.

        Official bhavcopy closes are stored as final; while the market is live the same day's
        row is refreshed with the latest quote, so an intraday price is available too.
        Safe by construction: SQLite upsert, no network, wrapped by the caller.
        """
        added = 0
        for k, e in list(self.cache.items()):
            doc = e["doc"]
            if doc.get("status") not in ("listed", "listing_today", "allotted", "closed"):
                continue
            raw = self.store.raw_for(k) or {}
            bhav = None
            for src in ("bse_bhav_last", "nse_bhav_last"):
                r = raw.get(src)
                if isinstance(r, dict) and r.get("close"):
                    bhav = r
                    break
            if bhav:
                if self.store.put_price(k, bhav.get("date") or str(today_ist()), bhav["close"],
                                        "close", final=True):
                    added += 1
            # listing-day close as the first point of the series
            lc, ld = doc.get("listing_close"), parse_date((doc.get("dates") or {}).get("listing"))
            if lc and ld and self.store.put_price(k, ld.isoformat(), lc, "listing close", final=True):
                added += 1
            # intraday: today's live price (never overwrites an official close)
            cur, cur_at = doc.get("current_price"), doc.get("current_as_of")
            if cur and cur_at and str(cur_at)[:4].isdigit():
                if self.store.put_price(k, str(cur_at)[:10], cur, "live", final=False):
                    added += 1
        log(f"price history: {added} row(s) added/updated")
        return added

    # ================================================================ schedulers
    def keys_by(self, pred):
        with self.lock:
            return [k for k, e in self.cache.items() if pred(e["doc"])]

    def live_loop(self):
        last = {}

        def due(name, every):
            if time.time() - last.get(name, 0) >= every:
                last[name] = time.time()
                return True
            return False

        while True:
            try:
                mkt = is_market_hours()
                hour = now_ist().hour
                if due("catalog_full", 3 * 3600 if 7 <= hour <= 23 else 6 * 3600):
                    self.sync_catalog(full=True)
                    last["catalog_fast"] = time.time()
                elif due("catalog_fast", 240 if 8 <= hour <= 22 else 1200):
                    self.sync_catalog(full=False)
                if due("live_details", 300 if 9 <= hour <= 18 else 1800):
                    ks = self.keys_by(lambda d: d["status"] in LIVE)
                    list(self.pool.map(self.fetch_details, ks))
                if due("near_details", 1800):
                    t = today_ist()

                    def near(d):
                        if d["status"] in ("closed", "allotment_today", "allotted", "listing_today"):
                            return True
                        if d["status"] == "upcoming":
                            o = parse_date(d["dates"].get("open"))
                            return bool(o and (o - t).days <= 10)
                        if d["status"] == "listed":
                            l = parse_date(d["dates"].get("listing"))
                            return bool(l and (t - l).days <= 3)
                        return False
                    list(self.pool.map(self.fetch_details, self.keys_by(near)))
                if due("quotes", 300 if mkt else 3600):
                    t = today_ist()
                    hot = self.keys_by(lambda d: d["status"] in ("listed", "listing_today") and
                                       parse_date(d["dates"].get("listing")) and
                                       (t - parse_date(d["dates"]["listing"])).days <= 30)
                    self.refresh_quotes(hot)
                if due("listed_feed", 900 if mkt else 3 * 3600):
                    rows = self.call("Narada", S.nd_listed, self.http, today_ist() - timedelta(days=self.lookback))
                    if rows:
                        nd_map = self.store.kv_get("nd_map", {})
                        items = [(nd_map[r["sym"]], "nd_list", r) for r in rows if r["sym"] in nd_map]
                        self.store.put_raw_many(items)
                        self.remerge_all()
                if due("prices", 600):
                    try:
                        self.record_prices()
                    except Exception:
                        pass
                if due("market", 600 if (8 <= hour <= 23) else 1800):
                    self.refresh_market()
                if due("bhavcopy", 6 * 3600) or (hour == 19 and due("bhavcopy_evening", 3600)):
                    self.refresh_bhavcopy()
                while self.priority:
                    k = self.priority.pop(0)
                    self.fetch_details(k)
                    d = (self.cache.get(k) or {}).get("doc") or {}
                    if d.get("status") in ("listed", "listing_today"):
                        self.refresh_quotes([k])
            except Exception:
                log("live loop error", traceback.format_exc(limit=4))
            self.wake.wait(8)
            self.wake.clear()

    def backfill_loop(self):
        """Fetch full details for every IPO (active first), then keep older ones fresh weekly."""
        if not self.cache:
            self.catalog_ready.wait()
        while True:
            worked = False
            try:
                fetched = self.store.raw_fetched("ig_detail")
                nd_fetched = self.store.raw_fetched("nd_detail")
                with self.lock:
                    docs = [e["doc"] for e in self.cache.values()]
                stale_days = lambda s: (now_ist() - datetime.fromisoformat(s)).days if s else 999
                todo = []
                for d in docs:
                    age = stale_days(fetched.get(d["key"]))
                    nd_age = stale_days(nd_fetched.get(d["key"]))
                    limit = 7 if d["status"] == "listed" else 1
                    if self.fail_until.get(d["key"], 0) > time.time():
                        continue
                    if time.time() - self.attempted.get(d["key"], 0) < (6 * 3600 if d["status"] == "listed" else 1800):
                        continue
                    has_ig = d.get("ig_id") is not None
                    if (has_ig and age >= limit) or (d.get("nd_sym") and nd_age >= limit):
                        todo.append(d)
                order = {"closing_today": 0, "open": 1, "listing_today": 2, "allotment_today": 3, "allotted": 4,
                         "closed": 5, "upcoming": 6, "listed": 7, "withdrawn": 9}
                todo.sort(key=lambda d: (order.get(d["status"], 8), -(parse_date(d["dates"].get("open") or d["dates"].get("listing") or "") or today_ist()).toordinal()))
                if todo:
                    self.progress.update(phase="backfill", total=len(todo), done=0,
                                         note="Fetching company details, GMP history & subscription")
                    log(f"backfill: {len(todo)} IPOs need details")
                    done = 0
                    for batch_start in range(0, len(todo), 12):
                        batch = todo[batch_start:batch_start + 12]
                        for d in batch:
                            self.attempted[d["key"]] = time.time()
                        list(self.pool.map(lambda d: self.fetch_details(d["key"], parts=("ig", "igsub", "nd")), batch))
                        done += len(batch)
                        self.progress.update(done=done)
                    self.progress.update(phase="ready", note="", done=0, total=0)
                    log("backfill: complete")
                    self.remerge_all()
                    worked = True
            except Exception:
                log("backfill error", traceback.format_exc(limit=4))
            time.sleep(30 if worked else 300)

    def cg_loop(self):
        """Chittorgarh is slow (uncached pages take 20-60 s) so it gets its own low-priority lane."""
        time.sleep(20)
        while self.enable_cg:
            try:
                have = self.store.raw_fetched("cg")
                t = today_ist()
                with self.lock:
                    docs = [e["doc"] for e in self.cache.values()]
                todo = []
                for d in docs:
                    if not d.get("cg_id"):
                        continue
                    ref = parse_date(d["dates"].get("close") or d["dates"].get("open") or "")
                    if not ref or (t - ref).days > 60 or d["status"] == "upcoming":
                        continue
                    age_h = (now_ist() - datetime.fromisoformat(have[d["key"]])).total_seconds() / 3600 if d["key"] in have else 1e9
                    need_h = 1 if d["status"] in LIVE else (6 if d["status"] in PRE_LISTING else 72)
                    if age_h >= need_h:
                        todo.append(d)
                todo.sort(key=lambda d: d.get("status_order", 9))
                for d in todo[:25]:
                    self.fetch_cg(d["key"])
            except Exception:
                log("chittorgarh loop error", traceback.format_exc(limit=3))
            time.sleep(90)

    def start(self):
        for fn in (self.live_loop, self.backfill_loop, self.cg_loop):
            threading.Thread(target=fn, daemon=True).start()

    # ================================================================ one-shot cloud run
    def run_once(self, budget_seconds=240, cg_budget=0, do_bhavcopy=True):
        """Single refresh cycle for cron/CI use: catalog -> hot details -> quotes -> export.

        Bounded by wall-clock `budget_seconds` so a scheduled job always finishes.
        """
        t0 = time.time()
        self.sync_catalog(full=True)
        if self.cache:
            self.remerge_all()

        def left():
            return budget_seconds - (time.time() - t0)

        # 1. live IPOs (open / closing today) get full detail every run
        ks = self.keys_by(lambda d: d["status"] in LIVE)
        if ks and left() > 45:
            list(self.pool.map(self.fetch_details, ks))
        # 2. allotment / listing-today / recently closed
        t = today_ist()
        hot = self.keys_by(lambda d: d["status"] in ("allotment_today", "allotted", "listing_today", "closed"))
        if hot and left() > 60:
            list(self.pool.map(self.fetch_details, hot[:25]))
        # 3. newest upcoming (details + GMP history)
        if left() > 60:
            up = self.keys_by(lambda d: d["status"] == "upcoming" and
                              parse_date(d["dates"].get("open") or "") and
                              (parse_date(d["dates"]["open"]) - t).days <= 21)
            pending = [k for k in up if not ((self.cache.get(k) or {}).get("doc") or {}).get("has_detail")]
            if pending:
                list(self.pool.map(self.fetch_details, pending[:20]))
        # 3b. backfill: every run fills in a few IPOs that have no detail yet (or a stale one),
        #     so coverage climbs run after run without ever blowing the time budget
        if left() > 90:
            fetched = self.store.raw_fetched("ig_detail")
            nd_fetched = self.store.raw_fetched("nd_detail")
            stale = lambda s: (now_ist() - datetime.fromisoformat(s)).days if s else 999
            todo = []
            for k, e in list(self.cache.items()):
                d = e["doc"]
                if d["status"] == "withdrawn":
                    continue
                limit = 7 if d["status"] == "listed" else 1
                if stale(fetched.get(k)) >= limit or (d.get("nd_sym") and stale(nd_fetched.get(k)) >= limit):
                    if not d.get("has_detail"):
                        prio = 0                       # never fetched -> first in line
                    else:
                        prio = 1
                    todo.append((prio, d.get("status_order", 9),
                                 -(parse_date(d["dates"].get("open") or d["dates"].get("listing") or "")
                                   or today_ist()).toordinal(), k))
            todo.sort()
            batch = [k for *_, k in todo[:30]]
            if batch:
                log(f"backfill slice: {len(batch)} IPOs ({len(todo)} pending)")
                for k in batch:
                    self.attempted[k] = time.time()
                list(self.pool.map(lambda k: self.fetch_details(k, parts=("ig", "igsub", "nd")), batch))
        # 4. live quotes for recent listings
        if left() > 40:
            recent = self.keys_by(lambda d: d["status"] in ("listed", "listing_today") and
                                  parse_date(d["dates"].get("listing")) and
                                  (t - parse_date(d["dates"]["listing"])).days <= 30)
            self.refresh_quotes(recent[:40])
        # 4b. market strip (9 quick requests; each failure keeps the previous value)
        if left() > 25:
            self.refresh_market()
        # 5. official EOD file (~1 request, cheap, keeps last close + listing price honest)
        if do_bhavcopy and left() > 30:
            self.refresh_bhavcopy()
        # 5b. official listing-day files we are still missing (bounded: a few dates per run)
        if do_bhavcopy and left() > 45:
            try:
                n, dates_done = self.backfill_listing_days(max_dates=8, budget=min(90, max(30, left() - 30)))
                if dates_done:
                    log(f"listing-day backfill: {n} IPO(s) from {dates_done} date(s)")
                if left() > 60:
                    m = self.backfill_listing_eod(max_items=30, budget=min(120, max(30, left() - 40)), per_day=6)
                    if m:
                        log(f"listing-day EOD: {m} IPO(s)")
            except Exception:
                log("listing-day backfill failed", traceback.format_exc(limit=2))
        # 6. Chittorgarh: slow; only when explicitly given a budget
        if cg_budget > 0 and left() > 30:
            cg_done, cg_t0 = 0, time.time()
            have = self.store.raw_fetched("cg")
            for k in self.keys_by(lambda d: d.get("cg_id") and d["status"] not in ("upcoming",)):
                if cg_done >= 6 or (time.time() - cg_t0) > cg_budget or left() < 20:
                    break
                if k not in have or (now_ist() - datetime.fromisoformat(have[k])).total_seconds() > 6 * 3600:
                    if self.fetch_cg(k):
                        cg_done += 1
            log(f"chittorgarh: {cg_done} pages")
        self.remerge_all()
        # 6b. daily close series for the price chart (no network, cheap upsert) and the daily candles
        #     the price study reads - the candles only fetch dates that are not stored yet
        try:
            n_ohlc = self.refresh_ohlc()
            if n_ohlc:
                log(f"daily candles: {n_ohlc} rows added")
        except Exception:
            log("daily candles failed", traceback.format_exc(limit=2))
        try:
            self.record_prices()
        except Exception:
            log("price history failed", traceback.format_exc(limit=2))
        # 7. expire stale detail fetches so the next scheduled run picks them up again
        self.progress.update(phase="ready", note="")
        log(f"run_once finished in {time.time() - t0:.1f}s - {len(self.cache)} IPOs")

    # ================================================================ market strip
    def refresh_market(self, force=False):
        """Indices / commodities / currency for the top ticker. Keeps last good values on failure.

        If the feed is unreachable (some cloud ranges are blocked by the provider) we back off for
        an hour instead of spending part of every run's budget on nine dead requests.
        """
        old = self.store.kv_get("market") or {}
        if not force:
            back = (self.store.kv_get("market_fail") or {}).get("at")
            if back:
                try:
                    age = (now_ist() - datetime.fromisoformat(back)).total_seconds()
                except Exception:
                    age = 9999
                if age < 3600:
                    log(f"market: backing off ({int(age/60)} min since last failure)")
                    return old
        try:
            fresh = market.fetch(self.http)
            payload = market.merge(old, fresh)
            ok = sum(1 for i in payload["items"] if i.get("value") is not None)
            self.store.kv_set("market", payload)
            self.store.kv_set("market_fail", {} if ok else {"at": now_ist().isoformat(timespec="seconds")})
            log(f"market: {ok}/{len(payload['items'])} instruments")
            return payload
        except Exception:
            log("market fetch failed", traceback.format_exc(limit=2))
            self.store.kv_set("market_fail", {"at": now_ist().isoformat(timespec="seconds")})
            return old

    # ================================================================ API payloads
    def list_payload(self):
        with self.lock:
            docs = [e["doc"] for e in self.cache.values()]
            ver = self.version
        counts = {}
        for d in docs:
            counts[d["status"]] = counts.get(d["status"], 0) + 1
        return {"version": ver, "generated": iso_now(), "today": today_ist().isoformat(),
                "market_open": is_market_hours(), "counts": counts, "progress": self.progress,
                "sources": self.sources_payload(), "ipos": docs, "app_version": VERSION}

    def sources_payload(self):
        st = self.store.statuses()
        out = []
        for name, role in (("NSE", "Official lists, live category bids & quotes"),
                           ("BSE", "Official quotes & bhavcopy (listing/closing prices)"),
                           ("Narada", "Status, GMP, share & application-wise subscription, prices"),
                           ("InvestorGain", "Catalog, live GMP, full GMP history, details, day-wise bids"),
                           ("Chittorgarh", "Total applications, basis of allotment, listing close")):
            s = st.get(name) or {}
            out.append({"name": name, "role": role, "ok": bool(s.get("ok")), "last_ok": s.get("last_ok"),
                        "last_try": s.get("last_try"), "error": s.get("last_error"), "calls": s.get("calls", 0),
                        "fails": s.get("fails", 0)})
        return out


# ==================================================================== HTTP server
ENGINE = None


class Handler(BaseHTTPRequestHandler):
    server_version = "IPODesk/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str, separators=(",", ":")).encode()
        elif isinstance(body, str):
            body = body.encode()
        if "gzip" in (self.headers.get("Accept-Encoding") or "") and len(body) > 1024:
            body = gzip.compress(body, 6)
            extra = dict(extra or {}, **{"Content-Encoding": "gzip"})
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        try:
            if p in ("/", "/index.html", "/ipo_desk.html"):
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if p == "/api/ipos":
                return self._send(200, ENGINE.list_payload())
            if p == "/api/compare":
                return self._send(200, ENGINE.value_payload())
            if p == "/api/subhist":
                return self._send(200, ENGINE.subhist_payload())
            if p == "/api/live":
                return self._send(200, ENGINE.live_payload())
            if p == "/api/market":
                return self._send(200, ENGINE.store.kv_get("market") or {"items": []})
            if p == "/api/day1":
                return self._send(200, ENGINE.day1_payload())
            if p in ("/day1.xlsx", "/day1.csv"):
                return self._send(200, ENGINE.day1_file(p.rsplit(".", 1)[1]),
                                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if p.endswith("xlsx")
                                  else "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="day1-listing-day.{p.rsplit(".", 1)[1]}"'})
            if p == "/api/version":
                return self._send(200, {"version": ENGINE.version, "progress": ENGINE.progress,
                                        "sources": ENGINE.sources_payload(), "market_open": is_market_hours(),
                                        "generated": iso_now()})
            if p.startswith("/api/ipo/"):
                key = p.split("/api/ipo/", 1)[1]
                e = ENGINE.cache.get(key)
                if not e:
                    return self._send(404, {"error": "not found"})
                return self._send(200, ENGINE.detail_payload(key, e))
            if p == "/api/export.json":
                with ENGINE.lock:
                    data = {"generated": iso_now(), "ipos": list(ENGINE.cache.values())}
                return self._send(200, data, extra={"Content-Disposition": "attachment; filename=ipo_data.json"})
            if p == "/api/export.csv":
                return self._send(200, export_csv(), "text/csv; charset=utf-8",
                                  {"Content-Disposition": "attachment; filename=ipo_desk.csv"})
            if p == "/api/health":
                return self._send(200, {"ok": True, "version": VERSION, "ipos": len(ENGINE.cache)})
            return self._send(404, {"error": "not found"})
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            log("http error", traceback.format_exc(limit=3))
            try:
                self._send(500, {"error": str(e)})
            except Exception:
                pass

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/api/refresh":
            key = (qs.get("key") or [None])[0]
            if key:
                if key in ENGINE.cache:
                    ENGINE.priority.append(key)
            else:
                ENGINE.priority.extend(ENGINE.keys_by(lambda d: d["status"] in LIVE))
                threading.Thread(target=ENGINE.sync_catalog, kwargs={"full": False}, daemon=True).start()
            ENGINE.wake.set()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def export_csv(engine=None):
    engine = engine or ENGINE
    cols = ["name", "board", "exchange", "status_label", "price_low", "price_high", "issue_price", "lot", "size_cr",
            "gmp", "gmp_pct", "est_listing", "rating", "sub_total", "listing_price", "listing_gain", "current_price",
            "current_gain", "nse_symbol", "bse_code"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols + ["open", "close", "allotment", "listing"])
    with engine.lock:
        docs = sorted((e["doc"] for e in engine.cache.values()), key=lambda d: d["dates"].get("open") or "", reverse=True)
    for d in docs:
        w.writerow([d.get(c) for c in cols] + [d["dates"].get(x) for x in ("open", "close", "allotment", "listing")])
    return buf.getvalue()


def main():
    global ENGINE
    ap = argparse.ArgumentParser(description="IPO Desk - live IPO dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", default=os.path.join(HERE, "ipo_desk.db"))
    ap.add_argument("--lookback", type=int, default=365, help="days of past IPOs to include")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-chittorgarh", action="store_true")
    # ---- cloud / one-shot mode -------------------------------------------------
    ap.add_argument("--once", action="store_true",
                    help="run one refresh cycle, write the static site, exit (for cron / GitHub Actions)")
    ap.add_argument("--static-dir", default=None,
                    help="folder to write the static dashboard into (with --once)")
    ap.add_argument("--budget", type=int, default=240, help="seconds allowed for --once")
    ap.add_argument("--cg-budget", type=int, default=0, help="seconds for Chittorgarh in --once (0 = skip)")
    ap.add_argument("--backfill-listing", type=int, default=0, metavar="N",
                    help="fetch official day-1 files for N listing dates, then exit")
    ap.add_argument("--export-only", action="store_true",
                    help="skip fetching, just re-export the static site from the existing database")
    args = ap.parse_args()

    try:                                   # Windows consoles default to cp1252 - never crash on '₹'
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.backfill_listing:
        ENGINE = Engine(args.db, args.lookback, enable_cg=False)
        ENGINE.sync_catalog(full=True)
        ENGINE.remerge_all()
        total, stale = 0, 0
        seen = {}                                          # how often each IPO has already refused
        for round_no in range(1, 60):                      # keep going until nothing is missing
            n, dates_done = ENGINE.backfill_listing_days(max_dates=args.backfill_listing, budget=600, tried=seen)
            m = ENGINE.backfill_listing_eod(max_items=args.backfill_listing * 3, budget=600,
                                            per_day=args.backfill_listing, tried=seen)
            total += n + m
            log(f"listing-day backfill round {round_no}: {n} official + {m} EOD ({dates_done} dates), {total} total")
            if not dates_done and not m:
                break
            stale = stale + 1 if (n + m) == 0 else 0       # transient blocks: stop after 3 dead rounds
            if stale >= 3:
                log("giving up - the archive keeps refusing; try again later")
                break
        if total:
            import export_static
            ENGINE.remerge_all()
        return

    if args.once or args.export_only:
        ENGINE = Engine(args.db, args.lookback, enable_cg=not args.no_chittorgarh)
        if not args.export_only:
            ENGINE.run_once(budget_seconds=args.budget, cg_budget=args.cg_budget)
        if args.static_dir:
            import export_static
            export_static.write_site(ENGINE, args.static_dir, csv_text=export_csv(ENGINE))
            log(f"static site written to {args.static_dir}/")
        return

    url = f"http://{'localhost' if args.host in ('0.0.0.0', '127.0.0.1') else args.host}:{args.port}/"
    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError:
        log(f"Port {args.port} is already in use - IPO Desk is probably running already. Opening {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return
    ENGINE = Engine(args.db, args.lookback, enable_cg=not args.no_chittorgarh)
    log(f"IPO Desk {VERSION} running at {url}   (Ctrl+C to stop)")
    ENGINE.start()
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("stopping")


if __name__ == "__main__":
    main()
