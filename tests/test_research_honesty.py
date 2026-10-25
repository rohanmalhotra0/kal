"""Research that can't fool itself: python -m unittest tests.test_research_honesty

One class per review finding: hourly luck, demo data on prod, the news-time anchor, the
promotion budget over time, rounded fees, retirements that survive a rebuild, research
memory, and research that daily restarts kept killing."""
import math
import os
import random
import shutil
import signal
import statistics
import time
from datetime import datetime, timezone
from unittest import mock

from kal import config, db, engine, report, scheduler
from kal.client import fee, fee_per_contract
from kal.dashboard import app as dash
from kal.research import backtest, discover, promote
from kal.strategies import favorites
from kal.strategies.base import Context
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven

T0 = 1_760_000_000          # 2025-10-09 08:53:20 UTC
H = 3600
DAY = 86400
MIDNIGHT = 1_760_054_400    # 2025-10-10 00:00 UTC


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def day_of(t):
    return datetime.fromtimestamp(t, timezone.utc).date().isoformat()


def cfg(**kw):
    return dict(config.DEFAULTS, **kw)


def paper_win(strategy, i, ts, event, meta, p=0.85, count=10, won=True):
    """One settled paper trade, matched to its signal (and so to its edge)."""
    sid = db.insert("signals", ts=ts, strategy=strategy, event=event, meta=meta, status="accepted")
    cost = count * p + fee(count, p)
    db.insert("paper", ts=ts, signal_id=sid, strategy=strategy, event=event, ticker=f"{event}-T{i}", side="yes",
              count=count, price=p, cost=cost, settled=1, result="yes" if won else "no",
              pnl=count * won - cost, settled_ts=ts + 6 * H)


def edge(eid, status="candidate", **kw):
    series, side, rng = eid.split(":")
    lo, hi = (float(x) for x in rng.split("-"))
    return dict({"id": eid, "series": series, "side": side, "lo": lo, "hi": hi, "hours_before_close": 6.0,
                 "roi": 0.05, "roi_lo": 0.02, "trades": 300, "events": 300, "status": status}, **kw)


# ---------- 1. hourly events are not independent ----------

class HourlyLuckTests(TempHome):
    EDGE = "KXBTCD:yes:0.80-0.90"

    def hourly_record(self, n, with_group=True):
        """n winning paper trades on an hourly series, one per hourly event: two days or so."""
        for i in range(n):
            ts = T0 + i * H
            meta = {"edge": self.EDGE, "paper_only": True}
            if with_group:
                meta["group"] = f"KXBTCD:{day_of(ts + 6 * H)}"
            paper_win("favorites", i, ts, f"KXBTCD-H{i}", meta)

    def test_hourly_paper_wins_count_once_a_day(self):
        discover.save_edges([edge(self.EDGE)])
        self.hourly_record(48)
        promote.apply(cfg())
        e = discover.load_edges()[0]
        self.assertEqual(e["status"], "candidate")  # 48 wins, but only 3 days of them
        self.assertEqual(e["results"]["events"], 3)

    def test_older_trades_without_a_group_count_by_day_too(self):
        discover.save_edges([edge(self.EDGE)])
        self.hourly_record(48, with_group=False)  # signals made before this fix
        promote.apply(cfg())
        e = discover.load_edges()[0]
        self.assertEqual(e["status"], "candidate")
        self.assertLessEqual(e["results"]["events"], 3)

    def test_backtest_counts_an_hourly_series_by_day(self):
        obs = [{"series": "KXBTCD", "event": f"KXBTCD-H{i}", "ticker": f"KXBTCD-H{i}-T", "close": T0 + i * H,
                "anchor": T0 + i * H, "decided": T0 + i * H - 6 * H, "yes_ask": 0.85, "yes_bid": 0.84,
                "result": "yes"} for i in range(96)]  # 4 days of hourly events: every favorite won
        row = [r for r in backtest.calibration(obs, by_side=True) if r["side"] == "yes" and r["lo"] == 0.80][0]
        self.assertEqual(row["trades"], 96)
        self.assertFalse(backtest.is_edge(row))  # 5 days is too few to tell skill from luck

    def test_one_series_day_has_one_cap(self):
        """Three earlier hourly bets are still open on the same price path: no room for a fourth."""
        now = MIDNIGHT + 2 * H
        c = FakeKalshi()

        def market(h, held_hours):
            m = {"ticker": f"KXBTCD-H{h}-T", "event_ticker": f"KXBTCD-H{h}", "title": "BTC", "status": "active",
                 "yes_ask_dollars": "0.8500", "yes_bid_dollars": "0.8400", "no_ask_dollars": "0.1600",
                 "no_bid_dollars": "0.1500", "volume_24h_fp": "5000.00", "close_time": iso(now + held_hours * H)}
            c.market_list.append(m)
            c.market_info[m["ticker"]] = m
            c.books[m["ticker"]] = {"yes": [(0.84, 500.0)], "no": [(0.15, 500.0)]}
            return m["ticker"]
        held = {market(5, 2.5), market(6, 3.5), market(7, 4.5)}
        market(8, 5.5)  # the one at the tested time
        research = {"edges": [edge(self.EDGE)]}

        def scan(held_now):
            ctx = Context(c, cfg(), "paper", 1000.0, 300.0, set(held_now), research, {})
            with mock.patch.object(favorites.time, "time", return_value=now):
                return favorites.STRATEGY.scan(ctx)
        self.assertEqual(len(scan(set())), 1)
        self.assertEqual(scan(held), [])  # 3 x $10 already rides on today's BTC path: 10% of $300
        two = scan(set(list(held)[:2]))
        self.assertEqual(len(two), 1)
        self.assertLessEqual(two[0].cost, 10.0 + 1e-9)

    def test_recheck_never_grows_a_bet_past_the_cap_it_was_sized_for(self):
        c = FakeKalshi()
        m = {"ticker": "KXBTCD-H8-T", "event_ticker": "KXBTCD-H8", "title": "BTC", "status": "active",
             "yes_ask_dollars": "0.8500", "yes_bid_dollars": "0.8400", "volume_24h_fp": "5000.00",
             "close_time": iso(time.time() + 5.5 * H)}
        c.market_list, c.market_info[m["ticker"]] = [m], m
        c.books[m["ticker"]] = {"yes": [(0.84, 500.0)], "no": [(0.15, 3.0)]}  # 3 contracts for sale
        ctx = Context(c, cfg(), "live", 1000.0, 300.0, set(), {"edges": [edge(self.EDGE, "live")]}, {})
        sig = favorites.STRATEGY.scan(ctx)[0]
        self.assertEqual(sig.legs[0].count, 3)
        c.books[m["ticker"]] = {"yes": [(0.84, 500.0)], "no": [(0.15, 500.0)]}  # now deep
        again = favorites.STRATEGY.recheck(ctx, sig)
        self.assertEqual(again.legs[0].count, 3)
        self.assertLessEqual(again.cost, sig.cost + 1e-9)


# ---------- 2. demo practice must not touch prod's research ----------

def one_settled_market(price):
    c = FakeKalshi(can_trade=False)
    c.settled["KXS"] = [{"ticker": "KXS-1-A", "event_ticker": "KXS-1", "open_time": iso(T0),
                         "close_time": iso(T0 + DAY), "status": "finalized", "result": "yes",
                         "settlement_value_dollars": "1.0000", "volume_fp": "500.00"}]
    c.candle_data["KXS-1-A"] = [{"end_period_ts": T0 + DAY - 7 * H, "yes_bid": {"close": f"{price - 0.01:.4f}"},
                                 "yes_ask": {"close": f"{price:.4f}"}}]
    return c


class ExchangeTests(TempHome):
    def test_demo_prices_never_reach_the_real_backtest(self):
        config.update({"env": "demo"})
        self.assertEqual(backtest.load_observations(one_settled_market(0.30), "KXS", 6)[0]["yes_ask"], 0.30)
        config.update({"env": "prod"})
        self.assertEqual(backtest.load_observations(one_settled_market(0.90), "KXS", 6)[0]["yes_ask"], 0.90)

    def test_demo_research_is_invisible_on_prod(self):
        config.update({"env": "demo"})
        discover.save_edges([edge("KXS:no:0.90-0.95", "live")])
        discover.save_words({"series": ["KXSAY"], "promoted": True, "roi_lo": 0.1})
        self.assertEqual(set(engine.research_outputs()), {"edges", "words"})
        self.assertEqual(report._research()["edges"], {"live": 1})
        self.assertEqual(dash.research_summary()["edges"]["total"], 1)
        config.update({"env": "prod"})
        self.assertEqual(engine.research_outputs(), {})
        self.assertEqual(report._research()["edges"], {})
        self.assertIsNone(dash.research_summary()["edges"])
        self.assertIsNone(dash.research_detail(config.load())["words"])
        # Prod's own files stay where they always were.
        discover.save_edges([edge("KXS:no:0.90-0.95")])
        self.assertTrue(os.path.exists(os.path.join(self.home, "research", "edges.json")))

    def test_demo_paper_wins_never_promote(self):
        eid = "KXS:no:0.90-0.95"
        for env, want in (("demo", "candidate"), ("prod", "live")):
            config.update({"env": env})
            discover.save_edges([edge(eid)])
            for i in range(60):  # 60 wins on 60 days: proof enough on the real exchange
                ts = T0 + i * DAY
                paper_win("favorites", i, ts, f"KXS-{i}", {"edge": eid, "group": f"KXS:{day_of(ts)}"}, p=0.90)
            notes = promote.apply(config.load())
            self.assertEqual(discover.load_edges()[0]["status"], want, (env, notes))

    def test_a_live_edge_on_demo_never_trades_real_money(self):
        eid = "KXS:no:0.90-0.95"
        config.update({"env": "demo"})
        discover.save_edges([edge(eid, "live", roi_lo=0.01)])
        config.update({"env": "prod", "enabled": True, "budget": 300,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"favorites": {"mode": "live"}}})
        orders_proven("prod")
        c = FakeKalshi(balance=300)
        m = {"ticker": "KXS-99-A", "event_ticker": "KXS-99", "series_ticker": "KXS", "status": "active",
             "close_time": iso(time.time() + 5.5 * H), "volume_24h_fp": "5000.00", "yes_bid_dollars": "0.0700",
             "yes_ask_dollars": "0.0800", "no_bid_dollars": "0.9200", "no_ask_dollars": "0.9300"}
        c.market_list, c.market_info["KXS-99-A"] = [m], m
        c.books["KXS-99-A"] = {"yes": [(0.07, 500.0)], "no": [(0.92, 500.0)]}
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual((s["live_trades"], c.orders), (0, []))


# ---------- 3. the decision time must not come from the news ----------

def by_date_series(c, events=150, seed=3):
    """'Will X happen by day 1, 2, 3?' events, priced fairly. X comes at a random time (about
    once in 4 days), and every YES market closes early, on the news. Like the reviewers'
    sim_anchor_lookahead.py."""
    rng = random.Random(seed)
    rate = 1 / (4 * DAY)
    markets = []
    for e in range(events):
        start = T0 + e * 5 * DAY
        news = start + rng.expovariate(rate)
        for days in (1, 2, 3):
            deadline = start + days * DAY
            yes = news <= deadline
            close = news if yes else deadline
            tk = f"KXWHEN-{e}-D{days}"
            markets.append({"ticker": tk, "event_ticker": f"KXWHEN-{e}", "open_time": iso(start),
                            "close_time": iso(close), "status": "finalized", "result": "yes" if yes else "no",
                            "volume_fp": "5000.00", "can_close_early": True})
            candles = []
            for h in range(int((close - start) // H)):
                end = start + (h + 1) * H
                p = 1 - math.exp(-rate * max(0.0, deadline - end))
                bid = min(0.98, max(0.01, round(p - 0.005, 2)))
                candles.append({"end_period_ts": end, "yes_bid": {"close": f"{bid:.4f}"},
                                "yes_ask": {"close": f"{bid + 0.01:.4f}"}})
            c.candle_data[tk] = candles
    c.settled["KXWHEN"] = markets


class AnchorTests(TempHome):
    def test_news_time_is_not_the_decision_time(self):
        c = FakeKalshi(can_trade=False)
        by_date_series(c)
        obs = backtest.load_observations(c, "KXWHEN", 6, max_markets=10_000)
        rows = backtest.calibration(obs, max_spread=0.02, by_side=True)
        self.assertEqual([r for r in rows if backtest.is_edge(r)], [])  # fair prices: nothing to find
        self.assertFalse([r for r in rows if r["side"] == "yes" and r["lo"] < 0.5 and r["win_rate"] == 1])

    def test_only_markets_that_cant_close_early_set_the_schedule(self):
        sched = T0 + 100 * H
        fixed = {"ticker": "EV-A", "event_ticker": "EV", "close_time": iso(sched), "can_close_early": False}
        early = {"ticker": "EV-B", "event_ticker": "EV", "close_time": iso(sched + 5 * H), "can_close_early": True}
        self.assertEqual(backtest.event_anchors([fixed, early]), {"EV": sched})
        self.assertEqual(backtest.event_anchors([early]), {})  # nothing shows the schedule: skip it


# ---------- 4. one false-promotion budget for every candidate, ever ----------

class PromotionBudgetTests(TempHome):
    def test_total_false_promotion_chance_stays_under_5_percent(self):
        tail = statistics.NormalDist().cdf
        total = sum(1 - tail(promote.z_look(k, rank=i)) for i in range(1, 201) for k in range(1, 201))
        self.assertLess(total, promote.ALPHA)
        self.assertLess(promote.z_look(1, rank=1), promote.z_look(1, rank=2))
        # Never easier than before for anyone: at least the old same-day split.
        for tries in (1, 2, 5):
            self.assertGreaterEqual(promote.z_look(3, tries, rank=1), promote.z_look(3, tries))

    def test_candidates_judged_on_different_days_share_the_budget(self):
        a, b = "KXA:yes:0.80-0.90", "KXB:yes:0.80-0.90"
        for eid, start in ((a, 0), (b, 100)):
            for i in range(40):
                ts = T0 + (start + i) * DAY
                paper_win("favorites", start + i, ts, f"{eid[:3]}-{i}", {"edge": eid, "group": f"X:{day_of(ts)}"})
        discover.save_edges([edge(a), edge(b, "retired", retired_by="backtest")])
        promote.apply(cfg(auto_promote=False))      # day 1: only A is judged
        edges = {e["id"]: e for e in discover.load_edges()}
        edges[a]["status"], edges[b]["status"] = "retired", "candidate"
        discover.save_edges(list(edges.values()))
        promote.apply(cfg(auto_promote=False))      # day 2: only B, judged alone
        edges = {e["id"]: e for e in discover.load_edges()}
        self.assertEqual((edges[a]["looks"], edges[b]["looks"]), (1, 1))
        # Same record, same look, but B is the second idea ever judged: it needs more proof.
        self.assertLess(edges[b]["results"]["roi_lo"], edges[a]["results"]["roi_lo"])


# ---------- 5. fees are rounded up per order ----------

class FeeTests(TempHome):
    def test_backtest_trade_is_a_real_size_order_with_its_real_fee(self):
        _, cost, pnl, _ = backtest.trade("E", 0.93, True)
        self.assertAlmostEqual(cost, 10 * 0.93 + fee(10, 0.93))  # a $10 bet: 10 contracts
        self.assertAlmostEqual(pnl, 10 - cost)
        self.assertGreater(cost / 10, 0.93 + fee_per_contract(0.93))

    def test_favorites_skips_a_bet_the_rounded_fee_turns_into_a_loser(self):
        c = FakeKalshi()
        m = {"ticker": "FAV-X-1", "event_ticker": "FAV-X", "title": "T", "status": "active",
             "yes_ask_dollars": "0.9300", "yes_bid_dollars": "0.9200", "volume_24h_fp": "500.00",
             "close_time": iso(time.time() + 5.5 * H)}
        c.market_list, c.market_info["FAV-X-1"] = [m], m
        c.books["FAV-X-1"] = {"yes": [(0.92, 1.0)], "no": [(0.07, 1.0)]}  # one contract: a whole cent of fee
        e = edge("FAV:yes:0.90-0.95", roi_lo=0.001)
        ctx = Context(c, cfg(), "paper", 1000.0, 1000.0, set(), {"edges": [e]}, {})
        self.assertEqual(favorites.STRATEGY.scan(ctx), [])


# ---------- 6. a retirement for losing money survives a rebuild ----------

class RebuildTests(TempHome):
    def test_a_retired_loser_stays_retired_without_its_file(self):
        eid = "KXS:yes:0.80-0.90"
        discover.save_edges([edge(eid, "retired", retired_by="results", looks=12, roi_lo=0.005)])
        self.assertEqual(promote.apply(cfg()), [])
        shutil.rmtree(os.path.join(self.home, "research"))   # a new server, the db from a backup
        row = {"lo": 0.80, "hi": 0.90, "side": "yes", "roi": 0.02, "roi_lo": 0.004, "trades": 200, "events": 60}
        edges = []
        discover.merge_edges(edges, "KXS", [row], 6.0, "2026-10-26")
        discover.save_edges(edges)
        for i in range(60):
            ts = T0 + i * DAY
            paper_win("favorites", i, ts, f"KXS-{i}", {"edge": eid, "group": f"KXS:{day_of(ts)}"})
        promote.apply(cfg())
        e = discover.load_edges()[0]
        self.assertEqual((e["status"], e.get("retired_by"), e.get("looks")), ("retired", "results", 12))


# ---------- 7. research memory ----------

def child_job():
    """Run in a spawned child: says where it ran and how keen the kernel is to kill it."""
    with open("/proc/self/oom_score_adj") as f:
        return {"ok": True, "pid": os.getpid(), "oom": int(f.read())}


def dying_job():
    os.kill(os.getpid(), signal.SIGKILL)  # what the kernel does to a process out of memory


def crashing_job():
    raise ValueError("no prices today")


class MemoryTests(TempHome):
    def test_the_price_cache_stops_growing(self):
        path = backtest.cache_path("S", 6)
        v2 = path.replace("_v3.json", "_v2.json")
        with open(v2, "w") as f:
            f.write('{"version": 1, "markets": {}}')   # an old cache: its prices may peek
        old = {f"S-OLD{i}": {"series": "S", "event": f"S-OLD{i}", "ticker": f"S-OLD{i}", "close": T0 + i,
                             "anchor": T0 + i, "decided": T0 + i - 6 * H, "yes_ask": 0.5, "yes_bid": 0.49,
                             "result": "no", "yes_value": 0.0} for i in range(5000)}
        backtest._write_cache(path, old)
        c = FakeKalshi(can_trade=False)
        for i in range(10):
            close = T0 + DAY * 100 + i * DAY
            tk = f"S-NEW{i}"
            c.settled.setdefault("S", []).append({"ticker": tk, "event_ticker": tk, "open_time": iso(close - 3 * DAY),
                                                  "close_time": iso(close), "status": "finalized", "result": "yes",
                                                  "volume_fp": "500.00"})
            c.candle_data[tk] = [{"end_period_ts": close - 7 * H, "yes_ask": {"close": "0.9000"},
                                  "yes_bid": {"close": "0.8900"}}]
        obs = backtest.load_observations(c, "S", 6, max_markets=10)
        cached = backtest._read_cache(path)
        self.assertLessEqual(len(cached), 3000)
        self.assertEqual(len(obs), len([o for o in cached.values() if o and "yes_ask" in o]))
        self.assertTrue(all(f"S-NEW{i}" in cached for i in range(10)))   # what Kalshi lists now stays
        self.assertNotIn("S-OLD0", cached)                                # the oldest go first
        self.assertFalse(os.path.exists(v2))                              # and the old format is gone

    def test_research_runs_in_its_own_process(self):
        out = scheduler.in_child(child_job)
        self.assertTrue(out["ok"])
        self.assertNotEqual(out["pid"], os.getpid())
        self.assertEqual(out["oom"], 1000)  # out of memory, the kernel stops research first

    def test_a_research_killed_for_memory_leaves_the_bot_running(self):
        with self.assertRaises(RuntimeError) as got:
            scheduler.in_child(dying_job)
        self.assertIn("memory", str(got.exception))
        # The scheduler records it like any crash, and carries on.
        s = scheduler.Scheduler(jobs={"research": lambda: scheduler.in_child(dying_job)}, threaded=False)
        with self.assertLogs("kal.scheduler", "ERROR"):
            s.run_now("research")
        self.assertFalse(s.state()["research"]["ok"])
        self.assertIn("memory", db.query("SELECT message FROM runs WHERE kind = 'research'")[0]["message"])

    def test_a_crash_in_research_comes_back_as_one(self):
        with self.assertRaises(RuntimeError) as got:
            scheduler.in_child(crashing_job)
        self.assertIn("no prices today", str(got.exception))

    def test_research_leaves_trading_its_share_of_the_rate_limit(self):
        from kal import client
        old = client.PACER.gap
        try:
            with mock.patch.object(scheduler, "run_research", return_value={"ok": True}):
                self.assertEqual(scheduler._research_slowly(), {"ok": True})
            self.assertEqual(client.PACER.gap, scheduler.RESEARCH_PACE)
            # Kalshi allows about 20 reads a second: trading's pace plus research's fits in it.
            self.assertLessEqual(1 / old + 1 / scheduler.RESEARCH_PACE, 20 + 1e-9)
        finally:
            client.PACER.gap = old

    def test_the_service_survives_an_out_of_memory_kill(self):
        with open(os.path.join(config.ROOT, "deploy", "kal.service")) as f:
            unit = f.read()
        self.assertIn("\nOOMPolicy=continue\n", unit)
        self.assertIn("\nMemoryMax=400M\n", unit)
        self.assertIs(scheduler.DEFAULT_JOBS["research"], scheduler.run_research_apart)


# ---------- 8. research the daily restart kept killing ----------

class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class RestartTests(TempHome):
    def test_research_cut_off_by_a_restart_runs_again_soon(self):
        start = MIDNIGHT + 9 * H
        db.put("last_research", {"ts": start - DAY + H, "ok": True})   # yesterday's run finished
        db.put("scheduler", {"research": start})                       # today's started, never finished
        clock = Clock(start + 25 * 60)                                   # the update restarted kal
        s = scheduler.Scheduler(jobs={"research": lambda: None}, clock=clock, threaded=False)
        self.assertEqual(s.due(), [])
        clock.t += scheduler.RESEARCH_RETRY
        self.assertEqual(s.due(), ["research"])

    def test_daily_restarts_cant_lock_research_out(self):
        """Like the reviewers' sim_research_drift.py: research takes an hour, and the update
        restarts kal every day at 09:23-09:33 UTC, killing whatever research is running."""
        rng = random.Random(1)
        settings = cfg()
        now = MIDNIGHT + 15 * H
        clock = Clock(now)

        def fresh():
            return scheduler.Scheduler(jobs={"research": lambda: None}, clock=clock, threaded=False)
        s, running, done, killed = fresh(), None, 0, 0
        restart = MIDNIGHT + DAY + 9 * H + 23 * 60 + rng.uniform(0, 600)
        while clock.t < MIDNIGHT + 31 * DAY:
            if running is not None and clock.t >= running:
                db.put("last_research", {"ts": clock.t, "ok": True})
                done, running = done + 1, None
            if clock.t >= restart:
                if running is not None:
                    killed, running = killed + 1, None
                s = fresh()
                restart = (restart // DAY + 1) * DAY + 9 * H + 23 * 60 + rng.uniform(0, 600)
            if running is None and "research" in s.due(cfg=settings):
                s.last["research"] = clock.t
                s._save()
                running = clock.t + H
            clock.t += 60
        self.assertGreaterEqual(done, 27, (done, killed))


class ResearchKeepsOneExchangeTests(TempHome):
    def test_a_switch_during_research_cant_send_its_results_to_the_other_exchange(self):
        # Research can take an hour or two. Started on prod, it keeps prod's folder and db even
        # if someone switches to demo meanwhile (round-3 supervisor review).
        import os
        from unittest import mock
        from kal import config, db, research
        from kal.research import discover, longshot, promote

        def discover_then_switch(client, cfg, quick=False):
            config.update({"env": "demo"})          # someone switches mid-run
            discover._save("edges.json", [{"id": "S:0.90-0.95"}])
            return {"tested": ["S"]}
        with mock.patch.object(discover, "run", discover_then_switch), \
                mock.patch.object(longshot, "run", return_value={"ok": True}), \
                mock.patch.object(promote, "apply", return_value=[]), \
                mock.patch.object(longshot, "apply_longshot", return_value=[]):
            research.run_all(client=object())
        self.assertTrue(os.path.exists(os.path.join(config.home(), "research", "edges.json")))
        self.assertFalse(os.path.exists(os.path.join(config.home(), "research-demo", "edges.json")))
        with db.pinned("prod"):
            self.assertEqual(len(db.query("SELECT * FROM runs WHERE kind = 'research'")), 1)
        with db.pinned("demo"):
            self.assertEqual(db.query("SELECT * FROM runs WHERE kind = 'research'"), [])
