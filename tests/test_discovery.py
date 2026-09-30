"""Tests for Discovery Mode (scripts/discovery.py). Run: python3 -m unittest discover tests"""
import copy
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import discovery as D  # noqa: E402

FILTERS = D.load_yaml("filters.yaml")
CATEGORIES = D.load_yaml("categories.yaml")


def rec(pid="1001", **kw):
    """A source record that passes every filter (test data, not real products)."""
    r = {"product_id": pid, "product_name": f"Garden Hose Nozzle {pid}", "product_url": f"https://x/{pid}",
         "shop_id": "s1", "shop_name": "Shop One", "category_path": "Home Supplies",
         "category_id": "600001", "price_min": 25.0, "price_max": 35.0,
         "gmv_30d": 60000, "units_30d": 2000, "growth_30d_pct": 45.0,
         "creator_count": 120, "selling_creator_count": 40, "video_count": 300,
         "shop_count": 3, "launch_date": "2026-05-01", "data_window_end": "2026-09-29"}
    r.update(kw)
    return r


def envelope(records, category_key="home", task_id="t1", as_text=None):
    body = as_text if as_text is not None else "Summary\n```json\n" + json.dumps(records) + "\n```"
    return {"category_key": category_key, "task_id": task_id, "market": "US", "currency": "USD",
            "fetched_at": "20260930T120000Z", "observation_date": "2026-09-30",
            "response": {"success": True, "data": {"status": "completed", "task_id": task_id,
                                                   "report": body, "report_url": "https://r/1"}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw_dir = Path(self.tmp.name) / "raw"

    def tearDown(self):
        for p in self.raw_dir.glob("*"):
            os.chmod(p, 0o644)
        self.tmp.cleanup()

    def run_env(self, *envs, filters=None):
        paths = [D.save_raw(e["response"], {k: v for k, v in e.items() if k != "response"}, self.raw_dir)
                 for e in envs]
        return D.run_discovery(paths, filters or FILTERS, CATEGORIES), paths

    def by_key(self, result):
        return {p["key"]: p for p in result["candidates"] + result["failed"]}


class FilterStatus(Base):
    def test_filter_pass(self):
        r, _ = self.run_env(envelope([rec()]))
        p = self.by_key(r)["1001"]
        self.assertEqual(p["calculated"]["filter_status"], "PASS")
        self.assertEqual(p["calculated"]["filter_reasons"], [])

    def test_filter_fail_gmv_below_min(self):
        r, _ = self.run_env(envelope([rec(gmv_30d=24999)]))
        p = self.by_key(r)["1001"]
        self.assertEqual(p["calculated"]["filter_status"], "FAIL")
        self.assertIn("gmv_30d_min", [x["rule"] for x in p["calculated"]["filter_reasons"]])
        self.assertEqual(r["candidates"], [])

    def test_filter_fail_price_out_of_range(self):
        r, _ = self.run_env(envelope([rec(price_min=95, price_max=160)]))   # avg 127.5
        p = self.by_key(r)["1001"]
        self.assertEqual(p["calculated"]["filter_status"], "FAIL")
        self.assertEqual(p["calculated"]["price"], 127.5)

    def test_filter_fail_collapsing_sales(self):
        r, _ = self.run_env(envelope([rec(growth_30d_pct=-30)]))
        self.assertEqual(self.by_key(r)["1001"]["calculated"]["filter_status"], "FAIL")

    def test_review_growth_below_preferred(self):
        r, _ = self.run_env(envelope([rec(growth_30d_pct=10)]))
        self.assertEqual(self.by_key(r)["1001"]["calculated"]["filter_status"], "REVIEW")

    def test_review_keyword_risk(self):
        r, _ = self.run_env(envelope([rec(product_name="Lithium Battery Car Vacuum")]))
        p = self.by_key(r)["1001"]
        self.assertEqual(p["calculated"]["filter_status"], "REVIEW")
        self.assertIn("shipping_problems", [x["rule"] for x in p["calculated"]["filter_reasons"]])

    def test_thresholds_come_from_yaml(self):
        f = copy.deepcopy(FILTERS)
        f["discovery"]["gmv_30d"]["min"] = 50000
        r, _ = self.run_env(envelope([rec(gmv_30d=40000)]), filters=f)
        self.assertEqual(self.by_key(r)["1001"]["calculated"]["filter_status"], "FAIL")

    def test_category_override_takes_precedence(self):
        cats = copy.deepcopy(CATEGORIES)
        home = next(c for c in cats["categories"] if c["key"] == "home")
        home["overrides"] = {"filters": {"discovery": {"gmv_30d": {"min": 100000}}}}
        fc = D.filters_for_category(FILTERS, cats, "home")
        self.assertEqual(fc["discovery"]["gmv_30d"]["min"], 100000)
        self.assertEqual(D.filters_for_category(FILTERS, cats, "pet")["discovery"]["gmv_30d"]["min"], 25000)


class MissingData(Base):
    def test_review_because_optional_data_missing(self):
        r, _ = self.run_env(envelope([rec(growth_30d_pct=None, selling_creator_count="N/A")]))
        p = self.by_key(r)["1001"]
        self.assertEqual(p["calculated"]["filter_status"], "REVIEW")   # not FAIL
        self.assertIsNone(p["facts"]["growth_30d"])
        self.assertIn("growth_30d", p["missing_fields"])

    def test_one_missing_optional_field_is_not_fail(self):
        r, _ = self.run_env(envelope([rec(video_count=None, shop_id=None)]))
        self.assertEqual(self.by_key(r)["1001"]["calculated"]["filter_status"], "PASS")

    def test_missing_critical_field_fails(self):
        r, _ = self.run_env(envelope([rec(units_30d=None)]))
        p = self.by_key(r)["1001"]
        self.assertEqual(p["calculated"]["filter_status"], "FAIL")
        self.assertIn("insufficient_data", [x["rule"] for x in p["calculated"]["filter_reasons"]])

    def test_valid_zero_values_are_not_missing(self):
        r, _ = self.run_env(envelope([rec(growth_30d_pct=0, shop_count=0, video_count=0)]))
        p = self.by_key(r)["1001"]
        self.assertEqual(p["facts"]["growth_30d"], 0)
        self.assertEqual(p["facts"]["shop_count"], 0)
        self.assertNotIn("growth_30d", p["missing_fields"])
        rules = [x["rule"] for x in p["calculated"]["filter_reasons"]]
        self.assertIn("growth_30d_below_preferred", rules)            # judged as 0%, a real value
        self.assertNotIn("missing_optional_data", rules)

    def test_zero_gmv_is_a_real_value_that_fails_threshold(self):
        r, _ = self.run_env(envelope([rec(gmv_30d=0)]))
        p = self.by_key(r)["1001"]
        self.assertEqual(p["facts"]["gmv_30d"], 0)
        reasons = p["calculated"]["filter_reasons"]
        self.assertIn("gmv_30d_min", [x["rule"] for x in reasons])
        self.assertNotIn("insufficient_data", [x["rule"] for x in reasons])


class LowBaseAndSellingCreators(Base):
    def test_missing_selling_creators_alone_is_pass(self):
        r, _ = self.run_env(envelope([rec(selling_creator_count=None)]))
        p = self.by_key(r)["1001"]
        self.assertEqual(p["calculated"]["filter_status"], "PASS")
        self.assertIn("selling_creator_count", p["missing_fields"])     # still shown as N/A

    def test_selling_creators_below_preferred_still_reviews_when_returned(self):
        r, _ = self.run_env(envelope([rec(selling_creator_count=2)]))
        self.assertEqual(self.by_key(r)["1001"]["calculated"]["filter_status"], "REVIEW")

    def test_new_launch_under_30_days_is_review(self):
        r, _ = self.run_env(envelope([rec(launch_date="2026-09-05", data_window_end="2026-09-30")]))
        c = self.by_key(r)["1001"]["calculated"]
        self.assertEqual(c["product_age_days"], 25)
        self.assertEqual(c["filter_status"], "REVIEW")
        self.assertIn("low_base_or_seasonal", [x["rule"] for x in c["filter_reasons"]])

    def test_exactly_30_days_not_flagged(self):
        r, _ = self.run_env(envelope([rec(launch_date="2026-08-31", data_window_end="2026-09-30")]))
        self.assertEqual(self.by_key(r)["1001"]["calculated"]["filter_status"], "PASS")

    def test_growth_over_1000_is_review(self):
        r, _ = self.run_env(envelope([rec(growth_30d_pct=1000.01)]))
        self.assertEqual(self.by_key(r)["1001"]["calculated"]["filter_status"], "REVIEW")
        r, _ = self.run_env(envelope([rec("1002", growth_30d_pct=1000)]))
        self.assertEqual(self.by_key(r)["1002"]["calculated"]["filter_status"], "PASS")

    def test_missing_launch_date_is_na_not_flag(self):
        r, _ = self.run_env(envelope([rec(launch_date=None)]))
        c = self.by_key(r)["1001"]["calculated"]
        self.assertIsNone(c["product_age_days"])
        self.assertEqual(c["filter_status"], "PASS")
        self.assertIn("low_base_or_seasonal:product_age_days", c["na_checks"])

    def test_age_uses_observation_date_when_window_end_missing(self):
        r, _ = self.run_env(envelope([rec(launch_date="2026-09-20", data_window_end=None)]))
        self.assertEqual(self.by_key(r)["1001"]["calculated"]["product_age_days"], 10)   # obs 2026-09-30


class Dedupe(Base):
    def test_duplicate_products_across_categories(self):
        a = envelope([rec("2002", video_count=None)], category_key="home", task_id="a")
        b = envelope([rec("2002")], category_key="kitchen", task_id="b")   # more complete
        r, _ = self.run_env(a, b)
        self.assertEqual(r["summary"]["unique"], 1)
        self.assertEqual(r["summary"]["duplicates_removed"], 1)
        kept = self.by_key(r)["2002"]
        self.assertEqual(kept["facts"]["video_count"], 300)               # most complete kept
        self.assertEqual(len(kept["duplicates"]), 1)

    def test_missing_product_id_uses_fallback_key(self):
        r, _ = self.run_env(envelope([rec(product_id=None, product_name="  Silicone   Brush "),
                                      rec(product_id="N/A", product_name="silicone brush")]))
        self.assertEqual(r["summary"]["unique"], 1)                        # same fallback key
        p = r["candidates"][0]
        expected = "fallback:" + hashlib.sha1("silicone brush|shop one|25.0".encode()).hexdigest()[:16]
        self.assertEqual(p["key"], expected)
        self.assertEqual(p["key_type"], "fallback")
        self.assertEqual(p["calculated"]["filter_status"], "REVIEW")
        self.assertIn("missing_product_id", [x["rule"] for x in p["calculated"]["filter_reasons"]])


class Limits(Base):
    def test_maximum_100_candidates_momentum_first(self):
        records = [rec(str(3000 + i), growth_30d_pct=30 + i, gmv_30d=10**6 - i * 1000) for i in range(130)]
        r, _ = self.run_env(envelope(records))
        self.assertEqual(len(r["candidates"]), 100)
        self.assertEqual(r["summary"]["over_limit"], 30)
        growth = [p["facts"]["growth_30d"] for p in r["candidates"]]
        self.assertEqual(growth, sorted(growth, reverse=True))             # by growth, not GMV
        self.assertEqual(r["candidates"][0]["key"], "3129")                # highest growth, lowest GMV

    def test_pass_before_review(self):
        r, _ = self.run_env(envelope([rec("1", growth_30d_pct=10), rec("2", growth_30d_pct=40)]))
        self.assertEqual([p["calculated"]["filter_status"] for p in r["candidates"]], ["PASS", "REVIEW"])


class RawPreservation(Base):
    def test_raw_file_unchanged_and_read_only(self):
        env = envelope([rec(), rec("9", gmv_30d="$1,200")])
        r, paths = self.run_env(env)
        raw = paths[0]
        before = hashlib.sha256(raw.read_bytes()).hexdigest()
        D.run_discovery([raw], FILTERS, CATEGORIES)
        self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), before)
        self.assertFalse(os.stat(raw).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        self.assertEqual(json.loads(raw.read_text())["response"], env["response"])

    def test_save_raw_never_overwrites(self):
        e = envelope([rec()])
        meta = {k: v for k, v in e.items() if k != "response"}
        p1 = D.save_raw(e["response"], meta, self.raw_dir)
        p2 = D.save_raw({"different": True}, meta, self.raw_dir)
        self.assertNotEqual(p1, p2)
        self.assertEqual(json.loads(p1.read_text())["response"], e["response"])

    def test_original_record_kept_with_product(self):
        r, _ = self.run_env(envelope([rec(gmv_30d="$60,000")]))
        p = self.by_key(r)["1001"]
        self.assertEqual(p["source"]["original_record"]["gmv_30d"], "$60,000")
        self.assertEqual(p["facts"]["gmv_30d"], 60000)
        self.assertEqual(p["source"]["task_id"], "t1")
        self.assertEqual(p["observation_date"], "2026-09-30")


class Malformed(Base):
    def test_no_json_block(self):
        r, _ = self.run_env(envelope([], as_text="| markdown | only |"))
        self.assertEqual(r["summary"]["unique"], 0)
        self.assertTrue(r["malformed"])

    def test_invalid_json_block(self):
        r, _ = self.run_env(envelope([], as_text="```json\n[{bad json}\n```"))
        self.assertTrue(r["malformed"])

    def test_bad_records_skipped_good_kept(self):
        r, _ = self.run_env(envelope([rec(), "not an object", 42, {"price_min": 10}]))
        self.assertEqual(r["summary"]["unique"], 1)
        self.assertEqual(r["summary"]["malformed"], 3)

    def test_unparseable_values_become_na_not_guessed(self):
        r, _ = self.run_env(envelope([rec(units_30d="lots", growth_30d_pct="high", video_count=-4,
                                          price_max="about 40")]))
        p = self.by_key(r)["1001"]
        for f in ("units_30d", "growth_30d", "video_count", "price_max"):
            self.assertIsNone(p["facts"][f], f)
        self.assertTrue(p["parse_warnings"])
        self.assertEqual(p["calculated"]["filter_status"], "FAIL")          # units is critical

    def test_parse_number(self):
        self.assertEqual(D.parse_number("4,206,171"), 4206171)
        self.assertEqual(D.parse_number("$40.30"), 40.3)
        self.assertEqual(D.parse_number("−24.6%"), -24.6)
        self.assertEqual(D.parse_number("29.0k"), 29000.0)
        self.assertEqual(D.parse_number(0), 0)
        for bad in (None, "N/A", "", True, "12 units", float("nan")):
            self.assertIsNone(D.parse_number(bad), repr(bad))


class Scores(Base):
    def test_wps_pending_and_confidence_present(self):
        r, _ = self.run_env(envelope([rec()]))
        c = self.by_key(r)["1001"]["calculated"]
        self.assertEqual(c["wps_if_calculable"]["status"], "pending")
        self.assertEqual(c["confidence_if_calculable"]["stage"], "discovery")
        self.assertIsInstance(c["confidence_if_calculable"]["score"], float)

    def test_deterministic(self):
        env = envelope([rec(str(i), growth_30d_pct=i) for i in range(40)])
        r1, p1 = self.run_env(env)
        r2 = D.run_discovery(p1, FILTERS, CATEGORIES)
        self.assertEqual(json.dumps(r1, sort_keys=True), json.dumps(r2, sort_keys=True))


class Queries(unittest.TestCase):
    def test_one_query_per_enabled_category_with_yaml_thresholds(self):
        qs = D.build_queries(FILTERS, CATEGORIES)
        enabled = [c["key"] for c in CATEGORIES["categories"] if c.get("enabled", True)]
        self.assertEqual([q["category_key"] for q in qs], enabled)
        self.assertIn("25000", qs[0]["query"])
        self.assertIn("Do NOT simply rank by total GMV", qs[0]["query"])


if __name__ == "__main__":
    unittest.main()
