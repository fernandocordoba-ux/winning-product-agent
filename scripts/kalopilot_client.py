"""Minimal KaloPilot API client (submit + poll + free credit balance).

SPENDS CREDITS: submit(). FREE: credits(), result().
Per CLAUDE.md, never call submit() without the user's explicit OK after
showing balance, estimated cost and the exact queries.

Step S: submit() is the single chokepoint for paid queries and calls the Live
Query Safety Gate (scripts/safety.py) first. With config/runtime.yaml defaults
(live_mode false, dry_run true, no explicit confirmation) every submit() is
BLOCKED with safety.LiveQueryBlocked before any network call.
Network failures (unreachable, timeout) return {"success": False, "error_category": ...}
instead of crashing.

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
import safety  # noqa: E402


class ProviderUnavailable(RuntimeError):
    """KaloData/KaloPilot could not be reached or returned no usable data."""

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
        return {"success": False, "http_status": e.code, "error_category": "http_error",
                "message": safety.redact(e.read().decode()[:500])}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"success": False, "error_category": "provider_unavailable",
                "message": safety.redact(f"KaloData/KaloPilot unreachable: {e}")}
    except json.JSONDecodeError:
        return {"success": False, "error_category": "malformed_response", "message": "response was not valid JSON"}


def credits():
    """FREE. Balance in credits (API returns 1/100 units)."""
    r = _request("GET", "credits")
    d = r.get("data") if isinstance(r, dict) else None
    if not isinstance(d, dict):
        raise ProviderUnavailable(f"credit balance unavailable ({r.get('error_category') or 'no data'}): "
                                  f"{r.get('message') or ''}".strip())
    return {k: (d.get(k) or 0) / 100 for k in ("totalRemain", "monthlyRemain", "permanentRemain")}


def submit(query, task_id=None, estimated_cost=None):
    """SPENDS CREDITS — only if the Live Query Safety Gate allows it."""
    rt = safety.load_runtime()
    pre = safety.check_live_query(rt, balance=float("inf"))          # flags + credentials, no network yet
    if not pre.allowed:
        raise safety.LiveQueryBlocked("BLOCKED LIVE QUERY: " + "; ".join(pre.reasons))
    try:
        balance = credits()["totalRemain"] if rt["safety"].get("require_credit_check", True) else float("inf")
    except ProviderUnavailable as e:
        raise safety.LiveQueryBlocked(f"BLOCKED LIVE QUERY: {e}")
    gate = safety.check_live_query(rt, balance=balance, estimated_cost=estimated_cost)
    if not gate.allowed:
        raise safety.LiveQueryBlocked("BLOCKED LIVE QUERY: " + "; ".join(gate.reasons))
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
    return {**r, "success": False, "error_category": "timeout",
            "message": f"task {task_id} still running after {max_polls} polls"}


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
