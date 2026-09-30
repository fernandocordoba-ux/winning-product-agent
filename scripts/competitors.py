"""Competitor Intelligence (Step X): competitor stores / listings for finalist products.

NOT combined with WPS / AVS / BVS (a BVS adapter is prepared but not wired). No scraping, no
access against a source's terms: only configured integrations and user-supplied research.
Nothing is estimated (counts, spend, traffic, sales): unknown = None / N/A.

Independent scores (config/competitors.yaml), never combined:
  Competitor Saturation Score   0-100  higher = more saturated
  Competitive Opportunity Score 0-100  higher = better opportunity (demand-gated; not 1 - saturation)
  Competitor Confidence         0-100  completeness / reliability of the evidence
  Store Quality Score           0-100  per store, observable yes/no criteria only

Storage: data/raw/competitors (read-only) · data/processed/competitors/<pid>/ · data/history/competitors/<pid>/
CLI: python -m winning_product_agent competitors import <file.csv|json> | show <product_id>
"""
import copy
import hashlib
import json
import math
import os
import re
import statistics
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import discovery as disc  # noqa: E402
import suppliers as SUP  # noqa: E402  (shared deterministic matcher + file loading)

NA = "N/A"
RAW_DIR = ROOT / "data" / "raw" / "competitors"
PROCESSED_DIR = ROOT / "data" / "processed" / "competitors"
HISTORY_DIR = ROOT / "data" / "history" / "competitors"
PLATFORMS = {"SHOPIFY", "OTHER_ECOMMERCE", "UNKNOWN"}
RELATIONSHIPS = ["DIRECT", "ADJACENT", "CATEGORY", "UNRELATED"]
OFFER_FEATURES = ["discount", "free_shipping", "bundle_offer", "bogo", "quantity_breaks", "subscription_offer",
                  "guarantee", "returns_messaging", "urgency_messaging", "upsells", "cross_sells"]
STORE_CRITERIA = ["clear_branding", "mobile_friendly", "product_page_complete", "trust_elements", "reviews_visible",
                  "shipping_clarity", "returns_clarity", "offer_structure_clear"]

# ============================================================================ Stage 1 — schema
SCHEMA = {
    "competitor_id": "str", "competitor_name": "str", "competitor_domain": "str", "competitor_url": "str",
    "matched_product_id": "id", "product_match_confidence": "num", "match_source": "str",
    "relationship_claimed": "str", "platform": "str", "brand": "str",
    "product_title": "str", "product_url": "str", "selling_price": "num", "currency": "str",
    "compare_at_price": "num", "discount_percent": "num",
    "shipping_offer": "str", "free_shipping": "bool", "estimated_delivery": "str",
    "bundle_offer": "bool", "bogo": "bool", "quantity_breaks": "bool", "subscription_offer": "bool",
    "guarantee": "bool", "returns_messaging": "bool", "urgency_messaging": "bool",
    "upsells": "bool", "cross_sells": "bool",
    "reviews_visible": "bool", "review_count": "count", "rating": "num",
    "store_age": "str", "store_traffic": "count", "estimated_sales": "count", "estimated_sales_source": "str",
    "active_ads_count": "count", "meta_ads_present": "bool", "tiktok_ads_present": "bool",
    "oldest_ad_start_date": "date", "creative_count": "count", "ad_offer_count": "count",
    "ad_landing_pages": "count", "ads_library_url": "str",
    "clear_branding": "bool", "mobile_friendly": "bool", "product_page_complete": "bool", "trust_elements": "bool",
    "shipping_clarity": "bool", "returns_clarity": "bool", "offer_structure_clear": "bool",
    "variants": "str", "observed_at": "str", "retrieved_at": "str", "source": "str", "raw_source_location": "str",
}
ALIASES = {"name": "competitor_name", "store": "competitor_name", "store_url": "competitor_url", "url": "product_url",
           "price": "selling_price", "compare_at": "compare_at_price", "discount": "discount_percent",
           "ads": "active_ads_count", "meta_ads": "meta_ads_present", "reviews": "review_count",
           "traffic": "store_traffic", "shipping": "shipping_offer", "bundles": "bundle_offer",
           "bundle": "bundle_offer", "store_platform": "platform", "title": "product_title",
           "relationship": "relationship_claimed", "ad_start_date": "oldest_ad_start_date",
           "creatives": "creative_count", "landing_pages": "ad_landing_pages", "variant": "variants",
           "match_confidence": "product_match_confidence"}


class MalformedCompetitorInput(ValueError):
    pass


class ProviderNotIntegrated(RuntimeError):
    pass


def load_cfg(path=None):
    import config_resolver as _CR
    path = path or _CR.path("competitors.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def _now():
    return datetime.now(timezone.utc).isoformat()


def parse_field(name, v):
    kind = SCHEMA[name]
    if kind == "bool":
        return SUP.parse_bool(v)
    if kind == "str":
        s = None if v is None else str(v).strip()
        return (s or None), None
    return disc.parse_value(v, kind)


def domain_of(url):
    try:
        host = urlparse(url or "").netloc.lower()
    except ValueError:
        return None
    return host[4:] if host.startswith("www.") else (host or None)


def competitor_id_for(c):
    path = urlparse(c.get("product_url") or "").path.rstrip("/").lower()
    basis = f"{c.get('matched_product_id')}|{c.get('competitor_domain')}|{path}"
    return "cmp_" + hashlib.sha1(basis.encode()).hexdigest()[:16]


# ============================================================================ Stage 2 — providers
class CompetitorProvider(ABC):
    name = "abstract"

    @abstractmethod
    def search_competitors(self, product):
        """Candidate competitor listings for a finalist product."""

    @abstractmethod
    def get_store_details(self, ref):
        """Store-level facts (platform, age, observable quality criteria)."""

    @abstractmethod
    def get_product_offer(self, ref):
        """Price and visible offer structure of the competitor's product page."""

    @abstractmethod
    def get_ad_activity(self, ref):
        """Publicly available ad activity (e.g. an ad library), never estimated spend."""

    @abstractmethod
    def normalize_result(self, raw, meta):
        """Raw -> normalized schema dict."""


class ManualCompetitorProvider(CompetitorProvider):
    """Research rows supplied by the user. No network access at all."""
    name = "manual_import"

    def __init__(self, rows):
        self.rows = rows

    def search_competitors(self, product):
        pid = str(product.get("product_id") if isinstance(product, dict) else product)
        return [r for r in self.rows if str(r.get("matched_product_id")) == pid]

    def get_store_details(self, ref):
        return {k: ref.get(k) for k in ("platform", "store_age", "store_traffic", *STORE_CRITERIA) if k in ref}

    def get_product_offer(self, ref):
        return {k: ref.get(k) for k in ("selling_price", "compare_at_price", "discount_percent", "shipping_offer",
                                        *OFFER_FEATURES) if k in ref}

    def get_ad_activity(self, ref):
        return {k: ref.get(k) for k in ("meta_ads_present", "active_ads_count", "oldest_ad_start_date",
                                        "creative_count", "ads_library_url") if k in ref}

    def normalize_result(self, raw, meta):
        return normalize_row(raw, meta)[0]


def get_provider(name, cfg=None, **kw):
    cfg = cfg or load_cfg()
    p = (cfg.get("providers") or {}).get(name)
    if not p or not p.get("implemented"):
        raise ProviderNotIntegrated(f"competitor source '{name}' is not integrated "
                                    f"({(p or {}).get('note', 'unknown source')}); use manual_import")
    return ManualCompetitorProvider(kw.get("rows", []))


# ============================================================================ Stage 3 — manual import
def normalize_row(row, meta, cfg=None):
    cfg = cfg or load_cfg()
    c, errors = {k: None for k in SCHEMA}, []
    src = {ALIASES.get(k, k): v for k, v in row.items()}
    for k in SCHEMA:
        if k in src:
            val, prob = parse_field(k, src[k])
            c[k] = val
            if prob:
                errors.append(f"{k}: {prob}")
    for k in cfg["manual_import"]["required_fields"]:
        if c.get(k) is None:
            errors.append(f"missing required field: {k}")
    for k in ("competitor_url", "product_url", "ads_library_url"):
        if c[k] and not re.match(r"^https?://", c[k]):
            errors.append(f"{k} must start with http:// or https://")
    if c["rating"] is not None and not 0 <= c["rating"] <= 5:
        errors.append(f"rating {c['rating']} outside 0-5")
    if c["product_match_confidence"] is not None and not 0 <= c["product_match_confidence"] <= 100:
        errors.append("product_match_confidence outside 0-100")
    c["currency"] = (c["currency"] or "USD").upper()
    if c["currency"] not in cfg["manual_import"]["accepted_currencies"]:
        errors.append(f"currency {c['currency']} not accepted (no FX conversion is invented)")
    c["platform"] = (c["platform"] or "UNKNOWN").upper().replace(" ", "_")
    if c["platform"] == "OTHER":
        c["platform"] = "OTHER_ECOMMERCE"
    if c["platform"] not in PLATFORMS:
        errors.append(f"platform must be one of {sorted(PLATFORMS)}")
    if c["relationship_claimed"]:
        c["relationship_claimed"] = c["relationship_claimed"].upper()
        if c["relationship_claimed"] not in RELATIONSHIPS[:3]:
            errors.append("relationship must be DIRECT, ADJACENT or CATEGORY")
    if c["estimated_sales"] is not None and not c["estimated_sales_source"]:
        errors.append("estimated_sales requires estimated_sales_source (only legitimately sourced figures)")
    # discount: DERIVED only from two visible prices when not given
    if c["discount_percent"] is None and c["compare_at_price"] and c["selling_price"] is not None \
            and c["compare_at_price"] > c["selling_price"]:
        c["discount_percent"] = round((c["compare_at_price"] - c["selling_price"]) / c["compare_at_price"] * 100, 2)
        c["discount_source"] = "derived from compare_at_price and selling_price"
    c["competitor_domain"] = c["competitor_domain"] or domain_of(c["competitor_url"])
    c["match_source"] = "manual" if c["product_match_confidence"] is not None else None
    c["source"] = c["source"] or meta.get("source", "manual_import")
    c["retrieved_at"] = meta.get("retrieved_at") or _now()
    c["observed_at"] = c["observed_at"] or c["retrieved_at"]
    c["raw_source_location"] = meta.get("raw_source_location")
    c["competitor_id"] = competitor_id_for(c)
    return c, errors


def dedupe(rows):
    """Same competitor listing (domain + product path) counted once; the most complete record is kept."""
    best, dups = {}, []
    for c in rows:
        k = c["competitor_id"]
        filled = sum(v is not None for v in c.values())
        if k in best:
            dups.append(k)
            if filled > sum(v is not None for v in best[k].values()):
                best[k] = c
        else:
            best[k] = c
    return list(best.values()), dups


# ============================================================================ Stage 4-5 — match / relationship
def match(tiktok, c, cfg):
    as_offer = {"supplier_product_title": c.get("product_title"), "supplier_brand": c.get("brand"),
                "variants": c.get("variants"), "match_source": c.get("match_source"),
                "match_confidence": c.get("product_match_confidence")}
    return SUP.match_confidence(tiktok, as_offer, cfg)


def relationship(score, tiktok, c, cfg):
    r = cfg["relationships"]
    if score is not None and score >= r["direct_min"]:
        computed = "DIRECT"
    elif score is not None and score >= r["adjacent_min"]:
        computed = "ADJACENT"
    else:
        fw = set(cfg["matching"]["function_words"])
        shared = SUP._tokens(tiktok.get("name"), cfg) & SUP._tokens(c.get("product_title"), cfg) & fw
        computed = "CATEGORY" if shared else "UNRELATED"
    claimed = c.get("relationship_claimed")
    if claimed and RELATIONSHIPS.index(claimed) > RELATIONSHIPS.index(computed):
        return claimed               # a researcher may DOWNGRADE (e.g. category store), never upgrade to DIRECT
    return computed


# ============================================================================ Stages 6-8 — ads / offers
def ad_age_days(c):
    d, obs = c.get("oldest_ad_start_date"), (c.get("observed_at") or "")[:10]
    if not d or not obs:
        return None
    try:
        return (datetime.strptime(obs, "%Y-%m-%d") - datetime.strptime(d, "%Y-%m-%d")).days
    except ValueError:
        return None


def longevity(days, cfg):
    if days is None:
        return None
    for cl in cfg["ads"]["longevity_classes"]:
        if cl["max_days"] is None or days <= cl["max_days"]:
            return cl["label"]


def offer_features(c):
    """Only features explicitly observed (True). Unknown (None) is not 'absent'."""
    feats = {f for f in OFFER_FEATURES if f != "discount" and c.get(f) is True}
    if c.get("discount_percent") is not None and c["discount_percent"] > 0:
        feats.add("discount")
    return sorted(feats)


def known_offer_fields(c):
    return [f for f in OFFER_FEATURES if f != "discount" and c.get(f) is not None] + \
        (["discount"] if c.get("discount_percent") is not None or c.get("compare_at_price") is not None else [])


def store_quality(c, cfg):
    q = cfg["store_quality"]
    avail = {k: c.get(k) for k in STORE_CRITERIA if c.get(k) is not None}
    have = sum(q["criteria"][k]["points"] for k in avail)
    if have < q["min_available_points"]:
        return None, {"available_points": have, "observed": avail}
    return round(100 * sum(q["criteria"][k]["points"] for k, v in avail.items() if v) / have, 2), \
        {"available_points": have, "observed": avail}


def provenance(c, score):
    ref = c.get("product_url")
    base = {"provider": c.get("source"), "source_reference": ref, "retrieved_at": c.get("retrieved_at"),
            "scope": "competitor product listing", "match_confidence": score}
    return {"selling_price": {**base, "value": c.get("selling_price")},
            "compare_at_price": {**base, "value": c.get("compare_at_price")},
            "review_count": {**base, "value": c.get("review_count")},
            "active_ads_count": {**base, "value": c.get("active_ads_count"), "scope": "public ad activity",
                                 "source_reference": c.get("ads_library_url") or ref},
            "meta_ads_present": {**base, "value": c.get("meta_ads_present"), "scope": "public ad activity",
                                 "source_reference": c.get("ads_library_url") or ref},
            "store_traffic": {**base, "value": c.get("store_traffic"), "scope": "store"},
            "estimated_sales": {**base, "value": c.get("estimated_sales"), "scope": "store",
                                "source_reference": c.get("estimated_sales_source")}}


def enrich(c, tiktok, cfg):
    e = copy.deepcopy(c)
    score, cls, det = match(tiktok, e, cfg)
    e["match_confidence_calc"], e["match_class"], e["match_detail"] = score, cls, det
    e["relationship"] = relationship(score, tiktok, e, cfg)
    e["ad_age_days"] = ad_age_days(e)
    e["ad_longevity"] = longevity(e["ad_age_days"], cfg)
    e["offer_features"] = offer_features(e)
    e["store_quality_score"], e["store_quality_detail"] = store_quality(e, cfg)
    e["provenance"] = provenance(e, score)
    return e


# ============================================================================ Stages 9-14 — analysis
def _share(items, pred, known):
    base = [x for x in items if known(x)]
    return (sum(1 for x in base if pred(x)) / len(base), len(base)) if base else (None, 0)


def _jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if (a | b) else None


def price_stats(direct, target, cfg):
    prices = sorted(c["selling_price"] for c in direct if c.get("selling_price") is not None)
    out = {"n": len(prices), "lowest_price": None, "median_price": None, "average_price": None, "highest_price": None,
           "spread": None, "our_target_price": target, "our_target_price_vs_median_pct": None, "price_position": None}
    if len(prices) < cfg["pricing"]["min_prices"]:
        out["note"] = f"{len(prices)} qualified price(s) < {cfg['pricing']['min_prices']}: distribution N/A"
        return out
    med = statistics.median(prices)
    out.update(lowest_price=prices[0], median_price=round(med, 2), average_price=round(statistics.mean(prices), 2),
               highest_price=prices[-1], spread=round((prices[-1] - prices[0]) / med, 4) if med else None)
    if target is not None and med:
        pct = round((target - med) / med * 100, 2)
        p = cfg["pricing"]["position"]
        out["our_target_price_vs_median_pct"] = pct
        out["price_position"] = ("PRICE_PREMIUM" if pct > p["premium_above_pct"] else
                                 "PRICE_DISCOUNTED" if pct < p["discounted_below_pct"] else "PRICE_ALIGNED")
    return out


def _weighted(components, points, min_points):
    avail = {k: v for k, v in components.items() if v is not None}
    have = sum(points[k] for k in avail)
    if have < min_points:
        return None, have
    return round(100 * sum(points[k] * v for k, v in avail.items()) / have, 2), have


def ad_signals(direct):
    adv, known = _share(direct, lambda c: c["meta_ads_present"] is True, lambda c: c.get("meta_ads_present") is not None)
    ages = [c["ad_age_days"] for c in direct if c.get("ad_age_days") is not None]
    counts = [c["active_ads_count"] for c in direct if c.get("active_ads_count") is not None]
    lon = {}
    for c in direct:
        if c.get("ad_longevity"):
            lon[c["ad_longevity"]] = lon.get(c["ad_longevity"], 0) + 1
    return {"direct_with_ad_data": known, "active_meta_advertisers": sum(1 for c in direct if c.get("meta_ads_present") is True),
            "advertiser_share": None if adv is None else round(adv, 4),
            "total_active_ads": sum(counts) if counts else None, "oldest_ad_age_days": max(ages) if ages else None,
            "longevity_counts": lon, "creative_count": sum(c["creative_count"] for c in direct
                                                           if c.get("creative_count") is not None) or None,
            "spend": "not estimated (never available as a fact)"}


def saturation(direct, prices, ads, cfg):
    s, pts = cfg["saturation"], {k: v["points"] for k, v in cfg["saturation"]["components"].items()}
    comp = s["components"]
    feats = [c["offer_features"] for c in direct if known_offer_fields(c)]
    sims = [x for i in range(len(feats)) for x in [_jaccard(feats[i], f) for f in feats[i + 1:]] if x is not None]
    reviews = [c["review_count"] for c in direct if c.get("review_count") is not None]
    parts = {
        "direct_competitor_count": min(len(direct) / comp["direct_competitor_count"]["full_at"], 1.0),
        "advertising_density": ads["advertiser_share"],
        "offer_similarity": (round(statistics.mean(sims), 4) if len(feats) >= comp["offer_similarity"]["min_competitors"]
                             and sims else None),
        "price_compression": (round(1 - min(prices["spread"] / comp["price_compression"]["full_spread"], 1.0), 4)
                              if prices["spread"] is not None else None),
        "store_dominance": (round(max(reviews) / sum(reviews), 4) if len(reviews) >= comp["store_dominance"]["min_competitors"]
                            and sum(reviews) > 0 else None),
    }
    score, have = _weighted(parts, pts, s["min_available_points"])
    return {"score": score, "components": parts, "available_points": have}


def opportunity(direct, prices, ads, deep, landed_cost, cfg):
    o = cfg["opportunity"]
    comp, pts = o["components"], {k: v["points"] for k, v in o["components"].items()}
    d = comp["demand_evidence"]
    units = (deep or {}).get("units")
    demand = (None if units is None else
              0.0 if units <= 0 else max(0.0, min(1.0, (math.log10(units) - math.log10(d["zero_at"])) /
                                              (math.log10(d["full_at"]) - math.log10(d["zero_at"])))))
    keyf = o["key_offer_features"]
    slots = [(c, f) for c in direct for f in keyf if c.get(f) is not None]
    sq = [c["store_quality_score"] for c in direct if c.get("store_quality_score") is not None]
    med = prices["median_price"]
    parts = {
        "demand_evidence": None if demand is None else round(demand, 4),
        "competition_room": round(1 - min(len(direct) / comp["competition_room"]["full_at"], 1.0), 4),
        "price_spread": (round(min(prices["spread"] / comp["price_spread"]["full_spread"], 1.0), 4)
                         if prices["spread"] is not None else None),
        "offer_weaknesses": round(sum(1 for c, f in slots if c[f] is False) / len(slots), 4) if slots else None,
        "creative_gaps": None if ads["advertiser_share"] is None else round(1 - ads["advertiser_share"], 4),
        "store_quality_gaps": round(1 - statistics.mean(sq) / 100, 4) if sq else None,
        "supplier_economics": (round(max(0.0, min((med - landed_cost) / med / comp["supplier_economics"]["full_margin"], 1.0)), 4)
                               if med and landed_cost is not None else None),
    }
    if o["require_demand"] and parts["demand_evidence"] is None:
        return {"score": None, "components": parts, "reason": "no demand evidence: opportunity N/A (never rewarded)"}
    score, have = _weighted(parts, pts, o["min_available_points"])
    cap = None
    if score is not None:
        for c in o["demand_caps"]:
            if parts["demand_evidence"] < c["max_demand_s"]:
                cap = c["cap"]
                break
        if cap is not None and score > cap:
            score = float(cap)
    return {"score": score, "components": parts, "available_points": have, "demand_cap_applied": cap}


def confidence(all_obs, direct, cfg):
    c = cfg["confidence"]
    pts = {k: v["points"] for k, v in c["components"].items()}
    n = len(direct)
    frac = (lambda pred: sum(1 for x in direct if pred(x)) / n) if n else (lambda pred: 0.0)
    cov = {
        "product_matching": (statistics.mean(x["match_confidence_calc"] for x in direct) / 100) if n else 0.0,
        "competitor_count": min(len(all_obs) / c["min_sample"], 1.0),
        "pricing": frac(lambda x: x.get("selling_price") is not None),
        "ad_activity": frac(lambda x: x.get("meta_ads_present") is not None),
        "offer_details": frac(lambda x: bool(known_offer_fields(x))),
        "platform": frac(lambda x: x.get("platform") not in (None, "UNKNOWN")),
        "traffic_sales": frac(lambda x: x.get("store_traffic") is not None or x.get("estimated_sales") is not None),
    }
    score = round(sum(pts[k] * v for k, v in cov.items()), 2)
    level = next(lv for lv, t in sorted(c["levels"].items(), key=lambda kv: -kv[1]) if score >= t)
    return {"score": score, "level": level, "components": {k: round(v, 4) for k, v in cov.items()}}


def differentiation(direct, prices, ads, cfg):
    d = cfg["differentiation"]
    n = len(direct)
    if n < d["min_direct"]:
        return {"available": False, "note": f"{n} direct competitor(s) < {d['min_direct']}: not enough evidence",
                "opportunities": []}
    gaps, opps = {}, []

    def feature_gap(key, feats, label):
        share, known = _share(direct, lambda c: any(c.get(f) is True for f in feats),
                              lambda c: any(c.get(f) is not None for f in feats))
        if share is None:
            gaps[key] = {"present": None, "facts": f"no {label} data observed"}
            return
        present = share < d["gap_if_share_below"]
        gaps[key] = {"present": present, "facts": f"{round(share * known)}/{known} direct competitors show {label}",
                     "share": round(share, 4)}
        if present:
            opps.append({"gap": key, "evidence": gaps[key]["facts"]})
    if prices["spread"] is not None:
        present = prices["price_position"] == "PRICE_DISCOUNTED" or prices["spread"] >= 0.3
        gaps["pricing_gap"] = {"present": present,
                               "facts": f"direct prices {prices['lowest_price']}–{prices['highest_price']} USD "
                                        f"(median {prices['median_price']}, spread {round(prices['spread'] * 100)}%); "
                                        f"our target {prices['our_target_price']} = {prices['price_position']}"}
        if present:
            opps.append({"gap": "pricing_gap", "evidence": gaps["pricing_gap"]["facts"]})
    else:
        gaps["pricing_gap"] = {"present": None, "facts": prices.get("note", "no price distribution")}
    feature_gap("bundle_gap", ["bundle_offer", "quantity_breaks", "bogo"], "a bundle / quantity break / BOGO")
    feature_gap("shipping_gap", ["free_shipping"], "free shipping")
    feature_gap("offer_gap", ["guarantee", "returns_messaging"], "a guarantee or returns messaging")
    if ads["advertiser_share"] is not None:
        present = ads["advertiser_share"] < d["creative_gap_advertiser_share_below"]
        gaps["creative_gap"] = {"present": present, "facts": f"{ads['active_meta_advertisers']}/{ads['direct_with_ad_data']} "
                                                             f"direct competitors run active Meta ads"}
        if present:
            opps.append({"gap": "creative_gap", "evidence": gaps["creative_gap"]["facts"]})
    else:
        gaps["creative_gap"] = {"present": None, "facts": "no ad activity data"}
    rv = [c["review_count"] for c in direct if c.get("review_count") is not None]
    if rv:
        med = statistics.median(rv)
        present = med < d["review_gap_median_reviews_below"]
        gaps["review_gap"] = {"present": present, "facts": f"median visible reviews {med} across {len(rv)} competitors"}
        if present:
            opps.append({"gap": "review_gap", "evidence": gaps["review_gap"]["facts"]})
    else:
        gaps["review_gap"] = {"present": None, "facts": "no review counts observed"}
    sq = [c["store_quality_score"] for c in direct if c.get("store_quality_score") is not None]
    if sq:
        m = round(statistics.mean(sq), 2)
        present = m < d["store_quality_gap_below"]
        gaps["store_quality_gap"] = {"present": present, "facts": f"mean observable store quality {m}/100 ({len(sq)} stores)"}
        if present:
            opps.append({"gap": "store_quality_gap", "evidence": gaps["store_quality_gap"]["facts"]})
    else:
        gaps["store_quality_gap"] = {"present": None, "facts": "no store quality criteria observed"}
    return {"available": True, "gaps": gaps, "opportunities": opps}


def red_flags(obs, direct, prices, ads, sat, diff, conf, cfg):
    f, out = cfg["flags"], []
    if len(direct) >= f["EXTREME_DIRECT_COMPETITION"]["direct_at_least"]:
        out.append({"flag": "EXTREME_DIRECT_COMPETITION", "direct": len(direct)})
    if ads["advertiser_share"] is not None and ads["advertiser_share"] >= f["META_AD_SATURATION"]["advertiser_share_at_least"] \
            and len(direct) >= f["META_AD_SATURATION"]["min_direct"]:
        out.append({"flag": "META_AD_SATURATION", "advertiser_share": ads["advertiser_share"]})
    if prices["spread"] is not None and prices["spread"] < f["PRICE_COMPRESSION"]["spread_below"]:
        out.append({"flag": "PRICE_COMPRESSION", "spread": prices["spread"]})
    dom = sat["components"].get("store_dominance")
    if dom is not None and dom >= f["DOMINANT_BRAND"]["top_share_at_least"]:
        top = max((c for c in direct if c.get("review_count") is not None), key=lambda c: c["review_count"])
        out.append({"flag": "DOMINANT_BRAND", "competitor": top["competitor_name"], "review_share": dom})
    sim = sat["components"].get("offer_similarity")
    if sim is not None and sim >= f["IDENTICAL_OFFERS"]["similarity_at_least"]:
        out.append({"flag": "IDENTICAL_OFFERS", "similarity": sim})
    if ads["oldest_ad_age_days"] is not None and ads["oldest_ad_age_days"] > f["LONG_RUNNING_COMPETITOR_ADS"]["ad_age_days_above"]:
        out.append({"flag": "LONG_RUNNING_COMPETITOR_ADS", "oldest_ad_age_days": ads["oldest_ad_age_days"],
                    "note": "persistence only, not proof of profitability"})
    if diff.get("available") and not diff["opportunities"] and len(direct) >= f["LOW_DIFFERENTIATION"]["min_direct"]:
        out.append({"flag": "LOW_DIFFERENTIATION", "direct": len(direct)})
    unrel = sum(1 for c in obs if c["match_class"] in ("UNRELIABLE", "REVIEW"))
    if obs and unrel / len(obs) >= f["UNRELIABLE_COMPETITOR_MATCHES"]["unreliable_share_at_least"]:
        out.append({"flag": "UNRELIABLE_COMPETITOR_MATCHES", "unreliable": unrel, "observed": len(obs)})
    if conf["score"] < f["INSUFFICIENT_COMPETITOR_DATA"]["confidence_below"]:
        out.append({"flag": "INSUFFICIENT_COMPETITOR_DATA", "competitor_confidence": conf["score"]})
    return out


def analyze_product(product_id, deep, observations, cfg=None, target_price=None, landed_cost=None):
    """All competitor observations for one product -> intelligence (never merged into WPS / AVS / BVS)."""
    cfg = cfg or load_cfg()
    tiktok = {"name": (deep or {}).get("product_name"), "brand": ((deep or {}).get("shop") or {}).get("brand")}
    uniq, dups = dedupe(observations)
    enriched = [enrich(c, tiktok, cfg) for c in uniq]
    groups = {r: [c for c in enriched if c["relationship"] == r] for r in RELATIONSHIPS}
    direct = groups["DIRECT"]
    if target_price is None:
        target_price = ((deep or {}).get("price") or {}).get("avg")
    prices = price_stats(direct, target_price, cfg)
    ads = ad_signals(direct)
    sat = saturation(direct, prices, ads, cfg)
    opp = opportunity(direct, prices, ads, deep, landed_cost, cfg)
    conf = confidence(enriched, direct, cfg)
    diff = differentiation(direct, prices, ads, cfg)
    flags = red_flags(enriched, direct, prices, ads, sat, diff, conf, cfg)
    patterns = {}
    for c in direct:
        for f in c["offer_features"]:
            patterns[f] = patterns.get(f, 0) + 1
    return {"product_id": str(product_id), "config_version": cfg.get("version"),
            "observed": len(enriched), "duplicates_removed": len(dups),
            "direct_competitors": len(direct), "adjacent_competitors": len(groups["ADJACENT"]),
            "category_competitors": len(groups["CATEGORY"]), "unrelated_ignored": len(groups["UNRELATED"]),
            "qualified_competitor_count": len(direct),
            "prices": prices, "ads": ads, "ad_longevity_limitation": cfg["ads"]["limitation"].strip(),
            "offer_patterns": dict(sorted(patterns.items(), key=lambda kv: -kv[1])),
            "saturation": sat, "opportunity": opp, "confidence": conf, "differentiation": diff,
            "red_flags": flags, "competitors": enriched,
            "note": "Competitor data alone never makes a product good or bad; not combined with WPS/AVS/BVS."}


# ============================================================================ Stage 20 — BVS adapter (NOT wired)
def bvs_inputs(analysis):
    """Prepared interface for a future BVS version. BVS does NOT call this yet (Step X rule)."""
    if not analysis:
        return {"ready": False, "reason": "no competitor analysis"}
    conf = analysis["confidence"]["score"]
    return {"ready": conf >= 60, "competitor_confidence": conf,
            "competitive_opportunity": analysis["opportunity"]["score"],
            "price_compression": analysis["saturation"]["components"].get("price_compression"),
            "direct_competitor_count": analysis["direct_competitors"],
            "advertising_saturation": analysis["ads"]["advertiser_share"],
            "note": "adapter only — BVS weights unchanged; wire in after source quality is validated"}


# ============================================================================ Stages 17-18 — storage / history
def _write_new(path, data, readonly=False):
    return SUP._write_new(path, data, readonly)


def import_file(path, raw_dir=RAW_DIR, processed_dir=PROCESSED_DIR, cfg=None):
    cfg = cfg or load_cfg()
    try:
        rows = SUP.load_rows(path)
    except SUP.MalformedSupplierInput as e:
        raise MalformedCompetitorInput(str(e))
    retrieved = _now()
    raw_path = _write_new(Path(raw_dir) / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_manual_import.json",
                          {"source": "manual_import", "file_name": Path(path).name, "retrieved_at": retrieved,
                           "rows": rows}, readonly=True)
    accepted, rejected, by_pid = [], [], {}
    for i, row in enumerate(rows):
        c, errs = normalize_row(row, {"retrieved_at": retrieved, "raw_source_location": f"{raw_path}#row{i}"}, cfg)
        if errs:
            rejected.append({"row": i, "errors": errs})
            continue
        accepted.append(c)
        by_pid.setdefault(c["matched_product_id"], []).append(c)
    saved = [str(_write_new(Path(processed_dir) / pid /
                            f"competitors_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json",
                            {"product_id": pid, "provider": "manual_import", "raw_file": str(raw_path),
                             "observed_at": max(c["observed_at"] for c in cs), "competitors": cs}))
             for pid, cs in by_pid.items()]
    return {"raw_file": str(raw_path), "rows": len(rows), "accepted": len(accepted), "rejected": rejected,
            "products": sorted(by_pid), "processed_files": saved}


def load_latest(product_id, processed_dir=PROCESSED_DIR):
    """Observations of the most recent snapshot for the product (older snapshots stay on disk)."""
    files = sorted((Path(processed_dir) / str(product_id)).glob("competitors_*.json"))
    if not files:
        return []
    return json.loads(files[-1].read_text()).get("competitors", [])


def snapshot(analysis):
    return {"product_id": analysis["product_id"],
            "observed_at": max((c["observed_at"] for c in analysis["competitors"]), default=None),
            "qualified_competitor_count": analysis["qualified_competitor_count"],
            "active_advertisers": analysis["ads"]["active_meta_advertisers"],
            "total_active_ads": analysis["ads"]["total_active_ads"],
            "median_price": analysis["prices"]["median_price"],
            "offer_patterns": analysis["offer_patterns"],
            "direct_ids": sorted(c["competitor_id"] for c in analysis["competitors"] if c["relationship"] == "DIRECT")}


def append_history(analysis, hist_dir=HISTORY_DIR):
    snap = snapshot(analysis)
    h = hashlib.sha256(json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()
    d = Path(hist_dir) / analysis["product_id"]
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        if json.loads(f.read_text()).get("snapshot_hash") == h:
            return "duplicate_snapshot", f
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return "written", _write_new(d / f"{stamp}.json", {**snap, "snapshot_hash": h, "imported_at": _now()}, readonly=True)


def history(product_id, hist_dir=HISTORY_DIR, cfg=None):
    """Snapshots + changes (new / disappeared competitors, price, ads). Velocity needs >= 2 snapshots."""
    cfg = cfg or load_cfg()
    d = Path(hist_dir) / str(product_id)
    snaps = sorted((json.loads(f.read_text()) for f in d.glob("*.json")), key=lambda s: (s["observed_at"] or "", s["imported_at"])) \
        if d.exists() else []
    changes = []
    for a, b in zip(snaps, snaps[1:]):
        ids_a, ids_b = set(a["direct_ids"]), set(b["direct_ids"])
        changes.append({"from": a["observed_at"], "to": b["observed_at"],
                        "new_competitors": sorted(ids_b - ids_a), "disappeared_competitors": sorted(ids_a - ids_b),
                        "qualified_count_change": b["qualified_competitor_count"] - a["qualified_competitor_count"],
                        "median_price_change": (None if None in (a["median_price"], b["median_price"])
                                                else round(b["median_price"] - a["median_price"], 2)),
                        "active_advertisers_change": b["active_advertisers"] - a["active_advertisers"],
                        "offer_patterns_changed": a["offer_patterns"] != b["offer_patterns"]})
    velocity = None
    if len(snaps) >= cfg["history"]["min_snapshots_for_velocity"]:
        try:
            days = (datetime.fromisoformat(snaps[-1]["observed_at"]) - datetime.fromisoformat(snaps[0]["observed_at"])).days
        except (TypeError, ValueError):
            days = 0
        if days > 0:
            velocity = round((snaps[-1]["qualified_competitor_count"] - snaps[0]["qualified_competitor_count"]) / days * 7, 3)
    return {"product_id": str(product_id), "snapshots": snaps, "changes": changes,
            "competition_velocity_per_week": velocity,
            "note": None if velocity is not None else "competition_velocity needs >= 2 snapshots on different days"}


def calibration_plan(product_ids, cfg=None):
    c = (cfg or load_cfg())["competitor_calibration"]
    if not c.get("enabled"):
        return {"enabled": False, "products": [], "max_competitors_per_product": c["max_competitors_per_product"]}
    return {"enabled": True, "products": list(product_ids)[: c["max_products"]],
            "max_competitors_per_product": c["max_competitors_per_product"]}


def report_rows(products, processed_dir=PROCESSED_DIR, hist_dir=None, cfg=None):
    """For the final report: every reported product that has competitor research (read-only)."""
    out = []
    for p in products:
        pid = p.get("product_id")
        obs = load_latest(pid, processed_dir) if pid else []
        if not obs:
            continue
        b = p.get("bvs_record") or {}
        landed = ((b.get("economics") or {}).get("landed_cost"))
        a = analyze_product(pid, p.get("deep") or {}, obs, cfg, landed_cost=None if landed == NA else landed)
        if hist_dir:
            append_history(a, hist_dir)
        out.append({"product_id": pid, "name": p.get("name"), "analysis": {k: v for k, v in a.items() if k != "competitors"},
                    "competitors": [{k: c.get(k) for k in ("competitor_name", "competitor_domain", "relationship",
                                                           "match_confidence_calc", "platform", "selling_price",
                                                           "compare_at_price", "meta_ads_present", "active_ads_count",
                                                           "ad_age_days", "ad_longevity", "offer_features",
                                                           "review_count", "store_quality_score")}
                                    for c in a["competitors"]]})
    return out
