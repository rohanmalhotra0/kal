"""longshot_fade and its research: python -m unittest tests.test_longshot"""
import json
import random
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from kal import config, db, engine, sizing
from kal.client import fee, ts
from kal.research import longshot
from kal.strategies import longshot_fade as fade
from kal.strategies.base import Context
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven

T0 = 1_760_000_000
H = 3600
WEATHER = "Climate and Weather"
CELL = "Climate and Weather:0.05-0.10"


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def cfg(**kw):
    return dict(config.DEFAULTS, **kw)


# ---------- research ----------

def add_series(c, series, category, n, hit, bid=0.07, ask=0.08, seed=1, title="", liquid=True):
    """n settled daily events in `series`. Each has a longshot priced at bid/ask 6h before
    its close, which wins with chance `hit`, and a 90c favorite that wins otherwise."""
    rng = random.Random(seed)
    c.series_list.append({"ticker": series, "title": title or series, "category": category, "frequency": "daily"})
    for i in range(n):
        close = T0 + i * 86400
        ev = f"{series}-E{i}"
        long_won = rng.random() < hit
        for leg, b, a, won in (("L", bid, ask, long_won), ("F", 0.90, 0.91, not long_won)):
            tk = f"{ev}-{leg}"
            c.settled.setdefault(series, []).append({
                "ticker": tk, "event_ticker": ev, "open_time": iso(close - 3 * 86400), "close_time": iso(close),
                "status": "settled", "volume_fp": "1000.00", "result": "yes" if won else "no"})
            c.candle_data[tk] = [{"end_period_ts": close - 7 * H, "yes_ask": {"close_dollars": f"{a:.4f}"},
                                  "yes_bid": {"close_dollars": f"{b:.4f}"}}]
    if liquid:  # an open event with volume: discover.active_series counts the series as busy
        c.event_list.append({"event_ticker": f"{series}-OPEN", "series_ticker": series,
                             "markets": [{"ticker": f"{series}-OPEN-L", "volume_24h_fp": "500.00"}]})


class CountingCandles(FakeKalshi):
    def candles(self, series, ticker, start_ts, end_ts, period=60):
        self.candle_calls = getattr(self, "candle_calls", {})
        self.candle_calls[series] = self.candle_calls.get(series, 0) + 1
        return super().candles(series, ticker, start_ts, end_ts, period)


class ResearchTests(TempHome):
    def test_planted_bias_is_found_and_a_fair_market_is_not(self):
        c = FakeKalshi()
        add_series(c, "KXHIGHNY", WEATHER, 200, hit=0.01, seed=1)  # YES at 7c hits 1%: over-bet
        add_series(c, "KXNBA", "Sports", 200, hit=0.075, seed=2)    # hits as often as its price says
        s = longshot.run(c, cfg())
        self.assertTrue(s["ok"], s)
        self.assertEqual(set(s["tested"]), {"KXHIGHNY", "KXNBA"})
        self.assertEqual(s["new_cells"], [CELL])
        cells = {x["id"]: x for x in longshot.load_cells()}
        self.assertEqual(list(cells), [CELL])
        cell = cells[CELL]
        for k in ("id", "category", "lo", "hi", "hours_before_close", "trades", "events", "roi", "roi_lo",
                  "status", "first_seen", "last_tested", "series"):
            self.assertIn(k, cell)
        self.assertEqual((cell["status"], cell["category"], cell["lo"], cell["hi"]), ("candidate", WEATHER, 0.05, 0.10))
        self.assertEqual(cell["hours_before_close"], 6.0)
        self.assertEqual((cell["trades"], cell["events"], cell["groups"]), (200, 200, 200))
        self.assertGreater(cell["roi_lo"], 0)
        self.assertEqual(cell["series"], ["KXHIGHNY"])
        # Every range tried is recorded, and the fair one lost after fees. The 90c favorites
        # never count: they're not longshots.
        rows = json.loads(db.query("SELECT results FROM backtests WHERE kind = 'longshot'")[0]["results"])["rows"]
        self.assertEqual({(r["category"], r["lo"]) for r in rows}, {(WEATHER, 0.05), ("Sports", 0.05)})
        self.assertLess([r for r in rows if r["category"] == "Sports"][0]["roi"], 0)
        # The engine hands the file to strategies as ctx.research["longshot"].
        self.assertEqual(fade.active_cells(engine.research_outputs())[0]["id"], CELL)

    def test_cells_split_by_category_and_pool_its_series(self):
        c = FakeKalshi()
        add_series(c, "KXHIGHNY", WEATHER, 120, hit=0.0, seed=1)
        add_series(c, "KXHIGHCHI", WEATHER, 120, hit=0.0, seed=2)
        add_series(c, "KXNFL", "Sports", 240, hit=0.0, bid=0.15, ask=0.16, seed=3)  # over-bet, at 15c
        add_series(c, "KXNBA", "Sports", 240, hit=0.075, seed=4)                   # fair, at 7c
        s = longshot.run(c, cfg())
        self.assertTrue(s["ok"], s)
        cells = {x["id"]: x for x in longshot.load_cells()}
        self.assertEqual(set(cells), {CELL, "Sports:0.10-0.20"})
        self.assertEqual(cells[CELL]["series"], ["KXHIGHCHI", "KXHIGHNY"])
        self.assertEqual(cells[CELL]["groups"], 240)  # two cities on one day are two groups
        self.assertEqual(cells["Sports:0.10-0.20"]["series"], ["KXNFL"])  # not KXNBA: no evidence there
        self.assertEqual(s["categories"], {WEATHER: 480, "Sports": 960})

    def test_cpi_fed_and_combos_are_never_tested(self):
        c = FakeKalshi()
        add_series(c, "KXCPI", "Economics", 200, hit=0.0, seed=1, title="CPI")
        add_series(c, "KXFEDDECISION", "Economics", 200, hit=0.0, seed=2, title="Fed decision")
        add_series(c, "KXRATES", "Economics", 200, hit=0.0, seed=3, title="Interest rates in December")
        add_series(c, "KXGDP", "Economics", 200, hit=0.0, seed=4, title="GDP growth")
        add_series(c, "KXMVESPORTS", "Sports", 200, hit=0.0, seed=5)
        s = longshot.run(c, cfg())
        self.assertEqual(s["tested"], ["KXGDP"])
        cells = longshot.load_cells()
        self.assertEqual([(x["id"], x["series"]) for x in cells], [("Economics:0.05-0.10", ["KXGDP"])])

    def test_wide_books_are_not_counted(self):
        c = FakeKalshi()
        add_series(c, "KXHIGHNY", WEATHER, 200, hit=0.0, bid=0.05, ask=0.09)  # 4c wide: a stub book
        s = longshot.run(c, cfg())
        self.assertTrue(s["ok"], s)
        self.assertEqual(longshot.load_cells(), [])

    def test_a_failing_series_does_not_stop_the_run(self):
        class Flaky(FakeKalshi):
            def settled_markets(self, series, limit=None):
                if series == "BAD":
                    raise RuntimeError("boom")
                return super().settled_markets(series, limit)

            def candles(self, series, ticker, start_ts, end_ts, period=60):
                if series == "NOCANDLES":
                    raise RuntimeError("no candles")
                return super().candles(series, ticker, start_ts, end_ts, period)
        c = Flaky()
        add_series(c, "BAD", WEATHER, 5, hit=0.0)
        add_series(c, "NOCANDLES", WEATHER, 10, hit=0.0)
        add_series(c, "KXHIGHNY", WEATHER, 200, hit=0.0, seed=1)
        s = longshot.run(c, cfg())
        self.assertTrue(s["ok"], s)
        self.assertIn("boom", s["failed"]["BAD"])
        self.assertIn("candles keep failing", s["failed"]["NOCANDLES"])
        self.assertEqual(s["tested"], ["KXHIGHNY"])
        self.assertEqual(s["new_cells"], [CELL])

    def test_whole_failure_is_reported_not_raised(self):
        c = FakeKalshi()
        c.series = lambda category=None: (_ for _ in ()).throw(RuntimeError("no network"))
        s = longshot.run(c, cfg())
        self.assertFalse(s["ok"])
        self.assertIn("no network", s["error"])
        # No list of busy series: test nothing rather than guess.
        c = FakeKalshi()
        add_series(c, "KXHIGHNY", WEATHER, 200, hit=0.0)
        c.events = lambda **p: (_ for _ in ()).throw(RuntimeError("down"))
        s = longshot.run(c, cfg())
        self.assertFalse(s["ok"])
        self.assertIn("busy", s["error"])
        self.assertEqual(longshot.load_cells(), [])

    def test_series_tested_before_still_count_from_the_cache(self):
        c = CountingCandles()
        add_series(c, "KXHIGHNY", WEATHER, 100, hit=0.0, seed=1)
        add_series(c, "KXHIGHCHI", WEATHER, 100, hit=0.0, seed=2)
        longshot.run(c, cfg(), series=["KXHIGHNY"])
        self.assertEqual(longshot.load_cells()[0]["trades"], 100)
        self.assertEqual(c.candle_calls, {"KXHIGHNY": 200})
        s = longshot.run(c, cfg(), series=["KXHIGHCHI"])
        self.assertEqual(s["tested"], ["KXHIGHCHI"])
        cell = longshot.load_cells()[0]
        self.assertEqual((cell["trades"], cell["series"]), (200, ["KXHIGHCHI", "KXHIGHNY"]))
        self.assertEqual(c.candle_calls, {"KXHIGHNY": 200, "KXHIGHCHI": 200})  # NY came from the cache

    def test_each_series_rows_are_dropped_before_the_next_loads(self):
        # Holding every busy series' rows at once took 489 MB on 300 series (round-3 review),
        # past the service's 400 MB cap. Only compact trades may outlive their series.
        import weakref
        from kal.research import backtest

        class Rows(list):
            pass
        alive, real = [], backtest.load_observations

        def load(*a, **k):
            self.assertEqual([r for r in alive if r() is not None], [])
            rows = Rows(real(*a, **k))
            alive.append(weakref.ref(rows))
            return rows
        c = FakeKalshi()
        add_series(c, "KXHIGHNY", WEATHER, 120, hit=0.0, seed=1)
        add_series(c, "KXHIGHCHI", WEATHER, 120, hit=0.0, seed=2)
        add_series(c, "KXNBA", "Sports", 120, hit=0.075, seed=3)
        with mock.patch.object(backtest, "load_observations", load):
            s = longshot.run(c, cfg())
        self.assertTrue(s["ok"], s)
        self.assertGreaterEqual(len(alive), 3)
        self.assertEqual(s["categories"], {WEATHER: 480, "Sports": 240})

    def test_quick_mode_tests_a_few_series(self):
        c = FakeKalshi()
        for i in range(6):
            add_series(c, f"KXS{i}", WEATHER, 12, hit=0.0, seed=i)
        self.assertEqual(len(longshot.run(c, cfg(), quick=True)["tested"]), 3)


class FadeTradeTests(unittest.TestCase):
    OBS = {"series": "S", "event": "E1", "ticker": "T1", "yes_bid": 0.05, "yes_ask": 0.06, "result": "no",
           "decided": T0 - 6 * H, "close": T0 - 5 * H}

    def test_fee_is_rounded_up_per_order(self):
        # At 99c the smooth fee is 0.07c a contract, but Kalshi charges a whole cent an order.
        self.assertAlmostEqual(fade.cost_each(0.99), (5 * 0.99 + 0.01) / 5)
        self.assertGreater(fade.cost_each(0.99), sizing.unit_cost(0.99))
        _, cost, pnl, _ = longshot.fade_trade(dict(self.OBS, yes_bid=0.01, yes_ask=0.02), 6)
        self.assertAlmostEqual(cost, 4.96)
        self.assertAlmostEqual(pnl, 0.04)

    def test_cancelled_market_counts_at_what_no_paid(self):
        # Real Kalshi: a called-off match paid $0.33 on each outcome. A NO at 95c lost 28c there.
        _, cost, pnl, _ = longshot.fade_trade(dict(self.OBS, result="scalar", yes_value=0.33), 6)
        self.assertAlmostEqual(cost, 5 * 0.95 + fee(5, 0.95))
        self.assertAlmostEqual(pnl, 5 * 0.67 - cost)
        _, _, lost, _ = longshot.fade_trade(dict(self.OBS, result="yes"), 6)
        self.assertAlmostEqual(lost, -cost)

    def test_groups_are_series_and_close_day(self):
        a = longshot.fade_trade(self.OBS, 6)
        self.assertEqual(a[0], fade.group_of("S", T0))
        self.assertEqual(longshot.fade_trade(dict(self.OBS, event="E2"), 6)[0], a[0])  # same series, same day
        self.assertNotEqual(longshot.fade_trade(dict(self.OBS, decided=T0 + 86400), 6)[0], a[0])
        self.assertNotEqual(longshot.fade_trade(dict(self.OBS, series="S2"), 6)[0], a[0])
        self.assertIsNone(longshot.fade_trade(dict(self.OBS, yes_ask=0.09), 6))  # too wide
        self.assertIsNone(longshot.fade_trade({"yes_bid": 0.05}, 6))            # unreadable

    def test_one_series_on_one_day_is_one_cluster(self):
        # 300 NO wins look like proof, until you see they're one series on one day.
        obs = [dict(self.OBS, event=f"E{i}", ticker=f"T{i}", yes_bid=0.07, yes_ask=0.08) for i in range(300)]
        row = longshot.calibrate({WEATHER: obs}, 6)[0]
        self.assertEqual((row["trades"], row["events"], row["groups"]), (300, 300, 1))
        self.assertIsNone(row["roi_lo"])
        self.assertFalse(longshot.is_edge(row))
        spread = [dict(o, decided=T0 + i * 86400) for i, o in enumerate(obs)]
        self.assertTrue(longshot.is_edge(longshot.calibrate({WEATHER: spread}, 6)[0]))

    def test_more_cells_need_more_confidence(self):
        obs = [dict(self.OBS, event=f"E{i}", decided=T0 + i * 86400, yes_bid=0.07, yes_ask=0.08) for i in range(70)]
        one = longshot.calibrate({WEATHER: obs}, 6)[0]
        many = [r for r in longshot.calibrate({WEATHER: obs, **{f"C{i}": obs for i in range(20)}}, 6)
                if r["category"] == WEATHER][0]
        self.assertAlmostEqual(one["roi"], many["roi"])
        self.assertLess(many["roi_lo"], one["roi_lo"])

    def test_retest_retires_revives_and_results_retire_for_good(self):
        good = {"category": WEATHER, "lo": 0.05, "hi": 0.10, "trades": 300, "events": 300, "groups": 300,
                "roi": 0.05, "roi_lo": 0.01, "series": ["S"]}
        bad = dict(good, roi=-0.01, roi_lo=-0.04)
        cells = []
        longshot.merge_cells(cells, [good], 6.0, "d1")
        self.assertEqual(cells[0]["status"], "candidate")
        ch = longshot.merge_cells(cells, [bad], 6.0, "d2")
        self.assertEqual((cells[0]["status"], ch["retired"]), ("retired", [CELL]))
        longshot.merge_cells(cells, [good], 6.0, "d3")
        self.assertEqual(cells[0]["status"], "candidate")  # the backtest retired it; it may revive it
        cells[0].update(status="retired", retired_by="results")
        longshot.merge_cells(cells, [good], 6.0, "d4")
        self.assertEqual(cells[0]["status"], "retired")  # lost paper or real money: stays retired
        cells[0].update(status="live", retired_by="")
        longshot.merge_cells(cells, [dict(bad, trades=5)], 6.0, "d5")
        self.assertEqual(cells[0]["status"], "live")  # too little data to judge...
        self.assertEqual(fade.active_cells({"longshot": cells}), [])  # ...but a worst case under 0 never trades
        longshot.merge_cells(cells, [bad], 12.0, "d6")
        self.assertEqual(cells[0]["last_tested"], "d5")  # another decision time: not comparable


# ---------- promotion ----------

PCELL = "Climate and Weather:0.10-0.20"


def save_cell(status, cid=PCELL, **kw):
    longshot.save_cells([dict({"id": cid, "category": WEATHER, "lo": 0.10, "hi": 0.20, "hours_before_close": 6.0,
                               "roi": 0.05, "roi_lo": 0.02, "trades": 300, "events": 300, "groups": 300,
                               "series": ["KXHIGHNY"], "status": status}, **kw)])


def paper_trade(i, won, cell=PCELL, group=None, p=0.90, count=5):
    sid = db.insert("signals", ts=T0 + i, strategy="longshot_fade", event=f"E{i}", status="accepted",
                    meta={"cell": cell, "group": group or f"KXHIGHNY:{i}", "paper_only": True})
    cost = count * p + fee(count, p)
    db.insert("paper", ts=T0 + i, signal_id=sid, strategy="longshot_fade", event=f"E{i}", ticker=f"T{i}",
              side="no", count=count, price=p, cost=cost, settled=1, result="no" if won else "yes",
              pnl=count * won - cost, settled_ts=T0 + i)


class MemoryTests(TempHome):
    def test_a_cell_retired_for_losing_stays_retired_without_its_file(self):
        # A rebuild from backups keeps the db but not research/*.json. The next research
        # run must not bring back a cell that lost money as a fresh candidate.
        import os
        from kal.research import discover
        longshot.save_cells([{"id": CELL, "category": WEATHER, "lo": 0.05, "hi": 0.10,
                              "hours_before_close": 6, "status": "retired", "retired_by": "results",
                              "looks": 3, "rank": 2}])
        os.remove(os.path.join(discover.research_dir(), longshot.FILE))
        cells = longshot.load_cells()
        row = {"category": WEATHER, "lo": 0.05, "hi": 0.10, "trades": 500, "events": 120, "groups": 120,
               "roi": 0.05, "roi_lo": 0.01, "win_rate": 0.99, "series": ["KXHIGHNY"]}
        changes = longshot.merge_cells(cells, [row], 6, "2026-10-25")
        self.assertEqual(changes["new"], [])
        self.assertEqual(cells[0]["status"], "retired")
        self.assertEqual((cells[0]["looks"], cells[0]["rank"]), (3, 2))   # never reset


class PromoteTests(TempHome):
    def cell(self):
        return longshot.load_cells()[0]

    def test_candidate_goes_live_only_with_enough_paper_wins(self):
        save_cell("candidate")
        for i in range(20):
            paper_trade(i, True)
        self.assertEqual(longshot.apply_longshot(cfg()), [])
        self.assertEqual(self.cell()["status"], "candidate")  # 20 is too few
        for i in range(20, 120):
            paper_trade(i, True)
        notes = longshot.apply_longshot(cfg(auto_promote=False))
        self.assertIn("Auto promote is off", notes[0])
        self.assertEqual(self.cell()["status"], "candidate")
        notes = longshot.apply_longshot(cfg())
        self.assertIn("won on paper", notes[0])
        self.assertIn("Set longshot_fade to live", notes[0])  # its default mode is paper
        self.assertEqual((self.cell()["status"], self.cell()["looks"]), ("live", 2))
        live_cfg = cfg(strategies={"longshot_fade": {"mode": "live"}})
        save_cell("candidate", looks=0)
        self.assertIn("It now uses real money", longshot.apply_longshot(live_cfg)[0])

    def test_trades_in_few_groups_dont_promote(self):
        save_cell("candidate")
        for i in range(120):  # 120 markets, but one series on 6 days: they win or lose together
            paper_trade(i, True, group=f"KXHIGHNY:day{i % 6}")
        longshot.apply_longshot(cfg())
        self.assertEqual(self.cell()["status"], "candidate")
        self.assertEqual(self.cell()["results"]["events"], 6)

    def test_paper_losses_keep_a_candidate_on_paper(self):
        save_cell("candidate")
        for i in range(120):
            paper_trade(i, i % 8 != 0)  # a NO at 90c that wins 7 times in 8 loses money
        longshot.apply_longshot(cfg())
        self.assertEqual(self.cell()["status"], "candidate")

    def test_other_cells_and_strategies_dont_count(self):
        save_cell("candidate")
        for i in range(120):
            paper_trade(i, True, cell="Sports:0.10-0.20")
        sid = db.insert("signals", strategy="favorites", event="X", meta={"cell": PCELL}, status="accepted")
        for i in range(120):
            db.insert("paper", ts=T0 + i, signal_id=sid, strategy="favorites", event=f"X{i}", ticker=f"X{i}",
                      side="no", count=5, price=0.9, cost=4.54, settled=1, result="no", pnl=0.46, settled_ts=T0)
        longshot.apply_longshot(cfg())
        self.assertEqual(self.cell()["status"], "candidate")
        self.assertNotIn("results", self.cell())

    def test_live_cell_retired_when_recent_trades_lose(self):
        save_cell("live")
        for i in range(40):
            paper_trade(i, True)
        self.assertEqual(longshot.apply_longshot(cfg(auto_promote=False)), [])
        self.assertEqual(self.cell()["status"], "live")
        # Real losses (from the ledger's settlements) count too, even with auto_promote off.
        for i in range(40, 46):
            sid = db.insert("signals", ts=T0 + i, strategy="longshot_fade", event=f"E{i}",
                            meta={"cell": PCELL, "group": f"KXHIGHNY:{i}"})
            db.insert("orders", ts=T0 + i, signal_id=sid, strategy="longshot_fade", ticker=f"L{i}", side="no",
                      count=5, price=0.90, filled=5, status="executed")
            db.insert("settlements", ticker=f"L{i}", settled_ts=T0 + i, result="yes", revenue=0, cost=4.54,
                      pnl=-4.54)
        notes = longshot.apply_longshot(cfg(auto_promote=False))
        self.assertIn("stopped", notes[0])
        self.assertEqual((self.cell()["status"], self.cell()["retired_by"]), ("retired", "results"))
        # The backtest can't bring it back.
        cells = longshot.load_cells()
        longshot.merge_cells(cells, [{"category": WEATHER, "lo": 0.10, "hi": 0.20, "trades": 300, "events": 300,
                                      "groups": 300, "roi": 0.1, "roi_lo": 0.05, "series": ["KXHIGHNY"]}],
                             6.0, "later")
        self.assertEqual(cells[0]["status"], "retired")

    def test_no_cells_no_work(self):
        self.assertEqual(longshot.apply_longshot(cfg()), [])


# ---------- the strategy ----------

def cell(status="candidate", **kw):
    return dict({"id": CELL, "category": WEATHER, "lo": 0.05, "hi": 0.10, "hours_before_close": 6.0, "roi": 0.05,
                 "roi_lo": 0.02, "trades": 300, "events": 300, "groups": 300, "series": ["KXHIGHNY"],
                 "status": status}, **kw)


def open_event(c, et, series="KXHIGHNY", hours=5.5, longshots=((0.07, 0.08),), category=WEATHER, title="NYC high"):
    """An open event closing `hours` from now: a 90c favorite plus longshot markets, each
    with an order book 100 contracts deep. Returns its markets (favorite first)."""
    close = iso(time.time() + hours * 3600)
    markets = []
    for i, (bid, ask) in enumerate(((0.90, 0.91),) + tuple(longshots)):
        tk = f"{et}-M{i}"
        m = {"ticker": tk, "event_ticker": et, "title": f"{title} {i}", "status": "active", "close_time": close,
             "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}", "volume_24h_fp": "500.00",
             "previous_yes_bid_dollars": f"{bid:.4f}", "rules_primary": "Pays if the high is in range."}
        markets.append(m)
        c.books[tk] = {"yes": [(bid, 100.0)], "no": [(round(1 - ask, 4), 100.0)]}
        c.market_info[tk] = m
    c.event_list.append({"event_ticker": et, "series_ticker": series, "title": title, "category": category,
                         "markets": markets})
    return markets


def ctx_for(c, cells, bankroll=1000.0, budget=1000.0, held=(), open_exposure=0.0, **cfg_kw):
    return Context(c, cfg(**cfg_kw), "paper", budget, bankroll, set(held), {"longshot": cells}, {},
                   open_exposure=open_exposure)


class StrategyTests(TempHome):
    def setUp(self):
        super().setUp()
        self.c = FakeKalshi()
        self.m = open_event(self.c, "KXHIGHNY-1")[1]  # the 7c longshot

    def scan(self, cells=None, **kw):
        return fade.STRATEGY.scan(ctx_for(self.c, [cell()] if cells is None else cells, **kw))

    def test_buys_no_at_the_no_ask_sized_cautiously(self):
        sigs = self.scan()
        self.assertEqual(len(sigs), 1)
        sig, leg = sigs[0], sigs[0].legs[0]
        self.assertEqual((leg.ticker, leg.side, leg.price), ("KXHIGHNY-1-M1", "no", 0.93))  # 1 - the 7c YES bid
        p = fade.cost_each(0.93) * 1.02  # cautious: pays exactly the backtest's worst case
        self.assertEqual(leg.count, sizing.contracts(p, 0.93, 1000.0, 0.25, 5.0, open_exposure=0.0, max_exposure=0.5))
        self.assertEqual(leg.count, 5)
        self.assertLessEqual(sig.cost, 5.0)
        self.assertAlmostEqual(sig.exp_profit, 5 * p - sig.cost)
        self.assertGreater(sig.exp_profit, 0)
        self.assertFalse(sig.guaranteed)
        self.assertEqual(sig.event, "KXHIGHNY-1")
        end = ts(self.m["close_time"])  # no can_close_early flag: a fixed close is the scheduled end
        self.assertEqual(sig.meta, {"cell": CELL, "group": fade.group_of("KXHIGHNY", end),
                                    "closes": end, "paper_only": True})
        self.assertFalse(self.scan([cell("live")])[0].meta["paper_only"])
        self.assertEqual(fade.STRATEGY.kind, "statistical")

    def test_loss_caps(self):
        sig = self.scan(bankroll=100.0)[0]  # 2% of capital: $2
        self.assertLessEqual(sig.cost, 2.0)
        self.assertEqual(sig.legs[0].count, 2)
        self.assertLessEqual(self.scan(max_per_trade=1.0)[0].cost, 1.0)
        big = self.scan(bankroll=1e6, budget=1e6, max_per_trade=500.0, max_per_event=500.0)[0]
        self.assertLessEqual(big.cost, 5.0)  # never over $5, whatever the settings
        self.assertLessEqual(self.scan(open_exposure=498.0)[0].cost, 2.0)  # max_exposure leaves $2
        self.assertEqual(self.scan(open_exposure=500.0), [])
        self.assertEqual(self.scan(budget=0.5), [])

    def test_thin_book_caps_the_size(self):
        self.c.books[self.m["ticker"]]["yes"] = [(0.07, 3.0)]
        self.assertEqual(self.scan()[0].legs[0].count, 3)
        # One contract pays a whole cent of fee: then even the cautious edge is gone.
        self.c.books[self.m["ticker"]]["yes"] = [(0.07, 1.0)]
        self.assertEqual(self.scan([cell(roi_lo=0.005)]), [])

    def test_guards(self):
        self.assertEqual(len(self.scan()), 1)
        book = lambda m: self.c.books[m["ticker"]]  # noqa: E731
        cases = {
            "wide listing": lambda m: m.update(yes_ask_dollars="0.1000"),
            "wide book": lambda m: book(m).update(no=[(0.88, 100.0)]),
            "no YES ask": lambda m: book(m).update(no=[]),
            "no YES bid": lambda m: book(m).update(yes=[]),
            "thin market": lambda m: m.update(volume_24h_fp="50.00"),
            "fair-price rule": lambda m: m.update(rules_primary="If the leader leaves office, settles at the "
                                                                "last traded price."),
            "not binary": lambda m: m.update(market_type="scalar"),
            "closed": lambda m: m.update(status="closed"),
            "YES bid jumped": lambda m: m.update(previous_yes_bid_dollars="0.0300"),
            "out of the range": lambda m: (m.update(yes_bid_dollars="0.1200", yes_ask_dollars="0.1300"),
                                           book(m).update(yes=[(0.12, 100.0)], no=[(0.87, 100.0)])),
            "book left the range": lambda m: book(m).update(yes=[(0.12, 100.0)], no=[(0.87, 100.0)]),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.c = FakeKalshi()
                change(open_event(self.c, "KXHIGHNY-1")[1])
                self.assertEqual(self.scan(), [])

    def test_only_near_the_tested_time_before_the_scheduled_close(self):
        for hours, n in ((5.5, 1), (5.95, 1), (6.5, 0), (4.5, 0), (0.2, 0)):
            self.c = FakeKalshi()
            open_event(self.c, "KXHIGHNY-1", hours=hours)
            self.assertEqual(len(self.scan()), n, hours)
        # A sibling that closes later shows the event's real close: still too far away.
        self.c = FakeKalshi()
        open_event(self.c, "KXHIGHNY-1")[0]["close_time"] = iso(time.time() + 30 * H)
        self.assertEqual(self.scan(), [])
        # A lone market that can close early: its close may be its outcome. Never tested.
        self.c = FakeKalshi()
        m = open_event(self.c, "KXHIGHNY-1")[1]
        self.c.event_list[0]["markets"] = [m]
        self.assertEqual(len(self.scan()), 1)
        m["can_close_early"] = True
        self.assertEqual(self.scan(), [])
        m["can_close_early"], m["close_time"] = False, "soon"  # unreadable close: never guess
        self.assertEqual(self.scan(), [])

    def test_games_trade_before_their_listed_end_not_their_deadline(self):
        # A real game market: close_time is a deadline ~45h after the game, and it really
        # closes when the game ends. The backtest decided before the listed end, so here too.
        def game(end, close):
            for m in self.c.event_list[0]["markets"]:
                m.update({"can_close_early": True, "close_time": iso(time.time() + close * H),
                          "occurrence_datetime": iso(time.time() + end * H),
                          "expected_expiration_time": iso(time.time() + end * H)})
        game(end=5.5, close=50.5)
        self.assertEqual(len(self.scan()), 1)
        game(end=50, close=5.5)
        self.assertEqual(self.scan(), [])  # it closes soon, but isn't listed to end for 50h

    def test_weather_trades_before_its_strike_date(self):
        # Real weather: trading stops at the event's strike_date; its other times are later.
        ev = self.c.event_list[0]
        for m in ev["markets"]:
            m.update({"can_close_early": True, "occurrence_datetime": iso(time.time() + 14.5 * H),
                      "expected_expiration_time": iso(time.time() + 19.5 * H)})
        self.assertEqual(self.scan(), [])
        ev["strike_date"] = ev["markets"][0]["close_time"]  # 5.5h from now
        self.assertEqual(len(self.scan()), 1)

    def test_held_untested_and_excluded_events(self):
        self.assertEqual(self.scan(held={"KXHIGHNY-1-M1"}), [])
        self.assertEqual(self.scan(held={"KXHIGHNY-1-M0"}), [])  # anything held in the event: one bet each
        self.assertEqual(self.scan([cell(series=["KXHIGHCHI"])]), [])  # research never tested this series
        self.assertEqual(self.scan([cell(category="Sports", id="Sports:0.05-0.10", series=["KXNBA"])]), [])
        for series, category, title in (("KXCPI", "Economics", "CPI in October"),
                                        ("KXFED", "Economics", "Fed rate in December"),
                                        ("KXMVESPORTS", "Sports", "Combo")):
            with self.subTest(series):
                c = FakeKalshi()
                open_event(c, f"{series}-1", series=series, category=category, title=title)
                ctx = ctx_for(c, [cell(category=category, series=[series])])
                self.assertEqual(fade.STRATEGY.scan(ctx), [])  # even if a cell lists them

    def test_one_bet_per_event_and_a_cap_per_series_day(self):
        self.c = FakeKalshi()
        open_event(self.c, "KXHIGHNY-1", longshots=((0.07, 0.08), (0.06, 0.07), (0.08, 0.09)))
        self.assertEqual(len(self.scan()), 1)
        # Eight events in one series closing the same day: at most 10% of capital together.
        self.c = FakeKalshi()
        for i in range(8):
            open_event(self.c, f"KXHIGHNY-{i}")
        sigs = self.scan(bankroll=100.0)
        self.assertEqual(len(sigs), 5)
        self.assertLessEqual(sum(s.cost for s in sigs), 10.0)
        self.assertEqual(len({s.meta["group"] for s in sigs}), 1)
        # What's already held there counts as a full $5 bet each.
        sigs = self.scan(bankroll=100.0, held={"KXHIGHNY-7-M1"})
        self.assertTrue(sigs)
        self.assertLessEqual(sum(s.cost for s in sigs) + fade.MAX_PER_TRADE, 10.0)
        # Another series is another group, with room of its own.
        for i in range(8):
            open_event(self.c, f"KXHIGHCHI-{i}", series="KXHIGHCHI")
        sigs = self.scan([cell(series=["KXHIGHNY", "KXHIGHCHI"])], bankroll=100.0)
        self.assertEqual(len(sigs), 10)
        self.assertEqual(len({s.meta["group"] for s in sigs}), 2)

    def test_recheck_before_a_live_order(self):
        ctx = ctx_for(self.c, [cell("live")])
        sig = fade.STRATEGY.scan(ctx)[0]
        again = fade.STRATEGY.recheck(ctx, sig)
        self.assertEqual((again.legs[0].price, again.legs[0].count), (0.93, 5))
        self.assertIsNone(fade.STRATEGY.recheck(ctx_for(self.c, [cell()]), sig))  # not live any more
        self.c.books[self.m["ticker"]]["yes"] = [(0.07, 3.0)]
        self.assertEqual(fade.STRATEGY.recheck(ctx, sig).legs[0].count, 3)  # smaller, never bigger
        self.c.books[self.m["ticker"]] = {"yes": [(0.12, 100.0)], "no": [(0.87, 100.0)]}
        self.assertIsNone(fade.STRATEGY.recheck(ctx, sig))  # the price ran out of the range
        self.c.books[self.m["ticker"]] = {"yes": [(0.07, 100.0)], "no": [(0.92, 100.0)]}
        self.assertIsNotNone(fade.STRATEGY.recheck(ctx, sig))
        self.m["status"] = "closed"
        self.assertIsNone(fade.STRATEGY.recheck(ctx, sig))
        self.m["status"] = "active"
        sig.meta["closes"] = time.time() + 20 * H  # out of the window
        self.assertIsNone(fade.STRATEGY.recheck(ctx, sig))

    def test_proven_needs_a_live_cell(self):
        def proven(cells):
            return fade.STRATEGY.proven(ctx_for(self.c, cells))[0]
        self.assertFalse(proven([cell()]))
        self.assertTrue(proven([cell("live")]))
        self.assertFalse(proven([cell("retired")]))
        self.assertFalse(proven([cell("live", roi_lo=-0.01)]))
        self.assertFalse(proven([]))

    def test_bad_cells_are_ignored(self):
        for bad in (cell("retired"), cell(roi_lo=None), cell(roi_lo=0), cell(roi_lo=float("nan")), cell(series=[]),
                    cell(lo=0.0), cell(hi=0.5), cell(hours_before_close="soon"), cell(category=""), {"id": "x"}, "junk"):
            with self.subTest(bad):
                self.assertEqual(self.scan([bad]), [])
        ctx = Context(self.c, cfg(), "paper", 1000, 1000, set(), {}, {})
        self.assertEqual(fade.STRATEGY.scan(ctx), [])

    def test_no_cells_means_no_api_calls(self):
        self.c.events = lambda **p: self.fail("no cells: no need to list events")
        self.assertEqual(self.scan([]), [])


class EngineTests(TempHome):
    def setUp(self):
        super().setUp()
        self.c = FakeKalshi(balance=300.0)
        open_event(self.c, "KXHIGHNY-1")
        config.update({"enabled": True, "budget": 300,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        orders_proven()

    def run_engine(self, status, mode):
        config.update({"strategies": {"longshot_fade": {"mode": mode}}})
        longshot.save_cells([cell(status)])
        with mock.patch.object(engine, "load_all", return_value={"longshot_fade": fade.STRATEGY}):
            return engine.run_once(self.c)

    def test_candidate_trades_on_paper_only(self):
        s = self.run_engine("candidate", "live")
        self.assertTrue(s["ok"], s)
        self.assertEqual(self.c.orders, [])
        self.assertEqual(s["paper_trades"], 1)
        self.assertTrue(any("paper instead of live" in n for n in s["notes"]))
        row = db.query("SELECT side, price, count FROM paper")[0]
        self.assertEqual((row["side"], row["price"], row["count"]), ("no", 0.93, 5))

    def test_live_cell_trades_for_real(self):
        s = self.run_engine("live", "live")
        self.assertTrue(s["ok"], s)
        self.assertEqual(s["live_trades"], 1)
        self.assertEqual(self.c.orders, [("KXHIGHNY-1-M1", "no", 5, 0.93)])
        row = db.query("SELECT mode, meta, cost FROM signals")[0]
        self.assertEqual(row["mode"], "live")
        self.assertEqual(json.loads(row["meta"])["cell"], CELL)
        self.assertLessEqual(row["cost"], 0.02 * 300)

    def test_live_cell_stays_on_paper_in_paper_mode(self):
        s = self.run_engine("live", "paper")
        self.assertEqual((self.c.orders, s["paper_trades"]), ([], 1))

    def test_research_then_scan(self):
        add_series(self.c, "KXHIGHNY", WEATHER, 200, hit=0.0)
        longshot.run(self.c, cfg())
        ctx = Context(self.c, cfg(), "paper", 1000, 1000, set(), engine.research_outputs(), {})
        sigs = fade.STRATEGY.scan(ctx)
        self.assertEqual([s.legs[0].ticker for s in sigs], ["KXHIGHNY-1-M1"])
        self.assertTrue(sigs[0].meta["paper_only"])


if __name__ == "__main__":
    unittest.main()


class DailyResearchTests(TempHome):
    def test_daily_research_tests_and_promotes_longshot_cells_too(self):
        from unittest import mock
        from kal import research
        with mock.patch.object(research.longshot, "run", return_value={"ok": True, "tested": []}) as run, \
                mock.patch.object(research.longshot, "apply_longshot", return_value=["a longshot note"]) as apply:
            s = research.run_all(FakeKalshi(), quick=True)
        run.assert_called_once()
        self.assertTrue(run.call_args.kwargs["quick"])
        apply.assert_called_once()
        self.assertEqual(s["longshot"], {"ok": True, "tested": []})
        self.assertIn("a longshot note", s["changes"])
