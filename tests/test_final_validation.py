"""FINAL STEP — Product Validation & Launch Gate. SYNTHETIC data only; network blocked; nothing is ordered,
no supplier is contacted and no ad money is spent."""
import ast
import copy
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import decision_engine as DE  # noqa: E402
import final_validation as FV  # noqa: E402
import test_decision as TD  # noqa: E402
import test_production as TP  # noqa: E402

CFG = FV.load_cfg()
FEES = {"payment_processing": {"percent": 2.9, "fixed": 0.30}, "platform_fee": {"percent": 0.0, "fixed": 0.0},
        "refund_rate_pct": None, "chargeback_rate_pct": None, "chargeback_fee": 15.0}
OFFER = {"offer_id": "OF1", "supplier_name": "Syn Supplier", "match_class": "VERY_STRONG", "product_cost": 6.0,
         "shipping_cost": 3.0, "effective_delivery_days": 9, "tracking_available": True, "supplier_rating": 4.8,
         "supplier_order_count": 5000, "available_quantity": 2000, "inventory_status": "IN_STOCK",
         "minimum_order_quantity": 1, "supplier_quality": 82, "supplier_confidence": 85}
APPROVED = {**FV.INPUT_TEMPLATE, "sample_status": "APPROVED", "sample_available": True, "final_quote_confirmed": True,
            "final_product_cost": 6.0, "final_shipping_cost": 3.0, "shopify_selling_price": 39.99,
            "sample_checklist": {k: True for k in CFG["sample"]["checklist"]},
            "reviews": {"ip_trademark": "PASSED", "ad_policy": "PASSED"}}


def candidate(state_kw=None, offer=None, flags=()):
    p = TD.product(flags=list(flags), env="PRODUCTION", **(state_kw or {}))
    d = DE.decide(DE.build_evidence(p, TD.comp(), TD.crea(), "PRODUCTION"), DE.load_cfg())
    return {"product_id": d["product_id"], "name": d["name"], "source_state": d["decision_state"], "decision": d,
            "bvs": {"bvs": 74.0, "economics": {"selling_price": 39.99},
                    "supplier_layer": {"selected": OFFER if offer is None else offer}},
            "competitor": None, "creative": None}


def ev(c=None, **inp):
    c = c or candidate()
    return FV.evaluate(c, {**copy.deepcopy(APPROVED), **inp}, CFG, FEES)


class Economics(unittest.TestCase):
    def test_break_even_and_scenarios(self):
        u = FV.unit_economics(40.0, 6.0, 3.0, FEES, [25, 30, 35, 40, 45])
        self.assertEqual(u["landed_cost"], 9.0)
        self.assertEqual(u["transaction_fees"], round(40 * 0.029 + 0.30, 2))
        self.assertEqual(u["gross_profit"], round(40 - 9 - 1.46, 2))
        self.assertEqual(u["gross_margin_percent"], round((40 - 9 - 1.46) / 40 * 100, 2))
        self.assertEqual(u["break_even_cac"], u["gross_profit"])                 # no refund rate configured
        self.assertIn("never assumed", u["refund_note"])
        self.assertEqual([x["cac_pct_of_price"] for x in u["cac_scenarios"]], [25, 30, 35, 40, 45])
        s25 = u["cac_scenarios"][0]
        self.assertEqual(s25["cac"], 10.0)
        self.assertEqual(s25["contribution_profit"], round(u["break_even_cac"] - 10.0, 2))

    def test_refund_allowance_only_when_configured(self):
        u = FV.unit_economics(40.0, 6.0, 3.0, {**FEES, "refund_rate_pct": 5}, [30])
        self.assertEqual(u["expected_refund_cost"], 2.0)
        self.assertEqual(u["break_even_cac"], round(u["gross_profit"] - 2.0, 2))

    def test_missing_costs_stay_none(self):
        u = FV.unit_economics(40.0, None, 3.0, FEES, [30])
        self.assertIsNone(u["landed_cost"])
        self.assertIsNone(u["break_even_cac"])
        self.assertIsNone(u["cac_scenarios"][0]["contribution_profit"])


class LaunchStates(unittest.TestCase):
    def test_approved_product_is_launch_test_ready(self):
        r = ev()
        self.assertEqual(r["state"], FV.LAUNCH, r["reasons"])
        self.assertTrue(r["test_plan"])
        self.assertIn("BLOCKED", r["test_plan"]["spend_status"])                  # budgets unset -> no spend
        self.assertIn("Launch controlled Shopify test (manual — the system never spends ad money)", r["action_queue"])

    def test_sample_not_approved(self):
        for st in ("NOT_ORDERED", "ORDERED", "RECEIVED"):
            r = ev(sample_status=st)
            self.assertEqual(r["state"], FV.VALIDATE, st)
            self.assertIsNone(r["test_plan"])
        self.assertIn("Order sample (manual — the system never orders)", ev(sample_status="NOT_ORDERED")["action_queue"])
        self.assertEqual(ev(sample_status="REJECTED")["state"], FV.REJECT)

    def test_missing_supplier_economics(self):
        r = ev(candidate(offer={}), final_quote_confirmed=False, final_product_cost=None, final_shipping_cost=None)
        self.assertEqual(r["state"], FV.VALIDATE)
        self.assertIsNone(r["economics"]["break_even_cac"])
        self.assertTrue(any("no selected supplier offer" in x for x in r["reasons"]["missing"]))

    def test_negative_unit_economics(self):
        r = ev(final_product_cost=30.0, final_shipping_cost=9.0)
        self.assertEqual(r["state"], FV.REJECT)                                    # confirmed quote + final price
        r2 = ev(final_quote_confirmed=False, shopify_selling_price=None,
                candidate=None) if False else ev(candidate(offer={**OFFER, "product_cost": 30.0, "shipping_cost": 9.0}),
                                                 final_quote_confirmed=False)
        self.assertEqual(r2["state"], FV.HOLD)                                     # preliminary data: hold, not reject

    def test_positive_unit_economics(self):
        r = ev()
        self.assertTrue(r["positive_unit_economics"])
        self.assertGreater(r["economics"]["break_even_cac"], 0)

    def test_thin_margin_holds(self):
        r = ev(final_product_cost=24.0, final_shipping_cost=5.0)          # ~24 % gross margin < 30 %
        self.assertEqual(r["state"], FV.HOLD)

    def test_unresolved_ip_issue(self):
        r = ev(candidate(flags=["IP_REVIEW_REQUIRED"]), reviews={"ip_trademark": "PENDING", "ad_policy": "PASSED"})
        self.assertNotEqual(r["state"], FV.LAUNCH)
        self.assertEqual(ev(reviews={"ip_trademark": "FAILED", "ad_policy": "PASSED"})["state"], FV.REJECT)

    def test_slow_shipping(self):
        self.assertEqual(ev(candidate(offer={**OFFER, "effective_delivery_days": 17}))["state"], FV.VALIDATE)
        self.assertEqual(ev(candidate(offer={**OFFER, "effective_delivery_days": 25}))["state"], FV.REJECT)

    def test_promising_never_launch(self):
        c = candidate()
        c["source_state"] = FV.PROMISING
        r = ev(c)
        self.assertEqual(r["state"], FV.VALIDATE)
        self.assertTrue(any("validation only" in x for x in r["reasons"]["missing"]))

    def test_weak_market_holds(self):
        r = ev(candidate(state_kw={"wps": 40.0}))
        self.assertEqual(r["state"], FV.HOLD)

    def test_deterministic(self):
        self.assertEqual(ev()["state"], ev()["state"])
        self.assertEqual(json.dumps(ev(), default=str, sort_keys=True), json.dumps(ev(), default=str, sort_keys=True))


class Reports(TP.ConfigRoot):
    def test_dashboard_and_packs_from_production_run(self):
        self.promote()
        fake = TP.T.FakeProvider(consistent=True)
        self.live(fake)
        n = len(fake.submits)
        r = FV.run(root=self.root, data_root=self.data)
        self.assertEqual(len(fake.submits), n)                                     # no query by final validation
        self.assertLessEqual(len(r["results"]), 3)
        dash = Path(r["dashboard"]).read_text()
        for col in ("Product", "Launch State", "WPS", "Momentum", "BVS", "Decision Confidence", "Landed Cost",
                    "Selling Price", "Gross Margin", "Break-even CAC", "Supplier Status", "Sample Status", "Competition",
                    "Creative Opportunity", "Main Risk"):
            self.assertIn(col, dash)
        for pid, p in r["packs"].items():
            t = Path(p).read_text()
            for h in ("## Product", "## Market", "## Supplier", "## Economics", "## Competition", "## Creative plan",
                      "## Sample checklist", "## Risks", "## Final missing items"):
                self.assertIn(h, t)
            self.assertTrue((self.data / "data" / "production" / "final_validation" / "inputs" / f"{pid}.json").exists())
        for x in r["results"]:
            self.assertNotEqual(x["state"], FV.LAUNCH)                              # nothing approved manually

    def test_no_run_yet(self):
        self.promote()
        r = FV.run(root=self.root, data_root=self.data)
        self.assertEqual(r["results"], [])
        self.assertIn("no production live run", Path(r["dashboard"]).read_text())

    def test_manual_inputs_never_overwritten(self):
        d = self.data / "inputs"
        FV.manual_inputs(d, "P1")
        f = d / "P1.json"
        f.write_text(json.dumps({"sample_status": "ORDERED"}))
        inp, _ = FV.manual_inputs(d, "P1")
        self.assertEqual(inp["sample_status"], "ORDERED")
        self.assertEqual(json.loads(f.read_text()), {"sample_status": "ORDERED"})


class NoAutomatedActions(unittest.TestCase):
    def test_no_orders_no_ad_spend_no_network(self):
        src = (ROOT / "scripts" / "final_validation.py").read_text()
        tree = ast.parse(src)
        imported = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)} | \
                   {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        for banned in ("urllib", "requests", "http", "kalopilot_client", "socket"):
            self.assertFalse(any(m and m.startswith(banned) for m in imported), banned)
        calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                 for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for banned in ("submit", "place_order", "order", "create_campaign", "spend", "launch"):
            self.assertNotIn(banned, calls)
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            ev()
        self.assertTrue(all(CFG["test_controls"][k] is None for k in ("max_test_budget", "max_daily_budget",
                                                                      "max_test_days")))


if __name__ == "__main__":
    unittest.main()
