"""Step AC — config promotion + production runner. SYNTHETIC data only: temp config roots, temp data roots,
in-memory fake provider, network blocked. The real config/production/ is never modified by these tests."""
import copy
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import config_resolver as CRS  # noqa: E402
import decision_engine as DE  # noqa: E402
import promotion as PROMO  # noqa: E402
import safety  # noqa: E402
import test_runner as T  # noqa: E402
from winning_product_agent import cli  # noqa: E402
from winning_product_agent import production as P  # noqa: E402
from winning_product_agent import runner as R  # noqa: E402

AB = {"generated_at": "2026-09-30T00:00:00+00:00", "result": "MORE_CALIBRATION_REQUIRED",
      "confidence_calibration": {"products": [{"double_penalties": [1]}, {"double_penalties": []}]},
      "decision_calibration": {"products": [{"reject_on_missing_evidence": True,
                                             "negative_evidence": ["gate:CREATOR_DEPENDENCY_WEAK_BROADER"]}]},
      "creative_calibration": {"passing_minimum_without_classification": 2, "products": 3},
      "cost_efficiency": {"observed_cost_per_query": {"deep_answer_ok": {"n": 9, "max": 3.57},
                                                       "discovery_answer": {"n": 3, "max": 3.98}}}}
REPLAY_OK = (True, {"blocked": False, "issues": []}, None)


def _rw(path):
    for p in Path(path).rglob("*"):
        try:
            p.chmod(0o755 if p.is_dir() else 0o644)
        except OSError:
            pass


class ConfigRoot(unittest.TestCase):
    """A temp project root with a copy of config/ (no production/) and the real proposed files."""

    def setUp(self):
        safety.clear_run_overrides()
        self.root = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "config", self.root / "config",
                        ignore=shutil.ignore_patterns("production"))
        _rw(self.root / "config")
        (self.root / "reports").mkdir()
        (self.root / "reports" / "production-calibration.md").write_text("# AB (synthetic copy)\n")
        self.data = Path(tempfile.mkdtemp())
        self._p = [mock.patch("urllib.request.urlopen", T.no_network),
                   mock.patch.dict(os.environ, {"KALOPILOT_TOKEN": T.FAKE})]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        safety.clear_run_overrides()
        for d in (self.root, self.data):
            _rw(d)
            shutil.rmtree(d, ignore_errors=True)

    def review(self):
        return PROMO.review(self.root, ab=AB, replay=REPLAY_OK)

    def promote(self, **kw):
        return PROMO.promote(self.root, rev=self.review(), replay_final=False, **kw)

    def runner(self, client=None, version_dir=None, **kw):
        d = version_dir or PROMO.active_dir(self.root)
        return P.ProductionRunner(root=ROOT, data_root=self.data, config_dir=d, client=client,
                                  preflight_fn=lambda: T.READY, out=lambda s="": None, **kw)

    def live(self, client, confirm=R.PRODUCTION_PHRASE, **kw):
        r = self.runner(client, isatty=False, **kw)
        return r, r.live(confirm_value=confirm)


# ============================================================================ change review
class ChangeReview(ConfigRoot):
    def test_approve_reject_defer(self):
        rows = {(c["file"], c["path"]): c for c in self.review()["changes"]}
        self.assertEqual(rows[("decision_rules_v2.yaml", "minimum_confidence.creative")]["approval_status"], PROMO.APPROVE)
        self.assertEqual(rows[("decision_rules_v2.yaml", "hard_gates.CREATOR_DEPENDENCY_WEAK_BROADER.broader_weak_statuses")]
                         ["approval_status"], PROMO.APPROVE)
        self.assertEqual(rows[("decision_rules_v2.yaml", "hard_gates.VIDEO_DEPENDENCY_WEAK_BROADER.broader_weak_statuses")]
                         ["approval_status"], PROMO.DEFER)                    # same logic, no live evidence
        for c in rows.values():
            for k in ("current_value", "proposed_value", "reason", "supporting_live_evidence", "expected_effect", "risk",
                      "approval_status"):
                self.assertIn(k, c)

    def test_reject_lowered_requirement_and_limit_increase(self):
        c = PROMO.assess("decision_rules_v2.yaml", "minimum_confidence.bvs", 50, 30, AB, True, True)
        self.assertEqual(c["approval_status"], PROMO.REJECT)
        self.assertFalse(c["checks"]["no_lowered_confidence_requirement"])
        c = PROMO.assess("runtime_production_v1.yaml", "limits.deep_analysis_max_products", 5, 40, AB, True, True)
        self.assertEqual(c["approval_status"], PROMO.REJECT)
        c = PROMO.assess("runtime_production_v1.yaml", "query_plan.estimated_credits.deep_analysis", 2.5, 2.0, AB, True, True)
        self.assertEqual(c["approval_status"], PROMO.REJECT)                  # below observed max weakens the cap
        c = PROMO.assess("competitors.yaml", "relationships.direct_min", 75, 60, AB, True, True)
        self.assertEqual(c["approval_status"], PROMO.REJECT)

    def test_reject_when_replay_or_regression_fails(self):
        c = PROMO.assess("decision_rules_v2.yaml", "minimum_confidence.creative", 50, 65, AB, True, False)
        self.assertEqual(c["approval_status"], PROMO.REJECT)
        c = PROMO.assess("decision_rules_v2.yaml", "minimum_confidence.creative", 50, 65, AB, False, True)
        self.assertEqual(c["approval_status"], PROMO.REJECT)

    def test_defer_unknown_change(self):
        c = PROMO.assess("scoring_v2.yaml", "output.rounding", 2, 3, AB, True, True)
        self.assertEqual(c["approval_status"], PROMO.DEFER)


# ============================================================================ promotion / hashes / immutability
class Promotion(ConfigRoot):
    def test_promotion_writes_versioned_set(self):
        r = self.promote()
        d = Path(r["dir"])
        self.assertEqual(d.name, "v1")
        for f in ("scoring.yaml", "filters.yaml", "runtime.yaml", "decision_rules.yaml", "provider_capabilities.yaml",
                  "supplier.yaml", "competitor.yaml", "creative.yaml", "emerging.yaml"):
            meta = PROMO._yaml(d / f)["production_meta"]
            for k in ("config_version", "created_at", "source_calibration_report", "change_summary"):
                self.assertTrue(meta.get(k), (f, k))
        m = json.loads((d / "manifest.json").read_text())
        for k in ("config_version", "files", "promotion_timestamp", "source_calibration_version", "decision_rules_version"):
            self.assertIn(k, m)
        dec = PROMO._yaml(d / "decision_rules.yaml")
        self.assertEqual(dec["minimum_confidence"]["creative"], 65)                       # approved
        self.assertEqual(dec["hard_gates"]["CREATOR_DEPENDENCY_WEAK_BROADER"]["broader_weak_statuses"], ["WEAK"])
        self.assertNotIn("broader_weak_statuses", dec["hard_gates"]["VIDEO_DEPENDENCY_WEAK_BROADER"])  # deferred
        rt = PROMO._yaml(d / "runtime.yaml")
        self.assertEqual((rt["runtime"]["live_mode"], rt["runtime"]["dry_run"]), (False, True))   # safe default
        self.assertTrue(all("production" in v for v in rt["paths"].values()))
        for k in ("cache_policy", "provider_retry", "credit_safety", "history_settings", "report_settings",
                  "degraded_mode"):
            self.assertIn(k, rt)
        self.assertEqual(rt["e2e"]["confirmation_phrase"], R.PRODUCTION_PHRASE)
        self.assertEqual(PROMO.active_version(self.root), "v1")

    def test_hashes_deterministic_and_verified(self):
        d = Path(self.promote()["dir"])
        v = PROMO.verify(d)
        self.assertTrue(v["ok"])
        self.assertEqual(v["hashes"], PROMO.verify(d)["hashes"])
        f = d / "scoring.yaml"
        f.chmod(0o644)
        f.write_text(f.read_text() + "\n# edited\n")
        v2 = PROMO.verify(d)
        self.assertFalse(v2["ok"])
        self.assertEqual(v2["changed"], ["scoring.yaml"])

    def test_immutable(self):
        d = Path(self.promote()["dir"])
        self.assertFalse(os.stat(d / "scoring.yaml").st_mode & stat.S_IWUSR)
        with self.assertRaises(FileExistsError):
            self.promote(version="v1")
        self.assertEqual(PROMO.next_version(self.root), "v2")

    def test_rollback_keeps_newer_versions(self):
        self.promote()
        v2 = self.promote()
        self.assertEqual(v2["version"], "v2")
        PROMO.activate(self.root, "v2")
        rec = PROMO.activate(self.root, "v1", note="rollback")
        self.assertEqual((rec["active_version"], rec["previous_version"]), ("v1", "v2"))
        self.assertTrue((self.root / "config" / "production" / "v2").exists())
        r = self.runner()
        self.assertEqual(r.config_version, "production-v1")
        log = (self.root / "config" / "production" / "activation_log.jsonl").read_text().splitlines()
        self.assertEqual(len(log), 3)

    def test_invalid_set_is_not_promoted(self):
        rev = self.review()
        rt = self.root / "config" / "runtime_aa_live.yaml"
        rt.write_text(rt.read_text().replace("market: US", "market: XX"))
        with self.assertRaises(ValueError):
            PROMO.promote(self.root, rev=rev, replay_final=False)
        self.assertFalse((self.root / "config" / "production" / "v1").exists())


# ============================================================================ production runner
class Runner(ConfigRoot):
    def setUp(self):
        super().setUp()
        self.promote()

    def test_dry_run_default_cli(self):
        calls = []

        class Stub:
            def __init__(self, **kw):
                calls.append("init")

            def dry_run(self):
                calls.append("dry_run")
                return {"status": "DRY_RUN_COMPLETED"}

            def live(self, **kw):
                raise AssertionError("production-run must never default to live")
        with mock.patch.object(P, "ProductionRunner", Stub), mock.patch.object(cli, "print_production_dry", lambda r: None):
            self.assertEqual(cli.main(["production-run"]), 0)
        self.assertIn("dry_run", calls)

    def test_dry_run_shows_everything_and_makes_no_query(self):
        fake = T.FakeProvider()
        d = self.runner(fake).dry_run()
        self.assertEqual(fake.submits, [])
        self.assertEqual(d["paid_queries_executed"], 0)
        self.assertEqual(d["config_version"], "production-v1")
        self.assertEqual(set(d["provider_health"]), {"kalopilot", "amazon", "supplier", "competitor", "creative"})
        self.assertIn("degraded_mode_decisions", d)
        self.assertIn("post_run_audit", [s["stage"] for s in d["plan"]])
        self.assertIn("reports/production", d["report_destinations"]["final_decision"])
        self.assertEqual(d["status"], "DRY_RUN_COMPLETED")
        self.assertNotIn(T.FAKE, json.dumps(d, default=str))

    def test_live_confirmation_exact(self):
        for typed in (R.CONFIRMATION_PHRASE, R.AA_PHRASE, "confirm production live run", "CONFIRM PRODUCTION LIVE RUN ",
                      "yes", None):
            fake = T.FakeProvider()
            _, s = self.live(fake, confirm=typed)
            self.assertEqual(s["final_status"], "BLOCKED", typed)
            self.assertEqual(fake.submits, [], typed)
        self.assertFalse(safety.effective_runtime()["explicit_live_confirmation"])

    def test_provider_unavailable_blocks(self):
        class Down(T.FakeProvider):
            def credits(self):
                raise ConnectionError("down")
        fake = Down()
        r, s = self.live(fake)
        self.assertEqual(s["final_status"], "BLOCKED")
        self.assertEqual(fake.submits, [])
        self.assertEqual(r.health["providers"]["kalopilot"]["status"], P.UNAVAILABLE)
        self.assertTrue(any("BLOCK_RUN" in b for b in s["blocking_reasons"]))

    def test_degraded_mode_and_full_run(self):
        fake = T.FakeProvider(consistent=True)
        r, s = self.live(fake)
        self.assertIn(s["final_status"], ("COMPLETED", "PARTIAL"))
        dm = {x["provider"]: x["action"] for x in r.health["degraded_mode_decisions"]}
        self.assertEqual(dm["competitor"], "CONTINUE_REDUCED_CONFIDENCE")
        self.assertEqual(dm["supplier"], "CONTINUE")
        js = json.loads(Path(s["outputs"]["final_decision_json"]).read_text())
        decs = [d for k in ("ready_products", "promising_products", "watchlist", "rejected_products", "insufficient_data")
                for d in js[k]]
        self.assertTrue(decs)
        for d in decs:
            self.assertEqual(d["decision_confidence"]["components"].get("degraded_mode_penalty"), -10)
            self.assertNotEqual(d["decision_state"], DE.READY)            # no supplier economics -> never READY
        self.assertLessEqual(len(js["shortlist"]), 3)
        m = json.loads((r.run_dir / "manifest.json").read_text())
        self.assertEqual(m["config_version"], "production-v1")
        for k, v in PROMO.verify(r.config_dir)["hashes"].items():
            self.assertEqual(m["config_hashes_at_start"][k], v)
        self.assertIn(m["post_run_audit"]["result"], (P.RUN_VALIDATED, P.RUN_REVIEW))

    def test_production_report(self):
        r, s = self.live(T.FakeProvider(consistent=True))
        rep = self.data / "reports" / "production"
        for name in ("latest-winning-products.md", "latest-final-decision.md"):
            t = (rep / name).read_text()
            for tag in ("CONFIG VERSION", "RUN ID", "DATA ENVIRONMENT"):
                self.assertIn(tag, t)
        self.assertTrue(list(rep.glob("20*-winning-products.md")))
        self.assertTrue(list(rep.glob("20*-final-decision.md")))
        self.assertTrue(list(rep.glob("20*-run-audit.md")))

    def test_cost_cap_partial_run(self):
        fake = T.FakeProvider(consistent=True)
        r = self.runner(fake, isatty=False)
        r.eff["query_plan"]["guardrails"]["max_queries_per_run"] = 2
        s = r.live(confirm_value=R.PRODUCTION_PHRASE)
        self.assertEqual(len(fake.submits), 2)                           # stopped new paid queries
        self.assertEqual(s["final_status"], "PARTIAL")
        self.assertTrue(Path(s["outputs"]["report_markdown"]).exists())  # completed data kept, report written
        m = json.loads((r.run_dir / "manifest.json").read_text())
        self.assertEqual(m["post_run_audit"]["result"], P.RUN_REVIEW)
        self.assertTrue(any("query_cap" in i for i in m["post_run_audit"]["issues"]))

    def test_config_drift_before_run_blocks(self):
        d = PROMO.active_dir(self.root)
        f = d / "filters.yaml"
        f.chmod(0o644)
        f.write_text(f.read_text() + "\n# drift\n")
        fake = T.FakeProvider()
        _, s = self.live(fake)
        self.assertEqual(s["final_status"], "BLOCKED")
        self.assertEqual(fake.submits, [])

    def test_config_drift_during_run_invalidates(self):
        d = PROMO.active_dir(self.root)

        class Drifting(T.FakeProvider):
            def wait(self, task_id):
                f = d / "filters.yaml"
                if os.stat(f).st_mode & stat.S_IWUSR == 0:
                    f.chmod(0o644)
                    f.write_text(f.read_text() + "\n# changed mid-run\n")
                return super().wait(task_id)
        fake = Drifting(consistent=True)
        r, s = self.live(fake)
        m = json.loads((r.run_dir / "manifest.json").read_text())
        self.assertIn("CONFIG_DRIFT", m.get("run_invalid", ""))
        self.assertEqual(len(fake.submits), 1)                           # nothing paid after the drift

    def test_production_calibration_and_synthetic_separation(self):
        self.assertEqual(self.runner().environment_for(type("C", (), {"data_environment": R.LIVE})()), P.PRODUCTION)
        self.assertFalse(R.raw_env_ok({"data_environment": R.LIVE}, P.PRODUCTION))   # calibration cache never reused
        self.assertFalse(R.raw_env_ok({}, P.PRODUCTION))
        r, s = self.live(T.FakeProvider(consistent=True))
        written = [p.relative_to(self.data) for p in self.data.rglob("*") if p.is_file()]
        self.assertTrue(written)
        for p in written:
            self.assertTrue(str(p).split("/")[0] in ("data", "runs", "reports") and "production" in str(p), p)
        with self.assertRaises(ValueError):                              # synthetic data may never enter real data
            P.ProductionRunner(root=ROOT, data_root=ROOT, config_dir=PROMO.active_dir(self.root),
                               client=T.FakeProvider(), preflight_fn=lambda: T.READY, isatty=False,
                               out=lambda s="": None).live(confirm_value=R.PRODUCTION_PHRASE)

    def test_production_history_append_only(self):
        r, s = self.live(T.FakeProvider(consistent=True))
        files = list((self.data / "data" / "production" / "history" / "decisions").glob("*.jsonl"))
        self.assertTrue(files)
        before = {f: f.read_text() for f in files}
        r2, s2 = self.live(T.FakeProvider(consistent=True))
        for f, t in before.items():
            self.assertTrue(f.read_text().startswith(t))                 # never rewritten

    def test_report_only_makes_no_query(self):
        self.live(T.FakeProvider(consistent=True))
        fake = T.FakeProvider()
        r = self.runner(fake)
        out = r.report_only()
        self.assertEqual(fake.submits, [])
        self.assertEqual(out["final_status"], "REPORT_ONLY_COMPLETED")
        self.assertTrue(Path(out["outputs"]["final_decision_json"]).exists())

    def test_profile_cannot_raise_limits(self):
        prof = self.root / "p.yaml"
        prof.write_text("limits:\n  deep_analysis_max_products: 40\n")
        r = self.runner(profile_path=str(prof))
        self.assertTrue(any("exceeds production" in e for e in r.profile_errors))

    def test_resolver_restored_after_run(self):
        self.runner(T.FakeProvider()).dry_run()
        self.assertIsNone(CRS.active_dir())
        self.assertEqual(CRS.path("scoring.yaml"), ROOT / "config" / "scoring.yaml")


class PostRunAudit(ConfigRoot):
    def test_shortlist_and_decision_audit(self):
        self.promote()
        r, s = self.live(T.FakeProvider(consistent=True))
        ck = json.loads((r.run_dir / "checkpoints" / "post_run_audit.json").read_text())
        names = {c["check"] for c in ck["checks"]}
        for n in ("config lock", "data separation", "score sanity", "mapping (critical fields)", "provenance",
                  "cost / guardrails", "secrets", "decision", "query limits", "confidence behavior",
                  "competition taxonomy (scope / comparability)", "history deduplication", "reports generated",
                  "supplier economics never fabricated", "no synthetic/live contamination"):
            self.assertIn(n, names)
        self.assertEqual(ck["result"], P.RUN_VALIDATED, ck["issues"])          # clean synthetic run validates
        self.assertEqual(next(c for c in ck["checks"] if c["check"] == "secrets")["status"], "OK")


class FirstProductionRun(ConfigRoot):
    """Step AD additions: run caps, manifest copy, health details, validation packs, run audit."""
    AD = ROOT / "config" / "run_profiles" / "ad_first_production.yaml"

    def setUp(self):
        super().setUp()
        self.promote()

    def test_caps_never_raise_production_limits(self):
        r = self.runner(profile_path=str(self.AD))
        self.assertEqual(r.profile_errors, [])
        prod = PROMO._yaml(PROMO.active_dir(self.root) / "runtime.yaml")
        for k, v in prod["limits"].items():
            self.assertLessEqual(r.limits[k], v)
        self.assertEqual(r.eff["limits"]["discovery_max_products"], min(30, prod["limits"]["discovery_max_products"]))
        self.assertEqual(r.eff["run_caps"]["limits.deep_analysis_max_products"]["effective"],
                         min(10, prod["limits"]["deep_analysis_max_products"]))
        self.assertTrue(r.e2e["first_production_run"])

    def test_health_details_and_degraded_effects(self):
        d = self.runner(T.FakeProvider(), profile_path=str(self.AD)).dry_run()
        kp = d["provider_health"]["kalopilot"]
        self.assertEqual(kp["authentication"], "AUTHENTICATED")
        for k in ("capability", "last_successful_query"):
            self.assertIn(k, kp)
        for k in ("WPS", "AVS", "BVS", "Competitor Intelligence", "Creative Intelligence", "Decision Confidence",
                  "READY_FOR_PRODUCT_VALIDATION eligibility"):
            self.assertIn(k, d["degraded_effects"])
        self.assertIn("run-audit", d["report_destinations"]["run_audit"])

    def test_first_run_manifest_audit_and_reports(self):
        r, s = self.live(T.FakeProvider(consistent=True), profile_path=str(self.AD))
        mirror = self.data / "data" / "production" / "runs" / r.run_id / "manifest.json"
        m = json.loads(mirror.read_text())
        for k in ("run_id", "started_at", "config_version", "config_hashes_at_start", "market", "data_environment",
                  "provider_health", "limits", "query_budget", "runtime_mode"):
            self.assertIn(k, m)
        self.assertNotIn(T.FAKE, mirror.read_text())
        ck = json.loads((r.run_dir / "checkpoints" / "post_run_audit.json").read_text())
        self.assertIn(ck["first_production_run_status"], ("FIRST_PRODUCTION_RUN_VALIDATED", "PRODUCTION_RUN_REVIEW_REQUIRED"))
        for k in ("Discovered", "PASS", "Deep Analyzed", "Shortlisted", DE.READY, DE.INSUFFICIENT):
            self.assertIn(k, ck["distribution"])
        for k in ("GMV", "supplier cost", "creative intelligence"):
            self.assertIn(k, ck["data_quality"]["fields"])
        self.assertIn("cost_per_deep_analysis", ck["cost_audit"])
        rep = self.data / "reports" / "production"
        for pat in ("20*-winning-products.md", "20*-winning-products.json", "20*-final-decision.md", "20*-run-audit.md"):
            self.assertTrue(list(rep.glob(pat)), pat)

    def test_validation_pack_for_ready_product(self):
        import test_decision as TD
        import validation_pack as VP
        r = self.runner()
        r.run_id, r.env, r.secrets = "RUN1", "PRODUCTION", [T.FAKE]
        p = TD.product(env="PRODUCTION")
        d = DE.decide(DE.build_evidence(p, TD.comp(), TD.crea(), "PRODUCTION"), DE.load_cfg())
        self.assertEqual(d["decision_state"], DE.READY)
        path = VP.write(r, d, p, {"competitor": {}, "creative": {}})
        t = path.read_text()
        self.assertTrue(path.name.endswith("-validation-pack.md"))
        self.assertEqual(path.parent.parent, r.reports_dir)
        for h in ("Product overview", "TikTok evidence", "Amazon evidence", "Supplier comparison", "Product economics",
                  "Competitor intelligence", "Creative intelligence", "Historical momentum", "Decision matrix",
                  "Risk checklist", "Manual validation checklist", "CONFIG VERSION", "RUN ID", "DATA ENVIRONMENT"):
            self.assertIn(h, t)


if __name__ == "__main__":
    unittest.main()
