"""Limits hold for every real order, not just at the start of a run. A swarm review of the
money path found (each finding checked by a separate skeptic):
- a budget cut, a lower cap or a strategy moved off live mid-run were ignored until the
  run ended: only OFF was re-checked before each real order;
- the legs a half-built basket left stayed "locked-in", so the cap on open bets never
  counted them.
A later cross-PR audit found the same check came before the rechecks that read Kalshi, so a
stop saved during them was missed; and a basket with a leg sold outside the bot still read
as locked in. python -m unittest tests.test_limits_hold
"""
import sqlite3
import time
from unittest import mock

from kal import config, db, engine, ledger, paper
from kal.strategies import load_all
from kal.client import fee
from kal.strategies.base import Context, held_dollars
from tests.helpers import TempHome, orders_proven
from tests.test_core import arb_event, fake_with_arb


def two_baskets():
    """Two live baskets (EV1, EV3), each locked in, each bought in one run."""
    c = fake_with_arb()
    c.event_list = [arb_event("EV1"), arb_event("EV3")]
    for ev in c.event_list:
        for m in ev["markets"]:
            b = float(m["yes_bid_dollars"])
            c.books[m["ticker"]] = {"yes": [(b, 40)], "no": []}
            c.market_info[m["ticker"]] = {"ticker": m["ticker"], "status": "active",
                                          "yes_bid_dollars": f"{b:.4f}", "no_bid_dollars": f"{1 - b - .01:.4f}"}
    return c


def baskets_bought(c):
    return {o[0].split("-")[0] for o in c.orders}


def assert_one_whole_basket(test, c):
    """The run stopped between baskets, never inside one, and nothing tripped."""
    test.assertEqual(len(baskets_bought(c)), 1)
    test.assertEqual(len(c.orders), 4)   # all 4 legs of the first basket
    cfg = config.load()
    test.assertTrue(cfg["enabled"])
    test.assertEqual(cfg["disabled_reason"], "")


class MidRunTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})
        orders_proven()

    def run_editing(self, change):
        """A run where the owner saves `change` from the dashboard right after the first buy."""
        c = two_baskets()
        real_buy = c.buy

        def buy_then_edit(*a, **k):
            r = real_buy(*a, **k)
            if len(c.orders) == 1:
                config.update(change)
            return r
        c.buy = buy_then_edit
        return c, engine.run_once(c)

    def test_a_budget_cut_mid_run_stops_real_orders(self):
        # The skeptic's repro: both baskets were bought, $57.60, with the budget cut to $10.
        c, s = self.run_editing({"budget": 10})
        assert_one_whole_basket(self, c)
        self.assertIn("budget set stricter mid-run", " ".join(s["notes"]))

    def test_a_lower_cap_mid_run_stops_real_orders(self):
        c, s = self.run_editing({"max_per_arb": 5})
        assert_one_whole_basket(self, c)

    def test_a_strategy_moved_off_live_mid_run_stops_its_real_orders(self):
        for mode in ("paper", "off"):
            with self.subTest(mode):
                config.update({"strategies": {"basket_arb": {"mode": "live"}}})
                db.execute("DELETE FROM orders")
                db.execute("DELETE FROM signals")
                c, s = self.run_editing({"strategies": {"basket_arb": {"mode": mode}}})
                assert_one_whole_basket(self, c)
                self.assertIn("basket_arb left live mid-run", " ".join(s["notes"]))

    def run_acting_during_recheck(self, act):
        """`act` lands while basket 2 is rechecked (its order books read), after the check
        before it passed: the audit's repro bought both baskets."""
        c = two_baskets()
        real, done = c.orderbook, []

        def orderbook(ticker):
            if len(c.orders) == 4 and not done:
                done.append(1)
                act()
            return real(ticker)
        c.orderbook = orderbook
        return c, engine.run_once(c)

    def test_a_stop_saved_while_a_signal_is_rechecked_stops_its_order(self):
        for name, act, note in (
                ("budget cut", lambda: config.update({"budget": 10}), "budget set stricter mid-run"),
                ("OFF", config.turn_off, "turned off mid-run"),
                ("health trip", lambda: config.trip("Disk almost full: 150 MB free."), "turned off mid-run")):
            with self.subTest(name):
                config.update({"enabled": True, "disabled_reason": "", "budget": 60})
                db.execute("DELETE FROM orders")
                db.execute("DELETE FROM signals")
                c, s = self.run_acting_during_recheck(act)
                self.assertEqual(len(baskets_bought(c)), 1, f"{len(c.orders)} orders after the {name}")
                self.assertEqual(len(c.orders), 4)
                self.assertIn(note, " ".join(s["notes"]))

    def test_a_stop_saved_while_the_signals_row_waits_on_the_books_stops_its_order(self):
        # The review's repro: the last check passed, then the row waited on a lock (sqlite
        # waits up to 30 s) and OFF was saved meanwhile.
        c = two_baskets()
        real_insert, done = db.insert, []

        def insert(table, **row):
            if table == "signals" and len(c.orders) == 4 and not done:
                done.append(1)
                config.turn_off()
            return real_insert(table, **row)
        with mock.patch.object(db, "insert", insert):
            s = engine.run_once(c)
        self.assertEqual(len(c.orders), 4)
        self.assertIn("turned off mid-run", " ".join(s["notes"]))
        self.assertTrue(any(r["status"].startswith("skipped: turned off mid-run")
                            for r in db.query("SELECT status FROM signals")))

    def test_a_mid_run_stop_whose_skip_cant_be_written_is_not_a_failed_run(self):
        # The books refuse the 'skipped' write: still no order, the run is fine, and the
        # stop's note stays (it isn't an error that counts toward the error trip).
        c = two_baskets()
        real_insert, real_execute, done = db.insert, db.execute, []

        def insert(table, **row):
            if table == "signals" and len(c.orders) == 4 and not done:
                done.append(1)
                config.update({"budget": 10})
            return real_insert(table, **row)

        def execute(sql, *args):
            if sql.startswith("UPDATE signals SET status") and str(args[0]).startswith("skipped:"):
                raise sqlite3.OperationalError("database is locked")
            return real_execute(sql, *args)
        with mock.patch.object(db, "insert", insert), mock.patch.object(db, "execute", execute), \
                self.assertLogs("kal", "WARNING"):
            s = engine.run_once(c)
        self.assertEqual(len(c.orders), 4)
        self.assertTrue(s["ok"], s)
        self.assertIn("budget set stricter mid-run", " ".join(s["notes"]))
        self.assertEqual(db.get("consecutive_errors"), 0)

    def test_a_looser_setting_mid_run_changes_nothing(self):
        c, s = self.run_editing({"budget": 200, "max_per_arb": 40})
        self.assertEqual(len(baskets_bought(c)), 2)
        self.assertNotIn("mid-run", " ".join(s["notes"]))


class StrandedLegTests(TempHome):
    def order(self, sid, ticker, filled, price=0.45):
        db.insert("orders", signal_id=sid, strategy="basket_arb", ticker=ticker, side="no", count=10,
                  price=price, filled=filled, status="executed", order_id=f"o-{ticker}")

    def signal(self, status):
        return db.insert("signals", strategy="basket_arb", event="EV1", title="basket", mode="live",
                         guaranteed=1, cost=20.0, payout_min=21.0, exp_profit=1.0, legs=[], status=status)

    def test_a_half_built_baskets_legs_count_as_bets(self):
        whole = self.signal("executed")
        self.order(whole, "EV9-0", 10)
        self.order(whole, "EV9-1", 10)
        self.assertEqual(ledger.open_directional(), 0)   # a whole basket is locked in
        stranded = self.signal("stranded: leg 2/4 EV1-1 filled 0/10; basket incomplete")
        self.order(stranded, "EV1-0", 10)
        self.assertAlmostEqual(ledger.open_directional(), 10 * 0.45 + fee(10, 0.45))

    def test_a_basket_marked_stranded_but_whole_after_all_stays_locked_in(self):
        # A lost reply on the last leg marks it stranded; reconcile then finds that leg filled.
        legs = [{"ticker": f"EV2-{i}"} for i in range(2)]
        sid = db.insert("signals", strategy="basket_arb", event="EV2", title="basket", mode="live",
                        guaranteed=1, cost=9.0, payout_min=10.0, exp_profit=1.0, legs=legs,
                        status="stranded: leg 2/2 EV2-1: no clear reply")
        self.order(sid, "EV2-0", 10)
        self.order(sid, "EV2-1", 10)
        self.assertEqual(ledger.open_directional(), 0)


    def test_a_half_built_basket_two_of_four_legs_filled_alike_is_still_bets(self):
        # A lost reply on leg 2; reconcile finds it filled: fills [10, 10], legs 3-4 never sent.
        legs = [{"ticker": f"EV3-{i}"} for i in range(4)]
        sid = db.insert("signals", strategy="basket_arb", event="EV3", title="basket", mode="live",
                        guaranteed=1, cost=20.0, payout_min=21.0, exp_profit=1.0, legs=legs,
                        status="stranded: leg 2/4 EV3-1: no clear reply")
        self.order(sid, "EV3-0", 10)
        self.order(sid, "EV3-1", 10)
        self.assertAlmostEqual(ledger.open_directional(), 2 * (10 * 0.45 + fee(10, 0.45)))

    def test_junk_legs_count_as_bets(self):
        sid = db.insert("signals", strategy="basket_arb", event="EV4", title="basket", mode="live",
                        guaranteed=1, cost=9.0, payout_min=10.0, exp_profit=1.0, legs={"a": 1, "b": 2},
                        status="stranded: odd")
        self.order(sid, "EV4-0", 10)
        self.order(sid, "EV4-1", 10)
        self.assertGreater(ledger.open_directional(), 0)


class SoldLegTests(TempHome):
    """A whole basket, then the owner sells one leg in Kalshi's app: the next run writes it
    off, and the other legs are naked bets now."""

    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})
        orders_proven()

    def test_a_basket_with_a_leg_sold_outside_the_bot_counts_as_bets(self):
        c = fake_with_arb()
        self.assertTrue(engine.run_once(c)["ok"])
        self.assertEqual([o[0] for o in c.orders], ["EV1-0", "EV1-1", "EV1-2", "EV1-3"])
        self.assertEqual(ledger.open_directional(), 0)   # whole: locked in
        c.held["EV1-0"] = 0                               # the owner sells the NO on EV1-0
        s = engine.run_once(c)
        self.assertIn("counted them as lost", " ".join(s["notes"]))
        left = sum(r["cost"] for r in ledger.open_positions() if r["count"] > 0)
        self.assertAlmostEqual(left, 24.60, places=2)     # 3 legs, no longer locked in
        # The legs left count; the sold one too, until it settles (as for a bet).
        bought = sum(10 * o[3] + fee(10, o[3]) for o in c.orders)
        self.assertAlmostEqual(ledger.open_directional(), bought, places=2)
        self.assertGreaterEqual(ledger.open_directional(), left)

    def test_a_fresh_basket_bought_after_a_write_off_on_its_markets_stays_locked_in(self):
        # The review's repro: the owner closes a whole basket in Kalshi's app; the arb is
        # still there, so the next run buys a fresh, whole one on the same markets.
        config.update({"daily_loss_limit": 1.0})   # the write-off reads as a loss: not probed here
        c = fake_with_arb(balance=1000.0)
        self.assertTrue(engine.run_once(c)["ok"])
        first = list(c.orders)
        for t, *_ in first:
            c.held[t] = 0
        time.sleep(0.01)   # the write-off, then the new orders, come later
        engine.run_once(c)
        self.assertGreater(len(c.orders), len(first))   # a second, whole basket
        old = sum(10 * o[3] + fee(10, o[3]) for o in first)
        self.assertAlmostEqual(ledger.open_directional(), old, places=2)   # the old one only

    def test_a_write_off_on_one_basket_leaves_another_basket_locked_in(self):
        c = two_baskets()
        self.assertTrue(engine.run_once(c)["ok"])
        self.assertEqual(baskets_bought(c), {"EV1", "EV3"})
        c.held["EV1-0"] = 0
        engine.run_once(c)
        ev1 = sum(10 * o[3] + fee(10, o[3]) for o in c.orders if o[0].startswith("EV1"))
        self.assertAlmostEqual(ledger.open_directional(), ev1, places=2)   # EV3 stays locked in


class ErrorStreakTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})
        orders_proven()

    def failed_runs_until_the_stop(self, locked_when):
        real_put, locked, failed = db.put, [False], 0

        def put(key, value):
            if key == "consecutive_errors" and locked[0]:
                raise sqlite3.OperationalError("database is locked")
            return real_put(key, value)
        with mock.patch.object(engine.fees, "check", side_effect=ValueError("a bug")), \
                mock.patch.object(db, "put", put), self.assertLogs("kal", "ERROR"):
            for i in range(6):
                if config.load()["disabled_reason"]:
                    break
                locked[0] = locked_when(i)
                failed += not engine.run_once(fake_with_arb())["ok"]
        return failed, config.load()["disabled_reason"]

    def test_failed_runs_stop_the_bot_at_three_when_the_books_take_only_some_counts(self):
        # The audit's repro: the books took every other count, so the two counts climbed in
        # turn and the stop came after 5 failed runs, not 3.
        failed, reason = self.failed_runs_until_the_stop(lambda i: i % 2 == 1)
        self.assertEqual(failed, 3)
        self.assertIn("3 failed runs in a row", reason)

    def test_a_good_run_in_another_process_ends_the_streak_held_in_memory(self):
        # The review's repro: two failed runs the books couldn't count, then `sudo kal run`
        # (another process) had a good run. One more failure here is 1 in a row, not 3.
        real_put, locked = db.put, [True]

        def put(key, value):
            if key == "consecutive_errors" and locked[0]:
                raise sqlite3.OperationalError("attempt to write a readonly database")
            return real_put(key, value)
        db.put("last_ok_run", time.time() - 600)
        with mock.patch.object(engine.fees, "check", side_effect=ValueError("a bug")), \
                mock.patch.object(db, "put", put), self.assertLogs("kal", "ERROR"):
            for _ in range(2):
                self.assertFalse(engine.run_once(fake_with_arb())["ok"])
            locked[0] = False
            db.put("consecutive_errors", 0)   # what the other process's good run writes
            db.put("last_ok_run", time.time())
            self.assertFalse(engine.run_once(fake_with_arb())["ok"])
        self.assertEqual(config.load()["disabled_reason"], "")
        self.assertEqual(db.get("consecutive_errors"), 1)

    def test_a_good_run_ends_a_streak_that_began_when_the_books_couldnt_be_read(self):
        # The review's sequence: the first failure can't even read the books; a good run
        # elsewhere; a failure the books can't count; one more. 2 after the good run: no stop.
        real_put, real_get = db.put, db.get
        state = {"put_locked": True, "get_locked": True}

        def put(key, value):
            if key == "consecutive_errors" and state["put_locked"]:
                raise sqlite3.OperationalError("database is locked")
            return real_put(key, value)

        def get(key, default=None):
            if key in ("last_ok_run", "consecutive_errors") and state["get_locked"]:
                raise sqlite3.OperationalError("database is locked")
            return real_get(key, default)
        real_put("last_ok_run", time.time() - 600)
        with mock.patch.object(engine.fees, "check", side_effect=ValueError("a bug")), \
                mock.patch.object(db, "put", put), mock.patch.object(db, "get", get), \
                self.assertLogs("kal", "ERROR"):
            engine.run_once(fake_with_arb())        # 1: the books can't be read at all
            time.sleep(0.01)
            state["get_locked"] = False
            real_put("consecutive_errors", 0)       # a good run elsewhere
            real_put("last_ok_run", time.time())
            engine.run_once(fake_with_arb())        # 2: its count can't be written
            state["put_locked"] = False
            engine.run_once(fake_with_arb())        # 3
        self.assertEqual(config.load()["disabled_reason"], "")
        self.assertEqual(real_get("consecutive_errors"), 2)

    def test_a_good_run_between_two_unreadable_failures_still_ends_the_streak(self):
        # The streak is dated from its first failure: a good run after that ends it, even
        # when the failure after the good run couldn't read the books either.
        real_put, real_get = db.put, db.get
        locked = [True]

        def put(key, value):
            if key == "consecutive_errors" and locked[0]:
                raise sqlite3.OperationalError("database is locked")
            return real_put(key, value)

        def get(key, default=None):
            if key in ("last_ok_run", "consecutive_errors") and locked[0]:
                raise sqlite3.OperationalError("database is locked")
            return real_get(key, default)
        real_put("last_ok_run", time.time() - 600)
        with mock.patch.object(engine.fees, "check", side_effect=ValueError("a bug")), \
                mock.patch.object(db, "put", put), mock.patch.object(db, "get", get), \
                self.assertLogs("kal", "ERROR"):
            engine.run_once(fake_with_arb())        # 1: the books can't be read
            time.sleep(0.01)
            real_put("last_ok_run", time.time())    # a good run elsewhere
            time.sleep(0.01)
            engine.run_once(fake_with_arb())        # 2: still can't be read
            locked[0] = False
            engine.run_once(fake_with_arb())        # 3: 2 after the good run, not 3
        self.assertEqual(config.load()["disabled_reason"], "")

    def test_a_third_failed_run_the_books_cant_count_still_stops_the_bot(self):
        failed, reason = self.failed_runs_until_the_stop(lambda i: i == 2)
        self.assertEqual(failed, 3)
        self.assertIn("3 failed runs in a row, and the books can't be written", reason)


class HeldDollarsTests(TempHome):
    def ctx(self, held_cost, **cfg):
        return Context(None, dict(config.DEFAULTS, **cfg), "live", 0, 300.0, set(held_cost), {}, {},
                       held_cost=held_cost)

    def test_a_held_ticker_counts_what_the_books_say_it_cost(self):
        ctx = self.ctx({"A": 10.0}, max_per_trade=3)
        self.assertEqual(held_dollars(ctx, "A"), 10.0)   # a later, lower max_per_trade can't shrink it
        self.assertEqual(held_dollars(ctx, "OWNERS"), 3)   # unknown to the books: the most one bet can be

    def test_open_cost_per_ticker_leaves_settled_ones_out(self):
        for tk, filled in (("A-1", 10), ("A-1", 5), ("B-1", 4)):
            db.insert("orders", signal_id=0, strategy="favorites", ticker=tk, side="yes", count=filled,
                      price=0.5, filled=filled, status="executed", order_id=f"o{tk}{filled}")
        db.insert("settlements", ticker="B-1", settled_ts=1.0, result="yes", revenue=4.0, cost=2.0, pnl=2.0)
        self.assertEqual(set(ledger.open_by_ticker()), {"A-1"})
        self.assertAlmostEqual(ledger.open_by_ticker()["A-1"], 7.5 + fee(10, 0.5) + fee(5, 0.5))


class EngineHeldCostTests(TempHome):
    """The engine hands each scan the books' open cost per ticker: live from the ledger,
    paper from the paper books."""

    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        orders_proven()

    def seen_held_cost(self, mode, client):
        config.update({"strategies": {"favorites": {"mode": mode}}})
        seen = []
        strat = load_all()["favorites"]
        with mock.patch.object(type(strat), "proven", lambda self, ctx: (True, "test")), \
                mock.patch.object(type(strat), "scan", lambda self, ctx: seen.append(dict(ctx.held_cost)) or []):
            engine.run_once(client)
        return seen[0]

    def test_a_live_scan_gets_the_ledgers_cost_per_ticker(self):
        c = fake_with_arb()
        for i in range(3):
            tk = f"KXBTCD-H{i}-T"
            db.insert("orders", signal_id=0, strategy="favorites", ticker=tk, side="yes", count=10,
                      price=1.0, filled=10, status="executed", order_id=f"o{i}")
            c.position_list = (c.position_list or []) + [{"ticker": tk, "position": 10}]
        got = self.seen_held_cost("live", c)
        self.assertEqual(got, ledger.open_by_ticker())
        self.assertEqual(len(got), 3)
        self.assertTrue(all(v >= 10.0 for v in got.values()))

    def test_a_paper_scan_gets_the_paper_books_cost_per_ticker(self):
        db.insert("paper", signal_id=0, strategy="favorites", event="E", ticker="P-1", side="yes",
                  count=10, price=0.5, cost=5.0)
        got = self.seen_held_cost("paper", fake_with_arb())
        self.assertEqual(got, paper.open_by_ticker())
        self.assertEqual(got, {"P-1": 5.0})

