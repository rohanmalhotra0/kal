"""Research pipeline and the two statistical strategies: python -m unittest tests.test_research"""
import json
import os
import random
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from kal import config, db, engine, research
from kal.client import fee, fee_per_contract
from kal.research import backtest, discover, promote
from kal.strategies import favorites, mentions
from kal.strategies.base import Context
from kal import sizing
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven

T0 = 1_760_000_000
NOW = T0 + 400 * 86400


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def add_series(c, series, n, ask, bid, win_rate, seed, words=None, title=""):
    """n settled events in `series`; each market priced at ask/bid 6h before close."""
    rng = random.Random(seed)
    c.series_list.append({"ticker": series, "title": title or series})
    for i in range(n):
        close = T0 + i * 86400
        for word, rate in (words or [(None, win_rate)]):
            tk = f"{series}-{i}" + (f"-{word[:4].upper()}" if word else "")
            m = {"ticker": tk, "event_ticker": f"{series}-E{i}", "open_time": iso(close - 3 * 86400),
                 "close_time": iso(close), "status": "settled", "volume_fp": "1000.00",
                 "result": "yes" if rng.random() < rate else "no"}
            if word:
                m["yes_sub_title"] = word.title()
            c.settled.setdefault(series, []).append(m)
            t = close - 6 * 3600
            c.candle_data[tk] = [{"end_period_ts": t - 3600, "yes_ask": {"close_dollars": f"{ask:.4f}"},
                                  "yes_bid": {"close_dollars": f"{bid:.4f}"}}]


class AllCandles(FakeKalshi):
    """Sends every candle whatever range is asked for, like a buggy API would."""

    def candles(self, series, ticker, start_ts, end_ts, period=60):
        self.candle_calls = getattr(self, "candle_calls", 0) + 1
        return self.candle_data.get(ticker, [])


class BacktestTests(TempHome):
    def test_snapshot_ignores_future_candles(self):
        c = AllCandles()
        close = T0 + 86400
        m = {"ticker": "S-1", "open_time": iso(close - 3 * 86400), "close_time": iso(close)}
        t = close - 6 * 3600
        c.candle_data["S-1"] = [
            {"end_period_ts": t - 3600, "yes_ask": {"close_dollars": "0.5000"}, "yes_bid": {"close_dollars": "0.4800"}},
            {"end_period_ts": t + 3600, "yes_ask": {"close_dollars": "0.9900"}, "yes_bid": {"close_dollars": "0.9800"}}]
        self.assertEqual(backtest.snapshot(c, "S", m, 6), (0.5, 0.48))
        c.candle_data["S-1"] = c.candle_data["S-1"][1:]  # only a future candle: no price at all
        self.assertIsNone(backtest.snapshot(c, "S", m, 6))
        self.assertIsNone(backtest.snapshot(c, "S", {"ticker": "S-1"}, 6))  # no times: skip

    def test_calibration_finds_planted_edge_not_fair_market(self):
        rng = random.Random(2)
        fair = [{"event": f"e{i}", "yes_ask": 0.93, "yes_bid": 0.92,
                 "result": "yes" if rng.random() < 0.93 else "no"} for i in range(400)]
        rows = {r["lo"]: r for r in backtest.calibration(fair)}
        self.assertFalse(backtest.is_edge(rows[0.9]))
        cheap = [{**o, "result": "yes" if rng.random() < 0.995 else "no"} for o in fair]
        rows = {r["lo"]: r for r in backtest.calibration(cheap)}
        self.assertTrue(backtest.is_edge(rows[0.9]))
        self.assertFalse(backtest.is_edge(rows[0.05]))  # the NO side of the same markets loses
        wide = [{**o, "yes_bid": 0.85} for o in cheap]  # too wide for favorites to trade
        self.assertEqual(backtest.calibration(wide, max_spread=0.02), [])

    def test_lucky_streak_is_not_proof(self):
        # 30 wins in a row at 93c happens 11% of the time even when the price is fair.
        trades = [backtest.trade(f"e{i}", 0.93, True) for i in range(30)]
        roi, lo = backtest.roi_stats(trades, backtest.z_for(1))
        self.assertGreater(roi, 0)
        self.assertLess(lo, 0)

    def test_mention_walk_forward_has_no_lookahead(self):
        obs = [{"series": "S", "event": f"e{i}", "word": "tariff", "close": i, "yes_ask": 0.5,
                "yes_bid": 0.48, "result": "yes"} for i in range(10)]
        trades, hist, _ = backtest.mention_test(obs, 0.15)
        self.assertEqual(len(trades), 10 - mentions.MIN_HISTORY)  # no trades before enough history
        self.assertEqual(hist[("S", "tariff")], [10, 10])

    def test_overlapping_events_dont_leak_results(self):
        # B is decided at 14:00 (6h before its 20:00 close). A closes at 15:00: after B's
        # decision, so A's results must not shape B's price.
        H = 3600
        base = [{"series": "S", "event": f"old{i}", "word": "tariff", "close": i * H, "decided": i * H - 6 * H,
                 "yes_ask": 0.5, "yes_bid": 0.48, "result": "no"} for i in range(3)]
        a = {"series": "S", "event": "A", "word": "tariff", "close": 15 * H, "decided": 9 * H,
             "yes_ask": 0.5, "yes_bid": 0.48, "result": "yes"}
        b = {"series": "S", "event": "B", "word": "tariff", "close": 20 * H, "decided": 14 * H,
             "yes_ask": 0.5, "yes_bid": 0.48, "result": "yes"}
        seen = {}
        real = backtest.word_estimate

        def spy(hits, n, base_rate):
            seen.setdefault(n, hits)
            return real(hits, n, base_rate)
        backtest.word_estimate = spy
        try:
            backtest.mention_test(base + [a, b], 0.15)
        finally:
            backtest.word_estimate = real
        self.assertNotIn(4, seen)  # B never saw A's result (it would be the 4th)

    def test_early_close_uses_the_events_scheduled_close(self):
        H = 3600
        sched = T0 + 100 * H
        said = {"ticker": "EV-SAID", "event_ticker": "EV", "open_time": iso(sched - 30 * H),
                "close_time": iso(sched - 20 * H), "status": "settled", "result": "yes", "volume_fp": "500"}
        not_said = {"ticker": "EV-NOT", "event_ticker": "EV", "open_time": iso(sched - 30 * H),
                    "close_time": iso(sched), "status": "settled", "result": "no", "volume_fp": "500"}
        anchors = backtest.event_anchors([said, not_said])
        self.assertEqual(anchors["EV"], sched)
        # Decided 6h before the scheduled close: the early YES had already closed then,
        # so neither "6h before its own close" (a peek at the outcome) nor a skewed sample.
        self.assertIsNone(backtest.decision_time(said, 6, anchor=anchors["EV"]))
        self.assertEqual(backtest.decision_time(not_said, 6, anchor=anchors["EV"]), sched - 6 * H)

    def test_single_market_that_can_close_early_is_skipped(self):
        c = AllCandles()
        add_series(c, "S", 5, 0.93, 0.92, 0.95, seed=1)
        for m in c.settled["S"]:
            m["can_close_early"] = True
            m["event_ticker"] = m["ticker"]  # one market per event
        self.assertEqual(backtest.load_observations(c, "S", 6, refresh=True), [])

    def test_observations_are_cached_and_recorded(self):
        c = AllCandles()
        add_series(c, "S", 20, 0.93, 0.92, 0.95, seed=1)
        obs = backtest.load_observations(c, "S", 6)
        self.assertEqual(len(obs), 20)
        self.assertEqual(c.candle_calls, 20)
        self.assertTrue(os.path.exists(os.path.join(self.home, "cache", "S_close6_v2.json")))
        self.assertEqual(len(backtest.load_observations(c, "S", 6)), 20)
        self.assertEqual(c.candle_calls, 20)  # second time: all from cache
        res = backtest.run(["S"], client=c)
        self.assertEqual(res["observations"], 20)
        self.assertEqual(db.query("SELECT kind FROM backtests")[0]["kind"], "manual")
        self.assertIn("worst case", backtest.report(res))


class DiscoverTests(TempHome):
    def test_rotation_is_deterministic_and_covers_everything(self):
        tickers = [f"S{i}" for i in range(37)]
        day = 739000
        self.assertEqual(discover.rotation(tickers, day, 5), discover.rotation(list(reversed(tickers)), day, 5))
        self.assertNotEqual(discover.rotation(tickers, day, 5), discover.rotation(tickers, day + 1, 5))
        seen = set()
        for d in range(day, day + 8):  # ceil(37 / 5) days
            picks = discover.rotation(tickers, d, 5)
            self.assertEqual(len(picks), 5)
            seen |= set(picks)
        self.assertEqual(seen, set(tickers))

    def test_stale_edges_get_retested(self):
        tickers = [f"S{i}" for i in range(100)]
        old = {"id": "S7:0.90-0.95", "series": "S7", "status": "live", "last_tested": iso(NOW - 10 * 86400)}
        fresh = {"id": "S8:0.90-0.95", "series": "S8", "status": "live", "last_tested": iso(NOW - 86400)}
        picks = discover.pick_series(tickers, [old, fresh], {}, 739000, 15, NOW)
        self.assertEqual(len(picks), 15)
        self.assertEqual(picks[0], "S7")
        self.assertNotIn("S8", picks[:3])

    def test_finds_edge_and_mentions(self):
        c = FakeKalshi()
        add_series(c, "FAV", 300, 0.93, 0.92, 0.995, seed=3)
        add_series(c, "FAIR", 300, 0.93, 0.92, 0.93, seed=4)
        add_series(c, "KXSAY", 50, 0.50, 0.48, None, seed=5, title="What will Trump say?",
                   words=[("tariff", 0.9), ("biden", 0.5)])
        s = research.run_all(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual(set(s["tested"]), {"FAV", "FAIR", "KXSAY"})
        edges = {e["id"]: e for e in discover.load_edges()}
        self.assertIn("FAV:yes:0.90-0.95", edges)  # the YES side won; the NO side didn't
        self.assertEqual(edges["FAV:yes:0.90-0.95"]["status"], "candidate")
        self.assertEqual(edges["FAV:yes:0.90-0.95"]["side"], "yes")
        self.assertFalse([e for e in edges if e.startswith("FAV:no:")])
        self.assertFalse([e for e in edges if e.startswith("FAIR")])
        words = discover.load_words()
        self.assertEqual(words["series"], ["KXSAY"])
        self.assertGreater(words["roi_lo"], 0)
        self.assertFalse(words["promoted"])
        self.assertEqual(words["words"]["KXSAY|tariff"][1], 50)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM backtests WHERE kind = 'calibration'")[0]["n"], 3)
        self.assertEqual(db.query("SELECT kind, ok FROM runs")[0], {"kind": "research", "ok": 1})
        self.assertTrue(db.get("last_research")["ok"])
        # The engine hands these to strategies as ctx.research.
        self.assertEqual(set(engine.research_outputs()), {"edges", "words"})

    def test_a_failing_series_does_not_stop_the_run(self):
        class Flaky(FakeKalshi):
            def settled_markets(self, series, limit=None):
                if series == "BAD":
                    raise RuntimeError("boom")
                return super().settled_markets(series, limit)
        c = Flaky()
        c.series_list.append({"ticker": "BAD", "title": "Bad"})
        add_series(c, "FAV", 300, 0.93, 0.92, 0.995, seed=3)
        s = research.run_all(c)
        self.assertIn("boom", s["failed"]["BAD"])
        self.assertEqual(s["tested"], ["FAV"])
        self.assertIn("FAV:yes:0.90-0.95", s["new_edges"])
        self.assertEqual(len(db.query("SELECT * FROM runs WHERE kind = 'research'")), 1)

    def test_named_series_and_quick_mode(self):
        c = FakeKalshi()
        for i in range(6):
            add_series(c, f"S{i}", 12, 0.93, 0.92, 0.95, seed=i)
        cfg = dict(config.DEFAULTS)
        self.assertEqual(discover.run(c, cfg, series=["S4"])["tested"], ["S4"])
        self.assertEqual(len(discover.run(c, cfg, quick=True)["tested"]), 3)

    def test_whole_failure_is_recorded_not_raised(self):
        c = FakeKalshi()
        c.series = lambda category=None: (_ for _ in ()).throw(RuntimeError("no network"))
        s = research.run_all(c)
        self.assertFalse(s["ok"])
        self.assertEqual(db.query("SELECT ok FROM runs")[0]["ok"], 0)

    def test_retest_retires_and_results_retire_for_good(self):
        good = {"lo": 0.9, "hi": 0.95, "trades": 300, "events": 300, "roi": 0.06, "roi_lo": 0.02}
        bad = dict(good, roi=-0.01, roi_lo=-0.05)
        edges = []
        discover.merge_edges(edges, "S", [good], 6.0, "d1")
        self.assertEqual(edges[0]["status"], "candidate")
        ch = discover.merge_edges(edges, "S", [bad], 6.0, "d2")
        self.assertEqual((edges[0]["status"], ch["retired"]), ("retired", ["S:0.90-0.95"]))
        discover.merge_edges(edges, "S", [good], 6.0, "d3")
        self.assertEqual(edges[0]["status"], "candidate")  # backtest retired it; backtest may revive it
        edges[0].update(status="retired", retired_by="results")
        discover.merge_edges(edges, "S", [good], 6.0, "d4")
        self.assertEqual(edges[0]["status"], "retired")  # lost real/paper money: stays retired
        few = dict(bad, trades=5)
        edges[0].update(status="live", retired_by="")
        discover.merge_edges(edges, "S", [few], 6.0, "d5")
        self.assertEqual(edges[0]["status"], "live")  # too little data to judge


def save_edges(*edges):
    """Edges from (id, status[, roi_lo]). An id like SERIES:side:lo-hi carries its side."""
    discover.save_edges([{"id": e[0], "series": e[0].split(":")[0],
                          "side": e[0].split(":")[1] if e[0].count(":") == 2 else None, "lo": 0.9, "hi": 0.95,
                          "hours_before_close": 6.0, "roi": 0.05, "roi_lo": e[2] if len(e) > 2 else 0.02,
                          "trades": 300, "events": 300, "status": e[1]} for e in edges])


def paper_trade(strategy, i, p, won, meta, count=1):
    sid = db.insert("signals", ts=T0 + i, strategy=strategy, event=f"E{i}", meta=meta, status="accepted")
    cost = count * p + fee(count, p)
    db.insert("paper", ts=T0 + i, signal_id=sid, strategy=strategy, event=f"E{i}", ticker=f"T{i}", side="yes",
              count=count, price=p, cost=cost, settled=1, result="yes" if won else "no",
              pnl=count * won - cost, settled_ts=T0 + i)


class PromoteTests(TempHome):
    EDGE = "FAV:0.90-0.95"

    def status(self):
        return {e["id"]: e["status"] for e in discover.load_edges()}[self.EDGE]

    def test_candidate_goes_live_only_with_enough_paper_wins(self):
        save_edges((self.EDGE, "candidate"))
        for i in range(20):
            paper_trade("favorites", i, 0.90, True, {"edge": self.EDGE, "paper_only": True})
        self.assertEqual(promote.apply(dict(config.DEFAULTS)), [])
        self.assertEqual(self.status(), "candidate")  # 20 is too few
        # Each daily look needs more confidence, so it takes a long, clean record.
        for i in range(20, 120):
            paper_trade("favorites", i, 0.90, True, {"edge": self.EDGE, "paper_only": True})
        notes = promote.apply(dict(config.DEFAULTS, auto_promote=False))
        self.assertIn("Auto promote is off", notes[0])
        self.assertEqual(self.status(), "candidate")
        notes = promote.apply(dict(config.DEFAULTS))
        self.assertIn("Set favorites to live", notes[0])  # the default mode is paper
        self.assertEqual(self.status(), "live")

    def test_copies_of_one_market_dont_count(self):
        save_edges((self.EDGE, "candidate"))
        for i in range(200):  # the same market bought over and over: one trade, one event
            sid = db.insert("signals", ts=T0 + i, strategy="favorites", event="E1",
                            meta={"edge": self.EDGE}, status="accepted")
            db.insert("paper", ts=T0 + i, signal_id=sid, strategy="favorites", event="E1", ticker="T1",
                      side="yes", count=1, price=0.9, cost=0.91, settled=1, result="yes", pnl=0.09,
                      settled_ts=T0 + i)
        promote.apply(dict(config.DEFAULTS))
        self.assertEqual(self.status(), "candidate")

    def test_few_events_dont_promote(self):
        save_edges((self.EDGE, "candidate"))
        for i in range(120):  # 120 markets, but only 6 events: they move together
            sid = db.insert("signals", ts=T0 + i, strategy="favorites", event=f"E{i % 6}",
                            meta={"edge": self.EDGE}, status="accepted")
            db.insert("paper", ts=T0 + i, signal_id=sid, strategy="favorites", event=f"E{i % 6}",
                      ticker=f"T{i}", side="yes", count=1, price=0.9, cost=0.91, settled=1, result="yes",
                      pnl=0.09, settled_ts=T0 + i)
        promote.apply(dict(config.DEFAULTS))
        self.assertEqual(self.status(), "candidate")

    def test_z_grows_with_each_look(self):
        self.assertLess(promote.z_look(1), promote.z_look(2))
        self.assertLess(promote.z_look(2), promote.z_look(30))

    def test_paper_losses_keep_candidate_on_paper(self):
        save_edges((self.EDGE, "candidate"))
        for i in range(40):
            paper_trade("favorites", i, 0.90, i % 5 != 0, {"edge": self.EDGE})  # 80% wins at 90c
        promote.apply(dict(config.DEFAULTS))
        self.assertEqual(self.status(), "candidate")

    def test_other_edges_trades_dont_count(self):
        save_edges((self.EDGE, "candidate"))
        for i in range(40):
            paper_trade("favorites", i, 0.90, True, {"edge": "OTHER:0.90-0.95"})
        promote.apply(dict(config.DEFAULTS))
        self.assertEqual(self.status(), "candidate")

    def test_live_edge_retired_when_recent_trades_lose(self):
        save_edges((self.EDGE, "live"))
        for i in range(40):
            paper_trade("favorites", i, 0.93, True, {"edge": self.EDGE})
        self.assertEqual(promote.apply(dict(config.DEFAULTS, auto_promote=False)), [])
        self.assertEqual(self.status(), "live")
        # Live losses (from the ledger's settlements) count too, even with auto_promote off.
        for i in range(40, 46):
            sid = db.insert("signals", ts=T0 + i, strategy="favorites", event=f"E{i}", meta={"edge": self.EDGE})
            db.insert("orders", ts=T0 + i, signal_id=sid, strategy="favorites", ticker=f"L{i}", side="yes",
                      count=5, price=0.93, filled=5, status="executed")
            db.insert("settlements", ticker=f"L{i}", settled_ts=T0 + i, result="no", revenue=0, cost=4.7, pnl=-4.7)
        notes = promote.apply(dict(config.DEFAULTS, auto_promote=False))
        self.assertIn("stopped", notes[0])
        self.assertEqual(self.status(), "retired")
        edge = discover.load_edges()[0]
        self.assertEqual(edge["retired_by"], "results")

    def test_mentions_promote_and_demote(self):
        discover.save_words({"series": ["KXSAY"], "margin": 0.15, "roi_lo": 0.1, "words": {}, "base": {},
                             "promoted": False})
        for i in range(40):
            paper_trade("mentions", i, 0.5, i % 7 != 0, {"paper_only": True})
        cfg = dict(config.DEFAULTS, strategies={"mentions": {"mode": "live"}})
        notes = promote.apply(cfg)
        self.assertIn("Mention words won on paper", notes[0])
        self.assertIn("It now uses real money", notes[0])
        self.assertTrue(discover.load_words()["promoted"])
        for i in range(40, 70):
            paper_trade("mentions", i, 0.5, i % 3 == 0, {"paper_only": False})
        notes = promote.apply(dict(config.DEFAULTS))
        self.assertIn("back to paper", notes[0])
        self.assertFalse(discover.load_words()["promoted"])

    def test_mentions_need_a_backtest_edge_too(self):
        discover.save_words({"series": ["KXSAY"], "margin": 0.15, "roi_lo": None, "promoted": False})
        for i in range(40):
            paper_trade("mentions", i, 0.5, True, {"paper_only": True})
        promote.apply(dict(config.DEFAULTS))
        self.assertFalse(discover.load_words()["promoted"])


def open_market(ticker="FAV-X-1", event="FAV-X", yes_ask=0.93, yes_bid=0.92, hours=5.5, vol=500, **kw):
    return {"ticker": ticker, "event_ticker": event, "title": "Test", "status": "active",
            "yes_ask_dollars": f"{yes_ask:.4f}", "yes_bid_dollars": f"{yes_bid:.4f}",
            "no_ask_dollars": f"{1 - yes_bid:.4f}", "no_bid_dollars": f"{1 - yes_ask:.4f}",
            "volume_24h_fp": f"{vol:.2f}", "close_time": iso(time.time() + hours * 3600), **kw}


def ctx_for(client, research, budget=1000.0, bankroll=1000.0, held=()):
    return Context(client, dict(config.DEFAULTS), "paper", budget, bankroll, set(held), research, {})


def book_for(c, m, depth=100):
    yes_bid = float(m["yes_bid_dollars"])
    no_bid = float(m["no_bid_dollars"])
    c.books[m["ticker"]] = {"yes": [(yes_bid, depth)], "no": [(no_bid, depth)]}


class FavoritesTests(TempHome):
    EDGE = {"id": "FAV:yes:0.90-0.95", "series": "FAV", "side": "yes", "lo": 0.9, "hi": 0.95,
            "hours_before_close": 6.0, "roi": 0.05, "roi_lo": 0.02, "trades": 300, "events": 300,
            "status": "candidate"}

    def setUp(self):
        super().setUp()
        self.c = FakeKalshi()
        self.good = open_market()
        self.c.market_list = [
            self.good,
            open_market("FAV-Y-1", "FAV-Y", hours=10),            # closes too late
            open_market("FAV-U-1", "FAV-U", hours=0.5),           # too close to the bell: not what was tested
            open_market("FAV-Z-1", "FAV-Z", yes_bid=0.88),          # spread too wide
            open_market("FAV-W-1", "FAV-W", vol=50),                # too thin
            open_market("OTHER-A-1", "OTHER-A"),                    # another series
            open_market("FAV-V-1", "FAV-V", status="closed"),
        ]
        for m in self.c.market_list:
            book_for(self.c, m)
            self.c.market_info[m["ticker"]] = m

    def test_signal_size_and_meta(self):
        sigs = favorites.STRATEGY.scan(ctx_for(self.c, {"edges": [self.EDGE]}))
        self.assertEqual(len(sigs), 1)
        sig = sigs[0]
        leg = sig.legs[0]
        self.assertEqual((leg.ticker, leg.side, leg.price), ("FAV-X-1", "yes", 0.93))
        p = (0.93 + fee_per_contract(0.93)) * 1.02
        self.assertEqual(leg.count, sizing.contracts(p, 0.93, 1000.0, 0.25, 10.0))
        self.assertLessEqual(sig.cost, 10.0)
        self.assertEqual(sig.meta, {"edge": "FAV:yes:0.90-0.95", "paper_only": True})
        self.assertAlmostEqual(sig.exp_profit, leg.count * p - sig.cost)
        live = dict(self.EDGE, status="live")
        self.assertFalse(favorites.STRATEGY.scan(ctx_for(self.c, {"edges": [live]}))[0].meta["paper_only"])

    def test_no_side_and_limits(self):
        # 7c NO ask on a 93c favorite: an edge at 0.05-0.10 would buy NO. Not with ours.
        self.assertEqual(favorites.STRATEGY.scan(ctx_for(self.c, {"edges": [self.EDGE]}, held={"FAV-X-1"})), [])
        no_edge = dict(self.EDGE, id="FAV:no:0.05-0.10", side="no", lo=0.05, hi=0.10)
        sig = favorites.STRATEGY.scan(ctx_for(self.c, {"edges": [no_edge]}))[0]
        self.assertEqual((sig.legs[0].side, sig.legs[0].price), ("no", 0.08))
        # Two edges that disagree about one market: stay out.
        self.assertEqual(favorites.STRATEGY.scan(ctx_for(self.c, {"edges": [self.EDGE, no_edge]})), [])
        # Thin book caps the size; tiny budget caps it too.
        book_for(self.c, self.good, depth=2)
        self.assertEqual(favorites.STRATEGY.scan(ctx_for(self.c, {"edges": [self.EDGE]}))[0].legs[0].count, 2)
        book_for(self.c, self.good)
        self.assertEqual(favorites.STRATEGY.scan(ctx_for(self.c, {"edges": [self.EDGE]}, budget=0.5)), [])

    def test_edge_only_buys_its_own_side(self):
        # A NO-side edge at 0.90-0.95 must never buy YES at 0.93, and a pooled edge with no
        # side (it could hide a losing side) is never traded at all.
        no_side = dict(self.EDGE, id="FAV:no:0.90-0.95", side="no")
        self.assertEqual(favorites.STRATEGY.scan(ctx_for(self.c, {"edges": [no_side]})), [])
        pooled = {k: v for k, v in self.EDGE.items() if k != "side"}
        self.assertEqual(favorites.STRATEGY.scan(ctx_for(self.c, {"edges": [pooled]})), [])
        sig = favorites.STRATEGY.scan(ctx_for(self.c, {"edges": [self.EDGE]}))[0]
        self.assertEqual(sig.legs[0].side, "yes")

    def test_bad_or_retired_edges_are_ignored(self):
        for e in (dict(self.EDGE, status="retired"), dict(self.EDGE, roi_lo=None), {"id": "x"},
                  dict(self.EDGE, roi_lo=-0.05)):
            self.assertEqual(favorites.STRATEGY.scan(ctx_for(self.c, {"edges": [e]})), [], e)
        self.assertEqual(favorites.STRATEGY.scan(ctx_for(self.c, {})), [])

    def test_recheck_before_live_order(self):
        live = dict(self.EDGE, status="live")
        ctx = ctx_for(self.c, {"edges": [live]})
        sig = favorites.STRATEGY.scan(ctx)[0]
        self.assertEqual(favorites.STRATEGY.recheck(ctx, sig).legs[0].count, sig.legs[0].count)
        self.assertIsNone(favorites.STRATEGY.recheck(ctx_for(self.c, {"edges": [self.EDGE]}), sig))  # not live now
        self.c.books["FAV-X-1"] = {"yes": [(0.97, 100)], "no": [(0.02, 100)]}  # price ran out of the range
        self.assertIsNone(favorites.STRATEGY.recheck(ctx, sig))
        book_for(self.c, self.good)
        self.c.market_info["FAV-X-1"] = dict(self.good, status="closed")
        self.assertIsNone(favorites.STRATEGY.recheck(ctx, sig))

    def test_proven_needs_a_live_edge(self):
        ok, _ = favorites.STRATEGY.proven(ctx_for(self.c, {"edges": [self.EDGE]}))
        self.assertFalse(ok)
        ok, _ = favorites.STRATEGY.proven(ctx_for(self.c, {"edges": [dict(self.EDGE, status="live")]}))
        self.assertTrue(ok)

    def _engine(self, status):
        config.update({"enabled": True, "budget": 50,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"favorites": {"mode": "live"}}})
        orders_proven()
        save_edges((self.EDGE["id"], status))
        with mock.patch.object(engine, "load_all", return_value={"favorites": favorites.STRATEGY}):
            return engine.run_once(self.c)

    def test_engine_trades_live_edge_for_real(self):
        s = self._engine("live")
        self.assertTrue(s["ok"], s)
        self.assertEqual(s["live_trades"], 1)
        self.assertEqual([o[:2] for o in self.c.orders], [("FAV-X-1", "yes")])
        meta = json.loads(db.query("SELECT meta FROM signals")[0]["meta"])
        self.assertEqual(meta["edge"], "FAV:yes:0.90-0.95")

    def test_engine_keeps_candidate_on_paper(self):
        s = self._engine("candidate")
        self.assertEqual(self.c.orders, [])
        self.assertEqual(s["paper_trades"], 1)
        self.assertEqual(db.query("SELECT mode FROM signals")[0]["mode"], "paper")


class MentionsTests(TempHome):
    WORDS = {"series": ["KXSAY"], "margin": 0.15, "roi_lo": 0.1, "promoted": False,
             "words": {"KXSAY|tariff": [9, 10], "KXSAY|biden": [1, 10], "KXSAY|new": [1, 1]},
             "base": {"KXSAY": [10, 20]}}

    def setUp(self):
        super().setUp()
        self.c = FakeKalshi()
        self.c.market_list = [
            open_market("KXSAY-1-TARI", "KXSAY-1", 0.50, 0.48, yes_sub_title="Tariff"),
            open_market("KXSAY-1-BIDE", "KXSAY-1", 0.52, 0.50, yes_sub_title="Biden"),
            open_market("KXSAY-1-NEW", "KXSAY-1", 0.10, 0.08, yes_sub_title="New"),  # too little history
        ]
        for m in self.c.market_list:
            book_for(self.c, m)

    def test_buys_both_ways_capped_at_five_dollars(self):
        sigs = mentions.STRATEGY.scan(ctx_for(self.c, {"words": self.WORDS}))
        got = {s.legs[0].ticker: (s.legs[0].side, s.legs[0].price) for s in sigs}
        self.assertEqual(got, {"KXSAY-1-TARI": ("yes", 0.5), "KXSAY-1-BIDE": ("no", 0.5)})
        for s in sigs:
            self.assertLessEqual(s.cost, 5.0)
            self.assertEqual(s.legs[0].count, 9)
            self.assertEqual(s.meta, {"paper_only": True})
            self.assertGreater(s.exp_profit, 0)
        cfg = dict(config.DEFAULTS, max_per_trade=2.0)
        ctx = Context(self.c, cfg, "paper", 1000, 1000, set(), {"words": self.WORDS}, {})
        self.assertTrue(all(s.cost <= 2.0 for s in mentions.STRATEGY.scan(ctx)))

    def test_proven_needs_promotion_and_backtest(self):
        self.assertFalse(mentions.STRATEGY.proven(ctx_for(self.c, {"words": self.WORDS}))[0])
        promoted = dict(self.WORDS, promoted=True)
        self.assertTrue(mentions.STRATEGY.proven(ctx_for(self.c, {"words": promoted}))[0])
        self.assertFalse(mentions.STRATEGY.proven(ctx_for(self.c, {"words": dict(promoted, roi_lo=None)}))[0])
        self.assertFalse(mentions.STRATEGY.proven(ctx_for(self.c, {}))[0])
        sig = mentions.STRATEGY.scan(ctx_for(self.c, {"words": promoted}))[0]
        self.assertEqual(sig.meta, {"paper_only": False})

    def test_recheck_uses_the_order_book(self):
        promoted = dict(self.WORDS, promoted=True)
        ctx = ctx_for(self.c, {"words": promoted})
        sig = [s for s in mentions.STRATEGY.scan(ctx) if s.legs[0].side == "yes"][0]
        m = self.c.market_list[0]
        self.c.market_info[m["ticker"]] = m
        self.c.books[m["ticker"]] = {"yes": [(0.48, 50)], "no": [(0.50, 3)]}
        again = mentions.STRATEGY.recheck(ctx, sig)
        self.assertEqual((again.legs[0].price, again.legs[0].count), (0.5, 3))
        self.c.books[m["ticker"]] = {"yes": [(0.85, 50)], "no": [(0.13, 50)]}  # price ran away
        self.assertIsNone(mentions.STRATEGY.recheck(ctx, sig))


if __name__ == "__main__":
    unittest.main()


class ActiveSeriesTests(TempHome):
    def test_busiest_open_series_first_and_combos_skipped(self):
        c = FakeKalshi()
        c.event_list = [
            {"series_ticker": "QUIET", "markets": [{"volume_24h_fp": "5.00"}]},
            {"series_ticker": "BUSY", "markets": [{"volume_24h_fp": "900.00"}, {"volume_24h_fp": "100.00"}]},
            {"series_ticker": "KXMVECROSSCATEGORY", "markets": [{"volume_24h_fp": "99999.00"}]},
            {"series_ticker": "DEAD", "markets": [{"volume_24h_fp": "0.00"}]},
        ]
        self.assertEqual(discover.active_series(c), ["BUSY", "QUIET"])

    def test_falls_back_when_events_fail(self):
        c = FakeKalshi()
        c.events = lambda **p: (_ for _ in ()).throw(RuntimeError("down"))
        self.assertEqual(discover.active_series(c), [])
