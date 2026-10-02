#!/usr/bin/env python3
"""One-off: pull the category-wise bidding record for issues whose tracker table went flat.

InvestorGain collapses an issue's bidding table to a single total line a few weeks after listing,
and the online runner never fetches Chittorgarh (slow pages, free-runner budget), so those IPOs
would show only a Total row forever. This script walks the issues that are missing the split,
fetches the record once, saves it in the database and in cg_sub_cache.json next to the code - the
cache file travels with the repo, so the cloud rebuild keeps the categories without any extra
fetching at run time.

    python3 cg_sub_backfill.py            # fill in whatever is still missing
    python3 cg_sub_backfill.py --limit 40 # stop after 40 pages
"""
import argparse
import json
import os
import sqlite3
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sources import Http, cg_detail                                          # noqa: E402
from util import now_ist                                                     # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cg_sub_cache.json")
FIELDS = ("bids", "boa", "times", "total_applications", "bid_as_of", "url", "listing_day_close")


def load_cache():
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, CACHE)


def has_categories(ig_rows):
    return len([r for r in (ig_rows or []) if r.get("cat") in ("qib", "nii", "bnii", "snii", "rii")]) >= 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "ipo_desk.db"))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    have_cg = {r["key"] for r in db.execute("select key from raw where source='cg'")}
    cache = load_cache()

    todo = []
    for row in db.execute("select key, data from raw where source='ig_detail'"):
        key = row["key"]
        d = json.loads(row["data"]).get("ipo") or {}
        cg_id = d.get("cor_id")
        slug = d.get("urlrewrite_folder_name")
        if not cg_id or not slug:
            continue
        if key in cache and cache[key].get("bids"):
            continue
        todo.append((key, slug, str(cg_id)))
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(todo)} IPO(s) need the category split (cache holds {len(cache)})", flush=True)

    http = Http()
    done = failed = 0
    for key, slug, cg_id in todo:
        try:
            d = cg_detail(http, slug, cg_id)
            cats = [k for k, v in ((d.get("bids") or [{}])[0].get("times") or {}).items()
                    if v and k in ("qib", "nii", "bnii", "snii", "rii")]
            if len(cats) < 2:
                print(f"  skip {key}: the record has no category split either", flush=True)
                cache[key] = {"url": d.get("url"), "bids": [], "boa": [], "times": {},
                              "total_applications": d.get("total_applications"),
                              "bid_as_of": d.get("bid_as_of"), "checked": now_ist().isoformat(timespec="seconds")}
                save_cache(cache)
                continue
            slim = {k: d.get(k) for k in FIELDS}
            slim["checked"] = now_ist().isoformat(timespec="seconds")
            cache[key] = slim
            save_cache(cache)
            db.execute("insert or replace into raw(key, source, data, fetched_at) values (?,?,?,?)",
                       (key, "cg", json.dumps(d, ensure_ascii=False), now_ist().isoformat(timespec="seconds")))
            db.commit()
            done += 1
            print(f"  {key} {slug}: {len(cats)} categories, bids as of {d.get('bid_as_of')}", flush=True)
        except Exception as e:
            failed += 1
            print(f"  FAIL {key} {slug}: {type(e).__name__} {e}", flush=True)
            traceback.print_exc(limit=2)
    print(f"backfill finished: {done} fetched, {failed} failed, cache now {len(cache)} entries, "
          f"{os.path.getsize(CACHE) if os.path.exists(CACHE) else 0} bytes", flush=True)


if __name__ == "__main__":
    main()
