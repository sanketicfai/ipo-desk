#!/usr/bin/env python3
"""Backfill daily candles (open/high/low/close/volume) for listed IPOs from the official BSE file.

One end-of-day file carries every scrip - mainboard and BSE SME alike - so a run of trading dates
fills in the whole board in a handful of requests. Days already stored are simply replaced with the
same official numbers, so the script is safe to re-run.

    python3 ohlc_backfill.py                 # the last 70 trading days
    python3 ohlc_backfill.py --trading-days 40
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sources import Http, bse_bhavcopy       # noqa: E402
from store import Store                      # noqa: E402
from util import parse_date, today_ist       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def trading_days(end, n, skip_weekends=True):
    """The n most recent dates ending at `end` (weekends skipped; holidays are skipped on failure)."""
    out, d = [], end
    while len(out) < n:
        if not skip_weekends or d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "ipo_desk.db"))
    ap.add_argument("--trading-days", type=int, default=70)
    ap.add_argument("--days", type=int, default=0, help="only IPOs listed within this many days")
    args = ap.parse_args()

    st = Store(args.db)
    today = today_ist()

    # which scrip code / ISIN belongs to which IPO
    by_code, by_isin = {}, {}
    for r in st.q("SELECT key, data FROM ipo"):
        doc = json.loads(r["data"])["doc"]
        if doc.get("status") not in ("listed", "listing_today"):
            continue
        ld = parse_date((doc.get("dates") or {}).get("listing") or "")
        if args.days and (not ld or (today - ld).days > args.days):
            continue
        if doc.get("bse_code"):
            by_code[str(doc["bse_code"]).strip()] = doc["key"]
        if doc.get("isin"):
            by_isin[str(doc["isin"]).strip().upper()] = doc["key"]
    print(f"{len(by_code)} IPO(s) carry a BSE code", flush=True)

    http = Http()
    dates = trading_days(today, args.trading_days)
    filled, missing, bars_n = 0, 0, 0
    per_key = {}
    for d in dates:
        try:
            rows = bse_bhavcopy(http, d)
        except Exception:
            missing += 1
            continue
        day = d.isoformat()
        n = 0
        for code, row in rows.items():
            key = by_code.get(str(code)) or by_isin.get((row.get("isin") or "").upper())
            if not key or not row.get("close"):
                continue
            per_key.setdefault(key, []).append({"date": day, "open": row.get("open"), "high": row.get("high"),
                                                "low": row.get("low"), "close": row.get("close"),
                                                "volume": row.get("volume")})
            n += 1
        filled += 1
        print(f"  {day}: {n} of our scrips", flush=True)

    for key, bars in per_key.items():
        bars.sort(key=lambda b: b["date"])
        bars_n += st.put_ohlc(key, bars, src="exchange eod")
    print(f"ohlc backfill: {filled} dates read ({missing} holidays/not published), "
          f"{len(per_key)} IPOs, {bars_n} candles", flush=True)
    sample = sorted(per_key.items(), key=lambda kv: -len(kv[1]))[:3]
    for k, v in sample:
        print(f"   {k}: {len(v)} candles {v[0]['date']} -> {v[-1]['date']}", flush=True)


if __name__ == "__main__":
    main()
