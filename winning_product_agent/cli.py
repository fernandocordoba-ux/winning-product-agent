"""Command line for the Master Runner (Step T). Run from the project root:

  python -m winning_product_agent preflight [--tests] [--profile P]
  python -m winning_product_agent run                       # = dry run (never paid)
  python -m winning_product_agent run --dry-run [--profile P] [--max-products N] [--check-balance]
  python -m winning_product_agent run --live --profile config/runtime_first_live.yaml --max-products 5
        [--confirm-live "CONFIRM LIVE RUN"]   # only for non-interactive sessions
        [--resume RUN_ID]                     # continue a stopped run without re-buying queries
        [--from-task TASK_ID]                 # Discovery = FREE import of a task finished in the web UI
        [--continue-task TASK_ID]             # Discovery = follow-up asking an unfinished task for its JSON
  python -m winning_product_agent status [RUN_ID]
  python -m winning_product_agent report [RUN_ID]
  python -m winning_product_agent audit [RUN_ID]           # Step U calibration audit (read-only)
"""
import argparse
import json
import sys
from pathlib import Path

from . import runner as R


def _p(s=""):
    print(s)


def cmd_preflight(a):
    import preflight as PF
    res = PF.preflight(R.ROOT, run_tests=a.tests)
    _p("PRE-FLIGHT")
    for c in res["checks"]:
        _p(f"  [{'OK' if c['ok'] else 'XX'}] {c['check']}: {c['detail']}")
    _p("\nSYSTEM STATUS")
    for k, v in res["system_status"].items():
        _p(f"  {k + ':':<24} {v['status']}" + (f"  ({'; '.join(v['reasons'])})" if v["reasons"] else ""))
    if a.profile:
        _, eff, errs = R.load_profile(a.profile, R.ROOT)
        _p(f"\nPROFILE {eff['profile_path']}: " + ("valid" if not errs else "INVALID — " + "; ".join(errs)))
        if errs:
            res["blocking_reasons"] += [f"profile: {e}" for e in errs]
            res["status"] = "BLOCKED"
    for w in res["warnings"]:
        _p(f"  WARNING: {w}")
    for b in res["blocking_reasons"]:
        _p(f"  BLOCKING: {b}")
    _p(f"\nPre-flight: {res['status']}")
    return 0 if res["status"] == "READY_FOR_DRY_RUN" else 1


def print_budget(b):
    if not b:
        _p("  (no budget: profile invalid)")
        return
    _p(f"  {'stage':<19}{'mode/batch':<16}{'max prod':>9}{'planned':>9}{'cached':>9}{'paid':>9}{'~credits':>16}")
    for s, r in b["stages"].items():
        mode = r.get("mode") or f"batch {r.get('batch_size')}"
        cached = r["cached"] if isinstance(r["cached"], int) else "at run"
        paid = f"{r['expected_paid_min']}–{r['expected_paid_max']}"
        cred = f"{r['estimated_credits_min']}–{r['estimated_credits_max']}"
        _p(f"  {s:<19}{mode:<16}{r['max_products']:>9}{r['planned_queries']:>9}{cached:>9}{paid:>9}{cred:>16}")
    _p(f"  TOTAL expected paid queries: {b['expected_paid_queries_min']}–{b['expected_paid_queries_max']} | "
       f"estimated credits: {b['estimated_credits_min']}–{b['estimated_credits_max']}")
    if b.get("providers"):
        _p("  BY PROVIDER")
        for name, r in b["providers"].items():
            _p(f"    {name:<52} planned {r['planned_queries']:>2} | paid {r['expected_paid_min']}–{r['expected_paid_max']}"
               f" | ~credits {r['estimated_credits_min']}–{r['estimated_credits_max']}"
               + (f" | sources {r['sources']}" if "sources" in r else "")
               + (f" | limit {r['limits']}" if r.get("limits") else ""))
            if r.get("note"):
                _p(f"      {r['note']}")
    _p(f"  basis: {b['estimate_basis']}")
    _p(f"  run credit cap: {b['max_credits_for_run']} | reserve kept: {b['min_balance_reserve']}")


def print_dry(r):
    _p(f"DRY RUN {r['run_id']}  | profile {r['profile']} | market {r['market']}")
    _p(f"Pre-flight: {r['preflight']['status']}")
    _p("\nPLANNED PIPELINE")
    for i, s in enumerate(r["plan"], 1):
        paid = s["paid"]
        _p(f"  {i:>2}. {s['stage']:<19} {s['does']}" + (f"  [paid queries ≤ {paid}]" if paid else ""))
    _p("\nQUERY BUDGET (before execution)")
    print_budget(r["query_budget"])
    _p(f"  current balance: {r['balance']}")
    dp = r.get("discovery_preview") or {}
    _p("\nPRODUCTS")
    if dp.get("available"):
        s = dp["summary"]
        _p(f"  Discovery would be a CACHE HIT: {s['unique']} products (PASS {s['PASS']}, REVIEW {s['REVIEW']}, "
           f"FAIL {s['FAIL']})")
        for c in dp["deep_candidates"]:
            _p(f"    - [{c['status']}] {c['product_id']} {(c['name'] or '')[:60]}"
               f"{'  (deep cached)' if c['deep_cached'] else ''}")
    else:
        _p(f"  {dp.get('note', 'n/a')}")
    for w in r["warnings"]:
        _p(f"  WARNING: {w}")
    for b in r["blocking_issues"]:
        _p(f"  BLOCKING: {b}")
    rd = r["readiness"]
    _p(f"\nPaid queries executed in this dry run: {r['paid_queries_executed']}")
    _p(f"Manifest: {r['manifest']}\nLog:      {r['log']}")
    _p(f"Dry-run status: {r['status']}")
    _p(f"Live readiness ({r['profile']}): {rd['verdict']}" + (f" — {'; '.join(rd['reasons'])}" if rd["reasons"] else ""))


def print_summary(s):
    _p("\n" + "=" * 66 + "\nRUN SUMMARY\n" + "=" * 66)
    _p(f"Run id:  {s['run_id']}\nMode:    {s['mode']} ({s['data_environment']})\nMarket:  {s['market']}\n"
       f"Profile: {s['profile']}")
    for st, v in s["stages"].items():
        d = s["stage_details"].get(st)
        _p(f"  {st:<19} {v:<10} {d or ''}")
    if s.get("products"):
        _p(f"Products: {json.dumps(s['products'])}")
    q = s["queries"]
    _p(f"Queries: planned {q['planned']} | executed {q['executed']} | cached {q['cached']} | blocked {q['blocked']} "
       f"| failed {q['failed']}")
    c = s["credits"]
    _p(f"Credits: estimated {c['estimated']} | used (reported) {c['used_reported']} | remaining {c['remaining']}")
    for k, v in s["outputs"].items():
        _p(f"  {k}: {v}")
    for b in s.get("blocking_reasons") or []:
        _p(f"  BLOCKED: {b}")
    _p(f"Overall status: {s['final_status']}")


def cmd_run(a):
    if a.live and a.dry_run:
        _p("BLOCKED: --live and --dry-run cannot be combined")
        return 2
    if not a.live and (a.confirm_live is not None or a.resume):
        _p("BLOCKED: --confirm-live / --resume are only valid with --live")
        return 2
    run = R.Runner(profile_path=a.profile, max_products=a.max_products)
    if a.continue_task:
        run.continue_task = a.continue_task
    if a.from_task:
        run.from_task = a.from_task
    if a.calibration_lane:
        run.cal_cfg = {**run.cal_cfg, "enabled": True}         # this run only; config file stays disabled
    if not a.live:                                    # default: DRY RUN, never paid
        r = run.dry_run(check_balance=a.check_balance)
        print_dry(r)
        return 0 if r["status"] == "DRY_RUN_COMPLETED" else 1
    s = run.live(confirm_value=a.confirm_live, resume_id=a.resume)
    print_summary(s)
    return 0 if s["final_status"] in ("COMPLETED", "PARTIAL") else 1


def cmd_status(a):
    rt = R.safety.load_runtime()
    m = R.run_status(R.ROOT / rt["paths"]["runs"], a.run_id)
    if not m:
        _p("No runs yet.")
        return 1
    _p(f"Run {m['run_id']} | mode {m['mode']} | env {m.get('data_environment')} | profile "
       f"{(m.get('profile') or {}).get('path')} | final {m.get('final_status')}")
    _p(f"started {m.get('started_at')} | completed {m.get('completed_at')}")
    for st, v in (m.get("stages") or {}).items():
        _p(f"  {st:<19} {v.get('status'):<10} {v.get('summary') or ''}")
    acc = m.get("query_accounting") or {}
    _p(f"queries executed {acc.get('executed_queries')} | cached {m.get('cache_hits')} | "
       f"credits {json.dumps(m.get('credits'))}")
    for b in m.get("blocking_reasons") or []:
        _p(f"  BLOCKED: {b}")
    return 0


def cmd_report(a):
    rt = R.safety.load_runtime()
    runs = R.ROOT / rt["paths"]["runs"]
    run_id = a.run_id or R.latest_run(runs, "LIVE")
    m = R.run_status(runs, run_id) if run_id else None
    out = (m or {}).get("outputs") or {}
    if not out.get("report_markdown"):
        _p("No live report yet (a report is written only by a live run).")
        latest = R.ROOT / rt["paths"]["reports"] / "latest-winning-products.md"
        if latest.exists():
            _p(f"Latest report file: {latest}")
        return 1
    _p(f"Run {run_id}")
    for k, v in out.items():
        _p(f"  {k}: {v}")
    return 0


def cmd_suppliers(a):
    """Step W: manual supplier offers (no orders, no supplier contact, no paid query)."""
    import suppliers as SUP
    rt = R.safety.load_runtime()
    p = rt["paths"]
    dirs = dict(raw_dir=R.ROOT / p["raw"] / "suppliers", processed_dir=R.ROOT / p["processed"] / "suppliers",
                hist_dir=R.ROOT / p["history"] / "suppliers")
    if a.action == "import":
        try:
            r = SUP.import_file(a.target, **dirs)
        except SUP.MalformedSupplierInput as e:
            _p(f"REJECTED FILE: {e}")
            return 1
        _p(f"Rows {r['rows']} | accepted {r['accepted']} | rejected {len(r['rejected'])} | products {r['products']}")
        for x in r["rejected"]:
            _p(f"  row {x['row']}: {'; '.join(x['errors'])}")
        _p(f"History: {r['history']} | raw: {r['raw_file']}")
        return 0 if r["accepted"] else 1
    offers = SUP.load_offers(a.target, dirs["processed_dir"])
    if not offers:
        _p(f"No supplier offers imported for {a.target}.")
        return 1
    deep = next((x for x in __import__("history").HistoryStore(R.ROOT / p["history"]).observations(a.target)[::-1]
                 if x.get("product_name")), {})
    ev = SUP.evaluate_product(a.target, {"name": deep.get("product_name")}, offers)
    for o in ev["offers"]:
        _p(f"  #{o['supplier_offer_rank']} {o['supplier_name']:<24} match {o['match_confidence_calc']} "
           f"landed {o['landed_cost']} eff.days {o['effective_delivery_days']} ({o['delivery_tier']}) "
           f"quality {o['supplier_quality']} conf {o['supplier_confidence']} eligible {o['eligible_for_economics']}")
    _p(f"Selected: {ev['selected_supplier_offer_id']} | flags: {[f['flag'] for f in ev['supplier_flags']]}")
    return 0


def cmd_competitors(a):
    """Step X: manual competitor research (no scraping, no paid query)."""
    import competitors as CI
    rt = R.safety.load_runtime()
    p = rt["paths"]
    proc = R.ROOT / p["processed"] / "competitors"
    if a.action == "import":
        try:
            r = CI.import_file(a.target, raw_dir=R.ROOT / p["raw"] / "competitors", processed_dir=proc)
        except CI.MalformedCompetitorInput as e:
            _p(f"REJECTED FILE: {e}")
            return 1
        _p(f"Rows {r['rows']} | accepted {r['accepted']} | rejected {len(r['rejected'])} | products {r['products']}")
        for x in r["rejected"]:
            _p(f"  row {x['row']}: {'; '.join(x['errors'])}")
        return 0 if r["accepted"] else 1
    obs = CI.load_latest(a.target, proc)
    if not obs:
        _p(f"No competitor research imported for {a.target}.")
        return 1
    obs_h = __import__("history").HistoryStore(R.ROOT / p["history"]).observations(a.target)
    deep = next((x for x in obs_h[::-1] if x.get("product_name")), {})
    an = CI.analyze_product(a.target, {"product_name": deep.get("product_name"),
                                       "units": (deep.get("tiktok") or {}).get("units")}, obs)
    _p(f"direct {an['direct_competitors']} | adjacent {an['adjacent_competitors']} | category {an['category_competitors']}")
    _p(f"saturation {an['saturation']['score']} | opportunity {an['opportunity']['score']} | "
       f"confidence {an['confidence']['score']} | flags {[f['flag'] for f in an['red_flags']]}")
    return 0


def cmd_creatives(a):
    """Step Y: creative research (manual import or SAVED KaloPilot top videos; no paid query)."""
    import creatives as CR
    p = R.safety.load_runtime()["paths"]
    proc = R.ROOT / p["processed"] / "creatives"
    if a.action == "import":
        try:
            r = CR.import_file(a.target, raw_dir=R.ROOT / p["raw"] / "creatives", processed_dir=proc)
        except CR.MalformedCreativeInput as e:
            _p(f"REJECTED FILE: {e}")
            return 1
        _p(f"Rows {r['rows']} | accepted {r['accepted']} | rejected {len(r['rejected'])} | products {r['products']}")
        for x in r["rejected"]:
            _p(f"  row {x['row']}: {'; '.join(x['errors'])}")
        return 0 if r["accepted"] else 1
    if a.action == "from-kalopilot":
        r = CR.import_from_kalopilot(a.target, R.ROOT / p["raw"] / "deep_analysis", proc)
        _p(json.dumps(r))
        return 0 if r["accepted"] else 1
    cs = CR.load_all(a.target, proc)
    if not cs:
        _p(f"No creative data for {a.target}.")
        return 1
    obs = __import__("history").HistoryStore(R.ROOT / p["history"]).observations(a.target)
    d = next((x for x in obs[::-1] if x.get("product_name")), {})
    an = CR.analyze_product(a.target, {"product_name": d.get("product_name"), "units": (d.get("tiktok") or {}).get("units")}, cs)
    _p(f"qualified {an['qualified_creatives']} | saturation {an['saturation']['score']} | opportunity "
       f"{an['opportunity']['score']} | confidence {an['confidence']['score']} | flags {[f['flag'] for f in an['red_flags']]}")
    for g in an["creative_gaps"]:
        _p(f"  gap {g['gap']}: {g['evidence']}")
    return 0


def cmd_audit(a):
    import calibration_audit as CA
    r = CA.audit(a.run_id)
    _p(f"Calibration audit: {r['paths']['markdown']}")
    for x in r["decision_reasons"]:
        _p(f"  - {x}")
    _p(r["decision"])
    return 0


def cmd_decide(a):
    """Step Z: rules-based final decision. Offline only: no query, no credits, nothing bought or launched."""
    import decision_engine as DE
    rt = R.safety.load_runtime()
    p = rt["paths"]
    reports = R.ROOT / p["reports"]
    if a.explain:
        saved = DE.load_saved(reports)
        if not saved:
            _p("No final decision saved yet (run `decide --rebuild` or a live run report stage).")
            return 1
        _p(json.dumps(DE.explain_decision(a.explain, saved), indent=2, ensure_ascii=False, default=str))
        return 0
    if not a.rebuild:
        saved = DE.load_saved(reports)
        if not saved:
            _p("No final decision saved yet. `decide --rebuild` recomputes it offline from saved LIVE data.")
            return 1
        m = saved["metadata"]
        _p(f"Final decision {m['timestamp']} · rules {m['decision_rules_version']} · env {m.get('data_environment')}")
        for d in saved["decisions"]:
            _p(f"  {d['decision_state']:<30} {d['name']}  (decision confidence {d['decision_confidence']['score']})")
        _p(f"Shortlist: {[s['name'] for s in saved['shortlist']] or 'empty (no READY product)'}")
        return 0
    import generate_report as GR
    import competitors as CI
    import creatives as CR
    env = "LIVE"
    inputs = GR.load_inputs(R.ROOT / p["processed"])
    disc = inputs["discovery"] or {}
    if disc and disc.get("data_environment") != env:
        inputs["discovery"] = None
    for k in ("deep", "amazon", "bvs"):
        inputs[k] = [r for r in inputs[k] if r.get("data_environment") == env]
    view = R.EnvStore(R.ROOT / p["history"], env)
    rep = GR.build_report(inputs, GR.load_cfg(), None, view if view.identities() else None)
    prods = rep["all_products"]
    comp = {str(r["product_id"]): r["analysis"] for r in CI.report_rows(prods, R.ROOT / p["processed"] / "competitors")}
    cre = {str(r["product_id"]): r["analysis"] for r in CR.report_rows(prods, R.ROOT / p["processed"] / "creatives")}
    mv = R.ROOT / p["processed"] / "manual_validation.json"
    res = DE.run(prods, comp, cre, manual=json.loads(mv.read_text()) if mv.exists() else None, expected_env=env)
    out = DE.write_reports(res, reports, secrets=GR.known_secrets())
    _p(f"Final decision: {out['markdown']}\nJSON: {out['json']}\nLatest: {out['latest']}")
    for d in res["decisions"]:
        _p(f"  {d['decision_state']:<30} {d['name']}")
    return 0


def cmd_calibrate(a):
    """Step AB: offline production calibration from saved LIVE data (no query, configs not applied)."""
    import production_calibration as PC
    return PC.main([])


def cmd_production_config(a):
    """Step AC: review / promote / verify / list / activate production config versions."""
    import promotion as PROMO
    if a.action == "review":
        rev = PROMO.review()
        _p(PROMO.render_review(rev))
        return 0
    if a.action == "promote":
        r = PROMO.promote()
        _p(PROMO.render_review(r["review"]))
        _p(f"Promoted {r['manifest']['config_version']} -> {r['dir']} "
           f"(approved {r['manifest']['changes']['approved']}, rejected {r['manifest']['changes']['rejected']}, "
           f"deferred {r['manifest']['changes']['deferred']})")
        return 0
    if a.action == "list":
        act = PROMO.active_version()
        for v in PROMO.versions():
            ok = PROMO.verify(PROMO.active_dir(version=v))["ok"]
            _p(f"  {v}{'  (ACTIVE)' if v == act else ''}  hashes {'OK' if ok else 'CHANGED'}")
        return 0
    if a.action == "verify":
        v = PROMO.verify(PROMO.active_dir(version=a.version))
        _p(json.dumps({k: v[k] for k in ("config_version", "ok", "changed", "missing", "extra")}))
        return 0 if v["ok"] else 1
    if a.action == "activate":
        if not a.version:
            _p("activate needs a version, e.g. v1")
            return 2
        _p(json.dumps(PROMO.activate(R.ROOT, a.version, note="manual activation (rollback / roll forward)")))
        return 0
    return 2


def print_production_dry(r):
    _p(f"PRODUCTION DRY RUN {r['run_id']} | config {r['config_version']} | market {r['market']}")
    v = r["config_hash_verification"]
    _p(f"Config hashes: {'OK' if v['ok'] else 'CHANGED ' + str(v['changed'] + v['missing'] + v['extra'])} ({r['config_dir']})")
    _p("\nPROVIDER HEALTH")
    for k, h in r["provider_health"].items():
        extra = {kk: vv for kk, vv in h.items() if kk not in ("status",)}
        _p(f"  {k:<11} {h['status']:<12} {json.dumps(extra, default=str)}")
    _p("\nDEGRADED-MODE DECISIONS")
    for d in r["degraded_mode_decisions"] or [{"provider": "-", "status": "-", "action": "none", "effect": ""}]:
        _p(f"  {d['provider']:<11} {d['status']:<12} -> {d['action']}  {d.get('effect') or ''}")
    if r.get("degraded_effects"):
        _p("\nDEGRADED-MODE EFFECTS")
        for k, v2 in r["degraded_effects"].items():
            _p(f"  {k:<42} {v2}")
    if r.get("run_caps"):
        _p("\nRUN CAPS (effective = min(production config, cap); never raised)")
        for k, v2 in r["run_caps"].items():
            _p(f"  {k:<42} production {v2['production']} | cap {v2['cap']} | effective {v2['effective']}")
    _p("\nPLANNED STAGES")
    for i, s in enumerate(r["plan"], 1):
        _p(f"  {i:>2}. {s['stage']:<24} {s['does']}" + (f"  [paid queries <= {s['paid']}]" if s["paid"] else ""))
    _p("\nQUERY BUDGET (before execution)")
    print_budget(r["query_budget"])
    _p(f"  guardrails: {json.dumps(r['guardrails'])}")
    _p(f"  current balance: {r['balance']}")
    _p("\nPRODUCT LIMITS")
    for k, v2 in r["product_limits"].items():
        _p(f"  {k:<36} {v2}")
    _p("\nREPORT DESTINATIONS")
    for k, v2 in r["report_destinations"].items():
        _p(f"  {k:<17} {v2}")
    _p(f"  data paths: {json.dumps(r['data_paths'])}")
    for w in r["warnings"]:
        _p(f"  WARNING: {w}")
    for b in r["blocking_issues"]:
        _p(f"  BLOCKING: {b}")
    _p(f"\nPaid queries executed in this dry run: {r['paid_queries_executed']}")
    _p(f"Manifest: {r['manifest']}")
    _p(f"Dry-run status: {r['status']}")
    _p(f"Live readiness: {r['readiness']['verdict']}" + (f" — {'; '.join(r['readiness']['reasons'])}"
                                                         if r["readiness"]["reasons"] else ""))


def cmd_production_run(a):
    from winning_product_agent import production as P
    if a.live and a.dry_run:
        _p("BLOCKED: --live and --dry-run cannot be combined")
        return 2
    if not a.live and (a.confirm_live is not None or a.resume):
        _p("BLOCKED: --confirm-live / --resume are only valid with --live")
        return 2
    run = P.ProductionRunner(version=a.config_version, profile_path=a.profile, max_products=a.max_products)
    if a.report_only:
        r = run.report_only(a.run_id)
        _p(json.dumps(r, indent=2, default=str))
        return 0
    if not a.live:                                    # default: DRY RUN, never paid
        r = run.dry_run()
        print_production_dry(r)
        return 0 if r["status"] == "DRY_RUN_COMPLETED" else 1
    s = run.live(confirm_value=a.confirm_live, resume_id=a.resume)
    print_summary(s)
    return 0 if s["final_status"] in ("COMPLETED", "PARTIAL") else 1


def cmd_final_validation(a):
    """FINAL STEP: product validation & launch gate (report-only: no order, no supplier contact, no ad spend)."""
    import final_validation as FV
    r = FV.run(product_id=a.product_id)
    m = r["run"]
    if not m:
        _p("No production live run with a final decision yet: run `python -m winning_product_agent production-run "
           "--live` first. Dashboard written (0 candidates).")
    else:
        _p(f"Run {m['run_id']} · config {m['config_version']} · {len(r['results'])} candidate(s)")
    for x in r["results"]:
        _p(f"  {x['state']:<18} {x['name']}  ({x['source_state']})")
        for i, act in enumerate(x["action_queue"][:5], 1):
            _p(f"      {i}. {act}")
    _p(f"Dashboard: {r['dashboard']}")
    for pid, p in r["packs"].items():
        _p(f"Pack {pid}: {p}")
    _p("No order placed, no supplier contacted, no advertising money spent, nothing launched.")
    return 0


def cmd_insights(a):
    """Daily '¿Qué quieres saber hoy?' snapshot (read-only; the only network call is the FREE balance check)."""
    import insights_export as IE
    r = IE.write(with_balance=not a.no_balance)
    _p(f"Insights: {r['html']}\nJSON: {r['json']}\nSource: {r['data']['source_label']}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="python -m winning_product_agent", description="winning-product-agent master runner")
    sub = p.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("preflight")
    pf.add_argument("--tests", action="store_true", help="also run the full test suite")
    pf.add_argument("--profile", help="also validate a run profile")
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="plan only (default)")
    r.add_argument("--live", action="store_true", help="PAID queries (needs exact confirmation)")
    r.add_argument("--profile", help="run profile, e.g. config/runtime_first_live.yaml")
    r.add_argument("--max-products", type=int, help="max products for Deep Analysis (<= profile limit)")
    r.add_argument("--check-balance", action="store_true", help="dry run: FREE balance check")
    r.add_argument("--confirm-live", help=f'non-interactive confirmation; must be exactly "{R.CONFIRMATION_PHRASE}" '
                                          f'("{R.AA_PHRASE}" for the Step AA profile)')
    r.add_argument("--resume", help="resume a stopped live run by run id")
    r.add_argument("--calibration-lane", action="store_true",
                   help="enable the Amazon/BVS calibration lane for THIS run (calibration_only, never ranked)")
    r.add_argument("--from-task", help="Discovery = FREE import of a KaloPilot task already finished (e.g. continued "
                                       "in the web UI after a plan-restriction pause); no new discovery query")
    r.add_argument("--continue-task", help="Discovery = ONE follow-up on an unfinished KaloPilot task (same "
                                           "conversation) asking only for the final JSON")
    st = sub.add_parser("status")
    st.add_argument("run_id", nargs="?")
    rp = sub.add_parser("report")
    rp.add_argument("run_id", nargs="?")
    su = sub.add_parser("suppliers", help="manual supplier offers: import <csv|json> / show <product_id>")
    su.add_argument("action", choices=["import", "show"])
    su.add_argument("target")
    co = sub.add_parser("competitors", help="manual competitor research: import <csv|json> / show <product_id>")
    co.add_argument("action", choices=["import", "show"])
    co.add_argument("target")
    cr = sub.add_parser("creatives", help="creatives: import <csv|json> / from-kalopilot <pid> / show <pid>")
    cr.add_argument("action", choices=["import", "from-kalopilot", "show"])
    cr.add_argument("target")
    de = sub.add_parser("decide", help="Step Z final decision (offline): show / --rebuild / --explain <product_id>")
    de.add_argument("--rebuild", action="store_true", help="recompute from saved LIVE data (no query, no credits)")
    de.add_argument("--explain", help="print the full decision path of one product")
    pr = sub.add_parser("production-run", help="Step AC canonical production command (default: dry run)")
    pr.add_argument("--dry-run", action="store_true", help="plan only (default)")
    pr.add_argument("--live", action="store_true", help=f'PAID queries; needs exactly "{R.PRODUCTION_PHRASE}"')
    pr.add_argument("--profile", help="optional profile that may only LOWER production limits")
    pr.add_argument("--max-products", type=int, help="max products for Deep Analysis (<= production limit)")
    pr.add_argument("--resume", help="resume a stopped production run by run id (same config version)")
    pr.add_argument("--report-only", action="store_true", help="rebuild reports / decision / audit (no query)")
    pr.add_argument("--run-id", help="source run for --report-only (default: latest production live run)")
    pr.add_argument("--confirm-live", help=f'non-interactive confirmation; must be exactly "{R.PRODUCTION_PHRASE}"')
    pr.add_argument("--config-version", help="production config version (default: ACTIVE)")
    pc = sub.add_parser("production-config", help="review | promote | verify [v] | list | activate v")
    pc.add_argument("action", choices=["review", "promote", "verify", "list", "activate"])
    pc.add_argument("version", nargs="?")
    fv = sub.add_parser("final-validation", help="product validation & launch gate (report-only, no paid action)")
    fv.add_argument("--latest", action="store_true", help="use the latest production run (default)")
    fv.add_argument("--product-id", help="validate one product of the latest production run")
    fv.add_argument("--report-only", action="store_true", help="default behaviour: reports only, no paid action")
    ins = sub.add_parser("insights", help="build the '¿Qué quieres saber hoy?' page data (read-only)")
    ins.add_argument("--no-balance", action="store_true", help="skip the free credit-balance check")
    sub.add_parser("calibrate", help="Step AB production calibration (offline, LIVE evidence only, writes proposals)")
    au = sub.add_parser("audit", help="calibration audit of a live run (read-only, no queries)")
    au.add_argument("run_id", nargs="?")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    return {"preflight": cmd_preflight, "run": cmd_run, "status": cmd_status, "report": cmd_report, "audit": cmd_audit,
            "suppliers": cmd_suppliers, "competitors": cmd_competitors,
            "creatives": cmd_creatives, "decide": cmd_decide, "calibrate": cmd_calibrate,
            "production-run": cmd_production_run, "production-config": cmd_production_config,
            "final-validation": cmd_final_validation, "insights": cmd_insights}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
