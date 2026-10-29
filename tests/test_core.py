import base64
import unittest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kal import client, config, db, engine, paper, risk, sizing
from kal.strategies import basket_arb
from kal.strategies.base import Context, Leg, Signal
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven


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

    def _capture(self, reply):
        k = client.Kalshi()
        sent = []

        def req(method, endpoint, params=None, body=None):
            sent.append((method, endpoint, body))
            import json as _j
            from types import SimpleNamespace
            return SimpleNamespace(status_code=201, json=lambda: reply, text=_j.dumps(reply))
        k._req = req
        return k, sent

    def test_v2_order_body_maps_yes_and_no_onto_the_yes_book(self):
        k, sent = self._capture({"order_id": "o1", "fill_count_fp": "5.00", "remaining_count_fp": "0.00"})
        k.buy("KX-1", "yes", 5, 0.93, client_order_id="c1")
        k.buy("KX-1", "no", 5, 0.95, client_order_id="c2")
        (m1, e1, yes), (m2, e2, no) = sent
        self.assertEqual((m1, e1), ("POST", "/portfolio/events/orders"))
        self.assertEqual((yes["side"], yes["price"], yes["count"]), ("bid", "0.9300", "5.00"))
        self.assertEqual((no["side"], no["price"], no["count"]), ("ask", "0.0500", "5.00"))  # NO at 95c = ask 5c
        self.assertEqual((yes["client_order_id"], yes["time_in_force"]), ("c1", "immediate_or_cancel"))
        with self.assertRaises(ValueError):
            k.buy("KX-1", "maybe", 1, 0.5)

    def test_v2_reply_without_status(self):
        self.assertFalse(client.resting({"order_id": "o", "fill_count": 5, "remaining_count": 0}))
        self.assertTrue(client.resting({"order_id": "o", "fill_count": 2, "remaining_count": 3}))
        self.assertTrue(client.resting({"status": "resting"}))
        self.assertFalse(client.resting({"status": "executed"}))
        self.assertEqual(client.filled({"fill_count": 4, "remaining_count": 1}, 5), 4)

    def test_rejected_order_raises_as_rejected(self):
        k, sent = self._capture({"error": {"code": "invalid_price"}})
        from types import SimpleNamespace
        k._req = lambda *a, **kw: SimpleNamespace(status_code=400, json=lambda: {}, text="bad price")
        with self.assertRaises(client.KalshiError) as e:
            k.buy("KX-1", "yes", 1, 0.5)
        self.assertTrue(e.exception.rejected)

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
        with self.assertRaises(ValueError):
            config.update({"trade_interval_min": float("inf")})   # was an OverflowError, not a refusal

    def test_summary_hour_is_a_real_setting(self):
        """docs/DEPLOY.md says `kal set summary_hour_utc 14`. It is in DEFAULTS (so `set`
        knows it), with the hour the scheduler always used, and a bad hour is refused instead
        of being saved and then ignored."""
        from kal import scheduler
        self.assertEqual(config.load()["summary_hour_utc"], scheduler.SUMMARY_HOUR)
        self.assertEqual(scheduler.daily_hours(config.load())["summary"], scheduler.SUMMARY_HOUR)
        config.update({"summary_hour_utc": 7})
        self.assertEqual(scheduler.daily_hours(config.load())["summary"], 7)
        for bad in (24, -1, 7.5, "7", True, None, float("inf"), float("nan")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.update({"summary_hour_utc": bad})
        self.assertEqual(config.load()["summary_hour_utc"], 7)

    def test_a_switch_of_exchange_waits_for_a_trade_run(self):
        """Each exchange has its own books, and a run looks the file up as it goes."""
        import threading
        import time
        from unittest import mock
        with mock.patch.object(config, "SWITCH_WAIT", 0.3), risk.run_lock():
            with self.assertRaises(config.Busy):
                config.switch({"env": "demo"})
            config.switch({"budget": 5})        # not a switch: no wait
            config.switch({"env": "prod"})      # nor is the exchange it's already on
        self.assertEqual((config.load()["env"], config.load()["budget"]), ("prod", 5))
        held, finished = threading.Event(), []

        def run():                              # a run that ends a moment after the switch is asked
            with risk.run_lock():
                held.set()
                time.sleep(0.5)
                finished.append(config.load()["env"])
        t = threading.Thread(target=run)
        t.start()
        held.wait(5)
        config.switch({"env": "demo"}, wait=10)
        self.assertEqual(finished, ["prod"])    # it waited for the run to end, then switched
        self.assertEqual(config.load()["env"], "demo")
        t.join()
        config.update({"enabled": True})
        with self.assertRaises(ValueError):
            config.switch({"env": "prod"})      # never while the bot is on
        self.assertEqual(config.load()["env"], "demo")


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
        orders_proven()

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

    def test_the_exchange_cant_change_under_a_run(self):
        """Mid-basket, the owner types `kal on --env demo` (bot on), then `kal off` and
        `kal set env demo` (bot off, but the run is still finishing). Both are refused, so
        every leg is booked in the real-money books (critic repro: EnvSwitchMidRun)."""
        import io
        import os
        import sqlite3
        from contextlib import redirect_stdout
        from unittest import mock
        from kal.__main__ import main as cli
        c = fake_with_arb(balance=10_000)
        real_buy = c.buy
        codes = []

        def buy(*a, **k):
            if not codes:
                with redirect_stdout(io.StringIO()):
                    codes.append(cli(["on", "--env", "demo"]))
                    config.turn_off()
                    codes.append(cli(["set", "env", "demo"]))   # waits for the run, which can't end yet
            return real_buy(*a, **k)
        c.buy = buy
        with mock.patch.object(config, "SWITCH_WAIT", 0.3):
            s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual(codes, [2, 1])
        self.assertEqual(config.load()["env"], "prod")
        self.assertEqual(len(c.orders), 4)
        prod = sqlite3.connect(db.path("prod"))
        self.assertEqual(prod.execute("SELECT COUNT(*) FROM orders WHERE filled > 0").fetchone()[0], 4)
        prod.close()
        self.assertFalse(os.path.exists(db.path("demo")))

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
    def test_trip_sends_an_alert_and_survives_failure(self):
        import os
        from unittest import mock
        from kal import notify
        os.environ["KAL_NTFY_TOPIC"] = "test-topic"
        self.addCleanup(os.environ.pop, "KAL_NTFY_TOPIC", None)
        with mock.patch.object(notify.requests, "post") as post:
            config.trip("lost too much")
        text = post.call_args.kwargs["data"].decode()
        self.assertIn("lost too much", text)
        # The bot is already off: it says so, and that there's no rush. It never sends the
        # owner to the dashboard, which a phone can't reach, and it isn't a 3 AM alarm.
        self.assertIn("The bot is off", text)
        self.assertIn("No rush", text)
        self.assertIn("sudo kal health", text)
        self.assertNotIn("dashboard", text)
        self.assertEqual(post.call_args.kwargs["headers"]["Priority"], "high")
        with mock.patch.object(notify.requests, "post", side_effect=OSError("no network")):
            config.trip("again")  # must not raise
        self.assertEqual(config.load()["disabled_reason"], "again")

    def test_a_failed_alert_never_logs_the_topic(self):
        """The error's text quotes the URL, which holds the secret topic."""
        import os
        import requests
        from unittest import mock
        from kal import notify
        os.environ["KAL_NTFY_TOPIC"] = "kal-SECRET-topic-77"
        self.addCleanup(os.environ.pop, "KAL_NTFY_TOPIC", None)
        boom = requests.ConnectionError("HTTPSConnectionPool(host='ntfy.sh', port=443): Max retries exceeded "
                                        "with url: /kal-SECRET-topic-77")
        with mock.patch.object(notify.requests, "post", side_effect=boom), \
                self.assertLogs("kal.notify", "WARNING") as logs:
            self.assertFalse(notify.send("t", "m"))
        self.assertNotIn("SECRET", "\n".join(logs.output))
        self.assertIn("ConnectionError", "\n".join(logs.output))

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
        orders_proven()
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

    def test_position_the_books_cant_explain_trips(self):
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})
        self._fill("A", "no", 10, 0.5)
        c = FakeKalshi()
        c.market_info["A"] = {"ticker": "A", "status": "active", "no_bid_dollars": "0.4900"}
        c.position_list = [{"ticker": "A", "position": -10, "exposure": 5}]
        engine.run_once(c)
        self.assertEqual(config.load()["disabled_reason"], "")  # matches the books
        c.position_list = [{"ticker": "A", "position": -25, "exposure": 12}]  # a lost fill
        engine.run_once(c)
        self.assertIn("books show", config.load()["disabled_reason"])

    def test_owners_own_positions_are_ignored(self):
        c = FakeKalshi()
        c.position_list = [{"ticker": "MINE", "position": 50, "exposure": 20}]
        engine.run_once(c)
        self.assertEqual(config.load()["disabled_reason"], "")

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


class YearlyReturnTests(unittest.TestCase):
    def test_short_fat_trade_does_not_overflow(self):
        import time as _t
        self.assertGreater(risk.yearly_return(1.09, 1 / 24), 1e6)   # 1.09 ** 8760 would overflow
        self.assertAlmostEqual(risk.yearly_return(1.10, 365), 0.10)
        self.assertEqual(risk.yearly_return(0, 10), -1.0)
        cfg = dict(config.DEFAULTS, max_per_trade=30)
        sig = Signal("x", "E", "t", [Leg("A", "no", 0.45, 10), Leg("B", "no", 0.45, 10)],
                     payout_min=10.0, closes_at=_t.time() + 600)
        self.assertIsNone(risk.Limits(cfg, 1000).refuse(sig))  # used to raise OverflowError

    def test_arbs_rank_by_yearly_return(self):
        import time as _t
        from kal.strategies import ladder_arb
        soon = Signal("x", "E1", "t", [Leg("A", "no", 0.45, 10), Leg("B", "no", 0.45, 10)],
                      payout_min=10.0, closes_at=_t.time() + 2 * 86400)
        later = Signal("x", "E2", "t", [Leg("C", "no", 0.40, 10), Leg("D", "no", 0.40, 10)],
                       payout_min=10.0, closes_at=_t.time() + 25 * 86400)
        self.assertTrue(soon.guaranteed and later.guaranteed)
        self.assertGreater(later.payout_min - later.cost, soon.payout_min - soon.cost)  # more dollars...
        self.assertGreater(ladder_arb.yearly(soon), ladder_arb.yearly(later))  # ...but far slower


class FinalityTests(unittest.TestCase):
    def test_determined_is_not_final(self):
        self.assertIsNone(client.settled_result({"status": "determined", "result": "yes"}))
        self.assertEqual(client.settled_result({"status": "finalized", "result": "yes"}), "yes")
        self.assertEqual(client.settled_result({"status": "settled", "result": "no"}), "no")


class SettlementValueTests(TempHome):
    def test_payouts(self):
        f = client.yes_payout
        self.assertIsNone(f({"status": "active", "result": ""}))
        self.assertIsNone(f({"status": "determined", "result": "yes"}))
        self.assertEqual(f({"status": "finalized", "result": "no", "settlement_value_dollars": "0.0000"}), 0.0)
        self.assertEqual(f({"status": "finalized", "result": "yes", "settlement_value_dollars": "1.0000"}), 1.0)
        self.assertEqual(f({"status": "settled", "result": "yes"}), 1.0)
        self.assertEqual(f({"status": "finalized", "result": "", "settlement_value_dollars": "0.4200"}), 0.42)
        for bad in ({"status": "finalized", "result": "void"},
                    {"status": "finalized", "result": "yes", "settlement_value_dollars": "0.0000"},
                    {"status": "finalized", "result": "", "settlement_value_dollars": "1.7"},
                    {"status": "finalized", "result": "", "settlement_value_dollars": "lots"}):
            with self.assertRaises(client.UnknownSettlement, msg=bad):
                f(bad)

    def _fill(self, ticker, side, n, p):
        db.insert("orders", ticker=ticker, side=side, count=n, price=p, filled=n, status="executed")

    def test_partial_payout_is_booked_both_sides(self):
        from kal import ledger
        self._fill("S", "yes", 10, 0.30)
        self._fill("T", "no", 10, 0.30)
        c = FakeKalshi()
        c.market_info["S"] = {"status": "finalized", "result": "", "settlement_value_dollars": "0.4200"}
        c.market_info["T"] = {"status": "finalized", "result": "", "settlement_value_dollars": "0.4200"}
        self.assertEqual(ledger.settle(c), 2)
        rows = {r["ticker"]: r for r in db.query("SELECT * FROM settlements")}
        self.assertAlmostEqual(rows["S"]["revenue"], 4.2)
        self.assertAlmostEqual(rows["T"]["revenue"], 5.8)
        self.assertEqual(ledger.open_cost(), 0)

    def test_unknown_settlement_trips_the_bot(self):
        config.update({"enabled": True, "budget": 50})
        self._fill("V", "no", 10, 0.9)
        c = FakeKalshi()
        c.market_info["V"] = {"ticker": "V", "status": "finalized", "result": "void"}
        engine.run_once(c)
        self.assertIn("doesn't understand", config.load()["disabled_reason"])
        self.assertEqual(c.orders, [])

    def test_unknown_paper_settlement_never_counts(self):
        from kal.strategies.base import Leg, Signal
        sig = Signal("favorites", "E", "t", [Leg("P", "yes", 0.5, 4)])
        paper.record(sig, 1)
        c = FakeKalshi()
        c.market_info["P"] = {"status": "finalized", "result": "void"}
        paper.settle(c)
        row = db.query("SELECT * FROM paper")[0]
        self.assertEqual((row["settled"], row["result"], row["pnl"]), (1, "unknown", None))


class PaperSettleTogetherTests(TempHome):
    def test_a_sets_legs_are_booked_together(self):
        # A month-end pair: the daily leg settles an hour after 5 PM, the monthly days later.
        # Booking the daily's win first would show a paper peak that never was.
        from kal.strategies.base import Leg, Signal
        sig = Signal("metal_link_arb", "MON", "t", [Leg("D-T4093", "no", 0.41, 23), Leg("MON-T4092.99", "yes", 0.51, 23)])
        paper.record(sig, 7)
        solo = Signal("favorites", "E", "t", [Leg("P", "yes", 0.5, 4)])
        paper.record(solo, 8)
        c = FakeKalshi()
        c.market_info["D-T4093"] = {"status": "finalized", "result": "no"}
        c.market_info["P"] = {"status": "finalized", "result": "yes"}
        c.market_info["MON-T4092.99"] = {"status": "closed", "result": ""}
        self.assertEqual(paper.settle(c), 1)        # the lone bet settles; the pair waits
        self.assertEqual({r["ticker"] for r in paper.open_rows()}, {"D-T4093", "MON-T4092.99"})
        before = paper.realized()
        c.market_info["MON-T4092.99"] = {"status": "finalized", "result": "no"}
        self.assertEqual(paper.settle(c), 2)        # both legs at once
        self.assertEqual(paper.open_rows(), [])
        self.assertAlmostEqual(paper.realized() - before, 23 * 1.0 - sig.cost, places=6)

    def test_an_unknown_leg_still_closes_once_the_set_is_done(self):
        from kal.strategies.base import Leg, Signal
        sig = Signal("x", "E", "t", [Leg("A", "yes", 0.5, 2), Leg("B", "no", 0.4, 2)])
        paper.record(sig, 3)
        c = FakeKalshi()
        c.market_info["A"] = {"status": "finalized", "result": "void"}
        c.market_info["B"] = {"status": "open", "result": ""}
        self.assertEqual(paper.settle(c), 0)
        self.assertEqual(len(paper.open_rows()), 2)
        c.market_info["B"] = {"status": "finalized", "result": "no"}
        paper.settle(c)
        rows = {r["ticker"]: r for r in db.query("SELECT * FROM paper")}
        self.assertEqual((rows["A"]["settled"], rows["A"]["result"], rows["A"]["pnl"]), (1, "unknown", None))
        self.assertEqual((rows["B"]["settled"], rows["B"]["result"]), (1, "no"))


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
