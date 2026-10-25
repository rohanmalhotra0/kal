import base64
import unittest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kal import client, config, db, engine, paper, risk, sizing
from kal.strategies import basket_arb
from kal.strategies.base import Context, Leg, Signal
from tests.fakes import FakeKalshi
from tests.helpers import TempHome


SOON = __import__("datetime").datetime.fromtimestamp(
    __import__("time").time() + 2 * 86400, __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def arb_event(ticker="EV1", bids=(0.55, 0.35, 0.15, 0.07), size=40, exclusive=True, close=None):
    markets = [{"ticker": f"{ticker}-{i}", "yes_bid_dollars": f"{b:.4f}", "status": "active",
                "close_time": close or SOON} for i, b in enumerate(bids)]
    return {"event_ticker": ticker, "title": "Test event", "mutually_exclusive": exclusive,
            "markets": markets}


def fake_with_arb(**kw):
    c = FakeKalshi(**kw)
    ev = arb_event()
    c.event_list = [ev, arb_event("EV2", exclusive=False)]
    for m in ev["markets"]:
        b = float(m["yes_bid_dollars"])
        c.books[m["ticker"]] = {"yes": [(b, 40)], "no": []}
        c.market_info[m["ticker"]] = {"ticker": m["ticker"], "status": "active",
                                      "yes_bid_dollars": f"{b:.4f}", "no_bid_dollars": f"{1 - b - 0.01:.4f}"}
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

    def test_price_shapes_seen_on_the_real_api(self):
        self.assertEqual(client.price({"yes_bid_dollars": "0.0500"}, "yes_bid"), 0.05)
        self.assertEqual(client.price({"close": "0.8000"}, "close"), 0.8)   # historical candles
        self.assertEqual(client.price({"close": 80}, "close"), 0.8)         # legacy cents
        self.assertEqual(client.price({"close": None}, "close"), 0.0)
        self.assertEqual(client.price({"close": "junk"}, "close"), 0.0)
        self.assertEqual(client.price({}, "close"), 0.0)
        self.assertEqual(client.candle_close({"yes_ask": {"close": "0.8000", "high": "1.0000"}}, "yes_ask"), 0.8)

    def test_markets_by_ticker_falls_back_to_one_by_one(self):
        k = client.Kalshi()
        k.markets = lambda limit=None, **p: []  # the batch filter came back empty on the real API
        k.market = lambda t: {"ticker": t, "status": "finalized", "result": "yes"}
        self.assertEqual(set(k.markets_by_ticker(["A", "B", "A"])), {"A", "B"})

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
    def test_trip_latch_beats_enabled(self):
        config.update({"enabled": True, "budget": 50})
        config.trip("test")
        self.assertFalse(config.load()["enabled"])
        config.update({"enabled": True})
        self.assertFalse(config.load()["enabled"])  # still latched
        config.clear_trip()
        self.assertFalse(config.load()["enabled"])  # Reset doesn't turn it back on by itself
        config.update({"enabled": True})
        self.assertTrue(config.load()["enabled"])

    def test_trip_works_with_invalid_file(self):
        import json, os
        config.update({"enabled": True, "budget": 50})
        path = os.path.join(config.home(), "config.json")
        data = json.load(open(path))
        data["strategies"]["basket_arb"]["mode"] = "Live"
        json.dump(data, open(path, "w"))
        config.trip("emergency")
        self.assertFalse(config.load()["enabled"])
        self.assertEqual(config.load()["disabled_reason"], "emergency")

    def test_validation(self):
        with self.assertRaises(ValueError):
            config.update({"budget": -1})
        with self.assertRaises(ValueError):
            config.update({"strategies": {"basket_arb": {"mode": "yolo"}}})


class BasketArbTests(TempHome):
    def test_profit_locked_for_every_outcome(self):
        b = basket_arb.no_basket([("A", 0.55, 40), ("B", 0.35, 25), ("C", 0.15, 60), ("D", 0.07, 10)], 30)
        sig = basket_arb.to_signal("E", "t", b)
        sig.closes_at = __import__("time").time() + 86400
        for winner in [l.ticker for l in sig.legs] + [None]:
            payout = sum(l.count for l in sig.legs if l.ticker != winner)
            self.assertGreater(payout - sig.cost, 0)
        self.assertTrue(sig.guaranteed)

    def test_non_binary_event_is_skipped(self):
        c = fake_with_arb()
        c.event_list[0]["markets"][0]["market_type"] = "scalar"
        ctx = Context(c, dict(config.DEFAULTS), "paper", 100, 100, set(), {}, {})
        self.assertEqual(basket_arb.STRATEGY.scan(ctx), [])

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

    def test_half_built_basket_stops_the_bot(self):
        c = fake_with_arb()
        c.fill = lambda t, side, n, p: 0 if t == "EV1-1" else n
        s = engine.run_once(c)
        self.assertFalse(s["ok"])
        self.assertEqual(len(c.orders), 2)  # stopped after the leg that didn't fill
        self.assertIn("half-built", config.load()["disabled_reason"])

    def test_maintenance_skips_quietly(self):
        c = fake_with_arb()
        c.trading_active = False
        for _ in range(5):
            s = engine.run_once(c)
        self.assertIn("closed", s["skipped"])
        self.assertEqual(c.orders, [])
        self.assertTrue(config.load()["enabled"])

    def test_kalshi_outage_never_trips(self):
        from kal.client import KalshiError
        c = fake_with_arb()
        c.balance = lambda: (_ for _ in ()).throw(KalshiError("503 Service Unavailable", 503))
        for _ in range(10):
            s = engine.run_once(c)
        self.assertTrue(s.get("transient"))
        self.assertTrue(config.load()["enabled"])
        self.assertEqual(db.get("consecutive_errors", 0), 0)

    def test_long_outage_sends_one_alert(self):
        import os, time as _t
        from unittest import mock
        from kal import notify
        from kal.client import KalshiError
        os.environ["KAL_NTFY_TOPIC"] = "t"
        self.addCleanup(os.environ.pop, "KAL_NTFY_TOPIC", None)
        db.put("last_ok_run", _t.time() - 7 * 3600)
        c = fake_with_arb()
        c.balance = lambda: (_ for _ in ()).throw(KalshiError("502", 502))
        with mock.patch.object(notify.requests, "post") as post:
            engine.run_once(c)
            engine.run_once(c)
        self.assertEqual(post.call_count, 1)
        self.assertIn("can't reach Kalshi", post.call_args.kwargs["headers"]["Title"])

    def test_network_errors_never_trip(self):
        import requests
        c = fake_with_arb()
        c.balance = lambda: (_ for _ in ()).throw(requests.ConnectionError("no route"))
        for _ in range(10):
            engine.run_once(c)
        self.assertTrue(config.load()["enabled"])

    def test_three_failures_trip_the_bot(self):
        c = fake_with_arb()
        c.events = lambda **p: (_ for _ in ()).throw(RuntimeError("boom"))
        c.balance = lambda: (_ for _ in ()).throw(RuntimeError("api down"))
        for _ in range(3):
            engine.run_once(c)
        cfg = config.load()
        self.assertFalse(cfg["enabled"])
        self.assertIn("failed runs", cfg["disabled_reason"])

    def test_first_leg_partial_shrinks_the_rest(self):
        c = fake_with_arb()
        c.fill = lambda t, side, n, p: 3 if t == "EV1-0" else n
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual([o[2] for o in c.orders][1:], [3, 3, 3])

    def test_lost_reply_is_reconciled_next_run(self):
        from kal.client import KalshiError
        c = fake_with_arb()
        real_buy = c.buy

        def flaky(ticker, side, count, price, client_order_id=None):
            real_buy(ticker, side, count, price, client_order_id)
            c.sent = {"client_order_id": client_order_id, "order_id": "o1", "status": "executed",
                      "fill_count_fp": f"{count:.2f}", "ticker": ticker}
            raise KalshiError("timeout", None)
        c.buy = flaky
        s = engine.run_once(c)
        self.assertFalse(s["ok"])
        self.assertEqual(db.query("SELECT status FROM orders")[0]["status"], "unknown")
        c.order_list = [c.sent]
        c.buy = real_buy
        c.event_list = []
        s = engine.run_once(c)
        self.assertEqual(s.get("reconciled"), 1, s)
        row = db.query("SELECT * FROM orders")[0]
        self.assertGreater(row["filled"], 0)
        from kal import ledger
        self.assertGreater(ledger.open_cost(), 0)  # the lost fill now counts against capital

    def test_resting_order_is_cancelled_and_trips(self):
        c = fake_with_arb()
        c.buy = lambda t, side, n, p, client_order_id=None: {"order_id": "r1", "status": "resting",
                                                            "fill_count_fp": "0.00"}
        engine.run_once(c)
        self.assertEqual(c.cancelled, ["r1"])
        self.assertIn("stayed on the book", config.load()["disabled_reason"])

    def test_bad_mode_never_trades(self):
        import json, os
        path = os.path.join(config.home(), "config.json")
        data = json.load(open(path))
        data["strategies"]["basket_arb"]["mode"] = "Live"
        json.dump(data, open(path, "w"))
        c = fake_with_arb()
        s = engine.run_once(c)
        self.assertEqual(c.orders, [])
        self.assertFalse(s["ok"])

    def test_zero_budget_means_no_real_trades(self):
        config.update({"budget": 0})
        c = fake_with_arb()
        engine.run_once(c)
        self.assertEqual(c.orders, [])

    def test_off_mid_run_stops_trading(self):
        c = fake_with_arb()
        c.event_list = [arb_event("EV1"), arb_event("EV3")]
        for ev in c.event_list:
            for m in ev["markets"]:
                b = float(m["yes_bid_dollars"])
                c.books[m["ticker"]] = {"yes": [(b, 40)], "no": []}
                c.market_info[m["ticker"]] = {"ticker": m["ticker"], "status": "active",
                                              "yes_bid_dollars": f"{b:.4f}", "no_bid_dollars": f"{1 - b - .01:.4f}"}
        real_buy = c.buy

        def buy_then_off(*a, **k):
            r = real_buy(*a, **k)
            config.turn_off()
            return r
        c.buy = buy_then_off
        engine.run_once(c)
        self.assertEqual(len({o[0].split("-")[0] for o in c.orders}), 1)  # only the first event

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
        for m in c.market_info.values():
            m["no_bid_dollars"] = "0.0100"  # the basket's NO legs crashed to a cent
        c.event_list = []
        engine.run_once(c)
        self.assertIn("daily limit", config.load()["disabled_reason"])

    def test_run_lock(self):
        with risk.run_lock():
            with self.assertRaises(risk.Locked):
                with risk.run_lock():
                    pass


class NotifyTests(TempHome):
    def test_trip_sends_an_urgent_alert_and_survives_failure(self):
        import os
        from unittest import mock
        from kal import notify
        os.environ["KAL_NTFY_TOPIC"] = "test-topic"
        self.addCleanup(os.environ.pop, "KAL_NTFY_TOPIC", None)
        with mock.patch.object(notify.requests, "post") as post:
            config.trip("lost too much")
        self.assertIn("lost too much", post.call_args.kwargs["data"].decode())
        self.assertEqual(post.call_args.kwargs["headers"]["Priority"], "urgent")
        with mock.patch.object(notify.requests, "post", side_effect=OSError("no network")):
            config.trip("again")  # must not raise
        self.assertEqual(config.load()["disabled_reason"], "again")

    def test_no_topic_no_alert(self):
        from unittest import mock
        from kal import notify
        with mock.patch.object(notify.requests, "post") as post:
            self.assertFalse(notify.send("t", "m"))
        post.assert_not_called()

    def test_live_trades_alert(self):
        import os
        from unittest import mock
        from kal import notify
        os.environ["KAL_NTFY_TOPIC"] = "test-topic"
        self.addCleanup(os.environ.pop, "KAL_NTFY_TOPIC", None)
        config.update({"enabled": True, "budget": 60,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})
        with mock.patch.object(notify.requests, "post") as post:
            engine.run_once(fake_with_arb())
        self.assertIn("real trade", post.call_args.kwargs["headers"]["Title"])


class RunCacheTests(TempHome):
    def test_strategies_share_one_events_download(self):
        config.update({"enabled": True, "budget": 60,
                       "strategies": {"basket_arb": {"mode": "live"}, "ladder_arb": {"mode": "live"},
                                      "bracket_arb": {"mode": "paper"}}})
        c = fake_with_arb()
        calls = []
        real = c.events
        c.events = lambda **p: calls.append(p) or real(**p)
        engine.run_once(c)
        self.assertEqual(len(calls), 1)

    def test_order_books_are_never_cached(self):
        c = FakeKalshi()
        rc = engine.RunCache(c)
        c.books["T"] = {"yes": [(0.5, 1)], "no": []}
        self.assertEqual(rc.best_bid("T"), (0.5, 1))
        c.books["T"] = {"yes": [(0.6, 1)], "no": []}
        self.assertEqual(rc.best_bid("T"), (0.6, 1))


class LedgerTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})

    def _fill(self, ticker, side, n, p):
        db.insert("orders", ticker=ticker, side=side, count=n, price=p, filled=n, status="executed")

    def test_losses_shrink_capital_and_profits_compound(self):
        from kal import ledger
        c = FakeKalshi()
        self._fill("A", "yes", 100, 0.90)
        self.assertAlmostEqual(ledger.available(config.load()), 300 - 90 - client.fee(100, 0.9))
        c.market_info["A"] = {"status": "settled", "result": "no"}
        ledger.settle(c)
        cfg = config.load()
        self.assertAlmostEqual(ledger.capital(cfg), 300 - 90 - client.fee(100, 0.9))
        self._fill("B", "no", 10, 0.50)
        c.market_info["B"] = {"status": "settled", "result": "no"}
        ledger.settle(c)
        self.assertGreater(ledger.capital(cfg), 300 - 90 - client.fee(100, 0.9))
        config.update({"reinvest_profits": False})
        self.assertLess(ledger.capital(config.load()), 300)  # net loss still counts

    def test_no_bid_is_valued_at_cost_not_zero(self):
        from kal import ledger
        c = FakeKalshi()
        self._fill("A", "no", 10, 0.90)
        c.market_info["A"] = {"ticker": "A", "status": "active", "no_bid_dollars": "0.0000"}
        cfg = config.load()
        self.assertAlmostEqual(ledger.equity(c, cfg), ledger.capital(cfg))
        c.market_info["A"]["no_bid_dollars"] = "0.5000"  # a real drop still shows
        self.assertLess(ledger.equity(c, cfg), ledger.capital(cfg) - 3)

    def test_settles_once(self):
        from kal import ledger
        c = FakeKalshi()
        self._fill("A", "yes", 10, 0.5)
        c.market_info["A"] = {"status": "settled", "result": "yes"}
        self.assertEqual(ledger.settle(c), 1)
        self.assertEqual(ledger.settle(c), 0)
        self.assertAlmostEqual(ledger.realized(), 10 - 5 - client.fee(10, 0.5))

    def test_capital_used_up_trips(self):
        c = FakeKalshi()
        self._fill("A", "yes", 303, 0.99)
        c.market_info["A"] = {"status": "settled", "result": "no"}
        engine.run_once(c)
        self.assertIn("used up", config.load()["disabled_reason"])

    def test_bot_never_spends_past_capital_even_with_a_big_account(self):
        config.update({"budget": 10, "strategies": {"basket_arb": {"mode": "live"}}})
        c = fake_with_arb(balance=10_000)
        engine.run_once(c)
        spent = sum(r["filled"] * r["price"] for r in db.query("SELECT * FROM orders"))
        self.assertLessEqual(spent, 10)


class SettleDateTests(unittest.TestCase):
    def _arb(self, days, cost_per=0.9, n=10):
        import time as _t
        return Signal("x", "E", "t", [Leg("A", "no", cost_per / 2, n), Leg("B", "no", cost_per / 2, n)],
                      payout_min=n * 1.0, closes_at=_t.time() + days * 86400 if days is not None else None)

    def test_locked_in_trade_must_settle_soon_and_pay_enough_per_year(self):
        cfg = dict(config.DEFAULTS, max_per_trade=30)
        lim = risk.Limits(cfg, 1000)
        self.assertIsNone(lim.refuse(self._arb(2)))
        self.assertIn("locks money up", lim.refuse(self._arb(4000)))      # a 2040 career ladder
        self.assertIn("no known settle date", lim.refuse(self._arb(None)))
        import time as _t
        tiny = Signal("x", "E", "t", [Leg("A", "no", 0.98, 10), Leg("B", "no", 0.005, 10)],
                      payout_min=10.0, closes_at=_t.time() + 29 * 86400)  # ~1% over a month
        self.assertTrue(tiny.guaranteed)
        self.assertIn("a year", lim.refuse(tiny))


class FinalityTests(unittest.TestCase):
    def test_determined_is_not_final(self):
        self.assertIsNone(client.settled_result({"status": "determined", "result": "yes"}))
        self.assertEqual(client.settled_result({"status": "finalized", "result": "yes"}), "yes")
        self.assertEqual(client.settled_result({"status": "settled", "result": "no"}), "no")


class PaperDuplicateTests(TempHome):
    def test_same_market_is_paper_bought_once(self):
        config.update({"enabled": True, "budget": 0,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"basket_arb": {"mode": "paper"}}})
        c = fake_with_arb(can_trade=False)
        engine.run_once(c)
        engine.run_once(c)
        engine.run_once(c)
        self.assertEqual(len(paper.open_rows()), 4)  # one basket, not three


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
