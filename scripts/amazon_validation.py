"""Amazon Validation (Step N): cross-validate TikTok Shop products against Amazon US.

Produces, per product, SEPARATE from WPS and TikTok Confidence:
  amazon_match_confidence (0-100) + class, AVS (0-100) + breakdown,
  Amazon Confidence (0-100) + breakdown, signals and Amazon red flags.
All rules/thresholds: config/amazon_validation.yaml. Nothing is estimated.

Pipeline:
  latest data/processed/deep_analysis/deep_*.json (results with status ok)
    -> select_eligible()   WPS >= min AND Confidence >= min, discovery FAIL excluded, max 20
    -> plan()              query per product, cache hits, cost          (FREE)
    -> [--live] balance check -> provider query -> raw (read-only) data/raw/amazon_validation/
    -> validate()          match -> Amazon data -> signals -> AVS -> Amazon Confidence -> flags
    -> save_results()      data/processed/amazon_validation/amazon_<ts>.json (new file)

CLI:
  python3 scripts/amazon_validation.py            # DRY-RUN (default), no credits
  python3 scripts/amazon_validation.py --live     # SPENDS CREDITS (only after user OK)
"""
import copy
import json
import math
import re
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
RAW_DIR = ROOT / "data" / "raw" / "amazon_validation"
OUT_DIR = ROOT / "data" / "processed" / "amazon_validation"
DEEP_DIR = ROOT / "data" / "processed" / "deep_analysis"

ATTR_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*-?\s*(in\s*-?\s*1|pack|pcs|pc|piece|pieces|ft|feet|foot|inch|inches|in|oz|ml|l|lb|lbs|"
    r"tier|tiers|layer|layers|count|ct|w|v|mah|qt|cm|mm|m|gal|gallon)\b", re.I)
UNIT_ALIASES = {"feet": "ft", "foot": "ft", "inches": "in", "inch": "in", "pcs": "pack", "pc": "pack",
                "piece": "pack", "pieces": "pack", "lbs": "lb", "tiers": "tier", "layers": "layer",
                "ct": "count", "gallon": "gal"}


def load_cfg(path=ROOT / "config" / "amazon_validation.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)["amazon_validation"]


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _round(x, p=2):
    return float(Decimal(repr(x)).quantize(Decimal(1).scaleb(-p), rounding=ROUND_HALF_UP))


def _clamp(s):
    return max(0.0, min(1.0, s))


def _linear(x, z, f):
    return _clamp((x - z) / (f - z))


def _log10(x, z, f):
    if x <= 0:
        return 0.0
    return _clamp((math.log10(x) - math.log10(z)) / (math.log10(f) - math.log10(z)))


def _level(score, levels):
    for name, t in sorted(levels.items(), key=lambda kv: -kv[1]):
        if score >= t:
            return name
    return None


# ================================================================== Stage 1
def latest_deep(deep_dir=DEEP_DIR):
    files = sorted(Path(deep_dir).glob("deep_*.json"))
    return files[-1] if files else None


def select_eligible(deep_results, cfg):
    """Deterministic eligibility + order. Returns (eligible, excluded)."""
    eligible, excluded = [], []
    for r in deep_results:
        pid = r.get("product_id")
        if r.get("status") != "ok":
            excluded.append({"product_id": pid, "reason": "deep analysis not ok"})
            continue
        dstatus = (r.get("source") or {}).get("discovery_status")
        wps, conf = _num(r.get("wps")), _num(r.get("confidence"))
        if dstatus in cfg["excluded_discovery_statuses"]:
            excluded.append({"product_id": pid, "reason": f"discovery status {dstatus}"})
        elif wps is None or wps < cfg["minimum_wps"]:
            excluded.append({"product_id": pid, "reason": f"WPS {r.get('wps')} < {cfg['minimum_wps']}"})
        elif conf is None or conf < cfg["minimum_confidence"]:
            excluded.append({"product_id": pid, "reason": f"Confidence {r.get('confidence')} < {cfg['minimum_confidence']}"})
        else:
            eligible.append(r)
    eligible.sort(key=lambda r: (-r["wps"], -r["confidence"], str(r["product_id"])))
    limit = cfg["max_products"]
    excluded += [{"product_id": r["product_id"], "reason": f"over max_products ({limit})"} for r in eligible[limit:]]
    return eligible[:limit], excluded


# ================================================================== Stage 2 — matching
def tokens(text, mcfg):
    if not text:
        return []
    out, seen = [], set()
    for t in re.findall(r"[a-z0-9]+", text.lower()):
        if len(t) < mcfg["min_token_length"] or t in mcfg["stopwords"] or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out[: mcfg["max_title_tokens"]]


def attr_tokens(text):
    if not text:
        return set()
    out = set()
    for n, unit in ATTR_RE.findall(text):
        u = re.sub(r"[\s-]", "", unit.lower())
        u = "in1" if u.startswith("in") and u.endswith("1") and len(u) <= 3 else UNIT_ALIASES.get(u, u)
        n = str(int(float(n))) if float(n).is_integer() else n
        out.add(f"{n}{u}")
    return out


def overlap(a, b):
    a, b = set(a), set(b)
    return len(a & b) / min(len(a), len(b)) if a and b else None


def _norm_brand(b):
    return re.sub(r"[^a-z0-9]", "", b.lower()) if isinstance(b, str) and b.strip() else None


def match_score(tiktok, cand, cfg):
    """Deterministic match confidence 0-100 with component detail."""
    m = cfg["matching"]
    comps, detail = m["components"], {}
    earned, possible = 0.0, 0.0

    # name
    ov = overlap(tokens(tiktok["name"], m), tokens(cand.get("title"), m))
    s = _linear(ov, comps["name_similarity"]["zero_at"], comps["name_similarity"]["full_at"]) if ov is not None else 0.0
    detail["name_similarity"] = {"overlap": None if ov is None else _round(ov, 4), "s": s}
    earned += s * comps["name_similarity"]["points"]
    possible += comps["name_similarity"]["points"]

    # brand (unknown on either side -> 0, never assumed)
    tb, ab = _norm_brand(tiktok.get("brand")), _norm_brand(cand.get("brand"))
    s = 1.0 if tb and ab and tb == ab else 0.0
    detail["brand"] = {"tiktok": tb, "amazon": ab, "s": s}
    earned += s * comps["brand"]["points"]
    possible += comps["brand"]["points"]

    # category
    ov = overlap(tokens(tiktok.get("category"), m), tokens(cand.get("category_path"), m))
    s = _linear(ov, comps["category"]["zero_at"], comps["category"]["full_at"]) if ov is not None else 0.0
    detail["category"] = {"overlap": None if ov is None else _round(ov, 4), "s": s}
    earned += s * comps["category"]["points"]
    possible += comps["category"]["points"]

    # variant / physical attributes
    ta = attr_tokens(tiktok["name"])
    if ta:
        amazon_text = " ".join([cand.get("title") or ""] + [a for a in (cand.get("attributes") or []) if isinstance(a, str)])
        s = len(ta & attr_tokens(amazon_text)) / len(ta)
        detail["variant_attributes"] = {"tiktok": sorted(ta), "s": s}
        earned += s * comps["variant_attributes"]["points"]
        possible += comps["variant_attributes"]["points"]
    else:
        detail["variant_attributes"] = {"tiktok": [], "s": None, "note": "no attribute tokens in TikTok title; excluded"}

    score = _round(earned / possible * 100 if possible else 0.0)
    return {"score": score, "class": _level(score, m["classes"]), "components": detail}


def best_match(tiktok, candidates, cfg):
    """Highest match score; tie -> earliest candidate. Returns (index, result) or (None, None)."""
    best = (None, None)
    for i, c in enumerate(candidates):
        r = match_score(tiktok, c, cfg)
        if best[1] is None or r["score"] > best[1]["score"]:
            best = (i, r)
    return best


# ================================================================== Stage 3 — normalize provider data
CAND_FIELDS = [("title", "str"), ("url", "str"), ("asin", "id"), ("brand", "str"), ("seller", "str"),
               ("category_path", "str"), ("price", "num"), ("rating", "num"), ("review_count", "count"),
               ("bsr", "count"), ("bsr_category", "str"), ("monthly_sales_estimate", "count"),
               ("monthly_sales_source", "str"), ("listing_date", "date")]


def normalize_provider(obj):
    """Returns (candidates, search, warnings). Values parsed, never estimated."""
    w = []
    cands = []
    for i, c in enumerate(obj.get("candidates") or [] if isinstance(obj.get("candidates"), list) else []):
        if not isinstance(c, dict):
            w.append(f"candidates[{i}]: not an object")
            continue
        n = {}
        for k, kind in CAND_FIELDS:
            n[k], prob = disc.parse_value(c.get(k), kind)
            if prob:
                w.append(f"candidates[{i}].{k}: {prob}")
        n["attributes"] = [a for a in (c.get("attributes") or []) if isinstance(a, str)]
        if n["rating"] is not None and not 0 <= n["rating"] <= 5:
            w.append(f"candidates[{i}].rating out of range: {n['rating']}")
            n["rating"] = None
        cands.append(n)
    s = obj.get("search") if isinstance(obj.get("search"), dict) else {}
    search = {}
    for k, kind in (("keyword", "str"), ("comparable_listings_count", "count"),
                    ("comparable_price_min", "num"), ("comparable_price_max", "num")):
        search[k], prob = disc.parse_value(s.get(k), kind)
        if prob:
            w.append(f"search.{k}: {prob}")
    tb = s.get("top_brands")
    search["top_brands"] = None
    if isinstance(tb, list):
        search["top_brands"] = []
        for e in tb:
            if isinstance(e, dict):
                share = disc.parse_value(e.get("share_pct"), "num")[0]
                search["top_brands"].append({"brand": disc.parse_value(e.get("brand"), "str")[0], "share_pct": share})
    rt = s.get("recent_trend")
    search["recent_trend"] = rt.lower() if isinstance(rt, str) and rt.lower() in ("growing", "stable", "declining") else None
    search["sources"] = [x for x in (s.get("sources") or []) if isinstance(x, str)]
    return cands, search, w


# ================================================================== Stage 4 — signals
def amazon_demand(listing, cfg):
    """s in [0,1] = mean of available sub-signals. None if none available."""
    if listing is None:
        return None, {}, []
    subs, missing = {}, []
    for key, sc in cfg["demand_signals"].items():
        x = _num(listing.get(key))
        if x is None:
            missing.append(key)
            continue
        subs[key] = _log10(x, sc["zero_at"], sc["full_at"])
    if not subs:
        return None, subs, missing
    return sum(subs.values()) / len(subs), subs, missing


def cross_platform_demand(amazon_s, tiktok_units, cfg):
    if amazon_s is None or _num(tiktok_units) is None:
        return "INSUFFICIENT_DATA"
    for label in ("STRONG", "MODERATE"):
        r = cfg["cross_platform_demand"][label]
        if amazon_s >= r["amazon_demand_min"] and tiktok_units >= r["tiktok_units_30d_min"]:
            return label
    return "WEAK"


def top3_share(search):
    tb = search.get("top_brands")
    if not tb:
        return None
    shares = sorted([s["share_pct"] for s in tb if _num(s.get("share_pct")) is not None], reverse=True)
    return sum(shares[:3]) if shares else None


def amazon_competition(comparable, matched_reviews, top3, cfg):
    c = cfg["amazon_competition"]
    hi, lo = c["HIGH"], c["LOW"]
    if (comparable is not None and comparable >= hi["comparable_listings_min"]) or \
       (matched_reviews is not None and matched_reviews >= hi["matched_review_count_min"]) or \
       (top3 is not None and top3 >= hi["top3_brand_share_pct_min"]):
        return "HIGH"
    if comparable is None and matched_reviews is None:
        return "INSUFFICIENT_DATA"
    if comparable is not None and matched_reviews is not None and \
       comparable <= lo["comparable_listings_max"] and matched_reviews <= lo["matched_review_count_max"]:
        return "LOW"
    return "MODERATE"


def price_alignment(amazon_price, tiktok_price, cfg):
    a, t = _num(amazon_price), _num(tiktok_price)
    if a is None or t is None or t <= 0:
        return "INSUFFICIENT_DATA", None
    ratio = a / t
    g, m = cfg["price_alignment"]["GOOD"], cfg["price_alignment"]["MIXED"]
    if g["ratio_min"] <= ratio <= g["ratio_max"]:
        return "GOOD", ratio
    if m["ratio_min"] <= ratio <= m["ratio_max"]:
        return "MIXED", ratio
    return "POOR", ratio


TREND_DIR = {"ACCELERATING": 1, "GROWING": 1, "STABLE": 0, "DECLINING": -1,
             "growing": 1, "stable": 0, "declining": -1}


def consistency(tiktok_trend, amazon_trend, cfg):
    a, b = TREND_DIR.get(tiktok_trend), TREND_DIR.get(amazon_trend)
    if a is None or b is None:
        return None
    cm = cfg["avs"]["consistency_map"]
    return cm["same"] if a == b else (cm["adjacent"] if abs(a - b) == 1 else cm["opposite"])


# ================================================================== Stage 5/6
def avs_score(inputs, cfg):
    comps, total, bd = cfg["avs"]["components"], 0.0, {}

    def put(name, s, status="full", **extra):
        nonlocal total
        pts = comps[name]["points"]
        if s is None:
            bd[name] = {"points": NA, "max": pts, "status": "N/A", **extra}
        else:
            total += s * pts
            bd[name] = {"points": _round(s * pts), "max": pts, "status": status, **extra}

    put("amazon_demand_evidence", inputs["amazon_demand"],
        "partial" if inputs["demand_missing"] and inputs["amazon_demand"] is not None else "full",
        missing_signals=inputs["demand_missing"])
    put("match_confidence", 0.0 if not inputs["matched"] else inputs["match_confidence"] / 100)
    cmap = comps["competition_opportunity"]["map"]
    put("competition_opportunity", cmap.get(inputs["competition"]))
    pmap = comps["price_alignment"]["map"]
    put("price_alignment", pmap.get(inputs["price_alignment"]))
    put("cross_platform_consistency", inputs["consistency"])
    return _round(total, cfg["avs"]["rounding"]), bd


def amazon_confidence(fields, match_conf, cfg):
    ac, total, bd = cfg["amazon_confidence"], 0.0, {}
    for name, c in ac["components"].items():
        if name == "match_confidence":
            frac = (match_conf or 0) / 100
        else:
            avail = [(_num(fields.get(f)) is not None) if f != "amazon_top_brands" else bool(fields.get(f))
                     for f in c["fields"]]
            frac = sum(avail) / len(avail)
        pts = c["points"] * frac
        total += pts
        bd[name] = {"earned": _round(pts), "max": c["points"],
                    "status": "full" if frac == 1 else ("missing" if frac == 0 else "partial")}
    score = _round(total, ac["rounding"])
    return score, _level(score, ac["levels"]), bd


# ================================================================== Stage 7
def amazon_flags(r, cfg, filters_cfg):
    rf, flags = cfg["red_flags"], []
    if r["amazon_match_status"] == "NO_RELIABLE_MATCH":
        flags.append({"flag": "NO_RELIABLE_MATCH", "best_candidate_score": r["amazon_match_confidence"]})
    if r["amazon_competition"] == rf["AMAZON_HIGH_SATURATION"]["competition"]:
        flags.append({"flag": "AMAZON_HIGH_SATURATION"})
    if r["price_ratio"] is not None and r["price_ratio"] < rf["AMAZON_PRICE_COMPRESSION"]["price_ratio_below"]:
        flags.append({"flag": "AMAZON_PRICE_COMPRESSION", "price_ratio": _round(r["price_ratio"], 3)})
    if r["amazon_demand_strength"] is not None and r["amazon_demand_strength"] < rf["AMAZON_LOW_DEMAND"]["amazon_demand_below"]:
        flags.append({"flag": "AMAZON_LOW_DEMAND", "amazon_demand_strength": r["amazon_demand_strength"]})
    t3 = r["amazon_top3_brand_share_pct"]
    if t3 is not None and t3 >= rf["AMAZON_DOMINATED_BY_MAJOR_BRANDS"]["top3_brand_share_pct_min"]:
        flags.append({"flag": "AMAZON_DOMINATED_BY_MAJOR_BRANDS", "top3_brand_share_pct": t3})
    if r["amazon_confidence"] < rf["AMAZON_DATA_INSUFFICIENT"]["amazon_confidence_below"]:
        flags.append({"flag": "AMAZON_DATA_INSUFFICIENT", "amazon_confidence": r["amazon_confidence"]})
    kws = filters_cfg["risk_rules"]["trademark_ip_risk"]["keywords"]
    texts = [r["tiktok_product_name"] or "", r["amazon_match_name"] or ""]
    hits = sorted({kw for kw in kws for t in texts if t and disc._word_match(t, kw)})
    brand = _norm_brand(r.get("amazon_brand"))
    hits += sorted({kw for kw in kws if brand and brand == _norm_brand(kw)} - set(hits))
    if hits:
        flags.append({"flag": "IP_REVIEW_REQUIRED", "keywords": hits,
                      "note": "keyword match only; not a legal or infringement conclusion"})
    return flags


# ================================================================== validate one product
def tiktok_view(deep):
    return {"product_id": deep.get("product_id"), "name": deep.get("product_name"),
            "category": deep.get("category"), "brand": (deep.get("shop") or {}).get("brand"),
            "shop_name": (deep.get("shop") or {}).get("shop_name"),
            "price": (deep.get("price") or {}).get("avg"), "units_30d": deep.get("units"),
            "trend": (deep.get("trend_metrics") or {}).get("label")}


def validate(envelope, raw_path, deep, cfg, filters_cfg, cache_hit=False):
    tk = tiktok_view(deep)
    data = (envelope.get("response") or {}).get("data") or {}
    base = {"product_id": tk["product_id"], "tiktok_product_name": tk["name"],
            "observation_timestamp": envelope.get("observation_timestamp"), "market": envelope.get("market"),
            "source": {"provider": envelope.get("provider", cfg["provider"]), "raw_file": str(raw_path),
                       "task_id": envelope.get("task_id") or data.get("task_id"),
                       "report_url": data.get("report_url"), "query_type": envelope.get("query_type"),
                       "cache_hit": cache_hit, "deep_analysis_file": deep.get("_deep_file")},
            "tiktok_scores": {"wps": deep.get("wps"), "confidence": deep.get("confidence")}}
    obj, errors = DA.extract_object(envelope)
    if obj is None:
        return {**base, "status": "malformed", "errors": errors}
    cands, search, warnings = normalize_provider(obj)

    idx, m = best_match(tk, cands, cfg)
    score = m["score"] if m else 0.0
    matched = m is not None and score >= cfg["matching"]["min_reliable_match"]
    listing = cands[idx] if matched else None           # never populate from an unreliable candidate

    s, subs, dmissing = amazon_demand(listing, cfg)
    comparable = search["comparable_listings_count"]
    reviews = listing["review_count"] if listing else None
    t3 = top3_share(search)
    competition = amazon_competition(comparable, reviews, t3, cfg)
    pa, ratio = price_alignment(listing["price"] if listing else None, tk["price"], cfg)
    cpd = cross_platform_demand(s, tk["units_30d"], cfg)
    cons = consistency(tk["trend"], search["recent_trend"], cfg)
    if not cands and comparable == 0:
        presence = "NO"
    elif matched:
        presence = "YES"
    else:
        presence = "UNCERTAIN"

    fields = {"amazon_price": listing["price"] if listing else None,
              "amazon_rating": listing["rating"] if listing else None,
              "amazon_review_count": reviews,
              "amazon_bsr": listing["bsr"] if listing else None,
              "amazon_sales_indicator": listing["monthly_sales_estimate"] if listing else None,
              "amazon_competitor_count": comparable,
              "amazon_price_range_min": search["comparable_price_min"],
              "amazon_price_range_max": search["comparable_price_max"],
              "amazon_top_brands": search["top_brands"]}
    avs, avs_bd = avs_score({"amazon_demand": s, "demand_missing": dmissing if listing else list(cfg["demand_signals"]),
                             "matched": matched, "match_confidence": score, "competition": competition,
                             "price_alignment": pa, "consistency": cons}, cfg)
    aconf, alevel, aconf_bd = amazon_confidence(fields, score if cands else 0, cfg)

    r = {**base, "status": "ok",
         "amazon_match_name": listing["title"] if listing else None,
         "amazon_url": listing["url"] if listing else None,
         "amazon_asin": listing["asin"] if listing else None,
         "amazon_brand": listing["brand"] if listing else None,
         "amazon_match_confidence": score,
         "amazon_match_class": m["class"] if m else None,
         "amazon_match_status": "MATCHED" if matched else "NO_RELIABLE_MATCH",
         "amazon_match_detail": m["components"] if m else None,
         "amazon_price": fields["amazon_price"],
         "amazon_category": listing["category_path"] if listing else None,
         "amazon_rating": fields["amazon_rating"],
         "amazon_review_count": reviews,
         "amazon_bsr": fields["amazon_bsr"],
         "amazon_bsr_category": listing["bsr_category"] if listing else None,
         "amazon_sales_indicator": {"monthly_sales_estimate": fields["amazon_sales_indicator"],
                                    "source": listing["monthly_sales_source"] if listing else None},
         "amazon_listing_date": listing["listing_date"] if listing else None,
         "amazon_competitor_count": comparable,
         "amazon_price_range": {"min": search["comparable_price_min"], "max": search["comparable_price_max"]},
         "amazon_top3_brand_share_pct": t3,
         "amazon_recent_trend": search["recent_trend"],
         "amazon_presence": presence,
         "amazon_demand_strength": None if s is None else _round(s, 4),
         "amazon_demand_signals": {k: _round(v, 4) for k, v in subs.items()},
         "price_ratio": ratio,
         "cross_platform_demand": cpd,
         "amazon_competition": competition,
         "price_alignment": pa,
         "cross_platform_consistency": cons,
         "avs": avs, "avs_breakdown": avs_bd,
         "amazon_confidence": aconf, "amazon_confidence_level": alevel,
         "amazon_confidence_breakdown": aconf_bd,
         "rejected_candidates": [{"title": c["title"], "url": c["url"]} for i, c in enumerate(cands) if not (matched and i == idx)],
         "search_sources": search["sources"],
         "parse_warnings": warnings,
         "config_version": cfg["version"]}
    r["missing_data"] = sorted([k for k, v in fields.items() if v is None or v == []])
    r["amazon_red_flags"] = amazon_flags(r, cfg, filters_cfg)
    return r


# ================================================================== reporting (Stage 10)
def _fmt(v, suffix="/100"):
    return f"{v}{suffix}" if isinstance(v, (int, float)) and not isinstance(v, bool) else NA


def format_report(r):
    """Human-readable block. WPS and AVS are shown side by side, never combined."""
    ts = r.get("tiktok_scores", {})
    flags = ", ".join(f["flag"] for f in r.get("amazon_red_flags", [])) or "none"
    return "\n".join([
        f"{r.get('tiktok_product_name')}  ({r.get('product_id')})",
        f"WPS: {_fmt(ts.get('wps'))}",
        f"Confidence: {_fmt(ts.get('confidence'))}",
        "",
        "Amazon:",
        f"Match: {r.get('amazon_match_status')} ({r.get('amazon_match_class')})",
        f"Match Confidence: {_fmt(r.get('amazon_match_confidence'))}",
        f"AVS: {_fmt(r.get('avs'))}",
        f"Amazon Confidence: {_fmt(r.get('amazon_confidence'))}",
        f"Cross-platform Demand: {r.get('cross_platform_demand')}",
        f"Competition: {r.get('amazon_competition')}",
        f"Price Alignment: {r.get('price_alignment')}",
        f"Amazon Red Flags: {flags}",
    ])


# ================================================================== queries / runner (Stage 9)
def build_query(deep, cfg):
    tk = tiktok_view(deep)
    text = (ROOT / "prompts" / "amazon_validation.md").read_text()
    template = re.split(r"^===\s*$", text, flags=re.M)[1].strip()
    return template.format(tiktok_name=tk["name"] or NA, tiktok_category=tk["category"] or NA,
                           tiktok_brand=tk["brand"] or tk["shop_name"] or NA,
                           tiktok_price=NA if tk["price"] is None else _round(tk["price"]),
                           max_candidates=cfg["max_candidates_requested"])


def plan(eligible, cfg, now, raw_dir=RAW_DIR):
    items, seen = [], set()
    for d in eligible:
        pid, qt = d["product_id"], cfg["query_type"]
        if (pid, qt) in seen:
            items.append({"product_id": pid, "query_type": qt, "action": "skip_duplicate"})
            continue
        seen.add((pid, qt))
        cached = DA.find_cached(pid, qt, cfg["cache_hours"], now, raw_dir)
        items.append({"product_id": pid, "product_name": d.get("product_name"), "wps": d.get("wps"),
                      "confidence": d.get("confidence"), "query_type": qt,
                      "action": "use_cache" if cached else "paid_query",
                      "cached_raw": str(cached) if cached else None, "query": build_query(d, cfg)})
    paid = sum(i["action"] == "paid_query" for i in items)
    return {"items": items, "paid_queries": paid, "cache_hits": sum(i["action"] == "use_cache" for i in items),
            "estimated_credits": round(paid * cfg["estimated_credits_per_query"], 2)}


def run(live=False, client=None, deep_path=None, now=None, raw_dir=RAW_DIR, out_dir=OUT_DIR,
        deep_dir=DEEP_DIR, cfg=None, filters_cfg=None):
    cfg = cfg or load_cfg()
    filters_cfg = filters_cfg or disc.load_yaml("filters.yaml")
    now = now or datetime.now(timezone.utc)
    report = {"mode": "live" if live else "dry_run", "enabled": cfg["enabled"]}
    if not cfg["enabled"]:
        return {**report, "note": "amazon_validation.enabled is false"}
    deep_path = deep_path or latest_deep(deep_dir)
    if not deep_path:
        return {**report, "deep_file": None, "eligible": 0, "excluded": [],
                "plan": {"items": [], "paid_queries": 0, "cache_hits": 0, "estimated_credits": 0.0},
                "note": "no deep-analysis results found (Step M has not been run live yet)"}
    deep = json.loads(Path(deep_path).read_text())
    results_in = [{**r, "_deep_file": str(deep_path)} for r in deep.get("results", [])]
    eligible, excluded = select_eligible(results_in, cfg)
    pl = plan(eligible, cfg, now, raw_dir)
    report.update({"deep_file": str(deep_path), "eligible": len(eligible), "excluded": excluded, "plan": pl})
    if client is not None:
        bal = client.credits()["totalRemain"]
        report["balance_before"] = bal
        report["affordable_paid_queries"] = max(0, int((bal - cfg["min_balance_reserve"]) // cfg["estimated_credits_per_query"]))
        report["sufficient_for_full_run"] = bal - pl["estimated_credits"] >= cfg["min_balance_reserve"]
    if not live:
        return report
    if client is None:
        raise ValueError("live mode requires a client")

    by_id = {d["product_id"]: d for d in eligible}
    results, paid, stopped = [], set(), None
    for it in pl["items"]:
        pid, qt = it["product_id"], it["query_type"]
        if it["action"] == "skip_duplicate" or (pid, qt) in paid:
            continue
        if it["action"] == "use_cache":
            raw_path, hit = Path(it["cached_raw"]), True
        else:
            bal = client.credits()["totalRemain"]
            if bal < cfg["estimated_credits_per_query"] + cfg["min_balance_reserve"]:
                stopped = {"reason": "insufficient_credits", "balance": bal, "at_product": pid}
                break
            sub = client.submit(it["query"])
            task_id = (sub.get("data") or {}).get("task_id")
            response = client.wait(task_id) if task_id else sub
            raw_path = DA.save_raw_deep(response, {
                "observation_timestamp": datetime.now(timezone.utc).isoformat(), "market": "US",
                "product_id": pid, "query_type": qt, "query": it["query"], "task_id": task_id,
                "provider": cfg["provider"]}, raw_dir)
            paid.add((pid, qt))
            hit = False
        results.append(validate(json.loads(raw_path.read_text()), raw_path, by_id[pid], cfg, filters_cfg, hit))
    report.update({"results": results, "stopped": stopped, "paid_queries_done": len(paid)})
    report["balance_after"] = client.credits()["totalRemain"]
    report["saved"] = str(save_results(report, out_dir))
    return report


def save_results(report, out_dir=OUT_DIR):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path, n = out_dir / f"amazon_{ts}.json", 1
    while path.exists():
        n += 1
        path = out_dir / f"amazon_{ts}_{n}.json"
    with open(path, "x") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def main(argv):
    live = "--live" in argv
    import kalopilot_client as kc

    class Client:
        credits = staticmethod(kc.credits)
        submit = staticmethod(lambda q: kc.submit(q))
        wait = staticmethod(lambda t: kc.wait(t))

    r = run(live=live, client=Client())
    print(f"MODE: {r['mode'].upper()}  | provider: {load_cfg()['provider']}")
    if r.get("note"):
        print(f"NOTE: {r['note']}")
    print(f"Deep-analysis file: {r.get('deep_file')}")
    pl = r["plan"]
    print(f"Eligible: {r.get('eligible')} | paid queries: {pl['paid_queries']} | cache hits: {pl['cache_hits']} "
          f"| estimated credits: {pl['estimated_credits']}")
    if "balance_before" in r:
        print(f"Balance: {r['balance_before']} | affordable (keeping reserve): {r['affordable_paid_queries']} "
              f"| enough for full run: {r['sufficient_for_full_run']}")
    for it in pl["items"]:
        print(f"  [{it['action']}] {it['product_id']} WPS {it.get('wps')} Conf {it.get('confidence')} {(it.get('product_name') or '')[:50]}")
    for e in r.get("excluded", []):
        print(f"  excluded: {e['product_id']} ({e['reason']})")
    if live:
        for res in r["results"]:
            print("\n" + format_report(res))
        print(f"\nDone: {r['paid_queries_done']} paid, stopped={r['stopped']}, saved={r['saved']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
