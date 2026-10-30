"""The bot's books are its own memory, and a disk can be lost. So every order the bot sends
carries a marked client_order_id, and Kalshi keeps it for good. A live run puts back any
marked order that bought something and that the books don't have, so the budget still
counts its losses after a lost, replaced or rolled-back disk."""
import time
import uuid

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

    def test_a_win_put_back_never_grows_the_budget(self):
        self.c.order_list = [order(self.mine)]
        self.settled(result="yes")
        engine.run_once(self.c)
        self.assertLessEqual(ledger.realized(), 0)   # restored orders: losses count, gains don't

    def test_an_order_the_store_holds_comes_back_too(self):
        self.c.archived_orders = [order(self.mine, ago=40 * DAY)]
        self.settled()
        engine.run_once(self.c)
        self.assertAlmostEqual(ledger.realized(), -(5 + client.fee(10, 0.5)))

    def test_rolled_back_books_get_the_later_orders(self):
        # A copy from before: its last check was 3 days ago, and the bot traded since.
        db.put("orders_checked", time.time() - 3 * DAY)
        self.c.honor_min_ts = True
        self.c.order_list = [order(self.mine, ago=DAY)]
        self.settled()
        engine.run_once(self.c)
        self.assertEqual(len(db.query("SELECT id FROM orders")), 1)

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
        rows = db.query("SELECT status, filled FROM orders")
        self.assertEqual([(r["status"], r["filled"]) for r in rows], [("recovered", 10)])
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
