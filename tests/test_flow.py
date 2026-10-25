import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from kal import config, db, engine, flow
from kal.client import KalshiError
from kal.strategies.base import Leg, Signal, Strategy
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven
from tests.test_core import fake_with_arb

NOW = 1_800_000_000


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def trade(took, count, ago=60, price=0.5):
    """One public trade in the real API's shape."""
    return {"taker_side": took, "count_fp": f"{count:.2f}", "created_time": iso(NOW - ago),
            "yes_price_dollars": f"{price:.4f}", "no_price_dollars": f"{1 - price:.4f}"}


class AgainstTests(unittest.TestCase):
    def test_share_of_volume_that_took_the_other_side(self):
        tape = [trade("yes", 10)] * 4 + [trade("no", 10)] * 2
        share, sweep, n = flow.against(tape, "no", NOW)
        self.assertAlmostEqual(share, 40 / 60)
        self.assertEqual((sweep, n), (0.0, 6))
        self.assertAlmostEqual(flow.against(tape, "yes", NOW)[0], 20 / 60)

    def test_old_trades_and_thin_tape_say_nothing(self):
        old = [trade("yes", 10, ago=2 * 3600)] * 10
        self.assertEqual(flow.against(old, "no", NOW), (0.0, 0.0, 0))
        self.assertEqual(flow.against([trade("yes", 50)] * 4, "no", NOW), (0.0, 0.0, 4))
        self.assertEqual(flow.against([{"junk": 1}, {"created_time": "soon"}], "no", NOW), (0.0, 0.0, 0))


class VetoTests(unittest.TestCase):
    def sig(self, side="no"):
        return Signal("t", "EV", "t", [Leg("M-1", side, 0.95, 5)])

    def client(self, tape):
        c = FakeKalshi()
        c.trade_list = {"M-1": tape}
        return c

    def test_one_sided_flow_against_us_vetoes(self):
        c = self.client([trade("yes", 20)] * 8 + [trade("no", 5)] * 2)
        why = flow.veto(c, self.sig("no"), now=NOW)
        self.assertIn("bet against NO", why)
        self.assertIsNone(flow.veto(c, self.sig("yes"), now=NOW))  # the same flow is on our side

    def test_one_big_trade_against_us_vetoes(self):
        tape = [trade("no", 50)] * 10 + [trade("yes", 300, price=0.40)]   # $120 YES sweep
        why = flow.veto(self.client(tape), self.sig("no"), now=NOW)
        self.assertIn("$120 trade", why)
        small = [trade("no", 50)] * 10 + [trade("yes", 100, price=0.40)]  # under 5x the median
        self.assertIsNone(flow.veto(self.client(small), self.sig("no"), now=NOW))

    def test_unreadable_trades_skip_the_bet(self):
        c = FakeKalshi()
        c.trades = mock.Mock(side_effect=KalshiError("503", 503))
        self.assertIn("couldn't read recent trades", flow.veto(c, self.sig(), now=NOW))


class OneBet(Strategy):
    """A directional bet on B-1, whatever the market does."""
    name, title, kind = "onebet", "One bet", "statistical"

    def proven(self, ctx):
        return True, "test"

    def scan(self, ctx):
        sig = Signal("onebet", "EVB", "Will B?", [Leg("B-1", "yes", 0.60, 5)])
        sig.exp_profit = 0.5
        return [sig]


class EngineFlowTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"basket_arb": {"mode": "live"}, "onebet": {"mode": "live"}}})
        orders_proven()
        from kal.strategies import basket_arb
        patcher = mock.patch.object(engine, "load_all", lambda: {"basket_arb": basket_arb.STRATEGY,
                                                                 "onebet": OneBet()})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_with(self, tape):
        c = fake_with_arb()
        c.books["B-1"] = {"yes": [(0.58, 50)], "no": [(0.40, 50)]}
        now = time.time()
        c.trade_list = {"B-1": [dict(t, created_time=iso(now - 60)) for t in tape]}
        return c, engine.run_once(c)

    def test_flow_against_a_bet_skips_it_but_not_a_locked_in_trade(self):
        c, s = self.run_with([trade("no", 30)] * 9 + [trade("yes", 5)])
        self.assertTrue(s["ok"], s)
        self.assertEqual({o[0] for o in c.orders}, {f"EV1-{i}" for i in range(4)})  # the arb still traded
        status = db.query("SELECT status FROM signals WHERE strategy = 'onebet'")[0]["status"]
        self.assertIn("skipped:", status)
        self.assertIn("bet against YES", status)

    def test_friendly_flow_lets_the_bet_through(self):
        c, s = self.run_with([trade("yes", 30)] * 9 + [trade("no", 5)])
        self.assertIn(("B-1", "yes", 5, 0.60), c.orders)

    def test_a_quiet_market_lets_the_bet_through(self):
        c, s = self.run_with([])
        self.assertIn(("B-1", "yes", 5, 0.60), c.orders)


if __name__ == "__main__":
    unittest.main()
