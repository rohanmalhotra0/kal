import json
import os
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

    def test_first_live_run_sends_the_two_test_orders_then_trades(self):
        c = fake_with_probe_market()
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        # Both order shapes real trades use: a YES bid, and a NO buy (an ask on the YES book).
        self.assertEqual(self.probes(c), [(BUSY, "yes", 1, 0.01), (BUSY, "no", 1, 0.01)])
        self.assertEqual([o[0] for o in c.orders[:2]], [BUSY, BUSY])  # tests go before any real order
        self.assertEqual(s["live_trades"], 1)
        self.assertTrue(gate.passed(c, "prod"))
        engine.run_once(c)
        self.assertEqual(len(self.probes(c)), 2)  # proven once per key, not every run

    def test_what_the_owner_reads_matches_the_orders_sent(self):
        # The probe sends a YES and a NO order. The CLI, its help and the README said one.
        import argparse
        from kal import __main__ as cli
        c = fake_with_probe_market()
        self.assertTrue(gate.probe(c, "prod")[0])
        self.assertEqual([o[1] for o in self.probes(c)], ["yes", "no"])
        self.assertIn("1 YES and 1 NO", cli.PROBE_TEXT)
        sub = next(a for a in cli.parser()._actions if isinstance(a, argparse._SubParsersAction))
        self.assertIn("two $0.01 orders", " ".join(sub.choices["selftest"].format_help().split()))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "README.md")) as f:
            self.assertIn("two $0.01 orders (one YES, one NO)", f.read())

    def test_a_refused_no_test_order_keeps_money_on_paper(self):
        # Baskets buy NO. If Kalshi refuses that shape, real baskets would fail at their 2nd leg.
        c = fake_with_probe_market()
        real_buy = c.buy

        def buy(ticker, side, count, limit_price, client_order_id=None):
            if ticker == BUSY and side == "no":
                c.orders.append((ticker, side, count, limit_price))
                raise KalshiError("Order failed (400): bad side", 400)
            return real_buy(ticker, side, count, limit_price, client_order_id)
        c.buy = buy
        s = engine.run_once(c)
        self.assertEqual(s["live_trades"], 0)
        self.assertEqual([o for o in c.orders if o[0] != BUSY], [])
        self.assertFalse(gate.passed(c, "prod"))
        self.assertIn("NO test order", gate.status()["detail"])
        self.assertTrue(config.load()["enabled"])

    def test_a_no_test_order_that_fills_stops_the_bot(self):
        c = fake_with_probe_market()
        c.fill = lambda t, side, n, p: n if (t != BUSY or side == "no") else 0
        engine.run_once(c)
        self.assertIn("NO test order", config.load()["disabled_reason"])
        self.assertIn("FILLED", config.load()["disabled_reason"])
        self.assertEqual([o for o in c.orders if o[0] != BUSY], [])

    def test_the_no_test_needs_a_low_yes_bid(self):
        # Our NO buy is an ask at $0.99: where someone bids YES at $0.95 it could fill.
        c = fake_with_probe_market()
        c.books[BUSY] = {"yes": [(0.95, 10)], "no": [(0.03, 10)]}
        s = engine.run_once(c)
        self.assertEqual(self.probes(c), [])
        self.assertEqual(s["live_trades"], 0)
        self.assertFalse(gate.passed(c, "prod"))

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

    # ---------- the key ID stays out of the books (they are in every backup) ----------

    REAL_ID = "1234abcd-12ab-34cd-56ef-1234567890ab"

    def books_text(self):
        out = ""
        for suffix in ("", "-wal"):
            if os.path.exists(db.path() + suffix):
                with open(db.path() + suffix, "rb") as f:
                    out += f.read().decode("latin-1")
        return out

    def test_the_key_id_is_never_stored(self):
        c = fake_with_probe_market()
        c.key_id = self.REAL_ID
        engine.run_once(c)
        self.assertTrue(gate.passed(c, "prod"))
        rec = db.get(gate.KEY)
        self.assertEqual(rec["fingerprint"], gate.fingerprint(self.REAL_ID, "prod"))
        self.assertRegex(rec["fingerprint"], r"^prod:[0-9a-f]{16}$")
        self.assertNotIn(self.REAL_ID, json.dumps(rec))
        self.assertNotIn(self.REAL_ID, self.books_text())
        other = FakeKalshi()
        other.key_id = self.REAL_ID[:-1] + "c"
        self.assertFalse(gate.passed(other, "prod"))   # one character off is another key

    def test_an_old_pass_is_hashed_and_tested_again_for_no(self):
        """Books from before these changes say "prod:<key ID>", and proved only the YES
        shape. The plain ID is rewritten as the hash, and the next run sends both test
        orders (they can't fill) before any real money."""
        c = fake_with_probe_market()
        c.key_id = self.REAL_ID
        db.put(gate.KEY, {"ok": True, "fingerprint": f"prod:{self.REAL_ID}", "detail": "accepted, 0 filled",
                          "ts": time.time() - 30 * 86400})
        self.assertFalse(gate.passed(c, "prod"))
        self.assertNotIn(self.REAL_ID, json.dumps(db.get(gate.KEY)))
        s = engine.run_once(c)
        self.assertEqual(self.probes(c), [(BUSY, "yes", 1, 0.01), (BUSY, "no", 1, 0.01)])
        self.assertEqual(s["live_trades"], 1, s)       # then real money, in the same run
        self.assertNotIn(self.REAL_ID, json.dumps(db.get(gate.KEY)))
        self.assertTrue(gate.passed(c, "prod"))
        self.assertFalse(gate.passed(c, "demo"))
        db.put(gate.KEY, {"ok": False, "fingerprint": "demo:old-demo-key-99", "detail": "refused", "ts": 0})
        self.assertEqual(gate.status()["fingerprint"], gate.fingerprint("old-demo-key-99", "demo"))
        self.assertNotIn("old-demo-key-99", json.dumps(db.get(gate.KEY)))


if __name__ == "__main__":
    unittest.main()
