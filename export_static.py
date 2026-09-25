#!/usr/bin/env python3
"""Turn the live engine into a plain static site (GitHub Pages / Netlify / any web host).

Layout produced:

    <out>/index.html          the dashboard (same file the local server serves)
    <out>/.nojekyll           so GitHub Pages serves every file as-is
    <out>/data/ipos.json      the list payload  (was /api/ipos)
    <out>/data/version.json   tiny poll payload (was /api/version)
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
    payload["static"] = True
    payload["version"] = stamp                      # content hash: the page reloads only on real change
    payload["engine_version"] = engine.version
    payload["refreshed_at"] = payload.get("generated")
    _dump(os.path.join(data_dir, "ipos.json"), payload)

    # ---- per-IPO detail ------------------------------------------------------------
    alive = set()
    for d in docs:
        k = d["key"]
        d.setdefault("keyfile", keyfile[k])
        entry = engine.cache.get(k)
        if not entry:
            continue
        out = {"doc": entry["doc"], "detail": entry["detail"], "keyfile": keyfile[k]}
        out["doc"]["keyfile"] = keyfile[k]
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
           "counts": payload["counts"], "progress": payload["progress"], "sources": payload["sources"]}
    _dump(os.path.join(data_dir, "version.json"), ver)

    # ---- downloads -----------------------------------------------------------------
    if csv_text is None:
        import ipo_desk
        csv_text = ipo_desk.export_csv(engine)
    _write_if_changed(os.path.join(data_dir, "export.csv"), csv_text)
    _dump(os.path.join(data_dir, "export.json"), {"generated": payload["generated"], "ipos": docs})

    size = sum(os.path.getsize(os.path.join(dp, f))
               for dp, _, fs in os.walk(out_dir) for f in fs)
    print(f"[static] {len(docs)} IPOs, version {stamp}, {size/1e6:.1f} MB in {out_dir}", flush=True)
    return stamp
