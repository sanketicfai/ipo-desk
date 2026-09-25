#!/usr/bin/env python3
"""Export the history your PC has accumulated into ONE small file the cloud job can import.

Your local ipo_desk.db is far too big to put on GitHub (and shouldn't be there). But it holds
the part that cannot be re-downloaded: every day's GMP from InvestorGain and Narada, and the
subscription snapshots the desk polled while issues were open. This script squeezes that into
a compressed file - normally well under 2 MB - which is safe to commit.

    python seed_export.py                     # -> seed/history.json.gz
    python seed_export.py --db other.db --out seed/history.json.gz

Then commit the seed/ folder (GitHub Desktop will pick it up, browser upload accepts it).
The cloud job imports any rows it doesn't already have, once, and keeps adding to them forever.
"""
import argparse
import gzip
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

COLS = ("key", "day", "source", "gmp", "gmp_pct", "est_listing", "sub2", "est_profit", "updated")


def main():
    ap = argparse.ArgumentParser(description="Export GMP + subscription history for the cloud copy")
    ap.add_argument("--db", default=os.path.join(HERE, "ipo_desk.db"))
    ap.add_argument("--out", default=os.path.join(HERE, "seed", "history.json.gz"))
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"! no database at {args.db} - run the dashboard first, or pass --db", file=sys.stderr)
        return 1

    # read-only: safe even while the dashboard is running
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        gmp = [dict(r) for r in db.execute(f"SELECT {','.join(COLS)} FROM gmp_hist ORDER BY day")]
    except sqlite3.OperationalError:
        print("! that file has no gmp_hist table - is it the right database?", file=sys.stderr)
        return 1
    sub = [{"key": r["key"], "ts": r["ts"], "source": r["source"], "data": json.loads(r["data"])}
           for r in db.execute("SELECT key,ts,source,data FROM sub_hist ORDER BY ts")]
    db.close()

    payload = {"exported": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
               "gmp": gmp, "sub": sub}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with gzip.open(args.out, "wt", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)

    size = os.path.getsize(args.out)
    print(f"wrote {args.out}")
    print(f"  {len(gmp):,} GMP history rows  ({len({r['key'] for r in gmp}):,} IPOs)")
    print(f"  {len(sub):,} subscription snapshots")
    print(f"  {size/1024:.1f} KB compressed  ({os.path.getsize(args.db)/1e6:.0f} MB database shrunk to this)")
    if size > 20 * 1024 * 1024:
        print("  ! still large - that is fine for git, but tell me if it gets near 25 MB")
    else:
        print("  -> commit the seed/ folder; the cloud job imports it on its next run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
