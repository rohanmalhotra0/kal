"""Maker backtest (kal/research/maker.py): one-contract bids, filled only when trades reach them."""
import importlib.util
import io
import json
import os
import random
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

from kal.client import fee_per_contract
from kal.research import backtest, maker
from tests.fakes import FakeKalshi
from tests.helpers import TempHome

H, DAY = 3600, 86400
T0 = 1_760_000_000
M0 = T0 - T0 % H   # on the hour, like Kalshi's candle ends


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def candle(end, lo=None, hi=None, volume=10, bid=None, ask=None):
    c = {"end_period_ts": end, "volume_fp": f"{volume:.2f}",
         "price": {"low_dollars": f"{lo:.4f}" if lo else None, "high_dollars": f"{hi:.4f}" if hi else None}}
    if bid is not None:
        c["yes_bid"], c["yes_ask"] = {"close": f"{bid:.4f}"}, {"close": f"{ask:.4f}"}
    return c


def row(lo=None, hi=None, ask=0.92, bid=0.90):
    return {"lo": lo, "hi": hi, "yes_ask": ask, "yes_bid": bid}


class FillRuleTests(unittest.TestCase):
    def test_a_bid_fills_only_when_a_trade_goes_through_it(self):
        # YES bid at 90c: a trade AT 90c may have filled the queue ahead of us, not us.
        self.assertFalse(maker.filled(row(lo=0.90), "yes", 0.90))
        self.assertTrue(maker.filled(row(lo=0.89), "yes", 0.90))
        self.assertTrue(maker.filled(row(lo=0.90), "yes", 0.90, at_price=True))
        self.assertFalse(maker.filled(row(lo=None), "yes", 0.90, at_price=True))  # nothing traded
        # NO bid at 10c is a YES ask at 90c: it fills when YES traded above 90c.
        self.assertFalse(maker.filled(row(hi=0.90), "no", 0.10))
        self.assertTrue(maker.filled(row(hi=0.91), "no", 0.10))
        self.assertTrue(maker.filled(row(hi=0.90), "no", 0.10, at_price=True))

    def test_an_improved_bid_fills_on_a_trade_at_its_price(self):
        # 1c above the best bid, it was alone at its price, so a trade there was with it.
        o = {**row(lo=0.91, hi=0.91, ask=0.92, bid=0.90), "result": "yes", "yes_value": 1.0,
             "series": "S", "anchor": M0, "close": M0}
        self.assertEqual([r["fills"] for r in maker.table([o], "improve") if r["side"] == "yes"], [1])
        self.assertEqual([r["fills"] for r in maker.table([o], "join") if r["side"] == "yes"], [0])

    def test_bids_join_or_improve_but_never_cross(self):
        self.assertEqual(maker.quotes(row(ask=0.92, bid=0.90)), [("yes", 0.90), ("no", 0.08)])
        self.assertEqual(maker.quotes(row(ask=0.92, bid=0.90), "improve"), [("yes", 0.91), ("no", 0.09)])
        self.assertEqual(maker.quotes(row(ask=0.91, bid=0.90), "improve"), [])   # 1c spread: would cross
        self.assertEqual(maker.quotes(row(ask=0.05, bid=0.0)), [("no", 0.95)])    # no YES bid to join
        self.assertEqual(maker.quotes(row(ask=1.0, bid=0.99)), [("yes", 0.99)])   # no ask: no NO bid
        for ask, bid in ((0.92, 0.90), (0.5, 0.4), (0.03, 0.01)):
            for side, p in maker.quotes(row(ask=ask, bid=bid), "improve"):
                if side == "yes":
                    self.assertLess(p, ask)          # our YES bid stays under the YES ask
                else:
                    self.assertGreater(1 - p, bid)   # our NO bid is a YES ask over the YES bid

    def test_maker_fee_is_a_quarter_of_the_taker_fee(self):
        self.assertAlmostEqual(maker.maker_fee(0.5), 0.004375)
        for p in (0.03, 0.5, 0.9, 0.99):
            self.assertAlmostEqual(maker.maker_fee(p), fee_per_contract(p) / 4)


class WindowTests(unittest.TestCase):
    def test_only_trades_while_the_bid_rested_count(self):
        decided, end = M0 + 1800, M0 + 6 * H + 1800   # mid-hour decision and scheduled end
        o = {"series": "S", "ticker": "S-1", "decided": decided, "close": end + DAY, "anchor": end}
        c = FakeKalshi(can_trade=False)
        c.candle_data["S-1"] = [
            candle(M0, lo=0.10, hi=0.99),                 # ended before the decision: not ours
            candle(M0 + H, lo=0.85, hi=0.91),             # the decision's hour: bid set before it began
            candle(M0 + 2 * H, lo=0.50, hi=0.95, volume=0),   # no trades: a carried-over range
            candle(M0 + 3 * H, lo=0.86, hi=0.93),
            candle(M0 + 6 * H, lo=0.84, hi=0.94),         # the last hour before the end's hour
            candle(M0 + 7 * H, lo=0.02, hi=0.98),         # the scheduled end's hour: cancelled by then
        ]
        self.assertEqual(maker.window_end(o), M0 + 6 * H)
        self.assertEqual(maker.window(c, o), (0.84, 0.94))

    def test_an_early_close_counts_the_news_hour(self):
        o = {"series": "S", "ticker": "S-1", "decided": M0 + 1800, "close": M0 + H + 1200, "anchor": M0 + 6 * H}
        c = FakeKalshi(can_trade=False)
        c.candle_data["S-1"] = [candle(M0 + H, lo=0.85, hi=0.91), candle(M0 + 2 * H, lo=0.01, hi=0.91),
                                candle(M0 + 4 * H, lo=0.30, hi=0.97)]   # after the close; ignored
        self.assertEqual(maker.window(c, o), (0.01, 0.91))   # the run to 1c is how bids get picked off

    def test_nothing_traded_and_nothing_sent(self):
        o = {"series": "S", "ticker": "S-1", "decided": M0, "close": M0 + 6 * H, "anchor": M0 + 6 * H}
        c = FakeKalshi(can_trade=False)
        c.candle_data["S-1"] = [candle(M0 + H), candle(M0 + 2 * H, lo=0.5, hi=0.6, volume=0)]
        self.assertEqual(maker.window(c, o), (None, None))
        c.candle_data["S-1"] = []
        self.assertIsNone(maker.window(c, o))   # unknown, not "nothing traded"

    def test_historical_candles_and_either_volume_field(self):
        # Historical candles send plain "low"/"high" dollar strings and "volume".
        o = {"series": "S", "ticker": "S-1", "decided": M0, "close": M0 + 6 * H, "anchor": M0 + 6 * H}
        c = FakeKalshi(can_trade=False)
        c.candle_data["S-1"] = [{"end_period_ts": M0 + H, "volume": "12.00", "volume_fp": None,
                                 "price": {"low": "0.0800", "high": "0.9600"}}]
        self.assertEqual(maker.window(c, o), (0.08, 0.96))


def settled(c, n, series="S", result=lambda i: "yes", lo=lambda i: 0.85, hi=lambda i: 0.95, bid=0.90, ask=0.92):
    for i in range(n):
        close = M0 + (i + 10) * DAY
        tk = f"{series}-{i}"
        c.settled.setdefault(series, []).append({
            "ticker": tk, "event_ticker": tk, "open_time": iso(close - 3 * DAY), "close_time": iso(close),
            "can_close_early": False, "status": "finalized", "result": result(i),
            "settlement_value_dollars": "1.0000" if result(i) == "yes" else "0.0000", "volume_fp": "500.00"})
        c.candle_data[tk] = [candle(close - 7 * H, bid=bid, ask=ask),        # the price at decision time
                             candle(close - 3 * H, lo=lo(i), hi=hi(i))]      # trades while the bid rested


class LoadTests(TempHome):
    def counting(self, c):
        calls, real = [], c.candles
        c.candles = lambda *a, **k: calls.append(a) or real(*a, **k)
        return calls

    def test_load_adds_the_range_and_caches_it(self):
        c = FakeKalshi(can_trade=False)
        settled(c, 3)
        calls = self.counting(c)
        got = maker.load(c, "S", 6)
        self.assertEqual([(o["lo"], o["hi"]) for o in got], [(0.85, 0.95)] * 3)
        self.assertEqual(len(calls), 6)          # a price snapshot and a window per market
        self.assertEqual(maker.load(c, "S", 6), got)
        self.assertEqual(len(calls), 6)          # both cached: settled markets don't change

    def test_a_cached_range_is_reused_only_for_the_same_window(self):
        c = FakeKalshi(can_trade=False)
        settled(c, 2)
        maker.load(c, "S", 6)
        path = maker.cache_path("S", 6)
        with open(path) as f:
            cached = json.load(f)
        cached["markets"]["S-0"]["decided"] -= H   # made for an earlier decision time
        cached["markets"]["S-0"]["lo"] = 0.70      # e.g. a trade from before the bid existed
        with open(path, "w") as f:
            json.dump(cached, f)
        calls = self.counting(c)
        got = {o["ticker"]: o["lo"] for o in maker.load(c, "S", 6)}
        self.assertEqual(got, {"S-0": 0.85, "S-1": 0.85})
        self.assertEqual(len(calls), 1)          # only the stale one was fetched again

    def test_failures_and_empty_replies_skip_the_market_not_the_series(self):
        c = FakeKalshi(can_trade=False)
        settled(c, 3)
        real = c.candles

        def flaky(series, ticker, start, end, period=60):
            if start > M0 + 11 * DAY - 6 * H and ticker == "S-1":
                raise RuntimeError("503")
            if start > M0 + 12 * DAY - 6 * H and ticker == "S-2":
                return []
            return real(series, ticker, start, end, period)
        c.candles = flaky
        self.assertEqual([o["ticker"] for o in maker.load(c, "S", 6)], ["S-0"])
        c.candles = real
        self.assertEqual(len(maker.load(c, "S", 6)), 3)   # fetched next time, not written off

    def test_newest_markets_only_and_a_deadline(self):
        c = FakeKalshi(can_trade=False)
        settled(c, 5)
        self.assertEqual([o["ticker"] for o in maker.load(c, "S", 6, max_markets=2)], ["S-3", "S-4"])
        c2 = FakeKalshi(can_trade=False)
        settled(c2, 5, "T")
        self.assertEqual(maker.load(c2, "T", 6, deadline=time.time() - 1), [])   # out of time: fetch nothing


class HonestyTests(unittest.TestCase):
    def obs(self, n, p_true, fills, seed=1, bid=0.90, ask=0.92):
        """n markets, one per day. fills(i, won) says whether trades went through the bid."""
        rng = random.Random(seed)
        out = []
        for i in range(n):
            won = rng.random() < p_true
            through = fills(i, won)
            out.append({"series": "S", "event": f"S-{i}", "ticker": f"S-{i}", "close": M0 + i * DAY,
                        "anchor": M0 + i * DAY, "decided": M0 + i * DAY - 6 * H, "yes_ask": ask, "yes_bid": bid,
                        "result": "yes" if won else "no", "yes_value": 1.0 if won else 0.0,
                        "lo": bid - 0.02 if through else bid, "hi": None})   # NO bids never fill here
        return out

    def yes_row(self, rows, lo=0.90):
        return next(r for r in rows if r["side"] == "yes" and r["lo"] == lo)

    def test_bids_that_get_picked_off_lose_even_when_the_price_was_fair(self):
        # Fair at 90c, but the price only runs through our bid when YES is losing.
        r = self.yes_row(maker.table(self.obs(300, 0.90, lambda i, won: not won), z=3.0))
        self.assertLess(r["roi"], -0.99)
        self.assertFalse(maker.is_lead(r))

    def test_fair_prices_and_random_fills_show_no_lead(self):
        z = backtest.z_for(2 * (len(backtest.BUCKETS) - 1) * len(maker.RULES))
        for seed in range(20):
            obs = self.obs(400, 0.90, lambda i, won, r=random.Random(seed + 1000): r.random() < 0.5, seed=seed)
            for rule in maker.RULES:
                for r in maker.table(obs, rule, z=z):
                    self.assertFalse(maker.is_lead(r), (seed, rule, r))

    def test_a_real_edge_is_found(self):
        # YES really wins 97% at a 90c bid, fills independent of the outcome: makers keep it.
        obs = self.obs(400, 0.97, lambda i, won, r=random.Random(7): r.random() < 0.6)
        self.assertTrue(maker.is_lead(self.yes_row(maker.table(obs, z=3.0))))

    def test_related_series_on_one_day_count_once(self):
        # 8 series ride one price path each day. Counted per series, luck looks like proof.
        rng, obs = random.Random(3), []
        for day in range(60):
            won, through = rng.random() < 0.9, rng.random() < 0.5
            for s in range(8):
                obs.append({"series": f"S{s}", "event": f"S{s}-{day}", "ticker": f"S{s}-{day}",
                            "close": M0 + day * DAY, "anchor": M0 + day * DAY, "yes_ask": 0.92, "yes_bid": 0.90,
                            "result": "yes" if won else "no", "yes_value": float(won), "unit": f"Crypto:{day}",
                            "lo": 0.88 if through else 0.90, "hi": None})
        r = self.yes_row(maker.table(obs))
        self.assertEqual(r["fills"], 8 * r["units"])   # 8 fills a day, one unit a day


class ToolTests(TempHome):
    def test_the_tool_runs_end_to_end_and_saves_its_tables(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "maker_research.py")
        spec = importlib.util.spec_from_file_location("maker_research", path)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        c = FakeKalshi(can_trade=False)
        c.series_list = [{"ticker": "S", "category": "Sports", "frequency": "daily"},
                         {"ticker": "T", "category": "Weather", "frequency": "daily"}]
        settled(c, 40, "S", result=lambda i: "yes" if i % 10 else "no", lo=lambda i: 0.85 if i % 2 else 0.90)
        settled(c, 35, "T", result=lambda i: "no", hi=lambda i: 0.95 if i % 3 else 0.91)
        out = io.StringIO()
        with mock.patch.object(tool, "Kalshi", return_value=c), \
                mock.patch.object(tool, "active_series", return_value=["S", "T"]), \
                mock.patch("sys.argv", ["maker_research.py", "--markets-per-series", "50", "--hours", "6", "24"]), \
                redirect_stdout(out):
            tool.main()
        text = out.getvalue()
        self.assertIn("6 hours before the scheduled end: 75 priced markets, 75 category-days", text)
        self.assertIn("6h Sports: improve the best bid", text)
        self.assertIn("for 144 tries", text)   # 2 sides x 12 ranges x 2 rules x 3 tables (24h priced none)
        self.assertNotIn("EDGE", text)          # a best case is never called an edge
        with open(os.path.join(self.home, "research", "maker_6h.json")) as f:
            self.assertEqual(json.load(f)["tables"]["ALL"]["markets"], 75)


if __name__ == "__main__":
    unittest.main()
