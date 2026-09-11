#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_ipo_update.py — entry point run by .github/workflows/scrape.yml
=====================================================================
Closing-day mode (primary): while IPOs are open for bidding today, capture a
15-minute slot snapshot of QIB / NII / Retail / Total subscription —
share-wise (offer vs applied), application-wise (reserve vs applied) and
₹-demand — for the issues closing today, into:

    qib_closing_log.json    (canonical, read by closing_day.html)
    qib_intraday_log.json   (mirror; committed by scrape.yml)

Runs stand-alone (stdlib only). When the tracked subscription pages are not
updated today (i.e. no closing-day window), it exits 0 without touching the
log, so the hourly cron on non-closing days is a cheap no-op.
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import closing_day_tracker as t  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
CANONICAL = os.path.join(ROOT, "qib_closing_log.json")
MIRROR = os.path.join(ROOT, "qib_intraday_log.json")


def main() -> int:
    # parse args loosely so scrape.yml's plain invocation works
    end = os.environ.get("CLOSING_END", t.DEFAULT_END)
    rc = t.run_once(CANONICAL, t.IPOS, end)

    # mirror into the file scrape.yml commits (only when a snapshot was written)
    if os.path.exists(CANONICAL) and rc == 0:
        try:
            shutil.copyfile(CANONICAL, MIRROR)
            print(f"mirrored -> {os.path.basename(MIRROR)}", flush=True)
        except OSError as e:
            print(f"mirror copy failed: {e}", flush=True)
            return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
