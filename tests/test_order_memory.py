"""The bot's books are its own memory, and a disk can be lost. So every order the bot sends
carries a marked client_order_id, and Kalshi keeps it for good. A live run puts back any
marked order that bought something and that the books don't have, so the budget still
counts its losses after a lost, replaced or rolled-back disk."""
import time
import uuid
from unittest import mock

from kal import client, config, db, engine, ledger
from kal.client import BOT_MARK, KalshiError, new_order_id
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven
from tests.test_core import fake_with_arb

DAY = 86400


def _all_off():
    return {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def order(coid, ticker="M-1", outcome="yes", n=10, p=0.5, ago=3600, status="executed"):
    """A bot order as Kalshi's order list shows it (V2 shape, no deprecated side/action)."""
    yes = p if outcome == "yes" else 1 - p
    return {"order_id": f"k-{coid}", "client_order_id": coid, "ticker": ticker, "outcome_side": outcome,
            "book_side": "bid" if outcome == "yes" else "ask", "status": status,
            "yes_price_dollars": f"{yes:.4f}", "no_price_dollars": f"{1 - yes:.4f}",
            "fill_count_fp": f"{n:.2f}", "remaining_count_fp": "0.00", "initial_count_fp": f"{n:.2f}",
            "created_time": iso(time.time() - ago)}


class MarkTests(TempHome):
    def test_every_bot_order_id_is_a_marked_uuid(self):
        ids = {new_order_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        for i in ids:
            self.assertTrue(i.startswith(BOT_MARK))
            self.assertEqual(str(uuid.UUID(i)), i)   # still a plain UUID, the shape Kalshi's examples use

    def test_live_trades_and_test_orders_use_marked_ids(self):
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30, "strategies": _all_off()})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})
        orders_proven()
        c = fake_with_arb()
        sent = []
        real_buy = c.buy

        def buy(ticker, side, count, price, client_order_id=None):
            sent.append(client_order_id)
            return real_buy(ticker, side, count, price, client_order_id)
        c.buy = buy
        s = engine.run_once(c)
        self.assertGreater(s["live_trades"], 0, s)
        self.assertTrue(sent and all(i.startswith(BOT_MARK) for i in sent), sent)
        k = client.Kalshi()
        k._req = lambda method, endpoint, params=None, body=None: sent.append(body["client_order_id"]) or \
            type("R", (), {"status_code": 201, "text": "{}", "json": lambda self: {}})()
        k.buy("M-1", "yes", 1, 0.01)    # the zero-risk test orders go through here
        self.assertTrue(sent[-1].startswith(BOT_MARK))


class RecoverTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300, "strategies": _all_off()})
        self.c = FakeKalshi(balance=1000)
        self.c.cutoffs = {"orders_updated_ts": time.time() - 15 * DAY, "trades_created_ts": time.time() - 60 * DAY}
        self.c.position_list = []
        self.mine = new_order_id()

    def settled(self, ticker="M-1", result="no"):
        self.c.market_info[ticker] = {"ticker": ticker, "status": "finalized", "result": result,
                                      "settlement_value_dollars": "1.0000" if result == "yes" else "0.0000"}

    def test_lost_books_get_the_bots_orders_back(self):
        # A new disk: empty books, but Kalshi still has the bot's order. Its market settled
        # NO since. Without this, the $5.09 lost would never count against the budget.
        self.c.order_list = [order(self.mine)]
        self.settled()
        s = engine.run_once(self.c)
        self.assertTrue(any("back in" in n for n in s["notes"]), s)
        self.assertAlmostEqual(ledger.realized(), -(5 + client.fee(10, 0.5)))
        row = db.query("SELECT strategy, status, client_order_id FROM orders")[0]
        self.assertEqual((row["strategy"], row["status"], row["client_order_id"]), ("restored", "recovered", self.mine))
        self.assertEqual(config.load()["disabled_reason"], "")

    def test_on_new_books_a_win_put_back_never_grows_the_budget(self):
        # New books can't see losses from before the mark, so only losses count there.
        self.c.order_list = [order(self.mine)]
        self.settled(result="yes")
        engine.run_once(self.c)
        self.assertAlmostEqual(ledger.realized(), 0)

    def test_on_rolled_back_books_a_win_counts_in_full(self):
        # An older copy still holds what came before: the bot's own order counts as it was.
        db.put("orders_checked", time.time() - 3 * DAY)
        self.c.order_list = [order(self.mine, ago=DAY)]
        self.settled(result="yes")
        engine.run_once(self.c)
        self.assertAlmostEqual(ledger.realized(), 10 - (5 + client.fee(10, 0.5)))

    def test_an_order_the_store_holds_comes_back_too(self):
        self.c.archived_orders = [order(self.mine, ago=40 * DAY)]
        self.settled()
        engine.run_once(self.c)
        self.assertAlmostEqual(ledger.realized(), -(5 + client.fee(10, 0.5)))

    def test_rolled_back_books_get_the_later_orders(self):
        # A copy from before: its last check was 3 days ago, and the bot traded since.
        db.put("orders_checked", time.time() - 3 * DAY)
        self.c.honor_min_ts = True
        self.c.order_list = [order(new_order_id(), ago=5 * DAY),   # before the last check: the books have it
                             order(self.mine, ago=DAY)]
        db.insert("orders", ticker="M-1", side="yes", count=10, price=0.5, filled=10, status="executed",
                  order_id=f"k-{self.c.order_list[0]['client_order_id']}",
                  client_order_id=self.c.order_list[0]["client_order_id"], ts=time.time() - 5 * DAY)
        self.settled()
        engine.run_once(self.c)
        self.assertEqual(len(db.query("SELECT id FROM orders")), 2)
        self.assertAlmostEqual(ledger.realized(), -2 * (5 + client.fee(10, 0.5)))

    def test_the_owners_orders_and_unfilled_ones_are_left_alone(self):
        self.c.order_list = [order(str(uuid.uuid4())),                     # the owner's own
                             order("", ticker="M-2"),                       # the website's: no id at all
                             order(new_order_id(), n=0, status="canceled")]  # a test order: bought nothing
        engine.run_once(self.c)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM orders")[0]["n"], 0)

    def test_an_order_the_books_have_is_never_added_twice(self):
        db.insert("orders", ticker="M-1", side="yes", count=10, price=0.5, filled=10, status="executed",
                  order_id=f"k-{self.mine}", client_order_id=self.mine)
        self.c.order_list = [order(self.mine)]
        self.c.position_list = [{"ticker": "M-1", "position": 10, "exposure": 5}]
        self.c.market_info["M-1"] = {"ticker": "M-1", "status": "active", "yes_bid_dollars": "0.5000"}
        for _ in range(2):
            engine.run_once(self.c)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM orders")[0]["n"], 1)

    def test_one_the_books_called_never_placed_is_corrected(self):
        db.insert("orders", strategy="favorites", ticker="M-1", side="yes", count=10, price=0.5, filled=0,
                  status="never_placed", client_order_id=self.mine, ts=time.time() - 2 * DAY)
        self.c.order_list = [order(self.mine)]
        self.settled()
        engine.run_once(self.c)
        rows = db.query("SELECT strategy, status, filled FROM orders")
        self.assertEqual([(r["strategy"], r["status"], r["filled"]) for r in rows], [("favorites", "recovered", 10)])
        self.assertAlmostEqual(ledger.realized(), -(5 + client.fee(10, 0.5)))

    def test_a_marked_order_it_cant_read_stops_the_bot(self):
        o = order(self.mine)
        for k in ("fill_count_fp", "remaining_count_fp", "initial_count_fp"):
            del o[k]
        o["status"] = "canceled"   # "canceled" alone can hide a part fill
        self.c.order_list = [o]
        engine.run_once(self.c)
        self.assertIn("can't read the bot's own orders", config.load()["disabled_reason"])

    def test_after_a_check_it_looks_back_only_to_then(self):
        self.c.honor_min_ts = True
        engine.run_once(self.c)
        checked = db.get("orders_checked")
        self.assertAlmostEqual(checked, time.time(), delta=60)
        reads = []
        real = self.c.orders.__call__
        self.c.orders = lambda ticker=None, min_ts=None, archive=False: reads.append(min_ts) or real(ticker, min_ts, archive)
        engine.run_once(self.c)
        self.assertTrue(reads and all(m is not None and m >= checked - ledger.RECOVER_SLACK - 1 for m in reads), reads)

    def test_a_kalshi_outage_waits(self):
        self.c.archive_error = KalshiError("GET /historical/orders -> 503", 503)
        s = engine.run_once(self.c)   # first check: the whole account, the store too
        self.assertTrue(s.get("transient"), s)
        self.assertIsNone(db.get("orders_checked"))   # checked next time instead


class RecoverReviewTests(TempHome):
    """From the independent review of this change."""

    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300, "strategies": _all_off()})
        self.c = FakeKalshi(balance=1000)
        self.c.cutoffs = {"orders_updated_ts": time.time() - 15 * DAY, "trades_created_ts": time.time() - 60 * DAY}
        self.c.position_list = []
        self.mine = new_order_id()

    def test_a_basket_leg_found_filled_after_all_stops_the_bot(self):
        # Leg 1 of 2 was booked never placed and the basket closed as "missed". Kalshi shows
        # it filled after all: half a basket is held, so the bot must stop, not trade on.
        legs = [{"ticker": f"B-{i}", "side": "no", "price": 0.5, "count": 10} for i in range(2)]
        sid = db.insert("signals", strategy="basket_arb", event="B", title="t", mode="live", guaranteed=1,
                        cost=10, payout_min=10, exp_profit=0.5, legs=legs, status="missed (no fill)")
        db.insert("orders", signal_id=sid, strategy="basket_arb", ticker="B-0", side="no", count=10, price=0.5,
                  filled=0, status="never_placed", client_order_id=self.mine, ts=time.time() - 1800)
        db.put("orders_checked", time.time() - 600)
        self.c.order_list = [order(self.mine, ticker="B-0", outcome="no", ago=1800)]
        self.c.position_list = [{"ticker": "B-0", "position": -10, "exposure": 5}]
        self.c.market_info["B-0"] = {"ticker": "B-0", "status": "active", "no_bid_dollars": "0.5000"}
        engine.run_once(self.c)
        self.assertIn("half-built", config.load()["disabled_reason"])
        self.assertEqual(db.query("SELECT status FROM signals")[0]["status"], "stranded: 1 of 2 legs bought")

    def test_one_found_after_its_market_was_booked_counts_as_lost(self):
        db.insert("settlements", ticker="M-1", settled_ts=time.time(), result="yes", revenue=0, cost=0, pnl=0)
        self.c.order_list = [order(self.mine)]
        engine.run_once(self.c)
        self.assertAlmostEqual(ledger.realized(), -(5 + client.fee(10, 0.5)))   # never left out

    def test_old_style_fields_only_cross_check_the_side(self):
        # A NO buy is an ask on the YES book; Kalshi may call it "sell YES" in the old fields.
        no_buy = order(self.mine, outcome="no", p=0.7)
        no_buy.update(side="yes", action="sell")
        yes_buy = order(new_order_id(), ticker="M-2", outcome="yes", p=0.4)
        yes_buy.update(side="yes", action="buy")
        self.c.order_list = [no_buy, yes_buy]
        engine.run_once(self.c)
        got = {(r["ticker"], r["side"], r["price"]) for r in db.query("SELECT ticker, side, price FROM orders")}
        self.assertEqual(got, {("M-1", "no", 0.7), ("M-2", "yes", 0.4)})
        self.assertEqual(config.load()["disabled_reason"], "")

    def assertStops(self, o, words):
        self.c.order_list = [o]
        engine.run_once(self.c)
        self.assertIn(words, config.load()["disabled_reason"])
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM orders")[0]["n"], 0)

    def test_fields_that_disagree_stop_the_bot(self):
        o = order(self.mine, outcome="no")
        o.update(side="yes", action="buy")   # says long YES and long NO at once
        self.assertStops(o, "can't read the bot's own orders")

    def test_executed_with_no_counts_is_not_a_number(self):
        # "executed" alone once read as "all of it": with no size asked, that was infinity.
        o = order(self.mine)
        for k in ("fill_count_fp", "remaining_count_fp", "initial_count_fp"):
            del o[k]
        self.assertStops(o, "doesn't say how much it bought")

    def test_an_order_with_no_order_id_stops_the_bot(self):
        o = order(self.mine)
        o["order_id"] = ""
        self.assertStops(o, "no order id")


class RecoverAtomicTests(TempHome):
    """Each order's writes go in one transaction: a run killed part way redoes all of it
    (second review of this change)."""

    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300, "strategies": _all_off()})
        self.c = FakeKalshi(balance=1000)
        self.c.cutoffs = {"orders_updated_ts": time.time() - 15 * DAY, "trades_created_ts": time.time() - 60 * DAY}
        self.mine = new_order_id()
        db.put("orders_checked", time.time() - 600)

    def test_a_write_off_that_fails_leaves_nothing_half_done(self):
        db.insert("settlements", ticker="M-1", settled_ts=time.time(), result="yes", revenue=0, cost=0, pnl=0)
        self.c.order_list = [order(self.mine, ago=1800)]
        with mock.patch.object(ledger, "fee", side_effect=OSError("database is locked")):
            with self.assertRaises(OSError):
                ledger.recover(self.c)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM orders")[0]["n"], 0)   # rolled back
        self.assertEqual(ledger.recover(self.c), 1)                                    # done again, whole
        self.assertAlmostEqual(ledger.realized(), -(5 + client.fee(10, 0.5)))

    def test_a_basket_check_that_fails_leaves_the_leg_unbooked(self):
        legs = [{"ticker": f"B-{i}", "side": "no", "price": 0.5, "count": 10} for i in range(2)]
        sid = db.insert("signals", strategy="basket_arb", event="B", title="t", mode="live", guaranteed=1,
                        cost=10, payout_min=10, exp_profit=0.5, legs=legs, status="missed (no fill)")
        db.insert("orders", signal_id=sid, strategy="basket_arb", ticker="B-0", side="no", count=10, price=0.5,
                  filled=0, status="never_placed", client_order_id=self.mine, ts=time.time() - 1800)
        self.c.order_list = [order(self.mine, ticker="B-0", outcome="no", ago=1800)]
        broken = mock.Mock(loads=mock.Mock(side_effect=OSError("killed")))
        with mock.patch.object(ledger, "json", broken):
            with self.assertRaises(OSError):
                ledger.recover(self.c)
        self.assertEqual(db.query("SELECT status FROM orders")[0]["status"], "never_placed")   # rolled back
        ledger.recover(self.c)
        self.assertEqual(db.query("SELECT status FROM signals")[0]["status"], "failed: a leg Kalshi shows filled after all")

    def test_a_negative_count_is_a_misread_not_nothing(self):
        for fields in ({"fill_count_fp": "-10.00"},
                       {"fill_count_fp": None, "initial_count_fp": "5.00", "remaining_count_fp": "9.00"}):
            o = order(new_order_id())
            o.update(fields)
            if fields.get("fill_count_fp", 0) is None:
                del o["fill_count_fp"]
            self.c.order_list = [o]
            with self.assertRaises(ledger.CatchUpError):
                ledger.recover(self.c)


class NullCountTests(TempHome):
    """A count that is there but null or empty says nothing: never "bought nothing" (third
    review of this change). Read as 0, the order was skipped and looked past for good."""

    def setUp(self):
        super().setUp()
        self.c = FakeKalshi(balance=1000)
        self.c.cutoffs = {"orders_updated_ts": time.time() - 15 * DAY}

    def shape(self, **counts):
        o = order(new_order_id())
        for k in ("fill_count_fp", "remaining_count_fp", "initial_count_fp"):
            del o[k]
        o.update(counts)
        self.c.order_list = [o]

    def test_a_null_fill_count_falls_back_to_initial_less_remaining_once_done(self):
        self.shape(fill_count_fp=None, initial_count_fp="10.00", remaining_count_fp="0.00", status="executed")
        self.assertEqual(ledger.recover(self.c), 1)
        self.assertEqual(db.query("SELECT filled FROM orders")[0]["filled"], 10)

    def test_on_a_cancelled_order_initial_less_remaining_is_no_count(self):
        # Kalshi zeroes "remaining" when it cancels the rest: 10 less 0 isn't 10 bought (fourth review).
        self.shape(fill_count_fp=None, initial_count_fp="10.00", remaining_count_fp="0.00", status="canceled")
        with self.assertRaises(ledger.CatchUpError):
            ledger.recover(self.c)

    def test_odd_numbers_are_misreads(self):
        for counts in ({"fill_count": True}, {"fill_count": False}, {"fill_count": 10 ** 400},
                       {"fill_count_fp": "12.00", "initial_count_fp": "10.00"}):
            with self.subTest(counts=counts):
                self.shape(**counts)
                with self.assertRaises(ledger.CatchUpError):
                    ledger.recover(self.c)

    def test_counts_that_say_nothing_stop_it(self):
        for counts in ({"fill_count_fp": ""}, {"fill_count_fp": None},
                       {"initial_count_fp": None, "remaining_count_fp": "0.00"},
                       {"initial_count_fp": "10.00", "remaining_count_fp": ""}):
            with self.subTest(counts=counts):
                self.shape(**counts)
                with self.assertRaises(ledger.CatchUpError):
                    ledger.recover(self.c)
                self.assertIsNone(db.get("orders_checked"))   # never looked past

    def test_plain_numbers_and_fractions_still_read(self):
        self.shape(fill_count=3, initial_count_fp="7.50", remaining_count_fp="4.50")
        self.assertEqual(ledger.recover(self.c), 1)
        self.shape(fill_count_fp="2.25")
        self.assertEqual(ledger.recover(self.c), 1)
        self.assertEqual(sorted(r["filled"] for r in db.query("SELECT filled FROM orders")), [2.25, 3])
