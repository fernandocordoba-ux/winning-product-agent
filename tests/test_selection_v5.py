"""Deep selection by preliminary score + seasonal exclusion; evidence-quality layer subset; insufficient rule off."""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

import decision_engine as DE  # noqa: E402
import deep_analysis as DA  # noqa: E402
import test_decision as TD  # noqa: E402


def cand(key, name, units, creators, videos, growth=300, status="PASS"):
    return {"key": key, "facts": {"product_id": key, "product_name": name, "units_30d": units, "creator_count": creators,
                                  "video_count": videos, "growth_30d": growth},
            "calculated": {"filter_status": status, "filter_reasons": []}}


class Selection(unittest.TestCase):
    def setUp(self):
        self.res = {"candidates": [cand("A", "Small thing", 300, 10, 20), cand("B", "Halloween Witch Cauldron", 9000, 400, 900),
                                   cand("C", "Collapsible Colander 3-Pack", 8000, 300, 500, status="REVIEW")]}
        self.cfg = DA.load_cfg()

    def test_default_order_unchanged(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["selection"]["deep_analysis_max_products"] = 2
        sel, _ = DA.select_candidates(copy.deepcopy(self.res), cfg)
        self.assertEqual([p["key"] for p in sel], ["A", "B"])

    def test_preliminary_order_and_seasonal(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["selection"].update(deep_analysis_max_products=2, order="preliminary_wps", exclude_keywords=["halloween"])
        sel, skipped = DA.select_candidates(copy.deepcopy(self.res), cfg)
        self.assertEqual([p["key"] for p in sel], ["C", "A"])
        self.assertTrue(any(s["key"] == "B" and "seasonal" in s["reason"] for s in skipped))
        self.assertGreater(sel[0]["calculated"]["preliminary_wps"], sel[1]["calculated"]["preliminary_wps"])


class Rules(unittest.TestCase):
    def test_layers_subset_and_no_insufficient_rule(self):
        cfg = copy.deepcopy(DE.load_cfg())
        p = TD.product(bvs=40.0, bconf=22.0, cost=None, momentum=None)
        ev = DE.build_evidence(p, None, None, "SYNTHETIC")
        base = DE.decide(ev, cfg)
        cfg["dimensions"]["evidence_quality"]["layers"] = ["wps", "momentum", "amazon", "competitor", "creative"]
        cfg["decision"]["insufficient_if_unknown_at_least"] = 99
        d = DE.decide(ev, cfg)
        self.assertNotIn("bvs", d["dimensions"]["evidence_quality"]["inputs"]["layer_confidences"])
        self.assertNotEqual(d["decision_state"], DE.INSUFFICIENT)
        self.assertIn(d["decision_state"], (DE.PROMISING, DE.WATCH, DE.READY, DE.REJECT))
        self.assertIsNotNone(base)


if __name__ == "__main__":
    unittest.main()


class BeautyV8(unittest.TestCase):
    def test_subniche_queries(self):
        import lens_experiment as LX
        qs = LX.build_queries(("beauty_personal_care",), ("proven",), subniches=["nails & lashes", "hair care"])
        self.assertEqual(len(qs), 2)
        self.assertIn('focus only on this sub-niche: "nails & lashes"', qs[0]["query"])
        self.assertEqual({q["category_key"] for q in qs}, {"beauty_personal_care"})

    def test_new_products_first(self):
        res = {"candidates": [cand("A", "Lash kit", 9000, 400, 900), cand("B", "Press on nails", 3000, 100, 200)]}
        cfg = copy.deepcopy(DA.load_cfg())
        cfg["selection"].update(deep_analysis_max_products=1, order="preliminary_wps", deprioritize_ids=["A"])
        sel, _ = DA.select_candidates(res, cfg)
        self.assertEqual(sel[0]["key"], "B")


class CandidatesReport(unittest.TestCase):
    def test_report_from_saved_run(self):
        import tempfile
        import candidates_report as CRP
        d = Path(tempfile.mkdtemp())
        r = CRP.write(out_dir=d)
        if r is None:
            self.skipTest("no production run saved in this checkout")
        self.assertTrue((d / "latest.csv").exists() and (d / "latest.md").exists())
        self.assertGreater(r["rows"], 0)


class Target(unittest.TestCase):
    def test_candidates_found(self):
        from winning_product_agent.runner import Runner
        rs = [{"status": "ok", "wps": 60, "confidence": 70}, {"status": "ok", "wps": 54, "confidence": 90},
              {"status": "ok", "wps": 70, "confidence": 50}, {"status": "failed"}]
        self.assertEqual(Runner.candidates_found(rs), 1)


class ScoreOnlyOrder(unittest.TestCase):
    """preliminary_only: the score alone decides; sustained 90-day growth no longer jumps the queue."""

    def _res(self):
        small = cand("S", "Small sustained", 300, 10, 20)                       # low score, sustained growth
        small["source"] = {"original_record": {"gmv_prev2_30d": 1000, "gmv_prev_30d": 2000, "gmv_30d": 3000}}
        big = cand("B", "Big steady", 9000, 400, 900)                           # high score, not sustained
        big["source"] = {"original_record": {"gmv_prev2_30d": 9000, "gmv_prev_30d": 5000, "gmv_30d": 6000}}
        return {"candidates": [small, big]}

    def test_sustained_first_vs_score_only(self):
        cfg = DA.load_cfg()
        cfg["selection"].update(deep_analysis_max_products=2, order="preliminary_wps")
        sel, _ = DA.select_candidates(self._res(), copy.deepcopy(cfg))
        self.assertEqual([p["key"] for p in sel], ["S", "B"])                   # previous rule, unchanged
        cfg["selection"]["order"] = "preliminary_only"
        sel, _ = DA.select_candidates(self._res(), copy.deepcopy(cfg))
        self.assertEqual([p["key"] for p in sel], ["B", "S"])
        self.assertTrue(all(p["calculated"]["preliminary_wps"] is not None for p in sel))


class PreliminaryFloorAndEmptyCache(unittest.TestCase):
    def test_floor_skips_low_scores_with_reason(self):
        cfg = DA.load_cfg()
        res = {"candidates": [cand("LOW", "Tiny", 210, 11, 21, growth=1), cand("HIGH", "Big", 9000, 400, 900)]}
        cfg["selection"].update(deep_analysis_max_products=5, order="preliminary_only", min_preliminary=32)
        sel, skipped = DA.select_candidates(res, copy.deepcopy(cfg))
        self.assertEqual([p["key"] for p in sel], ["HIGH"])
        self.assertTrue(any(s["key"] == "LOW" and "below 32" in s["reason"] for s in skipped))
        cfg["selection"].pop("min_preliminary")                                  # no floor -> both, as before
        sel, _ = DA.select_candidates(res, copy.deepcopy(cfg))
        self.assertEqual({p["key"] for p in sel}, {"LOW", "HIGH"})

    def test_empty_paid_answer_is_not_a_cache_hit(self):
        import json
        import tempfile
        from datetime import datetime, timezone
        d = Path(tempfile.mkdtemp())
        now = datetime.now(timezone.utc)
        env = {"observation_timestamp": now.isoformat(),
               "response": {"data": {"status": "completed", "text": "", "report": None}}}
        (d / "20260101T000000Z_P1_deep_product.json").write_text(json.dumps(env))
        self.assertIsNone(DA.find_cached("P1", "deep_product", 72, now, d))
        env["response"]["data"]["text"] = "```json\n{}\n```"
        (d / "20260101T000001Z_P1_deep_product.json").write_text(json.dumps(env))
        self.assertIsNotNone(DA.find_cached("P1", "deep_product", 72, now, d))


class OperationalExclusions(unittest.TestCase):
    def test_program_side_exclusion_has_its_own_reason(self):
        cfg = DA.load_cfg()
        res = {"candidates": [cand("PB", "20000mAh Power Bank Fast Charger", 9000, 400, 900),
                              cand("OK", "Magnetic Phone Holder for Car", 8000, 300, 500)]}
        cfg["selection"].update(deep_analysis_max_products=5, order="preliminary_only",
                                exclude_operational=["power bank", "charger"])
        sel, skipped = DA.select_candidates(res, copy.deepcopy(cfg))
        self.assertEqual([p["key"] for p in sel], ["OK"])
        self.assertTrue(any(s["key"] == "PB" and "operational exclusion" in s["reason"] for s in skipped))
