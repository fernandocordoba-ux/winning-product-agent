"""Business Viability Score (Step O): is this product commercially practical for our Shopify store?

Independent from WPS, Confidence, AVS and Amazon Confidence (reads them, never changes them).
All rules: config/business_viability.yaml. Deterministic; nothing estimated.

Inputs per product:
  - Deep Analysis result (Step M)       data/processed/deep_analysis/deep_*.json     (required)
  - Amazon Validation result (Step N)   data/processed/amazon_validation/amazon_*.json (optional)
  - Commercial / supplier data          data/raw/business_viability/*supplier_data*.json (optional;
    KaloData has NO supplier economics — until a supplier source is integrated these are N/A)

Outputs: data/processed/business_viability/bvs_<ts>.json (new file every run)

CLI:
  python3 scripts/business_viability.py                          # score latest Deep/Amazon results
  python3 scripts/business_viability.py add-supplier <product_id> <data.json>   # store supplier data (raw, read-only)
No paid queries are made by this module.
"""
import copy
import json
import math
import re
import statistics
import sys
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import deep_analysis as DA  # noqa: E402
import discovery as disc  # noqa: E402

NA = "N/A"
RAW_DIR = ROOT / "data" / "raw" / "business_viability"
OUT_DIR = ROOT / "data" / "processed" / "business_viability"
DEEP_DIR = ROOT / "data" / "processed" / "deep_analysis"
AMAZON_DIR = ROOT / "data" / "processed" / "amazon_validation"


def load_cfg(path=ROOT / "config" / "business_viability.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)["business_viability"]


# ================================================================== helpers
def num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def boolean(v):
    return v if isinstance(v, bool) else None


def rnd(x, p=2):
    return None if x is None else float(Decimal(repr(x)).quantize(Decimal(1).scaleb(-p), rounding=ROUND_HALF_UP))


def band(x, bands):
    """First band whose min (x >= min) or max (x <= max) matches. None if x is None."""
    if x is None:
        return None
    for b in bands:
        if ("min" in b and x >= b["min"]) or ("max" in b and x <= b["max"]):
            return b["s"]
    return 0.0


def kw_hits(text, keywords):
    if not text:
        return []
    return sorted({k for k in keywords if disc._word_match(text, k)})


def level(score, levels):
    for name, t in sorted(levels.items(), key=lambda kv: -kv[1]):
        if score >= t:
            return name
    return None


class Parts:
    """Collects sub-part scores for one component (N/A sub-parts earn 0)."""

    def __init__(self, cfg):
        self.cfg, self.detail, self.total = cfg, {}, 0.0

    def add(self, name, s, points=None, **info):
        pts = points if points is not None else self.cfg["parts"][name]["points"]
        if s is None:
            self.detail[name] = {"points": NA, "max": pts, **info}
        else:
            self.total += s * pts
            self.detail[name] = {"points": rnd(s * pts), "max": pts, "s": rnd(s, 4), **info}

    def result(self, factor=1.0, **extra):
        score = self.total * factor
        avail = [d for d in self.detail.values() if d["points"] != NA]
        status = "N/A" if not avail else ("full" if len(avail) == len(self.detail) else "partial")
        return {"score": rnd(score), "max": self.cfg["points"], "status": status, "parts": self.detail, **extra}


# ================================================================== Stage 3 — economics
def economics(sell, comm, cfg):
    """Product economics. Every output is N/A when any of its inputs is missing."""
    e = cfg["economics"]
    pc = num(comm.get("product_cost"))
    ship = num(comm.get("supplier_shipping_cost"))
    refund_rate = num(comm.get("estimated_refund_rate_pct", e["estimated_refund_rate_pct"]))
    cb_rate = num(comm.get("estimated_chargeback_rate_pct", e["estimated_chargeback_rate_pct"]))
    ad_cost = num(comm.get("estimated_ad_cost_per_order", e["estimated_ad_cost_per_order"]))

    pay = e["payment_processing"]
    plat = e["platform_fee"]
    payment_fee = sell * pay["percent"] / 100 + pay["fixed"] if sell is not None else None
    platform_fee = sell * plat["percent"] / 100 + plat["fixed"] if sell is not None else None
    duty = num(comm.get("import_duty_per_order"))            # Step W: ONLY when explicitly provided
    landed = pc + ship + (duty or 0.0) if None not in (pc, ship) else None
    gross = sell - landed - payment_fee - platform_fee if None not in (sell, landed) else None
    gross_pct = gross / sell * 100 if gross is not None and sell else None
    refund_cost = refund_rate / 100 * sell if None not in (refund_rate, sell) else None
    cb_cost = cb_rate / 100 * (sell + e["chargeback_fee"]) if None not in (cb_rate, sell) else None
    contrib = gross - ad_cost - refund_cost - cb_cost if None not in (gross, ad_cost, refund_cost, cb_cost) else None
    contrib_pct = contrib / sell * 100 if contrib is not None and sell else None
    return {
        "selling_price": rnd(sell), "product_cost": rnd(pc), "supplier_shipping_cost": rnd(ship),
        "landed_cost": rnd(landed), "import_duty_per_order": rnd(duty),
        "landed_cost_note": (None if landed is None else "includes explicit import duty" if duty is not None
                             else "excludes unknown import duties (never estimated)"),
        "payment_processing_fee": rnd(payment_fee), "platform_fee": rnd(platform_fee),
        "estimated_ad_cost_per_order": rnd(ad_cost), "estimated_refund_rate_pct": refund_rate,
        "estimated_chargeback_rate_pct": cb_rate, "expected_refund_cost": rnd(refund_cost),
        "expected_chargeback_cost": rnd(cb_cost), "gross_profit_before_ads": rnd(gross),
        "gross_margin_percent": rnd(gross_pct), "contribution_profit": rnd(contrib),
        "contribution_margin_percent": rnd(contrib_pct),
    }


def selling_price(deep, comm, cfg):
    for src in cfg["economics"]["selling_price_source"]:
        if src == "commercial_input" and num(comm.get("selling_price")) is not None:
            return num(comm["selling_price"]), "commercial_input"
        if src == "tiktok_price_avg" and num((deep.get("price") or {}).get("avg")) is not None:
            return num(deep["price"]["avg"]), "ASSUMPTION: TikTok Shop average price used as our selling price"
    return None, None


# ================================================================== components
def gross_margin_component(econ, cfg):
    c = cfg["gross_margin_potential"]
    p = Parts(c)
    p.add("gross_margin", band(econ["gross_margin_percent"], c["parts"]["gross_margin"]["bands"]),
          value=econ["gross_margin_percent"])
    p.add("contribution_margin", band(econ["contribution_margin_percent"], c["parts"]["contribution_margin"]["bands"]),
          value=econ["contribution_margin_percent"])
    flags = []
    if econ["gross_margin_percent"] is not None and econ["gross_margin_percent"] < c["low_gross_margin_below"]:
        flags.append({"flag": "LOW_GROSS_MARGIN", "gross_margin_percent": econ["gross_margin_percent"]})
    if econ["contribution_margin_percent"] is not None and econ["contribution_margin_percent"] < c["negative_contribution_below"]:
        flags.append({"flag": "NEGATIVE_CONTRIBUTION_MARGIN", "contribution_margin_percent": econ["contribution_margin_percent"]})
    return p.result(), flags


def shipping_component(comm, sell, name, cfg):
    c = cfg["shipping_viability"]
    p, flags = Parts(c), []
    days = num(comm.get("delivery_days_max"))
    p.add("delivery_time", band(days, c["parts"]["delivery_time"]["bands"]), value=days)
    ship = num(comm.get("supplier_shipping_cost"))
    pct = ship / sell * 100 if None not in (ship, sell) and sell else None
    p.add("shipping_cost", band(pct, c["parts"]["shipping_cost"]["bands"]), value=rnd(pct))
    tracking = boolean(comm.get("tracking_available"))
    p.add("tracking", None if tracking is None else float(tracking), value=tracking)

    oversized = boolean(comm.get("oversized"))
    ov = c["oversized_if"]
    side, weight = num(comm.get("longest_side_cm")), num(comm.get("weight_kg"))
    if oversized is None and (side is not None or weight is not None):
        oversized = bool((side is not None and side >= ov["longest_side_cm_min"]) or
                         (weight is not None and weight >= ov["weight_kg_min"]))
    handling = {"fragile": boolean(comm.get("fragile")), "contains_battery": boolean(comm.get("contains_battery")),
                "contains_liquid": boolean(comm.get("contains_liquid")), "oversized": oversized,
                "special_handling": boolean(comm.get("special_handling")), "customs_risk": boolean(comm.get("customs_risk"))}
    if all(handling[k] is not None for k in c["handling_core_fields"]):
        n = sum(1 for v in handling.values() if v is True)
        p.add("handling", max(0.0, 1 - c["handling_penalty_per_restriction"] * n), restrictions=n)
    else:
        p.add("handling", None, reason="core handling fields unknown")

    fl = c["flags"]
    if days is not None and days > fl["SLOW_SHIPPING"]["delivery_days_max_above"]:
        flags.append({"flag": "SLOW_SHIPPING", "delivery_days_max": days})
    if pct is not None and pct > fl["HIGH_SHIPPING_COST"]["shipping_cost_pct_above"]:
        flags.append({"flag": "HIGH_SHIPPING_COST", "shipping_cost_pct_of_price": rnd(pct)})
    if tracking is False:
        flags.append({"flag": "TRACKING_UNAVAILABLE"})
    data_flags = {"FRAGILE": handling["fragile"], "BATTERY_RESTRICTION": handling["contains_battery"],
                  "LIQUID_RESTRICTION": handling["contains_liquid"], "OVERSIZED": handling["oversized"],
                  "CUSTOMS_RISK": handling["customs_risk"]}
    for code, val in data_flags.items():
        hits = kw_hits(name, c["keywords"].get(code, []))
        if val is True:
            flags.append({"flag": code, "evidence": "supplier data"})
        elif hits and val is None:
            flags.append({"flag": code, "evidence": "keyword in product name", "keywords": hits})
    metrics = {"delivery_days_max": days, "tracking_available": tracking, "supplier_shipping_cost": ship,
               "shipping_cost_pct_of_price": rnd(pct), **handling, "longest_side_cm": side, "weight_kg": weight}
    return p.result(), flags, metrics


def supplier_component(comm, cfg):
    c = cfg["supplier_viability"]
    p, flags = Parts(c), []
    sups = [s for s in (comm.get("suppliers") or []) if isinstance(s, dict)] if isinstance(comm.get("suppliers"), list) else None
    count = num(comm.get("supplier_count"))
    if count is None and sups is not None:
        count = float(len(sups))
    p.add("supplier_count", band(count, c["parts"]["supplier_count"]["bands"]), value=count)
    prices = [num(s.get("price")) for s in sups or [] if num(s.get("price")) is not None]
    cv = statistics.pstdev(prices) / statistics.mean(prices) * 100 if len(prices) >= 2 and statistics.mean(prices) > 0 else None
    p.add("price_consistency", band(cv, c["parts"]["price_consistency"]["bands"]), value=rnd(cv))

    primary = None
    sel = comm.get("selected_supplier_index")
    if sups and isinstance(sel, int) and 0 <= sel < len(sups):
        primary = sups[sel]                        # Step W: the ranked, selected offer (never "cheapest")
    elif sups:                                     # legacy manual supplier_data files (pre Step W)
        priced = [(num(s.get("price")), i) for i, s in enumerate(sups) if num(s.get("price")) is not None]
        primary = sups[min(priced)[1]] if priced else sups[0]
    g = (lambda k: num(primary.get(k)) if primary else None)
    rating, proc, stock, moq = g("rating"), g("processing_days"), g("stock"), g("moq")
    us = boolean(primary.get("us_warehouse")) if primary else None
    p.add("rating", band(rating, c["parts"]["rating"]["bands"]), value=rating)
    p.add("processing_time", band(proc, c["parts"]["processing_time"]["bands"]), value=proc)
    p.add("us_warehouse", None if us is None else float(us), value=us)
    p.add("stock", band(stock, c["parts"]["stock"]["bands"]), value=stock)
    p.add("moq", band(moq, c["parts"]["moq"]["bands"]), value=moq)

    fl = c["flags"]
    if count is not None and 0 < count <= fl["SINGLE_SUPPLIER_DEPENDENCY"]["supplier_count_max"]:
        flags.append({"flag": "SINGLE_SUPPLIER_DEPENDENCY", "supplier_count": count})
    if rating is not None and rating < fl["LOW_SUPPLIER_RATING"]["rating_below"]:
        flags.append({"flag": "LOW_SUPPLIER_RATING", "rating": rating})
    if proc is not None and proc > fl["LONG_PROCESSING_TIME"]["processing_days_above"]:
        flags.append({"flag": "LONG_PROCESSING_TIME", "processing_days": proc})
    if stock is not None and stock < fl["LOW_STOCK"]["stock_below"]:
        flags.append({"flag": "LOW_STOCK", "stock": stock})
    if moq is not None and moq > fl["MOQ_TOO_HIGH"]["moq_above"]:
        flags.append({"flag": "MOQ_TOO_HIGH", "moq": moq})
    if us is False:
        flags.append({"flag": "NO_US_WAREHOUSE"})
    metrics = {"supplier_count": count, "price_cv_pct": rnd(cv), "primary_supplier": copy.deepcopy(primary),
               "rating": rating, "processing_days": proc, "us_warehouse": us, "stock": stock, "moq": moq,
               "orders": g("orders"), "warehouse_country": primary.get("warehouse_country") if primary else None}
    return p.result(), flags, metrics


def competition_component(deep, amz, cfg):
    c = cfg["competition_opportunity"]
    p, flags = Parts(c), []
    similar = num((deep.get("competition_metrics") or {}).get("similar_listings_count"))
    p.add("tiktok_saturation", band(similar, c["parts"]["tiktok_saturation"]["bands"]), value=similar)
    acomp = (amz or {}).get("amazon_competition")
    p.add("amazon_saturation", c["parts"]["amazon_saturation"]["map"].get(acomp), value=acomp)
    palign = (amz or {}).get("price_alignment")
    p.add("price_compression", c["parts"]["price_compression"]["map"].get(palign), value=palign)
    t3 = num((amz or {}).get("amazon_top3_brand_share_pct"))
    p.add("brand_concentration", band(t3, c["parts"]["brand_concentration"]["bands"]), value=t3)

    df = c["demand_factor"]
    cpd = (amz or {}).get("cross_platform_demand")
    units = num(deep.get("units"))
    if cpd in df["cross_platform"]:
        factor, basis = df["cross_platform"][cpd], f"cross_platform_demand {cpd}"
    elif units is not None:
        factor = next(b["factor"] for b in df["tiktok_units_30d"] if units >= b["min"])
        basis = f"tiktok units_30d {units:g}"
    else:
        factor, basis = 1.0, "demand_unverified"

    fl = c["flags"]
    ec = fl["EXTREME_COMPETITION"]
    if (similar is not None and similar >= ec["similar_listings_min"]) or acomp == ec["amazon_competition"] or \
       any(f.get("flag") == "EXTREME_SATURATION" for f in deep.get("red_flags") or []):
        flags.append({"flag": "EXTREME_COMPETITION", "similar_listings_count": similar, "amazon_competition": acomp})
    ratio = num((amz or {}).get("price_ratio"))
    if ratio is not None and ratio < fl["PRICE_COMPRESSION"]["price_ratio_below"]:
        flags.append({"flag": "PRICE_COMPRESSION", "amazon_to_tiktok_price_ratio": rnd(ratio, 3)})
    metrics = {"similar_listings_count": similar, "amazon_competition": acomp, "price_alignment": palign,
               "amazon_top3_brand_share_pct": t3, "cross_platform_demand": cpd, "tiktok_units_30d": units,
               "demand_factor": factor, "demand_basis": basis}
    return p.result(factor=factor, demand_factor=factor, demand_basis=basis), flags, metrics


def return_risk_component(name, comm, cfg):
    c = cfg["return_risk"]
    actual = num(comm.get("actual_refund_rate_pct"))
    found, flags = {}, []
    for code, ch in c["characteristics"].items():
        attr = boolean(comm.get(code.lower()))
        hits = kw_hits(name, ch["keywords"])
        if attr is True:
            found[code] = {"weight": ch["weight"], "evidence": "commercial data"}
        elif attr is None and hits:
            found[code] = {"weight": ch["weight"], "evidence": "keyword in product name", "keywords": hits}
    risk_sum = sum(v["weight"] for v in found.values())
    if actual is not None:
        s, basis = band(actual, c["actual_refund_rate_bands"]), "actual_refund_rate"
    elif name:
        s, basis = 1 - min(risk_sum, c["risk_cap"]) / c["risk_cap"], "keyword_screening"
    else:
        s, basis = None, None
    for code, v in found.items():
        if code in ("SIZE_DEPENDENT", "COMPATIBILITY_RISK", "FRAGILE_PRODUCT", "QUALITY_EXPECTATION_RISK"):
            flags.append({"flag": code, **{k: x for k, x in v.items() if k != "weight"}})
    if risk_sum >= c["high_return_risk_min"] or (actual is not None and s == 0.0):
        flags.append({"flag": "HIGH_RETURN_RISK", "risk_sum": risk_sum, "actual_refund_rate_pct": actual})
    pts = c["points"]
    comp = {"score": NA if s is None else rnd(s * pts), "max": pts, "status": "N/A" if s is None else "full",
            "basis": basis}
    return comp, flags, {"actual_refund_rate_pct": actual, "characteristics": found, "risk_sum": risk_sum, "basis": basis}


def advertising_component(deep, name, cfg):
    c = cfg["advertising_viability"]
    p = Parts(c)
    vm = deep.get("video_metrics") or {}
    videos = num(vm.get("selling"))
    if videos is None:
        videos = num(vm.get("total"))
    pv = c["parts"]["successful_videos"]
    s = None
    if videos is not None:
        s = 0.0 if videos <= 0 else max(0.0, min(1.0, (math.log10(videos) - math.log10(pv["zero_at"])) /
                                                 (math.log10(pv["full_at"]) - math.log10(pv["zero_at"]))))
    p.add("successful_videos", s, value=videos)
    cm = ((deep.get("concentration_metrics") or {}).get("metrics") or {})
    t3c = num((cm.get("top3_creator_revenue_share") or {}).get("value"))
    t3v = num((cm.get("top3_video_revenue_share") or {}).get("value"))
    p.add("creator_diversity", band(t3c, c["parts"]["creator_diversity"]["bands"]), value=t3c)
    p.add("creative_diversity", band(t3v, c["parts"]["creative_diversity"]["bands"]), value=t3v)
    hits = kw_hits(name, c["restricted_ad_keywords"])
    p.add("ad_restriction", None if not name else (0.0 if hits else 1.0), keywords=hits, basis="keyword_screening")
    return p.result(), hits, {"videos": videos, "top3_creator_revenue_share": t3c, "top3_video_revenue_share": t3v,
                              "restricted_ad_keywords": hits,
                              "not_scored": ["demonstration_potential", "visual_transformation", "problem_solution_clarity"]}


def compliance_component(name, category, amz, comm, ad_hits, filters_cfg, cfg):
    c = cfg["compliance_ip"]
    text = " | ".join(x for x in (name, category, (amz or {}).get("amazon_match_name")) if x)
    review = comm.get("compliance_review") if isinstance(comm.get("compliance_review"), dict) else None
    if not text and not review:
        return {"score": NA, "max": c["points"], "status": "N/A", "basis": None}, [], {"basis": None}
    found = {}
    ip = kw_hits(text, filters_cfg["risk_rules"]["trademark_ip_risk"]["keywords"])
    if ip or any(f.get("flag") == "IP_REVIEW_REQUIRED" for f in (amz or {}).get("amazon_red_flags") or []):
        found["IP_REVIEW_REQUIRED"] = ip or ["amazon IP_REVIEW_REQUIRED"]
    reg = kw_hits(text, filters_cfg["risk_rules"]["highly_regulated"]["keywords"])
    if reg:
        found["REGULATED_PRODUCT"] = reg
    for code in ("COUNTERFEIT_REVIEW_REQUIRED", "MEDICAL_CLAIM_RISK", "BRAND_DEPENDENCY"):
        h = kw_hits(text, c["keywords"][code])
        if h:
            found[code] = h
    if ad_hits:
        found["AD_POLICY_REVIEW_REQUIRED"] = ad_hits
    if review and review.get("status") == "cleared":
        s, basis = 1.0, "verified_review"
    else:
        extra = [f for f in (review or {}).get("flags", []) if f in c["penalties"]]
        for f in extra:
            found.setdefault(f, ["verified review"])
        pen = sum(c["penalties"][f] for f in found)
        s, basis = 1 - min(pen, 10) / 10, "verified_review" if review else "keyword_screening"
    flags = [{"flag": f, "evidence": "keyword screening" if basis == "keyword_screening" else "review",
              "matches": v, "note": "screening only; not a legal conclusion"} for f, v in sorted(found.items())]
    return ({"score": rnd(s * c["points"]), "max": c["points"], "status": "full", "basis": basis}, flags,
            {"basis": basis, "review": review, "findings": found})


# ================================================================== Stage 12
def bvs_confidence(econ, ship_m, sup_m, comp_m, ret_m, adv_m, comp_ip_m, cfg):
    bc = cfg["bvs_confidence"]
    kw = bc["keyword_evidence_weight"]
    have = lambda v: v is not None and v != "INSUFFICIENT_DATA"  # noqa: E731  (labels without data are not evidence)
    fr = {
        "supplier_cost": float(have(econ["product_cost"])),
        "shipping_cost": float(have(econ["supplier_shipping_cost"])),
        "delivery_time": float(have(ship_m["delivery_days_max"])),
        "supplier_data": sum(have(sup_m[k]) for k in ("supplier_count", "rating", "processing_days", "us_warehouse",
                                                      "stock", "moq")) / 6,
        "competition_data": sum(have(comp_m[k]) for k in ("similar_listings_count", "amazon_competition",
                                                          "price_alignment", "amazon_top3_brand_share_pct")) / 4 * 0.8
                            + (0.2 if comp_m["demand_basis"] != "demand_unverified" else 0.0),
        "return_risk_evidence": {"actual_refund_rate": 1.0, "keyword_screening": kw}.get(ret_m["basis"], 0.0),
        "ad_evidence": sum(have(adv_m[k]) for k in ("videos", "top3_creator_revenue_share",
                                                    "top3_video_revenue_share")) / 3,
        "compliance_evidence": {"verified_review": 1.0, "keyword_screening": kw}.get(comp_ip_m["basis"], 0.0),
    }
    total, bd = 0.0, {}
    for name, c in bc["components"].items():
        pts = c["points"] * fr[name]
        total += pts
        bd[name] = {"earned": rnd(pts), "max": c["points"],
                    "status": "full" if fr[name] == 1 else ("missing" if fr[name] == 0 else "partial")}
    score = rnd(total)
    return score, level(score, bc["levels"]), bd


# ================================================================== one product
def evaluate(deep, amz, comm, cfg, filters_cfg, comm_source=None):
    comm = comm or {}
    name, category = deep.get("product_name"), deep.get("category")
    sell, sell_src = selling_price(deep, comm, cfg)
    econ = economics(sell, comm, cfg)
    if econ["product_cost"] is None:
        # product cost may come from the primary supplier's price (still real supplier data)
        sups = comm.get("suppliers") if isinstance(comm.get("suppliers"), list) else []
        priced = sorted((num(s.get("price")), i) for i, s in enumerate(sups) if isinstance(s, dict) and num(s.get("price")) is not None)
        if priced:
            econ = economics(sell, {**comm, "product_cost": priced[0][0]}, cfg)
            econ["product_cost_source"] = "primary supplier price"
    econ["selling_price_source"] = sell_src

    gm, gm_flags = gross_margin_component(econ, cfg)
    sh, sh_flags, sh_m = shipping_component(comm, sell, name, cfg)
    sp, sp_flags, sp_m = supplier_component(comm, cfg)
    co, co_flags, co_m = competition_component(deep, amz, cfg)
    rr, rr_flags, rr_m = return_risk_component(name, comm, cfg)
    ad, ad_hits, ad_m = advertising_component(deep, name, cfg)
    ci, ci_flags, ci_m = compliance_component(name, category, amz, comm, ad_hits, filters_cfg, cfg)

    breakdown = {"gross_margin_potential": gm, "shipping_viability": sh, "supplier_viability": sp,
                 "competition_opportunity": co, "return_risk": rr, "advertising_viability": ad, "compliance_ip": ci}
    bvs = rnd(sum(num(c["score"]) or 0.0 for c in breakdown.values()))
    conf, conf_level, conf_bd = bvs_confidence(econ, sh_m, sp_m, co_m, rr_m, ad_m, ci_m, cfg)

    flags = gm_flags + sh_flags + sp_flags + co_flags + rr_flags + ci_flags
    layer = comm.get("supplier_layer") or {}
    have = {f["flag"] for f in flags}               # Step W supplier flags (evidence-based), no duplicates
    flags += [{**f, "source": "supplier_layer"} for f in layer.get("supplier_flags") or [] if f["flag"] not in have]
    missing_req = [k for k in cfg["insufficient_supplier_data"]["required"] if econ.get(k) is None]
    if missing_req:
        flags.append({"flag": "INSUFFICIENT_SUPPLIER_DATA", "missing": missing_req})
    for f in flags:
        f["severe"] = f["flag"] in cfg["severe_flags"]
    rw = cfg["bvs_confidence"]["reliability_warning"]
    warning = None
    if bvs >= rw["bvs_min"] and conf < rw["bvs_confidence_below"]:
        warning = f"HIGH BVS ({bvs}) WITH LOW BVS CONFIDENCE ({conf}): commercial evidence incomplete"

    econ_keys = ["selling_price", "product_cost", "supplier_shipping_cost", "import_duty_per_order", "landed_cost",
                 "landed_cost_note", "payment_processing_fee",
                 "platform_fee", "estimated_ad_cost_per_order", "expected_refund_cost", "expected_chargeback_cost",
                 "gross_profit_before_ads", "gross_margin_percent", "contribution_profit", "contribution_margin_percent"]
    return {
        "product_id": deep.get("product_id"), "product_name": name,
        "selling_price": econ["selling_price"], "selling_price_source": sell_src,
        "economics": {k: (NA if econ.get(k) is None else econ[k]) for k in econ_keys}
                     | {"selling_price_source": sell_src, "product_cost_source": econ.get("product_cost_source")},
        "supplier_metrics": sp_m, "shipping_metrics": sh_m, "competition_metrics": co_m,
        "return_risk_metrics": rr_m, "advertising_metrics": ad_m, "compliance_metrics": ci_m,
        "bvs": bvs, "bvs_breakdown": breakdown,
        "bvs_confidence": conf, "bvs_confidence_level": conf_level, "bvs_confidence_breakdown": conf_bd,
        "bvs_reliability_warning": warning,
        "commercial_red_flags": flags,
        "severe_flag_count": sum(f["severe"] for f in flags),
        "missing_data": sorted(k for k in econ_keys if econ.get(k) is None),
        "other_scores": {"wps": deep.get("wps"), "confidence": deep.get("confidence"),
                         "avs": (amz or {}).get("avs"), "amazon_confidence": (amz or {}).get("amazon_confidence")},
        "observation_timestamp": datetime.now(timezone.utc).isoformat(),
        "source": {"deep_analysis_file": deep.get("_file"), "amazon_file": (amz or {}).get("_file"),
                   "commercial_data_file": comm_source, "supplier_provider": cfg["supplier_source"]["provider"]},
        "config_version": cfg["version"],
        "selected_supplier_offer_id": layer.get("selected_supplier_offer_id"),
        "supplier_viability": layer.get("supplier_viability", NA if not comm.get("suppliers") else "LEGACY_INPUT"),
        "supplier_layer": layer or None,
    }


# ================================================================== IO / runner
def eligible(deep_results, cfg):
    e = cfg["eligibility"]
    out, excl = [], []
    for r in deep_results:
        w, c = num(r.get("wps")), num(r.get("confidence"))
        if r.get("status") != "ok":
            excl.append({"product_id": r.get("product_id"), "reason": "deep analysis not ok"})
        elif (r.get("source") or {}).get("discovery_status") == "FAIL":
            excl.append({"product_id": r.get("product_id"), "reason": "discovery FAIL"})
        elif w is None or w < e["minimum_wps"]:
            excl.append({"product_id": r.get("product_id"), "reason": f"WPS {r.get('wps')} < {e['minimum_wps']}"})
        elif c is None or c < e["minimum_confidence"]:
            excl.append({"product_id": r.get("product_id"), "reason": f"Confidence {r.get('confidence')} < {e['minimum_confidence']}"})
        else:
            out.append(r)
    out.sort(key=lambda r: (-r["wps"], -r["confidence"], str(r["product_id"])))
    excl += [{"product_id": r["product_id"], "reason": f"over max_products ({e['max_products']})"} for r in out[e["max_products"]:]]
    return out[: e["max_products"]], excl


def save_commercial_data(product_id, data, provider="manual", raw_dir=RAW_DIR):
    """Store supplier/commercial data as a raw observation (read-only, never overwritten)."""
    return DA.save_raw_deep(data, {"observation_timestamp": datetime.now(timezone.utc).isoformat(), "market": "US",
                                   "product_id": str(product_id), "query_type": "supplier_data",
                                   "provider": provider}, raw_dir)


def load_commercial_data(product_id, raw_dir=RAW_DIR):
    """Latest raw commercial observation for the product, or (None, None)."""
    files = sorted(Path(raw_dir).glob(f"*_{product_id}_supplier_data*.json"))
    if not files:
        return None, None
    env = json.loads(files[-1].read_text())
    return env.get("response"), str(files[-1])


def latest(dir_, pattern):
    files = sorted(Path(dir_).glob(pattern))
    return files[-1] if files else None


def run(deep_path=None, amazon_path=None, raw_dir=RAW_DIR, out_dir=OUT_DIR, cfg=None, filters_cfg=None, save=True,
        suppliers_dir=None):
    cfg = cfg or load_cfg()
    filters_cfg = filters_cfg or disc.load_yaml("filters.yaml")
    deep_path = deep_path or latest(DEEP_DIR, "deep_*.json")
    if not deep_path:
        return {"note": "no deep-analysis results found (Step M has not been run live yet)", "results": []}
    deep = json.loads(Path(deep_path).read_text())
    amazon_path = amazon_path or latest(AMAZON_DIR, "amazon_*.json")
    amz_by_id = {}
    if amazon_path:
        for a in json.loads(Path(amazon_path).read_text()).get("results", []):
            amz_by_id[a.get("product_id")] = {**a, "_file": str(amazon_path)}
    rows, excluded = eligible([{**r, "_file": str(deep_path)} for r in deep.get("results", [])], cfg)
    results = []
    import suppliers as SUP
    suppliers_dir = suppliers_dir or SUP.PROCESSED_DIR
    for d in rows:
        # Step W: real supplier offers (ranked, selected) first; legacy manual supplier_data file as fallback
        sell, _ = selling_price(d, {}, cfg)
        comm, src = SUP.commercial_data_for(d, suppliers_dir, selling_price=sell)
        if comm is None:
            comm, src = load_commercial_data(d["product_id"], raw_dir)
        results.append(evaluate(d, amz_by_id.get(d["product_id"]), comm, cfg, filters_cfg, src))
    report = {"deep_file": str(deep_path), "amazon_file": str(amazon_path) if amazon_path else None,
              "eligible": len(rows), "excluded": excluded, "results": results}
    if save and results:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path, n = out_dir / f"bvs_{ts}.json", 1
        while path.exists():
            n += 1
            path = out_dir / f"bvs_{ts}_{n}.json"
        with open(path, "x") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        report["saved"] = str(path)
    return report


def format_summary(r):
    flags = ", ".join(f["flag"] + ("!" if f["severe"] else "") for f in r["commercial_red_flags"]) or "none"
    lines = [f"{r['product_name']} ({r['product_id']})",
             f"WPS: {r['other_scores']['wps']}/100 | Confidence: {r['other_scores']['confidence']}/100 | "
             f"AVS: {r['other_scores']['avs'] if r['other_scores']['avs'] is not None else NA} | "
             f"Amazon Confidence: {r['other_scores']['amazon_confidence'] if r['other_scores']['amazon_confidence'] is not None else NA}",
             f"BVS: {r['bvs']}/100 | BVS Confidence: {r['bvs_confidence']}/100 ({r['bvs_confidence_level']})"]
    if r["bvs_reliability_warning"]:
        lines.append(f"WARNING: {r['bvs_reliability_warning']}")
    for k, c in r["bvs_breakdown"].items():
        lines.append(f"  {k:24} {c['score']!s:>6} / {c['max']}  {c['status']}")
    e = r["economics"]
    lines.append(f"  economics: price {e['selling_price']} | landed {e['landed_cost']} | gross margin "
                 f"{e['gross_margin_percent']}% | contribution {e['contribution_margin_percent']}%")
    lines.append(f"  flags (! = severe): {flags}")
    return "\n".join(lines)


def main(argv):
    if len(argv) >= 4 and argv[1] == "add-supplier":
        data = json.loads(Path(argv[3]).read_text())
        print(f"Saved raw commercial data: {save_commercial_data(argv[2], data)}")
        return 0
    r = run()
    if r.get("note"):
        print(f"NOTE: {r['note']}")
    for res in r["results"]:
        print(format_summary(res) + "\n")
    if r.get("saved"):
        print(f"Saved: {r['saved']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
