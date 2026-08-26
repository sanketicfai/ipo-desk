# 📱 IPO Desk on your phone — from ANYWHERE (free, no PC needed)

This kit turns your IPO Desk into a public website that **updates itself every
hour on GitHub's servers**. Your PC can be switched off. Open the link on your
phone from any network — home, office, 4G, anywhere.

Final result: `https://YOUR-USERNAME.github.io/ipo-desk/`

## One-time setup (~15 minutes)

1. **Create a GitHub account** at github.com (free), verify your email.
2. **Create the repository:** click the **+** (top-right) → **New repository**
   → name it `ipo-desk` → select **Public** → click **Create repository**.
3. **Add your API key as a secret** (so it stays hidden):
   In the repo: Settings → Secrets and variables → **Actions** →
   **New repository secret** → Name: `FNA_API_KEY`
   Secret: (paste your fna_live_... key) → **Add secret**.
4. **Upload everything in this kit** to the repo:
   On the repo page click **"uploading an existing file"** link →
   drag-and-drop ALL files/folders from this kit (including the `.github`
   folder — dragging a folder keeps its structure) →
   Commit message: `start ipo desk` → **Commit changes**.
5. **Turn on the website:** Settings → **Pages** (left sidebar) →
   Source: **Deploy from a branch** → Branch: **main** / folder: **/(root)**
   → **Save**. Wait ~2 minutes.
6. **Open on your phone (any network):**
   `https://YOUR-USERNAME.github.io/ipo-desk/`
   Chrome menu (⋮) → **Add to Home screen** → done, it's an app now.

## What happens after that

- GitHub runs `daily_ipo_update.py` **every hour, 09:00–22:00 IST**
  (see the "Actions" tab to watch each run).
- Fresh `ipo_data.json` is committed automatically, and your phone's desk
  re-fetches it every 60 s — always current, from anywhere.
- GMP history, intraday subscription history and the Fresh/OFS cache are
  committed too, so **history accumulates in the cloud** even if you never
  run the script locally again.
- Keep the repo **public** (required for free Pages). Only market data is
  published — your API key stays hidden as a secret (it is NOT in the
  uploaded script; the script reads it from the secret at runtime).

## Good to know

- GitHub's scheduler is best-effort: runs can start a few minutes late.
  That's fine for hourly IPO data.
- GitHub Pages caches files a few minutes, so the phone may lag up to ~10 min
  behind the newest scrape.
- The Fresh/OFS lookups and DuckDuckGo searches run from GitHub's IPs; the
  committed `fresh_ofs_cache.json` means each IPO is still only fetched once.
- Prefer everything private? GitHub's free plan doesn't allow Pages on private
  repos — use Tailscale (see main README) instead, or upgrade.

## Local use (optional)

The same script still works on your PC: put your key in a `fna_key.txt` file
next to it (never upload that file — it's in .gitignore), or set the
`FNA_API_KEY` environment variable.
