# Put your IPO Dashboard online — free, forever, PC off

Your dashboard currently lives at `http://localhost:8765`, which only works while
`ipo_desk.py` is running on your own PC. This guide moves the *fetching* and the *viewing*
into the cloud:

```
                 every 10 minutes (cloud, your PC can be OFF)
   ┌──────────────────────────────────────────────────────────────┐
   │  GitHub Actions runner                                       │
   │   python ipo_desk.py --once --static-dir site                │
   │   → fetches InvestorGain / Narada / NSE / BSE, merges,      │
   │     verifies, then writes  site/index.html + site/data/*.json│
   └───────────────────────────┬──────────────────────────────────┘
                               │ publishes (free)
                    ┌──────────▼───────────┐
                    │   GitHub Pages       │  ← https://<you>.github.io/ipo-desk/
                    └──────────┬───────────┘
                               │
                  phone · tablet · any Chrome · anywhere
```

Nothing to pay, no server to babysit, nothing running on your PC.
The dashboard looks and behaves the same — the only difference is where the numbers come from.

**You will not touch git commands, Python, or a terminal.** Everything below is done in a browser.

---

## What you need before you start

| Need | Why | Cost |
|---|---|---|
| A GitHub account | hosts the code + the runner + the website | free |
| The project folder (the zip in this chat) | it contains the upgraded code | free |
| 15–20 minutes | mostly waiting for clicks to save | free |

> Your dashboard code only changes in two small ways: `dashboard.html` can now also read
> `data/*.json` files (so it works on a plain web host), and `ipo_desk.py` gained a
> `--once` mode that refreshes everything and exits. Your local `start.bat` flow is untouched.

---

## Step 1 — Get the files onto your PC

1. Download **`ipo-desk-online.zip`** from this chat.
2. Right-click → **Extract All…** → you get a folder called **`ipo-desk`**.
   It contains:

   ```
   ipo-desk/
     dashboard.html          ← your dashboard (now works online too)
     ipo_desk.py             ← the engine (+ new --once mode)
     sources.py  merge.py  store.py  util.py  export_static.py  serve_site.py
     requirements.txt
     start.bat  start.sh  publish-now.bat
     README.md
     SETUP-ONLINE.md         ← this file
     .github/workflows/ipo-desk.yml   ← the job that runs in the cloud (hidden folder)
   ```

   If you can't see the `.github` folder, turn on **View → Hidden items** in File Explorer.
   It must be uploaded — it *is* the automation.

---

## Step 2 — Put the folder on GitHub

Pick **one** of these two routes.

### Route A — GitHub Desktop (easiest, 3 minutes)

1. Install **GitHub Desktop** from <https://desktop.github.com> and sign in (or create the account there).
2. **File → Add local repository…** → choose your extracted **`ipo-desk`** folder.
3. It will say *"this directory does not appear to be a Git repository"* → click **create a repository** → **Create repository**.
4. Click **Publish repository** (top bar). In the dialog, **untick "Keep this code private"**.
   → Repository name: `ipo-desk` → **Publish repository**.

   ⚠️ It must be **public**: public repos get unlimited free runner minutes, private ones only 2,000/month
   (this job uses ~4,000+). Nothing sensitive is in the folder — it's code plus public IPO data.

### Route B — browser only (no install)

> ⚠️ **Read this before dragging anything.** github.com rejects any single file over 25 MB with
> *"Yowza, that's a big file"*. Your project folder hides exactly such a file: **`ipo_desk.db`**,
> the SQLite database the desk writes on every run. It is your PC's live data — it must **not**
> be uploaded, and the cloud builds its own copy automatically.
> Drag **only the 18 files listed below**, from a folder that contains nothing else.

1. Go to <https://github.com/new>, name it **`ipo-desk`**, choose **Public**, click **Create repository**.
2. Extract **`ipo-desk-online.zip`** into a **brand-new empty folder** (e.g. `C:\Upload\ipo-desk`).
   Do **not** extract it into your existing working folder — that folder already holds the 26.8 MB database.
3. **Easiest path — let the toolbox build the upload folder for you.** In your *working* dashboard
   folder (the one with `ipo_desk.db` in it) double-click **`make-upload-folder.bat`**.
   It copies only the files GitHub needs into `..\ipo-desk-upload\ipo-desk`, checks every size,
   and opens that folder in Explorer:

   ```
   created C:\Users\OM\Documents\ipo-desk-upload\ipo-desk
     20 files, 246 KB in total
   VERDICT: safe to drag into github.com (or commit with GitHub Desktop).
   ```

4. In the folder it opens, turn on **View → Hidden items** (so `.github` and `.gitignore` show),
   press **Ctrl+A**, then **drag everything into the browser window** → **Commit changes**.
   Double-click **`check-upload.bat`** any time you want a size report before uploading.

   Expected contents (nothing more, nothing less):

   ```
   .github/workflows/ipo-desk.yml   dashboard.html   ipo_desk.py   sources.py   merge.py
   store.py   util.py   export_static.py   serve_site.py   seed_export.py   check_upload.py
   requirements.txt   start.bat   start.sh   publish-now.bat   check-upload.bat
   README.md   SETUP-ONLINE.md   workflow.yml   .gitignore
   ```

   *If `.github` did not come along* (browsers sometimes skip hidden folders), do this once:
   **Add file → Create new file**, type the name `.github/workflows/ipo-desk.yml`
   (typing the `/` creates the folders), then copy the contents of `workflow.yml` from the zip
   into the box and **Commit changes**.

> Drag the folder's **contents**, not the folder itself — dropping the `ipo-desk` folder into the
> uploader puts everything one level deep (`ipo-desk/dashboard.html`), and then the automation
> file is no longer at `.github/workflows/` where GitHub looks for it.

### Files that must never be uploaded

| File / folder | What it is | Why it stays out |
|---|---|---|
| `ipo_desk.db`, `ipo_desk.db-wal`, `ipo_desk.db-shm` | your live SQLite database | 25 MB+ easily; the cloud keeps its own copy |
| `site/` | the generated website | the cloud regenerates it every run |
| `ipo_data.json` | old export from the file-picker version | not needed, often large |
| `__pycache__/` | Python bytecode cache | junk |

`.gitignore` already lists all of these — plus `*.db`, `ipo_data.json`, `*.tmp` — so **GitHub Desktop / git
command line will ignore them automatically** — this is only a manual concern for drag-and-drop uploads.

---

## Step 3 — Tell GitHub to publish the site (one-time, 30 seconds)

1. In your repo: **Settings** (top menu) → **Pages** (left sidebar).
2. Under **Build and deployment → Source**, select **GitHub Actions** (not "Deploy from a branch").
3. That's it — no save button, no branch to choose.

---

## Step 4 — Run it once and watch it work

1. Click the **Actions** tab → if asked, click **"I understand my workflows, go ahead and enable them"**.
2. Left sidebar → **IPO Desk - refresh data & publish dashboard** → **Run workflow** button (right) → **Run workflow**.
3. Click the running job → click the **refresh** step to watch the log. You will see:

   ```
   catalog: 430 IPOs (IG 2075 catalog / 50 live / 611 listed, Narada 416, NSE 254)
   backfill slice: 30 IPOs (377 pending)
   bhavcopy: 397 price rows (latest 2026-09-24)
   run_once finished in 106.4s - 430 IPOs
   [static] 430 IPOs, version 54bd4201b163d4cc, 4.0 MB in site
   ```

4. When the job turns green, the **deploy** step prints your address:

   ```
   https://<your-username>.github.io/ipo-desk/
   ```

   Open it. First visit after a deploy can take a minute. (Settings → Pages also shows the URL.)

**Done.** From now on it refreshes by itself every ~10 minutes.

---

## Step 5 — Put it on your phone

1. Open the URL in **Chrome** on your phone.
2. Menu (⋮) → **Add to Home screen** → it opens full-screen like an app.
3. Chrome on desktop: the same URL, bookmark it. No `localhost`, no PC, works from any network.

On the page itself:
* **Refresh** button → re-reads the newest cloud snapshot (the server rebuilds it every 10 min).
* Everything else — tabs, search, sorting, GMP charts, subscription tables, verification panels — is identical.
* The header now says *"Market closed · updated 06:12:31 pm"*; that stamp is the last cloud refresh.

---

## What to expect from the cloud version (read this once)

| | On your PC | In the cloud (GitHub runner) |
|---|---|---|
| InvestorGain (catalog, live GMP, GMP history, day-wise bids) | ✅ | ✅ |
| Narada (status, GMP, share/application-wise subscription) | ✅ | ✅ |
| BSE (quotes, official bhavcopy) | ✅ | ✅ |
| NSE (official lists, live quotes) | ✅ | ✅ today — but NSE blocks many data-centre IPs, so some runs may show it amber/red. Other sources cover those fields. |
| Chittorgarh (total applications, BOA) | ✅ | ⏸ **off by default** — it takes 20–60 s per page and would eat the job's time budget. Turn it on later if you want (see below). |
| Company details for old listings | ✅ after ~5 min | fills in ~30 IPOs every run, so a fresh repo reaches full coverage in about 2 hours and stays there |

Source health is always visible: the little pills next to the title (green = ok, amber/red = that source
failed on the last run, hover for the reason). Your dashboard was built to survive exactly this.

---

## Troubleshooting

**"Yowza, that's a big file. Try again with a file smaller than 25MB."**
You dragged a folder containing `ipo_desk.db` (the SQLite database — it was ~15 MB after just two
runs in my test, and grows forever). Fix:
1. Extract `ipo-desk-online.zip` into a **new empty folder**.
2. Double-click **`check-upload.bat`** — it names every file that should not be uploaded.
3. Drag only the contents of that clean folder. The database never goes to GitHub; the cloud job
   builds its own and carries it between runs automatically.

If a file is genuinely too big and you *do* want it in the repo, GitHub's web uploader is the wrong
tool — use **GitHub Desktop** (it takes files up to 100 MB) or GitHub Releases (up to 2 GB).

**The workflow fails / shows a red ✗**
Open the failed run and read the last lines of the red step. The usual two:
* `Path Validation Error: ipo_desk.db*` on the *Save database* step → harmless on the very first run; the next run fixes it.
* A source changed its page layout → the log names the source. The site simply keeps the previous good data.

**The page says "Cloud data not reachable"**
You're offline, or the repo's Pages is not enabled yet (Step 3). Check `https://<you>.github.io/ipo-desk/data/version.json`
— if that shows a small JSON, hosting is fine and it's a network issue on the phone.

**The page works but is empty / 404**
Pages takes ~1 minute after the first deploy. If it persists, make sure the *Source* is **GitHub Actions**, and that the
job's last step (**Publish to GitHub Pages**) is green.

**I changed something and nothing happened**
Edits to `dashboard.html` or any `.py` file trigger a run automatically. Data-only changes are committed by the job itself.

**The site stopped updating**
GitHub pauses scheduled workflows after 60 days of *no repository activity*. Because this job deploys on every run,
that never happens in practice. If you ever see it paused, pressing **Run workflow** wakes it up.

---

## Making it fresher / faster (optional)

| Want | Do this |
|---|---|
| Refresh every 5 min instead of 10 | Edit `.github/workflows/ipo-desk.yml` → `cron: "*/5 * * * *"`. GitHub's free Pages allows ~10 builds/hour; `*/5` is right at the edge, so `*/7` or `*/10` is safer. |
| Fresher data per run (more time to fetch) | In the workflow, raise `--budget 420` to e.g. `--budget 900`. |
| Add Chittorgarh (total applications, basis of allotment) | Change `--cg-budget 0` to `--cg-budget 300` in the workflow. |
| Only one year of IPOs | Add `--lookback 180` to the command. |
| Custom domain (e.g. `ipo.yourname.com`) | Settings → Pages → Custom domain. Free, you only pay if you buy the domain. |

---

## Optional — carry your accumulated GMP history into the cloud

Your PC database holds something the cloud cannot re-download: every day's GMP the desk recorded,
for every IPO. InvestorGain deletes old history after a few days; your database kept it. That is
the one thing worth transferring — but the database itself is far too big to upload.

`seed_export.py` squeezes just that history into one small file:

```bat
cd C:\Users\OM\Documents\ipo
python seed_export.py
```

```
wrote C:\Users\OM\Documents\ipo\seed\history.json.gz
  1,062 GMP history rows  (201 IPOs)
  159 subscription snapshots
  19.0 KB compressed  (16 MB database shrunk to this)
```

*(Measured in the test run: a 16 MB database became a 19 KB file — yours will be a few hundred KB
at most, comfortably inside the 25 MB upload limit.)*

Then:
1. Upload/commit the new **`seed/`** folder to your repo (GitHub Desktop picks it up automatically;
   browser upload: click **Add file → Upload files** and drop `history.json.gz`).
2. The next cloud run prints `seed: imported … GMP rows` in the log and merges them into the cloud
   database — only rows it doesn't already have, and it never runs twice for the same file
   (guarded by a content hash).

After that, the cloud desk keeps growing that history on its own, every 10 minutes, forever.

## Appendix A — Optionally keep the local version as “the premium one”

Your PC version is still the best data (NSE works from an Indian home connection, Chittorgarh is on by default).
Keep `start.bat` for when you're at your desk; use the GitHub URL when you're out. They don't conflict —
the cloud copy is independent and always current.

If you later want the *cloud* copy refreshed from your own PC (best sources) whenever it's switched on,
that's a small addition — ask and I'll wire it up.

## Appendix B — Other free hosts (if you'd rather not use GitHub Actions)

* **Netlify Drop / Cloudflare Pages** — drag the generated `site` folder onto <https://app.netlify.com/drop> and you get a public URL in seconds. Great for a *manual* refresh: on your PC run `publish-now.bat`, then drag the new `site` folder again. No cron, so it only updates when you push it.
* **Render / Railway / Hugging Face Spaces** (free tier) — these can run the **full live server** (`python ipo_desk.py --host 0.0.0.0 --port $PORT`), which keeps the in-page 5-minute live refresh and the *Refresh* button working exactly like localhost. Caveats: free instances sleep when idle (first open takes ~50 s), and their disk is wiped on restart, so the SQLite memory is lost and rebuilds. Fine for a live glance; worse than GitHub Actions for long-term GMP history.

## Appendix C — What I'd improve in the dashboard next (you asked for round 2)

1. **₹0 GMP vs “no GMP”** — when InvestorGain shows `₹--` (no grey-market premium yet) and Narada says ₹0,
   the table prints “₹0 (0.0%) ✓”. Showing “–” with a “no GMP yet” tooltip would be more honest.
2. **`data/export.csv` link** — already regenerated by the cloud job, so CSV downloads keep working online.
3. **PWA / offline** — a tiny manifest + service worker would let the phone open it with no signal (last cached snapshot).
4. **Push notifications** for “IPO opens today / closing today / listing today” via a free service worker push.

---

## Updating the site later (after any new change)

Every improvement arrives as a fresh `ipo-desk-part2-upload.zip`. To put it online — browser only, about 3 minutes:

1. **Download** the zip from the chat, right-click → **Extract All…** → open the `ipo-desk` folder.
2. Turn on **View → Hidden items** (so `.github` and `.gitignore` are visible).
3. Open your repository page: `https://github.com/<your-name>/ipo-desk`.
4. Click **Add file → Upload files** (top right of the file list).
5. Press **Ctrl+A** in the extracted folder, then **drag everything** into the page's drop area.
   Files with the same names are replaced; nothing else is touched.
6. Scroll down, type a note like `round 8 — chart + market-study plans`, click **Commit changes**.
7. GitHub starts the job by itself (tab **Actions** → the top run shows a spinner). In about **2 minutes** it turns green.
8. Open `https://<your-name>.github.io/ipo-desk/` and hard-refresh (**Ctrl+Shift+R**). It now matches the preview.

If the page still shows the old version, wait one more minute and refresh again — Pages can lag behind the green tick.

**The three files that do the work**

| File | What it is |
|---|---|
| `dashboard.html` | the whole page — tabs, tables, chart, plans |
| `ipo_desk.py`, `sources.py`, `merge.py`, `store.py`, `export_static.py` | the fetching and building |
| `cg_sub_cache.json` | the archived category-wise subscription records for older issues |
| `.github/workflows/ipo-desk.yml` | the cloud job: refresh + publish, every 5 minutes |

Nothing on your PC needs to run for the site to stay current — the cloud job refreshes it by itself.
