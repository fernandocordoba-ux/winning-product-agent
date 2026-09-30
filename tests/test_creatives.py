"""Step Y — Creative Intelligence. SYNTHETIC data only; no network; no scripts / ad copy stored.
Run: python3 -m unittest discover tests
"""
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

import creatives as CR  # noqa: E402

CFG = CR.load_cfg()
DEEP = {"product_id": "9001", "product_name": "Bamboo Expandable Drawer Divider Organizer", "units": 40000}
LOW = {**DEEP, "units": 800}


def row(i=1, **kw):
    base = {"matched_product_id": "9001", "linked_product_id": "9001", "platform": "tiktok", "format": "video",
            "creative_url": f"https://www.tiktok.com/@c{i}/video/{1000 + i}", "creator": f"creator{i}",
            "views": 10000 * i, "likes": 500 * i, "comments": 40, "shares": 25, "published_at": "2026-09-01",
            "observed_at": "2026-09-30", "hook": "PROBLEM", "angle": "ORGANIZATION", "demo": "no", "ugc_style": "yes",
            "before_after": "no", "comparison": "no", "first_3_second_hook": "yes", "duration": 22}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def norm(*rows):
    out = []
    for r in rows:
        c, errs = CR.normalize_row(r, {"retrieved_at": "2026-09-30T12:00:00+00:00"}, CFG)
        assert not errs, errs
        out.append(c)
    return out


def analyze(rows, deep=DEEP):
    return CR.analyze_product("9001", deep, norm(*rows), CFG)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        for p in self.tmp.rglob("*"):
            p.chmod(0o755 if p.is_dir() else 0o644)
        shutil.rmtree(self.tmp, ignore_errors=True)


class Schema(unittest.TestCase):
    def test_valid_creative(self):
        c = norm(row())[0]
        for k in CR.SCHEMA:
            self.assertIn(k, c)
        self.assertEqual((c["platform"], c["format"], c["hook_category"], c["angle"]), ("TIKTOK", "VIDEO", "PROBLEM",
                                                                                      "ORGANIZATION"))
        self.assertIsNone(c["estimated_gmv"])                                   # never inferred
        self.assertTrue(c["creative_id"].startswith("crv_"))

    def test_no_scripts_or_ad_copy(self):
        _, errs = CR.normalize_row(row(script="Full word-for-word script ...", hook_summary="x" * 200), {}, CFG)
        joined = " ".join(errs)
        self.assertIn("forbidden field", joined)
        self.assertIn("analytical summary, not the ad copy", joined)

    def test_invalid_rows(self):
        _, errs = CR.normalize_row(row(format="hologram", hook="MAGIC", creative_url="tiktok.com/x", gmv=500,
                                       creative_url_extra=None), {}, CFG)
        joined = " ".join(errs)
        for s in ("format must be", "hook_category MAGIC not in the taxonomy", "creative_url must start",
                  "require sales_source"):
            self.assertIn(s, joined)

    def test_providers(self):
        self.assertIsInstance(CR.get_provider("manual_import", CFG), CR.CreativeProvider)
        self.assertIsInstance(CR.get_provider("kalopilot_saved", CFG), CR.CreativeProvider)
        for n in ("kalopilot_live", "meta_ad_library", "minea", "pipiads"):
            with self.assertRaises(CR.ProviderNotIntegrated):
                CR.get_provider(n, CFG)


class Classification(unittest.TestCase):
    def test_hook_classification(self):
        self.assertEqual(norm(row(hook=None, hook_summary="Tutorial: how to split a messy drawer"))[0]["hook_category"],
                         "HOW_TO")
        self.assertEqual(norm(row(hook=None, hook_summary="drawer shot"))[0]["hook_category"], "UNKNOWN")     # unclear
        amb = norm(row(hook=None, hook_summary="before and after vs the cheap one"))[0]
        self.assertEqual(amb["hook_category"], "UNKNOWN")                                                   # ambiguous
        self.assertEqual(norm(row(hook="unknown"))[0]["hook_category"], "UNKNOWN")

    def test_angle_classification(self):
        c = norm(row(angle=None, hook_summary="declutter your kitchen drawer"))[0]
        self.assertEqual((c["angle"], c["angle_source"]), ("ORGANIZATION", "summary_cue"))
        self.assertEqual(norm(row(angle=None, hook_summary="a drawer"))[0]["angle"], "UNKNOWN")


class Matching(unittest.TestCase):
    def test_unreliable_match_excluded(self):
        a = analyze([row(1), row(2, linked_product_id=None, title="Stainless Garden Hose Reel")])
        self.assertEqual((a["qualified_creatives"], a["unreliable_excluded"]), (1, 1))

    def test_no_identity_evidence_not_qualified(self):
        a = analyze([row(1, linked_product_id=None)])
        self.assertEqual(a["qualified_creatives"], 0)

    def test_duplicate_creative(self):
        a = analyze([row(1), row(1, creative_url="https://www.tiktok.com/@c1/video/1001?lang=en"), row(2)])
        self.assertEqual((a["observed"], a["duplicates_removed"], a["qualified_creatives"]), (2, 1, 2))


class Longevity(unittest.TestCase):
    def test_creative_longevity(self):
        a = analyze([row(1, published_at="2026-09-27"), row(2, published_at="2026-09-10"),
                     row(3, published_at="2026-07-15"), row(4, published_at="2026-01-01"),
                     row(5, published_at=None)])
        by = {c["advertiser_or_creator"]: c["longevity"] for c in a["performance"] and []} or \
            {c["creative_id"]: c["longevity"] for c in CR.analyze_product("9001", DEEP, norm(
                row(1, published_at="2026-09-27"), row(2, published_at="2026-09-10"), row(3, published_at="2026-07-15"),
                row(4, published_at="2026-01-01"), row(5, published_at=None)), CFG)["creatives"]}
        self.assertEqual(sorted(by.values()), sorted(["NEW", "EARLY", "ESTABLISHED", "LONG_RUNNING", "UNKNOWN"]))
        self.assertEqual(a["long_running_creatives"], 1)
        self.assertIn("PERSISTENCE only", a["longevity_limitation"])


class Concentration(unittest.TestCase):
    def test_angle_concentration(self):
        a = analyze([row(i) for i in range(1, 8)] + [row(8, angle="GIFT"), row(9, angle="COMFORT"), row(10, angle=None)])
        an = a["angles"]
        self.assertEqual((an["classified"], an["unique"], an["top"]), (9, 3, "ORGANIZATION"))
        self.assertEqual(an["top_share"], round(7 / 9, 4))
        self.assertEqual(an["top_3_share"], 1.0)
        self.assertIn("ANGLE_CONCENTRATION_HIGH", [f["flag"] for f in a["red_flags"]])

    def test_hook_concentration(self):
        a = analyze([row(i) for i in range(1, 6)] + [row(6, hook="CURIOSITY")])
        self.assertEqual(a["hooks"]["top_share"], round(5 / 6, 4))
        self.assertIn("HOOK_SATURATION_HIGH", [f["flag"] for f in a["red_flags"]])

    def test_format_concentration(self):
        a = analyze([row(i) for i in range(1, 6)])
        self.assertEqual(a["formats"]["top_share"], 1.0)
        self.assertIn("LOW_FORMAT_DIVERSITY", [f["flag"] for f in a["red_flags"]])
        b = analyze([row(1), row(2, format="IMAGE", platform="META"), row(3, format="CAROUSEL", platform="META")])
        self.assertAlmostEqual(b["formats"]["top_share"], 0.3333, places=4)

    def test_creator_concentration(self):
        a = analyze([row(i, creator="samecreator") for i in range(1, 6)])
        self.assertIn("CREATOR_CONCENTRATION_HIGH", [f["flag"] for f in a["red_flags"]])
        m = analyze([row(i, creator="brandx", platform="META") for i in range(1, 6)])
        self.assertIn("ADVERTISER_CONCENTRATION_HIGH", [f["flag"] for f in m["red_flags"]])


class Scores(unittest.TestCase):
    def test_creative_saturation_score(self):
        crowded = analyze([row(i, creator="samecreator", published_at="2026-01-01") for i in range(1, 41)])
        diverse = analyze([row(1, hook="CURIOSITY", angle="GIFT"), row(2, hook="HOW_TO", angle="EDUCATION", format="IMAGE",
                                                                            platform="META"),
                           row(3, hook="COMPARISON", angle="CLEANING", format="CAROUSEL", platform="META")])
        self.assertGreater(crowded["saturation"]["score"], diverse["saturation"]["score"])
        self.assertEqual(crowded["saturation"]["components"]["creative_volume"], 0.8)      # 40 / 50
        self.assertIn("CREATIVE_SATURATION_HIGH", [f["flag"] for f in crowded["red_flags"]])

    def test_creative_opportunity_not_inverse(self):
        a = analyze([row(i) for i in range(1, 7)])
        self.assertIsNotNone(a["opportunity"]["score"])
        self.assertNotEqual(a["opportunity"]["score"], round(100 - a["saturation"]["score"], 2))

    def test_low_demand_low_volume_not_high(self):
        few = [row(1, hook="CURIOSITY"), row(2, angle="GIFT", demo="yes")]
        low = analyze(few, deep=LOW)
        self.assertLessEqual(low["opportunity"]["score"], 30)
        self.assertEqual(low["opportunity"]["demand_cap_applied"], 30)
        self.assertIsNone(analyze(few, deep={"product_name": DEEP["product_name"]})["opportunity"]["score"])

    def test_strong_demand_with_creative_gap(self):
        rows = [row(i, demo="no", sales_source="KaloPilot top_videos", gmv=1000 * i) for i in range(1, 7)] + \
               [row(7, demo="yes", hook="DEMONSTRATION", angle="PROBLEM_SOLUTION", views=900000,
                    gmv=9000, sales_source="KaloPilot top_videos")]
        a = analyze(rows)
        self.assertGreaterEqual(a["opportunity"]["score"], 55)
        self.assertEqual(a["opportunity"]["components"]["demonstration_potential"], 1.0)
        self.assertIn("few_demos", [g["gap"] for g in a["creative_gaps"]])

    def test_creative_confidence(self):
        full = analyze([row(i, gmv=100, sales_source="KaloPilot") for i in range(1, 11)])
        thin = analyze([row(1, views=None, likes=None, comments=None, shares=None, hook=None, angle=None,
                            published_at=None, creator=None)])
        self.assertEqual(full["confidence"]["score"], 100.0)
        self.assertLess(thin["confidence"]["score"], 50)
        self.assertIn("LOW_CREATIVE_DATA", [f["flag"] for f in thin["red_flags"]])

    def test_weak_demonstration_flag(self):
        a = analyze([row(i, demo="no") for i in range(1, 7)])
        self.assertIn("WEAK_DEMONSTRATION_EVIDENCE", [f["flag"] for f in a["red_flags"]])

    def test_views_never_profitability(self):
        a = analyze([row(1)])
        self.assertIn("never treated as profitability", a["performance"]["note"])
        self.assertIsNone(a["performance"]["total_sourced_gmv"])


class GapsAndHypotheses(unittest.TestCase):
    def test_gaps_need_evidence(self):
        a = analyze([row(i) for i in range(1, 4)])                          # only 3 creatives -> no gap claims
        self.assertEqual([g for g in a["creative_gaps"] if g["gap"] == "few_demos"], [])
        b = analyze([row(i) for i in range(1, 7)])
        for g in b["creative_gaps"]:
            self.assertTrue(g["evidence"])
            self.assertNotIn("nobody", g["evidence"].lower())

    def test_creative_test_hypotheses(self):
        a = analyze([row(i) for i in range(1, 7)])
        hs = a["creative_test_hypotheses"]
        self.assertTrue(hs)
        for h in hs:
            for k in ("evidence", "observed_gap", "testable_hypothesis", "suggested_format", "suggested_hook_category",
                      "suggested_angle"):
                self.assertTrue(h[k])
            self.assertIn("not a prediction", h["testable_hypothesis"])
            self.assertIn(h["suggested_hook_category"], CFG["hooks"])
        gaps = {h["observed_gap"] for h in hs}
        self.assertTrue(gaps <= {g["gap"] for g in a["creative_gaps"]})     # only from detected gaps

    def test_pattern_library_has_no_text(self):
        a = analyze([row(i, hook_summary="messy drawer problem") for i in range(1, 4)])
        lib = CR.pattern_library([a])
        self.assertEqual(lib[0]["count"], 3)
        self.assertNotIn("messy", json.dumps(lib))

    def test_bvs_adapter_not_wired(self):
        b = CR.bvs_inputs(analyze([row(i) for i in range(1, 7)]))
        for k in ("creative_opportunity", "creative_confidence", "ad_saturation", "format_diversity", "creative_gaps"):
            self.assertIn(k, b)
        self.assertNotIn("creatives", (ROOT / "scripts" / "business_viability.py").read_text())


class Storage(Tmp):
    def test_manual_csv_import(self):
        f = self.tmp / "c.csv"
        f.write_text("matched_product_id,platform,creative_url,format,views,likes,comments,shares,creator,published_at,"
                     "ad_age,estimated_sales,angle,hook,script\n"
                     "9001,tiktok,https://www.tiktok.com/@a/video/1,video,5000,300,20,10,a,2026-09-01,,,ORGANIZATION,PROBLEM,\n"
                     "9001,meta,https://example.test/ad/2,image,,,,,brandx,,45,,,,The full ad script\n")
        r = CR.import_file(f, raw_dir=self.tmp / "raw", processed_dir=self.tmp / "proc")
        self.assertEqual((r["accepted"], len(r["rejected"])), (1, 1))             # the row with a script is rejected
        raw = json.loads(Path(r["raw_file"]).read_text())
        self.assertNotIn("script", json.dumps(raw["rows"]))                        # stripped even from raw
        self.assertEqual(oct(Path(r["raw_file"]).stat().st_mode)[-3:], "444")

    def test_manual_json_import(self):
        f = self.tmp / "c.json"
        f.write_text(json.dumps([row(1), row(2)]))
        self.assertEqual(CR.import_file(f, raw_dir=self.tmp / "raw", processed_dir=self.tmp / "proc")["accepted"], 2)
        self.assertEqual(len(CR.load_all("9001", self.tmp / "proc")), 2)
        bad = self.tmp / "x.json"
        bad.write_text("[1, 2]")
        with self.assertRaises(CR.MalformedCreativeInput):
            CR.import_file(bad, raw_dir=self.tmp / "raw", processed_dir=self.tmp / "proc")

    def test_kalopilot_saved_provider(self):
        import deep_analysis as DA
        obj = {"product_id": "9001", "data_window_end": "2026-09-30",
               "top_videos": [{"video_id": "7551", "creator": "a", "revenue": 12000.5, "views": 800000},
                              {"video_id": "7552", "creator": "b", "revenue": 3000, "views": 90000}]}
        DA.save_raw_deep({"success": True, "data": {"status": "completed", "report": "```json\n" + json.dumps(obj) + "\n```"}},
                         {"observation_timestamp": "2026-09-30T15:00:00+00:00", "market": "US", "product_id": "9001",
                          "query_type": "deep_product"}, self.tmp / "deep")
        r = CR.import_from_kalopilot("9001", self.tmp / "deep", self.tmp / "proc")
        self.assertEqual(r["accepted"], 2)
        cs = CR.load_all("9001", self.tmp / "proc")
        c = [x for x in cs if x["views"] == 800000][0]
        self.assertEqual((c["platform"], c["format"], c["estimated_gmv"], c["hook_category"]),
                         ("TIKTOK", "VIDEO", 12000.5, "UNKNOWN"))
        self.assertIn("KaloPilot", c["sales_source"])
        a = CR.analyze_product("9001", DEEP, cs, CFG)
        self.assertEqual(a["qualified_creatives"], 2)                                # linked product id = 100
        self.assertEqual(a["performance"]["total_sourced_gmv"], 15000.5)

    def test_history_preservation(self):
        h = self.tmp / "hist"
        a1 = analyze([row(i, observed_at="2026-09-30") for i in range(1, 4)])     # >= 3 so a saturation score exists
        self.assertEqual(CR.append_history(a1, h)[0], "written")
        self.assertEqual(CR.append_history(a1, h)[0], "duplicate_snapshot")
        self.assertIsNone(CR.history("9001", h, CFG)["creative_saturation_velocity_per_week"])
        a2 = analyze([row(i, observed_at="2026-10-14") for i in range(1, 6)])
        CR.append_history(a2, h)
        hist = CR.history("9001", h, CFG)
        self.assertEqual(len(hist["snapshots"]), 2)
        self.assertEqual(hist["changes"][0]["new_creatives"], 2)       # creatives 3 and 4 are new; 1-2 are the same URLs
        self.assertIsNotNone(hist["creative_saturation_velocity_per_week"])

    def test_calibration_small(self):
        c = CFG["creative_calibration"]
        self.assertEqual((c["enabled"], c["max_products"], c["max_creatives_per_product"]), (False, 3, 30))


class ReportIntegration(Tmp):
    def test_report_section(self):
        import os
        from unittest import mock
        import safety
        import test_runner as T
        from winning_product_agent import runner as R
        pid = T.E.PID["E1"]
        f = self.tmp / "c.json"
        f.write_text(json.dumps([row(i, matched_product_id=pid, linked_product_id=pid) for i in range(1, 7)]))
        CR.import_file(f, raw_dir=self.tmp / "data/raw/creatives", processed_dir=self.tmp / "data/processed/creatives")
        try:
            with mock.patch.dict(os.environ, {"KALOPILOT_TOKEN": T.FAKE}), \
                    mock.patch("urllib.request.urlopen", T.no_network):
                r = R.Runner(profile_path="config/runtime_first_live.yaml", data_root=self.tmp, client=T.FakeProvider(),
                             max_products=5, preflight_fn=lambda: T.READY, isatty=False, out=lambda s="": None)
                s = r.live(confirm_value=R.CONFIRMATION_PHRASE)
        finally:
            safety.clear_run_overrides()
        md = Path(s["outputs"]["report_markdown"]).read_text()
        self.assertIn("## Creative intelligence", md)
        self.assertIn("Qualified creatives: 6", md)
        js = json.loads(Path(s["outputs"]["report_json"]).read_text())
        self.assertEqual([x for x in js["creative_intelligence"] if x["product_id"] == pid][0]["analysis"]["qualified_creatives"], 6)
        self.assertTrue(list((self.tmp / "data/history/creatives" / pid).glob("*.json")))


if __name__ == "__main__":
    unittest.main()
