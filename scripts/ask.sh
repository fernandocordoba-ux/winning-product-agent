#!/usr/bin/env bash
# Ask KaloPilot a question and wait for the answer.
# Usage: bash scripts/ask.sh "<question>" [task_id_for_follow_up]
# Saves the unmodified API response (+ query, task_id, fetched_at) to data/raw/<timestamp>_<task_id>.json
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PILOT="$DIR/kalopilot/scripts/pilot.sh"
[ $# -ge 1 ] || { echo "Usage: bash scripts/ask.sh \"<question>\" [task_id]"; exit 1; }
bash "$DIR/scripts/setup-token.sh" >/dev/null
echo "Submitting: $1"
bash "$PILOT" query "$1" ${2:+"$2"}
echo "Waiting for KaloPilot (usually 1-3 min)..."
sleep 45
mkdir -p "$DIR/data/raw"
for i in $(seq 1 40); do
  out=$(bash "$PILOT" result)
  status=$(printf '%s' "$out" | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["data"]["status"])
except Exception: print("unknown")')
  if [ "$status" != "running" ] && [ "$status" != "submitted" ]; then
    ts=$(date -u +%Y%m%dT%H%M%SZ); tid=$(cat "$HOME/.kalopilot/task_id" 2>/dev/null || echo unknown)
    f="$DIR/data/raw/${ts}_${tid}.json"
    QUERY="$1" FETCHED="$ts" python3 -c 'import sys,json,os
print(json.dumps({"query":os.environ["QUERY"],"fetched_at":os.environ["FETCHED"],"response":json.loads(sys.stdin.read())},ensure_ascii=False,indent=2))' <<<"$out" > "$f"
    chmod 444 "$f"   # raw data is never edited
    printf '%s' "$out" | python3 -c 'import sys,json
d=json.load(sys.stdin).get("data") or {}
print("\nSTATUS:", d.get("status")); print("TASK_ID:", d.get("task_id"))
print("\n"+(d.get("text") or "")); 
if d.get("report"): print("\n"+d["report"])
if d.get("report_url"): print("\nFull report:", d["report_url"])
if d.get("error"): print("ERROR:", d["error"])
if d.get("credits_consumed") is not None: print("\n(credits consumed:", d["credits_consumed"],")")'
    echo "Saved: $f"; exit 0
  fi
  echo "  still running... ($i)"; sleep 30
done
echo "Timed out waiting; poll later with: bash kalopilot/scripts/pilot.sh result"
