"""Many runs of random trouble: the bot must never put more than its budget at risk.

A fake exchange with a seeded random number generator loses replies, half-fills orders,
leaves them resting, fails cancels and settles events, while a careless owner presses
Reset after every stop without looking. After every run, the cash the account has paid
out (and not yet got back) must stay within the budget. At the end, with every event
settled, the books' profit must match the exchange's to the cent: no fill was missed or
counted twice.
"""
import logging
import random
import time
import unittest
from unittest import mock

import requests

from kal import config, db, engine, ledger, maintenance
from kal.client import KalshiError, fee
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven

START_CASH = 1000.0   # far more than the budget: the books, not the account, must hold the line
BUDGET = 60.0
RUNS = 100
SEEDS = 3


class ChaosKalshi(FakeKalshi):
    def __init__(self, rng, clock):
        super().__init__(balance=START_CASH)
        self.rng, self.clock = rng, clock
        self.cash = START_CASH
        self.held = {}           # ticker -> net contracts (+ YES, - NO)
        self.order_list = []     # every order, as Kalshi's order lookup shows it
        self.live_events = {}    # event ticker -> [market tickers]
        self.made = 0
        self.sold = []
        self.owner_fills = []    # the owner's own sales, as Kalshi's fills show them

    # ---- the exchange's state ----

    def balance(self):
        return self.cash

    def positions(self):
        return [{"ticker": t, "position": n, "exposure": 0.0} for t, n in self.held.items() if abs(n) > 1e-9]

    def _apply(self, order, n):
        """Fill n more contracts of an order: cash out, position in."""
        if n <= 0:
            return
        p = order["price"]
        self.cash -= n * p + fee(n, p)
        self.held[order["ticker"]] = self.held.get(order["ticker"], 0) + (n if order["side"] == "yes" else -n)
        order["filled"] += n
        order["fill_count_fp"] = f"{order['filled']:.2f}"

    def _new_order(self, ticker, side, count, price, coid, status):
        o = {"order_id": f"o{len(self.order_list) + 1}", "client_order_id": coid, "ticker": ticker,
             "side": side, "price": price, "count": count, "filled": 0, "status": status,
             "fill_count_fp": "0.00"}
        self.order_list.append(o)
        return o

    def buy(self, ticker, side, count, limit_price, client_order_id=None):
        self.orders.append((ticker, side, count, limit_price))
        if limit_price <= 0.011:   # the $0.01 test order: the cheapest seller asks more
            return dict(self._new_order(ticker, side, count, limit_price, client_order_id, "canceled"))
        if count * limit_price + fee(count, limit_price) > self.cash:
            raise KalshiError("400 insufficient balance", 400)
        r = self.rng.random()
        if r < 0.03:
            raise requests.ConnectionError("connection reset before sending")
        if r < 0.05:
            raise KalshiError("429 too many requests", 429)
        o = self._new_order(ticker, side, count, limit_price, client_order_id, "executed")
        if r < 0.08:   # it went through, but the reply was lost
            self._apply(o, count)
            raise requests.ReadTimeout("no reply")
        if r < 0.10:   # immediate-or-cancel ignored: it rests
            o["status"] = "resting"
            return dict(o)
        n = count if r >= 0.22 else self.rng.randint(0, int(count) - 1)
        self._apply(o, n)
        o["status"] = "executed" if n == count else "canceled"
        return dict(o)

    def cancel(self, order_id):
        if self.rng.random() < 0.2:
            raise requests.ReadTimeout("cancel: no reply")
        for o in self.order_list:
            if o["order_id"] == order_id and o["status"] == "resting":
                o["status"] = "canceled"
        self.cancelled.append(order_id)
        return {}

    def fills(self, min_ts=None, ticker=None):
        return [{"order_id": o["order_id"], "ticker": o["ticker"], "side": o["side"], "action": "buy",
                 "count_fp": f"{o['filled']:.2f}", f"{o['side']}_price_dollars": f"{o['price']:.4f}",
                 "created_time": "2026-10-25T00:00:00Z"} for o in self.order_list
                if o["filled"] > 0 and ticker in (None, o["ticker"])] + \
            [f for f in self.owner_fills if ticker in (None, f["ticker"])]

    def sell_some(self):
        """The owner sells one of the bot's positions in the app, at the bid."""
        open_ = sorted(t for t, n in self.held.items() if n and self.market_info.get(t, {}).get("status") == "active")
        if not open_:
            return None
        t = self.rng.choice(open_)
        n = self.held.pop(t)
        self.cash += abs(n) * 0.5
        side = "yes" if n > 0 else "no"
        self.owner_fills.append({"order_id": f"owner-{len(self.owner_fills)}", "ticker": t, "side": side,
                                 "action": "sell", "count_fp": f"{abs(n):.2f}", f"{side}_price_dollars": "0.5000",
                                 "created_time": "2026-10-25T00:00:00Z"})
        return t

    # ---- between runs ----

    def tick(self):
        """Time passes: resting orders may fill, new events open, some events settle."""
        for o in self.order_list:
            if o["status"] == "resting" and self.rng.random() < 0.5:
                self._apply(o, o["count"] - o["filled"])
                o["status"] = "executed"
        if self.rng.random() < 0.6:
            self._open_event()
        if self.live_events and self.rng.random() < 0.15:
            self.settle(self.rng.choice(sorted(self.live_events)))

    def _open_event(self):
        self.made += 1
        ev = f"EV{self.made}"
        n = self.rng.randint(3, 5)
        bids = [round(self.rng.uniform(0.05, 0.6), 2) for _ in range(n)]
        close = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock() + 2 * 86400))
        markets = []
        for i, b in enumerate(bids):
            t = f"{ev}-{i}"
            markets.append({"ticker": t, "yes_bid_dollars": f"{b:.4f}", "status": "active", "close_time": close,
                            "volume_24h_fp": "500.00"})
            self.books[t] = {"yes": [(b, self.rng.randint(5, 60))], "no": [(round(0.99 - b, 2), 50)]}
            self.market_info[t] = {"ticker": t, "status": "active", "yes_bid_dollars": f"{b:.4f}",
                                   "no_bid_dollars": f"{0.99 - b:.4f}"}
        self.event_list.append({"event_ticker": ev, "title": ev, "mutually_exclusive": True, "markets": markets})
        self.market_list += markets
        self.live_events[ev] = [m["ticker"] for m in markets]

    def settle(self, ev):
        tickers = self.live_events.pop(ev)
        winner = self.rng.choice(tickers)
        self.event_list = [e for e in self.event_list if e["event_ticker"] != ev]
        self.market_list = [m for m in self.market_list if m["ticker"] not in tickers]
        for o in self.order_list:   # a settled market's orders can't fill any more
            if o["ticker"] in tickers and o["status"] == "resting":
                o["status"] = "canceled"
        for t in tickers:
            result = "yes" if t == winner else "no"
            self.market_info[t] = {"ticker": t, "status": "settled", "result": result}
            n = self.held.pop(t, 0)
            if (n > 0 and result == "yes") or (n < 0 and result == "no"):
                self.cash += abs(n)   # each winning contract pays $1

    def settle_all(self):
        for ev in sorted(self.live_events):
            self.settle(ev)


class ChaosTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": BUDGET, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})
        orders_proven()

    def chaos(self, seed, runs=RUNS, events=None):
        """`events`: {run number: "backup" | "restore" | "sell"}, things the owner does."""
        rng, now = random.Random(seed), [time.time()]
        clock = lambda: now[0]   # noqa: E731
        c = ChaosKalshi(rng, clock)
        trips = []
        with mock.patch("time.time", clock):
            for i in range(runs):
                c.tick()
                what = (events or {}).get(i)
                if what == "backup":
                    maintenance.backup()
                elif what == "restore":
                    maintenance.restore(maintenance.backups()[0][0])
                elif what == "sell":
                    c.sold.append(c.sell_some())
                engine.run_once(c)
                reason = config.load()["disabled_reason"]
                if reason:
                    trips.append(reason)
                    config.clear_trip()   # the careless owner: Reset, every time, without looking
                    config.update({"enabled": True})
                out = START_CASH - c.cash
                self.assertLessEqual(out, BUDGET + 1e-6, f"seed {seed}: ${out:.2f} out of the account")
                now[0] += 300
            c.settle_all()
            for _ in range(15):   # let lost replies resolve and settlements book
                engine.run_once(c)
                if config.load()["disabled_reason"]:
                    trips.append(config.load()["disabled_reason"])
                    config.clear_trip()
                    config.update({"enabled": True})
                now[0] += 300
        return c, trips

    def test_random_trouble_never_breaks_the_budget(self):
        logging.disable(logging.ERROR)   # hundreds of expected stops: keep the test output readable
        self.addCleanup(logging.disable, logging.NOTSET)
        traded = 0
        for seed in range(SEEDS):
            with self.subTest(seed=seed):
                for t in ("orders", "signals", "settlements", "writeoffs", "runs", "kv"):
                    db.execute(f"DELETE FROM {t}")
                orders_proven()
                config.update({"enabled": True, "disabled_reason": ""})
                c, trips = self.chaos(seed)
                bought = [o for o in c.order_list if o["filled"] > 0]
                traded += len(bought)
                # Nobody sold anything and every fill was found: the books never saw Kalshi
                # holding more or less than they knew about.
                self.assertFalse([r for r in trips if "books show" in r or "books don't know" in r], trips)
                self.assertEqual(db.query("SELECT COUNT(*) AS n FROM writeoffs")[0]["n"], 0)
                self.assertEqual(ledger.unresolved_orders(), [])
                # Every event settled: the books' profit is the exchange's, to the cent.
                self.assertAlmostEqual(ledger.realized(), c.cash - START_CASH, places=6)
                self.assertAlmostEqual(ledger.open_cost(), 0, places=6)
        self.assertGreater(traded, 20)   # the test did trade

    def test_restore_and_owner_sales_keep_the_books_safe(self):
        # The books are restored from an older backup mid-way, and the owner sells some of
        # the bot's contracts in the app. The books must stay within budget and never count
        # more profit than the exchange paid.
        logging.disable(logging.ERROR)
        self.addCleanup(logging.disable, logging.NOTSET)
        c, trips = self.chaos(7, runs=90, events={20: "backup", 45: "restore", 60: "sell", 70: "sell"})
        self.assertTrue(any("Books restored" in r for r in trips), trips)
        self.assertFalse([r for r in trips if "books show" in r], trips)
        self.assertEqual(ledger.unresolved_orders(), [])
        self.assertAlmostEqual(ledger.open_cost(), 0, places=6)
        true_pnl = c.cash - START_CASH
        if any(c.sold):
            self.assertGreater(db.query("SELECT COUNT(*) AS n FROM writeoffs")[0]["n"], 0)
            self.assertLessEqual(ledger.realized(), true_pnl + 1e-6)   # never richer than real
        else:
            self.assertAlmostEqual(ledger.realized(), true_pnl, places=6)


if __name__ == "__main__":
    unittest.main()
