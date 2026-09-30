"""Supplier Data Integration (Step W): capture, normalize, score and rank REAL supplier offers.

NO orders are placed and NO supplier is contacted. Nothing is estimated: a missing value stays
None (N/A) and lowers Supplier Confidence. WPS / AVS are not touched; BVS consumes the SELECTED
offer through commercial_data_for(). All thresholds: config/suppliers.yaml.

Independent scores (never combined):
  Supplier Match Confidence (0-100)   is this really the same product?
  Supplier Quality Score    (0-100)   how good is the offer/supplier (from available evidence)?
  Supplier Confidence       (0-100)   how complete / reliable is the evidence?

Storage:
  data/raw/suppliers/                 raw imported records (read-only, never overwritten)
  data/processed/suppliers/<pid>/     normalized offers per import (new file each time)
  data/history/suppliers/<offer_id>/  append-only price/delivery/inventory history per offer

CLI (via the master runner):
  python -m winning_product_agent suppliers import <offers.csv|offers.json>
  python -m winning_product_agent suppliers show <product_id>
"""
import copy
import csv
import hashlib
import io
import json
import math
import os
import re
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import discovery as disc  # noqa: E402

NA = "N/A"
RAW_DIR = ROOT / "data" / "raw" / "suppliers"
PROCESSED_DIR = ROOT / "data" / "processed" / "suppliers"
HISTORY_DIR = ROOT / "data" / "history" / "suppliers"

# ============================================================================ Stage 1 — schema
OFFER_SCHEMA = {                       # field -> parse kind (discovery.parse_value kinds + bool/str)
    "offer_id": "str", "supplier_source": "str", "supplier_name": "str", "supplier_url": "str",
    "supplier_product_id": "str", "supplier_product_title": "str", "supplier_brand": "str",
    "matched_product_id": "id", "match_confidence": "num", "match_source": "str",
    "product_cost": "num", "currency": "str",
    "shipping_cost": "num", "shipping_method": "str", "import_duty_per_order": "num",
    "estimated_delivery_min_days": "num", "estimated_delivery_max_days": "num", "processing_time_days": "num",
    "ship_from_country": "str", "ship_to_country": "str", "us_warehouse_available": "bool",
    "minimum_order_quantity": "count", "inventory_status": "str", "available_quantity": "count",
    "supplier_rating": "num", "supplier_review_count": "count", "supplier_order_count": "count",
    "tracking_available": "bool", "returns_policy_available": "bool",
    "variants": "str", "package_weight_kg": "num", "package_dimensions_cm": "str",
    "observed_at": "str", "retrieved_at": "str", "raw_source_location": "str",
}
# manual-import column aliases -> schema field
ALIASES = {
    "rating": "supplier_rating", "reviews": "supplier_review_count", "review_count": "supplier_review_count",
    "orders": "supplier_order_count", "order_count": "supplier_order_count", "moq": "minimum_order_quantity",
    "MOQ": "minimum_order_quantity", "warehouse": "us_warehouse_available", "us_warehouse": "us_warehouse_available",
    "tracking": "tracking_available", "inventory": "available_quantity", "stock": "available_quantity",
    "weight": "package_weight_kg", "weight_kg": "package_weight_kg", "dimensions": "package_dimensions_cm",
    "title": "supplier_product_title", "product_title": "supplier_product_title", "brand": "supplier_brand",
    "variant": "variants", "processing_days": "processing_time_days", "source": "supplier_source",
}
INVENTORY_STATUSES = {"IN_STOCK", "LOW_STOCK", "OUT_OF_STOCK"}


class MalformedSupplierInput(ValueError):
    pass


class ProviderNotIntegrated(RuntimeError):
    """Asked for a supplier source the project has no real access to (never faked)."""


def load_cfg(path=ROOT / "config" / "suppliers.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


def _now():
    return datetime.now(timezone.utc).isoformat()


def parse_bool(v):
    if isinstance(v, bool):
        return v, None
    if v is None:
        return None, None
    s = str(v).strip().lower()
    if s in ("", "n/a", "na", "null", "none", "unknown", "-"):
        return None, None
    if s in ("true", "yes", "y", "1", "us", "usa"):
        return True, None
    if s in ("false", "no", "n", "0"):
        return False, None
    return None, f"not a yes/no value: {v!r}"


def parse_field(name, v):
    kind = OFFER_SCHEMA[name]
    if kind == "bool":
        return parse_bool(v)
    if kind == "str":
        s = None if v is None else str(v).strip()
        return (s or None), None
    return disc.parse_value(v, kind)


def offer_id_for(o):
    basis = "|".join(str(o.get(k) or "").strip().lower() for k in
                     ("supplier_source", "supplier_name", "supplier_url", "supplier_product_id", "variants",
                      "matched_product_id"))
    return "off_" + hashlib.sha1(basis.encode()).hexdigest()[:16]


def empty_offer():
    return {k: None for k in OFFER_SCHEMA}


# ============================================================================ Stage 2 — providers
class SupplierProvider(ABC):
    """One adapter per supplier source. BVS never talks to a platform directly."""
    name = "abstract"

    @abstractmethod
    def search_product(self, product):
        """Candidate raw offers for a finalist product (dict with product_id, name, ...)."""

    @abstractmethod
    def get_offer_details(self, ref):
        """Full raw offer for one candidate."""

    @abstractmethod
    def get_shipping_quote(self, ref, ship_to="US"):
        """Raw shipping data for one offer (cost, method, delivery range)."""

    @abstractmethod
    def normalize_offer(self, raw, meta):
        """Raw offer -> normalized schema dict (None for unavailable fields)."""


class ManualImportProvider(SupplierProvider):
    """Offers the user supplies in CSV / JSON. No network, no supplier contact."""
    name = "manual_import"

    def __init__(self, rows, source_location):
        self.rows, self.source_location = rows, source_location

    def search_product(self, product):
        pid = str(product.get("product_id") if isinstance(product, dict) else product)
        return [r for r in self.rows if str(r.get("matched_product_id")) == pid]

    def get_offer_details(self, ref):
        return ref                                           # the imported row IS the offer detail

    def get_shipping_quote(self, ref, ship_to="US"):
        return {k: ref.get(k) for k in ("shipping_cost", "shipping_method", "estimated_delivery_min_days",
                                        "estimated_delivery_max_days", "ship_from_country")} | {"ship_to_country":
                                                                                              ref.get("ship_to_country") or ship_to}

    def normalize_offer(self, raw, meta):
        return normalize_row(raw, meta)[0]


def get_provider(name, cfg=None, **kw):
    cfg = cfg or load_cfg()
    p = (cfg.get("providers") or {}).get(name)
    if not p or not p.get("implemented"):
        raise ProviderNotIntegrated(f"supplier source '{name}' is not integrated in this project "
                                    f"({(p or {}).get('note', 'unknown source')}); use manual_import")
    if name == "manual_import":
        return ManualImportProvider(kw.get("rows", []), kw.get("source_location"))
    raise ProviderNotIntegrated(name)


# ============================================================================ Stage 3 — manual import
def load_rows(path):
    """CSV or JSON (list, or {"offers": [...]}) -> list of dict rows. Raises MalformedSupplierInput."""
    p = Path(path)
    try:
        text = p.read_text()
    except OSError as e:
        raise MalformedSupplierInput(f"cannot read {p.name}: {e.__class__.__name__}")
    if p.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise MalformedSupplierInput(f"invalid JSON: {e.msg}")
        rows = data.get("offers") if isinstance(data, dict) else data
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise MalformedSupplierInput("JSON must be a list of offer objects (or {\"offers\": [...]})")
        return rows
    if p.suffix.lower() == ".csv":
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            raise MalformedSupplierInput("CSV has no data rows")
        return [{k.strip(): v for k, v in r.items() if k is not None} for r in rows]
    raise MalformedSupplierInput("only .csv or .json supplier files are accepted")


def normalize_row(row, meta, cfg=None):
    """Returns (offer, errors). Required fields must be present and valid; nothing is filled in."""
    cfg = cfg or load_cfg()
    mi = cfg["manual_import"]
    o, errors = empty_offer(), []
    src = {ALIASES.get(k, k): v for k, v in row.items()}
    for k in OFFER_SCHEMA:
        if k in src:
            val, prob = parse_field(k, src[k])
            o[k] = val
            if prob:
                errors.append(f"{k}: {prob}")
    for k in mi["required_fields"]:
        if o.get(k) is None:
            errors.append(f"missing required field: {k}")
    lo, hi = o["estimated_delivery_min_days"], o["estimated_delivery_max_days"]
    if lo is not None and hi is not None and lo > hi:
        errors.append(f"estimated_delivery_min_days {lo} > estimated_delivery_max_days {hi}")
    if o["supplier_url"] and not re.match(r"^https?://", o["supplier_url"]):
        errors.append("supplier_url must start with http:// or https://")
    if o["supplier_rating"] is not None and not 0 <= o["supplier_rating"] <= 5:
        errors.append(f"supplier_rating {o['supplier_rating']} outside 0-5")
    if o["match_confidence"] is not None and not 0 <= o["match_confidence"] <= 100:
        errors.append(f"match_confidence {o['match_confidence']} outside 0-100")
    o["currency"] = (o["currency"] or mi["currency_default"]).upper()
    if o["currency"] not in mi["accepted_currencies"]:
        errors.append(f"currency {o['currency']} not accepted (no FX conversion is invented)")
    if o["inventory_status"]:
        st = o["inventory_status"].upper().replace(" ", "_")
        if st not in INVENTORY_STATUSES:
            errors.append(f"inventory_status must be one of {sorted(INVENTORY_STATUSES)}")
        o["inventory_status"] = st
    o["supplier_source"] = o["supplier_source"] or meta.get("supplier_source", "manual_import")
    o["match_source"] = "manual" if o["match_confidence"] is not None else None
    o["retrieved_at"] = meta.get("retrieved_at") or _now()
    o["observed_at"] = o["observed_at"] or o["retrieved_at"]
    o["raw_source_location"] = meta.get("raw_source_location")
    o["ship_to_country"] = o["ship_to_country"] or "US"
    o["offer_id"] = offer_id_for(o)
    return o, errors


# ============================================================================ Stage 4 — matching
def _tokens(text, cfg):
    stop = set(cfg["matching"]["stopwords"])
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in stop and len(t) > 1}


def _models(text):
    return {t for t in re.findall(r"\b[A-Za-z]*\d+[A-Za-z0-9-]*[A-Za-z]+[A-Za-z0-9-]*\b|\b[A-Za-z]+-?\d{2,}[A-Za-z0-9-]*\b",
                                  text or "") if len(t) >= 3}


def _dims(text):
    return {f"{float(n):g}{u.lower()}" for n, u in
            re.findall(r"(\d+(?:\.\d+)?)\s*(ml|oz|cm|mm|in|inch|pcs|pc|pack|count|ct|g|kg|lb|ft)\b", (text or "").lower())}


def _dice(a, b):
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else None


def match_confidence(tiktok, offer, cfg=None):
    """tiktok: {name, brand, variant}. Returns (score 0-100 or None, class, detail). Deterministic."""
    cfg = cfg or load_cfg()
    m = cfg["matching"]
    if offer.get("match_source") == "manual" and offer.get("match_confidence") is not None:
        sc = float(offer["match_confidence"])
        return sc, classify_match(sc, cfg), {"source": "manual (user-verified value from the import)"}
    title = offer.get("supplier_product_title")
    tk_name = tiktok.get("name") or ""
    s = {}
    s["name_tokens"] = _dice(_tokens(tk_name, cfg), _tokens(title, cfg)) if title else None
    fw = set(m["function_words"])
    f1, f2 = _tokens(tk_name, cfg) & fw, _tokens(title, cfg) & fw
    s["function"] = (len(f1 & f2) / len(f1 | f2) if (f1 | f2) else None) if title else None
    m1, m2 = {x.lower() for x in _models(tk_name)}, {x.lower() for x in _models(title)}
    s["model"] = (1.0 if m1 & m2 else 0.0) if (m1 and m2) else None
    d1, d2 = _dims(tk_name + " " + (tiktok.get("variant") or "")), _dims((title or "") + " " + (offer.get("variants") or ""))
    s["dimensions"] = (len(d1 & d2) / len(d1 | d2)) if (d1 and d2) else None
    v1, v2 = _tokens(tiktok.get("variant"), cfg), _tokens(offer.get("variants"), cfg)
    s["variant"] = _dice(v1, v2)
    b1, b2 = (tiktok.get("brand") or "").strip().lower(), (offer.get("supplier_brand") or "").strip().lower()
    s["brand"] = (1.0 if b1 == b2 else 0.0) if (b1 and b2) else None
    avail = {k: v for k, v in s.items() if v is not None}
    pts = {k: m["components"][k]["points"] for k in s}
    have = sum(pts[k] for k in avail)
    if have < m["min_evidence_points"]:
        return None, "REVIEW", {"components": s, "available_points": have,
                                "reason": f"match evidence {have} < {m['min_evidence_points']} points"}
    sc = round(100 * sum(pts[k] * v for k, v in avail.items()) / have, 2)
    return sc, classify_match(sc, cfg), {"components": {k: None if v is None else round(v, 4) for k, v in s.items()},
                                         "available_points": have}


def classify_match(score, cfg):
    if score is None:
        return "REVIEW"
    for label, t in sorted(cfg["matching"]["classes"].items(), key=lambda kv: -kv[1]):
        if score >= t:
            return label
    return "UNRELIABLE"


# ============================================================================ Stages 5-8 — scores
def landed_cost(o, cfg=None):
    """product_cost + shipping_cost (+ import duty ONLY when explicitly provided)."""
    pc, sh, duty = o.get("product_cost"), o.get("shipping_cost"), o.get("import_duty_per_order")
    if pc is None or sh is None:
        return None, "N/A: product_cost and shipping_cost are both required"
    if duty is not None:
        return round(pc + sh + duty, 2), "includes explicit import duty"
    return round(pc + sh, 2), "EXCLUDES unknown import duties (none provided; never estimated)"


def effective_delivery_days(o, cfg):
    hi = o.get("estimated_delivery_max_days")
    if hi is None:
        return None
    proc = o.get("processing_time_days")
    return hi + (proc if cfg["delivery"]["add_processing_time"] and proc is not None else 0)


def delivery_tier(o, cfg):
    days = effective_delivery_days(o, cfg)
    if days is None:
        return None, None, None
    for t in cfg["delivery"]["tiers"]:
        if t["max_days"] is None or days <= t["max_days"]:
            return t["tier"], t["score"], days
    return None, None, days


def _band_ge(v, bands):
    for thr, s in bands:
        if v >= thr:
            return s
    return 0.0


def _band_le(v, bands):
    for thr, s in bands:
        if v <= thr:
            return s
    return 0.0


def _log(v, z, f):
    if v <= 0:
        return 0.0
    return max(0.0, min(1.0, (math.log10(v) - math.log10(z)) / (math.log10(f) - math.log10(z))))


def supplier_quality(o, cfg):
    """0-100 over AVAILABLE inputs; missing inputs are listed (they lower Supplier Confidence)."""
    q = cfg["quality"]["components"]
    s = {}
    s["rating"] = _band_ge(o["supplier_rating"], q["rating"]["bands"]) if o.get("supplier_rating") is not None else None
    for k, f in (("review_count", "supplier_review_count"), ("order_count", "supplier_order_count")):
        s[k] = _log(o[f], q[k]["zero_at"], q[k]["full_at"]) if o.get(f) is not None else None
    s["processing_time"] = (_band_le(o["processing_time_days"], q["processing_time"]["bands_max"])
                            if o.get("processing_time_days") is not None else None)
    s["tracking"] = None if o.get("tracking_available") is None else float(o["tracking_available"])
    if o.get("inventory_status") == "OUT_OF_STOCK":
        s["inventory"] = 0.0
    else:
        s["inventory"] = (_band_ge(o["available_quantity"], q["inventory"]["bands"])
                          if o.get("available_quantity") is not None else None)
    s["us_warehouse"] = None if o.get("us_warehouse_available") is None else float(o["us_warehouse_available"])
    s["moq"] = _band_le(o["minimum_order_quantity"], q["moq"]["bands_max"]) if o.get("minimum_order_quantity") is not None else None
    have = sum(q[k]["points"] for k, v in s.items() if v is not None)
    missing = [k for k, v in s.items() if v is None]
    if have < cfg["quality"]["min_available_points"]:
        return None, {"components": s, "available_points": have, "missing": missing}
    score = round(100 * sum(q[k]["points"] * v for k, v in s.items() if v is not None) / have, 2)
    return score, {"components": {k: None if v is None else round(v, 4) for k, v in s.items()},
                   "available_points": have, "missing": missing}


def supplier_confidence(o, match, cfg):
    c = cfg["confidence"]["components"]
    has = lambda k: o.get(k) is not None  # noqa: E731
    cov = {
        "product_match": (match / 100) if match is not None else 0.0,
        "product_cost": float(has("product_cost")),
        "shipping_cost": float(has("shipping_cost")),
        "delivery_estimate": 1.0 if has("estimated_delivery_min_days") and has("estimated_delivery_max_days")
        else (0.7 if has("estimated_delivery_max_days") else 0.0),
        "reputation": (1.0 if has("supplier_rating") and (has("supplier_review_count") or has("supplier_order_count"))
                       else 0.5 if (has("supplier_rating") or has("supplier_review_count") or has("supplier_order_count"))
                       else 0.0),
        "inventory": float(has("available_quantity") or has("inventory_status")),
        "tracking": float(has("tracking_available")),
        "moq": float(has("minimum_order_quantity")),
    }
    score = round(sum(c[k]["points"] * v for k, v in cov.items()), 2)
    level = next(lv for lv, t in sorted(cfg["confidence"]["levels"].items(), key=lambda kv: -kv[1]) if score >= t)
    return score, level, {k: round(v, 4) for k, v in cov.items()}


def offer_flags(o, cfg, selling_price=None):
    f, out = cfg["flags"], []
    if o["match_confidence_calc"] is not None and o["match_confidence_calc"] < f["UNRELIABLE_PRODUCT_MATCH"]["match_below"]:
        out.append({"flag": "UNRELIABLE_PRODUCT_MATCH", "match_confidence": o["match_confidence_calc"]})
    if selling_price:
        if o.get("product_cost") is not None and o["product_cost"] / selling_price * 100 > \
                f["HIGH_PRODUCT_COST"]["product_cost_pct_of_price_above"]:
            out.append({"flag": "HIGH_PRODUCT_COST", "pct_of_price": round(o["product_cost"] / selling_price * 100, 2)})
        if o.get("shipping_cost") is not None and o["shipping_cost"] / selling_price * 100 > \
                f["HIGH_SHIPPING_COST"]["shipping_cost_pct_of_price_above"]:
            out.append({"flag": "HIGH_SHIPPING_COST", "pct_of_price": round(o["shipping_cost"] / selling_price * 100, 2)})
    d = o.get("effective_delivery_days")
    if d is not None and d > f["VERY_SLOW_DELIVERY"]["effective_days_above"]:
        out.append({"flag": "VERY_SLOW_DELIVERY", "effective_days": d})
    elif d is not None and d > f["SLOW_DELIVERY"]["effective_days_above"]:
        out.append({"flag": "SLOW_DELIVERY", "effective_days": d})
    if o.get("tracking_available") is False:
        out.append({"flag": "NO_TRACKING"})
    if o.get("supplier_rating") is not None and o["supplier_rating"] < f["LOW_SUPPLIER_RATING"]["rating_below"]:
        out.append({"flag": "LOW_SUPPLIER_RATING", "rating": o["supplier_rating"]})
    if o.get("supplier_order_count") is not None and o["supplier_order_count"] < f["LOW_ORDER_HISTORY"]["order_count_below"]:
        out.append({"flag": "LOW_ORDER_HISTORY", "orders": o["supplier_order_count"]})
    if o.get("minimum_order_quantity") is not None and o["minimum_order_quantity"] > f["MOQ_TOO_HIGH"]["moq_above"]:
        out.append({"flag": "MOQ_TOO_HIGH", "moq": o["minimum_order_quantity"]})
    if o.get("us_warehouse_available") is False:
        out.append({"flag": "NO_US_WAREHOUSE"})
    li = f["LOW_INVENTORY"]
    if (o.get("available_quantity") is not None and o["available_quantity"] < li["available_quantity_below"]) or \
            o.get("inventory_status") in li["statuses"]:
        out.append({"flag": "LOW_INVENTORY", "available_quantity": o.get("available_quantity"),
                    "status": o.get("inventory_status")})
    if o["supplier_confidence"] < f["SUPPLIER_DATA_INCOMPLETE"]["confidence_below"]:
        out.append({"flag": "SUPPLIER_DATA_INCOMPLETE", "supplier_confidence": o["supplier_confidence"]})
    return out


def score_offer(offer, tiktok, cfg, selling_price=None):
    o = copy.deepcopy(offer)
    match, cls, mdet = match_confidence(tiktok, o, cfg)
    o["match_confidence_calc"], o["match_class"], o["match_detail"] = match, cls, mdet
    el = cfg["ranking"]["eligibility"]
    o["supplier_match_status"] = "OK" if match is not None and match >= cfg["matching"]["min_for_economics"] else "REVIEW"
    o["landed_cost"], o["landed_cost_note"] = landed_cost(o, cfg)
    o["delivery_tier"], o["delivery_score"], o["effective_delivery_days"] = delivery_tier(o, cfg)
    o["supplier_quality"], o["quality_detail"] = supplier_quality(o, cfg)
    o["supplier_confidence"], o["supplier_confidence_level"], o["confidence_detail"] = supplier_confidence(o, match, cfg)
    reasons = []
    if match is None or match < el["min_match_confidence"]:
        reasons.append(f"match confidence {match} < {el['min_match_confidence']}")
    reasons += [f"{k} missing" for k in el["require"] if o.get(k) is None]
    if o.get("inventory_status") in el["exclude_inventory_status"]:
        reasons.append(f"inventory {o['inventory_status']}")
    o["eligible_for_economics"], o["ineligible_reasons"] = not reasons, reasons
    o["supplier_flags"] = offer_flags(o, cfg, selling_price)
    return o


# ============================================================================ Stages 9-10 — rank / select
TIER_ORDER = ["STRONGEST", "STRONG", "ACCEPTABLE", "WEAK", "POOR"]


def rank_key(o, cfg):
    key = []
    for k in cfg["ranking"]["rank_by"]:
        if k == "eligible":
            key.append(0 if o["eligible_for_economics"] else 1)
        elif k == "delivery_tier":
            key.append(TIER_ORDER.index(o["delivery_tier"]) if o["delivery_tier"] in TIER_ORDER else 99)
        elif k in ("supplier_quality", "supplier_confidence"):
            key += [o[k] is None, -(o[k] or 0)]
        elif k == "landed_cost":
            key += [o[k] is None, o[k] or 0]
        elif k == "offer_id":
            key.append(o["offer_id"])
        else:
            raise ValueError(f"unknown rank key {k}")
    return key


def evaluate_product(product_id, tiktok, offers, cfg=None, selling_price=None):
    """All offers for one product -> scored, ranked; selected = highest-ranked ELIGIBLE offer (not cheapest)."""
    cfg = cfg or load_cfg()
    scored = sorted((score_offer(o, tiktok, cfg, selling_price) for o in offers), key=lambda o: rank_key(o, cfg))
    for i, o in enumerate(scored, 1):
        o["supplier_offer_rank"] = i
    qualified = [o for o in scored if o["eligible_for_economics"]]
    selected = qualified[0] if qualified else None
    pflags = []
    if len(qualified) == 1:
        pflags.append({"flag": "SINGLE_SUPPLIER_DEPENDENCY", "qualified_offers": 1})
    if not qualified:
        pflags.append({"flag": "SUPPLIER_DATA_INCOMPLETE", "reason": "no qualified supplier offer"})
    if selected:
        pflags += [f for f in selected["supplier_flags"] if f["flag"] not in {x["flag"] for x in pflags}]
    return {"product_id": str(product_id), "offers": scored, "offer_count": len(scored),
            "qualified_offers": len(qualified),
            "selected_supplier_offer_id": selected["offer_id"] if selected else None,
            "supplier_viability": "AVAILABLE" if selected else NA,
            "supplier_flags": pflags, "config_version": cfg.get("version"),
            "selection_rule": "highest-ranked eligible offer; rank = " + " > ".join(cfg["ranking"]["rank_by"])}


def to_commercial_data(ev):
    """Selected offer -> the commercial-data dict BVS already understands (selected supplier FIRST)."""
    sel = next((o for o in ev["offers"] if o["offer_id"] == ev["selected_supplier_offer_id"]), None)
    if sel is None:
        return {"supplier_layer": summary(ev)}
    qualified = [o for o in ev["offers"] if o["eligible_for_economics"]]
    ordered = [sel] + [o for o in qualified if o is not sel]

    def legacy(o):
        wh = o.get("us_warehouse_available")
        return {"name": o["supplier_name"], "price": o["product_cost"], "rating": o.get("supplier_rating"),
                "processing_days": o.get("processing_time_days"), "us_warehouse": wh,
                "stock": o.get("available_quantity"), "moq": o.get("minimum_order_quantity"),
                "orders": o.get("supplier_order_count"), "warehouse_country": o.get("ship_from_country"),
                "offer_id": o["offer_id"]}
    comm = {"product_cost": sel["product_cost"], "supplier_shipping_cost": sel["shipping_cost"],
            "delivery_days_max": sel["effective_delivery_days"], "tracking_available": sel.get("tracking_available"),
            "suppliers": [legacy(o) for o in ordered], "selected_supplier_index": 0,
            "supplier_count": len(qualified), "supplier_layer": summary(ev)}
    if sel.get("import_duty_per_order") is not None:
        comm["import_duty_per_order"] = sel["import_duty_per_order"]
    if sel.get("package_weight_kg") is not None:
        comm["weight_kg"] = sel["package_weight_kg"]
    dims = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", sel.get("package_dimensions_cm") or "")]
    if dims:
        comm["longest_side_cm"] = max(dims)
    return comm


def summary(ev):
    sel = next((o for o in ev["offers"] if o["offer_id"] == ev["selected_supplier_offer_id"]), None)
    keep = ("offer_id", "supplier_name", "supplier_source", "supplier_url", "supplier_offer_rank", "match_confidence_calc",
            "match_class", "supplier_match_status", "product_cost", "shipping_cost", "landed_cost", "landed_cost_note",
            "estimated_delivery_min_days", "estimated_delivery_max_days", "effective_delivery_days", "delivery_tier",
            "supplier_rating", "supplier_order_count", "us_warehouse_available", "tracking_available",
            "minimum_order_quantity", "available_quantity", "inventory_status", "supplier_quality",
            "supplier_confidence", "supplier_confidence_level", "eligible_for_economics", "ineligible_reasons",
            "supplier_flags", "observed_at")
    return {"selected_supplier_offer_id": ev["selected_supplier_offer_id"], "qualified_offers": ev["qualified_offers"],
            "offer_count": ev["offer_count"], "supplier_viability": ev["supplier_viability"],
            "supplier_flags": ev["supplier_flags"], "selection_rule": ev["selection_rule"],
            "selected": {k: sel.get(k) for k in keep} if sel else None,
            "offers": [{k: o.get(k) for k in keep} for o in ev["offers"]]}


# ============================================================================ Stages 13-14 — storage / history
def _write_new(path, data, readonly=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base, n = path, 1
    while path.exists():
        n += 1
        path = base.with_name(f"{base.stem}_{n}{base.suffix}")
    with open(path, "x") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    if readonly:
        os.chmod(path, 0o444)
    return path


def snapshot_hash(o):
    keys = ("offer_id", "observed_at", "product_cost", "shipping_cost", "estimated_delivery_min_days",
            "estimated_delivery_max_days", "available_quantity", "inventory_status")
    return hashlib.sha256(json.dumps({k: o.get(k) for k in keys}, sort_keys=True, default=str).encode()).hexdigest()


def append_history(o, hist_dir=HISTORY_DIR):
    """One immutable file per offer observation; the same snapshot twice is skipped."""
    d = Path(hist_dir) / o["offer_id"]
    h = snapshot_hash(o)
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            if json.loads(f.read_text()).get("snapshot_hash") == h:
                return "duplicate_snapshot", f
        except (OSError, json.JSONDecodeError):
            continue
    lc, _ = landed_cost(o)
    rec = {"offer_id": o["offer_id"], "matched_product_id": o["matched_product_id"], "supplier_name": o["supplier_name"],
           "observed_at": o["observed_at"], "retrieved_at": o["retrieved_at"], "imported_at": _now(),
           "product_cost": o["product_cost"], "shipping_cost": o["shipping_cost"], "landed_cost": lc,
           "estimated_delivery_min_days": o["estimated_delivery_min_days"],
           "estimated_delivery_max_days": o["estimated_delivery_max_days"],
           "available_quantity": o.get("available_quantity"), "inventory_status": o.get("inventory_status"),
           "raw_source_location": o.get("raw_source_location"), "snapshot_hash": h}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return "written", _write_new(d / f"{stamp}.json", rec, readonly=True)


def price_history(offer_id, hist_dir=HISTORY_DIR):
    """Chronological quotes for one offer + changes between consecutive quotes (for future alerts)."""
    d = Path(hist_dir) / offer_id
    rows = sorted((json.loads(f.read_text()) for f in d.glob("*.json")), key=lambda r: (r["observed_at"], r["imported_at"])) \
        if d.exists() else []
    changes = []
    for a, b in zip(rows, rows[1:]):
        ch = {}
        for k in ("product_cost", "shipping_cost", "landed_cost", "estimated_delivery_max_days", "available_quantity"):
            if a.get(k) is not None and b.get(k) is not None and a[k] != b[k]:
                ch[k] = {"from": a[k], "to": b[k], "pct": round((b[k] - a[k]) / a[k] * 100, 2) if a[k] else None}
        if ch:
            changes.append({"from": a["observed_at"], "to": b["observed_at"], "changes": ch})
    return {"offer_id": offer_id, "observations": rows, "changes": changes}


def import_file(path, raw_dir=RAW_DIR, processed_dir=PROCESSED_DIR, hist_dir=HISTORY_DIR, cfg=None):
    """Manual import: validate every row; store raw + normalized + history. Rejected rows are reported, never stored
    as offers. Returns summary."""
    cfg = cfg or load_cfg()
    rows = load_rows(path)
    retrieved = _now()
    raw_path = _write_new(Path(raw_dir) / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_manual_import.json",
                          {"source": "manual_import", "file_name": Path(path).name, "retrieved_at": retrieved,
                           "rows": rows}, readonly=True)
    accepted, rejected, by_pid = [], [], {}
    prov = get_provider("manual_import", cfg, rows=rows, source_location=str(raw_path))
    for i, row in enumerate(rows):
        o, errs = normalize_row(row, {"retrieved_at": retrieved, "raw_source_location": f"{raw_path}#row{i}"}, cfg)
        if errs:
            rejected.append({"row": i, "errors": errs})
            continue
        accepted.append(o)
        by_pid.setdefault(o["matched_product_id"], []).append(o)
    hist = [append_history(o, hist_dir) for o in accepted]
    saved = [str(_write_new(Path(processed_dir) / pid /
                            f"offers_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json",
                            {"product_id": pid, "provider": prov.name, "raw_file": str(raw_path), "offers": offs}))
             for pid, offs in by_pid.items()]
    return {"raw_file": str(raw_path), "rows": len(rows), "accepted": len(accepted), "rejected": rejected,
            "products": sorted(by_pid), "processed_files": saved,
            "history": {"written": sum(h[0] == "written" for h in hist),
                        "duplicate_snapshots_skipped": sum(h[0] == "duplicate_snapshot" for h in hist)}}


def load_offers(product_id, processed_dir=PROCESSED_DIR):
    """Latest observation of every offer ever imported for the product (older quotes stay in history)."""
    latest = {}
    for f in sorted((Path(processed_dir) / str(product_id)).glob("offers_*.json")):
        for o in json.loads(f.read_text()).get("offers", []):
            cur = latest.get(o["offer_id"])
            if cur is None or (o["observed_at"], o["retrieved_at"]) >= (cur["observed_at"], cur["retrieved_at"]):
                latest[o["offer_id"]] = o
    return list(latest.values())


def tiktok_view(deep):
    return {"name": deep.get("product_name"), "brand": (deep.get("shop") or {}).get("brand"),
            "variant": deep.get("variant")}


def commercial_data_for(deep, processed_dir=PROCESSED_DIR, cfg=None, selling_price=None, max_offers=None):
    """(commercial_data, source) from the supplier layer, or (None, None) if no offer was imported.
    max_offers (Step AA): collection cap — the first N offers by offer_id are considered (deterministic)."""
    offers = load_offers(deep.get("product_id"), processed_dir)
    if not offers:
        return None, None
    if max_offers:
        offers = sorted(offers, key=lambda o: str(o.get("offer_id")))[:max_offers]
    ev = evaluate_product(deep.get("product_id"), tiktok_view(deep), offers, cfg, selling_price)
    return to_commercial_data(ev), f"supplier_layer:{processed_dir}"


def calibration_plan(product_ids, cfg=None):
    """Stage 17: which products / how many offers a supplier calibration may look at (small by design)."""
    c = (cfg or load_cfg())["supplier_calibration"]
    if not c.get("enabled"):
        return {"enabled": False, "products": [], "max_offers_per_product": c["max_offers_per_product"]}
    return {"enabled": True, "products": list(product_ids)[: c["max_products"]],
            "max_offers_per_product": c["max_offers_per_product"]}
