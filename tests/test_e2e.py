"""End-to-end integration tests (Step S): Discovery -> Deep Analysis (WPS + Confidence)
-> Amazon (AVS + Amazon Confidence) -> BVS (+ BVS Confidence) -> History -> Emerging
-> Report, on 10 SYNTHETIC scenarios in a temp workspace.

- Network is blocked for the whole module (any real HTTP call fails the tests).
- Paid calls go to in-memory fake providers only; no live query can run.
- A fake credential is planted in the environment to prove it never leaks.
Run: python3 -m unittest discover tests
"""
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import amazon_validation as AV  # noqa: E402
import business_viability as B  # noqa: E402
import deep_analysis as DA  # noqa: E402
import discovery as D  # noqa: E402
import emerging as EM  # noqa: E402
import generate_report as GR  # noqa: E402
import history as H  # noqa: E402
import pipeline as PL  # noqa: E402
import safety  # noqa: E402
from score_products import load_scoring_config  # noqa: E402
import test_deep_analysis as TD  # noqa: E402

FAKE_TOKEN = "e2e_" + "7" * 60
FILTERS, CATS = D.load_yaml("filters.yaml"), D.load_yaml("categories.yaml")
DCFGS = {"deep": DA.load_cfg(), "filters": FILTERS, "scoring": load_scoring_config()}

NAMES = {"E1": "Bamboo Drawer Divider Kit", "E2": "Linen Pillow Cover Duo", "E3": "Walnut Spice Rack Tower",
         "E4": "Cotton Rope Storage Basket", "E5": "Velvet Hanger Bundle Pro", "E6": "Magnetic Knife Strip Board",
         "E7": "Cedar Shoe Shelf Bench", "E8": "Acacia Serving Tray Wide", "E9": "Felt Desk Pad Organizer",
         "E10": "Tiny Wool Coaster Pair"}


def hi_profile(pid, **kw):
    """Calibrated with the real WPS engine: WPS 88.53, Confidence 97.5 (see Step S notes)."""
    base = dict(product_name=NAMES[pid], gmv_30d=150000, units_30d=40000, growth_30d_pct=90.0, category_growth_pct=40.0,
                category_product_count=900, video_count=6000, selling_video_count=900, video_sales_share_pct=92.0,
                creator_count=3500, selling_creator_count=800, commission_pct=8.0, price_min=45.0, price_max=55.0,
                similar_listings_count=15, shop_count=4,
                top_creators=[{"creator_id": str(i), "name": f"c{i}", "revenue": r, "growth_pct": g}
                              for i, (r, g) in enumerate([(15000, 20), (12000, 15), (10000, 12), (8000, 9)])],
                top_videos=[{"video_id": str(i), "creator": "c", "revenue": r, "views": 1000}
                            for i, r in enumerate([9000, 8000, 7000])])
    base.update(kw)
    return TD.deep_obj(pid, **base)


DEEP = {
    "E1": hi_profile("E1"),                                      # strong emerging (history accelerating)
    "E2": hi_profile("E2", similar_listings_count=0),            # stable (history flat); real 0 listings
    "E3": hi_profile("E3"),                                      # losing momentum (history declining)
    "E4": hi_profile("E4", daily_gmv=None, daily_units=None, shop_count=None, similar_listings_count=None,
                     top_videos=None, launch_date=None,
                     top_creators=[{"creator_id": str(i), "name": f"c{i}", "revenue": None, "growth_pct": g}
                                   for i, g in enumerate([20, 15, 12, 9])]),   # high WPS, low confidence
    "E5": hi_profile("E5"),                                      # high WPS, bad BVS
    "E6": hi_profile("E6"),                                      # TikTok strong, Amazon weak
    "E7": hi_profile("E7", top_creators=[{"creator_id": "0", "name": "viral", "revenue": 120000, "growth_pct": 50},
                                         {"creator_id": "1", "name": "b", "revenue": 3000, "growth_pct": 5},
                                         {"creator_id": "2", "name": "c", "revenue": 2000, "growth_pct": 5}]),
    "E8": hi_profile("E8", top_videos=[{"video_id": "0", "creator": "v", "revenue": 112500, "views": 9},
                                       {"video_id": "1", "creator": "v", "revenue": 3000, "views": 9},
                                       {"video_id": "2", "creator": "v", "revenue": 2000, "views": 9}]),
    "E9": hi_profile("E9", selling_creator_count=None, selling_video_count=None, shop_count=None, price_history=None),
}


def disc_record(pid):
    d = DEEP.get(pid) or {}
    return {"product_id": pid.replace("E", "9000"), "_pid": pid, "product_name": NAMES[pid],
            "product_url": f"https://example.test/p/{pid}", "shop_name": "Syn Shop", "category_path": "Home Supplies",
            "price_min": 45.0, "price_max": 55.0, "gmv_30d": d.get("gmv_30d", 1000),
            "units_30d": d.get("units_30d", 50), "growth_30d_pct": 90.0, "creator_count": d.get("creator_count", 5),
            "selling_creator_count": d.get("selling_creator_count", 1), "video_count": d.get("video_count", 5),
            "shop_count": d.get("shop_count"), "launch_date": "2026-03-01", "data_window_end": "2026-09-29"}


PID = {pid: pid.replace("E", "9000") for pid in NAMES}          # synthetic numeric product ids
BY_NUM = {v: k for k, v in PID.items()}


class FakeDeep:
    def __init__(self, fail=None):
        self.submits, self.fail = [], fail or {}

    def credits(self):
        return {"totalRemain": 1000.0}

    def submit(self, q):
        self.submits.append(q)
        return {"success": True, "data": {"task_id": f"t{len(self.submits)}"}}

    def wait(self, task_id):
        num = re.search(r"product ID (\d+)\)", self.submits[-1]).group(1)
        pid = BY_NUM[num]
        mode = self.fail.get(pid)
        if mode == "unavailable":
            return {"success": False, "error_category": "provider_unavailable", "message": "unreachable"}
        if mode == "timeout":
            return {"success": False, "error_category": "timeout", "data": {"status": "running"}}
        if mode == "malformed":
            return {"success": True, "data": {"status": "completed", "report": "no json here"}}
        if mode == "empty":
            return {}
        obj = copy.deepcopy(DEEP[pid])
        obj["product_id"] = num
        return {"success": True, "data": {"status": "completed", "task_id": task_id, "credits_consumed": 3.0,
                                          "report": "```json\n" + json.dumps(obj) + "\n```"}}


def amazon_obj(pid):
    if pid == "E6":
        cand = {"title": "Stainless Steel Garden Hose Reel 100 FT", "brand": "Zeta", "category_path": "Garden",
                "price": 20.0, "rating": 3.1, "review_count": 25000, "bsr": 90000, "attributes": []}
        return {"candidates": [cand], "search": {"comparable_listings_count": 3000, "recent_trend": "declining",
                                                 "top_brands": [{"brand": "Z", "share_pct": 70}]}}
    cand = {"title": NAMES[pid] + " Home Storage", "url": f"https://amazon.example/{pid}", "brand": None,
            "category_path": "Home & Kitchen > Home Supplies", "price": 50.0, "rating": 4.5, "review_count": 800,
            "bsr": 5000, "monthly_sales_estimate": 900, "attributes": []}
    return {"candidates": [cand], "search": {"comparable_listings_count": 150, "comparable_price_min": 30,
                                             "comparable_price_max": 70, "recent_trend": "growing",
                                             "top_brands": [{"brand": "A", "share_pct": 10}]}}


class FakeAmazon:
    def __init__(self):
        self.submits = []

    def credits(self):
        return {"totalRemain": 1000.0}

    def submit(self, q):
        self.submits.append(q)
        return {"success": True, "data": {"task_id": f"a{len(self.submits)}"}}

    def wait(self, task_id):
        name = re.search(r"Name: (.+)", self.submits[-1]).group(1).strip()
        pid = next(k for k, v in NAMES.items() if v == name)
        return {"success": True, "data": {"status": "completed",
                                          "report": "```json\n" + json.dumps(amazon_obj(pid)) + "\n```"}}


def commercial(pid):
    good = {"selling_price": 50.0, "product_cost": 8.0, "supplier_shipping_cost": 5.0, "estimated_refund_rate_pct": 3.0,
            "estimated_chargeback_rate_pct": 0.5, "estimated_ad_cost_per_order": 10.0, "delivery_days_max": 6,
            "tracking_available": True, "fragile": False, "contains_battery": False, "contains_liquid": False,
            "oversized": False, "special_handling": False, "customs_risk": False,
            "suppliers": [{"name": f"S{i}", "price": 8.0 + i * 0.2, "rating": 4.8, "processing_days": 1,
                           "us_warehouse": True, "stock": 2000, "moq": 1} for i in range(3)],
            "actual_refund_rate_pct": 2.0, "compliance_review": {"status": "cleared", "by": "synthetic"}}
    if pid == "E5":
        good.update(product_cost=36.0, supplier_shipping_cost=12.0, estimated_ad_cost_per_order=15.0)
    return good


def history_row(deep_rec, ts, f_gmv=1.0, f_cnt=1.0, wps_delta=0.0):
    o = H.empty_observation()
    cm, vm = deep_rec["creator_metrics"], deep_rec["video_metrics"]
    conc = deep_rec["concentration_metrics"]["metrics"]
    o.update({"product_id": deep_rec["product_id"], "identity_key": deep_rec["product_id"],
              "product_name": deep_rec["product_name"], "observation_timestamp": H.iso(ts),
              "observation_date": ts.date().isoformat(), "source_stage": "synthetic_prior"})
    o["scores"].update(wps=round(deep_rec["wps"] + wps_delta, 2), confidence=deep_rec["confidence"])
    o["tiktok"].update(gmv=round(deep_rec["gmv"] * f_gmv, 2), units=round(deep_rec["units"] * f_gmv, 2),
                       creator_count=round(cm["total"] * f_cnt), selling_creator_count=round(cm["selling"] * f_cnt),
                       video_count=round(vm["total"] * f_cnt), selling_video_count=round(vm["selling"] * f_cnt),
                       shop_count=deep_rec["competition_metrics"]["shop_count"])
    o["concentration"].update(top_creator_revenue_share=conc["top_creator_revenue_share"]["value"],
                              top_video_revenue_share=conc["top_video_revenue_share"]["value"])
    o["source"] = {"stage": "synthetic_prior", "raw_file": "synthetic"}
    return o


PRIOR = {  # 6 earlier cycles, 2 days apart, oldest first (factors relative to the current Deep values)
    "E1": ([0.40, 0.45, 0.52, 0.60, 0.70, 0.83], [0.55, 0.60, 0.66, 0.73, 0.81, 0.90], [-9, -7.5, -6, -4.5, -3, -1.5]),
    "E2": ([1.0, 1.002, 0.999, 1.001, 1.0, 1.0], [1.0, 1.0, 1.0, 1.0, 1.0, 1.0], [0, 0, 0, 0, 0, 0]),
    "E3": ([1.80, 1.65, 1.50, 1.35, 1.20, 1.10], [1.0, 1.0, 1.0, 1.0, 1.0, 1.0], [0, 0, 0, 0, 0, 0]),
}


class World:
    """Runs the whole pipeline once in a temp workspace."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.raw, self.proc = self.tmp / "data" / "raw", self.tmp / "data" / "processed"
        self.hist_dir, self.reports = self.tmp / "data" / "history", self.tmp / "reports"
        for d in (self.raw, self.proc, self.hist_dir, self.reports):
            d.mkdir(parents=True)
        self.now = datetime.now(timezone.utc)

    def run(self):
        # Discovery (the raw response is synthetic; no provider call)
        recs = [{k: v for k, v in disc_record(p).items() if k != "_pid"} for p in NAMES]
        meta = {"category_key": "home", "task_id": "syn-disc", "market": "US", "currency": "USD",
                "fetched_at": (self.now - timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ"),
                "observation_date": self.now.date().isoformat()}
        resp = {"success": True, "data": {"status": "completed", "task_id": "syn-disc",
                                          "report": "```json\n" + json.dumps(recs) + "\n```"}}
        self.disc_raw = D.save_raw(resp, meta, self.raw)
        self.discovery = D.run_discovery([self.disc_raw], FILTERS, CATS)
        D.save_processed(self.discovery, self.proc)
        # Deep Analysis (fake provider)
        self.deep_client = FakeDeep()
        self.deep = DA.run(live=True, client=self.deep_client, processed_dir=self.proc, raw_dir=self.raw / "deep_analysis",
                           out_dir=self.proc / "deep_analysis", cfgs=DCFGS)
        self.deep_path = Path(self.deep["saved"])
        self.deep_by = {BY_NUM[r["product_id"]]: r for r in self.deep["results"] if r.get("status") == "ok"}
        # Amazon (fake provider)
        self.amz_client = FakeAmazon()
        self.amz = AV.run(live=True, client=self.amz_client, deep_path=self.deep_path, raw_dir=self.raw / "amazon_validation",
                          out_dir=self.proc / "amazon_validation", cfg=AV.load_cfg(), filters_cfg=FILTERS)
        self.amz_by = {BY_NUM[r["product_id"]]: r for r in self.amz["results"]}
        # BVS (synthetic commercial data; E9 has none)
        for pid in NAMES:
            if pid != "E9":
                B.save_commercial_data(PID[pid], commercial(pid), raw_dir=self.raw / "business_viability")
        self.bvs = B.run(deep_path=self.deep_path, amazon_path=Path(self.amz["saved"]), raw_dir=self.raw / "business_viability",
                         out_dir=self.proc / "business_viability")
        self.bvs_by = {BY_NUM[r["product_id"]]: r for r in self.bvs["results"]}
        # History: migrate processed outputs, then add earlier synthetic cycles for E1-E3
        self.store = H.HistoryStore(self.hist_dir)
        self.migration = H.migrate(self.proc, self.store, dry_run=False)
        deep_ts = H.parse_ts(self.deep_by["E1"]["observation_timestamp"])
        for pid, (fg, fc, dw) in PRIOR.items():
            for k, (a, b, c) in enumerate(zip(fg, fc, dw)):
                ts = deep_ts - timedelta(days=2 * (6 - k))
                self.store.append(history_row(self.deep_by[pid], ts, a, b, c))
        self.store.rebuild_index()
        # Emerging + report
        self.emerging = EM.detect_all(self.store, now=self.now)
        self.em_by = {BY_NUM.get(r["product_id"], r["product_id"]): r for r in self.emerging["results"]}
        self.em_file, _ = EM.save(self.emerging, self.proc / "emerging")
        self.report = GR.generate(processed=self.proc, out_dir=self.reports, history_dir=self.hist_dir, now=self.now,
                                  secrets=[FAKE_TOKEN])
        self.report_json = json.loads(self.report["json"].read_text())
        return self

    def all_files(self):
        return [p for p in self.tmp.rglob("*") if p.is_file()]


W = None
_patches = []


def setUpModule():
    global W
    _patches.extend([mock.patch("urllib.request.urlopen", side_effect=AssertionError("network blocked in e2e tests")),
                     mock.patch.dict(os.environ, {"KALOPILOT_TOKEN": FAKE_TOKEN})])
    for p in _patches:
        p.start()
    W = World().run()


def tearDownModule():
    for p in _patches:
        p.stop()
    for f in W.tmp.rglob("*"):
        if f.is_file():
            os.chmod(f, 0o644)
    shutil.rmtree(W.tmp)


def report_entry(pid):
    j = W.report_json
    for sec in ("top_products", "watchlist", "rejected_products"):
        for p in j[sec]:
            if p.get("product_id") == PID[pid]:
                return sec, p
    return None, None


# =============================================================================
class Stage1FullPipeline(unittest.TestCase):
    def test_all_stages_ran_with_fakes_only(self):
        self.assertEqual(W.discovery["summary"]["unique"], 10)
        self.assertEqual(len(W.deep_client.submits), 9)                       # E10 rejected at Discovery
        self.assertEqual(len(W.deep_by), 9)
        self.assertGreaterEqual(len(W.amz_client.submits), 7)
        self.assertTrue(W.report["markdown"].exists())

    def test_1_strong_emerging(self):
        self.assertEqual(W.em_by["E1"]["emerging_status"], "EMERGING_STRONG", W.em_by["E1"]["status_reason"])
        self.assertEqual(W.report_json["emerging_products"][0]["product_id"], PID["E1"])

    def test_2_stable(self):
        self.assertEqual(W.em_by["E2"]["emerging_status"], "STABLE", W.em_by["E2"]["status_reason"])

    def test_3_losing_momentum(self):
        self.assertEqual(W.em_by["E3"]["emerging_status"], "LOSING_MOMENTUM")
        self.assertIn("DEMAND_DECLINING", W.em_by["E3"]["emerging_flags"])

    def test_4_high_wps_low_confidence(self):
        d = W.deep_by["E4"]
        self.assertGreaterEqual(d["wps"], 70)
        self.assertLess(d["confidence"], 60)
        sec, p = report_entry("E4")
        self.assertEqual(sec, "watchlist")
        self.assertTrue(any("promising WPS" in r for r in p["watch_reasons"]))
        self.assertNotIn("E4", W.amz_by)                                     # not eligible for Amazon

    def test_5_high_wps_bad_bvs(self):
        b = W.bvs_by["E5"]
        self.assertGreaterEqual(W.deep_by["E5"]["wps"], 70)
        self.assertIn("NEGATIVE_CONTRIBUTION_MARGIN", [f["flag"] for f in b["commercial_red_flags"]])
        sec, p = report_entry("E5")
        self.assertEqual(sec, "rejected_products")                            # shown, not hidden
        self.assertEqual(p["wps"], W.deep_by["E5"]["wps"])

    def test_6_tiktok_strong_amazon_weak(self):
        a = W.amz_by["E6"]
        self.assertEqual(a["amazon_match_status"], "NO_RELIABLE_MATCH")
        self.assertIsNone(a["amazon_price"])                                  # never filled from unrelated item
        self.assertLess(a["avs"], W.amz_by["E1"]["avs"])
        self.assertGreaterEqual(W.deep_by["E6"]["wps"], 70)

    def test_7_creator_dependency(self):
        self.assertIn("CREATOR_DEPENDENCY", [f["flag"] for f in W.deep_by["E7"]["red_flags"]])
        self.assertEqual(report_entry("E7")[1]["primary_rejection_reason"], "CREATOR_DEPENDENCY")

    def test_8_video_dependency(self):
        self.assertIn("VIDEO_DEPENDENCY", [f["flag"] for f in W.deep_by["E8"]["red_flags"]])
        self.assertEqual(report_entry("E8")[1]["primary_rejection_reason"], "VIDEO_DEPENDENCY")

    def test_9_missing_optional_data(self):
        d = W.deep_by["E9"]
        for k in ("selling_creator_count", "selling_video_count", "shop_count", "price_history"):
            self.assertIn(k, d["missing_data"])
        self.assertIsNone(d["creator_metrics"]["selling"])
        self.assertIn("INSUFFICIENT_SUPPLIER_DATA", [f["flag"] for f in W.bvs_by["E9"]["commercial_red_flags"]])
        self.assertEqual(W.bvs_by["E9"]["economics"]["product_cost"], "N/A")

    def test_10_rejected(self):
        c = W.discovery["failed"]
        self.assertEqual([p["facts"]["product_id"] for p in c], [PID["E10"]])
        sec, p = report_entry("E10")
        self.assertEqual((sec, p["primary_rejection_reason"]), ("rejected_products", "LOW_GMV"))


class Stage2ScoreIndependence(unittest.TestCase):
    def test_scores_differ_independently(self):
        e5, e6, e4, e3 = W.deep_by["E5"], W.deep_by["E6"], W.deep_by["E4"], W.deep_by["E3"]
        self.assertEqual(e5["wps"], W.deep_by["E1"]["wps"])                    # same TikTok profile, same WPS ...
        self.assertLess(W.bvs_by["E5"]["bvs"], W.bvs_by["E1"]["bvs"])          # ... but different BVS
        self.assertLess(W.amz_by["E6"]["avs"], W.amz_by["E1"]["avs"])          # ... and different AVS
        self.assertLess(e4["confidence"], e3["confidence"])                    # confidence differs, WPS both >= 70
        self.assertNotEqual(W.em_by["E1"]["emerging_status"], W.em_by["E3"]["emerging_status"])   # momentum differs

    def test_later_stages_never_modify_wps_or_confidence(self):
        for pid, d in W.deep_by.items():
            obs = [o for o in H.get_product_history(PID[pid], W.store) if o["source_stage"] == "deep_analysis"]
            self.assertEqual(obs[-1]["scores"]["wps"], d["wps"])
            self.assertEqual(obs[-1]["scores"]["confidence"], d["confidence"])
            sec, p = report_entry(pid)
            self.assertEqual((p["wps"], p["confidence"]), (d["wps"], d["confidence"]))

    def test_rerunning_amazon_bvs_emerging_leaves_deep_file_unchanged(self):
        h = hashlib.sha256(W.deep_path.read_bytes()).hexdigest()
        B.run(deep_path=W.deep_path, amazon_path=None, raw_dir=W.raw / "business_viability", save=False)
        EM.detect_all(W.store, now=W.now)
        AV.run(live=False, deep_path=W.deep_path, raw_dir=W.raw / "amazon_validation", cfg=AV.load_cfg(), filters_cfg=FILTERS)
        self.assertEqual(hashlib.sha256(W.deep_path.read_bytes()).hexdigest(), h)

    def test_confidence_scores_are_separate_fields(self):
        p = report_entry("E1")[1]
        for k in ("confidence", "amazon_confidence", "bvs_confidence"):
            self.assertIsInstance(p[k], float, k)
        self.assertEqual(p["amazon_confidence"], W.amz_by["E1"]["amazon_confidence"])
        self.assertEqual(p["bvs_confidence"], W.bvs_by["E1"]["bvs_confidence"])


class Stage3DataIntegrity(unittest.TestCase):
    def test_missing_stays_null_and_zero_is_preserved(self):
        self.assertEqual(W.deep_by["E2"]["competition_metrics"]["similar_listings_count"], 0)
        self.assertEqual(report_entry("E2")[1]["tiktok"]["competition_metrics"]["similar_listings_count"], 0)
        obs = [o for o in H.get_product_history(PID["E9"], W.store) if o["source_stage"] == "deep_analysis"][-1]
        self.assertIsNone(obs["tiktok"]["selling_creator_count"])
        self.assertIsNone(obs["tiktok"]["shop_count"])

    def test_no_fallback_values_inserted(self):
        e4 = W.deep_by["E4"]
        self.assertEqual(e4["wps_breakdown"]["trend_stability"]["points"], "N/A")
        self.assertIsNone(e4["sales_history"]["daily_gmv"])
        self.assertEqual(e4["concentration_metrics"]["metrics"]["top_video_revenue_share"]["value"], "N/A")

    def test_raw_preserved_read_only_and_not_mutated(self):
        raws = [p for p in W.raw.rglob("*.json")]
        self.assertGreaterEqual(len(raws), 1 + 9 + 7 + 8)
        for p in raws:
            self.assertFalse(os.stat(p).st_mode & 0o222, p)
        env = json.loads(W.disc_raw.read_text())
        self.assertEqual(json.loads(re.search(r"```json\n(.*)\n```", env["response"]["data"]["report"], re.S).group(1))[0]
                         ["product_name"], NAMES["E1"])

    def test_history_append_only(self):
        files = sorted((W.hist_dir / "products").rglob("*.json"))
        for p in files:
            self.assertFalse(os.stat(p).st_mode & 0o222)
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        r = H.migrate(W.proc, W.store, dry_run=False)
        self.assertEqual(r["summary"]["new"], 0)
        self.assertEqual({p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}, before)

    def test_no_secrets_anywhere(self):
        for p in W.all_files():
            self.assertNotIn(FAKE_TOKEN, p.read_text(errors="ignore"), p)
            self.assertNotIn("Authorization", p.read_text(errors="ignore"), p)


class Stage4Identity(unittest.TestCase):
    def test_stable_id_joins_all_stages(self):
        p = report_entry("E1")[1]
        self.assertEqual(set(p["source"]), {"discovery", "deep_analysis", "amazon_validation", "business_viability"})
        self.assertIsNotNone(p["history_snapshot"])

    def test_similar_names_not_merged_and_fallback_identity(self):
        recs = [{"product_id": "111", "product_name": "Silicone Brush Set"},
                {"product_id": "222", "product_name": "Silicone Brush Set"},
                {"product_id": None, "product_name": "Silicone Brush"},
                {"product_id": None, "product_name": "Silicone Brushes"}]
        for r in recs:
            r.update(price_min=30, price_max=40, gmv_30d=50000, units_30d=900, growth_30d_pct=50, shop_name="S")
        tmp = Path(tempfile.mkdtemp())
        try:
            resp = {"data": {"report": "```json\n" + json.dumps(recs) + "\n```"}}
            path = D.save_raw(resp, {"category_key": "home", "task_id": "x", "fetched_at": "20260930T000000Z"}, tmp)
            res = D.run_discovery([path], FILTERS, CATS)
            keys = sorted(p["key"] for p in res["candidates"] + res["failed"])
            self.assertEqual(len(keys), 4)                                     # none merged
            self.assertEqual(sum(k.startswith("fallback:") for k in keys), 2)
        finally:
            os.chmod(path, 0o644)
            shutil.rmtree(tmp)

    def test_changed_title_same_id_matches(self):
        # Deep Analysis returned the same product_id; the report joins by id even if the title differs
        inputs = GR.load_inputs(W.proc)
        d = copy.deepcopy(inputs["deep"])
        for r in d:
            if r["product_id"] == PID["E2"]:
                r["product_name"] = NAMES["E2"] + " (2026 Edition)"
        products, conflicts = GR.build_products({**inputs, "deep": d}, GR.load_cfg())
        e2 = [p for p in products if p["product_id"] == PID["E2"]]
        self.assertEqual(len(e2), 1)
        self.assertEqual(conflicts, [])

    def test_low_confidence_identity_not_merged(self):
        inputs = GR.load_inputs(W.proc)
        d = copy.deepcopy(inputs["deep"])
        rogue = copy.deepcopy(next(r for r in d if r["product_id"] == PID["E2"]))
        rogue["product_id"] = "55555"                                          # points to E2's discovery key, other id
        rogue["source"]["discovery_key"] = PID["E2"]
        d = [r for r in d if r["product_id"] != PID["E2"]] + [rogue]
        products, conflicts = GR.build_products({**inputs, "deep": d}, GR.load_cfg())
        self.assertEqual(len(conflicts), 1)
        self.assertTrue(any(p["product_id"] == "55555" for p in products))    # kept separate


class Stage5Duplicates(unittest.TestCase):
    def test_discovery_duplicates_across_categories(self):
        r = D.run_discovery([W.disc_raw, W.disc_raw], FILTERS, CATS)
        self.assertEqual(r["summary"]["unique"], 10)
        self.assertEqual(r["summary"]["duplicates_removed"], 10)

    def test_deep_and_amazon_duplicate_queries_prevented(self):
        dres = json.loads(sorted(W.proc.glob("discovery_*.json"))[-1].read_text())
        sel, _ = DA.select_candidates({"candidates": dres["candidates"] * 2}, DCFGS["deep"])
        pl = DA.plan(sel, DCFGS["deep"], raw_dir=W.tmp / "none")
        self.assertEqual(sum(i["action"] == "skip_duplicate" for i in pl["items"]), len(sel) // 2)
        el, _ = AV.select_eligible([*W.deep["results"], *W.deep["results"]], AV.load_cfg())
        apl = AV.plan(el, AV.load_cfg(), W.now, W.tmp / "none")
        self.assertEqual(sum(i["action"] == "skip_duplicate" for i in apl["items"]), len(el) // 2)

    def test_cache_prevents_repeat_paid_queries(self):
        c = FakeDeep()
        r = DA.run(live=True, client=c, processed_dir=W.proc, raw_dir=W.raw / "deep_analysis",
                   out_dir=W.tmp / "deep_rerun", cfgs=DCFGS)
        self.assertEqual(len(c.submits), 0)                                   # all 9 served from 24h cache
        self.assertEqual(r["plan"]["cache_hits"], 9)

    def test_history_duplicates_but_different_timestamps_kept(self):
        o = history_row(W.deep_by["E2"], H.parse_ts("2026-01-01T00:00:00+00:00"))
        self.assertEqual(W.store.append(o)[0], "written")
        self.assertEqual(W.store.append(o)[0], "duplicate")
        o2 = history_row(W.deep_by["E2"], H.parse_ts("2026-01-01T06:00:00+00:00"))
        self.assertEqual(W.store.append(o2)[0], "written")                     # same day, different time: kept

    def test_report_one_record_per_product(self):
        ids = [p["product_id"] for s in ("top_products", "watchlist", "rejected_products") for p in W.report_json[s]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_emerging_outputs_never_overwritten(self):
        p2, latest = EM.save(W.emerging, W.proc / "emerging")
        self.assertNotEqual(p2, W.em_file)
        self.assertTrue(W.em_file.exists())
        self.assertEqual(json.loads(latest.read_text())["products_evaluated"], W.emerging["products_evaluated"])

    def test_same_cycle_collapse_regression(self):
        # Discovery + Deep of the same run are ONE cycle for momentum (bug found in Step S)
        obs = H.get_product_history(PID["E1"], W.store)
        self.assertEqual(len(H.collapse_same_cycle(obs, 12)), len(obs) - 1)
        self.assertNotIn("MOMENTUM_SLOWING", W.em_by["E1"]["emerging_flags"])


class Stage6FailureRecovery(unittest.TestCase):
    def test_provider_failures_isolated(self):
        tmp = W.tmp / "fail_run"
        c = FakeDeep(fail={"E1": "unavailable", "E2": "timeout", "E3": "malformed", "E5": "empty"})
        r = DA.run(live=True, client=c, processed_dir=W.proc, raw_dir=tmp / "raw", out_dir=tmp / "out", cfgs=DCFGS)
        status = {BY_NUM.get(x["product_id"], x["product_id"]): x["status"] for x in r["results"]}
        for pid in ("E1", "E2", "E3", "E5"):
            self.assertEqual(status[pid], "malformed", pid)                   # recorded, nothing fabricated
        self.assertEqual(sum(v == "ok" for v in status.values()), 5)          # unaffected products continue
        for x in r["results"]:
            if x["status"] == "malformed":
                self.assertNotIn("wps", x)

    def test_amazon_unavailable(self):
        class Down(FakeAmazon):
            def wait(self, task_id):
                return {"success": False, "error_category": "provider_unavailable"}
        r = AV.run(live=True, client=Down(), deep_path=W.deep_path, raw_dir=W.tmp / "amz_fail",
                   out_dir=W.tmp / "amz_fail_out", cfg=AV.load_cfg(), filters_cfg=FILTERS)
        self.assertTrue(all(x["status"] == "malformed" for x in r["results"]))
        self.assertNotIn("avs", r["results"][0])

    def test_insufficient_credits_stops_safely(self):
        class Poor(FakeDeep):
            def credits(self):
                return {"totalRemain": 6.0}
        r = DA.run(live=True, client=Poor(), processed_dir=W.proc, raw_dir=W.tmp / "poor", out_dir=W.tmp / "poor_out",
                   cfgs=DCFGS)
        self.assertEqual(r["stopped"]["reason"], "insufficient_credits")
        self.assertEqual(r["paid_queries_done"], 0)

    def test_corrupted_processed_record_and_history_untouched(self):
        root = W.tmp / "corrupt_root"
        shutil.copytree(ROOT / "config", root / "config")
        (root / "data" / "processed" / "deep_analysis").mkdir(parents=True)
        for d in ("raw", "history"):
            (root / "data" / d).mkdir(parents=True, exist_ok=True)
        (root / "reports").mkdir()
        shutil.copy(sorted(W.proc.glob("discovery_*.json"))[-1], root / "data" / "processed")
        (root / "data" / "processed" / "deep_analysis" / "deep_corrupt.json").write_text("{not json")
        hist_before = {p: p.read_bytes() for p in (W.hist_dir / "products").rglob("*.json")}
        r = PL.dry_run(root, write_manifest=False, preflight_result={"status": "READY_FOR_DRY_RUN",
                                                                      "blocking_reasons": [], "warnings": []})
        errs = [s for s in r["stages"] if s["status"] == "error"]
        self.assertTrue(errs)
        self.assertTrue(any("JSONDecodeError" in s["error"] for s in errs))
        self.assertTrue(any(s["status"] == "planned" for s in r["stages"]))   # other stages continued
        self.assertEqual({p: p.read_bytes() for p in (W.hist_dir / "products").rglob("*.json")}, hist_before)


class Stage11DryRun(unittest.TestCase):
    def test_dry_run_plans_everything_and_spends_nothing(self):
        root = W.tmp / "dry_root"
        shutil.copytree(ROOT / "config", root / "config")
        shutil.copytree(W.tmp / "data", root / "data")
        (root / "reports").mkdir()
        for f in root.rglob("*"):
            if f.is_file():
                os.chmod(f, 0o644)
        before = {p: p.stat().st_mtime for p in (root / "data").rglob("*") if p.is_file()}
        r = PL.dry_run(root, check_balance=True, balance_fn=lambda: 72.24,
                       preflight_result={"status": "READY_FOR_DRY_RUN", "blocking_reasons": [], "warnings": []})
        self.assertEqual(r["mode"], "DRY_RUN")
        self.assertFalse(r["live_gate"]["allowed"])
        names = [s["stage"] for s in r["stages"]]
        self.assertEqual(names, ["discovery", "deep_analysis", "amazon_validation", "bvs_preparation",
                                 "history_preparation", "emerging_preparation", "report_preview"])
        self.assertEqual(r["query_budget"]["executed_queries"], 0)
        self.assertEqual(r["query_budget"]["remaining_balance"], 72.24)
        self.assertEqual({p: p.stat().st_mtime for p in (root / "data").rglob("*") if p.is_file()}, before)
        m = json.loads(Path(r["manifest"]).read_text())
        for k in ("run_id", "timestamp", "market", "runtime_mode", "configured_limits", "eligible_product_counts",
                  "planned_provider_queries", "cache_status", "config_hashes"):
            self.assertIn(k, m)
        self.assertNotIn(FAKE_TOKEN, Path(r["manifest"]).read_text() + Path(r["log"]).read_text())


class Stage14ReportConsistency(unittest.TestCase):
    def test_report_values_equal_stored_values(self):
        for pid in W.deep_by:
            sec, p = report_entry(pid)
            self.assertEqual(p["wps"], W.deep_by[pid]["wps"])
            self.assertEqual(p["confidence"], W.deep_by[pid]["confidence"])
            if pid in W.amz_by:
                self.assertEqual(p["avs"], W.amz_by[pid]["avs"])
            if pid in W.bvs_by:
                self.assertEqual(p["bvs"], W.bvs_by[pid]["bvs"])
        e = W.report_json["emerging_products"][0]
        self.assertEqual(e["momentum_score"], W.em_by["E1"]["momentum_score"])
        self.assertEqual(e["momentum_confidence"], W.em_by["E1"]["momentum_confidence"])

    def test_markdown_shows_stored_values(self):
        md = W.report["markdown"].read_text()
        row = next(l for l in md.splitlines() if l.startswith(f"| 1 | {NAMES['E1']}") or
                   (l.startswith("| ") and NAMES["E1"] in l and "EMERGING" not in l))
        self.assertIn(GR.fmt(W.deep_by["E1"]["wps"]), row)
        self.assertIn(GR.fmt(W.em_by["E1"]["momentum_score"]), md)


if __name__ == "__main__":
    unittest.main()
