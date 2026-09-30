"""Scoring engine: WPS (Winning Product Score) + Confidence Score.

Both are computed ONLY from config/scoring.yaml rules (Claude never assigns them).
They are independent: Confidence never changes WPS, WPS never changes Confidence.

Status:
  - Confidence Score: implemented (scripts/confidence.py)
  - WPS: NOT IMPLEMENTED YET -> reported as "N/A" (never guessed)

Usage:
    python3 scripts/score_products.py data/processed/<file>.json
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from confidence import NA, confidence_score, load_config as load_confidence_config  # noqa: E402


def wps_score(product):
    """WPS engine placeholder: returns N/A until implemented. Never estimates."""
    return {"score": NA, "status": "not_implemented", "version": "wps-v1"}


def score_product(product, confidence_cfg=None):
    wps = wps_score(product)
    conf = confidence_score(product, confidence_cfg)
    return {
        "product_id": product.get("product_id"),
        "product_name": product.get("product_name"),
        "wps": wps,
        "confidence": conf,
        "summary": {
            "WPS": f"{wps['score']}/100" if wps["score"] != NA else "N/A (engine not implemented)",
            "Confidence": f"{conf['score']}/100 ({conf['level']})",
        },
    }


def score_products(products, confidence_cfg=None):
    cfg = confidence_cfg or load_confidence_config()
    return [score_product(p, cfg) for p in products]


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 1
    data = json.load(open(argv[1]))
    products = data if isinstance(data, list) else [data]
    for r in score_products(products):
        print(r["product_name"] or r["product_id"])
        print(f"  WPS: {r['summary']['WPS']}")
        print(f"  Confidence: {r['summary']['Confidence']}")
        for name, c in r["confidence"]["breakdown"].items():
            print(f"    {name:24} {c['earned']:>6} / {c['max']:<3} {c['status']}")
        for w in r["confidence"]["warnings"]:
            print(f"  WARNING: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
