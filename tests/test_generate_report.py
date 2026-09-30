"""Tests for the Final Research Report (Step P). SYNTHETIC DATA ONLY, written to
temp folders (never mixed with real research data). Run: python3 -m unittest discover tests
"""
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import generate_report as G  # noqa: E402

CFG = G.load_cfg()
NOW = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
FAKE_TOKEN = "f" * 64                               # synthetic secret, must never appear in outputs


def disc_rec(pid, status="PASS", growth=45.0, reasons=None, name=None):
    return {"key": pid, "key_type": "product_id", "observation_date": "2026-09-30",
            "facts": {"product_id": pid, "product_name": name or f"SYNTHETIC product {pid}", "category": "Home",
                      "shop_name": "Syn Shop", "product_url": f"https://example.test/p/{pid}", "price_min": 30.0,
                      "price_max": 40.0, "growth_30d": growth},
            "source": {"raw_file": f"data/raw/syn_{pid}.json", "task_id": "syn-task", "fetched_at": "20260930T000000Z"},
            "calculated": {"filter_status": status, "filter_reasons": reasons or []}}


def deep_rec(pid, wps=80.0, conf=75.0, ts="2026-09-30T12:00:00+00:00", gmv=100000, creators=400, videos=900,
             trend="GROWING", flags=None, missing=None, top_share=20.0):
    return {"status": "ok", "product_id": pid, "product_name": f"SYNTHETIC product {pid}", "category": "Home",
            "product_url": f"https://example.test/p/{pid}", "shop": {"shop_name": "Syn Shop"},
            "price": {"min": 30.0, "max": 40.0, "avg": 35.0}, "gmv": gmv, "units": 3000,
            "growth": {"growth_30d_pct": 45.0}, "creator_metrics": {"total": creators, "selling": 100},
            "video_metrics": {"total": videos}, "competition_metrics": {"shop_count": 5, "similar_listings_count": 20},
            "trend_metrics": {"label": trend},
            "concentration_metrics": {"metrics": {"top_creator_revenue_share": {"value": top_share},
                                                  "top3_creator_revenue_share": {"value": 40.0},
                                                  "top_video_revenue_share": {"value": 10.0},
                                                  "top3_video_revenue_share": {"value": 25.0}}},
            "wps": wps, "confidence": conf, "confidence_level": "HIGH",
            "wps_breakdown": {"growth_momentum": {"points": 20.0, "max": 25}, "demand": {"points": 12.0, "max": 15},
                              "video_momentum": {"points": 10.0, "max": 15}, "creator_momentum": {"points": 10.0, "max": 15},
                              "competition": {"points": 5.0, "max": 15}, "margin_potential": {"points": 8.0, "max": 10},
                              "trend_stability": {"points": "N/A", "max": 5}},
            "red_flags": [{"flag": f} for f in flags or []], "missing_data": missing or [],
            "observation_timestamp": ts,
            "source": {"raw_file": f"data/raw/deep_analysis/syn_{pid}.json", "task_id": "syn-deep",
                       "discovery_key": pid, "discovery_status": "PASS"}}


def amz_rec(pid, avs=70.0, aconf=80.0):
    return {"status": "ok", "product_id": pid, "avs": avs, "amazon_confidence": aconf,
            "amazon_match_status": "MATCHED", "amazon_match_class": "STRONG", "amazon_match_confidence": 80.0,
            "amazon_price": 38.0, "amazon_competition": "MODERATE", "price_alignment": "GOOD",
            "cross_platform_demand": "STRONG", "amazon_red_flags": [], "missing_data": [],
            "observation_timestamp": "2026-09-30T13:00:00+00:00",
            "source": {"raw_file": f"data/raw/amazon_validation/syn_{pid}.json", "authorization": "Bearer " + FAKE_TOKEN}}


def bvs_rec(pid, bvs=60.0, bconf=40.0, flags=None):
    return {"product_id": pid, "bvs": bvs, "bvs_confidence": bconf,
            "economics": {"selling_price": 35.0, "product_cost": "N/A", "contribution_margin_percent": "N/A"},
            "bvs_breakdown": {"gross_margin_potential": {"score": "N/A", "max": 25}},
            "commercial_red_flags": [{"flag": f, "severe": True} for f in flags or ["INSUFFICIENT_SUPPLIER_DATA"]],
            "missing_data": ["product_cost"], "observation_timestamp": "2026-09-30T14:00:00+00:00",
            "source": {"commercial_data_file": None}}


class Env:
    """Synthetic data/processed tree in a temp folder."""
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.proc, self.out = self.root / "processed", self.root / "reports"
        for sub in ("deep_analysis", "amazon_validation", "business_viability"):
            (self.proc / sub).mkdir(parents=True)

    def write(self, discovery=None, deep=None, amazon=None, bvs=None):
        if discovery is not None:
            cands = [r for r in discovery if r["calculated"]["filter_status"] != "FAIL"]
            failed = [r for r in discovery if r["calculated"]["filter_status"] == "FAIL"]
            summary = {"unique": len(discovery), "PASS": sum(r["calculated"]["filter_status"] == "PASS" for r in discovery),
                       "REVIEW": sum(r["calculated"]["filter_status"] == "REVIEW" for r in discovery)}
            (self.proc / "discovery_20260930T000000Z.json").write_text(
                json.dumps({"summary": summary, "candidates": cands, "failed": failed}))
        for i, batch in enumerate(deep or []):
            (self.proc / "deep_analysis" / f"deep_2026093{i}.json").write_text(json.dumps({"results": batch}))
        if amazon:
            (self.proc / "amazon_validation" / "amazon_1.json").write_text(json.dumps({"results": amazon}))
        if bvs:
            (self.proc / "business_viability" / "bvs_1.json").write_text(json.dumps({"results": bvs}))

    def gen(self, cfg=CFG):
        return G.generate(processed=self.proc, out_dir=self.out, cfg=cfg, now=NOW, secrets=[FAKE_TOKEN])

    def close(self):
        self.tmp.cleanup()


def standard(env):
    ids = ["A", "B", "C", "D", "E"]
    env.write(discovery=[disc_rec(i) for i in ids] + [disc_rec("F", "FAIL", reasons=[{"rule": "gmv_30d_min",
                                                                                         "action": "reject"}])]
              + [disc_rec("G", "REVIEW")],
              deep=[[deep_rec("A", 90, 80), deep_rec("B", 80, 70), deep_rec("C", 80, 90), deep_rec("D", 65, 55),
                     deep_rec("E", 85, 85, flags=["CREATOR_DEPENDENCY"])]],
              amazon=[amz_rec("A")], bvs=[bvs_rec("A"), bvs_rec("C", bvs=70)])


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()


class Generation(Base):
    def test_report_generation(self):
        standard(self.env)
        r = self.env.gen()
        md = r["markdown"].read_text()
        self.assertTrue(r["markdown"].name.startswith("2026-10-01-winning-products"))
        for h in ("## Executive summary", "## Top products", "## Product details", "## Emerging products",
                  "## Watchlist", "## Rejected / high-risk products", "## Data quality"):
            self.assertIn(h, md)
        for bad in ("guaranteed winner", "must buy", "can't miss", "100% profitable"):
            self.assertNotIn(bad.lower(), md.lower())
        self.assertIn("not a launch recommendation", md)

    def test_ranking_logic(self):
        standard(self.env)
        top = self.env.gen()["report"]["top"]
        # A 90 | C 80/conf 90 | B 80/conf 70 ; E rejected (CREATOR_DEPENDENCY), D below thresholds
        self.assertEqual([p["id"] for p in top], ["A", "C", "B"])
        self.assertEqual([p["rank"] for p in top], [1, 2, 3])

    def test_ranking_tiebreak_bvs_then_avs(self):
        self.env.write(discovery=[disc_rec("X"), disc_rec("Y"), disc_rec("Z")],
                       deep=[[deep_rec("X", 80, 70), deep_rec("Y", 80, 70), deep_rec("Z", 80, 70)]],
                       amazon=[amz_rec("Z", avs=90)], bvs=[bvs_rec("Y", bvs=50)])
        top = self.env.gen()["report"]["top"]
        self.assertEqual([p["id"] for p in top], ["Y", "Z", "X"])       # BVS first, then AVS, then id

    def test_top_10_maximum(self):
        ids = [f"P{i:02d}" for i in range(15)]
        self.env.write(discovery=[disc_rec(i) for i in ids], deep=[[deep_rec(i, 70 + int(i[1:])) for i in ids]])
        r = self.env.gen()["report"]
        self.assertEqual(len(r["top"]), 10)
        self.assertEqual(r["top"][0]["id"], "P14")
        overflow = [p for p in r["watch"] if p["watch_reasons"][0].startswith("ranked below Top")]
        self.assertEqual(len(overflow), 5)

    def test_no_combined_score(self):
        standard(self.env)
        js = json.loads(self.env.gen()["json"].read_text())
        self.assertFalse(js["report_metadata"]["scores_combined"])
        for p in js["top_products"]:
            for k in ("overall_score", "master_score", "combined_score"):
                self.assertNotIn(k, p)


class Missing(Base):
    def test_missing_avs_and_bvs_are_na(self):
        standard(self.env)
        r = self.env.gen()
        md = r["markdown"].read_text()
        b = next(p for p in r["report"]["top"] if p["id"] == "B")
        self.assertIsNone(b["avs"])
        self.assertIsNone(b["bvs"])
        row = next(l for l in md.splitlines() if l.startswith("| 3 |"))
        self.assertEqual(row.count("N/A"), 4)                            # AVS, Amazon conf, BVS, BVS conf
        self.assertIn("**Amazon:** N/A (not validated)", md)
        self.assertIn("**Business viability:** N/A (BVS not calculated)", md)

    def test_missing_history(self):
        standard(self.env)
        md = self.env.gen()["markdown"].read_text()
        self.assertIn("WPS: INSUFFICIENT_HISTORY", md)
        self.assertIn("None. Emerging status requires at least 2 observations", md)

    def test_na_handling(self):
        self.assertEqual(G.fmt(None), "N/A")
        self.assertEqual(G.fmt("N/A"), "N/A")
        self.assertEqual(G.fmt(0), "0")                                 # real zero is shown, not N/A
        self.assertEqual(G.fmt(0, "money"), "$0.00")
        self.assertEqual(G.score(None), "N/A")


class Sections(Base):
    def test_emerging_products_section(self):
        self.env.write(discovery=[disc_rec("M")],
                       deep=[[deep_rec("M", 72, 70, ts="2026-09-23T00:00:00+00:00", gmv=80000, creators=300, videos=700)],
                             [deep_rec("M", 80, 72, ts="2026-09-30T00:00:00+00:00", gmv=100000, creators=380, videos=900,
                                       trend="ACCELERATING")]])
        r = self.env.gen()["report"]
        self.assertEqual(len(r["emerging"]), 1)
        e = r["emerging"][0]
        self.assertEqual((e["previous_wps"], e["current_wps"], e["wps_change"]), (72.0, 80.0, 8.0))
        self.assertEqual(e["gmv_change_pct"], 25.0)
        self.assertEqual((e["creator_change"], e["video_change"]), (80.0, 200.0))
        md = self.env.gen()["markdown"].read_text()
        self.assertIn("WPS: ↑", md)
        self.assertIn("GMV: ↑", md)

    def test_single_observation_never_emerging(self):
        self.env.write(discovery=[disc_rec("S")], deep=[[deep_rec("S", 95, 95, trend="ACCELERATING")]])
        self.assertEqual(self.env.gen()["report"]["emerging"], [])

    def test_watchlist(self):
        standard(self.env)
        r = self.env.gen()["report"]
        w = {p["id"]: p for p in r["watch"]}
        self.assertIn("D", w)                                            # WPS 65 / conf 55
        self.assertTrue(any("promising WPS" in x for x in w["D"]["watch_reasons"]))
        self.assertIn("G", w)                                            # REVIEW not yet deep-analyzed
        self.assertTrue(any("awaiting deep analysis" in x for x in w["G"]["watch_reasons"]))

    def test_rejected_products(self):
        standard(self.env)
        r = self.env.gen()["report"]
        rej = {p["id"]: p for p in r["rejected"]}
        self.assertEqual(rej["F"]["primary_rejection_reason"], "LOW_GMV")
        self.assertEqual(rej["E"]["primary_rejection_reason"], "CREATOR_DEPENDENCY")
        self.assertEqual(rej["E"]["wps"], 85.0)                          # high WPS shown, not hidden
        md = self.env.gen()["markdown"].read_text()
        self.assertIn("CREATOR_DEPENDENCY", md.split("## Rejected")[1])

    def test_high_confidence_tier_requires_clean_evidence(self):
        standard(self.env)
        top = self.env.gen()["report"]["top"]
        self.assertTrue(all(p["tier"] == "CANDIDATE" for p in top))      # all miss supplier data

    def test_interpretation_sections(self):
        standard(self.env)
        a = self.env.gen()["report"]["top"][0]
        it = a["interpretation"]
        self.assertTrue(it["why_it_passed"][0].startswith("WPS 90/100"))
        self.assertIn("supplier sourcing", it["next_validation_steps"])
        self.assertTrue(any("product_cost" in x for x in it["what_we_still_need_to_verify"]))

    def test_identity_conflict_not_merged(self):
        rec = deep_rec("999")
        rec["source"]["discovery_key"] = "A"                             # points to A but different product_id
        self.env.write(discovery=[disc_rec("A")], deep=[[rec]])
        r = self.env.gen()["report"]
        self.assertEqual(len(r["summary"]["identity_conflicts"]), 1)


class Outputs(Base):
    def test_json_export(self):
        standard(self.env)
        js = json.loads(self.env.gen()["json"].read_text())
        for k in ("report_metadata", "top_products", "emerging_products", "watchlist", "rejected_products", "data_quality"):
            self.assertIn(k, js)
        self.assertEqual(js["report_metadata"]["market"], "US")
        self.assertEqual(js["top_products"][0]["wps_breakdown"]["trend_stability"]["points"], "N/A")

    def test_dated_report_not_overwritten_latest_replaced(self):
        standard(self.env)
        r1 = self.env.gen()
        first = r1["markdown"].read_text()
        (self.env.proc / "business_viability" / "bvs_2.json").write_text(json.dumps({"results": [bvs_rec("B", bvs=99)]}))
        r2 = self.env.gen()
        self.assertNotEqual(r1["markdown"], r2["markdown"])
        self.assertTrue(r2["markdown"].name.endswith("-2.md"))
        self.assertEqual(r1["markdown"].read_text(), first)              # untouched
        self.assertEqual(r2["latest"].read_text(), r2["markdown"].read_text())   # latest = newest

    def test_source_traceability(self):
        standard(self.env)
        js = json.loads(self.env.gen()["json"].read_text())
        a = js["top_products"][0]
        self.assertEqual(a["source"]["discovery"]["raw_file"], "data/raw/syn_A.json")
        self.assertEqual(a["source"]["deep_analysis"]["observation_timestamp"], "2026-09-30T12:00:00+00:00")
        self.assertIn("raw_file", a["source"]["amazon_validation"])

    def test_secret_exclusion(self):
        standard(self.env)
        r = self.env.gen()
        for path in (r["markdown"], r["json"], r["latest"]):
            text = path.read_text()
            self.assertNotIn(FAKE_TOKEN, text)
            self.assertNotIn("authorization", text.lower())
        scrubbed = G.scrub({"a": {"api_key": "x", "note": f"tok {FAKE_TOKEN}"}}, ["api_key"], [FAKE_TOKEN])
        self.assertEqual(scrubbed, {"a": {"note": "tok [REDACTED]"}})

    def test_data_quality(self):
        standard(self.env)
        dq = self.env.gen()["report"]["data_quality"]
        self.assertEqual(dq["products_with_wps"], 5)
        self.assertEqual(dq["pct_with_amazon_data"], 20.0)
        self.assertEqual(dq["pct_with_supplier_data"], 0.0)
        self.assertEqual(dq["pct_with_history"], 0.0)

    def test_empty_inputs(self):
        r = self.env.gen()
        self.assertIn("No product currently meets the Top criteria", r["markdown"].read_text())


if __name__ == "__main__":
    unittest.main()
