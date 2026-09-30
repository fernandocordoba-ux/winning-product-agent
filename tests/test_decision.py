"""Step Z — Unified Decision Engine. SYNTHETIC data only (no live query, no credits)."""
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import decision_engine as DE  # noqa: E402

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
PROV_OK = {k: {"availability": "AVAILABLE"} for k in DE.PROVENANCE_KEYS}


def product(pid="P1", name="Synthetic Product", wps=78.0, conf=80.0, trend="RISING", growth=35.0, momentum=72.0,
            mconf=70.0, avs=None, aconf=None, bvs=74.0, bconf=72.0, cost=4.0, ship=3.0, gm=48.0, cm=18.0,
            sconf=80.0, flags=(), bvs_flags=(), supplier_flags=(), offers=None, creators=120, videos=400, hist=3,
            env="SYNTHETIC", deep=True, identity_conflict=False):
    sel = {"supplier_confidence": sconf, "supplier_quality": 75, "effective_delivery_days": 9} if cost is not None else {}
    offers = offers if offers is not None else ([{"match_class": "STRONG"}] if cost is not None else [])
    return {
        "id": pid, "product_id": pid, "name": name, "category": "Beauty & Personal Care", "url": None,
        "wps": wps, "confidence": conf, "avs": avs, "amazon_confidence": aconf, "bvs": bvs, "bvs_confidence": bconf,
        "trend": trend, "growth_30d": growth, "flags": list(flags), "identity_conflict": identity_conflict,
        "deep": ({"product_id": pid, "data_environment": env, "provenance": PROV_OK,
                  "creator_metrics": {"total": creators}, "video_metrics": {"total": videos}} if deep else None),
        "amazon": ({"amazon_match_status": "MATCHED"} if avs is not None else None),
        "bvs_record": ({"economics": {"product_cost": cost if cost is not None else "N/A",
                                      "supplier_shipping_cost": ship if ship is not None else "N/A",
                                      "landed_cost": (cost + ship) if None not in (cost, ship) else "N/A",
                                      "gross_margin_percent": gm if gm is not None else "N/A",
                                      "contribution_margin_percent": cm if cm is not None else "N/A"},
                        "commercial_red_flags": [dict(f) for f in bvs_flags],
                        "supplier_layer": ({"selected_supplier_offer_id": "OF1" if cost is not None else None,
                                            "offer_count": len(offers), "qualified_offers": 1 if cost is not None else 0,
                                            "selected": sel or None, "supplier_flags": [dict(f) for f in supplier_flags],
                                            "offers": offers} if offers else None)}
                       if bvs is not None else None),
        "emerging": ({"momentum_score": momentum, "momentum_confidence": mconf, "emerging_status": "EMERGING"}
                     if momentum is not None else None),
        "history": [{}] * hist, "history_source": "history_store" if hist else None,
        "data_environment": env,
    }


def comp(opp=65.0, sat=40.0, conf=70.0, flags=()):
    return {"opportunity": {"score": opp}, "saturation": {"score": sat}, "confidence": {"score": conf},
            "direct_competitors": 6, "red_flags": [{"flag": f} for f in flags]}


def crea(opp=62.0, sat=45.0, conf=65.0, flags=()):
    return {"opportunity": {"score": opp}, "saturation": {"score": sat}, "confidence": {"score": conf},
            "qualified_creatives": 12, "red_flags": [{"flag": f} for f in flags]}


def decide(p, cfg, c="default", r="default", manual=None, env="SYNTHETIC"):
    c = comp() if c == "default" else c
    r = crea() if r == "default" else r
    return DE.decide(DE.build_evidence(p, c, r, env), cfg, manual)


# ============================================================================ the 5 states
def test_ready_impl(cfg):
    d = decide(product(), cfg)
    assert d["decision_state"] == DE.READY
    assert d["dimension_status"]["market_momentum"] == "STRONG"
    assert d["dimension_status"]["commercial_viability"] == "STRONG"
    assert all(g["status"] != "FIRED" for g in d["hard_gates"])
    assert d["next_actions"] and all("Manual validation" in a for a in d["next_actions"])


def test_promising_lists_exact_tasks_impl(cfg):
    d = decide(product(), cfg, r=None)                      # no creative research
    assert d["decision_state"] == DE.PROMISING
    assert any("creative" in t["task"].lower() for t in d["validation_tasks"])


def test_watchlist_has_move_conditions_impl(cfg):
    d = decide(product(wps=45.0, bvs=74.0), cfg)            # weak momentum, strong commercial
    assert d["decision_state"] == DE.WATCH
    assert any(c["dimension"] == "market_momentum" and "WPS >= 55" in c["condition"] for c in d["move_up_conditions"])
    assert any(c["dimension"] == "commercial_viability" for c in d["move_down_conditions"])


def test_reject_by_hard_gate_impl(cfg):
    d = decide(product(cm=-5.0, bvs_flags=[{"flag": "NEGATIVE_CONTRIBUTION_MARGIN", "severe": True}]), cfg)
    assert d["decision_state"] == DE.REJECT
    assert d["reject_reasons"][0]["gate"] == "NEGATIVE_CONTRIBUTION_MARGIN"
    assert d["reject_reasons"][0]["evidence"]["contribution_margin_pct"] == -5.0


def test_insufficient_data_lists_missing_and_next_queries_impl(cfg):
    p = product(wps=None, conf=None, momentum=None, bvs=None, cost=None, ship=None, deep=False, hist=0)
    d = decide(p, cfg, c=None, r=None)
    assert d["decision_state"] == DE.INSUFFICIENT
    assert "wps" in d["missing_fields"] and "deep_analysis" in d["missing_sources"]
    q = {x["source"]: x for x in d["required_next_queries"]}
    assert q["deep_analysis"]["paid"] is True and q["supplier"]["paid"] is False
    assert all("explicit" in x["note"] or x["note"] == "free / manual" for x in d["required_next_queries"])


# ============================================================================ scenario tests
def test_strong_wps_poor_bvs_impl(cfg):
    d = decide(product(wps=82.0, bvs=40.0, gm=22.0), cfg)
    assert d["dimension_status"]["market_momentum"] == "STRONG"
    assert d["dimension_status"]["commercial_viability"] == "WEAK"
    assert d["decision_state"] == DE.WATCH
    assert any("Commercial Viability WEAK" in x["text"] for x in d["why_it_did_not_pass"])


def test_strong_wps_missing_supplier_economics_is_promising_not_ready_impl(cfg):
    d = decide(product(cost=None, ship=None, gm=None, cm=None), cfg)
    assert d["dimension_status"]["commercial_viability"] == "UNKNOWN"
    assert d["decision_state"] == DE.PROMISING
    assert d["validation_tasks"][0]["reason"] == "supplier economics missing"


def test_strong_commercial_weak_demand_impl(cfg):
    d = decide(product(wps=40.0, bvs=80.0, gm=55.0), cfg)
    assert d["dimension_status"]["commercial_viability"] == "STRONG"
    assert d["dimension_status"]["market_momentum"] == "WEAK"
    assert d["decision_state"] == DE.WATCH


def test_high_saturation_high_demand_not_rejected_impl(cfg):
    d = decide(product(wps=85.0), cfg, c=comp(opp=40.0, sat=85.0, flags=["EXTREME_DIRECT_COMPETITION"]))
    assert d["dimension_status"]["competitive_environment"] == "WEAK"
    gate = next(g for g in d["hard_gates"] if g["gate"] == "EXTREME_COMPETITION_WEAK_DEMAND")
    assert gate["status"] == "PASSED"                       # demand is strong -> no hard reject
    assert d["decision_state"] == DE.WATCH


def test_extreme_competition_with_weak_demand_rejected_impl(cfg):
    d = decide(product(wps=40.0), cfg, c=comp(opp=20.0, sat=90.0, flags=["EXTREME_DIRECT_COMPETITION"]))
    assert d["decision_state"] == DE.REJECT
    assert any(r.get("gate") == "EXTREME_COMPETITION_WEAK_DEMAND" for r in d["reject_reasons"])


def test_low_competition_low_demand_impl(cfg):
    # low saturation but demand-gated opportunity is low; momentum weak -> 2 weak majors -> REJECT
    d = decide(product(wps=35.0, bvs=50.0, gm=25.0), cfg, c=comp(opp=20.0, sat=10.0))
    assert d["dimension_status"]["competitive_environment"] == "WEAK"
    assert d["decision_state"] == DE.REJECT
    assert "weak_major_dimensions" in d["reject_reasons"][0]


def test_missing_creative_data_is_unknown_not_reject_impl(cfg):
    d = decide(product(), cfg, r=None)
    assert d["dimension_status"]["creative_opportunity"] == "UNKNOWN"
    assert d["decision_state"] != DE.REJECT


def test_missing_amazon_data_allows_ready_impl(cfg):
    d = decide(product(avs=None), cfg)
    assert d["dimension_status"]["cross_platform_demand"] == "UNKNOWN"
    assert d["decision_state"] == DE.READY


def test_weak_amazon_blocks_ready_impl(cfg):
    d = decide(product(avs=30.0, aconf=70.0), cfg)
    assert d["dimension_status"]["cross_platform_demand"] == "WEAK"
    assert d["decision_state"] == DE.WATCH


def test_low_confidence_makes_dimension_unknown_impl(cfg):
    d = decide(product(bconf=30.0), cfg)
    assert d["dimension_status"]["commercial_viability"] == "UNKNOWN"
    assert "below minimum 50" in d["dimensions"]["commercial_viability"]["reason"]


def test_missing_optional_metric_never_hard_rejects_impl(cfg):
    d = decide(product(cm=None, trend=None, growth=None), cfg)
    st = {g["gate"]: g["status"] for g in d["hard_gates"]}
    assert st["NEGATIVE_CONTRIBUTION_MARGIN"] == "NOT_EVALUATED"
    assert st["DEMAND_DECLINING"] == "NOT_EVALUATED"
    assert d["decision_state"] != DE.REJECT


# ============================================================================ hard gates
def test_hard_reject_gates_impl(cfg):
    for kw, gate in [
        ({"flags": ["REGULATED_PRODUCT"]}, "REGULATED_PRODUCT"),
        ({"trend": "DECLINING", "growth": -35.0}, "DEMAND_DECLINING"),
        ({"identity_conflict": True}, "UNRELIABLE_PRODUCT_MATCH"),
        ({"supplier_flags": [{"flag": "VERY_SLOW_DELIVERY"}]}, "VERY_SLOW_DELIVERY"),
        ({"flags": ["CREATOR_DEPENDENCY"], "creators": 4}, "CREATOR_DEPENDENCY_WEAK_BROADER"),
        ({"flags": ["VIDEO_DEPENDENCY"], "videos": 6}, "VIDEO_DEPENDENCY_WEAK_BROADER"),
    ]:
        d = decide(product(**kw), cfg)
        assert d["decision_state"] == DE.REJECT, gate
        assert any(r.get("gate") == gate for r in d["reject_reasons"]), gate


def test_dependency_with_broad_evidence_not_rejected_impl(cfg):
    d = decide(product(flags=["CREATOR_DEPENDENCY"], creators=4, avs=75.0, aconf=70.0), cfg)
    assert d["decision_state"] != DE.REJECT


def test_integrity_error_rejects_mixed_environment_impl(cfg):
    d = DE.decide(DE.build_evidence(product(env="SYNTHETIC"), comp(), crea(), expected_env="LIVE"), cfg)
    assert d["decision_state"] == DE.REJECT
    assert d["reject_reasons"][0]["gate"] == "CRITICAL_DATA_INTEGRITY_ERROR"


def test_ip_review_blocks_ready_until_manual_check_impl(cfg):
    p = product(flags=["IP_REVIEW_REQUIRED"])
    assert decide(p, cfg)["decision_state"] == DE.PROMISING
    manual = {"P1": {"ip_trademark_check": {"status": "COMPLETE", "note": "synthetic"}}}
    d = decide(p, cfg, manual=manual)
    assert d["decision_state"] == DE.READY
    assert next(g for g in d["hard_gates"] if g["gate"] == "IP_REVIEW_REQUIRED")["status"] == "RESOLVED"


def test_counterfeit_severe_rejects_unless_resolved_impl(cfg):
    p = product(bvs_flags=[{"flag": "COUNTERFEIT_REVIEW_REQUIRED", "severe": True}])
    assert decide(p, cfg)["decision_state"] == DE.REJECT
    p2 = product(bvs_flags=[{"flag": "COUNTERFEIT_REVIEW_REQUIRED", "severe": False}])
    assert decide(p2, cfg)["decision_state"] == DE.PROMISING


def test_supplier_match_unreliable_blocks_ready_impl(cfg):
    p = product(cost=None, ship=None, offers=[{"match_class": "UNRELIABLE"}, {"match_class": "UNRELIABLE"}])
    d = decide(p, cfg)
    assert next(g for g in d["hard_gates"] if g["gate"] == "SUPPLIER_MATCH_UNRELIABLE")["status"] == "FIRED"
    assert d["decision_state"] == DE.PROMISING


# ============================================================================ Decision Confidence
def test_decision_confidence_not_raised_by_performance_impl(cfg):
    lo = decide(product(wps=58.0, bvs=56.0, gm=31.0), cfg)["decision_confidence"]["score"]
    hi = decide(product(wps=95.0, bvs=95.0, gm=70.0), cfg)["decision_confidence"]["score"]
    assert lo == hi


def test_decision_confidence_falls_with_missing_evidence_impl(cfg):
    full = decide(product(avs=70.0, aconf=70.0), cfg)["decision_confidence"]
    thin = decide(product(momentum=None, hist=1), cfg, c=None, r=None)["decision_confidence"]
    assert 0 <= thin["score"] < full["score"] <= 100
    assert set(full["components"]) == {"core_dimension_coverage", "layer_confidences", "provenance_completeness",
                                       "reliable_sources", "historical_depth"}


# ============================================================================ shortlist
def run(products, cfg, cmap=None, rmap=None):
    ids = [p["product_id"] for p in products]
    cmap = cmap if cmap is not None else {i: comp() for i in ids}
    rmap = rmap if rmap is not None else {i: crea() for i in ids}
    return DE.run(products, cmap, rmap, cfg=cfg, expected_env="SYNTHETIC", now=NOW)


def test_shortlist_max_three_impl(cfg):
    ps = [product(pid=f"P{i}", name=f"Prod {i}") for i in range(5)]
    res = run(ps, cfg)
    assert sum(d["decision_state"] == DE.READY for d in res["decisions"]) == 5
    assert len(res["shortlist"]) == 3


def test_shortlist_not_filled_when_fewer_qualify_impl(cfg):
    ps = [product(pid="A"), product(pid="B", wps=40.0), product(pid="C", bvs=None, cost=None, ship=None)]
    res = run(ps, cfg)
    assert [s["product_id"] for s in res["shortlist"]] == ["A"]


def test_shortlist_tie_break_deterministic_impl(cfg):
    ps = [product(pid="X", wps=60.0), product(pid="Y", wps=80.0), product(pid="Z", wps=80.0, momentum=None, hist=1)]
    res = run(ps, cfg)
    order = [s["product_id"] for s in res["shortlist"]]
    assert order[0] == "Y" and order[-1] == "X"            # STRONG momentum first; higher decision confidence next
    assert order == [s["product_id"] for s in run(list(reversed(ps)), cfg)["shortlist"]]


# ============================================================================ traceability / reports
def test_traceability_impl(cfg):
    d = decide(product(), cfg)
    for x in d["why_it_passed"] + d["why_it_did_not_pass"]:
        assert x["rule"] and "evidence" in x
    for k, e in d["evidence"]["values"].items():
        assert (e["value"] is None) == (e["source"] is None)
    ex = DE.explain_decision("P1", {"decisions": [d], "metadata": {"decision_rules_version": "Z-v1"}})
    assert ex["step_4_state_rule_path"] and ex["step_2_hard_gates"] and len(ex["step_3_dimensions"]) == 6


def _all_states(cfg):
    ps = [product(pid="R1", name="Ready One"),
          product(pid="PR1", name="Promising One", cost=None, ship=None, gm=None, cm=None),
          product(pid="W1", name="Watch One", wps=45.0),
          product(pid="RJ1", name="Reject One", cm=-4.0),
          product(pid="I1", name="Thin One", wps=None, conf=None, momentum=None, bvs=None, cost=None, deep=False, hist=0)]
    return run(ps, cfg, {"R1": comp(), "PR1": comp(), "W1": comp(), "RJ1": comp()},
               {"R1": crea(), "PR1": crea(), "W1": crea(), "RJ1": crea()})


def test_all_five_states_in_one_run_impl(cfg):
    res = _all_states(cfg)
    assert {d["product_id"]: d["decision_state"] for d in res["decisions"]} == {
        "R1": DE.READY, "PR1": DE.PROMISING, "W1": DE.WATCH, "RJ1": DE.REJECT, "I1": DE.INSUFFICIENT}


def test_report_generation_impl(cfg, tmp_path=None):
    res = _all_states(cfg)
    out = DE.write_reports(res, tmp_path)
    md = Path(out["markdown"]).read_text()
    assert Path(out["markdown"]).name == "2026-09-30-final-decision.md"
    for h in ["## 1. Executive summary", "## 2. Shortlist", "## 3. Evidence matrix", "## 4. Decision state",
              "## 5. Decision confidence", "## 6. Main risks", "## 7. Missing validation",
              "## 8. Manual validation checklist", "## 9. Rejected products", "## 10. Watchlist"]:
        assert h in md
    assert "Reject One" in md                               # rejected never hidden
    assert (tmp_path / "latest-final-decision.md").read_text() == md
    out2 = DE.write_reports(res, tmp_path)                   # dated file never overwritten
    assert out2["markdown"].endswith("2026-09-30-final-decision-2.md")


def test_json_generation_impl(cfg, tmp_path=None):
    res = _all_states(cfg)
    js = json.loads(Path(DE.write_reports(res, tmp_path)["json"]).read_text())
    for k in ("metadata", "shortlist", "promising_products", "watchlist", "rejected_products", "insufficient_data",
              "decision_rules_version"):
        assert k in js
    assert js["decision_rules_version"] == "Z-v1"
    m = js["metadata"]
    assert m["scoring_config_hash"] != "N/A" and m["runtime_config_hash"] != "N/A" and m["timestamp"]
    assert js["rejected_products"][0]["reject_reasons"]
    assert js["insufficient_data"][0]["required_next_queries"]
    saved = DE.load_saved(tmp_path)
    assert DE.explain_decision("R1", saved)["decision_state"] == DE.READY


def test_no_secret_leakage_impl(cfg, tmp_path=None):
    fake = "kp_live_SYNTHETIC_SECRET_123456"
    p = product(pid="S1", name=f"Leaky {fake}")
    p["deep"]["api_token"] = fake
    res = run([p], cfg)
    out = DE.write_reports(res, tmp_path, secrets=[fake])
    for f in (out["markdown"], out["json"]):
        assert fake not in Path(f).read_text()


def test_forbidden_language_blocked_impl(cfg):
    res = _all_states(cfg)
    md = DE.render_markdown(res)
    DE.check_language(md, cfg)                               # our own output is clean
    try:
        DE.check_language(md + "\nThis is a guaranteed winner", cfg)
        raise AssertionError("forbidden language not blocked")
    except ValueError:
        pass


def test_checklist_defaults_pending_and_overrides_impl(cfg):
    cl = DE.checklist("P1", cfg, {"P1": {"supplier_sample": {"status": "COMPLETE"}, "packaging_considerations":
                                         {"status": "NOT_APPLICABLE"}, "returns_risk": {"status": "BOGUS"}}})
    assert len(cl) == 12
    assert cl["supplier_sample"]["status"] == "COMPLETE"
    assert cl["packaging_considerations"]["status"] == "NOT_APPLICABLE"
    assert cl["returns_risk"]["status"] == "PENDING" and cl["meta_ads_review"]["status"] == "PENDING"


def test_no_formula_changes_inputs_untouched_impl(cfg):
    p = product()
    before = copy.deepcopy(p)
    decide(p, cfg)
    assert p == before


class DecisionEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = DE.load_cfg()

    def test_ready(self):
        test_ready_impl(self.cfg)

    def test_promising_lists_exact_tasks(self):
        test_promising_lists_exact_tasks_impl(self.cfg)

    def test_watchlist_has_move_conditions(self):
        test_watchlist_has_move_conditions_impl(self.cfg)

    def test_reject_by_hard_gate(self):
        test_reject_by_hard_gate_impl(self.cfg)

    def test_insufficient_data_lists_missing_and_next_queries(self):
        test_insufficient_data_lists_missing_and_next_queries_impl(self.cfg)

    def test_strong_wps_poor_bvs(self):
        test_strong_wps_poor_bvs_impl(self.cfg)

    def test_strong_wps_missing_supplier_economics_is_promising_not_ready(self):
        test_strong_wps_missing_supplier_economics_is_promising_not_ready_impl(self.cfg)

    def test_strong_commercial_weak_demand(self):
        test_strong_commercial_weak_demand_impl(self.cfg)

    def test_high_saturation_high_demand_not_rejected(self):
        test_high_saturation_high_demand_not_rejected_impl(self.cfg)

    def test_extreme_competition_with_weak_demand_rejected(self):
        test_extreme_competition_with_weak_demand_rejected_impl(self.cfg)

    def test_low_competition_low_demand(self):
        test_low_competition_low_demand_impl(self.cfg)

    def test_missing_creative_data_is_unknown_not_reject(self):
        test_missing_creative_data_is_unknown_not_reject_impl(self.cfg)

    def test_missing_amazon_data_allows_ready(self):
        test_missing_amazon_data_allows_ready_impl(self.cfg)

    def test_weak_amazon_blocks_ready(self):
        test_weak_amazon_blocks_ready_impl(self.cfg)

    def test_low_confidence_makes_dimension_unknown(self):
        test_low_confidence_makes_dimension_unknown_impl(self.cfg)

    def test_missing_optional_metric_never_hard_rejects(self):
        test_missing_optional_metric_never_hard_rejects_impl(self.cfg)

    def test_hard_reject_gates(self):
        test_hard_reject_gates_impl(self.cfg)

    def test_dependency_with_broad_evidence_not_rejected(self):
        test_dependency_with_broad_evidence_not_rejected_impl(self.cfg)

    def test_integrity_error_rejects_mixed_environment(self):
        test_integrity_error_rejects_mixed_environment_impl(self.cfg)

    def test_ip_review_blocks_ready_until_manual_check(self):
        test_ip_review_blocks_ready_until_manual_check_impl(self.cfg)

    def test_counterfeit_severe_rejects_unless_resolved(self):
        test_counterfeit_severe_rejects_unless_resolved_impl(self.cfg)

    def test_supplier_match_unreliable_blocks_ready(self):
        test_supplier_match_unreliable_blocks_ready_impl(self.cfg)

    def test_decision_confidence_not_raised_by_performance(self):
        test_decision_confidence_not_raised_by_performance_impl(self.cfg)

    def test_decision_confidence_falls_with_missing_evidence(self):
        test_decision_confidence_falls_with_missing_evidence_impl(self.cfg)

    def test_shortlist_max_three(self):
        test_shortlist_max_three_impl(self.cfg)

    def test_shortlist_not_filled_when_fewer_qualify(self):
        test_shortlist_not_filled_when_fewer_qualify_impl(self.cfg)

    def test_shortlist_tie_break_deterministic(self):
        test_shortlist_tie_break_deterministic_impl(self.cfg)

    def test_traceability(self):
        test_traceability_impl(self.cfg)

    def test_all_five_states_in_one_run(self):
        test_all_five_states_in_one_run_impl(self.cfg)

    def test_report_generation(self):
        with tempfile.TemporaryDirectory() as t:
            test_report_generation_impl(self.cfg, Path(t))

    def test_json_generation(self):
        with tempfile.TemporaryDirectory() as t:
            test_json_generation_impl(self.cfg, Path(t))

    def test_no_secret_leakage(self):
        with tempfile.TemporaryDirectory() as t:
            test_no_secret_leakage_impl(self.cfg, Path(t))

    def test_forbidden_language_blocked(self):
        test_forbidden_language_blocked_impl(self.cfg)

    def test_checklist_defaults_pending_and_overrides(self):
        test_checklist_defaults_pending_and_overrides_impl(self.cfg)

    def test_no_formula_changes_inputs_untouched(self):
        test_no_formula_changes_inputs_untouched_impl(self.cfg)


if __name__ == "__main__":
    unittest.main()
