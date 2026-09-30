"""Tests for Step T — Master Runner and controlled first live run.
SYNTHETIC DATA ONLY: every provider is an in-memory fake, the network is blocked,
and all data is written to a temp workspace (never to the real project data).
Run: python3 -m unittest discover tests
"""
import copy
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import safety  # noqa: E402
import test_e2e as E  # noqa: E402
from winning_product_agent import cli  # noqa: E402
from winning_product_agent import runner as R  # noqa: E402

FAKE = "tok_runner_" + "5" * 50
PROFILE = "config/runtime_first_live.yaml"
READY = {"status": "READY_FOR_DRY_RUN", "blocking_reasons": [], "warnings": []}
PHRASE = R.CONFIRMATION_PHRASE
DISC_PIDS = ["E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9"]


def no_network(*a, **k):
    raise AssertionError("network access attempted during a test")


def setUpModule():
    global _patches
    _patches = [mock.patch("urllib.request.urlopen", no_network),
                mock.patch.dict(os.environ, {"KALOPILOT_TOKEN": FAKE})]
    for p in _patches:
        p.start()


def tearDownModule():
    for p in _patches:
        p.stop()
    safety.clear_run_overrides()


class FakeProvider:
    """Answers discovery (combined), deep (batch/single) and amazon (batch/single) queries."""

    def __init__(self, balance=200.0, cost=3.0, missing=(), down=None, env=None, leak=False, consistent=False):
        self.balance, self.cost, self.missing, self.down, self.leak = balance, cost, set(missing), down, leak
        self.consistent = consistent        # GMV / units / daily series internally consistent (audit tests)
        self.submits, self.kinds = [], []
        if env:
            self.data_environment = env

    def credits(self):
        return {"totalRemain": self.balance, "monthlyRemain": self.balance, "permanentRemain": 0.0}

    def kind(self, q):
        if "emerging products in total across" in q or "find up to" in q:
            return "discovery"
        if "Search Amazon US" in q:
            return "amazon"
        return "deep"

    def submit(self, q, estimated_cost=None):
        k = self.kind(q)
        if self.down == k:
            msg = f"unreachable Bearer {FAKE}" if self.leak else "unreachable"
            return {"success": False, "error_category": "provider_unavailable", "message": msg}
        self.submits.append(q)
        self.kinds.append(k)
        return {"success": True, "data": {"task_id": f"t{len(self.submits)}"}}

    def ids(self, q):
        return re.findall(r"- product_id (\d+):", q) or re.findall(r"product ID (\d+)\)", q)

    def wait(self, task_id):
        q, k = self.submits[-1], self.kinds[-1]
        self.balance = round(self.balance - self.cost, 2)
        if k == "discovery":
            recs = []
            for pid in DISC_PIDS:
                r = {kk: v for kk, v in E.disc_record(pid).items() if kk != "_pid"}
                r["category_key"] = "home"
                if self.consistent:
                    r["units_30d"] = 3000
                recs.append(r)
            body = recs
        elif k == "deep":
            body = []
            for num in self.ids(q):
                if E.BY_NUM[num] in self.missing:
                    continue
                o = copy.deepcopy(E.DEEP[E.BY_NUM[num]])
                o["product_id"] = num
                if self.consistent:
                    o["units_30d"] = 3000
                    if o.get("daily_gmv") is not None:
                        o["daily_gmv"], o["daily_units"] = [5000] * 30, [100] * 30
                body.append(o)
            if "Name: " in q and not self.ids(q):
                pass
        else:
            body = []
            nums = self.ids(q)
            if not nums:                                        # single amazon prompt
                name = re.search(r"Name: (.+)", q).group(1).strip()
                pid = next(kk for kk, v in E.NAMES.items() if v == name)
                body = E.amazon_obj(pid)
            for num in nums:
                body.append({"product_id": num, **E.amazon_obj(E.BY_NUM[num])})
        return {"success": True, "data": {"status": "completed", "task_id": task_id, "credits_consumed": self.cost,
                                          "report": "```json\n" + json.dumps(body) + "\n```"}}


class Base(unittest.TestCase):
    def setUp(self):
        safety.clear_run_overrides()
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        safety.clear_run_overrides()
        for p in self.tmp.rglob("*"):
            try:
                p.chmod(0o755 if p.is_dir() else 0o644)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def runner(self, client=None, profile=PROFILE, max_products=5, preflight=READY, **kw):
        return R.Runner(profile_path=profile, root=ROOT, data_root=self.tmp, client=client, max_products=max_products,
                        preflight_fn=lambda: preflight, out=lambda s="": None, **kw)

    def live(self, client, confirm=PHRASE, **kw):
        r = self.runner(client, isatty=False, **kw)
        return r, r.live(confirm_value=confirm)

    def files_text(self, *subdirs):
        out = []
        for sd in subdirs:
            for p in (self.tmp / sd).rglob("*"):
                if p.is_file():
                    out.append(p.read_text(errors="ignore"))
        return "\n".join(out)


# ============================================================ dry run / default
class DryRunDefaults(Base):
    def test_default_run_command_is_dry_run(self):
        calls = []

        class Stub:
            def __init__(self, **kw):
                calls.append(("init", kw))

            def dry_run(self, check_balance=False):
                calls.append(("dry_run",))
                return {"status": "DRY_RUN_COMPLETED"}

            def live(self, **kw):
                calls.append(("live",))
                raise AssertionError("plain `run` must never go live")

        with mock.patch.object(R, "Runner", Stub), mock.patch.object(cli, "print_dry", lambda r: None):
            self.assertEqual(cli.main(["run"]), 0)
        self.assertIn(("dry_run",), calls)
        self.assertNotIn(("live",), calls)

    def test_dry_run_makes_no_paid_query_even_with_live_profile(self):
        fake = FakeProvider()
        r = self.runner(fake).dry_run(check_balance=True)
        self.assertEqual(fake.submits, [])
        self.assertEqual(r["paid_queries_executed"], 0)
        self.assertEqual(r["status"], "DRY_RUN_COMPLETED")
        self.assertFalse((self.tmp / "data").exists())                    # nothing written to data/
        m = json.loads(Path(r["manifest"]).read_text())
        self.assertEqual(m["mode"], "DRY_RUN")
        self.assertEqual(r["readiness"]["verdict"], "READY_FOR_FIRST_LIVE_RUN")

    def test_dry_run_budget_first_live(self):
        b = self.runner().dry_run()["query_budget"]
        self.assertEqual(b["stages"]["discovery"]["planned_queries"], 1)       # combined: 1 instead of 9
        self.assertEqual(b["stages"]["deep_analysis"]["expected_paid_max"], 1)  # 5 products, batch 5
        self.assertEqual(b["stages"]["amazon_validation"]["expected_paid_min"], 0)
        self.assertEqual((b["expected_paid_queries_min"], b["expected_paid_queries_max"]), (1, 3))
        self.assertEqual(b["estimated_credits_max"], 12.0)
        self.assertEqual(b["max_credits_for_run"], 25)

    def test_unknown_estimate_stays_unknown(self):
        r = self.runner()
        r.eff["query_plan"]["estimated_credits"]["deep_analysis"] = None
        b = r.query_budget(R.LIVE, R.now_utc())
        self.assertEqual(b["estimated_credits_max"], safety.UNKNOWN)
        self.assertEqual(b["stages"]["deep_analysis"]["estimated_credits_max"], safety.UNKNOWN)

    def test_canonical_profile_is_not_live_ready(self):
        r = self.runner(profile=None, max_products=None).dry_run()
        self.assertEqual(r["readiness"]["verdict"], "NOT_READY")
        self.assertEqual(r["query_budget"]["stages"]["discovery"]["planned_queries"], 9)


# ============================================================ live gate / confirmation
class LiveBlocking(Base):
    def assertBlocked(self, s, fake, text):
        self.assertEqual(s["final_status"], "BLOCKED")
        self.assertEqual(fake.submits, [])
        self.assertTrue(any(text in b for b in s["blocking_reasons"]), s["blocking_reasons"])
        self.assertFalse(safety.effective_runtime()["explicit_live_confirmation"])     # overrides cleared/not set

    def test_live_without_confirmation_blocked(self):
        fake = FakeProvider()
        _, s = self.live(fake, confirm=None)
        self.assertBlocked(s, fake, "non-interactive")

    def test_live_without_credentials_blocked(self):
        fake = FakeProvider()
        with mock.patch.object(safety, "credentials_present", lambda rt=None: False):
            _, s = self.live(fake)
        self.assertBlocked(s, fake, "credentials")

    def test_failed_preflight_blocked(self):
        fake = FakeProvider()
        bad = {"status": "BLOCKED", "blocking_reasons": ["config schema invalid"], "warnings": []}
        _, s = self.live(fake, preflight=bad)
        self.assertBlocked(s, fake, "pre-flight")

    def test_incorrect_confirmations_rejected(self):
        for typed in ("yes", "y", "continue", "ok", "confirm live run", " CONFIRM LIVE RUN", "CONFIRM LIVE RUN.",
                      "CONFIRM  LIVE RUN", ""):
            fake = FakeProvider()
            r = self.runner(fake, isatty=True, input_fn=lambda _p, t=typed: t)
            s = r.live()
            self.assertBlocked(s, fake, "confirmation rejected")
            fake2 = FakeProvider()
            _, s2 = self.live(fake2, confirm=typed)
            self.assertBlocked(s2, fake2, "confirmation rejected")

    def test_exact_typed_confirmation_runs(self):
        fake = FakeProvider()
        prompts = []
        r = self.runner(fake, isatty=True, input_fn=lambda p: prompts.append(p) or PHRASE)
        s = r.live()
        self.assertIn(s["final_status"], ("COMPLETED", "PARTIAL"))
        self.assertGreater(len(fake.submits), 0)
        self.assertEqual(len(prompts), 1)
        self.assertFalse(safety.effective_runtime()["explicit_live_confirmation"])     # cleared after the run

    def test_confirmation_screen_shows_budget(self):
        r = self.runner(FakeProvider())
        r._init_run("LIVE", R.SYNTHETIC)
        t = r.confirmation_text(r.query_budget(R.SYNTHETIC, R.now_utc()), 72.24)
        for s in ("Market:", "US", "Limits:", "discovery 20", "deep 5", "amazon 5", "Estimated provider queries",
                  "Estimated credits", PHRASE, "72.24"):
            self.assertIn(s, t)

    def test_canonical_profile_cannot_go_live(self):
        fake = FakeProvider()
        _, s = self.live(fake, profile=None, max_products=None)
        self.assertBlocked(s, fake, "live_mode is not true")

    def test_max_products_above_profile_limit_blocked(self):
        fake = FakeProvider()
        _, s = self.live(fake, max_products=6)
        self.assertBlocked(s, fake, "exceeds the profile limit")

    def test_insufficient_balance_before_start_blocked(self):
        fake = FakeProvider(balance=6.0)
        _, s = self.live(fake)
        self.assertBlocked(s, fake, "insufficient credits")

    def test_profile_rules(self):
        _, eff, errs = R.load_profile(PROFILE, ROOT)
        self.assertEqual(errs, [])
        self.assertEqual(eff["limits"], {"discovery_max_products": 20, "deep_analysis_max_products": 5,
                                         "amazon_validation_max_products": 5, "bvs_max_products": 5})
        self.assertEqual((eff["runtime"]["live_mode"], eff["runtime"]["dry_run"],
                          eff["runtime"]["explicit_live_confirmation"]), (True, False, False))
        self.assertEqual(eff["runtime"]["market"], "US")
        bad = self.tmp / "bad.yaml"
        bad.write_text("runtime: {market: UK, live_mode: true, dry_run: false, explicit_live_confirmation: true}\n"
                       "limits: {deep_analysis_max_products: 500}\nsafety: {min_balance_reserve: 0}\n")
        _, _, errs = R.load_profile(bad, ROOT)
        joined = " ".join(errs)
        for s in ("not allowed", "never be stored as true", "market", "exceeds the canonical limit"):
            self.assertIn(s, joined)

    def test_canonical_runtime_file_still_safe(self):
        rt = safety.load_runtime()
        self.assertEqual((rt["runtime"]["live_mode"], rt["runtime"]["dry_run"],
                          rt["runtime"]["explicit_live_confirmation"]), (False, True, False))


# ============================================================ full synthetic live run
class LiveRun(Base):
    def test_first_live_limits_and_max_five_deep(self):
        fake = FakeProvider()
        r, s = self.live(fake)
        self.assertEqual(s["final_status"], "COMPLETED")      # BVS incomplete is optional (stage PARTIAL)
        disc_q = [q for q, k in zip(fake.submits, fake.kinds) if k == "discovery"]
        deep_q = [q for q, k in zip(fake.submits, fake.kinds) if k == "deep"]
        self.assertEqual(len(disc_q), 1)
        self.assertIn("up to 20 emerging products in total", disc_q[0])
        self.assertEqual(len(deep_q), 1)
        self.assertEqual(len(fake.ids(deep_q[0])), 5)
        self.assertEqual(s["products"]["deep_analyzed_ok"], 5)
        self.assertLessEqual(len(fake.submits), 3)

    def test_stage_statuses_and_bvs_not_fabricated(self):
        fake = FakeProvider()
        r, s = self.live(fake)
        st = s["stages"]
        for k in ("preflight", "discovery", "filtering", "deep_analysis", "wps", "wps_confidence",
                  "amazon_validation", "historical_storage", "emerging_detector", "final_report", "run_summary"):
            self.assertEqual(st[k], "COMPLETED", (k, s["stage_details"].get(k)))
        self.assertEqual(st["bvs"], "PARTIAL")                   # no supplier data -> incomplete, not invented
        bvs = json.loads(Path(r.stages["bvs"]["saved"]).read_text())
        for x in bvs["results"]:
            self.assertIn("INSUFFICIENT_SUPPLIER_DATA", [f["flag"] for f in x["commercial_red_flags"]])
            self.assertEqual(x["economics"]["product_cost"], "N/A")          # never estimated
        self.assertTrue(set(st.values()) <= {"PENDING", "RUNNING", "COMPLETED", "PARTIAL", "FAILED", "SKIPPED", "BLOCKED"})

    def test_overall_completed_when_all_required_done(self):
        fake = FakeProvider()
        r, s = self.live(fake)
        # only optional BVS is PARTIAL -> overall must not be FAILED; required stages all COMPLETED
        self.assertTrue(all(r.stages[x]["status"] == "COMPLETED" for x in R.REQUIRED_STAGES))
        r.stop_paid = None
        self.assertEqual(r.overall_status(), "COMPLETED")

    def test_cache_prevents_duplicate_paid_queries(self):
        fake = FakeProvider()
        self.live(fake)
        first = len(fake.submits)
        fake2 = FakeProvider()
        _, s2 = self.live(fake2)
        self.assertGreater(first, 0)
        self.assertEqual(fake2.submits, [])
        self.assertEqual(s2["queries"]["executed"], 0)
        self.assertEqual(s2["queries"]["by_stage"]["discovery"]["CACHE_HIT"], 1)
        self.assertEqual(s2["queries"]["by_stage"]["deep_analysis"]["CACHE_HIT"], 5)
        self.assertGreaterEqual(s2["queries"]["by_stage"]["amazon_validation"]["CACHE_HIT"], 1)
        m = json.loads(Path(s2["outputs"]["manifest"]).read_text())
        self.assertTrue({q["action"] for q in m["query_log"]} == {"CACHE_HIT"})

    def test_insufficient_credits_stops_and_resume_does_not_rebuy(self):
        fake = FakeProvider(balance=11.0, cost=3.0)          # discovery ok (11->8), deep: 8-4 < 5 reserve
        r, s = self.live(fake)
        self.assertEqual(fake.kinds, ["discovery"])
        self.assertEqual(r.stages["deep_analysis"]["status"], "BLOCKED")
        self.assertIn("insufficient credits", r.stages["deep_analysis"]["summary"])
        self.assertEqual(s["final_status"], "PARTIAL")        # work preserved, resumable
        self.assertTrue((r.ck_dir / "discovery.json").exists())
        self.assertTrue(list((self.tmp / "data" / "processed").glob("discovery_*.json")))
        # resume after a top-up: discovery comes from the checkpoint, only deep/amazon are bought
        fake2 = FakeProvider(balance=100.0)
        r2 = self.runner(fake2, isatty=False)
        s2 = r2.live(confirm_value=PHRASE, resume_id=r.run_id)
        self.assertEqual(r2.run_id, r.run_id)
        self.assertNotIn("discovery", fake2.kinds)
        self.assertIn("deep", fake2.kinds)
        self.assertEqual(s2["products"]["deep_analyzed_ok"], 5)
        self.assertIn(s2["final_status"], ("COMPLETED", "PARTIAL"))
        self.assertEqual(r2.stages["deep_analysis"]["status"], "COMPLETED")

    def test_checkpoint_resume_keeps_done_products(self):
        fake = FakeProvider()
        r, _ = self.live(fake)
        ck = json.loads((r.ck_dir / "deep_analysis.json").read_text())
        self.assertEqual(len(ck["results"]), 5)
        for name in ("discovery.json", "deep_analysis.json", "amazon_validation.json", "history.json",
                     "emerging.json", "report.json"):
            self.assertTrue((r.ck_dir / name).exists(), name)
        # pretend deep was interrupted: mark it RUNNING, resume -> no deep query (products in checkpoint)
        m = json.loads((r.run_dir / "manifest.json").read_text())
        m["stages"]["deep_analysis"]["status"] = "RUNNING"
        (r.run_dir / "manifest.json").write_text(json.dumps(m))
        fake2 = FakeProvider()
        r2 = self.runner(fake2, isatty=False)
        r2.live(confirm_value=PHRASE, resume_id=r.run_id)
        self.assertNotIn("deep", fake2.kinds)
        self.assertNotIn("discovery", fake2.kinds)

    def test_individual_product_failure_does_not_corrupt_run(self):
        fake = FakeProvider(missing={"E2"})
        r, s = self.live(fake)
        self.assertEqual(r.stages["deep_analysis"]["status"], "PARTIAL")
        failed = r.stages["deep_analysis"]["failed"]
        self.assertEqual([f["error"] for f in failed], ["missing_from_batch_response"])
        self.assertEqual(s["products"]["deep_analyzed_ok"], 4)
        self.assertEqual(r.stages["final_report"]["status"], "COMPLETED")
        self.assertEqual(s["final_status"], "PARTIAL")
        json.loads(Path(s["outputs"]["manifest"]).read_text())            # manifest still valid JSON

    def test_provider_failure_is_safe(self):
        fake = FakeProvider(down="deep", leak=True)
        r, s = self.live(fake)
        self.assertEqual(r.stages["deep_analysis"]["status"], "FAILED")
        self.assertEqual(s["final_status"], "FAILED")
        self.assertEqual(r.stages["historical_storage"]["status"], "COMPLETED")    # discovery work preserved
        self.assertNotIn(FAKE, self.files_text("runs"))                           # failure message redacted

    def test_manifest_contents(self):
        fake = FakeProvider()
        r, s = self.live(fake)
        m = json.loads(Path(s["outputs"]["manifest"]).read_text())
        for k in ("run_id", "started_at", "completed_at", "market", "mode", "runtime_config", "config_hashes",
                  "limits", "query_budget", "provider_query_counts", "cache_hits", "stages", "final_status",
                  "query_log", "data_environment", "profile"):
            self.assertIn(k, m)
        self.assertEqual(m["mode"], "LIVE")
        self.assertEqual(m["limits"]["deep_analysis_max_products"], 5)
        self.assertIn("runtime_first_live.yaml", m["config_hashes"])
        self.assertEqual({q["action"] for q in m["query_log"]}, {"LIVE_QUERY"})
        self.assertTrue(m["completed_at"])

    def test_no_secrets_in_manifest_logs_reports(self):
        fake = FakeProvider()
        r, s = self.live(fake)
        text = self.files_text("runs", "reports", "data/processed", "data/history")
        self.assertNotIn(FAKE, text)
        for p in (self.tmp / "runs").rglob("*.json*"):
            self.assertNotRegex(p.read_text(), r"(?i)bearer\s+[A-Za-z0-9._-]{8,}")

    def test_final_summary_correct(self):
        fake = FakeProvider(cost=2.5)
        r, s = self.live(fake)
        self.assertEqual(s["queries"]["executed"], len(fake.submits))
        self.assertEqual(s["credits"]["used_reported"], round(2.5 * len(fake.submits), 2))
        self.assertEqual(s["credits"]["remaining"], fake.balance)
        self.assertEqual(s["products"]["discovered"], 9)
        self.assertEqual(s["mode"], "LIVE")
        self.assertEqual(s["market"], "US")
        for k in ("report_markdown", "report_json", "report_latest", "manifest", "log"):
            self.assertTrue(Path(s["outputs"][k]).exists(), k)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.print_summary(s)
        for t in ("RUN SUMMARY", r.run_id, "Overall status", "Credits:", "Queries:"):
            self.assertIn(t, out.getvalue())


# ============================================================ data environment
class DataEnvironment(Base):
    def test_synthetic_run_tags_every_record(self):
        fake = FakeProvider()                                    # no data_environment -> SYNTHETIC
        r, s = self.live(fake)
        self.assertEqual(s["data_environment"], "SYNTHETIC")
        proc = self.tmp / "data" / "processed"
        d = json.loads(sorted(proc.glob("discovery_*.json"))[-1].read_text())
        self.assertEqual(d["data_environment"], "SYNTHETIC")
        self.assertTrue(all(c["data_environment"] == "SYNTHETIC" for c in d["candidates"] + d["failed"]))
        deep = json.loads(sorted((proc / "deep_analysis").glob("deep_*.json"))[-1].read_text())
        self.assertTrue(all(x["data_environment"] == "SYNTHETIC" for x in deep["results"]))
        for p in (self.tmp / "data" / "history" / "products").rglob("*.json"):
            self.assertEqual(json.loads(p.read_text())["data_environment"], "SYNTHETIC")
        for p in (self.tmp / "data" / "raw").rglob("*.json"):
            self.assertEqual(json.loads(p.read_text())["data_environment"], "SYNTHETIC")
        rep = json.loads(Path(s["outputs"]["report_json"]).read_text())
        self.assertEqual(rep["report_metadata"]["data_environment"], "SYNTHETIC")
        self.assertIn("Data environment: **SYNTHETIC**", Path(s["outputs"]["report_markdown"]).read_text())

    def test_live_report_ignores_synthetic_history_and_cache(self):
        syn = FakeProvider()
        self.live(syn)                                            # SYNTHETIC data now in this workspace
        live = FakeProvider(env="LIVE")
        r, s = self.live(live)
        self.assertGreater(len(live.submits), 0)                  # synthetic cache NOT reused for a LIVE run
        self.assertEqual(s["queries"]["cached"], 0)
        view = R.EnvStore(self.tmp / "data" / "history", "LIVE")
        for ident in view.identities():
            self.assertTrue(all(o["data_environment"] == "LIVE" for o in view.observations(ident)))
        self.assertEqual(r.stages["final_report"]["status"], "COMPLETED")

    def test_synthetic_record_blocks_live_report(self):
        live = FakeProvider(env="LIVE")
        r = self.runner(live, isatty=False)
        r._init_run("LIVE", "LIVE")
        inputs = {"discovery": {"data_environment": "LIVE", "candidates": [{"key": "1", "data_environment": "LIVE"}]},
                  "deep": [{"product_id": "9", "data_environment": "SYNTHETIC"}], "amazon": [], "bvs": []}
        self.assertEqual(len(r.environment_violations(inputs)), 1)
        ctx = {"discovery": {"data_environment": "LIVE", "candidates": [], "failed": [], "summary": {}},
               "discovery_file": "x", "deep": [{"status": "ok", "product_id": "9", "data_environment": "SYNTHETIC"}],
               "deep_file": "y"}
        status, detail = r.s_report(ctx, R.now_utc())
        self.assertEqual(status, "BLOCKED")
        self.assertFalse((self.tmp / "reports").exists())

    def test_synthetic_provider_cannot_write_real_project(self):
        r = R.Runner(profile_path=PROFILE, root=ROOT, client=FakeProvider(), preflight_fn=lambda: READY,
                     isatty=False, out=lambda s="": None)
        with self.assertRaises(ValueError):
            r.live(confirm_value=PHRASE)


# ============================================================ prompts / optimization
class Prompts(unittest.TestCase):
    def keys(self, name):
        t = re.split(r"^===\s*$", (ROOT / "prompts" / name).read_text(), flags=re.M)[1]
        return set(re.findall(r'"([a-z_0-9]+)"', t))

    def test_batch_prompt_keys_match_single_prompt(self):
        self.assertEqual(self.keys("deep_analysis.md"), self.keys("deep_analysis_batch.md"))

    def test_combined_discovery_keys_match_plus_category(self):
        self.assertEqual(self.keys("discovery.md") | {"category_key"}, self.keys("discovery_combined.md"))

    def test_combined_record_category_key_used(self):
        import discovery as D
        p, err = D.normalize({"product_id": "1", "product_name": "x", "category_key": "pet"},
                             {"category_key": "combined"}, 0)
        self.assertEqual(p["category_key"], "pet")
        p, err = D.normalize({"product_id": "1", "product_name": "x", "category_key": "pet"},
                             {"category_key": "home"}, 0)
        self.assertEqual(p["category_key"], "home")                      # per-category answers keep their key


if __name__ == "__main__":
    unittest.main()


# ============================================================ Step U calibration audit (synthetic)
class CalibrationAudit(Base):
    def audit(self, fake):
        import calibration_audit as CA
        r, s = self.live(fake)
        m = json.loads((r.run_dir / "manifest.json").read_text())
        m["mode"] = "LIVE"
        return CA, r, CA.audit(r.run_id, data_root=self.tmp, secrets=[FAKE])

    def test_audit_clean_run_maps_fields_and_reproduces_wps(self):
        CA, r, a = self.audit(FakeProvider(consistent=True))
        self.assertEqual(a["mapping_issues"], [])
        self.assertEqual(a["secret_leaks"], [])
        self.assertEqual(a["limit_issues"], [])
        self.assertEqual(a["raw_problems"], [])
        self.assertTrue(a["reports_generated"])
        for p in a["products"]:
            self.assertEqual(p["wps_mismatch"], [])
            self.assertTrue(all(row["status"] == "OK" for row in p["mapping"]))
        # SYNTHETIC run -> contamination listed -> never validated as a LIVE run
        self.assertEqual(a["decision"], "CALIBRATION_REQUIRED")
        self.assertTrue(any("non-LIVE" in x for x in a["decision_reasons"]))
        self.assertTrue(Path(a["paths"]["markdown"]).exists())
        self.assertEqual(a["credit_audit"]["executed"], a["credit_audit"]["planned"] - a["credit_audit"]["cached"])

    def test_audit_live_env_run_validates(self):
        CA, r, a = self.audit(FakeProvider(env="LIVE", consistent=True))
        self.assertEqual(a["contamination"], [])
        self.assertEqual(a["anomalies"], [])
        self.assertEqual(a["decision"], "LIVE_RUN_VALIDATED", a["decision_reasons"])

    def test_inconsistent_provider_data_flagged(self):
        CA, r, a = self.audit(FakeProvider(env="LIVE"))          # GMV 150000 / 40000 units = 3.75 USD at price 50
        self.assertTrue(any("GMV/units" in i for i in a["mapping_issues"]))
        self.assertEqual(a["decision"], "CALIBRATION_REQUIRED")

    def test_mapping_problem_flagged(self):
        import calibration_audit as CA
        rec = {"original_record": {"gmv_30d": 100000, "units_30d": 10, "price_min": 20, "price_max": 30},
               "gmv": 100000, "units": 10, "price": {"min": 20, "max": 30}}
        rows, issues = CA.mapping_checks(rec, None)
        self.assertTrue(any("GMV/units" in i for i in issues))
        rec["gmv"] = 5
        rows, issues = CA.mapping_checks(rec, None)
        self.assertTrue(any(r["status"] == CA.MAP for r in rows))

    def test_sanity_detects_anomalies(self):
        import calibration_audit as CA
        rec = {"growth": {"growth_30d_pct": -20}, "wps_breakdown": {"growth_momentum": {"points": 20, "max": 25},
                                                                   "creator_momentum": {"points": 5, "max": 15}},
               "creator_metrics": {"total": None}, "red_flags": [],
               "concentration_metrics": {"flags": {"CREATOR_DEPENDENCY": {"status": "CLEAR", "value": 80,
                                                                          "condition": "> 70"}}}}
        out = CA.sanity(rec, {"status": "ok", "amazon_match_status": "NO_RELIABLE_MATCH", "amazon_confidence": 70},
                        {"economics": {"product_cost": "N/A"}, "bvs_confidence": 75},
                        {"emerging_status": "EMERGING", "observation_count": 1}, None)
        self.assertEqual(len(out), 6)
