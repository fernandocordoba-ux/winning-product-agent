"""Step AA — Controlled End-to-End Live Run. SYNTHETIC data only: in-memory fake provider, network blocked,
temp workspace (never the real project data)."""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import aa_live as AA  # noqa: E402
import competitors as CI  # noqa: E402
import decision_engine as DE  # noqa: E402
import safety  # noqa: E402
import suppliers as S  # noqa: E402
import test_competitors as TC  # noqa: E402
import test_decision as TD  # noqa: E402
import test_runner as T  # noqa: E402
import test_suppliers as TS  # noqa: E402
from winning_product_agent import cli  # noqa: E402
from winning_product_agent import runner as R  # noqa: E402

PROFILE = "config/runtime_aa_live.yaml"


class Base(unittest.TestCase):
    def setUp(self):
        safety.clear_run_overrides()
        self.tmp = Path(tempfile.mkdtemp())
        self._p = [mock.patch("urllib.request.urlopen", T.no_network),
                   mock.patch.dict(os.environ, {"KALOPILOT_TOKEN": T.FAKE})]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        safety.clear_run_overrides()
        for p in self.tmp.rglob("*"):
            try:
                p.chmod(0o755 if p.is_dir() else 0o644)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def runner(self, client=None, **kw):
        return R.Runner(profile_path=PROFILE, root=ROOT, data_root=self.tmp, client=client,
                        preflight_fn=lambda: T.READY, out=lambda s="": None, **kw)

    def live(self, client, confirm=R.AA_PHRASE, **kw):
        r = self.runner(client, isatty=False, **kw)
        return r, r.live(confirm_value=confirm)

    def import_layers(self, key="E1", offers=7, comps=12):
        pid, name = T.E.PID[key], T.E.NAMES[key]
        rows = [TS.row(matched_product_id=pid, supplier_name=f"Sup {i}", supplier_url=f"https://example.test/s{i}",
                       supplier_product_title=name, product_cost=8.0 + i, shipping_cost=2.5, estimated_delivery_max_days=9,
                       warehouse="yes") for i in range(offers)]
        f = self.tmp / "offers.json"
        f.write_text(json.dumps(rows))
        S.import_file(f, raw_dir=self.tmp / "data/raw/suppliers", processed_dir=self.tmp / "data/processed/suppliers",
                      hist_dir=self.tmp / "data/history/suppliers")
        crow = [TC.row(i, matched_product_id=pid, product_title=name) for i in range(1, comps + 1)]
        f = self.tmp / "comps.json"
        f.write_text(json.dumps(crow))
        CI.import_file(f, raw_dir=self.tmp / "data/raw/competitors", processed_dir=self.tmp / "data/processed/competitors")
        return pid


# ============================================================================ Stage 2 — profile
class Profile(Base):
    def test_profile_limits(self):
        r = self.runner()
        self.assertEqual(r.profile_errors, [])
        lim, e = r.limits, r.e2e
        self.assertEqual((lim["discovery_max_products"], lim["deep_analysis_max_products"],
                          lim["amazon_validation_max_products"]), (20, 5, 3))
        self.assertEqual((e["supplier_products_max"], e["supplier_offers_per_product_max"], e["competitor_products_max"],
                          e["competitors_per_product_max"], e["creative_products_max"], e["creatives_per_product_max"],
                          e["final_decision_max_products"]), (3, 5, 3, 10, 3, 20, 5))
        self.assertEqual(r.eff["runtime"]["market"], "US")
        self.assertEqual(r.stage_list, R.AA_STAGES)

    def test_invalid_e2e_rejected(self):
        rt, eff, _ = R.load_profile(PROFILE)
        eff["e2e"] = {**eff["e2e"], "supplier_products_max": 0, "confirmation_phrase": "yes"}
        errs = R.validate_effective(rt, eff)
        self.assertTrue(any("supplier_products_max" in x for x in errs))
        self.assertTrue(any("confirmation_phrase" in x for x in errs))


# ============================================================================ Stages 3-4 — budget + confirmation
class BudgetAndConfirmation(Base):
    def test_budget_by_provider(self):
        b = self.runner(T.FakeProvider()).dry_run()["query_budget"]
        prov = b["providers"]
        self.assertEqual(len(prov), 5)
        for n in ("Suppliers", "Competitors", "Creatives"):
            self.assertEqual(prov[n]["expected_paid_max"], 0)
            self.assertEqual(prov[n]["estimated_credits_max"], 0.0)
        kp = prov["KaloPilot (TikTok Shop: discovery + deep analysis)"]
        self.assertEqual(kp["expected_paid_max"], 1 + 5)
        self.assertEqual(kp["estimated_credits_max"], 4.0 + 5 * 2.5)
        self.assertEqual(prov["Amazon (via KaloPilot Amazon validation)"]["expected_paid_max"], 2)   # 3 products, 2/query

    def test_unknown_cost_stays_unknown(self):
        r = self.runner()
        r.eff["query_plan"]["estimated_credits"]["amazon_validation"] = None
        b = r.query_budget(R.LIVE, R.now_utc())
        self.assertEqual(b["providers"]["Amazon (via KaloPilot Amazon validation)"]["estimated_credits_max"],
                         safety.UNKNOWN)

    def test_only_exact_aa_phrase(self):
        for typed in (R.CONFIRMATION_PHRASE, "confirm aa live run", "CONFIRM AA LIVE RUN ", "yes", "Confirm AA live run"):
            fake = T.FakeProvider()
            _, s = self.live(fake, confirm=typed)
            self.assertEqual(s["final_status"], "BLOCKED", typed)
            self.assertEqual(fake.submits, [], typed)

    def test_dry_run_makes_no_query(self):
        fake = T.FakeProvider()
        d = self.runner(fake).dry_run(check_balance=True)
        self.assertEqual(fake.submits, [])
        self.assertEqual(d["paid_queries_executed"], 0)


# ============================================================================ Stages 5-20 — full run
class FullRun(Base):
    def test_end_to_end_with_all_layers(self):
        pid = self.import_layers("E1", offers=7, comps=12)
        fake = T.FakeProvider(consistent=True)
        r, s = self.live(fake)
        st = s["stages"]
        for name in R.AA_STAGES[1:-1]:
            self.assertIn(st[name], ("COMPLETED", "PARTIAL", "SKIPPED"), name)
        ck = r._load_checkpoint("supplier_research")
        row = next(x for x in ck["products"] if x["product_id"] == pid)
        self.assertEqual((row["offers_available"], row["offers_used"]), (7, 5))          # offer cap respected
        comp = r._load_checkpoint("competitor_intelligence")
        crow = next(x for x in comp["products"] if x["product_id"] == pid)
        self.assertEqual(crow["observations_used"], 10)                                  # competitor cap respected
        outs = s["outputs"]
        for k in ("aa_markdown", "aa_json", "aa_latest", "aa_validation_audit", "final_decision_json"):
            self.assertTrue(Path(outs[k]).exists(), k)
        self.assertRegex(Path(outs["aa_markdown"]).name, r"^\d{4}-\d{2}-\d{2}-aa-live\.md$")
        self.assertRegex(Path(outs["aa_validation_audit"]).name, r"^\d{4}-\d{2}-\d{2}-aa-validation-audit\.md$")
        js = json.loads(Path(outs["aa_json"]).read_text())
        self.assertIn(js["result"], (AA.VALIDATED, AA.RECAL))
        self.assertLessEqual(len(js["shortlist"]), 3)
        self.assertLessEqual(len(js["products"]), 5)
        for p in js["products"]:
            for _, k in AA.METRICS:
                self.assertIn(k, p)
            for k in ("final_decision", "decision_confidence", "red_flags", "missing_metrics", "next_actions"):
                self.assertIn(k, p)
        prod = next(p for p in js["products"] if p["product_id"] == pid)
        self.assertIsNotNone(prod["supplier_confidence"])                              # real imported economics used
        checks = {c["check"]: c for c in js["validation_audit"]["checks"]}
        self.assertEqual(checks["supplier economics never fabricated"]["status"], "OK")
        self.assertEqual(checks["no synthetic/live contamination"]["status"], "OK")
        self.assertEqual(checks["final decision gates / states / shortlist"]["status"], "OK")
        self.assertEqual(js["limit_violations"], [])
        self.assertEqual(js["leakage_findings"], [])
        cost = js["cost_audit"]
        self.assertEqual(cost["by_provider"]["Suppliers"]["executed"], 0)
        self.assertEqual(cost["starting_balance"] - cost["ending_balance"], len(fake.submits) * fake.cost)
        m = json.loads((r.run_dir / "manifest.json").read_text())
        self.assertEqual(m["aa_result"], js["result"])

    def test_no_layer_sources_stay_na_and_not_exercised(self):
        r, s = self.live(T.FakeProvider(consistent=True))
        js = json.loads(Path(s["outputs"]["aa_json"]).read_text())
        for p in js["products"]:
            self.assertIsNone(p["supplier_quality"])
            self.assertIsNone(p["competitor_saturation"])
            self.assertNotEqual(p["final_decision"], DE.READY)       # no supplier economics -> never READY
        self.assertTrue(any("supplier matching" in x for x in js["limitations"]))
        self.assertTrue(any("competitor matching" in x for x in js["limitations"]))
        self.assertEqual(js["shortlist"], [])
        md = Path(s["outputs"]["aa_markdown"]).read_text()
        self.assertIn("slots are left empty", md)
        self.assertIn("NO_SOURCE_AVAILABLE", md)

    def test_no_secret_in_any_output(self):
        r, s = self.live(T.FakeProvider(consistent=True))
        for p in list(self.tmp.rglob("*")):
            if p.is_file():
                self.assertNotIn(T.FAKE, p.read_text(errors="ignore"), str(p))

    def test_history_dedup_on_rerun(self):
        self.live(T.FakeProvider(consistent=True))
        r2, s2 = self.live(T.FakeProvider(consistent=True))          # same answers (cache) -> duplicates skipped
        hc = r2._load_checkpoint("historical_storage")
        self.assertGreater(hc["duplicates"], 0)
        self.assertEqual(hc["written"], 0)

    def test_limits_never_raised(self):
        r, s = self.live(T.FakeProvider(consistent=True))
        self.assertEqual(r.limits["deep_analysis_max_products"], 5)
        prof = (ROOT / PROFILE).read_text()
        self.assertIn("deep_analysis_max_products: 5", prof)


# ============================================================================ Stage 18 — data trust
class DataTrust(unittest.TestCase):
    cfg = DE.load_cfg()

    def traced(self, **kw):
        p = TD.product(**kw)
        p["deep"].update({"source": {"raw_file": "raw/deep.json"}, "observation_timestamp": "2026-09-30T12:00:00Z",
                          "wps_input_provenance": {"wps": "x"},
                          "provenance": {**TD.PROV_OK, "growth_30d": {"availability": "DERIVED", "source_field": "gmv",
                                                                      "scope": "PRODUCT"},
                                         "daily_gmv": {"availability": "AVAILABLE", "scope": "PRODUCT"}}})
        p["emerging"].update({"sources": ["history/x.json"], "history_end": "2026-09-30T12:00:00Z"})
        p["bvs_record"].update({"source": {"deep_analysis_file": "deep.json"}, "observation_timestamp": "2026-09-30"})
        p["bvs_record"]["supplier_layer"]["selected"].update({"offer_id": "OF1", "supplier_url": "https://s.test",
                                                              "observed_at": "2026-09-30"})
        return p

    def meta(self, x):
        return {**x, "source_files": ["c.json"], "observed_at": "2026-09-30", "provenance_complete": True}

    def decide(self, p, comp=True):
        ev = DE.build_evidence(p, self.meta(TD.comp()) if comp else None, self.meta(TD.crea()), "SYNTHETIC")
        ev["trust_required"] = True
        return DE.decide(ev, self.cfg)

    def test_fully_traced_ready(self):
        d = self.decide(self.traced())
        self.assertTrue(d["data_trust"]["traceable"], d["data_trust"])
        self.assertEqual(d["decision_state"], DE.READY)

    def test_untraceable_supplier_cost_downgrades_ready(self):
        p = self.traced()
        p["bvs_record"]["supplier_layer"]["selected"].pop("supplier_url")
        d = self.decide(p)
        self.assertEqual(d["decision_state"], DE.PROMISING)
        self.assertIn("commercial_viability", d["data_trust"]["untraceable"])
        self.assertTrue(any("Restore provenance" in t["task"] for t in d["validation_tasks"]))

    def test_untraceable_momentum_is_insufficient(self):
        p = self.traced()
        p["deep"]["observation_timestamp"] = None
        d = self.decide(p)
        self.assertEqual(d["decision_state"], DE.INSUFFICIENT)
        self.assertTrue(any("DATA TRUST" in x for x in d["decision_path"]))

    def test_trust_not_enforced_without_flag(self):
        ev = DE.build_evidence(TD.product(), TD.comp(), TD.crea(), "SYNTHETIC")
        self.assertEqual(DE.decide(ev, self.cfg)["decision_state"], DE.READY)


# ============================================================================ verdict rules
class Verdict(unittest.TestCase):
    def audit(self, **flags):
        checks = [{"check": c, "status": "FLAG" if flags.get(c) else "OK", "items": ["x"] if flags.get(c) else []}
                  for c in ("provider field mappings", "supplier economics never fabricated",
                            "no synthetic/live contamination", "final decision gates / states / shortlist",
                            "provenance completeness (data trust)")]
        return {"checks": checks}

    def test_clean_validates(self):
        self.assertEqual(AA.verdict(self.audit(), [], [], True, [1])[0], AA.VALIDATED)

    def test_each_failure_requires_recalibration(self):
        for c in ("provider field mappings", "supplier economics never fabricated", "no synthetic/live contamination",
                  "final decision gates / states / shortlist", "provenance completeness (data trust)"):
            res, issues = AA.verdict(self.audit(**{c: True}), [], [], True, [1])
            self.assertEqual(res, AA.RECAL, c)
            self.assertTrue(issues)
        self.assertEqual(AA.verdict(self.audit(), ["deep 6 > 5"], [], True, [1])[0], AA.RECAL)
        self.assertEqual(AA.verdict(self.audit(), [], ["leak.md"], True, [1])[0], AA.RECAL)
        self.assertEqual(AA.verdict(self.audit(), [], [], False, [1])[0], AA.RECAL)
        self.assertEqual(AA.verdict(self.audit(), [], [], True, [])[0], AA.RECAL)


class Preflight(unittest.TestCase):
    def test_new_components_listed(self):
        import preflight as PF
        for c in ("Supplier Layer", "Competitor Layer", "Creative Layer", "Decision Engine", "WPS Confidence"):
            self.assertIn(c, PF.COMPONENTS)


if __name__ == "__main__":
    unittest.main()
