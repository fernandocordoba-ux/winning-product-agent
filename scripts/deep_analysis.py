"""Deep Analysis (Step M): detailed KaloPilot data for the best Discovery candidates.

Pipeline (deterministic; thresholds in config/deep_analysis.yaml, filters.yaml, scoring.yaml):
  latest data/processed/discovery_*.json
    -> select_candidates()      PASS then REVIEW, FAIL excluded, max N (default 40)
    -> plan()                   query per product/type; cache hits; cost estimate  (FREE)
    -> fetch (LIVE only)        balance check before each paid query, dedupe, cache,
                                raw saved read-only to data/raw/deep_analysis/
    -> normalize_deep()         source facts (null = N/A, real 0 kept)
    -> analyze()                trend, concentration, WPS, Confidence, red flags
    -> save_results()           data/processed/deep_analysis/deep_<ts>.json (new file)

CLI:
  python3 scripts/deep_analysis.py                 # DRY-RUN (default): no credits spent
  python3 scripts/deep_analysis.py --max 10        # dry-run, first 10 products
  python3 scripts/deep_analysis.py --live          # SPENDS CREDITS (only after user OK)
"""
import copy
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import concentration  # noqa: E402
import provenance  # noqa: E402
import discovery as disc  # noqa: E402
from score_products import load_scoring_config, score_product  # noqa: E402

NA = "N/A"
RAW_DIR = ROOT / "data" / "raw" / "deep_analysis"
OUT_DIR = ROOT / "data" / "processed" / "deep_analysis"


def load_cfg():
    import config_resolver as _CR
    with open(_CR.path("deep_analysis.yaml")) as f:
        return yaml.safe_load(f)


# ============================================================ Stage 1 — input
def latest_discovery(processed_dir=ROOT / "data" / "processed"):
    files = sorted(Path(processed_dir).glob("discovery_*.json"))
    return files[-1] if files else None


def preliminary_score(product, scoring_cfg=None):
    """PRELIMINARY WPS from Discovery facts only (growth, units, videos, creators): share of the available WPS
    points, 0-100. Used only to choose which products get a paid deep analysis; never shown as the WPS."""
    from score_products import wps_breakdown
    f = product.get("facts") or {}
    sin = {"revenue_growth_pct": f.get("growth_30d"), "units_sold": f.get("units_30d"),
           "videos_count": f.get("video_count"), "creators_count": f.get("creator_count")}
    b = wps_breakdown(sin, scoring_cfg or load_scoring_config())
    return round(100 * b["points_earned"] / b["points_possible"], 2) if b.get("points_possible") else None


def _seasonal(product, keywords):
    name = (product.get("facts") or {}).get("product_name") or ""
    return sorted({k for k in keywords or [] if re.search(rf"\b{re.escape(k)}", name, re.I)})


def select_candidates(discovery_result, cfg, scoring_cfg=None):
    """PASS before REVIEW (keeping Discovery's order inside each status), FAIL excluded.
    selection.order = preliminary_wps -> highest preliminary score first (status as tie-break).
    selection.exclude_keywords -> seasonal products are listed but not deep-analyzed."""
    sel = cfg["selection"]
    eligible = set(sel["eligible_statuses"])
    pool = [p for p in discovery_result.get("candidates", [])
            if p["calculated"]["filter_status"] in eligible]
    rank = {s: i for i, s in enumerate(sel["status_priority"])}
    skipped = []
    if sel.get("exclude_keywords"):
        keep = []
        for p in pool:
            hit = _seasonal(p, sel["exclude_keywords"])
            if hit:
                skipped.append({"key": p["key"], "reason": f"seasonal keyword {hit}: not deep-analyzed"})
            else:
                keep.append(p)
        pool = keep
    if sel.get("order") == "preliminary_wps":
        for p in pool:
            p["calculated"]["preliminary_wps"] = preliminary_score(p, scoring_cfg)
        ordered = sorted(enumerate(pool), key=lambda ip: (-(ip[1]["calculated"]["preliminary_wps"] or 0),
                                                          rank.get(ip[1]["calculated"]["filter_status"], 99), ip[0]))
    else:
        ordered = sorted(enumerate(pool), key=lambda ip: (rank.get(ip[1]["calculated"]["filter_status"], 99), ip[0]))
    selected = []
    for _, p in ordered:
        if not any(p["facts"].get(k) for k in sel["require_identifier"]):
            skipped.append({"key": p["key"], "reason": "no product_id or product_url to query"})
            continue
        selected.append(p)
    limit = sel["deep_analysis_max_products"]
    skipped += [{"key": p["key"], "reason": f"over deep_analysis_max_products ({limit})"} for p in selected[limit:]]
    return selected[:limit], skipped


# ============================================================ Stage 2 — queries
def build_query(product, cfg, market=None):
    market = market or {"region": "US", "currency": "USD"}
    text = (ROOT / "prompts" / "deep_analysis.md").read_text()
    template = re.split(r"^===\s*$", text, flags=re.M)[1].strip()
    f = product["facts"]
    ref = f.get("product_url") or f"TikTok Shop product ID {f['product_id']}"
    if f.get("product_id") and f.get("product_url"):
        ref = f"{f['product_url']} (product ID {f['product_id']})"
    q = cfg["queries"]
    return template.format(region=market["region"], currency=market["currency"], period_days=q["period_days"],
                           product_ref=ref, top_creators=q["top_creators"], top_videos=q["top_videos"])


def product_ref_id(product):
    return product["facts"].get("product_id") or product["key"]


def find_cached(product_id, query_type, ttl_hours, now, raw_dir=RAW_DIR):
    """Most recent raw response for product+type fetched within ttl (completed only)."""
    best = None
    for path in sorted(Path(raw_dir).glob(f"*_{product_id}_{query_type}*.json")):
        try:
            env = json.loads(path.read_text())
            ts = datetime.fromisoformat(env["observation_timestamp"])
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
        status = ((env.get("response") or {}).get("data") or {}).get("status")
        if status == "completed" and now - ts <= timedelta(hours=ttl_hours):
            best = path
    return best


def plan(selected, cfg, now=None, raw_dir=RAW_DIR, market=None):
    """FREE: what would be queried, cache hits, estimated credits."""
    now = now or datetime.now(timezone.utc)
    cp = cfg["credit_protection"]
    items, seen = [], set()
    for p in selected:
        pid = product_ref_id(p)
        for qt in cfg["queries"]["types"]:
            if (pid, qt) in seen:
                items.append({"product_id": pid, "query_type": qt, "action": "skip_duplicate"})
                continue
            seen.add((pid, qt))
            cached = find_cached(pid, qt, cp["cache_ttl_hours"], now, raw_dir)
            items.append({"product_id": pid, "product_name": p["facts"].get("product_name"),
                          "filter_status": p["calculated"]["filter_status"], "query_type": qt,
                          "action": "use_cache" if cached else "paid_query",
                          "cached_raw": str(cached) if cached else None,
                          "query": build_query(p, cfg, market)})
    paid = sum(i["action"] == "paid_query" for i in items)
    return {"items": items, "paid_queries": paid, "cache_hits": sum(i["action"] == "use_cache" for i in items),
            "estimated_credits": round(paid * cp["estimated_credits_per_query"], 2)}


# ============================================================ Stage 3 — raw storage
def save_raw_deep(response, meta, raw_dir=RAW_DIR):
    """Unmodified response + required metadata. New file, read-only, never overwritten."""
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    for k in ("observation_timestamp", "market", "product_id", "query_type"):
        if not meta.get(k):
            raise ValueError(f"raw record missing required metadata: {k}")
    stamp = datetime.fromisoformat(meta["observation_timestamp"]).strftime("%Y%m%dT%H%M%S%fZ")
    base = f"{stamp}_{meta['product_id']}_{meta['query_type']}"
    path, n = raw_dir / f"{base}.json", 1
    while path.exists():
        n += 1
        path = raw_dir / f"{base}_{n}.json"
    with open(path, "x") as fh:
        json.dump({"source": "kalopilot", **meta, "response": response}, fh, ensure_ascii=False, indent=2)
    os.chmod(path, 0o444)
    return path


def extract_object(envelope):
    """The product object from the JSON block. Returns (obj or None, errors)."""
    records, errors = disc.extract_records(envelope)
    if records:
        obj = records[0]
        return (obj, errors) if isinstance(obj, dict) else (None, errors + ["JSON block is not an object"])
    data = ((envelope.get("response") or {}).get("data") or {})
    for field in ("report", "text"):
        blob = data.get(field)
        if isinstance(blob, str):
            for m in re.finditer(r"```json\s*(.*?)```", blob, re.S):
                try:
                    obj = json.loads(m.group(1))
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    return obj, []
    return None, errors


# ============================================================ normalize
SCALARS = [("product_id", "id"), ("product_name", "str"), ("product_url", "str"), ("category_path", "str"),
           ("category_id", "id"), ("shop_id", "id"), ("shop_name", "str"), ("price_min", "num"),
           ("price_max", "num"), ("gmv_30d", "num"), ("units_30d", "count"), ("growth_30d_pct", "pct"),
           ("category_growth_pct", "pct"), ("launch_date", "date"), ("data_window_end", "date"),
           ("commission_pct", "num"), ("creator_count", "count"), ("selling_creator_count", "count"),
           ("creator_growth_pct", "pct"), ("video_count", "count"), ("selling_video_count", "count"),
           ("video_growth_pct", "pct"), ("video_sales_share_pct", "num"), ("shop_count", "count"),
           ("similar_listings_count", "count"), ("category_product_count", "count"),
           ("gmv_prev_30d", "num"), ("category_product_count_level", "str"), ("leaf_category_id", "id")]
SERIES = [("daily_gmv", "num"), ("daily_units", "count"), ("daily_creator_count", "count"),
          ("daily_video_count", "count")]


def _series(v, kind, warnings, name):
    if v is None:
        return None
    if not isinstance(v, list):
        warnings.append(f"{name}: not a list")
        return None
    out = []
    for i, x in enumerate(v):
        val, prob = disc.parse_value(x, kind)
        if prob:
            warnings.append(f"{name}[{i}]: {prob}")
        out.append(val)
    return out


def normalize_deep(obj, context):
    """Facts only (source values). context: discovery product. Never estimates."""
    w, f = [], {}
    for name, kind in SCALARS:
        val, prob = disc.parse_value(obj.get(name), kind)
        f[name] = val
        if prob:
            w.append(f"{name}: {prob}")
    for name, kind in SERIES:
        f[name] = _series(obj.get(name), kind, w, name)
    ph = obj.get("price_history")
    f["price_history"] = None
    if isinstance(ph, list):
        f["price_history"] = []
        for i, e in enumerate(ph):
            price = disc.parse_value(e.get("price"), "num")[0] if isinstance(e, dict) else None
            date = disc.parse_value(e.get("date"), "date")[0] if isinstance(e, dict) else None
            if price is None:
                w.append(f"price_history[{i}]: invalid")
            f["price_history"].append({"date": date, "price": price})
    for lst, fields in (("top_creators", [("creator_id", "id"), ("name", "str"), ("revenue", "num"), ("growth_pct", "pct")]),
                        ("top_videos", [("video_id", "id"), ("creator", "str"), ("revenue", "num"), ("views", "count")])):
        raw = obj.get(lst)
        if not isinstance(raw, list):
            f[lst] = None
            continue
        items = []
        for i, it in enumerate(raw):
            if not isinstance(it, dict):
                w.append(f"{lst}[{i}]: not an object")
                continue
            item = {}
            for k, kind in fields:
                item[k], prob = disc.parse_value(it.get(k), kind)
                if prob:
                    w.append(f"{lst}[{i}].{k}: {prob}")
            items.append(item)
        f[lst] = items
    verify_growth(f, w)
    # fall back to the Discovery identifier only for identity (not metrics)
    if f["product_id"] is None and context["facts"].get("product_id"):
        f["product_id"] = context["facts"]["product_id"]
        w.append("product_id: taken from Discovery record")
    missing = [k for k, v in f.items() if v is None and k not in AUX_FIELDS]
    return f, missing, w


# ============================================================ Step U calibration: verification
AUX_FIELDS = {"gmv_prev_30d", "category_product_count_level", "growth_30d_provider", "growth_source",
              "growth_mismatch", "leaf_category_id"}
verify_growth = disc.verify_growth                     # shared with Discovery (one definition)


COMPETITION_SCOPES = ("PRODUCT_CLUSTER", "SUBCATEGORY", "CATEGORY", "UNKNOWN")


def competition_scope(f, cfg=None):
    """Taxonomy scope of the competition count (Step U post-live calibration).
    PRODUCT_CLUSTER: similar_listings_count (same/similar product)
    SUBCATEGORY    : category_product_count whose stated level is the LEAF of category_path
    CATEGORY       : count for a parent level (e.g. the whole Beauty category)
    UNKNOWN        : level not stated / not matching the path (never guessed)
    Only comparable scopes (config) are scored or used for EXTREME_SATURATION."""
    cs = (cfg or {}).get("competition_scope") or {"comparable_scopes": ["PRODUCT_CLUSTER", "SUBCATEGORY"]}
    segs = [x.strip().lower() for x in (f.get("category_path") or "").split(">") if x.strip()]
    out = {"competition_count": None, "competition_scope": "UNKNOWN", "source_field": None,
           "competition_category_id": f.get("category_id"), "competition_subcategory_id": None,
           "competition_comparable": False, "category_level_stated": f.get("category_product_count_level")}
    if f.get("similar_listings_count") is not None:
        out.update(competition_count=f["similar_listings_count"], competition_scope="PRODUCT_CLUSTER",
                   source_field="similar_listings_count")
    elif f.get("category_product_count") is not None:
        out.update(competition_count=f["category_product_count"], source_field="category_product_count")
        level = (f.get("category_product_count_level") or "").split(">")[-1].strip().lower()
        if level and segs and level == segs[-1] and len(segs) >= 2:
            out.update(competition_scope="SUBCATEGORY", competition_subcategory_id=f.get("leaf_category_id"))
        elif level and level in segs:
            out["competition_scope"] = "CATEGORY"
    out["competition_comparable"] = (out["competition_count"] is not None
                                     and out["competition_scope"] in cs["comparable_scopes"])
    return out


# ============================================================ Stage 5 — trend
def _window_mean(vals, cfg):
    ok = [v for v in vals if v is not None]
    return sum(ok) / len(ok) if len(ok) >= cfg["min_points_per_window"] else None


def velocity(series, cfg):
    """velocity_pct (last window vs previous) and prior_velocity_pct. None = N/A."""
    out = {"velocity_pct": None, "prior_velocity_pct": None,
           "points": sum(v is not None for v in series) if series else 0}
    if not series or out["points"] < cfg["min_history_days"]:
        return out
    w = cfg["window_days"]
    last, prev, prev2 = series[-w:], series[-2 * w:-w], series[-3 * w:-2 * w] if len(series) >= 3 * w else []
    m_last, m_prev = _window_mean(last, cfg), _window_mean(prev, cfg)
    if m_last is not None and m_prev is not None and m_prev > 0:
        out["velocity_pct"] = round((m_last - m_prev) / m_prev * 100, 2)
    m_prev2 = _window_mean(prev2, cfg) if prev2 else None
    if m_prev is not None and m_prev2 is not None and m_prev2 > 0:
        out["prior_velocity_pct"] = round((m_prev - m_prev2) / m_prev2 * 100, 2)
    return out


def classify_trend(vel, cfg):
    t = cfg["thresholds"]
    v, pv = vel["velocity_pct"], vel["prior_velocity_pct"]
    if v is None:
        return "INSUFFICIENT_DATA"
    if v <= t["declining_max"]:
        return "DECLINING"
    if v >= t["growing_min"] and pv is not None and v - pv >= t["acceleration_margin"]:
        return "ACCELERATING"
    if v >= t["growing_min"]:
        return "GROWING"
    return "STABLE"


def trend_metrics(f, cfg):
    tc = cfg["trend"]
    out = {name: velocity(f.get(src), tc) for name, src in
           (("gmv", "daily_gmv"), ("units", "daily_units"), ("creators", "daily_creator_count"),
            ("videos", "daily_video_count"))}
    out["label"] = classify_trend(out[tc["classify_on"]], tc)
    out["classified_on"] = tc["classify_on"]
    return out


# ============================================================ Stage 4/6/7/8
def concentration_metrics(f, filters_cfg):
    prod = {"product_id": f["product_id"], "gmv": f["gmv_30d"],
            "creators_count": f["creator_count"], "videos_count": f["video_count"],
            "creators": [{"name": c.get("name"), "revenue": c.get("revenue")} for c in f["top_creators"] or []],
            "videos": [{"id": v.get("video_id"), "revenue": v.get("revenue")} for v in f["top_videos"] or []]}
    if f["top_creators"] is None:
        prod["creators"] = None
    if f["top_videos"] is None:
        prod["videos"] = None
    return concentration.analyze(prod, filters_cfg["concentration"])


def scoring_input(f, history_snapshots, trend=None, comp=None):
    pmin, pmax = f["price_min"], f["price_max"]
    launch_days = None
    if f["launch_date"] and f["data_window_end"]:
        launch_days = (datetime.strptime(f["data_window_end"], "%Y-%m-%d")
                       - datetime.strptime(f["launch_date"], "%Y-%m-%d")).days
    creators = f["top_creators"]
    return {
        "product_id": f["product_id"], "product_name": f["product_name"],
        # wps-v2 growth momentum: recent trend + acceleration from the daily GMV series (never fabricated)
        "recent_velocity_pct": ((trend or {}).get("gmv") or {}).get("velocity_pct"),
        "acceleration_pp": (lambda g: round(g["velocity_pct"] - g["prior_velocity_pct"], 2)
                            if g.get("velocity_pct") is not None and g.get("prior_velocity_pct") is not None
                            else None)((trend or {}).get("gmv") or {}),
        # competition count ONLY when its taxonomy scope is comparable (else N/A -> lowers Confidence)
        "competition_count_comparable": (comp or {}).get("competition_count") if (comp or {}).get(
            "competition_comparable") else None,
        # WPS inputs (config/scoring.yaml metrics)
        "revenue_growth_pct": f["growth_30d_pct"], "units_sold": f["units_30d"],
        "videos_count": f["video_count"], "video_sales_share_pct": f["video_sales_share_pct"],
        "creators_count": f["creator_count"],
        "top_creators_growth_pct": [c.get("growth_pct") for c in creators] if creators is not None else None,
        "category_growth_pct": f["category_growth_pct"],
        # Confidence evidence: a competition count only counts when its scope is comparable
        "category_product_count": (comp or {}).get("competition_count") if (comp or {}).get("competition_comparable")
        else None,
        "price_avg": (pmin + pmax) / 2 if None not in (pmin, pmax) else None,
        "commission_pct": f["commission_pct"], "daily_sales_series": f["daily_gmv"],
        # Confidence inputs (config/scoring.yaml confidence)
        "gmv": f["gmv_30d"], "price_min": pmin, "price_max": pmax,
        "creators": [{"revenue": c.get("revenue")} for c in creators] if creators is not None else None,
        "videos": [{"revenue": v.get("revenue")} for v in f["top_videos"]] if f["top_videos"] is not None else None,
        "shops_count": f["shop_count"], "similar_listings_count": f["similar_listings_count"],
        "launch_date_days": launch_days, "history_snapshots": history_snapshots,
    }


def red_flags(f, trend, conc, conf_score, filters_cfg, cfg, comp=None):
    rf, flags = cfg["red_flags"], []

    def add(code, **detail):
        flags.append({"flag": code, **detail})

    for code in ("CREATOR_DEPENDENCY", "VIDEO_DEPENDENCY"):
        st = conc["flags"][code]
        if st["status"] == "FLAGGED":
            add(code, value=st["value"], condition=st["condition"])
    sd = rf["SALES_DECLINING"]
    if trend["label"] == sd["trend_label"] or (f["growth_30d_pct"] is not None and f["growth_30d_pct"] <= sd["growth_30d_max"]):
        add("SALES_DECLINING", trend=trend["label"], growth_30d_pct=f["growth_30d_pct"])
    if f.get("growth_source") == "provider_unverified":
        add("GROWTH_UNVERIFIED", note="provider growth without previous-period revenue; not recalculated")
    if f.get("growth_mismatch"):
        add("GROWTH_MISMATCH", **f["growth_mismatch"], note="calculated growth used")
    # Step U: saturation only from a COMPARABLE competition scope (a whole-category count never flags a
    # subcategory product); PRODUCT_CLUSTER -> similar_listings rule, SUBCATEGORY -> category count rule
    comp = comp or competition_scope(f, cfg)
    inputs = {"similar_listings_count": comp["competition_count"] if comp["competition_comparable"]
              and comp["competition_scope"] == "PRODUCT_CLUSTER" else None,
              "category_product_count": comp["competition_count"] if comp["competition_comparable"]
              and comp["competition_scope"] == "SUBCATEGORY" else None}
    if comp["competition_count"] is not None and not comp["competition_comparable"]:
        add("COMPETITION_NOT_COMPARABLE", scope=comp["competition_scope"], count=comp["competition_count"],
            note="count not used for WPS or saturation; Confidence reduced instead")
    for chk in filters_cfg["risk_rules"]["extreme_seller_saturation"]["checks"]:
        v = inputs.get(chk["input"])
        if v is not None and disc._cmp(v, chk["condition"]):
            add("EXTREME_SATURATION", input=chk["input"], value=v, condition=chk["condition"])
    if trend["label"] == rf["INSUFFICIENT_HISTORY"]["trend_label"]:
        add("INSUFFICIENT_HISTORY", history_points=trend["gmv"]["points"])
    pi = rf["PRICE_INSTABILITY"]
    prices = [e["price"] for e in (f["price_history"] or []) if e.get("price") is not None]
    if len(prices) >= pi["min_points"] and min(prices) > 0:
        rng = (max(prices) - min(prices)) / min(prices) * 100
        if rng >= pi["price_range_pct_min"]:
            add("PRICE_INSTABILITY", price_range_pct=round(rng, 2))
    if conf_score < rf["LOW_DATA_CONFIDENCE"]["confidence_below"]:
        add("LOW_DATA_CONFIDENCE", confidence=conf_score)
    text = {"product_name": f["product_name"] or "", "category": f["category_path"] or ""}
    for code, rule in (("IP_REVIEW_REQUIRED", "trademark_ip_risk"), ("REGULATED_REVIEW_REQUIRED", "highly_regulated"),
                       ("SHIPPING_REVIEW_REQUIRED", "shipping_problems")):
        r = filters_cfg["risk_rules"][rule]
        fields = r.get("match_field", "product_name")
        fields = [fields] if isinstance(fields, str) else fields
        hits = sorted({kw for kw in r["keywords"] for fld in fields if text.get(fld) and disc._word_match(text[fld], kw)})
        if hits:
            add(code, keywords=hits, note="keyword match only; not a legal or compliance conclusion")
    # dedupe EXTREME_SATURATION if both inputs triggered
    out, seen = [], set()
    for fl in flags:
        if fl["flag"] in seen:
            continue
        seen.add(fl["flag"])
        out.append(fl)
    return out


def history_count(product_id, out_dir=OUT_DIR):
    """Prior deep-analysis observations of this product (evidence for Confidence)."""
    n = 0
    for path in sorted(Path(out_dir).glob("deep_*.json")):
        try:
            res = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        # Step U fix: only SUCCESSFUL prior analyses are evidence (a failed/empty answer is not a snapshot)
        n += any(r.get("product_id") == product_id and r.get("status") == "ok" for r in res.get("results", []))
    return n


def analyze(envelope, raw_path, context, cfgs, history_snapshots=0, cache_hit=False):
    """Normalized deep-analysis record (Stage 9 schema) from one raw response."""
    cfg, filters_cfg, scoring_cfg = cfgs["deep"], cfgs["filters"], cfgs["scoring"]
    data = (envelope.get("response") or {}).get("data") or {}
    base = {"observation_timestamp": envelope.get("observation_timestamp"), "market": envelope.get("market"),
            "source": {"raw_file": str(raw_path), "task_id": envelope.get("task_id") or data.get("task_id"),
                       "report_url": data.get("report_url"), "query_type": envelope.get("query_type"),
                       "cache_hit": cache_hit, "discovery_key": context["key"],
                       "discovery_status": context["calculated"]["filter_status"]}}
    obj, errors = extract_object(envelope)
    if obj is None:
        return {"product_id": envelope.get("product_id"), "status": "malformed", "errors": errors, **base}

    f, missing, warnings = normalize_deep(obj, context)
    trend = trend_metrics(f, cfg)
    conc = concentration_metrics(f, filters_cfg)
    comp = competition_scope(f, cfg)
    sin = scoring_input(f, history_snapshots, trend, comp)
    scored = score_product(sin, scoring_cfg["confidence"], scoring_cfg)
    wps, conf = scored["wps"], scored["confidence"]
    pmin, pmax = f["price_min"], f["price_max"]
    return {
        "status": "ok",
        "product_id": f["product_id"], "product_name": f["product_name"],
        "product_url": f["product_url"], "category": f["category_path"], "category_id": f["category_id"],
        "shop": {"shop_id": f["shop_id"], "shop_name": f["shop_name"]},
        "price": {"min": pmin, "max": pmax, "avg": (pmin + pmax) / 2 if None not in (pmin, pmax) else None,
                  "history": f["price_history"]},
        "gmv": f["gmv_30d"], "units": f["units_30d"],
        "growth": {"growth_30d_pct": f["growth_30d_pct"], "category_growth_pct": f["category_growth_pct"],
                   "gmv_prev_30d": f.get("gmv_prev_30d"), "growth_30d_provider": f.get("growth_30d_provider"),
                   "growth_source": f.get("growth_source")},
        "launch_date": f["launch_date"], "data_window_end": f["data_window_end"], "commission_pct": f["commission_pct"],
        "creator_metrics": {"total": f["creator_count"], "selling": f["selling_creator_count"],
                            "growth_pct": f["creator_growth_pct"], "top_creators": f["top_creators"],
                            "daily_count": f["daily_creator_count"]},
        "video_metrics": {"total": f["video_count"], "selling": f["selling_video_count"],
                          "growth_pct": f["video_growth_pct"], "sales_share_pct": f["video_sales_share_pct"],
                          "top_videos": f["top_videos"], "daily_count": f["daily_video_count"]},
        "competition_metrics": {"shop_count": f["shop_count"], "similar_listings_count": f["similar_listings_count"],
                                "category_product_count": f["category_product_count"],
                                "category_product_count_level": f.get("category_product_count_level"),
                                **{k: v for k, v in comp.items() if k != "source_field"},
                                "competition_source_field": comp["source_field"]},
        "sales_history": {"daily_gmv": f["daily_gmv"], "daily_units": f["daily_units"]},
        "trend_metrics": trend,
        "concentration_metrics": conc,
        "wps": wps["score"],
        "wps_complete": wps["complete"],
        "wps_breakdown": {k: {"points": m["points"], "max": m["max"], "tier": m.get("tier")}
                          for k, m in wps["metrics"].items()},
        "wps_groups": {k: {"points": g["points"], "max": g["max"], "na": g["na"]} for k, g in wps["groups"].items()},
        "wps_points": {"earned": wps["points_earned"], "possible": wps["points_possible"]},
        "wps_missing_by_tier": wps["missing_by_tier"],
        "wps_inputs": {k: v for k, v in sin.items() if not isinstance(v, list)},
        "confidence_adjustments": conf.get("adjustments", []),
        "confidence_before_adjustments": conf.get("score_before_adjustments", conf["score"]),
        "provenance": provenance.deep_provenance(f, f.get("data_window_end"), envelope.get("observation_timestamp"),
                                                 comp),
        "wps_input_provenance": {k: v for k, v in provenance.WPS_INPUT_PROVENANCE.items() if k in sin},
        "wps_na_metrics": wps["na_metrics"],
        "confidence": conf["score"], "confidence_level": conf["level"],
        "confidence_breakdown": {k: {"earned": v["earned"], "max": v["max"], "status": v["status"]}
                                 for k, v in conf["breakdown"].items()},
        "verdict": scored["verdict"],
        "red_flags": red_flags(f, trend, conc, conf["score"], filters_cfg, cfg, comp),
        "missing_data": missing,
        "parse_warnings": warnings,
        "labels": {"FACT": ["product_*", "category*", "shop", "price.min/max/history", "gmv", "units", "growth",
                            "creator_metrics", "video_metrics", "competition_metrics", "sales_history"],
                   "CALCULATION": ["price.avg", "trend_metrics", "concentration_metrics", "wps*",
                                   "confidence*", "verdict", "red_flags"],
                   "MISSING DATA": "missing_data"},
        "original_record": copy.deepcopy(obj),
        "config_versions": {"deep": cfg.get("version"), "filters": filters_cfg.get("version"),
                            "wps": scoring_cfg.get("version"), "confidence": scoring_cfg["confidence"].get("version")},
        **base,
    }


# ============================================================ runner + credit protection
class InsufficientCredits(Exception):
    pass


def run(live=False, client=None, discovery_path=None, max_products=None, now=None,
        raw_dir=RAW_DIR, out_dir=OUT_DIR, processed_dir=ROOT / "data" / "processed", cfgs=None):
    """Dry-run by default. Live mode needs a client with credits(), submit(query), wait(task_id)."""
    cfgs = cfgs or {"deep": load_cfg(), "filters": disc.load_yaml("filters.yaml"), "scoring": load_scoring_config()}
    cfg = copy.deepcopy(cfgs["deep"])
    if max_products is not None:
        cfg["selection"]["deep_analysis_max_products"] = max_products
    cfgs = {**cfgs, "deep": cfg}
    now = now or datetime.now(timezone.utc)
    discovery_path = discovery_path or latest_discovery(processed_dir)
    if not discovery_path:
        raise SystemExit("No Discovery output found in data/processed/.")
    dres = json.loads(Path(discovery_path).read_text())
    market = dres.get("market") or {"region": "US", "currency": "USD"}
    selected, skipped = select_candidates(dres, cfg)
    pl = plan(selected, cfg, now, raw_dir, market)
    report = {"mode": "live" if live else "dry_run", "discovery_file": str(discovery_path),
              "selected": len(selected), "skipped": skipped, "plan": pl}
    cp = cfg["credit_protection"]

    if client is not None:
        bal = client.credits()["totalRemain"]
        report["balance_before"] = bal
        need = cp["estimated_credits_per_query"] + cp["min_balance_reserve"]
        report["affordable_paid_queries"] = max(0, int((bal - cp["min_balance_reserve"]) // cp["estimated_credits_per_query"]))
        report["sufficient_for_full_run"] = bal - pl["estimated_credits"] >= cp["min_balance_reserve"]
    if not live:
        return report                                            # DRY-RUN: nothing spent

    if client is None:
        raise ValueError("live mode requires a client")
    by_id = {product_ref_id(p): p for p in selected}
    results, paid_done, stopped = [], set(), None
    for item in pl["items"]:
        pid, qt = item["product_id"], item["query_type"]
        if item["action"] == "skip_duplicate" or (pid, qt) in paid_done:
            continue                                             # no duplicate paid query
        if item["action"] == "use_cache":
            raw_path, cache_hit = Path(item["cached_raw"]), True
        else:
            bal = client.credits()["totalRemain"]
            if bal < cp["estimated_credits_per_query"] + cp["min_balance_reserve"]:
                stopped = {"reason": "insufficient_credits", "balance": bal, "at_product": pid}
                break                                            # stop safely
            sub = client.submit(item["query"])
            task_id = (sub.get("data") or {}).get("task_id")
            response = client.wait(task_id) if task_id else sub
            obs = datetime.now(timezone.utc).isoformat()
            raw_path = save_raw_deep(response, {"observation_timestamp": obs, "market": market.get("region", "US"),
                                                "product_id": pid, "query_type": qt, "query": item["query"],
                                                "task_id": task_id}, raw_dir)
            paid_done.add((pid, qt))
            cache_hit = False
        env = json.loads(raw_path.read_text())
        results.append(analyze(env, raw_path, by_id[pid], cfgs, history_count(pid, out_dir), cache_hit))

    report.update({"results": results, "stopped": stopped, "paid_queries_done": len(paid_done)})
    if client is not None:
        report["balance_after"] = client.credits()["totalRemain"]
    report["saved"] = str(save_results(report, out_dir))
    return report


def save_results(report, out_dir=OUT_DIR):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path, n = out_dir / f"deep_{ts}.json", 1
    while path.exists():
        n += 1
        path = out_dir / f"deep_{ts}_{n}.json"
    with open(path, "x") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def main(argv):
    live = "--live" in argv
    max_p = int(argv[argv.index("--max") + 1]) if "--max" in argv else None
    import kalopilot_client as kc

    class Client:
        credits = staticmethod(kc.credits)
        submit = staticmethod(lambda q: kc.submit(q))
        wait = staticmethod(lambda t: kc.wait(t))

    r = run(live=live, client=Client(), max_products=max_p)
    pl = r["plan"]
    print(f"MODE: {r['mode'].upper()}  (discovery: {Path(r['discovery_file']).name})")
    print(f"Selected: {r['selected']} | paid queries: {pl['paid_queries']} | cache hits: {pl['cache_hits']} "
          f"| estimated credits: {pl['estimated_credits']}")
    print(f"Balance: {r.get('balance_before')} | affordable paid queries (keeping reserve): "
          f"{r.get('affordable_paid_queries')} | enough for full run: {r.get('sufficient_for_full_run')}")
    for i, it in enumerate(pl["items"], 1):
        print(f"  {i:>2}. [{it.get('filter_status')}] {it['action']:<11} {it['product_id']}  {(it.get('product_name') or '')[:55]}")
    for s in r["skipped"]:
        print(f"  skipped: {s['key']} ({s['reason']})")
    if live:
        print(f"Done: {r['paid_queries_done']} paid, stopped={r['stopped']}, saved={r['saved']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
