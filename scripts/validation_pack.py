"""Step AD — product validation pack for every shortlisted (READY_FOR_PRODUCT_VALIDATION) product.

reports/production/{date}/{product_id}-validation-pack.md. Evidence only: every number comes from the run's
records; missing values stay N/A. The pack prepares MANUAL validation; it never launches a product, orders a
sample or contacts a supplier.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import decision_engine as DE  # noqa: E402
import generate_report as GR  # noqa: E402

NA = "N/A"


def _f(x):
    if x is None or x == NA:
        return NA
    return f"{x:g}" if isinstance(x, float) else str(x)


def render(d, p, ctx, meta):
    ev = d["evidence"]
    v = lambda k: _f(DE.v(ev, k))  # noqa: E731
    deep, amz, b = p.get("deep") or {}, p.get("amazon") or {}, p.get("bvs_record") or {}
    econ = b.get("economics") or {}
    layer = b.get("supplier_layer") or {}
    comp = (ctx.get("competitor") or {}).get(d["product_id"]) or {}
    cre = (ctx.get("creative") or {}).get(d["product_id"]) or {}
    em = p.get("emerging") or {}
    L = [f"# Validation pack — {d['name']}", "",
         f"**CONFIG VERSION:** {meta['config_version']} · **RUN ID:** {meta['run_id']} · **DATA ENVIRONMENT:** "
         f"{meta['env']} · product_id `{d['product_id']}`", "",
         f"> {DE.DISCLAIMER} Nothing has been launched, ordered or sent to a supplier.", "",
         "## 1. Product overview", "",
         f"- Decision: **{d['decision_state']}** · Decision Confidence {d['decision_confidence']['score']}",
         f"- Category: {d.get('category') or NA} · URL: {d.get('url') or NA}",
         f"- Why it passed: " + "; ".join(x["text"] for x in d["why_it_passed"][:8]), "",
         "## 2. TikTok evidence", "",
         f"- WPS {v('wps')} (confidence {v('wps_confidence')}) · trend {v('trend')} · 30D growth {v('growth_30d_pct')} %",
         f"- GMV 30D {_f(deep.get('gmv'))} · units 30D {_f(deep.get('units'))} · creators {v('creator_count')} · "
         f"videos {v('video_count')}",
         f"- Competition: scope {(deep.get('competition_metrics') or {}).get('competition_scope') or NA}, "
         f"count {_f((deep.get('competition_metrics') or {}).get('competition_count'))}",
         f"- Red flags: {', '.join(sorted(ev['flags'])) or 'none'}", "",
         "## 3. Amazon evidence", "",
         f"- Match: {amz.get('amazon_match_status') or NA} (confidence {_f(amz.get('amazon_match_confidence'))}) · AVS "
         f"{v('avs')} · Amazon Confidence {v('amazon_confidence')}",
         f"- Price {_f(amz.get('amazon_price'))} · rating {_f(amz.get('amazon_rating'))} · reviews "
         f"{_f(amz.get('amazon_review_count'))} · BSR {_f(amz.get('amazon_bsr'))}", "",
         "## 4. Supplier comparison", "",
         "| Rank | Supplier | Match | Cost | Shipping | Landed | Delivery (days) | Quality | Confidence | Eligible |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for o in layer.get("offers") or []:
        L.append(f"| {_f(o.get('supplier_offer_rank'))} | {o.get('supplier_name') or NA} | {o.get('match_class') or NA} | "
                 f"{_f(o.get('product_cost'))} | {_f(o.get('shipping_cost'))} | {_f(o.get('landed_cost'))} | "
                 f"{_f(o.get('effective_delivery_days'))} | {_f(o.get('supplier_quality'))} | "
                 f"{_f(o.get('supplier_confidence'))} | {o.get('eligible_for_economics')} |")
    if not layer.get("offers"):
        L.append("| — | no supplier offer | | | | | | | | |")
    L += ["", f"Selected offer: {layer.get('selected_supplier_offer_id') or NA} ({layer.get('selection_rule') or NA})", "",
          "## 5. Product economics", "",
          f"- Selling price {_f(econ.get('selling_price'))} · product cost {_f(econ.get('product_cost'))} · shipping "
          f"{_f(econ.get('supplier_shipping_cost'))} · landed cost {_f(econ.get('landed_cost'))} "
          f"({econ.get('landed_cost_note') or NA})",
          f"- Gross profit before ads {_f(econ.get('gross_profit_before_ads'))} · gross margin "
          f"{_f(econ.get('gross_margin_percent'))} % · contribution margin {_f(econ.get('contribution_margin_percent'))}"
          " (N/A when CAC / refund data is not available — never estimated)",
          f"- BVS {v('bvs')} (confidence {v('bvs_confidence')})", "",
          "## 6. Competitor intelligence", "",
          f"- Direct competitors {_f(comp.get('direct_competitors'))} · saturation {v('competitor_saturation')} · "
          f"opportunity {v('competitor_opportunity')} · confidence {v('competitor_confidence')}",
          f"- Differentiation gaps: {', '.join(g.get('gap', str(g)) if isinstance(g, dict) else str(g) for g in ((comp.get('differentiation') or {}).get('opportunities') or [])) or NA}",
          "", "## 7. Creative intelligence", "",
          f"- Qualified creatives {_f(cre.get('qualified_creatives'))} · saturation {v('creative_saturation')} · "
          f"opportunity {v('creative_opportunity')} · confidence {v('creative_confidence')}",
          f"- Gaps: {', '.join(g['gap'] for g in cre.get('creative_gaps') or []) or NA} (test hypotheses only; no script "
          "or ad copy stored)", "",
          "## 8. Historical momentum", "",
          f"- Emerging status {em.get('emerging_status') or 'INSUFFICIENT_HISTORY'} · Momentum {v('momentum_score')} "
          f"(confidence {v('momentum_confidence')}) · history observations {v('history_observations')}", "",
          "## 9. Decision matrix", "", "| Dimension | Status | Reason |", "|---|---|---|"]
    L += [f"| {DE.DIM_LABEL[k]} | {d['dimension_status'][k]} | {d['dimensions'][k]['reason']} |" for k in DE.DIMENSIONS]
    L += ["", "## 10. Risk checklist", ""]
    L += [f"- [ ] {x['text']}" for x in d["why_it_did_not_pass"]] or ["- [ ] no failed rule; review flags above"]
    L += [f"- [ ] flag {f}" for f in sorted(ev["flags"])]
    L += ["", "## 11. Manual validation checklist", ""]
    L += [f"- [{'x' if s['status'] == 'COMPLETE' else ' '}] {k} ({s['status']})" for k, s in d["manual_checklist"].items()]
    L += ["", "Next actions:", ""] + [f"- {a}" for a in d["next_actions"]]
    return "\n".join(L) + "\n"


def write(r, d, p, ctx, now=None):
    now = now or datetime.now(timezone.utc)
    meta = {"config_version": getattr(r, "config_version", NA), "run_id": r.run_id, "env": r.env}
    out = r.reports_dir / now.strftime("%Y-%m-%d")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{d['product_id']}-validation-pack.md"
    n = 2
    while path.exists():
        path = out / f"{d['product_id']}-validation-pack-{n}.md"
        n += 1
    pats = [x.lower() for x in GR.load_cfg()["secret_key_patterns"]]
    text = GR.scrub(render(d, p, ctx, meta), pats, r.secrets)
    DE.check_language(text, DE.load_cfg())
    path.write_text(text)
    return path
