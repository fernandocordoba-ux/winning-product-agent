"""Live-run safety (Step S): Live Query Safety Gate, Query Budget, secret redaction,
structured run logging and run manifests.

Every paid KaloPilot query goes through kalopilot_client.submit(), which calls
check_live_query() first. With the default config/runtime.yaml the gate BLOCKS.

Live overrides are IN-MEMORY and per process (set_run_overrides); the YAML file
keeps the safe defaults.
"""
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SECRET_KEY_PATTERNS = ("token", "api_key", "apikey", "authorization", "secret", "password", "bearer", "credential")
UNKNOWN = "UNKNOWN"

_OVERRIDES = {}


class LiveQueryBlocked(RuntimeError):
    """Raised when a paid query is attempted while the Live Query Safety Gate says no."""


# ================================================================== config
def load_runtime(path=None):
    import config_resolver as _CR
    path = path or _CR.path("runtime.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def set_run_overrides(**kw):
    """In-memory, per-process overrides for ONE run (e.g. live_mode, dry_run, explicit_live_confirmation)."""
    allowed = {"live_mode", "dry_run", "explicit_live_confirmation"}
    bad = set(kw) - allowed
    if bad:
        raise ValueError(f"unsupported override(s): {sorted(bad)}")
    _OVERRIDES.update(kw)


def clear_run_overrides():
    _OVERRIDES.clear()


def effective_runtime(rt=None):
    rt = rt or load_runtime()
    return {**rt["runtime"], **_OVERRIDES}


# ================================================================== credentials
def credentials_present(rt=None):
    """True/False only — the credential value is never returned, printed or logged."""
    rt = rt or load_runtime()
    src = rt["provider"]["credential_sources"]
    if any(os.environ.get(k) for k in src.get("env", [])):
        return True
    p = Path(os.path.expanduser(src.get("file", ""))) if src.get("file") else None
    return bool(p and p.exists() and p.read_text().strip())


def known_secret_values(rt=None):
    rt = rt or load_runtime()
    src = rt["provider"]["credential_sources"]
    vals = [os.environ.get(k) for k in src.get("env", [])]
    p = Path(os.path.expanduser(src.get("file", ""))) if src.get("file") else None
    if p and p.exists():
        vals.append(p.read_text().strip())
    return [v for v in vals if v and len(v) >= 8]


# ================================================================== gate
@dataclass
class GateDecision:
    allowed: bool
    reasons: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)


def check_live_query(rt=None, credentials_ok=None, balance=None, estimated_cost=None):
    """ALL must hold: live_mode, not dry_run, explicit_live_confirmation, credentials, credit check."""
    rt = rt or load_runtime()
    r = effective_runtime(rt)
    s = rt["safety"]
    reasons, checks = [], {}
    checks["live_mode"] = r.get("live_mode") is True
    if not checks["live_mode"]:
        reasons.append("live_mode is not true")
    checks["dry_run_off"] = r.get("dry_run") is False
    if not checks["dry_run_off"]:
        reasons.append("dry_run is not false")
    checks["explicit_live_confirmation"] = r.get("explicit_live_confirmation") is True
    if not checks["explicit_live_confirmation"]:
        reasons.append("explicit_live_confirmation is not true")
    creds = credentials_present(rt) if credentials_ok is None else credentials_ok
    checks["credentials"] = bool(creds)
    if not creds:
        reasons.append("provider credentials not available")
    if s.get("require_credit_check", True):
        est = s["estimated_credits_per_query"] if estimated_cost is None else estimated_cost
        if balance is None:
            checks["credit_check"] = False
            reasons.append("credit balance unavailable (credit check required)")
        elif balance - est < s["min_balance_reserve"]:
            checks["credit_check"] = False
            reasons.append(f"insufficient credits: balance {balance} - estimated {est} < reserve {s['min_balance_reserve']}")
        else:
            checks["credit_check"] = True
    return GateDecision(allowed=not reasons, reasons=reasons, checks=checks)


# ================================================================== redaction
def redact(obj, secrets=None, patterns=SECRET_KEY_PATTERNS):
    """Drop secret-looking keys; replace known secret values and bearer tokens in strings."""
    secrets = known_secret_values() if secrets is None else secrets
    if isinstance(obj, dict):
        return {k: redact(v, secrets, patterns) for k, v in obj.items()
                if not any(p in str(k).lower() for p in patterns)}
    if isinstance(obj, (list, tuple)):
        return [redact(v, secrets, patterns) for v in obj]
    if isinstance(obj, str):
        for s in secrets:
            obj = obj.replace(s, "[REDACTED]")
        obj = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[REDACTED]", obj)
        obj = re.sub(r"(?i)((?:token|api[_-]?key|secret|password)\s*[=:]\s*)[^\s,;&\"']+", r"\1[REDACTED]", obj)
    return obj


# ================================================================== budget
class QueryBudget:
    """Per-run query accounting. Unknown costs stay UNKNOWN (never fabricated)."""

    def __init__(self, run_id, estimated_per_query=None):
        self.run_id = run_id
        self.estimated_per_query = estimated_per_query
        self.planned = self.executed = self.cached = self.blocked = self.failed = 0
        self.actual_costs, self.remaining_balance = [], None

    def plan(self, n=1):
        self.planned += n

    def record(self, status, actual_cost=None, balance=None):
        if status not in ("executed", "cached", "blocked", "failed"):
            raise ValueError(status)
        setattr(self, status, getattr(self, status) + 1)
        if status == "executed":
            self.actual_costs.append(actual_cost)
        if balance is not None:
            self.remaining_balance = balance

    def summary(self):
        paid = self.planned - self.cached
        known = [c for c in self.actual_costs if isinstance(c, (int, float))]
        return {
            "run_id": self.run_id, "planned_queries": self.planned, "executed_queries": self.executed,
            "cached_queries": self.cached, "blocked_queries": self.blocked, "failed_queries": self.failed,
            "estimated_credit_cost": (round(max(paid, 0) * self.estimated_per_query, 2)
                                      if self.estimated_per_query is not None else UNKNOWN),
            "estimated_credit_cost_basis": ("configured estimate per query (not a provider quote)"
                                            if self.estimated_per_query is not None else "no estimate configured"),
            "actual_credit_cost": (round(sum(known), 2) if known and len(known) == len(self.actual_costs)
                                   else (UNKNOWN if self.actual_costs else 0.0)),
            "remaining_balance": UNKNOWN if self.remaining_balance is None else self.remaining_balance,
        }


# ================================================================== logging / manifest
def new_run_id(now=None):
    now = now or datetime.now(timezone.utc)
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"


class RunLogger:
    """Structured JSON-lines log in runs/{run_id}/log.jsonl. Everything is redacted."""

    def __init__(self, run_dir, run_id, secrets=None):
        self.path = Path(run_dir) / "log.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id, self.secrets = run_id, secrets

    def log(self, stage, status, product_id=None, query_type=None, error_category=None, message=None, **extra):
        rec = {"run_id": self.run_id, "timestamp": datetime.now(timezone.utc).isoformat(), "stage": stage,
               "status": status, "product_id": product_id, "query_type": query_type,
               "error_category": error_category, "message": message, **extra}
        rec = redact(rec, self.secrets)
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return rec


def config_hashes(config_dir=ROOT / "config"):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:16] for p in sorted(Path(config_dir).glob("*.yaml"))}


def write_manifest(run_dir, data, secrets=None):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "manifest.json"
    with open(path, "x") as f:                      # one manifest per run, never overwritten
        json.dump(redact(data, secrets), f, ensure_ascii=False, indent=2, default=str)
    return path
