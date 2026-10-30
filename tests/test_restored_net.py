"""On new books (a lost disk), recover() puts the bot's own orders back as 'restored': the
books can't see what came before the order mark, so their gains must never grow the budget.
That was done market by market: every winning leg booked $0 and every losing leg its full
cost, so each past basket (one leg loses, the others win) read as a loss of about one leg.
After a lost disk the bot stopped with "the $300.00 of capital is used up" while the account
was up (cross-PR money-path audit). Their gains now make up their losses, never more."""
import time

from kal import client, config, db, engine, ledger
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
