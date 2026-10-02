"""Market strip feed: indices, commodities and currency for the dashboard ticker.

Prices come from Yahoo Finance's public chart endpoint, which works from both home
connections and cloud servers. Each instrument carries:

  value, prev, chg, chg_pct   -> price and change since previous close
  live                        -> True when the last tick is less than 30 minutes old
  as_of                       -> IST timestamp of that tick

When the market is open the value is the live price; when it is closed the value is the
last traded (closing) price, and `live` is False so the dashboard can label it.
"""
import json
import time
from urllib.parse import quote

from util import now_ist, num

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=5m&range=1d"
LIVE_WINDOW_MIN = 30           # last tick newer than this -> treat as live

# key, display label, yahoo symbol, decimals, unit prefix, optional proxy note
INSTRUMENTS = [
    {"key": "SENSEX",    "label": "SENSEX",     "sym": "^BSESN",   "dp": 2},
    {"key": "NIFTY",     "label": "NIFTY 50",   "sym": "^NSEI",    "dp": 2},
    {"key": "GIFTNIFTY", "label": "GIFT NIFTY", "sym": "^NSEI",    "dp": 2,
     "note": "NSE IX blocks cloud servers - showing NIFTY 50 as a close proxy"},
    {"key": "GOLD",      "label": "GOLD",       "sym": "GC=F",     "dp": 2, "unit": "$"},
    {"key": "SILVER",    "label": "SILVER",     "sym": "SI=F",     "dp": 3, "unit": "$"},
    {"key": "CRUDE",     "label": "CRUDE",      "sym": "CL=F",     "dp": 2, "unit": "$"},
    {"key": "USDINR",    "label": "USD/INR",    "sym": "USDINR=X", "dp": 2},
    {"key": "DOW",       "label": "DOW JONES",  "sym": "^DJI",     "dp": 2},
    {"key": "NASDAQ",    "label": "NASDAQ",     "sym": "^IXIC",    "dp": 2},
]


def _quote(http, sym, timeout=15):
    """One instrument from Yahoo -> dict, raises on failure."""
    url = YAHOO.format(sym=quote(sym, safe=""))
    r = http.get(url, timeout=timeout, retries=1, headers={"Accept": "application/json"})
    res = (r.json().get("chart") or {}).get("result") or []
    if not res:
        raise ValueError(f"no chart data for {sym}")
    meta = res[0].get("meta") or {}
    price = num(meta.get("regularMarketPrice"))
    if price is None:
        # fall back to the last close in the series
        closes = [c for c in ((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or [] if c]
        price = num(closes[-1]) if closes else None
    if price is None:
        raise ValueError(f"no price for {sym}")
    prev = num(meta.get("chartPreviousClose")) or num(meta.get("previousClose"))
    ts = meta.get("regularMarketTime")
    when = None
    live = False
    if ts:
        try:
            when = now_ist().fromtimestamp(int(ts), tz=now_ist().tzinfo)
            live = (now_ist() - when).total_seconds() <= LIVE_WINDOW_MIN * 60
        except Exception:
            when = None
    chg = (price - prev) if prev else None
    chg_pct = (chg / prev * 100) if (chg is not None and prev) else None
    return {"value": price, "prev": prev, "chg": chg, "chg_pct": chg_pct,
            "live": live, "as_of": when.isoformat(timespec="seconds") if when else None,
            "currency": meta.get("currency")}


def fetch(http, instruments=None):
    """Fetch every instrument. Returns a payload even when some fail (they get marked stale)."""
    items = []
    for inst in (instruments or INSTRUMENTS):
        try:
            q = _quote(http, inst["sym"])
        except Exception as e:  # noqa: BLE001
            items.append({**inst, "value": None, "error": f"{type(e).__name__}: {e}"[:120]})
            continue
        items.append({**inst, **q})
    return {"generated": now_ist().isoformat(timespec="seconds"),
            "market_open_hint": any(i.get("live") for i in items),
            "items": items}


def merge(previous, fresh):
    """Keep the last good value for any instrument that just failed (marked stale)."""
    prev_by = {i.get("key"): i for i in (previous or {}).get("items", [])}
    out = []
    for it in fresh.get("items", []):
        if it.get("value") is None and prev_by.get(it["key"], {}).get("value") is not None:
            old = dict(prev_by[it["key"]])
            old["stale"] = True
            old["error"] = it.get("error")
            out.append(old)
        else:
            out.append(it)
    fresh["items"] = out
    return fresh
