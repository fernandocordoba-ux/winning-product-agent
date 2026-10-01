"""Master Runner (Step T): the ONE canonical way to run the whole pipeline.

    python -m winning_product_agent preflight
    python -m winning_product_agent run --dry-run [--profile P] [--check-balance]
    python -m winning_product_agent run --live --profile config/runtime_first_live.yaml --max-products 5
    python -m winning_product_agent status [RUN_ID]
    python -m winning_product_agent report [RUN_ID]

Stage order:
  PRE-FLIGHT -> DISCOVERY -> FILTERING -> DEEP ANALYSIS -> WPS -> WPS CONFIDENCE ->
  AMAZON VALIDATION (eligible only) -> BVS -> HISTORICAL STORAGE -> EMERGING DETECTOR ->
  FINAL REPORT -> RUN SUMMARY

Safety (never silently downgraded — anything missing BLOCKS the run):
  * default is always DRY RUN; plain `run` never makes a paid query
  * live needs: profile live_mode true + dry_run false, exact typed confirmation
    "CONFIRM LIVE RUN" (non-interactive: --confirm-live "CONFIRM LIVE RUN"), credentials,
    pre-flight READY, valid limits, credit check. The confirmation is held IN MEMORY
    (safety.set_run_overrides) for this process only.
  * every paid query: cache check first -> run credit cap -> Live Query Safety Gate -> submit
  * checkpoints after every stage in runs/{run_id}/checkpoints/ ; --resume never re-buys
  * every stored record carries data_environment LIVE | SYNTHETIC; a report never mixes them

"Claude interprets; the code calculates": all scores come from the existing stage modules.
"""
import copy
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parent
ROOT = PKG.parent
sys.path.insert(0, str(ROOT / "scripts"))

import amazon_validation as AV  # noqa: E402
import business_viability as B  # noqa: E402
import deep_analysis as DA  # noqa: E402
import discovery as D  # noqa: E402
import emerging as EM  # noqa: E402
import generate_report as GR  # noqa: E402
import history as HIST  # noqa: E402
import safety  # noqa: E402
from score_products import load_scoring_config  # noqa: E402

CONFIRMATION_PHRASE = "CONFIRM LIVE RUN"
LIVE, SYNTHETIC = "LIVE", "SYNTHETIC"
PENDING, RUNNING, COMPLETED, PARTIAL, FAILED, SKIPPED, BLOCKED = (
    "PENDING", "RUNNING", "COMPLETED", "PARTIAL", "FAILED", "SKIPPED", "BLOCKED")
STAGES = ["preflight", "discovery", "filtering", "deep_analysis", "wps", "wps_confidence", "amazon_validation",
          "bvs", "historical_storage", "emerging_detector", "final_report", "run_summary"]
AA_STAGES = ["preflight", "discovery", "filtering", "deep_analysis", "wps", "wps_confidence", "amazon_validation",
             "supplier_research", "bvs", "competitor_intelligence", "creative_intelligence", "historical_storage",
             "emerging_detector", "final_report", "final_decision", "aa_validation", "run_summary"]
AA_PHRASE = "CONFIRM AA LIVE RUN"
PRODUCTION_PHRASE = "CONFIRM PRODUCTION LIVE RUN"
E2E_KEYS = ("supplier_products_max", "supplier_offers_per_product_max", "competitor_products_max",
            "competitors_per_product_max", "creative_products_max", "creatives_per_product_max",
            "final_decision_max_products")
REQUIRED_STAGES = {"preflight", "discovery", "filtering", "deep_analysis", "wps", "wps_confidence",
                   "historical_storage", "final_report"}
CHECKPOINTS = {"discovery": "discovery.json", "deep_analysis": "deep_analysis.json",
               "amazon_validation": "amazon_validation.json", "bvs": "bvs.json", "historical_storage": "history.json",
               "emerging_detector": "emerging.json", "final_report": "report.json",
               "supplier_research": "suppliers.json", "competitor_intelligence": "competitors.json",
               "creative_intelligence": "creatives.json", "final_decision": "decision.json",
               "aa_validation": "aa_validation.json"}
PAID_STAGES = ("discovery", "deep_analysis", "amazon_validation")
PROFILE_KEYS = {"profile", "runtime", "limits", "query_plan", "e2e"}


# ====================================================================== small helpers
def sha(text):
    return hashlib.sha256((text or "").encode()).hexdigest()


def now_utc():
    return datetime.now(timezone.utc)


def write_json_atomic(path, data, secrets=None):
    """Run-state files (manifest, checkpoints) are replaced atomically; always redacted."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(safety.redact(data, secrets), ensure_ascii=False, indent=2, default=str))
    os.replace(tmp, path)
    return path


def write_new_json(path_stub, data):
    """Stored results: new file, never overwritten ('x')."""
    path_stub = Path(path_stub)
    path_stub.parent.mkdir(parents=True, exist_ok=True)
    path, n = path_stub, 1
    while path.exists():
        n += 1
        path = path_stub.with_name(f"{path_stub.stem}_{n}{path_stub.suffix}")
    with open(path, "x") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
    return path


def response_error(resp):
    """None if the provider answer is usable, else an error category."""
    if not isinstance(resp, dict) or not resp:
        return "empty_response"
    if resp.get("success") is False:
        return resp.get("error_category") or "provider_error"
    data = resp.get("data") or {}
    status = data.get("status")
    if status == "completed" and "text" in data and not data.get("text") and not data.get("report"):
        out_tok = (data.get("token_usage") or {}).get("output_tokens")
        if isinstance(out_tok, int) and out_tok >= 7900:
            return "empty_answer_output_token_limit"      # Step U: answer cut at ~8k output tokens
        if str(data.get("message_id")) != "0":
            return "empty_answer"
    if status == "completed" and str(data.get("message_id")) == "0":
        # Step U finding: KaloPilot pauses on a plan restriction ("not included in your current plan")
        # and waits for a click in the web UI; the API then says "completed" with no final message.
        return "paused_by_provider_plan_restriction"
    return None if status == "completed" else f"task_status_{status}"


def credits_consumed(resp):
    v = ((resp or {}).get("data") or {}).get("credits_consumed") if isinstance(resp, dict) else None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def raw_env_ok(envelope, env):
    """Cache reuse only within the same data environment. Raw files saved before Step T carry no tag;
    all of them are real KaloPilot answers, so they count as LIVE (never as SYNTHETIC)."""
    tag = envelope.get("data_environment")
    return tag == env or (tag is None and env == LIVE)


def _read_json(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def _add(a, b):
    return safety.UNKNOWN if safety.UNKNOWN in (a, b) else round(a + b, 2)


class StopPaid(Exception):
    """No more paid queries in this run (credits, cap, gate, credentials)."""

    def __init__(self, category, message):
        super().__init__(message)
        self.category, self.message = category, message


class ProviderFailure(Exception):
    """Provider-wide failure: the current stage stops."""

    def __init__(self, category, message):
        super().__init__(message)
        self.category, self.message = category, message


class EnvStore(HIST.HistoryStore):
    """History view limited to one data environment (a LIVE report never sees SYNTHETIC rows)."""

    def __init__(self, base_dir, env, cfg=None):
        super().__init__(base_dir, cfg)
        self.env = env

    def observations(self, identity):
        """Same environment only; one provider snapshot counts once (first kept, by source_snapshot_hash).
        Step U: one Discovery answer imported twice had been stored at two fetch times (append-only kept)."""
        out, seen = [], set()
        for o in super().observations(identity):
            if o.get("data_environment") != self.env:
                continue
            h = o.get("source_snapshot_hash") or HIST.snapshot_hash(o)
            if h in seen:
                continue
            seen.add(h)
            out.append(o)
        return out

    def duplicate_snapshots(self, identity):
        """Stored observations hidden by the snapshot dedupe (for audits)."""
        allobs = [o for o in HIST.HistoryStore.observations(self, identity) if o.get("data_environment") == self.env]
        return len(allobs) - len(self.observations(identity))

    def identities(self):
        return [i for i in super().identities() if self.observations(i)]


class KaloClient:
    """Real provider. submit() goes through kalopilot_client.submit() (Live Query Safety Gate inside)."""
    data_environment = LIVE

    def __init__(self):
        import kalopilot_client as KC
        self.KC = KC

    def credits(self):
        return self.KC.credits()

    def submit(self, query, estimated_cost=None, task_id=None):
        return self.KC.submit(query, task_id=task_id, estimated_cost=estimated_cost)

    def wait(self, task_id):
        return self.KC.wait(task_id)

    def result(self, task_id):
        """FREE: current state of an existing task (no new query)."""
        return self.KC.result(task_id)


# ====================================================================== profile
def load_profile(profile_path=None, root=ROOT):
    """(canonical runtime.yaml, effective config, profile errors). Profiles override only
    runtime / limits / query_plan; safety, provider and paths always come from runtime.yaml."""
    root = Path(root)
    rt = safety.load_runtime(root / "config" / "runtime.yaml")
    eff, errors = copy.deepcopy(rt), []
    eff.setdefault("query_plan", {})
    eff["profile_name"], eff["profile_path"] = "canonical", "config/runtime.yaml"
    if profile_path:
        p = Path(profile_path)
        p = p if p.is_absolute() else root / p
        try:
            prof = yaml.safe_load(p.read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            return rt, eff, [f"profile unreadable: {e.__class__.__name__}"]
        extra = set(prof) - PROFILE_KEYS
        if extra:
            errors.append(f"profile may only set {sorted(PROFILE_KEYS)}; not allowed: {sorted(extra)}")
        if (prof.get("runtime") or {}).get("explicit_live_confirmation") is True:
            errors.append("explicit_live_confirmation must never be stored as true (it is typed per run)")
        for k in ("runtime", "limits"):
            eff[k] = {**rt.get(k, {}), **(prof.get(k) or {})}
        qp = copy.deepcopy(rt.get("query_plan") or {})
        pq = prof.get("query_plan") or {}
        qp.update({k: v for k, v in pq.items() if k != "estimated_credits"})
        qp["estimated_credits"] = {**(qp.get("estimated_credits") or {}), **(pq.get("estimated_credits") or {})}
        eff["query_plan"] = qp
        eff["profile_name"] = prof.get("profile") or p.stem
        if prof.get("e2e") is not None:
            eff["e2e"] = prof["e2e"]
        try:
            eff["profile_path"] = str(p.relative_to(root))
        except ValueError:
            eff["profile_path"] = str(p)
        eff["profile_hash"] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    errors += validate_effective(rt, eff)
    return rt, eff, errors


def validate_effective(rt, eff):
    e = []
    r, lim, qp = eff.get("runtime") or {}, eff.get("limits") or {}, eff.get("query_plan") or {}
    if r.get("market") not in (rt.get("allowed_markets") or []):
        e.append(f"market {r.get('market')!r} not allowed {rt.get('allowed_markets')}")
    for k in ("live_mode", "dry_run", "explicit_live_confirmation"):
        if not isinstance(r.get(k), bool):
            e.append(f"runtime.{k} must be true/false")
    canon = rt.get("limits") or {}
    for k in ("discovery_max_products", "deep_analysis_max_products", "amazon_validation_max_products",
              "bvs_max_products"):
        v = lim.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            e.append(f"limits.{k} must be a positive integer (got {v!r})")
        elif isinstance(canon.get(k), int) and v > canon[k]:
            e.append(f"limits.{k}={v} exceeds the canonical limit {canon[k]} in runtime.yaml")
    if isinstance(lim.get("deep_analysis_max_products"), int) and isinstance(lim.get("discovery_max_products"), int) \
            and lim["deep_analysis_max_products"] > lim["discovery_max_products"]:
        e.append("limits.deep_analysis_max_products > discovery_max_products")
    if qp.get("discovery_mode") == "lenses" and not set(qp.get("discovery_lenses") or []) <= {"proven", "rising", "creators"}:
        e.append(f"query_plan.discovery_lenses must be a subset of proven/rising/creators (got {qp.get('discovery_lenses')!r})")
    if qp.get("discovery_mode", "per_category") not in ("per_category", "combined", "lenses"):
        e.append(f"query_plan.discovery_mode must be per_category or combined (got {qp.get('discovery_mode')!r})")
    for k in ("deep_batch_size", "amazon_batch_size"):
        v = qp.get(k, 1)
        if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 10:
            e.append(f"query_plan.{k} must be an integer 1..10 (got {v!r})")
    only = qp.get("discovery_categories")
    if only is not None:
        known = {c["key"] for c in (D.load_yaml("categories.yaml").get("categories") or []) if c.get("enabled", True)}
        if not isinstance(only, list) or not only or not set(only) <= known:
            e.append(f"query_plan.discovery_categories must be a non-empty list of enabled category keys "
                     f"(unknown: {sorted(set(only or []) - known) if isinstance(only, list) else only})")
    e2e = eff.get("e2e")
    if e2e is not None:
        if not isinstance(e2e, dict):
            e.append("e2e must be a mapping")
        else:
            for k in E2E_KEYS:
                v = e2e.get(k)
                if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                    e.append(f"e2e.{k} must be a positive integer (got {v!r})")
            if e2e.get("confirmation_phrase") not in (AA_PHRASE, PRODUCTION_PHRASE):
                e.append(f"e2e.confirmation_phrase must be exactly {AA_PHRASE!r} or {PRODUCTION_PHRASE!r}")
            if isinstance(e2e.get("final_decision_max_products"), int) and \
                    isinstance(lim.get("deep_analysis_max_products"), int) and \
                    e2e["final_decision_max_products"] < lim["deep_analysis_max_products"]:
                e.append("e2e.final_decision_max_products must be >= deep_analysis_max_products")
    cap = qp.get("max_credits_for_run")
    if cap is not None and (not isinstance(cap, (int, float)) or cap <= 0):
        e.append("query_plan.max_credits_for_run must be > 0 or null")
    for k, v in (qp.get("estimated_credits") or {}).items():
        if v is not None and (not isinstance(v, (int, float)) or v < 0):
            e.append(f"query_plan.estimated_credits.{k} must be >= 0 or null")
    return e


# ====================================================================== runner


def amazon_rule():
    """Amazon eligibility text from the ACTIVE config (never hard-coded)."""
    try:
        c = AV.load_cfg()
        return f"WPS>={c['minimum_wps']:g}, Conf>={c['minimum_confidence']:g}"
    except Exception:  # noqa: BLE001
        return "see amazon_validation.yaml"

BRAND_RULE = ("- Exclude products sold by well-known established brands or their official brand stores (national "
              "cosmetics, electronics, appliance or apparel brands). Prefer generic / unbranded products that "
              "independent sellers can source from dropshipping suppliers.")


def with_brand_rule(query, qp):
    """Owner-approved discovery rule (production-v2+): appended only when the config enables it, so earlier
    versions send byte-identical queries."""
    if not (qp or {}).get("discovery_exclude_established_brands"):
        return query
    marker = "\nDo not run deep analysis"
    k = query.find(marker)
    if k < 0:
        return query.rstrip() + "\n" + BRAND_RULE
    return query[:k].rstrip("\n") + "\n" + BRAND_RULE + "\n" + query[k:]


class Runner:
    def __init__(self, profile_path=None, root=ROOT, data_root=None, client=None, max_products=None,
                 preflight_fn=None, input_fn=None, isatty=None, out=None, now=None):
        self.root = Path(root)
        self.data_root = Path(data_root or root)
        self.rt, self.eff, self.profile_errors = load_profile(profile_path, self.root)
        self.client = client
        self.max_products_arg = max_products
        self.continue_task = None                  # Step U: follow-up on an unfinished discovery task
        self.from_task = None                      # Step U: import a task finished in the web UI (FREE fetch)
        self.preflight_fn = preflight_fn
        self.input_fn = input_fn or input
        self.isatty = sys.stdin.isatty() if isatty is None else isatty
        self.out = out or (lambda s="": print(s))
        self.now = now
        p = self.rt["paths"]
        self.raw = self.data_root / p["raw"]
        self.processed = self.data_root / p["processed"]
        self.history_dir = self.data_root / p["history"]
        self.reports_dir = self.data_root / p["reports"]
        self.runs_dir = self.data_root / p["runs"]
        self.cfg_deep = DA.load_cfg()
        self.filters = D.load_yaml("filters.yaml")
        self.categories = D.load_yaml("categories.yaml")
        self.ttl_hours = self.cfg_deep["credit_protection"]["cache_ttl_hours"]
        import config_resolver as _CR
        self.cal_cfg = (yaml.safe_load(Path(_CR.path("calibration_lane.yaml")).read_text()) or {}).get(
            "calibration") or {"enabled": False}

    # ------------------------------------------------------------------ e2e (Step AA)
    @property
    def e2e(self):
        return self.eff.get("e2e")

    @property
    def stage_list(self):
        return AA_STAGES if self.e2e else STAGES

    @property
    def phrase(self):
        return AA_PHRASE if self.e2e else CONFIRMATION_PHRASE

    # ------------------------------------------------------------------ limits / plan
    @property
    def limits(self):
        lim = dict(self.eff["limits"])
        if self.max_products_arg is not None:
            lim["deep_analysis_max_products"] = min(lim["deep_analysis_max_products"], self.max_products_arg)
            lim["amazon_validation_max_products"] = min(lim["amazon_validation_max_products"],
                                                        lim["deep_analysis_max_products"])
            lim["bvs_max_products"] = min(lim["bvs_max_products"], lim["deep_analysis_max_products"])
        return lim

    def limit_errors(self):
        e = list(self.profile_errors)
        m = self.max_products_arg
        if m is not None:
            if not isinstance(m, int) or m <= 0:
                e.append(f"--max-products must be a positive integer (got {m!r})")
            elif m > self.eff["limits"]["deep_analysis_max_products"]:
                e.append(f"--max-products {m} exceeds the profile limit "
                         f"deep_analysis_max_products={self.eff['limits']['deep_analysis_max_products']}")
        return e

    def est(self, stage):
        v = (self.eff["query_plan"].get("estimated_credits") or {}).get(stage)
        return float(v) if isinstance(v, (int, float)) else None

    def followup_source(self, task_id):
        """The saved raw answer of an unfinished discovery task (must exist locally, LIVE, same category set)."""
        for p in sorted(self.raw.glob(f"*_{task_id}*.json")):
            e = _read_json(p) or {}
            if e.get("task_id") == task_id and e.get("category_key") and raw_env_ok(e, LIVE):
                return p, e
        return None, None

    def followup_query(self, source_env):
        import re
        tpl = re.split(r"^===\s*$", (ROOT / "prompts" / "discovery_followup.md").read_text(), flags=re.M)[1].strip()
        m = self.filters["market"]
        return tpl.format(currency=m["currency"], limit=self.filters["discovery_mode"]["products_per_category"])

    def discovery_queries(self):
        qp, lim = self.eff["query_plan"], self.limits
        if self.from_task:
            p, e = self.followup_source(self.from_task)
            if not p:
                raise ValueError(f"no saved LIVE discovery answer for task {self.from_task}")
            return [{"category_key": e["category_key"], "query": e.get("query") or "", "fetch_task": self.from_task,
                     "source_raw": str(p)}]
        if self.continue_task:
            p, e = self.followup_source(self.continue_task)
            if not p:
                raise ValueError(f"no saved LIVE discovery answer for task {self.continue_task}")
            return [{"category_key": e["category_key"], "query": self.followup_query(e),
                     "followup_task": self.continue_task, "followup_raw": str(p)}]
        only = qp.get("discovery_categories")                     # optional subset of category keys
        if qp.get("discovery_mode") == "lenses":                  # Step AE: proven / rising / creators prompts
            import lens_experiment as LX
            cats = only or [c["key"] for c in self.categories["categories"] if c.get("enabled", True)]
            n = self.filters["discovery_mode"]["products_per_category"]
            return [{"category_key": q["category_key"], "query": q["query"], "lens": q["lens"] + (
                        f":{q['subniche']}" if q.get("subniche") else "")}
                    for q in LX.build_queries(cats, tuple(qp.get("discovery_lenses") or LX.LENSES), limit=n,
                                              subniches=qp.get("discovery_subniches"))]
        if qp.get("discovery_mode", "per_category") == "per_category":
            qs = D.build_queries(self.filters, self.categories)
            return [{**q, "query": with_brand_rule(q["query"], qp)} for q in qs
                    if not only or q["category_key"] in only]
        cats = [c for c in self.categories["categories"] if c.get("enabled", True)
                and (not only or c["key"] in only)]
        text = (ROOT / "prompts" / "discovery_combined.md").read_text()
        import re
        template = re.split(r"^===\s*$", text, flags=re.M)[1].strip()
        d, m = self.filters["discovery"], self.filters["market"]
        limit = lim["discovery_max_products"]
        lines = "\n".join(f"- {c['key']}: {c['name']} — {', '.join(c.get('kalodata_match') or [c['name']])}"
                          for c in cats)
        q = template.format(region=m["region"], currency=m["currency"], period_days=m["period_days"], limit=limit,
                            category_lines=lines, per_category_max=max(1, math.ceil(limit / max(1, len(cats))) + 1),
                            gmv_min=d["gmv_30d"]["min"], units_min=d["units_30d"]["min"],
                            price_min=d["price"]["min"], price_max=d["price"]["max"])
        return [{"category_key": "combined", "query": with_brand_rule(q, qp), "categories": [c["key"] for c in cats]}]

    def find_cached_discovery(self, query, env, now):
        best = None
        for p in sorted(self.raw.glob("*.json")):
            e = _read_json(p)
            if not isinstance(e, dict) or sha(e.get("query")) != sha(query) or not raw_env_ok(e, env):
                continue
            if response_error(e.get("response")):
                continue
            try:
                ts = datetime.strptime(e.get("fetched_at", ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if now - ts <= timedelta(hours=self.ttl_hours):
                best = p
        return best

    @staticmethod
    def prompt_version(query_type):
        """sha of the prompt files a cached answer must have been asked with (Step U: a changed
        question -> old answers are NOT reused, e.g. new gmv_prev_30d / category level fields)."""
        files = {"deep_product": ["deep_analysis.md", "deep_analysis_batch.md"]}.get(
            query_type, ["amazon_validation.md", "amazon_validation_batch.md"])
        return sha("".join((ROOT / "prompts" / f).read_text() for f in files))[:16]

    def find_cached_product(self, pid, query_type, ttl, now, raw_dir, env):
        p = DA.find_cached(pid, query_type, ttl, now, raw_dir)
        if not p:
            return None
        e = _read_json(p) or {}
        if raw_env_ok(e, env) and e.get("prompt_version") == self.prompt_version(query_type):
            return p
        return None

    def query_budget(self, env, now):
        """Planned paid queries BEFORE execution. Unknown costs stay UNKNOWN."""
        qp, lim = self.eff["query_plan"], self.limits
        dq = self.discovery_queries()
        cached = [q["category_key"] for q in dq if q.get("fetch_task") or self.find_cached_discovery(q["query"], env, now)]
        deep_n = min(lim["deep_analysis_max_products"], lim["discovery_max_products"])
        deep_q = math.ceil(deep_n / qp.get("deep_batch_size", 1))
        amz_n = min(lim["amazon_validation_max_products"], deep_n)
        amz_q = math.ceil(amz_n / qp.get("amazon_batch_size", 1))
        rows = {
            "discovery": {"planned_queries": len(dq), "cached": len(cached), "expected_paid_min": len(dq) - len(cached),
                          "expected_paid_max": len(dq) - len(cached), "mode": qp.get("discovery_mode", "per_category"),
                          "max_products": lim["discovery_max_products"]},
            "deep_analysis": {"planned_queries": deep_q, "cached": "checked per product at run time",
                              "expected_paid_min": 0, "expected_paid_max": deep_q, "max_products": deep_n,
                              "batch_size": qp.get("deep_batch_size", 1),
                              "note": "0 only if no PASS/REVIEW candidate or all cached (< 24 h)"},
            "amazon_validation": {"planned_queries": amz_q, "cached": "checked per product at run time",
                                  "expected_paid_min": 0, "expected_paid_max": amz_q, "max_products": amz_n,
                                  "batch_size": qp.get("amazon_batch_size", 1),
                                  "note": f"only products with {amazon_rule()} (may be 0)"},
        }
        tot_min = tot_max = 0.0
        unknown = False
        for stage, r in rows.items():
            e = self.est(stage)
            r["estimate_per_query"] = e if e is not None else safety.UNKNOWN
            if e is None:
                unknown = unknown or r["expected_paid_max"] > 0
                r["estimated_credits_min"] = r["estimated_credits_max"] = safety.UNKNOWN
            else:
                r["estimated_credits_min"] = round(r["expected_paid_min"] * e, 2)
                r["estimated_credits_max"] = round(r["expected_paid_max"] * e, 2)
                tot_min += r["estimated_credits_min"]
                tot_max += r["estimated_credits_max"]
        providers = self.provider_budget(rows) if self.e2e else None
        return {"stages": rows, **({"providers": providers} if providers else {}),
                "planned_queries_max": sum(r["planned_queries"] for r in rows.values()),
                "cached_queries": len(cached),
                "expected_paid_queries_min": sum(r["expected_paid_min"] for r in rows.values()),
                "expected_paid_queries_max": sum(r["expected_paid_max"] for r in rows.values()),
                "estimated_credits_min": safety.UNKNOWN if unknown else round(tot_min, 2),
                "estimated_credits_max": safety.UNKNOWN if unknown else round(tot_max, 2),
                "estimate_basis": "configured estimate per paid query (query_plan.estimated_credits) — NOT a provider "
                                  "quote; batched queries have no observed cost yet",
                "max_credits_for_run": qp.get("max_credits_for_run"),
                "min_balance_reserve": self.rt["safety"]["min_balance_reserve"]}

    def provider_budget(self, rows):
        """Step AA: planned queries per PROVIDER. Only KaloPilot is paid; other layers use configured sources."""
        import yaml as _y

        def implemented(name):
            import config_resolver as _CR
            c = _y.safe_load(Path(_CR.path(name)).read_text()) or {}
            return sorted(k for k, v in (c.get("providers") or {}).items() if isinstance(v, dict) and v.get("implemented"))
        e, dq, am = self.e2e, rows["discovery"], rows["amazon_validation"]
        dp = rows["deep_analysis"]
        kp_min = dq["expected_paid_min"] + dp["expected_paid_min"]
        kp_max = dq["expected_paid_max"] + dp["expected_paid_max"]
        return {
            "KaloPilot (TikTok Shop: discovery + deep analysis)": {
                "planned_queries": dq["planned_queries"] + dp["planned_queries"], "cache_hits_known": dq["cached"],
                "expected_paid_min": kp_min, "expected_paid_max": kp_max,
                "estimated_credits_min": _add(dq["estimated_credits_min"], dp["estimated_credits_min"]),
                "estimated_credits_max": _add(dq["estimated_credits_max"], dp["estimated_credits_max"]),
                "note": "deep cache (< 24 h, same prompt) checked per product at run time"},
            "Amazon (via KaloPilot Amazon validation)": {
                "planned_queries": am["planned_queries"], "cache_hits_known": 0,
                "expected_paid_min": am["expected_paid_min"], "expected_paid_max": am["expected_paid_max"],
                "estimated_credits_min": am["estimated_credits_min"], "estimated_credits_max": am["estimated_credits_max"],
                "note": am.get("note")},
            "Suppliers": {"planned_queries": 0, "expected_paid_min": 0, "expected_paid_max": 0,
                          "estimated_credits_min": 0.0, "estimated_credits_max": 0.0,
                          "sources": implemented("suppliers.yaml"),
                          "limits": f"{e['supplier_products_max']} products x {e['supplier_offers_per_product_max']} offers",
                          "note": "no automated supplier API configured: only offers already imported are used "
                                  "(no order, no supplier contact)"},
            "Competitors": {"planned_queries": 0, "expected_paid_min": 0, "expected_paid_max": 0,
                            "estimated_credits_min": 0.0, "estimated_credits_max": 0.0,
                            "sources": implemented("competitors.yaml"),
                            "limits": f"{e['competitor_products_max']} products x {e['competitors_per_product_max']}",
                            "note": "no automated competitor API configured: only research already imported is used"},
            "Creatives": {"planned_queries": 0, "expected_paid_min": 0, "expected_paid_max": 0,
                          "estimated_credits_min": 0.0, "estimated_credits_max": 0.0,
                          "sources": implemented("creatives.yaml"),
                          "limits": f"{e['creative_products_max']} products x {e['creatives_per_product_max']}",
                          "note": "saved KaloPilot top_videos of the deep answers (free, no new query) + manual imports"},
        }

    # ------------------------------------------------------------------ run state
    def _init_run(self, mode, env, resume_id=None):
        self.env, self.mode = env, mode
        self.run_id = resume_id or safety.new_run_id(self.now or now_utc())
        self.run_dir = self.runs_dir / self.run_id
        self.ck_dir = self.run_dir / "checkpoints"
        self.secrets = safety.known_secret_values(self.rt)
        self.log = safety.RunLogger(self.run_dir, self.run_id, self.secrets)
        self.budget = safety.QueryBudget(self.run_id, None)
        self.query_log, self.stop_paid, self.provider_down = [], None, None
        self.balance_start = self.balance_now = self.balance_end = None
        self.spent_known = 0.0
        self.counts = {s: {"LIVE_QUERY": 0, "CACHE_HIT": 0, "FETCHED_RESULT": 0, "BLOCKED": 0, "FAILED": 0}
                       for s in PAID_STAGES}
        old = _read_json(self.run_dir / "manifest.json") if resume_id else None
        self.manifest = old or {
            "run_id": self.run_id, "started_at": (self.now or now_utc()).isoformat(), "completed_at": None,
            "market": self.eff["runtime"]["market"], "mode": mode, "data_environment": env,
            "profile": {"name": self.eff["profile_name"], "path": self.eff["profile_path"],
                        "hash": self.eff.get("profile_hash")},
            "runtime_config": {"runtime": {**self.eff["runtime"]}, "query_plan": self.eff["query_plan"]},
            "config_hashes": safety.config_hashes(self.root / "config"),
            "limits": self.limits, "max_products_arg": self.max_products_arg,
            "calibration_lane": bool((self.cal_cfg or {}).get("enabled")),
            "stages": {s: {"status": PENDING} for s in self.stage_list}, "final_status": PENDING,
            **({"e2e": self.e2e} if self.e2e else {})}
        if resume_id:
            self.manifest.setdefault("resumed_at", []).append((self.now or now_utc()).isoformat())
            for s in PAID_STAGES:
                self.counts[s] = self.manifest.get("provider_query_counts", {}).get(s, self.counts[s])
            self.query_log = self.manifest.get("query_log", [])
        self.stages = self.manifest["stages"]

    def _save_manifest(self):
        self.manifest.update({"stages": self.stages, "query_log": self.query_log,
                              "provider_query_counts": self.counts,
                              "cache_hits": sum(c["CACHE_HIT"] for c in self.counts.values()),
                              "query_accounting": self.budget.summary(),
                              "credits": {"balance_start": self.balance_start, "balance_now": self.balance_now,
                                          "credits_used_reported": round(self.spent_known, 2),
                                          "credits_used_by_balance": (round(self.balance_start - self.balance_now, 2)
                                                                      if None not in (self.balance_start,
                                                                                      self.balance_now) else None)}})
        return write_json_atomic(self.run_dir / "manifest.json", self.manifest, self.secrets)

    def _set(self, stage, status, **detail):
        self.stages[stage] = {"status": status, **detail}
        self.log.log(stage, status, message=detail.get("summary"))
        self._save_manifest()

    def _checkpoint(self, stage, data):
        write_json_atomic(self.ck_dir / CHECKPOINTS[stage], {"run_id": self.run_id, "stage": stage,
                                                             "data_environment": self.env,
                                                             "saved_at": now_utc().isoformat(), **data}, self.secrets)

    def _load_checkpoint(self, stage):
        return _read_json(self.ck_dir / CHECKPOINTS[stage])

    def _qlog(self, stage, action, query_type, products=None, **kw):
        self.counts[stage][action] += 1
        rec = {"stage": stage, "action": action, "query_type": query_type, "products": products or [],
               "timestamp": now_utc().isoformat(), **kw}
        self.query_log.append(rec)
        self.log.log(stage, action, query_type=query_type, message=", ".join(map(str, products or [])) or None,
                     **{k: v for k, v in kw.items() if k in ("task_id", "credits", "error_category")})

    # ------------------------------------------------------------------ the one paid-query path
    def _balance(self):
        try:
            b = self.client.credits()["totalRemain"]
        except Exception as e:  # noqa: BLE001
            raise ProviderFailure("provider_unavailable", safety.redact(f"credit balance unavailable: {e}", self.secrets))
        self.balance_now = b
        return b

    def paid_query(self, stage, query_type, query, products, followup_task=None):
        """Cap -> gate -> submit -> wait. Returns (response, task_id, cost). Raises StopPaid / ProviderFailure."""
        est = self.est(stage)
        g = self.eff["query_plan"].get("guardrails") or {}
        if not self.stop_paid and g:
            executed = self.budget.executed
            by_stage = sum(c["LIVE_QUERY"] for c in self.counts.values())
            if g.get("max_queries_per_run") is not None and executed >= g["max_queries_per_run"]:
                self.stop_paid = ("query_cap", f"max_queries_per_run {g['max_queries_per_run']} reached")
            elif g.get("max_provider_queries", {}).get("kalopilot") is not None and \
                    by_stage >= g["max_provider_queries"]["kalopilot"]:
                self.stop_paid = ("provider_query_cap", f"max_provider_queries.kalopilot "
                                                        f"{g['max_provider_queries']['kalopilot']} reached")
            elif g.get("max_failed_queries") is not None and self.failed_answers() >= g["max_failed_queries"]:
                self.stop_paid = ("failed_query_cap", f"max_failed_queries {g['max_failed_queries']} reached")
        if self.stop_paid:
            self.budget.record("blocked")
            self._qlog(stage, "BLOCKED", query_type, products, error_category=self.stop_paid[0])
            raise StopPaid(*self.stop_paid)
        bal = self._balance()
        cap = self.eff["query_plan"].get("max_credits_for_run")
        spent = max(self.spent_known, (self.balance_start - bal) if self.balance_start is not None else 0.0)
        if cap is not None and spent + (est or 0.0) > cap:
            self.stop_paid = ("run_credit_cap", f"run credit cap {cap} reached (spent {round(spent, 2)} + "
                                                f"next ~{est if est is not None else 'UNKNOWN'})")
            return self.paid_query(stage, query_type, query, products, followup_task)
        gate = safety.check_live_query(self.rt, balance=bal, estimated_cost=est)
        if not gate.allowed:
            cat = ("insufficient_credits" if any("insufficient credits" in r for r in gate.reasons) else
                   "credentials" if any("credentials" in r for r in gate.reasons) else "gate_blocked")
            self.stop_paid = (cat, "BLOCKED LIVE QUERY: " + "; ".join(gate.reasons))
            return self.paid_query(stage, query_type, query, products, followup_task)
        try:
            sub = self.client.submit(query, estimated_cost=est,
                                     **({"task_id": followup_task} if followup_task else {}))
        except safety.LiveQueryBlocked as e:
            msg = str(e)
            cat = ("insufficient_credits" if "insufficient credits" in msg else
                   "credentials" if "credentials" in msg else "gate_blocked")
            self.stop_paid = (cat, msg)
            return self.paid_query(stage, query_type, query, products, followup_task)
        task_id = (sub.get("data") or {}).get("task_id") if isinstance(sub, dict) else None
        if not task_id:
            cat = (sub or {}).get("error_category") or "no_task_id"
            status = (sub or {}).get("http_status")
            self.budget.record("failed")
            self._qlog(stage, "FAILED", query_type, products, error_category=cat)
            if status in (401, 403):
                self.stop_paid = ("credentials", f"provider rejected the credentials (HTTP {status})")
            if cat in ("provider_unavailable", "http_error", "malformed_response", "no_task_id"):
                self.provider_down = cat
                raise ProviderFailure(cat, safety.redact(str((sub or {}).get("message") or cat), self.secrets))
            return sub, None, None
        try:
            resp = self.client.wait(task_id)
        except Exception as e:  # noqa: BLE001
            resp = {"success": False, "error_category": "exception",
                    "message": safety.redact(f"{e.__class__.__name__}: {e}", self.secrets)}
        cost = credits_consumed(resp)
        try:
            bal_after = self._balance()
        except ProviderFailure:
            bal_after = None
        if cost is None and bal_after is not None:
            cost = round(bal - bal_after, 2)
        if isinstance(cost, (int, float)):
            self.spent_known += cost
        err = response_error(resp)
        self.budget.record("executed", cost, bal_after)
        self._qlog(stage, "LIVE_QUERY", query_type, products, task_id=task_id, credits=cost, error_category=err)
        return resp, task_id, cost

    def failed_answers(self):
        """Paid queries that failed or returned no usable answer (guardrail input)."""
        return sum(1 for q in self.query_log if q["action"] in ("FAILED", "LIVE_QUERY") and q.get("error_category"))

    # ================================================================== DRY RUN
    def dry_run(self, check_balance=False, preflight_result=None):
        now = self.now or now_utc()
        self._init_run("DRY_RUN", "NONE")
        out = {"run_id": self.run_id, "mode": "DRY_RUN", "profile": self.eff["profile_path"],
               "market": self.eff["runtime"]["market"], "blocking_issues": [], "warnings": []}
        pf = preflight_result or self._preflight()
        out["preflight"] = {"status": pf["status"], "blocking_reasons": pf["blocking_reasons"],
                            "warnings": pf["warnings"]}
        out["blocking_issues"] += pf["blocking_reasons"] + self.limit_errors()
        self._set("preflight", COMPLETED if pf["status"] == "READY_FOR_DRY_RUN" else BLOCKED,
                  summary=pf["status"])
        env_for_cache = LIVE                     # the cache a live run would reuse (real data only)
        budget = self.query_budget(env_for_cache, now) if not self.profile_errors else None
        out["query_budget"] = budget
        balance = None
        if check_balance:
            try:
                balance = (self.client or KaloClient()).credits()["totalRemain"]     # FREE endpoint
            except Exception as e:  # noqa: BLE001
                out["warnings"].append(f"balance check failed: {safety.redact(str(e), self.secrets)}")
        out["balance"] = balance if balance is not None else safety.UNKNOWN
        if self.from_task and self.client is None:
            self.client = KaloClient()
        out["discovery_preview"] = self._discovery_preview(env_for_cache, now) if budget else None
        out["plan"] = self._planned_stages(budget)
        for s in self.stage_list[1:]:
            self.stages[s] = {"status": SKIPPED, "summary": "dry run: planned only, not executed"}
        out["readiness"] = self._readiness(pf, budget, balance)
        out["warnings"] += out["readiness"]["warnings"]
        out["status"] = "DRY_RUN_COMPLETED" if not out["blocking_issues"] else "BLOCKED"
        self.manifest.update({"query_budget": budget, "dry_run": {"readiness": out["readiness"],
                                                                  "blocking_issues": out["blocking_issues"],
                                                                  "warnings": out["warnings"]},
                              "final_status": out["status"], "completed_at": now_utc().isoformat()})
        self.stages["run_summary"] = {"status": COMPLETED, "summary": out["status"]}
        out["manifest"] = str(self._save_manifest())
        out["log"] = str(self.log.path)
        out["paid_queries_executed"] = self.budget.executed          # always 0 in a dry run
        return out

    def _preflight(self):
        if self.preflight_fn:
            return self.preflight_fn()
        import preflight as PF
        return PF.preflight(self.root)

    def _fetch_discovery(self, q, h):
        """FREE: read the finished answer of an existing task (no new query, no new credits)."""
        self.budget.record("cached")
        try:
            resp = self.client.result(q["fetch_task"])
        except Exception as e:  # noqa: BLE001
            resp = {"success": False, "error_category": "provider_unavailable",
                    "message": safety.redact(str(e), self.secrets)}
        err = response_error(resp)
        if not err and not D.extract_records({"response": resp})[0]:
            err = "no_product_json_in_answer"
        charged = credits_consumed(resp)
        ts = now_utc()
        meta = {"category_key": q["category_key"], "query": q["query"], "query_sha256": h, "task_id": q["fetch_task"],
                "market": self.eff["runtime"]["market"], "currency": self.filters["market"]["currency"],
                "fetched_at": ts.strftime("%Y%m%dT%H%M%SZ"), "observation_date": ts.strftime("%Y-%m-%d"),
                "data_environment": self.env, "run_id": self.run_id, "fetched_via": "result_endpoint (free)",
                "source_raw": q.get("source_raw"), "credits_consumed_by_task": charged,
                "note": "task finished after the user continued it in the KaloData web UI; credits were charged "
                        "then, not by this run"}
        path = D.save_raw(resp, meta, self.raw)
        self._qlog("discovery", "FAILED" if err else "FETCHED_RESULT", "discovery_result", [q["category_key"]],
                   task_id=q["fetch_task"], credits=charged, error_category=err)
        self.manifest["credits_charged_before_run"] = charged
        return {"category_key": q["category_key"], "query_sha256": h, "raw_file": None if err else str(path),
                "failed_raw_file": str(path) if err else None, "action": "FETCHED_RESULT", "error": err}

    def _discovery_preview(self, env, now):
        """If Discovery would be a CACHE HIT, show exactly which products Deep Analysis would get."""
        dq = self.discovery_queries()
        if self.from_task and self.client is not None:
            import tempfile
            resp = self.client.result(self.from_task)                  # FREE
            if response_error(resp) or not D.extract_records({"response": resp})[0]:
                return {"available": False, "note": f"task {self.from_task} has no finished product JSON yet"}
            tmp = Path(tempfile.mkdtemp()) / "preview.json"
            tmp.write_text(json.dumps({"category_key": dq[0]["category_key"], "fetched_at": now.strftime("%Y%m%dT%H%M%SZ"),
                                       "market": "US", "response": resp}))
            paths = [tmp]
        else:
            paths = [self.find_cached_discovery(q["query"], env, now) for q in dq]
        if not all(paths):
            return {"available": False,
                    "note": "Discovery needs a paid query: the candidates are only known after it runs"}
        filters = copy.deepcopy(self.filters)
        filters["discovery_mode"]["max_candidates"] = self.limits["discovery_max_products"]
        res = D.run_discovery(paths, filters, self.categories)
        cfg = copy.deepcopy(self.cfg_deep)
        cfg["selection"]["deep_analysis_max_products"] = self.limits["deep_analysis_max_products"]
        cfg["selection"]["order"] = self.eff["query_plan"].get("deep_selection") or cfg["selection"].get("order")
        cfg["selection"]["exclude_keywords"] = self.eff["query_plan"].get("deep_exclude_keywords") or []
        cfg["selection"]["deprioritize_ids"] = self.recently_analyzed(self.eff["query_plan"].get("prefer_new_days"))
        sel, _ = DA.select_candidates(res, cfg)
        return {"available": True, "raw_files": [str(p) for p in paths], "summary": res["summary"],
                "deep_candidates": [{"product_id": DA.product_ref_id(p), "name": p["facts"].get("product_name"),
                                     "status": p["calculated"]["filter_status"],
                                     "deep_cached": bool(self.find_cached_product(
                                         DA.product_ref_id(p), "deep_product", self.ttl_hours, now,
                                         self.raw / "deep_analysis", env))} for p in sel]}

    def _planned_stages(self, budget):
        lim, qp = self.limits, self.eff["query_plan"]
        b = (budget or {}).get("stages", {})
        return [
            {"stage": "preflight", "does": "config, limits, directories, modules, safety gate", "paid": 0},
            {"stage": "discovery", "does": f"{qp.get('discovery_mode')} discovery, max {lim['discovery_max_products']} "
                                           "products", "paid": b.get("discovery", {}).get("expected_paid_max")},
            {"stage": "filtering", "does": "PASS / REVIEW / FAIL with config/filters.yaml", "paid": 0},
            {"stage": "deep_analysis", "does": f"max {lim['deep_analysis_max_products']} products, "
                                               f"{qp.get('deep_batch_size')} per query",
             "paid": b.get("deep_analysis", {}).get("expected_paid_max")},
            {"stage": "wps", "does": "WPS v1 (scripts/score_products.py)", "paid": 0},
            {"stage": "wps_confidence", "does": "Confidence Score", "paid": 0},
            {"stage": "amazon_validation", "does": f"eligible only ({amazon_rule()}), max "
                                                   f"{lim['amazon_validation_max_products']}",
             "paid": b.get("amazon_validation", {}).get("expected_paid_max")},
            {"stage": "bvs", "does": "BVS from stored supplier data only (incomplete if none; never invented)",
             "paid": 0},
            {"stage": "historical_storage", "does": "append-only observations tagged data_environment", "paid": 0},
            {"stage": "emerging_detector", "does": "Momentum Score / Emerging Status (same-environment history)",
             "paid": 0},
            {"stage": "final_report", "does": "dated MD + JSON + latest MD (LIVE records only)", "paid": 0},
            {"stage": "run_summary", "does": "manifest + summary", "paid": 0}] if not self.e2e else [
            {"stage": "preflight", "does": "config, limits, directories, modules, safety gate", "paid": 0},
            {"stage": "discovery", "does": f"{qp.get('discovery_mode')} discovery, max {lim['discovery_max_products']} "
                                           "products", "paid": b.get("discovery", {}).get("expected_paid_max")},
            {"stage": "filtering", "does": "PASS / REVIEW / FAIL", "paid": 0},
            {"stage": "deep_analysis", "does": f"max {lim['deep_analysis_max_products']} products, WPS + Confidence + "
                                               "trend + concentration + red flags",
             "paid": b.get("deep_analysis", {}).get("expected_paid_max")},
            {"stage": "amazon_validation", "does": f"eligible only ({amazon_rule()}), max "
                                                   f"{lim['amazon_validation_max_products']}",
             "paid": b.get("amazon_validation", {}).get("expected_paid_max")},
            {"stage": "supplier_research", "does": f"top {self.e2e['supplier_products_max']} products, max "
                                                   f"{self.e2e['supplier_offers_per_product_max']} offers each "
                                                   "(configured sources only; no order)", "paid": 0},
            {"stage": "bvs", "does": "BVS with real supplier economics where available (never invented)", "paid": 0},
            {"stage": "competitor_intelligence", "does": f"top {self.e2e['competitor_products_max']} products, max "
                                                         f"{self.e2e['competitors_per_product_max']} competitors",
             "paid": 0},
            {"stage": "creative_intelligence", "does": f"top {self.e2e['creative_products_max']} products, max "
                                                       f"{self.e2e['creatives_per_product_max']} creatives (saved "
                                                       "KaloPilot videos + imports)", "paid": 0},
            {"stage": "historical_storage", "does": "append LIVE observations, snapshot dedupe", "paid": 0},
            {"stage": "emerging_detector", "does": "Emerging Status / Momentum (INSUFFICIENT_HISTORY if too short)",
             "paid": 0},
            {"stage": "final_report", "does": "winning-products report (LIVE only)", "paid": 0},
            {"stage": "final_decision", "does": f"Step Z on max {self.e2e['final_decision_max_products']} products, "
                                                "data trust enforced, shortlist <= 3 READY", "paid": 0},
            {"stage": "aa_validation", "does": "AA report + validation audit + cost audit + verdict", "paid": 0},
            {"stage": "run_summary", "does": "manifest + summary", "paid": 0}]

    def _readiness(self, pf, budget, balance):
        reasons, warnings = [], []
        r = self.eff["runtime"]
        if self.profile_errors:
            reasons += self.profile_errors
        if not (r.get("live_mode") is True and r.get("dry_run") is False):
            reasons.append(f"profile {self.eff['profile_path']} is not a live profile (live_mode must be true and "
                           f"dry_run false)")
        if pf["status"] != "READY_FOR_DRY_RUN":
            reasons.append("pre-flight is not READY")
        reasons += self.limit_errors()
        if not safety.credentials_present(self.rt):
            reasons.append("provider credentials not found")
        if budget:
            lo, hi = budget["estimated_credits_min"], budget["estimated_credits_max"]
            reserve = self.rt["safety"]["min_balance_reserve"]
            cap = budget["max_credits_for_run"]
            if isinstance(balance, (int, float)):
                if isinstance(lo, (int, float)) and balance - lo < reserve:
                    reasons.append(f"balance {balance} cannot cover the minimum plan (~{lo}) + reserve {reserve}")
                elif isinstance(hi, (int, float)) and balance - hi < reserve:
                    warnings.append(f"balance {balance} may not cover the maximum plan (~{hi}) + reserve {reserve}; "
                                    f"the run would stop early and keep completed work")
            else:
                warnings.append("balance not checked (use --check-balance; the live run checks it before every query)")
            if isinstance(hi, (int, float)) and cap is not None and hi > cap:
                warnings.append(f"maximum estimate ~{hi} is above the run cap {cap}: later queries may be stopped")
            warnings.append("credit estimates are configured values, not provider quotes; batched queries have no "
                            "observed cost yet (actual cost is measured and recorded during the run)")
        else:
            reasons.append("query budget unavailable")
        verdict = "READY_FOR_FIRST_LIVE_RUN" if not reasons else "NOT_READY"
        return {"verdict": verdict, "reasons": reasons, "warnings": warnings}

    # ================================================================== LIVE
    def confirmation_text(self, budget, balance):
        lim, b = self.limits, budget
        L = ["", "=" * 66, "  LIVE RUN — PAID KALOPILOT QUERIES", "=" * 66,
             f"  Market:              {self.eff['runtime']['market']}",
             f"  Profile:             {self.eff['profile_path']}",
             f"  Limits:              discovery {lim['discovery_max_products']} | deep {lim['deep_analysis_max_products']}"
             f" | amazon {lim['amazon_validation_max_products']} | bvs {lim['bvs_max_products']}"]
        for s, r in b["stages"].items():
            L.append(f"  {s:<20} paid queries {r['expected_paid_min']}–{r['expected_paid_max']} "
                     f"(cached {r['cached']}), ~{r['estimated_credits_min']}–{r['estimated_credits_max']} credits")
        L += [f"  Estimated provider queries: {b['expected_paid_queries_min']}–{b['expected_paid_queries_max']}",
              f"  Estimated credits:   {b['estimated_credits_min']}–{b['estimated_credits_max']} "
              f"({'configured estimate, not a quote' if b['estimated_credits_max'] != safety.UNKNOWN else 'UNKNOWN'})",
              f"  Run credit cap:      {b['max_credits_for_run']}   | reserve kept: {b['min_balance_reserve']}",
              f"  Current balance:     {balance}", "=" * 66,
              f"  Type exactly  {self.phrase}  to continue (anything else cancels).", ""]
        return "\n".join(L)

    def confirm(self, text, confirm_value=None):
        """True only for the exact phrase. yes / y / ok / continue / lowercase are rejected."""
        if confirm_value is not None:
            return confirm_value == self.phrase, "flag"
        if not self.isatty:
            return False, "non_interactive"
        self.out(text)
        try:
            typed = self.input_fn("> ")
        except EOFError:
            return False, "eof"
        return typed == self.phrase, "typed"

    def live(self, confirm_value=None, resume_id=None):
        now = self.now or now_utc()
        client = self.client or KaloClient()
        self.client = client
        env = self.environment_for(client)
        if env == SYNTHETIC and self.data_root.resolve() == ROOT.resolve():
            raise ValueError("a SYNTHETIC (test) provider may not write into the real project data")
        if resume_id and not (self.runs_dir / resume_id / "manifest.json").exists():
            raise SystemExit(f"run {resume_id} not found")
        self._init_run("LIVE", env, resume_id)
        blocks = []
        r = self.eff["runtime"]
        if r.get("live_mode") is not True:
            blocks.append(f"live_mode is not true in {self.eff['profile_path']}")
        if r.get("dry_run") is not False:
            blocks.append(f"dry_run is not false in {self.eff['profile_path']}")
        blocks += self.limit_errors()
        blocks += self.extra_live_blocks()
        if resume_id and self.manifest.get("profile", {}).get("path") != self.eff["profile_path"]:
            blocks.append("resume must use the same profile as the original run")
        pf = self._preflight()
        if pf["status"] != "READY_FOR_DRY_RUN":
            blocks.append("pre-flight not READY: " + "; ".join(pf["blocking_reasons"]))
        if not safety.credentials_present(self.rt):
            blocks.append("provider credentials not available")
        budget = self.query_budget(env, now) if not self.profile_errors else None
        if budget and resume_id:
            budget = self.resume_budget(budget)
        balance = None
        if not blocks:
            try:
                balance = self._balance()
            except ProviderFailure as e:
                blocks.append(f"credit check failed: {e.message}")
        if balance is not None and budget:
            reserve = self.rt["safety"]["min_balance_reserve"]
            lo = budget["estimated_credits_min"]
            first = self.est("discovery") or self.rt["safety"]["estimated_credits_per_query"]
            if balance - first < reserve or (isinstance(lo, (int, float)) and balance - lo < reserve):
                blocks.append(f"insufficient credits: balance {balance}, minimum plan ~{lo}, reserve {reserve}")
        self.balance_start = self.manifest.get("credits", {}).get("balance_start") if resume_id else balance
        if self.balance_start is None:
            self.balance_start = balance
        self.manifest["query_budget"] = budget
        self._set("preflight", COMPLETED if pf["status"] == "READY_FOR_DRY_RUN" else BLOCKED, summary=pf["status"])
        if not blocks:
            ok, how = self.confirm(self.confirmation_text(budget, balance), confirm_value)
            if not ok:
                blocks.append({"non_interactive": f"non-interactive session: pass --confirm-live \"{self.phrase}\"",
                               "eof": "no confirmation received"}.get(
                    how, f"confirmation rejected: you must type exactly {self.phrase}"))
        if blocks:
            for s in self.stage_list[1:]:
                if self.stages.get(s, {}).get("status", PENDING) == PENDING:
                    self.stages[s] = {"status": BLOCKED, "summary": "run blocked before execution"}
            self.manifest.update({"final_status": BLOCKED, "blocking_reasons": blocks,
                                  "completed_at": now_utc().isoformat()})
            self._save_manifest()
            self.log.log("run", BLOCKED, message="; ".join(blocks))
            return self.summary(blocks=blocks)

        safety.set_run_overrides(live_mode=True, dry_run=False, explicit_live_confirmation=True)   # in memory only
        try:
            self._execute(now)
        finally:
            safety.clear_run_overrides()
        return self.summary()

    def extra_live_blocks(self):
        """Hook (AC): additional reasons that block a live run before confirmation."""
        return []

    def report_banner_extra(self):
        return ""

    def environment_for(self, client):
        return getattr(client, "data_environment", SYNTHETIC)

    def pre_stage_check(self, name):
        """Hook (AC config lock): return an error string to stop the run before stage `name`."""
        return None

    def degraded_penalties(self, prods, ctx):
        """Hook (AC degraded mode): {product_id: [{"reason", "points"}]} lowering Decision Confidence."""
        return {}

    def e2e_steps(self):
        return [("discovery", self.s_discovery), ("filtering", self.s_filtering),
                ("deep_analysis", self.s_deep), ("wps", self.s_wps), ("wps_confidence", self.s_conf),
                ("amazon_validation", self.s_amazon), ("supplier_research", self.s_suppliers),
                ("bvs", self.s_bvs), ("competitor_intelligence", self.s_competitors),
                ("creative_intelligence", self.s_creatives), ("historical_storage", self.s_history),
                ("emerging_detector", self.s_emerging), ("final_report", self.s_report),
                ("final_decision", self.s_final_decision), ("aa_validation", self.s_aa_validation)]

    # ------------------------------------------------------------------ stage execution
    def _execute(self, now):
        ctx = {}
        steps = [("discovery", self.s_discovery), ("filtering", self.s_filtering),
                 ("deep_analysis", self.s_deep), ("wps", self.s_wps), ("wps_confidence", self.s_conf),
                 ("amazon_validation", self.s_amazon), ("bvs", self.s_bvs),
                 ("historical_storage", self.s_history), ("emerging_detector", self.s_emerging),
                 ("final_report", self.s_report)]
        if self.e2e:                                   # Step AA: full chain, all layers + decision + audit
            steps = self.e2e_steps()
        for name, fn in steps:
            err = self.pre_stage_check(name)
            if err:
                self.stages[name] = {"status": BLOCKED, "summary": err}
                for rest, _ in steps[[n for n, _ in steps].index(name) + 1:]:
                    self.stages[rest] = {"status": BLOCKED, "summary": "run stopped: " + err}
                self.manifest["run_invalid"] = err
                self.stop_paid = ("config_drift", err)
                self._save_manifest()
                break
            if self.stages.get(name, {}).get("status") == COMPLETED and name not in self.FREE_RERUN and \
                    self._restore(name, ctx):
                continue                                               # resume: completed stage reused
            self.stages[name] = {"status": RUNNING}
            self._save_manifest()
            try:
                status, detail = fn(ctx, now)
            except Exception as e:  # noqa: BLE001  one stage failing never corrupts the run
                status, detail = FAILED, {"summary": safety.redact(f"{e.__class__.__name__}: {e}", self.secrets),
                                          "error_category": e.__class__.__name__}
            self._set(name, status, **detail)
        self.stages["run_summary"] = {"status": COMPLETED}
        self.manifest["final_status"] = self.overall_status()
        self.manifest["completed_at"] = now_utc().isoformat()
        self.manifest["outputs"] = ctx.get("outputs", {})
        self.manifest["summary"] = self._summary_counts(ctx)
        self._set("run_summary", COMPLETED, summary=self.manifest["final_status"])

    @property
    def FREE_RERUN(self):
        """Stages that cost nothing and are recomputed on resume (AA: their inputs live in ctx only)."""
        if not self.e2e:
            return ()
        return ("supplier_research", "competitor_intelligence", "creative_intelligence", "final_report",
                "final_decision", "aa_validation", "bvs")

    def _restore(self, name, ctx):
        ck = self._load_checkpoint(name) if name in CHECKPOINTS else None
        if name in ("wps", "wps_confidence"):
            return "deep" in ctx
        if name == "filtering":
            ck = self._load_checkpoint("discovery")
            if ck and ck.get("processed_file"):
                ctx["discovery"] = _read_json(ck["processed_file"])
                ctx["discovery_file"] = ck["processed_file"]
                return ctx["discovery"] is not None
            return False
        if not ck:
            return False
        if name == "discovery":
            ctx["raw_files"] = ck.get("raw_files", [])
        elif name == "deep_analysis":
            ctx["deep"], ctx["deep_file"] = ck.get("results", []), ck.get("saved")
        elif name == "amazon_validation":
            ctx["amazon"], ctx["amazon_file"] = ck.get("results", []), ck.get("saved")
        elif name == "bvs":
            ctx["bvs"], ctx["bvs_file"] = ck.get("results", []), ck.get("saved")
        elif name == "final_report":
            ctx.setdefault("outputs", {}).update(ck.get("outputs", {}))
        return True

    # ---- DISCOVERY
    def s_discovery(self, ctx, now):
        ck = self._load_checkpoint("discovery") or {}
        done = {q["query_sha256"]: q["raw_file"] for q in ck.get("queries", []) if q.get("raw_file")}
        queries, rows, stop = self.discovery_queries(), [], None
        for q in queries:
            h = sha(q["query"])
            self.budget.plan(1)
            if q.get("fetch_task") and h not in done:
                rows.append(self._fetch_discovery(q, h))
                self._checkpoint("discovery", {"status": RUNNING, "queries": rows})
                continue
            if h in done:
                rows.append({"category_key": q["category_key"], "query_sha256": h, "raw_file": done[h],
                             "action": "CHECKPOINT"})
                self.budget.record("cached")
                continue
            cached = None if q.get("followup_task") else self.find_cached_discovery(q["query"], self.env, now)
            if cached:
                self.budget.record("cached")
                self._qlog("discovery", "CACHE_HIT", "discovery", [q["category_key"]], raw_file=str(cached))
                rows.append({"category_key": q["category_key"], "query_sha256": h, "raw_file": str(cached),
                             "action": "CACHE_HIT"})
                continue
            try:
                resp, task_id, cost = self.paid_query("discovery", "discovery_followup" if q.get("followup_task")
                                                      else "discovery", q["query"], [q["category_key"]],
                                                      q.get("followup_task"))
            except StopPaid as e:
                stop = e.message
                break
            except ProviderFailure as e:
                stop = f"provider failure: {e.message}"
                break
            ts = now_utc()
            meta = {"category_key": q["category_key"], "query": q["query"], "query_sha256": h, "task_id": task_id,
                    "market": self.eff["runtime"]["market"], "currency": self.filters["market"]["currency"],
                    "fetched_at": ts.strftime("%Y%m%dT%H%M%SZ"), "observation_date": ts.strftime("%Y-%m-%d"),
                    "data_environment": self.env, "run_id": self.run_id, "credits_consumed": cost}
            if q.get("followup_task"):
                meta.update({"followup_of_task_id": q["followup_task"], "followup_of_raw": q.get("followup_raw")})
            if q.get("categories"):
                meta["categories"] = q["categories"]
            if q.get("lens"):
                meta["lens"] = q["lens"]
            path = D.save_raw(resp, meta, self.raw)
            err = response_error(resp)
            if not err and not D.extract_records({"response": resp})[0]:
                # Step U finding: the provider can report "completed" without the product JSON block
                err = "no_product_json_in_answer"
            rows.append({"category_key": q["category_key"], "query_sha256": h, "raw_file": str(path) if not err else None,
                         "failed_raw_file": str(path) if err else None, "action": "LIVE_QUERY", "error": err})
            self._checkpoint("discovery", {"status": RUNNING, "queries": rows})
        ctx["raw_files"] = [r["raw_file"] for r in rows if r.get("raw_file")]
        ok, n = len(ctx["raw_files"]), len(queries)
        status = COMPLETED if ok == n else (PARTIAL if ok else (BLOCKED if stop and "BLOCKED" in stop else FAILED))
        self._checkpoint("discovery", {"status": status, "queries": rows, "raw_files": ctx["raw_files"]})
        return status, {"summary": f"{ok}/{n} discovery answers" + (f" — stopped: {stop}" if stop else ""),
                        "queries": n, "answers": ok, "stop_reason": stop}

    # ---- FILTERING
    def s_filtering(self, ctx, now):
        if not ctx.get("raw_files"):
            return SKIPPED, {"summary": "no discovery answer to filter"}
        filters = copy.deepcopy(self.filters)
        filters["discovery_mode"]["max_candidates"] = self.limits["discovery_max_products"]
        res = D.run_discovery(ctx["raw_files"], filters, self.categories)
        res.update({"data_environment": self.env, "run_id": self.run_id})
        for p in res["candidates"] + res["failed"]:
            p["data_environment"] = self.env
        path = D.save_processed(res, self.processed)
        ctx["discovery"], ctx["discovery_file"] = json.loads(Path(path).read_text()), str(path)
        ck = self._load_checkpoint("discovery") or {}
        self._checkpoint("discovery", {**ck, "processed_file": str(path)})
        s = res["summary"]
        status = COMPLETED if s["unique"] else PARTIAL
        return status, {"summary": f"{s['unique']} unique: PASS {s['PASS']}, REVIEW {s['REVIEW']}, FAIL {s['FAIL']} "
                                   f"(malformed {s['malformed']})", "counts": s, "processed_file": str(path)}

    # ---- DEEP ANALYSIS
    def _batches(self, items, size):
        return [items[i:i + size] for i in range(0, len(items), size)]

    def _batch_prompt(self, name, n, lines, **kw):
        import re
        text = (ROOT / "prompts" / name).read_text()
        return re.split(r"^===\s*$", text, flags=re.M)[1].strip().format(n=n, product_lines=lines, **kw)

    def deep_batch_query(self, products, cfg, market):
        lines = []
        for p in products:
            f = p["facts"]
            lines.append(f"- product_id {DA.product_ref_id(p)}: {f.get('product_name') or 'N/A'}"
                         + (f" — {f['product_url']}" if f.get("product_url") else ""))
        q = cfg["queries"]
        return self._batch_prompt("deep_analysis_batch.md", len(products), "\n".join(lines), region=market["region"],
                                  currency=market["currency"], period_days=q["period_days"],
                                  top_creators=q["top_creators"], top_videos=q["top_videos"])

    def _split(self, resp, pids):
        """{product_id: object} matched by product_id only (never by guess)."""
        recs, _ = D.extract_records({"response": resp})
        by = {}
        for o in recs:
            if isinstance(o, dict) and o.get("product_id") is not None and str(o["product_id"]).strip() in pids:
                by.setdefault(str(o["product_id"]).strip(), o)
        if len(pids) == 1 and len(recs) == 1 and isinstance(recs[0], dict) and not by:
            by[pids[0]] = recs[0]                     # single product: the one answer is that product
        return by

    def _derived(self, obj, resp, task_id, batch_raw, idx):
        data = (resp or {}).get("data") or {}
        return {"success": True, "derived_from_batch": True, "batch_raw_file": str(batch_raw), "batch_index": idx,
                "data": {"status": "completed", "task_id": task_id, "report_url": data.get("report_url"),
                         "report": "```json\n" + json.dumps(obj, ensure_ascii=False) + "\n```"}}

    def recently_analyzed(self, days):
        """product ids with a successful deep analysis in the last `days` days (prefer NEW products)."""
        if not days:
            return []
        cut = now_utc() - timedelta(days=days)
        ids = set()
        for p in (self.processed / "deep_analysis").glob("deep_*.json"):
            try:
                if datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) < cut:
                    continue
                ids |= {str(r.get("product_id")) for r in (_read_json(p) or {}).get("results") or []
                        if r.get("status") == "ok"}
            except OSError:
                continue
        return sorted(ids)

    def s_deep(self, ctx, now):
        if not ctx.get("discovery"):
            return SKIPPED, {"summary": "no filtered discovery result"}
        cfg = copy.deepcopy(self.cfg_deep)
        cfg["selection"]["deep_analysis_max_products"] = self.limits["deep_analysis_max_products"]
        cfg["selection"]["order"] = self.eff["query_plan"].get("deep_selection") or cfg["selection"].get("order")
        cfg["selection"]["exclude_keywords"] = self.eff["query_plan"].get("deep_exclude_keywords") or []
        cfg["selection"]["deprioritize_ids"] = self.recently_analyzed(self.eff["query_plan"].get("prefer_new_days"))
        cfgs = {"deep": cfg, "filters": self.filters, "scoring": load_scoring_config()}
        dres = ctx["discovery"]
        market = dres.get("market") or {"region": "US", "currency": "USD"}
        selected, skipped = DA.select_candidates(dres, cfg)
        if not selected:
            ctx["deep"] = []
            return SKIPPED, {"summary": "no PASS/REVIEW candidate with an identifier", "skipped": skipped}
        raw_dir, out_dir = self.raw / "deep_analysis", self.processed / "deep_analysis"
        ck = self._load_checkpoint("deep_analysis") or {}
        results = list(ck.get("results", []))
        done = {str(r.get("product_id")) for r in results} | {str(r.get("_pid")) for r in results}
        by_pid = {str(DA.product_ref_id(p)): p for p in selected}
        pending, stop = [], None

        def add(rec, pid):
            rec.update({"data_environment": self.env, "run_id": self.run_id, "_pid": pid})
            results.append(rec)
            self._checkpoint("deep_analysis", {"status": RUNNING, "results": results})

        for pid, p in by_pid.items():
            if pid in done:
                continue
            cached = self.find_cached_product(pid, "deep_product", self.ttl_hours, now, raw_dir, self.env)
            if cached:
                self.budget.plan(1)
                self.budget.record("cached")
                self._qlog("deep_analysis", "CACHE_HIT", "deep_product", [pid], raw_file=str(cached))
                add(DA.analyze(json.loads(cached.read_text()), cached, p, cfgs, DA.history_count(pid, out_dir), True), pid)
            else:
                pending.append(pid)
        size = self.eff["query_plan"].get("deep_batch_size", 1)
        for batch in self._batches(pending, size):
            self.budget.plan(1)
            prods = [by_pid[x] for x in batch]
            query = DA.build_query(prods[0], cfg, market) if len(batch) == 1 else self.deep_batch_query(prods, cfg, market)
            qtype = "deep_product" if len(batch) == 1 else "deep_batch"
            try:
                resp, task_id, _ = self.paid_query("deep_analysis", qtype, query, batch)
            except StopPaid as e:
                stop = e.message
                break
            except ProviderFailure as e:
                stop = f"provider failure: {e.message}"
                for x in batch:
                    add({"product_id": x, "status": "failed", "error_category": e.category}, x)
                break
            obs = now_utc().isoformat()
            base = {"observation_timestamp": obs, "market": market.get("region", "US"), "query": query,
                    "prompt_version": self.prompt_version("deep_product"),
                    "task_id": task_id, "data_environment": self.env, "run_id": self.run_id}
            err = response_error(resp)
            if len(batch) == 1:
                raw = DA.save_raw_deep(resp, {**base, "product_id": batch[0], "query_type": "deep_product"}, raw_dir)
                if err:
                    add({"product_id": batch[0], "status": "failed", "error_category": err,
                         "source": {"raw_file": str(raw)}}, batch[0])
                else:
                    add(DA.analyze(json.loads(raw.read_text()), raw, prods[0], cfgs,
                                   DA.history_count(batch[0], out_dir), False), batch[0])
                continue
            batch_raw = DA.save_raw_deep(resp, {**base, "product_id": "batch", "query_type": "deep_batch",
                                                "product_ids": batch}, raw_dir)
            objs = {} if err else self._split(resp, batch)
            for i, x in enumerate(batch):
                if x not in objs:
                    add({"product_id": x, "status": "failed", "error_category": err or "missing_from_batch_response",
                         "source": {"raw_file": str(batch_raw)}}, x)
                    continue
                raw = DA.save_raw_deep(self._derived(objs[x], resp, task_id, batch_raw, i),
                                       {**base, "product_id": x, "query_type": "deep_product",
                                        "batch_raw_file": str(batch_raw), "derived_from_batch": True}, raw_dir)
                add(DA.analyze(json.loads(raw.read_text()), raw, by_pid[x], cfgs, DA.history_count(x, out_dir), False), x)
        not_run = [x for x in by_pid if x not in {str(r.get("_pid")) for r in results}]
        report = {"mode": "live", "run_id": self.run_id, "data_environment": self.env,
                  "discovery_file": ctx.get("discovery_file"), "selected": len(selected), "skipped": skipped,
                  "not_run": not_run, "stopped": stop, "results": results}
        saved = DA.save_results(report, out_dir)
        ctx["deep"], ctx["deep_file"] = results, str(saved)
        ok = sum(r.get("status") == "ok" for r in results)
        status = COMPLETED if ok == len(selected) else (PARTIAL if ok else (BLOCKED if stop and not results else FAILED))
        self._checkpoint("deep_analysis", {"status": status, "results": results, "saved": str(saved),
                                           "not_run": not_run, "stopped": stop})
        return status, {"summary": f"{ok}/{len(selected)} analyzed ok" + (f"; not run: {len(not_run)}" if not_run else "")
                        + (f" — stopped: {stop}" if stop else ""), "ok": ok, "selected": len(selected),
                        "failed": [{"product_id": r.get("product_id"), "error": r.get("error_category") or r.get("status")}
                                   for r in results if r.get("status") != "ok"], "saved": str(saved)}

    # ---- WPS / CONFIDENCE (calculated inside Deep Analysis by the protected engines)
    def _ok_deep(self, ctx):
        return [r for r in ctx.get("deep") or [] if r.get("status") == "ok"]

    def s_wps(self, ctx, now):
        ok = self._ok_deep(ctx)
        if not ok:
            return SKIPPED, {"summary": "no deep-analysis result"}
        rows = [{"product_id": r["product_id"], "wps": r.get("wps"), "complete": r.get("wps_complete"),
                 "verdict": r.get("verdict"), "na_metrics": r.get("wps_na_metrics")} for r in ok]
        return COMPLETED, {"summary": f"WPS for {len(ok)} product(s); incomplete: "
                                      f"{sum(not r['complete'] for r in rows)}", "products": rows}

    def s_conf(self, ctx, now):
        ok = self._ok_deep(ctx)
        if not ok:
            return SKIPPED, {"summary": "no deep-analysis result"}
        rows = [{"product_id": r["product_id"], "confidence": r.get("confidence"), "level": r.get("confidence_level")}
                for r in ok]
        return COMPLETED, {"summary": f"Confidence for {len(ok)} product(s)", "products": rows}

    # ---- AMAZON
    def amazon_batch_query(self, deeps, cfg):
        lines = []
        for d in deeps:
            tk = AV.tiktok_view(d)
            lines.append(f"- product_id {d['product_id']}: Name: {tk['name'] or 'N/A'} | Category: "
                         f"{tk['category'] or 'N/A'} | Brand/shop: {tk['brand'] or tk['shop_name'] or 'N/A'} | "
                         f"TikTok price: {'N/A' if tk['price'] is None else round(tk['price'], 2)} USD")
        return self._batch_prompt("amazon_validation_batch.md", len(deeps), "\n".join(lines),
                                  max_candidates=cfg["max_candidates_requested"])

    def s_amazon(self, ctx, now):
        ok = self._ok_deep(ctx)
        if not ok:
            ctx["amazon"] = []
            return SKIPPED, {"summary": "no deep-analysis result"}
        cfg = AV.load_cfg()
        if not cfg["enabled"]:
            ctx["amazon"] = []
            return SKIPPED, {"summary": "amazon_validation.enabled is false"}
        cfg["max_products"] = self.limits["amazon_validation_max_products"]
        eligible, excluded = AV.select_eligible([{**r, "_deep_file": ctx.get("deep_file")} for r in ok], cfg)
        cal = self.calibration_products(ok, eligible, ctx)             # calibration lane (off by default)
        cal_ids = {str(d["product_id"]) for d in cal}
        eligible = eligible + cal
        if not eligible:
            ctx["amazon"] = []
            return SKIPPED, {"summary": f"no eligible product (WPS >= {cfg['minimum_wps']} and Confidence >= "
                                        f"{cfg['minimum_confidence']}): 0 paid queries", "excluded": excluded}
        raw_dir, qt = self.raw / "amazon_validation", cfg["query_type"]
        ck = self._load_checkpoint("amazon_validation") or {}
        results = list(ck.get("results", []))
        done = {str(r.get("product_id")) for r in results}
        by = {str(d["product_id"]): d for d in eligible}
        pending, stop = [], None

        def add(rec):
            rec.update({"data_environment": self.env, "run_id": self.run_id,
                        "calibration_only": str(rec.get("product_id")) in cal_ids})
            results.append(rec)
            self._checkpoint("amazon_validation", {"status": RUNNING, "results": results})

        for pid, d in by.items():
            if pid in done:
                continue
            cached = self.find_cached_product(pid, qt, cfg["cache_hours"], now, raw_dir, self.env)
            if cached:
                self.budget.plan(1)
                self.budget.record("cached")
                self._qlog("amazon_validation", "CACHE_HIT", qt, [pid], raw_file=str(cached))
                add(AV.validate(json.loads(cached.read_text()), cached, d, cfg, self.filters, True))
            else:
                pending.append(pid)
        for batch in self._batches(pending, self.eff["query_plan"].get("amazon_batch_size", 1)):
            self.budget.plan(1)
            ds = [by[x] for x in batch]
            query = AV.build_query(ds[0], cfg) if len(batch) == 1 else self.amazon_batch_query(ds, cfg)
            try:
                resp, task_id, _ = self.paid_query("amazon_validation", qt if len(batch) == 1 else "amazon_batch",
                                                   query, batch)
            except StopPaid as e:
                stop = e.message
                break
            except ProviderFailure as e:
                stop = f"provider failure: {e.message}"
                break
            base = {"observation_timestamp": now_utc().isoformat(), "market": "US", "query": query, "task_id": task_id,
                    "prompt_version": self.prompt_version(qt),
                    "provider": cfg["provider"], "data_environment": self.env, "run_id": self.run_id}
            err = response_error(resp)
            if len(batch) == 1:
                raw = DA.save_raw_deep(resp, {**base, "product_id": batch[0], "query_type": qt}, raw_dir)
                add({"product_id": batch[0], "status": "failed", "error_category": err, "source": {"raw_file": str(raw)}}
                    if err else AV.validate(json.loads(raw.read_text()), raw, ds[0], cfg, self.filters, False))
                continue
            batch_raw = DA.save_raw_deep(resp, {**base, "product_id": "batch", "query_type": "amazon_batch",
                                                "product_ids": batch}, raw_dir)
            objs = {} if err else self._split(resp, batch)
            for i, x in enumerate(batch):
                if x not in objs:
                    add({"product_id": x, "status": "failed", "error_category": err or "missing_from_batch_response",
                         "source": {"raw_file": str(batch_raw)}})
                    continue
                obj = {k: v for k, v in objs[x].items() if k != "product_id"}
                raw = DA.save_raw_deep(self._derived(obj, resp, task_id, batch_raw, i),
                                       {**base, "product_id": x, "query_type": qt, "batch_raw_file": str(batch_raw),
                                        "derived_from_batch": True}, raw_dir)
                add(AV.validate(json.loads(raw.read_text()), raw, by[x], cfg, self.filters, False))
        report = {"mode": "live", "run_id": self.run_id, "data_environment": self.env, "deep_file": ctx.get("deep_file"),
                  "eligible": len(eligible), "excluded": excluded, "stopped": stop, "results": results}
        saved = AV.save_results(report, self.processed / "amazon_validation")
        ctx["amazon"], ctx["amazon_file"] = results, str(saved)
        good = sum(r.get("status") != "failed" and r.get("status") != "malformed" for r in results)
        status = COMPLETED if good == len(eligible) else (PARTIAL if good else (BLOCKED if stop and not results else FAILED))
        self._checkpoint("amazon_validation", {"status": status, "results": results, "saved": str(saved), "stopped": stop})
        return status, {"summary": f"{good}/{len(eligible)} validated" + (f" — stopped: {stop}" if stop else ""),
                        "eligible": len(eligible), "saved": str(saved)}

    def resume_budget(self, b):
        """Resume: completed stages / products already in checkpoints cost nothing again."""
        import copy as _c
        b = _c.deepcopy(b)
        disc = self.stages.get("discovery", {}).get("status") == COMPLETED
        deep_ck = self._load_checkpoint("deep_analysis") or {}
        deep_left = len(deep_ck.get("not_run") or []) if deep_ck else None
        for stage, zero in (("discovery", disc), ("deep_analysis", deep_left == 0)):
            if zero:
                r = b["stages"][stage]
                r.update(expected_paid_min=0, expected_paid_max=0, estimated_credits_min=0.0, estimated_credits_max=0.0,
                         note="resume: already done (checkpoint), nothing re-bought")
        if self.stages.get("amazon_validation", {}).get("status") != COMPLETED:
            r = b["stages"]["amazon_validation"]
            extra = (self.cal_cfg or {}).get("max_products", 0) if (self.cal_cfg or {}).get("enabled") else 0
            n = min(self.limits["amazon_validation_max_products"] + extra, 99)
            if deep_left == 0:                          # deep finished: the Amazon candidates are known exactly
                ok = [d for d in deep_ck.get("results") or [] if d.get("status") == "ok"]
                cfg = AV.load_cfg()
                cfg["max_products"] = self.limits["amazon_validation_max_products"]
                elig, _ = AV.select_eligible(ok, cfg)
                n = len(elig) + len(self.calibration_products(ok, elig, {}))
            q = math.ceil(n / self.eff["query_plan"].get("amazon_batch_size", 1)) if n else 0
            e = self.est("amazon_validation")
            r.update(expected_paid_max=q, max_products=n, estimated_credits_max=round(q * e, 2) if e is not None
                     else safety.UNKNOWN, expected_paid_min=q if deep_left == 0 else 0,
                     estimated_credits_min=(round(q * e, 2) if e is not None else safety.UNKNOWN) if deep_left == 0
                     else 0.0, note=f"{n} product(s): eligible ({amazon_rule()}) + calibration lane")
        rows = b["stages"].values()
        b["expected_paid_queries_min"] = sum(r["expected_paid_min"] for r in rows)
        b["expected_paid_queries_max"] = sum(r["expected_paid_max"] for r in rows)
        vals = [r["estimated_credits_max"] for r in rows]
        b["estimated_credits_max"] = safety.UNKNOWN if safety.UNKNOWN in vals else round(sum(vals), 2)
        vals = [r["estimated_credits_min"] for r in rows]
        b["estimated_credits_min"] = safety.UNKNOWN if safety.UNKNOWN in vals else round(sum(vals), 2)
        b["resume"] = True
        return b

    # ---- calibration lane helpers
    def calibration_products(self, ok, eligible, ctx):
        c = self.cal_cfg or {}
        if not c.get("enabled") or not c.get("allow_non_qualified_products"):
            return []
        taken = {str(d["product_id"]) for d in eligible}
        pool = sorted([d for d in ok if str(d["product_id"]) not in taken],
                      key=lambda d: (-(d.get("wps") or 0), str(d["product_id"])))
        out = []
        for status, n in (c.get("select") or {}).items():
            out += [{**d, "_deep_file": ctx.get("deep_file")} for d in pool
                    if (d.get("source") or {}).get("discovery_status") == status][:n]
        return out[:c.get("max_products", 3)]

    def calibration_rows(self, ctx):
        amz = {str(a["product_id"]): a for a in ctx.get("amazon") or [] if a.get("calibration_only")}
        bvs = {str(b["product_id"]): b for b in ctx.get("bvs") or [] if b.get("calibration_only")}
        rows = []
        for d in self._ok_deep(ctx):
            pid = str(d["product_id"])
            if pid in amz or pid in bvs:
                a, b = amz.get(pid) or {}, bvs.get(pid) or {}
                rows.append({"product_id": pid, "name": d.get("product_name"), "calibration_only": True,
                             "wps": d.get("wps"), "confidence": d.get("confidence"),
                             "amazon_match_status": a.get("amazon_match_status", a.get("status")), "avs": a.get("avs"),
                             "amazon_confidence": a.get("amazon_confidence"), "bvs": b.get("bvs"),
                             "bvs_confidence": b.get("bvs_confidence")})
        return rows

    # ---- BVS (no paid query; supplier data only from stored raw records)
    def s_bvs(self, ctx, now):
        if not ctx.get("deep_file") or not self._ok_deep(ctx):
            ctx["bvs"] = []
            return SKIPPED, {"summary": "no deep-analysis result"}
        cfg = B.load_cfg()
        cfg["eligibility"]["max_products"] = self.limits["bvs_max_products"]
        aa = {}
        if self.e2e:
            aa = {"only_products": {str(x["product_id"]) for x in ctx.get("supplier_research") or []},
                  "max_offers": self.e2e["supplier_offers_per_product_max"]}
        r = B.run(deep_path=ctx["deep_file"], amazon_path=ctx.get("amazon_file"),
                  raw_dir=self.raw / "business_viability", cfg=cfg, filters_cfg=self.filters, save=False,
                  suppliers_dir=self.processed / "suppliers", **aa)
        results = [{**x, "data_environment": self.env, "run_id": self.run_id, "calibration_only": False}
                   for x in r.get("results", [])]
        cal_amz = {str(a["product_id"]): a for a in ctx.get("amazon") or [] if a.get("calibration_only")}
        if cal_amz:                                  # calibration lane: BVS plumbing, never ranked
            for d in self._ok_deep(ctx):
                if str(d["product_id"]) in cal_amz and str(d["product_id"]) not in {str(x["product_id"]) for x in results}:
                    import suppliers as SUP
                    sell, _ = B.selling_price(d, {}, cfg)
                    comm, src = SUP.commercial_data_for(d, self.processed / "suppliers", selling_price=sell)
                    if comm is None:
                        comm, src = B.load_commercial_data(d["product_id"], self.raw / "business_viability")
                    x = B.evaluate({**d, "_file": ctx["deep_file"]}, cal_amz[str(d["product_id"])], comm, cfg,
                                   self.filters, src)
                    results.append({**x, "data_environment": self.env, "run_id": self.run_id, "calibration_only": True})
        ctx["bvs"] = results
        if not results:
            return SKIPPED, {"summary": "no product eligible for BVS", "excluded": r.get("excluded")}
        saved = write_new_json(self.processed / "business_viability" /
                               f"bvs_{now_utc().strftime('%Y%m%dT%H%M%S%fZ')}.json",
                               {**r, "results": results, "run_id": self.run_id, "data_environment": self.env})
        ctx["bvs_file"] = str(saved)
        self._checkpoint("bvs", {"status": COMPLETED, "results": results, "saved": str(saved)})
        incomplete = sum(x.get("bvs") is None or any(f.get("flag") == "INSUFFICIENT_SUPPLIER_DATA"
                                                     for f in x.get("commercial_red_flags") or []) for x in results)
        status = PARTIAL if incomplete else COMPLETED
        return status, {"summary": f"{len(results)} evaluated; {incomplete} incomplete (no supplier data — "
                                   "not fabricated)", "saved": str(saved)}

    # ---- HISTORY
    def s_history(self, ctx, now):
        if not ctx.get("discovery"):
            return SKIPPED, {"summary": "nothing to store"}
        store = HIST.HistoryStore(self.history_dir)
        mkt = (ctx["discovery"].get("market") or {}).get("region", "US")
        obs = [HIST.from_discovery(r, ctx["discovery_file"], mkt)
               for r in ctx["discovery"].get("candidates", []) + ctx["discovery"].get("failed", [])]
        amz = {str(a.get("product_id")): a for a in ctx.get("amazon") or []
               if a.get("status") not in ("failed", "malformed") and not a.get("calibration_only")}
        bvs = {str(b.get("product_id")): b for b in ctx.get("bvs") or [] if not b.get("calibration_only")}
        obs += [HIST.from_deep(d, ctx["deep_file"], amz.get(str(d["product_id"])), bvs.get(str(d["product_id"])))
                for d in self._ok_deep(ctx)]
        written, dup, paths = 0, 0, []
        for o in obs:
            if not o:
                continue
            o["data_environment"], o["run_id"] = self.env, self.run_id
            action, path = store.append(o)
            written += action == "written"
            if action in ("duplicate", "duplicate_snapshot"):
                dup += 1
                self.log.log("historical_storage", "DUPLICATE_SNAPSHOT_SKIPPED", product_id=o.get("identity_key"),
                             message=f"same provider snapshot already stored: {Path(path).name}")
            paths.append(str(path))
        store.rebuild_index()
        self._checkpoint("historical_storage", {"status": COMPLETED, "written": written, "duplicates": dup,
                                                "paths": paths})
        return COMPLETED, {"summary": f"{written} observation(s) appended, {dup} duplicate(s) skipped",
                           "written": written, "duplicates": dup}

    # ---- EMERGING
    def s_emerging(self, ctx, now):
        view = EnvStore(self.history_dir, self.env)
        det = EM.detect_all(view, now=now)
        det.update({"data_environment": self.env, "run_id": self.run_id})
        path, latest = EM.save(det, out_dir=self.processed / "emerging")
        self._checkpoint("emerging_detector", {"status": COMPLETED, "saved": str(path),
                                               "products_evaluated": det.get("products_evaluated", 0),
                                               "status_counts": det.get("status_counts")})
        return COMPLETED, {"summary": f"{det.get('products_evaluated', 0)} product(s) evaluated: "
                                      f"{det.get('status_counts')}", "saved": str(path)}

    # ---- REPORT
    def environment_violations(self, inputs):
        bad = []
        disc = inputs.get("discovery") or {}
        if disc and disc.get("data_environment") != self.env:
            bad.append(f"discovery file ({disc.get('data_environment')})")
        for r in (disc.get("candidates") or []) + (disc.get("failed") or []):
            if r.get("data_environment") != self.env:
                bad.append(f"discovery record {r.get('key')} ({r.get('data_environment')})")
        for k in ("deep", "amazon", "bvs"):
            for r in inputs.get(k) or []:
                if r.get("data_environment") != self.env:
                    bad.append(f"{k} record {r.get('product_id')} ({r.get('data_environment')})")
        return bad

    def s_report(self, ctx, now):
        d = ctx.get("discovery") or {}
        if not d or not (d.get("candidates") or d.get("failed") or self._ok_deep(ctx)):
            return SKIPPED, {"summary": "no products: report not written (latest report left unchanged)"}
        inputs = {"discovery": {**ctx["discovery"], "_file": ctx["discovery_file"]},
                  "deep": [{**r, "_file": ctx["deep_file"]} for r in self._ok_deep(ctx)],
                  "amazon": [{**r, "_file": ctx.get("amazon_file")} for r in ctx.get("amazon") or []
                             if r.get("status", "ok") not in ("failed", "malformed") and r.get("product_id")
                             and not r.get("calibration_only")],
                  "bvs": [{**r, "_file": ctx.get("bvs_file")} for r in ctx.get("bvs") or [] if r.get("product_id")
                          and not r.get("calibration_only")]}
        cal_rows = self.calibration_rows(ctx)
        bad = self.environment_violations(inputs)
        if bad:
            return BLOCKED, {"summary": f"report blocked: {len(bad)} record(s) not {self.env}", "violations": bad[:20]}
        cfg = GR.load_cfg()
        view = EnvStore(self.history_dir, self.env)
        rep = GR.build_report(inputs, cfg, now, view if view.identities() else None)
        import competitors as CI                      # Step X: read-only intelligence, not combined with scores
        everyone = rep.get("all_products") or (rep["top"] + rep["watch"] + rep["rejected"])
        import creatives as CR                        # Step Y: read-only creative intelligence
        if self.e2e:                                  # Step AA: the capped layer analyses of this run
            names = {str(p.get("product_id")): p.get("name") for p in everyone}
            keep = ("competitor_name", "competitor_domain", "relationship", "match_confidence_calc", "platform",
                    "selling_price", "compare_at_price", "meta_ads_present", "active_ads_count", "ad_age_days",
                    "ad_longevity", "offer_features", "review_count", "store_quality_score")
            rep["competitor_intelligence"] = [{"product_id": k, "name": names.get(k), "analysis": {
                kk: vv for kk, vv in a.items() if kk != "competitors"},
                "competitors": [{f: c.get(f) for f in keep} for c in a.get("competitors") or []]}
                for k, a in (ctx.get("competitor") or {}).items()]
            rep["creative_intelligence"] = [{"product_id": k, "name": names.get(k), "analysis": {
                kk: vv for kk, vv in a.items() if kk != "creatives"}} for k, a in (ctx.get("creative") or {}).items()]
        else:
            rep["competitor_intelligence"] = CI.report_rows(everyone, self.processed / "competitors",
                                                            self.history_dir / "competitors")
            rep["creative_intelligence"] = CR.report_rows(everyone, self.processed / "creatives",
                                                          self.history_dir / "creatives")
        pats = [p.lower() for p in cfg["secret_key_patterns"]]
        md = GR.render_markdown(rep, cfg)
        banner = (f"\n> Data environment: **{self.env}** · Run `{self.run_id}` · profile `{self.eff['profile_path']}` · "
                  f"BVS without supplier data stays incomplete (never estimated).\n" + self.report_banner_extra())
        first, _, rest = md.partition("\n")
        if cal_rows:
            rest += ("\n\n## Calibration lane (not ranked)\n\n> calibration_only: tests Amazon/AVS/BVS plumbing with "
                     "real data. These products did NOT meet the production thresholds and are never promoted.\n\n"
                     "| Product | WPS | Confidence | Amazon match | AVS | Amazon Conf. | BVS | BVS Conf. |\n"
                     "|---|---|---|---|---|---|---|---|\n" + "\n".join(
                         f"| {c['name']} | {c['wps']} | {c['confidence']} | {c['amazon_match_status']} | {c['avs']} | "
                         f"{c['amazon_confidence']} | {c['bvs']} | {c['bvs_confidence']} |" for c in cal_rows) + "\n")
        md = GR.scrub(first + "\n" + banner + rest, pats, self.secrets)
        js = GR.build_json(rep, cfg)
        js["report_metadata"].update({"data_environment": self.env, "run_id": self.run_id})
        js["calibration_lane"] = cal_rows
        js = GR.scrub(js, pats, self.secrets)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        md_path, js_path = GR.dated_paths(self.reports_dir, rep["summary"]["research_date"])
        with open(md_path, "x") as f:
            f.write(md)
        with open(js_path, "x") as f:
            json.dump(js, f, ensure_ascii=False, indent=2)
        latest = self.reports_dir / "latest-winning-products.md"
        latest.write_text(md)
        outs = {"report_markdown": str(md_path), "report_json": str(js_path), "report_latest": str(latest),
                "manifest": str(self.run_dir / "manifest.json"), "log": str(self.log.path)}
        if self.e2e:
            ctx["rep"] = rep                            # Step AA: decision runs as its own stage
        else:
            dec = self.final_decision(rep, now)         # Step Z: rules-based decision (no query, no purchase)
            outs.update({"final_decision_markdown": dec["markdown"], "final_decision_json": dec["json"],
                         "final_decision_latest": dec["latest"]})
        ctx.setdefault("outputs", {}).update(outs)
        s = rep["summary"]
        self._checkpoint("final_report", {"status": COMPLETED, "outputs": outs, "top": s["top_count"],
                                          "watchlist": s["watchlist_count"], "rejected": s["rejected_count"]})
        return COMPLETED, {"summary": f"Top {s['top_count']} | watchlist {s['watchlist_count']} | "
                                      f"rejected {s['rejected_count']}", **outs}

    def final_decision(self, rep, now):
        import decision_engine as DE
        comp = {str(r["product_id"]): r["analysis"] for r in rep.get("competitor_intelligence") or []}
        cre = {str(r["product_id"]): r["analysis"] for r in rep.get("creative_intelligence") or []}
        mv = self.processed / "manual_validation.json"
        manual = json.loads(mv.read_text()) if mv.exists() else None
        res = DE.run(rep.get("all_products") or (rep["top"] + rep["watch"] + rep["rejected"]), comp, cre,
                     manual=manual, expected_env=self.env, now=now,
                     runtime_path=ROOT / self.eff["profile_path"] if not Path(self.eff["profile_path"]).is_absolute()
                     else Path(self.eff["profile_path"]))
        res["metadata"]["run_id"] = self.run_id
        return DE.write_reports(res, self.reports_dir, secrets=self.secrets)

    # ================================================================== Step AA stages (no paid query)
    def layer_products(self, ctx, n):
        """Deterministic: discovery PASS before REVIEW, then WPS desc, WPS Confidence desc, product_id."""
        rank = {"PASS": 0, "REVIEW": 1}
        ok = sorted(self._ok_deep(ctx), key=lambda d: (rank.get((d.get("source") or {}).get("discovery_status"), 2),
                                                       -(d.get("wps") or 0), -(d.get("confidence") or 0),
                                                       str(d["product_id"])))
        return ok[:n]

    def s_suppliers(self, ctx, now):
        """Supplier research: configured sources only (today: offers already imported). No order, no contact."""
        import suppliers as SUP
        prods = self.layer_products(ctx, self.e2e["supplier_products_max"])
        if not prods:
            ctx["supplier_research"] = []
            return SKIPPED, {"summary": "no deep-analyzed product"}
        cfg = SUP.load_cfg()
        live_sources = sorted(k for k, v in (cfg.get("providers") or {}).items()
                              if isinstance(v, dict) and v.get("implemented") and k != "manual_import")
        cap = self.e2e["supplier_offers_per_product_max"]
        rows = []
        for d in prods:
            pid = str(d["product_id"])
            offers = SUP.load_offers(pid, self.processed / "suppliers")
            used = sorted(offers, key=lambda o: str(o.get("offer_id")))[:cap]
            rows.append({"product_id": pid, "name": d.get("product_name"),
                         "source": "manual_import" if offers else "NO_SOURCE_AVAILABLE",
                         "automated_sources_configured": live_sources, "offers_available": len(offers),
                         "offers_used": len(used), "offer_ids": [o.get("offer_id") for o in used],
                         "note": None if offers else "no supplier offer imported for this product and no automated "
                                                     "supplier provider configured — economics stay N/A"})
        ctx["supplier_research"] = rows
        with_data = sum(r["offers_used"] > 0 for r in rows)
        self._checkpoint("supplier_research", {"status": COMPLETED, "products": rows})
        return (COMPLETED if with_data == len(rows) else PARTIAL), {
            "summary": f"{with_data}/{len(rows)} product(s) with real supplier offers (max {cap} each); "
                       f"automated sources configured: {live_sources or 'none'}", "products": rows}

    def s_competitors(self, ctx, now):
        import competitors as CI
        prods = self.layer_products(ctx, self.e2e["competitor_products_max"])
        cap = self.e2e["competitors_per_product_max"]
        bvs = {str(b.get("product_id")): b for b in ctx.get("bvs") or []}
        out, rows = {}, []
        for d in prods:
            pid = str(d["product_id"])
            files = sorted((self.processed / "competitors" / pid).glob("competitors_*.json"))
            obs = sorted(CI.load_latest(pid, self.processed / "competitors"),
                         key=lambda c: str(c.get("competitor_id") or c.get("competitor_url") or ""))[:cap]
            if not obs:
                rows.append({"product_id": pid, "name": d.get("product_name"), "source": "NO_SOURCE_AVAILABLE",
                             "observations_used": 0})
                continue
            landed = ((bvs.get(pid) or {}).get("economics") or {}).get("landed_cost")
            a = CI.analyze_product(pid, d, obs, landed_cost=None if landed in (None, B.NA) else landed)
            comps = a.get("competitors") or []
            a["observed_at"] = max((c.get("observed_at") or "" for c in comps), default=None) or None
            a["source_files"] = [str(files[-1])]
            a["provenance_complete"] = bool(comps) and all(c.get("provenance") for c in comps
                                                           if c.get("relationship") == "DIRECT")
            CI.append_history(a, self.history_dir / "competitors")
            out[pid] = a
            rows.append({"product_id": pid, "name": d.get("product_name"), "source": "manual_import",
                         "observations_used": len(obs), "direct": a["direct_competitors"],
                         "saturation": a["saturation"]["score"], "opportunity": a["opportunity"]["score"],
                         "confidence": a["confidence"]["score"]})
        ctx["competitor"], ctx["competitor_rows"] = out, rows
        self._checkpoint("competitor_intelligence", {"status": COMPLETED, "products": rows})
        if not prods:
            return SKIPPED, {"summary": "no deep-analyzed product"}
        return (COMPLETED if len(out) == len(prods) else PARTIAL), {
            "summary": f"{len(out)}/{len(prods)} product(s) with competitor evidence (max {cap} each); no automated "
                       "competitor provider configured", "products": rows}

    def s_creatives(self, ctx, now):
        import creatives as CR
        prods = self.layer_products(ctx, self.e2e["creative_products_max"])
        cap = self.e2e["creatives_per_product_max"]
        proc = self.processed / "creatives"
        out, rows = {}, []
        for d in prods:
            pid = str(d["product_id"])
            raw = (d.get("source") or {}).get("raw_file")
            saved = {"accepted": 0}
            if raw and raw_env_ok(_read_json(raw) or {}, self.env):      # only this environment's saved answers
                saved = CR.import_from_kalopilot(pid, self.raw / "deep_analysis", proc)
            cs = sorted(CR.load_all(pid, proc), key=lambda c: str(c.get("creative_id")))[:cap]
            if not cs:
                rows.append({"product_id": pid, "name": d.get("product_name"), "source": "NO_SOURCE_AVAILABLE",
                             "creatives_used": 0})
                continue
            a = CR.analyze_product(pid, {"product_name": d.get("product_name"), "units": d.get("units")}, cs)
            a["observed_at"] = max((c.get("observed_at") or c.get("retrieved_at") or "" for c in cs), default=None) or None
            a["source_files"] = sorted({str(c.get("raw_source_location") or "").split("#")[0] for c in cs} - {""})
            a["provenance_complete"] = all(c.get("raw_source_location") and c.get("retrieved_at")
                                           for c in a["creatives"] if c.get("qualified"))
            CR.append_history(a, self.history_dir / "creatives")
            out[pid] = a
            srcs = sorted({c.get("source") for c in cs if c.get("source")})
            rows.append({"product_id": pid, "name": d.get("product_name"), "source": ", ".join(srcs) or "manual_import",
                         "saved_kalopilot_imported": saved.get("accepted", 0), "creatives_used": len(cs),
                         "qualified": a["qualified_creatives"], "saturation": a["saturation"]["score"],
                         "opportunity": a["opportunity"]["score"], "confidence": a["confidence"]["score"]})
        ctx["creative"], ctx["creative_rows"] = out, rows
        self._checkpoint("creative_intelligence", {"status": COMPLETED, "products": rows})
        if not prods:
            return SKIPPED, {"summary": "no deep-analyzed product"}
        return (COMPLETED if len(out) == len(prods) else PARTIAL), {
            "summary": f"{len(out)}/{len(prods)} product(s) with creative evidence (max {cap} each; saved KaloPilot "
                       "top videos are free)", "products": rows}

    def s_final_decision(self, ctx, now):
        import decision_engine as DE
        rep = ctx.get("rep")
        if not rep:
            return SKIPPED, {"summary": "no report data"}
        analyzed = {str(d["product_id"]) for d in self._ok_deep(ctx)}
        prods = [p for p in rep.get("all_products") or [] if str(p.get("product_id")) in analyzed]
        prods = sorted(prods, key=lambda p: str(p.get("product_id")))[: self.e2e["final_decision_max_products"]]
        mv = self.processed / "manual_validation.json"
        manual = json.loads(mv.read_text()) if mv.exists() else None
        res = DE.run(prods, ctx.get("competitor") or {}, ctx.get("creative") or {}, manual=manual,
                     expected_env=self.env, now=now, require_trust=True,
                     runtime_path=self.root / self.eff["profile_path"] if not Path(self.eff["profile_path"]).is_absolute()
                     else Path(self.eff["profile_path"]),
                     confidence_penalties=self.degraded_penalties(prods, ctx),
                     config_version=getattr(self, "config_version", None))
        res["metadata"]["run_id"] = self.run_id
        out = DE.write_reports(res, self.reports_dir, secrets=self.secrets)
        ctx["decision"] = res
        ctx.setdefault("outputs", {}).update({"final_decision_markdown": out["markdown"],
                                              "final_decision_json": out["json"],
                                              "final_decision_latest": out["latest"]})
        counts = {}
        for d in res["decisions"]:
            counts[d["decision_state"]] = counts.get(d["decision_state"], 0) + 1
        self._checkpoint("final_decision", {"status": COMPLETED, "outputs": out, "states": counts,
                                            "shortlist": res["shortlist"]})
        return COMPLETED, {"summary": f"{len(res['decisions'])} decided: {counts}; shortlist "
                                      f"{[s['name'] for s in res['shortlist']] or 'empty'}", **out}

    def s_aa_validation(self, ctx, now):
        import aa_live as AA
        try:
            self.balance_end = self._balance()                  # FREE endpoint
        except ProviderFailure:
            self.balance_end = self.balance_now
        self._save_manifest()
        out = AA.finalize(self, ctx, now)
        ctx.setdefault("outputs", {}).update(out["paths"])
        self.manifest["aa_result"] = out["result"]
        self._checkpoint("aa_validation", {"status": COMPLETED, "result": out["result"], "issues": out["issues"],
                                           "outputs": out["paths"]})
        return COMPLETED, {"summary": out["result"] + (f" — {len(out['issues'])} issue(s)" if out["issues"] else ""),
                           **out["paths"]}

    # ------------------------------------------------------------------ status / summary
    def overall_status(self):
        st = {s: self.stages.get(s, {}).get("status") for s in self.stage_list}
        if any(st[s] == FAILED for s in REQUIRED_STAGES):
            return FAILED
        if any(st[s] == BLOCKED for s in REQUIRED_STAGES):
            resumable = self.stop_paid and self.stop_paid[0] in ("insufficient_credits", "run_credit_cap")
            return PARTIAL if resumable and st.get("discovery") in (COMPLETED, PARTIAL) else BLOCKED
        if any(st[s] == PARTIAL for s in REQUIRED_STAGES) or self.stop_paid or self.provider_down:
            return PARTIAL
        if st.get("discovery") == SKIPPED or st.get("deep_analysis") == SKIPPED:
            return PARTIAL
        return COMPLETED

    def _summary_counts(self, ctx):
        disc = (ctx.get("discovery") or {}).get("summary") or {}
        return {"discovered": disc.get("unique"), "pass": disc.get("PASS"), "review": disc.get("REVIEW"),
                "fail": disc.get("FAIL"), "deep_analyzed_ok": len(self._ok_deep(ctx)),
                "deep_failed": sum(r.get("status") != "ok" for r in ctx.get("deep") or []),
                "amazon_validated": sum(r.get("status") not in ("failed", "malformed") for r in ctx.get("amazon") or []),
                "bvs_evaluated": len(ctx.get("bvs") or []),
                "stop_reason": self.stop_paid[1] if self.stop_paid else None, "provider_failure": self.provider_down}

    def summary(self, blocks=None):
        m = self.manifest
        acc = self.budget.summary()
        return {"run_id": self.run_id, "mode": m["mode"], "market": m["market"], "data_environment": m["data_environment"],
                "profile": m["profile"]["path"], "final_status": m["final_status"], "blocking_reasons": blocks or [],
                "stages": {s: v.get("status") for s, v in self.stages.items()},
                "stage_details": {s: v.get("summary") for s, v in self.stages.items()},
                "products": m.get("summary"), "query_budget": m.get("query_budget"),
                "queries": {"planned": acc["planned_queries"], "executed": acc["executed_queries"],
                            "cached": sum(c["CACHE_HIT"] for c in self.counts.values()),
                            "blocked": acc["blocked_queries"], "failed": acc["failed_queries"],
                            "by_stage": self.counts},
                "credits": {"estimated": (m.get("query_budget") or {}).get("estimated_credits_max", safety.UNKNOWN),
                            "used_reported": round(self.spent_known, 2) if self.budget.executed else 0.0,
                            "remaining": self.balance_now if self.balance_now is not None else safety.UNKNOWN},
                "outputs": {**m.get("outputs", {}), "manifest": str(self.run_dir / "manifest.json"),
                            "log": str(self.log.path)}}


# ====================================================================== status / report helpers
def latest_run(runs_dir, mode=None):
    runs = sorted(p for p in Path(runs_dir).glob("*/manifest.json"))
    runs = [p for p in runs if mode is None or (_read_json(p) or {}).get("mode") == mode]
    return runs[-1].parent.name if runs else None


def run_status(runs_dir, run_id=None):
    run_id = run_id or latest_run(runs_dir)
    if not run_id:
        return None
    return _read_json(Path(runs_dir) / run_id / "manifest.json")
