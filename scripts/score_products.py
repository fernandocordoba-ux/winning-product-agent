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
    """Deterministic WPS (wps-v2) with per-metric breakdown.

    Tiers (scoring.yaml data_requirements): a missing CORE metric scores 0 and marks the WPS incomplete;
    a missing SUPPORTING/ENHANCEMENT metric is excluded from the denominator (no WPS penalty) and is
    reported so Confidence can be reduced. WPS = 100 * earned / possible.
    """
    cfg = cfg or load_scoring_config()
    places = cfg.get("output", {}).get("rounding", 2)
    metrics, na, earned, possible = {}, [], 0.0, 0.0
    missing_by_tier = {"CORE": [], "SUPPORTING": [], "ENHANCEMENT": []}
    groups = {}
    for name, m in cfg["metrics"].items():
        tier = m.get("tier", "CORE")
        comps, frac, missing = {}, 0.0, []
        for cname, c in m["components"].items():
            s_, d = component_value(product, c)
            comps[cname] = d
            if s_ is None:
                missing.append(cname)
            else:
                frac += s_ * c["weight"]
        g = groups.setdefault(m.get("group", name), {"points": 0.0, "max": 0.0, "metrics": [], "na": []})
        g["max"] += m["points"]
        g["metrics"].append(name)
        if missing:                                   # all components of a metric are required
            metrics[name] = {"points": NA, "max": m["points"], "tier": tier, "missing_components": missing,
                             "components": comps}
            na.append(name)
            missing_by_tier.setdefault(tier, []).append(name)
            g["na"].append(name)
            if tier == "CORE":
                possible += m["points"]              # CORE: counts as 0 points
        else:
            pts = m["points"] * frac
            earned += pts
            possible += m["points"]
            g["points"] += pts
            metrics[name] = {"points": _round(pts, places), "max": m["points"], "tier": tier, "components": comps}
    score = _round(100 * earned / possible, places) if possible > 0 else 0.0
    for g in groups.values():
        g["points"] = _round(g["points"], places)
    return {"score": score, "complete": not missing_by_tier["CORE"], "metrics": metrics, "na_metrics": na,
            "missing_by_tier": missing_by_tier, "groups": groups,
            "points_earned": _round(earned, places), "points_possible": _round(possible, places),
            "version": cfg.get("version")}


def confidence_adjustments(product, wps, cfg):
    """Confidence reductions for data the WPS could not use (never changes WPS)."""
    dr = cfg.get("data_requirements") or {}
    adj = dr.get("confidence_adjustments") or {}
    out = []
    for name in wps["missing_by_tier"].get("SUPPORTING", []) + wps["missing_by_tier"].get("ENHANCEMENT", []):
        out.append({"reason": f"SUPPORTING/ENHANCEMENT metric not available: {name}",
                    "points": -adj.get("missing_supporting_metric", 0)})
    for field in dr.get("enhancement_fields") or []:
        v = product.get(field)
        if v is None or (isinstance(v, list) and not [x for x in v if x is not None]):
            out.append({"reason": f"ENHANCEMENT field missing: {field}", "points": -adj.get("missing_enhancement_field", 0)})
    return [a for a in out if a["points"]]


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
    adj = confidence_adjustments(product, wps, scoring_cfg)
    if adj:                                            # wps-v2: missing supporting data lowers Confidence
        places = scoring_cfg.get("output", {}).get("rounding", 2)
        conf = {**conf, "score_before_adjustments": conf["score"], "adjustments": adj,
                "score": _round(max(0.0, conf["score"] + sum(a["points"] for a in adj)), places)}
        conf["level"] = next(lv for lv, t in sorted(scoring_cfg["confidence"]["levels"].items(), key=lambda kv: -kv[1])
                             if conf["score"] >= t)
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
