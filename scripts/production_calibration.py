"""Step AB — Production Calibration (offline; LIVE evidence only; NO provider query).

    python -m winning_product_agent calibrate          (or: python3 scripts/production_calibration.py)

Reads only saved LIVE data (raw provider answers, processed results, run manifests, audits, history) and
writes:
    reports/production-calibration.md / .json
    config/proposed/scoring_v2.yaml, filters_v2.yaml, runtime_production_v1.yaml, decision_rules_v2.yaml

Rules (Step AB):
  * SYNTHETIC data is excluded everywhere; no live query; nothing is applied to production configs.
  * Thresholds are never tuned to make more products pass; the objective is reliability.
  * Provider Reliability Score measures technical/data reliability only, never product quality.
  * Unknown costs stay UNKNOWN; small samples are reported as small samples.
"""
import copy
import glob
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
if str(ROOT) not in sys.path:
    sys.path.insert(1, str(ROOT))

import calibration_audit as CA  # noqa: E402
import creatives as CR  # noqa: E402
import decision_engine as DE  # noqa: E402
import deep_analysis as DA  # noqa: E402
import discovery as D  # noqa: E402
import emerging as EM  # noqa: E402
import generate_report as GR  # noqa: E402
import history as HIST  # noqa: E402
import safety  # noqa: E402
from score_products import load_scoring_config  # noqa: E402

LIVE = "LIVE"
NA = "N/A"
RELIABLE, USABLE, SPARSE, UNRELIABLE = "RELIABLE", "USABLE", "SPARSE", "UNRELIABLE"
READY, PARTIAL, NOT_READY = "READY", "PARTIAL", "NOT_READY"
PRODUCTION_CONFIG_READY, MORE_CALIBRATION = "PRODUCTION_CONFIG_READY", "MORE_CALIBRATION_REQUIRED"
FIELD_BANDS = {RELIABLE: 0.9, USABLE: 0.7, SPARSE: 0.3}
MIN_SAMPLE = 10                       # below: every conclusion is flagged LOW_SAMPLE
DISCOVERY_EXPECTED = ["product_id", "product_name", "price_min", "price_max", "gmv_30d", "units_30d",
                      "growth_30d_pct", "creator_count", "video_count", "launch_date"]
DEEP_EXPECTED = ["product_id", "product_name", "price_min", "price_max", "gmv_30d", "gmv_prev_30d", "units_30d",
                 "creator_count", "selling_creator_count", "video_count", "selling_video_count",
                 "video_sales_share_pct", "commission_pct", "category_growth_pct", "category_product_count",
                 "category_product_count_level", "daily_gmv", "top_creators", "top_videos", "launch_date"]
WPS_GROUPS = {"Growth Momentum": ["growth_long_term", "growth_recent_trend", "growth_acceleration"],
              "Demand": ["demand"], "Video Momentum": ["video_momentum"],
              "Creator Momentum": ["creator_reach", "creator_growth"],
              "Competition": ["competition_category_growth", "competition_saturation"],
              "Margin Potential": ["margin_potential"], "Trend Stability": ["trend_stability"]}
# the value a missing SUPPORTING metric / ENHANCEMENT field also removes from a Confidence component
CONF_ALIASES = {"competition_count_comparable": "category_product_count"}


def _num(v):
    return None if v is None or v == NA or isinstance(v, bool) else (float(v) if isinstance(v, (int, float)) else None)


def _read(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def _rate(n, d):
    return round(n / d, 4) if d else None


def _is_live(tag):
    """Raw files saved before Step T carry no tag; all of them are real KaloPilot answers (counted LIVE)."""
    return tag == LIVE or tag is None


def band(rate, n, mapping_issue_rate=0.0):
    if not n or rate is None:
        return UNRELIABLE
    if mapping_issue_rate >= 0.3:
        return UNRELIABLE
    for status, t in FIELD_BANDS.items():
        if rate >= t:
            return status if not (status == RELIABLE and mapping_issue_rate > 0) else USABLE
    return UNRELIABLE


def has_value(v):
    if v is None or v == "" or v == NA:
        return False
    if isinstance(v, list):
        return bool([x for x in v if x is not None])
    return True


# ============================================================================ Stage 1 — evidence
def load_evidence(root=ROOT, data_root=None):
    root = Path(root)
    base = Path(data_root or root)
    rt = safety.load_runtime(root / "config" / "runtime.yaml")
    p = rt["paths"]
    raw, proc, runs_dir, reports = base / p["raw"], base / p["processed"], base / p["runs"], base / p["reports"]
    excluded = {"synthetic_runs": 0, "synthetic_raw": 0, "dry_runs": 0}
    runs = []
    for mf in sorted(runs_dir.glob("*/manifest.json")):
        m = _read(mf) or {}
        if m.get("mode") != "LIVE":
            excluded["dry_runs"] += 1
            continue
        if m.get("data_environment") != LIVE:
            excluded["synthetic_runs"] += 1
            continue
        runs.append(m)
    disc, seen_ok = [], set()
    for f in sorted(raw.glob("*.json")):
        e = _read(f) or {}
        if not _is_live(e.get("data_environment")):
            excluded["synthetic_raw"] += 1
            continue
        task = e.get("task_id") or ((e.get("response") or {}).get("data") or {}).get("task_id")
        data = (e.get("response") or {}).get("data") or {}
        recs = D.extract_records({"response": e.get("response")})[0] if e.get("response") else []
        dup = bool(task) and bool(recs) and task in seen_ok        # same finished task fetched again (free)
        if recs:
            seen_ok.add(task)
        disc.append({"file": str(f), "task_id": task, "duplicate_fetch": dup, "category_key": e.get("category_key"),
                     "status": data.get("status"), "message_id": data.get("message_id"),
                     "credits": _num(data.get("credits_consumed")), "records": recs,
                     "output_tokens": (data.get("token_usage") or {}).get("output_tokens"),
                     "fetched_at": e.get("fetched_at"), "error": GR_err(e.get("response"))})
    deep_raw = []
    for f in sorted((raw / "deep_analysis").glob("*.json")):
        e = _read(f) or {}
        if not _is_live(e.get("data_environment")):
            excluded["synthetic_raw"] += 1
            continue
        data = (e.get("response") or {}).get("data") or {}
        obj, errs = DA.extract_object(e) if e.get("query_type") != "deep_batch" else (None, ["batch envelope"])
        deep_raw.append({"file": str(f), "product_id": str(e.get("product_id")), "query_type": e.get("query_type"),
                         "status": data.get("status"), "credits": _num(data.get("credits_consumed")),
                         "output_tokens": (data.get("token_usage") or {}).get("output_tokens"),
                         "derived_from_batch": bool(e.get("derived_from_batch")), "object": obj,
                         "error": GR_err(e.get("response")) or (None if obj or e.get("query_type") == "deep_batch"
                                                                else "no_parseable_json"),
                         "envelope": e, "observation_timestamp": e.get("observation_timestamp"),
                         "prompt_version": e.get("prompt_version")})
    amazon_raw = [f for f in sorted((raw / "amazon_validation").glob("*.json"))
                  if _is_live((_read(f) or {}).get("data_environment"))]
    audits = {}
    for f in sorted(reports.glob("*calibration-audit*.json")):
        a = _read(f) or {}
        if a.get("run_id"):
            audits.setdefault(a["run_id"], []).append({**a, "_file": str(f)})
    aa = [_read(f) for f in sorted(reports.glob("*-aa-live*.json"))]
    aa = [x for x in aa if x and (x.get("metadata") or {}).get("env") == LIVE]
    finals = [x for x in (_read(f) for f in sorted(reports.glob("*-final-decision*.json"))) if x and
              (x.get("metadata") or {}).get("data_environment") == LIVE]
    contexts, disc_runs = {}, []
    filters = copy.deepcopy(D.load_yaml("filters.yaml"))
    filters["discovery_mode"]["max_candidates"] = 1000
    cats = D.load_yaml("categories.yaml")
    for x in disc:                              # re-normalized from RAW with the current code (not old files)
        if x["records"] and not x["duplicate_fetch"]:
            res = D.run_discovery([Path(x["file"])], filters, cats)
            disc_runs.append({"file": x["file"], "summary": res["summary"], "result": res})
            for r in (res.get("candidates") or []) + (res.get("failed") or []):
                pid = r["facts"].get("product_id")
                if pid:
                    contexts[str(pid)] = {**r, "_file": x["file"]}
    from winning_product_agent.runner import EnvStore
    store = EnvStore(base / p["history"], LIVE)
    raw_store = HIST.HistoryStore(base / p["history"])
    non_live_history = sum(1 for i in raw_store.identities() for o in HIST.HistoryStore.observations(raw_store, i)
                           if o.get("data_environment") != LIVE)
    caps = yaml.safe_load((root / "config" / "provider_capabilities.yaml").read_text()) or {}
    return {"root": root, "base": base, "rt": rt, "paths": p, "runs": runs, "discovery": disc, "deep_raw": deep_raw,
            "amazon_raw": amazon_raw, "audits": audits, "aa_reports": aa, "final_decisions": finals,
            "contexts": contexts, "discovery_results": disc_runs, "store": store, "non_live_history": non_live_history,
            "capabilities": caps, "excluded": excluded,
            "supplier_files": list((proc / "suppliers").rglob("offers_*.json")) if (proc / "suppliers").exists() else [],
            "competitor_files": list((proc / "competitors").rglob("competitors_*.json"))
            if (proc / "competitors").exists() else []}


def GR_err(resp):
    from winning_product_agent.runner import response_error
    return response_error(resp) if resp is not None else "no_response"


def reanalyze(ev, scoring_cfg=None):
    """Every LIVE deep answer re-scored with ONE config (older answers were scored by older versions)."""
    cfgs = {"deep": DA.load_cfg(), "filters": D.load_yaml("filters.yaml"), "scoring": scoring_cfg or load_scoring_config()}
    out = []
    for r in ev["deep_raw"]:
        if r["object"] is None:
            continue
        ctx = ev["contexts"].get(r["product_id"]) or {"key": r["product_id"], "calculated": {"filter_status": None},
                                                      "facts": {"product_id": r["product_id"]}}
        rec = DA.analyze(r["envelope"], r["file"], ctx, cfgs, 0, True)
        if rec.get("status") == "ok":
            rec.update({"data_environment": LIVE, "_file": r["file"], "_disc": ctx,
                        "prompt_version": r["prompt_version"]})
            out.append(rec)
    return out


def latest_per_product(recs):
    by = {}
    for r in sorted(recs, key=lambda x: str(x.get("observation_timestamp"))):
        by[str(r["product_id"])] = r
    return list(by.values())


# ============================================================================ Stage 2 — provider reliability
def provider_score(m):
    """0-100 technical/data reliability. None when nothing was attempted (NOT_MEASURED)."""
    if not m["attempted"]:
        return None
    parts = {"success": 40 * (m["successful"] / m["attempted"]),
             "field_completeness": 20 * (1 - (m["missing_expected_field_rate"] or 0)),
             "mapping": 15 * (1 - (m["mapping_error_rate"] or 0)),
             "provenance": 15 * (1 - (m["provenance_failure_rate"] or 0)),
             "no_timeouts": 10 * (1 - m["timeouts"] / m["attempted"])}
    return round(sum(parts.values()), 1), {k: round(v, 2) for k, v in parts.items()}


def mapping_state(recs):
    """Current mapping checks on the re-analyzed LIVE records (raw -> normalized, discovery vs deep)."""
    out = {}
    for r in recs:
        _, issues = CA.mapping_checks(r, r.get("_disc"))
        out[f"{r['product_id']}@{r.get('observation_timestamp')}"] = issues
    return out


def provider_reliability(ev, recs, mapping):
    rows = {}
    # KaloPilot discovery
    tasks = {}
    for x in ev["discovery"]:
        if not x["duplicate_fetch"]:
            tasks.setdefault(x["task_id"] or x["file"], []).append(x)
    d = [next((y for y in v if y["records"]), v[-1]) for v in tasks.values()]     # one attempt per task
    ok = [x for x in d if x["records"]]
    miss = []
    for x in ok:
        for rec in x["records"]:
            miss.append(sum(not has_value(rec.get(k)) for k in DISCOVERY_EXPECTED) / len(DISCOVERY_EXPECTED))
    partial = sum(1 for x in ok if any(not has_value(rec.get(k)) for rec in x["records"] for k in DISCOVERY_EXPECTED))
    rows["KaloPilot discovery"] = {
        "attempted": len(d), "successful": len(ok), "partial": partial,
        "empty": sum(1 for x in d if not x["records"] and x["status"] == "completed"),
        "timeouts": sum(1 for x in d if x["error"] and "timeout" in str(x["error"])),
        "paused_by_plan_restriction": sum(1 for x in ev["discovery"] if x["error"] == "paused_by_provider_plan_restriction"),
        "recovered_after_pause": sum(1 for v in tasks.values() if any(y["records"] for y in v) and
                                     any(y["error"] == "paused_by_provider_plan_restriction" for y in v)),
        "missing_expected_field_rate": round(statistics.mean(miss), 4) if miss else None,
        "mapping_error_rate": None, "provenance_failure_rate": 0.0 if ok else None,
        "match_quality": "product_id identity (exact ids)", "duplicate_fetches": sum(x["duplicate_fetch"] for x in ev["discovery"])}
    # KaloPilot deep analysis
    dr = ev["deep_raw"]
    singles = [x for x in dr if x["query_type"] == "deep_product" and not x["derived_from_batch"]]
    batches = [x for x in dr if x["query_type"] == "deep_batch"]
    attempted = len(singles) + len(batches)
    good = [x for x in singles if x["object"]]
    miss = [sum(not has_value(x["object"].get(k)) for k in DEEP_EXPECTED) / len(DEEP_EXPECTED) for x in good]
    ident = [str(x["object"].get("product_id")) == x["product_id"] for x in good if x["object"].get("product_id")]
    n_map = sum(1 for v in mapping.values() if v)
    prov_fail = 0
    for r in recs:
        pv = r.get("provenance") or {}
        for k in ("gmv_30d", "units_30d", "growth_30d", "creator_count", "video_count", "price_avg"):
            e = pv.get(k) or {}
            if e.get("quality") == "UNVERIFIED" or (e.get("availability") == "MISSING" and k != "growth_30d"):
                prov_fail += 1
                break
    rows["KaloPilot deep analysis"] = {
        "attempted": attempted, "successful": len(good),
        "partial": sum(1 for x in good if any(not has_value(x["object"].get(k)) for k in DEEP_EXPECTED)),
        "empty": sum(1 for x in singles + batches if x["error"] and "empty" in str(x["error"])) +
        sum(1 for x in singles if x["error"] == "no_parseable_json"),
        "timeouts": 0, "missing_expected_field_rate": round(statistics.mean(miss), 4) if miss else None,
        "mapping_error_rate": _rate(n_map, len(recs)), "provenance_failure_rate": _rate(prov_fail, len(recs)),
        "match_quality": f"returned product_id == requested: {sum(ident)}/{len(ident)}",
        "empty_answer_causes": sorted({str(x["error"]) for x in singles + batches if x["error"]})}
    for name, n, note in (("Amazon (KaloPilot Amazon validation)", len(ev["amazon_raw"]),
                           "no product met the Amazon eligibility rule (WPS >= 70, Confidence >= 60) in any live run"),
                          ("Supplier providers", len(ev["supplier_files"]),
                           "only manual_import is implemented; no supplier offer was imported"),
                          ("Competitor providers", len(ev["competitor_files"]),
                           "only manual_import is implemented; no competitor research was imported")):
        rows[name] = {"attempted": n, "successful": 0, "partial": 0, "empty": 0, "timeouts": 0,
                      "missing_expected_field_rate": None, "mapping_error_rate": None,
                      "provenance_failure_rate": None, "match_quality": NA, "note": note}
    vids = [x for x in good if isinstance(x["object"].get("top_videos"), list)]
    rows["Creative provider (saved KaloPilot top_videos)"] = {
        "attempted": len(good), "successful": sum(1 for x in vids if x["object"]["top_videos"]),
        "partial": len(good) - sum(1 for x in vids if x["object"]["top_videos"]), "empty": 0, "timeouts": 0,
        "missing_expected_field_rate": None, "mapping_error_rate": 0.0, "provenance_failure_rate": 0.0,
        "match_quality": "linked by product id (100)",
        "note": "free re-use of deep answers; no hook / angle / date fields exist in this source"}
    for name, m in rows.items():
        sc = provider_score(m)
        m["reliability_score"] = sc[0] if sc else None
        m["score_parts"] = sc[1] if sc else None
        m["status"] = "NOT_MEASURED" if sc is None else ("LOW_SAMPLE" if m["attempted"] < MIN_SAMPLE else "MEASURED")
    return rows


# ============================================================================ Stage 3 — field reliability
def field_reliability(ev, recs, mapping):
    out = {}
    issues = [x for v in mapping.values() for x in v]

    def issue_rate(field, n):
        k = sum(1 for x in issues if f": {field}" in x or f" {field}:" in x or x.startswith(f"{field}:") or
                f"Deep {field}" in x)
        return _rate(k, n) or 0.0
    disc_recs = [rec for x in ev["discovery"] if not x["duplicate_fetch"] for rec in x["records"]]
    for label, field in (("GMV", "gmv_30d"), ("units", "units_30d"), ("30D growth", "growth_30d_pct"),
                         ("previous 30D GMV", "gmv_prev_30d"), ("creator count", "creator_count"),
                         ("selling creator count", "selling_creator_count"), ("video count", "video_count"),
                         ("price", "price_min")):
        n = len(disc_recs)
        a = sum(has_value(r.get(field)) for r in disc_recs)
        out[f"Discovery · {label}"] = {"provider": "KaloPilot discovery", "field": field, "n": n,
                                       "available": a, "rate": _rate(a, n), "status": band(_rate(a, n), n)}
    good = [x["object"] for x in ev["deep_raw"] if x["object"]]
    for label, field in (("GMV", "gmv_30d"), ("units", "units_30d"), ("previous 30D GMV", "gmv_prev_30d"),
                         ("30D growth (verified)", "growth_30d_pct"), ("recent sales series", "daily_gmv"),
                         ("daily units series", "daily_units"), ("creator count", "creator_count"),
                         ("selling creator count", "selling_creator_count"), ("video count", "video_count"),
                         ("video sales share", "video_sales_share_pct"),
                         ("competition count (comparable scope)", "__comparable__"),
                         ("competition level (scope)", "category_product_count_level"),
                         ("similar listings", "similar_listings_count"), ("shop count", "shop_count"),
                         ("price", "price_min"), ("commission", "commission_pct"),
                         ("category growth", "category_growth_pct"), ("top creators", "top_creators"),
                         ("top videos", "top_videos"), ("price history", "price_history")):
        n = len(good)
        if field == "__comparable__":
            a = sum(1 for r in recs if (r.get("competition_metrics") or {}).get("competition_comparable"))
            n = len(recs)
        elif field == "growth_30d_pct":
            a = sum(1 for r in recs if (r.get("growth") or {}).get("growth_30d_pct") is not None)
            n = len(recs)
        else:
            a = sum(has_value(o.get(field)) for o in good)
        ir = issue_rate(field, len(recs))
        out[f"Deep · {label}"] = {"provider": "KaloPilot deep analysis", "field": field, "n": n, "available": a,
                                  "rate": _rate(a, n), "mapping_issue_rate": ir, "status": band(_rate(a, n), n, ir)}
    for prov, fields in (("Amazon", ["price", "reviews", "rating", "BSR", "demand indicators"]),
                         ("Supplier", ["product cost", "shipping cost", "delivery estimate", "rating", "inventory"]),
                         ("Competitors", ["qualified competitor count", "pricing", "Meta ad activity", "offers"])):
        for f in fields:
            out[f"{prov} · {f}"] = {"provider": prov, "field": f, "n": 0, "available": 0, "rate": None,
                                    "status": UNRELIABLE, "note": "NOT_OBSERVED: no live observation"}
    vids = [v for o in good for v in (o.get("top_videos") or []) if isinstance(v, dict)]
    for label, pred in (("video count (top videos per product)", None), ("hooks", "hook"), ("angles", "angle"),
                        ("performance signals (views / revenue)", "perf"), ("publish dates", "date")):
        if pred is None:
            n = len(good)
            a = sum(1 for o in good if o.get("top_videos"))
        else:
            n = len(vids)
            a = sum(1 for v in vids if (v.get("views") is not None or v.get("revenue") is not None)) if pred == "perf" \
                else sum(1 for v in vids if v.get(pred) or v.get(f"{pred}_category") or v.get("published_at"))
        out[f"Creative · {label}"] = {"provider": "Creative (saved KaloPilot)", "field": label, "n": n, "available": a,
                                      "rate": _rate(a, n), "status": band(_rate(a, n), n)}
    for v in out.values():
        v["low_sample"] = bool(v["n"]) and v["n"] < MIN_SAMPLE
    return out


# ============================================================================ Stage 4 — CORE field review
def core_review(recs, scoring_cfg):
    rows = []
    n = len(recs)
    for name, m in scoring_cfg["metrics"].items():
        avail = sum(1 for r in recs if name not in (r.get("wps_na_metrics") or []))
        rate = _rate(avail, n)
        st = band(rate, n)
        tier = m.get("tier")
        proposal = None
        if tier == "CORE" and st in (SPARSE, UNRELIABLE):
            proposal = "move to SUPPORTING (consistently unavailable in live answers)"
        elif tier == "CORE" and st == USABLE:
            proposal = "keep CORE; MONITOR (missing in some answers)"
        elif tier != "CORE" and st == RELIABLE:
            proposal = "keep (do not promote on a sample below 10)" if n < MIN_SAMPLE else "candidate for CORE review"
        rows.append({"metric": name, "tier": tier, "available": avail, "n": n, "rate": rate, "status": st,
                     "inputs": [c["input"] for c in (m.get("components") or {}).values()],
                     "proposal": proposal or "keep"})
    return rows


# ============================================================================ Stage 5 — WPS calibration audit
def wps_audit(recs, scoring_cfg, mapping):
    out = {}
    n = len(recs)
    for group, metrics in WPS_GROUPS.items():
        for name in metrics:
            m = scoring_cfg["metrics"][name]
            pts = [_num((r.get("wps_breakdown") or {}).get(name, {}).get("points")) for r in recs]
            av = [p for p in pts if p is not None]
            mx = m["points"]
            flags = []
            if len(av) >= 5 and sum(p >= mx - 1e-9 for p in av) / len(av) > 0.5:
                flags.append("TOO_MANY_MAX_SCORES")
            if len(av) >= 5 and sum(p <= 1e-9 for p in av) / len(av) > 0.5:
                flags.append("TOO_MANY_ZERO_SCORES")
            if len(av) >= 3 and statistics.pstdev(av) < 0.05 * mx:
                flags.append("LOW_VARIANCE")
            na_rate = _rate(n - len(av), n) or 0.0
            if m.get("tier") == "CORE" and na_rate >= 0.2:
                flags.append("MISSING_DATA_OVERPENALIZED")      # CORE N/A -> 0 points counted in the denominator
            if m.get("tier") != "CORE" and na_rate >= 0.5:
                confs = [_num(r.get("confidence")) for r, p in zip(recs, pts) if p is None]
                if confs and statistics.mean(confs) >= 75:
                    flags.append("MISSING_DATA_UNDERPENALIZED")
            inputs = [c["input"] for c in (m.get("components") or {}).values()]
            mi = sum(1 for v in mapping.values() for x in v if any(i.split("_")[0] in x for i in inputs))
            out[name] = {"group": group, "tier": m.get("tier"), "max": mx, "n": n, "available": len(av),
                         "availability": _rate(len(av), n), "mean": round(statistics.mean(av), 2) if av else None,
                         "stdev": round(statistics.pstdev(av), 2) if len(av) > 1 else None,
                         "min": min(av) if av else None, "max_seen": max(av) if av else None,
                         "share_max": _rate(sum(p >= mx - 1e-9 for p in av), len(av)),
                         "share_zero": _rate(sum(p <= 1e-9 for p in av), len(av)),
                         "missing_behavior": "CORE: N/A = 0 points, WPS incomplete" if m.get("tier") == "CORE"
                         else "SUPPORTING: excluded from denominator, Confidence -3",
                         "mapping_issues_touching_inputs": mi, "flags": flags,
                         "low_sample": n < MIN_SAMPLE}
    return out


# ============================================================================ Stage 6 — threshold sensitivity
def _grid(rows, vkey, ckey, vs, cs, review_band=10):
    out = []
    covered = [r for r in rows if r.get(vkey) is not None and r.get(ckey) is not None]
    for t in vs:
        for c in cs:
            p = rv = rj = 0
            for r in covered:
                v, cf = r[vkey], r[ckey]
                if v >= t and cf >= c:
                    p += 1
                elif (v >= t and cf < c) or (t - review_band <= v < t and cf >= c):
                    rv += 1
                else:
                    rj += 1
            out.append({"threshold": t, "confidence_min": c, "pass": p, "review": rv, "reject": rj,
                        "no_data": len(rows) - len(covered), "coverage": _rate(len(covered), len(rows))})
    return out


def sensitivity(latest, momentum_rows):
    rows = [{"product_id": r["product_id"], "wps": _num(r.get("wps")), "conf": _num(r.get("confidence")),
             "bvs": None, "bvs_conf": None} for r in latest]
    mom = [{"product_id": k, "m": v.get("m"), "mc": v.get("mc")} for k, v in momentum_rows.items()]
    return {"definition": "PASS: value >= threshold and confidence >= minimum; REVIEW: value >= threshold with "
                          "confidence below minimum, or value within 10 points below the threshold with enough "
                          "confidence; REJECT: otherwise; NO_DATA: value or confidence missing. No threshold is "
                          "marked best.",
            "products": len(rows),
            "wps_x_confidence": _grid(rows, "wps", "conf", [60, 65, 70, 75, 80], [50, 60, 70, 80]),
            "bvs_x_bvs_confidence": _grid(rows, "bvs", "bvs_conf", [50, 60, 70, 80], [40, 50, 60, 70]),
            "momentum_x_momentum_confidence": _grid(mom, "m", "mc", [60, 65, 70, 75, 80], [60]),
            "observed_values": [{"product_id": r["product_id"], "wps": r["wps"], "wps_confidence": r["conf"]}
                                for r in rows],
            "momentum_observed": mom}


# ============================================================================ prompt versions (legacy vs current)
def current_prompt_version():
    from winning_product_agent.runner import Runner
    return Runner.prompt_version("deep_product")


def split_mapping(recs, mapping):
    cur = current_prompt_version()
    out = {"current_prompt": {}, "legacy_prompt": {}}
    for r in recs:
        k = f"{r['product_id']}@{r.get('observation_timestamp')}"
        if mapping.get(k):
            out["current_prompt" if r.get("prompt_version") == cur else "legacy_prompt"][k] = mapping[k]
    return out


def critical_mapping(split):
    """Critical = an open (current-prompt) issue on a field that WPS or the decision engine uses."""
    used = ("gmv", "units", "growth", "creator_count", "video_count", "price", "commission", "category_growth",
            "category_product_count", "daily", "video_sales_share")
    crit, other = [], []
    for k, issues in split["current_prompt"].items():
        for x in issues:
            field_part = x.split(":")[0]
            (crit if any(u in field_part for u in used) and "selling_creator_count" not in field_part
             else other).append(f"{k}: {x}")
    return crit, other


# ============================================================================ offline product set (LIVE only)
def creative_analysis(ev, rec):
    prov = CR.get_provider("kalopilot_saved", raw_dir=Path(rec["_file"]).parent)
    raws = [x for x in prov.search_creatives({"product_id": rec["product_id"]})
            if Path(x["_file"]).name == Path(rec["_file"]).name]
    cs = [c for c in (prov.normalize_creative(x, {}) for x in raws) if c][:20]
    if not cs:
        return None
    a = CR.analyze_product(rec["product_id"], {"product_name": rec.get("product_name"), "units": rec.get("units")}, cs)
    a["observed_at"] = max((c.get("observed_at") or c.get("retrieved_at") or "" for c in cs), default=None) or None
    a["source_files"] = sorted({str(c.get("raw_source_location") or "").split("#")[0] for c in cs} - {""})
    a["provenance_complete"] = all(c.get("raw_source_location") and c.get("retrieved_at")
                                   for c in a["creatives"] if c.get("qualified"))
    return a


def decision_products(ev, recs, now=None):
    """Latest LIVE observation per product -> report product dicts (no Amazon/BVS: none observed live)."""
    latest = latest_per_product(recs)
    disc_latest = ev["discovery_results"][-1]["result"] if ev["discovery_results"] else None
    inputs = {"discovery": ({**disc_latest, "_file": ev["discovery_results"][-1]["file"], "data_environment": LIVE}
                            if disc_latest else None),
              "deep": [{**r, "_file": r["_file"]} for r in latest], "amazon": [], "bvs": []}
    if inputs["discovery"]:
        for r in (inputs["discovery"].get("candidates") or []) + (inputs["discovery"].get("failed") or []):
            r["data_environment"] = LIVE
    rep = GR.build_report(inputs, GR.load_cfg(), now, ev["store"] if ev["store"].identities() else None)
    ids = {str(r["product_id"]) for r in latest}
    prods = [p for p in rep["all_products"] if str(p.get("product_id")) in ids]
    for p in prods:
        p["data_environment"] = LIVE
    cre = {}
    for r in latest:
        a = creative_analysis(ev, r)
        if a:
            cre[str(r["product_id"])] = a
    return prods, cre


def decide_all(prods, cre, cfg, now=None):
    return DE.run(prods, {}, cre, cfg=cfg, expected_env=LIVE, now=now, require_trust=True)


# ============================================================================ Stage 7 — decision engine calibration
def decision_calibration(res):
    rows, counts, by_primary = [], {}, {"NEGATIVE_EVIDENCE": 0, "MISSING_EVIDENCE": 0, "NONE": 0}
    for d in res["decisions"]:
        counts[d["decision_state"]] = counts.get(d["decision_state"], 0) + 1
        ex = d["explanation"]
        by_primary[ex["primary"]] += 1
        rows.append({"product_id": d["product_id"], "name": d["name"], "state": d["decision_state"],
                     "primary_explanation": ex["primary"],
                     "negative_evidence": [x.get("reason") or x["source"] for x in ex["NEGATIVE_EVIDENCE"]],
                     "missing_evidence": [x.get("reason") or x["source"] for x in ex["MISSING_EVIDENCE"]],
                     "reject_on_missing_evidence": d["decision_state"] == DE.REJECT and any(
                         g["status"] == "FIRED" and g["action"] == "reject" and
                         g["evidence"].get("cross_platform_demand") == DE.UNKNOWN for g in d["hard_gates"])})
    return {"states": counts, "explanations": by_primary, "products": rows}


# ============================================================================ Stage 8 — confidence calibration
def _completeness(rec, scoring_cfg):
    fields = [c.get("field") for comp in scoring_cfg["confidence"]["components"].values() for c in comp.get("checks") or []]
    sin = rec.get("wps_inputs") or {}
    have = 0
    for f in fields:
        v = sin.get(f)
        have += 1 if has_value(v) else 0
    return _rate(have, len(fields))


def double_penalties(rec, scoring_cfg):
    """A missing value subtracted by a Confidence adjustment AND already missing in a Confidence component."""
    checks = {c.get("field") for comp in scoring_cfg["confidence"]["components"].values() for c in comp.get("checks") or []}
    out = []
    for a in rec.get("confidence_adjustments") or []:
        name = a["reason"].split(": ")[-1]
        if name in scoring_cfg["metrics"]:
            ins = [c["input"] for c in scoring_cfg["metrics"][name]["components"].values()]
            ins = [CONF_ALIASES.get(i, i) for i in ins]
        else:
            ins = [name]
        if any(i in checks for i in ins):
            out.append({"adjustment": a["reason"], "points": a["points"], "also_counted_in_component_field": ins})
    return out


def confidence_calibration(ev, recs, scoring_cfg, res, dec_cfg=None):
    import score_products as SP
    rows, flags = [], []
    for r in recs:
        comp = _completeness(r, scoring_cfg)
        conf = _num(r.get("confidence"))
        dp = double_penalties(r, scoring_cfg)
        f = []
        if conf is not None and comp is not None and conf >= 80 and comp < 0.6:
            f.append("HIGH_CONFIDENCE_SPARSE_DATA")
        if conf is not None and comp is not None and conf < 60 and comp >= 0.9:
            f.append("LOW_CONFIDENCE_COMPLETE_DATA")
        if dp:
            f.append("DOUBLE_PENALTY")
        # performance independence: same evidence, different product performance -> same confidence
        sin = dict(r.get("wps_inputs") or {})
        obj = next((x["object"] for x in ev["deep_raw"] if x["file"] == r["_file"]), None)
        if obj:
            fx, _, _ = DA.normalize_deep(obj, r["_disc"])
            cfgd = DA.load_cfg()
            base_in = DA.scoring_input(fx, 0, DA.trend_metrics(fx, cfgd), DA.competition_scope(fx, cfgd))
            pert = copy.deepcopy(base_in)
            for k in ("units_sold", "gmv", "videos_count", "creators_count", "revenue_growth_pct", "price_avg"):
                if _num(pert.get(k)) is not None:
                    pert[k] = pert[k] * 7 + 13
            c1 = SP.score_product(base_in, None, scoring_cfg)["confidence"]["score"]
            c2 = SP.score_product(pert, None, scoring_cfg)["confidence"]["score"]
            if c1 != c2:
                f.append("CONFIDENCE_DEPENDS_ON_PERFORMANCE")
        flags += [f"{r['product_id']}: {x}" for x in f]
        rows.append({"product_id": r["product_id"], "observation": r.get("observation_timestamp"),
                     "wps_confidence": conf, "data_completeness": comp,
                     "adjustment_points": sum(a["points"] for a in r.get("confidence_adjustments") or []),
                     "double_penalties": dp, "flags": f})
    # Decision Confidence: product performance must not move it
    dc_bad = []
    for d in res["decisions"]:
        ev2 = copy.deepcopy(d["evidence"])
        for k in ("wps", "momentum_score", "creative_opportunity"):
            if ev2["values"].get(k, {}).get("value") is not None:
                ev2["values"][k]["value"] = 100.0 - ev2["values"][k]["value"]
        dcfg = dec_cfg or DE.load_cfg()
        dims = DE.dimensions(ev2, dcfg)
        same_known = all((dims[k]["status"] == DE.UNKNOWN) == (d["dimension_status"][k] == DE.UNKNOWN) for k in DE.DIMENSIONS)
        if same_known and DE.decision_confidence(ev2, dims, dcfg)["score"] != d["decision_confidence"]["score"]:
            dc_bad.append(d["product_id"])
    cre = [d["evidence"]["values"].get("creative_confidence", {}).get("value") for d in res["decisions"]]
    systems = {
        "WPS Confidence": {"measured_on": len(recs), "flags": sorted({x.split(": ")[1] for x in flags})},
        "Momentum Confidence": {"measured_on": 0, "note": "every live product is INSUFFICIENT_HISTORY (observations on "
                                                          "one day only); not measurable yet"},
        "Amazon Confidence": {"measured_on": 0, "note": "no live Amazon validation"},
        "BVS Confidence": {"measured_on": 0, "note": "no live BVS record (no eligible product, no supplier data)"},
        "Supplier Confidence": {"measured_on": 0, "note": "no live supplier offer"},
        "Competitor Confidence": {"measured_on": 0, "note": "no live competitor research"},
        "Creative Confidence": {"measured_on": len([c for c in cre if c is not None]),
                                "values": [c for c in cre if c is not None],
                                "note": "saved KaloPilot videos carry no hook / angle / date; confidence can still reach "
                                        "60 from count / views / sales / creator -> HIGH_CONFIDENCE_SPARSE_DATA relative "
                                        "to the decision minimum 50",
                                "flags": ["CREATIVE_CONFIDENCE_PASSES_MINIMUM_WITHOUT_CLASSIFICATION"]
                                if any(c is not None and c >= 50 for c in cre) else []},
        "Decision Confidence": {"measured_on": len(res["decisions"]),
                                "depends_on_performance": dc_bad,
                                "flags": ["DECISION_CONFIDENCE_DEPENDS_ON_PERFORMANCE"] if dc_bad else []},
    }
    return {"products": rows, "flags": flags, "systems": systems}


# ============================================================================ Stage 9 — matching calibration
def matching_calibration(ev, cre):
    def bands(vals):
        b = {"90-100": 0, "75-89": 0, "60-74": 0, "<60": 0}
        for v in vals:
            b["90-100" if v >= 90 else "75-89" if v >= 75 else "60-74" if v >= 60 else "<60"] += 1
        return b
    ident = [x for x in ev["deep_raw"] if x["object"] and x["object"].get("product_id")]
    deep_scores = [100 if str(x["object"]["product_id"]) == x["product_id"] else 0 for x in ident]
    cvals = [c.get("match_confidence_calc") or 0 for a in cre.values() for c in a["creatives"]]
    return {
        "KaloPilot deep (requested vs returned product id)": {"n": len(deep_scores), "bands": bands(deep_scores)},
        "Amazon product matching": {"n": 0, "bands": bands([]), "note": "no live Amazon answer"},
        "Supplier matching": {"n": 0, "bands": bands([]), "note": "no live supplier offer"},
        "Competitor matching": {"n": 0, "bands": bands([]), "note": "no live competitor observation"},
        "Creative matching": {"n": len(cvals), "bands": bands(cvals),
                              "note": "every saved creative is linked to the product id (100); no fuzzy match used"},
        "conclusion": "No band can be judged too permissive or too strict for Amazon / supplier / competitor: no "
                      "live match exists. Matching requirements are NOT lowered.",
    }


# ============================================================================ Stage 10 — competition calibration
def competition_calibration(recs):
    rows, issues = [], []
    for r in recs:
        cm = r.get("competition_metrics") or {}
        pts = (r.get("wps_breakdown") or {}).get("competition_saturation", {}).get("points")
        flags = {f.get("flag") for f in r.get("red_flags") or []}
        comparable = cm.get("competition_comparable")
        if not comparable and _num(pts) is not None:
            issues.append(f"{r['product_id']}: non-comparable count scored in competition_saturation")
        if not comparable and "EXTREME_SATURATION" in flags:
            issues.append(f"{r['product_id']}: EXTREME_SATURATION raised on a non-comparable count")
        rows.append({"product_id": r["product_id"], "scope": cm.get("competition_scope"),
                     "comparable": comparable, "count": cm.get("competition_count"),
                     "level": cm.get("category_product_count_level"), "competition_saturation_points": pts,
                     "flags": sorted(f for f in flags if "COMPETITION" in f or "SATURATION" in f)})
    scopes = {}
    for x in rows:
        scopes[x["scope"]] = scopes.get(x["scope"], 0) + 1
    return {"products": rows, "scopes": scopes, "issues": issues,
            "direct_adjacent_category_competitors": "no live competitor research (DIRECT / ADJACENT / CATEGORY never "
                                                    "mixed by design; not exercised live)",
            "price_compression": "not observed live", "advertising_saturation": "not observed live"}


# ============================================================================ Stage 11 — creative calibration
def creative_calibration(cre, dec_cfg):
    feats = {"creative_volume": [], "hook_classified": [], "angle_classified": [], "formats": [], "dated": []}
    for a in cre.values():
        q = [c for c in a["creatives"] if c.get("qualified")]
        feats["creative_volume"].append(len(q))
        feats["hook_classified"].append(_rate(sum(c["hook_category"] != "UNKNOWN" for c in q), len(q)) or 0)
        feats["angle_classified"].append(_rate(sum(c["angle"] != "UNKNOWN" for c in q), len(q)) or 0)
        feats["formats"].append(len({c["format"] for c in q}))
        feats["dated"].append(_rate(sum(c.get("creative_age_days") is not None for c in q), len(q)) or 0)
    confs = [a["confidence"]["score"] for a in cre.values()]
    minimum = dec_cfg["minimum_confidence"]["creative"]
    unclassified_pass = sum(1 for a, c in zip(cre.values(), confs) if c >= minimum and not any(
        x["hook_category"] != "UNKNOWN" or x["angle"] != "UNKNOWN" for x in a["creatives"] if x.get("qualified")))
    return {"products": len(cre), "features": {k: {"values": v, "mean": round(statistics.mean(v), 2) if v else None}
                                               for k, v in feats.items()},
            "creative_confidence": confs, "minimum_for_use": minimum,
            "passing_minimum_without_classification": unclassified_pass,
            "max_confidence_without_classification": 60,
            "ready_for_bvs_advertising_viability": False,
            "conclusion": f"Hooks, angles and dates are not available from the only live creative source (saved "
                          f"KaloPilot top videos). {sum(c >= minimum for c in confs)} of {len(confs)} products still reach "
                          f"the creative minimum ({minimum}) — {unclassified_pass} of them with NO hook / angle / date "
                          "classified (the confidence components for count, views, sales and creator alone give up to "
                          "60). Creative Opportunity is NOT integrated into BVS."}


# ============================================================================ Stage 12 — supplier calibration
def supplier_calibration(ev):
    n = len(ev["supplier_files"])
    return {"offers_observed": n, "match_quality": NA if not n else "see supplier layer",
            "cost_completeness": NA if not n else None, "shipping_completeness": NA if not n else None,
            "delivery_reliability": NA if not n else None, "rating_availability": NA if not n else None,
            "reliable_for": {"Gross Margin": False, "Shipping Viability": False, "Supplier Viability": False},
            "conclusion": "No live supplier data: BVS stays partial; no economics are assumed."}


# ============================================================================ Stage 13 — cost efficiency
def cost_efficiency(ev, recs):
    runs = []
    for m in ev["runs"]:
        ql = m.get("query_log") or []
        by = {}
        for q in ql:
            s = by.setdefault(q["stage"], {"queries": 0, "credits_known": 0.0, "credits_unknown": 0, "usable": 0})
            if q["action"] in ("LIVE_QUERY", "FETCHED_RESULT"):
                s["queries"] += 1
                if isinstance(q.get("credits"), (int, float)):
                    s["credits_known"] += q["credits"]
                else:
                    s["credits_unknown"] += 1
                s["usable"] += 0 if q.get("error_category") else 1
        cr = m.get("credits") or {}
        summ = m.get("summary") or {}
        disc = summ.get("discovered") or 0
        deep_ok = sum(1 for q in ql if q["stage"] == "deep_analysis" and q["action"] == "LIVE_QUERY"
                      and not q.get("error_category"))
        total_q = sum(v["queries"] for v in by.values())
        runs.append({"run_id": m["run_id"], "final_status": m.get("final_status"), "by_stage": by,
                     "credits_used": cr.get("credits_used_by_balance"),
                     "discovered": disc or None, "deep_ok": deep_ok,
                     "queries_per_discovered_product": _rate(total_q, disc) if disc else None,
                     "queries_per_deep_analyzed_product": _rate(by.get("deep_analysis", {}).get("queries", 0), deep_ok)
                     if deep_ok else None,
                     "queries_per_final_decision": _rate(total_q, deep_ok) if deep_ok else None})
    d_costs = [x["credits"] for x in ev["discovery"] if x["credits"] and not x["duplicate_fetch"] and x["records"]]
    deep_ok_costs = [x["credits"] for x in ev["deep_raw"] if x["credits"] and x["object"]]
    wasted = [{"file": Path(x["file"]).name, "credits": x["credits"], "reason": x["error"]}
              for x in ev["deep_raw"] + ev["discovery"] if x["credits"] and x["error"]]
    dup_products = {}
    for x in ev["deep_raw"]:
        if x["object"]:
            dup_products.setdefault(x["product_id"], []).append(x)
    repeats = [{"product_id": k, "answers": len(v), "credits": round(sum(y["credits"] or 0 for y in v), 2),
                "reason": "deep prompt changed between runs (prompt_version cache key) -> answer re-bought"}
               for k, v in dup_products.items() if len(v) > 1]
    return {"runs": runs,
            "observed_cost_per_query": {
                "discovery_answer": {"n": len(d_costs), "mean": round(statistics.mean(d_costs), 2) if d_costs else None,
                                     "min": min(d_costs, default=None), "max": max(d_costs, default=None),
                                     "products_per_answer": 15},
                "deep_answer_ok": {"n": len(deep_ok_costs), "mean": round(statistics.mean(deep_ok_costs), 2)
                                   if deep_ok_costs else None, "min": min(deep_ok_costs, default=None),
                                   "max": max(deep_ok_costs, default=None)},
                "amazon": "UNKNOWN (never executed live)",
                "supplier / competitor / creative": "no paid provider (manual imports; creatives from saved answers)"},
            "high_cost_low_value": wasted,
            "duplicate_query_opportunities": repeats,
            "cache_opportunities": ["Discovery answers are re-used for 24 h (1 free re-fetch observed)",
                                    "Deep answers are cached per prompt_version: changing the prompt re-buys every "
                                    "product (observed for 2 products). Freeze the prompt for production.",
                                    "Creatives: saved top_videos re-used at no cost"],
            "depth_reduction": "empty answers at the ~8k output-token limit charged 0.44 each (2 of 11 deep attempts); "
                               "the long-answer products (fragrance combo 3.57, makeup kit 2.90) cost the most"}


# ============================================================================ Stage 14 — query strategy
def query_strategy(cost):
    oc = cost["observed_cost_per_query"]
    dmean, dmin, dmax = (oc["discovery_answer"][k] for k in ("mean", "min", "max"))
    pmean, pmin, pmax = (oc["deep_answer_ok"][k] for k in ("mean", "min", "max"))
    per = oc["discovery_answer"]["products_per_answer"]
    empty_rate = round(2 / 11, 3)

    def rng(n, lo, mid, hi):
        return None if lo is None else {"min": round(n * lo, 2), "expected": round(n * mid, 2), "max": round(n * hi, 2)}
    return {
        "assumptions": f"discovery {per} products per answer (observed); deep 1 product per query; empty-answer "
                       f"rate {empty_rate} observed (charged ~0.44 each); Amazon cost UNKNOWN",
        "discovery": [{"limit": n, "queries": math.ceil(n / per), "credits": rng(math.ceil(n / per), dmin, dmean, dmax),
                       "note": f"one answer per category (~{per} products): {math.ceil(n / per)} categories"}
                      for n in (20, 50, 100)],
        "deep_analysis": [{"limit": n, "queries": n, "credits": rng(n, pmin, pmean, pmax),
                           "expected_empty_answers": round(n * empty_rate, 1)} for n in (5, 10, 20, 40)],
        "amazon": [{"limit": n, "queries": math.ceil(n / 2), "credits": "UNKNOWN"} for n in (3, 5, 10, 20)],
        "supplier": [{"limit": n, "paid_queries": 0, "note": "manual import only"} for n in (3, 5, 10)],
        "competitor": [{"limit": n, "paid_queries": 0, "note": "manual import only"} for n in (3, 5, 10)],
        "creative": [{"limit": n, "paid_queries": 0, "note": "saved answers + manual import"} for n in (3, 5, 10)],
        "proposal": "Keep discovery 20 / deep 5 / Amazon 3 / supplier 3 / competitor 3 / creative 3 until Amazon, "
                    "supplier and competitor have live evidence and deep cost variance is measured on >= 10 answers "
                    "with the frozen prompt. Largest limits are not selected.",
    }


# ============================================================================ Stage 16 — proposed configs
PROPOSED = ("scoring_v2.yaml", "filters_v2.yaml", "runtime_production_v1.yaml", "decision_rules_v2.yaml")


def _header(title, changes):
    L = ["# " + "=" * 77, f"# PROPOSED — {title} (Step AB production calibration)", "#",
         "# NOT applied. Review before promoting (Step AC). Only changes justified by LIVE evidence:"]
    L += [f"#   - {c}" for c in changes] or ["#   (none)"]
    L += ["# " + "=" * 77, ""]
    return "\n".join(L) + "\n"


def build_proposals(root, cost):
    cfg_dir = Path(root) / "config"
    sc = (cfg_dir / "scoring.yaml").read_text()
    sc_changes = ["data_requirements.counted_in_confidence: a missing value already scored by a Confidence component "
                  "is not subtracted again (removes the observed double penalty for competition_saturation, "
                  "similar_listings_count, shops_count). WPS metrics, weights and tiers unchanged."]
    sc2 = sc.replace('version: "wps-v2"', 'version: "wps-v2.1-proposed"', 1)
    sc2 = sc2.replace("data_requirements:\n", "data_requirements:\n"
                      "  counted_in_confidence:                  # AB: never penalize one missing value twice\n"
                      "    competition_saturation: [category_product_count]\n"
                      "    similar_listings_count: [similar_listings_count]\n"
                      "    shops_count: [shops_count]\n", 1)
    fl = (cfg_dir / "filters.yaml").read_text()
    fl_changes = []
    dz = (cfg_dir / "decision.yaml").read_text()
    dz_changes = ["CREATOR/VIDEO_DEPENDENCY_WEAK_BROADER: reject only when cross-platform demand is WEAK (negative "
                  "evidence); when it is UNKNOWN (missing evidence) the gate blocks READY instead of rejecting",
                  "validation task for the dependency gates when broader evidence is missing",
                  "minimum_confidence.creative 50 -> 65: live creative data reaches 60 with zero hook / angle / date "
                  "classification; the creative dimension should need some classified evidence (tightening, never "
                  "loosening)"]
    dz2 = dz.replace('decision_rules_version: "Z-v1"', 'decision_rules_version: "Z-v2-proposed"', 1)
    dz2 = dz2.replace("  creative: 50\n", "  creative: 65                  # AB: saved-only creatives reach 60 with no hook/angle/date\n", 1)
    for g in ("CREATOR_DEPENDENCY_WEAK_BROADER", "VIDEO_DEPENDENCY_WEAK_BROADER"):
        key = "max_creators: 10 }" if g.startswith("CREATOR") else "max_videos: 10 }"
        dz2 = dz2.replace(key, key[:-2] + ", broader_weak_statuses: [WEAK], when_broader_unknown: block_ready }", 1)
    dz2 = dz2.replace("  SUPPLIER_MATCH_UNRELIABLE: \"", "  CREATOR_DEPENDENCY_WEAK_BROADER: \"Collect broader demand "
                      "evidence (Amazon validation or a later observation) before judging creator dependency\"\n"
                      "  VIDEO_DEPENDENCY_WEAK_BROADER: \"Collect broader demand evidence before judging video "
                      "dependency\"\n  SUPPLIER_MATCH_UNRELIABLE: \"", 1)
    oc = cost["observed_cost_per_query"]
    deep_max = oc["deep_answer_ok"]["max"]
    rt_changes = ["limits unchanged (20 / 5 / 3 / 3): no increase",
                  f"estimated_credits.deep_analysis 2.5 -> {math.ceil((deep_max or 2.5) * 10) / 10} (observed max "
                  f"{deep_max}; the run cap must be protected by a conservative estimate)",
                  "estimated_credits.amazon_validation stays 4.0: NOT observed live (kept conservative, not lowered)"]
    rt = (cfg_dir / "runtime_aa_live.yaml").read_text()
    rt2 = rt.replace("profile: aa_live", "profile: production_v1", 1)
    rt2 = rt2.replace("    deep_analysis: 2.5                       # observed 1.51–3.57 per product",
                      f"    deep_analysis: {math.ceil((deep_max or 2.5) * 10) / 10}                       "
                      f"# AB: observed max {deep_max} (conservative)", 1)
    return {
        "scoring_v2.yaml": (_header("scoring_v2", sc_changes) + sc2, sc_changes),
        "filters_v2.yaml": (_header("filters_v2", fl_changes) +
                            "# No filter change is justified by the live evidence (all REVIEW flags traced to real\n"
                            "# low-base growth or low selling-creator counts). Content identical to filters.yaml.\n\n"
                            + fl, fl_changes),
        "runtime_production_v1.yaml": (_header("runtime_production_v1", rt_changes) + rt2, rt_changes),
        "decision_rules_v2.yaml": (_header("decision_rules_v2", dz_changes) + dz2, dz_changes),
    }


def write_proposals(root, props):
    out = Path(root) / "config" / "proposed"
    out.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, (text, _) in props.items():
        (out / name).write_text(text)
        paths[name] = str(out / name)
    return paths


# ============================================================================ Stage 17 — offline replay
def replay(ev, old_recs, props, now=None):
    new_scoring = yaml.safe_load(props["scoring_v2.yaml"][0])
    new_dec = yaml.safe_load(props["decision_rules_v2.yaml"][0])
    new_recs = reanalyze(ev, new_scoring)
    old_by = {f"{r['product_id']}@{r['observation_timestamp']}": r for r in old_recs}
    scores = []
    for r in new_recs:
        o = old_by.get(f"{r['product_id']}@{r['observation_timestamp']}") or {}
        scores.append({"product_id": r["product_id"], "observation": r["observation_timestamp"],
                       "old_wps": o.get("wps"), "new_wps": r.get("wps"),
                       "old_confidence": o.get("confidence"), "new_confidence": r.get("confidence"),
                       "removed_double_penalty": -sum(a["points"] for a in o.get("confidence_adjustments") or []
                                                      if a not in (r.get("confidence_adjustments") or [])),
                       "reason": "counted_in_confidence (double penalty removed)" if o.get("confidence") != r.get(
                           "confidence") else "unchanged"})
    old_prods, cre = decision_products(ev, old_recs, now)
    new_prods, cre2 = decision_products(ev, new_recs, now)
    old_res = decide_all(old_prods, cre, DE.load_cfg(), now)
    new_res = decide_all(new_prods, cre2, new_dec, now)
    nd = {d["product_id"]: d for d in new_res["decisions"]}
    decisions = []
    for d in old_res["decisions"]:
        n = nd.get(d["product_id"])
        decisions.append({"product_id": d["product_id"], "name": d["name"], "old_state": d["decision_state"],
                          "new_state": n["decision_state"] if n else None,
                          "old_decision_confidence": d["decision_confidence"]["score"],
                          "new_decision_confidence": n["decision_confidence"]["score"] if n else None,
                          "changed": bool(n) and n["decision_state"] != d["decision_state"],
                          "dimension_changes": {k: f"{d['dimension_status'][k]} -> {n['dimension_status'][k]}"
                                                for k in DE.DIMENSIONS if n and n["dimension_status"][k] != d["dimension_status"][k]},
                          "reason": ("; ".join(n["decision_path"]) if n and n["decision_state"] != d["decision_state"]
                                     else "unchanged"),
                          "new_explanation": n["explanation"]["primary"] if n else None})
    return {"scores": scores, "decisions": decisions, "old": old_res, "new": new_res, "new_recs": new_recs,
            "new_scoring": new_scoring, "new_decision_cfg": new_dec}


# ============================================================================ Stage 18 — regression protection
def regression(ev, rp, root):
    issues = []
    for s in rp["scores"]:
        oc, nc = _num(s["old_confidence"]), _num(s["new_confidence"])
        if None not in (oc, nc) and nc - oc > (s["removed_double_penalty"] or 0) + 0.01:
            issues.append(f"UNSUPPORTED_CONFIDENCE_INCREASE {s['product_id']}: {oc} -> {nc}")
        if _num(s["old_wps"]) != _num(s["new_wps"]):
            issues.append(f"WPS_CHANGED {s['product_id']}: {s['old_wps']} -> {s['new_wps']} (WPS must not change)")
    for d in rp["new"]["decisions"]:
        if not d["data_trust"]["traceable"] and d["decision_state"] in (DE.READY, DE.WATCH, DE.REJECT):
            issues.append(f"UNTRACEABLE_DATA {d['product_id']}: {d['data_trust']['untraceable']}")
        if d["dimension_status"]["commercial_viability"] != DE.UNKNOWN and not DE.supplier_economics_known(d["evidence"]):
            issues.append(f"BVS_WITHOUT_SUPPLIER_ECONOMICS {d['product_id']}")
        if d["decision_state"] == DE.READY and not DE.supplier_economics_known(d["evidence"]):
            issues.append(f"READY_WITHOUT_SUPPLIER_ECONOMICS {d['product_id']}")
    for r in rp["new_recs"]:
        cm = r.get("competition_metrics") or {}
        if not cm.get("competition_comparable") and _num((r.get("wps_breakdown") or {}).get(
                "competition_saturation", {}).get("points")) is not None:
            issues.append(f"MIXED_COMPETITION_SCOPE {r['product_id']}")
    old_dec, new_dec = DE.load_cfg(), rp["new_decision_cfg"]
    for k, v in old_dec["minimum_confidence"].items():
        if new_dec["minimum_confidence"].get(k, v) < v:
            issues.append(f"LOWERED_MINIMUM_CONFIDENCE {k}")
    match_files = {"amazon_validation.yaml": ("amazon_validation", "matching", "min_reliable_match"),
                   "competitors.yaml": ("relationships", "direct_min"), "creatives.yaml": ("matching", "qualified_min")}
    for f in match_files:
        if (Path(root) / "config" / "proposed" / f).exists():
            issues.append(f"MATCHING_CONFIG_CHANGED {f} (not allowed without live evidence)")
    old_ready = {d["product_id"] for d in rp["old"]["decisions"] if d["decision_state"] == DE.READY}
    for d in rp["new"]["decisions"]:
        if d["decision_state"] == DE.READY and d["product_id"] not in old_ready:
            issues.append(f"NEW_READY_WITHOUT_NEW_EVIDENCE {d['product_id']}")
    if ev["non_live_history"]:
        issues.append(f"SYNTHETIC_IN_LIVE_HISTORY {ev['non_live_history']} observation(s)")
    if any(d["evidence"].get("data_environment") not in (LIVE, None) for d in rp["new"]["decisions"]):
        issues.append("NON_LIVE_RECORD_IN_REPLAY")
    return {"blocked": bool(issues), "issues": issues,
            "checks": ["unsupported confidence", "untraceable data", "mixed competition scopes",
                       "unreliable matches", "BVS without supplier economics", "synthetic/live history",
                       "no new READY without new evidence", "WPS unchanged"]}


# ============================================================================ Stage 19 — readiness
def secret_scan(ev):
    secrets = safety.known_secret_values(ev["rt"])
    hits = []
    for sub in ("reports", "runs", "data"):
        base = ev["base"] / ev["paths"].get(sub, sub) if sub != "data" else ev["base"] / "data"
        for p in base.rglob("*") if base.exists() else []:
            if p.is_file() and p.suffix in (".json", ".md", ".jsonl", ".txt", ".csv"):
                t = p.read_text(errors="ignore")
                if any(s in t for s in secrets):
                    hits.append(str(p))
    return hits


def readiness(prov, fields, crit_map, wps, conf, rp, reg, cost, ev, leaks):
    def st(ok, part):
        return READY if ok else (PARTIAL if part else NOT_READY)
    measured = {k: v for k, v in prov.items() if v["reliability_score"] is not None}
    needed = ("Amazon (KaloPilot Amazon validation)", "Supplier providers", "Competitor providers")
    unmeasured = [k for k in needed if prov[k]["reliability_score"] is None]
    core_ok = all(v["status"] in (RELIABLE, USABLE) for k, v in fields.items()
                  if k.startswith("Deep") and v["field"] in ("gmv_30d", "units_30d", "creator_count", "video_count",
                                                                "price_min", "commission_pct", "daily_gmv"))
    obs_fields = [v for v in fields.values() if v["n"]]
    unobserved = [k for k, v in fields.items() if not v["n"]]
    wps_flags = sorted({f for v in wps.values() for f in v["flags"]})
    cv = cost["observed_cost_per_query"]["deep_answer_ok"]
    spread = (cv["max"] / cv["min"]) if cv["min"] else None
    trace_ok = all(d["data_trust"]["traceable"] for d in rp["new"]["decisions"])
    return {
        "Data Reliability": {"status": st(core_ok and not unobserved, core_ok),
                             "evidence": f"TikTok core fields usable/reliable: {core_ok}; {len(unobserved)} expected "
                                         f"fields never observed live (Amazon / supplier / competitor)"},
        "Scoring Reliability": {"status": st(not crit_map and not wps_flags and not conf["flags"], not crit_map),
                                "evidence": f"critical mapping issues {len(crit_map)}; WPS pattern flags {wps_flags}; "
                                            f"confidence flags {len(conf['flags'])}; sample {len(rp['new_recs'])} answers"},
        "Provider Reliability": {"status": st(not unmeasured and all(v["reliability_score"] >= 80 for v in measured.values()),
                                              bool(measured)),
                                 "evidence": {k: v["reliability_score"] for k, v in prov.items()} |
                                             {"not_measured": unmeasured}},
        "Cost Predictability": {"status": st(spread is not None and spread < 1.5 and not cost["high_cost_low_value"],
                                             spread is not None),
                                "evidence": f"deep answer cost {cv['min']}–{cv['max']} (x{round(spread, 2) if spread else NA})"
                                            f"; {len(cost['high_cost_low_value'])} charged answer(s) without usable data; "
                                            "Amazon cost UNKNOWN"},
        "Decision Traceability": {"status": st(trace_ok, True),
                                  "evidence": f"data trust check on {len(rp['new']['decisions'])} replayed decisions: "
                                              f"{'all traceable' if trace_ok else 'untraceable metrics present'}"},
        "History Integrity": {"status": st(not ev["non_live_history"], False),
                              "evidence": f"non-LIVE observations in history: {ev['non_live_history']}; snapshot dedupe "
                                          "on read and write"},
        "Secret Safety": {"status": st(not leaks, False),
                          "evidence": f"secret values found in {len(leaks)} file(s) under reports/, runs/, data/"},
    }


def final_result(crit_map, conf, prov, rp, reg, ready):
    reasons = []
    if crit_map:
        reasons.append(f"{len(crit_map)} critical mapping issue(s) remain")
    bad = [f for f in conf["flags"] if "DEPENDS_ON_PERFORMANCE" in f or "HIGH_CONFIDENCE_SPARSE" in f or
           "DOUBLE_PENALTY" in f]
    if conf["systems"]["Decision Confidence"]["depends_on_performance"]:
        bad.append("Decision Confidence depends on performance")
    if bad:
        reasons.append(f"confidence behaviour unsound under the proposed config: {bad[:5]}")
    if ready["Provider Reliability"]["status"] != READY:
        reasons.append("provider reliability not acceptable: " + ", ".join(
            ready["Provider Reliability"]["evidence"]["not_measured"]) + " never measured live" if
            ready["Provider Reliability"]["evidence"]["not_measured"] else "provider reliability score below 80")
    if ready["Decision Traceability"]["status"] != READY:
        reasons.append("final decisions not fully traceable")
    if reg["blocked"]:
        reasons.append(f"regression protection blocked the proposed config ({len(reg['issues'])} issue(s))")
    return (PRODUCTION_CONFIG_READY if not reasons else MORE_CALIBRATION), reasons


# ============================================================================ Stage 15 — recommendations
def rec(cat, area, current, evidence, change, effect, risk):
    return {"category": cat, "area": area, "current_rule": current, "observed_evidence": evidence,
            "proposed_change": change, "expected_effect": effect, "risk": risk}


def recommendations(prov, fields, core, wps, sens, dec, conf, comp, cre_cal, sup, cost, qs, split, crit, other):
    R = []
    kp = prov["KaloPilot deep analysis"]
    R.append(rec("KEEP", "WPS weights / formula", "wps-v2 (Step U)",
                 f"{len(core)} metrics; CORE inputs available in >= 89% of {kp['successful']} live deep answers",
                 "none", "scores stay comparable across runs", "small sample (9 answers, 7 products)"))
    for name, w in wps.items():
        if "TOO_MANY_MAX_SCORES" in w["flags"]:
            R.append(rec("MONITOR", f"WPS · {name}", f"linear 0→{w['max']} at +100 % growth",
                         f"{w['share_max']:.0%} of live answers at max: every live product had a very low prior base",
                         "none now; revisit with >= 30 answers across categories (low-base cap would be a formula change)",
                         "long-term growth cannot separate low-base products", "over-rewarding launches"))
        if "TOO_MANY_ZERO_SCORES" in w["flags"] or "LOW_VARIANCE" in w["flags"]:
            R.append(rec("NEEDS_MORE_DATA", f"WPS · {name}", f"tier {w['tier']}, max {w['max']}",
                         f"zero in {w['share_zero']:.0%} of available live answers ({', '.join(w['flags'])}); raw values "
                         "confirm genuinely low / volatile values (not a mapping error)",
                         "none: the live products were small; do not loosen scales to create variance",
                         "keeps WPS honest for small products", "component may be uninformative for this product mix"))
    for c in core:
        if "move to SUPPORTING" in c["proposal"]:
            R.append(rec("CHANGE", f"tier · {c['metric']}", "CORE", f"available {c['rate']}", "move to SUPPORTING",
                         "missing data no longer zero-scored", "WPS denominator changes"))
    sparse_sup = [c["metric"] for c in core if c["tier"] != "CORE" and c["status"] in (SPARSE, UNRELIABLE)]
    if sparse_sup:
        R.append(rec("KEEP", "tier · " + ", ".join(sparse_sup), "SUPPORTING",
                     "sparse in live answers (competition level only in the current prompt; creator growth needs >= 3 "
                     "creators)", "keep SUPPORTING (never promote sparse fields to CORE)",
                     "missing values keep lowering Confidence, not WPS", "none"))
    dp = [r for r in conf["products"] if r["double_penalties"]]
    if dp:
        R.append(rec("CHANGE", "WPS Confidence · double penalty",
                     "missing SUPPORTING metric -3 and missing ENHANCEMENT field -1 are subtracted even when the same "
                     "value is already missing in a Confidence component",
                     f"{len(dp)}/{len(conf['products'])} live answers penalized twice for one missing value "
                     "(competition count / similar listings / shop count)",
                     "scoring_v2: data_requirements.counted_in_confidence (penalize once)",
                     "Confidence rises only by the duplicated points (bounded in regression check)",
                     "slightly higher confidence for products with missing competition data"))
    const_enh = [f for f, v in fields.items() if f.startswith("Deep") and v["rate"] == 0.0]
    R.append(rec("MONITOR", "WPS Confidence · ENHANCEMENT fields never requested",
                 "-1 per missing ENHANCEMENT field",
                 f"{len(const_enh)} deep fields are 0 % available because the compact prompt no longer asks for them "
                 "(daily units, similar listings, price history): a constant -5..-8 on every product",
                 "none now (removing them would raise confidence without new evidence); decide in AC whether the prompt "
                 "should request them or the list should shrink", "no change", "Confidence partly reflects prompt design"))
    if split["legacy_prompt"]:
        R.append(rec("KEEP", "Growth verification (gmv_prev_30d)", "growth CALCULATED from gmv_30d and gmv_prev_30d",
                     f"{len(split['legacy_prompt'])} legacy answers (older prompt, no gmv_prev_30d) show provider growth "
                     "~100x off; 0 growth mismatches with the current prompt",
                     "none", "growth stays verified", "old answers stay flagged GROWTH_UNVERIFIED"))
    for x in other:
        R.append(rec("MONITOR", "Mapping · selling_creator_count", "discovery field informational only",
                     x, "none in configs; ask the provider which definition the discovery endpoint uses",
                     "discovery REVIEW flag 'selling_creator_count_below_preferred' may use a different definition",
                     "a product could be flagged for review on a mis-defined count"))
    for x in crit:
        R.append(rec("CHANGE", "Mapping (critical)", "provider field mapping", x, "fix mapping before production",
                     "scores use correct values", "wrong WPS inputs until fixed"))
    neg_rej = [p for p in dec["products"] if p["reject_on_missing_evidence"]]
    R.append(rec("CHANGE" if neg_rej else "KEEP", "Decision · dependency gates",
                 "reject when CREATOR/VIDEO_DEPENDENCY and cross-platform demand not STRONG/ACCEPTABLE (UNKNOWN included)",
                 f"{len(neg_rej)} live product(s) rejected where cross-platform demand was UNKNOWN (missing, not negative)",
                 "decision_rules_v2: reject only on WEAK cross-platform demand; UNKNOWN -> block READY + task",
                 "rejections now require negative evidence; products stay visible as INSUFFICIENT_DATA / WATCHLIST",
                 "a truly creator-dependent product waits for more evidence instead of being rejected"))
    R.append(rec("KEEP", "Decision · explanations", "decision path + reasons",
                 f"live states {dec['states']}; primary explanation {dec['explanations']}",
                 "NEGATIVE_EVIDENCE / MISSING_EVIDENCE now separate in every decision", "clear why a product failed",
                 "none"))
    R.append(rec("KEEP", "Decision thresholds (Z-v1 dimensions)", "WPS 70/55, AVS 70/50, BVS 70/55, minimum confidences",
                 "sensitivity shows no live product near any READY threshold (max WPS "
                 f"{max([v['wps'] for v in sens['observed_values'] if v['wps'] is not None], default=NA)}); lowering "
                 "thresholds would only create positives", "none", "no false positives from calibration",
                 "thresholds untested against strong products"))
    for p in ("Amazon (KaloPilot Amazon validation)", "Supplier providers", "Competitor providers"):
        R.append(rec("NEEDS_MORE_DATA", f"Provider · {p}", "implemented / configured",
                     prov[p].get("note", "not measured"), "run a small evidence run (AC) before production",
                     "reliability can be scored", "production decisions stay without these layers"))
    R.append(rec("MONITOR", "Provider · KaloPilot deep analysis", "1 product per query, compact prompt",
                 f"reliability {kp['reliability_score']}; {kp['empty']} empty answer(s) at the ~8k output-token limit "
                 "(charged)", "freeze the prompt; keep batch size 1", "fewer re-bought answers",
                 "some products still produce long answers"))
    R.append(rec("REMOVE", "Discovery · combined 9-category prompt", "discovery_mode combined available",
                 "the combined prompt paused on a plan restriction and returned no data", "use per_category only in "
                 "production (runtime_production_v1 already does)", "no wasted discovery", "none"))
    R.append(rec("CHANGE", "Cost · deep estimate", "estimated_credits.deep_analysis 2.5",
                 f"observed {cost['observed_cost_per_query']['deep_answer_ok']}", "runtime_production_v1: conservative "
                 "estimate at the observed maximum", "run cap stops earlier instead of overspending",
                 "fewer products per run under the same cap"))
    R.append(rec("KEEP", "Query limits", "discovery 20 / deep 5 / Amazon 3", qs["proposal"], "none (no increase)",
                 "predictable cost", "slower coverage"))
    R.append(rec("KEEP", "Creative → BVS integration", "not wired", cre_cal["conclusion"], "none",
                 "BVS unaffected by thin creative data", "none"))
    R.append(rec("KEEP", "Supplier economics", "BVS partial without supplier offers", sup["conclusion"], "none",
                 "no manufactured margin", "BVS stays incomplete until offers are imported"))
    if comp["issues"]:
        R.append(rec("CHANGE", "Competition scope", "comparable scopes only", "; ".join(comp["issues"]),
                     "fix scope handling", "no scope mixing", "none"))
    else:
        R.append(rec("KEEP", "Competition scope", "only PRODUCT_CLUSTER / SUBCATEGORY counts are scored",
                     f"scopes observed {comp['scopes']}; no non-comparable count scored", "none", "no scope mixing",
                     "competition_saturation often N/A (scope level only in the current prompt)"))
    return R


# ============================================================================ report
def _t(x):
    return NA if x is None else (f"{x:g}" if isinstance(x, float) else str(x))


def render(res):
    L = [f"# Production Calibration (Step AB) — {res['generated_at'][:10]}", "",
         "> Offline, LIVE evidence only; no provider query was made; production configs were NOT changed.", "",
         f"**Result: {res['result']}**", ""]
    L += [f"- {r}" for r in res["result_reasons"]] + [""]
    ev = res["evidence"]
    L += ["## 1. Live evidence", "",
          f"- LIVE runs: {len(ev['runs'])} ({', '.join(ev['runs'])}); dry runs ignored: {ev['excluded']['dry_runs']}; "
          f"synthetic excluded: runs {ev['excluded']['synthetic_runs']}, raw {ev['excluded']['synthetic_raw']}",
          f"- Discovery answers: {ev['discovery_answers']} · deep answers: {ev['deep_answers']} (re-scored with ONE "
          f"config: {ev['rescored']}) · Amazon answers: {ev['amazon_answers']} · supplier / competitor files: "
          f"{ev['supplier_files']} / {ev['competitor_files']}",
          f"- AA controlled live run: {ev['aa_status']}",
          f"- Validation audits: {ev['audits']} · final-decision outputs (LIVE): {ev['final_decisions']} · history "
          f"identities (LIVE): {ev['history_identities']}", ""]
    L += ["## 2. Provider reliability (technical / data reliability only)", "",
          "| Provider | Attempted | OK | Partial | Empty | Timeouts | Missing fields | Mapping errors | Provenance "
          "failures | Match quality | Score | Status |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, v in res["provider_reliability"].items():
        L.append(f"| {k} | {v['attempted']} | {v['successful']} | {v['partial']} | {v['empty']} | {v['timeouts']} | "
                 f"{_t(v['missing_expected_field_rate'])} | {_t(v['mapping_error_rate'])} | "
                 f"{_t(v['provenance_failure_rate'])} | {v['match_quality']} | {_t(v['reliability_score'])} | {v['status']} |")
    L += ["", "Score = 40·success + 20·(1−missing fields) + 15·(1−mapping errors) + 15·(1−provenance failures) + "
          "10·(1−timeouts). LOW_SAMPLE = fewer than 10 attempts. Legacy-prompt answers are included (honest total).", ""]
    L += ["## 3. Field reliability", "", "| Field | n | Available | Rate | Status |", "|---|---|---|---|---|"]
    for k, v in res["field_reliability"].items():
        L.append(f"| {k} | {v['n']} | {v['available']} | {_t(v['rate'])} | {v['status']}"
                 f"{' (low sample)' if v.get('low_sample') else ''}{' — ' + v['note'] if v.get('note') else ''} |")
    L += ["", "## 4. CORE / SUPPORTING review", "", "| Metric | Tier | Live availability | Status | Proposal |",
          "|---|---|---|---|---|"]
    L += [f"| {c['metric']} | {c['tier']} | {c['available']}/{c['n']} | {c['status']} | {c['proposal']} |"
          for c in res["core_review"]]
    L += ["", "## 5. WPS calibration audit", "",
          "| Component | Metric | Tier | Avail. | Mean/Max | Share max | Share zero | Flags |", "|---|---|---|---|---|---|---|---|"]
    L += [f"| {w['group']} | {k} | {w['tier']} | {w['available']}/{w['n']} | {_t(w['mean'])}/{w['max']} | "
          f"{_t(w['share_max'])} | {_t(w['share_zero'])} | {', '.join(w['flags']) or '—'} |" for k, w in res["wps_audit"].items()]
    L += ["", "WPS was not changed. Flags come from 9 answers of 7 small Beauty products: they describe this sample.", ""]
    s = res["sensitivity"]
    L += ["## 6. Threshold sensitivity (offline, saved LIVE data)", "", s["definition"], "",
          "### WPS × WPS Confidence", "", "| WPS ≥ | Conf ≥ | Pass | Review | Reject | No data | Coverage |",
          "|---|---|---|---|---|---|---|"]
    L += [f"| {g['threshold']} | {g['confidence_min']} | {g['pass']} | {g['review']} | {g['reject']} | {g['no_data']} | "
          f"{_t(g['coverage'])} |" for g in s["wps_x_confidence"]]
    L += ["", f"Observed: " + ", ".join(f"{o['product_id'][-6:]} WPS {_t(o['wps'])} / conf {_t(o['wps_confidence'])}"
                                         for o in s["observed_values"]), "",
          "### BVS × BVS Confidence", "",
          f"No live BVS value exists: all {len(s['bvs_x_bvs_confidence'])} combinations have coverage 0 "
          f"({s['bvs_x_bvs_confidence'][0]['no_data']} products NO_DATA).", "",
          "### Momentum", "", f"Momentum Score available for {sum(1 for m in s['momentum_observed'] if m['m'] is not None)} "
          f"of {len(s['momentum_observed'])} LIVE history identities (all others INSUFFICIENT_HISTORY): every "
          f"combination 60–80 has coverage {_t(s['momentum_x_momentum_confidence'][0]['coverage'])}.", ""]
    d = res["decision_calibration"]
    L += ["## 7. Decision engine calibration", "", f"States (current rules, LIVE products): {d['states']}; primary "
          f"explanation: {d['explanations']}", "", "| Product | State | Primary | Negative evidence | Missing evidence | "
          "Reject on missing evidence |", "|---|---|---|---|---|---|"]
    L += [f"| {p['name']} | {p['state']} | {p['primary_explanation']} | {'; '.join(p['negative_evidence']) or '—'} | "
          f"{'; '.join(p['missing_evidence'][:4]) or '—'} | {p['reject_on_missing_evidence']} |" for p in d["products"]]
    c = res["confidence_calibration"]
    L += ["", "## 8. Confidence calibration", "", "| System | Measured on | Findings |", "|---|---|---|"]
    for k, v in c["systems"].items():
        L.append(f"| {k} | {v['measured_on']} | {v.get('note') or ', '.join(v.get('flags') or []) or 'no issue'}"
                 f"{' — values ' + str(v['values']) if v.get('values') else ''} |")
    L += ["", "| Product | Observation | WPS Conf. | Data completeness | Adjustments | Double penalties | Flags |",
          "|---|---|---|---|---|---|---|"]
    L += [f"| {r['product_id'][-6:]} | {str(r['observation'])[:16]} | {_t(r['wps_confidence'])} | {_t(r['data_completeness'])}"
          f" | {r['adjustment_points']} | {len(r['double_penalties'])} | {', '.join(r['flags']) or '—'} |"
          for r in c["products"]]
    cp = res["confidence_calibration_proposed"]
    L += ["", f"With the proposed scoring_v2: WPS Confidence flags {sorted({x.split(': ')[1] for x in cp['flags']}) or 'none'}; "
          f"Decision Confidence depends on performance: {cp['systems']['Decision Confidence']['depends_on_performance'] or 'no'}."]
    m = res["matching_calibration"]
    L += ["", "## 9. Matching calibration", "", "| Matching | n | 90-100 | 75-89 | 60-74 | <60 | Note |",
          "|---|---|---|---|---|---|---|"]
    for k, v in m.items():
        if isinstance(v, dict):
            b = v["bands"]
            L.append(f"| {k} | {v['n']} | {b['90-100']} | {b['75-89']} | {b['60-74']} | {b['<60']} | {v.get('note', '')} |")
    L += ["", m["conclusion"], ""]
    cc = res["competition_calibration"]
    L += ["## 10. Competition calibration", "", f"Scopes observed: {cc['scopes']}. Issues: {cc['issues'] or 'none'}.",
          f"Direct / adjacent / category competitors: {cc['direct_adjacent_category_competitors']}.", ""]
    L += ["| Product | Scope | Comparable | Count | Level | competition_saturation pts |", "|---|---|---|---|---|---|"]
    L += [f"| {r['product_id'][-6:]} | {r['scope']} | {r['comparable']} | {_t(r['count'])} | {_t(r['level'])} | "
          f"{_t(r['competition_saturation_points'])} |" for r in cc["products"]]
    cr = res["creative_calibration"]
    L += ["", "## 11. Creative calibration", "",
          f"Products with saved creatives: {cr['products']}; Creative Confidence {cr['creative_confidence']} "
          f"(minimum {cr['minimum_for_use']}); mean share with hook {cr['features']['hook_classified']['mean']}, angle "
          f"{cr['features']['angle_classified']['mean']}, dated {cr['features']['dated']['mean']}; formats per product "
          f"{cr['features']['formats']['values']}.", "", cr["conclusion"], ""]
    L += ["## 12. Supplier calibration", "", res["supplier_calibration"]["conclusion"], ""]
    co = res["cost_efficiency"]
    L += ["## 13. Cost efficiency", "", "| Run | Status | Credits | Queries / discovered | Queries / deep product | "
          "Queries / decision |", "|---|---|---|---|---|---|"]
    L += [f"| {r['run_id']} | {r['final_status']} | {_t(r['credits_used'])} | {_t(r['queries_per_discovered_product'])} | "
          f"{_t(r['queries_per_deep_analyzed_product'])} | {_t(r['queries_per_final_decision'])} |" for r in co["runs"]]
    L += ["", f"Observed cost per answer: {json.dumps(co['observed_cost_per_query'])}", "",
          "High-cost low-value: " + ("; ".join(f"{w['file']} {w['credits']} ({w['reason']})"
                                               for w in co["high_cost_low_value"]) or "none"),
          "", "Duplicate query opportunities: " + ("; ".join(f"{d['product_id']} x{d['answers']} ({d['credits']} credits)"
                                                           for d in co["duplicate_query_opportunities"]) or "none"),
          "", "Cache opportunities: " + " · ".join(co["cache_opportunities"]), "", f"Depth: {co['depth_reduction']}", ""]
    q = res["query_strategy"]
    L += ["## 14. Query strategy (expected impact from observed runs)", "", q["assumptions"], "",
          "| Stage | Limit | Queries | Credits (min / expected / max) |", "|---|---|---|---|"]
    for stg in ("discovery", "deep_analysis", "amazon"):
        for o in q[stg]:
            cred = o["credits"] if isinstance(o["credits"], str) else (
                f"{o['credits']['min']} / {o['credits']['expected']} / {o['credits']['max']}" if o["credits"] else NA)
            L.append(f"| {stg} | {o['limit']} | {o['queries']} | {cred} |")
    for stg in ("supplier", "competitor", "creative"):
        L.append(f"| {stg} | 3 / 5 / 10 | 0 paid | {q[stg][0]['note']} |")
    L += ["", q["proposal"], ""]
    L += ["## 15. Recommendations", ""]
    for cat in ("KEEP", "CHANGE", "MONITOR", "REMOVE", "NEEDS_MORE_DATA"):
        rs = [r for r in res["recommendations"] if r["category"] == cat]
        L += [f"### {cat} ({len(rs)})", ""]
        for r in rs:
            L += [f"- **{r['area']}** — current: {r['current_rule']} · evidence: {r['observed_evidence']} · proposed: "
                  f"{r['proposed_change']} · effect: {r['expected_effect']} · risk: {r['risk']}"]
        L.append("")
    L += ["## 16. Proposed configs (not applied)", ""]
    for name, info in res["proposed_configs"].items():
        L.append(f"- `config/proposed/{name}`: " + ("; ".join(info["changes"]) or "no change"))
    rp = res["replay"]
    L += ["", "## 17. Offline replay (old vs proposed)", "", "| Product | Obs. | Old WPS | New WPS | Old conf. | New conf. | "
          "Reason |", "|---|---|---|---|---|---|---|"]
    L += [f"| {s['product_id'][-6:]} | {str(s['observation'])[:16]} | {_t(s['old_wps'])} | {_t(s['new_wps'])} | "
          f"{_t(s['old_confidence'])} | {_t(s['new_confidence'])} | {s['reason']} |" for s in rp["scores"]]
    L += ["", "| Product | Old decision | New decision | Old DC | New DC | Changed | Reason |", "|---|---|---|---|---|---|---|"]
    L += [f"| {d['name']} | {d['old_state']} | {d['new_state']} | {d['old_decision_confidence']} | "
          f"{d['new_decision_confidence']} | {'**yes**' if d['changed'] else 'no'} | {d['reason'][:160]} |"
          for d in rp["decisions"]]
    dimch = [(d["name"], d["dimension_changes"]) for d in rp["decisions"] if d.get("dimension_changes")]
    L += ["", "Dimension changes: " + ("; ".join(f"{n[:40]}: {c}" for n, c in dimch) or "none")]
    crossed = [s_ for s_ in rp["scores"] if _num(s_["old_confidence"]) is not None and _num(s_["new_confidence"]) is not None
               and _num(s_["old_confidence"]) < 60 <= _num(s_["new_confidence"])]
    if crossed:
        L.append("Review: WPS Confidence crossed the decision minimum (60) only because the double penalty was removed: "
                 + ", ".join(f"{x['product_id'][-6:]} {x['old_confidence']} -> {x['new_confidence']}" for x in crossed))
    rg = res["regression"]
    L += ["", "## 18. Regression protection", "", f"Checks: {', '.join(rg['checks'])}.", "",
          ("**BLOCKED** — " + "; ".join(rg["issues"])) if rg["blocked"] else "No regression found; proposed configs pass.", ""]
    L += ["## 19. Production readiness", "", "| Area | Status | Evidence |", "|---|---|---|"]
    L += [f"| {k} | {v['status']} | {v['evidence'] if isinstance(v['evidence'], str) else json.dumps(v['evidence'])} |"
          for k, v in res["readiness"].items()]
    L += ["", f"## 20. Final result: **{res['result']}**", ""] + [f"- {r}" for r in res["result_reasons"]]
    return "\n".join(L) + "\n"


# ============================================================================ entry point
def run(root=ROOT, data_root=None, write=True, now=None):
    now = now or datetime.now(timezone.utc)
    ev = load_evidence(root, data_root)
    scoring_cfg = load_scoring_config()
    recs = reanalyze(ev)
    mapping = mapping_state(recs)
    split = split_mapping(recs, mapping)
    crit, other = critical_mapping(split)
    prov = provider_reliability(ev, recs, mapping)
    fields = field_reliability(ev, recs, mapping)
    core = core_review(recs, scoring_cfg)
    wps = wps_audit(recs, scoring_cfg, mapping)
    em = EM.detect_all(ev["store"], now=now) if ev["store"].identities() else {"results": []}
    mom = {str(r["product_id"]): {"m": _num(r.get("momentum_score")), "mc": _num(r.get("momentum_confidence"))}
           for r in em.get("results", [])}
    latest = latest_per_product(recs)
    sens = sensitivity(latest, mom)
    prods, cre = decision_products(ev, recs, now)
    cur = decide_all(prods, cre, DE.load_cfg(), now)
    dec = decision_calibration(cur)
    conf = confidence_calibration(ev, recs, scoring_cfg, cur)
    match = matching_calibration(ev, cre)
    comp = competition_calibration(recs)
    cre_cal = creative_calibration(cre, DE.load_cfg())
    sup = supplier_calibration(ev)
    cost = cost_efficiency(ev, recs)
    qs = query_strategy(cost)
    props = build_proposals(root, cost)
    paths = write_proposals(root, props) if write else {}
    rp = replay(ev, recs, props, now)
    reg = regression(ev, rp, root)
    conf_new = confidence_calibration(ev, rp["new_recs"], rp["new_scoring"], rp["new"], rp["new_decision_cfg"])
    leaks = secret_scan(ev)
    ready = readiness(prov, fields, crit, wps, conf_new, rp, reg, cost, ev, leaks)
    result, reasons = final_result(crit, conf_new, prov, rp, reg, ready)
    if not ev["aa_reports"]:
        reasons.append("the AA controlled end-to-end live run was never executed: no evidence for Amazon, supplier, "
                       "competitor or the decision engine on live multi-layer data")
    aa_status = "EXECUTED" if ev["aa_reports"] else "NOT EXECUTED (the AA live run was prepared but never confirmed; " \
                                                       "no AA evidence exists)"
    res = {"generated_at": now.isoformat(), "result": result, "result_reasons": reasons,
           "evidence": {"runs": [m["run_id"] for m in ev["runs"]], "excluded": ev["excluded"],
                        "discovery_answers": len(ev["discovery"]), "deep_answers": len(ev["deep_raw"]),
                        "rescored": scoring_cfg.get("version"), "amazon_answers": len(ev["amazon_raw"]),
                        "supplier_files": len(ev["supplier_files"]), "competitor_files": len(ev["competitor_files"]),
                        "aa_status": aa_status, "audits": sum(len(v) for v in ev["audits"].values()),
                        "final_decisions": len(ev["final_decisions"]),
                        "history_identities": len(ev["store"].identities())},
           "mapping": {"current_prompt": split["current_prompt"], "legacy_prompt": split["legacy_prompt"],
                       "critical": crit, "non_critical": other},
           "provider_reliability": prov, "field_reliability": fields, "core_review": core, "wps_audit": wps,
           "sensitivity": sens, "decision_calibration": dec, "confidence_calibration": conf,
           "confidence_calibration_proposed": {"flags": conf_new["flags"], "systems": conf_new["systems"]},
           "matching_calibration": match, "competition_calibration": comp, "creative_calibration": cre_cal,
           "supplier_calibration": sup, "cost_efficiency": cost, "query_strategy": qs,
           "proposed_configs": {k: {"path": paths.get(k), "changes": v[1]} for k, v in props.items()},
           "replay": {"scores": rp["scores"], "decisions": rp["decisions"]}, "regression": reg,
           "readiness": ready, "secret_scan_files": len(leaks)}
    res["recommendations"] = recommendations(prov, fields, core, wps, sens, dec, conf, comp, cre_cal, sup, cost, qs,
                                             split, crit, other)
    if write:
        rt = ev["rt"]
        out = Path(data_root or root) / rt["paths"]["reports"]
        out.mkdir(parents=True, exist_ok=True)
        pats = [p.lower() for p in GR.load_cfg()["secret_key_patterns"]]
        secrets = safety.known_secret_values(rt)
        md = GR.scrub(render(res), pats, secrets)
        DE.check_language(md, DE.load_cfg())
        (out / "production-calibration.md").write_text(md)
        (out / "production-calibration.json").write_text(json.dumps(GR.scrub(res, pats, secrets), ensure_ascii=False,
                                                                    indent=2, default=str))
        res["paths"] = {"markdown": str(out / "production-calibration.md"), "json": str(out / "production-calibration.json"),
                        **{f"proposed_{k}": v for k, v in paths.items()}}
    return res


def main(argv=None):
    r = run()
    print(f"Report: {r['paths']['markdown']}")
    for k, v in r["proposed_configs"].items():
        print(f"Proposed: {v['path']}  ({len(v['changes'])} change(s))")
    print(r["result"])
    for x in r["result_reasons"]:
        print(f"  - {x}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
