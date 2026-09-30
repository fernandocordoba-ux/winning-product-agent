"""Tests for the Confidence Score (scripts/confidence.py) and its integration in
scripts/score_products.py. Run: python3 -m unittest discover tests
"""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from confidence import NA, confidence_score, level_for, load_config  # noqa: E402
from score_products import score_product  # noqa: E402

CFG = load_config()


def complete(**overrides):
    """A product with every confidence input available (values are test data)."""
    p = {
        "product_id": "p1", "product_name": "Test product",
        "daily_sales_series": [1000.0] * 30,
        "gmv": 30000, "units_sold": 900,
        "revenue_growth_pct": 12.5, "category_growth_pct": 4.0,
        "price_min": 25.0, "price_max": 40.0,
        "creators_count": 40,
        "creators": [{"name": f"c{i}", "revenue": 5000 - i} for i in range(5)],
        "videos_count": 120,
        "videos": [{"id": f"v{i}", "revenue": 4000 - i} for i in range(5)],
        "shops_count": 3, "category_product_count": 12000, "similar_listings_count": 15,
        "launch_date_days": 200, "history_snapshots": 2,
    }
    p.update(overrides)
    return p


def earned(result):
    return {k: v["earned"] for k, v in result["breakdown"].items()}


class Config(unittest.TestCase):
    def test_points_sum_to_100(self):
        self.assertEqual(sum(c["points"] for c in CFG["components"].values()), 100)

    def test_levels(self):
        L = CFG["levels"]
        self.assertEqual(level_for(100, L), "VERY_HIGH")
        self.assertEqual(level_for(90, L), "VERY_HIGH")
        self.assertEqual(level_for(89.99, L), "HIGH")
        self.assertEqual(level_for(75, L), "HIGH")
        self.assertEqual(level_for(74.99, L), "MODERATE")
        self.assertEqual(level_for(60, L), "MODERATE")
        self.assertEqual(level_for(59.99, L), "LOW")
        self.assertEqual(level_for(40, L), "LOW")
        self.assertEqual(level_for(39.99, L), "VERY_LOW")
        self.assertEqual(level_for(0, L), "VERY_LOW")


class CompleteData(unittest.TestCase):
    def test_complete_data_is_100(self):
        r = confidence_score(complete(), CFG)
        self.assertEqual(r["score"], 100.0)
        self.assertEqual(r["level"], "VERY_HIGH")
        for name, c in r["breakdown"].items():
            self.assertEqual(c["earned"], c["max"], name)
            self.assertEqual(c["status"], "full")


class PartialData(unittest.TestCase):
    def test_partially_missing(self):
        p = complete(daily_sales_series=[1000.0] * 15 + [None] * 15,  # 15/30 -> 7.5
                     category_growth_pct=None,                        # growth 2/3 -> 10
                     creators=[{"name": "a", "revenue": 1}],          # creators (1 + 1/3)/2 -> 10
                     similar_listings_count=None,                     # competition 2/3 -> 6.67
                     history_snapshots=1)                             # trend (1 + 0.5)/2 -> 3.75
        r = confidence_score(p, CFG)
        self.assertEqual(earned(r), {
            "sales_history": 7.5, "gmv_data": 10.0, "units_sold": 10.0,
            "growth_data": 10.0, "price_data": 5.0, "creator_data": 10.0,
            "video_data": 15.0, "competition_shop_data": 6.67, "trend_history_data": 3.75,
        })
        self.assertEqual(r["score"], 77.92)   # 7.5+10+10+10+5+10+15+6.6667+3.75
        self.assertEqual(r["level"], "HIGH")
        self.assertEqual(r["breakdown"]["sales_history"]["status"], "partial")

    def test_mostly_missing(self):
        p = {"product_id": "p2", "gmv": 50000, "price_min": 30}
        r = confidence_score(p, CFG)
        self.assertEqual(r["score"], 12.5)    # gmv 10 + price 1/2 of 5
        self.assertEqual(r["level"], "VERY_LOW")
        self.assertEqual(r["breakdown"]["units_sold"]["status"], "missing")

    def test_empty_product_is_zero(self):
        r = confidence_score({}, CFG)
        self.assertEqual(r["score"], 0.0)
        self.assertEqual(r["level"], "VERY_LOW")


class ZeroAndNA(unittest.TestCase):
    def test_zero_values_are_valid_observations(self):
        p = complete(revenue_growth_pct=0, category_growth_pct=0, units_sold=0, gmv=0,
                     shops_count=0, similar_listings_count=0,
                     daily_sales_series=[0.0] * 30,
                     creators=[{"name": f"c{i}", "revenue": 0} for i in range(3)])
        r = confidence_score(p, CFG)
        self.assertEqual(r["score"], 100.0)

    def test_null_and_na_values_are_missing(self):
        for missing in (None, "N/A", "", "n/a", "$400K", float("nan"), float("inf"), True):
            p = complete(units_sold=missing)
            r = confidence_score(p, CFG)
            self.assertEqual(r["breakdown"]["units_sold"]["earned"], 0.0, repr(missing))
            self.assertEqual(r["breakdown"]["units_sold"]["checks"][0]["value"], NA)
            self.assertEqual(r["score"], 90.0, repr(missing))

    def test_absent_key_is_missing(self):
        p = complete()
        del p["revenue_growth_pct"]
        self.assertEqual(confidence_score(p, CFG)["breakdown"]["growth_data"]["earned"], 5.0)

    def test_negative_count_is_invalid(self):
        r = confidence_score(complete(creators_count=-5), CFG)
        self.assertEqual(r["breakdown"]["creator_data"]["earned"], 7.5)

    def test_negative_growth_is_valid(self):
        r = confidence_score(complete(revenue_growth_pct=-45.0), CFG)
        self.assertEqual(r["breakdown"]["growth_data"]["earned"], 15.0)

    def test_inconsistent_price_gets_zero_and_warning(self):
        r = confidence_score(complete(price_min=80, price_max=40), CFG)
        self.assertEqual(r["breakdown"]["price_data"]["earned"], 0.0)
        self.assertTrue(r["warnings"])


class IndependentFromPerformance(unittest.TestCase):
    def test_poor_product_with_complete_data_has_high_confidence(self):
        poor = complete(gmv=1200, units_sold=40, revenue_growth_pct=-60.0,
                        category_growth_pct=-25.0, creators_count=3, videos_count=3,
                        daily_sales_series=[40.0] * 30)
        r = confidence_score(poor, CFG)
        self.assertEqual(r["score"], 100.0)
        self.assertEqual(r["level"], "VERY_HIGH")

    def test_strong_product_with_incomplete_data_has_low_confidence(self):
        strong = {"product_id": "s", "gmv": 4206171, "units_sold": 140317,
                  "revenue_growth_pct": 100.1, "price_min": 40.3, "price_max": 156.0}
        r = confidence_score(strong, CFG)
        self.assertEqual(r["score"], 35.0)    # gmv 10 + units 10 + growth 10 (no category growth) + price 5
        self.assertEqual(r["level"], "VERY_LOW")

    def test_performance_values_do_not_change_confidence(self):
        base = confidence_score(complete(), CFG)["score"]
        for gmv in (0, 1, 25000, 10**9):
            for growth in (-99.0, 0, 500.0):
                self.assertEqual(
                    confidence_score(complete(gmv=gmv, revenue_growth_pct=growth), CFG)["score"], base)


class EngineIntegration(unittest.TestCase):
    def test_wps_and_confidence_returned_separately(self):
        r = score_product(complete(), CFG)
        self.assertEqual(r["wps"]["score"], NA)                      # WPS engine not implemented
        self.assertEqual(r["wps"]["status"], "not_implemented")
        self.assertEqual(r["confidence"]["score"], 100.0)
        self.assertEqual(r["summary"]["Confidence"], "100.0/100 (VERY_HIGH)")
        self.assertIn("N/A", r["summary"]["WPS"])

    def test_high_confidence_does_not_create_or_raise_wps(self):
        hi = score_product(complete(), CFG)
        lo = score_product({"product_id": "x"}, CFG)
        self.assertEqual(hi["wps"], lo["wps"])


class Determinism(unittest.TestCase):
    def test_same_input_same_output(self):
        p = complete(category_growth_pct=None, history_snapshots=1)
        first = json.dumps(confidence_score(p, CFG), sort_keys=True)
        for _ in range(100):
            self.assertEqual(json.dumps(confidence_score(p, CFG), sort_keys=True), first)

    def test_input_not_modified(self):
        p = complete(units_sold=None)
        before = copy.deepcopy(p)
        confidence_score(p, CFG)
        self.assertEqual(p, before)


if __name__ == "__main__":
    unittest.main()
