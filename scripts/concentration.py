"""Creator / video revenue concentration analysis (deterministic).

Rules and thresholds come from config/filters.yaml -> `concentration`.
Nothing here is estimated: if the data needed for a metric is missing,
the metric is "N/A" and no flag/penalty is applied.

Input product (dict):
    {
      "product_id": "...",
      "gmv": 4206171,                     # product revenue for the period
      "creators_count": 3807,             # optional, total creators for product
      "creators": [{"name": "...", "revenue": 432695}, ...],
      "videos_count": 8516,               # optional, total videos for product
      "videos":   [{"id": "...", "revenue": 313800}, ...],
    }

Usage:
    python3 scripts/concentration.py data/processed/<file>.json
"""
import json
import math
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import yaml

NA = "N/A"
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "filters.yaml"


def load_config(path=None):
    import config_resolver as _CR
    path = path or (_CR.path(Path(DEFAULT_CONFIG).name) if _CR.active_dir() else DEFAULT_CONFIG)
    with open(path) as f:
        return yaml.safe_load(f)["concentration"]


def _number(v):
    """Return v as float if it is a real finite number (not bool), else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _round(x, places):
    q = Decimal(1).scaleb(-places)
    return float(Decimal(repr(x)).quantize(q, rounding=ROUND_HALF_UP))


def _check(value, condition):
    op, threshold = condition.split()
    threshold = float(threshold)
    return {
        ">": value > threshold, ">=": value >= threshold,
        "<": value < threshold, "<=": value <= threshold,
    }[op]


def share(product, source, top_n, cfg):
    """Top-N revenue share (%) for source 'creators' or 'videos'.

    Returns {"value": float | "N/A", "reason": str | None, "inputs": {...}}.
    """
    gmv = _number(product.get("gmv"))
    items = product.get(source)
    total_count = _number(product.get(f"{source}_count"))
    inputs = {"gmv": product.get("gmv"), f"{source}_count": product.get(f"{source}_count"),
              "items_returned": len(items) if isinstance(items, list) else None}

    def na(reason):
        return {"value": NA, "reason": reason, "inputs": inputs}

    if gmv is None or gmv <= 0:
        return na("product GMV missing or <= 0")
    if not isinstance(items, list) or not items:
        return na(f"no {source} revenue data returned")

    revenues = []
    for it in items:
        r = _number(it.get("revenue")) if isinstance(it, dict) else None
        if r is None or r < 0:
            return na(f"missing or invalid revenue for a returned {source[:-1]}")
        revenues.append(r)

    revenues.sort(reverse=True)
    if len(revenues) < top_n:
        # Only valid if we know the product has no more items than were returned.
        if total_count is None or total_count > len(revenues):
            return na(f"fewer than {top_n} {source} returned and total count unknown/larger")

    top_sum = sum(revenues[:top_n])
    value = top_sum / gmv * 100
    inputs["top_revenue_sum"] = top_sum
    if value > cfg.get("max_valid_share_pct", 100):
        return na(f"share {value:.2f}% > {cfg.get('max_valid_share_pct', 100)}% (inconsistent data)")
    return {"value": _round(value, cfg.get("rounding", 2)), "reason": None, "inputs": inputs}


def analyze(product, cfg=None):
    """Compute all concentration metrics and flags for one product."""
    cfg = cfg or load_config()
    if not cfg.get("enabled", True):
        return {"product_id": product.get("product_id"), "enabled": False}

    metrics = {name: share(product, m["source"], m["top_n"], cfg)
               for name, m in cfg["metrics"].items()}

    flags = {}
    for flag, rule in cfg["flags"].items():
        v = metrics[rule["metric"]]["value"]
        if v == NA:
            flags[flag] = {"status": NA, "reason": metrics[rule["metric"]]["reason"]}
        elif _check(v, rule["condition"]):
            flags[flag] = {"status": "FLAGGED", "action": rule["action"],
                           "value": v, "condition": rule["condition"]}
        else:
            flags[flag] = {"status": "CLEAR", "value": v, "condition": rule["condition"]}

    return {"product_id": product.get("product_id"), "metrics": metrics, "flags": flags}


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 1
    data = json.load(open(argv[1]))
    products = data if isinstance(data, list) else [data]
    cfg = load_config()
    print(json.dumps([analyze(p, cfg) for p in products], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
