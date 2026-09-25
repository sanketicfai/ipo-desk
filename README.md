# IPO Desk — live Indian IPO dashboard (Mainboard + SME)

Replaces the old `ipo_desk.html` + `ipo_data.json` file-picker setup. A small local server now
fetches, **merges and cross-verifies** data from **NSE, BSE, trynarada.com, InvestorGain and
Chittorgarh**, keeps refreshing it, and serves the dashboard at **http://localhost:8765**.

## Start (Windows)

1. Put this folder at `C:\Users\OM\Documents\ipo\` (or anywhere).
2. Double-click **`start.bat`**. The first run installs `requests` + `beautifulsoup4`.
3. Chrome opens `http://localhost:8765`. Keep the black window open while you use it.

Needs Python 3.8+ ([python.org](https://www.python.org/downloads/), tick *Add python.exe to PATH*).
macOS/Linux: `sh start.sh`.

The first run on an empty database takes about 5 minutes to load every IPO's details. The list is
usable right away and fills in as it goes. Later starts are instant because everything is kept in
`ipo_desk.db`.

## What you get

| Area | Details |
|---|---|
| Coverage | Every Mainboard + SME IPO whose issue opened in the last 12 months (~430), plus all upcoming ones |
| Status tabs | All · Open · Closing Today · Upcoming · Closed · Allotment Today · Allotted · Listing Today · Listed · Withdrawn, plus a Mainboard/SME filter, search, sortable columns, List/Cards views |
| GMP | Live GMP ₹ and %, low/high, estimated listing price, profit per lot, a sparkline in the table, and a **day-wise GMP history chart (₹ / %)** with open/close/allotment/listing markers and the actual listing gain |
| Subscription | **Share-wise** (₹ Cr and shares, QIB → FII/DFI/MF, NII → bNII/sNII, Retail/Individual, Employee) and **application-wise** (reserved vs received applications, times, allotment odds), day-wise table + chart, final basis of allotment |
| Performance | Listing price and listing gain %, day-1 close, **current price and % gain vs issue**, today's change |
| Company | Price band, lot, investment per category (RII/sNII/bNII min–max), reservation, timeline, about, financials, KPIs, objects of issue, promoters, lead managers, registrar, anchor investors, peers, DRHP/RHP links |
| Verification | Every field is compared across sources: ✓ agree · ⚠ differ (all values shown) · ◷ live (sources refresh at different times) · • single source |

## Where each number comes from

| Source | Used for |
|---|---|
| **NSE** | Official issue list, dates, price band, live category-wise bids, live quotes, listing-day bhavcopy |
| **BSE** | Live quotes, official bhavcopy (last close, listing-day open/close for BSE-listed issues) |
| **Narada** (trynarada.com) | Status buckets, GMP, share-wise + application-wise subscription, timeline, application sizes, listing & current prices |
| **InvestorGain** | IPO catalog, live GMP report, **full daily GMP history**, company details and financials, day-wise bids, listing tracker |
| **Chittorgarh** | Total applications, basis of allotment (applications / allottees per category), listing-day close |

Priority when sources disagree: official exchange data first for dates, prices and listing numbers.
The freshest snapshot wins for live subscription. InvestorGain is primary for GMP, with Narada shown
alongside.

## Refresh schedule (automatic)

- Open IPOs (GMP + subscription): every 5 min during the day. Current IPO lists: every 4 min.
- Listed prices: every 5 min in market hours (NSE/BSE live), plus Narada every 15 min and the BSE bhavcopy after the close.
- Everything else: every 30 min to 7 days, depending on how often it can change.
- **Refresh** (top bar) or **Refresh this IPO** (detail page) forces an immediate update.

## Good to know

- **GMP history keeps growing.** The desk saves every day's GMP from InvestorGain and Narada. Sources
  delete old history, but this database keeps it. For IPOs listed long ago, InvestorGain only
  publishes the last 1–3 days before listing plus the final GMP, so older charts are shorter.
- **NSE/BSE and cloud servers:** NSE blocks cloud/data-centre IPs. It works from a normal home or
  office connection in India. If a source is unreachable its pill turns amber/red (hover it for
  details) and the other sources cover the data.
- Data and settings live in `ipo_desk.db`. Delete it to start fresh.
- Exports: `http://localhost:8765/api/export.csv` (table) and `/api/export.json` (everything, like the old ipo_data.json).

## Options

```
python ipo_desk.py --port 8765 --lookback 365 --no-browser --no-chittorgarh --host 127.0.0.1
```
Use `--host 0.0.0.0` to open the dashboard from your phone on the same Wi-Fi (`http://<PC-IP>:8765`).

GMP is unofficial grey-market data and is indicative only. This is not investment advice.

### Helper scripts

| Script | Use |
|---|---|
| `check-upload.bat` / `check_upload.py` | Before dragging the folder into github.com: names every file that is too big (usually `ipo_desk.db`) |
| `seed_export.py` | Shrinks your accumulated GMP/subscription history into `seed/history.json.gz` so the cloud copy keeps it |
| `publish-now.bat` | Build `site/` on demand and preview it locally |

## Running it online (phone / anywhere, PC off)

This folder can also refresh itself in the cloud and publish itself as a static website on
GitHub Pages — same dashboard, same data, no server of your own.

See **[SETUP-ONLINE.md](SETUP-ONLINE.md)** for the step-by-step, no-terminal setup.

Quick reference for the cloud mode:

```
python ipo_desk.py --once --static-dir site --budget 420   # one refresh + build the static site
python serve_site.py site 8080                             # preview that site locally
```

```
python ipo_desk.py --once --export-only --static-dir site  # rebuild the site from the existing DB
```
