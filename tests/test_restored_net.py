"""On new books (a lost disk), recover() puts the bot's own orders back as 'restored': the
books can't see what came before the order mark, so their gains must never grow the budget.
That was done market by market: every winning leg booked $0 and every losing leg its full
cost, so each past basket (one leg loses, the others win) read as a loss of about one leg.
After a lost disk the bot stopped with "the $300.00 of capital is used up" while the account
was up (cross-PR money-path audit). Their gains now make up their losses, never more."""
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest import mock

from kal import client, config, db, engine, ledger, risk
from kal.client import fee, new_order_id
from tests.fakes import FakeKalshi
from tests.helpers import TempHome
from tests.test_order_memory import order


def final(ticker, result):
    return {"ticker": ticker, "status": "finalized", "result": result,
            "settlement_value_dollars": "1.0000" if result == "yes" else "0.0000"}


class RestoredNetTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        self.c = FakeKalshi(balance=1000)
        self.c.cutoffs = {"orders_updated_ts": time.time() - 15 * 86400,
                          "trades_created_ts": time.time() - 60 * 86400}
        self.truth = 0.0

    def bought(self, ticker, outcome, p, result, n=10, marked=True):
        """A past bot buy on a market now final, as Kalshi keeps it; adds to the true result."""
        coid = new_order_id() if marked else f"old-{ticker}"
        self.c.order_list.append(order(coid, ticker=ticker, outcome=outcome, n=n, p=p, ago=20 * 86400))
        self.c.market_info[ticker] = final(ticker, result)
        self.truth += n * (outcome == result) - n * p - fee(n, p)

    def basket(self, event, prices, winner):
        """NO on every market of a one-winner event: the winner's NO leg loses."""
        for i, p in enumerate(prices):
            self.bought(f"{event}-{i}", "no", p, "yes" if i == winner else "no")

    def lost_disk_then_settle(self):
        self.assertGreater(ledger.recover(self.c), 0)   # new books: no order check yet
        ledger.settle(self.c)

    def test_a_profitable_basket_books_nothing_not_a_loss(self):
        self.basket("EV", [0.60, 0.65, 0.66], winner=0)
        self.assertGreater(self.truth, 0)
        self.lost_disk_then_settle()
        self.assertAlmostEqual(ledger.realized(), 0)   # was -6.18: the losing leg in full

    def test_a_losing_history_books_its_net_loss(self):
        self.bought("A-1", "yes", 0.40, "no")    # lost 4.17
        self.bought("B-1", "yes", 0.80, "yes")   # won 1.89
        self.assertLess(self.truth, 0)
        self.lost_disk_then_settle()
        self.assertAlmostEqual(ledger.realized(), self.truth)   # was -4.17: the win never counted

    def test_a_winning_history_never_grows_the_budget(self):
        self.bought("A-1", "yes", 0.40, "yes")   # won 5.83
        self.bought("B-1", "yes", 0.20, "no")    # lost 2.12
        self.assertGreater(self.truth, 0)
        self.lost_disk_then_settle()
        self.assertAlmostEqual(ledger.realized(), 0)

    def test_catch_up_rows_only_ever_count_their_losses(self):
        # After `kal restore`, catch_up() can count the owner's own trades as the bot's: their
        # gains never make up anything.
        for ticker, p, result in (("A-1", 0.40, "no"), ("B-1", 0.80, "yes")):
            db.insert("orders", ts=time.time(), strategy="restored", ticker=ticker, side="yes", count=10,
                      price=p, filled=10, status="imported", order_id=f"k-{ticker}", client_order_id="")
            self.c.market_info[ticker] = final(ticker, result)
        ledger.settle(self.c)
        self.assertAlmostEqual(ledger.realized(), -(4 + fee(10, 0.40)))

    def test_the_books_reports_add_up_to_realized(self):
        self.basket("EV", [0.60, 0.65, 0.66], winner=0)
        self.bought("A-1", "yes", 0.40, "no")
        self.lost_disk_then_settle()
        self.assertAlmostEqual(sum(ledger.settled_order_pnl().values()), ledger.realized())
        from kal import report
        self.assertAlmostEqual(report._today(time.time())["live_settled_pnl"], round(ledger.realized(), 2))

    def test_a_lost_disk_after_many_baskets_keeps_trading(self):
        for k in range(60):
            self.basket(f"EV{k}", [0.60, 0.65, 0.66], winner=k % 3)
        s = engine.run_once(self.c)
        self.assertEqual(config.load()["disabled_reason"], "", s)   # was: "capital is used up"
        self.assertAlmostEqual(ledger.realized(), 0)

    def test_the_owners_gains_never_make_up_the_bots_losses(self):
        # A market settles at $0.70. The bot's put-back YES was all sold outside (written off),
        # so the owner's NO (a catch_up row) is all that's left: the whole payout is the
        # owner's, and none of it may make up the bot's other put-back loss (review: +$0.71).
        for ticker, side, p, coid in (("S", "yes", 0.40, new_order_id()), ("S", "no", 0.20, ""),
                                      ("C", "yes", 0.40, new_order_id())):
            db.insert("orders", ts=time.time() - 86400, strategy="restored", ticker=ticker, side=side, count=10,
                      price=p, filled=10, status="recovered", order_id=f"k-{ticker}-{side}", client_order_id=coid)
        ledger.write_off("S", "yes", 10, "sold outside")
        self.c.market_info["S"] = {"ticker": "S", "status": "finalized", "result": "",
                                   "settlement_value_dollars": "0.7000"}
        self.c.market_info["C"] = final("C", "no")
        ledger.settle(self.c)
        self.assertAlmostEqual(ledger.realized(), -2 * (4 + fee(10, 0.40)))   # was -7.63

    def test_a_stop_between_the_settlement_and_its_hold_back_books_neither(self):
        self.basket("EV", [0.60, 0.65, 0.66], winner=0)
        ledger.recover(self.c)
        real, died = db.connect, []

        @contextmanager
        def killed_at_the_hold_back():
            with real() as conn:
                class Conn:
                    def execute(self, sql, *args):
                        if "INTO held_gains" in sql and not died:
                            died.append(sql)
                            raise OSError("killed")
                        return conn.execute(sql, *args)
                yield Conn()
        with mock.patch.object(db, "connect", killed_at_the_hold_back), self.assertRaises(OSError):
            ledger.settle(self.c)
        ledger.settle(self.c)   # the next run
        self.assertAlmostEqual(ledger.realized(), 0)   # was -6.17: the basket's hold-back lost

    def day(self, when):
        return mock.patch.object(risk, "datetime", **{"now.return_value": when.replace(tzinfo=timezone.utc)})

    def test_open_put_back_baskets_never_trip_the_daily_stop(self):
        # Bids reach the result before the markets settle: the open baskets are worth what they
        # will settle for, not a loss of one leg each (review; main stopped here too).
        self.c.position_list = []
        for k in range(6):
            for i, p in enumerate([0.60, 0.65, 0.66]):
                t = f"EV{k}-{i}"
                self.c.order_list.append(order(new_order_id(), ticker=t, outcome="no", n=10, p=p, ago=86400))
                self.c.market_info[t] = {"ticker": t, "status": "active", "no_bid_dollars": f"{p:.4f}"}
                self.c.position_list.append({"ticker": t, "position": -10, "exposure": 10 * p})
        with self.day(datetime(2026, 10, 29, 10)):
            engine.run_once(self.c)
        for k in range(6):
            for i in range(3):
                self.c.market_info[f"EV{k}-{i}"]["no_bid_dollars"] = "0.0100" if i == 0 else "0.9900"
        with self.day(datetime(2026, 10, 29, 14)):
            engine.run_once(self.c)
        self.assertEqual(config.load()["disabled_reason"], "")   # was: "lost $33.48 today"

    def test_a_put_back_win_settling_later_never_hides_a_days_loss(self):
        # X (put back) lost on day 1. Y (put back) is decided on day 1 but settles on day 2,
        # the day a live bet Z loses $38. Y's gain making up X's loss must not land on day 2,
        # or it hides most of Z's loss from the daily stop (review; main stopped here).
        self.c.order_list = [order(new_order_id(), ticker=t, outcome="yes", n=30, p=0.40, ago=3 * 86400)
                             for t in ("X", "Y")]
        self.c.market_info["X"] = final("X", "no")
        self.c.market_info["Y"] = {"ticker": "Y", "status": "active", "yes_bid_dollars": "0.9900"}
        db.insert("orders", ts=0, strategy="favorites", ticker="Z", side="no", count=76, price=0.5, filled=76,
                  status="executed", order_id="kz", client_order_id=new_order_id())
        self.c.market_info["Z"] = {"ticker": "Z", "status": "active", "no_bid_dollars": "0.5000"}
        self.c.position_list = [{"ticker": "Y", "position": 30, "exposure": 12},
                                {"ticker": "Z", "position": -76, "exposure": 38}]
        for when in (datetime(2026, 10, 28, 10), datetime(2026, 10, 28, 23, 50), datetime(2026, 10, 29, 0, 30)):
            with self.day(when):
                engine.run_once(self.c)
        self.assertEqual(config.load()["disabled_reason"], "")
        self.c.market_info["Y"] = final("Y", "yes")
        self.c.market_info["Z"] = final("Z", "yes")
        self.c.position_list = []
        with self.day(datetime(2026, 10, 29, 12)):
            engine.run_once(self.c)
        self.assertIn("today", config.load()["disabled_reason"])   # was: no stop ($25.49 counted)
