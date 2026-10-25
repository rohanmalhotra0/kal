import base64
import json
import os
import sys
import threading
import time
import types
import unittest
from datetime import datetime, timezone
from unittest import mock

from kal import config, db, ledger, risk, scheduler
from kal.client import Kalshi, KalshiError, fee, load_key
from kal.dashboard import app as dash
from kal.research import backtest, promote
from kal.scheduler import Scheduler
from tests.fakes import FakeKalshi
from tests.helpers import TempHome

KEY_VARS = ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PATH", "KAL_DASHBOARD_PASSWORD",
            "KAL_NTFY_TOPIC")
T0 = 1_760_000_000


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Recorder:
    """A fake job that remembers each call."""

    def __init__(self, result=None, exc=None):
        self.calls, self.result, self.exc = 0, result or {"ok": True}, exc

    def __call__(self):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


class Market(FakeKalshi):
    """Public market data for the backtester and the series search. It has no key."""

    def __init__(self):
        super().__init__(can_trade=False)
        self.series_calls = 0

    def series(self, category=None):
        self.series_calls += 1
        return self.series_list


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def add_history(c, series, n, ask, bid, result="yes"):
    """n settled one-market events, each priced ask/bid an hour before 6 h before close."""
    c.series_list.append({"ticker": series, "title": f"{series} title"})
    for i in range(n):
        close = T0 + i * 86400
        tk = f"{series}-{i}"
        c.settled.setdefault(series, []).append({
            "ticker": tk, "event_ticker": f"{series}-E{i}", "open_time": iso(close - 3 * 86400),
            "close_time": iso(close), "status": "settled", "volume_fp": "1000.00", "result": result})
        c.candle_data[tk] = [{"end_period_ts": close - 7 * 3600, "yes_ask": {"close_dollars": f"{ask:.4f}"},
                              "yes_bid": {"close_dollars": f"{bid:.4f}"}}]


def order(ticker, side, n, p, filled=None, status="executed", strategy="basket_arb", ts=None, signal_id=None,
          error=None):
    return db.insert("orders", ts=ts or time.time(), signal_id=signal_id, strategy=strategy, ticker=ticker,
                     side=side, count=n, price=p, filled=n if filled is None else filled, status=status,
                     client_order_id=f"c-{ticker}", error=error)


def settle(results):
    """Book these markets' results the real way, through ledger.settle."""
    c = FakeKalshi()
    c.market_info = {tk: {"status": "settled", "result": r} for tk, r in results.items()}
    ledger.settle(c)


def write_research(home, name, data):
    os.makedirs(os.path.join(home, "research"), exist_ok=True)
    with open(os.path.join(home, "research", f"{name}.json"), "w") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))


class Base(TempHome):
    def setUp(self):
        super().setUp()
        self._saved = {k: os.environ.pop(k, None) for k in KEY_VARS}
        self.jobs = {"trade": Recorder(), "research": Recorder(), "report": Recorder()}
        self.sched = Scheduler(jobs=self.jobs, threaded=False)
        self.market = Market()
        self.client = self.make_client()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def make_client(self):
        self.app = dash.create_app(self.sched, market_client=lambda: self.market)
        return self.app.test_client()

    def post(self, path, body, **kw):
        return self.client.post(path, data=json.dumps(body, allow_nan=True), content_type="application/json", **kw)


class OrderTestStateTests(Base):
    """Real money waits for the $0.01 test order (kal/gate.py); the Home tab says where it is."""

    def state(self):
        return self.client.get("/api/state").get_json()["order_test"]

    def test_not_passed_until_it_passes_for_this_key_and_exchange(self):
        from kal import gate
        self.assertFalse(self.state()["passed"])
        fake = types.SimpleNamespace(key_id="kid-1")
        gate.record(fake, "prod", True, "KXA-1: accepted, 0 filled")
        with mock.patch.object(dash, "_key_id", return_value="kid-1"):
            t = self.state()
            self.assertTrue(t["passed"])
            self.assertIn("accepted", t["detail"])
            config.update({"env": "demo"})
            self.assertFalse(self.state()["passed"])   # passed on prod, not demo
        with mock.patch.object(dash, "_key_id", return_value="another-key"):
            config.update({"env": "prod"})
            self.assertFalse(self.state()["passed"])   # a new key needs its own test

    def test_a_failure_says_why(self):
        from kal import gate
        gate.record(types.SimpleNamespace(key_id="kid-1"), "prod", False, "Kalshi refused it: 400 bad field")
        with mock.patch.object(dash, "_key_id", return_value="kid-1"):
            t = self.state()
        self.assertFalse(t["passed"])
        self.assertIn("400 bad field", t["detail"])


class StateTests(Base):
    def seed(self):
        now = time.time()
        db.insert("equity", ts=now - 600, balance=100, exposure=0, equity=100, paper_equity=1000)
        db.insert("equity", ts=now, balance=90, exposure=8, equity=99.5, paper_equity=1002)
        db.put("day_start", {"day": time.strftime("%Y-%m-%d", time.gmtime(now)), "equity": 100})
        sid = db.insert("signals", strategy="basket_arb", event="EV1", title="Who wins?", mode="paper",
                        guaranteed=1, cost=9.5, payout_min=10, exp_profit=0.5, legs=[], status="accepted")
        db.insert("signals", strategy="basket_arb", event="EV2", title="skip me", mode="paper",
                  guaranteed=1, cost=9.5, payout_min=10, exp_profit=0.5, legs=[], status="skipped: over budget")
        db.insert("paper", signal_id=sid, strategy="basket_arb", event="EV1", ticker="EV1-0", side="no",
                  count=10, price=0.45, cost=4.6, settled=1, result="no", pnl=5.4, settled_ts=now)
        db.insert("paper", signal_id=sid, strategy="basket_arb", event="EV1", ticker="EV1-1", side="no",
                  count=10, price=0.5, cost=5.1)
        db.insert("runs", kind="trade", ok=1, duration=1.2, message=json.dumps(
            {"ok": True, "signals": 2, "live_trades": 0, "paper_trades": 1, "notes": ["no API key"]}))
        db.insert("runs", kind="trade", ok=0, duration=0.1, message="not json")
        edges = [{"id": "KXA:0.9-0.96", "series": "KXA", "lo": 0.9, "hi": 0.96, "hours_before_close": 6,
                  "roi": 0.04, "roi_lo": 0.01, "trades": 80, "events": 30, "status": "candidate"},
                 {"id": "KXB:0.8-0.9", "series": "KXB", "lo": 0.8, "hi": 0.9, "roi": "junk", "status": "live"},
                 "not an edge"]
        write_research(self.home, "edges", edges)
        write_research(self.home, "words", {"promoted": False, "roi": 0.1, "words": {"a|b": [1, 2]}, "series": ["X"]})
        write_research(self.home, "broken", "{nope")

    def test_shape(self):
        self.seed()
        r = self.client.get("/api/state")
        self.assertEqual(r.status_code, 200)
        s = r.get_json()
        for key in ("config", "account", "today", "positions", "money", "strategies", "research",
                    "trades", "orders", "runs", "equity", "scheduler", "has_key", "errors", "alerts"):
            self.assertIn(key, s)
        self.assertEqual(s["errors"], [])
        self.assertFalse(s["has_key"])
        self.assertAlmostEqual(s["account"]["cash"], 90)
        self.assertAlmostEqual(s["account"]["value"], 99.5)
        self.assertAlmostEqual(s["today"]["pnl"], -0.5)
        self.assertAlmostEqual(s["today"]["paper_pnl"], 5.4)
        self.assertEqual(s["positions"]["paper"], 1)
        self.assertEqual(len(s["equity"]), 2)
        for key in ("budget", "realized", "capital", "open_cost", "available", "max_loss", "reinvest_profits",
                    "paper_profit", "paper_value", "paper_open_cost"):
            self.assertIn(key, s["money"])
        self.assertAlmostEqual(s["money"]["paper_profit"], 5.4)
        self.assertAlmostEqual(s["money"]["paper_open_cost"], 5.1)

        names = {x["name"]: x for x in s["strategies"]}
        self.assertIn("basket_arb", names)
        b = names["basket_arb"]
        self.assertEqual(b["kind"], "arbitrage")
        self.assertEqual(b["mode"], "live")
        self.assertTrue(b["description"])
        self.assertEqual(b["paper"]["trades"], 2)
        self.assertAlmostEqual(b["paper"]["pnl"], 5.4)

        self.assertEqual([t["title"] for t in s["trades"]], ["Who wins?"])  # skipped signals left out
        self.assertEqual(s["runs"][1]["text"], "2 found, 0 real, 1 practice")
        self.assertEqual(s["runs"][1]["notes"], ["no API key"])
        self.assertFalse(s["runs"][0]["ok"])

        res = s["research"]
        self.assertEqual(res["edges"]["total"], 2)
        self.assertEqual(res["edges"]["counts"], {"candidate": 1, "live": 1})
        self.assertEqual(res["edges"]["top"][0]["status"], "live")   # live edges first
        self.assertIsNone(res["edges"]["top"][0]["roi"])             # junk number -> None, not a crash
        self.assertEqual(res["words"]["words"], 1)
        self.assertIn({"name": "broken", "ok": False}, res["files"])

        self.assertIn("trade", s["scheduler"])
        self.assertIn("next", s["scheduler"]["trade"])

    def test_empty_home(self):
        s = self.client.get("/api/state").get_json()
        self.assertIsNone(s["account"])
        self.assertIsNone(s["today"]["pnl"])
        self.assertEqual(s["equity"], [])
        self.assertEqual(s["trades"], [])
        self.assertEqual(s["money"]["realized"], 0)
        self.assertEqual(s["money"]["open_cost"], 0)

    def test_broken_strategy_doesnt_break_the_page(self):
        with mock.patch.object(dash, "load_all", side_effect=RuntimeError("bad module")):
            s = self.client.get("/api/state").get_json()
        self.assertTrue(any("bad module" in e for e in s["errors"]))
        self.assertIn("basket_arb", [x["name"] for x in s["strategies"]])  # still listed from config

    def test_page_and_static_files(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"How much money can the bot use?", r.data)
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        for path in ("/static/app.js", "/static/style.css"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200)
            r.close()

    def test_downsamples_long_history(self):
        now = time.time()
        with db.connect() as c:
            c.executemany("INSERT INTO equity (ts, balance, exposure, equity, paper_equity) VALUES (?, 0, 0, 0, ?)",
                          [(now - i * 60, 1000 + i) for i in range(2000)])
        rows = self.client.get("/api/state").get_json()["equity"]
        self.assertLessEqual(len(rows), dash.CHART_POINTS + 1)
        self.assertAlmostEqual(rows[-1]["ts"], now, delta=1)  # the newest point is always kept


class PageTests(Base):
    def test_every_tab_and_money_label_is_on_the_page(self):
        html = self.client.get("/").get_data(as_text=True)
        for text in ('id="tab-home"', 'id="tab-trades"', 'id="tab-research"', 'id="tab-test"', "Test an idea",
                     "Capital you gave", "Profit so far", "Bot's money now", "In open bets", "Free to use",
                     "Phone alerts", "Send test alert", "Find a series", "Hours before close", "Run test"):
            with self.subTest(text=text):
                self.assertIn(text, html)

    def test_env_overrides_are_gone(self):
        r = self.client.get("/static/app.js")
        js = r.get_data(as_text=True)
        r.close()
        self.assertNotIn("env_overrides", js)  # env vars can't change settings any more
        self.assertNotIn(".innerHTML", js)     # market titles must never run as code


class MoneyPanelTests(Base):
    def seed(self):
        config.update({"budget": 300})
        order("A", "no", 10, 0.40)                                # wins
        order("B", "yes", 5, 0.90)                                # loses
        order("C", "yes", 4, 0.50)                                # still open
        order("D", "yes", 7, 0.30, filled=0, status="canceled")   # never filled: costs nothing
        settle({"A": "no", "B": "no"})
        won = 10 - (10 * 0.40 + fee(10, 0.40))
        lost = -(5 * 0.90 + fee(5, 0.90))
        return won + lost, 4 * 0.50 + fee(4, 0.50)

    def money(self):
        return self.client.get("/api/state").get_json()["money"]

    def test_numbers_come_from_the_ledger(self):
        profit, open_cost = self.seed()
        m = self.money()
        self.assertEqual(m["budget"], 300)
        self.assertAlmostEqual(m["realized"], profit)
        self.assertAlmostEqual(m["capital"], 300 + profit)
        self.assertAlmostEqual(m["open_cost"], open_cost)
        self.assertAlmostEqual(m["available"], 300 + profit - open_cost)
        self.assertEqual(m["max_loss"], 300)  # "It can never lose more than $300 in total"
        self.assertTrue(m["reinvest_profits"])
        cfg = config.load()
        self.assertAlmostEqual(m["capital"], ledger.capital(cfg))
        self.assertAlmostEqual(m["available"], ledger.available(cfg))
        self.assertAlmostEqual(m["realized"], ledger.realized())

    def test_profit_kept_aside_when_not_reinvested(self):
        profit, open_cost = self.seed()
        self.assertGreater(profit, 0)
        config.update({"reinvest_profits": False})
        m = self.money()
        self.assertAlmostEqual(m["realized"], profit)
        self.assertAlmostEqual(m["capital"], 300)
        self.assertAlmostEqual(m["available"], 300 - open_cost)

    def test_a_loss_always_counts(self):
        config.update({"budget": 300, "reinvest_profits": False})
        order("E", "yes", 20, 0.90)
        settle({"E": "no"})
        m = self.money()
        self.assertAlmostEqual(m["realized"], -(18 + fee(20, 0.90)))
        self.assertAlmostEqual(m["capital"], 300 - 18 - fee(20, 0.90))

    def test_demo_has_its_own_books(self):
        profit, _ = self.seed()
        config.update({"env": "demo"})
        s = self.client.get("/api/state").get_json()
        self.assertEqual(s["config"]["env"], "demo")
        self.assertEqual(s["money"]["realized"], 0)    # demo fills never touch the real ledger
        self.assertEqual(s["money"]["open_cost"], 0)
        self.assertAlmostEqual(s["money"]["capital"], 300)
        config.update({"env": "prod"})
        self.assertAlmostEqual(self.money()["realized"], profit)

    def test_no_budget_means_nothing_to_lose(self):
        m = self.money()
        self.assertEqual((m["budget"], m["max_loss"], m["capital"], m["available"]), (0, 0, 0, 0))


class TradesApiTests(Base):
    def seed(self):
        now = time.time()
        sid = db.insert("signals", strategy="basket_arb", event="EV", title="Who wins?", mode="live",
                        guaranteed=1, cost=9, payout_min=10, exp_profit=1, legs=[], status="executed")
        order("A", "no", 10, 0.40, ts=now - 600, signal_id=sid)                                    # won
        order("B", "yes", 5, 0.90, ts=now - 500, strategy="favorites")                             # lost
        order("C", "yes", 10, 0.50, filled=4, status="canceled", ts=now - 400, strategy="favorites")  # part, open
        order("D", "yes", 7, 0.30, filled=0, status="canceled", ts=now - 300, strategy="favorites")   # no fill
        order("E", "no", 3, 0.20, filled=0, status="rejected", ts=now - 200, strategy="favorites", error="400 closed")
        order("F", "yes", 2, 0.60, filled=0, status="unknown", ts=now - 100, strategy="favorites")    # reply lost
        settle({"A": "no", "B": "no"})
        psid = db.insert("signals", strategy="mentions", event="PEV", title="Will X say Y?", mode="paper",
                         guaranteed=0, cost=3, payout_min=0, exp_profit=0.3, legs=[], status="accepted")
        db.insert("paper", ts=now - 550, signal_id=psid, strategy="mentions", event="PEV", ticker="P1", side="yes",
                  count=10, price=0.3, cost=3.1, settled=1, result="yes", pnl=6.9, settled_ts=now)
        db.insert("paper", ts=now - 50, signal_id=psid, strategy="mentions", event="PEV", ticker="P2", side="no",
                  count=5, price=0.6, cost=3.1)

    def test_every_trade_newest_first(self):
        self.seed()
        d = self.client.get("/api/trades").get_json()
        rows = d["trades"]
        self.assertEqual([t["ticker"] for t in rows], ["P2", "F", "E", "D", "C", "B", "P1", "A"])
        by = {t["ticker"]: t for t in rows}
        self.assertEqual({k: by[k]["state"] for k in by},
                         {"A": "won", "B": "lost", "C": "open", "D": "no_fill", "E": "refused", "F": "checking",
                          "P1": "won", "P2": "open"})
        self.assertEqual({t["ticker"] for t in rows if t["kind"] == "paper"}, {"P1", "P2"})
        self.assertAlmostEqual(by["A"]["pnl"], 10 - 4 - fee(10, 0.40))
        self.assertAlmostEqual(by["B"]["pnl"], -(4.5 + fee(5, 0.90)))
        self.assertAlmostEqual(by["P1"]["pnl"], 6.9)
        for k in ("C", "D", "E", "F", "P2"):
            self.assertIsNone(by[k]["pnl"])  # no profit until it settles (or nothing was bought)
        self.assertEqual(by["A"]["title"], "Who wins?")
        self.assertEqual(by["P1"]["title"], "Will X say Y?")
        self.assertEqual((by["C"]["count"], by["C"]["filled"]), (10, 4))
        self.assertEqual(by["E"]["error"], "400 closed")
        for k in ("strategy", "side", "price", "count", "status", "ts", "kind"):
            self.assertIn(k, by["A"])
        self.assertFalse(d["more"])

    def test_totals_per_strategy_real_and_practice_apart(self):
        self.seed()
        totals = self.client.get("/api/trades").get_json()["totals"]
        live, pap = totals["live"], totals["paper"]
        self.assertEqual(set(live), {"basket_arb", "favorites"})
        self.assertEqual(set(pap), {"mentions"})
        self.assertEqual((live["basket_arb"]["trades"], live["basket_arb"]["wins"]), (1, 1))
        fav = live["favorites"]
        self.assertEqual((fav["trades"], fav["open"], fav["settled"], fav["wins"]), (2, 1, 1, 0))
        self.assertAlmostEqual(fav["open_cost"], 2 + fee(4, 0.50))
        self.assertAlmostEqual(sum(t["pnl"] for t in live.values()), ledger.realized())  # agrees with the books
        m = pap["mentions"]
        self.assertEqual((m["trades"], m["open"], m["settled"], m["wins"]), (2, 1, 1, 1))
        self.assertAlmostEqual(m["pnl"], 6.9)

    def test_a_called_off_market_shows_its_split_payout(self):
        # Real Kalshi: a cancelled 3-way match settled "scalar" at $0.33 on each outcome.
        now = time.time()
        order("S", "no", 10, 0.60, ts=now - 30, strategy="favorites")
        c = FakeKalshi()
        c.market_info = {"S": {"status": "finalized", "result": "scalar", "settlement_value_dollars": "0.3300"}}
        ledger.settle(c)
        db.insert("paper", ts=now - 20, strategy="mentions", event="PS", ticker="PS", side="yes", count=10,
                  price=0.5, cost=5.1, settled=1, result="0.3300", pnl=3.3 - 5.1, settled_ts=now)
        db.insert("paper", ts=now - 10, strategy="mentions", event="PU", ticker="PU", side="yes", count=1,
                  price=0.5, cost=0.51, settled=1, result="unknown", pnl=None, settled_ts=now)
        d = self.client.get("/api/trades").get_json()
        by = {t["ticker"]: t for t in d["trades"]}
        self.assertEqual((by["S"]["state"], by["PS"]["state"], by["PU"]["state"]), ("split", "split", "unknown"))
        self.assertAlmostEqual(by["S"]["pnl"], 10 * 0.67 - 6 - fee(10, 0.60))
        self.assertAlmostEqual(by["S"]["pnl"], ledger.realized())  # the same number as the books
        self.assertAlmostEqual(by["PS"]["pnl"], -1.8)
        self.assertIsNone(by["PU"]["pnl"])
        fav, men = d["totals"]["live"]["favorites"], d["totals"]["paper"]["mentions"]
        self.assertEqual((fav["settled"], fav["open"], fav["wins"]), (1, 0, 0))
        self.assertAlmostEqual(fav["pnl"], ledger.realized())
        self.assertEqual((men["settled"], men["wins"]), (1, 0))
        self.assertAlmostEqual(men["pnl"], -1.8)

    def test_limit(self):
        self.seed()
        d = self.client.get("/api/trades?limit=3").get_json()
        self.assertEqual(len(d["trades"]), 3)
        self.assertTrue(d["more"])
        self.assertEqual(len(d["totals"]["live"]), 2)  # totals still count everything
        self.assertEqual(self.client.get("/api/trades?limit=lots").status_code, 400)
        self.assertEqual(len(self.client.get("/api/trades?limit=99999").get_json()["trades"]), 8)

    def test_empty(self):
        d = self.client.get("/api/trades").get_json()
        self.assertEqual((d["trades"], d["totals"], d["more"]), ([], {"live": {}, "paper": {}}, False))

    def test_demo_site_shows_only_demo_trades(self):
        self.seed()
        self.assertEqual(self.client.get("/api/trades").get_json()["env"], "prod")
        config.update({"env": "demo"})
        d = self.client.get("/api/trades").get_json()
        self.assertEqual((d["env"], d["trades"], d["totals"]), ("demo", [], {"live": {}, "paper": {}}))


class ResearchApiTests(Base):
    def edge(self, eid, status, roi_lo, **kw):
        return {"id": eid, "series": eid.split(":")[0], "lo": 0.9, "hi": 0.95, "hours_before_close": 6,
                "roi": roi_lo + 0.03, "roi_lo": roi_lo, "trades": 80, "events": 40, "status": status,
                "first_seen": "2026-10-01T00:00:00+00:00", "last_tested": "2026-10-20T00:00:00+00:00", **kw}

    def paper(self, eid, ticker, event, won=None, strategy="favorites"):
        sid = db.insert("signals", strategy=strategy, event=event, title="t", mode="paper", guaranteed=0,
                        cost=4.7, payout_min=0, exp_profit=0.1, legs=[], status="accepted",
                        meta={"edge": eid, "paper_only": True} if eid else {})
        cost = 5 * 0.93 + fee(5, 0.93)
        done = won is not None
        db.insert("paper", signal_id=sid, strategy=strategy, event=event, ticker=ticker, side="yes", count=5,
                  price=0.93, cost=cost, settled=int(done), result=("yes" if won else "no") if done else None,
                  pnl=(5 * bool(won) - cost) if done else None, settled_ts=time.time() if done else None)
        return cost

    def test_edges_with_their_practice_and_real_results(self):
        write_research(self.home, "edges", [
            self.edge("S1:0.90-0.95", "candidate", 0.01, side="yes",
                      results={"trades": 3, "paper_trades": 2, "events": 2, "pnl": 1.0, "roi": 0.05, "roi_lo": -0.2}),
            self.edge("S2:0.90-0.95", "live", 0.03, side="no"),
            self.edge("S3:0.90-0.95", "retired", -0.2, side="maybe"),
            "not an edge"])
        cost = self.paper("S1:0.90-0.95", "T1", "E1", won=True)
        self.paper("S1:0.90-0.95", "T1", "E1", won=True)       # the same market again: still one market
        self.paper("S1:0.90-0.95", "T2", "E2", won=False)
        self.paper("S1:0.90-0.95", "T3", "E3")                 # still open
        self.paper("S1:0.90-0.95", "X1", "EX", won=True, strategy="longshot_fade")  # another strategy's trade
        self.paper(None, "T9", "E9", won=True)                 # a favorites trade with no edge
        sid = db.insert("signals", strategy="favorites", event="LE", title="t", mode="live", guaranteed=0, cost=4.6,
                        payout_min=0, exp_profit=0.1, legs=[], status="executed", meta={"edge": "S2:0.90-0.95"})
        order("L1", "yes", 5, 0.90, strategy="favorites", signal_id=sid)
        settle({"L1": "yes"})

        d = self.client.get("/api/research").get_json()
        self.assertEqual([e["id"] for e in d["edges"]], ["S2:0.90-0.95", "S1:0.90-0.95", "S3:0.90-0.95"])
        by = {e["id"]: e for e in d["edges"]}
        s1 = by["S1:0.90-0.95"]
        self.assertEqual(s1["status"], "candidate")
        self.assertEqual(s1["first_seen"], "2026-10-01T00:00:00+00:00")
        self.assertAlmostEqual(s1["roi_lo"], 0.01)
        self.assertEqual(s1["trades"], 80)
        p = s1["paper"]
        self.assertEqual((p["trades"], p["open"], p["settled"], p["wins"], p["markets"], p["events"]),
                         (4, 1, 3, 2, 2, 2))
        self.assertAlmostEqual(p["pnl"], 2 * (5 - cost) - cost)
        self.assertAlmostEqual(s1["verdict"]["roi_lo"], -0.2)
        self.assertEqual(s1["live"]["trades"], 0)
        s2 = by["S2:0.90-0.95"]
        self.assertEqual((s2["live"]["trades"], s2["live"]["settled"], s2["live"]["wins"]), (1, 1, 1))
        self.assertAlmostEqual(s2["live"]["pnl"], 5 - 4.5 - fee(5, 0.90))
        self.assertEqual(by["S3:0.90-0.95"]["paper"]["trades"], 0)
        # The side an edge was proven on. None (pooled, or junk) means favorites never trades it.
        self.assertEqual([by[k]["side"] for k in ("S1:0.90-0.95", "S2:0.90-0.95", "S3:0.90-0.95")],
                         ["yes", "no", None])
        self.assertEqual(d["counts"], {"live": 1, "candidate": 1, "retired": 1})
        self.assertEqual(d["need"], {"markets": promote.MIN_PAPER_TRADES, "events": promote.MIN_EVENTS})
        self.assertEqual(d["favorites_mode"], "paper")
        self.assertEqual(d["problems"], [])

    def test_no_research_yet(self):
        d = self.client.get("/api/research").get_json()
        self.assertEqual((d["edges"], d["counts"], d["problems"], d["words"]), ([], {}, [], None))

    def test_broken_file_is_reported_not_a_crash(self):
        write_research(self.home, "edges", "{nope")
        r = self.client.get("/api/research")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["edges"], [])
        self.assertIn("edges.json", r.get_json()["problems"][0])


class BacktestApiTests(Base):
    def runner(self):
        return self.app.extensions["kal_backtests"]

    def wait(self):
        t = self.runner().thread
        t.join(20)
        self.assertFalse(t.is_alive())

    def test_runs_in_the_background_and_is_listed(self):
        add_history(self.market, "KXWIN", 400, 0.93, 0.92, result="yes")
        r = self.post("/api/backtest", {"series": "kxwin", "hours_before_close": 6})
        self.assertEqual(r.status_code, 202, r.get_json())
        self.assertEqual(r.get_json()["running"]["series"], ["KXWIN"])
        self.wait()
        d = self.client.get("/api/backtests").get_json()
        self.assertIsNone(d["running"])
        self.assertTrue(d["last"]["ok"])
        self.assertEqual(len(d["results"]), 1)
        res = d["results"][0]
        self.assertEqual((res["kind"], res["series"], res["hours_before_close"]), ("manual", ["KXWIN"], 6))
        self.assertEqual((res["observations"], res["events"]), (400, 400))
        rows = {x["lo"]: x for x in res["calibration"]}
        for k in ("hi", "trades", "events", "win_rate", "roi", "roi_lo", "edge"):
            self.assertIn(k, rows[0.9])
        self.assertTrue(rows[0.9]["edge"])     # bought every 93c YES, and they all won
        self.assertFalse(rows[0.05]["edge"])   # the NO side of the same markets all lost
        self.assertEqual(rows[0.9]["win_rate"], 1)
        self.assertIn("side", rows[0.9])
        self.assertEqual(res["edges"], 1)
        stored = json.loads(db.query("SELECT results FROM backtests")[0]["results"])["calibration"]
        self.assertEqual([x["edge"] for x in res["calibration"]], [backtest.is_edge(x) for x in stored])
        self.assertEqual((d["min_trades"], d["min_events"]), (backtest.MIN_TRADES, backtest.MIN_EVENTS))
        self.assertEqual(d["hours_default"], config.load()["research_hours_before_close"])
        # A test only reports: it never orders, and it never changes what the bot trades.
        self.assertEqual(self.market.orders, [])
        self.assertFalse(os.path.exists(os.path.join(self.home, "research", "edges.json")))

    def test_only_one_test_at_a_time(self):
        gate, started = threading.Event(), threading.Event()

        def slow(series, **kw):
            started.set()
            gate.wait(10)
            return {"observations": 0, "edges": []}

        with mock.patch.object(backtest, "run", side_effect=slow) as run:
            r = self.post("/api/backtest", {"series": "KXA, kxb kxa", "hours_before_close": "12"})
            self.assertEqual(r.status_code, 202)
            self.assertTrue(started.wait(5))
            r = self.post("/api/backtest", {"series": "KXC"})
            self.assertEqual(r.status_code, 409)
            self.assertIn("already running", r.get_json()["error"])
            running = self.client.get("/api/backtests").get_json()["running"]
            self.assertEqual((running["series"], running["hours_before_close"]), (["KXA", "KXB"], 12))
            gate.set()
            self.wait()
        self.assertEqual(run.call_count, 1)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["KXA", "KXB"])
        self.assertEqual(kwargs["hours_before_close"], 12)
        self.assertIs(kwargs["client"], self.market)
        self.assertEqual(kwargs["max_markets"], config.load()["research_max_markets"])
        self.assertIsNone(self.client.get("/api/backtests").get_json()["running"])
        self.assertEqual(self.post("/api/backtest", {"series": ["KXC"]}).status_code, 202)  # free again
        self.wait()

    def test_a_failed_test_says_why(self):
        with mock.patch.object(backtest, "run", side_effect=RuntimeError("candles keep failing")), \
                self.assertLogs("kal.dashboard.app", "WARNING"):
            self.assertEqual(self.post("/api/backtest", {"series": "KXA"}).status_code, 202)
            self.wait()
        last = self.client.get("/api/backtests").get_json()["last"]
        self.assertFalse(last["ok"])
        self.assertIn("candles keep failing", last["error"])
        self.assertEqual(last["series"], ["KXA"])

    def test_bad_input_is_refused_before_anything_runs(self):
        cases = [({}, "series"), ({"series": ""}, "series"), ({"series": " , "}, "series"), ({"series": 5}, "series"),
                 ({"series": ["KXA", 5]}, "series"), ({"series": "KX/../portfolio"}, "doesn't look like"),
                 ({"series": "KXA KX?B"}, "doesn't look like"),
                 ({"series": ",".join(f"S{i}" for i in range(dash.MAX_TEST_SERIES + 1))}, "at most"),
                 ({"series": "KXA", "hours_before_close": 0}, "more than 0"),
                 ({"series": "KXA", "hours_before_close": -6}, "more than 0"),
                 ({"series": "KXA", "hours_before_close": "soon"}, "number"),
                 ({"series": "KXA", "hours_before_close": True}, "number"),
                 ({"series": "KXA", "hours_before_close": float("nan")}, "more than 0"),
                 ({"series": "KXA", "hours_before_close": dash.MAX_TEST_HOURS + 1}, "at most")]
        for data, words in cases:
            with self.subTest(data=data):
                r = self.post("/api/backtest", data)
                self.assertEqual(r.status_code, 400)
                self.assertIn(words, r.get_json()["error"])
        self.assertIsNone(self.runner().thread)
        self.assertEqual(db.query("SELECT * FROM backtests"), [])

    def test_post_must_be_json_from_this_site(self):
        r = self.client.post("/api/backtest", data='{"series": "KXA"}', content_type="text/plain")
        self.assertEqual(r.status_code, 400)
        r = self.post("/api/backtest", {"series": "KXA"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(self.runner().thread)

    def test_lists_daily_research_runs_too(self):
        backtest.record("calibration", {"series": "KXA", "hours_before_close": 6},
                        {"observations": 50, "events": 40, "calibration": [
                            {"lo": 0.9, "hi": 0.95, "trades": 50, "events": 40, "win_rate": 0.99, "roi": 0.05,
                             "roi_lo": 0.02, "side": "no"},
                            {"lo": 0.5, "hi": 0.6, "trades": "junk", "roi_lo": "junk"}]})
        backtest.record("mentions", {"series": ["KXM"], "hours_before_close": 6},
                        {"best": {"margin": 0.15, "roi": 0.1, "roi_lo": None, "trades": 12, "events": 5,
                                  "enough": False}, "observations": 80})
        db.insert("backtests", kind="manual", params="not json", results="[1, 2]")
        self.assertEqual(len(self.client.get("/api/backtests").get_json()["results"]), 1)  # manual only
        rows = self.client.get("/api/backtests?kind=all").get_json()["results"]
        self.assertEqual([r["kind"] for r in rows], ["manual", "mentions", "calibration"])
        self.assertEqual((rows[0]["series"], rows[0]["calibration"]), ([], []))   # junk row: empty, no crash
        self.assertEqual(rows[1]["mentions"]["trades"], 12)
        self.assertFalse(rows[1]["mentions"]["enough"])
        cal = rows[2]
        self.assertEqual(cal["series"], ["KXA"])
        self.assertEqual([x["edge"] for x in cal["calibration"]], [True, False])  # junk numbers are never an edge
        self.assertEqual([x["side"] for x in cal["calibration"]], ["no", None])
        self.assertIsNone(cal["calibration"][1]["trades"])
        self.assertEqual(self.client.get("/api/backtests?kind=nope").status_code, 400)
        self.assertEqual(len(self.client.get("/api/backtests?kind=all&limit=1").get_json()["results"]), 1)

    def test_a_broken_backtester_doesnt_break_the_page(self):
        with mock.patch.dict(sys.modules, {"kal.research.backtest": None}), \
                self.assertLogs("kal.dashboard.app", "ERROR"):
            self.assertEqual(self.client.get("/api/backtests").status_code, 501)
            self.assertEqual(self.post("/api/backtest", {"series": "KXA"}).status_code, 501)
            self.assertEqual(self.client.get("/api/series?q=rain").status_code, 501)
            self.assertEqual(self.client.get("/api/state").status_code, 200)
        self.assertIsNone(self.runner().thread)

    def test_the_market_client_never_has_a_key(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        with open(os.path.join(self.home, "kalshi.pem"), "wb") as f:
            f.write(pem)
        os.environ["KALSHI_API_KEY_ID"] = "test-key"   # removed again by tearDown
        self.assertIsNotNone(load_key(self.home)[1])
        self.assertTrue(Kalshi.from_env(self.home).can_trade)  # a key really is there...
        self.assertFalse(dash._market_client().can_trade)      # ...but the backtester's client can't trade


class SeriesSearchTests(Base):
    def setUp(self):
        super().setUp()
        self.market.series_list = [
            {"ticker": "KXHIGHNY", "title": "Highest temperature in NYC"},
            {"ticker": "KXHIGHCHI", "title": "Highest temperature in Chicago"},
            {"ticker": "HIGH", "title": "Something else"},
            {"ticker": "KXFED", "title": "Fed decision", "category": "Economics", "frequency": "monthly"},
            {"ticker": None, "title": "a broken row"}, "junk"]

    def test_finds_by_title_or_ticker(self):
        d = self.client.get("/api/series?q=temperature").get_json()
        self.assertEqual({s["ticker"] for s in d["series"]}, {"KXHIGHNY", "KXHIGHCHI"})
        d = self.client.get("/api/series?q=high").get_json()
        self.assertEqual(d["total"], 3)
        self.assertEqual(d["series"][0]["ticker"], "HIGH")  # the exact ticker first
        d = self.client.get("/api/series?q=FED").get_json()
        self.assertEqual(d["series"], [{"ticker": "KXFED", "title": "Fed decision", "category": "Economics",
                                        "frequency": "monthly"}])
        self.assertEqual(self.client.get("/api/series?q=zzz").get_json()["series"], [])

    def test_long_lists_are_cut_short(self):
        self.market.series_list = [{"ticker": f"KXRAIN{i}", "title": f"Rain {i}"} for i in range(30)]
        d = self.client.get("/api/series?q=rain").get_json()
        self.assertEqual(d["total"], 30)
        self.assertEqual(len(d["series"]), dash.SERIES_SHOWN)

    def test_the_list_is_downloaded_once_per_exchange(self):
        self.client.get("/api/series?q=high")
        self.client.get("/api/series?q=fed")
        self.assertEqual(self.market.series_calls, 1)
        config.update({"env": "demo"})
        self.client.get("/api/series?q=fed")
        self.assertEqual(self.market.series_calls, 2)

    def test_the_list_expires(self):
        clock = Clock()
        sl = dash.SeriesList(lambda: self.market, ttl=60, clock=clock)
        sl.series()
        clock.t += 59
        sl.series()
        self.assertEqual(self.market.series_calls, 1)
        clock.t += 2
        self.assertEqual(len(sl.series()), 4)  # broken rows dropped
        self.assertEqual(self.market.series_calls, 2)

    def test_bad_query(self):
        for q in ("", "a", "x" * 61):
            with self.subTest(q=q):
                self.assertEqual(self.client.get("/api/series?q=" + q).status_code, 400)
        self.assertEqual(self.client.get("/api/series").status_code, 400)

    def test_kalshi_down(self):
        self.market.series = mock.Mock(side_effect=KalshiError("GET /series -> 503", 503))
        with self.assertLogs("kal.dashboard.app", "WARNING"):
            r = self.client.get("/api/series?q=rain")
        self.assertEqual(r.status_code, 502)
        self.assertIn("Couldn't get the list", r.get_json()["error"])


class AlertTests(Base):
    def test_state_says_whether_alerts_are_set_up(self):
        self.assertFalse(self.client.get("/api/state").get_json()["alerts"]["enabled"])
        os.environ["KAL_NTFY_TOPIC"] = "kal-test-topic"   # removed again by tearDown
        self.assertTrue(self.client.get("/api/state").get_json()["alerts"]["enabled"])

    def test_not_set_up(self):
        with mock.patch.object(dash.notify, "send") as send:
            r = self.post("/api/test-alert", {})
        self.assertEqual(r.status_code, 400)
        self.assertIn("not set up", r.get_json()["error"])
        send.assert_not_called()

    def test_sends_one_test_alert(self):
        with mock.patch.object(dash.notify, "enabled", return_value=True), \
                mock.patch.object(dash.notify, "send", return_value=True) as send:
            r = self.post("/api/test-alert", {})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["sent"])
            self.assertEqual(self.post("/api/test-alert", {}).status_code, 429)  # a double tap sends one
        send.assert_called_once()
        self.assertIn("test", send.call_args[0][0])

    def test_goes_to_the_topic(self):
        os.environ["KAL_NTFY_TOPIC"] = "kal-test-topic"
        with mock.patch("kal.notify.requests.post", return_value=mock.Mock(status_code=200)) as post:
            self.assertEqual(self.post("/api/test-alert", {}).status_code, 200)
        self.assertTrue(post.call_args[0][0].endswith("/kal-test-topic"))

    def test_a_failed_send_says_so(self):
        with mock.patch.object(dash.notify, "enabled", return_value=True), \
                mock.patch.object(dash.notify, "send", return_value=False):
            r = self.post("/api/test-alert", {})
        self.assertEqual(r.status_code, 502)
        self.assertIn("did not send", r.get_json()["error"])

    def test_must_be_json(self):
        with mock.patch.object(dash.notify, "enabled", return_value=True), \
                mock.patch.object(dash.notify, "send") as send:
            r = self.client.post("/api/test-alert", data="{}", content_type="text/plain")
        self.assertEqual(r.status_code, 400)
        send.assert_not_called()


class ConfigApiTests(Base):
    def test_partial_update(self):
        r = self.post("/api/config", {"budget": "300", "strategies": {"favorites": {"mode": "off"}}})
        self.assertEqual(r.status_code, 200, r.get_json())
        cfg = config.load()
        self.assertEqual(cfg["budget"], 300)
        self.assertEqual(cfg["strategies"]["favorites"]["mode"], "off")
        self.assertEqual(cfg["strategies"]["basket_arb"]["mode"], "live")  # untouched

    def test_bad_input_is_400_with_the_reason(self):
        cases = [
            ({"budget": -1}, "negative"),
            ({"budget": "lots"}, "number"),
            ({"budget": True}, "number"),
            ({"budget": float("nan")}, "number"),
            ({"strategies": {"basket_arb": {"mode": "yolo"}}}, "mode"),
            ({"strategies": {"nope": {"mode": "paper"}}}, "Unknown strategy"),
            ({"strategies": {"bracket_arb": {"mode": "live", "verified": True}}}, "only mode"),
            ({"enabled": "yes"}, "true or false"),
            ({"env": "moon"}, "env"),
            ({"daily_loss_limit": 2}, "daily_loss_limit"),
            ({"trade_interval_min": 0}, "at least"),
            ({"wat": 1}, "Unknown setting"),
            ({"disabled_reason": ""}, "Reset"),
            ({}, "settings"),
        ]
        for body, words in cases:
            with self.subTest(body=body):
                r = self.client.post("/api/config", data=json.dumps(body, allow_nan=True),
                                     content_type="application/json")
                self.assertEqual(r.status_code, 400)
                self.assertIn(words, r.get_json()["error"])
        self.assertEqual(config.load()["budget"], 0)

    def test_post_must_be_json(self):
        # A form on another web site can send text/plain without asking. It must not work.
        r = self.client.post("/api/config", data='{"enabled": true}', content_type="text/plain")
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/reset-trip", data="x", content_type="application/x-www-form-urlencoded")
        self.assertEqual(r.status_code, 400)

    def test_other_site_is_blocked(self):
        r = self.post("/api/config", {"budget": 5}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = self.post("/api/config", {"budget": 5}, headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 200)

    def test_on_without_budget_refused(self):
        r = self.post("/api/config", {"enabled": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("how much money", r.get_json()["error"])
        self.assertFalse(config.load()["enabled"])
        self.assertEqual(self.jobs["trade"].calls, 0)

    def test_on_with_budget_starts_a_trade_run(self):
        self.post("/api/config", {"budget": 50})
        r = self.post("/api/config", {"enabled": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(config.load()["enabled"])
        self.assertEqual(self.jobs["trade"].calls, 1)
        # and it can't drop to $0 while live strategies are on
        r = self.post("/api/config", {"budget": 0})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(config.load()["budget"], 50)

    def test_on_with_only_practice_needs_no_budget(self):
        self.post("/api/config", {"strategies": {k: {"mode": "paper"} for k in config.DEFAULTS["strategies"]}})
        self.assertEqual(self.post("/api/config", {"enabled": True}).status_code, 200)
        # turning a strategy to real money with no budget is refused while on
        r = self.post("/api/config", {"strategies": {"basket_arb": {"mode": "live"}}})
        self.assertEqual(r.status_code, 400)

    def test_cant_turn_on_while_tripped(self):
        config.update({"budget": 50})
        config.trip("lost too much")
        r = self.post("/api/config", {"enabled": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Reset", r.get_json()["error"])

    def test_cant_switch_exchange_while_on(self):
        config.update({"budget": 50, "enabled": True})
        r = self.post("/api/config", {"env": "demo"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(config.load()["env"], "prod")
        config.update({"enabled": False})
        self.assertEqual(self.post("/api/config", {"env": "demo"}).status_code, 200)

    def test_off_works_even_with_a_bad_value_in_the_file(self):
        config.update({"budget": 50, "enabled": True})
        path = os.path.join(config.home(), "config.json")
        with open(path) as f:
            data = json.load(f)
        data["kelly_fraction"] = 5  # invalid hand edit
        with open(path, "w") as f:
            json.dump(data, f)
        self.post("/api/config", {"enabled": False})
        self.assertFalse(config.load()["enabled"])

    def test_env_vars_are_ignored(self):
        os.environ["KAL_BUDGET"] = "25"
        try:
            self.assertEqual(config.load()["budget"], 0.0)
        finally:
            os.environ.pop("KAL_BUDGET")

    def test_reset_trip(self):
        config.trip("3 failed runs in a row")
        self.assertEqual(self.client.get("/api/state").get_json()["config"]["disabled_reason"],
                         "3 failed runs in a row")
        r = self.post("/api/reset-trip", {})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(config.load()["disabled_reason"], "")
        self.assertFalse(config.load()["enabled"])  # reset doesn't turn it on

    def test_run_now(self):
        r = self.post("/api/run", {"kind": "research"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["started"])
        self.assertEqual(self.jobs["research"].calls, 1)
        self.assertEqual(self.post("/api/run", {"kind": "rm -rf"}).status_code, 400)


class SelftestApiTests(Base):
    def test_not_installed(self):
        with mock.patch.dict(sys.modules, {"kal.selftest": None}):
            r = self.client.get("/api/selftest")
        self.assertEqual(r.status_code, 501)

    def test_runs_without_the_order_probe(self):
        seen = {}

        def run(client=None, order_probe=False):
            seen["order_probe"] = order_probe
            return [{"check": "api", "ok": True, "detail": "fine"}, {"check": "key", "ok": False, "detail": "none"}]

        fake = types.ModuleType("kal.selftest")
        fake.run = run
        with mock.patch.dict(sys.modules, {"kal.selftest": fake}):
            r = self.client.get("/api/selftest")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(len(r.get_json()["checks"]), 2)
        self.assertIs(seen["order_probe"], False)


class AuthTests(Base):
    def make_client(self):
        os.environ["KAL_DASHBOARD_PASSWORD"] = "s3cret"
        self.app = dash.create_app(self.sched, market_client=lambda: self.market)
        return self.app.test_client()

    @staticmethod
    def basic(user, pw):
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}

    def test_every_route_needs_the_password(self):
        for path in ("/", "/api/state", "/static/app.js", "/api/selftest", "/api/trades", "/api/research",
                     "/api/backtests", "/api/series?q=rain"):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 401)
                self.assertIn("Basic", r.headers["WWW-Authenticate"])
        for path in ("/api/config", "/api/backtest", "/api/test-alert"):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, {"enabled": True, "series": "KXA"}).status_code, 401)
        self.assertIsNone(self.app.extensions["kal_backtests"].thread)
        self.assertEqual(self.client.get("/api/state", headers=self.basic("kal", "wrong")).status_code, 401)
        self.assertEqual(self.client.get("/api/state", headers=self.basic("kal", "s3cret")).status_code, 200)

    def test_password_on_a_public_host_is_ok(self):
        r = self.client.get("/api/state", headers={**self.basic("kal", "s3cret"), "Host": "kal.example.com"})
        self.assertEqual(r.status_code, 200)

    def test_guessing_gets_blocked(self):
        for _ in range(dash.MAX_FAILS):
            self.client.get("/api/state", headers=self.basic("kal", "guess"))
        r = self.client.get("/api/state", headers=self.basic("kal", "s3cret"))
        self.assertEqual(r.status_code, 429)


class BindTests(Base):
    def test_no_password_means_localhost_only(self):
        self.assertIsNone(dash.bind_problem("127.0.0.1", ""))
        self.assertIsNone(dash.bind_problem("localhost", ""))
        self.assertIsNone(dash.bind_problem("::1", ""))
        self.assertIn("KAL_DASHBOARD_PASSWORD", dash.bind_problem("0.0.0.0", ""))
        self.assertIsNone(dash.bind_problem("0.0.0.0", "pw"))

    def test_main_refuses_to_start(self):
        with mock.patch.object(dash, "make_server") as make, mock.patch.object(dash.Scheduler, "start") as start:
            with self.assertRaises(SystemExit) as cm:
                dash.main("0.0.0.0", 8787)
        self.assertIn("KAL_DASHBOARD_PASSWORD", str(cm.exception))
        make.assert_not_called()
        start.assert_not_called()

    def test_main_serves_on_localhost(self):
        with mock.patch.object(dash, "make_server") as make, mock.patch.object(dash.Scheduler, "start") as start, \
                mock.patch("builtins.print"), mock.patch("logging.basicConfig"):
            dash.main()
        self.assertEqual(make.call_args[0][:2], ("127.0.0.1", 8787))
        make.return_value.serve_forever.assert_called_once()
        start.assert_called_once()

    def test_other_host_names_are_refused_without_password(self):
        # A web page can point its own domain at 127.0.0.1 (DNS rebinding). The Host shows it.
        self.assertEqual(self.client.get("/api/state", headers={"Host": "evil.example:8787"}).status_code, 403)
        self.assertEqual(self.client.get("/api/state", headers={"Host": "127.0.0.1:8787"}).status_code, 200)
        r = self.client.get("/api/state", headers={"X-Forwarded-For": "1.2.3.4"})
        self.assertEqual(r.status_code, 403)  # behind a proxy with no password


class SchedulerTests(TempHome):
    def test_calls_run_once_on_schedule_and_rereads_config(self):
        clock = Clock()
        config.update({"trade_interval_min": 5})
        s = Scheduler(jobs={"trade": scheduler.run_trade}, clock=clock, threaded=False)
        with mock.patch("kal.engine.run_once", return_value={"ok": True}) as run_once:
            self.assertEqual(s.tick(), ["trade"])
            clock.t += 299
            self.assertEqual(s.tick(), [])
            clock.t += 1
            self.assertEqual(s.tick(), ["trade"])
            config.update({"trade_interval_min": 10})  # a dashboard change applies on the next tick
            clock.t += 300
            self.assertEqual(s.tick(), [])
            clock.t += 300
            s.tick()
        self.assertEqual(run_once.call_count, 3)
        self.assertEqual(s.state()["trade"]["next"], clock.t + 600)

    def test_locked_is_swallowed(self):
        with mock.patch("kal.engine.run_once", side_effect=risk.Locked("busy")):
            self.assertIn("skipped", scheduler.run_trade())

    def test_survives_a_job_that_raises(self):
        clock = Clock()
        boom = Recorder(exc=RuntimeError("kaboom"))
        s = Scheduler(jobs={"trade": boom}, clock=clock, threaded=False)
        with self.assertLogs("kal.scheduler", "ERROR"):
            s.tick()
        st = s.state()["trade"]
        self.assertFalse(st["ok"])
        self.assertIn("kaboom", st["text"])
        rows = db.query("SELECT * FROM runs")
        self.assertEqual(rows[0]["ok"], 0)
        clock.t += 3600
        with self.assertLogs("kal.scheduler", "ERROR"):
            s.tick()
        self.assertEqual(boom.calls, 2)

    def test_thread_keeps_going_after_crashes(self):
        clock = Clock()
        calls = []

        def boom():
            calls.append(1)
            clock.t += 10
            raise RuntimeError("kaboom")

        s = Scheduler(jobs={"trade": boom}, clock=clock, tick=0.01, intervals=lambda cfg: {"trade": 5})
        with self.assertLogs("kal.scheduler", "ERROR") as logs:
            s.start()
            try:
                deadline = time.time() + 5
                while len(calls) < 3 and time.time() < deadline:
                    time.sleep(0.01)
                self.assertGreaterEqual(len(calls), 3)
                self.assertTrue(s.state()["alive"])
            finally:
                s.stop(wait=5)
        self.assertTrue(all("kaboom" in line for line in logs.output))
        self.assertFalse(s.state()["alive"])

    def test_one_copy_at_a_time(self):
        gate, started = threading.Event(), threading.Event()

        def slow():
            started.set()
            gate.wait(5)
            return {"ok": True}

        s = Scheduler(jobs={"research": slow})
        self.assertTrue(s.run_now("research")["started"])
        started.wait(5)
        self.assertFalse(s.run_now("research")["started"])
        self.assertNotIn("research", s.due())
        gate.set()
        s._workers["research"].join(5)
        self.assertTrue(s.state()["research"]["ok"])
        self.assertFalse(s.run_now("nope")["started"])

    def test_schedule_survives_a_restart(self):
        clock = Clock()
        Scheduler(jobs={"trade": Recorder(), "research": Recorder()}, clock=clock, threaded=False).tick()
        clock.t += 60
        again = Scheduler(jobs={"trade": Recorder(), "research": Recorder()}, clock=clock, threaded=False)
        self.assertEqual(again.due(), [])  # research isn't re-run just because the server restarted

    def test_research_run_from_the_cli_counts(self):
        clock = Clock()
        db.put("last_research", {"ts": clock.t - 60, "ok": True})
        s = Scheduler(jobs={"research": Recorder()}, clock=clock, threaded=False)
        self.assertEqual(s.due(), [])

    def test_missing_modules_are_skipped_quietly(self):
        with mock.patch.dict(sys.modules, {"kal.report": None, "kal.research": None}):
            self.assertIn("skipped", scheduler.run_report())
            self.assertIn("skipped", scheduler.run_research())

    def test_research_and_report_are_called(self):
        research = types.ModuleType("kal.research")
        research.run_all = mock.Mock(return_value={"ok": True})
        report = types.ModuleType("kal.report")
        report.maybe_push = mock.Mock(return_value=None)
        with mock.patch.dict(sys.modules, {"kal.research": research, "kal.report": report}):
            s = Scheduler(jobs={"research": scheduler.run_research, "report": scheduler.run_report},
                          clock=Clock(), threaded=False)
            self.assertEqual(sorted(s.tick()), ["report", "research"])
        research.run_all.assert_called_once_with()
        report.maybe_push.assert_called_once_with()

    def test_results_in_plain_words(self):
        self.assertEqual(scheduler.describe(False), "skipped: not set up")
        self.assertEqual(scheduler.describe(None), "ok")
        self.assertEqual(scheduler.describe({"ok": False, "error": "503"}), "error: 503")
        busy = Recorder(result={"ok": False, "skipped": "research is already running"})
        s = Scheduler(jobs={"research": busy}, clock=Clock(), threaded=False)
        s.tick()
        self.assertTrue(s.state()["research"]["ok"])

    def test_intervals_are_floored(self):
        got = scheduler.intervals({"trade_interval_min": 0, "research_interval_hours": "x"})
        self.assertEqual(got["trade"], config.DEFAULTS["trade_interval_min"] * 60)
        self.assertEqual(got["research"], config.DEFAULTS["research_interval_hours"] * 3600)
        self.assertEqual(scheduler.intervals({"trade_interval_min": 0.1})["trade"], scheduler.MIN_TRADE_EVERY)


if __name__ == "__main__":
    unittest.main()
