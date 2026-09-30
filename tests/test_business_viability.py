"""Tests for the Business Viability Score (Step O). SYNTHETIC DATA ONLY, in temp
folders (never mixed with real research data). Run: python3 -m unittest discover tests
"""
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import business_viability as B  # noqa: E402
import discovery as disc  # noqa: E402

CFG = B.load_cfg()
FILTERS = disc.load_yaml("filters.yaml")


def deep(pid="1", name="Bamboo Shoe Rack Organizer 5 Tier", wps=80.0, conf=75.0, **kw):
    d = {"status": "ok", "product_id": pid, "product_name": name, "category": "Home Supplies > Storage",
         "price": {"min": 35.0, "max": 45.0, "avg": 40.0}, "units": 3000, "wps": wps, "confidence": conf,
         "video_metrics": {"total": 900, "selling": 400},
         "competition_metrics": {"similar_listings_count": 15},
         "concentration_metrics": {"metrics": {"top3_creator_revenue_share": {"value": 25.0},
                                               "top3_video_revenue_share": {"value": 28.0}}},
         "red_flags": [], "source": {"discovery_status": "PASS"}}
    d.update(kw)
    return d


def amz(**kw):
    a = {"amazon_competition": "LOW", "price_alignment": "GOOD", "price_ratio": 1.0,
         "amazon_top3_brand_share_pct": 20.0, "cross_platform_demand": "STRONG", "avs": 78.0,
         "amazon_confidence": 85.0, "amazon_red_flags": [], "amazon_match_name": "Bamboo Shoe Rack 5 Tier"}
    a.update(kw)
    return a


def comm(**kw):
    """Complete synthetic commercial/supplier data (high-margin case)."""
    c = {"selling_price": 40.0, "product_cost": 6.0, "supplier_shipping_cost": 4.0,
         "estimated_refund_rate_pct": 5.0, "estimated_chargeback_rate_pct": 0.5, "estimated_ad_cost_per_order": 8.0,
         "delivery_days_max": 6, "tracking_available": True,
         "fragile": False, "contains_battery": False, "contains_liquid": False, "oversized": False,
         "special_handling": False, "customs_risk": False,
         "suppliers": [{"name": "S1", "price": 6.0, "rating": 4.8, "processing_days": 1, "us_warehouse": True,
                        "stock": 2000, "moq": 1, "orders": 5000},
                       {"name": "S2", "price": 6.3, "rating": 4.6, "processing_days": 2, "us_warehouse": False,
                        "stock": 800, "moq": 1},
                       {"name": "S3", "price": 6.6, "rating": 4.5, "processing_days": 3, "us_warehouse": False,
                        "stock": 300, "moq": 10}],
         "actual_refund_rate_pct": 2.0,
         "compliance_review": {"status": "cleared", "by": "synthetic"}}
    c.update(kw)
    return c


def ev(d=None, a="default", c="default"):
    return B.evaluate(d or deep(), amz() if a == "default" else a, comm() if c == "default" else c, CFG, FILTERS)


def flags(r):
    return [f["flag"] for f in r["commercial_red_flags"]]


# ------------------------------------------------------------------ margins
class Margins(unittest.TestCase):
    def test_high_margin_product(self):
        r = ev()
        e = r["economics"]
        self.assertEqual(e["landed_cost"], 10.0)
        self.assertEqual(e["payment_processing_fee"], 1.46)             # 40 * 2.9% + 0.30
        self.assertEqual(e["gross_profit_before_ads"], 28.54)
        self.assertEqual(e["gross_margin_percent"], 71.35)
        self.assertEqual(e["expected_refund_cost"], 2.0)
        self.assertEqual(e["expected_chargeback_cost"], 0.28)           # 0.5% * (40 + 15) = 0.275
        self.assertEqual(e["contribution_profit"], 18.27)               # 28.54 - 8 - 2 - 0.275 = 18.265
        self.assertEqual(e["contribution_margin_percent"], 45.66)
        self.assertEqual(r["bvs_breakdown"]["gross_margin_potential"]["score"], 25.0)

    def test_low_margin_product(self):
        r = ev(c=comm(product_cost=22.0, supplier_shipping_cost=6.0))
        self.assertEqual(r["economics"]["gross_margin_percent"], 26.35)
        self.assertIn("LOW_GROSS_MARGIN", flags(r))
        self.assertEqual(r["bvs_breakdown"]["gross_margin_potential"]["parts"]["gross_margin"]["points"], 0.0)

    def test_negative_contribution_margin(self):
        r = ev(c=comm(product_cost=22.0, supplier_shipping_cost=6.0, estimated_ad_cost_per_order=15.0))
        self.assertLess(r["economics"]["contribution_margin_percent"], 0)
        f = next(x for x in r["commercial_red_flags"] if x["flag"] == "NEGATIVE_CONTRIBUTION_MARGIN")
        self.assertTrue(f["severe"])

    def test_missing_supplier_cost(self):
        c = comm(product_cost=None)
        c["suppliers"] = []
        r = ev(c=c)
        for k in ("product_cost", "landed_cost", "gross_profit_before_ads", "gross_margin_percent", "contribution_profit"):
            self.assertEqual(r["economics"][k], "N/A", k)
        self.assertIn("INSUFFICIENT_SUPPLIER_DATA", flags(r))
        self.assertEqual(r["bvs_breakdown"]["gross_margin_potential"]["status"], "N/A")
        self.assertEqual(r["bvs_confidence_breakdown"]["supplier_cost"]["earned"], 0.0)

    def test_supplier_price_used_as_product_cost_when_given(self):
        r = ev(c=comm(product_cost=None))
        self.assertEqual(r["economics"]["product_cost"], 6.0)
        self.assertEqual(r["economics"]["product_cost_source"], "primary supplier price")

    def test_missing_shipping_cost(self):
        r = ev(c=comm(supplier_shipping_cost=None))
        self.assertEqual(r["economics"]["landed_cost"], "N/A")
        self.assertEqual(r["bvs_breakdown"]["shipping_viability"]["parts"]["shipping_cost"]["points"], "N/A")
        self.assertEqual(r["bvs_confidence_breakdown"]["shipping_cost"]["earned"], 0.0)

    def test_missing_ad_cost_keeps_contribution_na(self):
        r = ev(c=comm(estimated_ad_cost_per_order=None))
        self.assertNotEqual(r["economics"]["gross_margin_percent"], "N/A")
        self.assertEqual(r["economics"]["contribution_margin_percent"], "N/A")

    def test_valid_numeric_zero(self):
        r = ev(c=comm(supplier_shipping_cost=0.0, estimated_ad_cost_per_order=0.0, estimated_refund_rate_pct=0.0))
        e = r["economics"]
        self.assertEqual(e["supplier_shipping_cost"], 0.0)
        self.assertEqual(e["landed_cost"], 6.0)
        self.assertEqual(e["expected_refund_cost"], 0.0)
        self.assertNotIn("supplier_shipping_cost", r["missing_data"])
        self.assertEqual(r["bvs_breakdown"]["shipping_viability"]["parts"]["shipping_cost"]["points"], 4.0)

    def test_tiktok_price_used_as_labeled_assumption(self):
        r = ev(c=comm(selling_price=None))
        self.assertEqual(r["selling_price"], 40.0)
        self.assertTrue(r["selling_price_source"].startswith("ASSUMPTION"))


# ------------------------------------------------------------------ supplier / shipping
class SupplierShipping(unittest.TestCase):
    def test_strong_supplier(self):
        r = ev()
        sp = r["bvs_breakdown"]["supplier_viability"]
        self.assertEqual(sp["score"], 15.0)
        self.assertEqual(sp["status"], "full")
        for f in ("SINGLE_SUPPLIER_DEPENDENCY", "LOW_SUPPLIER_RATING", "NO_US_WAREHOUSE", "LOW_STOCK"):
            self.assertNotIn(f, flags(r))

    def test_single_supplier_dependency(self):
        c = comm()
        c["suppliers"] = [{"name": "Only", "price": 6.0, "rating": 4.2, "processing_days": 6, "us_warehouse": False,
                           "stock": 50, "moq": 100}]
        r = ev(c=c)
        for f in ("SINGLE_SUPPLIER_DEPENDENCY", "LOW_SUPPLIER_RATING", "LONG_PROCESSING_TIME", "LOW_STOCK",
                  "MOQ_TOO_HIGH", "NO_US_WAREHOUSE"):
            self.assertIn(f, flags(r), f)
        self.assertLess(r["bvs_breakdown"]["supplier_viability"]["score"], 5)

    def test_supplier_flags_only_with_data(self):
        c = comm()
        c["suppliers"] = [{"name": "X", "price": 6.0}, {"name": "Y", "price": 6.1}]
        r = ev(c=c)
        for f in ("LOW_SUPPLIER_RATING", "LONG_PROCESSING_TIME", "LOW_STOCK", "MOQ_TOO_HIGH", "NO_US_WAREHOUSE"):
            self.assertNotIn(f, flags(r), f)
        self.assertEqual(r["bvs_breakdown"]["supplier_viability"]["status"], "partial")

    def test_slow_shipping(self):
        r = ev(c=comm(delivery_days_max=18))
        self.assertIn("SLOW_SHIPPING", flags(r))
        self.assertEqual(r["bvs_breakdown"]["shipping_viability"]["parts"]["delivery_time"]["points"], 1.5)

    def test_fragile_product(self):
        r = ev(c=comm(fragile=True))
        self.assertIn("FRAGILE", flags(r))
        self.assertEqual(r["bvs_breakdown"]["shipping_viability"]["parts"]["handling"]["points"], 3.0)
        r2 = ev(d=deep(name="Glass Vase Set"), c=comm(fragile=None))            # keyword evidence
        f = next(x for x in r2["commercial_red_flags"] if x["flag"] == "FRAGILE")
        self.assertEqual(f["evidence"], "keyword in product name")

    def test_high_shipping_cost(self):
        r = ev(c=comm(supplier_shipping_cost=10.0))                        # 25 % of 40
        self.assertIn("HIGH_SHIPPING_COST", flags(r))


# ------------------------------------------------------------------ return risk
class ReturnRisk(unittest.TestCase):
    def test_high_return_risk(self):
        r = ev(d=deep(name="Compatible Replacement Bluetooth Charger for iPhone, Size XL"),
               c=comm(actual_refund_rate_pct=None))
        self.assertIn("HIGH_RETURN_RISK", flags(r))
        self.assertIn("COMPATIBILITY_RISK", flags(r))
        self.assertIn("SIZE_DEPENDENT", flags(r))
        self.assertEqual(r["return_risk_metrics"]["basis"], "keyword_screening")
        self.assertEqual(r["bvs_confidence_breakdown"]["return_risk_evidence"]["earned"], 5.0)

    def test_actual_refund_rate_preferred(self):
        r = ev(c=comm(actual_refund_rate_pct=12.0))
        self.assertEqual(r["bvs_breakdown"]["return_risk"]["score"], 0.0)
        self.assertIn("HIGH_RETURN_RISK", flags(r))


# ------------------------------------------------------------------ competition
class Competition(unittest.TestCase):
    def test_healthy_competition(self):
        r = ev()
        self.assertEqual(r["bvs_breakdown"]["competition_opportunity"]["score"], 15.0)

    def test_low_competition_with_weak_demand(self):
        healthy = ev()["bvs_breakdown"]["competition_opportunity"]["score"]
        weak = ev(d=deep(units=200), a=amz(cross_platform_demand="WEAK"))["bvs_breakdown"]["competition_opportunity"]
        self.assertEqual(weak["demand_factor"], 0.5)
        self.assertEqual(weak["score"], 7.5)
        self.assertLess(weak["score"], healthy)

    def test_extreme_saturation(self):
        r = ev(d=deep(competition_metrics={"similar_listings_count": 350}),
               a=amz(amazon_competition="HIGH", price_alignment="POOR", price_ratio=0.6,
                     amazon_top3_brand_share_pct=70.0))
        self.assertIn("EXTREME_COMPETITION", flags(r))
        self.assertIn("PRICE_COMPRESSION", flags(r))
        self.assertEqual(r["bvs_breakdown"]["competition_opportunity"]["score"], 0.0)

    def test_no_amazon_data_still_scores_with_lower_confidence(self):
        with_a, without = ev(), ev(a=None)
        self.assertEqual(without["bvs_breakdown"]["competition_opportunity"]["parts"]["amazon_saturation"]["points"], "N/A")
        self.assertLess(without["bvs_confidence"], with_a["bvs_confidence"])


# ------------------------------------------------------------------ advertising / compliance
class AdvertisingCompliance(unittest.TestCase):
    def test_advertising_viability(self):
        r = ev()
        ad = r["bvs_breakdown"]["advertising_viability"]
        self.assertEqual(ad["parts"]["creator_diversity"]["points"], 3.0)
        self.assertEqual(ad["parts"]["ad_restriction"]["points"], 2.0)
        self.assertEqual(ad["status"], "full")

    def test_missing_advertising_evidence(self):
        r = ev(d=deep(video_metrics={}, concentration_metrics={}))
        ad = r["bvs_breakdown"]["advertising_viability"]
        for p in ("successful_videos", "creator_diversity", "creative_diversity"):
            self.assertEqual(ad["parts"][p]["points"], "N/A")
        self.assertEqual(r["bvs_confidence_breakdown"]["ad_evidence"]["earned"], 0.0)

    def test_compliance_flags(self):
        r = ev(d=deep(name="Stanley Dupe Tumbler Weight Loss Detox Supplement"), c=comm(compliance_review=None))
        for f in ("IP_REVIEW_REQUIRED", "COUNTERFEIT_REVIEW_REQUIRED", "MEDICAL_CLAIM_RISK", "REGULATED_PRODUCT",
                  "AD_POLICY_REVIEW_REQUIRED"):
            self.assertIn(f, flags(r), f)
        self.assertEqual(r["bvs_breakdown"]["compliance_ip"]["score"], 0.0)
        self.assertTrue(all("not a legal" in x["note"] for x in r["commercial_red_flags"]
                            if x["flag"] in ("IP_REVIEW_REQUIRED", "COUNTERFEIT_REVIEW_REQUIRED")))

    def test_missing_compliance_evidence(self):
        r = ev(d=deep(name=None, category=None), a=None, c=comm(compliance_review=None))
        self.assertEqual(r["bvs_breakdown"]["compliance_ip"]["score"], "N/A")
        self.assertEqual(r["bvs_confidence_breakdown"]["compliance_evidence"]["earned"], 0.0)

    def test_keyword_screening_is_half_evidence(self):
        r = ev(c=comm(compliance_review=None))
        self.assertEqual(r["compliance_metrics"]["basis"], "keyword_screening")
        self.assertEqual(r["bvs_confidence_breakdown"]["compliance_evidence"]["earned"], 5.0)


# ------------------------------------------------------------------ totals
class Totals(unittest.TestCase):
    def test_bvs_calculation(self):
        r = ev()
        bd = r["bvs_breakdown"]
        self.assertEqual({k: v["max"] for k, v in bd.items()},
                         {"gross_margin_potential": 25, "shipping_viability": 15, "supplier_viability": 15,
                          "competition_opportunity": 15, "return_risk": 10, "advertising_viability": 10,
                          "compliance_ip": 10})
        self.assertEqual(r["bvs"], round(sum(v["score"] for v in bd.values()), 2))
        self.assertGreaterEqual(r["bvs"], 95)

    def test_bvs_confidence_calculation(self):
        full = ev()
        self.assertEqual(full["bvs_confidence"], 100.0)
        self.assertEqual(full["bvs_confidence_level"], "VERY_HIGH")
        none = ev(a=None, c=None)
        # no supplier data: supplier_cost 0, shipping 0, delivery 0, supplier 0,
        # competition 1/4*0.8 + 0.2 = 0.4 -> 4, return keyword 5, ads 10, compliance keyword 5 -> 24
        self.assertEqual(none["bvs_confidence"], 24.0)
        self.assertIn("INSUFFICIENT_SUPPLIER_DATA", flags(none))

    def test_high_bvs_low_confidence_is_marked(self):
        cfg = copy.deepcopy(CFG)
        cfg["bvs_confidence"]["reliability_warning"]["bvs_min"] = 20
        r = B.evaluate(deep(), None, None, cfg, FILTERS)
        self.assertIsNotNone(r["bvs_reliability_warning"])

    def test_independent_from_other_scores(self):
        a = ev(d=deep(wps=95, conf=95), a=amz(avs=99, amazon_confidence=99))
        b = ev(d=deep(wps=70, conf=60), a=amz(avs=10, amazon_confidence=10))
        self.assertEqual(a["bvs"], b["bvs"])
        self.assertEqual(a["bvs_confidence"], b["bvs_confidence"])
        self.assertEqual(a["other_scores"]["wps"], 95)

    def test_flags_do_not_hide_product(self):
        r = ev(c=comm(product_cost=30.0, estimated_ad_cost_per_order=20.0))
        self.assertIn("NEGATIVE_CONTRIBUTION_MARGIN", flags(r))
        self.assertIsNotNone(r["bvs"])                                  # product still returned with its score

    def test_deterministic(self):
        a, b = ev(), ev()
        a.pop("observation_timestamp"), b.pop("observation_timestamp")
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))


# ------------------------------------------------------------------ eligibility + storage
class RunAndStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.raw, self.out = t / "raw", t / "out"
        self.deep_path = t / "deep_x.json"

    def tearDown(self):
        for p in self.raw.glob("*"):
            os.chmod(p, 0o644)
        self.tmp.cleanup()

    def test_eligibility(self):
        rows, ex = B.eligible([deep("1"), deep("2", wps=69), deep("3", conf=59),
                               deep("4", source={"discovery_status": "FAIL"})], CFG)
        self.assertEqual([r["product_id"] for r in rows], ["1"])
        self.assertEqual(len(ex), 3)

    def test_historical_raw_data_preservation(self):
        p1 = B.save_commercial_data("1", comm(product_cost=6.0), raw_dir=self.raw)
        h1 = hashlib.sha256(p1.read_bytes()).hexdigest()
        p2 = B.save_commercial_data("1", comm(product_cost=7.0), raw_dir=self.raw)
        self.assertNotEqual(p1, p2)
        self.assertEqual(hashlib.sha256(p1.read_bytes()).hexdigest(), h1)   # old observation untouched
        self.assertFalse(os.stat(p1).st_mode & 0o222)                        # read-only
        data, src = B.load_commercial_data("1", self.raw)
        self.assertEqual(data["product_cost"], 7.0)                          # latest used
        self.assertEqual(src, str(p2))

    def test_run_end_to_end_synthetic(self):
        self.deep_path.write_text(json.dumps({"results": [deep("1"), deep("2", wps=50)]}))
        B.save_commercial_data("1", comm(), raw_dir=self.raw)
        r = B.run(deep_path=self.deep_path, amazon_path=None, raw_dir=self.raw, out_dir=self.out,
                  cfg=CFG, filters_cfg=FILTERS)
        self.assertEqual(r["eligible"], 1)
        res = r["results"][0]
        for k in ("product_id", "product_name", "selling_price", "economics", "supplier_metrics", "shipping_metrics",
                  "competition_metrics", "return_risk_metrics", "advertising_metrics", "compliance_metrics", "bvs",
                  "bvs_breakdown", "bvs_confidence", "commercial_red_flags", "missing_data",
                  "observation_timestamp", "source"):
            self.assertIn(k, res, k)
        self.assertTrue(Path(r["saved"]).exists())
        r2 = B.run(deep_path=self.deep_path, amazon_path=None, raw_dir=self.raw, out_dir=self.out,
                   cfg=CFG, filters_cfg=FILTERS)
        self.assertNotEqual(r["saved"], r2["saved"])                          # never overwritten


if __name__ == "__main__":
    unittest.main()
