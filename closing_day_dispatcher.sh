#!/usr/bin/env bash
# 15-min closing-day dispatcher (sandbox side).
#
# The sandbox cannot reach BSE/NSE/ipoji directly — but GitHub Actions can, and
# the sandbox CAN talk to api.github.com. Every quarter hour this loop:
#   1. pings repository_dispatch (event: closing-day-tick) → runs the
#      closing-day.yml workflow IF the user has added it (contents:write is
#      enough to send this event); a no-op ping until then;
#   2. also tries workflow_dispatch on scrape.yml (works if permissions allow);
#   3. syncs the committed qib_intraday_log.json / qib_closing_log.json from
#      GitHub (session branch AND main) into the workspace for the preview.
set -u
cd /home/user/ipo-desk
REPO="sanketicfai/ipo-desk"
BRANCH="arena/01a08fb0-ipo-desk"
IST() { TZ=Asia/Kolkata date "+%H:%M:%S"; }
log() { echo "[$(IST) IST] $*"; }

richest() { # stdout: ref (branch or main) whose committed log has more snapshots
  local b m
  b=$(gh api "repos/$REPO/contents/qib_intraday_log.json?ref=$BRANCH" --jq '.content' 2>/dev/null | base64 -d 2>/dev/null | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('snapshots',[])))" 2>/dev/null || echo -1)
  m=$(gh api "repos/$REPO/contents/qib_intraday_log.json?ref=main" --jq '.content' 2>/dev/null | base64 -d 2>/dev/null | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('snapshots',[])))" 2>/dev/null || echo -1)
  if [ "$((m))" -gt "$((b))" ]; then echo "main"; else echo "$BRANCH"; fi
}

sync_log() {
  local best; best=$(richest)
  local ref=$best
  if gh api "repos/$REPO/contents/qib_intraday_log.json?ref=$ref" --jq '.content' 2>/dev/null | base64 -d > qib_intraday_log.json.tmp 2>/dev/null && [ -s qib_intraday_log.json.tmp ]; then
    python3 - <<'PY'
import json
try:
    rem = json.load(open("qib_intraday_log.json.tmp"))
except Exception:
    raise SystemExit(0)
for p in ("qib_intraday_log.json", "qib_closing_log.json"):
    json.dump(rem, open(p, "w"), indent=1, ensure_ascii=False)
print("  snapshots in log:", len(rem.get("snapshots", [])), "· slots:", [s.get("slot") for s in rem.get("snapshots", [])])
PY
    rm -f qib_intraday_log.json.tmp
    log "log synced (from $ref)"
  else
    log "log sync failed (will retry next tick)"
  fi
}

log "dispatcher started (stops after 17:25 IST)"
sync_log
while :; do
  HHMM=$(TZ=Asia/Kolkata date +%H%M)
  if [ "$((10#$HHMM))" -ge 1725 ]; then log "past 17:25 IST — done for today"; sync_log; break; fi

  log "→ pinging repository_dispatch closing-day-tick"
  code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "https://api.github.com/repos/$REPO/dispatches" \
        -H "Authorization: Bearer ${GH_TOKEN:-}" -H "Accept: application/vnd.github+json" \
        -d '{"event_type":"closing-day-tick","client_payload":{"source":"sandbox"}}')
  log "  repository_dispatch HTTP $code $([ "$code" = "204" ] && echo '(sent — runs if closing-day.yml exists)' || echo '(failed)')"

  if gh workflow run scrape.yml --repo "$REPO" --ref "$BRANCH" 2>/dev/null; then
    log "  scrape.yml dispatch sent too"
  fi

  sleep 100          # let the runner finish (checkout+capture+commit ≈ 1-2 min)
  sync_log

  M=$(TZ=Asia/Kolkata date +%M); S=$(TZ=Asia/Kolkata date +%S)
  WAIT=$(( (15*60 - ((10#$M % 15)*60 + 10#$S)) + 75 ))
  log "next tick in ${WAIT}s"
  sleep "$WAIT"
done
