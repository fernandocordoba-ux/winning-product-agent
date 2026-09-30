"""'¿Qué quieres saber hoy?' page data (read-only). Uses saved LIVE files read-only; network blocked."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import insights_export as IE  # noqa: E402


class Insights(unittest.TestCase):
    def test_explanations_and_embedding(self):
        if not list((ROOT / "data" / "raw" / "deep_analysis").glob("*_deep_product*.json")) or \
                not (ROOT / "config" / "production" / "ACTIVE.json").exists():
            self.skipTest("no saved evidence / production config in this checkout")
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), tempfile.TemporaryDirectory() as t:
            r = IE.write(out_dir=t, with_balance=False)
            d = r["data"]
            self.assertIn(d["source"], ("PRODUCTION", "CALIBRATION"))
            for p in d["products"]:
                self.assertTrue(p["state_why"])
                self.assertEqual(len(p["dimensions"]), 6)
                for c in p["wps_components"]:
                    self.assertTrue(c["why"])
            html = Path(r["html"]).read_text()
            self.assertNotIn("/*__INSIGHTS_DATA__*/null", html)
            self.assertIn('"products"', html)
            self.assertNotIn("KALOPILOT_TOKEN", html)

    def test_scale_text(self):
        self.assertIn("0 puntos con 1,000", IE._scale_text({"scale": "log10", "zero_at": 1000, "full_at": 100000}))
        self.assertIn("puntaje completo con 500 o menos", IE._scale_text({"scale": "log10", "zero_at": 50000, "full_at": 500}))


if __name__ == "__main__":
    unittest.main()
