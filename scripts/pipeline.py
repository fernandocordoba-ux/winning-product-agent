"""Dry-run pipeline (Step S): preview every stage without any paid/live query.

Stages: pre-flight -> Discovery plan -> Deep Analysis plan -> Amazon plan ->
BVS preparation -> History preparation -> Emerging preparation -> Report preview.
Nothing is written to data/ or reports/; only runs/{run_id}/manifest.json and
runs/{run_id}/log.jsonl are created (redacted, no secrets).

CLI:
  python3 scripts/pipeline.py dry-run                  # no network at all
  python3 scripts/pipeline.py dry-run --check-balance  # + FREE KaloPilot balance check
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import safety  # noqa: E402


def _latest(dir_, pattern):
    files = sorted(Path(dir_).glob(pattern))
    return files[-1] if files else None


def dry_run(root=ROOT, check_balance=False, balance_fn=None, now=None, write_manifest=True, preflight_result=None):
    import amazon_validation as AV
    import business_viability as B
    import deep_analysis as DA
    import discovery as D
    import emerging as EM
    import generate_report as GR
    import history as HIST
    import preflight as PF

    root = Path(root)
    now = now or datetime.now(timezone.utc)
    rt = safety.load_runtime(root / "config" / "runtime.yaml")
    run_id = safety.new_run_id(now)
    run_dir = root / rt["paths"]["runs"] / run_id
    log = safety.RunLogger(run_dir, run_id)
    budget = safety.QueryBudget(run_id, rt["safety"]["estimated_credits_per_query"])
    processed = root / rt["paths"]["processed"]
    raw = root / rt["paths"]["raw"]
    lim = rt["limits"]
    out = {"run_id": run_id, "mode": "DRY_RUN", "timestamp": now.isoformat(), "market": rt["runtime"]["market"],
           "stages": [], "blocking_issues": []}

    def stage(name, fn):
        try:
            res = fn()
            out["stages"].append({"stage": name, "status": "planned", **res})
            log.log(name, "planned", message=res.get("summary"))
        except Exception as e:  # noqa: BLE001  fail safely, keep other stages going
            msg = safety.redact(f"{e.__class__.__name__}: {e}")
            out["stages"].append({"stage": name, "status": "error", "error": msg})
            out["blocking_issues"].append(f"{name}: {msg}")
            log.log(name, "error", error_category=e.__class__.__name__, message=msg)

    pf = preflight_result or PF.preflight(root)
    out["preflight"] = {"status": pf["status"], "blocking_reasons": pf["blocking_reasons"], "warnings": pf["warnings"]}
    out["blocking_issues"] += pf["blocking_reasons"]
    log.log("preflight", pf["status"])

    balance = None
    if check_balance:
        try:
            balance = (balance_fn or (lambda: __import__("kalopilot_client").credits()["totalRemain"]))()
            budget.remaining_balance = balance                      # FREE balance endpoint, no credits spent
        except Exception as e:  # noqa: BLE001
            out["blocking_issues"].append(f"balance check: {safety.redact(str(e))}")

    def s_discovery():
        qs = D.build_queries()
        per = D.load_yaml("filters.yaml")["discovery_mode"]["products_per_category"]
        budget.plan(len(qs))
        for q in qs:
            log.log("discovery", "planned", query_type="discovery", message=q["category_key"])
        return {"planned_queries": len(qs), "categories": [q["category_key"] for q in qs],
                "max_products_requested": len(qs) * per, "max_candidates_kept": lim["discovery_max_products"],
                "cache_hits": 0, "summary": f"{len(qs)} category queries"}

    def s_deep():
        r = DA.run(live=False, client=None, max_products=lim["deep_analysis_max_products"], processed_dir=processed,
                   raw_dir=raw / "deep_analysis", out_dir=processed / "deep_analysis")
        pl = r["plan"]
        budget.plan(pl["paid_queries"] + pl["cache_hits"])
        for _ in range(pl["cache_hits"]):
            budget.record("cached")
        return {"planned_queries": pl["paid_queries"], "cache_hits": pl["cache_hits"], "products": r["selected"],
                "would_process": [i["product_id"] for i in pl["items"] if i["action"] != "skip_duplicate"],
                "skipped": r["skipped"], "summary": f"{r['selected']} products from {Path(r['discovery_file']).name}"}

    def s_amazon():
        cfg = AV.load_cfg()
        cfg["max_products"] = lim["amazon_validation_max_products"]
        r = AV.run(live=False, client=None, cfg=cfg, deep_dir=processed / "deep_analysis",
                   raw_dir=raw / "amazon_validation", out_dir=processed / "amazon_validation")
        pl = r["plan"]
        budget.plan(pl["paid_queries"] + pl["cache_hits"])
        return {"planned_queries": pl["paid_queries"], "cache_hits": pl["cache_hits"], "products": r.get("eligible", 0),
                "skipped": r.get("excluded", []), "note": r.get("note"),
                "summary": r.get("note") or f"{r.get('eligible', 0)} eligible"}

    def s_bvs():
        deep_path = _latest(processed / "deep_analysis", "deep_*.json")
        if not deep_path:
            return {"planned_queries": 0, "products": 0, "note": "no deep-analysis results yet", "summary": "nothing to score"}
        r = B.run(deep_path=deep_path, amazon_path=_latest(processed / "amazon_validation", "amazon_*.json"),
                  raw_dir=raw / "business_viability", save=False)
        return {"planned_queries": 0, "products": r["eligible"], "skipped": r["excluded"],
                "note": "no paid queries; supplier data comes from data/raw/business_viability", "summary": f"{r['eligible']} eligible"}

    def s_history():
        store = HIST.HistoryStore(root / rt["paths"]["history"])
        r = HIST.migrate(processed, store, dry_run=True)
        return {"planned_queries": 0, **{k: r["summary"][k] for k in ("observations_found", "new", "duplicates", "products")},
                "summary": f"{r['summary']['new']} new observations would be appended"}

    def s_emerging():
        store = HIST.HistoryStore(root / rt["paths"]["history"])
        r = EM.detect_all(store, now=now)
        return {"planned_queries": 0, "products": r.get("products_evaluated", 0), "status_counts": r.get("status_counts"),
                "summary": f"{r.get('products_evaluated', 0)} products in history"}

    def s_report():
        store = HIST.HistoryStore(root / rt["paths"]["history"])
        rep = GR.build_report(GR.load_inputs(processed), GR.load_cfg(), now, store if store.identities() else None)
        s = rep["summary"]
        return {"planned_queries": 0, "top": s["top_count"], "watchlist": s["watchlist_count"],
                "rejected": s["rejected_count"], "emerging": len(rep["emerging"]),
                "summary": "preview only (no report file written)"}

    for name, fn in (("discovery", s_discovery), ("deep_analysis", s_deep), ("amazon_validation", s_amazon),
                     ("bvs_preparation", s_bvs), ("history_preparation", s_history),
                     ("emerging_preparation", s_emerging), ("report_preview", s_report)):
        stage(name, fn)

    gate = safety.check_live_query(rt, balance=balance)
    out["live_gate"] = {"allowed": gate.allowed, "reasons": gate.reasons}
    out["query_budget"] = budget.summary()
    out["estimated_query_count"] = sum(s.get("planned_queries", 0) for s in out["stages"])
    out["warnings"] = []
    est = out["query_budget"]["estimated_credit_cost"]
    if isinstance(balance, (int, float)) and isinstance(est, (int, float)) and \
            est > balance - rt["safety"]["min_balance_reserve"]:
        out["warnings"].append(f"planned queries (~{est} credits, configured estimate) exceed the balance {balance} minus "
                               f"reserve {rt['safety']['min_balance_reserve']}: a live run would stop early; reduce limits")
    out["status"] = "READY_FOR_DRY_RUN" if not out["blocking_issues"] else "BLOCKED"
    if write_manifest:
        manifest = {"run_id": run_id, "timestamp": now.isoformat(), "market": rt["runtime"]["market"],
                    "runtime_mode": {**safety.effective_runtime(rt), "mode": "DRY_RUN"},
                    "configured_limits": lim,
                    "eligible_product_counts": {s["stage"]: s.get("products") for s in out["stages"]},
                    "planned_provider_queries": {s["stage"]: s.get("planned_queries", 0) for s in out["stages"]},
                    "cache_status": {s["stage"]: s.get("cache_hits", 0) for s in out["stages"]},
                    "query_budget": out["query_budget"], "live_gate": out["live_gate"],
                    "config_hashes": safety.config_hashes(root / "config"), "status": out["status"],
                    "blocking_issues": out["blocking_issues"]}
        out["manifest"] = str(safety.write_manifest(run_dir, manifest))
        out["log"] = str(log.path)
    return out


def main(argv):
    if len(argv) >= 2 and argv[1] == "dry-run":
        r = dry_run(check_balance="--check-balance" in argv)
        print(f"RUN {r['run_id']}  MODE {r['mode']}  market {r['market']}")
        print(f"Pre-flight: {r['preflight']['status']}")
        for s in r["stages"]:
            extra = f"planned queries {s.get('planned_queries', 0)}, cache hits {s.get('cache_hits', 0)}"
            print(f"  [{s['status']}] {s['stage']:<21} {s.get('summary') or s.get('error')}  ({extra})")
        b = r["query_budget"]
        print(f"Estimated query count: {r['estimated_query_count']} | estimated credits: {b['estimated_credit_cost']} "
              f"({b['estimated_credit_cost_basis']}) | balance: {b['remaining_balance']}")
        print(f"Live gate: {'ALLOWED' if r['live_gate']['allowed'] else 'BLOCKED'} — {'; '.join(r['live_gate']['reasons'])}")
        for w in r.get("warnings", []):
            print(f"  WARNING: {w}")
        for i in r["blocking_issues"]:
            print(f"  BLOCKING: {i}")
        print(f"Manifest: {r.get('manifest')}\nStatus: {r['status']}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
