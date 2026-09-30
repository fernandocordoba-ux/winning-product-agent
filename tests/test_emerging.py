"""Tests for the Emerging Product Detector (Step R). SYNTHETIC DATA ONLY, in temp
folders (never mixed with real research data). Run: python3 -m unittest discover tests
"""
import copy
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

import emerging as E  # noqa: E402
import history as H  # noqa: E402

CFG = E.load_cfg()
HCFG = H.load_cfg()
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)

FIELDS = {"wps": ("scores", "wps"), "conf": ("scores", "confidence"), "gmv": ("tiktok", "gmv"),
          "units": ("tiktok", "units"), "creators": ("tiktok", "creator_count"),
          "scr": ("tiktok", "selling_creator_count"), "videos": ("tiktok", "video_count"),
          "svid": ("tiktok", "selling_video_count"), "shops": ("tiktok", "shop_count"),
          "tc": ("concentration", "top_creator_revenue_share"), "tv": ("concentration", "top_video_revenue_share")}


def ob(day, pid="S", **vals):
    o = H.empty_observation()
    ts = T0 + timedelta(days=day)
    o.update({"product_id": pid, "identity_key": pid, "product_name": f"SYNTHETIC {pid}",
              "observation_timestamp": H.iso(ts), "observation_date": ts.date().isoformat()})
    for k, v in vals.items():
        sec, f = FIELDS[k]
        o[sec][f] = v
    return o


def strong_rows(**over):
    """7 observations, 2 days apart: everything accelerating, low concentration -> EMERGING_STRONG."""
    rows = [(72, 40000, 1300, 300, 80, 500, 120, 10), (73, 43000, 1400, 320, 90, 540, 135, 10),
            (74, 47000, 1520, 345, 102, 590, 152, 11), (76, 53000, 1700, 380, 118, 660, 175, 11),
            (78, 62000, 1980, 430, 140, 760, 205, 12), (81, 75000, 2400, 500, 170, 900, 250, 12),
            (84, 93000, 2950, 600, 210, 1100, 310, 13)]
    out = []
    for i, (w, g, u, c, sc, v, sv, sh) in enumerate(rows):
        vals = dict(wps=w, conf=80, gmv=g, units=u, creators=c, scr=sc, videos=v, svid=sv, shops=sh, tc=20, tv=10)
        vals.update({k: (fn(i) if callable(fn) else fn) for k, fn in over.items()})
        out.append(ob(2 * i, **vals))
    return out


def geometric(start, rate, n):
    return [round(start * (1 + rate) ** i, 4) for i in range(n)]


def ev(obs):
    return E.evaluate(obs, CFG, HCFG, NOW)


class Eligibility(unittest.TestCase):
    def test_one_observation_insufficient(self):
        r = ev([ob(0, wps=80, conf=80, gmv=100)])
        self.assertEqual(r["emerging_status"], "INSUFFICIENT_HISTORY")
        self.assertIn("INSUFFICIENT_HISTORY", r["emerging_flags"])

    def test_two_observations_insufficient_when_minimum_is_3(self):
        r = ev([ob(0, wps=80, conf=80, gmv=100), ob(2, wps=82, conf=80, gmv=150)])
        self.assertEqual(r["emerging_status"], "INSUFFICIENT_HISTORY")
        self.assertIn("2 observation(s) < 3", r["status_reason"])

    def test_three_observations_same_day_insufficient_days(self):
        r = ev([ob(0, wps=80, conf=80), ob(0.1, wps=81, conf=80), ob(0.2, wps=82, conf=80)])
        self.assertIn("observation day(s) < 3", r["status_reason"])

    def test_three_valid_observations_evaluated(self):
        r = ev([ob(d, wps=80, conf=80, gmv=g, units=u) for d, g, u in ((0, 100, 10), (1, 110, 11), (2, 140, 14))])
        self.assertNotEqual(r["emerging_status"], "INSUFFICIENT_HISTORY")
        self.assertEqual(r["observation_count"], 3)
        self.assertIsInstance(r["momentum_score"], float)

    def test_missing_wps_is_insufficient(self):
        r = ev([ob(d, gmv=100 + d) for d in range(4)])
        self.assertEqual(r["emerging_status"], "INSUFFICIENT_HISTORY")
        self.assertIn("current WPS not available", r["status_reason"])


class Acceleration(unittest.TestCase):
    def test_accelerating_gmv_formula(self):
        r = ev([ob(0, wps=80, conf=80, gmv=100), ob(1, wps=80, conf=80, gmv=110), ob(2, wps=80, conf=80, gmv=140)])
        a = r["acceleration_detail"]["gmv"]
        # prev_vel = 10/day, cur_vel = 30/day, acc = 20/day^2 ; normalized by middle value 110 -> % per day
        self.assertEqual(a["raw_acceleration_per_day2"], 20.0)
        self.assertEqual(a["previous_velocity"], 9.0909)
        self.assertEqual(a["current_velocity"], 27.2727)
        self.assertEqual(a["label"], "ACCELERATING")
        self.assertEqual(r["gmv_acceleration"], 18.1818)

    def test_decelerating_gmv(self):
        r = ev([ob(0, wps=80, conf=80, gmv=100), ob(1, wps=80, conf=80, gmv=140), ob(2, wps=80, conf=80, gmv=150)])
        self.assertEqual(r["acceleration_detail"]["gmv"]["label"], "DECELERATING")
        self.assertIn("MOMENTUM_SLOWING", r["emerging_flags"])

    def test_irregular_timestamps(self):
        r = ev([ob(0, wps=80, conf=80, gmv=100), ob(2, wps=80, conf=80, gmv=120), ob(2.5, wps=80, conf=80, gmv=150)])
        a = r["acceleration_detail"]["gmv"]
        self.assertEqual(a["raw_acceleration_per_day2"], 50.0)          # (30 / 0.5) - (20 / 2) = 60 - 10
        self.assertEqual(r["gmv_velocity"], 60.0)                       # real elapsed 0.5 day, not 1 day

    def test_selling_creator_acceleration_preferred(self):
        obs = strong_rows(scr=lambda i: [10, 11, 12, 13, 15, 20, 30][i])
        r = ev(obs)
        self.assertEqual(r["acceleration_detail"]["selling_creators"]["label"], "ACCELERATING")
        self.assertEqual(r["acceleration_detail"]["creators_primary"]["label"], "ACCELERATING")
        self.assertEqual(r["momentum_score_breakdown"]["creator_momentum"]["rate_per_day"],
                         r["momentum_score_breakdown"]["creator_momentum"]["rate_per_day"])


class Momentum(unittest.TestCase):
    def test_growing_units_and_demand(self):
        r = ev(strong_rows())
        self.assertIn(r["trends"]["units"], ("GROWING", "ACCELERATING"))
        self.assertEqual(r["demand_momentum"], "STRONG_GROWTH")

    def test_declining_demand(self):
        obs = strong_rows(gmv=lambda i: 90000 - 8000 * i, units=lambda i: 3000 - 250 * i)
        r = ev(obs)
        self.assertEqual(r["demand_momentum"], "DECLINING")
        self.assertIn("DEMAND_DECLINING", r["emerging_flags"])
        self.assertEqual(r["emerging_status"], "LOSING_MOMENTUM")

    def test_demand_not_gmv_alone(self):
        obs = strong_rows(units=lambda i: 3000 - 250 * i)                # GMV up, units down -> conflicting
        self.assertEqual(ev(obs)["demand_momentum"], "STABLE")

    def test_growing_creators_and_videos(self):
        r = ev(strong_rows())
        self.assertIn(r["creator_momentum"], ("GROWING", "STRONG_GROWTH"))
        self.assertIn(r["video_momentum"], ("GROWING", "STRONG_GROWTH"))
        self.assertEqual(r["positive_supporting_signals"], 4)

    def test_competition_slower_than_demand(self):
        r = ev(strong_rows())
        self.assertIn(r["competition_status"], ("RISING", "HEALTHY"))
        self.assertLess(r["competition_pressure_ratio"], 0.5)

    def test_competition_outpacing_demand(self):
        obs = strong_rows(shops=lambda i: 10 * (2 ** i))                # sellers doubling every 2 days
        r = ev(obs)
        self.assertEqual(r["competition_status"], "OUTPACING_DEMAND")
        self.assertGreaterEqual(r["competition_pressure_ratio"], 1.0)
        self.assertIn("COMPETITION_OUTPACING_DEMAND", r["emerging_flags"])
        self.assertEqual(r["emerging_status"], "LOSING_MOMENTUM")

    def test_competition_zero_and_negative_demand_safe(self):
        self.assertEqual(E.competition_status(None, 1.0, CFG), ("INSUFFICIENT_DATA", "N/A"))
        self.assertEqual(E.competition_status(2.0, 0.0, CFG), ("OUTPACING_DEMAND", "N/A"))
        self.assertEqual(E.competition_status(0.0, -3.0, CFG), ("HEALTHY", "N/A"))
        self.assertEqual(E.competition_status(1.0, 4.0, CFG), ("RISING", 0.25))
        self.assertEqual(E.competition_status(3.0, 4.0, CFG), ("CATCHING_UP", 0.75))


class Concentration(unittest.TestCase):
    def test_creator_dependency_blocks_strong(self):
        r = ev(strong_rows(tc=75))
        self.assertIn("CREATOR_DEPENDENCY", r["emerging_flags"])
        self.assertNotEqual(r["emerging_status"], "EMERGING_STRONG")
        self.assertEqual(r["emerging_status"], "EMERGING")

    def test_video_dependency_blocks_strong(self):
        r = ev(strong_rows(tv=71))
        self.assertIn("VIDEO_DEPENDENCY", r["emerging_flags"])
        self.assertNotEqual(r["emerging_status"], "EMERGING_STRONG")

    def test_exactly_70_is_not_dependency(self):
        self.assertNotIn("CREATOR_DEPENDENCY", ev(strong_rows(tc=70))["emerging_flags"])

    def test_missing_concentration_lowers_confidence(self):
        full = ev(strong_rows())
        miss = ev(strong_rows(tc=None, tv=None))
        self.assertEqual(miss["momentum_confidence_breakdown"]["concentration_completeness"]["earned"], 0.0)
        self.assertLess(miss["momentum_confidence"], full["momentum_confidence"])
        self.assertNotIn("CREATOR_DEPENDENCY", miss["emerging_flags"])
        self.assertIn("top_creator_revenue_share", miss["missing_data"])
        self.assertEqual(miss["concentration"]["top_creator_revenue_share"], "N/A")


class Scores(unittest.TestCase):
    def test_momentum_score_calculation(self):
        r = ev(strong_rows())
        bd = r["momentum_score_breakdown"]
        self.assertEqual({k: v["max"] for k, v in bd.items()},
                         {"wps_trajectory": 20, "gmv_momentum": 25, "units_momentum": 15, "creator_momentum": 15,
                          "video_momentum": 15, "competition_balance": 10})
        self.assertEqual(r["momentum_score"], round(sum(v["points"] for v in bd.values()), 2))
        # WPS 72 -> 84 over 12 days = 1.0 pt/day >= full_at 0.5 -> s_rate 1 ; acceleration POSITIVE 0.6
        self.assertEqual(bd["wps_trajectory"]["points"], round(20 * (0.7 * 1.0 + 0.3 * 0.6), 2))
        self.assertEqual(bd["competition_balance"]["points"], 8.0)      # RISING

    def test_momentum_confidence_calculation(self):
        r = ev(strong_rows())
        bd = r["momentum_confidence_breakdown"]
        self.assertEqual(bd["observation_count"]["earned"], 15.0)        # 7 obs >= 6
        self.assertEqual(bd["observation_day_span"]["earned"], round(15 * 12 / 14, 2))
        for k in ("gmv_completeness", "units_completeness", "wps_completeness", "creator_completeness",
                  "video_completeness", "competition_completeness", "concentration_completeness"):
            self.assertEqual(bd[k]["earned"], bd[k]["max"], k)
        self.assertEqual(r["momentum_confidence"], round(100 - 15 + 15 * 12 / 14, 2))
        self.assertEqual(r["momentum_confidence_level"], "VERY_HIGH")

    def test_scores_independent_from_protected_scores(self):
        obs = strong_rows()
        before = copy.deepcopy(obs)
        ev(obs)
        self.assertEqual(obs, before)                                    # history never modified


class Statuses(unittest.TestCase):
    def test_emerging_strong(self):
        r = ev(strong_rows())
        self.assertEqual(r["emerging_status"], "EMERGING_STRONG", r["status_reason"])
        self.assertGreaterEqual(r["momentum_score"], 80)
        self.assertIn("RAPID_GMV_ACCELERATION", r["emerging_flags"])

    def test_emerging(self):
        # steady LINEAR growth (clearly positive, not accelerating) -> EMERGING, not EMERGING_STRONG
        obs = [ob(2 * i, wps=72 + i, conf=75, gmv=40000 + 3200 * i, units=1300 + 104 * i, creators=300 + 15 * i,
                  videos=500 + 25 * i, shops=10, tc=20, tv=10) for i in range(7)]
        r = ev(obs)
        self.assertEqual(r["emerging_status"], "EMERGING", (r["momentum_score"], r["status_reason"]))
        self.assertGreaterEqual(r["momentum_score"], 65)
        self.assertLess(r["momentum_score"], 80)

    def test_emerging_review_wps_below_minimum(self):
        r = ev(strong_rows(wps=lambda i: 60 + i))
        self.assertEqual(r["emerging_status"], "EMERGING_REVIEW")
        self.assertIn("WPS/WPS Confidence below minimum", r["status_reason"])

    def test_stable(self):
        obs = [ob(2 * i, wps=75, conf=80, gmv=50000 + (i % 2) * 200, units=1500, creators=300, videos=500, shops=10,
                  tc=20, tv=10) for i in range(6)]
        r = ev(obs)
        self.assertEqual(r["emerging_status"], "STABLE", (r["momentum_score"], r["status_reason"]))
        self.assertEqual(r["demand_momentum"], "STABLE")

    def test_losing_momentum_wps_decline(self):
        r = ev(strong_rows(wps=lambda i: 84 - 2 * i))
        self.assertIn("WPS_DECLINING", r["emerging_flags"])
        self.assertEqual(r["emerging_status"], "LOSING_MOMENTUM")


class DataHandling(unittest.TestCase):
    def test_valid_zero_values(self):
        r = ev(strong_rows(svid=0, shops=0))
        self.assertEqual(r["acceleration_detail"]["selling_videos"]["label"], "FLAT")
        self.assertEqual(r["selling_video_growth_rate"], 0.0)            # real 0 -> 0, not N/A
        self.assertEqual(r["competition_growth_rate"], 0.0)
        self.assertEqual(r["competition_status"], "HEALTHY")
        self.assertNotIn("competition_history", r["missing_data"])

    def test_new_growth_from_zero(self):
        r = ev(strong_rows(svid=lambda i: 0 if i < 3 else 10 * i))
        self.assertEqual(r["selling_video_growth_rate"], "NEW_GROWTH")

    def test_missing_values(self):
        obs = strong_rows()
        for o in obs[1::2]:
            o["tiktok"]["gmv"] = None
        o = obs[-1]
        o["tiktok"]["selling_video_count"] = None
        r = ev(obs)
        self.assertEqual(r["deltas"]["gmv"]["previous"], 62000)          # last two VALID values
        self.assertLess(r["momentum_confidence_breakdown"]["gmv_completeness"]["earned"], 15)
        r2 = ev([ob(d, wps=80, conf=80) for d in range(4)])
        self.assertEqual(r2["gmv_velocity"], "N/A")
        self.assertEqual(r2["demand_momentum"], "INSUFFICIENT_DATA")
        self.assertIn("gmv_history", r2["missing_data"])


class Storage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = H.HistoryStore(Path(self.tmp.name) / "history", HCFG)
        for o in strong_rows():
            self.store.append(o)
        for i, o in enumerate(strong_rows(gmv=lambda i: 90000 - 8000 * i, units=lambda i: 3000 - 250 * i)):
            o["product_id"] = o["identity_key"] = "LOSE"
            self.store.append(o)
        for o in [ob(0, pid="ONE", wps=90, conf=90, gmv=1)]:
            self.store.append(o)
        self.out = Path(self.tmp.name) / "emerging"

    def tearDown(self):
        for p in Path(self.tmp.name).rglob("*.json"):
            os.chmod(p, 0o644)
        self.tmp.cleanup()

    def test_detect_all_and_priority_list(self):
        rep = E.detect_all(self.store, CFG, NOW)
        st = {r["product_id"]: r["emerging_status"] for r in rep["results"]}
        self.assertEqual(st, {"S": "EMERGING_STRONG", "LOSE": "LOSING_MOMENTUM", "ONE": "INSUFFICIENT_HISTORY"})
        self.assertEqual([p["product_id"] for p in rep["priority_list"]], ["S"])

    def test_priority_ordering(self):
        rs = [{"product_id": "a", "product_name": "a", "emerging_status": "EMERGING", "momentum_score": 90,
               "momentum_confidence": 80, "current_wps": 80, "current_wps_confidence": 80},
              {"product_id": "b", "product_name": "b", "emerging_status": "EMERGING_STRONG", "momentum_score": 81,
               "momentum_confidence": 76, "current_wps": 71, "current_wps_confidence": 61},
              {"product_id": "c", "product_name": "c", "emerging_status": "EMERGING", "momentum_score": 90,
               "momentum_confidence": 85, "current_wps": 70, "current_wps_confidence": 60},
              {"product_id": "d", "product_name": "d", "emerging_status": "EMERGING_REVIEW", "momentum_score": 99,
               "momentum_confidence": 99, "current_wps": 99, "current_wps_confidence": 99},
              {"product_id": "e", "product_name": "e", "emerging_status": "STABLE", "momentum_score": 99,
               "momentum_confidence": 99, "current_wps": 99, "current_wps_confidence": 99}]
        self.assertEqual([p["product_id"] for p in E.priority_list(rs)], ["b", "c", "a", "d"])

    def test_historical_output_preservation(self):
        rep = E.detect_all(self.store, CFG, NOW)
        p1, latest = E.save(rep, self.out, CFG)
        h1 = hashlib.sha256(p1.read_bytes()).hexdigest()
        rep2 = {**rep, "generated_at": "later"}
        p2, latest2 = E.save(rep2, self.out, CFG)
        self.assertNotEqual(p1, p2)
        self.assertEqual(hashlib.sha256(p1.read_bytes()).hexdigest(), h1)      # immutable
        self.assertFalse(os.stat(p1).st_mode & 0o222)
        self.assertEqual(json.loads(latest2.read_text())["generated_at"], "later")   # latest replaced
        self.assertEqual(latest, latest2)

    def test_output_schema(self):
        r = E.detect_all(self.store, CFG, NOW)["results"]
        s = next(x for x in r if x["product_id"] == "S")
        for k in ("product_id", "product_name", "observation_count", "observation_days", "history_start", "history_end",
                  "current_wps", "previous_wps", "wps_change", "wps_velocity", "wps_acceleration", "current_gmv",
                  "gmv_change", "gmv_percent_change", "gmv_velocity", "gmv_acceleration", "current_units",
                  "units_change", "units_percent_change", "units_velocity", "units_acceleration",
                  "creator_growth_rate", "creator_velocity", "creator_acceleration", "selling_creator_growth_rate",
                  "selling_creator_velocity", "selling_creator_acceleration", "video_growth_rate", "video_velocity",
                  "video_acceleration", "selling_video_growth_rate", "selling_video_velocity",
                  "selling_video_acceleration", "competition_growth_rate", "competition_velocity",
                  "competition_pressure_ratio", "demand_momentum", "creator_momentum", "video_momentum",
                  "competition_status", "momentum_score", "momentum_score_breakdown", "momentum_confidence",
                  "momentum_confidence_breakdown", "emerging_status", "emerging_flags", "missing_data",
                  "observation_timestamp"):
            self.assertIn(k, s, k)


if __name__ == "__main__":
    unittest.main()
