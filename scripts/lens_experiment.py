"""Step AE — Discovery lens experiment: which way of ASKING KaloPilot finds better candidates?

Three lenses (prompts/discovery_lenses.md): proven best-sellers, sustained 90-day growth, accelerating creators.
Discovery only (no deep analysis). Dry-run by default; paid queries need the exact production phrase.
Outputs: data/production/raw/experiments/lenses/ (raw, read-only) and reports/production/experiments/.

Per lens it measures: products returned, availability of the 60-90 day fields, sustained growth (both steps up,
base >= base_min), units, and the FREE preliminary score (share of available WPS points from discovery facts).
Nothing is estimated: a missing value stays None and is reported as N/A.
"""
import hashlib
import json
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import config_resolver as CR  # noqa: E402
import discovery as D  # noqa: E402
import deep_analysis as DA  # noqa: E402
import safety  # noqa: E402

LENSES = ("proven", "rising", "creators")
DEFAULT_CATEGORIES = ("home", "kitchen")
PHRASE = "CONFIRM PRODUCTION LIVE RUN"
EST_PER_QUERY = 4.0
RUN_CAP = 26.0
BASE_MIN = 5000
RAW_DIR = ROOT / "data" / "production" / "raw" / "experiments" / "lenses"
OUT_DIR = ROOT / "reports" / "production" / "experiments"


def templates(path=ROOT / "prompts" / "discovery_lenses.md"):
    text = Path(path).read_text()
    parts = re.split(r"^=== ?(\w*)\s*$", text, flags=re.M)
    blocks = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2) if parts[i]}
    return blocks


def build_queries(categories=DEFAULT_CATEGORIES, lenses=LENSES, limit=15):
    with CR.active(__import__("promotion").active_dir(ROOT)):
        filters, cats = D.load_yaml("filters.yaml"), D.load_yaml("categories.yaml")
    t = templates()
    d, m = filters["discovery"], filters["market"]
    out = []
    for key in categories:
        c = next(x for x in cats["categories"] if x["key"] == key)
        for lens in lenses:
            q = t[lens].format(region=m["region"], currency=m["currency"], limit=limit, category_name=c["name"],
                               kalodata_match=", ".join(c.get("kalodata_match") or [c["name"]]),
                               units_min=d["units_30d"]["min"], gmv_min=d["gmv_30d"]["min"], base_min=BASE_MIN,
                               price_min=d["price"]["min"], price_max=d["price"]["max"],
                               common_rules="{common_rules}")
            q = q.replace("{common_rules}", t["common"].format(currency=m["currency"]))
            out.append({"lens": lens, "category_key": key, "query": q,
                        "query_sha256": hashlib.sha256(q.encode()).hexdigest()})
    return out


def _num(v):
    x, _ = D.parse_value(v, "num")
    return x


def facts(rec):
    f = {k: _num(rec.get(k)) for k in ("gmv_30d", "gmv_prev_30d", "gmv_prev2_30d", "units_30d", "units_prev_30d",
                                         "creator_count", "video_count", "creator_growth_pct", "price_min",
                                         "price_max")}
    g1 = (f["gmv_30d"] - f["gmv_prev_30d"]) / f["gmv_prev_30d"] * 100 if f["gmv_30d"] is not None and \
        f["gmv_prev_30d"] else None
    f.update(product_id=str(rec.get("product_id")) if rec.get("product_id") is not None else None,
             product_name=rec.get("product_name"), growth_30d=round(g1, 2) if g1 is not None else None)
    seq = (f["gmv_prev2_30d"], f["gmv_prev_30d"], f["gmv_30d"])
    f["has_90d"] = None not in seq
    f["base_ok"] = f["gmv_prev_30d"] is not None and f["gmv_prev_30d"] >= BASE_MIN
    f["sustained"] = bool(f["has_90d"] and seq[0] < seq[1] < seq[2] and f["base_ok"])
    return f


def analyze(raw_dir=RAW_DIR):
    with CR.active(__import__("promotion").active_dir(ROOT)):
        import score_products as SP
        scoring = SP.load_scoring_config()
    rows = []
    for p in sorted(Path(raw_dir).glob("*.json")):
        env = json.loads(p.read_text())
        recs, errs = D.extract_records(env)
        for r in recs:
            f = facts(r)
            f["preliminary"] = DA.preliminary_score({"facts": f}, scoring)
            rows.append({**f, "lens": env.get("lens"), "category_key": env.get("lens_category") or env.get("category_key"),
                         "raw": p.name, "errors": errs})
    by = {}
    for lens in LENSES:
        L = [r for r in rows if r["lens"] == lens]
        pre = [r["preliminary"] for r in L if r["preliminary"] is not None]
        by[lens] = {"products": len(L), "with_90d_pct": round(100 * sum(r["has_90d"] for r in L) / len(L), 1) if L else None,
                    "sustained": sum(r["sustained"] for r in L),
                    "median_units": statistics.median([r["units_30d"] for r in L if r["units_30d"] is not None])
                    if any(r["units_30d"] is not None for r in L) else None,
                    "median_preliminary": round(statistics.median(pre), 2) if pre else None,
                    "preliminary_ge_50": sum(x >= 50 for x in pre)}
    seen, top = set(), []
    for r in sorted(rows, key=lambda r: -(r["preliminary"] or 0)):
        if r["product_id"] in seen or not r["base_ok"]:
            continue
        seen.add(r["product_id"])
        top.append(r)
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "lenses": by, "top10": top[:10], "rows": rows}


def render(a):
    L = ["# Discovery lens experiment", "", f"Generated {a['generated_at']}. Discovery only; preliminary score = share of "
         "WPS points computable from discovery facts (not the final WPS).", "",
         "| Lens | Products | With 90-day data | Sustained growth | Median units 30d | Median preliminary | Preliminary >= 50 |",
         "|---|---|---|---|---|---|---|"]
    for k, x in a["lenses"].items():
        L.append(f"| {k} | {x['products']} | {x['with_90d_pct'] if x['with_90d_pct'] is not None else 'N/A'} % | "
                 f"{x['sustained']} | {x['median_units'] if x['median_units'] is not None else 'N/A'} | "
                 f"{x['median_preliminary'] if x['median_preliminary'] is not None else 'N/A'} | {x['preliminary_ge_50']} |")
    L += ["", "## Top 10 (base >= previous-month revenue minimum)", "",
          "| # | Product | Lens | Category | Preliminary | Units 30d | Revenue 90→60→30 | Sustained |", "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(a["top10"], 1):
        L.append(f"| {i} | {(r['product_name'] or '')[:60]} | {r['lens']} | {r['category_key']} | {r['preliminary']} | "
                 f"{r['units_30d']} | {r['gmv_prev2_30d']} → {r['gmv_prev_30d']} → {r['gmv_30d']} | {r['sustained']} |")
    return "\n".join(L) + "\n"


def run_live(queries, confirm, client=None, out=print):
    if confirm != PHRASE:
        raise SystemExit(f'BLOCKED: type exactly "{PHRASE}"')
    import kalopilot_client as K
    client = client or K
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    start = client.credits()["totalRemain"]
    reserve = safety.load_runtime()["safety"]["min_balance_reserve"]
    spent, saved = 0.0, []
    safety.set_run_overrides(live_mode=True, dry_run=False, explicit_live_confirmation=True)
    try:
        for q in queries:
            bal = client.credits()["totalRemain"]
            if spent + EST_PER_QUERY > RUN_CAP or bal - EST_PER_QUERY < reserve:
                out(f"STOP before {q['lens']}/{q['category_key']}: cap {RUN_CAP} or reserve {reserve} (balance {bal})")
                break
            sub = client.submit(q["query"], estimated_cost=EST_PER_QUERY)
            task = (sub.get("data") or {}).get("task_id")
            resp = client.wait(task) if task else sub
            now = datetime.now(timezone.utc)
            after = client.credits()["totalRemain"]
            spent = round(start - after, 2)
            meta = {"lens": q["lens"], "category_key": q["category_key"], "query": q["query"],
                    "query_sha256": q["query_sha256"], "task_id": task, "lens_category": q["category_key"], "data_environment": "PRODUCTION",
                    "experiment": "AE-discovery-lenses", "fetched_at": now.strftime("%Y%m%dT%H%M%SZ"),
                    "balance_before": bal, "balance_after": after}
            p = D.save_raw(resp, {**meta, "category_key": f"{q['lens']}_{q['category_key']}"}, RAW_DIR)
            saved.append(str(p))
            recs, errs = D.extract_records(json.loads(Path(p).read_text()))
            out(f"{q['lens']:8s} {q['category_key']:8s} records {len(recs)} credits {round(bal - after, 2)} "
                f"errors {errs[:1]}")
            time.sleep(1)
    finally:
        safety.clear_run_overrides()
    return {"saved": saved, "credits_spent": spent}


def write(a):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "lens-experiment.md").write_text(render(a))
    (OUT_DIR / "lens-experiment.json").write_text(json.dumps(a, indent=1, default=str))
    return OUT_DIR / "lens-experiment.md"
