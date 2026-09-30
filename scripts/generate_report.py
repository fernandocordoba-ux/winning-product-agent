"""Final Research Report (Step P): one Markdown + JSON report from all pipeline stages.

Scores stay independent: WPS, Confidence, AVS, Amazon Confidence, BVS and BVS
Confidence are shown side by side and NEVER combined. Every section assignment
is a deterministic rule from config/report.yaml. No paid queries are made.

Inputs (latest available, read-only):
  data/processed/discovery_*.json
  data/processed/deep_analysis/deep_*.json          (all files -> history)
  data/processed/amazon_validation/amazon_*.json
  data/processed/business_viability/bvs_*.json

Outputs:
  reports/YYYY-MM-DD-winning-products.md    (never overwritten: -2, -3 ... suffix)
  reports/YYYY-MM-DD-winning-products.json  (same suffix)
  reports/latest-winning-products.md        (replaced each run)

CLI:
  python3 scripts/generate_report.py
"""
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import history as HIST  # noqa: E402  (Step Q: append-only product history)
NA = "N/A"
PROCESSED = ROOT / "data" / "processed"

WPS_METRICS = [("growth_momentum", "Growth Momentum"), ("demand", "Demand"), ("video_momentum", "Video Momentum"),
               ("creator_momentum", "Creator Momentum"), ("competition", "Competition"),
               ("margin_potential", "Margin Potential"), ("trend_stability", "Trend Stability")]
BVS_PARTS = [("gross_margin_potential", "Gross margin potential"), ("shipping_viability", "Shipping viability"),
             ("supplier_viability", "Supplier viability"), ("competition_opportunity", "Competition opportunity"),
             ("return_risk", "Return risk"), ("advertising_viability", "Advertising viability"),
             ("compliance_ip", "Compliance / IP")]


def load_cfg(path=ROOT / "config" / "report.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)["report"]


# ================================================================== formatting
def num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def fmt(v, kind=None):
    if v is None or v == NA or v == "":
        return NA
    n = num(v)
    if n is None:
        return str(v)
    if kind == "money":
        return f"${n:,.2f}"
    if kind == "int":
        return f"{n:,.0f}"
    if kind == "pct":
        return f"{n:,.2f}%"
    return f"{n:g}" if n == int(n) else f"{n:,.2f}"


def score(v, mx=100):
    return NA if num(v) is None else f"{fmt(v)}/{mx}"


def pts(m, key="points"):
    """'12/15' | 'N/A (of 5)' | 'N/A' for a breakdown entry."""
    m = m or {}
    if "max" not in m:
        return NA
    return f"{fmt(m.get(key))}/{m['max']}" if num(m.get(key)) is not None else f"N/A (of {m['max']})"


# ================================================================== secrets
def known_secrets():
    vals = [os.environ.get(k) for k in ("KALOPILOT_TOKEN", "KALODATA_API_KEY")]
    p = Path.home() / ".kalopilot" / "token"
    if p.exists():
        vals.append(p.read_text().strip())
    return [v for v in vals if v and len(v) >= 8]


def scrub(obj, patterns, secrets):
    """Drop secret-looking keys and redact known secret values anywhere."""
    if isinstance(obj, dict):
        return {k: scrub(v, patterns, secrets) for k, v in obj.items()
                if not any(p in str(k).lower() for p in patterns)}
    if isinstance(obj, list):
        return [scrub(v, patterns, secrets) for v in obj]
    if isinstance(obj, str):
        for s in secrets:
            obj = obj.replace(s, "[REDACTED]")
    return obj


# ================================================================== loading
def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def load_inputs(processed=PROCESSED):
    processed = Path(processed)
    disc_files = sorted(processed.glob("discovery_*.json"))
    discovery = _read(disc_files[-1]) if disc_files else None
    if discovery is not None:
        discovery["_file"] = str(disc_files[-1])

    def observations(sub, pattern):
        out = []
        for f in sorted((processed / sub).glob(pattern)):
            data = _read(f) or {}
            for r in data.get("results", []):
                if r.get("status", "ok") == "ok" and r.get("product_id"):
                    out.append({**r, "_file": str(f)})
        return out

    return {"discovery": discovery, "deep": observations("deep_analysis", "deep_*.json"),
            "amazon": observations("amazon_validation", "amazon_*.json"),
            "bvs": observations("business_viability", "bvs_*.json")}


def _order(r):
    return (str(r.get("observation_timestamp")), r["_file"])


def latest_by_id(obs):
    out = {}
    for r in sorted(obs, key=_order):
        out[str(r["product_id"])] = r
    return out


# ================================================================== assembling products
def _codes(flags):
    return [f["flag"] for f in flags or [] if isinstance(f, dict) and f.get("flag")]


def build_products(inputs, cfg, store=None):
    """Join all stages by product_id (fallback key only for exact Discovery identity)."""
    deep_latest = latest_by_id(inputs["deep"])
    amz_latest, bvs_latest = latest_by_id(inputs["amazon"]), latest_by_id(inputs["bvs"])
    history = {}
    for r in sorted(inputs["deep"], key=_order):
        history.setdefault(str(r["product_id"]), []).append(r)

    products, used_deep, conflicts = {}, set(), []
    disc = inputs["discovery"] or {}
    for rec in (disc.get("candidates") or []) + (disc.get("failed") or []):
        pid = rec["facts"].get("product_id")
        key = str(pid) if pid else rec["key"]
        d = deep_latest.get(str(pid)) if pid else None
        if d is None:   # deep record linked by exact discovery key (fallback identity)
            d = next((x for x in deep_latest.values()
                      if (x.get("source") or {}).get("discovery_key") == rec["key"]), None)
        if d is not None and pid and str(d["product_id"]) != str(pid):
            conflicts.append({"discovery_key": rec["key"], "deep_product_id": d["product_id"]})
            d = None                                   # low identity confidence -> do not merge
        if d is not None:
            used_deep.add(str(d["product_id"]))
        products[key] = {"discovery": rec, "deep": d}
    for pid, d in deep_latest.items():                # deep results without a Discovery record
        if pid not in used_deep and pid not in products:
            products[pid] = {"discovery": None, "deep": d}

    out = []
    for key, p in products.items():
        d, rec = p["deep"], p["discovery"]
        pid = str(d["product_id"]) if d else (rec["facts"].get("product_id") if rec else None)
        a = amz_latest.get(str(pid)) if pid else None
        b = bvs_latest.get(str(pid)) if pid else None
        hist_obs = store.observations(pid or key) if store else []
        out.append(assemble(key, pid, rec, d, a, b, history.get(str(pid), []) if pid else [], cfg, disc,
                            hist_obs, store.cfg if store else None))
    return out, conflicts


def assemble(key, pid, rec, d, a, b, hist, cfg, disc, hist_obs=None, hist_cfg=None):
    facts = (rec or {}).get("facts") or {}
    calc = (rec or {}).get("calculated") or {}
    codes = cfg["discovery_reason_codes"]
    disc_flags = [codes.get(r["rule"], r["rule"].upper()) for r in calc.get("filter_reasons") or []
                  if r.get("action") == "reject"]
    deep_flags, amz_flags = _codes((d or {}).get("red_flags")), _codes((a or {}).get("amazon_red_flags"))
    bvs_flags = (b or {}).get("commercial_red_flags") or []
    all_flags = sorted(set(disc_flags + deep_flags + amz_flags + _codes(bvs_flags)))
    significant = sorted(set(f for f in all_flags if f in cfg["significant_flags"]) |
                         {f["flag"] for f in bvs_flags if f.get("severe")})
    price = ((d or {}).get("price") or {}).get("avg")
    if price is None and facts.get("price_min") is not None and facts.get("price_max") is not None:
        price = (facts["price_min"] + facts["price_max"]) / 2
    return {
        "id": key, "product_id": pid,
        "name": (d or {}).get("product_name") or facts.get("product_name"),
        "category": (d or {}).get("category") or facts.get("category"),
        "shop": ((d or {}).get("shop") or {}).get("shop_name") or facts.get("shop_name"),
        "url": (d or {}).get("product_url") or facts.get("product_url"),
        "price": price,
        "discovery_status": calc.get("filter_status"), "discovery_flags": disc_flags,
        "wps": num((d or {}).get("wps")), "confidence": num((d or {}).get("confidence")),
        "avs": num((a or {}).get("avs")), "amazon_confidence": num((a or {}).get("amazon_confidence")),
        "bvs": num((b or {}).get("bvs")), "bvs_confidence": num((b or {}).get("bvs_confidence")),
        "trend": ((d or {}).get("trend_metrics") or {}).get("label"),
        "flags": all_flags, "significant_flags": significant,
        "growth_30d": ((d or {}).get("growth") or {}).get("growth_30d_pct") if d else facts.get("growth_30d"),
        "deep": d, "amazon": a, "bvs_record": b,
        # Step Q history store is preferred (all stages, all dates); falls back to Deep Analysis files
        "history": rows_from_observations(_hist_obs(hist_obs, hist)),
        "history_source": "history_store" if hist_obs else ("deep_analysis_files" if hist else None),
        "history_snapshot": (HIST.snapshot_from(_hist_obs(hist_obs, hist), hist_cfg or HIST.load_cfg())["snapshot"]
                             if (hist_obs or hist) else None),
        "source": traceability(rec, d, a, b, disc),
    }


def traceability(rec, d, a, b, disc):
    s = {}
    if rec:
        s["discovery"] = {"processed_file": disc.get("_file"), "raw_file": rec["source"].get("raw_file"),
                          "task_id": rec["source"].get("task_id"), "fetched_at": rec["source"].get("fetched_at"),
                          "observation_date": rec.get("observation_date")}
    for name, r in (("deep_analysis", d), ("amazon_validation", a)):
        if r:
            s[name] = {"processed_file": r.get("_file"), "raw_file": (r.get("source") or {}).get("raw_file"),
                       "task_id": (r.get("source") or {}).get("task_id"),
                       "observation_timestamp": r.get("observation_timestamp")}
    if b:
        s["business_viability"] = {"processed_file": b.get("_file"),
                                   "commercial_data_file": (b.get("source") or {}).get("commercial_data_file"),
                                   "observation_timestamp": b.get("observation_timestamp")}
    return s


# ================================================================== history
def history_rows(hist):
    rows = []
    for r in hist:
        cm, vm = r.get("creator_metrics") or {}, r.get("video_metrics") or {}
        rows.append({"date": str(r.get("observation_timestamp"))[:10], "observation_timestamp": r.get("observation_timestamp"),
                     "wps": num(r.get("wps")), "confidence": num(r.get("confidence")), "gmv": num(r.get("gmv")),
                     "units": num(r.get("units")), "creators": num(cm.get("total")), "videos": num(vm.get("total"))})
    return rows


def _hist_obs(hist_obs, deep_records):
    """History-store observations, or Deep Analysis records converted to the same observation format."""
    if hist_obs:
        return hist_obs
    return [o for o in (HIST.from_deep(r, r.get("_file")) for r in deep_records or []) if o]


def rows_from_observations(obs):
    return [{"date": o.get("observation_date"), "observation_timestamp": o.get("observation_timestamp"),
             "stage": o.get("source_stage"), "wps": HIST.metric(o, "wps"), "confidence": HIST.metric(o, "confidence"),
             "gmv": HIST.metric(o, "gmv"), "units": HIST.metric(o, "units"), "creators": HIST.metric(o, "creators"),
             "videos": HIST.metric(o, "videos")} for o in obs]


def direction(rows, field, cfg, points=False):
    h = cfg["history"]
    vals = [r[field] for r in rows if r[field] is not None]
    if len(vals) < h["min_observations"]:
        return "INSUFFICIENT_HISTORY"
    prev, last = vals[-2], vals[-1]
    if points:
        diff, t = last - prev, h["wps_direction_threshold_points"]
    else:
        if prev == 0:
            return "↑" if last > 0 else "→"
        diff, t = (last - prev) / abs(prev) * 100, h["direction_threshold_pct"]
    return "↑" if diff > t else ("↓" if diff < -t else "→")


def change(rows, field, pct=False):
    vals = [r[field] for r in rows if r[field] is not None]
    if len(vals) < 2:
        return None
    prev, last = vals[-2], vals[-1]
    if pct:
        return None if prev == 0 else round((last - prev) / abs(prev) * 100, 2)
    return round(last - prev, 2)


# ================================================================== classification
def classify(products, cfg):
    t, w = cfg["top"], cfg["watchlist"]
    rejected, eligible, rest = [], [], []
    for p in products:
        reasons = [c for c in cfg["rejection_priority"] if c in p["flags"]]
        low_wps = p["wps"] is not None and p["wps"] < cfg["rejected_min_wps"]
        if p["discovery_status"] == "FAIL" or reasons or low_wps:
            p["section"] = "REJECTED"
            p["primary_rejection_reason"] = reasons[0] if reasons else ("LOW_WPS" if low_wps else "DISCOVERY_FAIL")
            rejected.append(p)
        elif p["wps"] is not None and p["wps"] >= t["minimum_wps"] and \
                p["confidence"] is not None and p["confidence"] >= t["minimum_confidence"]:
            eligible.append(p)
        else:
            rest.append(p)

    def neg(v):
        return (1, 0) if v is None else (0, -v)
    eligible.sort(key=lambda p: (-p["wps"], -p["confidence"], neg(p["bvs"]), neg(p["avs"]), str(p["id"])))
    top, overflow = eligible[: t["max_products"]], eligible[t["max_products"]:]
    for i, p in enumerate(top, 1):
        p["rank"] = i
    for p in top + overflow + rest:
        p["watch_reasons"] = watch_reasons(p, cfg)
    for p in overflow:
        p["watch_reasons"].insert(0, f"ranked below Top {t['max_products']}")
    for p in top:
        p["section"] = "TOP"
        p["tier"] = ("HIGH-CONFIDENCE CANDIDATE" if p["confidence"] >= cfg["high_confidence"]["minimum_confidence"]
                     and not p["watch_reasons"] and not p["significant_flags"] else "CANDIDATE")
    watch = [p for p in overflow + rest if p["watch_reasons"]]
    watch.sort(key=lambda p: (p["wps"] is None, -(p["wps"] or 0), -(num(p["growth_30d"]) or -1e18), str(p["id"])))
    for p in watch:
        p["section"] = "WATCHLIST"
    rejected.sort(key=lambda p: (p["wps"] is None, -(p["wps"] or 0), str(p["id"])))
    shown = watch[: w["max_products"]]
    return top, shown, len(watch) - len(shown), rejected


def watch_reasons(p, cfg):
    w, t, r = cfg["watchlist"], cfg["top"], []
    if p["deep"] is None:
        if w["include_awaiting_deep_analysis"] and p["discovery_status"] in ("PASS", "REVIEW"):
            r.append(f"awaiting deep analysis (Discovery {p['discovery_status']}; WPS not calculated yet)")
        return r
    if p["wps"] is not None and p["wps"] >= w["promising_wps_min"] and \
            (p["confidence"] is None or p["confidence"] < t["minimum_confidence"]):
        r.append(f"promising WPS {fmt(p['wps'])} but Confidence {fmt(p['confidence'])} < {t['minimum_confidence']}")
    if p["wps"] is not None and p["wps"] < t["minimum_wps"]:
        r.append(f"WPS {fmt(p['wps'])} below Top threshold {t['minimum_wps']}")
    g = num(p["growth_30d"])
    if g is not None and g >= w["early_growth_min_pct"] and len(p["history"]) < cfg["history"]["min_observations"]:
        r.append(f"early growth ({fmt(g, 'pct')}) but only {len(p['history'])} observation(s)")
    if p["bvs_record"] is None or "INSUFFICIENT_SUPPLIER_DATA" in p["flags"]:
        r.append("incomplete supplier data (BVS missing or INSUFFICIENT_SUPPLIER_DATA)")
    a = p["amazon"]
    if a is None:
        r.append("Amazon validation not available")
    elif a.get("amazon_match_status") != "MATCHED" or (num(a.get("amazon_confidence")) or 0) < 60:
        r.append(f"uncertain Amazon validation ({a.get('amazon_match_status')}, Amazon Confidence "
                 f"{fmt(a.get('amazon_confidence'))})")
    if len(p["significant_flags"]) == w["significant_flags_max"]:
        r.append(f"one significant unresolved red flag: {p['significant_flags'][0]}")
    return r


def emerging(products, cfg):
    e, out = cfg["emerging"], []
    for p in products:
        rows = p["history"]
        if len(rows) < e["min_observations"] or p.get("section") == "REJECTED":
            continue
        dw, dg = change(rows, "wps"), change(rows, "gmv", pct=True)
        if dw is None or dg is None:
            continue
        if dw >= e["wps_change_min_points"] and dg >= e["gmv_change_min_pct"] and p["trend"] in e["trend_labels"] \
                and (p["confidence"] or 0) >= e["minimum_confidence"]:
            wps_vals = [r["wps"] for r in rows if r["wps"] is not None]
            out.append({"id": p["id"], "product_id": p["product_id"], "name": p["name"], "current_wps": wps_vals[-1],
                        "previous_wps": wps_vals[-2], "wps_change": dw, "gmv_change_pct": dg,
                        "creator_change": change(rows, "creators"), "video_change": change(rows, "videos"),
                        "trend": p["trend"], "confidence": p["confidence"], "observations": len(rows)})
    return sorted(out, key=lambda x: (-x["wps_change"], str(x["id"])))


# ================================================================== interpretation (evidence only)
def interpret(p, cfg):
    d, a, b = p["deep"] or {}, p["amazon"], p["bvs_record"]
    passed, fail, verify, steps = [], [], [], []
    passed.append(f"WPS {fmt(p['wps'])}/100 ≥ {cfg['top']['minimum_wps']} and Confidence {fmt(p['confidence'])}/100 "
                  f"≥ {cfg['top']['minimum_confidence']} (CALCULATION)")
    if num(p["growth_30d"]) is not None:
        passed.append(f"30D revenue growth {fmt(p['growth_30d'], 'pct')} (FACT, KaloData)")
    if num(d.get("gmv")) is not None:
        passed.append(f"30D GMV {fmt(d.get('gmv'), 'money')}, units {fmt(d.get('units'), 'int')} (FACT, KaloData)")
    if p["trend"]:
        passed.append(f"Trend classification: {p['trend']} (CALCULATION)")
    for k, label in WPS_METRICS:
        m = (d.get("wps_breakdown") or {}).get(k) or {}
        if num(m.get("points")) is not None and m["points"] >= 0.8 * m["max"]:
            passed.append(f"Strong {label}: {fmt(m['points'])}/{m['max']}")
        elif num(m.get("points")) is not None and m["points"] < 0.4 * m["max"]:
            fail.append(f"Weak {label}: {fmt(m['points'])}/{m['max']}")
        elif m.get("points") == NA:
            verify.append(f"WPS {label} is N/A (data missing)")
    if a and a.get("cross_platform_demand") in ("STRONG", "MODERATE"):
        passed.append(f"Amazon cross-platform demand {a['cross_platform_demand']} (CALCULATION)")
    if b and num(b.get("bvs")) is not None and b["bvs"] >= 70:
        passed.append(f"BVS {fmt(b['bvs'])}/100 (BVS Confidence {fmt(b.get('bvs_confidence'))})")

    fail += [f"Red flag: {f}" for f in p["flags"]]
    cm = ((d.get("concentration_metrics") or {}).get("metrics") or {})
    top_c = num((cm.get("top_creator_revenue_share") or {}).get("value"))
    if top_c is not None and top_c > 50:
        fail.append(f"Top creator drives {fmt(top_c, 'pct')} of revenue (concentration)")
    if a and num(a.get("avs")) is not None and a["avs"] < 50:
        fail.append(f"Weak Amazon validation: AVS {fmt(a['avs'])}/100")
    if b:
        e = b.get("economics") or {}
        if num(e.get("contribution_margin_percent")) is not None and e["contribution_margin_percent"] < 10:
            fail.append(f"Thin contribution margin: {fmt(e['contribution_margin_percent'], 'pct')}")
        if b.get("bvs_reliability_warning"):
            fail.append(b["bvs_reliability_warning"])

    verify += [f"TikTok/KaloData field missing: {m}" for m in d.get("missing_data") or []]
    if a is None:
        verify.append("Amazon evidence (Step N not run for this product)")
    else:
        verify += [f"Amazon field missing: {m}" for m in a.get("missing_data") or []]
    if b is None:
        verify.append("Commercial evidence (BVS not calculated)")
    else:
        verify += [f"Economics missing: {m}" for m in b.get("missing_data") or []]
    if len(p["history"]) < cfg["history"]["min_observations"]:
        verify.append("Historical observations (only one snapshot; momentum over time unverified)")

    if b is None or "INSUFFICIENT_SUPPLIER_DATA" in p["flags"]:
        steps += ["supplier sourcing", "shipping quote"]
    if a is None:
        steps.append("Amazon validation (Step N)")
    if any(f in p["flags"] for f in ("IP_REVIEW_REQUIRED", "COUNTERFEIT_REVIEW_REQUIRED", "BRAND_DEPENDENCY")):
        steps.append("IP review")
    if any(f in p["flags"] for f in ("REGULATED_PRODUCT", "REGULATED_REVIEW_REQUIRED", "AD_POLICY_REVIEW_REQUIRED",
                                     "MEDICAL_CLAIM_RISK")):
        steps.append("compliance / ad-policy review")
    steps += ["Meta Ad Library research", "Shopify competitor research", "creative research"]
    if b and num((b.get("economics") or {}).get("contribution_margin_percent")) is not None:
        steps += ["margin confirmation", "sample order"]
    return {"why_it_passed": passed,
            "why_it_could_fail": fail or ["No red flags or weak metrics found in available data"],
            "what_we_still_need_to_verify": verify or ["Nothing missing in the available data sources"],
            "next_validation_steps": list(dict.fromkeys(steps)),
            "note": "Research candidate only. This is not a launch recommendation."}


# ================================================================== data quality
def data_quality(products, cfg):
    scored = [p for p in products if p["wps"] is not None]
    n = len(scored)

    def pct(k):
        return None if n == 0 else round(k / n * 100, 1)
    dq = cfg["data_quality"]
    missing = Counter()
    for p in scored:
        missing.update((p["deep"] or {}).get("missing_data") or [])
        missing.update(f"amazon:{m}" for m in (p["amazon"] or {}).get("missing_data") or [])
        missing.update(f"economics:{m}" for m in (p["bvs_record"] or {}).get("missing_data") or [])

    def econ(p, k):
        return ((p["bvs_record"] or {}).get("economics") or {}).get(k) not in (None, NA) if p["bvs_record"] else False
    return {
        "products_with_wps": n,
        "pct_complete_tiktok_data": pct(sum((p["confidence"] or 0) >= dq["complete_tiktok_confidence_min"] for p in scored)),
        "pct_with_amazon_data": pct(sum(bool(p["amazon"]) and p["amazon"].get("amazon_match_status") == "MATCHED"
                                        for p in scored)),
        "pct_with_supplier_data": pct(sum(econ(p, "product_cost") for p in scored)),
        "pct_complete_economics": pct(sum(econ(p, "contribution_margin_percent") for p in scored)),
        "pct_with_history": pct(sum(len(p["history"]) >= cfg["history"]["min_observations"] for p in scored)),
        "most_common_missing_fields": [{"field": f, "products": c} for f, c in missing.most_common(dq["top_missing_fields"])],
    }


# ================================================================== build report data
def ranking_logic_text(cfg):
    t = cfg["top"]
    return (f"Top section: products with WPS ≥ {t['minimum_wps']} and Confidence ≥ {t['minimum_confidence']} that are not "
            f"rejected, sorted by 1) WPS desc, 2) Confidence desc, 3) BVS desc (N/A last), 4) AVS desc (N/A last), "
            f"5) product_id asc; max {t['max_products']}. No combined score is used.")


def build_report(inputs, cfg, now=None, store=None):
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(cfg["timezone"]))
    products, conflicts = build_products(inputs, cfg, store)
    top, watch, watch_more, rejected = classify(products, cfg)
    for p in top:
        p["interpretation"] = interpret(p, cfg)
    em = emerging(products, cfg)
    ds = (inputs["discovery"] or {}).get("summary") or {}
    summary = {
        "research_date": local.strftime("%Y-%m-%d"), "generated_at": now.isoformat(), "market": cfg["market"],
        "products_discovered": ds.get("unique"),
        "products_passing_discovery": ds.get("PASS"), "products_review_discovery": ds.get("REVIEW"),
        "products_deep_analyzed": len({str(r["product_id"]) for r in inputs["deep"]}),
        "products_amazon_validated": len({str(r["product_id"]) for r in inputs["amazon"]}),
        "products_with_bvs": len({str(r["product_id"]) for r in inputs["bvs"]}),
        "top_count": len(top),
        "high_confidence_candidates": [p["name"] for p in top if p["tier"] == "HIGH-CONFIDENCE CANDIDATE"],
        "emerging_candidates": [e["name"] for e in em],
        "watchlist_count": len(watch) + watch_more, "rejected_count": len(rejected),
        "identity_conflicts": conflicts,
    }
    return {"summary": summary, "top": top, "emerging": em, "watch": watch, "watch_more": watch_more,
            "rejected": rejected, "data_quality": data_quality(products, cfg), "ranking_logic": ranking_logic_text(cfg)}


# ================================================================== rendering
def top_card(p, cfg):
    d, a, b = p["deep"] or {}, p["amazon"] or {}, p["bvs_record"] or {}
    cm, vm = d.get("creator_metrics") or {}, d.get("video_metrics") or {}
    comp = d.get("competition_metrics") or {}
    conc = ((d.get("concentration_metrics") or {}).get("metrics") or {})

    def cv(k):
        return (conc.get(k) or {}).get("value")
    wb, e, bb, rows = d.get("wps_breakdown") or {}, b.get("economics") or {}, b.get("bvs_breakdown") or {}, p["history"]
    L = [f"### {p['rank']}. {p['name'] or NA}  —  {p['tier']}", "",
         "**Product**", "",
         f"- Product ID: `{p['product_id'] or p['id']}` · Category: {p['category'] or NA} · Shop: {p['shop'] or NA}",
         f"- URL: {p['url'] or NA}", f"- Current price (avg): {fmt(p['price'], 'money')}", "",
         "**TikTok / KaloData** (FACT)", "",
         "| 30D GMV | 30D units | Growth | Creators | Selling creators | Videos | Shops | Similar listings | Trend |",
         "|---|---|---|---|---|---|---|---|---|",
         f"| {fmt(d.get('gmv'), 'money')} | {fmt(d.get('units'), 'int')} | {fmt(p['growth_30d'], 'pct')} | "
         f"{fmt(cm.get('total'), 'int')} | {fmt(cm.get('selling'), 'int')} | {fmt(vm.get('total'), 'int')} | "
         f"{fmt(comp.get('shop_count'), 'int')} | {fmt(comp.get('similar_listings_count'), 'int')} | {p['trend'] or NA} |", "",
         "**WPS** (CALCULATION)", "", "| Metric | Points |", "|---|---|"]
    L += [f"| {label} | {pts(wb.get(k))} |" for k, label in WPS_METRICS]
    L += [f"| **Total WPS** | **{score(p['wps'])}** |", "",
          f"**Confidence:** {score(p['confidence'])} ({d.get('confidence_level') or NA}). "
          f"Missing data affecting it: {', '.join(d.get('missing_data') or []) or 'none'}", "",
          f"**Concentration:** top creator {fmt(cv('top_creator_revenue_share'), 'pct')} · top 3 creators "
          f"{fmt(cv('top3_creator_revenue_share'), 'pct')} · top video {fmt(cv('top_video_revenue_share'), 'pct')} · "
          f"top 3 videos {fmt(cv('top3_video_revenue_share'), 'pct')}", ""]
    if p["amazon"]:
        L += ["**Amazon**", "",
              f"- Match: {a.get('amazon_match_status') or NA} ({a.get('amazon_match_class') or NA}), "
              f"match confidence {score(a.get('amazon_match_confidence'))}",
              f"- Price {fmt(a.get('amazon_price'), 'money')} · rating {fmt(a.get('amazon_rating'))} · reviews "
              f"{fmt(a.get('amazon_review_count'), 'int')} · BSR {fmt(a.get('amazon_bsr'), 'int')} · demand "
              f"{fmt(a.get('amazon_demand_strength'))}",
              f"- Competition {a.get('amazon_competition') or NA} · price alignment {a.get('price_alignment') or NA} · "
              f"cross-platform demand {a.get('cross_platform_demand') or NA}",
              f"- **AVS {score(a.get('avs'))}** · **Amazon Confidence {score(a.get('amazon_confidence'))}** · flags: "
              f"{', '.join(_codes(a.get('amazon_red_flags'))) or 'none'}", ""]
    else:
        L += ["**Amazon:** N/A (not validated)", ""]
    if p["bvs_record"]:
        L += ["**Business viability**", "",
              f"- Selling price {fmt(e.get('selling_price'), 'money')} · product cost {fmt(e.get('product_cost'), 'money')} · "
              f"shipping {fmt(e.get('supplier_shipping_cost'), 'money')} · landed {fmt(e.get('landed_cost'), 'money')}",
              f"- Gross margin {fmt(e.get('gross_margin_percent'), 'pct')} · contribution margin "
              f"{fmt(e.get('contribution_margin_percent'), 'pct')}",
              "- " + " · ".join(f"{label} {pts(bb.get(k), 'score')}" for k, label in BVS_PARTS),
              f"- **BVS {score(b.get('bvs'))}** · **BVS Confidence {score(b.get('bvs_confidence'))}**"
              + (f" · ⚠ {b['bvs_reliability_warning']}" if b.get("bvs_reliability_warning") else ""), ""]
    else:
        L += ["**Business viability:** N/A (BVS not calculated)", ""]
    L += history_block(p, rows, cfg)
    it = p["interpretation"]
    for title, key in (("Why it passed", "why_it_passed"), ("Why it could fail", "why_it_could_fail"),
                       ("What we still need to verify", "what_we_still_need_to_verify"),
                       ("Next validation step", "next_validation_steps")):
        L += [f"**{title}**", ""] + [f"- {x}" for x in it[key]] + [""]
    L += [f"_{it['note']}_", "", "<details><summary>Sources</summary>", "", "```json",
          json.dumps(p["source"], indent=2), "```", "", "</details>", "", "---", ""]
    return L


def history_block(p, rows, cfg):
    """Step Q history section. Arrows only from >= 2 observations; else INSUFFICIENT_HISTORY."""
    snap = p.get("history_snapshot")
    L = ["**History**", ""]
    if snap is None and not rows:
        return L + ["INSUFFICIENT_HISTORY (no observations stored yet)", ""]
    if snap is not None:
        L.append(f"First seen {snap['first_seen_date']} · last seen {snap['last_seen_date']} · observations "
                 f"{snap['observation_count']} · days tracked {snap['days_observed']} · tracking age "
                 f"{snap['product_tracking_age_days']} day(s) _(time we have tracked it, not the marketplace listing age)_")
        L.append("")
    if len(rows) < cfg["history"]["min_observations"]:
        return L + [f"INSUFFICIENT_HISTORY ({len(rows)} observation(s); no change or direction is inferred)", ""]
    s = snap or {}
    def cell(k):
        return fmt(s.get(k)) if snap else NA
    L += ["| Metric | Current | Previous | Change | % change / direction |", "|---|---|---|---|---|",
          f"| WPS | {cell('latest_wps')} | {cell('previous_wps')} | {cell('wps_change')} | {direction(rows, 'wps', cfg, True)} |",
          f"| GMV | {fmt(s.get('latest_gmv'), 'money')} | {fmt(s.get('previous_gmv'), 'money')} | "
          f"{fmt(s.get('gmv_change'), 'money')} | {fmt(s.get('gmv_percent_change'), 'pct')} {direction(rows, 'gmv', cfg)} |",
          f"| Creators | {cell('latest_creators')} | {cell('previous_creators')} | {cell('creator_change')} | "
          f"{direction(rows, 'creators', cfg)} |",
          f"| Videos | {cell('latest_videos')} | {cell('previous_videos')} | {cell('video_change')} | "
          f"{direction(rows, 'videos', cfg)} |", ""]
    if snap:
        L += [f"Trend (GMV): 7D {snap['short_trend']} · 14D {snap['medium_trend']} · 30D {snap['long_trend']} · "
              f"Volatility: {snap['volatility_level']}", ""]
    L += ["| Date | Stage | WPS | Confidence | GMV | Units | Creators | Videos |", "|---|---|---|---|---|---|---|---|"]
    L += [f"| {r['date']} | {r.get('stage') or 'deep_analysis'} | {fmt(r['wps'])} | {fmt(r['confidence'])} | "
          f"{fmt(r['gmv'], 'money')} | {fmt(r['units'], 'int')} | {fmt(r['creators'], 'int')} | {fmt(r['videos'], 'int')} |"
          for r in rows[-10:]]
    return L + [""]


def render_markdown(r, cfg):
    s = r["summary"]
    L = [f"# Winning Product Research — {s['research_date']}", "",
         f"Market: **{s['market']}** · Generated: {s['generated_at']} · Report version: {cfg['version']}", "",
         "> Scores are independent and never combined: **WPS** (TikTok opportunity), **Confidence** (TikTok evidence), "
         "**AVS** (Amazon validation), **Amazon Confidence**, **BVS** (business viability), **BVS Confidence**. "
         "Products listed here are research candidates, not launch recommendations.", "",
         "## Executive summary", "",
         "| Metric | Value |", "|---|---|",
         f"| Products discovered | {fmt(s['products_discovered'], 'int')} |",
         f"| Passing Discovery (PASS / REVIEW) | {fmt(s['products_passing_discovery'], 'int')} / "
         f"{fmt(s['products_review_discovery'], 'int')} |",
         f"| Deep-analyzed | {s['products_deep_analyzed']} |",
         f"| Amazon-validated | {s['products_amazon_validated']} |",
         f"| With BVS | {s['products_with_bvs']} |",
         f"| Top section | {s['top_count']} |", "",
         f"- **HIGH-CONFIDENCE CANDIDATES:** {', '.join(s['high_confidence_candidates']) or 'none'}",
         f"- **EMERGING CANDIDATES:** {', '.join(s['emerging_candidates']) or 'none (needs ≥ 2 observations per product)'}",
         f"- **WATCHLIST:** {s['watchlist_count']}",
         f"- **REJECTED / HIGH-RISK:** {s['rejected_count']}", ""]
    if s["identity_conflicts"]:
        L += [f"- Identity conflicts not merged: {len(s['identity_conflicts'])}", ""]
    L += ["## Top products", "", f"_Ranking: {r['ranking_logic']}_", ""]
    if r["top"]:
        L += ["| Rank | Product | Category | Price | WPS | Confidence | AVS | Amazon Conf. | BVS | BVS Conf. | Trend | Key red flags |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for p in r["top"]:
            L.append(f"| {p['rank']} | {p['name'] or NA} | {p['category'] or NA} | {fmt(p['price'], 'money')} | "
                     f"{fmt(p['wps'])} | {fmt(p['confidence'])} | {fmt(p['avs'])} | {fmt(p['amazon_confidence'])} | "
                     f"{fmt(p['bvs'])} | {fmt(p['bvs_confidence'])} | {p['trend'] or NA} | "
                     f"{', '.join(p['significant_flags'][:3]) or 'none'} |")
        L += ["", "## Product details", ""]
        for p in r["top"]:
            L += top_card(p, cfg)
    else:
        L += ["No product currently meets the Top criteria.", ""]
    L += ["## Emerging products", ""]
    if r["emerging"]:
        L += ["| Product | WPS | Prev. WPS | Δ WPS | Δ GMV | Δ creators | Δ videos | Trend | Confidence |",
              "|---|---|---|---|---|---|---|---|---|"]
        L += [f"| {e['name']} | {fmt(e['current_wps'])} | {fmt(e['previous_wps'])} | {fmt(e['wps_change'])} | "
              f"{fmt(e['gmv_change_pct'], 'pct')} | {fmt(e['creator_change'])} | {fmt(e['video_change'])} | "
              f"{e['trend']} | {fmt(e['confidence'])} |" for e in r["emerging"]]
    else:
        L.append(f"None. Emerging status requires at least {cfg['emerging']['min_observations']} observations of the "
                 "same product (never inferred from one snapshot).")
    L += ["", "## Watchlist", ""]
    if r["watch"]:
        L += ["| Product | WPS | Confidence | Why it is on watch |", "|---|---|---|---|"]
        L += [f"| {p['name'] or NA} | {fmt(p['wps'])} | {fmt(p['confidence'])} | {'; '.join(p['watch_reasons'])} |"
              for p in r["watch"]]
        if r["watch_more"]:
            L += ["", f"_+{r['watch_more']} more on watch (see JSON)._"]
    else:
        L.append("None.")
    L += ["", "## Rejected / high-risk products", ""]
    if r["rejected"]:
        L += ["| Product | WPS | Primary reason | Red flags |", "|---|---|---|---|"]
        L += [f"| {p['name'] or NA} | {fmt(p['wps'])} | {p['primary_rejection_reason']} | {', '.join(p['flags']) or 'none'} |"
              for p in r["rejected"]]
    else:
        L.append("None.")
    dq = r["data_quality"]
    L += ["", "## Data quality", "", f"Base: {dq['products_with_wps']} product(s) with WPS.", "",
          "| Measure | % of products |", "|---|---|",
          f"| Complete TikTok data | {fmt(dq['pct_complete_tiktok_data'], 'pct')} |",
          f"| Amazon data (matched) | {fmt(dq['pct_with_amazon_data'], 'pct')} |",
          f"| Supplier data | {fmt(dq['pct_with_supplier_data'], 'pct')} |",
          f"| Complete economics | {fmt(dq['pct_complete_economics'], 'pct')} |",
          f"| Historical observations (≥ {cfg['history']['min_observations']}) | {fmt(dq['pct_with_history'], 'pct')} |", "",
          "Most common missing fields: " + (", ".join(f"{m['field']} ({m['products']})"
                                                       for m in dq["most_common_missing_fields"]) or "none"), ""]
    return "\n".join(L)


def product_json(p):
    keep = {k: p.get(k) for k in ("rank", "tier", "section", "id", "product_id", "name", "category", "shop", "url", "price",
                                  "discovery_status", "wps", "confidence", "avs", "amazon_confidence", "bvs",
                                  "bvs_confidence", "trend", "growth_30d", "flags", "significant_flags", "watch_reasons",
                                  "primary_rejection_reason", "interpretation", "history", "history_source",
                                  "history_snapshot", "source")}
    d, a, b = p.get("deep") or {}, p.get("amazon") or {}, p.get("bvs_record") or {}
    keep["wps_breakdown"] = d.get("wps_breakdown")
    keep["confidence_breakdown"] = d.get("confidence_breakdown")
    keep["concentration"] = (d.get("concentration_metrics") or {}).get("metrics")
    keep["tiktok"] = {k: d.get(k) for k in ("gmv", "units", "growth", "creator_metrics", "video_metrics",
                                             "competition_metrics", "trend_metrics", "missing_data")} if d else None
    keep["amazon_detail"] = {k: a.get(k) for k in ("amazon_match_status", "amazon_match_class", "amazon_match_confidence",
                                                    "amazon_price", "amazon_rating", "amazon_review_count", "amazon_bsr",
                                                    "amazon_competition", "price_alignment", "cross_platform_demand",
                                                    "avs_breakdown", "amazon_red_flags", "missing_data")} if a else None
    keep["business_viability"] = {k: b.get(k) for k in ("economics", "bvs_breakdown", "bvs_confidence_breakdown",
                                                         "commercial_red_flags", "bvs_reliability_warning",
                                                         "missing_data")} if b else None
    return keep


def build_json(r, cfg):
    return {"report_metadata": {**r["summary"], "report_version": cfg["version"],
                                "ranking_logic": r["ranking_logic"], "scores_combined": False},
            "top_products": [product_json(p) for p in r["top"]],
            "emerging_products": r["emerging"],
            "watchlist": [product_json(p) for p in r["watch"]],
            "watchlist_not_shown": r["watch_more"],
            "rejected_products": [product_json(p) for p in r["rejected"]],
            "data_quality": r["data_quality"]}


# ================================================================== writing
def dated_paths(out_dir, date):
    n = 1
    while True:
        suffix = "" if n == 1 else f"-{n}"
        md = out_dir / f"{date}-winning-products{suffix}.md"
        js = out_dir / f"{date}-winning-products{suffix}.json"
        if not md.exists() and not js.exists():
            return md, js
        n += 1


def generate(processed=PROCESSED, out_dir=None, cfg=None, now=None, secrets=None, history_dir=None):
    cfg = cfg or load_cfg()
    out_dir = Path(out_dir or ROOT / cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    store = HIST.HistoryStore(history_dir) if history_dir else HIST.HistoryStore()
    store = store if store.identities() else None      # empty history -> fall back to Deep Analysis files
    report = build_report(load_inputs(processed), cfg, now, store)
    secrets = known_secrets() if secrets is None else secrets
    pats = [p.lower() for p in cfg["secret_key_patterns"]]
    md = scrub(render_markdown(report, cfg), pats, secrets)
    js = scrub(build_json(report, cfg), pats, secrets)
    md_path, js_path = dated_paths(out_dir, report["summary"]["research_date"])
    with open(md_path, "x") as f:                   # dated report: never overwritten
        f.write(md)
    with open(js_path, "x") as f:
        json.dump(js, f, ensure_ascii=False, indent=2)
    latest = out_dir / "latest-winning-products.md"
    latest.write_text(md)                           # replaced each run
    return {"markdown": md_path, "json": js_path, "latest": latest, "report": report}


def main(argv):
    r = generate()
    s = r["report"]["summary"]
    print(f"Report: {r['markdown']}\nJSON:   {r['json']}\nLatest: {r['latest']}")
    print(f"Top: {s['top_count']} | watchlist: {s['watchlist_count']} | rejected: {s['rejected_count']} | "
          f"deep-analyzed: {s['products_deep_analyzed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
