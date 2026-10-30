"""After `kal restore`, catch_up() reads the fills since the backup, in time order, and skips a
buy that nets contracts the books hold (an outside sale). recover() puts back the bot's own
marked orders. It ran after catch_up(): when a sale came first in time order (fills without
a time, or two in one second), catch_up() booked the sale as the bot's buy of the other
side and skipped the bot's real buy as netted; recover() then put that buy back. The books
held both sides of the market, Kalshi none, and the payout was booked on contracts already
sold: +9.53 where the truth is -8.34 (cross-PR money-path audit). recover() now runs first."""
import time
from datetime import datetime, timezone

from kal import client, config, db, engine, ledger, maintenance
from kal.client import new_order_id
from tests.fakes import FakeKalshi
from tests.helpers import TempHome

DAY = 86400


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def fill(oid, ticker, book_side, count, yes_price, when):
    """A fill as Kalshi sends it once the deprecated side and action are gone: book_side bid
    is a YES buy, ask a YES sale (a NO buy). when=None: no created_time (the spec allows it)."""
    f = {"fill_id": f"f-{oid}", "trade_id": f"t-{oid}", "order_id": oid, "ticker": ticker,
         "market_ticker": ticker, "outcome_side": "yes" if book_side == "bid" else "no", "book_side": book_side,
         "count_fp": f"{count:.2f}", "yes_price_dollars": f"{yes_price:.4f}",
         "no_price_dollars": f"{1 - yes_price:.4f}", "is_taker": True, "fee_cost": "0.1300"}
    if when is not None:
        f["created_time"] = iso(when)
    return f


def order(oid, coid, ticker, yes_price, count, when):
    """A bot order Kalshi keeps: an immediate-or-cancel YES buy, filled in full."""
    return {"order_id": oid, "client_order_id": coid, "ticker": ticker, "outcome_side": "yes",
            "book_side": "bid", "status": "executed", "yes_price_dollars": f"{yes_price:.4f}",
            "no_price_dollars": f"{1 - yes_price:.4f}", "fill_count_fp": f"{count:.2f}",
            "initial_count_fp": f"{count:.2f}", "remaining_count_fp": "0.00", "created_time": iso(when)}


class RestoreThenRecoverTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        self.now = time.time()
        self.cost = 20 * 0.40 + client.fee(20, 0.40)   # 8.34: what the bot paid for contracts sold outside it

    def restore_then_run(self, buy_time=True, sale_time=True, same_second=False, books_checked=True):
        backup_at, buy_at, sale_at = self.now - 3 * DAY, self.now - 2 * DAY, self.now - DAY
        if same_second:
            buy_at = int(buy_at) + 0.1
            sale_at = buy_at + 0.4
        coid_o1, coid_b1 = new_order_id(), new_order_id()
        # The books before the backup: 20 YES on T-1, and the last order check.
        db.insert("orders", ts=backup_at - DAY, strategy="favorites", ticker="T-1", side="yes", count=20,
                  price=0.40, filled=20, status="executed", order_id="o1", client_order_id=coid_o1)
        if books_checked:
            db.put("orders_checked", backup_at - 60)
        day = maintenance.backup(now=backup_at)["day"]
        # After the backup the bot buys 20 YES on T-2 (marked b1). The owner then sells them
        # at 90c outside the bot, restores the backup, presses Reset and ON. T-2 settles YES.
        db.insert("orders", ts=buy_at, strategy="favorites", ticker="T-2", side="yes", count=20, price=0.40,
                  filled=20, status="executed", order_id="b1", client_order_id=coid_b1)
        maintenance.restore(day)
        config.clear_trip()
        config.update({"enabled": True})
        c = FakeKalshi(balance=1000)
        c.honor_min_ts = True
        c.fill_list = [fill("own-1", "T-2", "ask", 20, 0.90, sale_at if sale_time else None),   # newest first
                       fill("b1", "T-2", "bid", 20, 0.40, buy_at if buy_time else None),
                       fill("o1", "T-1", "bid", 20, 0.40, backup_at - DAY)]
        c.order_list = [order("b1", coid_b1, "T-2", 0.40, 20, buy_at),
                        order("o1", coid_o1, "T-1", 0.40, 20, backup_at - DAY)]
        c.market_info["T-1"] = {"ticker": "T-1", "status": "active", "yes_bid_dollars": "0.4000",
                                "open_time": iso(self.now - 10 * DAY)}
        c.market_info["T-2"] = {"ticker": "T-2", "status": "finalized", "result": "yes",
                                "settlement_value_dollars": "1.0000", "open_time": iso(self.now - 10 * DAY)}
        c.position_list = [{"ticker": "T-1", "position": 20, "exposure": 8}]
        engine.run_once(c)

    def check(self):
        sides = {r["side"] for r in db.query("SELECT side FROM orders WHERE ticker = 'T-2' AND filled > 0")}
        self.assertEqual(sides, {"yes"})                               # never both sides of one market
        self.assertAlmostEqual(ledger.realized(), -self.cost)          # was +9.53: the payout booked

    def test_times_kalshi_normally_sends(self):
        self.restore_then_run()
        self.check()

    def test_fills_without_a_time(self):
        self.restore_then_run(buy_time=False, sale_time=False)
        self.check()

    def test_the_buy_without_a_time(self):
        self.restore_then_run(buy_time=False)
        self.check()

    def test_buy_and_sale_inside_one_second(self):
        self.restore_then_run(same_second=True)
        self.check()

    def test_books_with_no_order_check_yet(self):
        # A backup from before the books' first order check: recover() counts losses only,
        # and still never lets the sale in as the bot's buy.
        self.restore_then_run(buy_time=False, sale_time=False, books_checked=False)
        self.check()
