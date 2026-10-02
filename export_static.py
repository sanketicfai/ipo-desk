#!/usr/bin/env python3
"""Turn the live engine into a plain static site (GitHub Pages / Netlify / any web host).

Layout produced:

    <out>/index.html          the dashboard (same file the local server serves)
    <out>/.nojekyll           so GitHub Pages serves every file as-is
    <out>/data/ipos.json      the list payload  (was /api/ipos)
    <out>/data/version.json   tiny poll payload (was /api/version)
    <out>/data/market.json    indices/commodities/currency for the ticker (was /api/market)
    <out>/data/compare.json   price-vs-its-own-data check: fair value, entry ladder, stop loss
    <out>/data/ipo/<id>.json  one file per IPO  (was /api/ipo/<key>)
    <out>/data/export.csv     CSV download
    <out>/data/export.json    full JSON dump

The page detects at load time that there is no live API and switches to these files,
so the exact same dashboard.html works locally *and* online.
"""
import hashlib
import json
import os
import re
import shutil
import time

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- scrubbing
# The published page must not reveal where the data is collected from. Everything stays in the
# database and in the local dev server; the static export drops the giveaways.
SCRUB_KEYS = {"checks", "sources", "verification", "gmp_ig", "gmp_nd", "gmp_src", "sub_src",
              "current_src", "boa_src", "app_sizes_src", "candidates", "snapshots", "nse_rows",
              "nse_quote", "bse_quote", "nse_listing_day", "bse_listing_day", "ig_as_of"}
SCRUB_HOSTS = re.compile("https?://[^\\s\"'<>]*(?:chittorgarh|narada|investorgain|nseindia|bseindia)[^\\s\"'<>]*", re.I)
SCRUB_WORDS = re.compile(r"(trynarada|narada|investorgain|chittorgarh)", re.I)
SCRUB_ANCHOR = re.compile("<a\\b[^>]*href=\"[^\"]*(?:chittorgarh|narada|investorgain|nseindia|bseindia)[^\"]*\"[^>]*>(.*?)</a>", re.I | re.S)


def _scrub(obj, drop_logo=False):
    """Recursively drop source-bearing keys, links and brand names from a payload."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in SCRUB_KEYS or (isinstance(k, str) and k.endswith("_src")):
                continue
            if k == "logo" and drop_logo:
                continue
            if k == "links" and isinstance(v, dict):
                out[k] = {lk: lv for lk, lv in v.items() if re.search(r"drhp|rhp", lk, re.I)}
                continue
            out[k] = _scrub(v, drop_logo)
        return out
    if isinstance(obj, list):
        return [_scrub(x, drop_logo) for x in obj]
    if isinstance(obj, str):
        if "www." in obj:
            obj = re.sub(r"www\.(?:bseindia|nseindia)\.com", "the exchange website", obj, flags=re.I)
        if "<a" in obj:
            obj = SCRUB_ANCHOR.sub(r"\1", obj)      # keep the words, drop the link back to the source
        if SCRUB_HOSTS.search(obj) or SCRUB_WORDS.search(obj):
            obj = SCRUB_HOSTS.sub("#", obj)
            obj = SCRUB_WORDS.sub("", obj).replace("#)", ")").replace("#>", ">")
        return obj
    return obj


# --------------------------------------------------------------------------- company logos
# The logo files are copied into the site so the published page never points at a third-party
# host. Small, cached between runs, and skipped silently when the download fails.
LOGO_DIRNAME = "logo"
LOGO_MAX = 90 * 1024


def _local_logos(site_dir, docs):
    """Download each doc['logo'] once into <site>/logo/ and rewrite the field to a local path."""
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    out_dir = os.path.join(site_dir, LOGO_DIRNAME)
    os.makedirs(out_dir, exist_ok=True)

    def one(doc):
        url = doc.get("logo")
        if not url:
            return
        if not re.match(r"^https?://", url):            # already local from an earlier run
            return
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"):
            ext = ".png"
        name = safe_name(doc.get("key") or "x") + ext
        path = os.path.join(out_dir, name)
        if os.path.exists(path) and os.path.getsize(path) > 200:
            doc["logo"] = LOGO_DIRNAME + "/" + name
            return
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (IPO Desk)"})
            with urllib.request.urlopen(req, timeout=12) as r:
                blob = r.read(LOGO_MAX + 1)
            if not blob or len(blob) > LOGO_MAX:
                doc["logo"] = ""
                return
            with open(path, "wb") as f:
                f.write(blob)
            doc["logo"] = LOGO_DIRNAME + "/" + name
        except Exception:
            doc["logo"] = ""                            # initials avatar instead

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(one, docs))
    except Exception:
        pass
    return docs


def safe_name(key: str) -> str:
    """ig12345 -> ig12345 ; nd:FOO -> nd_FOO  (file-system & URL safe, stable)"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", key) or "x"


def _dump(path, obj, indent=None):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":") if indent is None else None,
                  indent=indent, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def _write_if_changed(path, text):
    """Return True when the file content actually changed (keeps git commits meaningful)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return True


def _value_rows(engine, docs):
    """Fair-value check for every IPO in scope + the fundamentals slice from the cached details."""
    try:
        import value
        fund = {}
        for k, e in getattr(engine, "cache", {}).items():
            fund[k] = value.fundamentals_from_detail((e or {}).get("detail") or {})
        out = value.build(docs, fund)
        out["generated"] = None
        return out
    except Exception as exc:                      # a model bug must never break the export
        print(f"[static] value check skipped: {exc}", flush=True)
        return {"rows": [], "picks": [], "counts": {}, "stats": {}}


def write_site(engine, out_dir, csv_text=None, dashboard_src=None):
    data_dir = os.path.join(out_dir, "data")
    ipo_dir = os.path.join(data_dir, "ipo")
    os.makedirs(ipo_dir, exist_ok=True)

    # ---- the dashboard page itself -------------------------------------------------
    src = dashboard_src or os.path.join(HERE, "dashboard.html")
    if os.path.abspath(src) != os.path.abspath(os.path.join(out_dir, "index.html")):
        shutil.copyfile(src, os.path.join(out_dir, "index.html"))
    open(os.path.join(out_dir, ".nojekyll"), "w").close()

    # ---- list payload --------------------------------------------------------------
    payload = engine.list_payload()
    docs = payload["ipos"]
    used, keyfile = {}, {}
    for d in docs:
        base = safe_name(d["key"])
        n = used.get(base, 0)
        used[base] = n + 1
        kf = base if n == 0 else f"{base}_{n}"
        d["keyfile"] = kf
        keyfile[d["key"]] = kf

    stamp = hashlib.sha1(json.dumps(docs, sort_keys=True, default=str).encode()).hexdigest()[:16]
    _local_logos(out_dir, docs)                     # keep the company logo, served from our own site
    docs = _scrub(docs)
    payload["ipos"] = docs
    payload.pop("sources", None)                    # source health stays local to the dev server
    payload["static"] = True
    payload["version"] = stamp                      # content hash: the page reloads only on real change
    payload["engine_version"] = engine.version
    payload["refreshed_at"] = payload.get("generated")
    _dump(os.path.join(data_dir, "ipos.json"), payload)

    # ---- value check (price vs the issue's own data) --------------------------------
    vc = _value_rows(engine, docs)
    _dump(os.path.join(data_dir, "compare.json"), vc)
    vbykey = {r["key"]: r for r in vc.get("rows", [])}

    # ---- daily price series (printed from the database, one row per trading day) --------------
    phist = engine.store.prices_all()

    # ---- subscription snapshots for the bidding IPOs (the last-day QIB signal) --------
    live_keys = [d["key"] for d in docs if d.get("status") in ("open", "closing_today", "allotted", "listing_today")]
    sh = {"generated": engine.version, "keys": {}}
    for k in live_keys:
        rows = engine.store.sub_for(k)
        if rows:
            sh["keys"][k] = [{"ts": r["ts"], "qib": r.get("qib"), "total": r.get("total"),
                              "bnii": r.get("bnii"), "snii": r.get("snii"), "rii": r.get("rii")} for r in rows[-40:]]
    _dump(os.path.join(data_dir, "subhist.json"), sh)

    # ---- per-IPO detail ------------------------------------------------------------
    alive = set()
    for d in docs:
        k = d["key"]
        d.setdefault("keyfile", keyfile[k])
        entry = engine.cache.get(k)
        if not entry:
            continue
        out = {"doc": dict(entry["doc"]), "detail": dict(entry["detail"])}
        out["doc"]["keyfile"] = keyfile[k]
        out["doc"]["logo"] = d.get("logo") or ""     # already copied into site/logo/
        out = _scrub(out)
        ser = list(phist.get(k) or [])
        # seed the series with the listing-day close so a freshly listed share still draws a line
        # exchange daily candles since listing - the shape of the price line and the market study
        cand = engine.store.ohlc_for(k, limit=220)
        if cand:
            out["detail"] = dict(out["detail"],
                                 prices=dict(out["detail"].get("prices") or {}, daily=cand))
        lday = (out["detail"].get("dates") or {}).get("listing")
        lclose = out["doc"].get("listing_close")
        if lday and lclose and not any(r.get("date") == lday for r in ser):
            ser.insert(0, {"date": lday, "close": lclose, "src": "listing close", "final": True})
        if ser:
            out["detail"] = dict(out["detail"], prices=dict(out["detail"].get("prices") or {}, history=ser))
        # our own subscription snapshots, so the day-wise tab can show the last bidding day hour by hour
        snap = engine.store.sub_for(k)
        if snap:
            out["detail"] = dict(out["detail"],
                                 subscription=dict(out["detail"].get("subscription") or {}, intraday=snap[-60:]))
        row = vbykey.get(k)
        if row:
            out["value"] = row
            out["detail"] = dict(out["detail"], value=row)
        out["keyfile"] = keyfile[k]
        name = keyfile[k] + ".json"
        alive.add(name)
        with open(os.path.join(ipo_dir, name), "w", encoding="utf-8") as f:
            json.dump(out, f, separators=(",", ":"), ensure_ascii=False, default=str)
    for old in os.listdir(ipo_dir):                  # prune IPOs that fell out of the window
        if old.endswith(".json") and old not in alive:
            os.remove(os.path.join(ipo_dir, old))

    # ---- poll payload --------------------------------------------------------------
    ver = {"version": stamp, "engine_version": engine.version, "generated": payload["generated"],
           "today": payload["today"], "market_open": payload["market_open"], "static": True,
           "counts": payload["counts"], "progress": payload["progress"]}
    _dump(os.path.join(data_dir, "version.json"), ver)

    # ---- market strip --------------------------------------------------------------
    _dump(os.path.join(data_dir, "market.json"), engine.store.kv_get("market") or {"items": []})

    # ---- downloads -----------------------------------------------------------------
    if csv_text is None:
        import ipo_desk
        csv_text = ipo_desk.export_csv(engine)
    _write_if_changed(os.path.join(data_dir, "export.csv"), csv_text)
    _dump(os.path.join(data_dir, "export.json"), {"generated": payload["generated"], "ipos": docs})

    # ---- day 1: dataset for the tab, plus the spreadsheet download ------------------
    import day1
    d1 = day1.export(docs, out_dir, generated=payload["generated"])
    _dump(os.path.join(data_dir, "day1.json"), d1)
    print(f"[static] day 1: {d1['count']} listings with listing-day prices, "
          f"xlsx {os.path.getsize(os.path.join(out_dir, 'day1.xlsx'))/1024:.0f} KB", flush=True)

    size = sum(os.path.getsize(os.path.join(dp, f))
               for dp, _, fs in os.walk(out_dir) for f in fs)
    print(f"[static] {len(docs)} IPOs, version {stamp}, {size/1e6:.1f} MB in {out_dir}", flush=True)
    return stamp
