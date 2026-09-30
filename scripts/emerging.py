"""Emerging Product Detector (Step R): momentum across historical observations.

Uses ONLY the Step Q history store (scripts/history.py). Produces, per product:
  Momentum Score (0-100), Momentum Confidence (0-100), Emerging Status + flags.
These are separate from WPS, Confidence, AVS, Amazon Confidence, BVS and BVS
Confidence, which are read (never modified). All thresholds: config/emerging.yaml.

Outputs (never overwritten): data/processed/emerging/emerging_<timestamp>.json
Replaced each run:          data/processed/emerging/latest.json

CLI:
  python3 scripts/emerging.py            # evaluate every product in data/history (no queries, free)
"""
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import history as HIST  # noqa: E402

NA = "N/A"
NEW_GROWTH = HIST.NEW_GROWTH
DELTA_METRICS = ["wps", "gmv", "units", "creators", "selling_creators", "videos", "selling_videos", "competition"]
POINTS_METRICS = {"wps"}
STATUS_ORDER = ["EMERGING_STRONG", "EMERGING", "EMERGING_REVIEW"]


def load_cfg(path=ROOT / "config" / "emerging.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)["emerging_detector"]


def rnd(x, p=4):
    if x is None or isinstance(x, str):
        return x
    return float(Decimal(repr(x)).quantize(Decimal(1).scaleb(-p), rounding=ROUND_HALF_UP))


def level(score, levels):
    for name, t in sorted(levels.items(), key=lambda kv: -kv[1]):
        if score >= t:
            return name
    return None


# ================================================================== series selection
class Series:
    """Resolves logical metrics (competition, creators_primary, ...) to history metrics."""

    def __init__(self, obs, cfg):
        self.obs, self.cfg = obs, cfg
        mn = cfg["minimum_observations"]
        comp = [m for m in cfg["competition_metrics"] if len(HIST.series(obs, m)) >= mn] or \
               [m for m in cfg["competition_metrics"] if HIST.series(obs, m)]
        self.map = {
            "competition": comp[0] if comp else cfg["competition_metrics"][0],
            "creators_primary": "selling_creators" if len(HIST.series(obs, "selling_creators")) >= mn else "creators",
            "videos_primary": "selling_videos" if len(HIST.series(obs, "selling_videos")) >= mn else "videos",
        }

    def name(self, logical):
        return self.map.get(logical, logical)

    def points(self, logical):
        return HIST.series(self.obs, self.name(logical))


# ================================================================== deltas / rates
def deltas(obs, s):
    out = {}
    for m in DELTA_METRICS:
        pts = s.points(m)
        cur = pts[-1][1] if pts else None
        prev = pts[-2][1] if len(pts) >= 2 else None
        out[m] = {"metric": s.name(m), "current": NA if cur is None else cur, "previous": NA if prev is None else prev,
                  "change": NA if prev is None else rnd(cur - prev),
                  "percent_change": NA if prev is None else HIST.pct_change(prev, cur)}
    return out


def growth_rate(points):
    """(% change first->last, %/day). NEW_GROWTH when first = 0 and last > 0. None if < 2 points or no span."""
    if len(points) < 2:
        return None, None
    (t0, v0), (t1, v1) = points[0], points[-1]
    span = (t1 - t0).total_seconds() / 86400
    if span <= 0:
        return None, None
    if v0 == 0:
        return (NEW_GROWTH, NEW_GROWTH) if v1 > 0 else ((0.0, 0.0) if v1 == 0 else (None, None))
    pct = (v1 - v0) / abs(v0) * 100
    return pct, pct / span


# ================================================================== acceleration (Stage 6)
def classify_acceleration(logical, obs, s, cfg):
    name = s.name(logical)
    a = HIST.acceleration_for(obs, name)
    if a is None:
        return {"label": "INSUFFICIENT_DATA", "acceleration": NA, "previous_velocity": NA, "current_velocity": NA}
    kind = "points_metrics" if logical in POINTS_METRICS else "percent_metrics"
    th = cfg["acceleration"][kind]
    pv, cv, acc = a["previous_velocity"], a["current_velocity"], a["acceleration"]
    if kind == "percent_metrics":
        v1, v2, v3 = a["values"]
        base = abs(v2) or max(abs(v1), abs(v2), abs(v3))
        if base == 0:
            return {"label": "FLAT", "acceleration": 0.0, "previous_velocity": 0.0, "current_velocity": 0.0}
        pv, cv, acc = pv / base * 100, cv / base * 100, acc / base * 100
    if cv > th["flat_band"] and acc >= th["accel_min"]:
        label = "ACCELERATING"
    elif acc <= -th["accel_min"] or cv < -th["flat_band"]:
        label = "DECELERATING"
    elif cv > th["flat_band"]:
        label = "POSITIVE"
    else:
        label = "FLAT"
    return {"label": label, "acceleration": rnd(acc), "previous_velocity": rnd(pv), "current_velocity": rnd(cv),
            "raw_acceleration_per_day2": rnd(a["acceleration"]),
            "unit": "points/day" if kind == "points_metrics" else "% of value/day"}


# ================================================================== trends (Step Q)
def trend(logical, obs, s, cfg, hist_cfg):
    name = s.name(logical)
    pts_all = HIST.series(obs, name)
    if not pts_all:
        return {"label": "INSUFFICIENT_DATA", "window": None}
    latest = pts_all[-1][0]
    kind = "points" if logical in POINTS_METRICS else "percent"
    wins = hist_cfg["windows"]
    days = {"short": wins["short_window_days"], "medium": wins["medium_window_days"], "long": wins["long_window_days"]}
    last = None
    for w in cfg["trend_windows"]:
        pts = [p for p in pts_all if p[0] >= latest - timedelta(days=days[w])]
        r = HIST.classify_window(pts, days[w], kind, hist_cfg)
        last = {**r, "window": w}
        if r["label"] != "INSUFFICIENT_DATA":
            return last
    return last


DIRECTION = {"ACCELERATING": "up", "GROWING": "up", "STABLE": "flat", "DECLINING": "down"}


def signal(tr, acc):
    d = DIRECTION.get(tr["label"])
    if d is None:
        return None
    return {"dir": d, "strong": d == "up" and (tr["label"] == "ACCELERATING" or acc["label"] == "ACCELERATING")}


def momentum_class(*signals):
    known = [x for x in signals if x is not None]
    if not known:
        return "INSUFFICIENT_DATA"
    dirs = {x["dir"] for x in known}
    if "down" in dirs and "up" not in dirs:
        return "DECLINING"
    if "down" in dirs and "up" in dirs:
        return "STABLE"
    if dirs == {"up"}:
        return "STRONG_GROWTH" if any(x["strong"] for x in known) else "GROWING"
    if "up" in dirs:
        return "GROWING"
    return "STABLE"


# ================================================================== competition (Stage 10)
def competition_status(comp_rate, demand_rate, cfg):
    c = cfg["competition"]
    if comp_rate is None or demand_rate is None:
        return "INSUFFICIENT_DATA", NA
    if comp_rate == NEW_GROWTH:
        comp_rate = math.inf
    if demand_rate == NEW_GROWTH:
        demand_rate = math.inf
    if demand_rate <= 0:
        return ("OUTPACING_DEMAND" if comp_rate > c["rising_min_pct_per_day"] else "HEALTHY"), NA
    if comp_rate <= c["rising_min_pct_per_day"]:
        ratio = comp_rate / demand_rate if math.isfinite(demand_rate) else 0.0
        return "HEALTHY", rnd(ratio)
    if math.isinf(demand_rate):
        return "RISING", 0.0
    ratio = comp_rate / demand_rate
    if ratio < c["catching_up_ratio"]:
        return "RISING", rnd(ratio)
    if ratio < c["outpacing_ratio"]:
        return "CATCHING_UP", rnd(ratio)
    return "OUTPACING_DEMAND", (NA if math.isinf(ratio) else rnd(ratio))


# ================================================================== score / confidence
def lin(x, z, f):
    if x == NEW_GROWTH:
        return 1.0
    if x is None:
        return None
    return max(0.0, min(1.0, (x - z) / (f - z)))


def momentum_score(rates, accels, comp_status, cfg):
    ms, total, bd = cfg["momentum_score"], 0.0, {}
    for name, c in ms["components"].items():
        if "map" in c:
            s = c["map"].get(comp_status)
            detail = {"competition_status": comp_status}
        else:
            sr = lin(rates.get(c["metric"]), c["rate_zero_at"], c["rate_full_at"])
            sa = ms["acceleration_map"].get(accels[c["metric"]]["label"])
            if sr is None:
                s = None
            else:
                s = c["rate_weight"] * sr + c["acceleration_weight"] * (sa if sa is not None else 0.0)
            detail = {"rate_per_day": rates.get(c["metric"]), "s_rate": rnd(sr), "acceleration": accels[c["metric"]]["label"],
                      "s_acceleration": sa}
        if s is None:
            bd[name] = {"points": NA, "max": c["points"], **detail}
        else:
            total += s * c["points"]
            bd[name] = {"points": rnd(s * c["points"], 2), "max": c["points"], **detail}
    return rnd(total, 2), bd


def momentum_confidence(obs, s, conc, cfg):
    mc, total, bd = cfg["momentum_confidence"], 0.0, {}
    n = len(obs)
    ts = [HIST.parse_ts(o["observation_timestamp"]) for o in obs]
    span = (max(ts) - min(ts)).total_seconds() / 86400 if ts else 0

    def share(logical):
        return len(s.points(logical)) / n if n else 0.0
    fr = {"observation_count": min(n / mc["components"]["observation_count"]["full_at"], 1.0),
          "observation_day_span": min(span / mc["components"]["observation_day_span"]["full_at"], 1.0),
          "gmv_completeness": share("gmv"), "units_completeness": share("units"), "wps_completeness": share("wps"),
          "creator_completeness": share("creators_primary"), "video_completeness": share("videos_primary"),
          "competition_completeness": share("competition"),
          "concentration_completeness": (0.5 * (conc["top_creator_share"] is not None)
                                         + 0.5 * (conc["top_video_share"] is not None))}
    for name, c in mc["components"].items():
        pts = c["points"] * fr[name]
        total += pts
        bd[name] = {"earned": rnd(pts, 2), "max": c["points"],
                    "status": "full" if fr[name] == 1 else ("missing" if fr[name] == 0 else "partial")}
    score = rnd(total, 2)
    return score, level(score, mc["levels"]), bd


# ================================================================== evaluate one product
def _latest_value(obs, name):
    pts = HIST.series(obs, name)
    return pts[-1][1] if pts else None


def evaluate(obs_all, cfg=None, hist_cfg=None, now=None):
    """Full Step R evaluation for one product's observations (oldest first)."""
    cfg = cfg or load_cfg()
    hist_cfg = hist_cfg or HIST.load_cfg()
    obs_all = sorted(obs_all, key=lambda o: HIST.parse_ts(o["observation_timestamp"]))
    # Step S fix: Discovery + Deep of the same run (minutes apart) are ONE cycle for momentum
    obs_all = HIST.collapse_same_cycle(obs_all, (hist_cfg.get("analysis") or {}).get("same_cycle_hours"))
    last = obs_all[-1] if obs_all else {}
    base = {"product_id": last.get("product_id") or last.get("identity_key"), "product_name": last.get("product_name"),
            "observation_timestamp": (now or datetime.now(timezone.utc)).isoformat(),
            "config_version": cfg["version"]}
    if not obs_all:
        return {**base, "emerging_status": "INSUFFICIENT_HISTORY", "emerging_flags": ["INSUFFICIENT_HISTORY"],
                "status_reason": "no observations"}
    latest_ts = HIST.parse_ts(last["observation_timestamp"])
    obs = [o for o in obs_all if HIST.parse_ts(o["observation_timestamp"]) >= latest_ts - timedelta(days=cfg["evaluation_window_days"])]
    age = HIST.tracking_age(obs)
    s = Series(obs, cfg)
    cur_wps, cur_conf = _latest_value(obs, "wps"), _latest_value(obs, "confidence")
    base.update({"observation_count": age["observation_count"], "observation_days": age["days_observed"],
                 "history_start": age["first_seen_timestamp"], "history_end": age["last_seen_timestamp"],
                 "current_wps": NA if cur_wps is None else cur_wps,
                 "current_wps_confidence": NA if cur_conf is None else cur_conf})

    reasons = []
    if age["observation_count"] < cfg["minimum_observations"]:
        reasons.append(f"{age['observation_count']} observation(s) < {cfg['minimum_observations']}")
    if age["days_observed"] < cfg["minimum_observation_days"]:
        reasons.append(f"{age['days_observed']} observation day(s) < {cfg['minimum_observation_days']}")
    if cur_wps is None:
        reasons.append("current WPS not available")
    if cur_conf is None:
        reasons.append("WPS Confidence not available")
    if reasons:
        return {**base, "emerging_status": "INSUFFICIENT_HISTORY", "emerging_flags": ["INSUFFICIENT_HISTORY"],
                "status_reason": "; ".join(reasons), "momentum_score": NA, "momentum_confidence": NA}

    d = deltas(obs, s)
    accels = {m: classify_acceleration(m, obs, s, cfg) for m in
              ["wps", "gmv", "units", "creators", "selling_creators", "videos", "selling_videos", "competition",
               "creators_primary", "videos_primary"]}
    trends = {m: trend(m, obs, s, cfg, hist_cfg) for m in
              ["wps", "gmv", "units", "creators", "selling_creators", "videos", "selling_videos", "competition"]}
    rates, pct = {}, {}
    for m in ["wps", "gmv", "units", "creators", "selling_creators", "videos", "selling_videos", "competition",
              "creators_primary", "videos_primary"]:
        pts = s.points(m)
        if m in POINTS_METRICS:
            span = (pts[-1][0] - pts[0][0]).total_seconds() / 86400 if len(pts) >= 2 else 0
            pct[m] = rnd(pts[-1][1] - pts[0][1]) if len(pts) >= 2 else None
            rates[m] = rnd((pts[-1][1] - pts[0][1]) / span) if span > 0 else None
        else:
            p, r = growth_rate(pts)
            pct[m], rates[m] = rnd(p, 2), rnd(r)
    vel = {m: HIST.velocity_for(obs, s.name(m)) for m in DELTA_METRICS}

    def sig(m):
        return signal(trends[m], accels[m])
    demand = momentum_class(sig("gmv"), sig("units"))
    sel_c = s.name("creators_primary") == "selling_creators"
    sel_v = s.name("videos_primary") == "selling_videos"
    creator_m = momentum_class(sig("selling_creators"), sig("creators")) if sel_c else momentum_class(sig("creators"))
    video_m = momentum_class(sig("selling_videos"), sig("videos")) if sel_v else momentum_class(sig("videos"))
    indiv = {k: momentum_class(sig(k)) for k in ("creators", "selling_creators", "videos", "selling_videos")}

    dem_rates = [r for r in (rates["gmv"], rates["units"]) if r is not None]
    if not dem_rates:
        demand_rate = None
    elif NEW_GROWTH in dem_rates:
        demand_rate = NEW_GROWTH
    else:
        demand_rate = sum(dem_rates) / len(dem_rates)
    comp_status, ratio = competition_status(rates["competition"], demand_rate, cfg)

    conc = {"top_creator_share": _latest_value(obs, "top_creator_share"),
            "top_video_share": _latest_value(obs, "top_video_share")}
    score, score_bd = momentum_score(rates, accels, comp_status, cfg)
    mconf, mlevel, mconf_bd = momentum_confidence(obs, s, conc, cfg)

    # ---------------------------------------------------------------- flags
    f, fl = [], cfg["flags"]
    rapid = fl["rapid_growth_pct_per_day"]

    def fast(m):
        r = rates.get(m)
        return r == NEW_GROWTH or (r is not None and r >= rapid)
    if accels["gmv"]["label"] == "ACCELERATING" and fast("gmv"):
        f.append("RAPID_GMV_ACCELERATION")
    if accels["units"]["label"] == "ACCELERATING" and fast("units"):
        f.append("RAPID_UNIT_ACCELERATION")
    for m, code in (("creators", "RAPID_CREATOR_GROWTH"), ("selling_creators", "RAPID_SELLING_CREATOR_GROWTH"),
                    ("videos", "RAPID_VIDEO_GROWTH"), ("selling_videos", "RAPID_SELLING_VIDEO_GROWTH")):
        if fast(m):
            f.append(code)
    if accels["wps"]["label"] == "ACCELERATING":
        f.append("WPS_ACCELERATING")
    f += {"RISING": ["COMPETITION_RISING"], "CATCHING_UP": ["COMPETITION_CATCHING_UP"],
          "OUTPACING_DEMAND": ["COMPETITION_OUTPACING_DEMAND"]}.get(comp_status, [])
    dep_c = conc["top_creator_share"] is not None and conc["top_creator_share"] / 100 > cfg["maximum_creator_dependency"]
    dep_v = conc["top_video_share"] is not None and conc["top_video_share"] / 100 > cfg["maximum_video_dependency"]
    if dep_c:
        f.append("CREATOR_DEPENDENCY")
    if dep_v:
        f.append("VIDEO_DEPENDENCY")
    if accels["gmv"]["label"] == "DECELERATING":
        f.append("MOMENTUM_SLOWING")
    if demand == "DECLINING":
        f.append("DEMAND_DECLINING")
    st = cfg["status"]
    wps_change = pct["wps"]
    if wps_change is not None and wps_change <= -st["wps_decline_points"]:
        f.append("WPS_DECLINING")
    if mconf < fl["insufficient_momentum_confidence_below"]:
        f.append("INSUFFICIENT_MOMENTUM_DATA")

    # ---------------------------------------------------------------- status (first match wins)
    positives = sum(indiv[k] in st["positive_signal_classes"] for k in indiv)
    wps_ok = cur_wps >= cfg["minimum_wps"] and cur_conf >= cfg["minimum_wps_confidence"]
    dep_block = (dep_c or dep_v) and not st["allow_dependency_for_strong"]
    severe = [x for x in f if x in st["severe_flags"]]
    if demand == "DECLINING" or "WPS_DECLINING" in f or \
            (ratio != NA and ratio >= cfg["competition"]["significant_outpacing_ratio"]) or \
            (comp_status == "OUTPACING_DEMAND" and ratio == NA):
        status, why = "LOSING_MOMENTUM", "demand declining, WPS declining or competition significantly outpacing demand"
    elif score >= cfg["strong_emerging_min_momentum_score"] and wps_ok and demand in st["strong_demand"] \
            and positives >= cfg["minimum_positive_growth_signals"] and comp_status != "OUTPACING_DEMAND" \
            and not dep_block and mconf >= st["strong_min_momentum_confidence"]:
        status, why = "EMERGING_STRONG", "all strong-emerging rules met"
    elif score >= cfg["emerging_min_momentum_score"] and wps_ok and demand in st["emerging_demand"] \
            and (creator_m in st["positive_signal_classes"] or video_m in st["positive_signal_classes"]) \
            and not severe and mconf >= st["emerging_min_momentum_confidence"]:
        status, why = "EMERGING", "all emerging rules met"
    elif score >= st["review_min_momentum_score"] and demand != "DECLINING":
        missing = []
        if not wps_ok:
            missing.append("WPS/WPS Confidence below minimum")
        if mconf < st["emerging_min_momentum_confidence"]:
            missing.append(f"Momentum Confidence {mconf} below {st['emerging_min_momentum_confidence']}")
        if demand not in st["emerging_demand"]:
            missing.append(f"demand momentum {demand}")
        if dep_block:
            missing.append("creator/video dependency")
        if severe:
            missing.append("severe flags: " + ", ".join(severe))
        status, why = "EMERGING_REVIEW", "promising momentum but: " + ("; ".join(missing) or "score below emerging threshold")
    else:
        status = "STABLE"
        why = "no meaningful acceleration" if wps_ok else "WPS/WPS Confidence below minimum; no qualifying momentum"
    if status in ("EMERGING_STRONG", "EMERGING") and "INSUFFICIENT_MOMENTUM_DATA" in f:
        status, why = "EMERGING_REVIEW", "momentum data insufficient"

    missing_data = [f"{m}_history" for m in ("wps", "gmv", "units", "creators_primary", "videos_primary", "competition")
                    if len(s.points(m)) < 2]
    if conc["top_creator_share"] is None:
        missing_data.append("top_creator_revenue_share")
    if conc["top_video_share"] is None:
        missing_data.append("top_video_revenue_share")

    def v(m):
        return NA if vel[m] is None else rnd(vel[m])

    def a(m):
        return accels[m]["acceleration"]
    prev_wps = d["wps"]["previous"]
    return {
        **base,
        "previous_wps": prev_wps, "wps_change": d["wps"]["change"], "wps_velocity": v("wps"), "wps_acceleration": a("wps"),
        "current_gmv": d["gmv"]["current"], "previous_gmv": d["gmv"]["previous"], "gmv_change": d["gmv"]["change"],
        "gmv_percent_change": d["gmv"]["percent_change"], "gmv_velocity": v("gmv"), "gmv_acceleration": a("gmv"),
        "current_units": d["units"]["current"], "units_change": d["units"]["change"],
        "units_percent_change": d["units"]["percent_change"], "units_velocity": v("units"), "units_acceleration": a("units"),
        "creator_growth_rate": NA if pct["creators"] is None else pct["creators"],
        "creator_velocity": v("creators"), "creator_acceleration": a("creators"),
        "selling_creator_growth_rate": NA if pct["selling_creators"] is None else pct["selling_creators"],
        "selling_creator_velocity": v("selling_creators"), "selling_creator_acceleration": a("selling_creators"),
        "video_growth_rate": NA if pct["videos"] is None else pct["videos"],
        "video_velocity": v("videos"), "video_acceleration": a("videos"),
        "selling_video_growth_rate": NA if pct["selling_videos"] is None else pct["selling_videos"],
        "selling_video_velocity": v("selling_videos"), "selling_video_acceleration": a("selling_videos"),
        "competition_metric": s.name("competition"),
        "competition_growth_rate": NA if pct["competition"] is None else pct["competition"],
        "competition_growth_rate_per_day": NA if rates["competition"] is None else rates["competition"],
        "demand_growth_rate_per_day": NA if demand_rate is None else rnd(demand_rate) if demand_rate != NEW_GROWTH else NEW_GROWTH,
        "competition_velocity": v("competition"), "competition_pressure_ratio": ratio,
        "demand_momentum": demand, "creator_momentum": creator_m, "video_momentum": video_m,
        "competition_status": comp_status,
        "individual_signals": indiv, "positive_supporting_signals": positives,
        "trends": {k: t["label"] for k, t in trends.items()},
        "trend_detail": trends, "acceleration_detail": accels, "deltas": d,
        "concentration": {"top_creator_revenue_share": NA if conc["top_creator_share"] is None else conc["top_creator_share"],
                          "top_video_revenue_share": NA if conc["top_video_share"] is None else conc["top_video_share"]},
        "momentum_score": score, "momentum_score_breakdown": score_bd,
        "momentum_confidence": mconf, "momentum_confidence_level": mlevel,
        "momentum_confidence_breakdown": mconf_bd,
        "emerging_status": status, "status_reason": why, "emerging_flags": f,
        "missing_data": missing_data,
        "growth_rate_note": "*_growth_rate = % change first->last valid observation in the evaluation window; "
                            "*_velocity = per day between the last two observations (real elapsed time)",
        "sources": sorted({o.get("_path") or (o.get("source") or {}).get("raw_file") or "" for o in obs} - {""}),
    }


# ================================================================== priority / run / storage
def priority_list(results):
    rank = {s: i for i, s in enumerate(STATUS_ORDER)}
    pool = [r for r in results if r.get("emerging_status") in rank]

    def neg(x):
        return -x if isinstance(x, (int, float)) and not isinstance(x, bool) else math.inf
    pool.sort(key=lambda r: (rank[r["emerging_status"]], neg(r.get("momentum_score")), neg(r.get("momentum_confidence")),
                             neg(r.get("current_wps")), neg(r.get("current_wps_confidence")), str(r.get("product_id"))))
    return [{"priority": i, "product_id": r["product_id"], "product_name": r["product_name"],
             "emerging_status": r["emerging_status"], "momentum_score": r["momentum_score"],
             "momentum_confidence": r["momentum_confidence"], "current_wps": r["current_wps"],
             "current_wps_confidence": r["current_wps_confidence"]} for i, r in enumerate(pool, 1)]


def detect_all(store=None, cfg=None, now=None):
    cfg = cfg or load_cfg()
    store = store or HIST.default_store()
    if not cfg["enabled"]:
        return {"enabled": False, "results": [], "priority_list": []}
    results = [evaluate(store.observations(i), cfg, store.cfg, now) for i in store.identities()]
    results = [r for r in results if r.get("product_id")]
    counts = {}
    for r in results:
        counts[r["emerging_status"]] = counts.get(r["emerging_status"], 0) + 1
    return {"version": cfg["version"], "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
            "products_evaluated": len(results), "status_counts": counts,
            "priority_list": priority_list(results), "results": results}


def save(report, out_dir=None, cfg=None):
    cfg = cfg or load_cfg()
    out_dir = Path(out_dir or ROOT / cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path, n = out_dir / f"emerging_{ts}.json", 1
    while path.exists():
        n += 1
        path = out_dir / f"emerging_{ts}_{n}.json"
    with open(path, "x") as fh:                      # historical outputs: never overwritten
        json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
    os.chmod(path, 0o444)
    latest = out_dir / "latest.json"
    tmp = out_dir / "latest.json.tmp"
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    os.replace(tmp, latest)                          # latest.json may be replaced
    return path, latest


def main(argv):
    r = detect_all()
    path, latest = save(r)
    print(f"Evaluated {r.get('products_evaluated', 0)} product(s): {r.get('status_counts')}")
    for p in r.get("priority_list", []):
        print(f"  {p['priority']:>2}. {p['emerging_status']:<16} momentum {p['momentum_score']} "
              f"(conf {p['momentum_confidence']}) WPS {p['current_wps']}  {p['product_name']}")
    print(f"Saved: {path}\nLatest: {latest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
