"""Holes the round-3 reviewers found in the money path, each shown and then closed."""
import json
import time
import unittest
from unittest import mock

import requests

from kal import client, config, db, engine, ledger, maintenance, paper, risk
from kal.client import KalshiError
from kal.strategies import basket_arb
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
