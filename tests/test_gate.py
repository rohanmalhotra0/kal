import time
import unittest
from unittest import mock

from kal import config, db, engine, gate
from kal.client import KalshiError
from tests.fakes import FakeKalshi
from tests.helpers import TempHome
from tests.test_core import fake_with_arb

BUSY = "BUSY-1"


def fake_with_probe_market(**kw):
    """An arb to trade, plus a busy market where the cheapest YES seller asks $0.45."""
    c = fake_with_arb(**kw)
    c.market_list = [{"ticker": "QUIET-1", "status": "active", "volume_24h": 3},
                     {"ticker": BUSY, "status": "active", "volume_24h": 5000}]
    c.books[BUSY] = {"yes": [(0.40, 10)], "no": [(0.55, 10)]}
    c.books["QUIET-1"] = {"yes": [], "no": []}
    c.fill = lambda t, side, n, p: 0 if t == BUSY else n  # a $0.01 bid under a $0.45 ask never fills
    return c


class GateTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})

    def probes(self, c):
        return [o for o in c.orders if o[0] == BUSY]

    def test_first_live_run_sends_one_test_order_then_trades(self):
        c = fake_with_probe_market()
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual(self.probes(c), [(BUSY, "yes", 1, 0.01)])
        self.assertEqual(c.orders[0][0], BUSY)  # the test order goes before any real one
        self.assertEqual(s["live_trades"], 1)
        self.assertTrue(gate.passed(c, "prod"))
        engine.run_once(c)
        self.assertEqual(len(self.probes(c)), 1)  # proven once per key, not every run

    def test_refused_test_order_keeps_money_on_paper_and_retries_hourly(self):
        c = fake_with_probe_market()
        real_buy = c.buy

        def buy(ticker, side, count, limit_price, client_order_id=None):
            if ticker == BUSY:
                c.orders.append((ticker, side, count, limit_price))
                raise KalshiError("Order failed (400): bad field", 400)
            return real_buy(ticker, side, count, limit_price, client_order_id)
        c.buy = buy
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual(s["live_trades"], 0)
        self.assertEqual([o for o in c.orders if o[0] != BUSY], [])  # no real orders at all
        self.assertTrue(any("test order" in n for n in s["notes"]), s["notes"])
        self.assertTrue(config.load()["enabled"])  # a refusal is not an emergency
        engine.run_once(c)
        self.assertEqual(len(self.probes(c)), 1)  # not every 5 minutes
        with mock.patch("kal.gate.time.time", return_value=time.time() + gate.RETRY_SECONDS + 1):
            engine.run_once(c)
        self.assertEqual(len(self.probes(c)), 2)

    def test_test_order_that_fills_stops_the_bot(self):
        c = fake_with_probe_market()
        c.fill = lambda t, side, n, p: n
        engine.run_once(c)
        self.assertFalse(config.load()["enabled"])
        self.assertIn("FILLED", config.load()["disabled_reason"])
        self.assertEqual([o for o in c.orders if o[0] != BUSY], [])
        self.assertFalse(gate.passed(c, "prod"))

    def test_resting_test_order_is_cancelled_and_stops_the_bot(self):
        c = fake_with_probe_market()
        c.buy = lambda ticker, side, count, limit_price, client_order_id=None: (
            c.orders.append((ticker, side, count, limit_price)) or
            {"order_id": "o9", "status": "resting", "fill_count_fp": "0.00"})
        engine.run_once(c)
        self.assertEqual(c.cancelled, ["o9"])
        self.assertIn("immediate-or-cancel", config.load()["disabled_reason"])
        self.assertEqual(len(c.orders), 1)

    def test_only_markets_with_a_high_ask_are_used(self):
        c = fake_with_probe_market()
        c.books[BUSY] = {"yes": [], "no": [(0.95, 10)]}  # YES ask $0.05: too close to $0.01
        s = engine.run_once(c)
        self.assertEqual(c.orders, [])
        self.assertEqual(s["live_trades"], 0)
        self.assertFalse(gate.passed(c, "prod"))

    def test_a_new_key_or_exchange_needs_a_new_test_order(self):
        c = FakeKalshi()
        gate.record(c, "prod", True, "ok")
        self.assertTrue(gate.passed(c, "prod"))
        self.assertFalse(gate.passed(c, "demo"))
        other = FakeKalshi()
        other.key_id = "a-new-key"
        self.assertFalse(gate.passed(other, "prod"))
        self.assertTrue(gate.due(other, "prod"))

    def test_any_refused_real_order_clears_the_pass(self):
        c = fake_with_arb()
        gate.record(c, "prod", True, "ok")

        def buy(ticker, side, count, limit_price, client_order_id=None):
            c.orders.append((ticker, side, count, limit_price))
            raise KalshiError("Order failed (400): price out of range", 400)
        c.buy = buy
        engine.run_once(c)
        self.assertFalse(gate.passed(c, "prod"))
        self.assertTrue(gate.due(c, "prod"))  # re-checked on the very next run

    def test_paper_only_or_no_budget_never_sends_a_test_order(self):
        c = fake_with_probe_market()
        config.update({"budget": 0})
        engine.run_once(c)
        config.update({"budget": 60, "strategies": {"basket_arb": {"mode": "paper"}}})
        engine.run_once(c)
        self.assertEqual(c.orders, [])
        self.assertEqual(db.get(gate.KEY), None)


if __name__ == "__main__":
    unittest.main()
