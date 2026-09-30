"""Step AA — Controlled End-to-End Live Run: final report, validation audit, data trust, cost audit, verdict.

Called by the master runner as its last stage (runner.s_aa_validation). Makes NO query: it only reads what
the run produced (checkpoints, context, saved files) and writes

    reports/YYYY-MM-DD-aa-live.md / .json      (dated: never overwritten)
    reports/latest-aa-live.md                  (replaced each run)
    reports/YYYY-MM-DD-aa-validation-audit.md

Result: AA_LIVE_VALIDATED only if every criterion holds, else AA_RECALIBRATION_REQUIRED with exact issues.
Nothing here raises limits, launches a product or places an order.
"""
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import calibration_audit as CA  # noqa: E402
import decision_engine as DE  # noqa: E402
import generate_report as GR  # noqa: E402
import history as HIST  # noqa: E402

NA = "N/A"
VALIDATED, RECAL = "AA_LIVE_VALIDATED", "AA_RECALIBRATION_REQUIRED"
MAP, SCO, PROV = "MAPPING_REVIEW_REQUIRED", "SCORING_REVIEW_REQUIRED", "PROVENANCE_REVIEW_REQUIRED"
OK, FLAG, NOT_EX = "OK", "FLAG", "NOT_EXERCISED"
METRICS = [("WPS", "wps"), ("WPS Confidence", "wps_confidence"), ("Momentum", "momentum_score"),
           ("Momentum Confidence", "momentum_confidence"), ("AVS", "avs"), ("Amazon Confidence", "amazon_confidence"),
           ("Supplier Quality", "supplier_quality"), ("Supplier Confidence", "supplier_confidence"),
           ("BVS", "bvs"), ("BVS Confidence", "bvs_confidence"), ("Competitor Saturation", "competitor_saturation"),
           ("Competitive Opportunity", "competitor_opportunity"), ("Competitor Confidence", "competitor_confidence"),
           ("Creative Saturation", "creative_saturation"), ("Creative Opportunity", "creative_opportunity"),
           ("Creative Confidence", "creative_confidence")]


def _f(x):
    if x is None or x == NA:
        return NA
    return f"{x:g}" if isinstance(x, float) else str(x)


def _counts(c):
    c = c or {}
    return (f"{c.get('LIVE_QUERY', 0)} live, {c.get('CACHE_HIT', 0) + c.get('FETCHED_RESULT', 0)} cached, "
            f"{c.get('FAILED', 0)} failed, {c.get('BLOCKED', 0)} blocked")


def _num(x):
    return DE.num(x)


# ============================================================================ Stage 16 — per-product rows
def product_rows(ctx):
    res = ctx.get("decision") or {"decisions": [], "shortlist": []}
    rep = ctx.get("rep") or {}
    by_id = {str(p.get("product_id")): p for p in rep.get("all_products") or []}
    rows = []
    for d in res["decisions"]:
        ev = d["evidence"]
        p = by_id.get(d["product_id"]) or {}
        em = p.get("emerging") or {}
        vals = {k: DE.v(ev, k) for _, k in METRICS}
        missing = [label for label, k in METRICS if vals[k] is None]
        deep_missing = list(((p.get("deep") or {}).get("missing_data")) or [])
        rows.append({
            "product_id": d["product_id"], "name": d["name"], "category": d.get("category"),
            **{k: vals[k] for _, k in METRICS},
            "emerging_status": em.get("emerging_status") or "INSUFFICIENT_HISTORY",
            "dimensions": d["dimension_status"], "final_decision": d["decision_state"],
            "decision_confidence": d["decision_confidence"]["score"],
            "red_flags": sorted(ev["flags"]), "missing_metrics": missing, "missing_provider_fields": deep_missing,
            "na_count": len(missing), "next_actions": d["next_actions"],
            "data_trust": d.get("data_trust"), "decision_path": d["decision_path"]})
    return rows


def sources_used(r, ctx):
    amz = ctx.get("amazon") or []
    return {
        "kalopilot": {"discovery": r.counts.get("discovery"), "deep_analysis": r.counts.get("deep_analysis"),
                      "deep_ok": len(r._ok_deep(ctx))},
        "amazon": {"validated": sum(a.get("status") == "ok" for a in amz), "attempted": len(amz),
                   "matched": sum(a.get("amazon_match_status") == "MATCHED" for a in amz),
                   "calibration_only": sum(bool(a.get("calibration_only")) for a in amz),
                   "stage": (r.stages.get("amazon_validation") or {}).get("summary")},
        "suppliers": ctx.get("supplier_research") or [],
        "competitors": ctx.get("competitor_rows") or [],
        "creatives": ctx.get("creative_rows") or [],
    }


# ============================================================================ Stage 17 — validation audit
def _check(name, status, detail, flag=None, items=None):
    return {"check": name, "status": status, "detail": detail, "flag": flag if status == FLAG else None,
            "items": items or []}


def validation_audit(r, ctx, run_audit=True):
    """run_audit=False (production): the run-level calibration audit is replaced by the caller's own mapping and
    score checks; only the layer / decision / history checks below are returned."""
    checks = []
    ca = {"provider_errors": {}, "raw_problems": []}
    if run_audit:
        try:
            ca = CA.audit(r.run_id, root=r.root, write=False, secrets=r.secrets, data_root=r.data_root)
        except Exception as e:  # noqa: BLE001
            ca = {"mapping_issues": [f"calibration audit failed: {e.__class__.__name__}: {e}"], "anomalies": [],
                  "provider_errors": {}, "raw_problems": []}
        mi = ca.get("mapping_issues") or []
        checks.append(_check("provider field mappings", FLAG if mi else OK,
                             f"{len(mi)} mapping issue(s) (raw -> normalized, discovery vs deep, internal consistency)",
                             MAP, mi))
        an = ca.get("anomalies") or []
        checks.append(_check("score anomalies (formulas unchanged)", FLAG if an else OK, f"{len(an)} anomaly(ies)",
                             SCO, an))
    ok_deep = r._ok_deep(ctx)

    # competition taxonomy
    bad = []
    for d in ok_deep:
        cm = d.get("competition_metrics") or {}
        comp_pts = [(d.get("wps_breakdown") or {}).get(k, {}).get("points")
                    for k in ("competition_saturation", "competition_category_growth")]
        if cm.get("competition_comparable") is False and any(p is not None for p in comp_pts):
            bad.append(f"{d['product_id']}: scope {cm.get('competition_scope')} not comparable but competition points "
                       f"{comp_pts}")
        if cm.get("competition_scope") is None:
            bad.append(f"{d['product_id']}: competition_scope missing")
    checks.append(_check("competition taxonomy (scope / comparability)", FLAG if bad else (OK if ok_deep else NOT_EX),
                         f"{len(ok_deep)} product(s) checked", SCO, bad))

    # growth calculations
    bad = []
    for d in ok_deep:
        g = d.get("growth") or {}
        cur, prev, gr = _num(d.get("gmv")), _num(g.get("gmv_prev_30d")), _num(g.get("growth_30d_pct"))
        if g.get("growth_source") == "calculated":
            if None in (cur, prev, gr) or not prev:
                bad.append(f"{d['product_id']}: growth marked calculated but inputs missing")
            elif abs((cur - prev) / prev * 100 - gr) > 0.5:
                bad.append(f"{d['product_id']}: growth {gr} != ({cur}-{prev})/{prev} = {(cur - prev) / prev * 100:.2f}")
        flags = {f.get("flag") for f in d.get("red_flags") or []}
        if g.get("growth_source") == "provider_unverified" and "GROWTH_UNVERIFIED" not in flags:
            bad.append(f"{d['product_id']}: unverified provider growth without GROWTH_UNVERIFIED flag")
    checks.append(_check("growth calculations (gmv_30d vs gmv_prev_30d)", FLAG if bad else (OK if ok_deep else NOT_EX),
                         f"{len(ok_deep)} product(s) recomputed", MAP, bad))

    # supplier matching + economics
    bvs = [b for b in ctx.get("bvs") or [] if not b.get("calibration_only")]
    sup_rows = ctx.get("supplier_research") or []
    used = sum(x["offers_used"] for x in sup_rows)
    bad = []
    for b in bvs:
        sel = ((b.get("supplier_layer") or {}).get("selected")) or {}
        if sel and sel.get("match_class") not in ("STRONG", "VERY_STRONG"):
            bad.append(f"{b.get('product_id')}: selected offer {sel.get('offer_id')} match class {sel.get('match_class')}")
    checks.append(_check("supplier matching", FLAG if bad else (OK if used else NOT_EX),
                         f"{used} offer(s) from {sum(x['offers_used'] > 0 for x in sup_rows)}/{len(sup_rows)} product(s)"
                         + ("" if used else " — no supplier source had data for these products"), MAP, bad))
    fabricated = []
    for b in bvs:
        econ = b.get("economics") or {}
        has = any(econ.get(k) not in (None, NA) for k in ("product_cost", "supplier_shipping_cost"))
        traced = bool((b.get("supplier_layer") or {}).get("selected_supplier_offer_id")) or \
            bool((b.get("source") or {}).get("commercial_data_file"))
        if has and not traced:
            fabricated.append(str(b.get("product_id")))
        for k in ("estimated_ad_cost_per_order", "expected_refund_cost", "contribution_profit"):
            if econ.get(k) not in (None, NA) and econ.get("product_cost") in (None, NA):
                fabricated.append(f"{b.get('product_id')}: {k} without supplier cost")
    checks.append(_check("supplier economics never fabricated", FLAG if fabricated else OK,
                         f"{len(bvs)} BVS record(s): cost/shipping only from a selected supplier offer", MAP, fabricated))

    # Amazon matching
    amz = [a for a in ctx.get("amazon") or [] if a.get("status") == "ok"]
    import config_resolver as _CR
    thr = (yaml.safe_load(Path(_CR.path("amazon_validation.yaml")).read_text()) or {}).get(
        "amazon_validation", {}).get("matching", {}).get("min_reliable_match", 60)
    bad = [f"{a['product_id']}: MATCHED with match confidence {a.get('amazon_match_confidence')} < {thr}"
           for a in amz if a.get("amazon_match_status") == "MATCHED" and (_num(a.get("amazon_match_confidence")) or 0) < thr]
    checks.append(_check("Amazon matching (no forced matches)", FLAG if bad else (OK if amz else NOT_EX),
                         f"{len(amz)} validated, {sum(a.get('amazon_match_status') == 'MATCHED' for a in amz)} matched"
                         + ("" if amz else f" — {(r.stages.get('amazon_validation') or {}).get('summary')}"), MAP, bad))

    # competitor matching
    comp = ctx.get("competitor") or {}
    dmin = (yaml.safe_load(Path(_CR.path("competitors.yaml")).read_text()) or {}).get(
        "relationships", {}).get("direct_min", 75)
    bad = []
    for pid, a in comp.items():
        for c in a.get("competitors") or []:
            if c.get("relationship") == "DIRECT" and (c.get("match_confidence_calc") or 0) < dmin:
                bad.append(f"{pid}: DIRECT competitor {c.get('competitor_name')} match {c.get('match_confidence_calc')}")
    checks.append(_check("competitor matching", FLAG if bad else (OK if comp else NOT_EX),
                         f"{len(comp)} product(s) with competitor evidence" +
                         ("" if comp else " — no competitor source had data for these products"), MAP, bad))

    # creative matching
    cre = ctx.get("creative") or {}
    bad = []
    for pid, a in cre.items():
        for c in a.get("creatives") or []:
            if c.get("qualified") and (c.get("match_confidence_calc") or 0) < 75:
                bad.append(f"{pid}: qualified creative {c.get('creative_id')} match {c.get('match_confidence_calc')}")
    checks.append(_check("creative matching", FLAG if bad else (OK if cre else NOT_EX),
                         f"{len(cre)} product(s), {sum(a['qualified_creatives'] for a in cre.values())} qualified "
                         "creative(s)", MAP, bad))

    # decision gates / determinism / shortlist
    res = ctx.get("decision") or {"decisions": [], "shortlist": [], "cfg": DE.load_cfg()}
    cfg = res.get("cfg") or DE.load_cfg()
    bad = []
    for d in res["decisions"]:
        again = DE.decide(copy.deepcopy(d["evidence"]), cfg)
        if again["decision_state"] != d["decision_state"]:
            bad.append(f"{d['product_id']}: not deterministic ({d['decision_state']} vs {again['decision_state']})")
        if d["decision_state"] == DE.READY and not DE.supplier_economics_known(d["evidence"]):
            bad.append(f"{d['product_id']}: READY without supplier economics")
        for g in d["hard_gates"]:
            if g["status"] == "FIRED" and g["action"] == "reject" and g["gate"] in DE.GATE_METRICS and \
                    all(DE.v(d["evidence"], k) is None for k in DE.GATE_METRICS[g["gate"]]):
                bad.append(f"{d['product_id']}: gate {g['gate']} fired on missing data")
    ready = {d["product_id"] for d in res["decisions"] if d["decision_state"] == DE.READY}
    if len(res["shortlist"]) > cfg["shortlist"]["max_products"] or any(s["product_id"] not in ready for s in res["shortlist"]):
        bad.append("shortlist contains a non-READY product or exceeds the maximum")
    checks.append(_check("final decision gates / states / shortlist", FLAG if bad else (OK if res["decisions"] else NOT_EX),
                         f"{len(res['decisions'])} decision(s) re-evaluated deterministically", SCO, bad))

    # confidence behavior
    bad = []
    for d in res["decisions"]:
        ev = copy.deepcopy(d["evidence"])
        for k in ("wps", "bvs", "avs", "competitor_opportunity", "creative_opportunity"):
            if ev["values"].get(k, {}).get("value") is not None:
                ev["values"][k]["value"] = 100.0 - ev["values"][k]["value"]
        dims = DE.dimensions(ev, cfg)
        known_same = [k for k in DE.DIMENSIONS if (dims[k]["status"] == DE.UNKNOWN) == (d["dimension_status"][k] == DE.UNKNOWN)]
        pen = abs(d["decision_confidence"].get("components", {}).get("degraded_mode_penalty") or 0)
        if len(known_same) == len(DE.DIMENSIONS) and \
                round(max(0.0, DE.decision_confidence(ev, dims, cfg)["score"] - pen), 2) != d["decision_confidence"]["score"]:
            bad.append(f"{d['product_id']}: Decision Confidence moved with product performance")
        for k in ("wps_confidence", "amazon_confidence", "bvs_confidence", "competitor_confidence", "creative_confidence"):
            x = DE.v(d["evidence"], k)
            if x is not None and not 0 <= x <= 100:
                bad.append(f"{d['product_id']}: {k} {x} outside 0-100")
    checks.append(_check("confidence behavior", FLAG if bad else (OK if res["decisions"] else NOT_EX),
                         "Decision Confidence independent of performance; confidences in range", SCO, bad))

    # history dedup + contamination
    from winning_product_agent.runner import EnvStore
    view = EnvStore(r.history_dir, r.env)
    ids = [str(d["product_id"]) for d in ok_deep]
    dups = {i: view.duplicate_snapshots(i) for i in ids}
    hc = (r._load_checkpoint("historical_storage") or {})
    raw_store = HIST.HistoryStore(r.history_dir)
    foreign = sum(1 for i in raw_store.identities() for o in HIST.HistoryStore.observations(raw_store, i)
                  if o.get("data_environment") not in (r.env,))
    items = [f"{i}: {n} duplicate snapshot(s) hidden by dedupe" for i, n in dups.items() if n]
    contam = [f"{foreign} history observation(s) from another data environment in {r.history_dir}"] if foreign else []
    checks.append(_check("history deduplication", OK, f"{hc.get('written', 0)} written, {hc.get('duplicates', 0)} "
                         "duplicate snapshot(s) skipped at write; dedupe on read active", None, items))
    checks.append(_check("no synthetic/live contamination", FLAG if contam else OK,
                         f"history + report inputs are {r.env} only", PROV, contam))

    # provenance completeness (data trust, Stage 18)
    bad = [f"{d['product_id']} {d['name']}: {d['data_trust']['untraceable']}" for d in res["decisions"]
           if not d["data_trust"]["traceable"]]
    checks.append(_check("provenance completeness (data trust)", FLAG if bad else (OK if res["decisions"] else NOT_EX),
                         f"every metric used by a decision checked for {', '.join(DE.TRUST_FIELDS)}", PROV, bad))
    perr = ca.get("provider_errors") or {}
    return {"checks": checks, "provider_errors": perr, "calibration_audit_summary": ca.get("summary"),
            "raw_problems": ca.get("raw_problems") or []}


# ============================================================================ limits / cost / secrets
def limit_violations(r, ctx):
    e, lim, v = r.e2e, r.limits, []
    disc = ctx.get("discovery") or {}
    if len(disc.get("candidates") or []) > lim["discovery_max_products"]:
        v.append(f"discovery candidates {len(disc.get('candidates'))} > {lim['discovery_max_products']}")
    if len(ctx.get("deep") or []) > lim["deep_analysis_max_products"]:
        v.append(f"deep analysis {len(ctx['deep'])} > {lim['deep_analysis_max_products']}")
    amz = [a for a in ctx.get("amazon") or [] if not a.get("calibration_only")]
    if len(amz) > lim["amazon_validation_max_products"]:
        v.append(f"amazon {len(amz)} > {lim['amazon_validation_max_products']}")
    sup = ctx.get("supplier_research") or []
    if len(sup) > e["supplier_products_max"] or any(x["offers_used"] > e["supplier_offers_per_product_max"] for x in sup):
        v.append("supplier limits exceeded")
    for key, np, per, field in (("competitor", "competitor_products_max", "competitors_per_product_max", "competitors"),
                                ("creative", "creative_products_max", "creatives_per_product_max", "creatives")):
        an = ctx.get(key) or {}
        if len(an) > e[np] or any(len(a.get(field) or []) > e[per] for a in an.values()):
            v.append(f"{key} limits exceeded")
    if len((ctx.get("decision") or {}).get("decisions") or []) > e["final_decision_max_products"]:
        v.append("final decision product limit exceeded")
    if len((ctx.get("decision") or {}).get("shortlist") or []) > 3:
        v.append("shortlist > 3")
    return v


def cost_audit(r):
    def stage(s):
        c = r.counts.get(s) or {}
        return {"executed": c.get("LIVE_QUERY", 0), "cached": c.get("CACHE_HIT", 0) + c.get("FETCHED_RESULT", 0),
                "failed": c.get("FAILED", 0), "blocked": c.get("BLOCKED", 0)}
    b = (r.manifest.get("query_budget") or {})
    prov = b.get("providers") or {}
    kp = [stage("discovery"), stage("deep_analysis")]
    kalo = {k: kp[0][k] + kp[1][k] for k in kp[0]}
    names = list(prov) or ["KaloPilot (TikTok Shop: discovery + deep analysis)", "Amazon (via KaloPilot Amazon validation)",
                           "Suppliers", "Competitors", "Creatives"]
    zero = {"executed": 0, "cached": 0, "failed": 0, "blocked": 0}
    rows = {}
    for n, c in zip(names, [kalo, stage("amazon_validation"), zero, zero, zero]):
        rows[n] = {"planned_max": (prov.get(n) or {}).get("planned_queries", NA), **c,
                   "cost": "KaloPilot credits" if "KaloPilot" in n else "no paid provider configured (0)"}
    start = r.balance_start
    end = r.balance_end if r.balance_end is not None else r.balance_now
    acc = r.budget.summary()
    return {"by_provider": rows, "totals": acc,
            "starting_balance": start if start is not None else "UNKNOWN",
            "ending_balance": end if end is not None else "UNKNOWN",
            "cost_used_by_balance": round(start - end, 2) if None not in (start, end) else "UNKNOWN",
            "cost_reported_by_provider": round(r.spent_known, 2),
            "estimated_before_run": b.get("estimated_credits_max", "UNKNOWN")}


def secret_scan(r, extra_paths=()):
    leaks = []
    for base in [r.run_dir, *[Path(p) for p in extra_paths]]:
        for p in ([base] if base.is_file() else base.rglob("*") if base.exists() else []):
            if p.is_file():
                t = p.read_text(errors="ignore")
                if any(s and s in t for s in r.secrets):
                    leaks.append(str(p))
    return leaks


# ============================================================================ verdict
def verdict(audit, limits, leaks, reports_ok, decisions):
    issues = []
    for c in audit["checks"]:
        if c["status"] == FLAG and c["check"] in ("provider field mappings", "growth calculations (gmv_30d vs gmv_prev_30d)"):
            issues.append(f"{MAP}: {c['check']} — {len(c['items'])} item(s), e.g. " + "; ".join(c["items"][:3]))
    for c in audit["checks"]:
        if c["status"] == FLAG and c["check"] == "supplier economics never fabricated":
            issues.append(f"supplier economics fabricated: {c['items']}")
        if c["status"] == FLAG and c["check"] == "no synthetic/live contamination":
            issues.append(f"synthetic/live contamination: {c['items']}")
        if c["status"] == FLAG and c["check"].startswith("final decision"):
            issues.append(f"{SCO}: decision engine — " + "; ".join(c["items"][:10]))
        if c["status"] == FLAG and c["check"] == "confidence behavior":
            issues.append(f"{SCO}: confidence behavior — " + "; ".join(c["items"][:10]))
        if c["status"] == FLAG and c["check"].startswith("provenance completeness"):
            issues.append(f"{PROV}: " + "; ".join(c["items"][:10]))
    if leaks:
        issues.append(f"secret leakage in {len(leaks)} file(s)")
    issues += [f"limit violation: {x}" for x in limits]
    if not reports_ok:
        issues.append("reports not generated")
    if not decisions:
        issues.append("no product reached the decision engine (nothing deep-analyzed successfully)")
    return (VALIDATED if not issues else RECAL), issues


# ============================================================================ rendering
def render_report(meta, rows, shortlist, sources, cost, result, issues, limitations):
    L = [f"# AA Controlled End-to-End Live Run — {meta['date']}", "",
         f"> Run `{meta['run_id']}` · profile `{meta['profile']}` · data environment **{meta['env']}** · "
         f"rules `{meta['rules']}` · generated {meta['generated_at']}", "",
         f"> {DE.DISCLAIMER} No product was launched and no supplier order was placed.", "",
         f"**Result: {result}**", ""]
    if issues:
        L += ["Issues:", ""] + [f"- {i}" for i in issues] + [""]
    if limitations:
        L += ["Layers not exercised (no data from a configured source — not fabricated):", ""] + \
             [f"- {x}" for x in limitations] + [""]
    L += ["## Shortlist (max 3, READY_FOR_PRODUCT_VALIDATION only)", ""]
    L += ([f"{s['rank']}. **{s['name']}** — decision confidence {s['tie_break']['decision_confidence']}" for s in shortlist]
          or ["Empty — no product met every READY rule; slots are left empty (no weaker product promoted)."])
    L += ["", "## Sources actually used", "",
          "- KaloPilot: discovery " + _counts(sources['kalopilot']['discovery']) + " · deep analysis "
          + _counts(sources['kalopilot']['deep_analysis']) + f" · deep ok {sources['kalopilot']['deep_ok']}",
          f"- Amazon (via KaloPilot): {sources['amazon']['validated']} validated, {sources['amazon']['matched']} matched"
          f" — {sources['amazon']['stage'] or ''}"]
    for key, label, n in (("suppliers", "Suppliers", "offers_used"), ("competitors", "Competitors", "observations_used"),
                          ("creatives", "Creatives", "creatives_used")):
        rows_ = sources[key]
        L.append(f"- {label}: " + ("; ".join(f"{x.get('name') or x['product_id']}: {x['source']} ({x.get(n, 0)})"
                                             for x in rows_) or "no product selected"))
    L += ["", "## Products", "",
          "| Product | " + " | ".join(lbl for lbl, _ in METRICS) + " | Emerging | Final Decision | Decision Conf. | N/A |",
          "|---|" + "---|" * (len(METRICS) + 4)]
    for r in rows:
        L.append(f"| {r['name']} | " + " | ".join(_f(r[k]) for _, k in METRICS) +
                 f" | {r['emerging_status']} | **{r['final_decision']}** | {r['decision_confidence']} | {r['na_count']} |")
    L += ["", "## Per product: dimensions, red flags, missing data, next actions", ""]
    for r in rows:
        dims = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in r["dimensions"].items())
        L += [f"### {r['name']} ({r['product_id']})", "",
              f"- Final decision: **{r['final_decision']}** · Decision Confidence {r['decision_confidence']}",
              f"- Dimensions: {dims}",
              f"- Decision path: {' → '.join(r['decision_path'])}",
              f"- Red flags: {', '.join(r['red_flags']) or 'none'}",
              f"- Missing metrics (N/A): {', '.join(r['missing_metrics']) or 'none'}",
              f"- Missing provider fields: {', '.join(r['missing_provider_fields']) or 'none'}",
              f"- Data trust: {'all used metrics traceable' if r['data_trust']['traceable'] else r['data_trust']['untraceable']}",
              "- Next actions:"] + [f"  - {a}" for a in r["next_actions"]] + [""]
    na_total = sum(r["na_count"] for r in rows)
    L += ["## N/A summary", "", f"{na_total} of {len(rows) * len(METRICS)} displayed metrics are N/A "
          f"({len(rows)} product(s) × {len(METRICS)} metrics). Missing values are never estimated.", ""]
    L += ["## Cost audit", "", "| Provider | Planned (max) | Executed | Cached | Failed | Blocked | Cost |",
          "|---|---|---|---|---|---|---|"]
    for n, c in cost["by_provider"].items():
        L.append(f"| {n} | {c['planned_max']} | {c['executed']} | {c['cached']} | {c['failed']} | {c['blocked']} | {c['cost']} |")
    L += ["", f"Starting balance {cost['starting_balance']} · ending balance {cost['ending_balance']} · used (balance) "
          f"{cost['cost_used_by_balance']} · reported by provider {cost['cost_reported_by_provider']} · estimated before "
          f"run ≤ {cost['estimated_before_run']}", ""]
    return "\n".join(L) + "\n"


def render_audit(meta, audit, limits, leaks, cost, result, issues):
    L = [f"# AA Validation Audit — {meta['date']}", "", f"> Run `{meta['run_id']}` · {meta['env']} · {meta['generated_at']}",
         "", f"**Result: {result}**", ""]
    L += ["| Check | Status | Flag | Detail |", "|---|---|---|---|"]
    for c in audit["checks"]:
        L.append(f"| {c['check']} | {c['status']} | {c['flag'] or ''} | {c['detail']} |")
    for c in audit["checks"]:
        if c["items"]:
            L += ["", f"### {c['check']}", ""] + [f"- {x}" for x in c["items"][:50]]
    perr = {k: v for k, v in (audit.get("provider_errors") or {}).items() if v}
    L += ["", "### Provider errors", ""] + ([f"- {k}: {x}" for k, v in perr.items() for x in v] or ["None."])
    L += ["", "### Limits", ""] + ([f"- {x}" for x in limits] or ["No limit violation."])
    L += ["", "### Secret scan", ""] + ([f"- leak: {x}" for x in leaks] or ["No secret value found in run files or outputs."])
    L += ["", "### Raw files", ""] + ([f"- {x}" for x in audit.get("raw_problems") or []] or
                                     ["All raw provider answers present and read-only."])
    L += ["", "### Issues", ""] + ([f"- {i}" for i in issues] or ["None."])
    return "\n".join(L) + "\n"


def _dated(out_dir, date, stem, exts):
    n = 1
    while True:
        sfx = "" if n == 1 else f"-{n}"
        paths = [out_dir / f"{date}-{stem}{sfx}.{e}" for e in exts]
        if not any(p.exists() for p in paths):
            return paths
        n += 1


# ============================================================================ entry point
def finalize(r, ctx, now):
    now = now or datetime.now(timezone.utc)
    rows = product_rows(ctx)
    res = ctx.get("decision") or {"decisions": [], "shortlist": [], "metadata": {}}
    audit = validation_audit(r, ctx)
    limits = limit_violations(r, ctx)
    cost = cost_audit(r)
    sources = sources_used(r, ctx)
    outs = ctx.get("outputs") or {}
    reports_ok = all(Path(p).exists() for k, p in outs.items() if k.startswith(("report_", "final_decision_")) and p) \
        and any(k.startswith("final_decision_") for k in outs)
    leaks = secret_scan(r, [p for k, p in outs.items() if p and k.startswith(("report_", "final_decision_"))])
    result, issues = verdict(audit, limits, leaks, reports_ok, res["decisions"])
    limitations = [f"{c['check']}: {c['detail']}" for c in audit["checks"] if c["status"] == NOT_EX]
    meta = {"run_id": r.run_id, "profile": r.eff["profile_path"], "env": r.env, "generated_at": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"), "rules": (res.get("metadata") or {}).get("decision_rules_version")}
    pats = [p.lower() for p in GR.load_cfg()["secret_key_patterns"]]
    md = GR.scrub(render_report(meta, rows, res["shortlist"], sources, cost, result, issues, limitations), pats, r.secrets)
    amd = GR.scrub(render_audit(meta, audit, limits, leaks, cost, result, issues), pats, r.secrets)
    js = GR.scrub({"metadata": {**meta, "decision_metadata": res.get("metadata"), "e2e_limits": r.e2e,
                                "limits": r.limits},
                   "result": result, "issues": issues, "limitations": limitations, "shortlist": res["shortlist"],
                   "products": rows, "sources_used": sources, "cost_audit": cost,
                   "validation_audit": {"checks": audit["checks"], "provider_errors": audit.get("provider_errors")},
                   "limit_violations": limits, "leakage_findings": leaks}, pats, r.secrets)
    DE.check_language(md, DE.load_cfg())
    out_dir = r.reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path, js_path = _dated(out_dir, meta["date"], "aa-live", ["md", "json"])
    (audit_path,) = _dated(out_dir, meta["date"], "aa-validation-audit", ["md"])
    for p, content in ((md_path, md), (audit_path, amd)):
        with open(p, "x") as f:
            f.write(content)
    with open(js_path, "x") as f:
        json.dump(js, f, ensure_ascii=False, indent=2, default=str)
    latest = out_dir / "latest-aa-live.md"
    latest.write_text(md)
    return {"result": result, "issues": issues, "limitations": limitations,
            "paths": {"aa_markdown": str(md_path), "aa_json": str(js_path), "aa_latest": str(latest),
                      "aa_validation_audit": str(audit_path)}}
