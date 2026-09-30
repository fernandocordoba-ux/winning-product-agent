"""Step AB — Production Calibration. Unit tests use SYNTHETIC inputs only; the one end-to-end check reads the
project's saved LIVE files read-only (write=False) with the network blocked, and is skipped when none exist."""
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import decision_engine as DE  # noqa: E402
import production_calibration as PC  # noqa: E402
import score_products as SP  # noqa: E402
import test_decision as TD  # noqa: E402


def no_network(*a, **k):
    raise AssertionError("network access attempted during calibration")


class FieldBands(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(PC.band(0.95, 20), PC.RELIABLE)
        self.assertEqual(PC.band(0.8, 20), PC.USABLE)
        self.assertEqual(PC.band(0.5, 20), PC.SPARSE)
        self.assertEqual(PC.band(0.1, 20), PC.UNRELIABLE)
        self.assertEqual(PC.band(None, 0), PC.UNRELIABLE)                    # never observed
        self.assertEqual(PC.band(1.0, 20, mapping_issue_rate=0.1), PC.USABLE)  # mapping issues cap RELIABLE
        self.assertEqual(PC.band(1.0, 20, mapping_issue_rate=0.5), PC.UNRELIABLE)

    def test_sparse_core_proposed_to_supporting_only(self):
        cfg = SP.load_scoring_config()
        recs = [{"wps_na_metrics": ["demand"] if i < 6 else []} for i in range(10)]
        rows = {r["metric"]: r for r in PC.core_review(recs, cfg)}
        self.assertIn("move to SUPPORTING", rows["demand"]["proposal"])
        self.assertEqual(rows["creator_growth"]["tier"], "SUPPORTING")
        self.assertNotIn("CORE", rows["creator_growth"]["proposal"].replace("candidate for CORE review", ""))


class ProviderScore(unittest.TestCase):
    def test_not_measured(self):
        self.assertIsNone(PC.provider_score({"attempted": 0}))

    def test_bounds_and_technical_only(self):
        m = {"attempted": 10, "successful": 10, "missing_expected_field_rate": 0, "mapping_error_rate": 0,
             "provenance_failure_rate": 0, "timeouts": 0}
        self.assertEqual(PC.provider_score(m)[0], 100.0)
        m2 = {**m, "successful": 5, "missing_expected_field_rate": 0.5, "timeouts": 5}
        self.assertEqual(PC.provider_score(m2)[0], 65.0)          # 20 + 10 + 15 + 15 + 5
        self.assertNotIn("wps", str(PC.provider_score.__code__.co_names).lower())


class WpsAudit(unittest.TestCase):
    def test_pattern_flags(self):
        cfg = SP.load_scoring_config()
        recs = []
        for i in range(8):
            bd = {k: {"points": 0.0} for k in cfg["metrics"]}
            bd["growth_long_term"] = {"points": 10.0}
            bd["demand"] = {"points": "N/A"} if i < 3 else {"points": 5.0 + i}
            recs.append({"wps_breakdown": bd, "confidence": 80})
        a = PC.wps_audit(recs, cfg, {})
        self.assertIn("TOO_MANY_MAX_SCORES", a["growth_long_term"]["flags"])
        self.assertIn("TOO_MANY_ZERO_SCORES", a["trend_stability"]["flags"])
        self.assertIn("LOW_VARIANCE", a["trend_stability"]["flags"])
        self.assertIn("MISSING_DATA_OVERPENALIZED", a["demand"]["flags"])


class Sensitivity(unittest.TestCase):
    def test_grid_counts_and_no_best(self):
        rows = [{"wps": 72, "conf": 65}, {"wps": 72, "conf": 40}, {"wps": 63, "conf": 90}, {"wps": 20, "conf": 90},
                {"wps": None, "conf": 90}]
        g = {(x["threshold"], x["confidence_min"]): x for x in PC._grid(rows, "wps", "conf", [70], [60])}
        x = g[(70, 60)]
        self.assertEqual((x["pass"], x["review"], x["reject"], x["no_data"]), (1, 2, 1, 1))
        s = PC.sensitivity([], {})
        self.assertNotIn("best", " ".join(k for k in s).lower())
        self.assertIn("No threshold is marked best", s["definition"])


class ConfidenceFindings(unittest.TestCase):
    def test_double_penalty_detected_and_removed_by_opt_in(self):
        cfg = SP.load_scoring_config()
        rec = {"confidence_adjustments": [
            {"reason": "SUPPORTING/ENHANCEMENT metric not available: competition_saturation", "points": -3},
            {"reason": "ENHANCEMENT field missing: similar_listings_count", "points": -1},
            {"reason": "ENHANCEMENT field missing: price_history", "points": -1}]}
        dp = PC.double_penalties(rec, cfg)
        self.assertEqual(len(dp), 2)                     # price_history is not a confidence check -> not double
        prod = {"competition_count_comparable": None, "category_product_count": None, "similar_listings_count": None}
        wps = {"missing_by_tier": {"SUPPORTING": ["competition_saturation"], "ENHANCEMENT": []}}
        base = SP.confidence_adjustments(prod, wps, cfg)
        cfg2 = copy.deepcopy(cfg)
        cfg2["data_requirements"]["counted_in_confidence"] = {"competition_saturation": ["category_product_count"],
                                                              "similar_listings_count": ["similar_listings_count"]}
        fixed = SP.confidence_adjustments(prod, wps, cfg2)
        self.assertEqual(len(base) - len(fixed), 2)

    def test_production_behaviour_unchanged_without_opt_in(self):
        cfg = SP.load_scoring_config()
        self.assertNotIn("counted_in_confidence", cfg["data_requirements"])


class DecisionExplanations(unittest.TestCase):
    cfg = DE.load_cfg()

    def test_negative_vs_missing(self):
        d = DE.decide(DE.build_evidence(TD.product(wps=40.0), TD.comp(), TD.crea(), "SYNTHETIC"), self.cfg)
        self.assertEqual(d["explanation"]["primary"], "NEGATIVE_EVIDENCE")
        d2 = DE.decide(DE.build_evidence(TD.product(), None, None, "SYNTHETIC"), self.cfg)
        self.assertEqual(d2["explanation"]["NEGATIVE_EVIDENCE"], [])
        self.assertTrue(d2["explanation"]["MISSING_EVIDENCE"])

    def test_dependency_gate_v2_needs_negative_evidence(self):
        p = TD.product(flags=["CREATOR_DEPENDENCY"], creators=4)            # Amazon UNKNOWN
        self.assertEqual(DE.decide(DE.build_evidence(p, TD.comp(), TD.crea(), "SYNTHETIC"), self.cfg)["decision_state"],
                         DE.REJECT)                                          # Z-v1 behaviour kept in production
        v2 = copy.deepcopy(self.cfg)
        for g in ("CREATOR_DEPENDENCY_WEAK_BROADER", "VIDEO_DEPENDENCY_WEAK_BROADER"):
            v2["hard_gates"][g].update({"broader_weak_statuses": ["WEAK"], "when_broader_unknown": "block_ready"})
        d = DE.decide(DE.build_evidence(p, TD.comp(), TD.crea(), "SYNTHETIC"), v2)
        self.assertNotEqual(d["decision_state"], DE.REJECT)
        self.assertNotEqual(d["decision_state"], DE.READY)
        self.assertTrue(any(x["source"] == "gate:CREATOR_DEPENDENCY_WEAK_BROADER"
                            for x in d["explanation"]["MISSING_EVIDENCE"]))
        weak = TD.product(flags=["CREATOR_DEPENDENCY"], creators=4, avs=30.0, aconf=70.0)
        self.assertEqual(DE.decide(DE.build_evidence(weak, TD.comp(), TD.crea(), "SYNTHETIC"), v2)["decision_state"],
                         DE.REJECT)                                          # negative evidence still rejects


class Regression(unittest.TestCase):
    def rp(self, **kw):
        dec = DE.decide(DE.build_evidence(TD.product(cost=None, ship=None, gm=None, cm=None, env="LIVE"), TD.comp(),
                                          TD.crea(), "LIVE"), DE.load_cfg())
        dec["data_trust"] = {"traceable": True, "untraceable": {}}
        base = {"scores": [{"product_id": "1", "old_confidence": 60, "new_confidence": 62, "removed_double_penalty": 3,
                            "old_wps": 50, "new_wps": 50}],
                "new": {"decisions": [dec]}, "old": {"decisions": [dec]}, "new_recs": [],
                "new_decision_cfg": DE.load_cfg()}
        base.update(kw)
        return base

    def ev(self, n=0):
        return {"non_live_history": n}

    def test_clean(self):
        r = PC.regression(self.ev(), self.rp(), ROOT)
        self.assertFalse(r["blocked"], r["issues"])

    def test_blocks(self):
        rp = self.rp()
        rp["scores"][0]["new_confidence"] = 70                                   # more than the removed duplicate
        self.assertTrue(any("UNSUPPORTED_CONFIDENCE" in x for x in PC.regression(self.ev(), rp, ROOT)["issues"]))
        self.assertTrue(any("SYNTHETIC_IN_LIVE" in x for x in PC.regression(self.ev(2), self.rp(), ROOT)["issues"]))
        low = copy.deepcopy(DE.load_cfg())
        low["minimum_confidence"]["bvs"] = 20
        self.assertTrue(any("LOWERED_MINIMUM" in x for x in
                            PC.regression(self.ev(), self.rp(new_decision_cfg=low), ROOT)["issues"]))
        rp = self.rp()
        rp["scores"][0]["new_wps"] = 55
        self.assertTrue(any("WPS_CHANGED" in x for x in PC.regression(self.ev(), rp, ROOT)["issues"]))

    def test_new_ready_without_evidence_blocks(self):
        ready = DE.decide(DE.build_evidence(TD.product(env="LIVE"), TD.comp(), TD.crea(), "LIVE"), DE.load_cfg())
        not_ready = DE.decide(DE.build_evidence(TD.product(wps=40.0, env="LIVE"), TD.comp(), TD.crea(), "LIVE"),
                              DE.load_cfg())
        self.assertEqual(ready["decision_state"], DE.READY)
        ready["product_id"] = not_ready["product_id"] = "X"
        rp = self.rp(new={"decisions": [ready]}, old={"decisions": [not_ready]})
        self.assertTrue(any("NEW_READY" in x for x in PC.regression(self.ev(), rp, ROOT)["issues"]))


class FinalResult(unittest.TestCase):
    def ready(self, prov=PC.READY, trace=PC.READY):
        return {"Provider Reliability": {"status": prov, "evidence": {"not_measured": [] if prov == PC.READY else ["X"]}},
                "Decision Traceability": {"status": trace}}

    def conf(self, flags=()):
        return {"flags": list(flags), "systems": {"Decision Confidence": {"depends_on_performance": []}}}

    def test_ready_only_when_everything_holds(self):
        reg = {"blocked": False, "issues": []}
        self.assertEqual(PC.final_result([], self.conf(), {}, {}, reg, self.ready())[0], PC.PRODUCTION_CONFIG_READY)
        self.assertEqual(PC.final_result(["x"], self.conf(), {}, {}, reg, self.ready())[0], PC.MORE_CALIBRATION)
        self.assertEqual(PC.final_result([], self.conf(["1: DOUBLE_PENALTY"]), {}, {}, reg, self.ready())[0],
                         PC.MORE_CALIBRATION)
        self.assertEqual(PC.final_result([], self.conf(), {}, {}, reg, self.ready(prov=PC.PARTIAL))[0], PC.MORE_CALIBRATION)
        self.assertEqual(PC.final_result([], self.conf(), {}, {}, reg, self.ready(trace=PC.PARTIAL))[0],
                         PC.MORE_CALIBRATION)
        self.assertEqual(PC.final_result([], self.conf(), {}, {}, {"blocked": True, "issues": ["x"]}, self.ready())[0],
                         PC.MORE_CALIBRATION)


class Proposals(unittest.TestCase):
    def test_proposals_are_valid_and_conservative(self):
        import config_validation as CV
        from winning_product_agent import runner as R
        cost = {"observed_cost_per_query": {"deep_answer_ok": {"max": 3.57}}}
        props = PC.build_proposals(ROOT, cost)
        self.assertEqual(set(props), set(PC.PROPOSED))
        cfgs, _ = CV.load_all()
        c2 = copy.deepcopy(cfgs)
        c2["scoring.yaml"] = yaml.safe_load(props["scoring_v2.yaml"][0])
        c2["filters.yaml"] = yaml.safe_load(props["filters_v2.yaml"][0])
        c2["decision.yaml"] = yaml.safe_load(props["decision_rules_v2.yaml"][0])
        self.assertEqual(CV.validate(c2), [])
        old, new = cfgs["scoring.yaml"], c2["scoring.yaml"]
        self.assertEqual({k: (m["points"], m.get("tier")) for k, m in old["metrics"].items()},
                         {k: (m["points"], m.get("tier")) for k, m in new["metrics"].items()})      # WPS untouched
        for k, v in cfgs["decision.yaml"]["minimum_confidence"].items():
            self.assertGreaterEqual(c2["decision.yaml"]["minimum_confidence"][k], v)             # never loosened
        rt = yaml.safe_load(props["runtime_production_v1.yaml"][0])
        live = yaml.safe_load((ROOT / "config" / "runtime_first_live.yaml").read_text())
        for k, v in rt["limits"].items():
            self.assertLessEqual(v, live["limits"][k])                                            # no limit increase
        self.assertGreaterEqual(rt["query_plan"]["estimated_credits"]["deep_analysis"], 3.57)


class LiveEvidenceOffline(unittest.TestCase):
    def test_offline_run_makes_no_query_and_writes_nothing(self):
        if not list((ROOT / "data" / "raw" / "deep_analysis").glob("*_deep_product*.json")):
            self.skipTest("no saved LIVE evidence in this checkout")
        before = {p: p.stat().st_mtime for p in (ROOT / "config").glob("*.yaml")}
        with mock.patch("urllib.request.urlopen", no_network):
            r = PC.run(write=False)
        self.assertIn(r["result"], (PC.PRODUCTION_CONFIG_READY, PC.MORE_CALIBRATION))
        self.assertEqual(before, {p: p.stat().st_mtime for p in (ROOT / "config").glob("*.yaml")})
        self.assertEqual(r["evidence"]["excluded"]["synthetic_runs"], 0)
        for cat in ("KEEP", "CHANGE", "MONITOR", "REMOVE", "NEEDS_MORE_DATA"):
            for x in [x for x in r["recommendations"] if x["category"] == cat]:
                for k in ("current_rule", "observed_evidence", "proposed_change", "expected_effect", "risk"):
                    self.assertTrue(x[k], (cat, k))
        self.assertFalse(r["regression"]["blocked"], r["regression"]["issues"])


if __name__ == "__main__":
    unittest.main()
