"""Calibration audit (Step U): RAW vs NORMALIZED vs CALCULATED for one live run.

Read-only: makes NO provider query, changes NO formula or threshold. Writes
reports/YYYY-MM-DD-calibration-audit.md (+ .json) and returns the decision
LIVE_RUN_VALIDATED or CALIBRATION_REQUIRED with the exact issues.

Sections: field mapping per product (MAPPING_REVIEW_REQUIRED), WPS reproducibility
and inputs, Confidence vs missing data, Amazon fields, BVS (no invented supplier
data), score sanity check, credit audit, provider error audit, run summary.

CLI:
  python3 scripts/calibration_audit.py [RUN_ID]      # default: latest LIVE run
"""
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import deep_analysis as DA  # noqa: E402
import discovery as D  # noqa: E402
import safety  # noqa: E402
from score_products import load_scoring_config, wps_breakdown  # noqa: E402

NA = "N/A"
MAP = "MAPPING_REVIEW_REQUIRED"
# provider field -> (path in the stored deep record, parse kind)
DEEP_FIELDS = [("product_name", ("product_name",), "str"), ("price_min", ("price", "min"), "num"),
               ("price_max", ("price", "max"), "num"), ("gmv_30d", ("gmv",), "num"), ("units_30d", ("units",), "count"),
               ("growth_30d_pct", ("growth", "growth_30d_pct"), "pct"),
               ("category_growth_pct", ("growth", "category_growth_pct"), "pct"),
               ("creator_count", ("creator_metrics", "total"), "count"),
               ("selling_creator_count", ("creator_metrics", "selling"), "count"),
               ("video_count", ("video_metrics", "total"), "count"),
               ("selling_video_count", ("video_metrics", "selling"), "count"),
               ("video_sales_share_pct", ("video_metrics", "sales_share_pct"), "num"),
               ("shop_count", ("competition_metrics", "shop_count"), "count"),
               ("similar_listings_count", ("competition_metrics", "similar_listings_count"), "count"),
               ("category_product_count", ("competition_metrics", "category_product_count"), "count"),
               ("commission_pct", ("commission_pct",), "num"), ("launch_date", ("launch_date",), "date")]
DISC_VS_DEEP = [("gmv_30d", ("gmv",)), ("units_30d", ("units",)), ("growth_30d", ("growth", "growth_30d_pct")),
                ("price_min", ("price", "min")), ("price_max", ("price", "max")),
                ("creator_count", ("creator_metrics", "total")), ("video_count", ("video_metrics", "total"))]


def _get(d, path):
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _read(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError, TypeError):
        return None


def fmt(v):
    if v is None:
        return "null"
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}"
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "..."


def _show(c):
    raw = c.get("raw")
    if isinstance(raw, list):
        return f"{len(raw)} items"
    return fmt(c.get("derived", raw))


def rel_diff(a, b):
    a, b = _num(a), _num(b)
    if a is None or b is None or max(abs(a), abs(b)) == 0:
        return None
    return abs(a - b) / max(abs(a), abs(b))


# ====================================================================== per product
def mapping_checks(rec, disc_rec):
    """Rows RAW | parsed | NORMALIZED | Discovery, plus MAPPING_REVIEW_REQUIRED issues."""
    raw = rec.get("original_record") or {}
    rows, issues = [], []
    for field, path, kind in DEEP_FIELDS:
        parsed, prob = D.parse_value(raw.get(field), kind)
        norm = _get(rec, path)
        ok = (parsed == norm) or (_num(parsed) is not None and _num(norm) is not None and abs(parsed - norm) < 1e-9)
        dv = (disc_rec or {}).get("facts", {}).get({"growth_30d_pct": "growth_30d"}.get(field, field)) if disc_rec else None
        rows.append({"field": field, "raw": raw.get(field), "parsed": parsed, "normalized": norm, "discovery": dv,
                     "status": "OK" if ok else MAP})
        if not ok:
            issues.append(f"{field}: raw {fmt(raw.get(field))} -> stored {fmt(norm)}")
        if prob:
            issues.append(f"{field}: parse problem ({prob})")
    g, u = _num(rec.get("gmv")), _num(rec.get("units"))
    pmin, pmax = _num(_get(rec, ("price", "min"))), _num(_get(rec, ("price", "max")))
    if None not in (pmin, pmax) and pmin > pmax:
        issues.append(f"price_min {pmin} > price_max {pmax}")
    if g and u and None not in (pmin, pmax) and u > 0:
        implied = g / u
        if not (0.5 * pmin <= implied <= 1.5 * pmax):
            issues.append(f"GMV/units = {implied:.2f} USD per unit, outside the listed price range {pmin}–{pmax}")
    sh = rec.get("sales_history") or {}
    for series, total, name in ((sh.get("daily_gmv"), g, "daily_gmv vs gmv_30d"),
                                (sh.get("daily_units"), u, "daily_units vs units_30d")):
        vals = [x for x in series or [] if _num(x) is not None]
        if total and len(vals) >= 20:
            d = abs(sum(vals) - total) / total
            if d > 0.25:
                issues.append(f"{name}: daily sum {sum(vals):,.0f} differs {d:.0%} from the 30-day total {total:,.0f}")
    for lst, name in (((rec.get("creator_metrics") or {}).get("top_creators"), "top creators"),
                      ((rec.get("video_metrics") or {}).get("top_videos"), "top videos")):
        rev = [_num(x.get("revenue")) for x in lst or [] if isinstance(x, dict)]
        rev = [x for x in rev if x is not None]
        if g and rev and sum(rev) > 1.05 * g:
            issues.append(f"{name} revenue sum {sum(rev):,.0f} > product GMV {g:,.0f}")
    for a, b, nm in (("selling", "total", "creators"),):
        cm = rec.get("creator_metrics") or {}
        if _num(cm.get(a)) is not None and _num(cm.get(b)) is not None and cm[a] > cm[b]:
            issues.append(f"selling {nm} {cm[a]} > total {nm} {cm[b]}")
    vm = rec.get("video_metrics") or {}
    if _num(vm.get("selling")) is not None and _num(vm.get("total")) is not None and vm["selling"] > vm["total"]:
        issues.append(f"selling videos {vm['selling']} > total videos {vm['total']}")
    if disc_rec:
        for dk, path in DISC_VS_DEEP:
            dv, nv = disc_rec["facts"].get(dk), _get(rec, path)
            d = rel_diff(dv, nv)
            if d is not None and d > 0.5:
                issues.append(f"Discovery vs Deep {dk}: {fmt(dv)} vs {fmt(nv)} ({d:.0%} apart)")
    gr = _num(_get(rec, ("growth", "growth_30d_pct")))
    if gr is not None and gr > 1000:
        issues.append(f"growth_30d_pct {gr:.0f}% (very low prior base or wrong field)")
    return rows, issues


def wps_recompute(rec, disc_rec, scoring_cfg):
    obj = rec.get("original_record") or {}
    ctx = disc_rec or {"facts": {}}
    f, _, _ = DA.normalize_deep(obj, ctx)
    inp = DA.scoring_input(f, 0)
    b = wps_breakdown(inp, scoring_cfg)
    stored = rec.get("wps_breakdown") or {}
    mism = [k for k, m in b["metrics"].items() if (stored.get(k) or {}).get("points") != m["points"]]
    return b, mism


def sanity(rec, amz, bvs, em, conc_rules):
    """Anomalies WITHOUT changing formulas."""
    out = []
    bd = rec.get("wps_breakdown") or {}
    gr = _num(_get(rec, ("growth", "growth_30d_pct")))
    gm = bd.get("growth_momentum") or {}
    flags = {f.get("flag") for f in rec.get("red_flags") or []}
    trend = (rec.get("trend_metrics") or {}).get("label")
    if _num(gm.get("points")) and gm["points"] > 0.5 * gm["max"] and (
            (gr is not None and gr <= 0) or "SALES_DECLINING" in flags or trend == "DECLINING"):
        out.append(f"declining sales (growth {gr}, trend {trend}) but Growth score {gm['points']}/{gm['max']}")
    cm = rec.get("creator_metrics") or {}
    cr = bd.get("creator_momentum") or {}
    if cm.get("total") is None and _num(cr.get("points")):
        out.append(f"creator count missing but Creator score {cr['points']}/{cr['max']}")
    for code, st in ((rec.get("concentration_metrics") or {}).get("flags") or {}).items():
        v, cond = st.get("value"), st.get("condition")
        if _num(v) is not None and cond and D._cmp(v, cond) and st.get("status") != "FLAGGED":
            out.append(f"{code}: value {v} meets {cond} but not flagged")
    if bvs:
        econ = bvs.get("economics") or {}
        if econ.get("product_cost") in (None, NA) and _num(bvs.get("bvs_confidence")) is not None \
                and bvs["bvs_confidence"] >= 60:
            out.append(f"no supplier economics but BVS Confidence {bvs['bvs_confidence']}")
    if amz and amz.get("status") == "ok" and amz.get("amazon_match_status") != "MATCHED" \
            and _num(amz.get("amazon_confidence")) is not None and amz["amazon_confidence"] >= 60:
        out.append(f"no reliable Amazon match but Amazon Confidence {amz['amazon_confidence']}")
    if em and str(em.get("emerging_status", "")).startswith("EMERGING") and (em.get("observation_count") or 0) < 2:
        out.append(f"Emerging status {em['emerging_status']} with {em.get('observation_count')} observation(s)")
    return out


# ====================================================================== run-level
def load_run(run_id, root=ROOT, data_root=None):
    rt = safety.load_runtime(root / "config" / "runtime.yaml")
    runs = Path(data_root or root) / rt["paths"]["runs"]
    if not run_id:
        cands = sorted(p.parent.name for p in runs.glob("*/manifest.json")
                       if (_read(p) or {}).get("mode") == "LIVE")
        run_id = cands[-1] if cands else None
    if not run_id:
        raise SystemExit("no LIVE run found")
    rd = runs / run_id
    m = _read(rd / "manifest.json")
    ck = {n: _read(rd / "checkpoints" / f"{n}.json") or {} for n in
          ("discovery", "deep_analysis", "amazon_validation", "bvs", "history", "emerging", "report")}
    return rt, rd, m, ck


def audit(run_id=None, root=ROOT, write=True, secrets=None, data_root=None):
    root = Path(root)
    rt, rd, m, ck = load_run(run_id, root, data_root)
    run_id = m["run_id"]
    env = m.get("data_environment")
    scoring_cfg = load_scoring_config()
    disc = _read(ck["discovery"].get("processed_file")) or {}
    disc_by = {}
    for r in (disc.get("candidates") or []) + (disc.get("failed") or []):
        if r["facts"].get("product_id"):
            disc_by[str(r["facts"]["product_id"])] = r
    deep = ck["deep_analysis"].get("results") or []
    amz = {str(a.get("product_id")): a for a in ck["amazon_validation"].get("results") or []}
    bvs = {str(b.get("product_id")): b for b in ck["bvs"].get("results") or []}
    em_file = _read(ck["emerging"].get("saved")) or {}
    em = {str(r.get("product_id")): r for r in em_file.get("results") or []}

    products, issues, anomalies = [], [], []
    for rec in deep:
        pid = str(rec.get("product_id"))
        if rec.get("status") != "ok":
            products.append({"product_id": pid, "status": rec.get("status"), "error": rec.get("error_category")})
            continue
        rows, mi = mapping_checks(rec, disc_by.get(pid))
        b, mism = wps_recompute(rec, disc_by.get(pid), scoring_cfg)
        if mism:
            mi.append(f"WPS not reproducible from the raw record for: {', '.join(mism)}")
        an = sanity(rec, amz.get(pid), bvs.get(pid), em.get(pid), None)
        issues += [f"{pid} {rec.get('product_name')}: {x}" for x in mi]
        anomalies += [f"{pid} {rec.get('product_name')}: {x}" for x in an]
        products.append({"product_id": pid, "name": rec.get("product_name"), "status": "ok", "mapping": rows,
                         "mapping_issues": mi, "wps": rec.get("wps"), "wps_complete": rec.get("wps_complete"),
                         "wps_recomputed": b, "wps_mismatch": mism, "confidence": rec.get("confidence"),
                         "confidence_level": rec.get("confidence_level"),
                         "confidence_breakdown": rec.get("confidence_breakdown"), "missing_data": rec.get("missing_data"),
                         "trend": (rec.get("trend_metrics") or {}).get("label"),
                         "concentration": (rec.get("concentration_metrics") or {}).get("flags"),
                         "red_flags": [f.get("flag") for f in rec.get("red_flags") or []],
                         "amazon": amz.get(pid), "bvs": bvs.get(pid), "emerging": em.get(pid), "anomalies": an,
                         "raw_file": (rec.get("source") or {}).get("raw_file"),
                         "parse_warnings": rec.get("parse_warnings")})

    # Amazon field check vs the raw candidate actually matched
    for pid, a in amz.items():
        if a.get("status") != "ok" or a.get("amazon_match_status") != "MATCHED":
            continue
        env_raw = _read((a.get("source") or {}).get("raw_file")) or {}
        obj, _ = DA.extract_object(env_raw)
        cand = next((c for c in (obj or {}).get("candidates") or [] if c.get("title") == a.get("amazon_match_name")), None)
        if cand is None:
            issues.append(f"{pid}: matched Amazon listing not found in its raw response")
            continue
        for k_raw, k in (("price", "amazon_price"), ("rating", "amazon_rating"), ("review_count", "amazon_review_count"),
                         ("bsr", "amazon_bsr")):
            pv = D.parse_value(cand.get(k_raw), "num" if k_raw in ("price", "rating") else "count")[0]
            if pv != a.get(k):
                issues.append(f"{pid}: Amazon {k_raw} raw {cand.get(k_raw)} -> stored {a.get(k)}")

    # BVS: supplier data never invented
    bvs_invented = []
    for pid, b in bvs.items():
        comm_file = (b.get("source") or {}).get("commercial_data_file")
        econ = b.get("economics") or {}
        if not comm_file and any(econ.get(k) not in (None, NA) for k in ("product_cost", "supplier_shipping_cost")):
            bvs_invented.append(pid)
    if bvs_invented:
        issues.append(f"BVS supplier economics present without a supplier record: {bvs_invented}")

    # limits
    lim, limit_issues = m.get("limits") or {}, []
    if len(disc.get("candidates") or []) > lim.get("discovery_max_products", 10 ** 9):
        limit_issues.append("discovery candidates above limit")
    if len(deep) > lim.get("deep_analysis_max_products", 10 ** 9):
        limit_issues.append(f"deep analysis {len(deep)} > {lim.get('deep_analysis_max_products')}")
    if len(amz) > lim.get("amazon_validation_max_products", 10 ** 9):
        limit_issues.append(f"amazon {len(amz)} > {lim.get('amazon_validation_max_products')}")

    # raw preserved (exists + read-only)
    raw_files = set(ck["discovery"].get("raw_files") or [])
    raw_files |= {(r.get("source") or {}).get("raw_file") for r in deep} | \
                 {(a.get("source") or {}).get("raw_file") for a in amz.values()}
    raw_files.discard(None)
    raw_problems = [f for f in raw_files if not Path(f).exists() or (os.stat(f).st_mode & stat.S_IWUSR)]
    # contamination
    contamination = []
    if env != "LIVE":
        contamination.append(f"run data_environment is {env}")
    for name, rows in (("discovery", (disc.get("candidates") or []) + (disc.get("failed") or [])), ("deep", deep),
                       ("amazon", list(amz.values())), ("bvs", list(bvs.values()))):
        contamination += [f"{name} {r.get('product_id') or r.get('key')}" for r in rows
                          if r.get("data_environment") != "LIVE"]
    hist_paths = ck["history"].get("paths") or []
    contamination += [f"history {p}" for p in hist_paths if (_read(p) or {}).get("data_environment") != "LIVE"]
    rep_json = _read((ck["report"].get("outputs") or {}).get("report_json")) or {}
    if (rep_json.get("report_metadata") or {}).get("data_environment") != "LIVE":
        contamination.append("report JSON not marked LIVE")
    # secrets
    secrets = safety.known_secret_values(rt) if secrets is None else secrets
    scan = [rd] + [Path(p) for p in (ck["report"].get("outputs") or {}).values() if p]
    scan += [Path(ck["discovery"].get("processed_file") or rd)] + [Path(p) for p in hist_paths]
    leaks = []
    for base in scan:
        for p in ([base] if base.is_file() else base.rglob("*")):
            if p.is_file():
                t = p.read_text(errors="ignore")
                if any(s in t for s in secrets):
                    leaks.append(str(p))
    reports_ok = all(Path(p).exists() for k, p in (ck["report"].get("outputs") or {}).items()
                     if k.startswith("report_")) and bool(ck["report"].get("outputs"))

    # credit + provider error audit
    ql = m.get("query_log") or []
    by_stage = m.get("provider_query_counts") or {}
    acc = m.get("query_accounting") or {}
    cr = m.get("credits") or {}
    errors = {"timeouts": [], "rate_limits": [], "malformed": [], "empty": [], "auth": [], "other": []}
    for q in ql:
        e = q.get("error_category")
        if not e:
            continue
        key = ("timeouts" if "timeout" in e or e == "task_status_running" else "rate_limits" if "429" in e or "rate" in e
               else "malformed" if "malformed" in e else "empty" if "empty" in e else
               "auth" if e in ("credentials",) or "401" in e or "403" in e else "other")
        errors[key].append(f"{q['stage']} {q.get('products')}: {e}")
    for r in deep + list(amz.values()):
        if r.get("status") in ("failed", "malformed"):
            e = r.get("error_category") or r.get("status")
            key = "malformed" if r.get("status") == "malformed" else ("timeouts" if "timeout" in str(e) else "other")
            errors[key].append(f"product {r.get('product_id')}: {e} {r.get('errors') or ''}".strip())
    ok_deep = [r for r in deep if r.get("status") == "ok"]
    unsupported = sorted(k for k, path, _ in DEEP_FIELDS if ok_deep and all(_get(r, path) is None for r in ok_deep))
    series_missing = sorted(k for k in ("daily_gmv", "daily_units") if ok_deep and all(
        not [x for x in ((r.get("sales_history") or {}).get(k) or []) if x is not None] for r in ok_deep))
    partial = {str(r.get("product_id")): r.get("missing_data") for r in ok_deep if r.get("missing_data")}

    def top(rows, key):
        vals = [(r.get(key), r) for r in rows if _num(r.get(key)) is not None]
        if not vals:
            return None
        v, r = max(vals, key=lambda x: x[0])
        return {"value": v, "product_id": r.get("product_id"),
                "name": r.get("product_name") or r.get("tiktok_product_name")}

    s = disc.get("summary") or {}
    summary = {"run_id": run_id, "mode": m.get("mode"), "market": m.get("market"), "data_environment": env,
               "discovered": s.get("unique"), "candidates_kept": len(disc.get("candidates") or []),
               "PASS": s.get("PASS"), "REVIEW": s.get("REVIEW"), "FAIL": s.get("FAIL"),
               "deep_selected": len(deep), "deep_ok": len(ok_deep),
               "amazon_validated": sum(a.get("status") == "ok" for a in amz.values()),
               "with_wps": sum(_num(r.get("wps")) is not None for r in ok_deep),
               "with_avs": sum(_num(a.get("avs")) is not None for a in amz.values()),
               "with_bvs": sum(_num(b.get("bvs")) is not None for b in bvs.values()),
               "top_wps": top(ok_deep, "wps"), "top_confidence": top(ok_deep, "confidence"),
               "top_avs": top(list(amz.values()), "avs"), "top_bvs": top(list(bvs.values()), "bvs"),
               "final_status": m.get("final_status"), "report_outputs": ck["report"].get("outputs")}
    credit = {"balance_start": cr.get("balance_start"), "balance_end": cr.get("balance_now"),
              "consumed_by_balance": cr.get("credits_used_by_balance"),
              "consumed_reported_by_provider": cr.get("credits_used_reported"),
              "estimated_before_run": (m.get("query_budget") or {}).get("estimated_credits_max"),
              "planned": acc.get("planned_queries"), "executed": acc.get("executed_queries"),
              "cached": m.get("cache_hits"), "failed": acc.get("failed_queries"), "blocked": acc.get("blocked_queries"),
              "by_stage": by_stage,
              "per_query": [{"stage": q["stage"], "type": q.get("query_type"), "action": q["action"],
                             "credits": q.get("credits"), "products": len(q.get("products") or [])} for q in ql]}

    reasons = []
    if issues:
        reasons.append(f"{len(issues)} provider-field mapping issue(s) ({MAP})")
    if anomalies:
        reasons.append(f"{len(anomalies)} score anomaly(ies)")
    if leaks:
        reasons.append(f"secret found in {len(leaks)} file(s)")
    if limit_issues:
        reasons += limit_issues
    if raw_problems:
        reasons.append(f"{len(raw_problems)} raw file(s) missing or writable")
    if not reports_ok:
        reasons.append("reports not generated")
    if contamination:
        reasons.append(f"{len(contamination)} non-LIVE record(s)")
    if not ok_deep:
        reasons.append("no product was deep-analyzed successfully")
    decision = "LIVE_RUN_VALIDATED" if not reasons else "CALIBRATION_REQUIRED"
    res = {"run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(), "decision": decision,
           "decision_reasons": reasons, "mapping_issues": issues, "anomalies": anomalies, "secret_leaks": leaks,
           "limit_issues": limit_issues, "raw_problems": raw_problems, "contamination": contamination[:50],
           "reports_generated": reports_ok, "products": products, "credit_audit": credit,
           "provider_errors": errors, "unsupported_metrics": unsupported, "missing_daily_series": series_missing,
           "partial_records": partial, "summary": summary}
    if write:
        res["paths"] = write_audit(res, Path(data_root or root) / rt["paths"]["reports"])
    return res


# ====================================================================== markdown
def render(res):
    L = [f"# Calibration audit — run {res['run_id']}", "",
         "> DATA ENVIRONMENT: **LIVE** · read-only audit · no formula or threshold was changed · "
         "no product is a guaranteed winner", "",
         f"## Decision: **{res['decision']}**", ""]
    L += [f"- {r}" for r in res["decision_reasons"]] or ["- all checks passed"]
    s = res["summary"]
    L += ["", "## Run summary", "", f"- Run {s['run_id']} · mode {s['mode']} · market {s['market']}",
          f"- Discovered {s['discovered']} (kept {s['candidates_kept']}) · PASS {s['PASS']} · REVIEW {s['REVIEW']} · "
          f"FAIL {s['FAIL']}",
          f"- Deep analyzed {s['deep_ok']}/{s['deep_selected']} · Amazon validated {s['amazon_validated']} · "
          f"with WPS {s['with_wps']} · with AVS {s['with_avs']} · with BVS {s['with_bvs']}"]
    for k in ("top_wps", "top_confidence", "top_avs", "top_bvs"):
        t = s[k]
        L.append(f"- {k.replace('_', ' ').title()}: " + (f"{t['value']} — {t['name']} ({t['product_id']})" if t else NA))
    for p in res["products"]:
        L += ["", f"## Product {p['product_id']} — {p.get('name') or ''}", ""]
        if p["status"] != "ok":
            L.append(f"Deep analysis {p['status']}: {p.get('error')}")
            continue
        L += ["### Field mapping (RAW → NORMALIZED, Discovery for comparison)", "",
              "| Field | Raw | Normalized | Discovery | Check |", "|---|---|---|---|---|"]
        for r in p["mapping"]:
            L.append(f"| {r['field']} | {fmt(r['raw'])} | {fmt(r['normalized'])} | {fmt(r['discovery'])} | {r['status']} |")
        L += ["", "Mapping issues: " + ("; ".join(p["mapping_issues"]) if p["mapping_issues"] else "none")]
        L += ["", f"### WPS {p['wps']} (complete: {p['wps_complete']}) — recomputed from the raw record",
              "", "| Metric | Points | Inputs used |", "|---|---|---|"]
        for k, mm in p["wps_recomputed"]["metrics"].items():
            ins = ", ".join(f"{c['input']}={_show(c)}" for c in mm["components"].values())
            L.append(f"| {k} | {mm['points']}/{mm['max']} | {ins} |")
        L.append("" if not p["wps_mismatch"] else f"\nWPS mismatch vs stored: {p['wps_mismatch']}")
        L += ["", f"### Confidence {p['confidence']} ({p['confidence_level']})", "",
              "| Component | Earned | Status |", "|---|---|---|"]
        for k, c in (p["confidence_breakdown"] or {}).items():
            L.append(f"| {k} | {c['earned']}/{c['max']} | {c['status']} |")
        L += ["", f"Missing data: {', '.join(p['missing_data'] or []) or 'none'}",
              f"Trend: {p['trend']} · Red flags: {', '.join(p['red_flags']) or 'none'}",
              "Concentration: " + "; ".join(f"{k} {v.get('status')} ({v.get('value', v.get('reason'))})"
                                            for k, v in (p["concentration"] or {}).items())]
        a = p.get("amazon")
        if a:
            L += ["", f"### Amazon — {a.get('status')}",
                  f"Match: {a.get('amazon_match_status')} ({a.get('amazon_match_class')}, confidence "
                  f"{a.get('amazon_match_confidence')}) · {fmt(a.get('amazon_match_name'))}",
                  f"AVS {a.get('avs')} · Amazon Confidence {a.get('amazon_confidence')} · cross-platform demand "
                  f"{a.get('cross_platform_demand')} · competition {a.get('amazon_competition')} · price alignment "
                  f"{a.get('price_alignment')}",
                  f"Amazon missing: {', '.join(a.get('missing_data') or []) or 'none'}"]
        else:
            L += ["", "### Amazon — not validated (not eligible or not run)"]
        b = p.get("bvs")
        if b:
            e = b.get("economics") or {}
            L += ["", f"### BVS {b.get('bvs')} · BVS Confidence {b.get('bvs_confidence')} ({b.get('bvs_confidence_level')})",
                  f"product_cost {e.get('product_cost')} · supplier_shipping_cost {e.get('supplier_shipping_cost')} · "
                  f"flags: {', '.join(f['flag'] for f in b.get('commercial_red_flags') or [])}"]
        else:
            L += ["", "### BVS — not evaluated (not eligible)"]
        em = p.get("emerging") or {}
        L += ["", f"Emerging: {em.get('emerging_status', NA)} — {em.get('status_reason', '')}",
              f"Anomalies: {'; '.join(p['anomalies']) or 'none'}", f"Raw file: `{p['raw_file']}`"]
    L += ["", "## Score sanity check (no formula changed)", ""] + ([f"- {x}" for x in res["anomalies"]] or ["- none found"])
    L += ["", "## All mapping issues", ""] + ([f"- {MAP}: {x}" for x in res["mapping_issues"]] or ["- none"])
    c = res["credit_audit"]
    L += ["", "## Credit audit", "",
          f"- Balance start {c['balance_start']} · end {c['balance_end']} · consumed (balance) {c['consumed_by_balance']} "
          f"· consumed (provider-reported) {c['consumed_reported_by_provider']} · estimated before run ≤ "
          f"{c['estimated_before_run']}",
          f"- Queries: planned {c['planned']} · executed {c['executed']} · cached {c['cached']} · failed {c['failed']} "
          f"· blocked {c['blocked']}", "", "| Stage | Live | Cached | Failed | Blocked |", "|---|---|---|---|---|"]
    for st, v in (c["by_stage"] or {}).items():
        L.append(f"| {st} | {v.get('LIVE_QUERY')} | {v.get('CACHE_HIT')} | {v.get('FAILED')} | {v.get('BLOCKED')} |")
    L += ["", "| Query | Type | Action | Products | Credits |", "|---|---|---|---|---|"]
    for i, q in enumerate(c["per_query"], 1):
        L.append(f"| {i} | {q['type']} | {q['action']} | {q['products']} | {q['credits'] if q['credits'] is not None else 'UNKNOWN'} |")
    e = res["provider_errors"]
    L += ["", "## Provider error audit", ""]
    for k in ("timeouts", "rate_limits", "malformed", "empty", "auth", "other"):
        L.append(f"- {k}: {'; '.join(e[k]) if e[k] else 'none'}")
    L += [f"- unsupported metrics (null for every product): {', '.join(res['unsupported_metrics']) or 'none'}",
          f"- daily series never returned: {', '.join(res['missing_daily_series']) or 'none'}",
          f"- partial records: {len(res['partial_records'])} product(s) with missing fields",
          "", "## Integrity", "",
          f"- secret leaks: {res['secret_leaks'] or 'none'}", f"- limits: {res['limit_issues'] or 'respected'}",
          f"- raw preserved (exists + read-only): {'yes' if not res['raw_problems'] else res['raw_problems']}",
          f"- synthetic/live contamination: {res['contamination'] or 'none'}",
          f"- reports generated: {res['reports_generated']}", "", f"**{res['decision']}**", ""]
    return "\n".join(L)


def write_audit(res, reports_dir):
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    n, md = 1, reports_dir / f"{date}-calibration-audit.md"
    while md.exists():
        n += 1
        md = reports_dir / f"{date}-calibration-audit-{n}.md"
    js = md.with_suffix(".json")
    secrets = safety.known_secret_values()
    with open(md, "x") as f:
        f.write(safety.redact(render(res), secrets))
    with open(js, "x") as f:
        json.dump(safety.redact(res, secrets), f, ensure_ascii=False, indent=2, default=str)
    return {"markdown": str(md), "json": str(js)}


def main(argv):
    r = audit(argv[1] if len(argv) > 1 else None)
    print(f"Calibration audit: {r['paths']['markdown']}")
    for x in r["decision_reasons"]:
        print(f"  - {x}")
    print(r["decision"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
