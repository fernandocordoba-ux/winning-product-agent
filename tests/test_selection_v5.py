"""Deep selection by preliminary score + seasonal exclusion; evidence-quality layer subset; insufficient rule off."""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

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
