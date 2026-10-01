"""Step AC — promote calibrated config changes into an immutable, versioned production config set.

    python -m winning_product_agent production-config review          # change review only (no write)
    python -m winning_product_agent production-config promote         # review + write config/production/vN/
    python -m winning_product_agent production-config verify [vN]     # hashes vs manifest (drift check)
    python -m winning_product_agent production-config list
    python -m winning_product_agent production-config activate vN     # rollback / roll forward (nothing deleted)

Rules:
  * Every proposed change is reviewed individually: APPROVE / REJECT / DEFER. Only APPROVE is promoted.
  * APPROVE requires: live evidence, no loss of data integrity, no lowered confidence requirement, no reliance
    on unreliable provider fields, offline replay intact, no regression failure.
  * A promoted version is immutable (read-only files + manifest hashes). Changes create vN+1.
"""
import copy
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
if str(ROOT) not in sys.path:
    sys.path.insert(1, str(ROOT))

APPROVE, REJECT, DEFER = "APPROVE", "REJECT", "DEFER"
PROPOSED_SOURCES = {                           # proposed file -> (development file it was derived from, production name)
    "scoring_v2.yaml": ("scoring.yaml", "scoring.yaml"),
    "filters_v2.yaml": ("filters.yaml", "filters.yaml"),
    "decision_rules_v2.yaml": ("decision.yaml", "decision_rules.yaml"),
    "runtime_production_v1.yaml": ("runtime_aa_live.yaml", "runtime.yaml"),
}
# development config -> production file name (copied unchanged unless an approved change applies)
PRODUCTION_FILES = {
    "scoring.yaml": "scoring.yaml", "filters.yaml": "filters.yaml", "decision.yaml": "decision_rules.yaml",
    "provider_capabilities.yaml": "provider_capabilities.yaml", "suppliers.yaml": "supplier.yaml",
    "competitors.yaml": "competitor.yaml", "creatives.yaml": "creative.yaml", "emerging.yaml": "emerging.yaml",
    "categories.yaml": "categories.yaml", "deep_analysis.yaml": "deep_analysis.yaml",
    "amazon_validation.yaml": "amazon_validation.yaml", "business_viability.yaml": "business_viability.yaml",
    "report.yaml": "report.yaml", "history.yaml": "history.yaml", "calibration_lane.yaml": "calibration_lane.yaml",
}
METADATA_PATHS = {"version", "decision_rules_version", "profile"}      # labels, not behaviour
PRODUCTION_PATHS = {"raw": "data/production/raw", "processed": "data/production/processed",
                    "history": "data/production/history", "reports": "reports/production",
                    "runs": "runs/production", "cache": "data/production/raw"}


# ============================================================================ helpers
def _yaml(path):
    return yaml.safe_load(Path(path).read_text()) or {}


def flatten(d, prefix=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict) and v:
                out.update(flatten(v, p))
            else:
                out[p] = v
    return out


def diff(current, proposed):
    a, b = flatten(current), flatten(proposed)
    return [(p, a.get(p), b.get(p)) for p in sorted(set(a) | set(b)) if a.get(p) != b.get(p)]


def set_path(d, path, value):
    keys = path.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    if value is None:
        d.pop(keys[-1], None)
    else:
        d[keys[-1]] = value


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# ============================================================================ Stage 2-3 — change review
def load_ab(root):
    p = Path(root) / "reports" / "production-calibration.json"
    return json.loads(p.read_text()) if p.exists() else None


def assess(file, path, old, new, ab, replay_ok, regression_ok):
    """One proposed change -> review row with the promotion-rule checks and an approval status."""
    ev = (ab or {})
    checks = {"supported_by_live_data": False, "keeps_data_integrity": True, "no_lowered_confidence_requirement": True,
              "no_unreliable_provider_fields": True, "offline_replay_ok": replay_ok, "no_regression_failure": regression_ok}
    reason = evidence = effect = risk = None
    leaf = path.split(".")[-1]
    o, n = _num(old), _num(new)

    if file == "decision_rules_v2.yaml" and path.startswith("minimum_confidence."):
        reason = f"{path}: {old} -> {new}"
        if o is not None and n is not None and n < o:
            checks["no_lowered_confidence_requirement"] = False
            evidence, effect, risk = "none required: lowering is not allowed", "more products pass", "false positives"
        elif path.endswith(".creative"):
            k = (ev.get("creative_calibration") or {}).get("passing_minimum_without_classification") or 0
            checks["supported_by_live_data"] = k > 0
            evidence = (f"{k} of {(ev.get('creative_calibration') or {}).get('products')} live products reached the "
                        f"creative minimum with no hook / angle / date classified (saved KaloPilot videos max 60)")
            effect = "creative dimension needs some classified creative evidence; otherwise UNKNOWN (missing evidence)"
            risk = "creative dimension UNKNOWN more often until manual creative research is imported"
        else:
            evidence = "no live evidence mapped to this minimum"
    elif file == "decision_rules_v2.yaml" and "_DEPENDENCY_WEAK_BROADER" in path and leaf in (
            "broader_weak_statuses", "when_broader_unknown"):
        rows = (ev.get("decision_calibration") or {}).get("products") or []
        gate = path.split(".")[1]
        k = sum(1 for r in rows if r.get("reject_on_missing_evidence") and
                any(gate in str(x) for x in r.get("negative_evidence") or []))
        checks["supported_by_live_data"] = k > 0
        reason = f"{path}: {old} -> {new} (reject only on negative broader evidence)"
        evidence = (f"{k} live product(s) rejected by {gate} while cross-platform demand was UNKNOWN (missing, not "
                    "negative)" if k else f"no live product was rejected by {gate}: same logic, but untested live")
        effect = "REJECT requires WEAK broader evidence; UNKNOWN blocks READY and adds a validation task"
        risk = "a creator-dependent product waits (INSUFFICIENT_DATA / WATCHLIST) instead of being rejected"
    elif file == "decision_rules_v2.yaml" and path.startswith("validation_tasks."):
        rows = (ev.get("decision_calibration") or {}).get("products") or []
        checks["supported_by_live_data"] = any(r.get("reject_on_missing_evidence") and any(
            leaf in str(x) for x in r.get("negative_evidence") or []) for r in rows)
        reason, evidence = f"task text for {leaf}", "companion of the dependency-gate change (same evidence rule)"
        effect, risk = "the missing broader evidence becomes an explicit next action", "none"
    elif file == "decision_rules_v2.yaml" and path.startswith("hard_gates.") and new is None:
        checks["keeps_data_integrity"] = False
        reason, evidence, effect, risk = f"removes {path}", "not allowed", "gate disabled", "unsafe decisions"
    elif file == "scoring_v2.yaml" and path.startswith("data_requirements.counted_in_confidence"):
        rows = (ev.get("confidence_calibration") or {}).get("products") or []
        k = sum(1 for r in rows if r.get("double_penalties"))
        checks["supported_by_live_data"] = k > 0
        reason = f"{path}: {old} -> {new} (penalize a missing value once)"
        evidence = f"{k}/{len(rows)} live answers penalized twice for the same missing value"
        effect = "WPS Confidence rises exactly by the duplicated points; WPS unchanged"
        risk = "one live product crossed the WPS Confidence minimum (56 -> 61): its momentum became WEAK instead of UNKNOWN"
    elif file == "scoring_v2.yaml" and path.startswith("metrics."):
        checks["keeps_data_integrity"] = False
        reason, evidence = f"WPS metric change {path}", "WPS changes are not part of AB (formula frozen)"
        effect, risk = "scores change", "incomparable history"
    elif file == "runtime_production_v1.yaml" and path.startswith("limits."):
        reason = f"{path}: {old} -> {new}"
        if o is not None and n is not None and n > o:
            checks["keeps_data_integrity"] = False
            evidence, effect, risk = "AB: do not increase production limits", "larger runs", "unpredictable cost"
        else:
            checks["supported_by_live_data"] = True
            evidence, effect, risk = "AB query strategy", "smaller runs", "slower coverage"
    elif file == "runtime_production_v1.yaml" and path.startswith("query_plan.estimated_credits."):
        oc = ((ev.get("cost_efficiency") or {}).get("observed_cost_per_query") or {})
        stage = leaf
        obs = oc.get("deep_answer_ok") if stage == "deep_analysis" else oc.get("discovery_answer") if stage == "discovery" \
            else None
        reason = f"{path}: {old} -> {new}"
        if isinstance(obs, dict) and obs.get("n"):
            checks["supported_by_live_data"] = True
            if n is not None and obs.get("max") is not None and n < obs["max"]:
                checks["keeps_data_integrity"] = False          # an estimate below the observed max weakens the cap
            evidence = f"observed {stage} cost per answer: {obs}"
            effect = "the run credit cap stops before a query that could exceed it"
            risk = "fewer queries per run under the same cap"
        else:
            evidence = f"no live cost observed for {stage}"
    elif file == "runtime_production_v1.yaml" and path.startswith("query_plan.max_credits_for_run"):
        reason = f"{path}: {old} -> {new}"
        if o is not None and n is not None and n > o:
            checks["keeps_data_integrity"] = False
        evidence = "AB: keep the run cap"
    elif any(x in path for x in ("min_reliable_match", "direct_min", "qualified_min", "match_min")):
        reason = f"matching threshold {path}: {old} -> {new}"
        if o is not None and n is not None and n < o:
            checks["no_unreliable_provider_fields"] = False
        evidence = "no live matching evidence (AB Stage 9)"
    else:
        reason, evidence = f"{path}: {old} -> {new}", "no live evidence mapped to this change"
        effect, risk = "unknown", "unreviewed behaviour change"

    hard_fail = not all(checks[k] for k in ("keeps_data_integrity", "no_lowered_confidence_requirement",
                                            "no_unreliable_provider_fields", "offline_replay_ok",
                                            "no_regression_failure"))
    status = REJECT if hard_fail else (APPROVE if checks["supported_by_live_data"] else DEFER)
    return {"file": file, "path": path, "current_value": old, "proposed_value": new, "reason": reason,
            "supporting_live_evidence": evidence, "expected_effect": effect, "risk": risk, "checks": checks,
            "approval_status": status}


def replay_check(root, proposed_dir):
    """Offline replay of the proposed files on saved LIVE data (AB machinery). Returns (replay_ok, regression)."""
    import production_calibration as PC
    try:
        ev = PC.load_evidence(root)
        recs = PC.reanalyze(ev)
        props = {name: (Path(proposed_dir, name).read_text(), []) for name in PC.PROPOSED
                 if Path(proposed_dir, name).exists()}
        rp = PC.replay(ev, recs, props)
        reg = PC.regression(ev, rp, root)
        return True, reg, rp
    except Exception as e:  # noqa: BLE001
        return False, {"blocked": True, "issues": [f"replay failed: {e.__class__.__name__}: {e}"]}, None


def review(root=ROOT, proposed_dir=None, ab=None, replay=None):
    root = Path(root)
    proposed_dir = Path(proposed_dir or root / "config" / "proposed")
    ab = ab if ab is not None else load_ab(root)
    replay_ok, reg, rp = replay if replay is not None else replay_check(root, proposed_dir)
    regression_ok = not reg.get("blocked")
    rows, meta_changes = [], []
    for name, (src, _) in PROPOSED_SOURCES.items():
        p = proposed_dir / name
        if not p.exists():
            continue
        cur, prop = _yaml(root / "config" / src), _yaml(p)
        for path, old, new in diff(cur, prop):
            if path in METADATA_PATHS:
                meta_changes.append({"file": name, "path": path, "current_value": old, "proposed_value": new})
                continue
            rows.append(assess(name, path, old, new, ab, replay_ok, regression_ok))
    counts = {s: sum(r["approval_status"] == s for r in rows) for s in (APPROVE, REJECT, DEFER)}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "changes": rows, "metadata_changes": meta_changes,
            "counts": counts, "replay_ok": replay_ok, "regression": reg,
            "replay_decisions": (rp or {}).get("decisions"),
            "source_calibration": {"report": "reports/production-calibration.md",
                                   "generated_at": (ab or {}).get("generated_at"), "result": (ab or {}).get("result")}}


# ============================================================================ Stage 4-6 — promotion
def versions(root=ROOT):
    base = Path(root) / "config" / "production"
    return sorted((p.name for p in base.glob("v*") if p.is_dir() and p.name[1:].isdigit()), key=lambda v: int(v[1:]))


def next_version(root=ROOT):
    vs = versions(root)
    return f"v{int(vs[-1][1:]) + 1}" if vs else "v1"


def _meta_block(version, created, source, summary):
    return {"config_version": f"production-{version}", "created_at": created, "source_calibration_report": source,
            "change_summary": summary or ["no change vs development config"]}


def production_runtime(root, approved_runtime, version, created, source):
    """Production runtime: canonical safety/provider (safe defaults) + approved production plan + production paths."""
    base = _yaml(Path(root) / "config" / "runtime.yaml")
    rt = copy.deepcopy(base)
    rt["runtime"] = {"market": approved_runtime["runtime"]["market"], "live_mode": False, "dry_run": True,
                     "explicit_live_confirmation": False}
    rt["limits"] = dict(approved_runtime["limits"])
    qp = copy.deepcopy(approved_runtime["query_plan"])
    lim = rt["limits"]
    deep_q = lim["deep_analysis_max_products"] // max(1, qp.get("deep_batch_size", 1))
    amz_q = -(-lim["amazon_validation_max_products"] // max(1, qp.get("amazon_batch_size", 1)))
    disc_q = len(qp.get("discovery_categories") or []) or 1
    qp["guardrails"] = {"max_queries_per_run": disc_q + deep_q + amz_q,
                        "max_provider_queries": {"kalopilot": disc_q + deep_q + amz_q},
                        "max_failed_queries": 2, "max_retry_count": 0,
                        "note": "sized from the approved limits; stop new paid queries, keep completed data, PARTIAL report"}
    rt["query_plan"] = qp
    e2e = copy.deepcopy(approved_runtime["e2e"])
    e2e.update({"step": "PRODUCTION", "confirmation_phrase": "CONFIRM PRODUCTION LIVE RUN"})
    rt["e2e"] = e2e
    rt["paths"] = dict(PRODUCTION_PATHS)
    rt["cache_policy"] = {"discovery_ttl_hours": 24, "deep_ttl_hours": 24, "amazon_ttl_hours": 24,
                          "cache_key": "query sha256 + prompt_version", "reuse_across_environments": False,
                          "note": "only PRODUCTION-tagged raw answers under data/production/raw are reused"}
    rt["provider_retry"] = {"max_retries": 0, "retry_on": [], "note": "no automatic re-buy of a paid query"}
    rt["credit_safety"] = {"min_balance_reserve": base["safety"]["min_balance_reserve"],
                           "max_credits_for_run": qp.get("max_credits_for_run"), "free_balance_check": True}
    rt["history_settings"] = {"path": PRODUCTION_PATHS["history"], "append_only": True, "dedupe": "source_snapshot_hash",
                              "decision_history": True, "environment": "PRODUCTION"}
    rt["report_settings"] = {"dir": PRODUCTION_PATHS["reports"], "dated_never_overwritten": True,
                             "latest_files": ["latest-winning-products.md", "latest-final-decision.md"],
                             "shortlist_max": 3, "shortlist_only_state": "READY_FOR_PRODUCT_VALIDATION"}
    rt["degraded_mode"] = {
        "kalopilot": {"critical": True, "on_unavailable": "BLOCK_RUN", "on_degraded": "CONTINUE_WITH_WARNING"},
        "amazon": {"mandatory": False, "on_unavailable": "CONTINUE",
                   "effect": "cross-platform demand UNKNOWN (never estimated)"},
        "supplier": {"on_unavailable": "CONTINUE_NO_READY", "on_degraded": "CONTINUE",
                     "effect": "READY requires supplier economics (decision rules ready.require_supplier_economics)"},
        "competitor": {"on_unavailable": "CONTINUE_REDUCED_CONFIDENCE", "on_degraded": "CONTINUE_REDUCED_CONFIDENCE",
                       "decision_confidence_penalty": 10,
                       "effect": "Decision Confidence -10 for products without competitor evidence"},
        "creative": {"on_unavailable": "CONTINUE", "effect": "creative dimension UNKNOWN"},
    }
    rt["production_meta"] = _meta_block(version, created, source, ["production runtime built from approved "
                                                                  "runtime_production_v1 changes + safe defaults"])
    return rt


def _header(version, created, source, summary):
    L = ["# " + "=" * 77, f"# PRODUCTION CONFIG production-{version} — IMMUTABLE (Step AC promotion)",
         f"# created_at: {created}", f"# source_calibration_report: {source}", "# change_summary:"]
    L += [f"#   - {s}" for s in summary] or ["#   - no change vs development config"]
    L += ["# Do not edit. A new change creates config/production/v<N+1>/.", "# " + "=" * 77, ""]
    return "\n".join(L) + "\n"


def _strip_proposed_header(text):
    lines = text.splitlines(keepends=True)
    i = 0
    if lines and lines[0].startswith("# ====") and len(lines) > 1 and "PROPOSED" in lines[1]:
        i = 1
        while i < len(lines) and not lines[i].startswith("# ===="):
            i += 1
        i += 1
    return "".join(lines[i:]).lstrip("\n")


def promote(root=ROOT, rev=None, version=None, now=None, proposed_dir=None, replay_final=True):
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    created = now.isoformat()
    proposed_dir = Path(proposed_dir or root / "config" / "proposed")
    rev = rev or review(root, proposed_dir)
    version = version or next_version(root)
    target = root / "config" / "production" / version
    if target.exists():
        raise FileExistsError(f"{target} exists: production versions are immutable (use {next_version(root)})")
    ab_path = root / "reports" / "production-calibration.md"
    source = (f"reports/production-calibration.md (generated {rev['source_calibration'].get('generated_at')}, "
              f"sha256 {sha256(ab_path)[:16] if ab_path.exists() else 'N/A'}, result "
              f"{rev['source_calibration'].get('result')})")
    approved = [c for c in rev["changes"] if c["approval_status"] == APPROVE]
    tmp = target.with_name(version + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    written = {}
    for dev_name, prod_name in PRODUCTION_FILES.items():
        prop_name = next((k for k, (src, pn) in PROPOSED_SOURCES.items() if src == dev_name), None)
        file_changes = [c for c in rev["changes"] if c["file"] == prop_name]
        ok = [c for c in file_changes if c["approval_status"] == APPROVE]
        summary = [f"{c['path']}: {c['current_value']} -> {c['proposed_value']}" for c in ok]
        summary += [f"NOT promoted ({c['approval_status']}): {c['path']}" for c in file_changes if c not in ok]
        if not file_changes:                                       # unchanged: keep the development text as-is
            text = (root / "config" / dev_name).read_text()
            data = yaml.safe_load(text)
        elif len(ok) == len(file_changes):                         # everything approved: keep commented text
            text = _strip_proposed_header((proposed_dir / prop_name).read_text()).replace("-proposed", "")
            data = yaml.safe_load(text)
        else:
            data = _yaml(root / "config" / dev_name)
            for c in ok:
                set_path(data, c["path"], c["proposed_value"])
            for label in ("decision_rules_version", "version"):     # partial promotion: its own version label
                if ok and label in data:
                    data[label] = f"{data[label]}-production-{version}"
                    summary.append(f"{label} -> {data[label]} (partial promotion of {prop_name})")
                    break
            text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        meta = _meta_block(version, created, source, summary)
        body = text.rstrip() + "\n\nproduction_meta:\n" + "\n".join(
            "  " + line for line in yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).splitlines()) + "\n"
        (tmp / prod_name).write_text(_header(version, created, source, meta["change_summary"]) + body)
        written[prod_name] = dev_name
    # runtime (production profile): approved runtime changes on top of the AA profile it was derived from
    base_rt = _yaml(root / "config" / "runtime_aa_live.yaml")
    for c in approved:
        if c["file"] == "runtime_production_v1.yaml":
            set_path(base_rt, c["path"], c["proposed_value"])
    prt = production_runtime(root, base_rt, version, created, source)
    rt_summary = [f"{c['path']}: {c['current_value']} -> {c['proposed_value']}" for c in approved
                  if c["file"] == "runtime_production_v1.yaml"] + ["safe defaults: live_mode false, dry_run true",
                                                                   "production data paths, guardrails, degraded mode"]
    (tmp / "runtime.yaml").write_text(_header(version, created, source, rt_summary) +
                                      yaml.safe_dump(prt, sort_keys=False, allow_unicode=True))
    errors = validate_set(tmp)
    if not errors and replay_final:                     # replay the EXACT promoted set (approved changes only)
        import production_calibration as PC
        try:
            ev = PC.load_evidence(root)
            props = {"scoring_v2.yaml": ((tmp / "scoring.yaml").read_text(), []),
                     "decision_rules_v2.yaml": ((tmp / "decision_rules.yaml").read_text(), [])}
            rp = PC.replay(ev, PC.reanalyze(ev), props)
            reg = PC.regression(ev, rp, root)
            rev["promoted_set_replay"] = {"regression": reg, "decisions": rp["decisions"]}
            if reg["blocked"]:
                errors.append("offline replay of the promoted set failed regression: " + "; ".join(reg["issues"]))
        except Exception as e:  # noqa: BLE001
            errors.append(f"offline replay of the promoted set failed: {e.__class__.__name__}: {e}")
    if errors:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError("production config set invalid: " + "; ".join(errors))
    (tmp / "change_review.json").write_text(json.dumps(rev, ensure_ascii=False, indent=2, default=str))
    dec = _yaml(tmp / "decision_rules.yaml")
    manifest = {"config_version": f"production-{version}", "version": version, "promotion_timestamp": created,
                "source_calibration_version": rev["source_calibration"], "source_calibration_report": source,
                "decision_rules_version": dec.get("decision_rules_version"),
                "scoring_version": _yaml(tmp / "scoring.yaml").get("version"),
                "changes": {"approved": len(approved), "rejected": rev["counts"][REJECT], "deferred": rev["counts"][DEFER]},
                "aliases": {"decision.yaml": "decision_rules.yaml", "suppliers.yaml": "supplier.yaml",
                            "competitors.yaml": "competitor.yaml", "creatives.yaml": "creative.yaml"},
                "hash_algorithm": "sha256",
                "files": {p.name: sha256(p) for p in sorted(tmp.glob("*")) if p.is_file()}}
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))
    os.replace(tmp, target)
    for p in target.glob("*"):
        p.chmod(0o444)                                   # immutable after promotion
    if not (root / "config" / "production" / "ACTIVE.json").exists():
        activate(root, version, note="first promotion")
    return {"version": version, "dir": str(target), "manifest": manifest, "review": rev}



# ============================================================================ owner-approved change (v<N> -> v<N+1>)
OWNER_CHANGEABLE = {                      # production file -> path prefixes the owner may change explicitly
    "decision_rules.yaml": ("decision.insufficient_if_unknown_at_least", "dimensions.evidence_quality.layers"),
    "runtime.yaml": ("limits.discovery_max_products", "limits.deep_analysis_max_products",
                     "query_plan.deep_selection", "query_plan.deep_exclude_keywords", "query_plan.discovery_categories",
                     "query_plan.discovery_mode", "query_plan.discovery_lenses", "e2e.final_decision_max_products",
                     "query_plan.discovery_subniches", "query_plan.prefer_new_days", "query_plan.target_candidates", "query_plan.discovery_exclude_established_brands",
                     "query_plan.max_credits_for_run", "query_plan.guardrails.max_queries_per_run",
                     "query_plan.guardrails.max_provider_queries.kalopilot", "credit_safety.max_credits_for_run"),
    # which products get the (optional) Amazon / BVS checks — eligibility only, never the AVS / BVS formulas
    "amazon_validation.yaml": ("amazon_validation.minimum_wps",),
    "business_viability.yaml": ("business_viability.eligibility.minimum_wps",),
    # WPS scale end-points only (owner-approved recalibration); points, weights and metrics stay fixed
    "scoring.yaml": ("version",
                     "metrics.demand.components.units_sold.zero_at", "metrics.demand.components.units_sold.full_at",
                     "metrics.creator_reach.components.creator_count.zero_at",
                     "metrics.creator_reach.components.creator_count.full_at",
                     "metrics.video_momentum.components.video_count.zero_at",
                     "metrics.video_momentum.components.video_count.full_at"),
}


def _y(v):
    return f'"{v}"' if isinstance(v, str) else v


def _text_set_scalar(text, path, old, new):
    """Replace one scalar in YAML text keeping comments; None if the line is not uniquely identifiable."""
    if isinstance(new, (dict, list)) or isinstance(old, (dict, list)) or old is None:
        return None
    parts = path.split(".")
    leaf = parts[-1]
    pat = re.compile(rf"^(\s*{re.escape(leaf)}:\s*)[\"']?{re.escape(str(old))}[\"']?(\s*(#.*)?)$", re.M)
    hits = pat.findall(text)
    if len(hits) == 1:
        return pat.sub(lambda m: f"{m.group(1)}{_y(new)}{m.group(2)}", text, count=1)
    # ambiguous leaf: walk the key path in order (block YAML), replace the leaf under the last parent
    lines = text.splitlines(keepends=True)
    i, indent = 0, -1
    for part in parts[:-1]:
        rx = re.compile(rf"^(\s*){re.escape(part)}:\s*(#.*)?$")
        while i < len(lines):
            m = rx.match(lines[i].rstrip("\n"))
            if m and len(m.group(1)) > indent:
                indent = len(m.group(1))
                break
            i += 1
        else:
            return None
        i += 1
    lp = re.compile(rf"^(\s*{re.escape(leaf)}:\s*)[\"']?{re.escape(str(old))}[\"']?(\s*(#.*)?)$")
    for j in range(i, len(lines)):
        ln = lines[j].rstrip("\n")
        if ln.strip() and not ln.lstrip().startswith("#") and len(ln) - len(ln.lstrip()) <= indent:
            return None                                   # left the parent block
        m = lp.match(ln)
        if m:
            lines[j] = f"{m.group(1)}{_y(new)}{m.group(2)}" + ("\n" if lines[j].endswith("\n") else "")
            return "".join(lines)
    return None


def _split_production_text(text):
    """-> (body without header and without the trailing production_meta block)."""
    lines = text.splitlines(keepends=True)
    i = 0
    if lines and lines[0].startswith("# ====") and len(lines) > 1 and "PRODUCTION CONFIG" in lines[1]:
        i = 1
        while i < len(lines) and not lines[i].startswith("# ===="):
            i += 1
        i += 1
    body = "".join(lines[i:]).lstrip("\n")
    k = body.find("\nproduction_meta:")
    return (body[:k] if k >= 0 else body).rstrip() + "\n"


def promote_owner_change(root=ROOT, changes=None, approved_by=None, approved_at=None, note=None, base_version=None,
                         now=None, activate_new=False):
    """Create v<N+1> from an existing production version with changes the OWNER approved explicitly.

    * Only paths listed in OWNER_CHANGEABLE can change (discovery scope, brand rule, run credit / query caps).
      Scores, filters, decision rules and confidence minimums cannot change through this path.
    * Every change is recorded with approver + timestamp in change_review.json and the manifest.
    * Unchanged files keep their text verbatim (only header + production_meta are re-stamped).
    * The new version is immutable and verified; it is NOT activated unless activate_new=True.
    """
    root = Path(root)
    if not changes or not approved_by or not approved_at:
        raise ValueError("owner promotion needs changes, approved_by and approved_at")
    base_version = base_version or active_version(root)
    base = active_dir(root, base_version)
    vb = verify(base)
    if not vb["ok"]:
        raise ValueError(f"base {base_version} failed hash verification: {vb}")
    now = now or datetime.now(timezone.utc)
    created = now.isoformat()
    version = next_version(root)
    target = root / "config" / "production" / version
    if target.exists():
        raise FileExistsError(f"{target} exists: production versions are immutable")
    rows = []
    by_file = {}
    for c in changes:
        f, path = c["file"], c["path"]
        if not any(path == p or path.startswith(p + ".") for p in OWNER_CHANGEABLE.get(f, ())):
            raise ValueError(f"{f}:{path} cannot be changed through an owner promotion")
        old = _yaml(base / f)
        for part in path.split("."):
            old = old.get(part) if isinstance(old, dict) else None
        rows.append({"file": f, "path": path, "current_value": old, "proposed_value": c["value"],
                     "reason": c.get("reason"), "approval_status": APPROVE, "approved_by": approved_by,
                     "approved_at": approved_at, "approval_kind": "OWNER_EXPLICIT"})
        by_file.setdefault(f, []).append(rows[-1])
    source = f"owner-approved change of production-{base_version} (approved by {approved_by} at {approved_at})"
    tmp = target.with_name(version + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for p in sorted(base.glob("*.yaml")):
        rs = by_file.get(p.name, [])
        summary = [f"{r['path']}: {r['current_value']} -> {r['proposed_value']} (owner approved)" for r in rs] or \
                  [f"unchanged from production-{base_version}"]
        if rs:
            body = _split_production_text(p.read_text())
            for r in rs:                                  # keep comments when a scalar line is unambiguous
                body = _text_set_scalar(body, r["path"], r["current_value"], r["proposed_value"]) if body else None
            if body is None:
                data = _yaml(p)
                data.pop("production_meta", None)
                for r in rs:
                    set_path(data, r["path"], r["proposed_value"])
                body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        else:
            body = _split_production_text(p.read_text())
        meta = _meta_block(version, created, source, summary)
        meta["derived_from"] = f"production-{base_version}"
        body = body.rstrip() + "\n\nproduction_meta:\n" + "\n".join(
            "  " + line for line in yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).splitlines()) + "\n"
        (tmp / p.name).write_text(_header(version, created, source, summary).replace(
            "(Step AC promotion)", "(owner-approved promotion)") + body)
    errors = validate_set(tmp)
    for f, rs in by_file.items():                         # the written values must read back exactly
        d = _yaml(tmp / f)
        for r in rs:
            v = d
            for part in r["path"].split("."):
                v = v.get(part) if isinstance(v, dict) else None
            if v != r["proposed_value"]:
                errors.append(f"{f}:{r['path']} did not persist")
    if errors:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError("production config set invalid: " + "; ".join(errors))
    rev = {"generated_at": created, "kind": "OWNER_APPROVED", "base_version": base_version, "note": note,
           "changes": rows, "counts": {APPROVE: len(rows), REJECT: 0, DEFER: 0}}
    (tmp / "change_review.json").write_text(json.dumps(rev, ensure_ascii=False, indent=2, default=str))
    bm = json.loads((base / "manifest.json").read_text())
    manifest = {"config_version": f"production-{version}", "version": version, "promotion_timestamp": created,
                "promotion_kind": "OWNER_APPROVED", "derived_from": f"production-{base_version}",
                "approved_by": approved_by, "approved_at": approved_at,
                "source_calibration_version": bm.get("source_calibration_version"),
                "source_calibration_report": source,
                "decision_rules_version": _yaml(tmp / "decision_rules.yaml").get("decision_rules_version"),
                "scoring_version": _yaml(tmp / "scoring.yaml").get("version"),
                "changes": {"approved": len(rows), "rejected": 0, "deferred": 0},
                "aliases": bm.get("aliases"), "hash_algorithm": "sha256",
                "files": {p.name: sha256(p) for p in sorted(tmp.glob("*")) if p.is_file()}}
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))
    os.replace(tmp, target)
    for p in target.glob("*"):
        p.chmod(0o444)
    act = activate(root, version, note=note or "owner-approved promotion") if activate_new else None
    return {"version": version, "dir": str(target), "manifest": manifest, "review": rev, "activation": act}


def validate_set(d):
    """Run the normal config validation on a production set (production names mapped back)."""
    import config_validation as CV
    d = Path(d)
    inv = {v: k for k, v in PRODUCTION_FILES.items()}
    cfgs = {}
    for p in d.glob("*.yaml"):
        cfgs[inv.get(p.name, p.name)] = _yaml(p)
    errs = [e for e in CV.validate(cfgs) if "limit conflict" not in e]
    rt = cfgs.get("runtime.yaml") or {}
    r = rt.get("runtime") or {}
    if not (r.get("live_mode") is False and r.get("dry_run") is True and r.get("explicit_live_confirmation") is False):
        errs.append("production runtime must keep safe defaults (live_mode false, dry_run true)")
    if any("production" not in str(v) for v in (rt.get("paths") or {}).values()):
        errs.append("production runtime paths must be production paths")
    for f in list(PRODUCTION_FILES.values()) + ["runtime.yaml"]:
        if not (d / f).exists():
            errs.append(f"missing {f}")
        elif not (_yaml(d / f).get("production_meta") or {}).get("config_version"):
            errs.append(f"{f}: production_meta missing")
    return errs


# ============================================================================ verify / activate (rollback)
def verify(version_dir):
    d = Path(version_dir)
    m = json.loads((d / "manifest.json").read_text())
    files = {p.name: sha256(p) for p in d.glob("*") if p.is_file() and p.name != "manifest.json"}
    mism = sorted(k for k in m["files"] if k in files and files[k] != m["files"][k])
    missing = sorted(set(m["files"]) - set(files))
    extra = sorted(set(files) - set(m["files"]))
    return {"ok": not (mism or missing or extra), "config_version": m["config_version"], "changed": mism,
            "missing": missing, "extra": extra, "hashes": files}


def active_version(root=ROOT):
    p = Path(root) / "config" / "production" / "ACTIVE.json"
    return json.loads(p.read_text())["active_version"] if p.exists() else None


def active_dir(root=ROOT, version=None):
    v = version or active_version(root)
    if not v:
        raise FileNotFoundError("no production config promoted yet (production-config promote)")
    d = Path(root) / "config" / "production" / v
    if not d.exists():
        raise FileNotFoundError(f"production config {v} not found")
    return d


def activate(root, version, note=None):
    """Point production at an existing version (rollback v2 -> v1 or roll forward). Nothing is deleted."""
    root = Path(root)
    d = active_dir(root, version)
    v = verify(d)
    if not v["ok"]:
        raise ValueError(f"{version} failed hash verification: {v}")
    base = root / "config" / "production"
    prev = active_version(root)
    rec = {"active_version": version, "previous_version": prev, "activated_at": datetime.now(timezone.utc).isoformat(),
           "note": note}
    (base / "ACTIVE.json").write_text(json.dumps(rec, indent=2))
    with open(base / "activation_log.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def render_review(rev):
    L = ["# Production config promotion — change review", "", f"Generated {rev['generated_at']} · source "
         f"{rev['source_calibration']} · offline replay ok: {rev['replay_ok']} · regression blocked: "
         f"{rev['regression'].get('blocked')}", "", f"Counts: {rev['counts']}", "",
         "| File | Change | Current | Proposed | Evidence | Effect | Risk | Status |", "|---|---|---|---|---|---|---|---|"]
    for c in rev["changes"]:
        L.append(f"| {c['file']} | {c['path']} | {c['current_value']} | {c['proposed_value']} | "
                 f"{c['supporting_live_evidence']} | {c['expected_effect']} | {c['risk']} | **{c['approval_status']}** |")
    if rev["metadata_changes"]:
        L += ["", "Metadata (labels, not behaviour): " + "; ".join(
            f"{m['file']} {m['path']}: {m['current_value']} -> {m['proposed_value']}" for m in rev["metadata_changes"])]
    return "\n".join(L) + "\n"
