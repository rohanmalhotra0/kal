"""A half-built basket must stop the bot even if the process dies right at the stop. The
basket was marked 'stranded' first and the stop written after it. A process killed in between
(an update's restart, an out-of-memory kill) left it marked and never reported, and
stranded_baskets() never looks at a marked one again (cross-PR money-path audit). The stop is
now written first."""
import time
from unittest import mock

from kal import config, db, engine
from kal.client import new_order_id
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven
from tests.test_core import fake_with_arb
from tests.test_order_memory import order


class Killed(BaseException):
    """The process dying: not an Exception, so nothing in the bot catches it."""


def killed_at_the_stop():
    real = config.trip

    def trip(reason):
        if "half-built" in reason:
            raise Killed(reason)
        return real(reason)
    return mock.patch.object(config, "trip", trip)


class KilledAtTheStopTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})

    def test_a_short_second_leg(self):
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})
        orders_proven()
        c = fake_with_arb()
        calls = []

        def fill(ticker, side, count, price):
            calls.append(ticker)
            return count if len(calls) == 1 else 0   # leg 1 fills, leg 2 gets nothing
        c.fill = fill
        with killed_at_the_stop(), self.assertRaises(Killed):
            engine.run_once(c)
        self.assertEqual(config.load()["disabled_reason"], "")
        c.fill = lambda *a: 0   # the restart: nothing new to buy
        engine.run_once(c)
        self.assertIn("half-built", config.load()["disabled_reason"])   # was: never stopped
        config.clear_trip()
        config.update({"enabled": True})
        engine.run_once(c)
        self.assertEqual(config.load()["disabled_reason"], "")          # reported once, then marked

    def test_a_leg_found_filled_after_all(self):
        # recover() finds a leg the books called never placed filled on Kalshi: the basket is
        # checked again, and the stop comes from stranded_baskets().
        c = FakeKalshi(balance=1000)
        c.cutoffs = {"orders_updated_ts": time.time() - 15 * 86400, "trades_created_ts": time.time() - 60 * 86400}
        mine = new_order_id()
        legs = [{"ticker": f"B-{i}", "side": "no", "price": 0.5, "count": 10} for i in range(2)]
        sid = db.insert("signals", strategy="basket_arb", event="B", title="t", mode="live", guaranteed=1,
                        cost=10, payout_min=10, exp_profit=0.5, legs=legs, status="missed (no fill)")
        db.insert("orders", signal_id=sid, strategy="basket_arb", ticker="B-0", side="no", count=10, price=0.5,
                  filled=0, status="never_placed", client_order_id=mine, ts=time.time() - 1800)
        db.put("orders_checked", time.time() - 600)
        c.order_list = [order(mine, ticker="B-0", outcome="no", ago=1800)]
        c.position_list = [{"ticker": "B-0", "position": -10, "exposure": 5}]
        c.market_info["B-0"] = {"ticker": "B-0", "status": "active", "no_bid_dollars": "0.5000"}
        with killed_at_the_stop(), self.assertRaises(Killed):
            engine.run_once(c)
        self.assertEqual(config.load()["disabled_reason"], "")
        engine.run_once(c)
        self.assertIn("half-built", config.load()["disabled_reason"])   # was: never stopped
