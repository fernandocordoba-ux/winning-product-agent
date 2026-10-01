"""Amazon validation label + KaloPilot potential list: labels only, never change decision states."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import decision_engine as DE  # noqa: E402
import test_decision as TD  # noqa: E402

CFG = DE.load_cfg()


def dec(**kw):
    return DE.decide(DE.build_evidence(TD.product(**kw), TD.comp(), TD.crea(), "SYNTHETIC"), CFG)


def level(**kw):
    p = TD.product(**{k: x for k, x in kw.items() if k != "match"})
    if kw.get("match"):
        p["amazon"] = {"amazon_match_status": kw["match"]}
    ev = DE.build_evidence(p, TD.comp(), TD.crea(), "SYNTHETIC")
    return DE.amazon_validation_level(ev, CFG, av_min=(55, 60))


class AmazonLevel(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(level(avs=72.0, aconf=70.0)["level"], "COMPLETA")
        self.assertEqual(level(avs=72.0, aconf=55.0)["level"], "MEDIA")
        self.assertEqual(level(avs=72.0, aconf=40.0)["level"], "PARCIAL")
        self.assertEqual(level(avs=72.0, aconf=70.0, match="NO_RELIABLE_MATCH")["level"], "PARCIAL")
        n = level(wps=50.0)
        self.assertEqual(n["level"], "NO_VALIDADA")
        self.assertIn("no elegible", n["reason"])
        self.assertIn("no se consultó", level(wps=80.0)["reason"])

    def test_label_never_changes_state(self):
        for kw in ({}, {"avs": 72.0, "aconf": 70.0}, {"avs": 72.0, "aconf": 40.0}):
            d = dec(**kw)
            self.assertIn(d["amazon_validation"]["level"], DE.AMZ_LEVELS)
        a = dec(avs=None, aconf=None)
        b = DE.decide(DE.build_evidence(TD.product(), TD.comp(), TD.crea(), "SYNTHETIC"), CFG)
        self.assertEqual(a["decision_state"], b["decision_state"])


class Potential(unittest.TestCase):
    def test_potential_lists_momentum_ok_regardless_of_amazon(self):
        ds = [dec(pid="A", wps=80.0), dec(pid="B", wps=60.0, momentum=None, cost=None, bvs=None),
              dec(pid="C", wps=40.0)]
        pot = DE.kalopilot_potential(ds)
        self.assertEqual([p["product_id"] for p in pot], ["A", "B"])
        self.assertTrue(all(p["amazon_validation"]["level"] == "NO_VALIDADA" for p in pot))
        self.assertEqual(len(DE.kalopilot_potential(ds, max_products=1)), 1)

    def test_reports_include_potential(self):
        r = DE.run([TD.product(pid="A", wps=80.0), TD.product(pid="C", wps=40.0)], expected_env="SYNTHETIC")
        self.assertEqual([p["product_id"] for p in r["kalopilot_potential"]], ["A"])
        md = DE.render_markdown(r)
        self.assertIn("KaloPilot potential (Amazon not required)", md)
        self.assertIn("Sin validar en Amazon", md)
        self.assertEqual(DE.build_json(r)["kalopilot_potential"][0]["product_id"], "A")


if __name__ == "__main__":
    unittest.main()
