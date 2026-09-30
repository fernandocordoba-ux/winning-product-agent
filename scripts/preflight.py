"""Pre-flight check (Step S): is the system ready for a DRY RUN?

Returns READY_FOR_DRY_RUN or BLOCKED with explicit reasons, plus a per-component
SYSTEM STATUS. It never makes network calls and never prints credential values.
It never returns READY_FOR_LIVE (live readiness is decided per run by the
Live Query Safety Gate after an explicit user confirmation).

CLI:
  python3 scripts/preflight.py              # checks only
  python3 scripts/preflight.py --tests      # also runs the full test suite
"""
import importlib
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(ROOT))
import config_validation as CV  # noqa: E402
import safety  # noqa: E402

COMPONENTS = {
    "Discovery": {"modules": ["discovery"], "configs": ["filters.yaml", "categories.yaml"], "tests": ["test_discovery"]},
    "Deep Analysis": {"modules": ["deep_analysis"], "configs": ["deep_analysis.yaml"], "tests": ["test_deep_analysis"]},
    "WPS": {"modules": ["score_products"], "configs": ["scoring.yaml"], "tests": ["test_deep_analysis", "test_confidence"]},
    "WPS Confidence": {"modules": ["confidence"], "configs": ["scoring.yaml"], "tests": ["test_confidence"]},
    "Amazon Validation": {"modules": ["amazon_validation"], "configs": ["amazon_validation.yaml"],
                          "tests": ["test_amazon_validation"]},
    "BVS": {"modules": ["business_viability"], "configs": ["business_viability.yaml"], "tests": ["test_business_viability"]},
    "Reports": {"modules": ["generate_report"], "configs": ["report.yaml"], "tests": ["test_generate_report"]},
    "History": {"modules": ["history"], "configs": ["history.yaml"], "tests": ["test_history"]},
    "Emerging Detector": {"modules": ["emerging"], "configs": ["emerging.yaml"], "tests": ["test_emerging"]},
    "Live Query Safety Gate": {"modules": ["safety", "kalopilot_client"], "configs": ["runtime.yaml"],
                               "tests": ["test_safety"]},
    "Supplier Layer": {"modules": ["suppliers"], "configs": ["suppliers.yaml"], "tests": ["test_suppliers"]},
    "Competitor Layer": {"modules": ["competitors"], "configs": ["competitors.yaml"], "tests": ["test_competitors"]},
    "Creative Layer": {"modules": ["creatives"], "configs": ["creatives.yaml"], "tests": ["test_creatives"]},
    "Decision Engine": {"modules": ["decision_engine"], "configs": ["decision.yaml"], "tests": ["test_decision"]},
    "AA End-to-End": {"modules": ["aa_live"], "configs": ["runtime.yaml"], "tests": ["test_aa_live"]},
    "Production Calibration": {"modules": ["production_calibration"], "configs": ["scoring.yaml"],
                               "tests": ["test_production_calibration"]},
    "Config Promotion": {"modules": ["promotion", "config_resolver"], "configs": ["runtime.yaml"],
                         "tests": ["test_production"]},
    "Production Runner": {"modules": ["winning_product_agent.production", "production_audit"],
                          "configs": ["runtime.yaml"], "tests": ["test_production"]},
    "Master Runner": {"modules": ["winning_product_agent.runner"], "configs": ["runtime.yaml"],
                      "tests": ["test_runner", "test_e2e"]},
}


def run_test_suite(test_dir=ROOT / "tests"):
    """Runs every test module; returns per-module results and totals."""
    sys.path.insert(0, str(test_dir))
    loader, results, t0 = unittest.TestLoader(), {}, time.time()
    for f in sorted(Path(test_dir).glob("test_*.py")):
        suite = loader.loadTestsFromName(f.stem)
        r = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        results[f.stem] = {"run": r.testsRun, "failures": len(r.failures), "errors": len(r.errors),
                           "skipped": len(r.skipped), "ok": r.wasSuccessful()}
    tot = {k: sum(v[k] for v in results.values()) for k in ("run", "failures", "errors", "skipped")}
    tot["passed"] = tot["run"] - tot["failures"] - tot["errors"] - tot["skipped"]
    tot["seconds"] = round(time.time() - t0, 2)
    return {"modules": results, "totals": tot}


def _writable(path):
    p = Path(path)
    if not p.is_dir():
        return False, "missing"
    try:
        with tempfile.NamedTemporaryFile(dir=p, prefix=".preflight_", delete=True):
            pass
        return True, "writable"
    except OSError as e:
        return False, f"not writable ({e.__class__.__name__})"


def preflight(root=ROOT, run_tests=False, config_dir=None):
    root = Path(root)
    config_dir = Path(config_dir or root / "config")
    checks, blocking, warnings = [], [], []

    def check(name, ok, detail, block=True):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            (blocking if block else warnings).append(f"{name}: {detail}")

    # 1. configuration present + valid (weights, thresholds, limits, conflicts)
    cfgs, load_errors = CV.load_all(config_dir)
    check("config files present and parseable", not load_errors, "; ".join(load_errors) or f"{len(cfgs)} files")
    val_errors = CV.validate(cfgs) if not load_errors else []
    check("config schema valid (weights, thresholds, ranges)", not [e for e in val_errors if "limit conflict" not in e],
          "; ".join(e for e in val_errors if "limit conflict" not in e) or "all totals and ranges valid")
    check("no conflicting configuration", not [e for e in val_errors if "limit conflict" in e],
          "; ".join(e for e in val_errors if "limit conflict" in e) or "runtime.yaml limits agree with stage configs")

    # 2. runtime safety flags must be the SAFE defaults in the file
    rt = cfgs.get("runtime.yaml") or {}
    r = rt.get("runtime") or {}
    safe = r.get("live_mode") is False and r.get("dry_run") is True and r.get("explicit_live_confirmation") is False
    check("runtime safety flags are safe defaults", safe,
          f"live_mode={r.get('live_mode')}, dry_run={r.get('dry_run')}, "
          f"explicit_live_confirmation={r.get('explicit_live_confirmation')}" + ("" if safe else
          " (runtime.yaml must keep safe defaults; live is enabled per run, in memory, after confirmation)"))
    lim = rt.get("limits") or {}
    check("query limits valid", all(isinstance(lim.get(k), int) and lim.get(k) > 0 for k in lim) and bool(lim),
          ", ".join(f"{k}={v}" for k, v in lim.items()))

    # 3. provider integration + credentials (value never shown)
    check("KaloPilot integration configured",
          (root / "scripts" / "kalopilot_client.py").exists() and (root / "kalopilot" / "scripts" / "pilot.sh").exists(),
          "scripts/kalopilot_client.py + vendored kalopilot skill")
    creds = safety.credentials_present(rt) if rt else False
    check("provider credentials detected", creds, "present (value hidden)" if creds else
          "not found (needed only for live runs)", block=False)

    # 4. directories
    paths = rt.get("paths") or {}
    for label in ("raw", "processed", "history", "reports", "runs", "cache"):
        ok, why = _writable(root / paths.get(label, label))
        check(f"{label} directory writable", ok, f"{paths.get(label, label)}: {why}")

    # 5. modules import
    import_errors = {}
    for comp in COMPONENTS.values():
        for m in comp["modules"]:
            try:
                importlib.import_module(m)
            except Exception as e:  # noqa: BLE001
                import_errors[m] = f"{e.__class__.__name__}: {e}"
    check("all pipeline modules import", not import_errors, "; ".join(f"{k}: {v}" for k, v in import_errors.items())
          or "ok")

    # 6. live gate blocks with the current file config
    gate = safety.check_live_query(rt, credentials_ok=True, balance=1e9) if rt else None
    check("Live Query Safety Gate blocks by default", gate is not None and not gate.allowed,
          "; ".join(gate.reasons) if gate else "runtime.yaml unavailable")

    # 7. tests
    tests = run_test_suite(root / "tests") if run_tests else None
    if tests:
        t = tests["totals"]
        check("test suite", t["failures"] == 0 and t["errors"] == 0,
              f"{t['passed']} passed, {t['failures']} failed, {t['errors']} errors, {t['skipped']} skipped in {t['seconds']}s")

    # SYSTEM STATUS per component
    status = {}
    for name, comp in COMPONENTS.items():
        reasons = []
        reasons += [f"import {m}" for m in comp["modules"] if m in import_errors]
        reasons += [e for e in load_errors + val_errors if any(c in e for c in comp["configs"])]
        if tests:
            reasons += [f"{t} failing" for t in comp["tests"] if t in tests["modules"] and not tests["modules"][t]["ok"]]
            reasons += [f"{t} missing" for t in comp["tests"] if t not in tests["modules"]]
        if name == "Live Query Safety Gate" and (gate is None or gate.allowed):
            reasons.append("gate does not block with default config")
        status[name] = {"status": "READY" if not reasons else "NOT READY", "reasons": reasons}
    overall = "READY_FOR_DRY_RUN" if not blocking and all(v["status"] == "READY" for v in status.values()) else "BLOCKED"
    return {"status": overall, "blocking_reasons": blocking, "warnings": warnings, "checks": checks,
            "system_status": status, "tests": tests,
            "note": "READY_FOR_LIVE is never returned by pre-flight; live runs need the per-run gate + user confirmation."}


def main(argv):
    res = preflight(run_tests="--tests" in argv)
    for c in res["checks"]:
        print(f"  [{'OK' if c['ok'] else 'XX'}] {c['check']}: {c['detail']}")
    print("\nSYSTEM STATUS")
    for k, v in res["system_status"].items():
        print(f"  {k + ':':<24} {v['status']}" + (f"  ({'; '.join(v['reasons'])})" if v["reasons"] else ""))
    for w in res["warnings"]:
        print(f"  WARNING: {w}")
    for b in res["blocking_reasons"]:
        print(f"  BLOCKING: {b}")
    print(f"\nOverall: {res['status']}")
    return 0 if res["status"] == "READY_FOR_DRY_RUN" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
