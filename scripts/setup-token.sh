#!/usr/bin/env bash
# Saves the KaloData token to ~/.kalopilot/token (where KaloPilot reads it).
# Order: 1) argument  2) $KALOPILOT_TOKEN / $KALODATA_API_KEY env var  3) already saved
set -euo pipefail
TOKEN="${1:-${KALOPILOT_TOKEN:-${KALODATA_API_KEY:-}}}"
mkdir -p "$HOME/.kalopilot"
if [ -n "$TOKEN" ]; then
  printf '%s' "$TOKEN" > "$HOME/.kalopilot/token"; chmod 600 "$HOME/.kalopilot/token"
  echo "Token saved."
elif [ -s "$HOME/.kalopilot/token" ]; then
  echo "Token already configured."
else
  echo "ERROR: no token. Run: bash scripts/setup-token.sh <YOUR_KALODATA_TOKEN>" >&2; exit 1
fi
# Free connectivity check (no credits): a fake task_id should return TASK_NOT_FOUND, not 401.
code=$(curl -s -o /tmp/kp_check.json -w '%{http_code}' -G \
  "https://www.kalodata.com/api/pilot/skill/ext/v1/chat/async/result" \
  --data-urlencode "task_id=connectivity-test" \
  -H "Authorization: Bearer $(cat "$HOME/.kalopilot/token")")
case "$code" in
  404) echo "OK: KaloData reachable and token accepted." ;;
  401) echo "ERROR: token rejected (401). Get a fresh one at kalodata.com/pilot." >&2; exit 1 ;;
  403) echo "ERROR: 403. Check www.kalodata.com is in Settings > Capabilities > allowed domains." >&2; exit 1 ;;
  *)   echo "Unexpected HTTP $code:"; cat /tmp/kp_check.json; echo; exit 1 ;;
esac
