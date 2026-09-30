"""Step AC — Production Runner: the one canonical production command.

    python -m winning_product_agent production-run                 # DRY RUN (default, never paid)
    python -m winning_product_agent production-run --live          # needs: CONFIRM PRODUCTION LIVE RUN
    python -m winning_product_agent production-run --live --resume RUN_ID
    python -m winning_product_agent production-run --report-only [--run-id RUN_ID]

* Uses ONLY the active immutable production config set (config/production/vN, hashes in manifest.json).
  Hashes are verified before the run and before every stage: any drift stops the run (run_invalid).
* Data environment PRODUCTION; data under data/production/, runs under runs/production/, reports under
  reports/production/. Never mixed with calibration (LIVE) or SYNTHETIC data.
* Provider health check before any paid query; deterministic degraded-mode rules from the config.
* Cost guardrails (queries, provider queries, failed answers, credits) stop new paid queries and keep
  completed data (PARTIAL report). Nothing is retried automatically.
* Post-run audit: RUN_VALIDATED or RUN_REVIEW_REQUIRED. A failed audit never re-buys queries.
"""
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from . import runner as R

import config_resolver as CR  # noqa: E402  (scripts/ is on sys.path via runner)
import promotion as PROMO  # noqa: E402
import safety  # noqa: E402

PRODUCTION = "PRODUCTION"
NOT_MEASURED = "NOT_MEASURED"
AVAILABLE, DEGRADED, UNAVAILABLE = "AVAILABLE", "DEGRADED", "UNAVAILABLE"
RUN_VALIDATED, RUN_REVIEW = "RUN_VALIDATED", "RUN_REVIEW_REQUIRED"
PRODUCTION_STAGES = ["preflight", "provider_health", "discovery", "filtering", "deep_analysis", "wps", "wps_confidence",
                     "amazon_validation", "supplier_research", "bvs", "competitor_intelligence", "creative_intelligence",
                     "historical_storage", "emerging_detector", "final_report", "final_decision",
                     "validation_packs", "production_history", "post_run_audit", "run_summary"]
R.CHECKPOINTS.update({"validation_packs": "validation_packs.json", "provider_health": "provider_health.json", "production_history": "production_history.json",
                      "post_run_audit": "post_run_audit.json"})


def _hashes(d):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(d).glob("*")) if p.is_file()}


class ProductionRunner(R.Runner):
    def __init__(self, root=R.ROOT, data_root=None, version=None, config_dir=None, profile_path=None, client=None,
                 max_products=None, preflight_fn=None, input_fn=None, isatty=None, out=None, now=None):
        self.config_dir = Path(config_dir) if config_dir else PROMO.active_dir(root, version)
        self.config_manifest = json.loads((self.config_dir / "manifest.json").read_text())
        self.config_version = self.config_manifest["config_version"]
        self.lock_verify = PROMO.verify(self.config_dir)            # drift before start
        self.lock_hashes = _hashes(self.config_dir)
        self.health = None
        self.report_only_source = None
        with CR.active(self.config_dir):
            super().__init__(profile_path=None, root=root, data_root=data_root, client=client,
                             max_products=max_products, preflight_fn=preflight_fn, input_fn=input_fn, isatty=isatty,
                             out=out, now=now)
            self.rt = safety.load_runtime()                         # production runtime (safe defaults in file)
            self.eff, self.profile_errors = self.production_effective(profile_path)
        p = self.rt["paths"]
        self.raw, self.processed = self.data_root / p["raw"], self.data_root / p["processed"]
        self.history_dir, self.reports_dir = self.data_root / p["history"], self.data_root / p["reports"]
        self.runs_dir = self.data_root / p["runs"]

    # ------------------------------------------------------------------ config
    def production_effective(self, profile_path):
        rt = self.rt
        eff = copy.deepcopy(rt)
        errors = []
        # production is a live-capable profile; the FILE keeps safe defaults and the gate reads file + in-memory
        # overrides, which are only set after the exact confirmation.
        eff["runtime"] = {**rt["runtime"], "live_mode": True, "dry_run": False, "explicit_live_confirmation": False}
        eff["profile_name"] = self.config_version
        rel = self.config_dir / "runtime.yaml"
        try:
            eff["profile_path"] = str(rel.relative_to(self.root))
        except ValueError:
            eff["profile_path"] = str(rel)
        eff["profile_hash"] = hashlib.sha256(rel.read_bytes()).hexdigest()[:16]
        if profile_path:                                   # optional profile: may only LOWER production limits
            import yaml
            p = Path(profile_path)
            p = p if p.is_absolute() else self.root / p
            prof = yaml.safe_load(p.read_text()) or {}
            for k in ("limits", "e2e"):
                for kk, v in (prof.get(k) or {}).items():
                    cur = (eff.get(k) or {}).get(kk)
                    if isinstance(cur, int) and not isinstance(cur, bool) and isinstance(v, int) and v > cur:
                        errors.append(f"profile {k}.{kk}={v} exceeds production {cur}")
                    elif kk in (eff.get(k) or {}) or kk == "first_production_run":
                        eff[k][kk] = v
            # caps: maximums for THIS run; the effective value is min(production config, cap) - never raised
            applied = {}
            for k in ("limits", "e2e"):
                for kk, cap in ((prof.get("caps") or {}).get(k) or {}).items():
                    cur = (eff.get(k) or {}).get(kk)
                    if isinstance(cur, int) and isinstance(cap, int):
                        eff[k][kk] = min(cur, cap)
                        applied[f"{k}.{kk}"] = {"production": cur, "cap": cap, "effective": eff[k][kk]}
            eff["run_caps"] = applied
            eff["run_profile"] = {"path": str(p), "name": prof.get("name"), "note": prof.get("note")}
            eff["profile_path"] = str(rel.relative_to(self.root)) if str(rel).startswith(str(self.root)) else str(rel)
        errors += R.validate_effective(rt, eff)
        return eff, errors

    @property
    def stage_list(self):
        return PRODUCTION_STAGES

    @property
    def phrase(self):
        return R.PRODUCTION_PHRASE

    def environment_for(self, client):
        env = getattr(client, "data_environment", R.SYNTHETIC)
        return PRODUCTION if env == R.LIVE else env                # tests keep SYNTHETIC (temp workspace only)

    def report_banner_extra(self):
        return (f"\n**CONFIG VERSION:** {self.config_version} · **RUN ID:** {self.run_id} · "
                f"**DATA ENVIRONMENT:** {self.env}\n")

    def drift(self):
        now = _hashes(self.config_dir)
        changed = sorted(k for k in set(now) | set(self.lock_hashes) if now.get(k) != self.lock_hashes.get(k))
        return changed

    def pre_stage_check(self, name):
        changed = self.drift()
        if changed:
            return f"CONFIG_DRIFT: production config changed after the run started ({changed}); run invalid"
        return None

    def _save_manifest(self):
        path = super()._save_manifest()
        if getattr(self, "mode", None) and self.mode != "DRY_RUN":      # Stage AD-7: data/production/runs/{run_id}
            R.write_json_atomic(self.data_root / "data" / "production" / "runs" / self.run_id / "manifest.json",
                                {**self.manifest, "manifest_copy_of": str(path)}, self.secrets)
        return path

    def _init_run(self, mode, env, resume_id=None):
        super()._init_run(mode, env, resume_id)
        self.manifest.update({"config_version": self.config_version, "config_dir": str(self.config_dir),
                              "runtime_mode": mode, "run_caps": self.eff.get("run_caps"),
                              "run_profile": self.eff.get("run_profile"),
                              "config_manifest_hashes": self.config_manifest["files"],
                              "config_hashes_at_start": self.lock_hashes,
                              "production_data_paths": self.rt["paths"]})
        if self.health:
            self.manifest["provider_health"] = self.health

    # ------------------------------------------------------------------ provider health / degraded mode
    def provider_health(self, client):
        import yaml
        dm = self.rt.get("degraded_mode") or {}
        out = {}
        creds = safety.credentials_present(self.rt)
        kp = {"status": UNAVAILABLE, "detail": "credentials not found (value never shown)"}
        if creds:
            try:
                bal = client.credits()["totalRemain"]                 # FREE endpoint, no query
                reserve = self.rt["safety"]["min_balance_reserve"]
                kp = {"status": AVAILABLE if bal - reserve > 0 else DEGRADED, "balance": bal,
                      "detail": "credentials present; free credit endpoint answered"}
                if bal - reserve <= 0:
                    kp["detail"] = f"balance {bal} at or below the reserve {reserve}"
            except Exception as e:  # noqa: BLE001
                kp = {"status": UNAVAILABLE, "detail": safety.redact(f"credit endpoint failed: {e.__class__.__name__}",
                                                                     self.secrets if hasattr(self, "secrets") else [])}
        kp["authentication"] = ("AUTHENTICATED" if kp["status"] != UNAVAILABLE else
                                 "CREDENTIALS_PRESENT_NOT_VERIFIED" if creds else "NO_CREDENTIALS")
        kp["capability"] = "discovery + deep analysis + Amazon validation (provider_capabilities.yaml)"
        kp["last_successful_query"] = self.last_successful_query()
        out["kalopilot"] = kp
        with CR.active(self.config_dir):
            amz = yaml.safe_load(Path(CR.path("amazon_validation.yaml")).read_text()) or {}
            sup = yaml.safe_load(Path(CR.path("suppliers.yaml")).read_text()) or {}
            comp = yaml.safe_load(Path(CR.path("competitors.yaml")).read_text()) or {}
            cre = yaml.safe_load(Path(CR.path("creatives.yaml")).read_text()) or {}
        amz_on = (amz.get("amazon_validation") or amz).get("enabled", True)
        out["amazon"] = ({"status": UNAVAILABLE, "detail": "amazon_validation disabled"} if not amz_on else
                         {"status": UNAVAILABLE, "detail": "needs KaloPilot"} if kp["status"] == UNAVAILABLE else
                         {"status": DEGRADED, "detail": "via KaloPilot; reliability never measured live (AB)"})

        def layer(cfg, name, files):
            impl = sorted(k for k, v in (cfg.get("providers") or {}).items() if isinstance(v, dict) and v.get("implemented"))
            auto = [x for x in impl if x != "manual_import"]
            n = len(list(files)) if files is not None else 0
            if auto:
                return {"status": AVAILABLE, "sources": impl}
            if impl:
                return {"status": DEGRADED, "sources": impl, "imported_files": n,
                        "detail": f"manual import only (no automated {name} provider configured)"}
            return {"status": UNAVAILABLE, "sources": [], "detail": f"no {name} provider implemented"}
        sd, cd = self.processed / "suppliers", self.processed / "competitors"
        out["supplier"] = layer(sup, "supplier", sd.rglob("offers_*.json") if sd.exists() else None)
        out["competitor"] = layer(comp, "competitor", cd.rglob("competitors_*.json") if cd.exists() else None)
        cimpl = sorted(k for k, v in (cre.get("providers") or {}).items() if isinstance(v, dict) and v.get("implemented"))
        out["creative"] = ({"status": AVAILABLE, "sources": cimpl, "detail": "saved KaloPilot top videos + imports"}
                           if kp["status"] != UNAVAILABLE and "kalopilot_saved" in cimpl else
                           {"status": DEGRADED if cimpl else UNAVAILABLE, "sources": cimpl})
        for k in ("amazon", "supplier", "competitor", "creative"):
            out[k].setdefault("authentication", "via KaloPilot" if k in ("amazon", "creative") else "not required (manual)")
            out[k].setdefault("capability", {"amazon": "KaloPilot Amazon prompt (never measured live)",
                                             "supplier": "manual_import only", "competitor": "manual_import only",
                                             "creative": "saved KaloPilot top_videos (no hooks / angles / dates) + "
                                                         "manual_import"}[k])
            out[k].setdefault("last_successful_query", NOT_MEASURED if k == "amazon" else "N/A (no paid query)")
        decisions, blocks = [], []
        for prov, h in out.items():
            rule = dm.get(prov) or {}
            st = h["status"]
            if st == AVAILABLE:
                continue
            action = rule.get("on_unavailable" if st == UNAVAILABLE else "on_degraded", "CONTINUE")
            if prov == "amazon" and rule.get("mandatory") and st == UNAVAILABLE:
                action = "BLOCK_RUN"
            decisions.append({"provider": prov, "status": st, "action": action, "effect": rule.get("effect")})
            if action == "BLOCK_RUN":
                blocks.append(f"{prov} {st}: degraded-mode rule BLOCK_RUN")
        dec_rules = {}
        with CR.active(self.config_dir):
            import decision_engine as DE
            dec_rules = DE.load_cfg()
        if out["supplier"]["status"] == UNAVAILABLE and not dec_rules["decision"]["ready"].get("require_supplier_economics"):
            blocks.append("supplier UNAVAILABLE but decision rules do not require supplier economics for READY")
        return {"providers": out, "degraded_mode_decisions": decisions, "blocks": blocks,
                "degraded_effects": self.degraded_effects(out),
                "checked_at": datetime.now(timezone.utc).isoformat()}

    def last_successful_query(self):
        """Most recent usable paid answer: production runs first; calibration runs labelled as such."""
        best = None
        for base, label in ((self.runs_dir, "production"), (self.root / "runs", "calibration (LIVE)")):
            for mf in base.glob("*/manifest.json") if base.exists() else []:
                m = R._read_json(mf) or {}
                if m.get("mode") != "LIVE":
                    continue
                for q in m.get("query_log") or []:
                    if q.get("action") == "LIVE_QUERY" and not q.get("error_category"):
                        ts = q.get("timestamp")
                        if ts and (best is None or ts > best[0]):
                            best = (ts, label, q.get("stage"))
        return {"timestamp": best[0], "source": best[1], "stage": best[2]} if best else "none recorded"

    def degraded_effects(self, prov):
        st = {k: v["status"] for k, v in prov.items()}
        pen = ((self.rt.get("degraded_mode") or {}).get("competitor") or {}).get("decision_confidence_penalty", 0)
        return {
            "WPS": "unaffected (TikTok data only)" if st["kalopilot"] == AVAILABLE else "BLOCKED: KaloPilot unavailable",
            "AVS": ("computed only for eligible products (WPS >= 70, Confidence >= 60); Amazon never measured live, "
                    "matches are not forced" if st["amazon"] != UNAVAILABLE else "N/A -> Cross-Platform Demand UNKNOWN"),
            "BVS": ("partial: real supplier economics only for products with imported offers; otherwise "
                    "INSUFFICIENT_SUPPLIER_DATA (never estimated)" if st["supplier"] != AVAILABLE else "full inputs"),
            "Competitor Intelligence": ("only for products with imported competitor research; otherwise Competitive "
                                        "Environment UNKNOWN" if st["competitor"] != AVAILABLE else "available"),
            "Creative Intelligence": "saved KaloPilot top videos (free) + imports; without hook / angle / date the "
                                     "creative confidence stays <= 60 < minimum 65 -> Creative Opportunity UNKNOWN",
            "Decision Confidence": (f"-{pen} for every product without competitor evidence (competitor provider "
                                    f"{st['competitor']})" if st["competitor"] != AVAILABLE and pen else "no penalty"),
            "READY_FOR_PRODUCT_VALIDATION eligibility": (
                "requires supplier economics (imported offers), competitor evidence and classified creative evidence; "
                "with the current sources NO product can reach READY unless those are imported before the run"
                if st["supplier"] != AVAILABLE or st["competitor"] != AVAILABLE else "normal rules"),
        }

    def extra_live_blocks(self):
        b = []
        if self.health:
            st = R.BLOCKED if self.health["blocks"] else R.COMPLETED
            self.stages["provider_health"] = {"status": st, "summary": ", ".join(
                f"{k} {v['status']}" for k, v in self.health["providers"].items())}
            self._checkpoint("provider_health", {"status": st, **self.health})
        if not self.lock_verify["ok"]:
            b.append(f"production config {self.config_version} failed hash verification: {self.lock_verify}")
        b += self.health["blocks"] if self.health else ["provider health check not run"]
        if self.env not in (PRODUCTION, R.SYNTHETIC):
            b.append(f"production run in environment {self.env}")
        if any("production" not in str(v) for v in self.rt["paths"].values()):
            b.append("production paths are not separated")
        m = self.manifest
        if m.get("resumed_at") and m.get("config_version") != self.config_version:
            b.append("resume must use the same production config version")
        return b

    def degraded_penalties(self, prods, ctx):
        rule = (self.rt.get("degraded_mode") or {}).get("competitor") or {}
        st = ((self.health or {}).get("providers") or {}).get("competitor", {}).get("status", AVAILABLE)
        pts = rule.get("decision_confidence_penalty") or 0
        if st == AVAILABLE or not pts:
            return {}
        have = set((ctx.get("competitor") or {}).keys())
        return {str(p.get("product_id")): [{"reason": f"competitor provider {st} and no competitor evidence "
                                                      "(degraded mode)", "points": pts}]
                for p in prods if str(p.get("product_id")) not in have}

    # ------------------------------------------------------------------ steps
    def e2e_steps(self):
        steps = [s for s in super().e2e_steps() if s[0] != "aa_validation"]
        return steps + [("validation_packs", self.s_validation_packs), ("production_history", self.s_production_history),
                        ("post_run_audit", self.s_post_run_audit)]

    def _readiness(self, pf, budget, balance):
        r = super()._readiness(pf, budget, balance)
        if r["verdict"] == "READY_FOR_FIRST_LIVE_RUN":
            r["verdict"] = "READY_FOR_PRODUCTION_LIVE_RUN"
        return r

    def _planned_stages(self, budget):
        rows = [r for r in super()._planned_stages(budget) if r["stage"] != "aa_validation"]
        for r in rows:
            r["does"] = r["does"].replace("LIVE only", "PRODUCTION data only").replace("append LIVE", "append PRODUCTION")
        rows.insert(1, {"stage": "provider_health", "does": "KaloPilot / Amazon / supplier / competitor / creative "
                                                            "health + degraded-mode rules", "paid": 0})
        i = [r["stage"] for r in rows].index("final_decision") + 1
        rows[i:i] = [{"stage": "validation_packs", "does": "validation-pack.md for every shortlisted product", "paid": 0},
                     {"stage": "production_history", "does": "append decision observations (append-only)", "paid": 0},
                     {"stage": "post_run_audit", "does": "score / mapping / provenance / cost / secret / decision audit",
                      "paid": 0}]
        return rows

    def s_validation_packs(self, ctx, now):
        import validation_pack as VP
        res = ctx.get("decision")
        if not res or not res["shortlist"]:
            self._checkpoint("validation_packs", {"status": R.SKIPPED, "packs": []})
            return R.SKIPPED, {"summary": "no READY_FOR_PRODUCT_VALIDATION product: no validation pack"}
        prods = {str(p.get("product_id")): p for p in (ctx.get("rep") or {}).get("all_products") or []}
        paths = []
        for s_ in res["shortlist"]:
            d = next(x for x in res["decisions"] if x["product_id"] == s_["product_id"])
            paths.append(str(VP.write(self, d, prods.get(d["product_id"]) or {}, ctx, now)))
        ctx.setdefault("outputs", {}).update({f"validation_pack_{i + 1}": p for i, p in enumerate(paths)})
        self._checkpoint("validation_packs", {"status": R.COMPLETED, "packs": paths})
        return R.COMPLETED, {"summary": f"{len(paths)} validation pack(s)", "packs": paths}

    def s_production_history(self, ctx, now):
        res = ctx.get("decision")
        if not res:
            return R.SKIPPED, {"summary": "no decisions"}
        base = self.history_dir / "decisions"
        written = dup = 0
        for d in res["decisions"]:
            ev = d["evidence"]
            v = lambda k: (ev["values"].get(k) or {}).get("value")  # noqa: E731
            rec = {"product_id": d["product_id"], "name": d["name"], "decision_state": d["decision_state"],
                   "dimensions": d["dimension_status"], "decision_confidence": d["decision_confidence"]["score"],
                   "metrics": {k: v(k) for k in ("wps", "wps_confidence", "momentum_score", "momentum_confidence",
                                                 "avs", "amazon_confidence", "bvs", "bvs_confidence", "supplier_quality",
                                                 "supplier_confidence", "competitor_saturation", "competitor_opportunity",
                                                 "competitor_confidence", "creative_saturation", "creative_opportunity",
                                                 "creative_confidence")},
                   "config_version": self.config_version}
            h = hashlib.sha256(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest()
            f = base / f"{d['product_id']}.jsonl"
            f.parent.mkdir(parents=True, exist_ok=True)
            seen = {json.loads(x).get("snapshot_hash") for x in f.read_text().splitlines()} if f.exists() else set()
            if h in seen:
                dup += 1
                continue
            with open(f, "a") as fh:                                  # append-only
                fh.write(json.dumps({**rec, "snapshot_hash": h, "run_id": self.run_id, "data_environment": self.env,
                                     "observed_at": now.isoformat()}, default=str) + "\n")
            written += 1
        self._checkpoint("production_history", {"status": R.COMPLETED, "written": written, "duplicates": dup})
        return R.COMPLETED, {"summary": f"{written} decision observation(s) appended, {dup} unchanged snapshot(s) skipped"}

    def s_post_run_audit(self, ctx, now):
        import production_audit as PA
        try:
            self.balance_end = self._balance() if self.env != "REPORT_ONLY" and self.client else self.balance_now
        except R.ProviderFailure:
            self.balance_end = self.balance_now
        self._save_manifest()
        out = PA.audit(self, ctx, now)
        ctx.setdefault("outputs", {}).update(out["paths"])
        self.manifest["post_run_audit"] = {"result": out["result"], "issues": out["issues"],
                                           "first_production_run_status": out.get("first_production_run_status"),
                                           "critical_issues": out.get("critical_issues")}
        self._checkpoint("post_run_audit", {"status": R.COMPLETED, **out})
        return R.COMPLETED, {"summary": out["result"] + (f" — {len(out['issues'])} issue(s)" if out["issues"] else ""),
                             **out["paths"]}

    # ------------------------------------------------------------------ entry points
    def confirmation_text(self, budget, balance):
        lim, e = self.limits, self.e2e
        h = self.health or {}
        L = ["", "=" * 70, "  PRODUCTION LIVE RUN — PAID KALOPILOT QUERIES", "=" * 70,
             f"  Market:            {self.eff['runtime']['market']}",
             f"  Config version:    {self.config_version}",
             f"  Discovery limit:   {lim['discovery_max_products']}",
             f"  Deep-analysis:     {lim['deep_analysis_max_products']}",
             f"  Amazon:            {lim['amazon_validation_max_products']}",
             f"  Supplier:          {e['supplier_products_max']} products x {e['supplier_offers_per_product_max']} offers",
             f"  Competitor:        {e['competitor_products_max']} products x {e['competitors_per_product_max']}",
             f"  Creative:          {e['creative_products_max']} products x {e['creatives_per_product_max']}",
             f"  Estimated queries: {budget['expected_paid_queries_min']}–{budget['expected_paid_queries_max']}",
             f"  Estimated credits: {budget['estimated_credits_min']}–{budget['estimated_credits_max']} "
             "(configured estimate, not a quote)",
             f"  Guardrails:        {json.dumps(self.eff['query_plan'].get('guardrails'))}",
             f"  Run credit cap:    {budget['max_credits_for_run']} | reserve {budget['min_balance_reserve']} | balance {balance}"]
        for x in h.get("degraded_mode_decisions") or []:
            L.append(f"  Degraded: {x['provider']} {x['status']} -> {x['action']}")
        L += ["=" * 70, f"  Type exactly  {self.phrase}  to continue (anything else cancels).", ""]
        return "\n".join(L)

    def dry_run(self, check_balance=True, preflight_result=None):
        with CR.active(self.config_dir):
            self.health = self.provider_health(self.client or R.KaloClient())
            ok = self.health["providers"]["kalopilot"]["status"] != UNAVAILABLE
            out = super().dry_run(check_balance=ok, preflight_result=preflight_result)
        out.update({"config_version": self.config_version, "config_dir": str(self.config_dir),
                    "config_hash_verification": self.lock_verify, "provider_health": self.health["providers"],
                    "degraded_mode_decisions": self.health["degraded_mode_decisions"],
                    "degraded_effects": self.health["degraded_effects"], "run_caps": self.eff.get("run_caps"),
                    "health_blocks": self.health["blocks"],
                    "balance": self.health["providers"]["kalopilot"].get("balance", out.get("balance")),
                    "product_limits": {**self.limits, **{k: v for k, v in self.e2e.items() if k.endswith("_max")}},
                    "guardrails": self.eff["query_plan"].get("guardrails"),
                    "report_destinations": {
                        "winning_products": str(self.reports_dir / "YYYY-MM-DD-winning-products.md"),
                        "final_decision": str(self.reports_dir / "YYYY-MM-DD-final-decision.md"),
                        "latest": [str(self.reports_dir / "latest-winning-products.md"),
                                   str(self.reports_dir / "latest-final-decision.md")],
                        "run_audit": str(self.reports_dir / "YYYY-MM-DD-run-audit.md"),
                        "winning_products_json": str(self.reports_dir / "YYYY-MM-DD-winning-products.json"),
                        "validation_packs": str(self.reports_dir / "YYYY-MM-DD" / "<product_id>-validation-pack.md"),
                        "run_manifest": str(self.data_root / "data" / "production" / "runs" / "<run_id>" /
                                            "manifest.json")},
                    "data_paths": self.rt["paths"]})
        if not self.lock_verify["ok"]:
            out["blocking_issues"].append("production config hash verification failed")
        out["blocking_issues"] += self.health["blocks"]
        out["status"] = "DRY_RUN_COMPLETED" if not out["blocking_issues"] else "BLOCKED"
        self.manifest.update({"provider_health": self.health, "final_status": out["status"]})
        self._save_manifest()
        return out

    def live(self, confirm_value=None, resume_id=None):
        with CR.active(self.config_dir):
            client = self.client or R.KaloClient()
            self.client = client
            self.health = self.provider_health(client)
            return super().live(confirm_value=confirm_value, resume_id=resume_id)

    def report_only(self, run_id=None, now=None):
        """Rebuild reports + decision + audit from a finished production run. Never a paid query."""
        now = now or self.now or R.now_utc()
        runs = sorted(p.parent.name for p in self.runs_dir.glob("*/manifest.json")
                      if (R._read_json(p) or {}).get("mode") == "LIVE")
        src = run_id or (runs[-1] if runs else None)
        if not src or not (self.runs_dir / src / "manifest.json").exists():
            raise SystemExit("no production live run to report on")
        sm = R._read_json(self.runs_dir / src / "manifest.json")
        with CR.active(self.config_dir):
            self.health = self.provider_health(self.client or R.KaloClient()) if self.client else None
            self._init_run("REPORT_ONLY", sm.get("data_environment"))
            self.manifest["report_only_source_run"] = src
            self.stop_paid = ("report_only", "report-only run: paid queries are disabled")
            ctx = {}
            src_ck, own_ck = self.runs_dir / src / "checkpoints", self.ck_dir
            self.ck_dir = src_ck
            for name in ("discovery", "filtering", "deep_analysis", "amazon_validation"):
                self._restore(name, ctx)
            self.ck_dir = own_ck
            for name in ("discovery", "filtering", "deep_analysis", "wps", "wps_confidence", "amazon_validation"):
                self.stages[name] = {"status": R.SKIPPED, "summary": f"restored from {src} (no query)"}
            steps = [("supplier_research", self.s_suppliers), ("bvs", self.s_bvs),
                     ("competitor_intelligence", self.s_competitors), ("creative_intelligence", self.s_creatives),
                     ("emerging_detector", self.s_emerging), ("final_report", self.s_report),
                     ("final_decision", self.s_final_decision), ("post_run_audit", self.s_post_run_audit)]
            for name, fn in steps:
                err = self.pre_stage_check(name)
                if err:
                    self.stages[name] = {"status": R.BLOCKED, "summary": err}
                    self.manifest["run_invalid"] = err
                    break
                try:
                    status, detail = fn(ctx, now)
                except Exception as e:  # noqa: BLE001
                    status, detail = R.FAILED, {"summary": safety.redact(f"{e.__class__.__name__}: {e}", self.secrets)}
                self._set(name, status, **detail)
            self.manifest.update({"final_status": "REPORT_ONLY_COMPLETED" if not self.manifest.get("run_invalid")
                                  else "INVALID", "completed_at": R.now_utc().isoformat(),
                                  "outputs": ctx.get("outputs", {})})
            self.stages["run_summary"] = {"status": R.COMPLETED}
            self._save_manifest()
        return {"run_id": self.run_id, "source_run": src, "stages": {k: v.get("status") for k, v in self.stages.items()},
                "outputs": ctx.get("outputs", {}), "final_status": self.manifest["final_status"]}
