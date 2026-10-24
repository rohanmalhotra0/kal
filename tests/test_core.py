import base64
import unittest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kal import client, config, db, engine, paper, risk, sizing
from kal.strategies import basket_arb
from kal.strategies.base import Leg, Signal
from tests.fakes import FakeKalshi
from tests.helpers import TempHome


def arb_event(ticker="EV1", bids=(0.55, 0.35, 0.15, 0.07), size=40, exclusive=True):
    markets = [{"ticker": f"{ticker}-{i}", "yes_bid_dollars": f"{b:.4f}", "status": "active"}
               for i, b in enumerate(bids)]
    return {"event_ticker": ticker, "title": "Test event", "mutually_exclusive": exclusive,
            "markets": markets}


def fake_with_arb(**kw):
    c = FakeKalshi(**kw)
    ev = arb_event()
    c.event_list = [ev, arb_event("EV2", exclusive=False)]
    for m in ev["markets"]:
        c.books[m["ticker"]] = {"yes": [(float(m["yes_bid_dollars"]), 40)], "no": []}
    return c


class ClientTests(unittest.TestCase):
    def test_signature_verifies(self):
        k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        sig = client.sign(k, "1700000000000", "GET", "/trade-api/v2/portfolio/balance")
        k.public_key().verify(base64.b64decode(sig), b"1700000000000GET/trade-api/v2/portfolio/balance",
                              padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                              hashes.SHA256())

    def test_fees_and_fields(self):
        self.assertEqual(client.fee(1, 0.95), 0.01)
        self.assertEqual(client.fee(10, 0.95), 0.04)
        self.assertEqual(client.price({"yes_ask_dollars": "0.9300"}, "yes_ask"), 0.93)
        self.assertEqual(client.price({"yes_ask": 93}, "yes_ask"), 0.93)
        self.assertEqual(client.filled({"fill_count_fp": "3.00"}, 5), 3)
        self.assertEqual(client.filled({"status": "executed"}, 5), 5)

    def test_orderbook_both_formats(self):
        k = client.Kalshi()
        k.get = lambda path, params=None: {"orderbook_fp": {"yes_dollars": [["0.4100", "5.00"], ["0.4200", "9.00"]]}}
        self.assertEqual(k.best_bid("T"), (0.42, 9.0))
        k.get = lambda path, params=None: {"orderbook": {"yes": [[40, 3], [43, 7]], "no": [[55, 2]]}}
        self.assertEqual(k.best_bid("T", "no"), (0.55, 2.0))


class SizingTests(unittest.TestCase):
    def test_kelly(self):
        self.assertEqual(sizing.kelly(0.5, 0.6), 0)
        self.assertAlmostEqual(sizing.kelly(0.9, 0.8), 0.5)
        self.assertEqual(sizing.contracts(0.99, 0.95, 100, 0.25, max_loss=5), 5)


class ConfigTests(TempHome):
    def test_trip_beats_env(self):
        import os
        config.update({"enabled": True, "budget": 50})
        os.environ["KAL_ENABLED"] = "true"
        config.trip("test")
        self.assertFalse(config.load()["enabled"])
        config.clear_trip()
        self.assertTrue(config.load()["enabled"])

    def test_validation(self):
        with self.assertRaises(ValueError):
            config.update({"budget": -1})
        with self.assertRaises(ValueError):
            config.update({"strategies": {"basket_arb": {"mode": "yolo"}}})


class BasketArbTests(TempHome):
    def test_profit_locked_for_every_outcome(self):
        b = basket_arb.no_basket([("A", 0.55, 40), ("B", 0.35, 25), ("C", 0.15, 60), ("D", 0.07, 10)], 30)
        sig = basket_arb.to_signal("E", "t", b)
        for winner in [l.ticker for l in sig.legs] + [None]:
            payout = sum(l.count for l in sig.legs if l.ticker != winner)
            self.assertGreater(payout - sig.cost, 0)
        self.assertTrue(sig.guaranteed)

    def test_no_arb_when_bids_under_a_dollar(self):
        self.assertIsNone(basket_arb.no_basket([("A", 0.60, 50), ("B", 0.38, 50)], 30))


class EngineTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})

    def test_off_does_nothing(self):
        config.update({"enabled": False})
        c = fake_with_arb()
        self.assertIn("skipped", engine.run_once(c))
        self.assertEqual(c.orders, [])

    def test_live_arb_buys_every_leg_once(self):
        c = fake_with_arb()
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual(s["live_trades"], 1)
        self.assertEqual({o[0] for o in c.orders}, {f"EV1-{i}" for i in range(4)})
        self.assertTrue(all(o[1] == "no" for o in c.orders))
        cost = sum(r["count"] * r["price"] for r in db.query("SELECT * FROM orders"))
        self.assertLessEqual(cost, 30)

    def test_partial_basket_stops_and_counts_error(self):
        c = fake_with_arb()
        c.fill = lambda t, side, n, p: 0 if t == "EV1-1" else n
        s = engine.run_once(c)
        self.assertFalse(s["ok"])
        self.assertEqual(len(c.orders), 2)  # stopped after the leg that didn't fill
        self.assertEqual(db.get("consecutive_errors"), 1)

    def test_three_failures_trip_the_bot(self):
        c = fake_with_arb()
        c.fill = lambda t, side, n, p: 0 if t == "EV1-1" else n
        for _ in range(3):
            engine.run_once(c)
        cfg = config.load()
        self.assertFalse(cfg["enabled"])
        self.assertIn("failed runs", cfg["disabled_reason"])

    def test_first_leg_miss_is_harmless(self):
        c = fake_with_arb()
        c.fill = lambda t, side, n, p: 0
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual(len(c.orders), 1)

    def test_budget_respected(self):
        config.update({"budget": 5})
        c = fake_with_arb()
        engine.run_once(c)
        spent = sum(r["filled"] * r["price"] for r in db.query("SELECT * FROM orders"))
        self.assertTrue(0 < spent <= 5)
        config.update({"budget": 1})
        c = fake_with_arb()
        c.position_list = []
        engine.run_once(c)
        self.assertEqual(c.orders, [])

    def test_no_key_means_paper(self):
        c = fake_with_arb(can_trade=False)
        s = engine.run_once(c)
        self.assertEqual(c.orders, [])
        self.assertEqual(s["paper_trades"], 1)
        self.assertEqual(len(paper.open_rows()), 4)

    def test_paper_settles(self):
        c = fake_with_arb(can_trade=False)
        engine.run_once(c)
        for i in range(4):
            c.market_info[f"EV1-{i}"] = {"status": "settled", "result": "yes" if i == 0 else "no"}
        engine.run_once(c)
        self.assertGreater(paper.realized(), 0)

    def test_daily_loss_limit_trips(self):
        c = fake_with_arb()
        engine.run_once(c)
        c._balance = 50  # equity fell $50 on a $60 budget
        c.event_list = []
        engine.run_once(c)
        self.assertIn("daily limit", config.load()["disabled_reason"])

    def test_run_lock(self):
        with risk.run_lock():
            with self.assertRaises(risk.Locked):
                with risk.run_lock():
                    pass


class LimitsTests(unittest.TestCase):
    def test_directional_caps(self):
        cfg = dict(config.DEFAULTS, max_per_trade=10, max_per_event=15)
        lim = risk.Limits(cfg, 100)
        sig = Signal("x", "E", "t", [Leg("T", "yes", 0.9, 12)])
        self.assertIn("max_per_trade", lim.refuse(sig))
        sig = Signal("x", "E", "t", [Leg("T", "yes", 0.9, 10)])
        self.assertIsNone(lim.refuse(sig))
        lim.spend(sig)
        self.assertIn("max_per_event", lim.refuse(Signal("x", "E", "t", [Leg("U", "yes", 0.9, 10)])))
        self.assertIn("bad leg", lim.refuse(Signal("x", "F", "t", [Leg("U", "maybe", 0.9, 1)])))


if __name__ == "__main__":
    unittest.main()
