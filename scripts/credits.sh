#!/usr/bin/env bash
# Free KaloPilot credit balance check (costs no credits). API values are in 1/100 credits.
set -euo pipefail
bash "$(cd "$(dirname "$0")" && pwd)/setup-token.sh" >/dev/null
curl -s -H "Authorization: Bearer $(cat "$HOME/.kalopilot/token")" \
  https://www.kalodata.com/api/pilot/skill/ext/v1/credits | python3 -c '
import sys,json; d=json.load(sys.stdin)["data"]
f=lambda k:(d.get(k) or 0)/100
print(f"Total: {f(\"totalRemain\"):.2f} credits | Monthly: {f(\"monthlyRemain\"):.2f} | Permanent: {f(\"permanentRemain\"):.2f}")'
