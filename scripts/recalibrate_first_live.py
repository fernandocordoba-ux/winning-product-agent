"""Offline recalibration of a live run with the current (post-live-calibration) code.

READ-ONLY and FREE: no provider query, nothing written to data/ or history.
Re-normalizes the run's SAVED raw Deep Analysis answers, applies the competition scope rules,
the history snapshot dedupe (read view), the rebuilt Growth Momentum (wps-v2), and recalculates
WPS and Confidence. Writes reports/first-live-recalibration.md (+ .json).

CLI:
  python3 scripts/recalibrate_first_live.py [RUN_ID]      # default: the first complete live run
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
import deep_analysis as DA  # noqa: E402
import discovery as D  # noqa: E402
import safety  # noqa: E402
from score_products import load_scoring_config, wps_breakdown  # noqa: E402

DEFAULT_RUN = "20260930T134127Z_547f9f"
NA = "N/A"


def fnum(v, p=2):
    return NA if v is None or v == NA else (f"{v:,.{p}f}" if isinstance(v, float) else f"{v:,}")


def scale_text(comp, d):
    """Human-readable formula for one component with its numbers."""
    if d.get("s") is None:
        return f"{comp['input']} = {d.get('reason', 'missing')} -> N/A"
    raw = d.get("derived", d.get("raw"))
    z, f = comp.get("zero_at"), comp.get("full_at")
    if comp.get("scale") == "ratio":
        return f"{comp['input']}: share of items {comp['match']} = {d['s']:.4f}"
    if comp.get("scale") == "log10":
        return (f"{comp['input']} = {fnum(raw)}; s = clamp((log10(x) - log10({z})) / (log10({f}) - log10({z}))) "
                f"= {d['s']:.4f}")
    return f"{comp['input']} = {fnum(raw)}; s = clamp((x - ({z})) / ({f} - ({z}))) = {d['s']:.4f}"


def old_competition(rec):
    c = rec.get("competition_metrics") or {}
    flags = [f["flag"] for f in rec.get("red_flags") or []]
    pts = (rec.get("wps_breakdown") or {}).get("competition", {}).get("points")
    return {"category_product_count": c.get("category_product_count"), "scope": "not tracked (v1)",
            "used_in_wps": True, "competition_points": pts, "extreme_saturation": "EXTREME_SATURATION" in flags}


def recalibrate(run_id=DEFAULT_RUN, root=ROOT):
    root = Path(root)
    rt = safety.load_runtime(root / "config" / "runtime.yaml")
    rd = root / rt["paths"]["runs"] / run_id
    deep_ck = json.loads((rd / "checkpoints" / "deep_analysis.json").read_text())
    disc_file = json.loads((rd / "checkpoints" / "discovery.json").read_text())["processed_file"]
    disc = json.loads(Path(disc_file).read_text())
    ctx_by = {c["facts"]["product_id"]: c for c in disc["candidates"] + disc["failed"]}
    cfg_deep, scoring = DA.load_cfg(), load_scoring_config()
    cfgs = {"deep": cfg_deep, "filters": D.load_yaml("filters.yaml"), "scoring": scoring}
    from winning_product_agent.runner import EnvStore
    view = EnvStore(root / rt["paths"]["history"], "LIVE")
    out = []
    for old in deep_ck["results"]:
        if old.get("status") != "ok":
            out.append({"product_id": old.get("product_id"), "status": old.get("status")})
            continue
        pid = str(old["product_id"])
        raw_path = old["source"]["raw_file"]
        env = json.loads(Path(raw_path).read_text())
        ctx = ctx_by[pid]
        new = DA.analyze(env, raw_path, ctx, cfgs, 0)
        obj, _ = DA.extract_object(env)
        f, _, _ = DA.normalize_deep(obj, ctx)
        trend = DA.trend_metrics(f, cfg_deep)
        comp = DA.competition_scope(f, cfg_deep)
        b = wps_breakdown(DA.scoring_input(f, 0, trend, comp), scoring)
        math_lines = []
        for name, m in b["metrics"].items():
            mc = scoring["metrics"][name]
            parts = [scale_text(mc["components"][cn], d) for cn, d in m["components"].items()]
            pts = m["points"]
            math_lines.append({"metric": name, "tier": m["tier"], "points": pts, "max": m["max"],
                               "formula": f"{m['max']} x sum(weight x s)" if pts != NA else "N/A", "components": parts})
        og = (old.get("wps_breakdown") or {}).get("growth_momentum", {}).get("points")
        ng = b["groups"]["growth_momentum"]
        g = trend["gmv"]
        dup = view.duplicate_snapshots(pid)
        out.append({
            "product_id": pid, "name": old.get("product_name"),
            "old": {"wps": old.get("wps"), "wps_version": "wps-v1", "wps_complete": old.get("wps_complete"),
                    "growth_score": og, "confidence": old.get("confidence"), "competition": old_competition(old),
                    "red_flags": [x["flag"] for x in old.get("red_flags") or []]},
            "new": {"wps": new["wps"], "wps_version": scoring["version"], "wps_complete": new["wps_complete"],
                    "points_earned": b["points_earned"], "points_possible": b["points_possible"],
                    "growth_score": ng["points"], "growth_na": ng["na"],
                    "growth_parts": {k: b["metrics"][k]["points"] for k in ng["metrics"]},
                    "growth_inputs": {"revenue_growth_pct": f.get("growth_30d_pct"),
                                      "growth_source": f.get("growth_source"),
                                      "recent_velocity_pct": g.get("velocity_pct"),
                                      "prior_velocity_pct": g.get("prior_velocity_pct"),
                                      "trend_label": trend["label"]},
                    "confidence": new["confidence"], "confidence_before_adjustments": new["confidence_before_adjustments"],
                    "confidence_adjustments": new["confidence_adjustments"],
                    "competition": {k: comp[k] for k in ("competition_count", "competition_scope",
                                                         "competition_comparable", "competition_category_id",
                                                         "competition_subcategory_id")}
                    | {"used_in_wps": comp["competition_comparable"],
                       "extreme_saturation": "EXTREME_SATURATION" in [x["flag"] for x in new["red_flags"]]},
                    "red_flags": [x["flag"] for x in new["red_flags"]],
                    "missing_by_tier": b["missing_by_tier"], "missing_fields": new["missing_data"]},
            "history_duplicates_removed": dup,
            "math": math_lines,
        })
    return {"run_id": run_id, "scoring_version": scoring["version"], "products": out,
            "provider_queries": 0, "note": "offline recalculation from saved raw answers; nothing written to history"}


def render(r):
    L = ["# First live run — offline recalibration", "",
         f"> Run `{r['run_id']}` · recalculated with **{r['scoring_version']}** from the SAVED raw KaloPilot answers · "
         "**0 provider queries** · history not modified (duplicates hidden by the read view only) · "
         "no product is a guaranteed winner", "",
         "| Product | OLD WPS (v1) | NEW WPS (v2) | OLD Growth /25 | NEW Growth /25 | OLD Conf. | NEW Conf. |",
         "|---|---|---|---|---|---|---|"]
    for p in r["products"]:
        if "old" not in p:
            continue
        L.append(f"| {p['name'][:40]} | {p['old']['wps']} | {p['new']['wps']}{'' if p['new']['wps_complete'] else ' (incomplete)'} "
                 f"| {p['old']['growth_score']} | {p['new']['growth_score']} | {p['old']['confidence']} | {p['new']['confidence']} |")
    for p in r["products"]:
        if "old" not in p:
            L += ["", f"## {p['product_id']} — deep analysis {p['status']} (nothing to recalculate)"]
            continue
        o, n = p["old"], p["new"]
        gi = n["growth_inputs"]
        L += ["", f"## {p['name']} ({p['product_id']})", "",
              f"**WPS {o['wps']} -> {n['wps']}**  ·  Confidence {o['confidence']} -> {n['confidence']}", "",
              "### Growth Momentum", "",
              f"- OLD (v1): 25 x clamp(growth / 100) with growth {gi['revenue_growth_pct']} % -> **{o['growth_score']}/25**",
              f"- NEW (v2): long-term + recent trend + acceleration = "
              f"{n['growth_parts']['growth_long_term']} + {n['growth_parts']['growth_recent_trend']} + "
              f"{n['growth_parts']['growth_acceleration']} = **{n['growth_score']}/25**"
              + (f" (N/A parts excluded: {', '.join(n['growth_na'])})" if n["growth_na"] else ""),
              f"  - long-term: 10 x clamp(({gi['revenue_growth_pct']} - 0) / 100)  (growth source: {gi['growth_source']})",
              f"  - recent trend: 10 x clamp(({gi['recent_velocity_pct']} + 20) / 50)  (last 7 days vs previous 7, %)",
              f"  - acceleration: 5 x clamp(({'N/A' if gi['recent_velocity_pct'] is None or gi['prior_velocity_pct'] is None else round(gi['recent_velocity_pct'] - gi['prior_velocity_pct'], 2)} + 30) / 60)"
              f"  (velocity {gi['recent_velocity_pct']} - prior {gi['prior_velocity_pct']}); daily trend label {gi['trend_label']}",
              "", "### Competition", "",
              f"- OLD: category_product_count {fnum(o['competition']['category_product_count'])} used as-is (no scope); "
              f"competition points {o['competition']['competition_points']}/15; EXTREME_SATURATION: "
              f"{'YES' if o['competition']['extreme_saturation'] else 'no'}",
              f"- NEW: count {fnum(n['competition']['competition_count'])}, scope **{n['competition']['competition_scope']}**, "
              f"comparable **{n['competition']['competition_comparable']}** -> "
              + ("scored in WPS" if n["competition"]["used_in_wps"] else
                 "NOT scored (saturation metric excluded from the WPS denominator; Confidence reduced instead)")
              + f"; EXTREME_SATURATION: {'YES' if n['competition']['extreme_saturation'] else 'no'}",
              "", "### WPS arithmetic (v2)", "",
              f"WPS = 100 x earned / possible = 100 x {n['points_earned']} / {n['points_possible']} = **{n['wps']}**  "
              "(possible = all CORE points + available SUPPORTING points)", "",
              "| Metric | Tier | Points | Formula |", "|---|---|---|---|"]
        for m in p["math"]:
            L.append(f"| {m['metric']} | {m['tier']} | {m['points']}/{m['max']} | {'; '.join(m['components'])} |")
        L += ["", f"OLD WPS (v1) = sum of 7 metric points with N/A = 0 (100 possible) = {o['wps']}.", "",
              "### Confidence", "",
              f"{n['confidence_before_adjustments']} (evidence completeness) "
              + " ".join(f"{a['points']:+} ({a['reason']})" for a in n["confidence_adjustments"])
              + f" = **{n['confidence']}**", "",
              "### Missing metrics", "",
              f"- CORE missing: {', '.join(n['missing_by_tier']['CORE']) or 'none'}",
              f"- SUPPORTING missing: {', '.join(n['missing_by_tier']['SUPPORTING']) or 'none'}",
              f"- Missing source fields: {', '.join(n['missing_fields']) or 'none'}",
              f"- Red flags OLD: {', '.join(o['red_flags']) or 'none'}  ·  NEW: {', '.join(n['red_flags']) or 'none'}",
              "", f"### History duplicates removed: {p['history_duplicates_removed']}",
              "(same provider snapshot stored twice before the fix; kept on disk, counted once by the read view)"]
    return "\n".join(L) + "\n"


def main(argv):
    run_id = argv[1] if len(argv) > 1 else DEFAULT_RUN
    r = recalibrate(run_id)
    out = ROOT / "reports"
    md, js = out / "first-live-recalibration.md", out / "first-live-recalibration.json"
    md.write_text(safety.redact(render(r)))
    js.write_text(json.dumps(safety.redact(r), ensure_ascii=False, indent=2, default=str))
    print(f"{md}\n{js}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
