"""Historical Tracking (Step Q): append-only product observations across runs and dates.

Rules (config/history.yaml):
  - one immutable, read-only file per observation:
      data/history/products/{safe_id}/{YYYY-MM-DD}T{HHMMSSffffff}Z.json
  - never overwritten or deleted; same product + same exact timestamp -> duplicate, skipped
  - several observations on the same day are all kept
  - data/history/history_index.json is the only file rebuilt each run
Everything below is deterministic; missing values stay None (shown as N/A).

Utilities (reused by Step R):
  get_product_history, get_latest_observation, get_previous_observation,
  get_observations_between, calculate_deltas, calculate_velocity, calculate_trends,
  calculate_volatility, tracking_age, momentum_snapshot

CLI:
  python3 scripts/history.py migrate            # DRY-RUN (default): shows what would be imported
  python3 scripts/history.py migrate --apply    # append processed observations to history
  python3 scripts/history.py show <product_id>  # snapshot for one product
"""
import hashlib
import json
import math
import os
import re
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
NA = "N/A"
NEW_GROWTH = "NEW_GROWTH"
SCHEMA_VERSION = "history-v1.2"   # v1.2 (Step U): provider_observation_timestamp, retrieved_at, imported_at, source_snapshot_hash
_PREV_SCHEMA = "history-v1.1"   # v1.1 (Step R): + selling_video_count, competition, concentration (additive)
SECRET_PATTERNS = ("token", "api_key", "apikey", "authorization", "secret", "password", "bearer")

# metric -> (section, field) inside an observation
METRICS = {
    "wps": ("scores", "wps"), "confidence": ("scores", "confidence"),
    "gmv": ("tiktok", "gmv"), "units": ("tiktok", "units"), "creators": ("tiktok", "creator_count"),
    "videos": ("tiktok", "video_count"), "price": ("tiktok", "price"),
    "avs": ("amazon", "avs"), "bvs": ("business", "bvs"),
    # added in history-v1.1 for Step R (absent in older observations -> None / N/A)
    "selling_creators": ("tiktok", "selling_creator_count"), "selling_videos": ("tiktok", "selling_video_count"),
    "shop_count": ("tiktok", "shop_count"), "similar_listings": ("competition", "similar_listings_count"),
    "category_products": ("competition", "category_product_count"),
    "top_creator_share": ("concentration", "top_creator_revenue_share"),
    "top_video_share": ("concentration", "top_video_revenue_share"),
}
DELTA_METRICS = ["wps", "gmv", "units", "creators", "videos", "price", "avs", "bvs"]
PCT_METRICS = ["gmv", "units", "creators", "videos", "price"]
VELOCITY_METRICS = ["gmv", "units", "creators", "videos", "wps"]


def load_cfg(path=ROOT / "config" / "history.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)["history"]


# ================================================================== helpers
def num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def parse_ts(v):
    """ISO 8601, '20260930T014532Z' or 'YYYY-MM-DD' -> aware UTC datetime (None if invalid)."""
    if not isinstance(v, str) or not v:
        return None
    s = v.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S%fZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def safe_id(identity):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(identity))


def scrub(obj):
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if not any(p in str(k).lower() for p in SECRET_PATTERNS)}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def metric(obs, name):
    sec, field = METRICS[name]
    return num((obs.get(sec) or {}).get(field))


def _flags(v):
    return [f["flag"] for f in v or [] if isinstance(f, dict) and f.get("flag")]


# ================================================================== snapshot identity (Step U)
def snapshot_hash(obs):
    """Deterministic identity of ONE provider snapshot of a product.
    = stage + product + stable provider time + normalized source payload (tiktok/competition/concentration).
    Stable provider time: provider_observation_timestamp (the provider's data_window_end); if the provider
    gave none, the provider task (task_id + record) ; only if neither exists, the observation time itself
    (so rows without any provider identity are never merged). Two records on the same day with different
    values always hash differently."""
    src = obs.get("source") or {}
    anchor = (obs.get("provider_observation_timestamp")
              or (f"task:{src.get('task_id')}#{src.get('record_index')}" if src.get("task_id") else None)
              or f"obs:{obs.get('observation_timestamp')}")
    payload = {"stage": obs.get("source_stage"), "identity": obs.get("identity_key"), "anchor": anchor,
               "tiktok": obs.get("tiktok"), "competition": obs.get("competition"),
               "concentration": obs.get("concentration")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _timestamps(o, provider_date, retrieved):
    o["provider_observation_timestamp"] = provider_date          # provider's data as-of (data_window_end)
    o["retrieved_at"] = retrieved                                 # when we fetched the provider answer
    o["source_snapshot_hash"] = snapshot_hash(o)
    return o


# ================================================================== observation builders
def empty_observation():
    return {"schema_version": SCHEMA_VERSION,
            "tiktok": {k: None for k in ("price", "gmv", "units", "growth", "creator_count", "selling_creator_count",
                                         "video_count", "selling_video_count", "shop_count")},
            "competition": {"similar_listings_count": None, "category_product_count": None},
            "concentration": {"top_creator_revenue_share": None, "top_video_revenue_share": None},
            "scores": {"wps": None, "confidence": None},
            "amazon": {k: None for k in ("avs", "amazon_confidence", "amazon_match_confidence", "amazon_price",
                                         "amazon_competition", "observation_timestamp")},
            "business": {k: None for k in ("bvs", "bvs_confidence", "gross_margin_percent",
                                           "contribution_margin_percent", "observation_timestamp")},
            "flags": {"red_flags": [], "commercial_red_flags": [], "amazon_red_flags": []},
            "missing_data": [], "source": {}}


def from_discovery(rec, processed_file, market="US"):
    """One Discovery record -> observation (no WPS at this stage)."""
    f, c, s = rec.get("facts") or {}, rec.get("calculated") or {}, rec.get("source") or {}
    ts = parse_ts(s.get("fetched_at")) or parse_ts(rec.get("observation_date"))
    if ts is None:
        return None
    o = empty_observation()
    price = num(c.get("price"))
    if price is None and None not in (num(f.get("price_min")), num(f.get("price_max"))):
        price = (f["price_min"] + f["price_max"]) / 2
    o.update({"product_id": f.get("product_id"), "identity_key": f.get("product_id") or rec.get("key"),
              "key_type": rec.get("key_type"), "product_name": f.get("product_name"), "category": f.get("category"),
              "shop": f.get("shop_name"), "market": rec.get("market", market),
              "observation_timestamp": iso(ts), "observation_date": ts.date().isoformat(),
              "source_stage": "discovery", "data_environment": rec.get("data_environment")})
    o["tiktok"].update({"price": price, "gmv": f.get("gmv_30d"), "units": f.get("units_30d"),
                        "growth": f.get("growth_30d"), "creator_count": f.get("creator_count"),
                        "selling_creator_count": f.get("selling_creator_count"), "video_count": f.get("video_count"),
                        "shop_count": f.get("shop_count")})
    o["flags"]["red_flags"] = sorted({r.get("rule") for r in c.get("filter_reasons") or [] if r.get("rule")})
    o["missing_data"] = list(rec.get("missing_fields") or [])
    o["discovery_status"] = c.get("filter_status")
    o["source"] = {"stage": "discovery", "processed_file": str(processed_file), "raw_file": s.get("raw_file"),
                   "task_id": s.get("task_id"), "report_url": s.get("report_url"), "record_index": s.get("record_index")}
    return _timestamps(o, f.get("data_window_end"), iso(ts))


def from_deep(d, processed_file, amazon=None, bvs=None):
    """One Deep Analysis result (+ Amazon/BVS of the same cycle) -> observation."""
    ts = parse_ts(d.get("observation_timestamp"))
    if ts is None or not d.get("product_id"):
        return None
    o = empty_observation()
    cm, vm = d.get("creator_metrics") or {}, d.get("video_metrics") or {}
    o.update({"product_id": str(d["product_id"]), "identity_key": str(d["product_id"]), "key_type": "product_id",
              "product_name": d.get("product_name"), "category": d.get("category"),
              "shop": (d.get("shop") or {}).get("shop_name"), "market": d.get("market") or "US",
              "observation_timestamp": iso(ts), "observation_date": ts.date().isoformat(),
              "source_stage": "deep_analysis", "data_environment": d.get("data_environment")})
    o["tiktok"].update({"price": (d.get("price") or {}).get("avg"), "gmv": d.get("gmv"), "units": d.get("units"),
                        "growth": (d.get("growth") or {}).get("growth_30d_pct"), "creator_count": cm.get("total"),
                        "selling_creator_count": cm.get("selling"), "video_count": vm.get("total"),
                        "selling_video_count": vm.get("selling"),
                        "shop_count": (d.get("competition_metrics") or {}).get("shop_count")})
    comp = d.get("competition_metrics") or {}
    o["competition"] = {"similar_listings_count": comp.get("similar_listings_count"),
                        "category_product_count": comp.get("category_product_count")}
    conc = ((d.get("concentration_metrics") or {}).get("metrics") or {})
    o["concentration"] = {"top_creator_revenue_share": num((conc.get("top_creator_revenue_share") or {}).get("value")),
                          "top_video_revenue_share": num((conc.get("top_video_revenue_share") or {}).get("value"))}
    o["scores"] = {"wps": num(d.get("wps")), "confidence": num(d.get("confidence"))}
    o["flags"]["red_flags"] = _flags(d.get("red_flags"))
    o["missing_data"] = list(d.get("missing_data") or [])
    src = d.get("source") or {}
    o["source"] = {"stage": "deep_analysis", "processed_file": str(processed_file), "raw_file": src.get("raw_file"),
                   "task_id": src.get("task_id"), "report_url": src.get("report_url")}
    if amazon:
        o["amazon"] = {"avs": num(amazon.get("avs")), "amazon_confidence": num(amazon.get("amazon_confidence")),
                       "amazon_match_confidence": num(amazon.get("amazon_match_confidence")),
                       "amazon_price": num(amazon.get("amazon_price")),
                       "amazon_competition": amazon.get("amazon_competition"),
                       "observation_timestamp": amazon.get("observation_timestamp")}
        o["flags"]["amazon_red_flags"] = _flags(amazon.get("amazon_red_flags"))
        o["source"]["amazon_raw_file"] = (amazon.get("source") or {}).get("raw_file")
        o["source"]["amazon_processed_file"] = amazon.get("_file")
        o["missing_data"] += [f"amazon:{m}" for m in amazon.get("missing_data") or []]
    if bvs:
        e = bvs.get("economics") or {}
        o["business"] = {"bvs": num(bvs.get("bvs")), "bvs_confidence": num(bvs.get("bvs_confidence")),
                         "gross_margin_percent": num(e.get("gross_margin_percent")),
                         "contribution_margin_percent": num(e.get("contribution_margin_percent")),
                         "observation_timestamp": bvs.get("observation_timestamp")}
        o["flags"]["commercial_red_flags"] = _flags(bvs.get("commercial_red_flags"))
        o["source"]["bvs_processed_file"] = bvs.get("_file")
        o["source"]["commercial_data_file"] = (bvs.get("source") or {}).get("commercial_data_file")
        o["missing_data"] += [f"economics:{m}" for m in bvs.get("missing_data") or []]
    return _timestamps(o, d.get("data_window_end"), iso(ts))


# ================================================================== store
class HistoryStore:
    def __init__(self, base_dir=None, cfg=None):
        self.cfg = cfg or load_cfg()
        self.base = Path(base_dir or ROOT / self.cfg["base_dir"])
        self.products = self.base / "products"
        self.index_path = self.base / "history_index.json"

    def path_for(self, obs):
        ts = parse_ts(obs["observation_timestamp"])
        return self.products / safe_id(obs["identity_key"]) / f"{ts.strftime('%Y-%m-%dT%H%M%S%f')}Z.json"

    def append(self, obs, dry_run=False):
        """Append one observation. Returns ('written'|'duplicate'|'would_write', path). Never overwrites."""
        if not obs or not obs.get("identity_key") or not parse_ts(obs.get("observation_timestamp")):
            raise ValueError("observation needs identity_key and a valid observation_timestamp")
        path = self.path_for(obs)
        if path.exists():
            return "duplicate", path
        h = obs.get("source_snapshot_hash") or snapshot_hash(obs)
        for prev in self.observations(obs["identity_key"]):
            if (prev.get("source_snapshot_hash") or snapshot_hash(prev)) == h:
                return "duplicate_snapshot", Path(prev["_path"])  # DUPLICATE_SNAPSHOT_SKIPPED: first one kept
        if dry_run:
            return "would_write", path
        path.parent.mkdir(parents=True, exist_ok=True)
        now_iso = iso(datetime.now(timezone.utc))
        body = scrub({**obs, "source_snapshot_hash": h, "written_at": now_iso, "imported_at": now_iso})
        with open(path, "x") as fh:                        # 'x' -> fails instead of overwriting
            json.dump(body, fh, ensure_ascii=False, indent=2)
        os.chmod(path, 0o444)                              # immutable once written
        return "written", path

    def identities(self):
        return sorted(p.name for p in self.products.iterdir() if p.is_dir()) if self.products.exists() else []

    def observations(self, identity):
        d = self.products / safe_id(identity)
        if not d.exists():
            return []
        obs = []
        for f in sorted(d.glob("*.json")):
            try:
                o = json.loads(f.read_text())
            except json.JSONDecodeError:
                continue
            o["_path"] = str(f)
            obs.append(o)
        return sorted(obs, key=lambda o: parse_ts(o["observation_timestamp"]))

    def rebuild_index(self, now=None):
        """history_index.json is derived from observation files; safe to rebuild each run."""
        products = {}
        for ident in self.identities():
            obs = self.observations(ident)
            if not obs:
                continue
            age = tracking_age(obs)
            wps = [metric(o, "wps") for o in obs if metric(o, "wps") is not None]
            last = obs[-1]
            products[ident] = {
                "product_id": last.get("product_id"), "product_name": last.get("product_name"),
                **age,
                "latest_wps": wps[-1] if wps else None, "previous_wps": wps[-2] if len(wps) >= 2 else None,
                "latest_gmv": latest_value(obs, "gmv"), "latest_units": latest_value(obs, "units"),
                "latest_creators": latest_value(obs, "creators"), "latest_videos": latest_value(obs, "videos"),
            }
        index = {"version": SCHEMA_VERSION, "generated_at": iso(now or datetime.now(timezone.utc)),
                 "product_count": len(products), "products": products}
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2))
        os.replace(tmp, self.index_path)
        return index


_DEFAULT = None


def default_store():
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = HistoryStore()
    return _DEFAULT


# ================================================================== retrieval utilities
def get_product_history(product_id, store=None):
    return (store or default_store()).observations(product_id)


def get_latest_observation(product_id, store=None):
    obs = get_product_history(product_id, store)
    return obs[-1] if obs else None


def get_previous_observation(product_id, store=None):
    obs = get_product_history(product_id, store)
    return obs[-2] if len(obs) >= 2 else None


def get_observations_between(product_id, start_date, end_date, store=None):
    """Observations whose UTC date is within [start_date, end_date] (inclusive)."""
    s = start_date if isinstance(start_date, date) else date.fromisoformat(start_date)
    e = end_date if isinstance(end_date, date) else date.fromisoformat(end_date)
    return [o for o in get_product_history(product_id, store)
            if s <= parse_ts(o["observation_timestamp"]).date() <= e]


def latest_value(obs, name):
    vals = [metric(o, name) for o in obs if metric(o, name) is not None]
    return vals[-1] if vals else None


def _last_two(obs, name):
    pts = [(parse_ts(o["observation_timestamp"]), metric(o, name)) for o in obs if metric(o, name) is not None]
    return pts[-2:] if len(pts) >= 2 else None


# ================================================================== analysis view (Step S fix)
def collapse_same_cycle(obs, hours):
    """Analysis-only view: observations of the same product closer than `hours` belong to ONE
    collection cycle (e.g. Discovery + Deep Analysis minutes apart). Keep the one with the most
    non-null metrics; tie -> the later one. Stored history is NOT modified (append-only)."""
    if not hours:
        return list(obs)
    def filled(o):
        return sum(metric(o, m) is not None for m in METRICS)
    out = []
    for o in sorted(obs, key=lambda o: parse_ts(o["observation_timestamp"])):
        t = parse_ts(o["observation_timestamp"])
        if out and (t - parse_ts(out[-1]["observation_timestamp"])).total_seconds() < hours * 3600:
            if filled(o) >= filled(out[-1]):
                out[-1] = o
            continue
        out.append(o)
    return out


# ================================================================== generic series helpers (Step R)
def series(obs, name):
    """[(timestamp, value)] of valid values, oldest first. A real 0 is kept."""
    return [(parse_ts(o["observation_timestamp"]), metric(o, name)) for o in obs if metric(o, name) is not None]


def velocity_for(obs, name):
    """Per-day velocity between the last two valid points using REAL elapsed time (None if unavailable)."""
    pts = series(obs, name)
    if len(pts) < 2:
        return None
    (t0, v0), (t1, v1) = pts[-2:]
    days = (t1 - t0).total_seconds() / 86400
    return None if days <= 0 else (v1 - v0) / days


def acceleration_for(obs, name):
    """Needs 3 valid points: prev_vel=(v2-v1)/dt1, cur_vel=(v3-v2)/dt2, acceleration=cur_vel-prev_vel."""
    pts = series(obs, name)
    if len(pts) < 3:
        return None
    (t1, v1), (t2, v2), (t3, v3) = pts[-3:]
    dt1, dt2 = (t2 - t1).total_seconds() / 86400, (t3 - t2).total_seconds() / 86400
    if dt1 <= 0 or dt2 <= 0:
        return None
    prev_v, cur_v = (v2 - v1) / dt1, (v3 - v2) / dt2
    return {"previous_velocity": prev_v, "current_velocity": cur_v, "acceleration": cur_v - prev_v,
            "values": [v1, v2, v3], "elapsed_days": [dt1, dt2]}


# ================================================================== deltas / velocity
def pct_change(prev, cur):
    """Never divides by zero: prev 0 -> cur > 0 NEW_GROWTH, cur 0 -> 0.0."""
    if prev is None or cur is None:
        return NA
    if prev == 0:
        return NEW_GROWTH if cur > 0 else (0.0 if cur == 0 else NA)
    return round((cur - prev) / abs(prev) * 100, 2)


def deltas_from(obs):
    """Per metric, compares the last two observations where that metric has a value."""
    out = {}
    for m in DELTA_METRICS:
        two = _last_two(obs, m)
        if not two:
            out[m] = {"previous": NA, "current": latest_value(obs, m) if latest_value(obs, m) is not None else NA,
                      "delta": NA, "percent_change": NA if m in PCT_METRICS else None}
            continue
        (t0, v0), (t1, v1) = two
        out[m] = {"previous": v0, "current": v1, "delta": round(v1 - v0, 4),
                  "previous_timestamp": iso(t0), "current_timestamp": iso(t1)}
        if m in PCT_METRICS:
            out[m]["percent_change"] = pct_change(v0, v1)
    return out


def velocity_from(obs):
    """(current - previous) / elapsed days between their REAL timestamps; weekly = per_day * 7."""
    out = {}
    for m in VELOCITY_METRICS:
        two = _last_two(obs, m)
        if not two:
            out[f"{m}_velocity_per_day"] = out[f"{m}_velocity_per_week"] = NA
            continue
        (t0, v0), (t1, v1) = two
        days = (t1 - t0).total_seconds() / 86400
        if days <= 0:
            out[f"{m}_velocity_per_day"] = out[f"{m}_velocity_per_week"] = NA
            continue
        per_day = (v1 - v0) / days
        out[f"{m}_velocity_per_day"] = round(per_day, 4)
        out[f"{m}_velocity_per_week"] = round(per_day * 7, 4)
        out[f"{m}_elapsed_days"] = round(days, 4)
    return out


# ================================================================== volatility
def cv(values):
    if len(values) < 1:
        return None
    mean = statistics.mean(values)
    return None if mean <= 0 else statistics.pstdev(values) / mean


def volatility_from(obs, cfg):
    v = cfg["volatility"]
    out = {}
    for m in v["metrics"]:
        vals = [metric(o, m) for o in obs if metric(o, m) is not None]
        c = cv(vals) if len(vals) >= v["min_observations"] else None
        if c is None:
            out[m] = {"cv": NA, "level": "INSUFFICIENT_DATA", "observations": len(vals)}
        else:
            lvl = "LOW" if c < v["levels"]["LOW_max"] else ("MODERATE" if c < v["levels"]["MODERATE_max"] else "HIGH")
            out[m] = {"cv": round(c, 4), "level": lvl, "observations": len(vals)}
    out["volatility_level"] = out[v["primary_metric"]]["level"]
    return out


# ================================================================== trends
def classify_window(points, window_days, kind, cfg):
    """points: [(ts, value)] inside the window, oldest first. Returns detail + label."""
    t = cfg["trend"]
    th = t["thresholds"]["points_metrics" if kind == "points" else "percent_metrics"]
    n = len(points)
    detail = {"observations": n, "window_days": window_days}
    if n < 1:
        return {**detail, "label": "INSUFFICIENT_DATA"}
    span = (points[-1][0] - points[0][0]).total_seconds() / 86400
    detail["span_days"] = round(span, 3)
    if n < t["min_observations"] or span < window_days * t["min_span_fraction"]:
        return {**detail, "label": "INSUFFICIENT_DATA"}
    first, last = points[0][1], points[-1][1]
    if kind == "points":
        change = last - first
    elif first == 0:
        change = math.inf if last > 0 else (0.0 if last == 0 else -math.inf)
    else:
        change = (last - first) / abs(first) * 100
    steps = [b[1] - a[1] for a, b in zip(points, points[1:])]
    sign = 1 if change > 0 else (-1 if change < 0 else 0)
    consistency = (sum(1 for s in steps if (s > 0 if sign > 0 else s < 0 if sign < 0 else s == 0)) / len(steps))
    c = cv([p[1] for p in points])
    mid = n // 2

    def vel(seg):
        d = (seg[-1][0] - seg[0][0]).total_seconds() / 86400
        return (seg[-1][1] - seg[0][1]) / d if d > 0 else None
    v1, v2 = vel(points[: mid + 1]), vel(points[mid:])
    detail.update({"change": NEW_GROWTH if math.isinf(change) and change > 0 else round(change, 2),
                   "change_unit": "points" if kind == "points" else "percent",
                   "consistency": round(consistency, 3), "cv": None if c is None else round(c, 4),
                   "first_half_velocity_per_day": None if v1 is None else round(v1, 4),
                   "second_half_velocity_per_day": None if v2 is None else round(v2, 4)})
    g, dmin = th["growing_min"], th["declining_min"]
    growing = change >= g and consistency >= t["min_consistency"]
    accelerating = False
    if growing and v1 is not None and v2 is not None and v2 > 0:
        # 2nd half faster than 1st half by the margin (if 1st half was flat/negative, any faster positive pace)
        accelerating = v2 > v1 * (1 + t["acceleration_margin"]) if v1 > 0 else v2 > v1
    if c is not None and c >= t["volatile_cv"] and consistency < t["min_consistency"]:
        label = "VOLATILE"
    elif accelerating:
        label = "ACCELERATING"
    elif growing:
        label = "GROWING"
    elif change <= -dmin and consistency >= t["min_consistency"]:
        label = "DECLINING"
    elif abs(change) < g and abs(change) < dmin:
        label = "STABLE"
    else:
        label = "VOLATILE"
    return {**detail, "label": label}


def trends_from(obs, cfg):
    w, out = cfg["windows"], {}
    if not obs:
        return {}
    latest = max(parse_ts(o["observation_timestamp"]) for o in obs)
    for wname, days in (("short", w["short_window_days"]), ("medium", w["medium_window_days"]),
                        ("long", w["long_window_days"])):
        start = latest - timedelta(days=days)
        out[wname] = {}
        for m, kind in cfg["trend"]["metrics"].items():
            pts = [(parse_ts(o["observation_timestamp"]), metric(o, m)) for o in obs
                   if metric(o, m) is not None and parse_ts(o["observation_timestamp"]) >= start]
            out[wname][m] = classify_window(pts, days, kind, cfg)
    return out


# ================================================================== age / snapshot
def tracking_age(obs):
    """TRACKING age (how long WE have observed it) — not the marketplace listing age."""
    if not obs:
        return {"first_seen_date": None, "last_seen_date": None, "observation_count": 0, "days_observed": 0,
                "product_tracking_age_days": None}
    ts = [parse_ts(o["observation_timestamp"]) for o in obs]
    return {"first_seen_date": min(ts).date().isoformat(), "last_seen_date": max(ts).date().isoformat(),
            "first_seen_timestamp": iso(min(ts)), "last_seen_timestamp": iso(max(ts)),
            "observation_count": len(obs), "days_observed": len({t.date() for t in ts}),
            "product_tracking_age_days": (max(ts).date() - min(ts).date()).days}


def snapshot_from(obs, cfg):
    raw_obs = obs
    obs = collapse_same_cycle(obs, (cfg.get("analysis") or {}).get("same_cycle_hours"))
    d, trends, vol = deltas_from(obs), trends_from(obs, cfg), volatility_from(obs, cfg)
    age = tracking_age(raw_obs)

    def g(m, k):
        return d[m].get(k, NA)
    snap = {
        "latest_wps": g("wps", "current"), "previous_wps": g("wps", "previous"), "wps_change": g("wps", "delta"),
        "latest_gmv": g("gmv", "current"), "previous_gmv": g("gmv", "previous"), "gmv_change": g("gmv", "delta"),
        "gmv_percent_change": g("gmv", "percent_change"),
        "latest_units": g("units", "current"), "units_change": g("units", "delta"),
        "units_percent_change": g("units", "percent_change"),
        "latest_creators": g("creators", "current"), "previous_creators": g("creators", "previous"),
        "creator_change": g("creators", "delta"), "creator_percent_change": g("creators", "percent_change"),
        "latest_videos": g("videos", "current"), "previous_videos": g("videos", "previous"),
        "video_change": g("videos", "delta"), "video_percent_change": g("videos", "percent_change"),
        "short_trend": trends.get("short", {}).get("gmv", {}).get("label", "INSUFFICIENT_DATA"),
        "medium_trend": trends.get("medium", {}).get("gmv", {}).get("label", "INSUFFICIENT_DATA"),
        "long_trend": trends.get("long", {}).get("gmv", {}).get("label", "INSUFFICIENT_DATA"),
        "volatility_level": vol["volatility_level"],
        "observation_count": age["observation_count"], "days_observed": age["days_observed"],
        "first_seen_date": age["first_seen_date"], "last_seen_date": age["last_seen_date"],
        "product_tracking_age_days": age["product_tracking_age_days"],
        "insufficient_history": age["observation_count"] < 2,
    }
    return {"snapshot": snap, "deltas": d, "velocity": velocity_from(obs), "trends": trends, "volatility": vol}


def calculate_deltas(product_id, store=None):
    return deltas_from(get_product_history(product_id, store))


def calculate_velocity(product_id, store=None):
    return velocity_from(get_product_history(product_id, store))


def calculate_trends(product_id, store=None, cfg=None):
    return trends_from(get_product_history(product_id, store), cfg or (store or default_store()).cfg)


def calculate_volatility(product_id, store=None, cfg=None):
    return volatility_from(get_product_history(product_id, store), cfg or (store or default_store()).cfg)


def momentum_snapshot(product_id, store=None, cfg=None):
    store = store or default_store()
    return snapshot_from(get_product_history(product_id, store), cfg or store.cfg)


# ================================================================== migration (Stage 14)
def _read(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def collect_observations(processed_dir=ROOT / "data" / "processed", cfg=None):
    """Build observations from existing processed files (read-only). Never deletes sources."""
    cfg = cfg or load_cfg()
    processed = Path(processed_dir)
    mig, obs, skipped = cfg["migration"], [], []
    if "discovery" in mig["sources"]:
        for f in sorted(processed.glob("discovery_*.json")):
            data = _read(f) or {}
            recs = (data.get("candidates") or []) + ((data.get("failed") or []) if mig["include_discovery_failed"] else [])
            for r in recs:
                o = from_discovery(r, f, (data.get("market") or {}).get("region", "US"))
                (obs if o else skipped).append(o or {"file": str(f), "reason": "no valid timestamp"})
    if "deep_analysis" in mig["sources"]:
        def load(sub, pat):
            out = []
            for f in sorted((processed / sub).glob(pat)):
                for r in (_read(f) or {}).get("results", []):
                    if r.get("status", "ok") == "ok" and r.get("product_id"):
                        out.append({**r, "_file": str(f)})
            return out
        deep, amz, bvs = load("deep_analysis", "deep_*.json"), load("amazon_validation", "amazon_*.json"), \
            load("business_viability", "bvs_*.json")
        by_pid = {}
        for d in deep:
            by_pid.setdefault(str(d["product_id"]), []).append(d)
        for pid, ds in by_pid.items():
            ds = sorted(ds, key=lambda d: parse_ts(d.get("observation_timestamp")) or datetime.min.replace(tzinfo=timezone.utc))
            for i, d in enumerate(ds):
                t0 = parse_ts(d.get("observation_timestamp"))
                t1 = parse_ts(ds[i + 1].get("observation_timestamp")) if i + 1 < len(ds) else None

                def in_cycle(r):
                    t = parse_ts(r.get("observation_timestamp"))
                    return t is not None and t0 is not None and t >= t0 and (t1 is None or t < t1)
                a = [r for r in amz if str(r["product_id"]) == pid and in_cycle(r)]
                b = [r for r in bvs if str(r["product_id"]) == pid and in_cycle(r)]
                last = (lambda xs: sorted(xs, key=lambda r: parse_ts(r["observation_timestamp"]))[-1] if xs else None)
                o = from_deep(d, d["_file"], last(a), last(b))
                (obs if o else skipped).append(o or {"file": d["_file"], "reason": "no valid timestamp"})
    return obs, skipped


def migrate(processed_dir=ROOT / "data" / "processed", store=None, dry_run=True):
    """Append processed observations to history. DRY-RUN by default: nothing is written."""
    store = store or default_store()
    obs, skipped = collect_observations(processed_dir, store.cfg)
    plan, seen = [], set()
    for o in obs:
        key = (safe_id(o["identity_key"]), o["observation_timestamp"])
        if key in seen:
            plan.append({"identity": key[0], "timestamp": key[1], "stage": o["source_stage"], "action": "duplicate_in_batch"})
            continue
        seen.add(key)
        action, path = store.append(o, dry_run=dry_run)
        plan.append({"identity": key[0], "timestamp": key[1], "stage": o["source_stage"], "action": action,
                     "path": str(path)})
    summary = {"dry_run": dry_run, "observations_found": len(obs), "skipped_invalid": len(skipped),
               "new": sum(p["action"] in ("written", "would_write") for p in plan),
               "duplicates": sum(p["action"] in ("duplicate", "duplicate_in_batch", "duplicate_snapshot") for p in plan),
               "products": len({p["identity"] for p in plan}),
               "by_stage": {s: sum(p["stage"] == s for p in plan) for s in ("discovery", "deep_analysis")}}
    if not dry_run:
        summary["index_products"] = store.rebuild_index()["product_count"]
    return {"summary": summary, "plan": plan, "skipped": skipped}


# ================================================================== CLI
def main(argv):
    if len(argv) >= 2 and argv[1] == "migrate":
        r = migrate(dry_run="--apply" not in argv)
        s = r["summary"]
        print(f"MODE: {'DRY-RUN (nothing written)' if s['dry_run'] else 'APPLY'}")
        print(json.dumps(s, indent=2))
        for p in r["plan"][:10]:
            print(f"  [{p['action']}] {p['stage']:<13} {p['identity']}  {p['timestamp']}")
        if len(r["plan"]) > 10:
            print(f"  ... {len(r['plan']) - 10} more")
        return 0
    if len(argv) >= 3 and argv[1] == "show":
        print(json.dumps(momentum_snapshot(argv[2]), indent=2, default=str))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
