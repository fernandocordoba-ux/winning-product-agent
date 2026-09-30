"""Step U calibration fixes (from the first live run). SYNTHETIC data, no network.
- growth is CALCULATED from gmv_30d and gmv_prev_30d (provider value kept, mismatch flagged)
- category_product_count only when it is for the product's leaf category
- WPS v1.1: growth_momentum capped when the daily-sales trend is DECLINING
- Discovery order: low-base / spike products after the others
- history: one provider answer stored once, duplicates ignored on read
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

import deep_analysis as DA  # noqa: E402
import discovery as D  # noqa: E402
import history as H  # noqa: E402
from score_products import load_scoring_config, wps_breakdown  # noqa: E402
import test_deep_analysis as TD  # noqa: E402
from winning_product_agent import runner as R  # noqa: E402

CTX = {"key": "1", "facts": {"product_id": "1"}, "calculated": {"filter_status": "PASS"}}


class Growth(unittest.TestCase):
    def norm(self, **kw):
        obj = TD.deep_obj("1", **kw)
        return DA.normalize_deep(obj, CTX)

    def test_growth_calculated_from_previous_revenue(self):
        f, _, w = self.norm(gmv_30d=150000, gmv_prev_30d=100000, growth_30d_pct=50.0)
        self.assertEqual(f["growth_30d_pct"], 50.0)
        self.assertEqual(f["growth_source"], "calculated")
        self.assertFalse(any("MISMATCH" in x for x in w))

    def test_provider_growth_wrong_is_replaced_and_flagged(self):
        # like Discovery on 2026-09-29: provider 29,604 % while revenue went 16,097 -> 42,570
        f, _, w = self.norm(gmv_30d=42570, gmv_prev_30d=16097, growth_30d_pct=29604.35)
        self.assertEqual(f["growth_30d_pct"], round((42570 - 16097) / 16097 * 100, 2))
        self.assertEqual(f["growth_30d_provider"], 29604.35)
        self.assertTrue(any("GROWTH_MISMATCH" in x for x in w))

    def test_zero_previous_revenue_means_na(self):
        f, missing, _ = self.norm(gmv_30d=5000, gmv_prev_30d=0, growth_30d_pct=99999)
        self.assertIsNone(f["growth_30d_pct"])
        self.assertEqual(f["growth_source"], "no_previous_revenue")
        self.assertIn("growth_30d_pct", missing)

    def test_no_previous_revenue_keeps_provider_but_unverified(self):
        f, _, w = self.norm(growth_30d_pct=60.0)
        self.assertEqual(f["growth_30d_pct"], 60.0)
        self.assertEqual(f["growth_source"], "provider_unverified")
        self.assertNotIn("gmv_prev_30d", DA.normalize_deep(TD.deep_obj("1"), CTX)[1])   # aux field not "missing"

    def test_flags_in_analysis(self):
        cfgs = {"deep": DA.load_cfg(), "filters": D.load_yaml("filters.yaml"), "scoring": load_scoring_config()}
        env = {"response": {"data": {"status": "completed", "report": "```json\n" + json.dumps(
            TD.deep_obj("1", gmv_30d=42570, gmv_prev_30d=16097, growth_30d_pct=29604.35)) + "\n```"}},
               "observation_timestamp": "2026-09-30T00:00:00+00:00", "market": "US"}
        r = DA.analyze(env, "x", CTX, cfgs)
        self.assertIn("GROWTH_MISMATCH", [f["flag"] for f in r["red_flags"]])
        self.assertEqual(r["growth"]["growth_source"], "calculated")

    def test_discovery_uses_same_calculation(self):
        rec = {"product_id": "5", "product_name": "x", "gmv_30d": 41130, "gmv_prev_30d": 138.5,
               "growth_30d_pct": 29604.35}
        p, _ = D.normalize(rec, {"category_key": "beauty_personal_care"}, 0)
        self.assertEqual(p["facts"]["growth_30d"], round((41130 - 138.5) / 138.5 * 100, 2))
        self.assertEqual(p["facts"]["growth_source"], "calculated")


class CategoryLevel(unittest.TestCase):
    def test_leaf_level_kept(self):
        f, _, _ = DA.normalize_deep(TD.deep_obj("1", category_product_count=467,
                                                category_product_count_level="Storage"), CTX)
        self.assertEqual(f["category_product_count"], 467)
        self.assertTrue(f["category_count_verified"])

    def test_parent_level_rejected(self):
        # like Blue Boomshampoo: 211,238 = the whole Beauty category, not its leaf
        f, _, w = DA.normalize_deep(TD.deep_obj("1", category_product_count=211238,
                                                category_product_count_level="Home Supplies"), CTX)
        self.assertIsNone(f["category_product_count"])
        self.assertFalse(f["category_count_verified"])
        self.assertTrue(any("not the product's leaf category" in x for x in w))

    def test_unstated_level_rejected(self):
        obj = TD.deep_obj("1")
        obj.pop("category_product_count_level", None)
        f, _, _ = DA.normalize_deep(obj, CTX)
        self.assertIsNone(f["category_product_count"])


class TrendCap(unittest.TestCase):
    def inp(self, trend):
        return {"revenue_growth_pct": 164.39, "recent_trend": trend}

    def test_declining_caps_growth_points(self):
        cfg = load_scoring_config()
        full = wps_breakdown(self.inp("GROWING"), cfg)["metrics"]["growth_momentum"]
        cap = wps_breakdown(self.inp("DECLINING"), cfg)["metrics"]["growth_momentum"]
        self.assertEqual(full["points"], 25.0)
        self.assertEqual(cap["points"], 12.5)
        self.assertEqual(cap["trend_cap_applied"]["uncapped_points"], 25.0)
        self.assertEqual(cfg["version"], "wps-v1.1")

    def test_cap_does_not_raise_low_scores(self):
        cfg = load_scoring_config()
        m = wps_breakdown({"revenue_growth_pct": 20, "recent_trend": "DECLINING"}, cfg)["metrics"]["growth_momentum"]
        self.assertEqual(m["points"], 5.0)
        self.assertNotIn("trend_cap_applied", m)


class DiscoveryOrder(unittest.TestCase):
    def test_low_base_products_sorted_last(self):
        filters, cats = D.load_yaml("filters.yaml"), D.load_yaml("categories.yaml")
        recs = [{"product_id": str(i), "product_name": f"p{i}", "price_min": 40, "price_max": 50, "gmv_30d": 60000,
                 "gmv_prev_30d": prev, "units_30d": 1500, "launch_date": "2025-01-01", "data_window_end": "2026-09-28"}
                for i, prev in ((1, 100), (2, 40000), (3, 30000))]         # 1: +59,900 % (near-zero base)
        tmp = Path(tempfile.mkdtemp())
        try:
            raw = D.save_raw({"success": True, "data": {"status": "completed",
                                                        "report": "```json\n" + json.dumps(recs) + "\n```"}},
                             {"category_key": "home", "task_id": "t", "fetched_at": "20260930T000000Z"}, tmp)
            res = D.run_discovery([raw], filters, cats)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        order = [c["facts"]["product_id"] for c in res["candidates"]]
        self.assertEqual(order, ["3", "2", "1"])                             # 100 %, 50 %, then the spike


class HistoryDedupe(unittest.TestCase):
    def test_same_provider_answer_counts_once(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            store = H.HistoryStore(tmp)
            base = H.empty_observation()
            base.update({"product_id": "9", "identity_key": "9", "source_stage": "discovery",
                         "data_environment": "LIVE", "source": {"task_id": "abc", "record_index": 0}})
            for ts in ("2026-09-30T04:50:00+00:00", "2026-09-30T13:41:00+00:00"):
                store.append({**copy.deepcopy(base), "observation_timestamp": ts, "observation_date": ts[:10]})
            self.assertEqual(len(store.observations("9")), 2)                 # append-only: both kept
            view = R.EnvStore(tmp, "LIVE")
            obs = view.observations("9")
            self.assertEqual(len(obs), 1)
            self.assertTrue(obs[0]["observation_timestamp"].startswith("2026-09-30T04:50"))
        finally:
            for p in tmp.rglob("*"):
                p.chmod(0o755 if p.is_dir() else 0o644)
            shutil.rmtree(tmp, ignore_errors=True)


class CacheInvalidation(unittest.TestCase):
    def test_answer_to_an_old_question_is_not_reused(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            r = R.Runner(profile_path="config/runtime_first_live.yaml", data_root=tmp, out=lambda s="": None)
            now = R.now_utc()
            ok = {"success": True, "data": {"status": "completed", "report": "x"}}
            DA.save_raw_deep(ok, {"observation_timestamp": now.isoformat(), "market": "US", "product_id": "7",
                                  "query_type": "deep_product", "data_environment": "LIVE",
                                  "prompt_version": "old"}, tmp)
            self.assertIsNone(r.find_cached_product("7", "deep_product", 24, now, tmp, "LIVE"))
            DA.save_raw_deep(ok, {"observation_timestamp": now.isoformat(), "market": "US", "product_id": "7",
                                  "query_type": "deep_product", "data_environment": "LIVE",
                                  "prompt_version": r.prompt_version("deep_product")}, tmp)
            self.assertIsNotNone(r.find_cached_product("7", "deep_product", 24, now, tmp, "LIVE"))
        finally:
            for p in tmp.rglob("*"):
                p.chmod(0o755 if p.is_dir() else 0o644)
            shutil.rmtree(tmp, ignore_errors=True)


class Prompts(unittest.TestCase):
    def test_prompts_ask_for_previous_revenue_and_leaf_level(self):
        for f in ("discovery.md", "discovery_combined.md", "discovery_followup.md", "deep_analysis.md",
                  "deep_analysis_batch.md"):
            self.assertIn('"gmv_prev_30d"', (ROOT / "prompts" / f).read_text(), f)
        for f in ("deep_analysis.md", "deep_analysis_batch.md"):
            self.assertIn('"category_product_count_level"', (ROOT / "prompts" / f).read_text(), f)


if __name__ == "__main__":
    unittest.main()
