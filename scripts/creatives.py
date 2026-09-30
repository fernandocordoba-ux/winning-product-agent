"""Creative Intelligence (Step Y): angles, hooks, formats, saturation and evidence-backed creative gaps.

NOT combined with WPS / AVS / BVS (BVS adapter prepared, not wired). Competitor scripts, ad copy and
transcripts are NEVER stored: only short analytical summaries and normalized categories.
Nothing inferred: unavailable metrics stay None; unclear hook/angle = UNKNOWN; views are never
profitability; long-running = persistence only.

Independent scores (config/creatives.yaml): Creative Saturation, Creative Opportunity, Creative Confidence.
Sources: manual_import (CSV/JSON) and kalopilot_saved (top_videos from SAVED deep answers, no new query).
Storage: data/raw/creatives (read-only) · data/processed/creatives/<pid>/ · data/history/creatives/<pid>/
CLI: python -m winning_product_agent creatives import <file> | from-kalopilot <product_id> | show <product_id>
"""
import copy
import hashlib
import json
import math
import re
import statistics
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import discovery as disc  # noqa: E402
import suppliers as SUP  # noqa: E402

NA = "N/A"
UNKNOWN = "UNKNOWN"
RAW_DIR = ROOT / "data" / "raw" / "creatives"
PROCESSED_DIR = ROOT / "data" / "processed" / "creatives"
HISTORY_DIR = ROOT / "data" / "history" / "creatives"
DEEP_RAW_DIR = ROOT / "data" / "raw" / "deep_analysis"
FEATURES = ["first_3_second_hook", "demo_present", "human_face_present", "voiceover_present", "text_overlay_present",
            "before_after_present", "testimonial_present", "ugc_style", "product_closeup", "problem_visualized",
            "result_visualized", "cta_present", "unboxing", "comparison", "storytelling", "problem_solution"]

# ============================================================================ Stage 1 — schema
SCHEMA = {
    "creative_id": "str", "platform": "str", "source": "str", "matched_product_id": "id", "linked_product_id": "id",
    "product_title_seen": "str", "product_match_confidence": "num", "match_source": "str",
    "advertiser_or_creator": "str", "creative_url": "str", "landing_page_url": "str",
    "format": "str", "duration_seconds": "num",
    "published_at": "date", "first_seen_at": "date", "last_seen_at": "date",
    "views": "count", "likes": "count", "comments": "count", "shares": "count",
    "estimated_sales": "count", "estimated_gmv": "num", "sales_source": "str",
    "ad_active_status": "bool", "ad_age_days": "count",
    "hook_text_summary": "str", "hook_category": "str", "angle": "str", "problem_addressed": "str",
    "benefit_claim": "str", "demonstration_type": "str", "cta_type": "str",
    **{f: "bool" for f in FEATURES},
    "observed_at": "str", "retrieved_at": "str", "raw_source_location": "str",
}
SUMMARY_FIELDS = ["hook_text_summary", "problem_addressed", "benefit_claim"]
ALIASES = {"url": "creative_url", "creator": "advertiser_or_creator", "advertiser": "advertiser_or_creator",
           "hook": "hook_category", "ad_age": "ad_age_days", "sales": "estimated_sales", "gmv": "estimated_gmv",
           "demo": "demo_present", "voiceover": "voiceover_present", "text_overlay": "text_overlay_present",
           "before_after": "before_after_present", "testimonial": "testimonial_present", "cta": "cta_present",
           "match_confidence": "product_match_confidence", "title": "product_title_seen", "duration": "duration_seconds",
           "hook_summary": "hook_text_summary"}


class MalformedCreativeInput(ValueError):
    pass


class ProviderNotIntegrated(RuntimeError):
    pass


def load_cfg(path=None):
    import config_resolver as _CR
    path = path or _CR.path("creatives.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _date(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d") if s else None
    except ValueError:
        return None


def parse_field(name, v):
    kind = SCHEMA[name]
    if kind == "bool":
        return SUP.parse_bool(v)
    if kind == "str":
        s = None if v is None else str(v).strip()
        return (s or None), None
    return disc.parse_value(v, kind)


def creative_id_for(c):
    basis = f"{c.get('platform')}|{(c.get('creative_url') or '').split('?')[0].rstrip('/').lower()}|{c.get('matched_product_id')}"
    return "crv_" + hashlib.sha1(basis.encode()).hexdigest()[:16]


# ============================================================================ Stage 2 — providers
class CreativeProvider(ABC):
    name = "abstract"

    @abstractmethod
    def search_creatives(self, product):
        """Creatives for one shortlisted product."""

    @abstractmethod
    def get_creative_details(self, ref):
        """Observable details of one creative (format, duration, dates, features)."""

    @abstractmethod
    def get_performance_signals(self, ref):
        """Only metrics the source actually provides (views, engagement, sourced sales/GMV)."""

    @abstractmethod
    def normalize_creative(self, raw, meta):
        """Raw -> normalized schema dict."""


class ManualCreativeProvider(CreativeProvider):
    name = "manual_import"

    def __init__(self, rows):
        self.rows = rows

    def search_creatives(self, product):
        pid = str(product.get("product_id") if isinstance(product, dict) else product)
        return [r for r in self.rows if str(r.get("matched_product_id")) == pid]

    def get_creative_details(self, ref):
        return {k: ref.get(k) for k in ("format", "duration_seconds", "published_at", "first_seen_at", "last_seen_at",
                                        *FEATURES) if k in ref}

    def get_performance_signals(self, ref):
        return {k: ref.get(k) for k in ("views", "likes", "comments", "shares", "estimated_sales", "estimated_gmv",
                                        "sales_source") if k in ref}

    def normalize_creative(self, raw, meta):
        return normalize_row(raw, meta)[0]


class KaloPilotSavedProvider(CreativeProvider):
    """top_videos already present in SAVED KaloPilot deep-analysis answers. Reads files only: no network,
    no credits. KaloPilot gives video id, creator, revenue, views (no hooks/angles/dates -> UNKNOWN / None)."""
    name = "kalopilot_saved"

    def __init__(self, raw_dir=DEEP_RAW_DIR):
        self.raw_dir = Path(raw_dir)

    def search_creatives(self, product):
        pid = str(product.get("product_id") if isinstance(product, dict) else product)
        files = sorted(self.raw_dir.glob(f"*_{pid}_deep_product*.json"))
        out = []
        for f in files[-1:]:                                  # latest saved answer only
            env = json.loads(f.read_text())
            import deep_analysis as DA
            obj, _ = DA.extract_object(env)
            for i, v in enumerate((obj or {}).get("top_videos") or []):
                if isinstance(v, dict):
                    out.append({**v, "_pid": pid, "_file": str(f), "_idx": i,
                                "_retrieved": env.get("observation_timestamp"), "_window": obj.get("data_window_end")})
        return out

    def get_creative_details(self, ref):
        return {"format": "VIDEO", "platform": "TIKTOK"}

    def get_performance_signals(self, ref):
        return {"views": ref.get("views"), "estimated_gmv": ref.get("revenue")}

    def normalize_creative(self, raw, meta):
        vid = str(raw.get("video_id") or "").strip()
        row = {"matched_product_id": raw["_pid"], "linked_product_id": raw["_pid"], "platform": "TIKTOK",
               "format": "VIDEO", "creative_url": f"https://www.tiktok.com/video/{vid}" if vid.isdigit() else None,
               "advertiser_or_creator": raw.get("creator"), "views": raw.get("views"),
               "estimated_gmv": raw.get("revenue"),
               "sales_source": "KaloPilot deep analysis top_videos (revenue attributed to this video)",
               "source": "kalopilot_saved", "observed_at": raw.get("_window")}
        if row["creative_url"] is None:
            row["creative_url"] = f"kalopilot://video/{vid or raw['_idx']}"
        c, errs = normalize_row(row, {"retrieved_at": raw.get("_retrieved"),
                                      "raw_source_location": f"{raw['_file']}#top_videos[{raw['_idx']}]"},
                                allow_non_http=True)
        return c if not errs else None


def get_provider(name, cfg=None, **kw):
    cfg = cfg or load_cfg()
    p = (cfg.get("providers") or {}).get(name)
    if not p or not p.get("implemented"):
        raise ProviderNotIntegrated(f"creative source '{name}' is not integrated "
                                    f"({(p or {}).get('note', 'unknown source')})")
    if name == "kalopilot_saved":
        return KaloPilotSavedProvider(kw.get("raw_dir", DEEP_RAW_DIR))
    return ManualCreativeProvider(kw.get("rows", []))


# ============================================================================ Stage 3 — import / validation
def classify(text, taxonomy):
    """Category whose cue phrase appears in the analytical summary (whole words). None if unclear/ambiguous."""
    t = f" {(text or '').lower()} "
    hits = [cat for cat, cues in taxonomy.items()
            for cue in cues if re.search(r"(?<![a-z])" + re.escape(cue.lower()) + r"(?![a-z])", t)]
    uniq = list(dict.fromkeys(hits))
    return uniq[0] if len(uniq) == 1 else None               # 0 or several categories -> not forced


def normalize_row(row, meta, cfg=None, allow_non_http=False):
    cfg = cfg or load_cfg()
    mi = cfg["manual_import"]
    c, errors = {k: None for k in SCHEMA}, []
    bad = [k for k in row if k in mi["forbidden_fields"] and str(row[k] or "").strip()]   # content, not an empty column
    if bad:
        errors.append(f"forbidden field(s) {bad}: competitor scripts / ad copy / transcripts are never stored")
    src = {ALIASES.get(k, k): v for k, v in row.items() if k not in mi["forbidden_fields"]}
    for k in SCHEMA:
        if k in src:
            val, prob = parse_field(k, src[k])
            c[k] = val
            if prob:
                errors.append(f"{k}: {prob}")
    for k in mi["required_fields"]:
        if c.get(k) is None:
            errors.append(f"missing required field: {k}")
    for k in SUMMARY_FIELDS:
        if c[k] and len(c[k]) > mi["summary_max_chars"]:
            errors.append(f"{k} longer than {mi['summary_max_chars']} chars: store an analytical summary, not the ad copy")
    for k in ("creative_url", "landing_page_url"):
        if c[k] and not (re.match(r"^https?://", c[k]) or (allow_non_http and c[k].startswith("kalopilot://"))):
            errors.append(f"{k} must start with http:// or https://")
    c["platform"] = (c["platform"] or "").upper() or None
    if c["platform"] and c["platform"] not in cfg["platforms"]:
        errors.append(f"platform must be one of {cfg['platforms']}")
    c["format"] = (c["format"] or "").upper() or None
    if c["format"] and c["format"] not in cfg["formats"]:
        errors.append(f"format must be one of {cfg['formats']}")
    for k, tax in (("hook_category", cfg["hooks"]), ("angle", cfg["angles"])):
        if c[k]:
            c[k] = c[k].upper().replace(" ", "_")
            if c[k] != UNKNOWN and c[k] not in tax:
                errors.append(f"{k} {c[k]} not in the taxonomy (config/creatives.yaml)")
    if (c["estimated_sales"] is not None or c["estimated_gmv"] is not None) and not c["sales_source"]:
        errors.append("estimated_sales / estimated_gmv require sales_source (only legitimately sourced figures)")
    if c["product_match_confidence"] is not None and not 0 <= c["product_match_confidence"] <= 100:
        errors.append("product_match_confidence outside 0-100")
    c["hook_source"] = "import_label" if c["hook_category"] else None
    c["angle_source"] = "import_label" if c["angle"] else None
    if not c["hook_category"]:
        c["hook_category"] = classify(c["hook_text_summary"], cfg["hooks"])
        c["hook_source"] = "summary_cue" if c["hook_category"] else None
    if not c["angle"]:
        c["angle"] = classify(" ".join(filter(None, [c["hook_text_summary"], c["benefit_claim"], c["problem_addressed"]])),
                              cfg["angles"])
        c["angle_source"] = "summary_cue" if c["angle"] else None
    c["hook_category"] = c["hook_category"] or UNKNOWN
    c["angle"] = c["angle"] or UNKNOWN
    c["match_source"] = "manual" if c["product_match_confidence"] is not None else None
    c["source"] = c["source"] or meta.get("source", "manual_import")
    c["retrieved_at"] = meta.get("retrieved_at") or _now()
    c["observed_at"] = c["observed_at"] or c["retrieved_at"]
    c["raw_source_location"] = meta.get("raw_source_location")
    c["creative_id"] = creative_id_for(c)
    return c, errors


def dedupe(rows):
    best, dups = {}, []
    for c in rows:
        k = c["creative_id"]
        if k in best:
            dups.append(k)
            if sum(v is not None for v in c.values()) > sum(v is not None for v in best[k].values()):
                best[k] = c
        else:
            best[k] = c
    return list(best.values()), dups


# ============================================================================ Stages 4, 7-9 — enrich
def match(c, product, cfg):
    m = cfg["matching"]
    if c.get("match_source") == "manual" and c.get("product_match_confidence") is not None:
        return float(c["product_match_confidence"]), "manual"
    if c.get("linked_product_id") and str(c["linked_product_id"]) == str(c.get("matched_product_id")):
        return float(m["linked_product_confidence"]), "linked_product_id"
    if c.get("product_title_seen"):
        sc, _, _ = SUP.match_confidence({"name": (product or {}).get("product_name")},
                                        {"supplier_product_title": c["product_title_seen"]}, SUP.load_cfg())
        return sc, "title_match"
    return None, "no identity evidence"


def creative_age(c):
    start = _date(c.get("first_seen_at")) or _date(c.get("published_at"))
    end = _date(c.get("last_seen_at")) or _date(c.get("observed_at"))
    if c.get("ad_age_days") is not None:
        return c["ad_age_days"]
    return (end - start).days if start and end and end >= start else None


def label_by(v, table, key="max_days"):
    if v is None:
        return UNKNOWN
    for t in table:
        if t[key] is None or v <= t[key]:
            return t["label"]


def enrich(c, product, cfg):
    e = copy.deepcopy(c)
    e["match_confidence_calc"], e["match_basis"] = match(e, product, cfg)
    e["qualified"] = e["match_confidence_calc"] is not None and e["match_confidence_calc"] >= cfg["matching"]["qualified_min"]
    e["creative_age_days"] = creative_age(e)
    e["longevity"] = label_by(e["creative_age_days"], cfg["longevity"])
    e["duration_bucket"] = label_by(e.get("duration_seconds"), cfg["duration_buckets"], "max") \
        if e.get("duration_seconds") is not None else UNKNOWN
    v = e.get("views")
    eng = [x for x in (e.get("likes"), e.get("comments"), e.get("shares")) if x is not None]
    e["engagement_rate"] = round(sum(eng) / v, 4) if v and eng else None
    e["pattern"] = {"hook_category": e["hook_category"], "angle": e["angle"], "format": e["format"],
                    "demo_type": e.get("demonstration_type") or UNKNOWN, "cta_type": e.get("cta_type") or UNKNOWN,
                    "duration_bucket": e["duration_bucket"]}
    return e


# ============================================================================ Stages 10-18 — analysis
def _dist(values):
    d = {}
    for v in values:
        d[v] = d.get(v, 0) + 1
    return dict(sorted(d.items(), key=lambda kv: (-kv[1], kv[0])))


def concentration(values, min_n=1):
    vals = [v for v in values if v not in (None, UNKNOWN)]
    d = _dist(vals)
    n = len(vals)
    if n < min_n or not n:
        return {"classified": n, "unique": len(d), "distribution": d, "top": None, "top_share": None,
                "top_3_share": None}
    counts = list(d.values())
    return {"classified": n, "unique": len(d), "distribution": d, "top": next(iter(d)),
            "top_share": round(counts[0] / n, 4), "top_3_share": round(sum(counts[:3]) / n, 4)}


def _share(items, feat):
    known = [c for c in items if c.get(feat) is not None]
    return (sum(1 for c in known if c[feat] is True) / len(known), len(known)) if known else (None, 0)


def _weighted(parts, pts, min_points):
    avail = {k: v for k, v in parts.items() if v is not None}
    have = sum(pts[k] for k in avail)
    if have < min_points:
        return None, have
    return round(100 * sum(pts[k] * v for k, v in avail.items()) / have, 2), have


def performance(q):
    """Performance evidence: sourced sales/GMV > 0, else views >= median views. Never profitability."""
    views = [c["views"] for c in q if c.get("views") is not None]
    med = statistics.median(views) if views else None
    strong = []
    for c in q:
        if (c.get("estimated_gmv") or 0) > 0 or (c.get("estimated_sales") or 0) > 0:
            strong.append(c)
        elif med is not None and c.get("views") is not None and c["views"] >= med:
            strong.append(c)
    return {"with_views": len(views), "median_views": med,
            "with_sourced_sales_or_gmv": sum(1 for c in q if c.get("estimated_gmv") is not None or c.get("estimated_sales") is not None),
            "total_sourced_gmv": round(sum(c["estimated_gmv"] for c in q if c.get("estimated_gmv") is not None), 2) or None,
            "performance_evidence_count": len(strong), "strong_ids": [c["creative_id"] for c in strong],
            "note": "views alone are never treated as profitability"}


def saturation(q, angles, hooks, creators, formats, cfg):
    s = cfg["saturation"]
    comp, pts = s["components"], {k: v["points"] for k, v in s["components"].items()}
    dated = [c for c in q if c["longevity"] != UNKNOWN]
    parts = {
        "creative_volume": min(len(q) / comp["creative_volume"]["full_at"], 1.0),
        "angle_concentration": angles["top_share"] if angles["classified"] >= comp["angle_concentration"]["min_classified"] else None,
        "hook_concentration": hooks["top_share"] if hooks["classified"] >= comp["hook_concentration"]["min_classified"] else None,
        "creator_repetition": creators["top_share"] if creators["classified"] >= comp["creator_repetition"]["min_identified"] else None,
        "longevity_concentration": (round(sum(1 for c in dated if c["longevity"] == "LONG_RUNNING") / len(dated), 4)
                                    if len(dated) >= comp["longevity_concentration"]["min_dated"] else None),
        "format_concentration": formats["top_share"],
    }
    score, have = _weighted(parts, pts, s["min_available_points"])
    return {"score": score, "components": parts, "available_points": have}


def opportunity(q, angles, hooks, formats, perf, deep, cfg):
    o = cfg["opportunity"]
    comp, pts = o["components"], {k: v["points"] for k, v in o["components"].items()}
    d = comp["proven_demand"]
    units = (deep or {}).get("units")
    demand = (None if units is None else 0.0 if units <= 0 else
              max(0.0, min(1.0, (math.log10(units) - math.log10(d["zero_at"])) / (math.log10(d["full_at"]) - math.log10(d["zero_at"])))))
    strong = [c for c in q if c["creative_id"] in set(perf["strong_ids"])]
    patterns = {(c["hook_category"], c["angle"]) for c in strong if UNKNOWN not in (c["hook_category"], c["angle"])}
    demo_share, demo_known = _share(q, "demo_present")
    demo_strong = [c for c in strong if c.get("demo_present") is True]
    demo_any = [c for c in q if c.get("demo_present") is True]
    parts = {
        "proven_demand": None if demand is None else round(demand, 4),
        "proven_patterns": round(min(len(patterns) / comp["proven_patterns"]["full_at"], 1.0), 4) if strong else None,
        "underused_angles": None if angles["top_share"] is None else round(1 - angles["top_share"], 4),
        "format_diversity_gap": formats["top_share"],
        "weak_demos": None if demo_share is None else round(1 - demo_share, 4),
        "low_hook_diversity": hooks["top_share"],
        "demonstration_potential": (1.0 if demo_strong else 0.5 if demo_any else (0.0 if demo_known else None)),
    }
    if o["require_demand"] and parts["proven_demand"] is None:
        return {"score": None, "components": parts, "reason": "no demand evidence: opportunity N/A (never rewarded)"}
    score, have = _weighted(parts, pts, o["min_available_points"])
    cap = None
    if score is not None:
        for c in o["demand_caps"]:
            if parts["proven_demand"] < c["max_demand_s"]:
                cap = c["cap"]
                break
        if cap is not None and score > cap:
            score = float(cap)
    return {"score": score, "components": parts, "available_points": have, "demand_cap_applied": cap,
            "proven_patterns": sorted(f"{h}/{a}" for h, a in patterns)}


def confidence(q, cfg):
    c = cfg["confidence"]
    pts = {k: v["points"] for k, v in c["components"].items()}
    n = len(q)
    frac = (lambda pred: sum(1 for x in q if pred(x)) / n) if n else (lambda pred: 0.0)
    cov = {
        "creative_count": min(n / c["full_count"], 1.0),
        "performance_data": frac(lambda x: x.get("views") is not None or x.get("engagement_rate") is not None),
        "hook_classification": frac(lambda x: x["hook_category"] != UNKNOWN),
        "angle_classification": frac(lambda x: x["angle"] != UNKNOWN),
        "timestamps": frac(lambda x: bool(x.get("first_seen_at") or x.get("published_at") or x.get("ad_age_days") is not None)),
        "sales_attribution": frac(lambda x: x.get("estimated_sales") is not None or x.get("estimated_gmv") is not None),
        "source_identification": frac(lambda x: bool(x.get("advertiser_or_creator"))),
    }
    score = round(sum(pts[k] * v for k, v in cov.items()), 2)
    level = next(lv for lv, t in sorted(c["levels"].items(), key=lambda kv: -kv[1]) if score >= t)
    return {"score": score, "level": level, "components": {k: round(v, 4) for k, v in cov.items()}}


def gaps(q, angles, hooks, formats, cfg):
    g = cfg["gaps"]
    out = []

    def feature_gap(key, feat, limit, label):
        share, known = _share(q, feat)
        if known >= g["min_known"] and share < limit:
            n = round(share * known)
            out.append({"gap": key, "evidence": f"{n}/{known} qualified creatives with known data show {label}",
                        "share": round(share, 4), "scope": "observed qualified creatives only"})
    feature_gap("few_demos", "demo_present", g["share_below"], "a product demo")
    feature_gap("few_ugc", "ugc_style", g["share_below"], "a UGC style")
    feature_gap("few_before_after", "before_after_present", g["rare_share_below"], "a before/after")
    feature_gap("few_comparisons", "comparison", g["rare_share_below"], "a comparison")
    feature_gap("weak_opening_hooks", "first_3_second_hook", 0.5, "a clear hook in the first 3 seconds")
    if angles["classified"] >= g["min_known"]:
        edu = angles["distribution"].get("EDUCATION", 0) + hooks["distribution"].get("HOW_TO", 0)
        if edu / angles["classified"] < g["rare_share_below"]:
            out.append({"gap": "few_educational", "evidence": f"{edu}/{angles['classified']} classified creatives use an "
                                                              "EDUCATION angle or HOW_TO hook",
                        "scope": "classified creatives only"})
        for a in g["underused_angle_candidates"]:
            n = angles["distribution"].get(a, 0)
            if n / angles["classified"] < g["rare_share_below"]:
                out.append({"gap": "underused_angle", "angle": a,
                            "evidence": f"{n}/{angles['classified']} classified creatives use the {a} angle "
                                        f"(dominant: {angles['top']} {round(angles['top_share'] * 100)}%)",
                            "scope": "classified creatives only"})
    if formats["classified"] >= g["min_known"]:
        for f in g["format_candidates"]:
            n = formats["distribution"].get(f, 0)
            if n == 0 and formats["top"] != f:
                out.append({"gap": "underused_format", "format": f,
                            "evidence": f"0 of {formats['classified']} observed qualified creatives use {f} "
                                        f"(dominant: {formats['top']})", "scope": "observed sources only"})
    return out


def hypotheses(gap_list, angles, hooks, cfg):
    tmpl, out = cfg["hypotheses"], []
    dominant = f"{hooks['top'] or UNKNOWN} hook / {angles['top'] or UNKNOWN} angle"
    for g in gap_list:
        t = tmpl.get(g["gap"])
        if not t:
            continue
        fmt = g.get("format") or t["format"]
        angle = g.get("angle") or t["angle"] or angles["top"] or "PROBLEM_SOLUTION"
        out.append({"evidence": g["evidence"], "observed_gap": g["gap"],
                    "testable_hypothesis": f"An original {fmt.lower()} creative using a {t['hook']} hook and a {angle} "
                                           f"angle may stand out against the dominant pattern ({dominant}). "
                                           "Test it against a control; this is not a prediction that it will work.",
                    "suggested_format": fmt, "suggested_hook_category": t["hook"], "suggested_angle": angle,
                    "copy_policy": "write original creative; do not copy competitor scripts or ad text"})
    return out


def red_flags(q, sat, angles, hooks, creators_t, advertisers, formats, conf, cfg):
    f, out = cfg["flags"], []
    if sat["score"] is not None and sat["score"] >= f["CREATIVE_SATURATION_HIGH"]["score_at_least"]:
        out.append({"flag": "CREATIVE_SATURATION_HIGH", "score": sat["score"]})
    for code, t in (("ANGLE_CONCENTRATION_HIGH", angles), ("HOOK_SATURATION_HIGH", hooks)):
        r = f[code]
        if t["classified"] >= r["min_classified"] and t["top_share"] is not None and t["top_share"] >= r["top_share_at_least"]:
            out.append({"flag": code, "top": t["top"], "top_share": t["top_share"], "classified": t["classified"]})
    for code, t in (("CREATOR_CONCENTRATION_HIGH", creators_t), ("ADVERTISER_CONCENTRATION_HIGH", advertisers)):
        r = f[code]
        if t["classified"] >= r["min_identified"] and t["top_share"] is not None and t["top_share"] >= r["top_share_at_least"]:
            out.append({"flag": code, "top": t["top"], "top_share": t["top_share"]})
    r = f["LOW_FORMAT_DIVERSITY"]
    if len(q) >= r["min_qualified"] and formats["top_share"] is not None and formats["top_share"] >= r["top_share_at_least"]:
        out.append({"flag": "LOW_FORMAT_DIVERSITY", "top": formats["top"], "top_share": formats["top_share"]})
    if conf["score"] < f["LOW_CREATIVE_DATA"]["confidence_below"]:
        out.append({"flag": "LOW_CREATIVE_DATA", "creative_confidence": conf["score"]})
    share, known = _share(q, "demo_present")
    r = f["WEAK_DEMONSTRATION_EVIDENCE"]
    if known >= r["min_known"] and share < r["demo_share_below"]:
        out.append({"flag": "WEAK_DEMONSTRATION_EVIDENCE", "demo_share": round(share, 4), "known": known})
    return out


def analyze_product(product_id, deep, creatives, cfg=None):
    cfg = cfg or load_cfg()
    uniq, dups = dedupe(creatives)
    enriched = [enrich(c, deep, cfg) for c in uniq]
    q = [c for c in enriched if c["qualified"]]
    angles = concentration([c["angle"] for c in q])
    hooks = concentration([c["hook_category"] for c in q])
    formats = concentration([c["format"] for c in q])
    social = [c for c in q if c["platform"] in ("TIKTOK", "INSTAGRAM", "YOUTUBE")]
    ads = [c for c in q if c["platform"] == "META"]
    creators_t = concentration([c.get("advertiser_or_creator") for c in social])
    advertisers = concentration([c.get("advertiser_or_creator") for c in ads])
    all_src = concentration([c.get("advertiser_or_creator") for c in q])
    perf = performance(q)
    sat = saturation(q, angles, hooks, all_src, formats, cfg)
    opp = opportunity(q, angles, hooks, formats, perf, deep, cfg)
    conf = confidence(q, cfg)
    gap_list = gaps(q, angles, hooks, formats, cfg)
    return {"product_id": str(product_id), "config_version": cfg.get("version"),
            "observed": len(enriched), "duplicates_removed": len(dups), "qualified_creatives": len(q),
            "unreliable_excluded": len(enriched) - len(q),
            "angles": angles, "hooks": hooks, "formats": formats, "creators": creators_t, "advertisers": advertisers,
            "longevity": _dist(c["longevity"] for c in q),
            "long_running_creatives": sum(1 for c in q if c["longevity"] == "LONG_RUNNING"),
            "active_ads": sum(1 for c in q if c.get("ad_active_status") is True),
            "performance": perf, "saturation": sat, "opportunity": opp, "confidence": conf,
            "creative_gaps": gap_list, "creative_test_hypotheses": hypotheses(gap_list, angles, hooks, cfg),
            "red_flags": red_flags(q, sat, angles, hooks, creators_t, advertisers, formats, conf, cfg),
            "longevity_limitation": cfg["longevity_limitation"].strip(),
            "creatives": enriched,
            "note": "Creative data never makes a product good or bad on its own; not combined with WPS/AVS/BVS."}


# ============================================================================ Stage 16 — pattern library
def pattern_library(analyses):
    """Aggregated normalized patterns (no text, no scripts) across analyzed products."""
    lib = {}
    for a in analyses:
        for c in a["creatives"]:
            if not c["qualified"]:
                continue
            key = "|".join(str(c["pattern"][k]) for k in ("hook_category", "angle", "format", "demo_type", "cta_type",
                                                          "duration_bucket"))
            e = lib.setdefault(key, {**c["pattern"], "count": 0, "products": set(), "with_performance_evidence": 0})
            e["count"] += 1
            e["products"].add(a["product_id"])
            e["with_performance_evidence"] += c["creative_id"] in set(a["performance"]["strong_ids"])
    return sorted(({**v, "products": sorted(v["products"])} for v in lib.values()), key=lambda v: -v["count"])


# ============================================================================ Stage 22 — BVS adapter (NOT wired)
def bvs_inputs(analysis):
    """For BVS Advertising Viability later. BVS does NOT call this yet (Step Y rule)."""
    if not analysis:
        return {"ready": False, "reason": "no creative analysis"}
    conf = analysis["confidence"]["score"]
    return {"ready": conf >= 60, "creative_confidence": conf,
            "creative_opportunity": analysis["opportunity"]["score"],
            "ad_saturation": analysis["saturation"]["score"],
            "format_diversity": analysis["formats"]["unique"], "creative_gaps": [g["gap"] for g in analysis["creative_gaps"]],
            "note": "adapter only — BVS weights unchanged"}


# ============================================================================ Stages 19-20 — storage / history
def _write_new(path, data, readonly=False):
    return SUP._write_new(path, data, readonly)


def _save_processed(pid, cs, raw_path, provider, processed_dir):
    return _write_new(Path(processed_dir) / pid / f"creatives_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json",
                      {"product_id": pid, "provider": provider, "raw_file": str(raw_path), "creatives": cs})


def import_file(path, raw_dir=RAW_DIR, processed_dir=PROCESSED_DIR, cfg=None):
    cfg = cfg or load_cfg()
    try:
        rows = SUP.load_rows(path)
    except SUP.MalformedSupplierInput as e:
        raise MalformedCreativeInput(str(e))
    retrieved = _now()
    raw_rows = [{k: v for k, v in r.items() if k not in cfg["manual_import"]["forbidden_fields"]} for r in rows]
    raw_path = _write_new(Path(raw_dir) / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_manual_import.json",
                          {"source": "manual_import", "file_name": Path(path).name, "retrieved_at": retrieved,
                           "rows": raw_rows,
                           "note": "forbidden script/ad-copy fields are stripped even from raw storage"}, readonly=True)
    accepted, rejected, by_pid = [], [], {}
    for i, row in enumerate(rows):
        c, errs = normalize_row(row, {"retrieved_at": retrieved, "raw_source_location": f"{raw_path}#row{i}"}, cfg)
        if errs:
            rejected.append({"row": i, "errors": errs})
            continue
        accepted.append(c)
        by_pid.setdefault(c["matched_product_id"], []).append(c)
    saved = [str(_save_processed(pid, cs, raw_path, "manual_import", processed_dir)) for pid, cs in by_pid.items()]
    return {"raw_file": str(raw_path), "rows": len(rows), "accepted": len(accepted), "rejected": rejected,
            "products": sorted(by_pid), "processed_files": saved}


def import_from_kalopilot(product_id, deep_raw_dir=DEEP_RAW_DIR, processed_dir=PROCESSED_DIR, cfg=None):
    """FREE: normalize top_videos from the latest SAVED deep answer (no network, no credits)."""
    prov = get_provider("kalopilot_saved", cfg, raw_dir=deep_raw_dir)
    raws = prov.search_creatives({"product_id": product_id})
    cs = [c for c in (prov.normalize_creative(r, {}) for r in raws) if c]
    if not cs:
        return {"product_id": str(product_id), "accepted": 0, "note": "no saved top_videos for this product"}
    path = _save_processed(str(product_id), cs, raws[0]["_file"], "kalopilot_saved", processed_dir)
    return {"product_id": str(product_id), "accepted": len(cs), "processed_file": str(path)}


def load_all(product_id, processed_dir=PROCESSED_DIR):
    """Latest observation of every creative ever imported for the product (older snapshots stay on disk)."""
    latest = {}
    for f in sorted((Path(processed_dir) / str(product_id)).glob("creatives_*.json")):
        for c in json.loads(f.read_text()).get("creatives", []):
            latest[c["creative_id"]] = c
    return list(latest.values())


def snapshot(a):
    return {"product_id": a["product_id"],
            "observed_at": max((c["observed_at"] for c in a["creatives"]), default=None),
            "creative_count": a["qualified_creatives"], "active_ad_count": a["active_ads"],
            "angle_distribution": a["angles"]["distribution"], "hook_distribution": a["hooks"]["distribution"],
            "format_distribution": a["formats"]["distribution"], "long_running_count": a["long_running_creatives"],
            "creative_ids": sorted(c["creative_id"] for c in a["creatives"] if c["qualified"]),
            "saturation_score": a["saturation"]["score"]}


def append_history(a, hist_dir=HISTORY_DIR):
    snap = snapshot(a)
    h = hashlib.sha256(json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()
    d = Path(hist_dir) / a["product_id"]
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        if json.loads(f.read_text()).get("snapshot_hash") == h:
            return "duplicate_snapshot", f
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return "written", _write_new(d / f"{stamp}.json", {**snap, "snapshot_hash": h, "imported_at": _now()}, readonly=True)


def history(product_id, hist_dir=HISTORY_DIR, cfg=None):
    cfg = cfg or load_cfg()
    d = Path(hist_dir) / str(product_id)
    snaps = sorted((json.loads(f.read_text()) for f in d.glob("*.json")),
                   key=lambda s: (s["observed_at"] or "", s["imported_at"])) if d.exists() else []
    changes = []
    for a, b in zip(snaps, snaps[1:]):
        new = set(b["creative_ids"]) - set(a["creative_ids"])
        changes.append({"from": a["observed_at"], "to": b["observed_at"], "new_creatives": len(new),
                        "new_creative_rate": round(len(new) / b["creative_count"], 4) if b["creative_count"] else None,
                        "creative_count_change": b["creative_count"] - a["creative_count"],
                        "long_running_change": b["long_running_count"] - a["long_running_count"],
                        "angle_distribution_changed": a["angle_distribution"] != b["angle_distribution"]})
    velocity = None
    if len(snaps) >= cfg["history"]["min_snapshots_for_velocity"]:
        s0, s1 = snaps[0], snaps[-1]
        try:
            days = (datetime.fromisoformat(s1["observed_at"]) - datetime.fromisoformat(s0["observed_at"])).days
        except (TypeError, ValueError):
            days = 0
        if days > 0 and None not in (s0["saturation_score"], s1["saturation_score"]):
            velocity = round((s1["saturation_score"] - s0["saturation_score"]) / days * 7, 3)
    return {"product_id": str(product_id), "snapshots": snaps, "changes": changes,
            "creative_saturation_velocity_per_week": velocity,
            "note": None if velocity is not None else "needs >= 2 snapshots on different days"}


def calibration_plan(product_ids, cfg=None):
    c = (cfg or load_cfg())["creative_calibration"]
    if not c.get("enabled"):
        return {"enabled": False, "products": [], "max_creatives_per_product": c["max_creatives_per_product"]}
    return {"enabled": True, "products": list(product_ids)[: c["max_products"]],
            "max_creatives_per_product": c["max_creatives_per_product"]}


def report_rows(products, processed_dir=PROCESSED_DIR, hist_dir=None, cfg=None):
    out = []
    for p in products:
        pid = p.get("product_id")
        cs = load_all(pid, processed_dir) if pid else []
        if not cs:
            continue
        d = p.get("deep") or {}
        a = analyze_product(pid, d, cs, cfg)
        if hist_dir:
            append_history(a, hist_dir)
        out.append({"product_id": pid, "name": p.get("name"),
                    "analysis": {k: v for k, v in a.items() if k != "creatives"}})
    return out
