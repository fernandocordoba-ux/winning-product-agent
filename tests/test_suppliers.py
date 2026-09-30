"""Step W — Supplier Data Integration. SYNTHETIC data only; no network, no orders, no supplier contact.
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
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import business_viability as B  # noqa: E402
import discovery as D  # noqa: E402
import suppliers as S  # noqa: E402

CFG = S.load_cfg()
TK = {"name": "Rechargeable Electric Shower Back Scrubber Long Handle 5 Heads", "brand": None, "variant": None}
SELL = 29.99


def row(**kw):
    base = {"matched_product_id": "9001", "supplier_name": "Syn Supplier A", "supplier_url": "https://example.test/a",
            "supplier_product_title": "Electric Shower Back Scrubber Rechargeable Long Handle 5 Heads",
            "product_cost": 7.50, "shipping_cost": 3.20, "estimated_delivery_min_days": 5,
            "estimated_delivery_max_days": 8, "processing_time_days": 2, "rating": 4.8, "reviews": 2400,
            "orders": 8000, "moq": 1, "warehouse": "yes", "tracking": "yes", "inventory": 3000}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def offer(**kw):
    o, errs = S.normalize_row(row(**kw), {"retrieved_at": "2026-09-30T12:00:00+00:00"}, CFG)
    assert not errs, errs
    return o


def ev(offers):
    return S.evaluate_product("9001", TK, offers, CFG, SELL)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dirs = dict(raw_dir=self.tmp / "raw", processed_dir=self.tmp / "proc", hist_dir=self.tmp / "hist")

    def tearDown(self):
        for p in self.tmp.rglob("*"):
            p.chmod(0o755 if p.is_dir() else 0o644)
        shutil.rmtree(self.tmp, ignore_errors=True)


class Schema(unittest.TestCase):
    def test_valid_supplier_offer(self):
        o = offer()
        for k in S.OFFER_SCHEMA:
            self.assertIn(k, o)
        self.assertEqual((o["product_cost"], o["shipping_cost"], o["us_warehouse_available"]), (7.5, 3.2, True))
        self.assertEqual(o["currency"], "USD")
        self.assertTrue(o["offer_id"].startswith("off_"))
        self.assertIsNone(o["package_weight_kg"])                       # not provided -> None, never invented

    def test_missing_product_cost(self):
        _, errs = S.normalize_row(row(product_cost=None), {}, CFG)
        self.assertIn("missing required field: product_cost", errs)

    def test_missing_shipping_cost(self):
        _, errs = S.normalize_row(row(shipping_cost=None), {}, CFG)
        self.assertIn("missing required field: shipping_cost", errs)

    def test_malformed_values_rejected(self):
        _, errs = S.normalize_row(row(product_cost="cheap", estimated_delivery_min_days=12,
                                      estimated_delivery_max_days=5, supplier_url="example.test", rating=9,
                                      currency="EUR"), {}, CFG)
        joined = " ".join(errs)
        for s in ("product_cost", "min_days 12 > estimated_delivery_max_days 5", "supplier_url", "rating 9",
                  "currency EUR"):
            self.assertIn(s, joined)


class Providers(unittest.TestCase):
    def test_only_real_integrations(self):
        self.assertIsInstance(S.get_provider("manual_import", CFG), S.SupplierProvider)
        for name in ("aliexpress", "cjdropshipping", "alibaba", "zendrop", "autods", "kalopilot", "unknown"):
            with self.assertRaises(S.ProviderNotIntegrated):
                S.get_provider(name, CFG)

    def test_manual_provider_interface(self):
        p = S.get_provider("manual_import", CFG, rows=[row(), row(matched_product_id="42")])
        found = p.search_product({"product_id": "9001"})
        self.assertEqual(len(found), 1)
        self.assertEqual(p.get_shipping_quote(found[0])["shipping_cost"], 3.2)
        self.assertEqual(p.normalize_offer(p.get_offer_details(found[0]), {})["product_cost"], 7.5)


class Matching(unittest.TestCase):
    def test_strong_match(self):
        sc, cls, _ = S.match_confidence(TK, offer(), CFG)
        self.assertGreaterEqual(sc, 75)
        self.assertIn(cls, ("STRONG", "VERY_STRONG"))

    def test_unreliable_match_not_used_for_economics(self):
        o = offer(supplier_product_title="Silicone Kitchen Dish Sponge Holder Tray")
        r = ev([o])
        s = r["offers"][0]
        self.assertLess(s["match_confidence_calc"], 60)
        self.assertEqual(s["match_class"], "UNRELIABLE")
        self.assertEqual(s["supplier_match_status"], "REVIEW")
        self.assertFalse(s["eligible_for_economics"])
        self.assertIsNone(r["selected_supplier_offer_id"])
        self.assertIn("UNRELIABLE_PRODUCT_MATCH", [f["flag"] for f in s["supplier_flags"]])

    def test_no_title_is_review_not_guess(self):
        o = offer(supplier_product_title=None)
        s = ev([o])["offers"][0]
        self.assertIsNone(s["match_confidence_calc"])
        self.assertEqual(s["supplier_match_status"], "REVIEW")

    def test_manual_verified_match(self):
        s = ev([offer(supplier_product_title=None, match_confidence=95)])["offers"][0]
        self.assertEqual((s["match_confidence_calc"], s["match_class"]), (95.0, "VERY_STRONG"))


class Scores(unittest.TestCase):
    def test_landed_cost_excludes_unknown_duty(self):
        lc, note = S.landed_cost(offer())
        self.assertEqual(lc, 10.7)
        self.assertIn("EXCLUDES unknown import duties", note)
        lc2, note2 = S.landed_cost(offer(import_duty_per_order=1.3))
        self.assertEqual(lc2, 12.0)
        self.assertIn("explicit", note2)

    def test_fast_delivery(self):
        tier, score, days = S.delivery_tier(offer(estimated_delivery_max_days=5, processing_time_days=1), CFG)
        self.assertEqual((tier, days), ("STRONGEST", 6))

    def test_slow_delivery(self):
        o = ev([offer(estimated_delivery_min_days=12, estimated_delivery_max_days=16, processing_time_days=1)])["offers"][0]
        self.assertEqual(o["delivery_tier"], "WEAK")
        self.assertIn("SLOW_DELIVERY", [f["flag"] for f in o["supplier_flags"]])
        o = ev([offer(estimated_delivery_min_days=15, estimated_delivery_max_days=25)])["offers"][0]
        self.assertEqual(o["delivery_tier"], "POOR")
        self.assertIn("VERY_SLOW_DELIVERY", [f["flag"] for f in o["supplier_flags"]])

    def test_moq(self):
        o = ev([offer(moq=50)])["offers"][0]
        self.assertIn("MOQ_TOO_HIGH", [f["flag"] for f in o["supplier_flags"]])
        self.assertLess(o["quality_detail"]["components"]["moq"], 1.0)

    def test_no_tracking(self):
        o = ev([offer(tracking="no")])["offers"][0]
        self.assertIn("NO_TRACKING", [f["flag"] for f in o["supplier_flags"]])
        self.assertEqual(o["quality_detail"]["components"]["tracking"], 0.0)

    def test_us_warehouse(self):
        yes = ev([offer()])["offers"][0]
        no = ev([offer(warehouse="no")])["offers"][0]
        self.assertGreater(yes["supplier_quality"], no["supplier_quality"])
        self.assertIn("NO_US_WAREHOUSE", [f["flag"] for f in no["supplier_flags"]])
        self.assertNotIn("NO_US_WAREHOUSE", [f["flag"] for f in yes["supplier_flags"]])

    def test_supplier_quality_score(self):
        q, det = S.supplier_quality(offer(), CFG)
        self.assertGreaterEqual(q, 90)
        o = offer(rating=None, reviews=None, orders=None, moq=None, inventory=None, processing_time_days=None)
        q2, det2 = S.supplier_quality(o, CFG)               # only tracking + warehouse = 20 pts < 40 -> N/A
        self.assertIsNone(q2)
        self.assertIn("rating", det2["missing"])

    def test_supplier_confidence_separate_and_reduced_by_missing(self):
        full = ev([offer()])["offers"][0]
        self.assertGreater(full["supplier_confidence"], 90)
        o = offer()
        for k in ("supplier_rating", "supplier_review_count", "supplier_order_count", "minimum_order_quantity",
                  "available_quantity", "tracking_available"):
            o[k] = None
        sc, lvl, _ = S.supplier_confidence(o, 90, CFG)
        self.assertLess(sc, full["supplier_confidence"])
        self.assertEqual(S.supplier_confidence(offer(), None, CFG)[0], full["supplier_confidence"] -
                         25 * full["match_confidence_calc"] / 100)          # no match evidence -> 0 of 25

    def test_low_inventory_rating_orders(self):
        o = ev([offer(inventory=40, rating=4.2, orders=30)])["offers"][0]
        fl = [f["flag"] for f in o["supplier_flags"]]
        for f in ("LOW_INVENTORY", "LOW_SUPPLIER_RATING", "LOW_ORDER_HISTORY"):
            self.assertIn(f, fl)

    def test_high_costs_need_selling_price(self):
        o = S.evaluate_product("9001", TK, [offer(product_cost=15, shipping_cost=6)], CFG, None)["offers"][0]
        self.assertNotIn("HIGH_PRODUCT_COST", [f["flag"] for f in o["supplier_flags"]])   # no price -> no flag
        o = ev([offer(product_cost=15, shipping_cost=6)])["offers"][0]
        fl = [f["flag"] for f in o["supplier_flags"]]
        self.assertIn("HIGH_PRODUCT_COST", fl)
        self.assertIn("HIGH_SHIPPING_COST", fl)


class Ranking(unittest.TestCase):
    def three(self):
        cheap_slow = offer(supplier_name="Cheap Slow", supplier_url="https://example.test/c", product_cost=4.9,
                           shipping_cost=2.1, estimated_delivery_min_days=12, estimated_delivery_max_days=20,
                           processing_time_days=3, warehouse="no", rating=4.6, reviews=900, orders=3000, inventory=5000)
        fast_us = offer(supplier_name="Fast US", supplier_url="https://example.test/f")
        mid = offer(supplier_name="Mid CN", supplier_url="https://example.test/m", product_cost=6.0, shipping_cost=2.5,
                    estimated_delivery_min_days=7, estimated_delivery_max_days=10, warehouse="no", rating=4.7,
                    reviews=1500, orders=5000)
        return cheap_slow, fast_us, mid

    def test_multiple_suppliers_ranked(self):
        r = ev(list(self.three()))
        self.assertEqual(r["offer_count"], 3)
        self.assertEqual(r["qualified_offers"], 3)
        self.assertEqual([o["supplier_offer_rank"] for o in r["offers"]], [1, 2, 3])
        self.assertNotIn("SINGLE_SUPPLIER_DEPENDENCY", [f["flag"] for f in r["supplier_flags"]])

    def test_cheapest_supplier_not_automatically_selected(self):
        cheap_slow, fast_us, mid = self.three()
        r = ev([cheap_slow, fast_us, mid])
        sel = next(o for o in r["offers"] if o["offer_id"] == r["selected_supplier_offer_id"])
        self.assertEqual(sel["supplier_name"], "Fast US")
        cheapest = min(r["offers"], key=lambda o: o["landed_cost"])
        self.assertEqual(cheapest["supplier_name"], "Cheap Slow")
        self.assertNotEqual(sel["offer_id"], cheapest["offer_id"])

    def test_single_supplier_dependency(self):
        r = ev([offer()])
        self.assertIn("SINGLE_SUPPLIER_DEPENDENCY", [f["flag"] for f in r["supplier_flags"]])

    def test_ineligible_never_selected(self):
        r = ev([offer(inventory_status="OUT_OF_STOCK", inventory=None)])
        self.assertIsNone(r["selected_supplier_offer_id"])
        self.assertEqual(r["supplier_viability"], "N/A")


class BVSIntegration(Tmp):
    def deep(self):
        import test_e2e as E
        return {"product_id": "9001", "product_name": TK["name"], "price": {"min": 29.99, "max": 29.99, "avg": 29.99},
                "category": "Beauty & Personal Care > Bath & Body Care > Bathing Accessories", "wps": 80, "confidence": 80,
                "status": "ok", "source": {"discovery_status": "PASS"}, "competition_metrics": {}, "video_metrics": {},
                "creator_metrics": {}, "growth": {}, "red_flags": [], "_E": E}

    def test_bvs_uses_selected_offer_and_missing_cac(self):
        cheap_slow, fast_us, mid = Ranking().three()
        ev_ = ev([cheap_slow, fast_us, mid])
        comm = S.to_commercial_data(ev_)
        d = self.deep()
        d.pop("_E")
        cfg, fcfg = B.load_cfg(), D.load_yaml("filters.yaml")
        r = B.evaluate(d, None, comm, cfg, fcfg, "supplier_layer")
        e = r["economics"]
        self.assertEqual(e["product_cost"], 7.5)                      # Fast US, not the cheapest (4.9)
        self.assertEqual(e["supplier_shipping_cost"], 3.2)
        self.assertEqual(e["landed_cost"], 10.7)
        self.assertIn("excludes unknown import duties", e["landed_cost_note"])
        self.assertNotEqual(e["gross_profit_before_ads"], "N/A")
        self.assertNotEqual(e["gross_margin_percent"], "N/A")
        self.assertEqual(e["contribution_margin_percent"], "N/A")      # ad cost (CAC) unknown -> never invented
        self.assertEqual(r["selected_supplier_offer_id"], fast_us["offer_id"])
        self.assertEqual(r["supplier_metrics"]["primary_supplier"]["name"], "Fast US")
        self.assertNotIn("INSUFFICIENT_SUPPLIER_DATA", [f["flag"] for f in r["commercial_red_flags"]])

    def test_no_qualified_offer_keeps_bvs_partial(self):
        comm = S.to_commercial_data(ev([offer(supplier_product_title="Dish Sponge Holder")]))
        d = self.deep()
        d.pop("_E")
        r = B.evaluate(d, None, comm, B.load_cfg(), D.load_yaml("filters.yaml"))
        with_offer = B.evaluate(d, None, S.to_commercial_data(ev([offer()])), B.load_cfg(), D.load_yaml("filters.yaml"))
        self.assertEqual(r["economics"]["product_cost"], "N/A")
        self.assertEqual(r["supplier_viability"], "N/A")
        self.assertIn("INSUFFICIENT_SUPPLIER_DATA", [f["flag"] for f in r["commercial_red_flags"]])
        self.assertLess(r["bvs_confidence"], with_offer["bvs_confidence"])

    def test_run_reads_supplier_layer(self):
        imp = self.tmp / "offers.json"
        imp.write_text(json.dumps([row()]))
        res = S.import_file(imp, **self.dirs)
        self.assertEqual(res["accepted"], 1)
        d = self.deep()
        d.pop("_E")
        deep_file = self.tmp / "deep.json"
        deep_file.write_text(json.dumps({"results": [d]}))
        cfg = B.load_cfg()
        cfg["eligibility"]["minimum_wps"] = 0
        r = B.run(deep_path=deep_file, raw_dir=self.tmp / "legacy", cfg=cfg, save=False,
                  suppliers_dir=self.dirs["processed_dir"])
        self.assertEqual(r["results"][0]["economics"]["product_cost"], 7.5)
        self.assertIsNotNone(r["results"][0]["selected_supplier_offer_id"])


class Import(Tmp):
    def test_manual_csv_import(self):
        f = self.tmp / "offers.csv"
        f.write_text("matched_product_id,supplier_name,supplier_url,product_cost,shipping_cost,"
                     "estimated_delivery_min_days,estimated_delivery_max_days,rating,reviews,order_count,MOQ,warehouse,"
                     "tracking,inventory,weight,dimensions\n"
                     "9001,Syn A,https://example.test/a,7.50,3.20,5,8,4.8,2400,8000,1,yes,yes,3000,0.6,30x10x8\n"
                     "9001,Syn B,https://example.test/b,,3.20,5,8,,,,,,,,,\n")
        r = S.import_file(f, **self.dirs)
        self.assertEqual((r["rows"], r["accepted"], len(r["rejected"])), (2, 1, 1))
        self.assertIn("missing required field: product_cost", r["rejected"][0]["errors"])
        o = S.load_offers("9001", self.dirs["processed_dir"])[0]
        self.assertEqual((o["package_weight_kg"], o["package_dimensions_cm"], o["minimum_order_quantity"]),
                         (0.6, "30x10x8", 1))
        raw = json.loads(Path(r["raw_file"]).read_text())
        self.assertEqual(len(raw["rows"]), 2)                         # raw keeps rejected rows too (audit)

    def test_manual_json_import(self):
        f = self.tmp / "offers.json"
        f.write_text(json.dumps({"offers": [row(), row(supplier_name="Syn C", supplier_url="https://example.test/c")]}))
        r = S.import_file(f, **self.dirs)
        self.assertEqual(r["accepted"], 2)
        self.assertEqual(len(S.load_offers("9001", self.dirs["processed_dir"])), 2)

    def test_malformed_supplier_input(self):
        for name, text in (("bad.json", "{not json"), ("bad.json", json.dumps({"x": 1})), ("empty.csv", "a,b\n"),
                           ("offers.txt", "hello")):
            f = self.tmp / name
            f.write_text(text)
            with self.assertRaises(S.MalformedSupplierInput):
                S.import_file(f, **self.dirs)

    def test_historical_quotes_preserved(self):
        f1 = self.tmp / "q1.json"
        f1.write_text(json.dumps([row(observed_at="2026-09-30T10:00:00+00:00")]))
        f2 = self.tmp / "q2.json"
        f2.write_text(json.dumps([row(observed_at="2026-10-07T10:00:00+00:00", product_cost=8.25, shipping_cost=3.5)]))
        S.import_file(f1, **self.dirs)
        r = S.import_file(f1, **self.dirs)                            # same quote again -> not a new observation
        self.assertEqual(r["history"]["duplicate_snapshots_skipped"], 1)
        S.import_file(f2, **self.dirs)
        oid = S.load_offers("9001", self.dirs["processed_dir"])[0]["offer_id"]
        h = S.price_history(oid, self.dirs["hist_dir"])
        self.assertEqual(len(h["observations"]), 2)
        self.assertEqual(h["changes"][0]["changes"]["product_cost"], {"from": 7.5, "to": 8.25, "pct": 10.0})
        self.assertEqual(S.load_offers("9001", self.dirs["processed_dir"])[0]["product_cost"], 8.25)   # latest used
        self.assertEqual(len(list((self.dirs["raw_dir"]).glob("*.json"))), 3)                          # raw never overwritten


class Calibration(unittest.TestCase):
    def test_disabled_by_default_and_small(self):
        c = CFG["supplier_calibration"]
        self.assertEqual((c["enabled"], c["max_products"], c["max_offers_per_product"]), (False, 3, 5))
        self.assertEqual(S.calibration_plan(["1", "2", "3", "4"], CFG)["products"], [])
        cfg = copy.deepcopy(CFG)
        cfg["supplier_calibration"]["enabled"] = True
        self.assertEqual(S.calibration_plan(["1", "2", "3", "4"], cfg)["products"], ["1", "2", "3"])

    def test_ranking_not_price_first(self):
        self.assertNotEqual(CFG["ranking"]["rank_by"][0], "landed_cost")


if __name__ == "__main__":
    unittest.main()


class ReportIntegration(Tmp):
    def test_report_shows_supplier_validation_and_comparison(self):
        import os
        from unittest import mock
        import safety
        import test_runner as T
        sys.path.insert(0, str(ROOT))
        from winning_product_agent import runner as R
        pid = T.E.PID["E1"]
        name = T.E.NAMES["E1"]
        offers = [row(matched_product_id=pid, supplier_name=n, supplier_url=f"https://example.test/{n}",
                      supplier_product_title=name, product_cost=c, shipping_cost=s, estimated_delivery_max_days=d,
                      warehouse=w) for n, c, s, d, w in (("Alpha", 9.0, 3.0, 8, "yes"), ("Beta", 6.5, 2.0, 18, "no"),
                                                         ("Gamma", 8.0, 2.5, 10, "no"))]
        f = self.tmp / "offers.json"
        f.write_text(json.dumps(offers))
        S.import_file(f, raw_dir=self.tmp / "data/raw/suppliers", processed_dir=self.tmp / "data/processed/suppliers",
                      hist_dir=self.tmp / "data/history/suppliers")
        try:
            with mock.patch.dict(os.environ, {"KALOPILOT_TOKEN": T.FAKE}), \
                    mock.patch("urllib.request.urlopen", T.no_network):
                r = R.Runner(profile_path="config/runtime_first_live.yaml", data_root=self.tmp, client=T.FakeProvider(),
                             max_products=5, preflight_fn=lambda: T.READY, isatty=False, out=lambda s="": None)
                s = r.live(confirm_value=R.CONFIRMATION_PHRASE)
        finally:
            safety.clear_run_overrides()
        md = Path(s["outputs"]["report_markdown"]).read_text()
        self.assertIn("## Supplier validation", md)
        self.assertIn("Selected supplier: Alpha", md)                  # fastest/best-ranked, not cheapest (Beta)
        self.assertIn("| Rank | Supplier | Match |", md)
        js = json.loads(Path(s["outputs"]["report_json"]).read_text())
        sv = [x for x in js["supplier_validation"] if x["product_id"] == pid][0]
        self.assertEqual(sv["layer"]["selected"]["supplier_name"], "Alpha")
        self.assertEqual(sv["landed_cost"], 12.0)
