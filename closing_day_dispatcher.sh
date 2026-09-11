#!/usr/bin/env bash
# 15-min closing-day dispatcher.
# The sandbox cannot reach BSE/NSE/ipoji directly, but GitHub Actions can.
# This loop triggers the repo's existing scrape.yml workflow every quarter hour
# (+75 s so the source pages have fresh data), waits for the run, then syncs the
# committed qib_intraday_log.json back into the workspace for the live preview.
set -u
cd /home/user/ipo-desk
REPO="sanketicfai/ipo-desk"
BRANCH="arena/01a08fb0-ipo-desk"
WF="scrape.yml"
IST() { TZ=Asia/Kolkata date "+%H:%M:%S"; }
log() { echo "[$(IST) IST] $*"; }

sync_log() {
  if gh api "repos/$REPO/contents/qib_intraday_log.json?ref=$BRANCH" --jq '.content' 2>/dev/null | base64 -d > qib_intraday_log.json.tmp 2>/dev/null && [ -s qib_intraday_log.json.tmp ]; then
    python3 - <<'PY'
import json, shutil
try:
    loc = json.load(open("qib_intraday_log.json"))
except Exception:
    loc = {"snapshots": []}
try:
    rem = json.load(open("qib_intraday_log.json.tmp"))
except Exception:
    raise SystemExit(0)
keep = rem if len(rem.get("snapshots", [])) >= len(loc.get("snapshots", [])) else loc
for p in ("qib_intraday_log.json", "qib_closing_log.json"):
    json.dump(keep, open(p, "w"), indent=1, ensure_ascii=False)
print("  snapshots in log:", len(keep.get("snapshots", [])))
PY
    rm -f qib_intraday_log.json.tmp
    log "log synced from GitHub"
  else
    log "log sync failed (will retry next tick)"
  fi
}

log "dispatcher started (branch $BRANCH, stops after 17:25 IST)"
while :; do
  HHMM=$(TZ=Asia/Kolkata date +%H%M)
  if [ "10#$HHMM" -ge 1725 ]; then log "past 17:25 IST — done for today"; sync_log; break; fi

  log "→ triggering workflow scrape.yml on $BRANCH"
  if gh workflow run "$WF" --repo "$REPO" --ref "$BRANCH" -f note="sandbox 15-min tick $(IST)" 2>&1; then
    sleep 25
    for i in $(seq 1 24); do
      J=$(gh run list --repo "$REPO" --workflow "$WF" --branch "$BRANCH" --limit 1 --json status,conclusion 2>/dev/null || echo '[]')
      ST=$(python3 -c "import sys,json;d=json.load(sys.stdin);print(d[0]['status'] if d else 'none')" <<<"$J" 2>/dev/null || echo none)
      CC=$(python3 -c "import sys,json;d=json.load(sys.stdin);print(d[0].get('conclusion') or '-' if d else '-')" <<<"$J" 2>/dev/null || echo -)
      if [ "$ST" = "completed" ]; then log "workflow run $CC"; break; fi
      sleep 10
    done
  else
    log "dispatch failed — will retry next tick"
  fi

  sync_log

  # sleep to next quarter mark + 75 s
  M=$(TZ=Asia/Kolkata date +%M); S=$(TZ=Asia/Kolkata date +%S)
  WAIT=$(( (15*60 - ((10#$M % 15)*60 + 10#$S)) + 75 ))
  HHMM=$(TZ=Asia/Kolkata date +%H%M)
  if [ "10#$HHMM" -ge 1720 ]; then log "final window reached"; fi
  log "next tick in ${WAIT}s"
  sleep "$WAIT"
done
