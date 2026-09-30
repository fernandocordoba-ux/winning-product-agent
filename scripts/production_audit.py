"""Step AC — post-run audit of every production run (no query; a failed audit never re-buys anything).

Checks: config lock, data separation, score sanity, mapping, provenance (data trust), cost / guardrails, secrets,
decision logic. Result: RUN_VALIDATED or RUN_REVIEW_REQUIRED. Writes reports/production/YYYY-MM-DD-post-run-audit.md.
"""
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import calibration_audit as CA  # noqa: E402
import decision_engine as DE  # noqa: E402
import generate_report as GR  # noqa: E402

RUN_VALIDATED, RUN_REVIEW = "RUN_VALIDATED", "RUN_REVIEW_REQUIRED"


def _c(name, ok, detail, items=None, na=False):
    return {"check": name, "status": "NOT_EXERCISED" if na else ("OK" if ok else "FLAG"), "detail": detail,
            "items": items or []}


def audit(r, ctx, now=None):
    now = now or datetime.now(timezone.utc)
    checks = []
    drift = r.drift()
    checks.append(_c("config lock", not drift, f"{r.config_version}: hashes unchanged during the run" if not drift
                     else f"changed: {drift}", drift))
    bad_env = []
    for k in ("deep", "amazon", "bvs"):
        bad_env += [f"{k} {x.get('product_id')}" for x in ctx.get(k) or [] if x.get("data_environment") not in (r.env,)]
    paths_ok = all("production" in str(v) for v in r.rt["paths"].values())
    checks.append(_c("data separation", not bad_env and paths_ok,
                     f"environment {r.env}; production paths {paths_ok}", bad_env))
    ok_deep = r._ok_deep(ctx)
    disc = {str(x["facts"].get("product_id")): x for x in ((ctx.get("discovery") or {}).get("candidates") or [])
            + ((ctx.get("discovery") or {}).get("failed") or [])}
    amz = {str(a.get("product_id")): a for a in ctx.get("amazon") or []}
    bvs = {str(b.get("product_id")): b for b in ctx.get("bvs") or []}
    sanity, mapping = [], []
    for d in ok_deep:
        pid = str(d["product_id"])
        sanity += [f"{pid}: {x}" for x in CA.sanity(d, amz.get(pid), bvs.get(pid), None, None)]
        mapping += [f"{pid}: {x}" for x in CA.mapping_checks(d, disc.get(pid))[1]]
    checks.append(_c("score sanity", not sanity, f"{len(ok_deep)} product(s)", sanity, na=not ok_deep))
    checks.append(_c("mapping", not mapping, f"{len(ok_deep)} product(s)", mapping, na=not ok_deep))
    res = ctx.get("decision") or {"decisions": [], "shortlist": []}
    untr = [f"{d['product_id']}: {d['data_trust']['untraceable']}" for d in res["decisions"]
            if not d["data_trust"]["traceable"]]
    checks.append(_c("provenance", not untr, "every metric used by a decision traceable", untr, na=not res["decisions"]))
    g = r.eff["query_plan"].get("guardrails") or {}
    acc = r.budget.summary()
    cap = r.eff["query_plan"].get("max_credits_for_run")
    spent = (round(r.balance_start - r.balance_end, 2) if None not in (r.balance_start, getattr(r, "balance_end", None))
             else acc.get("actual_credit_cost"))
    cost_items = []
    if g.get("max_queries_per_run") is not None and acc["executed_queries"] > g["max_queries_per_run"]:
        cost_items.append(f"executed {acc['executed_queries']} > max_queries_per_run {g['max_queries_per_run']}")
    if isinstance(spent, (int, float)) and cap is not None and spent > cap:
        cost_items.append(f"credits used {spent} > cap {cap}")
    if r.stop_paid and r.stop_paid[0] not in ("report_only",):
        cost_items.append(f"paid queries stopped: {r.stop_paid[0]} ({r.stop_paid[1]}) — run PARTIAL, data kept")
    checks.append(_c("cost / guardrails", not cost_items,
                     f"executed {acc['executed_queries']}, cached {acc['cached_queries']}, failed {acc['failed_queries']}, "
                     f"blocked {acc['blocked_queries']}; credits used {spent} (cap {cap})", cost_items))
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
    issues = [f"{c['check']}: {'; '.join(map(str, c['items'][:5])) or c['detail']}" for c in checks if c["status"] == "FLAG"]
    result = RUN_VALIDATED if not issues else RUN_REVIEW
    L = [f"# Post-run audit — {now.strftime('%Y-%m-%d')}", "",
         f"**CONFIG VERSION:** {r.config_version} · **RUN ID:** {r.run_id} · **DATA ENVIRONMENT:** {r.env}", "",
         f"**Result: {result}**", "", "| Check | Status | Detail |", "|---|---|---|"]
    L += [f"| {c['check']} | {c['status']} | {c['detail']} |" for c in checks]
    for c in checks:
        if c["items"]:
            L += ["", f"### {c['check']}", ""] + [f"- {x}" for x in c["items"][:50]]
    L += ["", "A failed audit never triggers a new paid query."]
    r.reports_dir.mkdir(parents=True, exist_ok=True)
    n, date = 1, now.strftime("%Y-%m-%d")
    while (r.reports_dir / f"{date}-post-run-audit{'' if n == 1 else f'-{n}'}.md").exists():
        n += 1
    path = r.reports_dir / f"{date}-post-run-audit{'' if n == 1 else f'-{n}'}.md"
    pats = [p.lower() for p in GR.load_cfg()["secret_key_patterns"]]
    path.write_text(GR.scrub("\n".join(L) + "\n", pats, r.secrets))
    return {"result": result, "issues": issues, "checks": checks, "paths": {"post_run_audit": str(path)}}
