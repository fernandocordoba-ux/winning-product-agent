"""Step X — Competitor Intelligence. SYNTHETIC data only; no network, no scraping.
Run: python3 -m unittest discover tests
"""
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import competitors as C  # noqa: E402

CFG = C.load_cfg()
NAME = "Bamboo Expandable Drawer Divider Organizer 4 Pack"
DEEP = {"product_id": "9001", "product_name": NAME, "units": 40000, "price": {"avg": 34.99}}
LOW_DEMAND = {**DEEP, "units": 900}


def row(i=1, **kw):
    base = {"matched_product_id": "9001", "competitor_name": f"Store {i}",
            "competitor_url": f"https://store{i}.example.test",
            "product_url": f"https://store{i}.example.test/products/bamboo-drawer-divider",
            "product_title": "Bamboo Expandable Drawer Divider Organizer 4 Pack", "selling_price": 30 + i,
            "platform": "shopify", "meta_ads": "yes", "ads": 4, "ad_start_date": "2026-08-01",
            "observed_at": "2026-09-30T12:00:00+00:00", "free_shipping": "yes", "bundle": "no",
            "guarantee": "no", "returns_messaging": "yes", "reviews": 100 * i, "traffic": 5000,
            "clear_branding": "yes", "mobile_friendly": "yes", "product_page_complete": "yes",
            "trust_elements": "no", "reviews_visible": "yes", "shipping_clarity": "yes", "returns_clarity": "no",
            "offer_structure_clear": "yes"}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def obs(*rows):
    out = []
    for r in rows:
        c, errs = C.normalize_row(r, {"retrieved_at": "2026-09-30T12:00:00+00:00"}, CFG)
        assert not errs, errs
        out.append(c)
    return out


def analyze(rows, deep=DEEP, **kw):
    return C.analyze_product("9001", deep, obs(*rows), CFG, **kw)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        for p in self.tmp.rglob("*"):
            p.chmod(0o755 if p.is_dir() else 0o644)
        shutil.rmtree(self.tmp, ignore_errors=True)


class Schema(unittest.TestCase):
    def test_schema_and_nulls(self):
        c = obs(row())[0]
        for k in C.SCHEMA:
            self.assertIn(k, c)
        self.assertEqual((c["platform"], c["competitor_domain"], c["meta_ads_present"]), ("SHOPIFY", "store1.example.test", True))
        self.assertIsNone(c["estimated_sales"])                                 # never invented
        self.assertTrue(c["competitor_id"].startswith("cmp_"))

    def test_discount_only_from_visible_prices(self):
        c = obs(row(compare_at_price=50, selling_price=35))[0]
        self.assertEqual(c["discount_percent"], 30.0)
        self.assertIsNone(obs(row())[0]["discount_percent"])

    def test_invalid_rows(self):
        _, errs = C.normalize_row(row(selling_price=None, product_url="store.test/x", platform="amazonish",
                                      estimated_sales=500, currency="GBP"), {}, CFG)
        joined = " ".join(errs)
        for s in ("selling_price", "product_url must start", "platform must be", "estimated_sales requires",
                  "currency GBP"):
            self.assertIn(s, joined)

    def test_only_real_providers(self):
        self.assertIsInstance(C.get_provider("manual_import", CFG), C.CompetitorProvider)
        for n in ("meta_ad_library", "google_search", "minea", "dropship_io", "similarweb", "builtwith", "kalopilot"):
            with self.assertRaises(C.ProviderNotIntegrated):
                C.get_provider(n, CFG)


class Relationships(unittest.TestCase):
    def test_direct_competitor(self):
        a = analyze([row(1)])
        self.assertEqual((a["direct_competitors"], a["competitors"][0]["relationship"]), (1, "DIRECT"))

    def test_adjacent_competitor(self):
        a = analyze([row(1, product_title="Bamboo Drawer Organizer Tray Set 6 Pack", match_confidence=65)])
        self.assertEqual((a["direct_competitors"], a["adjacent_competitors"]), (0, 1))

    def test_category_competitor_not_direct(self):
        a = analyze([row(1, product_title="Acrylic Makeup Organizer Tray", match_confidence=None)])
        rel = a["competitors"][0]["relationship"]
        self.assertIn(rel, ("CATEGORY", "UNRELATED"))
        self.assertEqual(a["direct_competitors"], 0)

    def test_unreliable_match_excluded_and_flagged(self):
        a = analyze([row(i, product_title="Stainless Steel Garden Hose Reel") for i in range(1, 4)] + [row(9)])
        self.assertEqual(a["direct_competitors"], 1)
        self.assertEqual(a["qualified_competitor_count"], 1)
        self.assertIn("UNRELIABLE_COMPETITOR_MATCHES", [f["flag"] for f in a["red_flags"]])
        self.assertEqual(a["prices"]["n"], 1)                                   # unreliable prices not used

    def test_claim_can_downgrade_never_upgrade(self):
        a = analyze([row(1, relationship="category")])
        self.assertEqual(a["competitors"][0]["relationship"], "CATEGORY")
        b = analyze([row(1, product_title="Acrylic Makeup Organizer Tray", relationship="direct")])
        self.assertNotEqual(b["competitors"][0]["relationship"], "DIRECT")

    def test_duplicate_competitor(self):
        a = analyze([row(1), row(1, reviews=None), row(2)])
        self.assertEqual((a["observed"], a["duplicates_removed"], a["direct_competitors"]), (2, 1, 2))


class Pricing(unittest.TestCase):
    def test_price_distribution(self):
        a = analyze([row(i, selling_price=p) for i, p in enumerate([24.99, 29.99, 34.99, 49.99], 1)])
        pr = a["prices"]
        self.assertEqual((pr["lowest_price"], pr["median_price"], pr["highest_price"]), (24.99, 32.49, 49.99))
        self.assertEqual(pr["average_price"], 34.99)
        self.assertEqual(pr["our_target_price_vs_median_pct"], round((34.99 - 32.49) / 32.49 * 100, 2))
        self.assertEqual(pr["price_position"], "PRICE_ALIGNED")
        self.assertEqual(analyze([row(i, selling_price=20) for i in range(1, 4)])["prices"]["price_position"],
                         "PRICE_PREMIUM")

    def test_price_compression(self):
        a = analyze([row(i, selling_price=p) for i, p in enumerate([29.99, 30.49, 30.99, 31.49], 1)])
        self.assertIn("PRICE_COMPRESSION", [f["flag"] for f in a["red_flags"]])
        self.assertGreater(a["saturation"]["components"]["price_compression"], 0.8)

    def test_distribution_needs_three_prices(self):
        self.assertIsNone(analyze([row(1), row(2)])["prices"]["median_price"])


class Ads(unittest.TestCase):
    def test_active_ad_presence(self):
        a = analyze([row(1), row(2, meta_ads="no", ads=0, ad_start_date=None), row(3, meta_ads=None, ads=None,
                                                                                    ad_start_date=None)])
        ads = a["ads"]
        self.assertEqual((ads["active_meta_advertisers"], ads["direct_with_ad_data"]), (1, 2))
        self.assertEqual(ads["advertiser_share"], 0.5)
        self.assertEqual(ads["spend"], "not estimated (never available as a fact)")

    def test_ad_longevity(self):
        a = analyze([row(1, ad_start_date="2026-09-27"), row(2, ad_start_date="2026-09-10"),
                     row(3, ad_start_date="2026-07-15"), row(4, ad_start_date="2025-12-01")])
        by = {c["competitor_name"]: (c["ad_age_days"], c["ad_longevity"]) for c in a["competitors"]}
        self.assertEqual(by["Store 1"], (3, "very_new"))
        self.assertEqual(by["Store 2"], (20, "new"))
        self.assertEqual(by["Store 3"], (77, "established"))
        self.assertEqual(by["Store 4"][1], "long_running")
        fl = [f for f in a["red_flags"] if f["flag"] == "LONG_RUNNING_COMPETITOR_ADS"][0]
        self.assertIn("not proof of profitability", fl["note"])
        self.assertIn("NOT evidence of profitability", a["ad_longevity_limitation"])


class Scores(unittest.TestCase):
    def test_competitor_saturation(self):
        heavy = analyze([row(i, selling_price=30 + i * 0.2, reviews=100) for i in range(1, 16)])
        light = analyze([row(1, meta_ads="no", selling_price=20), row(2, meta_ads="no", selling_price=35, bundle="yes"),
                         row(3, meta_ads="no", selling_price=50, guarantee="yes")])
        self.assertGreater(heavy["saturation"]["score"], light["saturation"]["score"])
        self.assertIn("EXTREME_DIRECT_COMPETITION", [f["flag"] for f in heavy["red_flags"]])
        self.assertIn("META_AD_SATURATION", [f["flag"] for f in heavy["red_flags"]])
        comp = heavy["saturation"]["components"]
        self.assertEqual(comp["direct_competitor_count"], 0.75)                 # 15 / 20

    def test_competitive_opportunity_not_inverse(self):
        a = analyze([row(1), row(2), row(3)])
        self.assertIsNotNone(a["opportunity"]["score"])
        self.assertNotEqual(a["opportunity"]["score"], round(100 - a["saturation"]["score"], 2))

    def test_low_competition_low_demand_not_high(self):
        few = [row(1, meta_ads="no", free_shipping="no", selling_price=20),
               row(2, meta_ads="no", free_shipping="no", selling_price=45)]
        low = analyze(few, deep=LOW_DEMAND)
        high = analyze(few, deep=DEEP)
        self.assertLessEqual(low["opportunity"]["score"], 30)                   # demand cap
        self.assertEqual(low["opportunity"]["demand_cap_applied"], 30)
        self.assertGreater(high["opportunity"]["score"], low["opportunity"]["score"])

    def test_no_demand_evidence_is_na(self):
        a = analyze([row(1)], deep={"product_name": NAME})
        self.assertIsNone(a["opportunity"]["score"])

    def test_high_demand_moderate_competition(self):
        rows = [row(i, selling_price=p, meta_ads=m) for i, (p, m) in
                enumerate([(24.99, "yes"), (32.99, "no"), (39.99, "yes"), (44.99, "no"), (49.99, "no")], 1)]
        a = analyze(rows, landed_cost=11.0)
        self.assertGreaterEqual(a["opportunity"]["score"], 50)
        self.assertEqual(a["opportunity"]["demand_cap_applied"], None)
        self.assertIsNotNone(a["opportunity"]["components"]["supplier_economics"])

    def test_dominant_brand(self):
        a = analyze([row(1, reviews=9000), row(2, reviews=300), row(3, reviews=200)])
        fl = [f for f in a["red_flags"] if f["flag"] == "DOMINANT_BRAND"]
        self.assertEqual(fl[0]["competitor"], "Store 1")

    def test_competitor_confidence(self):
        full = analyze([row(i) for i in range(1, 6)])
        thin = analyze([row(1, meta_ads=None, ads=None, ad_start_date=None, traffic=None, platform=None,
                            free_shipping=None, bundle=None, guarantee=None, returns_messaging=None)])
        self.assertGreaterEqual(full["confidence"]["score"], 90)
        self.assertLess(thin["confidence"]["score"], 60)
        self.assertIn("INSUFFICIENT_COMPETITOR_DATA", [f["flag"] for f in thin["red_flags"]])

    def test_store_quality_only_observable(self):
        c = C.enrich(obs(row())[0], {"name": NAME}, CFG)
        self.assertEqual(c["store_quality_score"], 80.0)      # 15+15+15+15+10+10 true of 100 observable points
        bare = C.enrich(obs(row(clear_branding=None, mobile_friendly=None, product_page_complete=None,
                                trust_elements=None, reviews_visible=None, shipping_clarity=None,
                                returns_clarity=None, offer_structure_clear=None))[0], {"name": NAME}, CFG)
        self.assertIsNone(bare["store_quality_score"])


class Differentiation(unittest.TestCase):
    def test_offer_differentiation_with_evidence(self):
        a = analyze([row(i, bundle="no", free_shipping="no", guarantee="no", returns_messaging="no", meta_ads="no",
                         selling_price=30 + i) for i in range(1, 5)])
        gaps = {o["gap"]: o["evidence"] for o in a["differentiation"]["opportunities"]}
        self.assertEqual(gaps["bundle_gap"], "0/4 direct competitors show a bundle / quantity break / BOGO")
        self.assertIn("shipping_gap", gaps)
        self.assertIn("offer_gap", gaps)
        self.assertIn("creative_gap", gaps)
        for o in a["differentiation"]["opportunities"]:
            self.assertTrue(o["evidence"])                                     # every opportunity has facts

    def test_identical_offers_low_differentiation(self):
        a = analyze([row(i, bundle="yes", free_shipping="yes", guarantee="yes", returns_messaging="yes",
                         selling_price=30 + i * 0.3, reviews=200) for i in range(1, 5)])
        fl = [f["flag"] for f in a["red_flags"]]
        self.assertIn("IDENTICAL_OFFERS", fl)
        self.assertIn("LOW_DIFFERENTIATION", fl)

    def test_not_enough_evidence(self):
        self.assertFalse(analyze([row(1)])["differentiation"]["available"])


class BVSAdapter(unittest.TestCase):
    def test_adapter_prepared_not_wired(self):
        a = analyze([row(i) for i in range(1, 6)])
        b = C.bvs_inputs(a)
        for k in ("competitive_opportunity", "price_compression", "direct_competitor_count", "advertising_saturation"):
            self.assertIn(k, b)
        src = (ROOT / "scripts" / "business_viability.py").read_text()
        self.assertNotIn("competitors", src)                                   # BVS does not consume it yet


class Storage(Tmp):
    def test_manual_csv_import_and_raw_preservation(self):
        f = self.tmp / "c.csv"
        f.write_text("matched_product_id,competitor_name,competitor_url,product_url,selling_price,ads,reviews,traffic,"
                     "shipping,bundles,discount,store_platform\n"
                     "9001,Store A,https://a.example.test,https://a.example.test/p/x,32.99,5,120,4000,Free US,yes,,shopify\n"
                     "9001,Store B,https://b.example.test,,29.99,,,,,,,\n")
        r = C.import_file(f, raw_dir=self.tmp / "raw", processed_dir=self.tmp / "proc")
        self.assertEqual((r["accepted"], len(r["rejected"])), (1, 1))
        raw = Path(r["raw_file"])
        self.assertEqual(len(json.loads(raw.read_text())["rows"]), 2)           # raw keeps everything
        self.assertEqual(oct(raw.stat().st_mode)[-3:], "444")                    # read-only
        c = C.load_latest("9001", self.tmp / "proc")[0]
        self.assertEqual((c["active_ads_count"], c["bundle_offer"], c["platform"]), (5, True, "SHOPIFY"))

    def test_manual_json_import(self):
        f = self.tmp / "c.json"
        f.write_text(json.dumps({"offers": [row(1), row(2)]}))
        r = C.import_file(f, raw_dir=self.tmp / "raw", processed_dir=self.tmp / "proc")
        self.assertEqual(r["accepted"], 2)
        bad = self.tmp / "bad.json"
        bad.write_text("{oops")
        with self.assertRaises(C.MalformedCompetitorInput):
            C.import_file(bad, raw_dir=self.tmp / "raw", processed_dir=self.tmp / "proc")

    def test_historical_snapshots(self):
        h = self.tmp / "hist"
        a1 = analyze([row(1), row(2), row(3)])
        self.assertEqual(C.append_history(a1, h)[0], "written")
        self.assertEqual(C.append_history(a1, h)[0], "duplicate_snapshot")     # same snapshot not stored twice
        one = C.history("9001", h, CFG)
        self.assertIsNone(one["competition_velocity_per_week"])                 # never a trend from 1 observation
        a2 = analyze([row(1, observed_at="2026-10-14T12:00:00+00:00"), row(2, observed_at="2026-10-14T12:00:00+00:00"),
                      row(4, observed_at="2026-10-14T12:00:00+00:00"), row(5, observed_at="2026-10-14T12:00:00+00:00")])
        C.append_history(a2, h)
        hist = C.history("9001", h, CFG)
        ch = hist["changes"][0]
        self.assertEqual((len(ch["new_competitors"]), len(ch["disappeared_competitors"]), ch["qualified_count_change"]),
                         (2, 1, 1))
        self.assertEqual(hist["competition_velocity_per_week"], 0.5)            # +1 competitor over 14 days
        self.assertEqual(len(hist["snapshots"]), 2)

    def test_provenance_on_metrics(self):
        c = analyze([row(1, ads_library_url="https://example.test/ads/1")])["competitors"][0]
        p = c["provenance"]
        for k in ("selling_price", "active_ads_count", "review_count"):
            for f in ("provider", "source_reference", "retrieved_at", "scope", "match_confidence"):
                self.assertIn(f, p[k])
        self.assertEqual(p["active_ads_count"]["source_reference"], "https://example.test/ads/1")

    def test_calibration_small(self):
        c = CFG["competitor_calibration"]
        self.assertEqual((c["enabled"], c["max_products"], c["max_competitors_per_product"]), (False, 3, 10))
        self.assertEqual(C.calibration_plan(["1", "2", "3", "4"], CFG)["products"], [])


class ReportIntegration(Tmp):
    def test_report_section(self):
        import os
        from unittest import mock
        import safety
        import test_runner as T
        from winning_product_agent import runner as R
        pid, name = T.E.PID["E1"], T.E.NAMES["E1"]
        f = self.tmp / "c.json"
        f.write_text(json.dumps([row(i, matched_product_id=pid, product_title=name) for i in range(1, 5)]))
        C.import_file(f, raw_dir=self.tmp / "data/raw/competitors", processed_dir=self.tmp / "data/processed/competitors")
        try:
            with mock.patch.dict(os.environ, {"KALOPILOT_TOKEN": T.FAKE}), \
                    mock.patch("urllib.request.urlopen", T.no_network):
                r = R.Runner(profile_path="config/runtime_first_live.yaml", data_root=self.tmp, client=T.FakeProvider(),
                             max_products=5, preflight_fn=lambda: T.READY, isatty=False, out=lambda s="": None)
                s = r.live(confirm_value=R.CONFIRMATION_PHRASE)
        finally:
            safety.clear_run_overrides()
        md = Path(s["outputs"]["report_markdown"]).read_text()
        self.assertIn("## Competitor intelligence", md)
        self.assertIn("Qualified direct competitors: 4", md)
        js = json.loads(Path(s["outputs"]["report_json"]).read_text())
        ci = [x for x in js["competitor_intelligence"] if x["product_id"] == pid][0]
        self.assertEqual(ci["analysis"]["direct_competitors"], 4)
        top = [p for p in js["top_products"] + js["watchlist"] if p.get("product_id") == pid]
        self.assertTrue(all("competitor" not in json.dumps(p.get("wps_breakdown")) for p in top))   # not in WPS
        self.assertTrue(list((self.tmp / "data/history/competitors" / pid).glob("*.json")))


if __name__ == "__main__":
    unittest.main()
