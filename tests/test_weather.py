"""Weather research (kal/research/weather.py): NBM forecasts against daily high-temperature prices."""
import importlib.util
import io
import json
import os
import random
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from kal.research import backtest, weather
from tests.fakes import FakeKalshi
from tests.helpers import TempHome

H, DAY = 3600, 86400


def ts(d, hour):
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc).timestamp())


def nbs_csv(day, txn, xnd, runtime="2026-10-20 09:00:00"):
    nxt = (day + timedelta(days=1)).isoformat()
    return ("runtime,ftime,model,tmp,txn,xnd\n"
            f"{runtime},{day.isoformat()} 12:00:00,NBS,50,48.0,2.0\n"
            f"{runtime},{nxt} 00:00:00,NBS,60,{txn},{xnd}\n"
            f"{runtime},{nxt} 12:00:00,NBS,52,45.0,2.0\n")


LADDER = [("less", None, 57), ("between", 57, 58), ("between", 59, 60), ("between", 61, 62),
          ("between", 63, 64), ("greater", 64, None)]


class PricingTests(unittest.TestCase):
    def test_brackets_pay_on_the_whole_degrees_kalshi_says(self):
        self.assertEqual(weather.bracket({"strike_type": "greater", "floor_strike": 64}), (65, None))
        self.assertEqual(weather.bracket({"strike_type": "less", "cap_strike": 57}), (None, 56))
        self.assertEqual(weather.bracket({"strike_type": "between", "floor_strike": 63, "cap_strike": 64}), (63, 64))
        self.assertIsNone(weather.bracket({"strike_type": "between", "floor_strike": 63}))
        self.assertIsNone(weather.bracket({"strike_type": "custom", "floor_strike": "x"}))

    def test_a_full_ladder_adds_to_one(self):
        rngs = [weather.bracket({"strike_type": t, "floor_strike": f, "cap_strike": c}) for t, f, c in LADDER]
        for mu, s in ((60.0, 2.0), (55.3, 3.1), (70.0, 1.0)):
            self.assertAlmostEqual(sum(weather.prob(r, mu, s) for r in rngs), 1.0, places=9)

    def test_forecast_for_the_day_and_run_timing(self):
        day = date(2026, 10, 20)
        run = ts(day, 9)
        self.assertEqual(weather.parse_max(nbs_csv(day, "66.0", "1.0"), day, run), (66.0, 1.0, run))
        self.assertIsNone(weather.parse_max(nbs_csv(day, "", "1.0"), day, run))                 # no high
        self.assertEqual(weather.parse_max(nbs_csv(day, "66.0", ""), day, run), (66.0, None, run))   # spread optional
        self.assertIsNone(weather.parse_max("runtime,ftime\n", day, run))
        decided = ts(day, 11) + 59 * 60
        self.assertEqual(weather.run_for(decided), ts(day, 9))   # published by then, with room to spare
        self.assertEqual(weather.day_of(ts(day + timedelta(days=1), 5)), day)   # New York: ends 05 UTC next day
        self.assertEqual(weather.day_of(ts(day + timedelta(days=1), 8)), day)   # Los Angeles: 08 UTC

    def test_a_later_run_is_never_used(self):
        # A reply holding a run that started after the decision must not leak into it.
        day = date(2026, 10, 20)
        text = nbs_csv(day, "66.0", "1.0", runtime="2026-10-20 07:00:00") + \
            nbs_csv(day, "71.0", "1.0", runtime="2026-10-20 21:00:00").split("\n", 1)[1]
        self.assertEqual(weather.parse_max(text, day, ts(day, 9))[0], 66.0)
        self.assertIsNone(weather.parse_max(text, day, ts(day, 6)))   # nothing that early

    def test_forecast_runs_are_cached_and_failures_retried(self):
        day, calls = date(2026, 10, 20), []

        def fetch(url, params):
            calls.append((params["sts"], params["ets"]))
            if len(calls) == 1:
                raise OSError("timeout")
            if len(calls) == 2:
                return "runtime,ftime,txn,xnd\n"   # an empty reply
            return nbs_csv(day, "66.0", "1.0", runtime="2026-10-20 07:00:00")   # 09Z isn't archived: 07Z is
        path = os.path.join(self._tmp(), "nbm.json")
        want = [(ts(day, 9), day)]
        got = {want[0]: (66.0, 1.0, ts(day, 7))}
        self.assertEqual(weather.forecasts(fetch, "KNYC", want, path), {})   # failed: not cached
        self.assertEqual(weather.forecasts(fetch, "KNYC", want, path), {})   # empty: not cached
        self.assertEqual(weather.forecasts(fetch, "KNYC", want, path), got)  # the latest older run
        self.assertEqual(weather.forecasts(fetch, "KNYC", want, path), got)
        self.assertEqual(len(calls), 3)                                      # the fourth came from the cache
        self.assertEqual(calls[-1], ("2026-10-20T03:00Z", "2026-10-20T09:00Z"))

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d

    def test_fit_finds_the_bias_and_spread(self):
        rng, days = random.Random(2), []
        for _ in range(150):
            mu = rng.uniform(40, 90)
            high = round(mu + 2.0 + rng.gauss(0, 2.5))
            lo = high - high % 2
            days.append((mu, (lo, lo + 1)))
        b, s, w, k = weather.fit(days)
        self.assertAlmostEqual(b, 2.0, delta=0.6)
        self.assertAlmostEqual(s, 2.6, delta=0.6)
        self.assertLessEqual(w, 0.1)                   # a Normal error: thin tails
        far = [(mu + 5.0, rng) for mu, rng in days]   # a forecast 7°F too warm still fits
        self.assertAlmostEqual(weather.fit(far)[0], -3.0, delta=0.6)
        wide = [(mu, (lo - 6, lo + 1)) for mu, (lo, _) in days[:40]] + days[40:]
        self.assertLessEqual(weather.fit(wide)[1], 10.0)

    def test_fat_tails_are_found_and_priced(self):
        # 85% of days the error is 2°F wide; on 15% it's 8°F (fronts). A plain Normal would
        # price the far brackets near zero.
        rng, days = random.Random(9), []
        for i in range(300):
            mu = rng.uniform(40, 90)
            high = round(mu + rng.gauss(0, 8.0 if rng.random() < 0.15 else 2.0))
            lo = high - high % 2
            days.append((mu, (lo, lo + 1)))
        b, s, w, k = weather.fit(days)
        self.assertGreaterEqual(w, 0.05)
        self.assertGreater(weather.prob((70, None), 60, s, w, k), weather.prob((70, None), 60, s))

    def test_brackets_must_tile_every_degree_once(self):
        def event(e, rungs, winner):
            return [{"event_ticker": e, "strike_type": t, "floor_strike": f, "cap_strike": c,
                     "result": "yes" if i == winner else "no"} for i, (t, f, c) in enumerate(rungs)]
        good = event("A", LADDER, 2)
        gap = event("B", [("less", None, 57), ("between", 58, 59)] + LADDER[2:], 1)   # 57 is in no bracket
        two = event("C", LADDER, 2)
        two[3]["result"] = "yes"
        self.assertEqual(weather.tiling(good), (1, 1))
        self.assertEqual(weather.tiling(good + gap + two), (3, 1))

    def test_trades_are_chosen_on_the_cost_they_are_scored_at(self):
        from kal.strategies.favorites import cost_each
        ask = 0.40
        row = {"anchor": 1_760_000_000, "yes_ask": ask, "yes_bid": 0.39, "result": "yes", "yes_value": 1.0}
        just = cost_each(ask) + 0.05
        self.assertEqual(len(weather.trades([dict(row, p=just + 0.001)], 0.05)["yes"]), 1)
        self.assertEqual(len(weather.trades([dict(row, p=just - 0.001)], 0.05)["yes"]), 0)


def city_days(n, bias_of=lambda i: 0.0, start=date(2026, 3, 1), seed=5, price_of=None):
    """n days of one city: settled markets, priced observations and forecasts."""
    rng = random.Random(seed)
    markets, obs, fc = [], [], {}
    for i in range(n):
        day = start + timedelta(days=i)
        anchor = ts(day + timedelta(days=1), 5)
        mu = 60.0 + rng.gauss(0, 5)
        high = round(mu + bias_of(i) + rng.gauss(0, 2.0))
        ev = f"KXHIGHNY-{day.isoformat()}"
        fc[ev] = (mu, 1.0)
        base = round(mu) - round(mu) % 2 - 3   # a ladder around the forecast
        rungs = [("less", None, base)] + [("between", base + 2 * k, base + 2 * k + 1) for k in range(4)] + \
                [("greater", base + 7, None)]
        for k, (t, f, c) in enumerate(rungs):
            m = {"ticker": f"{ev}-{k}", "event_ticker": ev, "strike_type": t, "floor_strike": f, "cap_strike": c}
            r = weather.bracket(m)
            won = (r[0] is None or high >= r[0]) and (r[1] is None or high <= r[1])
            m["result"] = "yes" if won else "no"
            markets.append(m)
            p = price_of(r, mu, high) if price_of else 0.30
            obs.append({"series": "KXHIGHNY", "event": ev, "ticker": m["ticker"], "anchor": anchor,
                        "close": anchor, "decided": anchor - 18 * H, "yes_ask": min(0.99, round(p + 0.01, 2)),
                        "yes_bid": max(0.0, round(p - 0.01, 2)), "result": m["result"],
                        "yes_value": 1.0 if won else 0.0})
    return markets, obs, fc


class NoPeekingTests(unittest.TestCase):
    def test_a_day_is_priced_only_from_days_settled_before_its_week(self):
        # The forecast runs 4°F cold from day 60 on. Days before that can't know.
        markets, obs, fc = city_days(200, bias_of=lambda i: 4.0 if i >= 60 else 0.0)
        rows = weather.priced(obs, markets, fc)
        bias = {weather.day_of(o["anchor"]): o["bias"] for o in rows}
        start = date(2026, 3, 1)
        for d, b in bias.items():
            if (d - start).days < 60:
                self.assertLess(abs(b), 1.5, d)          # never fitted on the cold days ahead
            elif (d - start).days >= 190:
                self.assertLess(abs(b - 4.0), 1.0, d)    # 120 days back are all cold by then
        first = min(bias)
        self.assertGreaterEqual((first - start).days, weather.MIN_FIT)   # no fit before 20 days of history

    def test_a_forecast_with_no_information_shows_no_edge(self):
        # The market prices each rung at the truth; the forecast is that plus noise.
        def truth(r, mu, high):
            return min(0.97, max(0.03, weather.prob(r, mu, 2.0)))
        z = backtest.z_for(8)
        for seed in range(6):
            markets, obs, fc = city_days(200, seed=seed, price_of=truth)
            noise = random.Random(seed + 99)   # fresh noise for every event, not one shared offset
            noisy = {e: (mu + noise.gauss(0, 3), s) for e, (mu, s) in fc.items()}
            rows = weather.priced(obs, markets, noisy)
            for margin in (0.05, 0.10):
                for side, t in weather.trades(rows, margin).items():
                    roi, lo = backtest.roi_stats(t, z) if len(t) > 1 else (None, None)
                    self.assertFalse(lo is not None and lo > 0 and len(t) >= 30, (seed, margin, side, roi, lo))

    def test_a_forecast_that_knows_more_than_the_market_is_found(self):
        # The market prices every rung at 30c; the forecast knows the high within ~2°F, so
        # selling the unlikely rungs (buying NO at 71c) pays.
        markets, obs, fc = city_days(200)
        rows = weather.priced(obs, markets, fc)
        t = weather.trades(rows, 0.10)["no"]
        roi, lo = backtest.roi_stats(t, backtest.z_for(8))
        self.assertGreater(lo, 0)

    def test_calibration_and_far_misses(self):
        markets, obs, fc = city_days(120)
        rows = weather.priced(obs, markets, fc)
        cal = weather.calibration(rows)
        self.assertEqual(sum(r["markets"] for r in cal), len(rows))
        days, share = weather.far_misses(rows)
        self.assertGreater(days, 50)
        self.assertLess(share, 0.10)          # this fake's errors are Normal

    def test_one_request_covers_a_week_of_runs(self):
        calls, start = [], date(2026, 10, 1)

        def fetch(url, params):
            calls.append((params["sts"], params["ets"]))
            rows = [nbs_csv(d, "60.0", "2.0", runtime=f"{d.isoformat()} 09:00:00").split("\n", 1)[1]
                    for d in (start + timedelta(days=i) for i in range(10))]
            return "runtime,ftime,model,tmp,txn,xnd\n" + "".join(rows)
        wants = [(ts(start + timedelta(days=i), 9), start + timedelta(days=i)) for i in range(10)]
        got = weather.forecasts(fetch, "KNYC", wants)
        self.assertEqual(len(calls), 2)       # 10 days: two weekly requests
        self.assertEqual(sorted(got), sorted(wants))
        self.assertTrue(all(v and v[0] == 60.0 for v in got.values()))

    def test_scores_compare_forecast_and_market(self):
        markets, obs, fc = city_days(60)
        s = weather.scores(weather.priced(obs, markets, fc))
        self.assertLess(s["brier_forecast"], s["brier_market"])   # 30c everywhere is a poor guess
        self.assertIsNone(weather.scores([]))


class ToolTests(TempHome):
    def test_the_tool_runs_end_to_end(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "weather_research.py")
        spec = importlib.util.spec_from_file_location("weather_research", path)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        markets, obs, fc = city_days(60)
        c = FakeKalshi(can_trade=False)
        c.settled_markets = lambda series, limit=None: markets if series == "KXHIGHNY" else []
        by_day = {weather.day_of(o["anchor"]): fc[o["event"]] for o in obs}

        def fetch(url, params):
            run = datetime.strptime(params["ets"], "%Y-%m-%dT%H:%MZ")
            mu, s = by_day.get(run.date(), (None, None))
            return nbs_csv(run.date(), mu if mu is not None else "", s if s is not None else "",
                           runtime=run.strftime("%Y-%m-%d %H:%M:00"))
        out = io.StringIO()
        with mock.patch.object(tool, "Kalshi", return_value=c), mock.patch.object(tool, "fetcher", return_value=fetch), \
                mock.patch.object(tool.backtest, "load_observations",
                                  side_effect=lambda cl, s, h, max_markets=0: obs if s == "KXHIGHNY" else []), \
                mock.patch("sys.argv", ["weather_research.py", "--hours", "18"]), redirect_stdout(out):
            tool.main()
        text = out.getvalue()
        self.assertIn("18h KXHIGHNY", text)
        self.assertIn("18 hours before the scheduled end", text)
        with open(os.path.join(self.home, "research", "weather.json")) as f:
            self.assertEqual(json.load(f)["tries"], 4)


if __name__ == "__main__":
    unittest.main()
