"""Step AE lens experiment: queries, confirmation gate, analysis. Fake KaloPilot only."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import lens_experiment as LX  # noqa: E402


def answer(rows):
    return {"success": True, "data": {"status": "completed", "report": "```json\n" + json.dumps(rows) + "\n```"}}


class Fake:
    def __init__(self):
        self.bal, self.sub = 100.0, []

    def credits(self):
        return {"totalRemain": self.bal}

    def submit(self, q, estimated_cost=None):
        self.sub.append(q)
        self.bal -= 3.6
        return {"data": {"task_id": f"T{len(self.sub)}"}}

    def wait(self, t):
        return answer([{"product_id": t + "a", "product_name": "Over Sink Colander", "gmv_30d": 90000, "gmv_prev_30d": 60000,
                        "gmv_prev2_30d": 30000, "units_30d": 6000, "creator_count": 300, "video_count": 500},
                       {"product_id": t + "b", "product_name": "Spike", "gmv_30d": 30000, "gmv_prev_30d": 60,
                        "gmv_prev2_30d": None, "units_30d": 800, "creator_count": 20, "video_count": 30}])


class Lens(unittest.TestCase):
    def test_queries(self):
        qs = LX.build_queries()
        self.assertEqual(len(qs), 6)
        self.assertEqual({q["lens"] for q in qs}, set(LX.LENSES))
        for q in qs:
            self.assertIn("gmv_prev2_30d", q["query"])
            self.assertIn("Exclude seasonal", q["query"])
            self.assertNotIn("{", q["query"].split("```json")[0].replace("{region}", ""))

    def test_gate_and_analysis(self):
        with self.assertRaises(SystemExit):
            LX.run_live(LX.build_queries(), "CONFIRM PRODUCTION LIVE RUN.", client=Fake())
        d = Path(tempfile.mkdtemp())
        f = Fake()
        with mock.patch.object(LX, "RAW_DIR", d), mock.patch.object(LX.time, "sleep"):
            r = LX.run_live(LX.build_queries(), LX.PHRASE, client=f, out=lambda s: None)
        self.assertEqual(len(f.sub), 6)
        self.assertLessEqual(r["credits_spent"], LX.RUN_CAP)
        a = LX.analyze(d)
        self.assertEqual(a["lenses"]["proven"]["products"], 4)
        self.assertEqual(a["lenses"]["rising"]["sustained"], 2)
        self.assertTrue(all(t["base_ok"] for t in a["top10"]))           # the 60-USD-base spike is excluded
        self.assertIn("Top 10", LX.render(a))


if __name__ == "__main__":
    unittest.main()
