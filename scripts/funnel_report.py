"""Illustrated funnel report: how each production run narrowed the products down, stage by stage.

PDF (charts) + TXT with the same numbers:
  1. Discovery: queries, answers, records, duplicates / malformed
  2. Filters: PASS / REVIEW / FAIL and the reasons for FAIL / REVIEW
  3. Deep selection: seasonal skipped, over the limit, selected, not run (target reached / budget)
  4. Deep analysis: ok / failed, WPS distribution (55 / 70 lines), qualify (WPS >= 55 and Confidence >= 60)
  5. Decision states and Amazon labels
  6. Projection (dry-run estimate, expected hit rate) vs actual
  7. Comparison with every earlier production run: what worked better (candidates per credit)
Reads saved run data only (no query). Missing numbers are N/A.
"""
import collections
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

OUT = ROOT / "reports" / "production" / "funnel"
EXPECTED_HIT_RATE = 0.5          # assumption stated in the plan (share of deep-analyzed products reaching WPS >= 55)
INK, MUTED, ACCENT, GOOD, BAD, WARN = "#1f2a2e", "#7a8a8f", "#1f5c4a", "#2f8f5b", "#b4473a", "#c99a2e"


def _j(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, TypeError, ValueError):
        return None


def _runs(root):
    runs = root / "runs" / "production"
    out = []
    for mf in sorted(runs.glob("*/manifest.json")):
        m = _j(mf) or {}
        if m.get("mode") == "LIVE":
            out.append((m, mf.parent))
    return out


def _rebuild(root, m):
    best = None
    for mf in sorted((root / "runs" / "production").glob("*/manifest.json")):
        r = _j(mf) or {}
        if r.get("report_only_source_run") == m.get("run_id"):
            best = (r, mf.parent)
    return best


def funnel(root, m, run_dir):
    ck = run_dir / "checkpoints"
    disc = _j(ck / "discovery.json") or {}
    proc = _j(disc.get("processed_file")) or {}
    summ = proc.get("summary") or {}
    deep_ck = _j(ck / "deep_analysis.json") or {}
    rb = _rebuild(root, m)
    dec_file = (m.get("outputs") or {}).get("final_decision_json")
    if rb:
        deep_ck = _j(rb[1] / "checkpoints" / "deep_analysis.json") or deep_ck
        dec_file = (rb[0].get("outputs") or {}).get("final_decision_json") or dec_file
    dec = _j(dec_file) or {}
    results = deep_ck.get("results") or []
    ok = [r for r in results if r.get("status") == "ok"]
    qual = [r for r in ok if (r.get("wps") or 0) >= 55 and (r.get("confidence") or 0) >= 60]
    reasons = collections.Counter()
    for p in (proc.get("failed") or []) + (proc.get("candidates") or []):
        for r in (p.get("calculated") or {}).get("filter_reasons") or []:
            reasons[f"{(p.get('calculated') or {}).get('filter_status')}: {r.get('rule')}"] += 1
    states = collections.Counter()
    amz = collections.Counter()
    for k in ("ready_products", "promising_products", "watchlist", "rejected_products", "insufficient_data"):
        for d in dec.get(k) or []:
            states[d.get("decision_state")] += 1
            amz[(d.get("amazon_validation") or {}).get("label") or "N/A"] += 1
    qs = disc.get("queries") or []
    stg = m.get("stages") or {}
    deep_stage = stg.get("deep_analysis") or {}
    cred = (m.get("credits") or {}).get("credits_used_by_balance")
    qb = m.get("query_budget") or {}
    est = qb.get("estimated_credits_max") or qb.get("estimated_credits_max_total")
    return {
        "run_id": m.get("run_id"), "config": (rb[0] if rb else m).get("config_version"),
        "rescored_by": rb[0].get("run_id") if rb else None,
        "queries": len(qs), "answers_ok": sum(1 for q in qs if q.get("raw_file") and not q.get("error")),
        "answers_failed": sum(1 for q in qs if q.get("error") or q.get("failed_raw_file")),
        "records": summ.get("records"), "unique": summ.get("unique"), "duplicates": summ.get("duplicates_removed"),
        "malformed": summ.get("malformed"), "pass": summ.get("PASS"), "review": summ.get("REVIEW"),
        "fail": summ.get("FAIL"), "candidates_kept": summ.get("returned_candidates"), "over_limit": summ.get("over_limit"),
        "filter_reasons": reasons.most_common(10),
        "deep_selected": deep_stage.get("selected") or len(results), "deep_ok": len(ok),
        "deep_failed": len(results) - len(ok), "deep_not_run": len(deep_ck.get("not_run") or []),
        "target": deep_ck.get("target"), "qualified": len(qual), "wps": [r.get("wps") for r in ok],
        "states": dict(states), "amazon": dict(amz), "credits": cred, "estimated_max": est,
        "hit_rate": round(len(qual) / len(ok), 3) if ok else None,
        "credits_per_candidate": round(cred / len(qual), 2) if cred and qual else None,
        "status": m.get("final_status") or (stg.get("run_summary") or {}).get("summary"),
    }


def projection(f):
    """What the plan expected vs what happened (stated assumption: EXPECTED_HIT_RATE)."""
    target = f.get("target") or 10
    need = round(target / EXPECTED_HIT_RATE)
    return {"expected_deep_needed": need, "expected_candidates": round((f["deep_ok"] or 0) * EXPECTED_HIT_RATE, 1),
            "actual_candidates": f["qualified"], "expected_hit_rate": EXPECTED_HIT_RATE, "actual_hit_rate": f["hit_rate"],
            "estimated_credits_max": f["estimated_max"], "actual_credits": f["credits"]}


def recommendations(all_f):
    good = [x for x in all_f if x["credits_per_candidate"]]
    L = []
    if good:
        best = min(good, key=lambda x: x["credits_per_candidate"])
        L.append(f"Best efficiency so far: run {best['run_id']} ({best['config']}) with {best['credits_per_candidate']} "
                 f"credits per candidate and hit rate {best['hit_rate']}.")
    cur = all_f[-1]
    if cur["hit_rate"] is not None and cur["hit_rate"] < EXPECTED_HIT_RATE:
        L.append("Hit rate below the 50 % assumption: tighten pre-selection (sustained growth + higher preliminary score) "
                 "before paying for deep analyses.")
    elif cur["hit_rate"] is not None:
        L.append("Hit rate at or above the 50 % assumption: the discovery/pre-selection setup is working; keep it.")
    if cur.get("answers_failed"):
        L.append(f"{cur['answers_failed']} discovery answer(s) failed: keep answers short (fewer fields / products per query).")
    if cur.get("fail") and cur.get("records") and cur["fail"] / max(1, cur["records"]) > 0.25:
        L.append("More than 25 % of discovery records fail the filters: adjust the discovery prompt to the filter limits.")
    return L


def _charts(f, all_f, tmp):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 9, "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": INK,
                         "ytick.color": INK, "axes.spines.top": False, "axes.spines.right": False})
    files = {}
    # 1 funnel
    stages = [("Registros", f["records"]), ("Únicos", f["unique"]), ("Pasan filtros (PASS+REVIEW)", (f["pass"] or 0) + (f["review"] or 0)),
              ("Candidatos conservados", f["candidates_kept"]), ("Análisis profundo OK", f["deep_ok"]),
              ("Califican (WPS>=55, Conf>=60)", f["qualified"]), ("Prometedor / Listo",
                                                                   f["states"].get("PROMISING_NEEDS_VALIDATION", 0) + f["states"].get("READY_FOR_PRODUCT_VALIDATION", 0))]
    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    vals = [v or 0 for _, v in stages]
    ax.barh(range(len(stages))[::-1], vals, color=[MUTED] * 4 + [ACCENT, GOOD, GOOD])
    ax.set_yticks(range(len(stages))[::-1], [s for s, _ in stages])
    for i, v in enumerate(vals):
        ax.text(v, len(stages) - 1 - i, f" {v}", va="center", color=INK)
    ax.set_title("Embudo: de la búsqueda a los candidatos", loc="left", color=INK)
    fig.tight_layout()
    files["funnel"] = tmp / "funnel.png"
    fig.savefig(files["funnel"], dpi=160)
    plt.close(fig)
    # 2 filter reasons
    if f["filter_reasons"]:
        fig, ax = plt.subplots(figsize=(7.2, 2.8))
        labels = [r for r, _ in f["filter_reasons"]][::-1]
        ax.barh(labels, [n for _, n in f["filter_reasons"]][::-1],
                color=[BAD if l.startswith("FAIL") else WARN for l in labels])
        ax.set_title("Por qué los filtros detuvieron o marcaron productos", loc="left", color=INK)
        fig.tight_layout()
        files["reasons"] = tmp / "reasons.png"
        fig.savefig(files["reasons"], dpi=160)
        plt.close(fig)
    # 3 WPS distribution
    w = [x for x in f["wps"] if x is not None]
    if w:
        fig, ax = plt.subplots(figsize=(7.2, 2.6))
        ax.hist(w, bins=range(0, 101, 5), color=ACCENT, edgecolor="white")
        for t, c, lab in ((55, WARN, "55 aceptable"), (70, GOOD, "70 fuerte")):
            ax.axvline(t, color=c, ls="--", lw=1)
            ax.text(t + 0.5, ax.get_ylim()[1] * (0.92 if t == 55 else 0.75), lab, color=c)
        ax.set_xlabel("WPS")
        ax.set_title("WPS de los productos analizados a fondo", loc="left", color=INK)
        fig.tight_layout()
        files["wps"] = tmp / "wps.png"
        fig.savefig(files["wps"], dpi=160)
        plt.close(fig)
    # 4 projection vs actual
    p = projection(f)
    fig, ax = plt.subplots(figsize=(7.2, 2.4))
    cats = ["Candidatos", "Tasa de acierto (%)"]
    exp = [p["expected_candidates"] or 0, (p["expected_hit_rate"] or 0) * 100]
    act = [p["actual_candidates"] or 0, (p["actual_hit_rate"] or 0) * 100]
    xs = range(len(cats))
    ax.bar([x - 0.2 for x in xs], exp, 0.4, color=MUTED, label="Proyectado")
    ax.bar([x + 0.2 for x in xs], act, 0.4, color=ACCENT, label="Real")
    ax.set_xticks(list(xs), cats)
    ax.legend(frameon=False)
    ax.set_title("Proyección vs resultado real", loc="left", color=INK)
    fig.tight_layout()
    files["projection"] = tmp / "projection.png"
    fig.savefig(files["projection"], dpi=160)
    plt.close(fig)
    # 5 comparison across runs
    if len(all_f) > 1:
        fig, ax = plt.subplots(figsize=(7.2, 2.8))
        names = [f"{x['run_id'][:13]}\n{x['config']}" for x in all_f]
        ax.bar(names, [x["qualified"] for x in all_f], color=ACCENT, label="Candidatos (WPS>=55)")
        ax2 = ax.twinx()
        ax2.plot(names, [x["credits"] or 0 for x in all_f], color=WARN, marker="o", label="Créditos usados")
        ax.set_ylabel("Candidatos")
        ax2.set_ylabel("Créditos", color=WARN)
        ax.set_title("Corridas comparadas: candidatos vs créditos", loc="left", color=INK)
        ax.tick_params(axis="x", labelsize=7)
        fig.tight_layout()
        files["compare"] = tmp / "compare.png"
        fig.savefig(files["compare"], dpi=160)
        plt.close(fig)
    return files


def text(f, all_f):
    p = projection(f)
    L = [f"FUNNEL REPORT — production run {f['run_id']} (config {f['config']})",
         f"WPS/decision from free rebuild {f['rescored_by']}" if f["rescored_by"] else "", "",
         "1. DISCOVERY",
         f"   queries {f['queries']} | answers ok {f['answers_ok']} | failed {f['answers_failed']}",
         f"   records {f['records']} | unique {f['unique']} | duplicates {f['duplicates']} | malformed {f['malformed']}",
         "2. FILTERS", f"   PASS {f['pass']} | REVIEW {f['review']} | FAIL {f['fail']}"]
    L += [f"   - {r}: {n}" for r, n in f["filter_reasons"]]
    L += ["3. DEEP SELECTION",
          f"   kept as candidates {f['candidates_kept']} | over the candidate limit {f['over_limit']}",
          f"   selected {f['deep_selected']} | not run {f['deep_not_run']}" + (f" (target {f['target']})" if f["target"] else ""),
          "4. DEEP ANALYSIS",
          f"   ok {f['deep_ok']} | failed {f['deep_failed']} | qualified (WPS>=55, Conf>=60) {f['qualified']} | hit rate {f['hit_rate']}",
          f"   WPS: {', '.join(str(x) for x in sorted([w for w in f['wps'] if w is not None], reverse=True))}",
          "5. DECISION", "   " + ", ".join(f"{k} {v}" for k, v in f["states"].items()),
          "   Amazon: " + ", ".join(f"{k} {v}" for k, v in f["amazon"].items()),
          "6. PROJECTION VS ACTUAL",
          f"   expected hit rate {p['expected_hit_rate']} -> actual {p['actual_hit_rate']}",
          f"   expected candidates {p['expected_candidates']} -> actual {p['actual_candidates']}",
          f"   credits: estimated max {p['estimated_credits_max']} -> actual {p['actual_credits']}",
          "7. COMPARISON WITH EARLIER RUNS"]
    for x in all_f:
        L.append(f"   {x['run_id']} {x['config']}: records {x['records']}, deep ok {x['deep_ok']}, candidates "
                 f"{x['qualified']}, hit rate {x['hit_rate']}, credits {x['credits']}, credits/candidate {x['credits_per_candidate']}")
    L += ["", "WHAT COULD WORK BETTER"] + [f"   - {r}" for r in recommendations(all_f)]
    return "\n".join(x for x in L if x is not None) + "\n"


def write(root=ROOT, out_dir=OUT):
    runs = _runs(root)
    if not runs:
        return None
    all_f = [funnel(root, m, d) for m, d in runs]
    f = all_f[-1]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f["run_id"]
    txt = text(f, all_f)
    base.with_suffix(".txt").write_text(txt)
    tmp = out_dir / f".charts_{f['run_id']}"
    tmp.mkdir(exist_ok=True)
    files = _charts(f, all_f, tmp)
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Image, Paragraph, PageBreak, SimpleDocTemplate, Spacer
    styles = getSampleStyleSheet()
    story = [Paragraph(f"Reporte de embudo — corrida {f['run_id']}", styles["Title"]),
             Paragraph(f"Config {f['config']}. Cada etapa muestra cuántos productos siguieron y por qué los demás se detuvieron. "
                       "Datos de la corrida guardada; N/A = dato no disponible.", styles["Normal"]),
             Spacer(1, 8)]
    for key in ("funnel", "reasons", "wps"):
        if key in files:
            story += [Image(str(files[key]), width=7.0 * inch, height=7.0 * inch * {"funnel": 3.4, "reasons": 2.8, "wps": 2.6}[key] / 7.2),
                      Spacer(1, 6)]
    story.append(PageBreak())
    for key in ("projection", "compare"):
        if key in files:
            story += [Image(str(files[key]), width=7.0 * inch, height=7.0 * inch * {"projection": 2.4, "compare": 2.8}[key] / 7.2),
                      Spacer(1, 6)]
    for line in txt.splitlines():
        story.append(Paragraph(line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") or "&nbsp;",
                               styles["Code"] if line.startswith("   ") else styles["Normal"]))
    SimpleDocTemplate(str(base.with_suffix(".pdf")), pagesize=letter, leftMargin=36, rightMargin=36,
                      topMargin=36, bottomMargin=36).build(story)
    for ext in (".pdf", ".txt"):
        (out_dir / f"latest{ext}").write_bytes(base.with_suffix(ext).read_bytes())
    return {"run_id": f["run_id"], "files": [str(base.with_suffix(".pdf")), str(base.with_suffix(".txt"))]}


if __name__ == "__main__":
    print(write())
