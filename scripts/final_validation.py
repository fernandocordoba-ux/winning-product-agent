"""FINAL STEP — Product Validation & Launch Gate.

    python -m winning_product_agent final-validation --latest [--product-id ID] [--report-only]

Turns the latest production run into a go / no-go workflow for a real Shopify TEST:
    candidates (max 3)  ->  supplier / economics / market / competition / creative / risk / sample checks
    ->  LAUNCH_TEST_READY | VALIDATE_MORE | HOLD | REJECT  (deterministic, config/final_validation.yaml)
    ->  reports/production/final-validation/{product_id}.md + FINAL-DASHBOARD.md + manual action queue

No new research score and no provider query. Nothing is ordered, no supplier is contacted, no ad money is
spent and nothing is launched: every real-world step is a MANUAL action for the user.
Manual evidence lives in data/production/final_validation/inputs/{product_id}.json.
"""
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
if str(ROOT) not in sys.path:
    sys.path.insert(1, str(ROOT))

NA = "N/A"
LAUNCH, VALIDATE, HOLD, REJECT = "LAUNCH_TEST_READY", "VALIDATE_MORE", "HOLD", "REJECT"
READY, PROMISING = "READY_FOR_PRODUCT_VALIDATION", "PROMISING_NEEDS_VALIDATION"
REVIEW_STATES = ("PASSED", "FAILED", "PENDING")
INPUT_TEMPLATE = {
    "sample_status": "NOT_ORDERED",
    "sample_checklist": {},                 # item -> true / false (see config sample.checklist)
    "sample_notes": None,
    "sample_available": None,               # supplier offers samples: true / false
    "final_quote_confirmed": False,         # the user confirmed final product cost + shipping with the supplier
    "final_product_cost": None,
    "final_shipping_cost": None,
    "other_per_order_costs": None,          # e.g. packaging insert, duty (USD per order), only if known
    "shopify_selling_price": None,
    "reviews": {"ip_trademark": "PENDING", "ad_policy": "PENDING"},
    "notes": None,
}


def load_cfg(path=None):
    return yaml.safe_load(Path(path or ROOT / "config" / "final_validation.yaml").read_text())


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _r(x, n=2):
    return None if x is None else round(x, n)


def _f(x):
    if x is None or x == NA:
        return NA
    return f"{x:g}" if isinstance(x, float) else str(x)


# ============================================================================ 1. input
def latest_production_run(root=ROOT, data_root=None):
    """(manifest, run_dir) of the latest production LIVE run that produced a final decision, else (None, None)."""
    import promotion as PROMO
    try:
        rt = yaml.safe_load((PROMO.active_dir(root) / "runtime.yaml").read_text())
    except FileNotFoundError:
        return None, None
    runs = Path(data_root or root) / rt["paths"]["runs"]
    best = None
    for mf in sorted(runs.glob("*/manifest.json")) if runs.exists() else []:
        m = json.loads(mf.read_text())
        if m.get("mode") == "LIVE" and (m.get("outputs") or {}).get("final_decision_json"):
            best = (m, mf.parent)
    if best:                                     # a newer free rebuild (--report-only [--rescore]) of that run wins
        m, d = best
        for mf in sorted(runs.glob("*/manifest.json")):
            r = json.loads(mf.read_text())
            if r.get("report_only_source_run") == m.get("run_id") and (r.get("outputs") or {}).get("final_decision_json"):
                m = {**m, "outputs": {**(m.get("outputs") or {}), "final_decision_json": r["outputs"]["final_decision_json"]},
                     "config_version": r.get("config_version", m.get("config_version")), "rebuilt_by": r.get("run_id")}
        best = (m, d)
    return best or (None, None)


def _read(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError, TypeError):
        return None


def load_candidates(root=ROOT, data_root=None, product_id=None, cfg=None):
    """Candidates (max 3) from the latest production run, with every record the checks need."""
    cfg = cfg or load_cfg()
    m, run_dir = latest_production_run(root, data_root)
    if not m:
        return None, []
    dec = _read(m["outputs"]["final_decision_json"]) or {}
    decisions = {d["product_id"]: d for k in ("ready_products", "promising_products", "watchlist", "rejected_products",
                                                "insufficient_data") for d in dec.get(k) or []}
    ready = [s["product_id"] for s in dec.get("shortlist") or []]
    if product_id:
        pool = [product_id] if product_id in decisions else []
    elif ready:
        pool = ready
    else:
        prom = [d for d in dec.get("promising_products") or []]
        prom.sort(key=lambda d: (-d["decision_confidence"]["score"], d["product_id"]))
        pool = [d["product_id"] for d in prom]
    pool = pool[: cfg["candidates"]["max_products"]]
    ck = run_dir / "checkpoints"
    deep = {str(x.get("product_id")): x for x in (_read(ck / "deep_analysis.json") or {}).get("results") or []}
    bvs = {str(x.get("product_id")): x for x in (_read(ck / "bvs.json") or {}).get("results") or []}
    config_dir = Path(m.get("config_dir") or "")
    out = []
    import config_resolver as CR
    import competitors as CI
    import creatives as CRE
    for pid in pool:
        d = decisions[pid]
        comp = cre = None
        processed = Path(data_root or root) / m["production_data_paths"]["processed"] if m.get(
            "production_data_paths") else None
        if processed and config_dir.exists():
            with CR.active(config_dir):
                obs = CI.load_latest(pid, processed / "competitors")
                if obs:
                    comp = CI.analyze_product(pid, deep.get(pid) or {}, obs[:10])
                cs = CRE.load_all(pid, processed / "creatives")[:20]
                if cs:
                    cre = CRE.analyze_product(pid, {"product_name": (deep.get(pid) or {}).get("product_name"),
                                                    "units": (deep.get(pid) or {}).get("units")}, cs)
        out.append({"product_id": pid, "name": d.get("name"), "source_state": d["decision_state"], "decision": d,
                    "deep": deep.get(pid), "bvs": bvs.get(pid), "competitor": comp, "creative": cre})
    meta = {"run_id": m["run_id"], "config_version": m.get("config_version"), "config_dir": str(config_dir),
            "data_environment": m.get("data_environment"), "decision_file": m["outputs"]["final_decision_json"]}
    return meta, out


def manual_inputs(inputs_dir, pid, create=True):
    """Manual evidence for one product. A template is created once and never overwritten."""
    p = Path(inputs_dir) / f"{pid}.json"
    if not p.exists():
        if create:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"product_id": pid, **INPUT_TEMPLATE}, indent=2))
        return dict(INPUT_TEMPLATE), str(p)
    data = json.loads(p.read_text())
    merged = {**INPUT_TEMPLATE, **data}
    merged["reviews"] = {**INPUT_TEMPLATE["reviews"], **(data.get("reviews") or {})}
    return merged, str(p)


# ============================================================================ 4. unit economics
def fees_config(config_dir=None):
    import config_resolver as CR
    if config_dir and Path(config_dir).exists():
        with CR.active(config_dir):
            bv = yaml.safe_load(Path(CR.path("business_viability.yaml")).read_text())
    else:
        bv = yaml.safe_load(Path(CR.path("business_viability.yaml")).read_text())
    e = (bv.get("business_viability") or bv)["economics"]
    return {"payment_processing": e["payment_processing"], "platform_fee": e["platform_fee"],
            "refund_rate_pct": e.get("estimated_refund_rate_pct"),
            "chargeback_rate_pct": e.get("estimated_chargeback_rate_pct"), "chargeback_fee": e.get("chargeback_fee")}


def unit_economics(selling_price, product_cost, shipping_cost, fees, scenarios, other_per_order=None):
    """Deterministic unit economics. Missing inputs -> None (never estimated). CAC is never assumed:
    scenarios are CAC = x % of the selling price."""
    sp, pc, sh = _num(selling_price), _num(product_cost), _num(shipping_cost)
    other = _num(other_per_order) or 0.0
    landed = pc + sh + other if None not in (pc, sh) else None
    pay, plat = fees["payment_processing"], fees["platform_fee"]
    tx = (sp * pay["percent"] / 100 + pay["fixed"] + sp * plat["percent"] / 100 + plat["fixed"]) if sp is not None else None
    gross = sp - landed - tx if None not in (sp, landed, tx) else None
    gm = gross / sp * 100 if gross is not None and sp else None
    rr = _num(fees.get("refund_rate_pct"))
    refund = rr / 100 * sp if None not in (rr, sp) else None
    cbr = _num(fees.get("chargeback_rate_pct"))
    chargeback = cbr / 100 * (sp + (fees.get("chargeback_fee") or 0)) if None not in (cbr, sp) else None
    be = gross - (refund or 0.0) - (chargeback or 0.0) if gross is not None else None
    rows = []
    for pct in scenarios:
        cac = sp * pct / 100 if sp is not None else None
        cp = be - cac if None not in (be, cac) else None
        rows.append({"cac_pct_of_price": pct, "cac": _r(cac), "contribution_profit": _r(cp),
                     "contribution_margin_percent": _r(cp / sp * 100) if cp is not None and sp else None})
    return {"selling_price": _r(sp), "product_cost": _r(pc), "shipping_cost": _r(sh), "other_per_order_costs": _r(other),
            "landed_cost": _r(landed), "transaction_fees": _r(tx), "gross_profit": _r(gross),
            "gross_margin_percent": _r(gm), "expected_refund_cost": _r(refund), "expected_chargeback_cost": _r(chargeback),
            "break_even_cac": _r(be),
            "refund_note": ("refund allowance from configured rate" if refund is not None else
                            "refund allowance N/A: no refund rate configured (never assumed); break-even CAC excludes "
                            "refunds"),
            "cac_scenarios": rows}


# ============================================================================ 2-5. checks + launch state
def evaluate(c, inputs, cfg, fees):
    d = c["decision"]
    ev = d.get("evidence") or {}
    val = lambda k: ((ev.get("values") or {}).get(k) or {}).get("value")  # noqa: E731
    flags = set((ev.get("flags") or {}).keys())
    dims = d.get("dimension_status") or {}
    b = c.get("bvs") or {}
    layer = b.get("supplier_layer") or {}
    sel = layer.get("selected") or {}
    econ_run = b.get("economics") or {}
    scfg, ecfg, mcfg, rcfg = cfg["supplier"], cfg["economics"], cfg["market"], cfg["risk"]
    missing, critical, hold = [], [], []

    # ---------------- supplier
    delivery = _num(sel.get("effective_delivery_days"))
    sup = {"selected_offer": sel.get("offer_id"), "supplier": sel.get("supplier_name"),
           "match_class": sel.get("match_class"), "reliable_match": sel.get("match_class") in scfg["reliable_match_classes"],
           "final_quote_confirmed": bool(inputs.get("final_quote_confirmed")),
           "delivery_days": delivery, "tracking": sel.get("tracking_available"),
           "rating": sel.get("supplier_rating"), "order_history": sel.get("supplier_order_count"),
           "stock": {"available_quantity": sel.get("available_quantity"), "status": sel.get("inventory_status")},
           "moq": sel.get("minimum_order_quantity"), "sample_available": inputs.get("sample_available"),
           "quality": sel.get("supplier_quality"), "confidence": sel.get("supplier_confidence")}
    if not sel:
        missing.append("supplier: no selected supplier offer (import offers; no economics without them)")
    else:
        if not sup["reliable_match"]:
            missing.append(f"supplier: match class {sup['match_class']} is not a reliable match")
        if scfg["require_final_quote_confirmed"] and not sup["final_quote_confirmed"]:
            missing.append("supplier: final product cost + shipping quote not confirmed")
        if delivery is None:
            missing.append("supplier: delivery estimate missing")
        elif delivery > scfg["reject_delivery_days_above"]:
            critical.append(f"shipping: delivery {delivery:g} days > {scfg['reject_delivery_days_above']} (very slow)")
        elif delivery > scfg["max_delivery_days"]:
            missing.append(f"shipping: delivery {delivery:g} days > acceptable {scfg['max_delivery_days']}")
        if scfg["require_tracking"] and sel.get("tracking_available") is not True:
            missing.append("supplier: tracking not confirmed")
        if sel.get("supplier_rating") is None or sel.get("supplier_order_count") is None:
            missing.append("supplier: rating / order history missing")
        if sel.get("inventory_status") in ("OUT_OF_STOCK",) or (_num(sel.get("available_quantity")) == 0):
            critical.append("supplier: selected offer out of stock")
        if inputs.get("sample_available") is not True:
            missing.append("supplier: sample availability not confirmed")
    supplier_validated = bool(sel) and sup["reliable_match"] and sup["final_quote_confirmed"] and \
        delivery is not None and delivery <= scfg["max_delivery_days"] and \
        (not scfg["require_tracking"] or sel.get("tracking_available") is True)

    # ---------------- economics
    price_final = _num(inputs.get("shopify_selling_price"))
    price = price_final if price_final is not None else _num(econ_run.get("selling_price"))
    confirmed = sup["final_quote_confirmed"]
    pc = _num(inputs.get("final_product_cost")) if confirmed and inputs.get("final_product_cost") is not None \
        else _num(sel.get("product_cost"))
    sh = _num(inputs.get("final_shipping_cost")) if confirmed and inputs.get("final_shipping_cost") is not None \
        else _num(sel.get("shipping_cost"))
    ue = unit_economics(price, pc, sh, fees, ecfg["cac_scenarios_pct_of_price"], inputs.get("other_per_order_costs"))
    ue["selling_price_basis"] = ("Shopify price set by the user" if price_final is not None else
                                 "REFERENCE only (TikTok average price) — set shopify_selling_price")
    ue["cost_basis"] = "final confirmed quote" if confirmed else "selected supplier offer (not yet confirmed)"
    positive = ue["break_even_cac"] is not None and ue["break_even_cac"] > 0 and \
        (ue["gross_margin_percent"] or 0) >= ecfg["min_gross_margin_percent"]
    if price_final is None:
        missing.append("economics: Shopify selling price not set")
    if ue["break_even_cac"] is None:
        missing.append("economics: unit economics not computable (supplier cost / shipping / price missing)")
    elif ue["break_even_cac"] <= 0:
        (critical if confirmed and price_final is not None else hold).append(
            f"economics: break-even CAC {ue['break_even_cac']} <= 0 at price {ue['selling_price']}")
    elif (ue["gross_margin_percent"] or 0) < ecfg["min_gross_margin_percent"]:
        hold.append(f"economics: gross margin {ue['gross_margin_percent']} % < {ecfg['min_gross_margin_percent']} %")

    # ---------------- market / competition / creative (existing outputs only)
    trend = val("trend")
    market = {"wps": val("wps"), "wps_confidence": val("wps_confidence"), "momentum": val("momentum_score"),
              "momentum_confidence": val("momentum_confidence"), "avs": val("avs"),
              "amazon_confidence": val("amazon_confidence"), "trend": trend, "growth_30d_pct": val("growth_30d_pct"),
              "decision_state": d["decision_state"], "decision_confidence": d["decision_confidence"]["score"],
              "dimensions": dims}
    for k in mcfg["hold_if_weak"]:
        if dims.get(k) == "WEAK":
            hold.append(f"market: {k} WEAK")
    if trend in mcfg["hold_on_trend"]:
        hold.append(f"market: daily trend {trend}")
    for k, ok in mcfg["launch_dimensions_required"].items():
        if dims.get(k) not in ok and not (dims.get(k) == "WEAK" and k in mcfg["hold_if_weak"]):
            missing.append(f"{k}: {dims.get(k)} (needs {'/'.join(ok)})")
    if d["decision_confidence"]["score"] < mcfg["min_decision_confidence"]:
        missing.append(f"decision confidence {d['decision_confidence']['score']} < {mcfg['min_decision_confidence']}")
    if d["decision_state"] == "REJECT":
        critical.append("decision engine: REJECT")
    comp = c.get("competitor") or {}
    competition = {"direct_competitors": comp.get("direct_competitors"),
                   "median_price": (comp.get("prices") or {}).get("median_price"),
                   "active_meta_advertisers": (comp.get("ads") or {}).get("active_meta_advertisers"),
                   "total_active_ads": (comp.get("ads") or {}).get("total_active_ads"),
                   "saturation": val("competitor_saturation"), "opportunity": val("competitor_opportunity"),
                   "price_compression": "PRICE_COMPRESSION" in flags,
                   "differentiation_gaps": [o.get("opportunity") or o.get("gap") or str(o) if isinstance(o, dict) else str(o)
                                            for o in ((comp.get("differentiation") or {}).get("opportunities") or [])],
                   "status": dims.get("competitive_environment")}
    cre = c.get("creative") or {}
    angles = [(k, v) for k, v in sorted(((cre.get("angles") or {}).get("distribution") or {}).items(),
                                        key=lambda kv: -kv[1]) if k != "UNKNOWN"]
    hyps = cre.get("creative_test_hypotheses") or []
    creative = {"opportunity": val("creative_opportunity"), "saturation": val("creative_saturation"),
                "confidence": val("creative_confidence"), "status": dims.get("creative_opportunity"),
                "angles": [a for a, _ in angles[:3]],
                "hooks_to_test": list(dict.fromkeys(h.get("hook") for h in hyps if h.get("hook")))[:3],
                "formats": list(dict.fromkeys(h.get("format") for h in hyps if h.get("format")))[:3],
                "demo_potential": (cre.get("opportunity") or {}).get("components", {}).get("demonstration_potential"),
                "hypotheses": hyps[:3], "gaps": [g["gap"] for g in cre.get("creative_gaps") or []]}

    # ---------------- risk
    reviews = inputs.get("reviews") or {}
    severe = sorted(flags & set(rcfg["severe_flags"]))
    risk = {"severe_flags": severe, "shipping_flags": sorted(flags & set(rcfg["shipping_flags"])),
            "return_flags": sorted(flags & set(rcfg["return_flags"])),
            "quality_flags": sorted(flags & set(rcfg["quality_flags"])), "reviews": reviews,
            "all_flags": sorted(flags)}
    for rv in rcfg["manual_reviews"]:
        st = reviews.get(rv, "PENDING")
        if st == "FAILED":
            critical.append(f"risk: {rv} review FAILED")
        elif st != "PASSED":
            missing.append(f"risk: {rv} review {st}")
    if severe and reviews.get("ip_trademark") != "PASSED":
        missing.append(f"risk: unresolved severe flag(s) {severe}")
    if risk["return_flags"]:
        missing.append(f"risk: return risk {risk['return_flags']}")
    if risk["quality_flags"]:
        missing.append(f"risk: quality {risk['quality_flags']}")

    # ---------------- sample gate
    sst = inputs.get("sample_status") or "NOT_ORDERED"
    if sst not in cfg["sample"]["states"]:
        sst = "NOT_ORDERED"
    checklist = {item: (inputs.get("sample_checklist") or {}).get(item) for item in cfg["sample"]["checklist"]}
    if sst == "REJECTED":
        critical.append("sample: REJECTED")
    elif sst != cfg["sample"]["required_for_launch"]:
        missing.append(f"sample: {sst} (needs APPROVED)")
    if sst == "APPROVED" and not all(v is True for v in checklist.values()):
        missing.append("sample: APPROVED but checklist incomplete")

    # ---------------- launch state (deterministic)
    launch_ok = (c["source_state"] == cfg["candidates"]["launch_source_state"] and not missing and not hold
                 and not critical and supplier_validated and positive)
    if critical:
        state = REJECT
    elif hold:
        state = HOLD
    elif launch_ok:
        state = LAUNCH
    else:
        state = VALIDATE
    if c["source_state"] != cfg["candidates"]["launch_source_state"] and state == LAUNCH:
        state = VALIDATE
    reasons = {"critical": critical, "hold": hold, "missing": missing}
    if c["source_state"] != cfg["candidates"]["launch_source_state"]:
        missing.insert(0, f"source state {c['source_state']}: validation only, not eligible for launch")
    result = {"product_id": c["product_id"], "name": c["name"], "source_state": c["source_state"], "state": state,
              "reasons": reasons, "supplier": sup, "supplier_validated": supplier_validated, "economics": ue,
              "positive_unit_economics": positive, "market": market, "competition": competition, "creative": creative,
              "risk": risk, "sample": {"status": sst, "checklist": checklist, "notes": inputs.get("sample_notes")}}
    result["action_queue"] = action_queue(result, cfg)
    result["test_plan"] = test_plan(result, cfg) if state == LAUNCH else None
    return result


# ============================================================================ 10. manual action queue
def action_queue(r, cfg):
    sup, s, ue = r["supplier"], r["sample"], r["economics"]
    q = []

    def add(cond, text):
        if cond:
            q.append(text)
    add(not sup["selected_offer"], "Import real supplier offers for this product (no order, no supplier contact by the system)")
    add(sup["selected_offer"] and not sup["reliable_match"], "Confirm a reliable supplier match (same product)")
    add(bool(sup["selected_offer"]) and not sup["final_quote_confirmed"], "Confirm final product cost + shipping quote")
    add(bool(sup["selected_offer"]) and (sup["tracking"] is not True or sup["delivery_days"] is None),
        "Confirm tracking and a realistic delivery estimate")
    add(sup["sample_available"] is not True, "Confirm sample availability")
    add(s["status"] == "NOT_ORDERED", "Order sample (manual — the system never orders)")
    add(s["status"] == "ORDERED", "Wait for the sample")
    add(s["status"] in ("RECEIVED",) or (s["status"] == "APPROVED" and not all(v is True for v in s["checklist"].values())),
        "Inspect product quality (sample checklist)")
    add(r["risk"]["reviews"].get("ip_trademark") != "PASSED", "Confirm IP / trademark")
    add(r["risk"]["reviews"].get("ad_policy") != "PASSED", "Confirm platform / ad-policy compliance")
    add(ue["selling_price_basis"].startswith("REFERENCE"), "Finalize Shopify selling price")
    add(r["competition"]["status"] in (None, "UNKNOWN"), "Import competitor research (direct competitors)")
    add(r["creative"]["status"] in (None, "UNKNOWN"), "Import classified creative research (hooks / angles)")
    if r["state"] == LAUNCH:
        q.append("Build landing page")
        q.append("Produce 3 creatives (from the creative hypotheses)")
        tc = cfg["test_controls"]
        add(any(tc.get(k) is None for k in ("max_test_budget", "max_daily_budget", "max_test_days")) or
            any(v is None for v in (tc.get("stop_loss_thresholds") or {}).values()),
            "Define test budget and stop-loss thresholds (config/final_validation.yaml test_controls)")
        q.append("Launch controlled Shopify test (manual — the system never spends ad money)")
    return q


# ============================================================================ 7-8. test plan + stop-loss
def test_plan(r, cfg):
    tc = cfg["test_controls"]
    unset = [k for k in ("max_test_budget", "max_daily_budget", "max_test_days") if tc.get(k) is None]
    unset += [f"stop_loss_thresholds.{k}" for k, v in (tc.get("stop_loss_thresholds") or {}).items() if v is None]
    comp, cre = r["competition"], r["creative"]
    return {"recommended_initial_selling_price": r["economics"]["selling_price"],
            "offer_hypothesis": (f"differentiate on: {', '.join(comp['differentiation_gaps'][:2])}"
                                 if comp["differentiation_gaps"] else NA),
            "landing_page_hypothesis": (f"lead with angle {cre['angles'][0]}" if cre["angles"] else
                                        "N/A — no classified angle evidence"),
            "creative_hypotheses": cre["hypotheses"][:3],
            "metrics_to_observe": cfg["metrics_to_observe"],
            "benchmarks": "not set — the system does not invent acceptable benchmark values",
            "test_controls": tc, "unset_controls": unset,
            "spend_status": "BLOCKED until every test control and stop-loss threshold is set by the user" if unset
            else "controls set by the user — spending remains a manual action"}


# ============================================================================ 6 + 9. rendering
def render_pack(r, meta, input_path):
    ue, sup, m, comp, cre = r["economics"], r["supplier"], r["market"], r["competition"], r["creative"]
    L = [f"# Final validation — {r['name']}", "",
         f"**Final validation state: {r['state']}** · source decision {r['source_state']} · run `{meta.get('run_id')}` · "
         f"config {meta.get('config_version')} · product_id `{r['product_id']}`", "",
         "> Nothing is ordered, no supplier is contacted, no ad money is spent and nothing is launched by the system.",
         f"> Manual evidence file: `{input_path}`", "",
         "## Product", "", f"- Decision Confidence {m['decision_confidence']} · dimensions: "
         + ", ".join(f"{k} {v}" for k, v in (m["dimensions"] or {}).items()), "",
         "## Market", "", f"- WPS {_f(m['wps'])} (confidence {_f(m['wps_confidence'])}) · Momentum {_f(m['momentum'])} "
         f"(confidence {_f(m['momentum_confidence'])}) · trend {_f(m['trend'])} · 30D growth {_f(m['growth_30d_pct'])} %",
         f"- Amazon: AVS {_f(m['avs'])} · Amazon Confidence {_f(m['amazon_confidence'])}", "",
         "## Supplier", "",
         f"- Selected: {_f(sup['supplier'])} (offer {_f(sup['selected_offer'])}, match {_f(sup['match_class'])})",
         f"- Cost {_f(ue['product_cost'])} · shipping {_f(ue['shipping_cost'])} · delivery {_f(sup['delivery_days'])} days · "
         f"tracking {_f(sup['tracking'])} · rating {_f(sup['rating'])} · orders {_f(sup['order_history'])} · MOQ "
         f"{_f(sup['moq'])} · stock {_f(sup['stock']['available_quantity'])} ({_f(sup['stock']['status'])}) · sample "
         f"available {_f(sup['sample_available'])}",
         f"- Quality {_f(sup['quality'])} · confidence {_f(sup['confidence'])} · final quote confirmed "
         f"{sup['final_quote_confirmed']} · supplier validated {r['supplier_validated']}", "",
         "## Economics", "",
         f"- Selling price {_f(ue['selling_price'])} ({ue['selling_price_basis']}) · costs: {ue['cost_basis']}",
         f"- Landed cost {_f(ue['landed_cost'])} · transaction fees {_f(ue['transaction_fees'])} · gross profit "
         f"{_f(ue['gross_profit'])} · gross margin {_f(ue['gross_margin_percent'])} %",
         f"- Break-even CAC {_f(ue['break_even_cac'])} — {ue['refund_note']}", "",
         "| CAC (% of price) | CAC | Contribution profit | Contribution margin |", "|---|---|---|---|"]
    L += [f"| {x['cac_pct_of_price']} % | {_f(x['cac'])} | {_f(x['contribution_profit'])} | "
          f"{_f(x['contribution_margin_percent'])} |" for x in ue["cac_scenarios"]]
    L += ["", "CAC scenarios are hypotheses (x % of price); actual CAC is unknown until a test runs.", "",
          "## Competition", "",
          f"- Direct competitors {_f(comp['direct_competitors'])} · median price {_f(comp['median_price'])} · active Meta "
          f"advertisers {_f(comp['active_meta_advertisers'])} · saturation {_f(comp['saturation'])} · opportunity "
          f"{_f(comp['opportunity'])} · price compression {comp['price_compression']}",
          f"- Differentiation gaps: {', '.join(comp['differentiation_gaps']) or NA}", "",
          "## Creative plan", "",
          f"- 3 strongest evidence-backed angles: {', '.join(cre['angles']) or 'N/A — no classified angle evidence'}",
          f"- 3 hook categories to test: {', '.join(cre['hooks_to_test']) or NA}",
          f"- Recommended formats: {', '.join(cre['formats']) or NA}",
          f"- Creative risks: saturation {_f(cre['saturation'])}, confidence {_f(cre['confidence'])}; gaps "
          f"{', '.join(cre['gaps']) or NA}", "",
          "## Sample checklist", "", f"Status: **{r['sample']['status']}** · notes: {_f(r['sample']['notes'])}", ""]
    L += [f"- [{'x' if v is True else ' '}] {k}{' (failed)' if v is False else ''}" for k, v in r["sample"]["checklist"].items()]
    L += ["", "## Risks", ""]
    rk = r["risk"]
    L += [f"- Severe: {', '.join(rk['severe_flags']) or 'none'} · shipping: {', '.join(rk['shipping_flags']) or 'none'} · "
          f"returns: {', '.join(rk['return_flags']) or 'none'} · quality: {', '.join(rk['quality_flags']) or 'none'}",
          f"- Manual reviews: {rk['reviews']}", f"- All flags: {', '.join(rk['all_flags']) or 'none'}"]
    L += ["", "## Final missing items", ""]
    L += [f"- CRITICAL: {x}" for x in r["reasons"]["critical"]] + [f"- HOLD: {x}" for x in r["reasons"]["hold"]] + \
         [f"- {x}" for x in r["reasons"]["missing"]] or ["- none"]
    L += ["", "## Manual action queue (in dependency order)", ""] + [f"{i}. {a}" for i, a in enumerate(r["action_queue"], 1)]
    if r["test_plan"]:
        tp = r["test_plan"]
        L += ["", "## Test plan (not executed)", "",
              f"- Recommended initial selling price: {_f(tp['recommended_initial_selling_price'])}",
              f"- Offer hypothesis: {tp['offer_hypothesis']}", f"- Landing-page hypothesis: {tp['landing_page_hypothesis']}",
              "- Creative hypotheses: " + ("; ".join(f"{h.get('format')} / {h.get('hook')} / {h.get('angle')}"
                                                    for h in tp["creative_hypotheses"]) or NA),
              f"- Metrics to observe: {', '.join(tp['metrics_to_observe'])} (benchmarks: {tp['benchmarks']})",
              f"- Stop-loss: {tp['spend_status']}; unset: {', '.join(tp['unset_controls']) or 'none'}"]
    return "\n".join(L) + "\n"


def render_dashboard(results, meta, cfg):
    L = ["# FINAL DASHBOARD — Product Validation & Launch Gate", ""]
    if meta:
        L += [f"Run `{meta['run_id']}` · config {meta['config_version']} · data environment {meta['data_environment']}",
              ""]
    L += ["> Go / no-go for a controlled Shopify TEST. The system never orders, contacts suppliers, spends ad money or "
          "launches.", ""]
    if not results:
        L += ["No candidate: " + ("no production live run with a final decision exists yet — run "
                                  "`python -m winning_product_agent production-run --live` first." if not meta else
                                  "the latest production run has no READY_FOR_PRODUCT_VALIDATION or "
                                  "PROMISING_NEEDS_VALIDATION product."), ""]
    else:
        L += ["| Product | Launch State | WPS | Momentum | BVS | Decision Confidence | Landed Cost | Selling Price | "
              "Gross Margin | Break-even CAC | Supplier Status | Sample Status | Competition | Creative Opportunity | "
              "Main Risk |", "|---" * 15 + "|"]
        for r in results[: cfg["candidates"]["max_products"]]:
            ue, m = r["economics"], r["market"]
            bvs = (r.get("_bvs") or {}).get("bvs")
            miss = [x for x in r["reasons"]["missing"] if not x.startswith("source state")] or r["reasons"]["missing"]
            main = (r["reasons"]["critical"] or r["reasons"]["hold"] or miss or ["none"])[0]
            L.append(f"| {r['name']} | **{r['state']}** | {_f(m['wps'])} | {_f(m['momentum'])} | {_f(bvs)} | "
                     f"{_f(m['decision_confidence'])} | {_f(ue['landed_cost'])} | {_f(ue['selling_price'])} | "
                     f"{_f(ue['gross_margin_percent'])} | {_f(ue['break_even_cac'])} | "
                     f"{'VALIDATED' if r['supplier_validated'] else ('OFFER' if r['supplier']['selected_offer'] else 'NONE')} | "
                     f"{r['sample']['status']} | {_f(r['competition']['status'])} | {_f(r['creative']['status'])} | {main} |")
        L += ["", "## Manual action queue", ""]
        for r in results:
            L += [f"**{r['name']}** ({r['state']})", ""] + [f"{i}. {a}" for i, a in enumerate(r["action_queue"], 1)] + [""]
    return "\n".join(L) + "\n"


# ============================================================================ entry point
def run(root=ROOT, data_root=None, product_id=None, cfg=None, now=None):
    cfg = cfg or load_cfg()
    now = now or datetime.now(timezone.utc)
    base = Path(data_root or root)
    meta, cands = load_candidates(root, data_root, product_id, cfg)
    fees = fees_config((meta or {}).get("config_dir"))
    out_dir = base / "reports" / "production" / "final-validation"
    inputs_dir = base / "data" / "production" / "final_validation" / "inputs"
    results, paths = [], {}
    for c in cands:
        inputs, ipath = manual_inputs(inputs_dir, c["product_id"])
        r = evaluate(c, inputs, cfg, fees)
        r["_bvs"] = c.get("bvs")
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"{c['product_id']}.md"
        p.write_text(render_pack(r, meta or {}, ipath))
        paths[c["product_id"]] = str(p)
        results.append(r)
    dash = base / "reports" / "production" / "FINAL-DASHBOARD.md"
    dash.parent.mkdir(parents=True, exist_ok=True)
    dash.write_text(render_dashboard(results, meta, cfg))
    js = base / "reports" / "production" / "final-validation" / "final-validation.json"
    js.parent.mkdir(parents=True, exist_ok=True)
    js.write_text(json.dumps({"generated_at": now.isoformat(), "run": meta,
                              "results": [{k: v for k, v in r.items() if k != "_bvs"} for r in results]},
                             indent=2, default=str))
    return {"run": meta, "results": results, "packs": paths, "dashboard": str(dash), "json": str(js)}
