"""Tests for Amazon Validation (Step N). SYNTHETIC DATA ONLY, in temp folders
(never mixed with real research data). Run: python3 -m unittest discover tests
"""
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import amazon_validation as AV  # noqa: E402
import deep_analysis as DA  # noqa: E402
import discovery as disc  # noqa: E402

CFG = AV.load_cfg()
FILTERS = disc.load_yaml("filters.yaml")
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

TT_NAME = "Acme 3-in-1 Bamboo Shoe Rack Organizer 5 Tier Entryway Storage"


def deep(pid="1", wps=80.0, conf=75.0, status="PASS", **kw):
    d = {"status": "ok", "product_id": pid, "product_name": TT_NAME,
         "category": "Home Supplies > Home Organizers > Shoe Racks",
         "shop": {"shop_id": "9", "shop_name": "Acme Home", "brand": "Acme"},
         "price": {"min": 30.0, "max": 40.0, "avg": 35.0}, "units": 3000,
         "trend_metrics": {"label": "GROWING"}, "wps": wps, "confidence": conf,
         "source": {"discovery_status": status}}
    d.update(kw)
    return d


def cand(**kw):
    c = {"title": "Acme 3-in-1 Bamboo Shoe Rack 5 Tier Entryway Organizer Storage",
         "url": "https://amazon.example/dp/SYN1", "asin": "SYN0000001", "brand": "Acme", "seller": "Acme",
         "category_path": "Home & Kitchen > Storage & Organization > Shoe Racks", "price": 38.0,
         "rating": 4.5, "review_count": 1500, "bsr": 5000, "bsr_category": "Home & Kitchen",
         "monthly_sales_estimate": 900, "monthly_sales_source": "synthetic", "listing_date": "2025-01-01",
         "attributes": ["5 tier", "bamboo"]}
    c.update(kw)
    return c


def provider_obj(cands=None, **search):
    s = {"keyword": "bamboo shoe rack", "comparable_listings_count": 500, "comparable_price_min": 20.0,
         "comparable_price_max": 60.0, "top_brands": [{"brand": "A", "share_pct": 15}, {"brand": "B", "share_pct": 10}],
         "recent_trend": "growing", "sources": ["synthetic"]}
    s.update(search)
    return {"candidates": [cand()] if cands is None else cands, "search": s}


def env(obj, pid="1", ts=None):
    return {"source": "kalopilot", "observation_timestamp": (ts or NOW).isoformat(), "market": "US",
            "product_id": pid, "query_type": "amazon_match", "task_id": "t",
            "response": {"success": True, "data": {"status": "completed",
                                                   "report": "```json\n" + json.dumps(obj) + "\n```"}}}


def validate(obj, d=None):
    return AV.validate(env(obj), "synthetic.json", d or deep(), CFG, FILTERS)


class FakeClient:
    def __init__(self, balance=100.0, cost=4.0):
        self.balance, self.cost, self.submits = balance, cost, []

    def credits(self):
        return {"totalRemain": self.balance}

    def submit(self, q):
        self.submits.append(q)
        self.balance -= self.cost
        return {"success": True, "data": {"task_id": f"t{len(self.submits)}"}}

    def wait(self, task_id):
        return {"success": True, "data": {"status": "completed",
                                          "report": "```json\n" + json.dumps(provider_obj()) + "\n```"}}


# ------------------------------------------------------------------ Stage 1
class Eligibility(unittest.TestCase):
    def test_wps_below_threshold_excluded(self):
        el, ex = AV.select_eligible([deep(wps=69.99)], CFG)
        self.assertEqual(el, [])
        self.assertIn("WPS", ex[0]["reason"])

    def test_confidence_below_threshold_excluded(self):
        el, ex = AV.select_eligible([deep(conf=59.99)], CFG)
        self.assertEqual(el, [])
        self.assertIn("Confidence", ex[0]["reason"])

    def test_eligible_product_proceeds_including_review(self):
        el, _ = AV.select_eligible([deep("1", 70, 60), deep("2", 85, 90, status="REVIEW")], CFG)
        self.assertEqual([d["product_id"] for d in el], ["2", "1"])     # WPS desc

    def test_fail_and_na_wps_excluded(self):
        el, ex = AV.select_eligible([deep("1", status="FAIL"), deep("2", wps="N/A")], CFG)
        self.assertEqual(el, [])
        self.assertEqual(len(ex), 2)

    def test_thresholds_from_yaml(self):
        cfg = copy.deepcopy(CFG)
        cfg["minimum_wps"] = 90
        self.assertEqual(AV.select_eligible([deep(wps=85)], cfg)[0], [])

    def test_max_20(self):
        el, ex = AV.select_eligible([deep(str(i), wps=70 + i / 10) for i in range(30)], CFG)
        self.assertEqual(len(el), 20)
        self.assertEqual(el[0]["product_id"], "29")                       # highest WPS first
        self.assertEqual(sum("over max_products" in e["reason"] for e in ex), 10)


# ------------------------------------------------------------------ Stage 2
class Matching(unittest.TestCase):
    def test_exact_match(self):
        r = validate(provider_obj())
        self.assertGreaterEqual(r["amazon_match_confidence"], 90)
        self.assertEqual(r["amazon_match_class"], "EXACT")
        self.assertEqual(r["amazon_match_status"], "MATCHED")
        self.assertEqual(r["amazon_url"], "https://amazon.example/dp/SYN1")

    def test_possible_match(self):
        c = cand(title="Bamboo Shoe Rack 5 Tier Entryway Organizer", brand="OtherCo",
                 category_path="Home & Kitchen > Storage > Racks")
        r = validate(provider_obj([c]))
        self.assertEqual(r["amazon_match_class"], "POSSIBLE", r["amazon_match_confidence"])
        self.assertEqual(r["amazon_match_status"], "MATCHED")

    def test_unreliable_match_is_not_used(self):
        c = cand(title="Stainless Steel Kitchen Knife Set 12 Piece", brand="Zeta",
                 category_path="Kitchen > Knives", attributes=[])
        r = validate(provider_obj([c]))
        self.assertLess(r["amazon_match_confidence"], 60)
        self.assertEqual(r["amazon_match_class"], "UNRELIABLE")
        self.assertEqual(r["amazon_match_status"], "NO_RELIABLE_MATCH")
        for k in ("amazon_match_name", "amazon_url", "amazon_price", "amazon_rating", "amazon_review_count"):
            self.assertIsNone(r[k], k)                                    # never populated from unrelated item
        self.assertIn("NO_RELIABLE_MATCH", [f["flag"] for f in r["amazon_red_flags"]])

    def test_no_match(self):
        r = validate(provider_obj([], comparable_listings_count=0))
        self.assertEqual(r["amazon_match_status"], "NO_RELIABLE_MATCH")
        self.assertEqual(r["amazon_presence"], "NO")
        self.assertEqual(r["amazon_match_confidence"], 0.0)

    def test_best_candidate_chosen_deterministically(self):
        bad = cand(title="Garden Hose 50 ft", brand="X", category_path="Garden", attributes=[])
        r = validate(provider_obj([bad, cand()]))
        self.assertEqual(r["amazon_url"], "https://amazon.example/dp/SYN1")

    def test_attribute_tokens(self):
        self.assertEqual(AV.attr_tokens("3-in-1 rack, 5 Tier, 2 Pack, 6FT, 32 oz"),
                         {"3in1", "5tier", "2pack", "6ft", "32oz"})


# ------------------------------------------------------------------ Stage 3/4
class DataAndSignals(unittest.TestCase):
    def test_missing_amazon_data_is_na(self):
        r = validate(provider_obj([cand(bsr=None, monthly_sales_estimate=None, rating="N/A")],
                                  top_brands=None, comparable_listings_count=None))
        self.assertIsNone(r["amazon_bsr"])
        self.assertIsNone(r["amazon_rating"])
        for k in ("amazon_bsr", "amazon_sales_indicator", "amazon_rating", "amazon_top_brands", "amazon_competitor_count"):
            self.assertIn(k, r["missing_data"])
        self.assertEqual(r["avs_breakdown"]["amazon_demand_evidence"]["status"], "partial")
        self.assertLess(r["amazon_confidence"], 100)

    def test_valid_numeric_zero(self):
        r = validate(provider_obj([cand(review_count=0)], comparable_listings_count=0))
        self.assertEqual(r["amazon_review_count"], 0)
        self.assertEqual(r["amazon_competitor_count"], 0)
        self.assertNotIn("amazon_review_count", r["missing_data"])
        self.assertEqual(r["amazon_competition"], "LOW")                  # 0 is real data
        self.assertEqual(r["amazon_demand_signals"]["review_count"], 0.0)

    def test_high_competition(self):
        self.assertEqual(validate(provider_obj(comparable_listings_count=1500))["amazon_competition"], "HIGH")
        self.assertEqual(validate(provider_obj([cand(review_count=25000)]))["amazon_competition"], "HIGH")
        r = validate(provider_obj(top_brands=[{"brand": "a", "share_pct": 40}, {"brand": "b", "share_pct": 25}]))
        self.assertEqual(r["amazon_competition"], "HIGH")
        self.assertIn("AMAZON_HIGH_SATURATION", [f["flag"] for f in r["amazon_red_flags"]])
        self.assertIn("AMAZON_DOMINATED_BY_MAJOR_BRANDS", [f["flag"] for f in r["amazon_red_flags"]])

    def test_low_competition(self):
        r = validate(provider_obj([cand(review_count=300)], comparable_listings_count=150))
        self.assertEqual(r["amazon_competition"], "LOW")

    def test_moderate_and_insufficient_competition(self):
        self.assertEqual(validate(provider_obj())["amazon_competition"], "MODERATE")        # 500 listings, 1500 reviews
        r = validate(provider_obj([cand(review_count=None)], comparable_listings_count=None, top_brands=None))
        self.assertEqual(r["amazon_competition"], "INSUFFICIENT_DATA")

    def test_price_alignment(self):
        cases = {38.0: "GOOD", 28.0: "MIXED", 70.0: "MIXED", 20.0: "POOR", 100.0: "POOR"}   # tiktok avg 35
        for price, label in cases.items():
            self.assertEqual(validate(provider_obj([cand(price=price)]))["price_alignment"], label, price)
        r = validate(provider_obj([cand(price=20.0)]))
        self.assertIn("AMAZON_PRICE_COMPRESSION", [f["flag"] for f in r["amazon_red_flags"]])
        self.assertEqual(validate(provider_obj([cand(price=None)]))["price_alignment"], "INSUFFICIENT_DATA")

    def test_cross_platform_demand(self):
        self.assertEqual(validate(provider_obj())["cross_platform_demand"], "STRONG")
        weak = cand(review_count=60, bsr=90000, monthly_sales_estimate=120)
        self.assertEqual(validate(provider_obj([weak]))["cross_platform_demand"], "WEAK")
        self.assertEqual(validate(provider_obj(), deep(units=800))["cross_platform_demand"], "MODERATE")
        self.assertEqual(validate(provider_obj(), deep(units=None))["cross_platform_demand"], "INSUFFICIENT_DATA")
        none = cand(review_count=None, bsr=None, monthly_sales_estimate=None)
        self.assertEqual(validate(provider_obj([none]))["cross_platform_demand"], "INSUFFICIENT_DATA")


# ------------------------------------------------------------------ Stage 5/6
class Scores(unittest.TestCase):
    def test_avs_calculation(self):
        r = validate(provider_obj())
        bd = r["avs_breakdown"]
        # demand: review 1500 -> log(1500/50)/log(100)=0.7386; bsr 5000 -> log(100000/5000)/2=0.6505;
        # monthly 900 -> log(9)/log(30)=0.6460 ; mean 0.6784 -> 20.35
        self.assertEqual(bd["amazon_demand_evidence"]["points"], 20.35)
        self.assertEqual(bd["match_confidence"]["points"], round(r["amazon_match_confidence"] / 100 * 20, 2))
        self.assertEqual(bd["competition_opportunity"]["points"], 10.0)    # MODERATE
        self.assertEqual(bd["price_alignment"]["points"], 15.0)            # GOOD
        self.assertEqual(bd["cross_platform_consistency"]["points"], 15.0) # GROWING vs growing
        self.assertAlmostEqual(r["avs"], sum(v["points"] for v in bd.values()), places=1)
        self.assertEqual(sum(v["max"] for v in bd.values()), 100)

    def test_avs_na_components_score_zero(self):
        r = validate(provider_obj([], comparable_listings_count=None, recent_trend=None))
        bd = r["avs_breakdown"]
        self.assertEqual(bd["amazon_demand_evidence"]["points"], "N/A")
        self.assertEqual(bd["price_alignment"]["points"], "N/A")
        self.assertEqual(bd["cross_platform_consistency"]["points"], "N/A")
        self.assertEqual(r["avs"], 0.0)

    def test_consistency_opposite(self):
        r = validate(provider_obj(recent_trend="declining"))
        self.assertEqual(r["cross_platform_consistency"], 0.0)

    def test_amazon_confidence_calculation(self):
        r = validate(provider_obj())
        bd = r["amazon_confidence_breakdown"]
        for name in ("price", "rating_reviews", "ranking_demand", "comparable_listings", "competition_data"):
            self.assertEqual(bd[name]["earned"], bd[name]["max"], name)
        self.assertEqual(r["amazon_confidence"], round(70 + r["amazon_match_confidence"] * 0.3, 2))
        thin = validate(provider_obj([cand(bsr=None, monthly_sales_estimate=None)], top_brands=None))
        self.assertEqual(thin["amazon_confidence_breakdown"]["ranking_demand"]["earned"], 0.0)
        self.assertEqual(thin["amazon_confidence_breakdown"]["competition_data"]["earned"], 0.0)

    def test_scores_are_independent_from_wps(self):
        a = validate(provider_obj(), deep(wps=95, conf=95))
        b = validate(provider_obj(), deep(wps=70, conf=60))
        self.assertEqual(a["avs"], b["avs"])
        self.assertEqual(a["amazon_confidence"], b["amazon_confidence"])
        self.assertEqual(a["tiktok_scores"]["wps"], 95)                    # WPS untouched, shown separately

    def test_ip_review_required_not_a_claim(self):
        r = validate(provider_obj([cand(title="Acme 3-in-1 Shoe Rack Stanley style 5 Tier", brand="Acme")]))
        f = next(x for x in r["amazon_red_flags"] if x["flag"] == "IP_REVIEW_REQUIRED")
        self.assertIn("not a legal", f["note"])

    def test_report_format_keeps_scores_separate(self):
        text = AV.format_report(validate(provider_obj()))
        self.assertIn("WPS: 80.0/100", text)
        self.assertIn("AVS:", text)
        self.assertIn("Cross-platform Demand: STRONG", text)

    def test_deterministic(self):
        self.assertEqual(json.dumps(validate(provider_obj()), sort_keys=True),
                         json.dumps(validate(provider_obj()), sort_keys=True))


# ------------------------------------------------------------------ Stage 8/9
class Runs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.raw, self.out, self.deep_dir = t / "raw", t / "out", t / "deep"
        self.deep_dir.mkdir()

    def tearDown(self):
        for p in self.raw.glob("*"):
            os.chmod(p, 0o644)
        self.tmp.cleanup()

    def run_av(self, results, **kw):
        path = self.deep_dir / "deep_20260930T000000Z.json"
        path.write_text(json.dumps({"results": results}))
        return AV.run(deep_path=path, now=NOW, raw_dir=self.raw, out_dir=self.out, cfg=CFG, filters_cfg=FILTERS, **kw)

    def test_dry_run_spends_nothing(self):
        c = FakeClient(balance=50)
        r = self.run_av([deep("1"), deep("2")], live=False, client=c)
        self.assertEqual(r["mode"], "dry_run")
        self.assertEqual(c.submits, [])
        self.assertEqual(r["plan"]["paid_queries"], 2)
        self.assertEqual(r["plan"]["estimated_credits"], 8.0)
        self.assertFalse(self.raw.exists())
        self.assertFalse(self.out.exists())

    def test_no_deep_results_dry_run(self):
        r = AV.run(deep_dir=self.deep_dir, now=NOW, raw_dir=self.raw, out_dir=self.out, cfg=CFG, filters_cfg=FILTERS)
        self.assertIn("no deep-analysis results", r["note"])

    def test_duplicate_query_prevention(self):
        c = FakeClient()
        r = self.run_av([deep("1"), deep("1"), deep("2")], live=True, client=c)
        self.assertEqual(len(c.submits), 2)
        self.assertEqual(sum(i["action"] == "skip_duplicate" for i in r["plan"]["items"]), 1)

    def test_cache_behavior(self):
        self.raw.mkdir(parents=True)
        DA.save_raw_deep(env(provider_obj())["response"],
                         {"observation_timestamp": (NOW - timedelta(hours=3)).isoformat(), "market": "US",
                          "product_id": "1", "query_type": "amazon_match"}, self.raw)
        c = FakeClient()
        r = self.run_av([deep("1"), deep("2")], live=True, client=c)
        self.assertEqual(len(c.submits), 1)
        self.assertTrue(next(x for x in r["results"] if x["product_id"] == "1")["source"]["cache_hit"])
        cfg = copy.deepcopy(CFG)
        cfg["cache_hours"] = 1                                             # configurable cache age
        path = self.deep_dir / "deep_x.json"
        path.write_text(json.dumps({"results": [deep("1")]}))
        r2 = AV.run(deep_path=path, now=NOW, raw_dir=self.raw, out_dir=self.out, cfg=cfg, filters_cfg=FILTERS)
        self.assertEqual(r2["plan"]["paid_queries"], 1)

    def test_insufficient_credits_stops_safely(self):
        c = FakeClient(balance=13.0)
        r = self.run_av([deep(str(i)) for i in range(4)], live=True, client=c)
        self.assertEqual(len(c.submits), 2)                                # need >= 9: 13 ok, 9 ok, 5 stop
        self.assertEqual(r["stopped"]["reason"], "insufficient_credits")
        self.assertTrue(Path(r["saved"]).exists())

    def test_raw_data_preservation(self):
        c = FakeClient()
        r = self.run_av([deep("1")], live=True, client=c)
        raw = sorted(self.raw.glob("*.json"))
        self.assertEqual(len(raw), 1)
        h = hashlib.sha256(raw[0].read_bytes()).hexdigest()
        self.assertFalse(os.stat(raw[0]).st_mode & 0o222)                  # read-only
        e = json.loads(raw[0].read_text())
        for k in ("observation_timestamp", "market", "product_id", "query_type", "provider"):
            self.assertIn(k, e)
        self.run_av([deep("1")], live=True, client=FakeClient())           # cache reuse, no overwrite
        self.assertEqual(hashlib.sha256(raw[0].read_bytes()).hexdigest(), h)
        self.assertEqual(r["results"][0]["source"]["raw_file"], str(raw[0]))

    def test_saved_schema_fields(self):
        r = self.run_av([deep("1")], live=True, client=FakeClient())
        res = r["results"][0]
        for k in ("product_id", "tiktok_product_name", "amazon_match_name", "amazon_url", "amazon_match_confidence",
                  "amazon_match_status", "amazon_price", "amazon_category", "amazon_rating", "amazon_review_count",
                  "amazon_bsr", "amazon_sales_indicator", "amazon_competitor_count", "amazon_price_range",
                  "cross_platform_demand", "amazon_competition", "price_alignment", "avs", "avs_breakdown",
                  "amazon_confidence", "amazon_red_flags", "missing_data", "observation_timestamp", "source"):
            self.assertIn(k, res, k)


if __name__ == "__main__":
    unittest.main()
