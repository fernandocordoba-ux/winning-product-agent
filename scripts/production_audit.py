"""Post-run audit of every production run (Step AC, extended in Step AD). No query: a failed audit never
re-buys anything and never changes a scoring rule.

Writes reports/production/YYYY-MM-DD-run-audit.md with:
  checks        config lock, data separation, query limits, score sanity, mappings, provenance, confidence,
                matching (Amazon / supplier / competitor / creative), taxonomy scope, growth, history dedup,
                supplier economics, decision rules, cost / guardrails, secrets
  cost audit    per provider + cost per discovered / deep-analyzed / evaluated / shortlisted product
  distribution  discovery statuses -> decision states -> shortlist
  data quality  live availability of the key fields and the main missing-data bottlenecks
Result: RUN_VALIDATED / RUN_REVIEW_REQUIRED, and for the first production run
FIRST_PRODUCTION_RUN_VALIDATED / PRODUCTION_RUN_REVIEW_REQUIRED.
"""
import copy
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aa_live as AA  # noqa: E402
import calibration_audit as CA  # noqa: E402
import decision_engine as DE  # noqa: E402
import generate_report as GR  # noqa: E402

RUN_VALIDATED, RUN_REVIEW = "RUN_VALIDATED", "RUN_REVIEW_REQUIRED"
FIRST_OK, FIRST_REVIEW = "FIRST_PRODUCTION_RUN_VALIDATED", "PRODUCTION_RUN_REVIEW_REQUIRED"
NA = "N/A"
# a FLAG on one of these checks blocks FIRST_PRODUCTION_RUN_VALIDATED
CRITICAL = {"config lock", "data separation", "query limits", "mapping (critical fields)", "provenance",
            "secrets", "decision", "supplier economics never fabricated", "no synthetic/live contamination",
            "reports generated"}
NON_CRITICAL_FIELDS = ("selling_creator_count",)          # documented definition mismatch (AB), discovery flag only


def _c(name, ok, detail, items=None, na=False):
    return {"check": name, "status": "NOT_EXERCISED" if na else ("OK" if ok else "FLAG"), "detail": detail,
            "items": items or []}


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _share(n, d):
    return round(n / d, 3) if d else None


def query_limits(r, ctx):
    lim, e, v = r.limits, r.e2e, []
    disc = ctx.get("discovery") or {}
    if len(disc.get("candidates") or []) > lim["discovery_max_products"]:
        v.append(f"discovery {len(disc['candidates'])} > {lim['discovery_max_products']}")
    if len(ctx.get("deep") or []) > lim["deep_analysis_max_products"]:
        v.append(f"deep {len(ctx['deep'])} > {lim['deep_analysis_max_products']}")
    amz = [a for a in ctx.get("amazon") or [] if not a.get("calibration_only")]
    if len(amz) > lim["amazon_validation_max_products"]:
        v.append(f"amazon {len(amz)} > {lim['amazon_validation_max_products']}")
    sup = ctx.get("supplier_research") or []
    if len(sup) > e["supplier_products_max"] or any(x["offers_used"] > e["supplier_offers_per_product_max"] for x in sup):
        v.append("supplier limits exceeded")
    for key, np_, per, field in (("competitor", "competitor_products_max", "competitors_per_product_max", "competitors"),
                                 ("creative", "creative_products_max", "creatives_per_product_max", "creatives")):
        an = ctx.get(key) or {}
        if len(an) > e[np_] or any(len(a.get(field) or []) > e[per] for a in an.values()):
            v.append(f"{key} limits exceeded")
    res = ctx.get("decision") or {}
    if len(res.get("decisions") or []) > e["final_decision_max_products"]:
        v.append("final decision limit exceeded")
    if len(res.get("shortlist") or []) > 3:
        v.append("shortlist > 3")
    g = r.eff["query_plan"].get("guardrails") or {}
    if g.get("max_queries_per_run") is not None and r.budget.executed > g["max_queries_per_run"]:
        v.append(f"executed {r.budget.executed} > max_queries_per_run {g['max_queries_per_run']}")
    return v


def distribution(r, ctx):
    disc = ctx.get("discovery") or {}
    s = disc.get("summary") or {}
    ok = r._ok_deep(ctx)
    res = ctx.get("decision") or {"decisions": [], "shortlist": []}
    import yaml
    import config_resolver as CR
    try:
        thr = (yaml.safe_load(Path(CR.path("decision.yaml")).read_text()) or {})["dimensions"]["market_momentum"][
            "acceptable"]["wps_min"]
    except Exception:  # noqa: BLE001
        thr = None
    states = {}
    for d in res["decisions"]:
        states[d["decision_state"]] = states.get(d["decision_state"], 0) + 1
    return {"Discovered": s.get("unique"), "PASS": s.get("PASS"), "REVIEW": s.get("REVIEW"), "FAIL": s.get("FAIL"),
            "Deep Analyzed": len(ok), f"WPS >= production threshold ({thr})": sum(
                1 for d in ok if thr is not None and (_num(d.get("wps")) or 0) >= thr),
            "Amazon validated": sum(1 for a in ctx.get("amazon") or [] if a.get("status") == "ok"
                                    and not a.get("calibration_only")),
            "Supplier validated": sum(1 for x in ctx.get("supplier_research") or [] if x["offers_used"]),
            "Competitor analyzed": len(ctx.get("competitor") or {}),
            "Creative analyzed": len(ctx.get("creative") or {}),
            **{st: states.get(st, 0) for st in (DE.READY, DE.PROMISING, DE.WATCH, DE.REJECT, DE.INSUFFICIENT)},
            "Shortlisted": len(res["shortlist"])}


def data_quality(r, ctx):
    ok = r._ok_deep(ctx)
    n = len(ok)
    res = ctx.get("decision") or {"decisions": []}
    m = len(res["decisions"])

    def rate(pred, rows, d):
        return {"available": sum(1 for x in rows if pred(x)), "n": d, "rate": _share(sum(1 for x in rows if pred(x)), d)}
    rows = {
        "GMV": rate(lambda d: d.get("gmv") is not None, ok, n),
        "units": rate(lambda d: d.get("units") is not None, ok, n),
        "growth (verified)": rate(lambda d: (d.get("growth") or {}).get("growth_30d_pct") is not None, ok, n),
        "creator data": rate(lambda d: (d.get("creator_metrics") or {}).get("total") is not None, ok, n),
        "video data": rate(lambda d: (d.get("video_metrics") or {}).get("total") is not None, ok, n),
        "competition (comparable scope)": rate(lambda d: (d.get("competition_metrics") or {}).get(
            "competition_comparable"), ok, n),
        "Amazon": rate(lambda d: (d["evidence"]["values"].get("avs") or {}).get("value") is not None, res["decisions"], m),
        "supplier cost": rate(lambda d: (d["evidence"]["values"].get("product_cost") or {}).get("value") is not None,
                              res["decisions"], m),
        "shipping": rate(lambda d: (d["evidence"]["values"].get("supplier_shipping_cost") or {}).get("value") is not None,
                         res["decisions"], m),
        "competitor intelligence": rate(lambda d: d["evidence"]["layers_present"].get("competitor"), res["decisions"], m),
        "creative intelligence": rate(lambda d: d["evidence"]["layers_present"].get("creative"), res["decisions"], m),
    }
    bottlenecks = sorted((k for k, v in rows.items() if v["rate"] is not None and v["rate"] < 0.5),
                         key=lambda k: rows[k]["rate"])
    missing = {}
    for d in res["decisions"]:
        for x in (d.get("explanation") or {}).get("MISSING_EVIDENCE") or []:
            if x["source"].startswith("dimension:"):
                missing[x["source"][10:]] = missing.get(x["source"][10:], 0) + 1
    return {"fields": rows, "bottlenecks": bottlenecks, "missing_evidence_by_dimension": missing}


def cost_audit(r, ctx, dist):
    c = r.counts

    def row(stages):
        return {k: sum((c.get(s) or {}).get(k, 0) for s in stages) for k in ("LIVE_QUERY", "CACHE_HIT", "FETCHED_RESULT",
                                                                             "FAILED", "BLOCKED")}
    b = r.manifest.get("query_budget") or {}
    st = b.get("stages") or {}
    prov = {"KaloPilot discovery": ("discovery",), "KaloPilot deep analysis": ("deep_analysis",),
            "Amazon (KaloPilot)": ("amazon_validation",)}
    out = {}
    for name, stages in prov.items():
        x = row(stages)
        costs = [q.get("credits") for q in r.query_log if q["stage"] in stages and q["action"] == "LIVE_QUERY"]
        known = [q for q in costs if isinstance(q, (int, float))]
        out[name] = {"planned_max": sum((st.get(s) or {}).get("expected_paid_max", 0) for s in stages),
                     "executed": x["LIVE_QUERY"], "cached": x["CACHE_HIT"] + x["FETCHED_RESULT"], "failed": x["FAILED"],
                     "blocked": x["BLOCKED"],
                     "credits": round(sum(known), 2) if known and len(known) == len(costs) else
                     ("UNKNOWN" if costs else 0.0)}
    for name in ("Supplier", "Competitor", "Creative"):
        out[name] = {"planned_max": 0, "executed": 0, "cached": 0, "failed": 0, "blocked": 0,
                     "credits": "no paid provider (0)"}
    start, end = r.balance_start, getattr(r, "balance_end", None)
    used = round(start - end, 2) if None not in (start, end) else "UNKNOWN"

    def per(n):
        return round(used / n, 2) if isinstance(used, (int, float)) and n else ("UNKNOWN" if not isinstance(used, (int, float))
                                                                               else NA)
    return {"by_provider": out, "starting_balance": start if start is not None else "UNKNOWN",
            "ending_balance": end if end is not None else "UNKNOWN", "credits_used": used,
            "reported_by_provider": round(r.spent_known, 2),
            "cost_per_discovered_candidate": per(dist.get("Discovered")),
            "cost_per_deep_analysis": per(dist.get("Deep Analyzed")),
            "cost_per_final_evaluated_product": per(len((ctx.get("decision") or {}).get("decisions") or [])),
            "cost_per_shortlisted_product": per(dist.get("Shortlisted"))}


def audit(r, ctx, now=None, first_run=None):
    now = now or datetime.now(timezone.utc)
    first_run = r.e2e.get("first_production_run", False) if first_run is None else first_run
    checks = []
    drift = r.drift()
    checks.append(_c("config lock", not drift, f"{r.config_version}: hashes unchanged during the run" if not drift
                     else f"changed: {drift}", drift))
    bad_env = []
    for k in ("deep", "amazon", "bvs"):
        bad_env += [f"{k} {x.get('product_id')}" for x in ctx.get(k) or [] if x.get("data_environment") not in (r.env,)]
    paths_ok = all("production" in str(v) for v in r.rt["paths"].values())
    checks.append(_c("data separation", not bad_env and paths_ok,
                     f"environment {r.env}; production paths {paths_ok}; calibration cache not reused", bad_env))
    lim = query_limits(r, ctx)
    checks.append(_c("query limits", not lim, f"limits {r.limits}; layers {dict((k, v) for k, v in r.e2e.items() if k.endswith('_max'))}",
                     lim))
    ok_deep = r._ok_deep(ctx)
    disc = {str(x["facts"].get("product_id")): x for x in ((ctx.get("discovery") or {}).get("candidates") or [])
            + ((ctx.get("discovery") or {}).get("failed") or [])}
    amz = {str(a.get("product_id")): a for a in ctx.get("amazon") or []}
    bvs = {str(b.get("product_id")): b for b in ctx.get("bvs") or []}
    sanity, crit_map, other_map = [], [], []
    for d in ok_deep:
        pid = str(d["product_id"])
        sanity += [f"{pid}: {x}" for x in CA.sanity(d, amz.get(pid), bvs.get(pid), None, None)]
        for x in CA.mapping_checks(d, disc.get(pid))[1]:
            (other_map if any(f in x for f in NON_CRITICAL_FIELDS) else crit_map).append(f"{pid}: {x}")
    checks.append(_c("score sanity", not sanity, f"{len(ok_deep)} product(s)", sanity, na=not ok_deep))
    checks.append(_c("mapping (critical fields)", not crit_map, f"{len(ok_deep)} product(s); fields used by WPS / "
                     "decisions", crit_map, na=not ok_deep))
    checks.append(_c("mapping (non-critical fields)", not other_map, "documented definition mismatch "
                     "(selling_creator_count, discovery flag only)", other_map, na=not ok_deep))
    res = ctx.get("decision") or {"decisions": [], "shortlist": []}
    untr = [f"{d['product_id']}: {d['data_trust']['untraceable']}" for d in res["decisions"]
            if not d["data_trust"]["traceable"]]
    short_ids = {s["product_id"] for s in res["shortlist"]}
    untr_short = [x for x in untr if x.split(":")[0] in short_ids]
    checks.append(_c("provenance", not untr_short, f"shortlisted decisions traceable; {len(untr)} decision(s) with "
                     "untraceable metrics were downgraded by the data-trust rule", untr_short + untr,
                     na=not res["decisions"]))
    for c in AA.validation_audit(r, ctx, run_audit=False)["checks"]:
        checks.append({"check": c["check"], "status": c["status"], "detail": c["detail"], "items": c["items"]})
    g = r.eff["query_plan"].get("guardrails") or {}
    acc = r.budget.summary()
    cap = r.eff["query_plan"].get("max_credits_for_run")
    spent = (round(r.balance_start - r.balance_end, 2) if None not in (r.balance_start, getattr(r, "balance_end", None))
             else acc.get("actual_credit_cost"))
    cost_items = []
    if isinstance(spent, (int, float)) and cap is not None and spent > cap:
        cost_items.append(f"credits used {spent} > cap {cap}")
    if r.stop_paid and r.stop_paid[0] not in ("report_only",):
        cost_items.append(f"paid queries stopped: {r.stop_paid[0]} ({r.stop_paid[1]}) — run PARTIAL, data kept")
    checks.append(_c("cost / guardrails", not cost_items,
                     f"executed {acc['executed_queries']}, cached {acc['cached_queries']}, failed {acc['failed_queries']}, "
                     f"blocked {acc['blocked_queries']}; credits used {spent} (cap {cap}); guardrails {g}", cost_items))
    leaks = []
    outs = [p for p in (ctx.get("outputs") or {}).values() if p]
    for base in [r.run_dir, *[Path(p) for p in outs]]:
        for p in ([base] if Path(base).is_file() else Path(base).rglob("*") if Path(base).exists() else []):
            if p.is_file() and any(s and s in p.read_text(errors="ignore") for s in r.secrets):
                leaks.append(str(p))
    checks.append(_c("secrets", not leaks, f"{len(leaks)} file(s) with a secret value", leaks))
    dec_items = []
    cfg = res.get("cfg") or DE.load_cfg()
    for d in res["decisions"]:
        again = DE.decide(copy.deepcopy(d["evidence"]), cfg)
        if again["decision_state"] != d["decision_state"] and not d["data_trust"]["untraceable"]:
            dec_items.append(f"{d['product_id']}: not deterministic")
        if d["decision_state"] == DE.READY and not DE.supplier_economics_known(d["evidence"]):
            dec_items.append(f"{d['product_id']}: READY without supplier economics")
    ready = {d["product_id"] for d in res["decisions"] if d["decision_state"] == DE.READY}
    if len(res["shortlist"]) > 3 or any(s["product_id"] not in ready for s in res["shortlist"]):
        dec_items.append("shortlist > 3 or contains a non-READY product")
    checks.append(_c("decision", not dec_items, f"{len(res['decisions'])} decision(s); shortlist "
                     f"{[s['name'] for s in res['shortlist']] or 'empty'}", dec_items, na=not res["decisions"]))
    missing_reports = [k for k in ("report_markdown", "report_json", "final_decision_markdown", "final_decision_json")
                       if not (ctx.get("outputs") or {}).get(k) or not Path(ctx["outputs"][k]).exists()]
    checks.append(_c("reports generated", not missing_reports, "winning-products md/json + final-decision md/json",
                     missing_reports))

    dist = distribution(r, ctx)
    dq = data_quality(r, ctx)
    cost = cost_audit(r, ctx, dist)
    issues = [f"{c['check']}: {'; '.join(map(str, c['items'][:5])) or c['detail']}" for c in checks if c["status"] == "FLAG"]
    critical = [i for c, i in zip([c for c in checks if c["status"] == "FLAG"], issues) if c["check"] in CRITICAL]
    result = RUN_VALIDATED if not issues else RUN_REVIEW
    first = (FIRST_OK if not critical else FIRST_REVIEW) if first_run else None

    L = [f"# Production run audit — {now.strftime('%Y-%m-%d')}", "",
         f"**CONFIG VERSION:** {r.config_version} · **RUN ID:** {r.run_id} · **DATA ENVIRONMENT:** {r.env}", "",
         f"**Result: {result}**" + (f" · **{first}**" if first else ""), ""]
    if critical:
        L += ["Blocking (critical) issues:", ""] + [f"- {i}" for i in critical] + [""]
    noncrit = [i for i in issues if i not in critical]
    if noncrit:
        L += ["Non-critical anomalies (review, not blocking):", ""] + [f"- {i}" for i in noncrit] + [""]
    L += ["## Checks", "", "| Check | Status | Detail |", "|---|---|---|"]
    L += [f"| {c['check']}{' (critical)' if c['check'] in CRITICAL else ''} | {c['status']} | {c['detail']} |"
          for c in checks]
    L += ["", "## Result distribution", "", "| Stage | Count |", "|---|---|"]
    L += [f"| {k} | {v if v is not None else NA} |" for k, v in dist.items()]
    L += ["", "## Cost audit", "", "| Provider | Planned (max) | Executed | Cached | Failed | Blocked | Credits |",
          "|---|---|---|---|---|---|---|"]
    L += [f"| {k} | {v['planned_max']} | {v['executed']} | {v['cached']} | {v['failed']} | {v['blocked']} | {v['credits']} |"
          for k, v in cost["by_provider"].items()]
    L += ["", f"Starting balance {cost['starting_balance']} · ending {cost['ending_balance']} · used {cost['credits_used']} "
          f"(reported by provider {cost['reported_by_provider']})",
          f"Cost per discovered candidate {cost['cost_per_discovered_candidate']} · per deep analysis "
          f"{cost['cost_per_deep_analysis']} · per evaluated product {cost['cost_per_final_evaluated_product']} · per "
          f"shortlisted product {cost['cost_per_shortlisted_product']}", ""]
    L += ["## Data quality (live production availability)", "", "| Field | Available | n | Rate |", "|---|---|---|---|"]
    L += [f"| {k} | {v['available']} | {v['n']} | {v['rate'] if v['rate'] is not None else NA} |"
          for k, v in dq["fields"].items()]
    L += ["", f"Main bottlenecks (< 50 %): {', '.join(dq['bottlenecks']) or 'none'}",
          f"Missing evidence by decision dimension: {dq['missing_evidence_by_dimension'] or 'none'}", ""]
    for c in checks:
        if c["items"]:
            L += [f"### {c['check']}", ""] + [f"- {x}" for x in c["items"][:50]] + [""]
    L += ["A failed audit never triggers a new paid query; scoring rules are not changed during a run."]
    r.reports_dir.mkdir(parents=True, exist_ok=True)
    n, date = 1, now.strftime("%Y-%m-%d")
    while (r.reports_dir / f"{date}-run-audit{'' if n == 1 else f'-{n}'}.md").exists():
        n += 1
    path = r.reports_dir / f"{date}-run-audit{'' if n == 1 else f'-{n}'}.md"
    pats = [p.lower() for p in GR.load_cfg()["secret_key_patterns"]]
    path.write_text(GR.scrub("\n".join(L) + "\n", pats, r.secrets))
    return {"result": result, "first_production_run_status": first, "issues": issues, "critical_issues": critical,
            "checks": checks, "distribution": dist, "data_quality": dq, "cost_audit": cost,
            "paths": {"run_audit": str(path)}}
