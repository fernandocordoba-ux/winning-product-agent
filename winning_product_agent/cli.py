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


def cmd_audit(a):
    import calibration_audit as CA
    r = CA.audit(a.run_id)
    _p(f"Calibration audit: {r['paths']['markdown']}")
    for x in r["decision_reasons"]:
        _p(f"  - {x}")
    _p(r["decision"])
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
    r.add_argument("--confirm-live", help=f'non-interactive confirmation; must be exactly "{R.CONFIRMATION_PHRASE}"')
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
    au = sub.add_parser("audit", help="calibration audit of a live run (read-only, no queries)")
    au.add_argument("run_id", nargs="?")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    return {"preflight": cmd_preflight, "run": cmd_run, "status": cmd_status, "report": cmd_report, "audit": cmd_audit}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
