"""Config schema validation (Step S). Returns a list of human-readable errors (empty = valid).

Checks weights/totals, negative values, min > max, confidence ranges 0-100, limits,
market, cache hours, and that per-stage limits agree with config/runtime.yaml (canonical).
Never modifies any config.
"""
import math
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILES = ["runtime.yaml", "scoring.yaml", "filters.yaml", "categories.yaml", "deep_analysis.yaml",
                "amazon_validation.yaml", "business_viability.yaml", "report.yaml", "history.yaml", "emerging.yaml"]


def load_all(config_dir=ROOT / "config"):
    """Returns (configs, errors). Missing or unparseable files are reported, not raised."""
    cfgs, errors = {}, []
    for name in CONFIG_FILES:
        p = Path(config_dir) / name
        if not p.exists():
            errors.append(f"{name}: missing config file")
            continue
        try:
            cfgs[name] = yaml.safe_load(p.read_text())
        except yaml.YAMLError as e:
            errors.append(f"{name}: invalid YAML ({str(e).splitlines()[0]})")
    return cfgs, errors


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _total(errors, where, items, key, expected=100):
    vals = [c.get(key) for c in items]
    for v in vals:
        if not _num(v) or v < 0:
            errors.append(f"{where}: invalid/negative {key} {v!r}")
            return
    t = sum(vals)
    if abs(t - expected) > 1e-9:
        errors.append(f"{where}: {key} total {t} != {expected}")


def _range01_100(errors, where, v):
    if not _num(v) or not 0 <= v <= 100:
        errors.append(f"{where}: {v!r} outside 0-100")


def _levels(errors, where, levels):
    for k, v in (levels or {}).items():
        _range01_100(errors, f"{where}.{k}", v)


def _pos_int(errors, where, v):
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        errors.append(f"{where}: must be a positive integer, got {v!r}")


def validate(cfgs):
    e = []
    rt = cfgs.get("runtime.yaml") or {}
    r = rt.get("runtime") or {}
    # ---------------------------------------------------------------- runtime
    if r.get("market") not in (rt.get("allowed_markets") or []):
        e.append(f"runtime.yaml: invalid market {r.get('market')!r} (allowed {rt.get('allowed_markets')})")
    for k in ("live_mode", "dry_run", "explicit_live_confirmation"):
        if not isinstance(r.get(k), bool):
            e.append(f"runtime.yaml: runtime.{k} must be true/false")
    lim = rt.get("limits") or {}
    for k in ("discovery_max_products", "deep_analysis_max_products", "amazon_validation_max_products", "bvs_max_products"):
        _pos_int(e, f"runtime.yaml limits.{k}", lim.get(k))
    s = rt.get("safety") or {}
    for k in ("min_balance_reserve", "estimated_credits_per_query"):
        if not _num(s.get(k)) or s.get(k) < 0:
            e.append(f"runtime.yaml: safety.{k} must be >= 0")
    # canonical limits must agree (no silent conflicts)
    pairs = [("discovery_max_products", "filters.yaml", ("discovery_mode", "max_candidates")),
             ("deep_analysis_max_products", "deep_analysis.yaml", ("selection", "deep_analysis_max_products")),
             ("amazon_validation_max_products", "amazon_validation.yaml", ("amazon_validation", "max_products")),
             ("bvs_max_products", "business_viability.yaml", ("business_viability", "eligibility", "max_products"))]
    for key, fname, path in pairs:
        cur = cfgs.get(fname)
        for p in path:
            cur = (cur or {}).get(p)
        if cur is not None and lim.get(key) is not None and cur != lim[key]:
            e.append(f"limit conflict: runtime.yaml limits.{key}={lim[key]} but {fname} {'.'.join(path)}={cur} "
                     "(runtime.yaml is canonical)")

    # ---------------------------------------------------------------- WPS + TikTok confidence
    sc = cfgs.get("scoring.yaml") or {}
    _total(e, "scoring.yaml metrics (WPS)", list((sc.get("metrics") or {}).values()), "points")
    for name, m in (sc.get("metrics") or {}).items():
        ws = [c.get("weight") for c in (m.get("components") or {}).values()]
        if any(not _num(w) or w < 0 for w in ws) or abs(sum(w for w in ws if _num(w)) - 1) > 1e-9:
            e.append(f"scoring.yaml metric {name}: component weights must be >= 0 and sum to 1")
    _total(e, "scoring.yaml confidence", list(((sc.get("confidence") or {}).get("components") or {}).values()), "points")
    _levels(e, "scoring.yaml confidence.levels", (sc.get("confidence") or {}).get("levels"))
    _range01_100(e, "scoring.yaml confidence.min_for_verdict", (sc.get("confidence") or {}).get("min_for_verdict"))

    # ---------------------------------------------------------------- filters
    f = cfgs.get("filters.yaml") or {}
    pr = ((f.get("discovery") or {}).get("price") or {})
    if _num(pr.get("min")) and _num(pr.get("max")) and pr["min"] > pr["max"]:
        e.append(f"filters.yaml: price.min {pr['min']} > price.max {pr['max']}")
    for k in ("gmv_30d", "units_30d"):
        v = ((f.get("discovery") or {}).get(k) or {}).get("min")
        if not _num(v) or v < 0:
            e.append(f"filters.yaml: discovery.{k}.min invalid {v!r}")

    # ---------------------------------------------------------------- deep analysis
    d = cfgs.get("deep_analysis.yaml") or {}
    cp = d.get("credit_protection") or {}
    if not _num(cp.get("cache_ttl_hours")) or cp.get("cache_ttl_hours") <= 0:
        e.append("deep_analysis.yaml: credit_protection.cache_ttl_hours must be > 0")
    th = ((d.get("trend") or {}).get("thresholds") or {})
    if _num(th.get("declining_max")) and _num(th.get("growing_min")) and th["declining_max"] >= th["growing_min"]:
        e.append("deep_analysis.yaml: trend declining_max must be < growing_min")

    # ---------------------------------------------------------------- amazon (AVS + Amazon confidence)
    a = (cfgs.get("amazon_validation.yaml") or {}).get("amazon_validation") or {}
    _total(e, "amazon_validation.yaml avs", list(((a.get("avs") or {}).get("components") or {}).values()), "points")
    _total(e, "amazon_validation.yaml amazon_confidence",
           list(((a.get("amazon_confidence") or {}).get("components") or {}).values()), "points")
    _total(e, "amazon_validation.yaml matching", list(((a.get("matching") or {}).get("components") or {}).values()), "points")
    for k in ("minimum_wps", "minimum_confidence"):
        _range01_100(e, f"amazon_validation.yaml {k}", a.get(k))
    if not _num(a.get("cache_hours")) or a.get("cache_hours") <= 0:
        e.append("amazon_validation.yaml: cache_hours must be > 0")
    g, m = ((a.get("price_alignment") or {}).get("GOOD") or {}), ((a.get("price_alignment") or {}).get("MIXED") or {})
    for name, rng in (("GOOD", g), ("MIXED", m)):
        if _num(rng.get("ratio_min")) and _num(rng.get("ratio_max")) and rng["ratio_min"] > rng["ratio_max"]:
            e.append(f"amazon_validation.yaml: price_alignment.{name} ratio_min > ratio_max")

    # ---------------------------------------------------------------- BVS + BVS confidence
    b = (cfgs.get("business_viability.yaml") or {}).get("business_viability") or {}
    comps = [b.get(k) or {} for k in ("gross_margin_potential", "shipping_viability", "supplier_viability",
                                      "competition_opportunity", "return_risk", "advertising_viability", "compliance_ip")]
    _total(e, "business_viability.yaml BVS components", comps, "points")
    for k in ("gross_margin_potential", "shipping_viability", "supplier_viability", "competition_opportunity",
              "advertising_viability"):
        c = b.get(k) or {}
        parts = list((c.get("parts") or {}).values())
        if parts:
            _total(e, f"business_viability.yaml {k}.parts", parts, "points", c.get("points"))
    _total(e, "business_viability.yaml bvs_confidence",
           list(((b.get("bvs_confidence") or {}).get("components") or {}).values()), "points")
    el = b.get("eligibility") or {}
    for k in ("minimum_wps", "minimum_confidence"):
        _range01_100(e, f"business_viability.yaml eligibility.{k}", el.get(k))

    # ---------------------------------------------------------------- emerging (Momentum)
    em = (cfgs.get("emerging.yaml") or {}).get("emerging_detector") or {}
    _total(e, "emerging.yaml momentum_score", list(((em.get("momentum_score") or {}).get("components") or {}).values()), "points")
    _total(e, "emerging.yaml momentum_confidence",
           list(((em.get("momentum_confidence") or {}).get("components") or {}).values()), "points")
    for k in ("minimum_wps", "minimum_wps_confidence", "strong_emerging_min_momentum_score", "emerging_min_momentum_score"):
        _range01_100(e, f"emerging.yaml {k}", em.get(k))
    if _num(em.get("emerging_min_momentum_score")) and _num(em.get("strong_emerging_min_momentum_score")) and \
            em["emerging_min_momentum_score"] > em["strong_emerging_min_momentum_score"]:
        e.append("emerging.yaml: emerging_min_momentum_score > strong_emerging_min_momentum_score")
    for k in ("maximum_creator_dependency", "maximum_video_dependency"):
        if not _num(em.get(k)) or not 0 < em[k] <= 1:
            e.append(f"emerging.yaml: {k} must be in (0, 1]")
    for k in ("minimum_observations", "minimum_observation_days"):
        _pos_int(e, f"emerging.yaml {k}", em.get(k))

    # ---------------------------------------------------------------- history / report
    h = (cfgs.get("history.yaml") or {}).get("history") or {}
    w = h.get("windows") or {}
    if not (_num(w.get("short_window_days")) and _num(w.get("medium_window_days")) and _num(w.get("long_window_days"))
            and 0 < w["short_window_days"] <= w["medium_window_days"] <= w["long_window_days"]):
        e.append("history.yaml: windows must satisfy 0 < short <= medium <= long")
    vl = ((h.get("volatility") or {}).get("levels") or {})
    if _num(vl.get("LOW_max")) and _num(vl.get("MODERATE_max")) and vl["LOW_max"] > vl["MODERATE_max"]:
        e.append("history.yaml: volatility LOW_max > MODERATE_max")
    rp = (cfgs.get("report.yaml") or {}).get("report") or {}
    _pos_int(e, "report.yaml top.max_products", (rp.get("top") or {}).get("max_products"))
    return e


def validate_dir(config_dir=ROOT / "config"):
    cfgs, errors = load_all(config_dir)
    return errors + (validate(cfgs) if not errors else [])


if __name__ == "__main__":
    errs = validate_dir()
    print("CONFIG VALID" if not errs else "CONFIG INVALID:\n  - " + "\n  - ".join(errs))
