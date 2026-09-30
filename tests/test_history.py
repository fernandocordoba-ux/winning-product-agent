"""Tests for Historical Tracking (Step Q). SYNTHETIC DATA ONLY, in temp folders
(never mixed with real research data). Run: python3 -m unittest discover tests
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import history as H  # noqa: E402

CFG = H.load_cfg()
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def obs(pid="P1", ts=T0, wps=None, gmv=None, units=None, creators=None, videos=None, price=None, avs=None, bvs=None,
        name="SYNTHETIC product"):
    o = H.empty_observation()
    o.update({"product_id": pid, "identity_key": pid, "key_type": "product_id", "product_name": name,
              "category": "Home", "shop": "Syn", "market": "US", "observation_timestamp": H.iso(ts),
              "observation_date": ts.date().isoformat(), "source_stage": "deep_analysis"})
    o["tiktok"].update({"gmv": gmv, "units": units, "creator_count": creators, "video_count": videos, "price": price})
    o["scores"]["wps"] = wps
    o["amazon"]["avs"] = avs
    o["business"]["bvs"] = bvs
    o["source"] = {"stage": "deep_analysis", "raw_file": "data/raw/deep_analysis/synthetic.json",
                   "api_key": "should-never-be-stored"}
    return o


def series(values, start=T0, step_days=1.0, metric="gmv", pid="P1"):
    return [obs(pid, start + timedelta(days=i * step_days), **{metric: v}) for i, v in enumerate(values)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = H.HistoryStore(Path(self.tmp.name) / "history", CFG)

    def tearDown(self):
        for p in Path(self.tmp.name).rglob("*.json"):
            os.chmod(p, 0o644)
        self.tmp.cleanup()

    def add(self, *observations):
        return [self.store.append(o) for o in observations]


# ------------------------------------------------------------------ storage
class Storage(Base):
    def test_first_observation(self):
        (action, path), = self.add(obs(wps=70, gmv=1000))
        self.assertEqual(action, "written")
        self.assertEqual(path.parent.name, "P1")
        self.assertTrue(path.name.startswith("2026-09-01T"))
        stored = json.loads(path.read_text())
        self.assertEqual(stored["scores"]["wps"], 70)
        self.assertEqual(stored["source"]["raw_file"], "data/raw/deep_analysis/synthetic.json")
        self.assertNotIn("api_key", stored["source"])                   # secrets never stored
        self.assertTrue(H.get_product_history("P1", self.store)[0]["_path"].endswith(path.name))

    def test_second_observation(self):
        self.add(obs(wps=70), obs(ts=T0 + timedelta(days=1), wps=75))
        self.assertEqual(len(H.get_product_history("P1", self.store)), 2)
        self.assertEqual(H.get_latest_observation("P1", self.store)["scores"]["wps"], 75)
        self.assertEqual(H.get_previous_observation("P1", self.store)["scores"]["wps"], 70)

    def test_duplicate_exact_timestamp(self):
        (a1, p1), (a2, p2) = self.add(obs(wps=70), obs(wps=99))
        self.assertEqual((a1, a2), ("written", "duplicate"))
        self.assertEqual(p1, p2)
        self.assertEqual(json.loads(p1.read_text())["scores"]["wps"], 70)   # original kept
        self.assertEqual(len(H.get_product_history("P1", self.store)), 1)

    def test_multiple_observations_same_day(self):
        self.add(obs(ts=T0, wps=70), obs(ts=T0 + timedelta(hours=5), wps=72))
        h = H.get_product_history("P1", self.store)
        self.assertEqual(len(h), 2)
        self.assertEqual(H.tracking_age(h)["days_observed"], 1)

    def test_append_only_and_immutable(self):
        (_, p), = self.add(obs(wps=70))
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        self.assertFalse(os.stat(p).st_mode & 0o222)                     # read-only
        self.add(obs(wps=1), obs(ts=T0 + timedelta(days=3), wps=80))
        self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), digest)
        with self.assertRaises(ValueError):
            self.store.append({"identity_key": None, "observation_timestamp": "bad"})

    def test_fallback_identity(self):
        o = obs(pid=None)
        o["identity_key"] = "fallback:abc123"
        _, path = self.store.append(o)
        self.assertEqual(path.parent.name, "fallback_abc123")

    def test_observations_between(self):
        self.add(*[obs(ts=T0 + timedelta(days=i), wps=70 + i) for i in range(10)])
        got = H.get_observations_between("P1", "2026-09-03", "2026-09-05", self.store)
        self.assertEqual([o["scores"]["wps"] for o in got], [72, 73, 74])

    def test_index_update(self):
        self.add(obs(ts=T0, wps=70, gmv=1000, units=10, creators=5, videos=9),
                 obs(ts=T0 + timedelta(days=2), wps=78, gmv=None, units=12))
        idx = self.store.rebuild_index()
        p = idx["products"]["P1"]
        self.assertEqual((p["first_seen_date"], p["last_seen_date"]), ("2026-09-01", "2026-09-03"))
        self.assertEqual((p["observation_count"], p["days_observed"]), (2, 2))
        self.assertEqual((p["latest_wps"], p["previous_wps"]), (78, 70))
        self.assertEqual(p["latest_gmv"], 1000)                           # last VALID value, not the missing one
        self.assertEqual(p["latest_units"], 12)
        self.add(obs(ts=T0 + timedelta(days=4), wps=81))
        self.assertEqual(self.store.rebuild_index()["products"]["P1"]["observation_count"], 3)
        self.assertTrue(self.store.index_path.exists())


# ------------------------------------------------------------------ deltas / velocity
class Deltas(Base):
    def test_missing_values_stay_na(self):
        h = [obs(ts=T0, wps=70), obs(ts=T0 + timedelta(days=1), gmv=500)]
        d = H.deltas_from(h)
        self.assertEqual(d["gmv"]["delta"], "N/A")
        self.assertEqual(d["gmv"]["percent_change"], "N/A")
        self.assertEqual(d["wps"]["delta"], "N/A")
        self.assertEqual(d["avs"]["delta"], "N/A")

    def test_percentage_change(self):
        d = H.deltas_from([obs(ts=T0, gmv=1000, units=100), obs(ts=T0 + timedelta(days=1), gmv=1250, units=90)])
        self.assertEqual((d["gmv"]["delta"], d["gmv"]["percent_change"]), (250, 25.0))
        self.assertEqual((d["units"]["delta"], d["units"]["percent_change"]), (-10, -10.0))   # negative change

    def test_valid_zero_and_previous_zero(self):
        d = H.deltas_from([obs(ts=T0, gmv=0, videos=0, creators=0),
                           obs(ts=T0 + timedelta(days=1), gmv=500, videos=0, creators=3)])
        self.assertEqual(d["gmv"]["percent_change"], "NEW_GROWTH")       # never infinity
        self.assertEqual(d["gmv"]["delta"], 500)
        self.assertEqual(d["videos"]["percent_change"], 0.0)             # 0 -> 0 is a real "no change"
        self.assertEqual(d["creators"]["percent_change"], "NEW_GROWTH")

    def test_wps_avs_bvs_deltas(self):
        d = H.deltas_from([obs(ts=T0, wps=70, avs=60, bvs=40), obs(ts=T0 + timedelta(days=2), wps=74.5, avs=55, bvs=40)])
        self.assertEqual((d["wps"]["delta"], d["avs"]["delta"], d["bvs"]["delta"]), (4.5, -5, 0))

    def test_velocity_calculation(self):
        v = H.velocity_from([obs(ts=T0, gmv=1000, wps=70), obs(ts=T0 + timedelta(days=2), gmv=1400, wps=76)])
        self.assertEqual(v["gmv_velocity_per_day"], 200.0)
        self.assertEqual(v["gmv_velocity_per_week"], 1400.0)
        self.assertEqual(v["wps_velocity_per_day"], 3.0)
        self.assertEqual(v["units_velocity_per_day"], "N/A")

    def test_irregular_observation_spacing(self):
        v = H.velocity_from([obs(ts=T0, gmv=1000), obs(ts=T0 + timedelta(hours=36), gmv=1300)])
        self.assertEqual(v["gmv_elapsed_days"], 1.5)
        self.assertEqual(v["gmv_velocity_per_day"], 200.0)               # 300 / 1.5 days, not / 1 day
        v2 = H.velocity_from([obs(ts=T0, gmv=1000), obs(ts=T0 + timedelta(days=5, hours=12), gmv=2100)])
        self.assertEqual(v2["gmv_velocity_per_day"], 200.0)


# ------------------------------------------------------------------ trends / volatility
class Trends(Base):
    def label(self, values, window="short", step_days=1.0, metric="gmv"):
        return H.trends_from(series(values, step_days=step_days, metric=metric), CFG)[window][metric]

    def test_7_day_trend_growing(self):
        t = self.label([100, 110, 120, 130, 140, 150, 160, 170])
        self.assertEqual(t["label"], "GROWING")                          # steady linear growth
        self.assertEqual(t["consistency"], 1.0)

    def test_7_day_trend_accelerating(self):
        t = self.label([100, 102, 104, 106, 120, 140, 170, 210])
        self.assertEqual(t["label"], "ACCELERATING", t)

    def test_declining(self):
        self.assertEqual(self.label([200, 190, 180, 170, 160, 150, 140, 130])["label"], "DECLINING")

    def test_stable(self):
        self.assertEqual(self.label([100, 101, 100, 102, 101, 100, 101, 102])["label"], "STABLE")

    def test_volatile(self):
        self.assertEqual(self.label([100, 400, 50, 380, 60, 420, 40, 390])["label"], "VOLATILE")

    def test_14_day_trend(self):
        vals = [100 + 5 * i for i in range(15)]
        t = H.trends_from(series(vals), CFG)
        self.assertEqual(t["medium"]["gmv"]["label"], "GROWING")
        self.assertEqual(t["medium"]["gmv"]["observations"], 15)
        self.assertEqual(t["short"]["gmv"]["observations"], 8)           # only last 7 days in short window

    def test_30_day_trend(self):
        vals = [1000 - 10 * i for i in range(31)]
        t = H.trends_from(series(vals), CFG)
        self.assertEqual(t["long"]["gmv"]["label"], "DECLINING")

    def test_wps_trend_uses_points(self):
        t = self.label([60, 61, 62, 63, 64, 65, 66, 67], metric="wps")
        self.assertEqual(t["change_unit"], "points")
        self.assertEqual(t["change"], 7)

    def test_insufficient_history(self):
        self.assertEqual(self.label([100, 150])["label"], "INSUFFICIENT_DATA")          # 2 points < 3
        self.assertEqual(self.label([100, 150, 200], step_days=0.25)["label"], "INSUFFICIENT_DATA")  # span too short
        t = H.trends_from(series([100, 110, 120, 130, 140, 150, 160, 170]), CFG)
        self.assertEqual(t["long"]["gmv"]["label"], "INSUFFICIENT_DATA")                 # 7 days < 30 * 0.4
        snap = H.snapshot_from([obs(wps=70, gmv=100)], CFG)["snapshot"]
        self.assertTrue(snap["insufficient_history"])
        self.assertEqual(snap["short_trend"], "INSUFFICIENT_DATA")

    def test_new_growth_trend(self):
        t = self.label([0, 50, 100, 150, 200, 250, 300, 350])
        self.assertEqual(t["change"], "NEW_GROWTH")
        self.assertIn(t["label"], ("GROWING", "ACCELERATING"))

    def test_volatility(self):
        low = H.volatility_from(series([100, 102, 98, 101, 99]), CFG)
        high = H.volatility_from(series([100, 300, 50, 250, 80]), CFG)
        few = H.volatility_from(series([100, 300]), CFG)
        self.assertEqual(low["gmv"]["level"], "LOW")
        self.assertEqual(high["gmv"]["level"], "HIGH")
        self.assertEqual(few["gmv"]["level"], "INSUFFICIENT_DATA")
        self.assertEqual(high["volatility_level"], "HIGH")                # primary metric = gmv

    def test_snapshot(self):
        self.add(obs(ts=T0, wps=70, gmv=1000, creators=10, videos=20),
                 obs(ts=T0 + timedelta(days=3), wps=76, gmv=1500, creators=14, videos=20))
        s = H.momentum_snapshot("P1", self.store)["snapshot"]
        self.assertEqual((s["latest_wps"], s["previous_wps"], s["wps_change"]), (76, 70, 6))
        self.assertEqual((s["gmv_change"], s["gmv_percent_change"]), (500, 50.0))
        self.assertEqual((s["creator_change"], s["video_change"]), (4, 0))
        self.assertEqual((s["observation_count"], s["days_observed"], s["product_tracking_age_days"]), (2, 2, 3))

    def test_utilities_by_product_id(self):
        self.add(obs(ts=T0, gmv=100, wps=70), obs(ts=T0 + timedelta(days=1), gmv=120, wps=71))
        self.assertEqual(H.calculate_deltas("P1", self.store)["gmv"]["delta"], 20)
        self.assertEqual(H.calculate_velocity("P1", self.store)["gmv_velocity_per_day"], 20.0)
        self.assertIn("short", H.calculate_trends("P1", self.store))
        self.assertEqual(H.get_product_history("UNKNOWN", self.store), [])


# ------------------------------------------------------------------ migration
class Migration(Base):
    def make_processed(self):
        proc = Path(self.tmp.name) / "processed"
        (proc / "deep_analysis").mkdir(parents=True)
        (proc / "amazon_validation").mkdir()
        (proc / "business_viability").mkdir()
        rec = {"key": "D1", "key_type": "product_id", "observation_date": "2026-09-30", "missing_fields": ["shop_count"],
               "facts": {"product_id": "D1", "product_name": "SYNTHETIC D1", "category": "Home", "shop_name": "S",
                         "price_min": 20, "price_max": 30, "gmv_30d": 50000, "units_30d": 900, "growth_30d": 40,
                         "creator_count": 50, "video_count": 80},
               "source": {"raw_file": "data/raw/syn.json", "task_id": "t", "fetched_at": "20260930T014532Z"},
               "calculated": {"filter_status": "PASS", "filter_reasons": [], "price": 25.0}}
        (proc / "discovery_20260930T015607Z.json").write_text(json.dumps({"candidates": [rec], "failed": []}))
        deep = {"status": "ok", "product_id": "D1", "product_name": "SYNTHETIC D1", "wps": 78, "confidence": 70,
                "gmv": 52000, "units": 950, "price": {"avg": 25.0}, "creator_metrics": {"total": 55},
                "video_metrics": {"total": 90}, "observation_timestamp": "2026-10-01T10:00:00+00:00",
                "source": {"raw_file": "data/raw/deep_analysis/syn.json"}, "red_flags": [{"flag": "LOW_DATA_CONFIDENCE"}]}
        (proc / "deep_analysis" / "deep_1.json").write_text(json.dumps({"results": [deep]}))
        amz = {"status": "ok", "product_id": "D1", "avs": 66, "amazon_confidence": 70, "amazon_price": 27,
               "observation_timestamp": "2026-10-01T11:00:00+00:00", "source": {"raw_file": "data/raw/amz.json"}}
        (proc / "amazon_validation" / "amazon_1.json").write_text(json.dumps({"results": [amz]}))
        return proc

    def test_migration_dry_run(self):
        proc = self.make_processed()
        r = H.migrate(proc, self.store, dry_run=True)
        self.assertTrue(r["summary"]["dry_run"])
        self.assertEqual(r["summary"]["new"], 2)                          # 1 discovery + 1 deep observation
        self.assertEqual(r["summary"]["by_stage"], {"discovery": 1, "deep_analysis": 1})
        self.assertFalse(self.store.base.exists())                        # nothing written
        self.assertTrue((proc / "discovery_20260930T015607Z.json").exists())   # sources untouched

    def test_migration_apply_preserves_timestamps_and_sources(self):
        proc = self.make_processed()
        H.migrate(proc, self.store, dry_run=False)
        h = H.get_product_history("D1", self.store)
        self.assertEqual([o["observation_timestamp"] for o in h],
                         ["2026-09-30T01:45:32+00:00", "2026-10-01T10:00:00+00:00"])
        deep_obs = h[1]
        self.assertEqual(deep_obs["amazon"]["avs"], 66)                   # attached to the same cycle
        self.assertEqual(deep_obs["source"]["amazon_raw_file"], "data/raw/amz.json")
        self.assertEqual(h[0]["source"]["raw_file"], "data/raw/syn.json")
        self.assertIsNone(h[0]["scores"]["wps"])                          # discovery has no WPS: never invented
        self.assertTrue(self.store.index_path.exists())

    def test_migration_duplicate_prevention(self):
        proc = self.make_processed()
        H.migrate(proc, self.store, dry_run=False)
        r = H.migrate(proc, self.store, dry_run=False)
        self.assertEqual(r["summary"]["new"], 0)
        self.assertEqual(r["summary"]["duplicates"], 2)
        self.assertEqual(len(H.get_product_history("D1", self.store)), 2)


if __name__ == "__main__":
    unittest.main()
