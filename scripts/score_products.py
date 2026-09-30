"""Scoring engine: WPS (Winning Product Score) + Confidence Score.

Both are computed ONLY from config/scoring.yaml rules (Claude never assigns them).
They are independent: Confidence never changes WPS, WPS never changes Confidence.

WPS (wps-v1) implements config/scoring.yaml `metrics` exactly:
  scale functions  linear | log10 | ratio | derive: coefficient_of_variation
  s clamped to [0, 1]; component = s * weight; metric = points * sum(components)
  missing_data.metric_rule = all_components_required -> any N/A component => metric N/A (0 pts)
  wps = sum(metric points), N/A = 0; rounding half-up to output.rounding decimals.

Usage:
    python3 scripts/score_products.py <normalized products .json>
"""
import json
import math
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from confidence import NA, confidence_score, load_config as load_confidence_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def load_scoring_config(path=ROOT / "config" / "scoring.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _round(x, places):
    return float(Decimal(repr(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _clamp(s):
    return max(0.0, min(1.0, s))


def _cmp(value, condition):
    op, t = condition.split()
    t = float(t)
    return {">": value > t, ">=": value >= t, "<": value < t, "<=": value <= t}[op]


def component_value(product, comp):
    """Return (s in [0,1] or None, detail). None = N/A (never estimated)."""
    raw = product.get(comp["input"])
    detail = {"input": comp["input"], "raw": raw}

    if comp.get("derive") == "coefficient_of_variation":
        vals = [v for v in (_num(x) for x in (raw if isinstance(raw, list) else [])) if v is not None]
        if len(vals) < comp.get("min_items", 1):
            return None, {**detail, "raw": f"{len(vals)} valid points", "reason": "not enough points"}
        mean = sum(vals) / len(vals)
        if mean == 0:
            return None, {**detail, "raw": f"{len(vals)} valid points", "reason": "mean is 0"}
        x = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals)) / mean
        detail = {**detail, "raw": f"{len(vals)} valid points", "derived": x}
    elif comp["scale"] == "ratio":
        vals = [v for v in (_num(x) for x in (raw if isinstance(raw, list) else [])) if v is not None]
        if len(vals) < comp.get("min_items", 1):
            return None, {**detail, "reason": f"fewer than {comp.get('min_items', 1)} valid items"}
        s = sum(_cmp(v, comp["match"]) for v in vals) / len(vals)
        return s, {**detail, "s": s}
    else:
        x = _num(raw)
        if x is None:
            return None, {**detail, "reason": "missing"}

    z, f = float(comp["zero_at"]), float(comp["full_at"])
    if comp["scale"] == "linear":
        s = (x - z) / (f - z)
    elif comp["scale"] == "log10":
        s = 0.0 if x <= 0 else (math.log10(x) - math.log10(z)) / (math.log10(f) - math.log10(z))
    else:
        raise ValueError(f"unknown scale {comp['scale']}")
    s = _clamp(s)
    return s, {**detail, "s": s}


def wps_breakdown(product, cfg=None):
    """Full deterministic WPS with per-metric breakdown (N/A metrics score 0)."""
    cfg = cfg or load_scoring_config()
    places = cfg.get("output", {}).get("rounding", 2)
    metrics, na, total = {}, [], 0.0
    for name, m in cfg["metrics"].items():
        comps, frac, missing = {}, 0.0, []
        for cname, c in m["components"].items():
            s, d = component_value(product, c)
            comps[cname] = d
            if s is None:
                missing.append(cname)
            else:
                frac += s * c["weight"]
        if missing:                                   # all_components_required
            metrics[name] = {"points": NA, "max": m["points"], "missing_components": missing, "components": comps}
            na.append(name)
        else:
            cap = m.get("trend_cap")                  # wps-v1.1: e.g. full growth score but DECLINING daily sales
            capped = None
            if cap and product.get(cap["input"]) in cap["when"] and frac > cap["max_fraction"]:
                capped = {"input": cap["input"], "value": product.get(cap["input"]),
                          "uncapped_points": _round(m["points"] * frac, places), "max_fraction": cap["max_fraction"]}
                frac = cap["max_fraction"]
            pts = m["points"] * frac
            total += pts
            metrics[name] = {"points": _round(pts, places), "max": m["points"], "components": comps}
            if capped:
                metrics[name]["trend_cap_applied"] = capped
    return {"score": _round(total, places), "complete": not na, "metrics": metrics,
            "na_metrics": na, "version": cfg.get("version")}


def wps_score(product, cfg=None):
    """WPS only when every metric is computable; otherwise N/A with status 'incomplete'.

    (Discovery uses this: a partial WPS is never presented as a final score.)
    """
    b = wps_breakdown(product, cfg)
    if b["complete"]:
        return {"score": b["score"], "status": "complete", "version": b["version"], "breakdown": b}
    return {"score": NA, "status": "incomplete", "version": b["version"],
            "partial_score": b["score"], "na_metrics": b["na_metrics"], "breakdown": b}


def verdict(wps_value, conf_value, scoring_cfg):
    if conf_value < scoring_cfg["confidence"]["min_for_verdict"]:
        return "insufficient_data"
    for label, t in sorted(scoring_cfg["output"]["verdict_bands"].items(), key=lambda kv: -kv[1]):
        if wps_value >= t:
            return label
    return None


def score_product(product, confidence_cfg=None, scoring_cfg=None):
    scoring_cfg = scoring_cfg or load_scoring_config()
    wps = wps_breakdown(product, scoring_cfg)
    conf = confidence_score(product, confidence_cfg or scoring_cfg["confidence"])
    return {
        "product_id": product.get("product_id"),
        "product_name": product.get("product_name"),
        "wps": wps,
        "confidence": conf,
        "verdict": verdict(wps["score"], conf["score"], scoring_cfg),
        "summary": {
            "WPS": f"{wps['score']}/100" + ("" if wps["complete"] else f" (N/A metrics: {', '.join(wps['na_metrics'])})"),
            "Confidence": f"{conf['score']}/100 ({conf['level']})",
        },
    }


def score_products(products, confidence_cfg=None):
    cfg = load_scoring_config()
    return [score_product(p, confidence_cfg, cfg) for p in products]


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 1
    data = json.load(open(argv[1]))
    products = data if isinstance(data, list) else [data]
    for r in score_products(products):
        print(r["product_name"] or r["product_id"])
        print(f"  WPS: {r['summary']['WPS']}")
        for name, m in r["wps"]["metrics"].items():
            print(f"    {name:20} {m['points']!s:>6} / {m['max']}")
        print(f"  Confidence: {r['summary']['Confidence']}")
        for name, c in r["confidence"]["breakdown"].items():
            print(f"    {name:24} {c['earned']:>6} / {c['max']:<3} {c['status']}")
        for w in r["confidence"]["warnings"]:
            print(f"  WARNING: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
