"""Tests for Deep Analysis (Step M). SYNTHETIC DATA ONLY, in temp folders
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

import deep_analysis as DA  # noqa: E402
import discovery as disc  # noqa: E402
from score_products import load_scoring_config  # noqa: E402

CFGS = {"deep": DA.load_cfg(), "filters": disc.load_yaml("filters.yaml"), "scoring": load_scoring_config()}
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def cand(pid, status="PASS", url=True):
    return {"key": pid, "key_type": "product_id",
            "facts": {"product_id": pid, "product_name": f"Synthetic product {pid}",
                      "product_url": f"https://example.test/p/{pid}" if url else None},
            "calculated": {"filter_status": status}}


def discovery_result(cands, failed=()):
    return {"market": {"region": "US", "currency": "USD", "period_days": 30},
            "candidates": list(cands), "failed": list(failed)}


def deep_obj(pid="1", **kw):
    """Synthetic complete deep response object."""
    o = {"product_id": pid, "product_name": f"Synthetic product {pid}", "product_url": f"https://example.test/p/{pid}",
         "category_path": "Home Supplies > Storage", "category_id": "1", "shop_id": "9", "shop_name": "Shop Nine",
         "price_min": 30.0, "price_max": 40.0,
         "price_history": [{"date": "2026-09-01", "price": 35.0}, {"date": "2026-09-15", "price": 34.0},
                           {"date": "2026-09-29", "price": 35.0}],
         "gmv_30d": 100000, "units_30d": 3000, "growth_30d_pct": 60.0, "category_growth_pct": 10.0,
         "launch_date": "2026-03-01", "data_window_end": "2026-09-29", "commission_pct": 12.0,
         "daily_gmv": [3000.0] * 30, "daily_units": [100] * 30,
         "creator_count": 400, "selling_creator_count": 120, "creator_growth_pct": 20.0, "daily_creator_count": None,
         "top_creators": [{"creator_id": str(i), "name": f"c{i}", "revenue": r, "growth_pct": g}
                          for i, (r, g) in enumerate([(20000, 10), (15000, 5), (10000, -3), (5000, 8)])],
         "video_count": 900, "selling_video_count": 300, "video_growth_pct": 15.0, "video_sales_share_pct": 80.0,
         "daily_video_count": None,
         "top_videos": [{"video_id": str(i), "creator": "c", "revenue": r, "views": 1000}
                        for i, r in enumerate([12000, 9000, 7000])],
         "shop_count": 5, "similar_listings_count": 20, "category_product_count": 8000,
         "category_product_count_level": "Storage"}
    o.update(kw)
    return o


def envelope(obj, pid="1", qt="deep_product", ts=None):
    return {"source": "kalopilot", "observation_timestamp": (ts or NOW).isoformat(), "market": "US",
            "product_id": pid, "query_type": qt, "task_id": "t",
            "response": {"success": True, "data": {"status": "completed", "task_id": "t",
                                                   "report": "```json\n" + json.dumps(obj) + "\n```"}}}


class FakeClient:
    """Synthetic KaloPilot: counts paid submits; balance drops per query."""
    def __init__(self, balance=100.0, cost=4.0, obj_for=None):
        self.balance, self.cost, self.submits, self.obj_for = balance, cost, [], obj_for or (lambda q: deep_obj())

    def credits(self):
        return {"totalRemain": self.balance}

    def submit(self, q):
        self.submits.append(q)
        self.balance -= self.cost
        return {"success": True, "data": {"task_id": f"task{len(self.submits)}", "status": "submitted"}}

    def wait(self, task_id):
        q = self.submits[-1]
        pid = q.split("product ID ")[-1].split(")")[0]
        obj = self.obj_for(pid)
        return {"success": True, "data": {"status": "completed", "task_id": task_id, "credits_consumed": self.cost,
                                          "report": "```json\n" + json.dumps(obj) + "\n```"}}


class TmpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.processed, self.raw, self.out = t / "processed", t / "raw", t / "out"
        self.processed.mkdir()

    def tearDown(self):
        for p in self.raw.glob("*"):
            os.chmod(p, 0o644)
        self.tmp.cleanup()

    def write_discovery(self, dres):
        path = self.processed / "discovery_20260930T000000Z.json"
        path.write_text(json.dumps(dres))
        return path

    def run_da(self, dres, **kw):
        return DA.run(discovery_path=self.write_discovery(dres), now=NOW, raw_dir=self.raw, out_dir=self.out,
                      processed_dir=self.processed, cfgs=CFGS, **kw)

    def analyze(self, obj, pid="1"):
        env = envelope(obj, pid)
        return DA.analyze(env, "synthetic.json", cand(pid), CFGS)


# ------------------------------------------------------------------ selection
class Selection(unittest.TestCase):
    def test_pass_and_review_selected_fail_excluded(self):
        dres = discovery_result([cand("1", "PASS"), cand("2", "REVIEW")], failed=[cand("3", "FAIL")])
        dres["candidates"].append(cand("4", "FAIL"))          # even if misplaced
        sel, _ = DA.select_candidates(dres, CFGS["deep"])
        self.assertEqual([p["key"] for p in sel], ["1", "2"])

    def test_pass_prioritized_over_review(self):
        dres = discovery_result([cand("r1", "REVIEW"), cand("p1", "PASS"), cand("r2", "REVIEW"), cand("p2", "PASS")])
        sel, _ = DA.select_candidates(dres, CFGS["deep"])
        self.assertEqual([p["key"] for p in sel], ["p1", "p2", "r1", "r2"])   # discovery order kept inside

    def test_max_40(self):
        dres = discovery_result([cand(str(i), "PASS" if i % 2 else "REVIEW") for i in range(60)])
        sel, skipped = DA.select_candidates(dres, CFGS["deep"])
        self.assertEqual(len(sel), 40)
        self.assertTrue(all(p["calculated"]["filter_status"] == "PASS" for p in sel[:30]))
        self.assertEqual(sum("over deep_analysis_max_products" in s["reason"] for s in skipped), 20)

    def test_max_is_configurable(self):
        cfg = copy.deepcopy(CFGS["deep"])
        cfg["selection"]["deep_analysis_max_products"] = 5
        sel, _ = DA.select_candidates(discovery_result([cand(str(i)) for i in range(9)]), cfg)
        self.assertEqual(len(sel), 5)


# ------------------------------------------------------------------ data
class DataIntegrity(TmpBase):
    def test_missing_optional_data_is_na_not_invented(self):
        r = self.analyze(deep_obj(creator_count=None, selling_creator_count=None, video_count=None, shop_count=None,
                                  creator_growth_pct=None))
        self.assertEqual(r["status"], "ok")
        for k in ("creator_count", "selling_creator_count", "video_count", "shop_count"):
            self.assertIn(k, r["missing_data"])
        self.assertIsNone(r["creator_metrics"]["total"])
        self.assertEqual(r["wps_breakdown"]["video_momentum"]["points"], "N/A")
        self.assertLess(r["confidence"], 100)

    def test_valid_zero_values(self):
        r = self.analyze(deep_obj(growth_30d_pct=0, shop_count=0, similar_listings_count=0, category_growth_pct=0))
        self.assertEqual(r["growth"]["growth_30d_pct"], 0)
        self.assertEqual(r["competition_metrics"]["shop_count"], 0)
        self.assertNotIn("growth_30d_pct", r["missing_data"])
        self.assertEqual(r["wps_breakdown"]["growth_long_term"]["points"], 0.0)  # scored as real 0, not N/A


# ------------------------------------------------------------------ concentration
class Concentration(TmpBase):
    def test_creator_dependency(self):
        r = self.analyze(deep_obj(gmv_30d=100000, top_creators=[{"name": "v", "revenue": 75000, "growth_pct": 1},
                                                                {"name": "a", "revenue": 1000, "growth_pct": 1},
                                                                {"name": "b", "revenue": 1000, "growth_pct": 1}]))
        self.assertEqual(r["concentration_metrics"]["metrics"]["top_creator_revenue_share"]["value"], 75.0)
        self.assertIn("CREATOR_DEPENDENCY", [f["flag"] for f in r["red_flags"]])

    def test_video_dependency(self):
        r = self.analyze(deep_obj(gmv_30d=100000, top_videos=[{"video_id": "1", "revenue": 71000},
                                                              {"video_id": "2", "revenue": 100},
                                                              {"video_id": "3", "revenue": 100}]))
        self.assertIn("VIDEO_DEPENDENCY", [f["flag"] for f in r["red_flags"]])

    def test_insufficient_concentration_data_is_na_no_flag(self):
        r = self.analyze(deep_obj(top_creators=None, top_videos=[{"video_id": "1", "revenue": None}]))
        c = r["concentration_metrics"]
        self.assertEqual(c["metrics"]["top_creator_revenue_share"]["value"], "N/A")
        self.assertEqual(c["flags"]["CREATOR_DEPENDENCY"]["status"], "N/A")
        self.assertEqual(c["flags"]["VIDEO_DEPENDENCY"]["status"], "N/A")
        flags = [f["flag"] for f in r["red_flags"]]
        self.assertNotIn("CREATOR_DEPENDENCY", flags)
        self.assertNotIn("VIDEO_DEPENDENCY", flags)


# ------------------------------------------------------------------ trend
class Trend(TmpBase):
    def test_accelerating(self):
        series = [100.0] * 9 + [100.0] * 7 + [110.0] * 7 + [150.0] * 7     # prior +10%, last +36.36%
        r = self.analyze(deep_obj(daily_gmv=series))
        t = r["trend_metrics"]
        self.assertEqual(t["gmv"]["velocity_pct"], 36.36)
        self.assertEqual(t["gmv"]["prior_velocity_pct"], 10.0)
        self.assertEqual(t["label"], "ACCELERATING")

    def test_growing_not_accelerating(self):
        series = [100.0] * 16 + [120.0] * 7 + [140.0] * 7                   # +20%, then +16.67%
        self.assertEqual(self.analyze(deep_obj(daily_gmv=series))["trend_metrics"]["label"], "GROWING")

    def test_stable(self):
        self.assertEqual(self.analyze(deep_obj(daily_gmv=[100.0] * 30))["trend_metrics"]["label"], "STABLE")

    def test_declining(self):
        series = [200.0] * 23 + [150.0] * 7                                  # -25%
        r = self.analyze(deep_obj(daily_gmv=series))
        self.assertEqual(r["trend_metrics"]["label"], "DECLINING")
        self.assertIn("SALES_DECLINING", [f["flag"] for f in r["red_flags"]])

    def test_insufficient_history(self):
        r = self.analyze(deep_obj(daily_gmv=[100.0] * 10 + [None] * 20))
        self.assertEqual(r["trend_metrics"]["label"], "INSUFFICIENT_DATA")
        self.assertIn("INSUFFICIENT_HISTORY", [f["flag"] for f in r["red_flags"]])

    def test_no_series_at_all(self):
        r = self.analyze(deep_obj(daily_gmv=None))
        self.assertEqual(r["trend_metrics"]["label"], "INSUFFICIENT_DATA")
        self.assertEqual(r["wps_breakdown"]["trend_stability"]["points"], "N/A")

    def test_zero_prior_window_is_na(self):
        series = [0.0] * 23 + [50.0] * 7
        self.assertIsNone(DA.velocity(series, CFGS["deep"]["trend"])["velocity_pct"])


# ------------------------------------------------------------------ WPS / Confidence
class Scoring(TmpBase):
    def test_wps_integration_complete(self):
        r = self.analyze(deep_obj())
        self.assertTrue(r["wps_complete"])
        self.assertEqual(set(r["wps_groups"]), {"growth_momentum", "demand", "video_momentum", "creator_momentum",
                                                "competition", "margin_potential", "trend_stability"})
        avail = [m for m in r["wps_breakdown"].values() if m["points"] != "N/A"]
        earned = sum(m["points"] for m in avail)
        possible = sum(m["max"] for m in r["wps_breakdown"].values() if m["points"] != "N/A" or m["tier"] == "CORE")
        self.assertAlmostEqual(r["wps"], round(100 * earned / possible, 2), places=1)
        # wps-v2 growth: long-term 60 % -> 0.6*10 = 6.0; flat daily series: velocity 0 -> (0+20)/50*10 = 4.0;
        # acceleration 0 -> (0+30)/60*5 = 2.5; group = 12.5 of 25 (deterministic, from scoring.yaml)
        self.assertEqual(r["wps_breakdown"]["growth_long_term"]["points"], 6.0)
        self.assertEqual(r["wps_breakdown"]["growth_recent_trend"]["points"], 4.0)
        self.assertEqual(r["wps_breakdown"]["growth_acceleration"]["points"], 2.5)
        self.assertEqual(r["wps_groups"]["growth_momentum"]["points"], 12.5)
        self.assertEqual(r["wps_breakdown"]["trend_stability"]["points"], 5.0)    # flat series, CV 0

    def test_wps_deterministic(self):
        a, b = self.analyze(deep_obj()), self.analyze(deep_obj())
        self.assertEqual(a["wps"], b["wps"])
        self.assertEqual(a["wps_breakdown"], b["wps_breakdown"])

    def test_confidence_integration_and_independence(self):
        full = self.analyze(deep_obj())
        thin = self.analyze(deep_obj(daily_gmv=None, top_creators=None, top_videos=None, shop_count=None,
                                     similar_listings_count=None, category_product_count=None))
        self.assertEqual(set(full["confidence_breakdown"]),
                         {"sales_history", "gmv_data", "units_sold", "growth_data", "price_data", "creator_data",
                          "video_data", "competition_shop_data", "trend_history_data"})
        self.assertGreater(full["confidence"], thin["confidence"])
        self.assertIn("LOW_DATA_CONFIDENCE", [f["flag"] for f in thin["red_flags"]])
        # a weaker product with the same data completeness keeps the same confidence
        weak = self.analyze(deep_obj(growth_30d_pct=-5.0, gmv_30d=30000))
        self.assertEqual(weak["confidence"], full["confidence"])
        self.assertLess(weak["wps"], full["wps"])

    def test_ip_review_required_not_a_legal_claim(self):
        r = self.analyze(deep_obj(product_name="Stanley style tumbler dupe"))
        f = next(x for x in r["red_flags"] if x["flag"] == "IP_REVIEW_REQUIRED")
        self.assertIn("not a legal", f["note"])

    def test_price_instability(self):
        hist = [{"date": "2026-09-01", "price": 20.0}, {"date": "2026-09-10", "price": 30.0},
                {"date": "2026-09-20", "price": 25.0}]
        self.assertIn("PRICE_INSTABILITY", [f["flag"] for f in self.analyze(deep_obj(price_history=hist))["red_flags"]])
        self.assertNotIn("PRICE_INSTABILITY", [f["flag"] for f in self.analyze(deep_obj(price_history=None))["red_flags"]])


# ------------------------------------------------------------------ runs & credits
class Runs(TmpBase):
    def test_dry_run_spends_nothing(self):
        client = FakeClient(balance=50)
        r = self.run_da(discovery_result([cand("1"), cand("2", "REVIEW")]), live=False, client=client)
        self.assertEqual(r["mode"], "dry_run")
        self.assertEqual(client.submits, [])
        self.assertEqual(r["plan"]["paid_queries"], 2)
        self.assertEqual(r["plan"]["estimated_credits"], 8.0)
        self.assertFalse(self.raw.exists() and any(self.raw.iterdir()))
        self.assertFalse(self.out.exists())

    def test_live_run_saves_raw_and_results(self):
        client = FakeClient(balance=100, obj_for=lambda pid: deep_obj(pid))
        r = self.run_da(discovery_result([cand("1"), cand("2")]), live=True, client=client)
        self.assertEqual(len(client.submits), 2)
        self.assertEqual([x["product_id"] for x in r["results"]], ["1", "2"])
        raws = sorted(self.raw.glob("*.json"))
        self.assertEqual(len(raws), 2)
        env = json.loads(raws[0].read_text())
        for k in ("observation_timestamp", "market", "product_id", "query_type", "source"):
            self.assertIn(k, env)
        self.assertTrue(Path(r["saved"]).exists())

    def test_raw_data_preserved_and_never_overwritten(self):
        meta = {"observation_timestamp": NOW.isoformat(), "market": "US", "product_id": "1", "query_type": "deep_product"}
        p1 = DA.save_raw_deep({"a": 1}, meta, self.raw)
        h1 = hashlib.sha256(p1.read_bytes()).hexdigest()
        p2 = DA.save_raw_deep({"b": 2}, meta, self.raw)                    # same timestamp
        self.assertNotEqual(p1, p2)
        self.assertEqual(hashlib.sha256(p1.read_bytes()).hexdigest(), h1)
        self.assertFalse(os.stat(p1).st_mode & 0o222)                       # read-only
        with self.assertRaises(ValueError):
            DA.save_raw_deep({}, {**meta, "product_id": None}, self.raw)    # required metadata

    def test_duplicate_query_prevention(self):
        client = FakeClient(balance=100)
        dres = discovery_result([cand("1"), cand("1"), cand("2")])          # same product twice
        r = self.run_da(dres, live=True, client=client)
        self.assertEqual(len(client.submits), 2)
        self.assertEqual(sum(i["action"] == "skip_duplicate" for i in r["plan"]["items"]), 1)

    def test_cache_prevents_paid_requery(self):
        self.raw.mkdir(parents=True)
        DA.save_raw_deep(envelope(deep_obj("1"))["response"],
                         {"observation_timestamp": (NOW - timedelta(hours=2)).isoformat(), "market": "US",
                          "product_id": "1", "query_type": "deep_product"}, self.raw)
        client = FakeClient(balance=100)
        r = self.run_da(discovery_result([cand("1"), cand("2")]), live=True, client=client)
        self.assertEqual(len(client.submits), 1)                            # only product 2 paid
        self.assertTrue(r["results"][0]["source"]["cache_hit"])

    def test_stale_cache_is_not_used(self):
        self.raw.mkdir(parents=True)
        DA.save_raw_deep(envelope(deep_obj("1"))["response"],
                         {"observation_timestamp": (NOW - timedelta(hours=48)).isoformat(), "market": "US",
                          "product_id": "1", "query_type": "deep_product"}, self.raw)
        r = self.run_da(discovery_result([cand("1")]), live=False, client=FakeClient())
        self.assertEqual(r["plan"]["paid_queries"], 1)

    def test_insufficient_credits_stops_safely(self):
        client = FakeClient(balance=14.0, cost=4.0)                         # reserve 5 + est 4 = 9 needed
        r = self.run_da(discovery_result([cand(str(i)) for i in range(5)]), live=True, client=client)
        self.assertEqual(len(client.submits), 2)                            # 14 -> 10 -> 6 (stop)
        self.assertEqual(r["stopped"]["reason"], "insufficient_credits")
        self.assertEqual(len(r["results"]), 2)                              # work done is kept
        self.assertTrue(Path(r["saved"]).exists())

    def test_dry_run_reports_affordability(self):
        r = self.run_da(discovery_result([cand(str(i)) for i in range(10)]), live=False,
                        client=FakeClient(balance=30.0))
        self.assertEqual(r["plan"]["estimated_credits"], 40.0)
        self.assertEqual(r["affordable_paid_queries"], 6)                   # (30 - 5) // 4
        self.assertFalse(r["sufficient_for_full_run"])

    def test_malformed_response_recorded(self):
        env = envelope(deep_obj())
        env["response"]["data"]["report"] = "no json here"
        r = DA.analyze(env, "x.json", cand("1"), CFGS)
        self.assertEqual(r["status"], "malformed")


if __name__ == "__main__":
    unittest.main()
