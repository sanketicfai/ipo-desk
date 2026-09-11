# 🔔 Closing-Day QIB & Subscription Tracker (15-min snapshots)

Live tracker + notice board for IPOs **closing today** — currently the four
issues closing **Fri, 11 Sep 2026**:

| IPO | Price band | Registrar |
|---|---|---|
| Karamtara Engineering (₹875 Cr) | ₹241–254 | MUFG Intime |
| Steamhouse India (₹290 Cr) | ₹77–81 | Kfin |
| Rentomojo (₹1,255.57 Cr) | ₹384–404 | Kfin |
| LCC Projects (₹299 Cr) | ₹139–146 | Kfin |

Every **15 minutes** (:00 / :15 / :30 / :45 IST) until the **5:00 PM** bidding
close it captures, per IPO:

- **Share-wise** — QIB / NII (bHNI + sHNI) / Retail / Total: offered shares vs
  applied shares and ×-subscribed
- **Application-wise** — bHNI / sHNI / Retail: reserved vs applied application
  counts and ×-by-applications, plus total applications
- **₹-demand** — per category, with the QIB FII / DII / MF split

Open **`closing_day.html`** for the dashboard: live cards, the QIB-participation
graph, per-IPO share-wise graphs, application-wise graphs, a printable
**forwardable notice** per snapshot, and the full snapshot history.

## Files

| File | Purpose |
|---|---|
| `closing_day_tracker.py` | Scraper (IPOJi's BSE/NSE bid data). One capture per run; `--loop` for a local 15-min runner. `--selftest` validates the parser offline. |
| `daily_ipo_update.py` | Runner used by Actions; writes `qib_closing_log.json` + mirrors to `qib_intraday_log.json` (committed). |
| `qib_closing_log.json` / `qib_intraday_log.json` | The snapshot log (identical content; the mirror exists because the existing workflow commits it). |
| `closing_day.html` | Dashboard (charts, notice, history; auto-refresh 60 s). |
| `closing_day_dispatcher.sh` | Sandbox helper: pings GitHub every 15 min to trigger a capture and syncs the committed log back. |
| `workflow-templates/closing-day.yml.txt` | Copy-paste workflow that gives a dedicated 15-min cron (see below). |
| `serve_closing_day.py` | Tiny preview server (port 4173). |

## How it runs

**On GitHub (recommended):** the existing `scrape.yml` cron runs
`daily_ipo_update.py` every 30 min (09:00–22:00 IST) once this branch is merged
to `main` — that alone keeps 30-min snapshots. For a true **15-min** cadence,
also add the workflow file from
`workflow-templates/closing-day.yml.txt` (GitHub does not let the bot create
workflow files): repo → *Add file* → *Create new file* →
`.github/workflows/closing-day.yml` → paste the part below `CUT BELOW` →
commit. It fires on its own every 15 min on closing days and also reacts to
`repository_dispatch` pings.

**On your own PC (zero setup, full 15-min fidelity):**

```
python closing_day_tracker.py --loop          # runs until 17:05 IST, then stops
python -m http.server 8000                    # then open http://localhost:8000/closing_day.html
```

**Edit the IPO list** for the next closing day in `IPOS` at the top of
`closing_day_tracker.py` (name → IPOJi slug).

## Data source

IPOJi (`ipoji.com/ipo-subscription/<slug>`), which compiles the **BSE & NSE
public-issue bidding platforms**. Snapshots are only stored when the source
page is stamped *today*, so the log never records stale numbers. The tracker
keeps the richest entry per 15-min slot (re-runs within a slot refresh it
instead of duplicating).
