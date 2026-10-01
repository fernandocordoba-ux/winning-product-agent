"""Real supplier sources for the supplier layer (Step W): CJdropshipping API and Zendrop (MCP connector records).

READ-ONLY. This module never places orders, never adds products to a store and never contacts a supplier.
Nothing is estimated: a field the source does not return stays None (N/A). Offers are written in the manual-import
format and go through suppliers.import_file(), so match confidence, Supplier Quality / Confidence and economics
use the same rules as every other offer (a weak name match never feeds BVS).

CJdropshipping
  key:   env CJ_API_KEY, else ~/.cj/api_key (never printed, never written to the project)
  token: ~/.cj/token.json (0600), access token valid 180 days, refreshed when expired
  limit: 1 request per second (CJ QPS = 1)
Zendrop
  The Zendrop MCP connector is called by the assistant; its raw catalog / shipping records are saved as JSON and
  converted here with zendrop_rows().
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

CJ_BASE = "https://developers.cjdropshipping.com/api2.0/v1/"
CJ_DIR = Path.home() / ".cj"
STOP = {"the", "a", "an", "and", "or", "with", "for", "of", "to", "in", "on", "by", "new", "hot", "best", "set", "kit",
        "pcs", "pack", "piece", "pieces", "women", "men", "gift", "gifts", "premium", "great", "option", "full",
        "free", "&", "-", "|", "1", "2", "3", "4", "5"}
READ_ONLY_PATHS = ("authentication/getAccessToken", "authentication/refreshAccessToken", "product/listV2",
                   "product/variant/query", "logistic/freightCalculate")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _clean(name):
    name = re.sub(r"[【\[(（][^】\])）]*[】\])）]", " ", name or "")          # drop bracketed shop tags
    return re.split(r"\s[-–|,]\s|,", name)[0]                               # main clause only


def keywords(name, max_words=4):
    """Short supplier search phrase from a TikTok product title (first content words of the main clause)."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", _clean(name).lower()) if w and w not in STOP and len(w) > 1
             and not re.fullmatch(r"\d+[a-z]*", w)]
    return " ".join(words[:max_words])


def keyword_candidates(name, max_words=3):
    """Several phrases (with and without the leading word, which is often a brand) — results are ranked by the
    supplier layer's own match score, so a wrong phrase only costs a search, never a wrong match."""
    words = keywords(name, 8).split()
    out = []
    for c in (words[1:1 + max_words], words[:max_words], words[-max_words:]):
        k = " ".join(c)
        if k and k not in out:
            out.append(k)
    return out


def parse_days(aging):
    m = re.match(r"\s*(\d+)\s*-\s*(\d+)", str(aging or ""))
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"\s*(\d+)\s*$", str(aging or ""))
    return (int(m.group(1)), int(m.group(1))) if m else (None, None)


# ============================================================================ CJdropshipping (read-only)
class CJClient:
    def __init__(self, key=None, token_dir=CJ_DIR, opener=None, sleep=1.1):
        self.key = key
        self.dir = Path(token_dir)
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep
        self._last = 0.0

    def _api_key(self):
        k = self.key or os.environ.get("CJ_API_KEY")
        if not k and (self.dir / "api_key").exists():
            k = (self.dir / "api_key").read_text().strip()
        if not k:
            raise RuntimeError("CJ API key not configured (env CJ_API_KEY)")
        return k

    def _raw(self, method, path, params=None, body=None, token=None):
        if path not in READ_ONLY_PATHS:
            raise PermissionError(f"CJ path {path} is not in the read-only allowlist")
        wait = self.sleep - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        url = CJ_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        h = {"Content-Type": "application/json"}
        if token:
            h["CJ-Access-Token"] = token
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=h,
                                     method=method)
        try:
            return json.loads(self.opener(req, timeout=40).read())
        finally:
            self._last = time.time()

    def token(self):
        p = self.dir / "token.json"
        if p.exists():
            d = json.loads(p.read_text())
            exp = d.get("accessTokenExpiryDate")
            try:
                if exp and datetime.fromisoformat(exp) > datetime.now(timezone.utc):
                    return d["accessToken"]
            except ValueError:
                pass
        r = self._raw("POST", "authentication/getAccessToken", body={"apiKey": self._api_key()})
        if not (r.get("result") and r.get("data")):
            raise RuntimeError(f"CJ token request failed: {r.get('code')} {r.get('message')}")
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        p.write_text(json.dumps(r["data"]))
        os.chmod(p, 0o600)
        return r["data"]["accessToken"]

    def call(self, method, path, params=None, body=None):
        r = self._raw(method, path, params, body, token=self.token())
        if not r.get("result"):
            raise RuntimeError(f"CJ {path} failed: {r.get('code')} {r.get('message')}")
        return r.get("data")

    def search(self, keyword, size=5):
        d = self.call("GET", "product/listV2", {"keyWord": keyword, "page": 1, "size": size}) or {}
        out = []
        for block in d.get("content") or []:
            out += block.get("productList") or []
        return out[:size]

    def variants(self, pid):
        return self.call("GET", "product/variant/query", {"pid": pid}) or []

    def freight(self, vid, start="CN", end="US", qty=1):
        return self.call("POST", "logistic/freightCalculate",
                         body={"startCountryCode": start, "endCountryCode": end,
                               "products": [{"quantity": qty, "vid": vid}]}) or []


def cj_rows_for_product(client, product, max_results=3):
    """CJ offers for one TikTok product -> manual-import rows (one per CJ product: cheapest variant + cheapest US
    freight). Returns (rows, log)."""
    import suppliers as SUP
    kws = keyword_candidates(product.get("name"))
    log = {"product_id": str(product.get("product_id")), "keywords": kws, "searched_at": _now(), "results": []}
    rows = []
    if not kws:
        log["error"] = "no searchable keyword"
        return rows, log
    seen, found = set(), []
    for kw in kws:
        for item in client.search(kw, size=10):
            if item.get("id") in seen:
                continue
            seen.add(item.get("id"))
            sc, cls, _ = SUP.match_confidence({"name": product.get("name")}, {"supplier_product_title": item.get("nameEn")})
            found.append((sc if sc is not None else -1, item))
    found.sort(key=lambda x: -x[0])
    log["ranked"] = [{"cj_pid": i.get("id"), "title": i.get("nameEn"), "match": s} for s, i in found[:8]]
    for _, item in found[:max_results]:
        pid = item.get("id")
        vs = [v for v in client.variants(pid) if v.get("variantSellPrice") is not None]
        if not vs:
            log["results"].append({"cj_pid": pid, "skipped": "no priced variant"})
            continue
        v = min(vs, key=lambda x: float(x["variantSellPrice"]))
        opts = [o for o in client.freight(v["vid"]) if o.get("logisticPrice") is not None]
        if not opts:
            log["results"].append({"cj_pid": pid, "skipped": "no US freight option"})
            continue
        f = min(opts, key=lambda o: float(o["logisticPrice"]))
        lo, hi = parse_days(f.get("logisticAging"))
        inv = item.get("warehouseInventoryNum")
        rows.append({
            "matched_product_id": str(product.get("product_id")), "supplier_source": "cjdropshipping_api",
            "supplier_name": "CJdropshipping", "supplier_url": f"https://cjdropshipping.com/product/-p-{pid}.html",
            "supplier_product_id": pid, "supplier_product_title": item.get("nameEn"),
            "variants": v.get("variantNameEn"), "product_cost": float(v["variantSellPrice"]), "currency": "USD",
            "shipping_cost": float(f["logisticPrice"]), "shipping_method": f.get("logisticName"),
            "estimated_delivery_min_days": lo, "estimated_delivery_max_days": hi, "ship_to_country": "US",
            "available_quantity": inv if isinstance(inv, int) else None,
            "inventory_status": ("IN_STOCK" if isinstance(inv, int) and inv > 0 else
                                 "OUT_OF_STOCK" if inv == 0 else None),
            "observed_at": _now(),
        })
        log["results"].append({"cj_pid": pid, "title": item.get("nameEn"), "variant": v.get("variantNameEn"),
                               "cost": v["variantSellPrice"], "freight": f})
    return rows, log


# ============================================================================ Zendrop (records saved from the MCP)
def zendrop_rows(records):
    """records: [{"matched_product_id", "product": <get_catalog_product>, "shipping": <get_catalog_shipping_estimate>}]
    -> manual-import rows (cheapest variant + cheapest US option). Missing values stay None."""
    rows = []
    for r in records:
        p, s = r.get("product") or {}, r.get("shipping") or {}
        variants = [v for v in p.get("variants") or [] if v.get("price") not in (None, "")]
        v = min(variants, key=lambda x: float(x["price"])) if variants else None
        price = float(v["price"]) if v else (float(p["price"]) if p.get("price") not in (None, "") else None)
        opts = [o for o in s.get("shipping_options") or [] if o.get("price") is not None]
        o = min(opts, key=lambda x: float(x["price"])) if opts else None
        days = re.findall(r"\d+", str((o or {}).get("estimated_delivery") or ""))
        lo, hi = (int(days[0]), int(days[-1])) if days else (None, None)
        sup = p.get("supplier") or {}
        rows.append({
            "matched_product_id": str(r["matched_product_id"]), "supplier_source": "zendrop_mcp",
            "supplier_name": f"Zendrop ({sup.get('name')})" if sup.get("name") else "Zendrop",
            "supplier_url": p.get("product_url") or f"https://app.zendrop.com/product/{p.get('id')}",
            "supplier_product_id": str(p.get("id")) if p.get("id") is not None else None,
            "supplier_product_title": p.get("name"),
            "variants": (v or {}).get("title") or (v or {}).get("name"), "product_cost": price, "currency": "USD",
            "shipping_cost": float(o["price"]) if o else None, "shipping_method": (o or {}).get("type"),
            "estimated_delivery_min_days": lo, "estimated_delivery_max_days": hi, "ship_to_country": "US",
            "ship_from_country": sup.get("country"),
            "inventory_status": ("IN_STOCK" if (p.get("availability") or {}).get("in_stock") is True else
                                 "OUT_OF_STOCK" if (p.get("availability") or {}).get("in_stock") is False else None),
            "observed_at": r.get("observed_at") or _now(),
        })
    return rows


def write_import_file(rows, out_dir, label):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_{label}.json"
    p.write_text(json.dumps({"offers": rows}, indent=1, ensure_ascii=False))
    return p
