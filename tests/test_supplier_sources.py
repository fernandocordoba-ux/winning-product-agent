"""CJdropshipping / Zendrop supplier sources: read-only, nothing estimated. Network is faked."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import supplier_sources as SS  # noqa: E402
import suppliers as SUP  # noqa: E402


class FakeResp:
    def __init__(self, data):
        self.data = data

    def read(self):
        return json.dumps(self.data).encode()


def fake_cj(calls):
    def opener(req, timeout=None):
        path = req.full_url.split("/api2.0/v1/")[1].split("?")[0]
        calls.append((req.get_method(), path, req.headers))
        if path == "authentication/getAccessToken":
            return FakeResp({"result": True, "code": 200, "data": {"accessToken": "T", "accessTokenExpiryDate":
                                                                   "2099-01-01T00:00:00+08:00"}})
        if path == "product/listV2":
            return FakeResp({"result": True, "data": {"content": [{"productList": [
                {"id": "P1", "nameEn": "Silicone Kitchen Utensil Set 10pcs", "warehouseInventoryNum": 50}]}]}})
        if path == "product/variant/query":
            return FakeResp({"result": True, "data": [{"vid": "V1", "variantSellPrice": 4.5, "variantNameEn": "Black"},
                                                      {"vid": "V2", "variantSellPrice": 3.9, "variantNameEn": "Red"}]})
        if path == "logistic/freightCalculate":
            return FakeResp({"result": True, "data": [{"logisticName": "A", "logisticPrice": 7.2, "logisticAging": "6-12"},
                                                      {"logisticName": "B", "logisticPrice": 5.1, "logisticAging": "8-15"}]})
        raise AssertionError(path)
    return opener


class CJ(unittest.TestCase):
    def test_rows_and_import(self):
        calls = []
        d = Path(tempfile.mkdtemp())
        c = SS.CJClient(key="CJ1@api@x", token_dir=d, opener=fake_cj(calls), sleep=0)
        rows, log = SS.cj_rows_for_product(c, {"product_id": "TT1", "name": "10pcs Silicone Kitchen Utensil Set"})
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["product_cost"], r["shipping_cost"], r["shipping_method"]), (3.9, 5.1, "B"))
        self.assertEqual((r["estimated_delivery_min_days"], r["estimated_delivery_max_days"]), (8, 15))
        self.assertEqual(r["supplier_source"], "cjdropshipping_api")
        self.assertTrue(all(h.get("Cj-access-token") == "T" for m, p, h in calls if p != "authentication/getAccessToken"))
        self.assertEqual(oct((d / "token.json").stat().st_mode & 0o777), "0o600")
        f = SS.write_import_file(rows, d / "in", "cj")
        res = SUP.import_file(f, raw_dir=d / "raw", processed_dir=d / "proc", hist_dir=d / "hist")
        self.assertEqual(res["accepted"], 1, res["rejected"])

    def test_read_only_allowlist(self):
        c = SS.CJClient(key="k", token_dir=tempfile.mkdtemp(), opener=fake_cj([]), sleep=0)
        for bad in ("shopping/order/createOrder", "shopping/order/confirmOrder", "product/addToMyProduct"):
            with self.assertRaises(PermissionError):
                c._raw("POST", bad, body={})

    def test_missing_key(self):
        import os
        old = os.environ.pop("CJ_API_KEY", None)
        try:
            c = SS.CJClient(token_dir=tempfile.mkdtemp(), opener=fake_cj([]), sleep=0)
            with self.assertRaises(RuntimeError):
                c.token()
        finally:
            if old is not None:
                os.environ["CJ_API_KEY"] = old


class Zendrop(unittest.TestCase):
    def test_rows_keep_missing_as_none(self):
        rows = SS.zendrop_rows([
            {"matched_product_id": "TT1", "product": {"id": 9, "name": "Utensil Set", "price": "4.87",
                                                      "supplier": {"name": "Zendrop Fulfillment", "country": "CN"},
                                                      "availability": {"in_stock": True}},
             "shipping": {"shipping_options": [{"type": "regular", "price": 27.46, "estimated_delivery": "8 days"}]}},
            {"matched_product_id": "TT2", "product": {"id": 10, "name": "X"}, "shipping": {}}])
        a, b = rows
        self.assertEqual((a["product_cost"], a["shipping_cost"], a["estimated_delivery_max_days"]), (4.87, 27.46, 8))
        self.assertIsNone(b["product_cost"])
        self.assertIsNone(b["shipping_cost"])
        d = Path(tempfile.mkdtemp())
        res = SUP.import_file(SS.write_import_file(rows, d, "zd"), raw_dir=d / "r", processed_dir=d / "p", hist_dir=d / "h")
        self.assertEqual(res["accepted"], 1)                 # the incomplete row is rejected, never filled in

    def test_keywords(self):
        self.assertEqual(SS.keywords("Rechargeable Motorized Electric Shower Back Scrubber - Long Handle"),
                         "rechargeable motorized electric shower")
        self.assertEqual(SS.keywords(""), "")
        self.assertEqual(SS.keywords("【FaddishDeal】Durable Air Conditioner Cover, Full Mesh"), "durable air conditioner cover")
        self.assertIn("collapsible colanders sturdy", SS.keyword_candidates("Gevoli Collapsible Colanders 3-Pack Sturdy"))


if __name__ == "__main__":
    unittest.main()
