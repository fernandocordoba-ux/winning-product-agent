"""Tests for scripts/concentration.py. Run: python3 -m unittest discover tests"""
import copy
import json
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from concentration import NA, analyze, load_config  # noqa: E402

CFG = load_config()
MEDICUBE = json.load(open(ROOT / "tests" / "fixtures" / "medicube_2026-09-29.json"))


def product(gmv, creators=None, videos=None, creators_count=None, videos_count=None):
    return {
        "product_id": "t", "gmv": gmv,
        "creators": [{"name": f"c{i}", "revenue": r} for i, r in enumerate(creators or [])],
        "videos": [{"id": f"v{i}", "revenue": r} for i, r in enumerate(videos or [])],
        "creators_count": creators_count, "videos_count": videos_count,
    }


class RealData(unittest.TestCase):
    def test_medicube(self):
        r = analyze(MEDICUBE, CFG)
        m = {k: v["value"] for k, v in r["metrics"].items()}
        self.assertEqual(m, {
            "top_creator_revenue_share": 10.29,
            "top3_creator_revenue_share": 26.25,
            "top_video_revenue_share": 7.46,
            "top3_video_revenue_share": 17.95,
        })
        self.assertEqual(r["flags"]["CREATOR_DEPENDENCY"]["status"], "CLEAR")
        self.assertEqual(r["flags"]["VIDEO_DEPENDENCY"]["status"], "CLEAR")


class Flags(unittest.TestCase):
    def test_viral_creator_false_positive(self):
        # $500K GMV, $400K from one creator -> 80% -> flagged
        r = analyze(product(500000, [400000, 30000, 20000], [50000, 40000, 30000]), CFG)
        self.assertEqual(r["metrics"]["top_creator_revenue_share"]["value"], 80.0)
        self.assertEqual(r["flags"]["CREATOR_DEPENDENCY"]["status"], "FLAGGED")
        self.assertEqual(r["flags"]["VIDEO_DEPENDENCY"]["status"], "CLEAR")

    def test_video_dependency(self):
        r = analyze(product(100000, [10000, 9000, 8000], [71000, 5000, 4000]), CFG)
        self.assertEqual(r["flags"]["VIDEO_DEPENDENCY"]["status"], "FLAGGED")

    def test_exactly_70_is_not_flagged(self):
        r = analyze(product(100000, [70000, 1000, 1000], [70000, 1000, 1000]), CFG)
        self.assertEqual(r["flags"]["CREATOR_DEPENDENCY"]["status"], "CLEAR")
        self.assertEqual(r["flags"]["VIDEO_DEPENDENCY"]["status"], "CLEAR")

    def test_threshold_comes_from_yaml_config(self):
        cfg = copy.deepcopy(CFG)
        cfg["flags"]["CREATOR_DEPENDENCY"]["condition"] = "> 5"
        r = analyze(MEDICUBE, cfg)
        self.assertEqual(r["flags"]["CREATOR_DEPENDENCY"]["status"], "FLAGGED")


class MissingData(unittest.TestCase):
    def assertAllNA(self, r):
        for v in r["metrics"].values():
            self.assertEqual(v["value"], NA)
        for f in r["flags"].values():
            self.assertEqual(f["status"], NA)  # no penalty/flag applied

    def test_missing_gmv(self):
        self.assertAllNA(analyze(product(None, [400000], [400000]), CFG))

    def test_zero_gmv(self):
        self.assertAllNA(analyze(product(0, [1], [1]), CFG))

    def test_no_creator_or_video_data(self):
        self.assertAllNA(analyze(product(500000), CFG))

    def test_missing_revenue_value(self):
        p = product(500000, [400000, 1000, 1000], [1000, 1000, 1000])
        p["creators"][1]["revenue"] = None
        r = analyze(p, CFG)
        self.assertEqual(r["flags"]["CREATOR_DEPENDENCY"]["status"], NA)
        self.assertEqual(r["flags"]["VIDEO_DEPENDENCY"]["status"], "CLEAR")

    def test_string_revenue_is_not_parsed_or_guessed(self):
        p = product(500000, [400000], [])
        p["creators"][0]["revenue"] = "$400K"
        self.assertEqual(analyze(p, CFG)["metrics"]["top_creator_revenue_share"]["value"], NA)

    def test_fewer_than_3_with_unknown_total_is_na_for_top3(self):
        r = analyze(product(100000, [50000, 10000]), CFG)
        self.assertEqual(r["metrics"]["top_creator_revenue_share"]["value"], 50.0)
        self.assertEqual(r["metrics"]["top3_creator_revenue_share"]["value"], NA)

    def test_fewer_than_3_but_product_only_has_2(self):
        r = analyze(product(100000, [50000, 10000], creators_count=2), CFG)
        self.assertEqual(r["metrics"]["top3_creator_revenue_share"]["value"], 60.0)

    def test_share_over_100_is_na(self):
        r = analyze(product(100000, [150000]), CFG)
        self.assertEqual(r["metrics"]["top_creator_revenue_share"]["value"], NA)
        self.assertEqual(r["flags"]["CREATOR_DEPENDENCY"]["status"], NA)


class Determinism(unittest.TestCase):
    def test_same_input_same_output_100_times(self):
        first = json.dumps(analyze(MEDICUBE, CFG), sort_keys=True)
        for _ in range(100):
            self.assertEqual(json.dumps(analyze(MEDICUBE, CFG), sort_keys=True), first)

    def test_input_order_does_not_matter(self):
        expected = analyze(MEDICUBE, CFG)["metrics"]
        rng = random.Random(0)
        for _ in range(20):
            p = copy.deepcopy(MEDICUBE)
            rng.shuffle(p["creators"])
            rng.shuffle(p["videos"])
            got = analyze(p, CFG)["metrics"]
            self.assertEqual({k: v["value"] for k, v in got.items()},
                             {k: v["value"] for k, v in expected.items()})

    def test_raw_input_not_modified(self):
        p = copy.deepcopy(MEDICUBE)
        analyze(p, CFG)
        self.assertEqual(p, MEDICUBE)


if __name__ == "__main__":
    unittest.main()
