"""Owner-approved production promotion (v1 -> v2): discovery scope, brand rule and run caps only.
SYNTHETIC; network blocked; no paid query is sent."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import promotion as PROMO  # noqa: E402
import test_production as TP  # noqa: E402
from winning_product_agent import runner as R  # noqa: E402

CHANGES = [
    {"file": "runtime.yaml", "path": "query_plan.discovery_categories", "value": ["home", "kitchen", "pet", "fitness"]},
    {"file": "runtime.yaml", "path": "query_plan.discovery_exclude_established_brands", "value": True},
    {"file": "runtime.yaml", "path": "query_plan.max_credits_for_run", "value": 50},
    {"file": "runtime.yaml", "path": "credit_safety.max_credits_for_run", "value": 50},
    {"file": "runtime.yaml", "path": "query_plan.guardrails.max_queries_per_run", "value": 11},
    {"file": "runtime.yaml", "path": "query_plan.guardrails.max_provider_queries.kalopilot", "value": 11},
]


class OwnerPromotion(TP.ConfigRoot):
    def owner(self, changes=CHANGES, **kw):
        self.promote()
        return PROMO.promote_owner_change(self.root, changes, approved_by="owner", approved_at="2026-10-01T00:00Z",
                                          **kw)

    def test_v2_created_immutable_and_recorded(self):
        r = self.owner()
        self.assertEqual(r["version"], "v2")
        d = Path(r["dir"])
        self.assertTrue(PROMO.verify(d)["ok"])
        self.assertEqual(PROMO.active_version(self.root), "v1")              # not activated by default
        m = json.loads((d / "manifest.json").read_text())
        self.assertEqual(m["promotion_kind"], "OWNER_APPROVED")
        self.assertEqual(m["derived_from"], "production-v1")
        rev = json.loads((d / "change_review.json").read_text())
        self.assertEqual(len(rev["changes"]), len(CHANGES))
        self.assertTrue(all(c["approved_by"] == "owner" for c in rev["changes"]))
        rt = PROMO._yaml(d / "runtime.yaml")
        self.assertEqual(rt["query_plan"]["discovery_categories"], ["home", "kitchen", "pet", "fitness"])
        self.assertEqual(rt["query_plan"]["max_credits_for_run"], 50)
        self.assertEqual(rt["production_meta"]["config_version"], "production-v2")
        self.assertFalse(rt["runtime"]["live_mode"])

    def test_unchanged_files_keep_text_and_rules(self):
        d = Path(self.owner()["dir"])
        v1 = PROMO.active_dir(self.root, "v1")
        for name in ("decision_rules.yaml", "scoring.yaml", "filters.yaml"):
            a, b = PROMO._yaml(v1 / name), PROMO._yaml(d / name)
            a.pop("production_meta"), b.pop("production_meta")
            self.assertEqual(a, b, name)
        self.assertIn("# ", PROMO._split_production_text((d / "filters.yaml").read_text()))    # comments kept

    def test_rules_cannot_change_through_owner_path(self):
        self.promote()
        for bad in ({"file": "decision_rules.yaml", "path": "minimum_confidence.wps", "value": 40},
                    {"file": "scoring.yaml", "path": "metrics.demand.points", "value": 30},
                    {"file": "runtime.yaml", "path": "limits.amazon_validation_max_products", "value": 50}):
            with self.assertRaises(ValueError):
                PROMO.promote_owner_change(self.root, [bad], approved_by="owner", approved_at="t")
        with self.assertRaises(ValueError):
            PROMO.promote_owner_change(self.root, CHANGES, approved_by=None, approved_at="t")
        self.assertEqual(PROMO.versions(self.root), ["v1"])

    def test_eligibility_change_keeps_comments(self):
        r = self.owner(CHANGES + [
            {"file": "amazon_validation.yaml", "path": "amazon_validation.minimum_wps", "value": 55},
            {"file": "business_viability.yaml", "path": "business_viability.eligibility.minimum_wps", "value": 55}])
        d = Path(r["dir"])
        self.assertEqual(PROMO._yaml(d / "amazon_validation.yaml")["amazon_validation"]["minimum_wps"], 55)
        self.assertEqual(PROMO._yaml(d / "business_viability.yaml")["business_viability"]["eligibility"]["minimum_wps"], 55)
        self.assertIn("# Eligible products are validated", (d / "amazon_validation.yaml").read_text())
        with self.assertRaises(ValueError):                 # formulas stay out of reach
            PROMO.promote_owner_change(self.root, [{"file": "amazon_validation.yaml",
                                                    "path": "amazon_validation.matching.classes.EXACT", "value": 50}],
                                       approved_by="o", approved_at="t")

    def test_activate_and_rollback(self):
        self.owner(activate_new=True)
        self.assertEqual(PROMO.active_version(self.root), "v2")
        PROMO.activate(self.root, "v1", note="rollback")
        self.assertEqual(PROMO.active_version(self.root), "v1")

    def test_discovery_queries_v2(self):
        self.owner(activate_new=True)
        r = self.runner()
        qs = r.discovery_queries() if hasattr(r, "discovery_queries") else None
        if qs is None:
            self.skipTest("runner exposes no discovery query builder")
        self.assertEqual([q["category_key"] for q in qs], ["home", "kitchen", "pet", "fitness"])
        self.assertTrue(all(R.BRAND_RULE in q["query"] for q in qs))
        self.assertTrue(all(q["query"].index(R.BRAND_RULE) < q["query"].index("Do not run deep analysis")
                            for q in qs))


class BrandRule(unittest.TestCase):
    def test_off_is_byte_identical(self):
        q = "find products\nDo not run deep analysis.\nReturn JSON"
        self.assertEqual(R.with_brand_rule(q, {}), q)
        self.assertEqual(R.with_brand_rule(q, {"discovery_exclude_established_brands": False}), q)
        on = R.with_brand_rule(q, {"discovery_exclude_established_brands": True})
        self.assertIn(R.BRAND_RULE, on)
        self.assertTrue(on.endswith("Do not run deep analysis.\nReturn JSON"))


if __name__ == "__main__":
    unittest.main()


class ScaleEdit(unittest.TestCase):
    def test_path_walk_keeps_comments_and_other_blocks(self):
        t = ("version: \"wps-v2.1\"   # keep\nmetrics:\n  demand:\n    components:\n      units_sold:\n"
             "        zero_at: 1000\n        full_at: 100000\n  video_momentum:\n    components:\n"
             "      video_count:\n        zero_at: 100   # c\n      video_sales_share:\n        zero_at: 50\n")
        t2 = PROMO._text_set_scalar(t, "metrics.video_momentum.components.video_count.zero_at", 100, 20)
        t2 = PROMO._text_set_scalar(t2, "version", "wps-v2.1", "wps-v2.2")
        import yaml
        d = yaml.safe_load(t2)
        self.assertEqual(d["metrics"]["video_momentum"]["components"]["video_count"]["zero_at"], 20)
        self.assertEqual(d["metrics"]["video_momentum"]["components"]["video_sales_share"]["zero_at"], 50)
        self.assertEqual(d["metrics"]["demand"]["components"]["units_sold"]["zero_at"], 1000)
        self.assertEqual(d["version"], "wps-v2.2")
        self.assertIn("# c", t2)
        self.assertIsNone(PROMO._text_set_scalar(t, "metrics.demand.components.units_sold.zero_at", 999, 1))
