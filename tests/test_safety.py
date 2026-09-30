"""Tests for Step S safety: Live Query Safety Gate, query budget, redaction, logging,
manifest, config validation, pre-flight and provider-failure handling.
SYNTHETIC DATA ONLY; the network is blocked in every test (no live query can run).
Run: python3 -m unittest discover tests
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import config_validation as CV  # noqa: E402
import kalopilot_client as KC  # noqa: E402
import preflight as PF  # noqa: E402
import safety  # noqa: E402

FAKE = "tok_" + "9" * 60                            # synthetic secret


def no_network(*a, **k):
    raise AssertionError("network access attempted during a test")


class Gate(unittest.TestCase):
    def setUp(self):
        self.rt = safety.load_runtime()
        safety.clear_run_overrides()

    def tearDown(self):
        safety.clear_run_overrides()

    def test_default_config_is_safe(self):
        r = self.rt["runtime"]
        self.assertEqual((r["live_mode"], r["dry_run"], r["explicit_live_confirmation"]), (False, True, False))
        d = safety.check_live_query(self.rt, credentials_ok=True, balance=1e9)
        self.assertFalse(d.allowed)
        self.assertEqual(len(d.reasons), 3)

    def test_each_condition_blocks(self):
        full = dict(live_mode=True, dry_run=False, explicit_live_confirmation=True)
        for missing in full:
            safety.clear_run_overrides()
            safety.set_run_overrides(**{**full, missing: not full[missing]})
            d = safety.check_live_query(self.rt, credentials_ok=True, balance=100)
            self.assertFalse(d.allowed, missing)
        safety.set_run_overrides(**full)
        self.assertFalse(safety.check_live_query(self.rt, credentials_ok=False, balance=100).allowed)
        self.assertFalse(safety.check_live_query(self.rt, credentials_ok=True, balance=None).allowed)
        d = safety.check_live_query(self.rt, credentials_ok=True, balance=8.0)             # 8 - 4 < 5 reserve
        self.assertFalse(d.allowed)
        self.assertIn("insufficient credits", d.reasons[0])
        self.assertTrue(safety.check_live_query(self.rt, credentials_ok=True, balance=9.0).allowed)

    def test_overrides_are_in_memory_only(self):
        before = (ROOT / "config" / "runtime.yaml").read_text()
        safety.set_run_overrides(live_mode=True)
        self.assertEqual((ROOT / "config" / "runtime.yaml").read_text(), before)
        with self.assertRaises(ValueError):
            safety.set_run_overrides(require_credit_check=False)                      # not overridable

    def test_submit_blocked_by_default_without_network(self):
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            with self.assertRaises(safety.LiveQueryBlocked) as cm:
                KC.submit("synthetic query")
        self.assertIn("BLOCKED LIVE QUERY", str(cm.exception))

    def test_submit_blocked_when_balance_insufficient(self):
        safety.set_run_overrides(live_mode=True, dry_run=False, explicit_live_confirmation=True)
        with mock.patch.object(KC, "credits", return_value={"totalRemain": 6.0}), \
                mock.patch.object(safety, "credentials_present", return_value=True), \
                mock.patch.object(KC, "_request", side_effect=AssertionError("paid call attempted")):
            with self.assertRaises(safety.LiveQueryBlocked):
                KC.submit("synthetic query")

    def test_submit_allowed_only_when_everything_passes(self):
        safety.set_run_overrides(live_mode=True, dry_run=False, explicit_live_confirmation=True)
        with mock.patch.object(KC, "credits", return_value={"totalRemain": 50.0}), \
                mock.patch.object(safety, "credentials_present", return_value=True), \
                mock.patch.object(KC, "_request", return_value={"success": True, "data": {"task_id": "syn"}}) as req:
            self.assertEqual(KC.submit("q")["data"]["task_id"], "syn")
            req.assert_called_once()


class ProviderFailures(unittest.TestCase):
    def test_kalodata_unavailable(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")), \
                mock.patch.object(KC, "_token", return_value=FAKE):
            r = KC._request("GET", "credits")
            self.assertEqual(r["error_category"], "provider_unavailable")
            with self.assertRaises(KC.ProviderUnavailable) as cm:
                KC.credits()
        self.assertIn("unavailable", str(cm.exception))

    def test_timeout_is_labeled(self):
        with mock.patch.object(KC, "result", return_value={"success": True, "data": {"status": "running"}}), \
                mock.patch("time.sleep"):
            r = KC.wait("t", first_wait=0, interval=0, max_polls=2)
        self.assertEqual(r["error_category"], "timeout")
        self.assertFalse(r["success"])

    def test_http_error_message_redacted(self):
        err = urllib.error.HTTPError("u", 401, "x", {}, None)
        err.read = lambda: f"bad token={FAKE}".encode()
        with mock.patch("urllib.request.urlopen", side_effect=err), mock.patch.object(KC, "_token", return_value=FAKE):
            r = KC._request("GET", "credits")
        self.assertNotIn(FAKE, json.dumps(r))


class BudgetAndRedaction(unittest.TestCase):
    def test_query_budget(self):
        b = safety.QueryBudget("r1", estimated_per_query=4.0)
        b.plan(5)
        b.record("cached")
        b.record("executed", actual_cost=2.78, balance=70.0)
        b.record("blocked")
        b.record("failed")
        s = b.summary()
        self.assertEqual((s["planned_queries"], s["executed_queries"], s["cached_queries"], s["blocked_queries"],
                          s["failed_queries"]), (5, 1, 1, 1, 1))
        self.assertEqual(s["estimated_credit_cost"], 16.0)                              # 4 paid x 4.0 (configured)
        self.assertEqual(s["actual_credit_cost"], 2.78)
        self.assertEqual(s["remaining_balance"], 70.0)

    def test_unknown_costs_are_never_fabricated(self):
        b = safety.QueryBudget("r2", estimated_per_query=None)
        b.plan(2)
        b.record("executed", actual_cost=None)
        s = b.summary()
        self.assertEqual(s["estimated_credit_cost"], "UNKNOWN")
        self.assertEqual(s["actual_credit_cost"], "UNKNOWN")
        self.assertEqual(s["remaining_balance"], "UNKNOWN")

    def test_redaction(self):
        data = {"Authorization": f"Bearer {FAKE}", "api_key": FAKE, "note": f"header Bearer {FAKE} and token={FAKE}",
                "nested": [{"password": "x", "ok": f"value {FAKE}"}]}
        red = safety.redact(data, secrets=[FAKE])
        self.assertNotIn(FAKE, json.dumps(red))
        self.assertNotIn("Authorization", red)
        self.assertNotIn("api_key", red)
        self.assertEqual(red["nested"], [{"ok": "value [REDACTED]"}])

    def test_logger_and_manifest_never_contain_secrets(self):
        tmp = tempfile.mkdtemp()
        try:
            log = safety.RunLogger(tmp, "run1", secrets=[FAKE])
            log.log("deep_analysis", "error", product_id="P1", query_type="deep_product",
                    error_category="http_error", message=f"401 for Bearer {FAKE}", authorization=FAKE)
            line = json.loads(Path(tmp, "log.jsonl").read_text().splitlines()[0])
            for k in ("run_id", "stage", "product_id", "query_type", "status", "timestamp", "error_category"):
                self.assertIn(k, line)
            self.assertNotIn(FAKE, json.dumps(line))
            m = safety.write_manifest(tmp, {"run_id": "run1", "token": FAKE, "info": f"x {FAKE}"}, secrets=[FAKE])
            self.assertNotIn(FAKE, m.read_text())
            with self.assertRaises(FileExistsError):
                safety.write_manifest(tmp, {"run_id": "run1"})                          # never overwritten
        finally:
            shutil.rmtree(tmp)


class ConfigValidation(unittest.TestCase):
    def setUp(self):
        self.cfgs, errs = CV.load_all()
        self.assertEqual(errs, [])

    def mutate(self, fn):
        c = copy.deepcopy(self.cfgs)
        fn(c)
        return CV.validate(c)

    def test_current_configs_valid(self):
        self.assertEqual(CV.validate(self.cfgs), [])

    def test_totals_must_be_100(self):
        cases = {
            "WPS": lambda c: c["scoring.yaml"]["metrics"]["demand"].update(points=20),
            "AVS": lambda c: c["amazon_validation.yaml"]["amazon_validation"]["avs"]["components"]["match_confidence"].update(points=25),
            "BVS": lambda c: c["business_viability.yaml"]["business_viability"]["return_risk"].update(points=11),
            "Momentum": lambda c: c["emerging.yaml"]["emerging_detector"]["momentum_score"]["components"]["gmv_momentum"].update(points=30),
        }
        for name, fn in cases.items():
            self.assertTrue(any("total" in e for e in self.mutate(fn)), name)

    def test_negative_weight(self):
        errs = self.mutate(lambda c: c["scoring.yaml"]["metrics"]["demand"]["components"]["units_sold"].update(weight=-1))
        self.assertTrue(any("weights" in e for e in errs))

    def test_min_greater_than_max(self):
        errs = self.mutate(lambda c: c["filters.yaml"]["discovery"]["price"].update(min=200))
        self.assertTrue(any("price.min" in e for e in errs))

    def test_negative_limit_invalid_market_cache_and_ranges(self):
        self.assertTrue(self.mutate(lambda c: c["runtime.yaml"]["limits"].update(deep_analysis_max_products=-1)))
        self.assertTrue(any("market" in e for e in self.mutate(lambda c: c["runtime.yaml"]["runtime"].update(market="XX"))))
        self.assertTrue(any("cache" in e for e in
                            self.mutate(lambda c: c["amazon_validation.yaml"]["amazon_validation"].update(cache_hours=0))))
        self.assertTrue(any("0-100" in e for e in
                            self.mutate(lambda c: c["emerging.yaml"]["emerging_detector"].update(minimum_wps=140))))

    def test_limit_conflict_detected(self):
        errs = self.mutate(lambda c: c["deep_analysis.yaml"]["selection"].update(deep_analysis_max_products=50))
        self.assertTrue(any("limit conflict" in e for e in errs))

    def test_missing_config_file(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            for f in (ROOT / "config").glob("*.yaml"):
                shutil.copy(f, tmp / f.name)
            (tmp / "emerging.yaml").unlink()
            errs = CV.validate_dir(tmp)
            self.assertTrue(any("emerging.yaml: missing config file" in e for e in errs))
            res = PF.preflight(ROOT, config_dir=tmp)
            self.assertEqual(res["status"], "BLOCKED")
            self.assertEqual(res["system_status"]["Emerging Detector"]["status"], "NOT READY")
        finally:
            shutil.rmtree(tmp)


class Preflight(unittest.TestCase):
    def test_ready_for_dry_run_never_live(self):
        res = PF.preflight(ROOT)
        self.assertEqual(res["status"], "READY_FOR_DRY_RUN", res["blocking_reasons"])
        self.assertNotIn("READY_FOR_LIVE", json.dumps(res).replace("READY_FOR_LIVE is never", ""))
        self.assertEqual(set(res["system_status"]), set(PF.COMPONENTS))

    def test_blocked_when_file_flags_unsafe(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            for f in (ROOT / "config").glob("*.yaml"):
                shutil.copy(f, tmp / f.name)
            txt = (tmp / "runtime.yaml").read_text().replace("live_mode: false", "live_mode: true")
            (tmp / "runtime.yaml").write_text(txt)
            res = PF.preflight(ROOT, config_dir=tmp)
            self.assertEqual(res["status"], "BLOCKED")
            self.assertTrue(any("safe defaults" in b for b in res["blocking_reasons"]))
        finally:
            shutil.rmtree(tmp)

    def test_credentials_never_revealed(self):
        with mock.patch.dict(os.environ, {"KALOPILOT_TOKEN": FAKE}):
            res = PF.preflight(ROOT)
        self.assertNotIn(FAKE, json.dumps(res))


if __name__ == "__main__":
    unittest.main()
