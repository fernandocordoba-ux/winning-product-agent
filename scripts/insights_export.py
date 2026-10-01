"""Daily insights snapshot for the "¿Qué quieres saber hoy?" page (read-only, no paid query).

    python -m winning_product_agent insights

Source: the latest PRODUCTION run when one exists; otherwise the saved LIVE calibration data re-scored with the
active production config (clearly labelled). Every value carries a plain-Spanish explanation of why it has that
value (input, rule, threshold). Missing values stay N/A. Output: reports/insights/insights.json and
reports/insights/insights.html (the template with the data embedded).
"""
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
if str(ROOT) not in sys.path:
    sys.path.insert(1, str(ROOT))

NA = None
STATE_ES = {
    "READY_FOR_PRODUCT_VALIDATION": ("Listo para validar", "good",
                                     "Pasó todas las reglas con datos reales. El siguiente paso es la validación manual "
                                     "(muestra, cotización final, margen real). No es una garantía de ventas."),
    "PROMISING_NEEDS_VALIDATION": ("Prometedor, falta validar", "warn",
                                   "La demanda en TikTok es suficiente, pero faltan datos (normalmente proveedor, "
                                   "competidores o creativos) para poder decidir."),
    "WATCHLIST": ("En observación", "warn", "Tiene algo interesante, pero una dimensión importante está débil. Se vuelve "
                                            "a mirar en una próxima corrida."),
    "REJECT": ("Descartado", "bad", "Hay evidencia negativa clara (una regla dura o dos dimensiones principales débiles)."),
    "INSUFFICIENT_DATA": ("Datos insuficientes", "muted",
                          "No hay suficiente evidencia para decidir. No significa que sea malo: faltan datos."),
}
DIM_ES = {"market_momentum": "Impulso en TikTok", "cross_platform_demand": "Demanda en Amazon",
          "commercial_viability": "Viabilidad comercial", "competitive_environment": "Competencia",
          "creative_opportunity": "Oportunidad creativa", "evidence_quality": "Calidad de la evidencia"}
DIM_STATUS_ES = {"STRONG": ("Fuerte", "good"), "ACCEPTABLE": ("Aceptable", "good"), "WEAK": ("Débil", "bad"),
                 "UNKNOWN": ("Sin datos", "muted")}
FLAG_ES = {
    "CREATOR_DEPENDENCY": "Depende de muy pocos creadores: si uno deja de publicar, las ventas pueden caer.",
    "VIDEO_DEPENDENCY": "Depende de muy pocos videos.",
    "SALES_DECLINING": "Las ventas diarias van a la baja en la ventana analizada.",
    "GROWTH_UNVERIFIED": "El crecimiento lo dio el proveedor sin el dato del mes anterior; no se pudo verificar.",
    "GROWTH_MISMATCH": "El crecimiento del proveedor no coincide con el calculado.",
    "COMPETITION_NOT_COMPARABLE": "El conteo de competencia no es del mismo nivel de categoría; no se usa para puntuar.",
    "LOW_DATA_CONFIDENCE": "La confianza en los datos es baja.",
    "EXTREME_SATURATION": "La categoría está muy saturada.",
    "INSUFFICIENT_HISTORY": "Todavía no hay historial suficiente para medir tendencia en el tiempo.",
    "INSUFFICIENT_SUPPLIER_DATA": "No hay costo de proveedor: el margen no se puede calcular (no se inventa).",
    "IP_REVIEW_REQUIRED": "Posible tema de marca/propiedad intelectual: requiere revisión manual.",
    "REGULATED_REVIEW_REQUIRED": "Categoría posiblemente regulada: requiere revisión manual.",
    "PRICE_INSTABILITY": "El precio cambió mucho en el periodo.",
}
INPUT_ES = {
    "revenue_growth_pct": ("crecimiento de ventas 30 días vs 30 días anteriores", "%"),
    "recent_velocity_pct": ("ventas de los últimos 7 días vs los 7 anteriores", "%"),
    "acceleration_pp": ("cambio de esa velocidad (aceleración)", " pp"),
    "units_sold": ("unidades vendidas en 30 días", ""),
    "videos_count": ("videos que promocionan el producto", ""),
    "video_sales_share_pct": ("parte de las ventas que viene de videos", "%"),
    "creators_count": ("creadores que lo promocionan", ""),
    "top_creators_growth_pct": ("creadores principales que están creciendo", ""),
    "category_growth_pct": ("crecimiento de la categoría", "%"),
    "competition_count_comparable": ("productos competidores en la subcategoría", ""),
    "price_avg": ("precio promedio", " USD"),
    "commission_pct": ("comisión a afiliados", "%"),
    "daily_sales_series": ("variabilidad de las ventas diarias (coeficiente de variación)", ""),
}
METRIC_ES = {"growth_long_term": "Crecimiento 30 días", "growth_recent_trend": "Tendencia reciente",
             "growth_acceleration": "Aceleración", "demand": "Demanda", "video_momentum": "Impulso de videos",
             "creator_reach": "Alcance de creadores", "creator_growth": "Crecimiento de creadores",
             "competition_category_growth": "Crecimiento de la categoría",
             "competition_saturation": "Saturación de competencia", "margin_potential": "Potencial de margen",
             "trend_stability": "Estabilidad de ventas"}


def _n(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _fmt(v, unit=""):
    if v is None:
        return "N/A"
    if isinstance(v, float) and abs(v) >= 1000:
        return f"{v:,.0f}{unit}"
    return f"{round(v, 2):g}{unit}" if isinstance(v, (int, float)) else f"{v}{unit}"


def _scale_text(c):
    z, f, sc = c.get("zero_at"), c.get("full_at"), c.get("scale")
    if sc == "ratio":
        return f"cuenta la proporción que cumple {c.get('match')} (mínimo {c.get('min_items')} elementos)"
    inv = z is not None and f is not None and z > f
    kind = " (escala logarítmica)" if sc == "log10" else ""
    if inv:
        return f"da puntaje completo con {_fmt(float(f))} o menos y 0 con {_fmt(float(z))} o más{kind}"
    return f"da 0 puntos con {_fmt(float(z))} o menos y puntaje completo con {_fmt(float(f))} o más{kind}"


def wps_explained(rec, scoring):
    out = []
    ins = rec.get("wps_inputs") or {}
    daily = [x for x in ((rec.get("sales_history") or {}).get("daily_gmv") or []) if isinstance(x, (int, float))]
    cv = statistics.pstdev(daily) / statistics.mean(daily) if len(daily) >= 2 and statistics.mean(daily) else None
    for name, m in scoring["metrics"].items():
        b = (rec.get("wps_breakdown") or {}).get(name) or {}
        pts = b.get("points")
        parts = []
        for cname, c in (m.get("components") or {}).items():
            key = c["input"]
            label, unit = INPUT_ES.get(key, (key, ""))
            if key == "top_creators_growth_pct":
                g = [x.get("growth_pct") for x in ((rec.get("creator_metrics") or {}).get("top_creators") or [])
                     if isinstance(x, dict) and x.get("growth_pct") is not None]
                shown = f"{sum(1 for x in g if x > 0)} de {len(g)} creadores principales crecen" if g else "N/A"
                parts.append(f"{label}: {shown} — la escala {_scale_text(c)}")
                continue
            val = cv if key == "daily_sales_series" else ins.get(key)
            parts.append(f"{label}: {_fmt(_n(val) if not isinstance(val, list) else None, unit)} — la escala "
                         f"{_scale_text(c)}")
            if key == "revenue_growth_pct" and (_n(val) or 0) > 1000:
                parts.append("un % tan alto indica que el mes anterior vendió muy poco (base baja), no necesariamente "
                             "un producto grande")
            if key == "daily_sales_series" and cv is not None and cv >= 1:
                parts.append("las ventas diarias suben y bajan mucho (picos), por eso la estabilidad da 0")
        na = pts in (None, "N/A")
        tier = m.get("tier")
        why = ("; ".join(parts) + ".") if parts else ""
        if na:
            why += (" Sin dato: al ser una métrica CORE cuenta 0 puntos." if tier == "CORE" else
                    " Sin dato: se excluye del cálculo y baja un poco la confianza (no se inventa).")
        out.append({"metric": METRIC_ES.get(name, name), "points": None if na else _n(pts), "max": m["points"],
                    "tier": tier, "why": why.strip()})
    return out


def dim_explained(d):
    ev = d["evidence"]["values"]
    v = lambda k: (ev.get(k) or {}).get("value")  # noqa: E731
    st = d["dimension_status"]
    txt = {
        "market_momentum": f"WPS {_fmt(v('wps'))} (aceptable desde 55, fuerte desde 70) con confianza "
                           f"{_fmt(v('wps_confidence'))} (mínimo 60). Momentum: {_fmt(v('momentum_score'))}.",
        "cross_platform_demand": ("Aún no hay validación en Amazon: solo se consulta si el WPS llega a 70."
                                  if v("avs") is None else f"AVS {_fmt(v('avs'))} con confianza {_fmt(v('amazon_confidence'))}."),
        "commercial_viability": ("No hay costo de proveedor importado, así que el margen no se puede calcular."
                                 if v("product_cost") is None else
                                 f"BVS {_fmt(v('bvs'))}, margen bruto {_fmt(v('gross_margin_pct'), '%')}."),
        "competitive_environment": ("No hay investigación de competidores importada."
                                    if v("competitor_confidence") is None else
                                    f"Oportunidad {_fmt(v('competitor_opportunity'))}, saturación "
                                    f"{_fmt(v('competitor_saturation'))}, confianza {_fmt(v('competitor_confidence'))}."),
        "creative_opportunity": ("No hay creativos analizados." if v("creative_confidence") is None else
                                 f"Confianza creativa {_fmt(v('creative_confidence'))} (mínimo 65). Los videos guardados "
                                 "de KaloPilot no traen hooks ni ángulos, por eso suele quedar sin datos."),
        "evidence_quality": "Resume si las confianzas de cada capa llegan a su mínimo.",
    }
    return [{"dimension": DIM_ES[k], "status": DIM_STATUS_ES[st[k]][0], "tone": DIM_STATUS_ES[st[k]][1],
             "why": txt[k], "technical": d["dimensions"][k]["reason"]} for k in DIM_ES]


def action_es(a):
    a = str(a)
    table = [("Import supplier offers", "Importar ofertas reales de proveedor (costo + envío). El sistema no hace pedidos."),
             ("Import competitor research", "Importar investigación de competidores directos."),
             ("creatives from-kalopilot", "Importar creativos clasificados (hooks / ángulos)."),
             ("Optional: Amazon validation", "Opcional: validar en Amazon (consulta pagada)."),
             ("Raise the weakest layer", "Mejorar la capa con menor confianza."),
             ("A further deep-analysis observation", "Otra observación en una próxima corrida para confirmar tendencia."),
             ("Collect broader demand evidence", "Conseguir evidencia de demanda fuera de TikTok antes de juzgar la "
                                                 "dependencia de creadores."),
             ("KaloPilot deep analysis", "Análisis profundo en KaloPilot (pagado)."),
             ("Manual supplier offer import", "Importar ofertas de proveedor (manual)."),
             ("Manual competitor research import", "Importar investigación de competidores (manual)."),
             ("Re-observe later", "Volver a observar en una próxima corrida."),
             ("No further research spend", "No gastar más en investigar este producto mientras siga el motivo de descarte."),
             ("Manual validation", "Validación manual: " + a.split(":", 1)[-1].strip())]
    for k, es in table:
        if k.lower() in a.lower():
            return es
    return a


# ============================================================================ sources
def production_snapshot(root):
    import final_validation as FV
    m, run_dir = FV.latest_production_run(root)
    if not m:
        return None
    dec = json.loads(Path(m["outputs"]["final_decision_json"]).read_text())
    decisions = [d for k in ("ready_products", "promising_products", "watchlist", "rejected_products",
                             "insufficient_data") for d in dec.get(k) or []]
    deep = {str(x.get("product_id")): x for x in (json.loads((run_dir / "checkpoints" / "deep_analysis.json")
                                                             .read_text()).get("results") or [])}
    return {"source": "PRODUCTION", "label": f"Corrida de producción {m['run_id']}", "run_id": m["run_id"],
            "config_version": m.get("config_version"), "decisions": decisions, "deep": deep,
            "shortlist": dec.get("shortlist") or [], "generated_from": m["outputs"]["final_decision_json"],
            "manifest": m}


def calibration_snapshot(root):
    import config_resolver as CR
    import decision_engine as DE
    import production_calibration as PC
    import promotion as PROMO
    ev = PC.load_evidence(root)
    recs = PC.reanalyze(ev)
    latest = PC.latest_per_product(recs)
    d = PROMO.active_dir(root)
    with CR.active(d):
        prods, cre = PC.decision_products(ev, recs)
        res = DE.run(prods, {}, cre, expected_env="LIVE", require_trust=True)
    decisions = []
    for x in res["decisions"]:
        decisions.append({k: v for k, v in x.items() if k != "evidence"} | {
            "evidence": {"values": x["evidence"]["values"], "flags": x["evidence"]["flags"]}})
    return {"source": "CALIBRATION", "label": "Datos de calibración del 30-sep (NO son de producción: 2 corridas de "
                                              "prueba en Beauty & Personal Care)",
            "run_id": None, "config_version": json.loads((d / "manifest.json").read_text())["config_version"],
            "decisions": decisions, "deep": {str(r["product_id"]): r for r in latest}, "shortlist": res["shortlist"]}


def costs(root):
    rows = []
    for base, env in ((root / "runs", "calibración"), (root / "runs" / "production", "producción")):
        for mf in sorted(base.glob("*/manifest.json")) if base.exists() else []:
            m = json.loads(mf.read_text())
            if m.get("mode") != "LIVE":
                continue
            cr = m.get("credits") or {}
            q = sum((c or {}).get("LIVE_QUERY", 0) for c in (m.get("provider_query_counts") or {}).values())
            if not q and not cr.get("credits_used_by_balance"):
                continue
            deep = (m.get("provider_query_counts") or {}).get("deep_analysis", {}).get("LIVE_QUERY", 0)
            used = cr.get("credits_used_by_balance")
            rows.append({"run_id": m["run_id"], "tipo": env, "fecha": m.get("started_at", "")[:10],
                         "consultas_pagadas": q, "creditos": used, "saldo_inicio": cr.get("balance_start"),
                         "saldo_fin": cr.get("balance_now"), "estado": m.get("final_status"),
                         "por_analisis_profundo": round(used / deep, 2) if used and deep else None})
    return rows


def health(root):
    import preflight as PF
    try:
        pf = PF.preflight(root)
        comps = {k: v["status"] for k, v in pf["system_status"].items()}
        status = pf["status"]
    except Exception as e:  # noqa: BLE001
        comps, status = {}, f"no disponible ({e.__class__.__name__})"
    import promotion as PROMO
    try:
        v = PROMO.verify(PROMO.active_dir(root))
        cfg = {"version": v["config_version"], "hashes_ok": v["ok"]}
    except Exception:  # noqa: BLE001
        cfg = {"version": None, "hashes_ok": False}
    return {"preflight": status, "components": comps, "config": cfg}


def balance():
    try:
        from winning_product_agent.runner import KaloClient
        return KaloClient().credits().get("totalRemain")          # FREE endpoint, no query
    except Exception:  # noqa: BLE001
        return None


# ============================================================================ build
def build(root=ROOT, now=None, with_balance=True):
    now = now or datetime.now(timezone.utc)
    import score_products as SP
    snap = production_snapshot(root) or calibration_snapshot(root)
    scoring = SP.load_scoring_config()
    products = []
    for d in snap["decisions"]:
        rec = snap["deep"].get(str(d["product_id"])) or {}
        vals = d["evidence"]["values"]
        v = lambda k: (vals.get(k) or {}).get("value")  # noqa: E731
        st = STATE_ES.get(d["decision_state"], (d["decision_state"], "muted", ""))
        flags = sorted((d["evidence"].get("flags") or {}).keys())
        dc = d["decision_confidence"]
        products.append({
            "id": str(d["product_id"]), "name": d["name"], "category": d.get("category"),
            "state": d["decision_state"], "state_es": st[0], "tone": st[1], "state_why": st[2],
            "decision_confidence": dc["score"],
            "dc_why": "Mide cuánta evidencia confiable hay detrás de la decisión (no qué tan bueno es el producto): "
                      + ", ".join(f"{k.replace('_', ' ')} {x}" for k, x in dc.get("components", {}).items()) + ".",
            "wps": v("wps"), "wps_confidence": v("wps_confidence"), "trend": v("trend"),
            "growth": v("growth_30d_pct"), "gmv": _n(rec.get("gmv")), "units": _n(rec.get("units")),
            "price": _n((rec.get("price") or {}).get("avg")), "creators": v("creator_count"), "videos": v("video_count"),
            "wps_components": wps_explained(rec, scoring) if rec else [],
            "dimensions": dim_explained(d),
            "flags": [{"code": f, "es": FLAG_ES.get(f, f.replace("_", " ").lower())} for f in flags],
            "missing": [x.get("reason") or x.get("source") for x in (d.get("explanation") or {}).get("MISSING_EVIDENCE") or []
                        if str(x.get("source", "")).startswith("dimension:")],
            "negative": [x.get("reason") or x.get("source") for x in (d.get("explanation") or {}).get("NEGATIVE_EVIDENCE") or []],
            "next_actions": list(dict.fromkeys(action_es(a) for a in d.get("next_actions") or [])),
        })
    products.sort(key=lambda p: (["READY_FOR_PRODUCT_VALIDATION", "PROMISING_NEEDS_VALIDATION", "WATCHLIST",
                                  "INSUFFICIENT_DATA", "REJECT"].index(p["state"]) if p["state"] in STATE_ES else 9,
                                 -(p["wps"] or 0)))
    ab = root / "reports" / "production-calibration.json"
    dq = []
    if ab.exists():
        a = json.loads(ab.read_text())
        for k, x in a.get("field_reliability", {}).items():
            if x["n"]:
                dq.append({"campo": k, "disponible": x["available"], "total": x["n"], "estado": x["status"]})
    fv_json = root / "reports" / "production" / "final-validation" / "final-validation.json"
    fv = json.loads(fv_json.read_text()).get("results") if fv_json.exists() else []
    bal = balance() if with_balance else None
    counts = {}
    for p in products:
        counts[p["state_es"]] = counts.get(p["state_es"], 0) + 1
    return {"generated_at": now.isoformat(), "source": snap["source"], "source_label": snap["label"],
            "run_id": snap["run_id"], "config_version": snap["config_version"], "balance": bal,
            "counts": counts, "shortlist": [s.get("name") for s in snap["shortlist"]], "products": products,
            "costs": costs(root), "health": health(root), "data_quality": dq,
            "final_validation": [{"name": r["name"], "state": r["state"], "missing": r["reasons"]["missing"][:6],
                                  "actions": r["action_queue"]} for r in fv or []],
            "production_pending": snap["source"] != "PRODUCTION",
            "audit": audit_es((snap.get("manifest") or {}).get("post_run_audit"))}


def audit_es(a):
    """Post-run audit in plain Spanish (production runs only)."""
    if not a:
        return None
    items = []
    for x in a.get("issues") or []:
        es = x
        if "GMV/units" in x:
            import re
            m = re.search(r"(\d+): GMV/units = ([\d.]+) USD per unit, outside the listed price range ([\d.]+)", x)
            if m:
                es = (f"Producto {m.group(1)}: las ventas divididas entre unidades dan {m.group(2)} USD por unidad, pero el "
                      f"precio publicado es {m.group(3)} USD. Puede deberse a descuentos, promociones o paquetes, o a un "
                      "dato inconsistente del proveedor (discovery y análisis profundo traen las mismas cifras, así que "
                      "no es un error de lectura nuestro). Mientras no se revise, el puntaje de margen de ese producto "
                      "puede estar inflado.")
        items.append(es)
    return {"result": a.get("result"), "first_run": a.get("first_production_run_status"), "issues": items}


def font_faces(root=ROOT):
    """DejaVu Sans / Sans Mono (free license, templates/fonts/LICENSE-DejaVu.txt) embedded as data URIs, so the
    page looks the same on every device without loading fonts from the network."""
    import base64
    fd = Path(root) / "templates" / "fonts"
    faces = [("WPA Sans", "DejaVuSans.woff", "400 500"), ("WPA Sans", "DejaVuSans-Bold.woff", "600 800"),
             ("WPA Mono", "DejaVuSansMono.woff", "400 700")]
    css = []
    for fam, fn, w in faces:
        p = fd / fn
        if p.exists():
            b64 = base64.b64encode(p.read_bytes()).decode()
            css.append(f'@font-face {{ font-family: "{fam}"; src: url(data:font/woff;base64,{b64}) format("woff"); '
                       f'font-weight: {w}; font-style: normal; font-display: swap; }}')
    return "\n".join(css)


def write(root=ROOT, template=None, out_dir=None, with_balance=True):
    data = build(root, with_balance=with_balance)
    out = Path(out_dir or root / "reports" / "insights")
    out.mkdir(parents=True, exist_ok=True)
    (out / "insights.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    tpl = Path(template or root / "templates" / "insights.html").read_text()
    payload = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    html = tpl.replace("/*__INSIGHTS_DATA__*/null", payload).replace("/*__FONTS__*/", font_faces(root))
    (out / "insights.html").write_text(html)
    return {"json": str(out / "insights.json"), "html": str(out / "insights.html"), "data": data}


if __name__ == "__main__":
    r = write()
    print(r["html"])
