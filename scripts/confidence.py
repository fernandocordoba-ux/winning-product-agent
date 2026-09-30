"""Confidence Score (deterministic) — data completeness and evidence quality.

Separate from WPS. It never judges performance: a real 0, a negative growth or
a tiny GMV count as available data exactly like strong numbers. Rules and
points come from config/scoring.yaml -> `confidence`.

Missing = absent key, None, "", "N/A", non-numeric text, NaN/inf, or a value
below the check's min_value. 0 is a valid observation, NOT missing.
"""
import math
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import yaml

NA = "N/A"
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "scoring.yaml"


def load_config(path=None):
    import config_resolver as _CR
    path = path or (_CR.path(Path(DEFAULT_CONFIG).name) if _CR.active_dir() else DEFAULT_CONFIG)
    with open(path) as f:
        return yaml.safe_load(f)["confidence"]


def _available(v, min_value=None):
    """True if v is a real finite number (bool excluded), optionally >= min_value."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return False
    return min_value is None or v >= min_value


def _round(x, places):
    return float(Decimal(repr(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _coverage(product, check):
    """Return (coverage 0..1, detail dict) for one check. Never estimates."""
    t, field = check["type"], check["field"]
    value = product.get(field)
    min_value = check.get("min_value")

    if t == "field":
        ok = _available(value, min_value)
        return (1.0 if ok else 0.0), {"field": field, "value": value if ok else NA}

    if t == "series":
        expected = check["expected_items"]
        if not isinstance(value, list):
            return 0.0, {"field": field, "available_items": NA, "expected_items": expected}
        n = sum(1 for x in value if _available(x, min_value))
        return min(n / expected, 1.0), {"field": field, "available_items": n, "expected_items": expected}

    if t == "list":
        req, key = check["required_items"], check["value_key"]
        if not isinstance(value, list):
            return 0.0, {"field": field, "usable_items": NA, "required_items": req}
        n = sum(1 for it in value if isinstance(it, dict) and _available(it.get(key), 0))
        return min(n / req, 1.0), {"field": field, "usable_items": n, "required_items": req}

    if t == "count_evidence":
        full = check["full_at"]
        if not _available(value, 0):
            return 0.0, {"field": field, "value": NA, "full_at": full}
        return min(value / full, 1.0), {"field": field, "value": value, "full_at": full}

    raise ValueError(f"unknown check type: {t}")


def _consistency_violations(product, rules):
    out = []
    for r in rules or []:
        if r["rule"] == "lte":
            a, b = (product.get(f) for f in r["fields"])
            if _available(a) and _available(b) and a > b:
                out.append(f"{r['fields'][0]} ({a}) > {r['fields'][1]} ({b})")
    return out


def level_for(score, levels):
    for name, threshold in sorted(levels.items(), key=lambda kv: -kv[1]):
        if score >= threshold:
            return name
    return None


def confidence_score(product, cfg=None):
    """Compute Confidence Score for one normalized product record.

    Returns {"score", "level", "breakdown": {component: {...}}, "warnings", "version"}.
    The input dict is never modified.
    """
    cfg = cfg or load_config()
    places = cfg.get("rounding", 2)
    breakdown, warnings, total = {}, [], 0.0

    for name, comp in cfg["components"].items():
        checks = comp["checks"]
        weights = [c.get("weight", 1) for c in checks]
        details, weighted = [], 0.0
        for c, w in zip(checks, weights):
            cov, d = _coverage(product, c)
            d["coverage"] = _round(cov, 4)
            details.append(d)
            weighted += w * cov
        fraction = weighted / sum(weights)

        violations = _consistency_violations(product, comp.get("consistency"))
        if violations:
            fraction = 0.0
            warnings += [f"{name}: inconsistent data: {v}" for v in violations]

        earned = comp["points"] * fraction
        total += earned
        breakdown[name] = {
            "earned": _round(earned, places),
            "max": comp["points"],
            "status": "full" if fraction == 1 else ("missing" if fraction == 0 else "partial"),
            "checks": details,
        }

    score = _round(total, places)
    return {
        "score": score,
        "level": level_for(score, cfg["levels"]),
        "breakdown": breakdown,
        "warnings": warnings,
        "version": cfg.get("version"),
    }
