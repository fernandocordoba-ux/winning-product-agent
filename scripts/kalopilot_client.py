"""Minimal KaloPilot API client (submit + poll + free credit balance).

SPENDS CREDITS: submit(). FREE: credits(), result().
Per CLAUDE.md, never call submit() without the user's explicit OK after
showing balance, estimated cost and the exact queries.

CLI:
  python3 scripts/kalopilot_client.py credits
  python3 scripts/kalopilot_client.py discover [category_key ...]   # SPENDS CREDITS
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discovery  # noqa: E402

BASE = "https://www.kalodata.com/api/pilot/skill/ext/v1"


def _token():
    t = os.environ.get("KALOPILOT_TOKEN") or os.environ.get("KALODATA_API_KEY")
    if not t:
        p = Path.home() / ".kalopilot" / "token"
        t = p.read_text().strip() if p.exists() else None
    if not t:
        raise SystemExit("No KaloData token. Run: bash scripts/setup-token.sh <TOKEN>")
    return t


def _request(method, path, params=None, body=None, timeout=30):
    url = f"{BASE}/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {_token()}", "Content-Type": "application/json",
        # Cloudflare blocks the default Python-urllib User-Agent (error 1010)
        "User-Agent": "winning-product-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"success": False, "http_status": e.code, "message": e.read().decode()[:500]}


def credits():
    """FREE. Balance in credits (API returns 1/100 units)."""
    d = _request("GET", "credits")["data"]
    return {k: (d.get(k) or 0) / 100 for k in ("totalRemain", "monthlyRemain", "permanentRemain")}


def submit(query, task_id=None):
    """SPENDS CREDITS."""
    body = {"query": query, **({"task_id": task_id} if task_id else {})}
    return _request("POST", "chat/async/submit", body=body)


def result(task_id):
    return _request("GET", "chat/async/result", params={"task_id": task_id})


def wait(task_id, first_wait=90, interval=30, max_polls=40):
    time.sleep(first_wait)
    for _ in range(max_polls):
        r = result(task_id)
        if (r.get("data") or {}).get("status") not in ("running", "submitted"):
            return r
        time.sleep(interval)
    return r


def discover(category_keys=None):
    """SPENDS CREDITS. One query per category; raw responses saved read-only."""
    saved = []
    for q in discovery.build_queries():
        if category_keys and q["category_key"] not in category_keys:
            continue
        sub = submit(q["query"])
        task_id = (sub.get("data") or {}).get("task_id")
        now = datetime.now(timezone.utc)
        meta = {"category_key": q["category_key"], "query": q["query"], "task_id": task_id,
                "market": "US", "currency": "USD",
                "fetched_at": now.strftime("%Y%m%dT%H%M%SZ"), "observation_date": now.strftime("%Y-%m-%d")}
        response = wait(task_id) if task_id else sub
        path = discovery.save_raw(response, meta)
        used = ((response.get("data") or {}).get("credits_consumed"))
        print(f"{q['category_key']}: status={(response.get('data') or {}).get('status')} credits={used} raw={path}")
        saved.append(path)
    return saved


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "credits":
        c = credits()
        print(f"Total: {c['totalRemain']:.2f} | Monthly: {c['monthlyRemain']:.2f} | Permanent: {c['permanentRemain']:.2f}")
    elif len(sys.argv) >= 2 and sys.argv[1] == "discover":
        discover(sys.argv[2:] or None)
    else:
        print(__doc__)
