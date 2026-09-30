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


CFGS = {"deep": DA.load_cfg(), "filters": D.load_yaml("filters.yaml"), "scoring": load_scoring_config()}


def analyze(obj):
    env = {"response": {"data": {"status": "completed", "report": "```json\n" + json.dumps(obj) + "\n```"}},
           "observation_timestamp": "2026-09-30T13:46:00+00:00", "market": "US"}
    return DA.analyze(env, "raw.json", CTX, CFGS)


def flags(r):
    return [f["flag"] for f in r["red_flags"]]


class CompetitionScope(unittest.TestCase):
    """1. parent-category competition not comparable / subcategory comparable / false saturation prevented"""
    def test_parent_category_count_not_comparable(self):
        # Blue Boomshampoo case: 211,238 = whole Beauty category; path leaf is "Bath & Shower"
        r = analyze(TD.deep_obj("1", similar_listings_count=None, category_path="Beauty & Personal Care > Bath & Body Care > Bath & Shower",
                                category_product_count=211238,
                                category_product_count_level="Beauty & Personal Care"))
        c = r["competition_metrics"]
        self.assertEqual((c["competition_scope"], c["competition_comparable"]), ("CATEGORY", False))
        self.assertEqual(c["competition_count"], 211238)                 # kept, just not comparable
        self.assertEqual(r["wps_breakdown"]["competition_saturation"]["points"], "N/A")

    def test_false_extreme_saturation_prevented(self):
        r = analyze(TD.deep_obj("1", similar_listings_count=None, category_product_count=211238, category_product_count_level="Home Supplies"))
        self.assertNotIn("EXTREME_SATURATION", flags(r))
        self.assertIn("COMPETITION_NOT_COMPARABLE", flags(r))

    def test_unstated_level_is_unknown(self):
        obj = TD.deep_obj("1", similar_listings_count=None, category_product_count=211238)
        obj.pop("category_product_count_level", None)
        c = analyze(obj)["competition_metrics"]
        self.assertEqual((c["competition_scope"], c["competition_comparable"]), ("UNKNOWN", False))

    def test_subcategory_count_comparable_and_scored(self):
        r = analyze(TD.deep_obj("1", similar_listings_count=None, category_product_count=467, category_product_count_level="Storage",
                                leaf_category_id="700123"))
        c = r["competition_metrics"]
        self.assertEqual((c["competition_scope"], c["competition_comparable"]), ("SUBCATEGORY", True))
        self.assertEqual(c["competition_subcategory_id"], "700123")
        self.assertNotEqual(r["wps_breakdown"]["competition_saturation"]["points"], "N/A")

    def test_subcategory_saturation_still_flags_when_comparable(self):
        r = analyze(TD.deep_obj("1", similar_listings_count=None, category_product_count=80000, category_product_count_level="Storage"))
        self.assertIn("EXTREME_SATURATION", flags(r))

    def test_product_cluster_preferred(self):
        c = analyze(TD.deep_obj("1", similar_listings_count=12))["competition_metrics"]
        self.assertEqual((c["competition_scope"], c["competition_count"]), ("PRODUCT_CLUSTER", 12))

    def test_not_comparable_lowers_confidence_not_wps(self):
        good = analyze(TD.deep_obj("1", similar_listings_count=None, category_product_count=500,
                                   category_product_count_level="Storage"))
        bad = analyze(TD.deep_obj("1", similar_listings_count=None, category_product_count=500,
                                  category_product_count_level="Home Supplies"))
        self.assertEqual(bad["wps_breakdown"]["competition_saturation"]["points"], "N/A")
        # excluded from the denominator: WPS not dragged down by the missing SUPPORTING metric
        self.assertEqual(bad["wps_points"]["possible"], good["wps_points"]["possible"] - 7.5)
        self.assertLess(bad["confidence"], good["confidence"])


def daily(last_week, prev_week, prev2_week):
    return [prev2_week] * 9 + [prev_week] * 7 + [last_week] * 7 + [last_week] * 7    # 30 points, oldest first


class GrowthMomentum(unittest.TestCase):
    """2. rebuilt Growth Momentum = long-term 10 + recent trend 10 + acceleration 5"""
    def g(self, r):
        return {k: r["wps_breakdown"][k]["points"] for k in ("growth_long_term", "growth_recent_trend",
                                                             "growth_acceleration")}

    def test_positive_30d_growth_with_declining_recent_sales(self):
        # +164 % over 30 days but the last 7 days are 40 % below the 7 before (Blue Boomshampoo pattern)
        r = analyze(TD.deep_obj("1", growth_30d_pct=164.39, daily_gmv=[3000] * 9 + [2000] * 7 + [2000] * 7 + [1200] * 7))
        g = self.g(r)
        self.assertEqual(g["growth_long_term"], 10.0)
        self.assertLess(g["growth_recent_trend"], 10.0)                 # declining: never full
        self.assertEqual(g["growth_recent_trend"], 0.0)                 # -40 % <= -20 % -> 0
        self.assertLess(r["wps_groups"]["growth_momentum"]["points"], 25.0)

    def test_positive_30d_growth_with_accelerating_recent_sales(self):
        r = analyze(TD.deep_obj("1", growth_30d_pct=150.0, daily_gmv=[1000] * 9 + [1000] * 7 + [1100] * 7 + [1650] * 7))
        g = self.g(r)
        # last week vs previous: (1650-1100)/1100 = +50 % -> full 10; prior velocity +10 % -> accel +40 pp -> full 5
        self.assertEqual(g, {"growth_long_term": 10.0, "growth_recent_trend": 10.0, "growth_acceleration": 5.0})

    def test_positive_30d_growth_alone_never_25(self):
        cfg = load_scoring_config()
        b = wps_breakdown({"revenue_growth_pct": 10000}, cfg)
        self.assertEqual(b["groups"]["growth_momentum"]["points"], 10.0)

    def test_decelerating_not_full_acceleration(self):
        r = analyze(TD.deep_obj("1", daily_gmv=[1000] * 9 + [1000] * 7 + [1500] * 7 + [1650] * 7))
        self.assertLess(self.g(r)["growth_acceleration"], 5.0)         # +10 % after +50 % -> decelerating

    def test_missing_recent_trend_not_fabricated(self):
        r = analyze(TD.deep_obj("1", daily_gmv=None))
        g = self.g(r)
        self.assertEqual((g["growth_recent_trend"], g["growth_acceleration"]), ("N/A", "N/A"))
        self.assertIn("growth_recent_trend", r["wps_missing_by_tier"]["SUPPORTING"])
        self.assertTrue(r["wps_complete"])                              # SUPPORTING missing: still complete
        self.assertTrue(any("growth_recent_trend" in a["reason"] for a in r["confidence_adjustments"]))

    def test_missing_acceleration_only(self):
        r = analyze(TD.deep_obj("1", daily_gmv=[None] * 14 + [1000] * 16))   # 16 days: velocity yes, prior no
        g = self.g(r)
        self.assertNotEqual(g["growth_recent_trend"], "N/A")
        self.assertEqual(g["growth_acceleration"], "N/A")

    def test_thresholds_in_yaml(self):
        m = load_scoring_config()["metrics"]
        self.assertEqual(m["growth_recent_trend"]["components"]["recent_velocity"]["full_at"], 30)
        self.assertGreater(m["growth_recent_trend"]["components"]["recent_velocity"]["full_at"], 0)
        self.assertEqual(sum(m[k]["points"] for k in ("growth_long_term", "growth_recent_trend",
                                                      "growth_acceleration")), 25)


class DataRequirements(unittest.TestCase):
    """6. CORE / SUPPORTING / ENHANCEMENT"""
    def test_missing_core_metric(self):
        r = analyze(TD.deep_obj("1", units_30d=None))
        self.assertFalse(r["wps_complete"])
        self.assertIn("demand", r["wps_missing_by_tier"]["CORE"])
        self.assertEqual(r["wps_points"]["possible"], 100 - sum(
            r["wps_breakdown"][k]["max"] for k in r["wps_missing_by_tier"]["SUPPORTING"]))   # CORE stays in

    def test_unavailable_enhancement_metric_only_lowers_confidence(self):
        full = analyze(TD.deep_obj("1"))
        no_enh = analyze(TD.deep_obj("1", price_history=None, daily_video_count=None))
        self.assertEqual(full["wps"], no_enh["wps"])
        self.assertTrue(no_enh["wps_complete"])
        self.assertTrue(any("price_history" in a["reason"] for a in no_enh["confidence_adjustments"]))

    def test_non_reliable_series_not_core(self):
        tiers = {n: m["tier"] for n, m in load_scoring_config()["metrics"].items()}
        caps = __import__("yaml").safe_load((ROOT / "config" / "provider_capabilities.yaml").read_text())
        reliable = set(caps["fields"]["RELIABLY_AVAILABLE"]) | set(caps["fields"]["DERIVED"])
        cfg = load_scoring_config()
        for name, m in cfg["metrics"].items():
            if tiers[name] == "CORE":
                for c in m["components"].values():
                    self.assertIn(caps["wps_input_to_field"][c["input"]], reliable, (name, c["input"]))
        for f in ("daily_creator_count", "daily_video_count", "similar_listings_count", "price_history"):
            self.assertNotIn(f, reliable)


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
    """3. snapshot hash, provider vs import timestamps"""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = H.HistoryStore(self.tmp)

    def tearDown(self):
        for p in self.tmp.rglob("*"):
            p.chmod(0o755 if p.is_dir() else 0o644)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def obs(self, ts, gmv=41130, window="2026-09-28", task="abc"):
        o = H.empty_observation()
        o.update({"product_id": "9", "identity_key": "9", "source_stage": "discovery", "data_environment": "LIVE",
                  "observation_timestamp": ts, "observation_date": ts[:10],
                  "source": {"task_id": task, "record_index": 0}})
        o["tiktok"]["gmv"] = gmv
        o["provider_observation_timestamp"], o["retrieved_at"] = window, ts
        return o

    def test_duplicate_provider_snapshot_skipped(self):
        a = self.store.append(self.obs("2026-09-30T04:50:00+00:00"))
        b = self.store.append(self.obs("2026-09-30T13:41:00+00:00"))         # same snapshot imported again
        self.assertEqual((a[0], b[0]), ("written", "duplicate_snapshot"))
        self.assertEqual(b[1], a[1])                                          # points at the FIRST one
        self.assertEqual(len(self.store.observations("9")), 1)

    def test_genuinely_new_snapshot_same_day_kept(self):
        self.store.append(self.obs("2026-09-30T04:50:00+00:00"))
        r = self.store.append(self.obs("2026-09-30T13:41:00+00:00", gmv=45000, task="def"))   # new values
        self.assertEqual(r[0], "written")
        r = self.store.append(self.obs("2026-10-01T10:00:00+00:00", window="2026-09-29", task="ghi"))
        self.assertEqual(r[0], "written")                                     # same values, newer window
        self.assertEqual(len(self.store.observations("9")), 3)

    def test_provider_vs_import_timestamps(self):
        _, path = self.store.append(self.obs("2026-09-30T04:50:00+00:00"))
        o = json.loads(Path(path).read_text())
        self.assertEqual(o["provider_observation_timestamp"], "2026-09-28")
        self.assertEqual(o["retrieved_at"], "2026-09-30T04:50:00+00:00")
        self.assertTrue(o["imported_at"])
        self.assertNotEqual(o["imported_at"], o["retrieved_at"])
        self.assertEqual(len(o["source_snapshot_hash"]), 64)

    def test_builders_fill_timestamps(self):
        rec = {"key": "5", "key_type": "product_id", "facts": {"product_id": "5", "data_window_end": "2026-09-28"},
               "calculated": {}, "source": {"fetched_at": "20260930T045000Z", "task_id": "t", "record_index": 0}}
        o = H.from_discovery(rec, "f.json")
        self.assertEqual(o["provider_observation_timestamp"], "2026-09-28")
        self.assertTrue(o["retrieved_at"].startswith("2026-09-30T04:50"))
        self.assertEqual(o["source_snapshot_hash"], H.snapshot_hash(o))

    def test_legacy_duplicates_hidden_on_read(self):
        # rows stored before Step U (no hash field) that are the same provider answer: read view keeps the first
        base = self.obs("2026-09-30T04:50:00+00:00")
        for k in ("provider_observation_timestamp", "retrieved_at"):
            base.pop(k)
        for ts in ("2026-09-30T04:50:00+00:00", "2026-09-30T13:41:00+00:00"):
            p = self.store.products / "9" / f"{ts[:19].replace(':', '')}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({**base, "observation_timestamp": ts}))
        view = R.EnvStore(self.tmp, "LIVE")
        self.assertEqual(len(view.observations("9")), 1)
        self.assertEqual(view.duplicate_snapshots("9"), 1)


class HistoryCount(unittest.TestCase):
    def test_failed_prior_answers_are_not_evidence(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "deep_1.json").write_text(json.dumps({"results": [{"product_id": "9", "status": "failed"}]}))
            self.assertEqual(DA.history_count("9", tmp), 0)
            (tmp / "deep_2.json").write_text(json.dumps({"results": [{"product_id": "9", "status": "ok"}]}))
            self.assertEqual(DA.history_count("9", tmp), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Provenance(unittest.TestCase):
    """4. field provenance preserved"""
    def test_deep_provenance(self):
        r = analyze(TD.deep_obj("1", gmv_30d=42570, gmv_prev_30d=16097, category_product_count=211238,
                                similar_listings_count=None,
                                category_product_count_level="Home Supplies", data_window_end="2026-09-28"))
        pv = r["provenance"]
        for k in ("price_min", "price_avg", "gmv_30d", "units_30d", "growth_30d", "creator_count", "video_count",
                  "competition_count"):
            self.assertIn(k, pv)
            for field in ("value", "provider", "source_field", "scope", "period", "provider_observation_timestamp",
                          "retrieved_at", "availability", "quality"):
                self.assertIn(field, pv[k], (k, field))
        self.assertEqual(pv["growth_30d"]["quality"], "CALCULATED")
        self.assertEqual(pv["growth_30d"]["source_field"], "gmv_30d,gmv_prev_30d")
        self.assertEqual(pv["competition_count"]["availability"], "NOT_COMPARABLE")
        self.assertEqual(pv["gmv_30d"]["provider_observation_timestamp"], "2026-09-28")
        self.assertEqual(pv["gmv_30d"]["retrieved_at"], "2026-09-30T13:46:00+00:00")
        self.assertEqual(r["wps_input_provenance"]["units_sold"], "units_30d")
        self.assertEqual(r["wps_inputs"]["units_sold"], pv["units_30d"]["value"])     # score <- traceable value

    def test_discovery_provenance(self):
        p, _ = D.normalize({"product_id": "5", "product_name": "x", "gmv_30d": 100, "data_window_end": "2026-09-28"},
                           {"category_key": "home", "fetched_at": "20260930T045000Z"}, 0)
        self.assertEqual(p["provenance"]["gmv_30d"]["value"], 100)
        self.assertEqual(p["provenance"]["units_30d"]["availability"], "MISSING")


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


class CalibrationLane(unittest.TestCase):
    """7. calibration_only products isolated from the production ranking"""
    def test_default_disabled(self):
        import yaml
        c = yaml.safe_load((ROOT / "config" / "calibration_lane.yaml").read_text())["calibration"]
        self.assertEqual((c["enabled"], c["max_products"], c["allow_non_qualified_products"]), (False, 3, True))

    def test_calibration_only_isolation(self):
        import os
        from unittest import mock
        import amazon_validation as AV
        import test_runner as T
        tmp = Path(tempfile.mkdtemp())
        real_cfg = AV.load_cfg
        try:
            with mock.patch.dict(os.environ, {"KALOPILOT_TOKEN": T.FAKE}), \
                    mock.patch("urllib.request.urlopen", T.no_network), \
                    mock.patch.object(AV, "load_cfg", lambda *a, **k: {**real_cfg(), "minimum_wps": 101}):
                r = R.Runner(profile_path="config/runtime_first_live.yaml", data_root=tmp, client=T.FakeProvider(),
                             max_products=5, preflight_fn=lambda: T.READY, isatty=False, out=lambda s="": None)
                r.cal_cfg = {**r.cal_cfg, "enabled": True}
                s = r.live(confirm_value=R.CONFIRMATION_PHRASE)
            amz = json.loads((r.ck_dir / "amazon_validation.json").read_text())["results"]
            self.assertTrue(amz)
            self.assertLessEqual(len(amz), 3)
            self.assertTrue(all(a["calibration_only"] for a in amz))
            cal_ids = {a["product_id"] for a in amz}
            bvs = json.loads((r.ck_dir / "bvs.json").read_text())["results"]
            for b in bvs:                          # production BVS keeps its own eligibility; lane rows are tagged
                if b["calibration_only"]:
                    self.assertIn(b["product_id"], cal_ids)
            rep = json.loads(Path(s["outputs"]["report_json"]).read_text())
            self.assertEqual({c["product_id"] for c in rep["calibration_lane"]}, cal_ids)
            ranked = rep["top_products"] + rep["watchlist"] + rep["rejected_products"]
            self.assertTrue(all(p.get("avs") is None for p in ranked))           # calibration AVS never ranked
            self.assertIn("Calibration lane (not ranked)", Path(s["outputs"]["report_markdown"]).read_text())
            for f in (tmp / "data" / "history" / "products").rglob("*.json"):
                self.assertIsNone(json.loads(f.read_text())["amazon"]["avs"])            # not in production history
        finally:
            safety_mod = __import__("safety")
            safety_mod.clear_run_overrides()
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


class CompactDeepPrompt(unittest.TestCase):
    """Step U: one single-product answer hit the ~8k output-token limit."""
    def test_prompt_is_compact(self):
        for f in ("deep_analysis.md", "deep_analysis_batch.md"):
            t = __import__("re").split(r"^===\s*$", (ROOT / "prompts" / f).read_text(), flags=__import__("re").M)[1]
            for gone in ('"daily_units"', '"daily_creator_count"', '"daily_video_count"'):
                self.assertNotIn(gone, t, f)                       # never returned in 9 live answers
            self.assertIn('"daily_gmv"', t)
            self.assertIn("output ONLY the JSON block", t)
        q = DA.load_cfg()["queries"]
        self.assertEqual((q["top_creators"], q["top_videos"]), (5, 5))   # concentration needs top 1 / top 3 only


class ResumeBudget(unittest.TestCase):
    def test_resume_does_not_rebuy_done_stages(self):
        import test_runner as T
        from unittest import mock
        import os
        tmp = Path(tempfile.mkdtemp())
        try:
            with mock.patch.dict(os.environ, {"KALOPILOT_TOKEN": T.FAKE}), \
                    mock.patch("urllib.request.urlopen", T.no_network):
                r = R.Runner(profile_path="config/runtime_first_live.yaml", data_root=tmp, client=T.FakeProvider(),
                             max_products=5, preflight_fn=lambda: T.READY, isatty=False, out=lambda s="": None)
                r.live(confirm_value=R.CONFIRMATION_PHRASE)
                r2 = R.Runner(profile_path="config/runtime_first_live.yaml", data_root=tmp, client=T.FakeProvider(),
                              max_products=5, preflight_fn=lambda: T.READY, isatty=False, out=lambda s="": None)
                r2.cal_cfg = {**r2.cal_cfg, "enabled": True}
                r2._init_run("LIVE", R.SYNTHETIC, r.run_id)
                b = r2.resume_budget(r2.query_budget(R.SYNTHETIC, R.now_utc()))
            self.assertEqual(b["stages"]["discovery"]["expected_paid_max"], 0)
            self.assertEqual(b["stages"]["deep_analysis"]["expected_paid_max"], 0)
            self.assertTrue(b["resume"])
        finally:
            __import__("safety").clear_run_overrides()
            for p in tmp.rglob("*"):
                p.chmod(0o755 if p.is_dir() else 0o644)
            shutil.rmtree(tmp, ignore_errors=True)
