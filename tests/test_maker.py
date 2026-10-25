"""Maker backtest (kal/research/maker.py): resting bids, filled only when trades go through them."""
import importlib.util
import io
import os
import random
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

from kal.client import fee
from kal.research import backtest, maker
from tests.fakes import FakeKalshi
from tests.helpers import TempHome

H, DAY = 3600, 86400
T0 = 1_760_000_000
M0 = T0 - T0 % H   # on the hour, like Kalshi's candle ends


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def candle(end, lo=None, hi=None, volume=10, bid=None, ask=None):
    c = {"end_period_ts": end, "volume": volume,
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
        self.assertTrue(maker.filled(row(lo=0.90), "yes", 0.90, touch=True))
        self.assertFalse(maker.filled(row(lo=None), "yes", 0.90, touch=True))  # nothing traded
        # NO bid at 10c is a YES ask at 90c: it fills when YES traded above 90c.
        self.assertFalse(maker.filled(row(hi=0.90), "no", 0.10))
        self.assertTrue(maker.filled(row(hi=0.91), "no", 0.10))
        self.assertTrue(maker.filled(row(hi=0.90), "no", 0.10, touch=True))

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

    def test_maker_fee_rounds_up_and_is_a_quarter_of_the_taker_rate(self):
        self.assertEqual(maker.maker_fee(1, 0.5), 0.01)
        self.assertEqual(maker.maker_fee(100, 0.5), 0.44)
        self.assertEqual(maker.maker_fee(10, 0.99), 0.01)
        for n, p in ((1, 0.5), (10, 0.9), (100, 0.3), (7, 0.07)):
            self.assertLessEqual(maker.maker_fee(n, p), fee(n, p))


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
            candle(M0 + 7 * H, lo=0.84, hi=0.94),         # the end's hour: counts (late fills are mostly bad)
            candle(M0 + 8 * H, lo=0.02, hi=0.98),         # after the bid stopped resting
        ]
        self.assertEqual(maker.window(c, o), (0.84, 0.94))

    def test_window_ends_at_an_early_close(self):
        decided = M0 + 1800
        o = {"series": "S", "ticker": "S-1", "decided": decided, "close": M0 + 2 * H, "anchor": M0 + 6 * H}
        c = FakeKalshi(can_trade=False)
        c.candle_data["S-1"] = [candle(M0 + H, lo=0.85, hi=0.91), candle(M0 + 2 * H, lo=0.01, hi=0.91),
                                candle(M0 + 4 * H, lo=0.30, hi=0.97)]   # can't exist after a close; ignored
        self.assertEqual(maker.window(c, o), (0.01, 0.91))   # the news run counts: it's how bids get picked off

    def test_nothing_traded(self):
        o = {"series": "S", "ticker": "S-1", "decided": M0, "close": M0 + 6 * H, "anchor": M0 + 6 * H}
        c = FakeKalshi(can_trade=False)
        c.candle_data["S-1"] = [candle(M0 + H), candle(M0 + 2 * H, lo=0.5, hi=0.6, volume=0)]
        self.assertEqual(maker.window(c, o), (None, None))


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
    def test_load_adds_the_range_and_caches_it(self):
        c = FakeKalshi(can_trade=False)
        settled(c, 3)
        calls = []
        real = c.candles
        c.candles = lambda *a, **k: calls.append(a) or real(*a, **k)
        got = maker.load(c, "S", 6)
        self.assertEqual([(o["lo"], o["hi"]) for o in got], [(0.85, 0.95)] * 3)
        self.assertEqual(len(calls), 6)          # a price snapshot and a window per market
        again = maker.load(c, "S", 6)
        self.assertEqual(len(calls), 6)          # both cached: settled markets don't change
        self.assertEqual(again, got)
        self.assertTrue(os.path.exists(maker.cache_path("S", 6)))

    def test_a_failing_window_skips_the_market_not_the_series(self):
        c = FakeKalshi(can_trade=False)
        settled(c, 3)
        real = c.candles

        def flaky(series, ticker, start, end, period=60):
            if ticker == "S-1" and start > M0 + 11 * DAY - 6 * H:
                raise RuntimeError("503")
            return real(series, ticker, start, end, period)
        c.candles = flaky
        self.assertEqual([o["ticker"] for o in maker.load(c, "S", 6)], ["S-0", "S-2"])
        c.candles = real
        self.assertEqual(len(maker.load(c, "S", 6)), 3)   # fetched next time, not written off


class HonestyTests(unittest.TestCase):
    def obs(self, n, p_true, fills, seed=1, bid=0.90, ask=0.92):
        """n fair-ish markets, one per day. fills(i, won) says whether trades went through the bid."""
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
        obs = self.obs(300, 0.90, lambda i, won: not won)
        r = self.yes_row(maker.table(obs, z=3.0))
        self.assertLess(r["roi"], -0.99)
        self.assertFalse(maker.is_edge(r))
        self.assertGreater(r["roi_if_all_filled"], -0.05)   # every bid filling would have been about fair

    def test_fair_prices_and_random_fills_show_no_edge(self):
        z = backtest.z_for(2 * (len(backtest.BUCKETS) - 1) * len(maker.RULES))
        for seed in range(20):
            obs = self.obs(400, 0.90, lambda i, won, r=random.Random(seed + 1000): r.random() < 0.5, seed=seed)
            for rule in maker.RULES:
                for r in maker.table(obs, rule, z=z):
                    self.assertFalse(maker.is_edge(r), (seed, rule, r))

    def test_a_real_edge_is_found(self):
        # YES really wins 97% at a 90c bid, fills independent of the outcome: makers keep it.
        obs = self.obs(400, 0.97, lambda i, won, r=random.Random(7): r.random() < 0.6)
        self.assertTrue(maker.is_edge(self.yes_row(maker.table(obs, z=3.0))))


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
                mock.patch("sys.argv", ["maker_research.py", "--markets-per-series", "50"]), redirect_stdout(out):
            tool.main()
        text = out.getvalue()
        self.assertIn("75 priced markets", text)
        self.assertIn("ALL: join the best bid", text)
        self.assertIn("Sports: improve the best bid", text)
        self.assertTrue(os.path.exists(os.path.join(self.home, "research", "maker_6h.json")))


if __name__ == "__main__":
    unittest.main()
