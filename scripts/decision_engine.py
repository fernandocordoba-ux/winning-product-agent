"""Unified Decision Engine — Step Z (decision_rules_version Z-v1).

Combines every research layer WITHOUT a weighted mega-score:

    evidence (per product, with source paths)
      -> hard gates            (config/decision.yaml: hard_gates)
      -> 6 dimensions          STRONG / ACCEPTABLE / WEAK / UNKNOWN (deterministic YAML thresholds)
      -> decision state        READY_FOR_PRODUCT_VALIDATION | PROMISING_NEEDS_VALIDATION | WATCHLIST |
                               REJECT | INSUFFICIENT_DATA
      -> Decision Confidence   (confidence in the DECISION; never uses product performance values)
      -> shortlist             (max 3, READY only, deterministic tie-break, never filled)

Rules:
  * No existing formula (WPS / AVS / BVS / supplier / competitor / creative) is changed here.
  * Missing values stay None / "N/A"; nothing is estimated. A missing optional layer is UNKNOWN, never a reject.
  * No query is executed, nothing is bought or launched. READY means "ready for MANUAL product validation".
"""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

NA = "N/A"
READY, PROMISING, WATCH, REJECT, INSUFFICIENT = ("READY_FOR_PRODUCT_VALIDATION", "PROMISING_NEEDS_VALIDATION",
                                                  "WATCHLIST", "REJECT", "INSUFFICIENT_DATA")
STRONG, ACCEPTABLE, WEAK, UNKNOWN = "STRONG", "ACCEPTABLE", "WEAK", "UNKNOWN"
DIMENSIONS = ["market_momentum", "cross_platform_demand", "commercial_viability", "competitive_environment",
              "creative_opportunity", "evidence_quality"]
DIM_LABEL = {"market_momentum": "Market Momentum", "cross_platform_demand": "Cross-Platform Demand",
             "commercial_viability": "Commercial Viability", "competitive_environment": "Competitive Environment",
             "creative_opportunity": "Creative Opportunity", "evidence_quality": "Evidence Quality"}
RANK = {STRONG: 0, ACCEPTABLE: 1, UNKNOWN: 2, WEAK: 3}
# layer -> (evidence key of its confidence, minimum_confidence key)
LAYERS = {"wps": "wps_confidence", "momentum": "momentum_confidence", "amazon": "amazon_confidence",
          "bvs": "bvs_confidence", "supplier": "supplier_confidence", "competitor": "competitor_confidence",
          "creative": "creative_confidence"}
PROVENANCE_KEYS = ["gmv_30d", "gmv_prev_30d", "units_30d", "growth_30d", "daily_gmv", "creator_count",
                   "video_count", "competition_count", "price_avg"]
CHECK_STATUSES = ("COMPLETE", "PENDING", "NOT_APPLICABLE")
DISCLAIMER = ("READY_FOR_PRODUCT_VALIDATION means the product passed the data rules and is ready for MANUAL "
              "validation (sample, quotes, margin, policy). It is not a prediction of sales or profit.")


def load_cfg(path=None):
    import config_resolver as _CR
    path = path or _CR.path("decision.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def num(v):
    if v is None or v == NA or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def file_hash(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return NA


def _codes(flags):
    out = set()
    for f in flags or []:
        out.add(f["flag"] if isinstance(f, dict) else str(f))
    return out


# ============================================================================ Stage 1 — evidence
def build_evidence(p, competitor=None, creative=None, expected_env=None):
    """One report product dict (+ optional competitor / creative analyses) -> flat evidence with source paths.

    Every value is copied, never computed from a guess. Missing = None (rendered N/A)."""
    d, a, b = p.get("deep") or {}, p.get("amazon") or {}, p.get("bvs_record") or {}
    em = p.get("emerging") or {}
    econ = b.get("economics") or {}
    layer = b.get("supplier_layer") or {}
    sel = layer.get("selected") or {}
    comp = competitor or {}
    cre = creative or {}
    cm, vm = d.get("creator_metrics") or {}, d.get("video_metrics") or {}
    ev = {}

    def put(key, value, source):
        ev[key] = {"value": value, "source": source if value is not None else None}

    put("wps", num(p.get("wps")), "deep_analysis.wps")
    put("wps_confidence", num(p.get("confidence")), "deep_analysis.confidence")
    put("momentum_score", num(em.get("momentum_score")), "emerging.momentum_score")
    put("momentum_confidence", num(em.get("momentum_confidence")), "emerging.momentum_confidence")
    put("emerging_status", em.get("emerging_status"), "emerging.emerging_status")
    put("trend", p.get("trend"), "deep_analysis.trend_metrics.label")
    put("growth_30d_pct", num(p.get("growth_30d")), "deep_analysis.growth.growth_30d_pct")
    put("creator_count", num(cm.get("total")), "deep_analysis.creator_metrics.total")
    put("video_count", num(vm.get("total")), "deep_analysis.video_metrics.total")
    put("avs", num(p.get("avs")), "amazon_validation.avs")
    put("amazon_confidence", num(p.get("amazon_confidence")), "amazon_validation.amazon_confidence")
    put("amazon_match_status", a.get("amazon_match_status"), "amazon_validation.amazon_match_status")
    put("bvs", num(p.get("bvs")), "business_viability.bvs")
    put("bvs_confidence", num(p.get("bvs_confidence")), "business_viability.bvs_confidence")
    put("product_cost", num(econ.get("product_cost")), "business_viability.economics.product_cost")
    put("supplier_shipping_cost", num(econ.get("supplier_shipping_cost")),
        "business_viability.economics.supplier_shipping_cost")
    put("landed_cost", num(econ.get("landed_cost")), "business_viability.economics.landed_cost")
    put("gross_margin_pct", num(econ.get("gross_margin_percent")), "business_viability.economics.gross_margin_percent")
    put("contribution_margin_pct", num(econ.get("contribution_margin_percent")),
        "business_viability.economics.contribution_margin_percent")
    put("selected_supplier_offer_id", layer.get("selected_supplier_offer_id"),
        "business_viability.supplier_layer.selected_supplier_offer_id")
    put("supplier_confidence", num(sel.get("supplier_confidence")), "supplier_layer.selected.supplier_confidence")
    put("supplier_quality", num(sel.get("supplier_quality")), "supplier_layer.selected.supplier_quality")
    put("supplier_delivery_days", num(sel.get("effective_delivery_days")),
        "supplier_layer.selected.effective_delivery_days")
    put("supplier_offer_count", num(layer.get("offer_count")), "supplier_layer.offer_count")
    put("supplier_qualified_offers", num(layer.get("qualified_offers")), "supplier_layer.qualified_offers")
    put("competitor_saturation", num((comp.get("saturation") or {}).get("score")), "competitor.saturation.score")
    put("competitor_opportunity", num((comp.get("opportunity") or {}).get("score")), "competitor.opportunity.score")
    put("competitor_confidence", num((comp.get("confidence") or {}).get("score")), "competitor.confidence.score")
    put("direct_competitors", num(comp.get("direct_competitors")), "competitor.direct_competitors")
    put("creative_saturation", num((cre.get("saturation") or {}).get("score")), "creative.saturation.score")
    put("creative_opportunity", num((cre.get("opportunity") or {}).get("score")), "creative.opportunity.score")
    put("creative_confidence", num((cre.get("confidence") or {}).get("score")), "creative.confidence.score")
    put("qualified_creatives", num(cre.get("qualified_creatives")), "creative.qualified_creatives")
    hist = p.get("history") or []
    put("history_observations", float(len(hist)) if hist else None, f"history ({p.get('history_source')})")

    prov = d.get("provenance") or {}
    if prov:
        have = [k for k in PROVENANCE_KEYS if (prov.get(k) or {}).get("availability") in ("AVAILABLE", "DERIVED")]
        put("provenance_completeness", round(len(have) / len(PROVENANCE_KEYS), 4), "deep_analysis.provenance")
    else:
        put("provenance_completeness", None, None)

    flags = {}
    for code in p.get("flags") or []:
        flags.setdefault(code, {"sources": [], "severe": False})["sources"].append("report.flags")
    for f in b.get("commercial_red_flags") or []:
        e = flags.setdefault(f["flag"], {"sources": [], "severe": False})
        e["sources"].append("business_viability.commercial_red_flags")
        e["severe"] = e["severe"] or bool(f.get("severe"))
    for f in layer.get("supplier_flags") or []:
        flags.setdefault(f["flag"], {"sources": [], "severe": False})["sources"].append("supplier_layer.supplier_flags")
    for f in comp.get("red_flags") or []:
        flags.setdefault(f["flag"], {"sources": [], "severe": False})["sources"].append("competitor.red_flags")
    for f in cre.get("red_flags") or []:
        flags.setdefault(f["flag"], {"sources": [], "severe": False})["sources"].append("creative.red_flags")
    for f in (d.get("red_flags") or []):
        if isinstance(f, dict) and f.get("severe"):
            flags.setdefault(f["flag"], {"sources": [], "severe": False})["severe"] = True

    ev_trust = trust_records(p, d, a, b, em, layer, sel, comp, cre, ev)

    env = p.get("data_environment") or d.get("data_environment")
    integrity = list(p.get("integrity_errors") or [])
    if expected_env and env is not None and env != expected_env:
        integrity.append(f"data_environment {env} != {expected_env}")
    if expected_env and d and d.get("data_environment") not in (None, expected_env):
        integrity.append(f"deep record data_environment {d.get('data_environment')} != {expected_env}")
    offers = layer.get("offers") or []
    return {
        "product_id": str(p.get("product_id") or p.get("id")), "name": p.get("name"),
        "category": p.get("category"), "url": p.get("url"),
        "values": ev, "flags": flags,
        "layers_present": {"deep_analysis": bool(d), "history": bool(hist) and em.get("momentum_score") not in (None, NA),
                           "amazon": bool(a), "bvs": bool(b), "supplier": bool(layer.get("offers")),
                           "competitor": bool(comp), "creative": bool(cre)},
        "supplier_all_unreliable": bool(offers) and all(o.get("match_class") == "UNRELIABLE" for o in offers),
        "identity_conflict": bool(p.get("identity_conflict")),
        "integrity_errors": integrity, "data_environment": env, "trust": ev_trust,
    }


# ============================================================================ Stage 18 (AA) — data trust
TRUST_FIELDS = ("value", "source", "source_field", "scope", "observed_at", "provenance")


def _t(value, source, source_field, scope, observed_at, provenance):
    return {"value": value, "source": source, "source_field": source_field, "scope": scope,
            "observed_at": observed_at, "provenance": provenance}


def trust_records(p, d, a, b, em, layer, sel, comp, cre, ev):
    """Per metric: where it came from. A field is None when it cannot be traced (never filled in)."""
    val = lambda k: ev[k]["value"]  # noqa: E731
    dsrc, dts = (d.get("source") or {}).get("raw_file"), d.get("observation_timestamp")
    prov = d.get("provenance") or {}
    wprov = d.get("wps_input_provenance") or {}

    def pv(key):
        e = prov.get(key) or {}
        return e if e.get("availability") in ("AVAILABLE", "DERIVED") else None
    t = {}
    for k in ("wps", "wps_confidence"):
        t[k] = _t(val(k), dsrc, f"calculated:{'wps' if k == 'wps' else 'confidence'} (config/scoring.yaml)", "PRODUCT",
                  dts, ("wps_input_provenance" if wprov and prov else None))
    g = pv("growth_30d")
    t["growth_30d_pct"] = _t(val("growth_30d_pct"), dsrc, (g or {}).get("source_field"), (g or {}).get("scope"),
                             (g or {}).get("provider_observation_timestamp") or (g or {}).get("retrieved_at") or dts,
                             "deep_analysis.provenance.growth_30d" if g else None)
    dg = pv("daily_gmv")
    t["trend"] = _t(val("trend"), dsrc, "calculated:trend_metrics from daily_gmv" if dg else None,
                    (dg or {}).get("scope"), dts, "deep_analysis.provenance.daily_gmv" if dg else None)
    for k, pk in (("creator_count", "creator_count"), ("video_count", "video_count")):
        e = pv(pk)
        t[k] = _t(val(k), dsrc, (e or {}).get("source_field"), (e or {}).get("scope"), dts,
                  f"deep_analysis.provenance.{pk}" if e else None)
    esrc = (em.get("sources") or [None])[0]
    for k in ("momentum_score", "momentum_confidence"):
        t[k] = _t(val(k), esrc, f"calculated:{k} (config/emerging.yaml)", "PRODUCT", em.get("history_end"),
                  "history_store" if em.get("sources") else None)
    asrc = (a.get("source") or {}).get("raw_file")
    for k in ("avs", "amazon_confidence"):
        t[k] = _t(val(k), asrc, f"calculated:{k} (config/amazon_validation.yaml)", "PRODUCT (Amazon US match)",
                  a.get("observation_timestamp"), "amazon_validation.source" if asrc else None)
    bsrc = (b.get("source") or {}).get("deep_analysis_file") or b.get("_file")
    bts = b.get("observation_timestamp")
    supplier_ok = bool(sel.get("offer_id")) and bool(sel.get("supplier_source") or sel.get("supplier_url"))
    for k in ("bvs", "bvs_confidence", "gross_margin_pct", "contribution_margin_pct"):
        t[k] = _t(val(k), bsrc, f"calculated:{k} (config/business_viability.yaml)", "PRODUCT", bts,
                  "business_viability.source" if bsrc else None)
    for k, f in (("product_cost", "product_cost"), ("supplier_shipping_cost", "shipping_cost"),
                 ("supplier_confidence", "supplier_confidence"), ("supplier_quality", "supplier_quality")):
        t[k] = _t(val(k), sel.get("supplier_url") or sel.get("supplier_source"), f"supplier_offer.{f}",
                  "SUPPLIER_OFFER", sel.get("observed_at"), f"supplier_offer:{sel.get('offer_id')}" if supplier_ok else None)
    for pre, x in (("competitor", comp), ("creative", cre)):
        src = x.get("source_files") or x.get("source")
        src = src[0] if isinstance(src, list) and src else (src if isinstance(src, str) else None)
        for part in ("saturation", "opportunity", "confidence"):
            k = f"{pre}_{part}"
            t[k] = _t(val(k), src, f"calculated:{part} (config/{pre}s.yaml)", "PRODUCT (qualified matches)",
                      x.get("observed_at"), f"{pre}_layer" if x.get("provenance_complete") else None)
    return t


def untraceable(ev, keys):
    """Metrics among keys that are USED (value present) but miss any trust field."""
    out = {}
    for k in keys:
        r = (ev.get("trust") or {}).get(k)
        if r is None or r.get("value") is None:
            continue
        miss = [f for f in TRUST_FIELDS if r.get(f) in (None, "", [])]
        if miss:
            out[k] = miss
    return out


DIM_METRICS = {
    "market_momentum": ["wps", "wps_confidence", "momentum_score", "momentum_confidence", "trend"],
    "cross_platform_demand": ["avs", "amazon_confidence"],
    "commercial_viability": ["bvs", "bvs_confidence", "gross_margin_pct", "product_cost", "supplier_shipping_cost",
                             "supplier_confidence"],
    "competitive_environment": ["competitor_saturation", "competitor_opportunity", "competitor_confidence"],
    "creative_opportunity": ["creative_saturation", "creative_opportunity", "creative_confidence"],
}
GATE_METRICS = {"NEGATIVE_CONTRIBUTION_MARGIN": ["contribution_margin_pct"],
                "DEMAND_DECLINING": ["trend", "growth_30d_pct"],
                "CREATOR_DEPENDENCY_WEAK_BROADER": ["creator_count"], "VIDEO_DEPENDENCY_WEAK_BROADER": ["video_count"]}


def trust_check(ev, dims, gates):
    """Every metric a decision depends on must be traceable (Stage 18)."""
    used = {}
    for k, keys in DIM_METRICS.items():
        if dims[k]["status"] != UNKNOWN:
            ks = [x for x in keys if not (x.startswith("momentum") and "momentum_ignored" in dims[k]["inputs"])]
            bad = untraceable(ev, ks)
            if bad:
                used[k] = bad
    for g in gates:
        if g["status"] == "FIRED" and g["gate"] in GATE_METRICS:
            bad = untraceable(ev, GATE_METRICS[g["gate"]])
            if bad:
                used[f"gate:{g['gate']}"] = bad
    return {"traceable": not used, "untraceable": used,
            "checked_fields": list(TRUST_FIELDS)}


def v(ev, key):
    return ev["values"].get(key, {}).get("value")


def supplier_economics_known(ev):
    return v(ev, "product_cost") is not None and v(ev, "supplier_shipping_cost") is not None


# ============================================================================ Stages 4-5 — dimensions
def _dim(status, reason, inputs, rule):
    return {"status": status, "reason": reason, "inputs": inputs, "rule": rule}


def _cap(status, cap):
    return cap if RANK[status] < RANK[cap] else status


def dim_market_momentum(ev, cfg):
    c, mins = cfg["dimensions"]["market_momentum"], cfg["minimum_confidence"]
    wps, conf = v(ev, "wps"), v(ev, "wps_confidence")
    mom, mconf, trend = v(ev, "momentum_score"), v(ev, "momentum_confidence"), v(ev, "trend")
    inputs = {"wps": wps, "wps_confidence": conf, "momentum_score": mom, "momentum_confidence": mconf, "trend": trend}
    rule = (f"STRONG: WPS >= {c['strong']['wps_min']} and (Momentum >= {c['strong']['momentum_min_if_known']} if "
            f"known) and trend != DECLINING; ACCEPTABLE: WPS >= {c['acceptable']['wps_min']}; WPS Confidence >= "
            f"{mins['wps']}; Momentum used only if Momentum Confidence >= {mins['momentum']}")
    if wps is None:
        return _dim(UNKNOWN, "WPS not available", inputs, rule)
    if conf is None or conf < mins["wps"]:
        return _dim(UNKNOWN, f"WPS Confidence {conf if conf is not None else NA} below minimum {mins['wps']}", inputs, rule)
    if mom is not None and (mconf is None or mconf < mins["momentum"]):
        inputs["momentum_ignored"] = f"Momentum Confidence {mconf if mconf is not None else NA} < {mins['momentum']}"
        mom = None
    if wps >= c["strong"]["wps_min"] and (mom is None or mom >= c["strong"]["momentum_min_if_known"]):
        st, why = STRONG, f"WPS {wps} >= {c['strong']['wps_min']}"
    elif wps >= c["acceptable"]["wps_min"]:
        st, why = ACCEPTABLE, f"WPS {wps} >= {c['acceptable']['wps_min']}"
    else:
        return _dim(WEAK, f"WPS {wps} < {c['acceptable']['wps_min']}", inputs, rule)
    if trend == "DECLINING" and st == STRONG:
        st, why = _cap(st, c["declining_trend_caps_at"]), why + "; capped: daily trend DECLINING"
    if mom is not None and mom < c["momentum_weak_below"] and st == STRONG:
        st, why = ACCEPTABLE, why + f"; capped: Momentum {mom} < {c['momentum_weak_below']}"
    return _dim(st, why, inputs, rule)


def dim_cross_platform(ev, cfg):
    c, mins = cfg["dimensions"]["cross_platform_demand"], cfg["minimum_confidence"]
    avs, conf, match = v(ev, "avs"), v(ev, "amazon_confidence"), v(ev, "amazon_match_status")
    inputs = {"avs": avs, "amazon_confidence": conf, "amazon_match_status": match}
    rule = (f"STRONG: AVS >= {c['strong']['avs_min']} and Amazon Confidence >= {c['strong']['confidence_min']}; "
            f"ACCEPTABLE: AVS >= {c['acceptable']['avs_min']}; Amazon Confidence >= {mins['amazon']}; "
            f"no reliable Amazon match -> UNKNOWN")
    if avs is None or match == "NO_RELIABLE_MATCH":
        return _dim(UNKNOWN, "no Amazon data / no reliable Amazon match (optional layer)", inputs, rule)
    if conf is None or conf < mins["amazon"]:
        return _dim(UNKNOWN, f"Amazon Confidence {conf if conf is not None else NA} below minimum {mins['amazon']}",
                    inputs, rule)
    if avs >= c["strong"]["avs_min"] and conf >= c["strong"]["confidence_min"]:
        return _dim(STRONG, f"AVS {avs} >= {c['strong']['avs_min']}, Amazon Confidence {conf}", inputs, rule)
    if avs >= c["acceptable"]["avs_min"]:
        return _dim(ACCEPTABLE, f"AVS {avs} >= {c['acceptable']['avs_min']}", inputs, rule)
    return _dim(WEAK, f"AVS {avs} < {c['acceptable']['avs_min']}", inputs, rule)


def dim_commercial(ev, cfg):
    c, mins = cfg["dimensions"]["commercial_viability"], cfg["minimum_confidence"]
    bvs, conf, gm = v(ev, "bvs"), v(ev, "bvs_confidence"), v(ev, "gross_margin_pct")
    sconf, cm = v(ev, "supplier_confidence"), v(ev, "contribution_margin_pct")
    inputs = {"bvs": bvs, "bvs_confidence": conf, "gross_margin_pct": gm, "contribution_margin_pct": cm,
              "product_cost": v(ev, "product_cost"), "supplier_shipping_cost": v(ev, "supplier_shipping_cost"),
              "supplier_confidence": sconf}
    rule = (f"STRONG: BVS >= {c['strong']['bvs_min']} and gross margin >= {c['strong']['gross_margin_min']}%; "
            f"ACCEPTABLE: BVS >= {c['acceptable']['bvs_min']} and gross margin >= "
            f"{c['acceptable']['gross_margin_min']}%; requires supplier product_cost + shipping; BVS Confidence >= "
            f"{mins['bvs']}; Supplier Confidence >= {mins['supplier']} (if a supplier offer is selected)")
    if c["require_supplier_economics"] and not supplier_economics_known(ev):
        return _dim(UNKNOWN, "supplier economics missing (product_cost / shipping_cost not provided; never estimated)",
                    inputs, rule)
    if bvs is None:
        return _dim(UNKNOWN, "BVS not available", inputs, rule)
    if conf is None or conf < mins["bvs"]:
        return _dim(UNKNOWN, f"BVS Confidence {conf if conf is not None else NA} below minimum {mins['bvs']}", inputs, rule)
    if sconf is not None and sconf < mins["supplier"]:
        return _dim(UNKNOWN, f"Supplier Confidence {sconf} below minimum {mins['supplier']}", inputs, rule)
    if cm is not None and cm < 0:
        return _dim(WEAK, f"contribution margin {cm}% < 0", inputs, rule)
    if gm is not None and bvs >= c["strong"]["bvs_min"] and gm >= c["strong"]["gross_margin_min"]:
        return _dim(STRONG, f"BVS {bvs} >= {c['strong']['bvs_min']}, gross margin {gm}%", inputs, rule)
    if gm is not None and bvs >= c["acceptable"]["bvs_min"] and gm >= c["acceptable"]["gross_margin_min"]:
        return _dim(ACCEPTABLE, f"BVS {bvs} >= {c['acceptable']['bvs_min']}, gross margin {gm}%", inputs, rule)
    return _dim(WEAK, f"BVS {bvs} / gross margin {gm if gm is not None else NA}% below ACCEPTABLE thresholds",
                inputs, rule)


def _sat_opp_dim(name, opp, sat, conf, min_conf, c, label, extra_cap=None):
    inputs = {f"{label}_opportunity": opp, f"{label}_saturation": sat, f"{label}_confidence": conf}
    rule = (f"STRONG: opportunity >= {c['strong']['opportunity_min']} and saturation <= {c['strong']['saturation_max']}; "
            f"ACCEPTABLE: opportunity >= {c['acceptable']['opportunity_min']} and saturation <= "
            f"{c['acceptable']['saturation_max']}; {label} confidence >= {min_conf}")
    if conf is None:
        return _dim(UNKNOWN, f"no {label} research", inputs, rule)
    if conf < min_conf:
        return _dim(UNKNOWN, f"{label} confidence {conf} below minimum {min_conf}", inputs, rule)
    if opp is None or sat is None:
        return _dim(UNKNOWN, f"{label} opportunity / saturation not computable (insufficient components)", inputs, rule)
    if opp >= c["strong"]["opportunity_min"] and sat <= c["strong"]["saturation_max"]:
        st, why = STRONG, f"opportunity {opp} >= {c['strong']['opportunity_min']}, saturation {sat} <= {c['strong']['saturation_max']}"
    elif opp >= c["acceptable"]["opportunity_min"] and sat <= c["acceptable"]["saturation_max"]:
        st, why = ACCEPTABLE, f"opportunity {opp}, saturation {sat} within ACCEPTABLE thresholds"
    else:
        return _dim(WEAK, f"opportunity {opp} / saturation {sat} outside ACCEPTABLE thresholds", inputs, rule)
    if extra_cap and st == STRONG:
        st, why = _cap(st, extra_cap[0]), why + f"; capped: {extra_cap[1]}"
    return _dim(st, why, inputs, rule)


def dim_competitive(ev, cfg):
    c = cfg["dimensions"]["competitive_environment"]
    cap = (c["compression_caps_at"], "PRICE_COMPRESSION") if "PRICE_COMPRESSION" in ev["flags"] else None
    return _sat_opp_dim("competitive_environment", v(ev, "competitor_opportunity"), v(ev, "competitor_saturation"),
                        v(ev, "competitor_confidence"), cfg["minimum_confidence"]["competitor"], c, "competitor", cap)


def dim_creative(ev, cfg):
    return _sat_opp_dim("creative_opportunity", v(ev, "creative_opportunity"), v(ev, "creative_saturation"),
                        v(ev, "creative_confidence"), cfg["minimum_confidence"]["creative"],
                        cfg["dimensions"]["creative_opportunity"], "creative")


def layer_confidences(ev):
    return {layer: v(ev, key) for layer, key in LAYERS.items()}


def dim_evidence(ev, cfg):
    c, mins = cfg["dimensions"]["evidence_quality"], cfg["minimum_confidence"]
    confs = layer_confidences(ev)
    if c.get("layers"):                       # owner-approved: only these layers judge evidence quality
        confs = {k: x for k, x in confs.items() if k in c["layers"]}
    known = {k: x for k, x in confs.items() if x is not None}
    passing = [k for k, x in known.items() if x >= mins[k]]
    inputs = {"layer_confidences": confs, "minimums": mins, "passing": passing}
    rule = (f"UNKNOWN if {c['core_layer']} confidence missing; WEAK if it is below its minimum or fewer than "
            f"{int(c['acceptable_min_passing_share'] * 100)}% of known layer confidences meet their minimum; STRONG if "
            f">= {c['strong_min_known_layers']} layers known and all >= minimum + {c['strong_margin']}")
    core = known.get(c["core_layer"])
    if core is None:
        return _dim(UNKNOWN, f"{c['core_layer']} confidence missing", inputs, rule)
    if core < mins[c["core_layer"]]:
        return _dim(WEAK, f"{c['core_layer']} confidence {core} < {mins[c['core_layer']]}", inputs, rule)
    if len(passing) < c["acceptable_min_passing_share"] * len(known):
        return _dim(WEAK, f"only {len(passing)}/{len(known)} known layer confidences meet their minimum", inputs, rule)
    if len(known) >= c["strong_min_known_layers"] and all(x >= mins[k] + c["strong_margin"] for k, x in known.items()):
        return _dim(STRONG, f"{len(known)} layers known, all >= minimum + {c['strong_margin']}", inputs, rule)
    return _dim(ACCEPTABLE, f"{len(passing)}/{len(known)} known layer confidences meet their minimum", inputs, rule)


def dimensions(ev, cfg):
    return {"market_momentum": dim_market_momentum(ev, cfg), "cross_platform_demand": dim_cross_platform(ev, cfg),
            "commercial_viability": dim_commercial(ev, cfg), "competitive_environment": dim_competitive(ev, cfg),
            "creative_opportunity": dim_creative(ev, cfg), "evidence_quality": dim_evidence(ev, cfg)}


# ============================================================================ Stage 2 — hard gates
def hard_gates(ev, dims, cfg, checklist=None):
    """Every gate is reported: FIRED / PASSED / NOT_EVALUATED (inputs missing) / RESOLVED (manual check COMPLETE)."""
    g, out = cfg["hard_gates"], []
    checklist = checklist or {}
    flags = ev["flags"]

    def add(name, status, evidence, action=None):
        out.append({"gate": name, "status": status, "action": action or g[name]["action"], "evidence": evidence})

    cm = v(ev, "contribution_margin_pct")
    if cm is None:
        add("NEGATIVE_CONTRIBUTION_MARGIN", "NOT_EVALUATED", {"contribution_margin_pct": NA})
    else:
        add("NEGATIVE_CONTRIBUTION_MARGIN", "FIRED" if cm < g["NEGATIVE_CONTRIBUTION_MARGIN"]["below"] else "PASSED",
            {"contribution_margin_pct": cm, "source": ev["values"]["contribution_margin_pct"]["source"]})
    add("UNRELIABLE_PRODUCT_MATCH", "FIRED" if ev["identity_conflict"] else "PASSED",
        {"identity_conflict": ev["identity_conflict"]})
    for name in ("REGULATED_PRODUCT", "REGULATED_REVIEW_REQUIRED", "IP_REVIEW_REQUIRED", "COUNTERFEIT_REVIEW_REQUIRED"):
        rule = g[name]
        hit = [f for f in rule["flags"] if f in flags]
        if not hit:
            add(name, "PASSED", {"flags": []})
            continue
        evidence = {"flags": hit, "sources": sorted({s for f in hit for s in flags[f]["sources"]})}
        if rule.get("resolved_by") and checklist.get(rule["resolved_by"], {}).get("status") == "COMPLETE":
            add(name, "RESOLVED", {**evidence, "resolved_by": rule["resolved_by"]})
            continue
        severe = any(flags[f]["severe"] for f in hit)
        action = rule.get("severe_action", rule["action"]) if severe else rule["action"]
        add(name, "FIRED", {**evidence, "severe": severe}, action)
    mm = dims["market_momentum"]["status"]
    hit = [f for f in g["EXTREME_COMPETITION_WEAK_DEMAND"]["flags"] if f in flags]
    add("EXTREME_COMPETITION_WEAK_DEMAND", "FIRED" if hit and mm == WEAK else "PASSED",
        {"flags": hit, "market_momentum": mm})
    trend, growth = v(ev, "trend"), v(ev, "growth_30d_pct")
    r = g["DEMAND_DECLINING"]
    if trend is None and growth is None:
        add("DEMAND_DECLINING", "NOT_EVALUATED", {"trend": NA, "growth_30d_pct": NA})
    else:
        fired = trend == r["trend"] and growth is not None and growth <= r["growth_at_most"]
        add("DEMAND_DECLINING", "FIRED" if fired else "PASSED", {"trend": trend, "growth_30d_pct": growth})
    xp = dims["cross_platform_demand"]["status"]
    for name, flag_key, count_key, max_key in (("CREATOR_DEPENDENCY_WEAK_BROADER", "flags", "creator_count", "max_creators"),
                                               ("VIDEO_DEPENDENCY_WEAK_BROADER", "flags", "video_count", "max_videos")):
        r = g[name]
        hit = [f for f in r[flag_key] if f in flags]
        n = v(ev, count_key)
        narrow = bool(hit) and n is not None and n <= r[max_key]
        weak_statuses = r.get("broader_weak_statuses", [WEAK, UNKNOWN])   # Z-v1: UNKNOWN counted as weak
        if narrow and xp in weak_statuses:
            add(name, "FIRED", {"flags": hit, count_key: n, "cross_platform_demand": xp})
        elif narrow and xp == UNKNOWN and r.get("when_broader_unknown"):
            add(name, "FIRED", {"flags": hit, count_key: n, "cross_platform_demand": xp,
                                "note": "broader evidence missing (not negative)"}, r["when_broader_unknown"])
        else:
            add(name, "PASSED", {"flags": hit, count_key: n, "cross_platform_demand": xp})
    sup = ev["flags"].get("VERY_SLOW_DELIVERY")
    sup_hit = bool(sup) and "supplier_layer.supplier_flags" in sup["sources"] or \
        (bool(sup) and "business_viability.commercial_red_flags" in sup["sources"])
    add("VERY_SLOW_DELIVERY", "FIRED" if sup_hit and v(ev, "selected_supplier_offer_id") else
        ("NOT_EVALUATED" if not ev["layers_present"]["supplier"] else "PASSED"),
        {"selected_supplier_offer_id": v(ev, "selected_supplier_offer_id"),
         "effective_delivery_days": v(ev, "supplier_delivery_days")})
    add("SUPPLIER_MATCH_UNRELIABLE", "FIRED" if ev["supplier_all_unreliable"] else
        ("NOT_EVALUATED" if not ev["layers_present"]["supplier"] else "PASSED"),
        {"offer_count": v(ev, "supplier_offer_count"), "all_offers_unreliable": ev["supplier_all_unreliable"]})
    add("CRITICAL_DATA_INTEGRITY_ERROR", "FIRED" if ev["integrity_errors"] else "PASSED",
        {"errors": ev["integrity_errors"]})
    for x in out:
        x["rule"] = {k: val for k, val in g[x["gate"]].items()}
    return out


# ============================================================================ Stage 13 — Decision Confidence
def decision_confidence(ev, dims, cfg):
    """Confidence in the DECISION (0-100): coverage and quality of evidence only.

    Uses: how many dimensions are known, layer confidences, provenance completeness, reliable sources,
    historical depth. Never uses WPS / AVS / BVS / opportunity values, so better product performance can
    never raise it."""
    c, mins = cfg["decision_confidence"], cfg["minimum_confidence"]
    comp = c["components"]
    known_dims = [k for k in DIMENSIONS if dims[k]["status"] != UNKNOWN]
    confs = layer_confidences(ev)
    known = {k: x for k, x in confs.items() if x is not None}
    prov = v(ev, "provenance_completeness")
    hist = v(ev, "history_observations") or 0
    reliable = [k for k, x in known.items() if x >= mins[k]]
    parts = {
        "core_dimension_coverage": comp["core_dimension_coverage"]["points"] * len(known_dims) / len(DIMENSIONS),
        "layer_confidences": (comp["layer_confidences"]["points"] * (sum(known.values()) / len(known) / 100)
                              * (len(known) / len(LAYERS))) if known else 0.0,
        "provenance_completeness": comp["provenance_completeness"]["points"] * (prov or 0.0),
        "reliable_sources": comp["reliable_sources"]["points"] * min(len(reliable) / c["sources_full_at"], 1.0),
        "historical_depth": comp["historical_depth"]["points"] * min(hist / c["history_full_at"], 1.0),
    }
    score = round(sum(parts.values()), 2)
    return {"score": score, "components": {k: round(x, 2) for k, x in parts.items()},
            "inputs": {"known_dimensions": known_dims, "layer_confidences": confs, "provenance_completeness": prov,
                       "reliable_sources": reliable, "history_observations": hist},
            "formula": "30*known_dims/6 + 25*mean(known layer conf)/100*known_layers/7 + 15*provenance_share "
                       "+ 15*min(reliable_layers/5,1) + 15*min(history_obs/3,1)",
            "note": "confidence in the decision, not a product score; product performance never raises it"}


# ============================================================================ Stage 15 — checklist
def checklist(product_id, cfg, manual=None):
    """Manual validation checklist. Nothing is auto-completed: only a manual_validation file can mark items."""
    over = (manual or {}).get(str(product_id)) or {}
    out = {}
    for item in cfg["checklist"]:
        o = over.get(item) or {}
        st = o.get("status", "PENDING") if isinstance(o, dict) else str(o)
        out[item] = {"status": st if st in CHECK_STATUSES else "PENDING", "note": o.get("note") if isinstance(o, dict) else None}
    return out


# ============================================================================ Stages 6-12 — decision
def _task(cfg, key, fallback):
    return cfg["validation_tasks"].get(key, fallback)


def move_conditions(ev, dims, cfg):
    """WATCHLIST: exact conditions (from YAML thresholds) to move up or down."""
    d, mins = cfg["dimensions"], cfg["minimum_confidence"]
    txt_up = {
        "market_momentum": f"WPS >= {d['market_momentum']['acceptable']['wps_min']} with WPS Confidence >= {mins['wps']} "
                           f"(now WPS {v(ev, 'wps') if v(ev, 'wps') is not None else NA})",
        "cross_platform_demand": f"AVS >= {d['cross_platform_demand']['acceptable']['avs_min']} with Amazon Confidence >= "
                                 f"{mins['amazon']} (now AVS {v(ev, 'avs') if v(ev, 'avs') is not None else NA})",
        "commercial_viability": f"supplier cost + shipping known, BVS >= {d['commercial_viability']['acceptable']['bvs_min']} "
                                f"and gross margin >= {d['commercial_viability']['acceptable']['gross_margin_min']}% "
                                f"(now BVS {v(ev, 'bvs') if v(ev, 'bvs') is not None else NA})",
        "competitive_environment": f"competitor opportunity >= {d['competitive_environment']['acceptable']['opportunity_min']}"
                                   f" and saturation <= {d['competitive_environment']['acceptable']['saturation_max']}",
        "creative_opportunity": f"creative opportunity >= {d['creative_opportunity']['acceptable']['opportunity_min']} and "
                                f"saturation <= {d['creative_opportunity']['acceptable']['saturation_max']}",
        "evidence_quality": "WPS Confidence and at least half of known layer confidences at their minimums",
    }
    txt_down = {
        "market_momentum": f"WPS < {d['market_momentum']['acceptable']['wps_min']} or trend DECLINING with growth <= "
                           f"{cfg['hard_gates']['DEMAND_DECLINING']['growth_at_most']}%",
        "cross_platform_demand": f"AVS < {d['cross_platform_demand']['acceptable']['avs_min']}",
        "commercial_viability": "contribution margin < 0 (hard reject) or BVS / gross margin below ACCEPTABLE",
        "competitive_environment": "competitor opportunity / saturation outside ACCEPTABLE (with a second WEAK major -> REJECT)",
        "creative_opportunity": "creative opportunity / saturation outside ACCEPTABLE",
        "evidence_quality": f"WPS Confidence < {mins['wps']}",
    }
    up = [{"dimension": k, "now": dims[k]["status"], "condition": txt_up[k]} for k in DIMENSIONS
          if dims[k]["status"] in (WEAK, UNKNOWN)]
    down = [{"dimension": k, "now": dims[k]["status"], "condition": txt_down[k]} for k in DIMENSIONS
            if dims[k]["status"] in (STRONG, ACCEPTABLE)]
    return up, down


def missing_for_insufficient(ev, cfg):
    need = ["wps", "wps_confidence", "momentum_score", "momentum_confidence", "avs", "amazon_confidence", "bvs",
            "bvs_confidence", "product_cost", "supplier_shipping_cost", "competitor_confidence", "creative_confidence"]
    fields = [k for k in need if v(ev, k) is None]
    src_map = {"deep_analysis": "deep_analysis", "history": "history", "amazon": "amazon", "supplier": "supplier",
               "competitor": "competitor", "creative": "creative"}
    sources = [k for k in src_map if not ev["layers_present"].get(k)]
    queries = []
    for s in sources:
        q = cfg["next_queries"][src_map[s]]
        queries.append({"source": s, "action": q["action"].replace("<product_id>", ev["product_id"]),
                        "paid": q["paid"], "est_credits": q.get("est_credits", 0) if q["paid"] else 0,
                        "note": "paid: requires explicit live-run confirmation; never run automatically" if q["paid"]
                        else "free / manual"})
    return fields, sources, queries


AMZ_LEVELS = {"COMPLETA": "Validación Amazon completa", "MEDIA": "Validación Amazon media",
              "PARCIAL": "Validación Amazon parcial", "NO_VALIDADA": "Sin validar en Amazon"}


def _amazon_min_wps():
    try:
        import amazon_validation as _AV
        c = _AV.load_cfg()
        return c.get("minimum_wps"), c.get("minimum_confidence")
    except Exception:  # noqa: BLE001
        return None, None


def amazon_validation_level(ev, cfg, av_min=None):
    """How far the (optional) Amazon check got. Label only: it never changes the decision state.
    COMPLETA  reliable match + AVS + Amazon Confidence >= strong minimum
    MEDIA     reliable match + AVS + Amazon Confidence >= minimum (below strong)
    PARCIAL   Amazon answered but no reliable match / no AVS / confidence below minimum
    NO_VALIDADA  no Amazon data (not eligible, not queried or provider unavailable)"""
    c, mins = cfg["dimensions"]["cross_platform_demand"], cfg["minimum_confidence"]
    avs, conf, match = v(ev, "avs"), v(ev, "amazon_confidence"), v(ev, "amazon_match_status")
    wmin, cmin = av_min if av_min is not None else _amazon_min_wps()
    wps, wconf = v(ev, "wps"), v(ev, "wps_confidence")
    if avs is None and conf is None and match is None:
        if wmin is not None and (wps is None or wps < wmin):
            why = f"no elegible para Amazon: WPS {_f(wps)} < {wmin:g}"
        elif cmin is not None and (wconf is None or wconf < cmin):
            why = f"no elegible para Amazon: confianza WPS {_f(wconf)} < {cmin:g}"
        else:
            why = "Amazon no se consultó (límite por corrida o proveedor no disponible)"
        level = "NO_VALIDADA"
    elif match == "NO_RELIABLE_MATCH":
        level, why = "PARCIAL", "Amazon respondió, pero no hubo coincidencia confiable con el mismo producto"
    elif avs is None:
        level, why = "PARCIAL", "hay coincidencia en Amazon, pero sin AVS calculable"
    elif conf is None or conf < mins["amazon"]:
        level, why = "PARCIAL", f"confianza Amazon {_f(conf)} < mínimo {mins['amazon']:g}"
    elif conf >= c["strong"]["confidence_min"]:
        level, why = "COMPLETA", f"coincidencia confiable, AVS {_f(avs)}, confianza Amazon {_f(conf)}"
    else:
        level, why = "MEDIA", (f"coincidencia confiable, AVS {_f(avs)}, confianza Amazon {_f(conf)} "
                               f"(< {c['strong']['confidence_min']:g} para completa)")
    return {"level": level, "label": AMZ_LEVELS[level], "reason": why, "avs": avs, "amazon_confidence": conf}


def kalopilot_potential(decisions, max_products=None):
    """Products whose TikTok (KaloPilot) market momentum meets the rules, whatever Amazon or the other
    layers say. Reporting only: it never changes a decision state or the shortlist."""
    rows = [d for d in decisions if d["dimension_status"]["market_momentum"] in (STRONG, ACCEPTABLE)]
    rows.sort(key=lambda d: (-(v(d["evidence"], "wps") or 0), str(d["product_id"])))
    rows = rows[:max_products] if max_products else rows
    return [{"product_id": d["product_id"], "name": d["name"], "category": d.get("category"),
             "wps": v(d["evidence"], "wps"), "wps_confidence": v(d["evidence"], "wps_confidence"),
             "market_momentum": d["dimension_status"]["market_momentum"], "decision_state": d["decision_state"],
             "amazon_validation": d.get("amazon_validation")} for d in rows]


def decide(ev, cfg, manual=None):
    dims = dimensions(ev, cfg)
    cl = checklist(ev["product_id"], cfg, manual)
    gates = hard_gates(ev, dims, cfg, cl)
    dconf = decision_confidence(ev, dims, cfg)
    rules = cfg["decision"]
    st = {k: dims[k]["status"] for k in DIMENSIONS}
    fired_reject = [g for g in gates if g["status"] == "FIRED" and g["action"] == "reject"]
    fired_block = [g for g in gates if g["status"] == "FIRED" and g["action"] == "block_ready"]
    weak_major = [k for k in rules["major_dimensions"] if st[k] == WEAK]
    non_ev_unknown = [k for k in DIMENSIONS[:-1] if st[k] == UNKNOWN]
    path = []

    ready_fail = [f"{k} is {st[k]} (needs {'/'.join(ok)})" for k, ok in rules["ready"]["require_in"].items()
                  if st[k] not in ok]
    ready_fail += [f"{k} is {st[k]} (forbidden for READY)" for k, bad in rules["ready"]["forbid"].items() if st[k] in bad]
    if rules["ready"]["require_supplier_economics"] and not supplier_economics_known(ev):
        ready_fail.append("supplier economics missing (product_cost + shipping_cost)")
    ready_fail += [f"gate {g['gate']} requires manual validation" for g in fired_block]

    if fired_reject:
        state = REJECT
        path.append(f"hard gate(s) fired with action reject: {[g['gate'] for g in fired_reject]}")
    elif any(st[k] == UNKNOWN for k in rules["insufficient_if_unknown"]) or \
            len(non_ev_unknown) >= rules["insufficient_if_unknown_at_least"]:
        state = INSUFFICIENT
        path.append(f"no reject gate; insufficient: required UNKNOWN {[k for k in rules['insufficient_if_unknown'] if st[k] == UNKNOWN]}"
                    f", UNKNOWN dimensions {len(non_ev_unknown)} (limit {rules['insufficient_if_unknown_at_least']})")
    elif len(weak_major) >= rules["reject_if_weak_major_at_least"]:
        state = REJECT
        path.append(f"{len(weak_major)} WEAK major dimensions {weak_major} >= {rules['reject_if_weak_major_at_least']}")
    elif not ready_fail:
        state = READY
        path.append("all READY requirements met, no hard gate fired")
    elif st["market_momentum"] in rules["promising"]["require_in"]["market_momentum"] and \
            not any(st[k] == WEAK for k in rules["promising"]["forbid_weak"]):
        state = PROMISING
        path.append(f"not READY ({'; '.join(ready_fail)}); momentum {st['market_momentum']} and no WEAK dimension -> PROMISING")
    else:
        state = WATCH
        path.append(f"not READY ({'; '.join(ready_fail)}); not PROMISING (momentum {st['market_momentum']}, WEAK: "
                    f"{[k for k in rules['promising']['forbid_weak'] if st[k] == WEAK]}) -> WATCHLIST")

    tasks = []
    if not supplier_economics_known(ev):
        tasks.append({"task": _task(cfg, "supplier_economics", "supplier economics"), "reason": "supplier economics missing"})
    for k in DIMENSIONS:
        if st[k] == UNKNOWN and not (k == "commercial_viability" and not supplier_economics_known(ev)):
            tasks.append({"task": _task(cfg, k, k), "reason": f"{DIM_LABEL[k]} UNKNOWN: {dims[k]['reason']}"})
    for g in fired_block:
        tasks.append({"task": _task(cfg, g["gate"], g["gate"]), "reason": f"gate {g['gate']} (block_ready)"})
    trust = trust_check(ev, dims, gates)
    if ev.get("trust_required") and not trust["traceable"]:
        bad = trust["untraceable"]
        gate_bad = {g["gate"] for g in fired_reject if f"gate:{g['gate']}" in bad}
        severe = ("market_momentum" in bad and state != REJECT) or \
            (state == REJECT and fired_reject and gate_bad == {g["gate"] for g in fired_reject}) or \
            (state == REJECT and not fired_reject and any(k in bad for k in weak_major))
        if severe and state != INSUFFICIENT:
            path.append(f"DATA TRUST: decision depends on untraceable metrics {bad} -> INSUFFICIENT_DATA "
                        f"(was {state})")
            state = INSUFFICIENT
        elif state == READY:
            path.append(f"DATA TRUST: untraceable metrics {bad} -> PROMISING_NEEDS_VALIDATION (was READY)")
            state = PROMISING
        for k, miss in bad.items():
            tasks.append({"task": f"Restore provenance for {k} (missing: {miss})", "reason": "data trust check"})

    passed = [{"text": f"{DIM_LABEL[k]} {st[k]}: {dims[k]['reason']}", "rule": dims[k]["rule"], "evidence": dims[k]["inputs"]}
              for k in DIMENSIONS if st[k] in (STRONG, ACCEPTABLE)]
    passed += [{"text": f"gate {g['gate']} passed", "rule": g["rule"], "evidence": g["evidence"]}
               for g in gates if g["status"] in ("PASSED", "RESOLVED")]
    not_passed = [{"text": f"gate {g['gate']} FIRED ({g['action']})", "rule": g["rule"], "evidence": g["evidence"]}
                  for g in gates if g["status"] == "FIRED"]
    not_passed += [{"text": f"{DIM_LABEL[k]} {st[k]}: {dims[k]['reason']}", "rule": dims[k]["rule"], "evidence": dims[k]["inputs"]}
                   for k in DIMENSIONS if st[k] in (WEAK, UNKNOWN)]

    pending = [k for k, x in cl.items() if x["status"] == "PENDING"]
    explanation = evidence_explanation(ev, dims, gates)

    out = {"product_id": ev["product_id"], "name": ev["name"], "category": ev["category"], "url": ev["url"],
           "decision_state": state, "decision_path": path,
           "dimensions": dims, "dimension_status": st, "hard_gates": gates,
           "decision_confidence": dconf,
           "why_it_passed": passed, "why_it_did_not_pass": not_passed,
           "missing_validation": [t["task"] for t in tasks] + [f"manual checklist: {k}" for k in pending],
           "manual_checklist": cl, "data_trust": trust, "explanation": explanation, "evidence": ev}
    if state == PROMISING:
        out["validation_tasks"] = tasks
    if state == WATCH:
        up, down = move_conditions(ev, dims, cfg)
        out["move_up_conditions"], out["move_down_conditions"] = up, down
    if state == REJECT:
        out["reject_reasons"] = ([{"gate": g["gate"], "evidence": g["evidence"]} for g in fired_reject] or
                                 [{"weak_major_dimensions": weak_major,
                                   "detail": {k: dims[k]["reason"] for k in weak_major}}])
    if state == INSUFFICIENT:
        f, s, q = missing_for_insufficient(ev, cfg)
        out["missing_fields"], out["missing_sources"], out["required_next_queries"] = f, s, q
    out["amazon_validation"] = amazon_validation_level(ev, cfg)
    out["next_actions"] = next_actions(out, tasks, pending)
    return out


def evidence_explanation(ev, dims, gates):
    """AB Stage 7: separate what the data SHOWS (negative evidence) from what the data LACKS (missing evidence).

    NEGATIVE_EVIDENCE: a fired gate with its measured values, or a WEAK dimension computed from real values.
    MISSING_EVIDENCE : an UNKNOWN dimension, a gate that could not be evaluated, or a gate fired only because
                       broader evidence is missing."""
    neg, miss = [], []
    for k in DIMENSIONS:
        d = dims[k]
        if d["status"] == WEAK and k != "evidence_quality":
            neg.append({"source": f"dimension:{k}", "reason": d["reason"], "evidence": d["inputs"]})
        elif d["status"] == UNKNOWN or (d["status"] == WEAK and k == "evidence_quality"):   # low confidence = thin data
            miss.append({"source": f"dimension:{k}", "reason": d["reason"]})
    for g in gates:
        if g["status"] == "FIRED":
            (miss if g["evidence"].get("note", "").startswith("broader evidence missing") else neg).append(
                {"source": f"gate:{g['gate']}", "action": g["action"], "evidence": g["evidence"]})
        elif g["status"] == "NOT_EVALUATED":
            miss.append({"source": f"gate:{g['gate']}", "reason": f"gate {g['gate']} not evaluable (inputs missing)"})
    primary = "NEGATIVE_EVIDENCE" if neg else ("MISSING_EVIDENCE" if miss else "NONE")
    return {"primary": primary, "NEGATIVE_EVIDENCE": neg, "MISSING_EVIDENCE": miss}


def next_actions(dec, tasks, pending):
    s = dec["decision_state"]
    if s == READY:
        return [f"Manual validation: {k}" for k in pending] or ["All manual checks complete — review decision with a human"]
    if s == PROMISING:
        return [t["task"] for t in tasks]
    if s == WATCH:
        return ["Re-observe later; move up when: " + "; ".join(c["condition"] for c in dec["move_up_conditions"])]
    if s == INSUFFICIENT:
        return [q["action"] + (f" (paid, ~{q['est_credits']} credits est., needs explicit confirmation)" if q["paid"] else "")
                for q in dec["required_next_queries"]]
    return ["No further research spend recommended while the reject reason stands (kept visible in the report)"]


# ============================================================================ Stage 14 — shortlist
def shortlist(decisions, cfg):
    s = cfg["shortlist"]
    ready = [d for d in decisions if d["decision_state"] == s["only_state"]]

    def key(d):
        ev = d["evidence"]
        co, cr = v(ev, "competitor_opportunity"), v(ev, "creative_opportunity")
        return (RANK[d["dimension_status"]["market_momentum"]], RANK[d["dimension_status"]["commercial_viability"]],
                -d["decision_confidence"]["score"], -(co if co is not None else -1), -(cr if cr is not None else -1),
                d["product_id"])
    ranked = sorted(ready, key=key)
    return [{"rank": i, "product_id": d["product_id"], "name": d["name"],
             "tie_break": {"market_momentum": d["dimension_status"]["market_momentum"],
                           "commercial_viability": d["dimension_status"]["commercial_viability"],
                           "decision_confidence": d["decision_confidence"]["score"],
                           "competitive_opportunity": v(d["evidence"], "competitor_opportunity"),
                           "creative_opportunity": v(d["evidence"], "creative_opportunity")}}
            for i, d in enumerate(ranked[: s["max_products"]], 1)]


# ============================================================================ run / versioning
def run(products, competitor_by_id=None, creative_by_id=None, cfg=None, manual=None, expected_env=None,
        now=None, require_trust=False, scoring_path=None, runtime_path=None, decision_path=None,
        confidence_penalties=None, config_version=None):
    import config_resolver as _CR
    scoring_path = scoring_path or _CR.path("scoring.yaml")
    runtime_path = runtime_path or _CR.path("runtime.yaml")
    decision_path = decision_path or _CR.path("decision.yaml")
    cfg = cfg or load_cfg(decision_path)
    now = now or datetime.now(timezone.utc)
    competitor_by_id, creative_by_id = competitor_by_id or {}, creative_by_id or {}
    decisions = []
    for p in products:
        pid = str(p.get("product_id") or p.get("id"))
        ev = build_evidence(p, competitor_by_id.get(pid), creative_by_id.get(pid), expected_env)
        ev["trust_required"] = require_trust
        d = decide(ev, cfg, manual)
        pens = (confidence_penalties or {}).get(pid) or []
        if pens:                                   # AC degraded mode: penalties only LOWER Decision Confidence
            dc = d["decision_confidence"]
            total = sum(abs(x["points"]) for x in pens)
            dc["components"]["degraded_mode_penalty"] = -round(total, 2)
            dc["score"] = round(max(0.0, dc["score"] - total), 2)
            dc["degraded_mode"] = pens
        decisions.append(d)
    decisions.sort(key=lambda d: d["product_id"])
    meta = {"decision_rules_version": cfg["decision_rules_version"],
            "scoring_config_hash": file_hash(scoring_path), "runtime_config_hash": file_hash(runtime_path),
            "decision_config_hash": file_hash(decision_path), "timestamp": now.isoformat(),
            "data_environment": expected_env or NA, "products_evaluated": len(decisions),
            "data_trust_enforced": require_trust, "config_version": config_version or NA,
            "disclaimer": DISCLAIMER}
    for d in decisions:
        d["versioning"] = {k: meta[k] for k in ("decision_rules_version", "scoring_config_hash",
                                                "runtime_config_hash", "decision_config_hash", "timestamp")}
    return {"metadata": meta, "decisions": decisions, "shortlist": shortlist(decisions, cfg), "cfg": cfg,
            "kalopilot_potential": kalopilot_potential(decisions)}


def explain_decision(product_id, result):
    """Full decision path for one product (from a run() result or a saved final-decision JSON)."""
    decs = result.get("decisions") or result.get("all_decisions") or []
    d = next((x for x in decs if str(x["product_id"]) == str(product_id)), None)
    if d is None:
        return {"product_id": str(product_id), "error": "product not in this decision run"}
    meta = result.get("metadata") or {}
    return {
        "product_id": d["product_id"], "name": d["name"], "decision_state": d["decision_state"],
        "decision_rules_version": meta.get("decision_rules_version"),
        "step_1_evidence": {k: x for k, x in d["evidence"]["values"].items()},
        "step_1_flags": d["evidence"]["flags"],
        "step_2_hard_gates": [{"gate": g["gate"], "status": g["status"], "action": g["action"],
                               "evidence": g["evidence"]} for g in d["hard_gates"]],
        "step_3_dimensions": {k: {"status": x["status"], "reason": x["reason"], "rule": x["rule"], "inputs": x["inputs"]}
                              for k, x in d["dimensions"].items()},
        "step_4_state_rule_path": d["decision_path"],
        "step_5_decision_confidence": d["decision_confidence"],
        "why_it_passed": [x["text"] for x in d["why_it_passed"]],
        "why_it_did_not_pass": [x["text"] for x in d["why_it_did_not_pass"]],
        "missing_validation": d["missing_validation"], "next_actions": d["next_actions"],
        "versioning": d.get("versioning"),
    }


# ============================================================================ Stages 16-17 — reports
def _f(x):
    return NA if x is None else (f"{x:g}" if isinstance(x, float) else str(x))


def build_json(result):
    decs = result["decisions"]
    by = {s: [d for d in decs if d["decision_state"] == s] for s in (READY, PROMISING, WATCH, REJECT, INSUFFICIENT)}
    strip = lambda d: {k: x for k, x in d.items() if k != "evidence"} | {  # noqa: E731
        "evidence": {"values": d["evidence"]["values"], "flags": d["evidence"]["flags"],
                     "layers_present": d["evidence"]["layers_present"], "trust": d["evidence"].get("trust")}}
    return {"metadata": result["metadata"], "decision_rules_version": result["metadata"]["decision_rules_version"],
            "shortlist": result["shortlist"],
            "kalopilot_potential": result.get("kalopilot_potential") or kalopilot_potential(decs),
            "ready_products": [strip(d) for d in by[READY]],
            "promising_products": [strip(d) for d in by[PROMISING]],
            "watchlist": [strip(d) for d in by[WATCH]],
            "rejected_products": [strip(d) for d in by[REJECT]],
            "insufficient_data": [strip(d) for d in by[INSUFFICIENT]]}


def render_markdown(result):
    meta, decs, sl = result["metadata"], result["decisions"], result["shortlist"]
    by = {s: [d for d in decs if d["decision_state"] == s] for s in (READY, PROMISING, WATCH, REJECT, INSUFFICIENT)}
    L = [f"# Final Decision — {meta['timestamp'][:10]}", ""]
    if meta.get("config_version") not in (None, NA):
        L += [f"**CONFIG VERSION:** {meta['config_version']} · **RUN ID:** {meta.get('run_id', NA)} · "
              f"**DATA ENVIRONMENT:** {meta.get('data_environment')}", ""]
    L += [
         f"> Rules `{meta['decision_rules_version']}` · scoring config `{meta['scoring_config_hash']}` · runtime config "
         f"`{meta['runtime_config_hash']}` · decision config `{meta['decision_config_hash']}` · data environment "
         f"**{meta['data_environment']}** · {meta['timestamp']}", "", f"> {DISCLAIMER}", ""]
    L += ["## 1. Executive summary", "",
          f"Products evaluated: {len(decs)} · READY {len(by[READY])} · PROMISING {len(by[PROMISING])} · WATCHLIST "
          f"{len(by[WATCH])} · REJECT {len(by[REJECT])} · INSUFFICIENT_DATA {len(by[INSUFFICIENT])}.",
          "No weighted mega-score is used: each product passes hard gates and six separately visible dimensions.",
          "No product was bought or launched and no query was executed by this engine.", ""]
    L += ["## 2. Shortlist (max 3, READY only, never filled)", ""]
    if sl:
        L += ["| # | Product | Momentum | Commercial | Decision Conf. | Competitor opp. | Creative opp. |",
              "|---|---|---|---|---|---|---|"]
        L += [f"| {s['rank']} | {s['name']} | {s['tie_break']['market_momentum']} | {s['tie_break']['commercial_viability']} | "
              f"{s['tie_break']['decision_confidence']} | {_f(s['tie_break']['competitive_opportunity'])} | "
              f"{_f(s['tie_break']['creative_opportunity'])} |" for s in sl]
    else:
        L.append("No product met every READY rule. The shortlist is left empty rather than filled with weaker products.")
    pot = result.get("kalopilot_potential")
    pot = pot if pot is not None else kalopilot_potential(decs)
    L += ["", "## 2b. KaloPilot potential (Amazon not required)", "",
          "Products whose TikTok Shop metrics (KaloPilot) meet the market-momentum rule, shown whatever the other "
          "layers say. The Amazon label says how far the optional Amazon check got; it never hides a product.", ""]
    if pot:
        L += ["| Product | WPS | WPS Conf. | Momentum | Decision state | Amazon | Why |", "|---|---|---|---|---|---|---|"]
        L += [f"| {p['name']} | {_f(p['wps'])} | {_f(p['wps_confidence'])} | {p['market_momentum']} | "
              f"{p['decision_state']} | {(p['amazon_validation'] or {}).get('label', NA)} | "
              f"{(p['amazon_validation'] or {}).get('reason', NA)} |" for p in pot]
    else:
        L.append("No product met the KaloPilot market-momentum rule in this run.")
    L += ["", "## 3. Evidence matrix", "",
          "| Product | WPS | WPS Conf. | Momentum | Mom. Conf. | AVS | Amz Conf. | BVS | BVS Conf. | Supplier Conf. | "
          "Comp. Sat/Opp/Conf | Creative Sat/Opp/Conf | History obs. |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for d in decs:
        e = lambda k: _f(v(d["evidence"], k))  # noqa: E731
        L.append(f"| {d['name']} | {e('wps')} | {e('wps_confidence')} | {e('momentum_score')} | {e('momentum_confidence')} | "
                 f"{e('avs')} | {e('amazon_confidence')} | {e('bvs')} | {e('bvs_confidence')} | {e('supplier_confidence')} | "
                 f"{e('competitor_saturation')}/{e('competitor_opportunity')}/{e('competitor_confidence')} | "
                 f"{e('creative_saturation')}/{e('creative_opportunity')}/{e('creative_confidence')} | "
                 f"{e('history_observations')} |")
    L += ["", "## 4. Decision state per product", "",
          "| Product | State | Momentum | Cross-Platform | Commercial | Competitive | Creative | Evidence | Amazon |",
          "|---|---|---|---|---|---|---|---|---|"]
    for d in decs:
        s = d["dimension_status"]
        L.append(f"| {d['name']} | **{d['decision_state']}** | " + " | ".join(s[k] for k in DIMENSIONS) +
                 f" | {(d.get('amazon_validation') or {}).get('label', NA)} |")
    L += ["", "## 5. Decision confidence", "",
          "Confidence in the decision (evidence coverage and quality), not a product score.", "",
          "| Product | Decision Conf. | Coverage | Layer conf. | Provenance | Reliable sources | History |",
          "|---|---|---|---|---|---|---|"]
    for d in decs:
        c = d["decision_confidence"]["components"]
        L.append(f"| {d['name']} | {d['decision_confidence']['score']} | {c['core_dimension_coverage']} | "
                 f"{c['layer_confidences']} | {c['provenance_completeness']} | {c['reliable_sources']} | {c['historical_depth']} |")
    L += ["", "## 6. Main risks", ""]
    for d in decs:
        risks = [x["text"] for x in d["why_it_did_not_pass"]]
        fl = sorted(d["evidence"]["flags"])
        L.append(f"- **{d['name']}** — " + ("; ".join(risks) if risks else "no failed rule") +
                 (f" · flags: {', '.join(fl)}" if fl else ""))
    L += ["", "## 7. Missing validation", ""]
    for d in decs:
        if d["decision_state"] in (READY, PROMISING):
            L.append(f"- **{d['name']}** ({d['decision_state']}):")
            L += [f"  - {m}" for m in d["missing_validation"]]
    if not by[READY] and not by[PROMISING]:
        L.append("No READY or PROMISING product.")
    L += ["", "## 8. Manual validation checklist", ""]
    cands = by[READY] + by[PROMISING]
    if cands:
        items = list(cands[0]["manual_checklist"])
        L += ["| Item | " + " | ".join(d["name"] for d in cands) + " |", "|---|" + "---|" * len(cands)]
        L += [f"| {i} | " + " | ".join(d["manual_checklist"][i]["status"] for d in cands) + " |" for i in items]
    else:
        L.append("No READY or PROMISING product; checklist not opened.")
    L += ["", "## 9. Rejected products (never hidden)", ""]
    for d in by[REJECT]:
        L.append(f"- **{d['name']}** — " + "; ".join(
            (f"gate {r['gate']}: {json.dumps(r['evidence'], default=str)}" if "gate" in r else
             f"WEAK major dimensions {r['weak_major_dimensions']}: {json.dumps(r['detail'])}") for r in d["reject_reasons"]))
    if not by[REJECT]:
        L.append("None.")
    L += ["", "## 10. Watchlist", ""]
    for d in by[WATCH]:
        L.append(f"- **{d['name']}**")
        L += [f"  - move up if {c['dimension']} ({c['now']}): {c['condition']}" for c in d["move_up_conditions"]]
        L += [f"  - move down if {c['dimension']} ({c['now']}): {c['condition']}" for c in d["move_down_conditions"]]
    if not by[WATCH]:
        L.append("None.")
    L += ["", "### Insufficient data", ""]
    for d in by[INSUFFICIENT]:
        L.append(f"- **{d['name']}** — missing fields: {', '.join(d['missing_fields']) or 'none'}; missing sources: "
                 f"{', '.join(d['missing_sources']) or 'none'}")
        L += [f"  - next: {q['action']}" + (f" (paid, ~{q['est_credits']} credits est.; needs explicit confirmation)"
                                            if q["paid"] else " (free)") for q in d["required_next_queries"]]
    if not by[INSUFFICIENT]:
        L.append("None.")
    return "\n".join(L) + "\n"


def check_language(text, cfg):
    low = text.lower()
    bad = [w for w in cfg["forbidden_language"] if w.lower() in low]
    if bad:
        raise ValueError(f"forbidden decision language in output: {bad}")


def dated_paths(out_dir, date):
    n = 1
    while True:
        sfx = "" if n == 1 else f"-{n}"
        md, js = out_dir / f"{date}-final-decision{sfx}.md", out_dir / f"{date}-final-decision{sfx}.json"
        if not md.exists() and not js.exists():
            return md, js
        n += 1


def write_reports(result, out_dir, secrets=(), secret_patterns=("token", "secret", "api_key", "password", "authorization")):
    import generate_report as GR
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pats = [p.lower() for p in secret_patterns]
    md = GR.scrub(render_markdown(result), pats, list(secrets))
    js = GR.scrub(build_json(result), pats, list(secrets))
    check_language(md, result["cfg"])
    check_language(json.dumps(js, default=str), result["cfg"])
    md_path, js_path = dated_paths(out_dir, result["metadata"]["timestamp"][:10])
    with open(md_path, "x") as f:                           # dated: never overwritten
        f.write(md)
    with open(js_path, "x") as f:
        json.dump(js, f, ensure_ascii=False, indent=2, default=str)
    (out_dir / "latest-final-decision.md").write_text(md)   # replaced each run
    (out_dir / "latest-final-decision.json").write_text(json.dumps(js, ensure_ascii=False, indent=2, default=str))
    return {"markdown": str(md_path), "json": str(js_path), "latest": str(out_dir / "latest-final-decision.md")}


def load_saved(out_dir):
    """Latest saved final-decision JSON -> structure explain_decision understands."""
    p = Path(out_dir) / "latest-final-decision.json"
    if not p.exists():
        return None
    js = json.loads(p.read_text())
    decs = [d for k in ("ready_products", "promising_products", "watchlist", "rejected_products", "insufficient_data")
            for d in js.get(k) or []]
    return {"metadata": js["metadata"], "decisions": decs, "shortlist": js["shortlist"]}
