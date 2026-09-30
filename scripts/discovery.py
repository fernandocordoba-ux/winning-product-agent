"""Discovery Mode (Step L): normalize, deduplicate, filter and save candidates.

Deterministic. All thresholds come from config/filters.yaml (+ category
overrides in config/categories.yaml). Nothing is estimated: unparseable or
missing values become None (shown as "N/A"); a real 0 stays 0.

Flow:
  raw KaloPilot response (data/raw/, read-only, never overwritten)
    -> extract_records()   JSON block from the report/text
    -> normalize()         facts (source values) kept apart from calculated fields
    -> dedupe()            product_id, else documented fallback key
    -> evaluate()          PASS / FAIL / REVIEW with reasons
    -> select()            momentum-first order, max N candidates
    -> save_processed()    data/processed/discovery_<timestamp>.json (new file)

CLI:
  python3 scripts/discovery.py plan                       # queries + cost estimate (FREE)
  python3 scripts/discovery.py process data/raw/<f>.json  # process saved raw files (FREE)
Fetching (spends credits) lives in scripts/kalopilot_client.py.
"""
import copy
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from confidence import confidence_score, load_config as load_confidence_config  # noqa: E402
from score_products import load_scoring_config, wps_score  # noqa: E402
import provenance  # noqa: E402

NA = "N/A"
NA_TOKENS = {"", "n/a", "na", "null", "none", "-", "--", "—", "not available", "unknown"}
PIPELINE_VERSION = "discovery-v1"

# Normalized fact fields: (name, kind). kind: str | num | count | pct | date
FACT_FIELDS = [
    ("product_id", "id"), ("product_name", "str"), ("product_url", "str"),
    ("shop_id", "id"), ("shop_name", "str"),
    ("category", "str"), ("category_id", "id"),
    ("price_min", "num"), ("price_max", "num"),
    ("gmv_30d", "num"), ("gmv_prev_30d", "num"), ("units_30d", "count"), ("growth_30d", "pct"),
    ("creator_count", "count"), ("selling_creator_count", "count"), ("creator_growth_pct", "pct"),
    ("video_count", "count"), ("video_growth_pct", "pct"), ("shop_count", "count"),
    ("launch_date", "date"), ("data_window_end", "date"),
]
# Source key aliases -> normalized field (first match wins)
ALIASES = {
    "product_id": ["product_id", "productId", "id"],
    "product_name": ["product_name", "name", "title"],
    "product_url": ["product_url", "url"],
    "shop_id": ["shop_id", "shopId"],
    "shop_name": ["shop_name", "shop"],
    "category": ["category_path", "category"],
    "category_id": ["category_id", "categoryId"],
    "price_min": ["price_min", "min_price", "price"],
    "price_max": ["price_max", "max_price", "price"],
    "gmv_30d": ["gmv_30d", "gmv", "revenue", "sales"],
    "gmv_prev_30d": ["gmv_prev_30d", "gmv_previous_30d", "previous_gmv"],
    "units_30d": ["units_30d", "units", "units_sold"],
    "growth_30d": ["growth_30d_pct", "growth_30d", "revenue_growth_pct", "growth"],
    "creator_count": ["creator_count", "creators"],
    "selling_creator_count": ["selling_creator_count", "selling_creators"],
    "creator_growth_pct": ["creator_growth_pct"],
    "video_count": ["video_count", "videos"],
    "video_growth_pct": ["video_growth_pct"],
    "shop_count": ["shop_count", "shops", "seller_count"],
    "launch_date": ["launch_date"],
    "data_window_end": ["data_window_end"],
}
# Filter-config input names -> normalized fields available in discovery
RULE_INPUTS = {
    "revenue_growth_pct": "growth_30d",
    "similar_listings_count": "shop_count",
}

NUM_RE = re.compile(r"^([+-]?)\$?\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)\s*([kmb])?\s*%?$", re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------- config
def load_yaml(name):
    import config_resolver as _CR
    with open(_CR.path(name)) as f:
        return yaml.safe_load(f)


def deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def filters_for_category(filters_cfg, categories_cfg, category_key):
    """Category overrides > global filters (deterministic)."""
    for c in (categories_cfg or {}).get("categories", []):
        if c.get("key") == category_key:
            return deep_merge(filters_cfg, (c.get("overrides") or {}).get("filters"))
    return filters_cfg


# --------------------------------------------------------------------------- parsing
def parse_number(v):
    """Real number or None. Never guesses: text that is not an explicit number -> None.

    Accepts ints/floats and explicit numeric strings like "4,206,171", "$40.30",
    "+100.1%", "-24.6%", "29.0k". bool, NaN, inf and anything else -> None.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v if math.isfinite(v) else None
    if not isinstance(v, str):
        return None
    s = v.strip().replace("−", "-")
    if s.lower() in NA_TOKENS:
        return None
    m = NUM_RE.match(s)
    if not m:
        return None
    sign, digits, suffix = m.groups()
    n = float(digits.replace(",", ""))
    n *= {None: 1, "k": 1e3, "m": 1e6, "b": 1e9}[suffix.lower() if suffix else None]
    n = -n if sign == "-" else n
    return int(n) if n.is_integer() and "." not in digits and not suffix else n


def parse_value(v, kind):
    """Return (value or None, problem or None)."""
    if v is None:
        return None, None
    if kind in ("str", "id", "date"):
        if isinstance(v, bool) or isinstance(v, (dict, list)):
            return None, "not a scalar"
        s = str(v).strip() if not isinstance(v, float) else (str(int(v)) if v.is_integer() else str(v))
        if s.lower() in NA_TOKENS:
            return None, None
        if kind == "id" and isinstance(v, float):
            return None, "numeric id may have lost precision"
        if kind == "date" and not DATE_RE.match(s):
            return None, f"unrecognized date '{s}'"
        return " ".join(s.split()) if kind == "str" else s, None
    n = parse_number(v)
    if n is None:
        is_na = isinstance(v, str) and v.strip().lower() in NA_TOKENS
        return None, (None if is_na else f"unparseable number {v!r}")
    if kind == "count" and (n < 0 or (isinstance(n, float) and not n.is_integer())):
        return None, f"invalid count {v!r}"
    if kind == "num" and n < 0:
        return None, f"negative value {v!r}"
    return n, None


def extract_records(raw_envelope):
    """Get product records from a saved raw envelope. Returns (records, errors).

    Looks for the first ```json fenced block (array) in response.data.report, then .text.
    """
    errors = []
    try:
        data = raw_envelope["response"]["data"]
    except (KeyError, TypeError):
        return [], ["raw envelope has no response.data"]
    if not isinstance(data, dict):
        return [], ["response.data is not an object"]
    for field in ("report", "text"):
        blob = data.get(field)
        if not isinstance(blob, str):
            continue
        for m in re.finditer(r"```json\s*(.*?)```", blob, re.S):
            try:
                parsed = json.loads(m.group(1))
            except json.JSONDecodeError as e:
                errors.append(f"{field}: invalid JSON block ({e.msg})")
                continue
            if isinstance(parsed, dict):
                parsed = parsed.get("products", [parsed])
            if isinstance(parsed, list):
                return parsed, errors
            errors.append(f"{field}: JSON block is not an array")
    errors.append("no parseable ```json product array found in report/text")
    return [], errors


# --------------------------------------------------------------------------- growth (Step U)
GROWTH_TOLERANCE_PCT = 5.0          # provider growth vs our calculation (percentage points, or 5 % relative)

def verify_growth(f, w, gmv_key="gmv_30d", growth_key="growth_30d_pct"):
    """The CODE calculates growth = (gmv - gmv_prev) / gmv_prev * 100 from the two revenue facts.
    Provider growth is kept as growth_30d_provider. prev = 0 -> growth N/A (new, no base).
    No gmv_prev -> provider growth kept but labeled unverified."""
    gmv, prev, prov = f.get(gmv_key), f.get("gmv_prev_30d"), f.get(growth_key)
    f["growth_30d_provider"] = prov
    if gmv is not None and prev is not None:
        if prev > 0:
            calc = round((gmv - prev) / prev * 100, 2)
            if prov is not None and abs(prov - calc) > max(GROWTH_TOLERANCE_PCT, abs(calc) * 0.05):
                f["growth_mismatch"] = {"provider": prov, "calculated": calc}
                w.append(f"GROWTH_MISMATCH: provider growth {prov} vs calculated {calc} "
                         f"(gmv {gmv}, previous {prev}); calculated value used")
            f[growth_key], f["growth_source"] = calc, "calculated"
        else:
            if prov is not None:
                w.append(f"growth: previous-period revenue is 0 -> growth N/A (provider said {prov})")
            f[growth_key], f["growth_source"] = None, "no_previous_revenue"
    else:
        f["growth_source"] = "provider_unverified" if prov is not None else None
        if prov is not None:
            w.append("GROWTH_UNVERIFIED: no previous-period revenue returned; provider growth kept as reported")


# --------------------------------------------------------------------------- normalize
def fallback_key(facts):
    name = " ".join((facts.get("product_name") or "").lower().split())
    shop = " ".join((facts.get("shop_name") or "").lower().split())
    price = facts.get("price_min")
    basis = f"{name}|{shop}|{'' if price is None else repr(float(price))}"
    return "fallback:" + hashlib.sha1(basis.encode()).hexdigest()[:16]


def normalize(record, meta, index):
    """Normalize one source record. Returns (product or None, malformed_reason or None)."""
    if not isinstance(record, dict):
        return None, f"record {index} is not an object"
    facts, problems = {}, []
    for field, kind in FACT_FIELDS:
        raw = next((record[a] for a in ALIASES[field] if a in record), None)
        value, problem = parse_value(raw, kind)
        facts[field] = value
        if problem:
            problems.append(f"{field}: {problem}")
    if facts["product_id"] is not None and not re.fullmatch(r"\d+", facts["product_id"]):
        problems.append(f"product_id: non-numeric id '{facts['product_id']}' ignored")
        facts["product_id"] = None
    verify_growth(facts, problems, "gmv_30d", "growth_30d")    # Step U: the code calculates growth
    if not facts["product_name"] and facts["product_id"] is None:
        return None, f"record {index} has neither product_id nor product_name"

    key_type = "product_id" if facts["product_id"] else "fallback"
    key = facts["product_id"] if key_type == "product_id" else fallback_key(facts)
    return {
        "key": key,
        "key_type": key_type,
        "market": meta.get("market", "US"),
        "currency": meta.get("currency", "USD"),
        "period_days": meta.get("period_days", 30),
        "observation_date": meta.get("observation_date"),
        "category_key": (record.get("category_key") if meta.get("category_key") == "combined"
                         and isinstance(record.get("category_key"), str) else meta.get("category_key")),
        "facts": facts,                                   # SOURCE values only
        "missing_fields": [f for f, _ in FACT_FIELDS if facts[f] is None],
        "parse_warnings": problems,
        "source": {
            "raw_file": meta.get("raw_file"),
            "task_id": meta.get("task_id"),
            "report_url": meta.get("report_url"),
            "fetched_at": meta.get("fetched_at"),
            "record_index": index,
            "original_record": copy.deepcopy(record),     # unchanged source record
        },
        "calculated": {},                                 # filled by evaluate()/score
        "provenance": provenance.discovery_provenance(facts, meta),
    }, None


def dedupe(products):
    """Keep one record per key. Returns (unique, duplicates_log)."""
    groups = {}
    for p in products:
        groups.setdefault(p["key"], []).append(p)
    unique, dup_log = [], []
    for key in sorted(groups):
        group = sorted(groups[key], key=lambda p: (-sum(v is not None for v in p["facts"].values()),
                                                   str(p["source"]["raw_file"]), p["source"]["record_index"]))
        keep = group[0]
        if len(group) > 1:
            keep["duplicates"] = [{"raw_file": d["source"]["raw_file"], "record_index": d["source"]["record_index"]}
                                  for d in group[1:]]
            dup_log.append({"key": key, "kept": keep["source"]["record_index"], "dropped": len(group) - 1})
        unique.append(keep)
    return unique, dup_log


# --------------------------------------------------------------------------- filters
def _cmp(value, condition):
    op, threshold = condition.split()
    t = float(threshold)
    return {">": value > t, ">=": value >= t, "<": value < t, "<=": value <= t, "==": value == t}[op]


def _word_match(text, keyword):
    return re.search(r"(?<!\w)" + re.escape(keyword.lower()) + r"(?!\w)", text.lower()) is not None


def product_age_days(product):
    """CALCULATION: days between launch_date and data_window_end (else observation_date).
    None if either date is missing or invalid (never estimated)."""
    f = product["facts"]
    end = f.get("data_window_end") or product.get("observation_date")
    if not f.get("launch_date") or not end:
        return None
    try:
        return (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(f["launch_date"], "%Y-%m-%d")).days
    except (ValueError, TypeError):
        return None


def evaluate(product, fcfg):
    """Apply filters.yaml. Returns calculated dict with filter_status and reasons.

    FAIL   : any reject rule triggered, or a critical field missing
    REVIEW : no FAIL, but any flag rule triggered or optional data missing
    PASS   : nothing triggered
    """
    f = product["facts"]
    fails, reviews, na_checks = [], [], []

    def rule(name, action, detail):
        (fails if action == "reject" else reviews).append({"rule": name, "action": action, **detail})

    # 1) critical data
    ins = fcfg["risk_rules"]["insufficient_data"]
    missing_critical = [x for x in ins["required_fields"] if f.get(x) is None]
    if missing_critical:
        rule("insufficient_data", ins["missing_required_action"], {"missing": missing_critical})
    if product["key_type"] == "fallback":
        rule("missing_product_id", ins.get("missing_product_id_action", "flag"),
             {"fallback_key": product["key"]})

    # 2) discovery hard/soft filters
    d = fcfg["discovery"]
    pc = d["price"]
    basis = pc.get("basis", "avg")
    if basis == "avg":
        price = (f["price_min"] + f["price_max"]) / 2 if None not in (f["price_min"], f["price_max"]) else None
    else:
        price = f.get(f"price_{basis}")
    if price is None:
        na_checks.append("price")
        if f["price_min"] is not None:          # price known but basis not computable
            rule("price_basis_unavailable", "flag", {"basis": basis})
    elif price < pc["min"] or price > pc["max"]:
        rule("price_range", pc["action"], {"value": price, "basis": basis, "min": pc["min"], "max": pc["max"]})

    for field, key in (("gmv_30d", "gmv_30d"), ("units_30d", "units_30d")):
        v = f[field]
        if v is None:
            na_checks.append(field)
        elif v < d[key]["min"]:
            rule(f"{field}_min", d[key]["action"], {"value": v, "min": d[key]["min"]})

    for field, key in (("growth_30d", "growth_30d"), ("selling_creator_count", "selling_creators")):
        v = f[field]
        if v is None:
            na_checks.append(field)
        elif v < d[key]["preferred_min"]:
            rule(f"{field}_below_preferred", d[key]["action"], {"value": v, "preferred_min": d[key]["preferred_min"]})

    for field in fcfg["discovery_mode"]["review_if_missing"]:
        if f.get(field) is None:
            rule("missing_optional_data", "flag", {"field": field})

    # 3) numeric risk rules (only inputs available in discovery)
    age = product_age_days(product)
    inputs = {k: f.get(v) for k, v in RULE_INPUTS.items()}
    inputs["product_age_days"] = age
    for name in ("collapsing_sales", "extreme_seller_saturation", "low_base_or_seasonal"):
        if name not in fcfg["risk_rules"]:
            continue
        for chk in fcfg["risk_rules"][name]["checks"]:
            v = inputs.get(chk["input"])
            if v is None:
                na_checks.append(f"{name}:{chk['input']}")
            elif _cmp(v, chk["condition"]):
                rule(name, chk["action"], {"input": chk["input"], "value": v, "condition": chk["condition"]})

    # 4) keyword risk rules
    for name in ("trademark_ip_risk", "highly_regulated", "shipping_problems"):
        r = fcfg["risk_rules"][name]
        fields = r.get("match_field", "product_name")
        fields = [fields] if isinstance(fields, str) else fields
        text_fields = {"product_name": f["product_name"], "category": f["category"]}
        hits = sorted({kw for kw in r["keywords"] for fld in fields
                       if text_fields.get(fld) and _word_match(text_fields[fld], kw)})
        if hits:
            rule(name, r["action"], {"keywords": hits})

    status = "FAIL" if fails else ("REVIEW" if reviews else "PASS")
    return {
        "price": price,
        "price_basis": basis,
        "product_age_days": age,
        "filter_status": status,
        "filter_reasons": fails + reviews,
        "na_checks": sorted(set(na_checks)),
        "filters_version": fcfg.get("version"),
    }


# --------------------------------------------------------------------------- scoring
def scoring_input(product):
    f = product["facts"]
    return {
        "product_id": f["product_id"] or product["key"],
        "product_name": f["product_name"],
        "gmv": f["gmv_30d"], "units_sold": f["units_30d"], "revenue_growth_pct": f["growth_30d"],
        "price_min": f["price_min"], "price_max": f["price_max"],
        "creators_count": f["creator_count"], "videos_count": f["video_count"],
        "shops_count": f["shop_count"],
    }


def attach_scores(product, conf_cfg, scoring_cfg=None):
    wps = wps_score(scoring_input(product), scoring_cfg)          # Step S: config loaded once per run
    conf = confidence_score(scoring_input(product), conf_cfg)
    product["calculated"]["wps_if_calculable"] = (
        {"status": "pending", "score": NA, "reason": "insufficient data for a full WPS at discovery stage; deep analysis required", "na_metrics": wps.get("na_metrics")}
        if wps["score"] == NA else wps)
    product["calculated"]["confidence_if_calculable"] = {
        "score": conf["score"], "level": conf["level"], "stage": "discovery",
        "breakdown": {k: v["earned"] for k, v in conf["breakdown"].items()},
    }


# --------------------------------------------------------------------------- selection
def select(products, dcfg):
    """Order PASS/REVIEW candidates (momentum first) and cap at max_candidates."""
    order = {}
    for s in dcfg["sort_by"]:
        if "order" in s:
            order = {v: i for i, v in enumerate(s["order"])}

    def sort_key(p):
        f = p["facts"]
        key = []
        for s in dcfg["sort_by"]:
            if s["field"] == "filter_status":
                key.append(order.get(p["calculated"]["filter_status"], 99))
            elif s["field"] == "key":
                key.append(p["key"])
            elif s["field"] == "risk_flag":             # flagged products sort after unflagged ones
                key.append(any(r.get("rule") == s["rule"] for r in p["calculated"].get("filter_reasons") or []))
            else:
                v = f.get(s["field"])
                key += [v is None, 0 if v is None else (-v if s.get("direction") == "desc" else v)]
        return key

    candidates = sorted([p for p in products if p["calculated"]["filter_status"] != "FAIL"], key=sort_key)
    limit = dcfg["max_candidates"]
    return candidates[:limit], [p["key"] for p in candidates[limit:]]


# --------------------------------------------------------------------------- IO
def save_raw(response, meta, raw_dir=ROOT / "data" / "raw"):
    """Save an unmodified API response. Never overwrites: new file, then read-only."""
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    ts = meta.get("fetched_at") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"{ts}_{meta.get('category_key', 'na')}_{meta.get('task_id', 'na')}"
    path = raw_dir / f"{base}.json"
    n = 1
    while path.exists():
        n += 1
        path = raw_dir / f"{base}_{n}.json"
    envelope = {"saved_by": PIPELINE_VERSION, **{k: v for k, v in meta.items()}, "response": response}
    with open(path, "x") as fh:                           # 'x' = fail if file exists
        json.dump(envelope, fh, ensure_ascii=False, indent=2)
    os.chmod(path, 0o444)
    return path


def run_discovery(raw_paths, filters_cfg=None, categories_cfg=None, conf_cfg=None):
    """Process saved raw files (read-only). Returns the processed result dict."""
    filters_cfg = filters_cfg or load_yaml("filters.yaml")
    categories_cfg = categories_cfg or load_yaml("categories.yaml")
    conf_cfg = conf_cfg or load_confidence_config()
    scoring_cfg = load_scoring_config()
    products, malformed = [], []
    for path in raw_paths:
        with open(path) as fh:
            env = json.load(fh)
        data = (env.get("response") or {}).get("data") or {}
        meta = {
            "raw_file": str(path), "task_id": env.get("task_id") or data.get("task_id"),
            "report_url": data.get("report_url"), "fetched_at": env.get("fetched_at"),
            "observation_date": env.get("observation_date"), "category_key": env.get("category_key"),
            "market": env.get("market", filters_cfg["market"]["region"]),
            "currency": env.get("currency", filters_cfg["market"]["currency"]),
            "period_days": filters_cfg["market"]["period_days"],
        }
        records, errors = extract_records(env)
        malformed += [{"raw_file": str(path), "error": e} for e in errors if not records]
        for i, rec in enumerate(records):
            p, err = normalize(rec, meta, i)
            if err:
                malformed.append({"raw_file": str(path), "record_index": i, "error": err})
            else:
                products.append(p)

    unique, dup_log = dedupe(products)
    for p in unique:
        p["calculated"].update(evaluate(p, filters_for_category(filters_cfg, categories_cfg, p["category_key"])))
        attach_scores(p, conf_cfg, scoring_cfg)
    candidates, over_limit = select(unique, filters_cfg["discovery_mode"])
    failed = sorted([p for p in unique if p["calculated"]["filter_status"] == "FAIL"], key=lambda p: p["key"])
    counts = {s: sum(p["calculated"]["filter_status"] == s for p in unique) for s in ("PASS", "REVIEW", "FAIL")}
    return {
        "pipeline": PIPELINE_VERSION,
        "config_versions": {"filters": filters_cfg.get("version"), "categories": categories_cfg.get("version"),
                            "confidence": conf_cfg.get("version")},
        "market": filters_cfg["market"],
        "raw_files": [str(p) for p in raw_paths],
        "summary": {"records": len(products), "unique": len(unique), "duplicates_removed": len(products) - len(unique),
                    "malformed": len(malformed), **counts, "returned_candidates": len(candidates),
                    "over_limit": len(over_limit)},
        "candidates": candidates,
        "failed": failed,
        "over_limit_keys": over_limit,
        "duplicates": dup_log,
        "malformed": malformed,
    }


def save_processed(result, out_dir=ROOT / "data" / "processed"):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path, n = out_dir / f"discovery_{ts}.json", 1
    while path.exists():
        n += 1
        path = out_dir / f"discovery_{ts}_{n}.json"
    with open(path, "x") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return path


# --------------------------------------------------------------------------- queries
def build_queries(filters_cfg=None, categories_cfg=None):
    """One discovery query per enabled category (text only; sends nothing)."""
    filters_cfg = filters_cfg or load_yaml("filters.yaml")
    categories_cfg = categories_cfg or load_yaml("categories.yaml")
    template = re.split(r"^===\s*$", (ROOT / "prompts" / "discovery.md").read_text(), flags=re.M)[1].strip()
    out = []
    for c in categories_cfg["categories"]:
        if not c.get("enabled", True):
            continue
        fc = filters_for_category(filters_cfg, categories_cfg, c["key"])
        d, m = fc["discovery"], fc["market"]
        out.append({"category_key": c["key"], "query": template.format(
            region=m["region"], currency=m["currency"], period_days=m["period_days"],
            limit=fc["discovery_mode"]["products_per_category"], category_name=c["name"],
            kalodata_match=", ".join(c.get("kalodata_match") or [c["name"]]),
            gmv_min=d["gmv_30d"]["min"], units_min=d["units_30d"]["min"],
            price_min=d["price"]["min"], price_max=d["price"]["max"])})
    return out


def main(argv):
    if len(argv) >= 2 and argv[1] == "plan":
        qs = build_queries()
        print(f"{len(qs)} discovery queries (one per enabled category):")
        for q in qs:
            print(f"  - {q['category_key']}")
        print("\nExample query:\n" + qs[0]["query"])
        return 0
    if len(argv) >= 3 and argv[1] == "process":
        result = run_discovery([Path(p) for p in argv[2:]])
        path = save_processed(result)
        print(json.dumps(result["summary"], indent=2))
        print(f"Saved: {path}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
