"""Holes the reviewers found in the money path, each shown and then closed."""
import io
import json
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import requests

import kal.__main__ as cli
from kal import client, config, db, engine, gate, ledger, maintenance, paper, risk
from kal.client import KalshiError
from kal.strategies import base, basket_arb
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven
from tests.test_core import arb_event, fake_with_arb


class LiveBasketTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})
        orders_proven()

    def test_lost_reply_on_a_later_leg_stops_at_once(self):
        # Leg 1 filled, leg 2's reply never came: the basket may be half-built.
        c = fake_with_arb()
        real_buy = c.buy

        def buy(ticker, side, count, price, client_order_id=None):
            if len(c.orders) == 1:
                raise requests.ConnectionError("reset by peer")
            return real_buy(ticker, side, count, price, client_order_id)
        c.buy = buy
        s = engine.run_once(c)
        self.assertFalse(s["ok"])
        self.assertIn("half-built", config.load()["disabled_reason"])
        self.assertEqual([r["status"] for r in db.query("SELECT status FROM orders ORDER BY id")],
                         ["executed", "unknown"])
        self.assertTrue(db.query("SELECT status FROM signals")[0]["status"].startswith("stranded:"))

    def test_a_run_that_died_between_legs_is_caught_next_run(self):
        # What a killed process leaves behind: the signal still 'accepted', one leg bought.
        legs = [{"ticker": f"EV1-{i}", "side": "no", "price": 0.5, "count": 10} for i in range(4)]
        sid = db.insert("signals", strategy="basket_arb", event="EV1", title="t", mode="live",
                        guaranteed=1, cost=20, payout_min=30, exp_profit=1, legs=legs, status="accepted")
        db.insert("orders", signal_id=sid, strategy="basket_arb", ticker="EV1-0", side="no", count=10,
                  price=0.5, filled=10, status="executed", order_id="o1", client_order_id="c1")
        c = FakeKalshi()
        c.position_list = [{"ticker": "EV1-0", "position": -10, "exposure": 5}]
        c.market_info["EV1-0"] = {"ticker": "EV1-0", "status": "active", "no_bid_dollars": "0.5000"}
        engine.run_once(c)
        self.assertIn("half-built on EV1", config.load()["disabled_reason"])
        self.assertEqual(db.query("SELECT status FROM signals")[0]["status"], "stranded: 1 of 4 legs bought")
        config.clear_trip()
        config.update({"enabled": True})
        engine.run_once(c)   # reported once: Reset lets the bot carry on
        self.assertEqual(config.load()["disabled_reason"], "")

    def test_a_cancellation_clause_basket_trades_on_paper_even_when_live(self):
        # If the game is cancelled, Kalshi settles at a price it picks: not locked in, so
        # basket_arb's live mode must not put real money into it.
        c = fake_with_arb()
        for m in c.event_list[0]["markets"]:
            m["rules_primary"] = ("If this team wins, Yes. If the game is cancelled or not started within "
                                  "48 hours of its originally scheduled start, the market will resolve to a fair price.")
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual((s["live_trades"], s["paper_trades"]), (0, 1))
        self.assertEqual(list(c.orders), [])                      # not one real order
        row = db.query("SELECT mode, status, guaranteed, meta FROM signals")[0]
        self.assertEqual((row["mode"], row["status"], row["guaranteed"]), ("paper", "accepted", 0))
        self.assertIs(json.loads(row["meta"])["paper_only"], True)
        self.assertGreater(paper.open_directional(), 0)   # counts against the bet exposure cap
        # The same event without the clause trades for real, as before.
        for m in c.event_list[0]["markets"]:
            del m["rules_primary"]
        db.execute("DELETE FROM paper")
        s = engine.run_once(c)
        self.assertEqual(s["live_trades"], 1)

    GAME = ("If this team wins, Yes. If the game is cancelled or not started within 48 hours of its "
            "originally scheduled start, the market will resolve to a fair price.")

    def clause_events(self, n):
        c = FakeKalshi()
        c.event_list = []
        for i in range(n):
            ev = arb_event(f"EV{i + 1}")
            for m in ev["markets"]:
                m["rules_primary"] = self.GAME
                b = float(m["yes_bid_dollars"])
                c.books[m["ticker"]] = {"yes": [(b, 40)], "no": []}
                c.market_info[m["ticker"]] = {"ticker": m["ticker"], "status": "active",
                                              "yes_bid_dollars": f"{b:.4f}", "no_bid_dollars": f"{1 - b - 0.01:.4f}"}
            c.event_list.append(ev)
        return c

    def test_an_open_paper_basket_is_not_logged_again_every_run(self):
        c = self.clause_events(1)
        engine.run_once(c)
        engine.run_once(c)
        engine.run_once(c)
        self.assertEqual(len(db.query("SELECT id FROM signals")), 1)   # found once, not once a run

    def test_not_locked_in_baskets_share_at_most_a_quarter_of_the_paper_money(self):
        # A day with many close games must not use up the paper money that other ideas
        # need to prove themselves.
        config.update({"paper_bankroll": 100.0})
        c = self.clause_events(5)
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        held = paper.open_cost_by_kind().get("cancel_clause_basket", 0)
        self.assertGreater(held, 0)
        self.assertLessEqual(held, basket_arb.PAPER_SHARE * 100.0 + 1e-9)
        skipped = [r["status"] for r in db.query("SELECT status FROM signals") if r["status"] != "accepted"]
        self.assertTrue(skipped)
        self.assertTrue(all("25% share of the paper money" in x for x in skipped), skipped)
        self.assertEqual(s["paper_trades"], len(db.query("SELECT id FROM signals WHERE status = 'accepted'")))
        self.assertEqual(list(c.orders), [])   # and never a real order

    def test_a_finished_basket_left_accepted_is_just_recorded(self):
        legs = [{"ticker": f"EV1-{i}", "side": "no", "price": 0.5, "count": 10} for i in range(2)]
        sid = db.insert("signals", strategy="basket_arb", event="EV1", title="t", mode="live",
                        guaranteed=1, cost=10, payout_min=10.5, exp_profit=.5, legs=legs, status="accepted")
        for i in range(2):
            db.insert("orders", signal_id=sid, strategy="basket_arb", ticker=f"EV1-{i}", side="no",
                      count=10, price=0.5, filled=10, status="executed", order_id=f"o{i}", client_order_id=f"c{i}")
        c = FakeKalshi()
        c.position_list = [{"ticker": f"EV1-{i}", "position": -10, "exposure": 5} for i in range(2)]
        engine.run_once(c)
        self.assertEqual(config.load()["disabled_reason"], "")
        self.assertEqual(db.query("SELECT status FROM signals")[0]["status"], "executed")

    def test_lost_reply_on_a_first_leg_that_filled_stops_once_found(self):
        c = fake_with_arb()
        real_buy = c.buy

        def lost(ticker, side, count, price, client_order_id=None):
            real_buy(ticker, side, count, price, client_order_id)
            c.order_list = [{"client_order_id": client_order_id, "order_id": "o1", "status": "executed",
                             "fill_count_fp": f"{count:.2f}", "ticker": ticker}]
            raise requests.ReadTimeout("no reply")
        c.buy = lost
        s = engine.run_once(c)
        self.assertTrue(s.get("waiting"), s)
        self.assertTrue(config.load()["enabled"])   # a blip isn't a failure
        c.buy = real_buy
        engine.run_once(c)   # finds leg 1 filled with legs 2-4 never sent
        self.assertIn("half-built", config.load()["disabled_reason"])

    def test_a_network_blip_on_an_order_never_trips(self):
        c = fake_with_arb()
        real_buy = c.buy
        c.buy = lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("DNS failure"))
        c.event_list = [arb_event()]
        s = engine.run_once(c)
        self.assertTrue(s.get("waiting"), s)
        c.buy = real_buy
        for _ in range(3):   # Kalshi never saw it; runs wait out the grace, uncounted
            s = engine.run_once(c)
            self.assertTrue(s.get("waiting"), s)
        self.assertTrue(config.load()["enabled"])
        self.assertEqual(db.get("consecutive_errors", 0), 0)
        db.execute("UPDATE orders SET ts = ?", time.time() - engine.RECONCILE_GRACE - 1)
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual(db.query("SELECT status FROM orders ORDER BY id")[0]["status"], "never_placed")
        self.assertEqual(s["live_trades"], 1)

    def test_a_failed_cancel_is_reported_and_looked_up_again(self):
        c = fake_with_arb()
        c.buy = lambda t, side, n, p, client_order_id=None: {
            "order_id": "r1", "status": "resting", "fill_count_fp": "0.00", "client_order_id": client_order_id}
        c.cancel = lambda oid: (_ for _ in ()).throw(requests.ReadTimeout("cancel timed out"))
        engine.run_once(c)
        reason = config.load()["disabled_reason"]
        self.assertIn("cancel FAILED", reason)
        self.assertIn("r1", reason)
        self.assertNotIn("cancelled it", reason)
        self.assertEqual(db.query("SELECT status FROM orders")[0]["status"], "unknown")
        # After Reset, the next run looks the order up first and cancels it if it's still there.
        coid = db.query("SELECT client_order_id FROM orders")[0]["client_order_id"]
        c.order_list = [{"client_order_id": coid, "order_id": "r1", "status": "resting",
                         "fill_count_fp": "0.00", "ticker": "EV1-0"}]
        c.cancel = lambda oid: c.cancelled.append(oid) or {}
        c.event_list = []
        config.clear_trip()
        config.update({"enabled": True})
        s = engine.run_once(c)
        self.assertEqual(s.get("reconciled"), 1, s)
        self.assertEqual(c.cancelled, ["r1"])

    def test_env_switch_mid_run_keeps_one_runs_orders_in_one_books(self):
        c = fake_with_arb()
        real_buy = c.buy

        def buy_then_switch(*a, **k):
            r = real_buy(*a, **k)
            if len(c.orders) == 1:
                config.update({"env": "demo"})
            return r
        c.buy = buy_then_switch
        engine.run_once(c)
        self.assertEqual(len(db.query("SELECT * FROM orders", )), 0)   # demo's books: untouched
        self.assertEqual(len(c.orders), 4)
        with db.pinned("prod"):
            rows = db.query("SELECT status, filled FROM orders")
        self.assertEqual([r["status"] for r in rows], ["executed"] * 4)

    def test_basket_skips_events_about_to_close(self):
        c = fake_with_arb()
        soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 120))
        ev = arb_event(close=soon)
        c.event_list = [ev]
        s = engine.run_once(c)
        self.assertEqual(c.orders, [])
        self.assertEqual(s["signals"], 0)

    def test_recheck_drops_a_basket_whose_market_is_about_to_close(self):
        c = fake_with_arb()
        sig = basket_arb.to_signal("EV1", "t", basket_arb.no_basket(
            [(f"EV1-{i}", b, 40) for i, b in enumerate((0.55, 0.35, 0.15, 0.07))], 30))
        sig.meta["first_close"] = time.time() + 60
        ctx = mock.Mock(client=c, budget_left=60, cfg={"max_per_arb": 30})
        self.assertIsNone(basket_arb.STRATEGY.recheck(ctx, sig))
        sig.meta["first_close"] = time.time() + 3600
        self.assertIsNotNone(basket_arb.STRATEGY.recheck(ctx, sig))


class SoldOutsideTheBotTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        db.insert("orders", ticker="A", side="no", count=10, price=0.90, filled=10, status="executed",
                  order_id="o1", client_order_id="c1")
        self.cost = 9 + client.fee(10, 0.90)

    def test_contracts_sold_outside_the_bot_are_written_off(self):
        c = FakeKalshi()
        c.position_list = []   # the owner sold all 10 in the app
        c.market_info["A"] = {"ticker": "A", "status": "active", "no_bid_dollars": "0.6000"}
        s = engine.run_once(c)
        self.assertTrue(config.load()["enabled"])
        self.assertTrue(any("fewer NO on A" in n for n in s["notes"]), s)
        self.assertAlmostEqual(ledger.realized(), -self.cost)
        self.assertAlmostEqual(ledger.open_cost(), 0)
        # It settles the bot's way later: the books must not earn on contracts they don't hold.
        c.market_info["A"] = {"ticker": "A", "status": "settled", "result": "no"}
        ledger.settle(c)
        self.assertAlmostEqual(ledger.realized(), -self.cost)
        self.assertEqual(ledger.shortfalls([]), [])

    def test_partly_sold_keeps_the_rest_at_its_share_of_the_cost(self):
        c = FakeKalshi()
        c.position_list = [{"ticker": "A", "position": -4, "exposure": 3.6}]
        c.market_info["A"] = {"ticker": "A", "status": "active", "no_bid_dollars": "0.9000"}
        engine.run_once(c)
        self.assertAlmostEqual(ledger.realized(), -self.cost * 0.6)
        self.assertAlmostEqual(ledger.open_cost(), self.cost * 0.4)
        c.market_info["A"] = {"ticker": "A", "status": "settled", "result": "no"}
        ledger.settle(c)
        self.assertAlmostEqual(ledger.realized(), 4 - self.cost)
        engine.run_once(c)   # and nothing more happens once the books match Kalshi
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM writeoffs")[0]["n"], 1)

    def test_a_closed_market_is_left_alone(self):
        # Kalshi may already be paying it out; the settlement books it.
        c = FakeKalshi()
        c.position_list = []
        c.market_info["A"] = {"ticker": "A", "status": "closed"}
        engine.run_once(c)
        self.assertAlmostEqual(ledger.realized(), 0)
        self.assertAlmostEqual(ledger.open_cost(), self.cost)


class RestoreCatchUpTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})

    def fill(self, oid, ticker="N", side="no", n=10, p=0.9, action="buy"):
        return {"order_id": oid, "ticker": ticker, "side": side, "action": action,
                "count_fp": f"{n:.2f}", f"{side}_price_dollars": f"{p:.4f}", "created_time": "2026-10-24T10:00:00Z"}

    def test_restore_brings_back_the_trades_made_after_the_backup(self):
        db.insert("orders", ticker="OLD", side="no", count=5, price=0.8, filled=5, status="executed",
                  order_id="old1", client_order_id="c0")
        maintenance.backup()
        db.insert("orders", ticker="N", side="no", count=10, price=0.9, filled=10, status="executed",
                  order_id="new1", client_order_id="c1")   # made after the backup...
        day = maintenance.backups()[0][0]
        maintenance.restore(day)   # ...so the restored books don't have it
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM orders WHERE order_id = 'new1'")[0]["n"], 0)
        self.assertTrue(db.get("restored"))
        before = ledger.available(config.load())
        c = FakeKalshi()
        c.fill_list = [self.fill("old1", "OLD", n=5, p=0.8), self.fill("new1"),
                       self.fill("mine", "OWN", "yes", 3, 0.4, action="sell")]
        c.position_list = [{"ticker": "OLD", "position": -5, "exposure": 4},
                           {"ticker": "N", "position": -10, "exposure": 9}]
        config.clear_trip()
        config.update({"enabled": True})
        s = engine.run_once(c)
        self.assertTrue(any("added 1 order" in n for n in s["notes"]), s)
        self.assertEqual(config.load()["disabled_reason"], "")
        self.assertAlmostEqual(before - ledger.available(config.load()), 9 + client.fee(10, 0.9))
        self.assertIsNone(db.get("restored"))
        engine.run_once(c)   # done once: no copies
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM orders WHERE order_id = 'new1'")[0]["n"], 1)

    def test_after_a_restore_positions_the_books_dont_know_stop_once(self):
        # Kalshi's fills missed a trade made after the backup: its position must not be
        # waved through as "the owner's own business" (round-3 RestoreBlindSpot).
        db.insert("orders", ticker="OLD-1", side="no", count=10, price=0.5, filled=10, status="executed",
                  order_id="o0", client_order_id="c0")
        maintenance.backup()
        maintenance.restore(maintenance.backups()[0][0])
        c = FakeKalshi(balance=10_000)
        c.market_info["OLD-1"] = {"ticker": "OLD-1", "status": "active", "no_bid_dollars": "0.4900"}
        c.position_list = [{"ticker": "OLD-1", "position": -10, "exposure": 5},
                           {"ticker": "NEW-1", "position": 100, "exposure": 90}]
        config.clear_trip()
        config.update({"enabled": True})
        engine.run_once(c)
        self.assertIn("books don't know: NEW-1", config.load()["disabled_reason"])
        config.clear_trip()   # "they're mine": the owner's call, once
        config.update({"enabled": True})
        engine.run_once(c)
        self.assertEqual(config.load()["disabled_reason"], "")

    def test_a_second_restore_keeps_the_first_ones_start(self):
        db.insert("orders", ticker="OLD", side="no", count=5, price=0.8, filled=5, status="executed",
                  order_id="old1", client_order_id="c0")
        db.put("restored", {"day": "2026-10-01", "since": 1000.0})   # never caught up
        maintenance.backup()
        maintenance.restore(maintenance.backups()[0][0])
        self.assertEqual(db.get("restored")["since"], 1000.0)

    def test_a_fill_it_cant_read_stops_the_bot(self):
        db.put("restored", {"day": "2026-10-24", "since": time.time() - 86400})
        c = FakeKalshi()
        c.fill_list = [{"order_id": "x", "ticker": "N", "count_fp": "10.00", "price": "0.9"}]
        engine.run_once(c)
        self.assertIn("can't read Kalshi's fills", config.load()["disabled_reason"])
        self.assertTrue(db.get("restored"))   # still waiting: nothing guessed


class BudgetTypoTests(TempHome):
    def test_daily_stop_uses_what_the_account_can_lose(self):
        cfg = {**config.DEFAULTS, "budget": 3000.0}
        risk.daily_loss(cfg, 3000.0, 300.0)   # starts the day
        self.assertIsNone(risk.daily_loss(cfg, 2975.0, 300.0))
        self.assertIn("daily limit", risk.daily_loss(cfg, 2965.0, 300.0))
        self.assertIsNone(risk.daily_loss(cfg, 2965.0))   # the old, budget-only base

    def test_a_budget_bigger_than_the_account_still_stops_on_a_bad_day(self):
        config.update({"enabled": True, "budget": 3000, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})
        orders_proven()
        c = fake_with_arb(balance=100)
        engine.run_once(c)
        for m in c.market_info.values():
            m["no_bid_dollars"] = "0.0100"
        c.event_list = []
        engine.run_once(c)
        self.assertIn("daily limit", config.load()["disabled_reason"])


class StrictAccountListTests(unittest.TestCase):
    def client(self, reply):
        k = client.Kalshi.__new__(client.Kalshi)
        k.get = lambda endpoint, params=None: reply
        return k

    def test_a_renamed_list_key_is_an_error_not_an_empty_account(self):
        k = self.client({"positions": [{"ticker": "A", "position": -40}], "cursor": ""})
        with self.assertRaises(KalshiError):
            k.positions()
        with self.assertRaises(KalshiError):
            self.client({"order_list": [], "cursor": ""}).orders()

    def test_an_empty_or_null_list_is_empty(self):
        self.assertEqual(self.client({"market_positions": [], "cursor": ""}).positions(), [])
        self.assertEqual(self.client({"orders": None, "cursor": ""}).orders(), [])

    def test_market_lists_stay_lenient(self):
        self.assertEqual(self.client({"cursor": ""}).markets(), [])


if __name__ == "__main__":
    unittest.main()


class InterestTests(TempHome):
    """Kalshi pays interest on cash (RESEARCH.md idea #12). It lands in the account, never
    in the bot's books, so it can't stretch the rule that the bot loses at most its budget.
    The same holds for a deposit or anything else the bot didn't trade."""

    def test_money_the_bot_did_not_trade_never_raises_what_it_may_spend(self):
        config.update({"budget": 300.0})
        cfg = config.load()
        c = FakeKalshi(balance=300.0)
        before = (ledger.capital(cfg), ledger.available(cfg), ledger.equity(c, cfg))
        for balance in (301.0, 310.0, 1000.0):     # a month of interest, a year, a deposit
            c._balance = balance
            self.assertEqual((ledger.capital(cfg), ledger.available(cfg), ledger.equity(c, cfg)), before)
        self.assertEqual(before[1], 300.0)


# ---------- holes the 2026-10-25 evening audit found ----------

def _all_off():
    return {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}


class RestoredOrdersTests(TempHome):
    """After a restore, catch_up imports every buy since the backup, the owner's own too.
    Their losses count against the bot; their gains never count, so the owner's winnings
    can't become money the bot may lose."""

    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300, "strategies": _all_off()})
        db.put("books_exist", True)
        maintenance.backup()
        maintenance.restore(maintenance.backups()[0][0])
        self.c = FakeKalshi(balance=1000)
        # After the backup, the owner bought 100 YES at $0.10 by hand on the same account.
        self.c.fill_list = [{"order_id": "own1", "ticker": "OWN-1", "side": "yes", "action": "buy",
                             "count_fp": "100.00", "yes_price_dollars": "0.1000",
                             "created_time": "2026-10-24T10:00:00Z"}]
        self.c.position_list = [{"ticker": "OWN-1", "position": 100, "exposure": 10}]
        self.c.market_info["OWN-1"] = {"ticker": "OWN-1", "status": "active", "yes_bid_dollars": "0.1000"}
        config.clear_trip()
        config.update({"enabled": True})
        engine.run_once(self.c)   # catch_up adds it as the bot's
        self.assertEqual(config.load()["disabled_reason"], "")
        self.cost = 10 + client.fee(100, 0.10)

    def _settle(self, result):
        self.c.market_info["OWN-1"] = {"ticker": "OWN-1", "status": "finalized", "result": result,
                                       "settlement_value_dollars": "1.0000" if result == "yes" else "0.0000"}
        self.c.position_list = []
        engine.run_once(self.c)

    def test_a_win_on_an_imported_order_never_grows_capital(self):
        self._settle("yes")   # the owner's $10 bet pays $100
        self.assertAlmostEqual(ledger.realized(), 0)
        self.assertLessEqual(ledger.capital(config.load()), 300 + 1e-9)

    def test_a_loss_on_an_imported_order_still_counts(self):
        self._settle("no")
        self.assertAlmostEqual(ledger.realized(), -self.cost)


class DailyStopTests(TempHome):
    """README: "Lose over 10% of the budget in a day, and it turns itself off." The day is
    measured by profit and loss, so a budget change can't hide a loss or fake one."""

    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300, "strategies": _all_off()})
        # 200 NO at $0.50 ($103.50 with the fee), marked at $0.50.
        db.insert("orders", ticker="M-1", side="no", count=200, price=0.5, filled=200,
                  status="executed", order_id="o1", client_order_id="c1")
        self.c = FakeKalshi(balance=1000)
        self.c.market_info["M-1"] = {"ticker": "M-1", "status": "active", "no_bid_dollars": "0.5000"}
        self.c.position_list = [{"ticker": "M-1", "position": -200, "exposure": 100}]
        engine.run_once(self.c)   # the UTC day starts here
        self.assertEqual(config.load()["disabled_reason"], "")

    def _lose_it(self):   # M-1 settles against the bot: the $100 it was marked at is gone
        self.c.market_info["M-1"] = {"ticker": "M-1", "status": "finalized", "result": "yes",
                                     "settlement_value_dollars": "1.0000"}
        self.c.position_list = []
        engine.run_once(self.c)

    def test_a_bad_day_stops_it(self):
        self._lose_it()
        self.assertIn("daily limit", config.load()["disabled_reason"])

    def test_a_same_day_budget_raise_doesnt_hide_the_loss(self):
        config.update({"budget": 600})
        self._lose_it()
        # $100 lost today is over 10% of even the new $600 budget.
        self.assertIn("daily limit", config.load()["disabled_reason"])

    def test_a_budget_cut_is_not_a_loss(self):
        config.update({"budget": 150})
        engine.run_once(self.c)
        self.assertEqual(config.load()["disabled_reason"], "")

    def test_a_day_that_started_before_this_change_still_counts(self):
        start = db.get("day_start")
        db.put("day_start", {"day": start["day"], "equity": start["equity"]})   # the old record, no pnl
        self._lose_it()
        self.assertIn("daily limit", config.load()["disabled_reason"])


class SoldThenSettledTests(TempHome):
    """A run sees contracts sold outside the bot in Kalshi's positions only while the market
    trades. Kalshi's fills still show the sale after that, so the payout isn't booked."""

    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300, "strategies": _all_off()})
        db.insert("orders", ticker="S-1", side="no", count=20, price=0.9, filled=20,
                  status="executed", order_id="o1", client_order_id="c1")
        self.cost = 18 + client.fee(20, 0.9)
        self.c = FakeKalshi(balance=1000)
        self.c.fill_list = [self.fill("o1", "buy")]   # the bot's own buy
        self.c.market_info["S-1"] = {"ticker": "S-1", "status": "active", "no_bid_dollars": "0.9000"}
        self.c.position_list = [{"ticker": "S-1", "position": -20, "exposure": 18}]
        engine.run_once(self.c)   # books match Kalshi
        self.assertEqual(config.load()["disabled_reason"], "")

    def fill(self, oid, action, side="no", n=20, p=0.9):
        return {"order_id": oid, "ticker": "S-1", "side": side, "action": action, "count_fp": f"{n:.2f}",
                f"{side}_price_dollars": f"{p:.4f}", "created_time": "2026-10-25T10:00:00Z"}

    def _settle_no(self):
        self.c.position_list = []
        self.c.market_info["S-1"] = {"ticker": "S-1", "status": "finalized", "result": "no",
                                     "settlement_value_dollars": "0.0000"}
        engine.run_once(self.c)

    def test_sold_while_the_bot_was_stopped(self):
        # A half-built-basket stop says "Check open positions on Kalshi": the owner sells the
        # leg, the market settles, then the owner presses Reset.
        config.trip("basket left half-built on S. Check open positions on Kalshi.")
        self.c.fill_list.append(self.fill("owner-1", "sell"))
        config.clear_trip()
        config.update({"enabled": True})
        self._settle_no()
        self.assertAlmostEqual(ledger.realized(), -self.cost)   # never the $20 payout

    def test_sold_just_before_the_close(self):
        self.c.fill_list.append(self.fill("owner-1", "sell"))
        self.c.position_list = []
        self.c.market_info["S-1"] = {"ticker": "S-1", "status": "closed"}
        engine.run_once(self.c)   # closed: Kalshi may be paying out, so nothing written off yet
        self.assertAlmostEqual(ledger.realized(), 0)
        self._settle_no()
        self.assertAlmostEqual(ledger.realized(), -self.cost)

    def test_buying_the_other_side_nets_contracts_away(self):
        self.c.fill_list.append(self.fill("owner-1", "buy", side="yes", n=5, p=0.1))
        self._settle_no()
        self.assertAlmostEqual(ledger.realized(), 15 - self.cost)   # 15 NO left were paid

    def test_the_owners_own_round_trip_changes_nothing(self):
        self.c.fill_list += [self.fill("owner-1", "buy", n=10), self.fill("owner-2", "sell", n=10)]
        self._settle_no()
        self.assertAlmostEqual(ledger.realized(), 20 - self.cost)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM writeoffs")[0]["n"], 0)

    def test_a_fill_it_cant_read_stops_it(self):
        self.c.fill_list.append({"order_id": "x", "ticker": "S-1", "count_fp": "5.00"})
        self._settle_no()
        self.assertIn("can't read Kalshi's fills", config.load()["disabled_reason"])
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM settlements")[0]["n"], 0)   # nothing guessed


class DemoPracticeTests(TempHome):
    """Strategy modes, the budget and the money limits are one setting for both exchanges.
    Back on prod each gets what prod had: one turned live for practice on demo (fake money)
    goes back to paper, a practice budget or cap goes back to the real one, and the owner
    is told."""

    def cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_a_strategy_live_for_demo_practice_buys_nothing_real(self):
        from tests.test_arb_strategies import fake_for, ladder_event, set_book
        config.update({"env": "demo", "budget": 60, "max_per_arb": 30, "strategies": _all_off()})
        config.update({"strategies": {"ladder_arb": {"mode": "live"}}})   # fake-money practice
        config.switch({"env": "prod"})                                    # then real money
        self.assertEqual(config.load()["strategies"]["ladder_arb"]["mode"], "paper")
        config.update({"enabled": True, "budget": 60})   # no real budget was saved: set on prod
        orders_proven("prod")
        c = fake_for(ladder_event())
        set_book(c, "LAD-60", .62, .64, bid_size=15)
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual(c.orders, [])

    def test_basket_arb_stays_live(self):   # live by default: the owner chose it on prod
        config.update({"env": "demo"})
        config.switch({"env": "prod"})
        self.assertEqual(config.load()["strategies"]["basket_arb"]["mode"], "live")

    def test_going_to_demo_changes_no_mode(self):
        config.update({"strategies": {"ladder_arb": {"mode": "live"}}})   # on prod, on purpose
        config.switch({"env": "demo"})
        self.assertEqual(config.load()["strategies"]["ladder_arb"]["mode"], "live")

    def test_kal_on_says_what_trades_and_what_went_back(self):
        config.update({"env": "demo", "budget": 300, "strategies": {"ladder_arb": {"mode": "live"}}})
        code, text = self.cli("on", "--env", "prod")
        self.assertEqual(code, 0)
        self.assertIn("Back to the real-money modes: ladder_arb paper.", text)
        self.assertIn("Live (real money): basket_arb.", text)

    def practice(self, **loose):
        """$300 of real money on prod, then demo with looser settings for practice."""
        config.update({"budget": 300.0})
        config.switch({"env": "demo"})
        config.update(loose)

    def test_a_practice_budget_never_becomes_the_real_one(self):
        self.practice(budget=5000.0)   # practice with more fake money
        self.assertEqual(config.switch({"env": "prod"})["budget"], 300.0)

    def test_practice_limits_never_reach_real_money(self):
        self.practice(budget=1000.0, max_per_arb=500.0, max_per_trade=50.0, kelly_fraction=1.0,
                      daily_loss_limit=1.0, strategies={"bracket_arb": {"mode": "live", "verified": True}})
        cfg = config.switch({"env": "prod"})
        self.assertEqual((cfg["budget"], cfg["max_per_arb"], cfg["max_per_trade"], cfg["kelly_fraction"],
                          cfg["daily_loss_limit"]), (300.0, 30.0, 10.0, 0.25, 0.10))
        self.assertEqual(cfg["strategies"]["bracket_arb"], {"mode": "paper", "verified": False})
        config.update({"enabled": True, "strategies": {**_all_off(), "basket_arb": {"mode": "live"}}})
        orders_proven("prod")
        c = fake_with_arb(balance=1000.0)   # the account holds more than the bot's $300
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertTrue(c.orders)
        self.assertLessEqual(sum(n * p for _, _, n, p in c.orders), 30.0)   # max_per_arb, not 500

    def test_every_money_limit_goes_back(self):
        mine = {"budget": 300.0, "cash_floor": 20.0, "max_per_trade": 8.0, "max_per_arb": 25.0,
                "max_per_event": 15.0, "max_exposure": 0.4, "max_arb_days": 20, "min_arb_apr": 0.3,
                "daily_loss_limit": 0.05, "kelly_fraction": 0.2, "max_consecutive_errors": 2,
                "reinvest_profits": False, "auto_promote": False}
        config.update(mine)
        config.switch({"env": "demo"})
        config.update({"budget": 5000.0, "cash_floor": 0.0, "max_per_trade": 50.0, "max_per_arb": 500.0,
                       "max_per_event": 200.0, "max_exposure": 1.0, "max_arb_days": 365, "min_arb_apr": 0.0,
                       "daily_loss_limit": 1.0, "kelly_fraction": 1.0, "max_consecutive_errors": 50,
                       "reinvest_profits": True, "auto_promote": True})
        cfg = config.switch({"env": "prod"})
        self.assertEqual({k: cfg[k] for k in mine}, mine)

    def test_a_bracket_check_made_on_demo_buys_nothing_real(self):
        """`set env prod` prints how to turn a strategy live. Doing that for bracket_arb must
        not bring back the hand check made on demo."""
        from tests.test_arb_strategies import bracket_event, fake_for
        config.update({"budget": 60.0, "strategies": _all_off()})
        config.switch({"env": "demo"})
        config.update({"strategies": {"bracket_arb": {"mode": "live", "verified": True}}})
        text = self.cli("set", "env", "prod")[1]
        self.assertIn("set strategies.bracket_arb.mode live", text)
        self.assertEqual(self.cli("set", "strategies.bracket_arb.mode", "live")[0], 0)
        config.update({"enabled": True})
        orders_proven("prod")
        c = fake_for(bracket_event())
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual(c.orders, [])
        self.assertEqual(s["paper_trades"], 1)

    def test_kal_set_env_prod_names_what_went_back(self):
        self.practice(budget=5000.0, kelly_fraction=1.0, strategies={"bracket_arb": {"verified": True}})
        code, text = self.cli("set", "env", "prod")
        self.assertEqual(code, 0)
        self.assertIn("Back to the real-money settings: budget $300, kelly_fraction 0.25, "
                      "bracket_arb.verified false. Settings on demo were practice.", text)

    def test_a_budget_typed_with_the_switch_is_checked_against_prod(self):
        self.practice(budget=3000.0)
        with mock.patch("kal.setup.kalshi_balance", return_value=(True, 5000.0)) as balance, \
                mock.patch.object(cli, "_tty", return_value=False):
            code, text = self.cli("on", "--env", "prod", "--budget", "3000")
        self.assertEqual(code, 2)
        self.assertIn("Not changed. That's more than double the budget now ($300).", text)
        balance.assert_called_once_with("prod")
        cfg = config.load()
        self.assertEqual((cfg["env"], cfg["enabled"]), ("demo", False))
        code, text = self.cli("on", "--env", "prod")   # no budget typed: the real one comes back
        self.assertEqual(code, 0)
        self.assertIn("Budget: $300.00.", text)

    def test_a_config_from_before_this_goes_back_safe(self):
        """It left prod before the settings were saved. Each goes to the safer of where it is
        and its default, so the budget must be set again on prod."""
        config.update({"env": "demo", "budget": 3000.0, "max_per_arb": 500.0, "max_per_trade": 5.0,
                       "strategies": {"bracket_arb": {"verified": True}}})
        cfg = config.switch({"env": "prod"})
        self.assertEqual((cfg["budget"], cfg["max_per_arb"], cfg["max_per_trade"]), (0.0, 30.0, 5.0))
        self.assertIs(cfg["strategies"]["bracket_arb"]["verified"], False)


class UnreadableReplyTests(TempHome):
    """An immediate-or-cancel order that part-fills ends "canceled". A reply that says only
    that can't be read as 0 filled: the order is looked up before anything else trades."""

    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30, "strategies": _all_off()})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})
        orders_proven()

    def test_a_first_leg_reply_with_no_count_is_looked_up(self):
        c = fake_with_arb()

        def buy(ticker, side, count, price, client_order_id=None):
            c.orders.append((ticker, side, count, price))
            return {"order_id": f"o{len(c.orders)}", "status": "canceled"}
        c.buy = buy
        s = engine.run_once(c)
        self.assertFalse(s["ok"], s)
        self.assertEqual(len(c.orders), 1)   # nothing more bought
        self.assertEqual([r["status"] for r in ledger.unresolved_orders()], ["unknown"])

    def test_a_later_leg_reply_with_no_count_stops_the_bot(self):
        c = fake_with_arb()
        real_buy = c.buy

        def buy(ticker, side, count, price, client_order_id=None):
            if c.orders:
                c.orders.append((ticker, side, count, price))
                return {"order_id": f"o{len(c.orders)}", "status": "canceled"}
            return real_buy(ticker, side, count, price, client_order_id)
        c.buy = buy
        engine.run_once(c)
        self.assertIn("half-built", config.load()["disabled_reason"])

    def test_the_test_orders_must_say_how_much_filled(self):
        ok, detail, alarming = gate.judge("T", 0.5, {"order_id": "x", "status": "canceled"})
        self.assertEqual((ok, alarming), (False, False))
        self.assertIn("how much filled", detail)
        ok, _, _ = gate.judge("T", 0.5, {"order_id": "x", "status": "canceled", "fill_count": "0.00"})
        self.assertTrue(ok)


STANDARD = ("If the game is cancelled or rescheduled to over 48 hours away, the market will resolve "
            "to a fair price in accordance with the rules.")


class FairPriceWordingTests(TempHome):
    """plain_payouts keeps "Kalshi picks the price" events off real money. Spacing, line
    breaks and "fair market value" must not slip past it."""

    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30, "strategies": _all_off()})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})
        orders_proven()

    def _live_orders_for(self, rules):
        c = fake_with_arb()
        for m in c.event_list[0]["markets"]:
            m["rules_secondary"] = rules
        engine.run_once(c)
        return c.orders

    def test_plain_rules_still_trade(self):   # the control: this basket does trade
        self.assertNotEqual(self._live_orders_for("The market resolves to Yes if it happens."), [])

    def test_fair_price_wordings_never_trade_real_money(self):
        for rules in (STANDARD, STANDARD.replace("fair price", "fair  price"),
                      STANDARD.replace("fair price", "fair\nprice"), STANDARD.replace(" ", "\u00a0"),
                      "If the person dies before the date, the market will settle at fair market value "
                      "as determined by Kalshi.",
                      "If trading stops, the market settles at the price of the last trade."):
            with self.subTest(rules=rules):
                self.assertEqual(self._live_orders_for(rules), [])

    def test_the_cancelled_game_clause_still_reads_with_odd_spacing(self):
        m = {"rules_secondary": STANDARD.replace(" will ", "  will ").replace("fair price", "fair\u00a0price")}
        self.assertFalse(base.plain_payouts([m]))
        self.assertTrue(base.fair_price_only_if_cancelled([m]))   # paper only, as a bet

    def test_lines_with_no_full_stop_stay_apart(self):
        # Real tennis and golf rules end lines with no full stop. The clause is still its own line.
        m = {"rules_secondary": "If a player retires, the opponent wins\n" + STANDARD + "\nKalshi is not affiliated"}
        self.assertTrue(base.fair_price_only_if_cancelled([m]))

    def test_a_clause_split_across_lines_is_not_trusted(self):
        m = {"rules_secondary": STANDARD.replace("fair price", "fair\nprice")}
        self.assertFalse(base.plain_payouts([m]))
        self.assertFalse(base.fair_price_only_if_cancelled([m]))   # skipped, not even paper

    def test_the_election_clause_is_paper_only(self):
        # Seen on 200+ real election events (2026-10-25); the old exact-text check missed it.
        m = {"rules_secondary": "Party affiliation is set when elected. If the election is postponed beyond one "
                                "year from the original date or cancelled entirely, the market resolves to the "
                                "last fair market price. Only seats actually contested are counted."}
        self.assertFalse(base.plain_payouts([m]))
        self.assertTrue(base.fair_price_only_if_cancelled([m]))

    def test_esports_and_darts_cancel_clauses_are_paper_only(self):
        # Word for word from real events (2026-10-25): the match or map never happens.
        for text in ("If the relevant match is cancelled before play begins, the market will resolve to the fair "
                     "market price.",
                     "If the relevant match or map is postponed and not started within 48 hours of its originally "
                     "scheduled start time, the market will resolve to the fair market price.",
                     "If the relevant map is cancelled before play begins, the market will resolve to the fair "
                     "market price.",
                     "If a match is forfeited before any play occurs, the market will resolve to the fair market "
                     "price.",
                     "If a map is forfeited before any play occurs in the map, the market will resolve to the fair "
                     "market price.",
                     "If the relevant match is forfeited before any play occurs, the market will resolve to the fair "
                     "market price.",
                     "If the cancelled match is not played, rescheduled, or declared a tie, the market will resolve "
                     "to the fair market price."):
            with self.subTest(text=text):
                m = {"rules_secondary": "The market refers to the full match.\n" + text}
                self.assertFalse(base.plain_payouts([m]))
                self.assertTrue(base.fair_price_only_if_cancelled([m]))

    def test_the_exchanges_discretion_is_not_trusted(self):
        # Real tennis rules: Kalshi decides what "can't be settled" means.
        m = {"rules_secondary": "Any markets that cannot be unconditionally settled will resolve to fair market "
                                "price, as determined at the sole discretion of the exchange."}
        self.assertFalse(base.plain_payouts([m]))
        self.assertFalse(base.fair_price_only_if_cancelled([m]))

    def test_golf_fair_market_value_is_not_trusted(self):
        m = {"rules_secondary": "If a golfer withdraws:\nTournament Winner markets will resolve to No\n"
                                "Head-to-Head markets will resolve to Fair Market Value\n\nKalshi is not affiliated"}
        self.assertFalse(base.plain_payouts([m]))
        self.assertFalse(base.fair_price_only_if_cancelled([m]))


# ---------- holes the PR #23 review found (2026-10-25) ----------

def _fill(oid, action, side="no", n=20, p=0.9, ticker="S-1"):
    return {"order_id": oid, "ticker": ticker, "side": side, "action": action, "count_fp": f"{n:.2f}",
            f"{side}_price_dollars": f"{p:.4f}", "created_time": "2026-10-25T10:00:00Z"}


class WrittenOffOnceTests(TempHome):
    """A part sale a run saw in Kalshi's positions (market still open) was written off then.
    At settlement, Kalshi's fills show the same sale: it must not be written off again."""

    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300, "strategies": _all_off()})
        db.insert("orders", ticker="S-1", side="no", count=20, price=0.9, filled=20,
                  status="executed", order_id="o1", client_order_id="c1")
        self.cost = 18 + client.fee(20, 0.9)
        self.c = FakeKalshi(balance=1000)
        self.c.fill_list = [_fill("o1", "buy")]
        self.c.market_info["S-1"] = {"ticker": "S-1", "status": "active", "no_bid_dollars": "0.9000"}
        self.c.position_list = [{"ticker": "S-1", "position": -20, "exposure": 18}]
        engine.run_once(self.c)
        self.assertEqual(config.load()["disabled_reason"], "")

    def test_a_part_sale_seen_while_open_is_written_off_once(self):
        self.c.fill_list.append(_fill("owner-1", "sell", n=5))   # the owner sells 5 of 20
        self.c.position_list = [{"ticker": "S-1", "position": -15, "exposure": 13.5}]
        engine.run_once(self.c)
        self.assertAlmostEqual(sum(r["count"] for r in ledger.open_positions()), 15)
        self.c.position_list = []
        self.c.market_info["S-1"] = {"ticker": "S-1", "status": "finalized", "result": "no",
                                     "settlement_value_dollars": "0.0000"}
        engine.run_once(self.c)   # NO wins: Kalshi pays $15 for the 15 still held
        self.assertAlmostEqual(db.query("SELECT SUM(count) AS n FROM writeoffs")[0]["n"], 5)
        self.assertAlmostEqual(ledger.realized(), 15 - self.cost)


class KeylessSettleTests(TempHome):
    """With no key, a run can't read Kalshi's fills, so it books no live payout: it waits for
    a run that can check them. A payout booked unchecked is never looked at again."""

    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300, "strategies": _all_off()})
        db.insert("orders", ticker="S-1", side="no", count=20, price=0.9, filled=20,
                  status="executed", order_id="o1", client_order_id="c1")
        self.cost = 18 + client.fee(20, 0.9)
        self.c = FakeKalshi(can_trade=False, balance=1000)
        self.c.market_info["S-1"] = {"ticker": "S-1", "status": "finalized", "result": "no",
                                     "settlement_value_dollars": "0.0000"}

    def test_it_waits_for_the_key(self):
        engine.run_once(self.c)
        self.assertAlmostEqual(ledger.realized(), 0)
        self.assertAlmostEqual(ledger.open_cost(), self.cost)   # still counted as spent

    def test_a_sale_while_the_key_was_missing_is_never_booked_as_paid(self):
        self.c.fill_list = [_fill("o1", "buy"), _fill("owner-1", "sell")]
        engine.run_once(self.c)
        self.c.can_trade = True   # the key is back
        engine.run_once(self.c)
        self.assertAlmostEqual(ledger.realized(), -self.cost)   # not +$1.87: Kalshi paid nothing
        self.assertLessEqual(ledger.capital(config.load()), 300 - self.cost + 1e-9)


class RestoredMarksTests(TempHome):
    """Imported (maybe the owner's) orders' gains never count, in equity as at settlement,
    so their win settling doesn't read as a loss to the daily stop."""

    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300, "strategies": _all_off()})
        db.put("books_exist", True)
        maintenance.backup()
        maintenance.restore(maintenance.backups()[0][0])
        self.c = FakeKalshi(balance=1000)
        self.c.fill_list = [_fill("own1", "buy", side="yes", n=100, p=0.10, ticker="OWN-1")]
        self.c.position_list = [{"ticker": "OWN-1", "position": 100, "exposure": 10}]
        self.cost = 10 + client.fee(100, 0.10)

    def _start(self, bid):
        self.c.market_info["OWN-1"] = {"ticker": "OWN-1", "status": "active", "yes_bid_dollars": bid}
        config.clear_trip()
        config.update({"enabled": True})
        engine.run_once(self.c)   # catch_up imports it; the UTC day starts here
        self.assertEqual(config.load()["disabled_reason"], "")

    def _settle(self, result):
        self.c.market_info["OWN-1"] = {"ticker": "OWN-1", "status": "finalized", "result": result,
                                       "settlement_value_dollars": "1.0000" if result == "yes" else "0.0000"}
        self.c.position_list = []
        engine.run_once(self.c)

    def test_its_win_settling_is_not_a_daily_loss(self):
        self._start("0.9500")
        self.assertAlmostEqual(ledger.equity(self.c, config.load()), 300)   # never above its cost
        self._settle("yes")
        self.assertAlmostEqual(ledger.realized(), 0)
        self.assertEqual(config.load()["disabled_reason"], "")

    def test_its_loss_still_counts(self):
        self._start("0.0500")
        self.assertAlmostEqual(ledger.equity(self.c, config.load()), 300 - self.cost + 5)
        self._settle("no")
        self.assertAlmostEqual(ledger.realized(), -self.cost)


class ReconcileUnreadableTests(TempHome):
    """A lookup that says only "canceled" can hide a part fill, like a reply that does. It is
    never booked as 0 filled: the order stays unknown and the bot stops for a person."""

    def test_a_lookup_that_says_only_canceled_stops_the_bot(self):
        config.update({"enabled": True, "budget": 300, "strategies": _all_off()})
        db.insert("orders", ts=time.time(), ticker="T-1", side="no", count=5, price=0.9, filled=0,
                  status="unknown", order_id="o1", client_order_id="c1")
        c = FakeKalshi(balance=1000)
        c.order_list = [{"order_id": "o1", "client_order_id": "c1", "ticker": "T-1", "status": "canceled"}]
        c.market_info["T-1"] = {"ticker": "T-1", "status": "active", "no_bid_dollars": "0.9000"}
        c.position_list = [{"ticker": "T-1", "position": -3, "exposure": 2.7}]   # 3 of 5 filled
        engine.run_once(c)
        row = db.query("SELECT status, filled FROM orders WHERE client_order_id = 'c1'")[0]
        self.assertEqual(row["status"], "unknown")
        self.assertIn("can't tell how much order c1 on T-1 filled", config.load()["disabled_reason"])


class ProdModesTests(TempHome):
    """Back on prod, each strategy gets the mode it had there. A demo round trip can't turn
    real money on, or off, behind the owner's back."""

    def test_basket_arb_set_to_paper_on_prod_stays_paper(self):
        config.update({"strategies": {"basket_arb": {"mode": "paper"}}})   # no real money for now
        config.switch({"env": "demo"})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})    # fake-money practice
        config.switch({"env": "prod"})
        self.assertEqual(config.load()["strategies"]["basket_arb"]["mode"], "paper")

    def test_a_strategy_chosen_live_on_prod_stays_live(self):
        config.update({"strategies": {"ladder_arb": {"mode": "live"}}})    # on prod, on purpose
        config.switch({"env": "demo"})
        config.switch({"env": "prod"})
        self.assertEqual(config.load()["strategies"]["ladder_arb"]["mode"], "live")

    def test_off_on_prod_comes_back_off(self):
        config.update({"strategies": {"mentions": {"mode": "off"}}})
        config.switch({"env": "demo"})
        config.update({"strategies": {"mentions": {"mode": "live"}}})
        config.switch({"env": "prod"})
        self.assertEqual(config.load()["strategies"]["mentions"]["mode"], "off")

    def test_kal_set_env_says_what_changed(self):
        config.update({"strategies": {"basket_arb": {"mode": "paper"}}})
        config.switch({"env": "demo"})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["set", "env", "prod"])
        self.assertIn("Back to the real-money modes: basket_arb paper.", out.getvalue())


class GateVersionTests(TempHome):
    def test_a_pass_from_before_fill_counts_were_needed_is_proved_again(self):
        c = FakeKalshi()
        gate.record(c, "prod", True, "passed under the old rule")
        rec = db.get(gate.KEY)
        db.put(gate.KEY, {**rec, "version": 2})
        self.assertFalse(gate.passed(c, "prod"))
        gate.record(c, "prod", True, "passed again")
        self.assertTrue(gate.passed(c, "prod"))

