"""CLI, self-test, status report and deploy files: python -m unittest tests.test_ops"""
import base64
import io
import json
import os
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from email.utils import formatdate
from types import SimpleNamespace
from unittest import mock

import kal.__main__ as cli
from kal import client, config, db, ledger, report, selftest
from kal.strategies.base import Strategy
from tests.fakes import FakeKalshi
from tests.helpers import TempHome

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = time.time()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def good_markets():
    """Two markets in Kalshi's real shapes: one fixed-point, one legacy cents."""
    m1 = {"ticker": "KXA-1", "event_ticker": "KXA-25OCT", "status": "active",
          "yes_bid_dollars": "0.4500", "yes_ask_dollars": "0.4700", "no_bid_dollars": "0.5300",
          "no_ask_dollars": "0.5500", "volume_24h_fp": "5000.00", "volume_fp": "90000.00",
          "close_time": iso(NOW + 86400)}
    m2 = {"ticker": "KXA-2", "event_ticker": "KXA-25OCT", "status": "active",
          "yes_bid": 20, "yes_ask": 22, "no_bid": 78, "no_ask": 80, "volume_24h": 100, "volume": 1000,
          "close_time": iso(NOW + 86400)}
    return [m1, m2]


def candle(t, bid="0.4500", ask="0.4700"):
    return {"end_period_ts": int(t), "yes_bid": {"close_dollars": bid}, "yes_ask": {"close_dollars": ask},
            "price": {"close_dollars": "0.4600"}}


class GoodFake(FakeKalshi):
    """Everything the self-test reads, in the shapes Kalshi really sends."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.market_list = good_markets()
        self.event_list = [{"event_ticker": "KXA-25OCT", "series_ticker": "KXA", "title": "A",
                            "mutually_exclusive": True, "markets": good_markets()}]
        self.books = {"KXA-1": {"yes": [(0.45, 10.0), (0.44, 5.0)], "no": [(0.53, 7.0)]},
                      "KXA-2": {"yes": [(0.20, 3.0)], "no": [(0.78, 2.0)]}}
        self.candle_data = {"KXA-1": [candle(NOW - 7200), candle(NOW - 3600)]}
        self.series_list = [{"ticker": "KXA", "frequency": "daily"}]
        self.position_list = [{"ticker": "KXA-1", "position": 3.0, "exposure": 1.4}]


def rows_by_check(rows):
    return {r["check"]: r for r in rows}


# ---------- CLI ----------

class CliTests(TempHome):
    def cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_no_command_shows_help(self):
        code, text = self.cli()
        self.assertEqual(code, 2)
        self.assertIn("selftest", text)

    def fake_dashboard(self):
        calls = []

        def main(host=None, port=None, scheduler=True):
            calls.append((host, port))
        app = types.ModuleType("kal.dashboard.app")
        app.main = main
        pkg = types.ModuleType("kal.dashboard")
        pkg.app = app
        mods = mock.patch.dict(sys.modules, {"kal.dashboard": pkg, "kal.dashboard.app": app})
        mods.start()
        self.addCleanup(mods.stop)
        sig = mock.patch.object(cli, "_sigterm_is_ctrl_c")
        sig.start()
        self.addCleanup(sig.stop)
        self.log_setup = self.no_log_file()
        return calls

    def no_log_file(self):
        """logs.setup() would add a file handler for the rest of the test run."""
        p = mock.patch("kal.logs.setup")
        self.addCleanup(p.stop)
        return p.start()

    def test_app_runs_dashboard_on_localhost(self):
        calls = self.fake_dashboard()
        code, _ = self.cli("app", "--port", "9000")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("127.0.0.1", 9000)])
        self.log_setup.assert_called_once_with(self.home)   # logs go to $KAL_HOME/logs/kal.log

    def test_app_refuses_open_host_without_password(self):
        calls = self.fake_dashboard()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KAL_DASHBOARD_PASSWORD", None)
            code, text = self.cli("app", "--host", "0.0.0.0")
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        self.assertIn("KAL_DASHBOARD_PASSWORD", text)
        self.log_setup.assert_not_called()

    def test_app_open_host_with_password(self):
        calls = self.fake_dashboard()
        with mock.patch.dict(os.environ, {"KAL_DASHBOARD_PASSWORD": "a-long-password"}):
            code, _ = self.cli("app", "--host", "0.0.0.0")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("0.0.0.0", None)])

    def test_sigterm_acts_like_ctrl_c(self):
        """So the dashboard's shutdown code runs and a trade run can finish its basket."""
        import signal
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        cli._sigterm_is_ctrl_c()
        with self.assertRaises(KeyboardInterrupt):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    def test_daemon(self):
        log_setup = self.no_log_file()
        with mock.patch("kal.scheduler.Scheduler") as S:
            code, _ = self.cli("daemon")
        self.assertEqual(code, 0)
        S.return_value.serve_forever.assert_called_once()
        log_setup.assert_called_once_with(self.home)

    def test_run(self):
        ok = {"ok": True, "signals": 2, "live_trades": 1, "paper_trades": 1, "notes": ["hello"]}
        with mock.patch("kal.engine.run_once", return_value=ok):
            code, text = self.cli("run")
        self.assertEqual(code, 0)
        self.assertIn("Live trades: 1", text)
        self.assertIn("hello", text)
        with mock.patch("kal.engine.run_once", return_value={"ok": False, "error": "boom"}):
            code, text = self.cli("run")
        self.assertEqual(code, 1)
        self.assertIn("boom", text)
        with mock.patch("kal.engine.run_once", return_value={"ok": True, "skipped": "bot is off"}):
            code, text = self.cli("run")
        self.assertEqual(code, 0)
        self.assertIn("bot is off", text)

    def test_run_while_locked(self):
        from kal import risk
        with mock.patch("kal.engine.run_once", side_effect=risk.Locked("busy")):
            code, _ = self.cli("run")
        self.assertEqual(code, 1)

    def test_scan_args(self):
        with mock.patch.object(cli, "scan_all") as scan:
            self.assertEqual(self.cli("scan")[0], 0)
            scan.assert_called_with(only=None)
            self.cli("scan", "basket_arb", "ladder_arb")
            scan.assert_called_with(only=["basket_arb", "ladder_arb"])

    def test_research(self):
        calls = []
        fake = types.ModuleType("kal.research")
        fake.run_all = lambda quick=False: calls.append(quick) or {
            "ok": True, "tested": ["A", "B"], "new_edges": ["A:0.9-0.95"], "failed": {}, "changes": ["x went live"]}
        with mock.patch.dict(sys.modules, {"kal.research": fake}):
            code, text = self.cli("research", "--quick")
            self.cli("research")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [True, False])
        self.assertIn("Series tested: 2", text)
        fake.run_all = lambda quick=False: {"ok": False, "error": "discovery failed"}
        with mock.patch.dict(sys.modules, {"kal.research": fake}):
            code, text = self.cli("research")
        self.assertEqual(code, 1)
        self.assertIn("discovery failed", text)

    def test_selftest(self):
        good = [{"check": "markets", "ok": True, "detail": "fine"}]
        with mock.patch("kal.selftest.run", return_value=good) as run:
            code, text = self.cli("selftest")
        self.assertEqual(code, 0)
        run.assert_called_with(order_probe=False)
        self.assertIn("All checks passed", text)
        bad = good + [{"check": "order probe", "ok": False, "detail": "nope"}]
        with mock.patch("kal.selftest.run", return_value=bad) as run:
            code, text = self.cli("selftest", "--order-probe")
        self.assertEqual(code, 1)
        run.assert_called_with(order_probe=True)
        self.assertIn("FAIL  order probe: nope", text)

    def test_status_and_report(self):
        code, text = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("kal status: OFF", text)
        code, text = self.cli("report", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["config"]["enabled"], False)

    def test_report_push(self):
        with mock.patch("kal.report.maybe_push", return_value=False):
            code, text = self.cli("report", "--push")
        self.assertEqual(code, 0)
        self.assertIn("Not pushed", text)
        with mock.patch("kal.report.maybe_push", return_value=True):
            self.assertIn("Pushed to the status branch", self.cli("report", "--push")[1])
        with mock.patch("kal.report.maybe_push", side_effect=report.PushError("GitHub said 403")):
            code, text = self.cli("report", "--push")
        self.assertEqual(code, 1)
        self.assertIn("403", text)

    def test_on_and_off(self):
        code, text = self.cli("on", "--budget", "300")
        self.assertEqual(code, 0)
        cfg = config.load()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["budget"], 300)
        self.assertIn("$300.00", text)
        code, _ = self.cli("off")
        self.assertEqual(code, 0)
        self.assertFalse(config.load()["enabled"])
        self.assertEqual(config.load()["budget"], 300)  # off keeps the budget

    def test_on_without_budget_warns(self):
        code, text = self.cli("on")
        self.assertEqual(code, 0)
        self.assertIn("Budget is $0", text)

    def test_bad_budgets_are_refused(self):
        for bad in ("-5", "nan", "inf", "lots"):
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                    mock.patch("sys.stderr", io.StringIO()):
                cli.main(["on", "--budget", bad])
        self.assertFalse(config.load()["enabled"])

    def test_on_will_not_clear_a_trip_by_itself(self):
        config.trip("lost too much today")
        code, text = self.cli("on", "--budget", "50")
        self.assertEqual(code, 1)
        self.assertIn("lost too much today", text)
        self.assertFalse(config.load()["enabled"])
        code, _ = self.cli("on", "--clear-trip")
        self.assertEqual(code, 0)
        cfg = config.load()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["disabled_reason"], "")

    def test_env_vars_cant_turn_it_on(self):
        self.addCleanup(os.environ.pop, "KAL_ENABLED", None)
        os.environ["KAL_ENABLED"] = "true"
        self.assertFalse(config.load()["enabled"])

    def test_off_works_with_a_bad_value_in_the_file(self):
        self.cli("on", "--budget", "10")
        import json
        path = os.path.join(config.home(), "config.json")
        data = json.load(open(path))
        data["strategies"]["bracket_arb"]["mode"] = "Paper"  # a hand-edit typo
        json.dump(data, open(path, "w"))
        code, _ = self.cli("off")
        self.assertEqual(code, 0)
        self.assertFalse(config.load()["enabled"])

    def test_set_changes_one_setting(self):
        code, _ = self.cli("set", "max_per_trade", "5")
        self.assertEqual((code, config.load()["max_per_trade"]), (0, 5))
        code, _ = self.cli("set", "strategies.ladder_arb.mode", "live")
        self.assertEqual(config.load()["strategies"]["ladder_arb"]["mode"], "live")
        code, _ = self.cli("set", "kelly_fraction", "3")
        self.assertEqual(code, 2)  # validated
        code, _ = self.cli("set", "enabled", "true")
        self.assertEqual(code, 2)  # on/off only
        self.cli("on", "--budget", "10")
        code, _ = self.cli("set", "env", "demo")
        self.assertEqual((code, config.load()["env"]), (2, "prod"))  # not while on

    def test_on_can_pick_demo(self):
        code, text = self.cli("on", "--budget", "10", "--env", "demo")
        self.assertEqual(code, 0)
        self.assertEqual(config.load()["env"], "demo")
        self.assertIn("demo", text)

    # ---------- upkeep commands ----------

    def kalshi_date(self, behind=0):
        reply = SimpleNamespace(status_code=200, headers={"Date": formatdate(time.time() - behind, usegmt=True)})
        p = mock.patch("kal.maintenance.requests.get", return_value=reply)
        self.addCleanup(p.stop)
        return p.start()

    def disk(self, free_mb=50_000):
        """Tests shouldn't depend on how full this machine's disk is."""
        p = mock.patch("kal.maintenance.shutil.disk_usage",
                       return_value=SimpleNamespace(total=10 ** 12, used=0, free=free_mb * 1024 * 1024))
        self.addCleanup(p.stop)
        return p.start()

    def test_health(self):
        self.kalshi_date(0)
        self.disk()
        code, text = self.cli("health")
        self.assertEqual(code, 0)   # only warnings: nothing has run yet
        for bit in ("ok    disk:", "ok    clock: within 5 s", "WARN  timer:", "ok    trading:",
                    "WARN  backup: none yet", "ok    bot: off", "Last trade run: never", "Last error: none"):
            self.assertIn(bit, text)
        db.insert("runs", kind="trade", ok=0, duration=1, message=json.dumps({"ok": False, "error": "boom"}))
        config.trip("lost too much today")
        code, text = self.cli("health")
        self.assertEqual(code, 1)
        self.assertIn("FAIL  bot: stopped itself: lost too much today", text)
        self.assertIn("Last trade run: 0 s ago, FAILED: boom", text)
        self.assertIn("Last error: 0 s ago, trade: boom", text)
        self.assertEqual(config.load()["disabled_reason"], "lost too much today")   # it only looks

    def test_health_never_trips_even_when_the_clock_or_disk_is_bad(self):
        self.kalshi_date(120)
        self.disk(100)
        for _ in range(2):
            code, text = self.cli("health")
        self.assertEqual(code, 1)
        self.assertIn("FAIL  clock:", text)
        self.assertIn("s ahead of Kalshi's clock", text)
        self.assertIn("FAIL  disk: only 100 MB free", text)
        self.assertEqual(config.load()["disabled_reason"], "")

    def test_health_alive_is_local_and_needs_the_timer(self):
        from kal import maintenance
        self.disk(500)   # a disk warning alone doesn't make it unhealthy
        with mock.patch("kal.maintenance.requests.get", side_effect=AssertionError("no network here")):
            code, text = self.cli("health", "--alive")
            self.assertEqual(code, 1)
            self.assertIn("FAIL  timer", text)
            maintenance.beat()
            code, text = self.cli("health", "--alive")
        self.assertEqual(code, 0, text)

    def test_backup_and_restore(self):
        self.disk()
        db.insert("orders", strategy="basket_arb", ticker="A", side="no", count=5, price=0.5, filled=5,
                  status="executed")
        self.assertEqual(self.cli("restore")[0], 1)   # no backups yet
        code, text = self.cli("backup")
        self.assertEqual(code, 0)
        self.assertIn("Backed up kal.db", text)
        day = datetime.now(timezone.utc).date().isoformat()
        code, text = self.cli("restore")
        self.assertEqual(code, 0)
        self.assertIn(f"{day}: kal.db", text)
        db.insert("orders", strategy="basket_arb", ticker="B", side="no", count=5, price=0.5, filled=5,
                  status="executed")
        code, text = self.cli("restore", day)
        self.assertEqual(code, 2)   # asks first, changes nothing
        self.assertIn("--yes", text)
        self.assertEqual(len(db.query("SELECT * FROM orders")), 2)
        self.assertEqual(config.load()["disabled_reason"], "")
        code, text = self.cli("restore", day, "--yes")
        self.assertEqual(code, 0, text)
        self.assertEqual(len(db.query("SELECT * FROM orders")), 1)
        self.assertIn("on --clear-trip", text)
        self.assertIn("restored", config.load()["disabled_reason"])
        code, text = self.cli("restore", "2020-01-01", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Not restored", text)

    def test_backup_failure_is_plain(self):
        from kal import maintenance
        with mock.patch("kal.maintenance.backup", side_effect=maintenance.MaintenanceError("disk error")):
            code, text = self.cli("backup")
        self.assertEqual(code, 1)
        self.assertIn("Backup FAILED: disk error", text)
        self.assertIn("Nothing to back up", self.cli("backup")[1])

    def test_logs(self):
        from kal import logs
        code, text = self.cli("logs")
        self.assertEqual(code, 0)
        self.assertIn("No log yet", text)
        os.makedirs(os.path.dirname(logs.path()))
        with open(logs.path(), "w") as f:
            f.write("".join(f"line {i}\n" for i in range(80)))
        code, text = self.cli("logs")
        self.assertEqual(text.splitlines(), [f"line {i}" for i in range(30, 80)])
        self.assertEqual(self.cli("logs", "-n", "2")[1].splitlines(), ["line 78", "line 79"])
        for bad in ("0", "-3", "lots"):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
                self.cli("logs", "-n", bad)

    def test_summary(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("KAL_NTFY_TOPIC", None)
            code, text = self.cli("summary")
            self.assertEqual(code, 0)
            self.assertIn("Real money: +$0.00", text)
            code, text = self.cli("summary", "--send")
            self.assertEqual(code, 1)
            self.assertIn("KAL_NTFY_TOPIC", text)
            os.environ["KAL_NTFY_TOPIC"] = "kal-test-topic-123"
            with mock.patch("kal.notify.requests.post") as post:
                post.return_value.status_code = 200
                code, text = self.cli("summary", "--send")
        self.assertEqual(code, 0)
        self.assertIn("Sent", text)
        self.assertTrue(post.call_args.args[0].endswith("/kal-test-topic-123"))

    def test_setup_runs_the_guided_setup(self):
        with mock.patch("kal.setup.run", return_value=0) as run:
            self.assertEqual(self.cli("setup")[0], 0)
        run.assert_called_once_with()

    def test_hints_match_how_the_owner_types_commands(self):
        config.trip("lost too much today")
        with mock.patch.dict(os.environ, {"KAL_CMD": "sudo kal"}):
            self.assertIn("Then run: sudo kal on --clear-trip", self.cli("on")[1])
        self.assertIn("Then run: python -m kal on --clear-trip", self.cli("on")[1])


# ---------- scan ----------

def arb_fake():
    """One mutually exclusive event whose YES bids add up past $1: a locked-in basket."""
    c = FakeKalshi(can_trade=True)
    bids = (0.55, 0.35, 0.15, 0.07)
    markets = [{"ticker": f"EV1-{i}", "event_ticker": "EV1", "status": "active",
                "close_time": iso(NOW + 86400), "yes_bid_dollars": f"{b:.4f}"} for i, b in enumerate(bids)]
    c.event_list = [{"event_ticker": "EV1", "title": "Test event", "mutually_exclusive": True,
                     "markets": markets}]
    for m, b in zip(markets, bids):
        c.books[m["ticker"]] = {"yes": [(b, 40)], "no": []}
        c.market_info[m["ticker"]] = dict(m)
    return c


class Rogue(Strategy):
    name = "rogue"

    def __init__(self, action):
        self.action = action

    def scan(self, ctx):
        self.action(ctx)
        return []


class ScanTests(TempHome):
    def scan(self, c, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            found = cli.scan_all(client=c, **kw)
        return found, out.getvalue()

    def test_scan_finds_arb_but_never_buys_or_writes(self):
        config.update({"enabled": True, "budget": 100})
        c = arb_fake()
        found, text = self.scan(c)
        self.assertEqual(c.orders, [])
        self.assertEqual(len(found["basket_arb"]), 1)
        self.assertIn("basket_arb (set to live): 1 found", text)
        self.assertIn("No orders were placed", text)
        self.assertFalse(os.path.exists(db.path()))

    def test_scan_only(self):
        found, _ = self.scan(arb_fake(), only=["basket_arb"])
        self.assertEqual(list(found), ["basket_arb"])

    def test_scan_blocks_a_strategy_that_tries_to_buy(self):
        c = arb_fake()
        rogue = Rogue(lambda ctx: ctx.client.buy("EV1-0", "no", 1, 0.45))
        with mock.patch("kal.strategies.load_all", return_value={"rogue": rogue}):
            _, text = self.scan(c)
        self.assertEqual(c.orders, [])
        self.assertIn("rogue: scan failed", text)

    def test_scan_blocks_private_access_and_db_writes(self):
        c = arb_fake()
        sneaky = Rogue(lambda ctx: ctx.client._inner.buy("EV1-0", "no", 1, 0.45))
        writer = Rogue(lambda ctx: db.insert("kv", key="x", value="1"))
        writer.name = "writer"
        with mock.patch("kal.strategies.load_all", return_value={"rogue": sneaky, "writer": writer}):
            _, text = self.scan(c)
        self.assertEqual(c.orders, [])
        self.assertIn("writer: scan failed", text)
        self.assertFalse(os.path.exists(db.path()))
        self.assertIs(db.connect.__name__, "connect")  # put back afterwards

    def test_no_orders_wrapper(self):
        w = cli.NoOrders(arb_fake())
        self.assertFalse(w.can_trade)
        self.assertEqual(len(w.events()), 1)
        for name in ("buy", "_req", "s", "_inner", "__dict__"):
            with self.assertRaises(RuntimeError):
                getattr(w, name)
        with self.assertRaises(RuntimeError):
            w.can_trade = True


# ---------- self-test ----------

class SelftestTests(unittest.TestCase):
    def test_good_shapes_pass(self):
        rows = selftest.run(GoodFake())
        bad = [r for r in rows if not r["ok"]]
        self.assertEqual(bad, [])
        self.assertEqual({r["check"] for r in rows},
                         {"markets", "market fields", "events", "order book", "candlesticks", "series",
                          "clock", "balance", "positions"})
        for r in rows:
            self.assertEqual(set(r), {"check", "ok", "detail"})
        self.assertIn("skipped", rows_by_check(rows)["clock"]["detail"])   # a fake has no clock to ask

    def test_no_key_skips_account(self):
        c = GoodFake(can_trade=False)
        c.balance = mock.Mock(side_effect=AssertionError("should not be called"))
        checks = rows_by_check(selftest.run(c))
        self.assertTrue(checks["account"]["ok"])
        self.assertNotIn("balance", checks)

    def assertFails(self, c, check):
        rows = rows_by_check(selftest.run(c))
        self.assertFalse(rows[check]["ok"], rows[check])
        return rows[check]

    def test_missing_price_fields_fail(self):
        c = GoodFake()
        for m in c.market_list:
            for k in list(m):
                if k.startswith(("yes_", "no_")):
                    del m[k]
        self.assertFails(c, "markets")

    def test_prices_over_a_dollar_fail(self):
        c = GoodFake()
        c.market_list[0]["yes_ask_dollars"] = "47"   # cents in a dollars field
        self.assertFails(c, "markets")
        c = GoodFake()
        c.market_list[0]["yes_bid_dollars"] = "zero"
        self.assertFails(c, "markets")

    def test_no_markets_fail(self):
        c = GoodFake()
        c.market_list = []
        self.assertFails(c, "markets")
        self.assertFails(c, "market fields")

    def test_events_without_mutually_exclusive_fail(self):
        c = GoodFake()
        del c.event_list[0]["mutually_exclusive"]
        self.assertIn("mutually_exclusive", self.assertFails(c, "events")["detail"])

    def test_empty_or_bad_order_books_fail(self):
        c = GoodFake()
        c.books = {}
        self.assertFails(c, "order book")
        c = GoodFake()
        c.books["KXA-1"] = {"yes": [(0.60, 5.0)], "no": [(0.55, 5.0)]}   # bids add past $1
        self.assertIn("misread", self.assertFails(c, "order book")["detail"])
        c = GoodFake()
        c.books["KXA-1"] = {"yes": [(45, 5.0)], "no": []}                 # cents, not dollars
        self.assertFails(c, "order book")

    def test_bad_candles_fail(self):
        c = GoodFake()
        c.candles = lambda *a, **k: [{"yes_bid": {"close_dollars": "0.45"}}]
        self.assertFails(c, "candlesticks")
        c = GoodFake()
        c.candle_data = {"KXA-1": [candle(NOW - 60, bid="0", ask="0")]}
        self.assertFails(c, "candlesticks")
        c = GoodFake()
        c.candle_data = {}
        self.assertFails(c, "candlesticks")

    def test_candles_move_past_a_wrong_series(self):
        c = GoodFake()
        c.candle_data = {"KXA-2": [candle(NOW - 3600)]}
        real = c.candles

        def candles(series, ticker, *a, **k):
            if ticker == "KXA-1":
                raise client.KalshiError("GET ... -> 404: not found")
            return real(series, ticker, *a, **k)
        c.candles = candles
        self.assertTrue(rows_by_check(selftest.run(c))["candlesticks"]["ok"])

    def test_series_and_account_failures(self):
        c = GoodFake()
        c.series_list = []
        self.assertFails(c, "series")
        c = GoodFake()
        c._balance = None
        self.assertFails(c, "balance")
        c = GoodFake()
        c.position_list = [{"position": 1}]
        self.assertFails(c, "positions")

    def test_never_raises(self):
        class Broken:
            def __getattr__(self, name):
                raise client.KalshiError(f"{name} -> 500")
        rows = selftest.run(Broken(), order_probe=True)
        self.assertTrue(rows)
        self.assertTrue(all(not r["ok"] for r in rows if r["check"] != "account"))

        class Weird(FakeKalshi):
            def markets(self, limit=None, **p):
                return "not a list"

            def events(self, **p):
                return [None, 5, "x"]

            def series(self, category=None):
                return {"oops": 1}
        rows = selftest.run(Weird(), order_probe=True)
        self.assertTrue(rows)
        self.assertFalse(rows_by_check(rows)["markets"]["ok"])


class SelftestClockTests(TempHome):
    def check(self, behind):
        date = SimpleNamespace(status_code=200, headers={"Date": formatdate(time.time() - behind, usegmt=True)})
        with mock.patch("kal.maintenance.requests.get", return_value=date):
            return selftest._Checks(client.Kalshi()).clock_check()

    def test_a_bad_clock_fails_but_never_trips(self):
        """Kalshi refuses signed requests with a wrong timestamp, so orders would fail."""
        self.assertEqual(self.check(0)[0], True)
        for behind in (10, 60, -60, 60):
            ok, detail = self.check(behind)
            self.assertFalse(ok)
            self.assertIn("Kalshi's clock", detail)
        self.assertEqual(config.load()["disabled_reason"], "")


class OrderProbeTests(unittest.TestCase):
    def test_not_sent_unless_asked(self):
        c = GoodFake()
        selftest.run(c)
        self.assertEqual(c.orders, [])

    def test_one_cent_order_that_does_not_fill_passes(self):
        c = GoodFake()
        c.fill = lambda t, side, n, p: 0
        probe = rows_by_check(selftest.run(c, order_probe=True))["order probe"]
        self.assertTrue(probe["ok"], probe)
        self.assertEqual(c.orders, [("KXA-1", "yes", 1, 0.01)])

    def test_a_fill_fails_the_probe(self):
        c = GoodFake()  # the fake fills everything by default
        probe = rows_by_check(selftest.run(c, order_probe=True))["order probe"]
        self.assertFalse(probe["ok"])
        self.assertIn("FILLED", probe["detail"])
        self.assertEqual(len(c.orders), 1)  # never a second order

    def test_skips_markets_where_a_cent_could_fill(self):
        c = GoodFake()
        c.fill = lambda t, side, n, p: 0
        c.books = {"KXA-1": {"yes": [(0.01, 5.0)], "no": [(0.95, 5.0)]},   # YES ask $0.05
                   "KXA-2": {"yes": [(0.20, 5.0)], "no": []}}              # no sellers at all
        probe = rows_by_check(selftest.run(c, order_probe=True))["order probe"]
        self.assertFalse(probe["ok"])
        self.assertEqual(c.orders, [])

    def test_needs_a_key(self):
        c = GoodFake(can_trade=False)
        probe = rows_by_check(selftest.run(c, order_probe=True))["order probe"]
        self.assertFalse(probe["ok"])
        self.assertEqual(c.orders, [])

    def test_resting_or_unclear_reply_fails(self):
        c = GoodFake()
        c.buy = lambda *a, **k: c.orders.append(a) or {"order_id": "o1", "status": "resting"}
        self.assertFalse(rows_by_check(selftest.run(c, order_probe=True))["order probe"]["ok"])
        c = GoodFake()
        c.buy = lambda *a, **k: c.orders.append(a) or {"order_id": "o1"}
        self.assertFalse(rows_by_check(selftest.run(c, order_probe=True))["order probe"]["ok"])

    def test_real_client_sends_ioc_at_one_cent(self):
        """The real Kalshi class, with HTTP stubbed: proves the probe's request body."""
        m = good_markets()[0]
        pages = {
            "/markets": {"markets": [m], "cursor": ""},
            "/events": {"events": [{"event_ticker": "KXA-25OCT", "series_ticker": "KXA", "title": "A",
                                    "mutually_exclusive": False, "markets": [m]}], "cursor": ""},
            "/markets/KXA-1/orderbook": {"orderbook_fp": {"yes_dollars": [["0.4500", "10.00"]],
                                                          "no_dollars": [["0.5300", "7.00"]]}},
            "/series/KXA/markets/KXA-1/candlesticks": {"candlesticks": [candle(NOW - 3600)]},
            "/series": {"series": [{"ticker": "KXA"}], "cursor": ""},
            "/portfolio/balance": {"balance_dollars": "12.3400"},
            "/portfolio/positions": {"market_positions": [{"ticker": "KXA-1", "position_fp": "3.00",
                                                           "market_exposure_dollars": "1.4000"}]},
        }
        posts = []

        def req(method, endpoint, params=None, body=None):
            if method == "POST":
                posts.append((endpoint, body))
                data, code = {"order": {"order_id": "o1", "status": "canceled", "fill_count_fp": "0.00"}}, 201
            else:
                data, code = pages[endpoint], 200
            return SimpleNamespace(status_code=code, json=lambda: data, text=json.dumps(data))
        k = client.Kalshi(key_id="kid", private_key=object())
        k._req = req
        date = SimpleNamespace(status_code=200, headers={"Date": formatdate(time.time(), usegmt=True)})
        with mock.patch("kal.maintenance.requests.get", return_value=date) as get:
            rows = selftest.run(k, order_probe=True)
        self.assertEqual([r for r in rows if not r["ok"]], [])
        self.assertEqual(get.call_args.args[0], k.base + "/trade-api/v2/markets")   # the clock check
        self.assertEqual(len(posts), 1)
        endpoint, body = posts[0]
        # Kalshi's V2 order endpoint (V1 now answers 410): buying YES is a bid on the YES book.
        self.assertEqual(endpoint, "/portfolio/events/orders")
        self.assertEqual(body["time_in_force"], "immediate_or_cancel")
        self.assertEqual((body["side"], body["ticker"]), ("bid", "KXA-1"))
        self.assertEqual(body["price"], "0.0100")
        self.assertEqual(body["count"], "1.00")


# ---------- report ----------

PEM_TEXT = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqSECRETPEMBODY\n-----END PRIVATE KEY-----\n"
SECRETS = {"KALSHI_API_KEY_ID": "kid-SECRET-1234567", "KALSHI_PRIVATE_KEY": PEM_TEXT,
           "KAL_GITHUB_TOKEN": "github_pat_SECRET_7654321", "KAL_DASHBOARD_PASSWORD": "pw-SECRET-hunter2",
           "KAL_NTFY_TOPIC": "kal-SECRET-topic-42", "KAL_GITHUB_REPO": "owner/kal"}


class FakeGitHub:
    """Stands in for requests.request against the GitHub API."""

    def __init__(self, branch=True, put_code=201, private=True):
        self.branch, self.put_code, self.private = branch, put_code, private
        self.calls = []

    def __call__(self, method, url, headers=None, timeout=None, json=None, params=None):
        path = url.split("/repos/owner/kal", 1)[1]
        self.calls.append((method, path, json, headers))
        if method == "GET" and path == "":
            return self.resp(200, {"full_name": "owner/kal", "private": self.private})
        if method == "GET" and path == "/git/ref/heads/status":
            return self.resp(200 if self.branch else 404, {"object": {"sha": "c0"}})
        if method == "GET" and path.startswith("/contents/"):
            return self.resp(200, {"sha": "old-" + path})
        if method == "PUT":
            return self.resp(self.put_code, {"content": {}})
        if method == "POST":
            return self.resp(201, {"sha": {"/git/trees": "t1", "/git/commits": "c1"}.get(path, "r1")})
        return self.resp(404, {})

    @staticmethod
    def resp(code, data):
        return SimpleNamespace(status_code=code, json=lambda: data, text=json.dumps(data))


class ReportTests(TempHome):
    def setUp(self):
        # Before TempHome: cleanups run after tearDown, so a snapshot taken later would put
        # back the temp KAL_HOME after it's deleted.
        env = mock.patch.dict(os.environ, SECRETS)
        env.start()
        self.addCleanup(env.stop)
        alerts = mock.patch("kal.notify.requests.post")   # the topic is set: never send a real alert
        self.alerts = alerts.start()
        self.addCleanup(alerts.stop)
        super().setUp()
        with open(os.path.join(self.home, "kalshi.pem"), "w") as f:
            f.write(PEM_TEXT)
        config.update({"budget": 300})
        db.insert("runs", kind="trade", ok=0, duration=1.0,
                  message=json.dumps({"ok": False, "error": f"auth failed for {SECRETS['KALSHI_API_KEY_ID']} "
                                                            f"{PEM_TEXT} {SECRETS['KAL_GITHUB_TOKEN']} "
                                                            f"https://ntfy.sh/{SECRETS['KAL_NTFY_TOPIC']}"}))
        db.insert("runs", kind="trade", ok=1, duration=2.0,
                  message=json.dumps({"ok": True, "signals": 3, "live_trades": 1, "paper_trades": 2, "notes": []}))
        db.insert("equity", balance=310.0, exposure=20.0, equity=305.0, paper_equity=1001.0)
        db.insert("paper", strategy="favorites", event="E", ticker="P1", side="yes", count=10, price=0.9,
                  cost=9.1, settled=1, result="yes", pnl=0.9, settled_ts=time.time())
        db.insert("orders", strategy="basket_arb", ticker="L1", side="no", count=5, price=0.5, filled=5,
                  status="executed")
        db.insert("settlements", ticker="L1", settled_ts=time.time(), result="no", revenue=5, cost=2.6, pnl=2.4)

    def everything(self):
        rep = report.build()
        return rep, json.dumps(rep) + report.markdown(rep) + report.markdown(rep, short=True)

    def test_no_secrets_anywhere(self):
        rep, text = self.everything()
        for name, value in SECRETS.items():
            if name != "KAL_GITHUB_REPO":
                self.assertNotIn(value.strip(), text, name)
        for bit in ("SECRET", "BEGIN PRIVATE", "PRIVATE KEY", "kalshi.pem"):
            self.assertNotIn(bit, text)
        self.assertTrue(rep["config"]["has_api_key"])

    def test_contents(self):
        config.trip("lost $40 today")
        rep, text = self.everything()
        self.assertEqual(rep["tripped"], "lost $40 today")
        self.assertIn("STOPPED", text)
        self.assertEqual(rep["config"]["budget"], 300)
        self.assertEqual(rep["config"]["strategies"]["basket_arb"], "live")
        self.assertEqual(rep["equity"]["equity"], 305.0)
        self.assertAlmostEqual(rep["today"]["paper_settled_pnl"], 0.9)
        self.assertAlmostEqual(rep["today"]["live_settled_pnl"], 2.4)
        self.assertEqual(rep["strategies"]["favorites"]["paper"]["trades"], 1)
        self.assertEqual(rep["strategies"]["basket_arb"]["live"]["wins"], 1)
        self.assertEqual(len(rep["runs"]), 2)
        self.assertIn("auth failed", rep["runs"][1]["error"])
        self.assertEqual(rep["runs"][0]["live_trades"], 1)

    def test_runs_capped_at_20(self):
        for _ in range(30):
            db.insert("runs", kind="trade", ok=1, duration=0, message="{}")
        self.assertEqual(len(report.build()["runs"]), 20)

    def test_research_summary(self):
        d = os.path.join(self.home, "research")
        os.makedirs(d)
        with open(os.path.join(d, "edges.json"), "w") as f:
            json.dump([{"id": "KXA:0.9-0.95", "status": "live", "roi_lo": 0.02, "trades": 80},
                       {"id": "KXB:0.8-0.85", "status": "candidate", "roi_lo": 0.01, "trades": 50},
                       {"id": "KXC:0.5-0.6", "status": "retired"}], f)
        with open(os.path.join(d, "words.json"), "w") as f:
            f.write("{not json")
        r = report.build()["research"]
        self.assertEqual(r["edges"], {"live": 1, "candidate": 1, "retired": 1})
        self.assertEqual(r["top_edges"][0]["id"], "KXA:0.9-0.95")
        self.assertIsNone(r["mentions"])

    def test_push_is_a_no_op_without_env(self):
        for var in ("KAL_GITHUB_TOKEN", "KAL_GITHUB_REPO"):
            with mock.patch.dict(os.environ, {}):
                os.environ.pop(var)
                with mock.patch("kal.report.requests.request", side_effect=AssertionError("no network")):
                    self.assertFalse(report.maybe_push())

    def test_push_updates_the_status_branch(self):
        gh = FakeGitHub(branch=True)
        with mock.patch("kal.report.requests.request", gh):
            self.assertTrue(report.maybe_push())
        puts = [c for c in gh.calls if c[0] == "PUT"]
        self.assertEqual(sorted(p[1] for p in puts), ["/contents/report.json", "/contents/report.md"])
        for _, path, body, headers in puts:
            self.assertEqual(body["branch"], "status")
            self.assertEqual(body["sha"], "old-" + path)
            text = base64.b64decode(body["content"]).decode()
            self.assertNotIn("SECRET", text)
            self.assertNotIn("PRIVATE KEY", text)
            self.assertEqual(headers["Authorization"], f"Bearer {SECRETS['KAL_GITHUB_TOKEN']}")
        json.loads(base64.b64decode(next(p[2] for p in puts if p[1].endswith(".json"))["content"]))

    def test_push_creates_the_branch_without_code(self):
        gh = FakeGitHub(branch=False)
        with mock.patch("kal.report.requests.request", gh):
            self.assertTrue(report.maybe_push())
        posts = {c[1]: c[2] for c in gh.calls if c[0] == "POST"}
        self.assertEqual(sorted(e["path"] for e in posts["/git/trees"]["tree"]), ["report.json", "report.md"])
        self.assertEqual(posts["/git/commits"]["parents"], [])
        self.assertEqual(posts["/git/refs"], {"ref": "refs/heads/status", "sha": "c1"})
        self.assertNotIn("SECRET", json.dumps(posts))
        self.assertFalse([c for c in gh.calls if c[0] == "PUT"])

    def test_push_failure_raises_without_the_token(self):
        gh = FakeGitHub(put_code=403)
        with mock.patch("kal.report.requests.request", gh):
            with self.assertRaises(report.PushError) as e:
                report.maybe_push()
        self.assertIn("403", str(e.exception))
        self.assertNotIn(SECRETS["KAL_GITHUB_TOKEN"], str(e.exception))

    def test_split_payouts_count_in_the_report(self):
        from tests.fakes import FakeKalshi
        db.insert("orders", ts=time.time(), strategy="favorites", ticker="S", side="no", count=10, price=0.60,
                  filled=10, status="executed", client_order_id="c-S")
        c = FakeKalshi()
        c.market_info = {"S": {"status": "finalized", "result": "scalar", "settlement_value_dollars": "0.3300"}}
        ledger.settle(c)
        live = report._live_stats()["favorites"]
        self.assertEqual((live["settled"], live["wins"]), (1, 0))
        self.assertAlmostEqual(live["pnl"], round(10 * 0.67 - 6 - client.fee(10, 0.60), 2))  # 0.53

    def test_public_repo_is_refused(self):
        gh = FakeGitHub(private=False)
        with mock.patch("kal.report.requests.request", gh):
            with self.assertRaises(report.PushError) as e:
                report.maybe_push()
        self.assertIn("public", str(e.exception))
        self.assertEqual([c[:2] for c in gh.calls], [("GET", "")])  # nothing written

    def test_bad_repo_name_is_refused(self):
        with mock.patch.dict(os.environ, {"KAL_GITHUB_REPO": "https://evil.example/x"}), \
                mock.patch("kal.report.requests.request", side_effect=AssertionError("no network")):
            with self.assertRaises(report.PushError):
                report.maybe_push()

    def test_server_upkeep_is_in_the_report(self):
        from kal import maintenance
        rep, text = self.everything()
        self.assertEqual(rep["ops"]["last_backup"], None)
        db.put("health", {"ts": time.time(), "checks": [
            {"check": "disk", "ok": False, "level": "warn", "detail": "600 MB free.", "free_mb": 600},
            {"check": "clock", "ok": True, "level": "ok", "detail": "within 5 s of Kalshi's"},
            {"check": "clock", "ok": False, "level": "warn", "detail": "couldn't reach Kalshi", "unknown": True}]})
        with mock.patch("kal.maintenance.shutil.disk_usage",
                        return_value=SimpleNamespace(total=10 ** 12, used=0, free=10 ** 11)):
            maintenance.backup()
        maintenance.beat()
        rep, text = self.everything()
        self.assertEqual(rep["ops"]["disk_free_mb"], 600)
        self.assertEqual(rep["ops"]["last_backup"], datetime.now(timezone.utc).date().isoformat())
        self.assertLess(rep["ops"]["timer_seconds_ago"], 60)
        self.assertIn("Server disk: 600 MB free.", rep["health"])
        self.assertNotIn("couldn't reach", " ".join(rep["health"]))   # can't tell is not a problem
        self.assertIn("- Server: 600 MB disk free", text)
        db.put("health", {"ts": time.time() - 2 * 3600, "checks": [
            {"check": "disk", "ok": False, "level": "warn", "detail": "600 MB free.", "free_mb": 600}]})
        self.assertNotIn("Server disk: 600 MB free.", report.build()["health"])   # too old to warn from

    def test_key_and_topic_saved_by_setup(self):
        """`kal setup` saves the key ID and alert topic as files: the report must see the key
        and never show either."""
        os.environ.pop("KALSHI_API_KEY_ID")
        os.environ.pop("KAL_NTFY_TOPIC")
        self.assertFalse(report.build()["config"]["has_api_key"])
        for name, value in (("kalshi_key_id", "kid-file-SECRET-77"), ("ntfy_topic", "kal-file-SECRET-88")):
            with open(os.path.join(self.home, name), "w") as f:
                f.write(value + "\n")
        db.insert("runs", kind="trade", ok=0, duration=0,
                  message=json.dumps({"ok": False, "error": "kid-file-SECRET-77 posting to kal-file-SECRET-88"}))
        rep, text = self.everything()
        self.assertTrue(rep["config"]["has_api_key"])
        self.assertNotIn("kid-file-SECRET-77", text)
        self.assertNotIn("kal-file-SECRET-88", text)

    def test_an_old_backup_needs_a_look(self):
        os.makedirs(os.path.join(self.home, "backups", "2020-01-01"))
        with open(os.path.join(self.home, "backups", "2020-01-01", "kal.db"), "w"):
            pass
        self.assertIn("No backup since 2020-01-01.", report.build()["health"])


# ---------- deploy and CI files ----------

def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


class DeployFileTests(unittest.TestCase):
    def test_ci(self):
        ci = read(".github", "workflows", "ci.yml")
        self.assertIn("python-version: \"3.12\"", ci)
        self.assertIn("python -m unittest discover -s tests -t . -v", ci)
        self.assertIn("actions/checkout@v4", ci)

    def test_market_check_is_read_only(self):
        mc = read(".github", "workflows", "market-check.yml")
        for bit in ("workflow_dispatch", "schedule", "python -m kal selftest", "python -m kal scan",
                    "python -m kal research --quick", "actions/upload-artifact@v4", "contents: read"):
            self.assertIn(bit, mc)
        self.assertNotIn("secrets.", mc)
        self.assertNotIn("--order-probe", mc)

    def test_docker(self):
        df = read("deploy", "Dockerfile")
        self.assertIn("FROM python:3.12-slim", df)
        self.assertIn("USER kal", df)
        self.assertIn("KAL_HOME=/data", df)
        compose = read("deploy", "docker-compose.yml")
        self.assertIn("127.0.0.1:8787:8787", compose)
        self.assertIn("unless-stopped", compose)
        # Health checks are local (no network) and use the CLI command, which really exists.
        for text in (df, compose):
            self.assertIn('"python", "-m", "kal", "health", "--alive"', text)
        self.assertIn("mem_limit: 400m", compose)
        self.assertTrue(cli.parser().parse_args(["health", "--alive"]).alive)

    def test_systemd(self):
        unit = read("deploy", "kal.service")
        lines = set(unit.splitlines())
        self.assertIn("-m kal app", unit)
        self.assertNotIn("0.0.0.0", unit)
        for line in ("User=kal", "Restart=always", "NoNewPrivileges=true", "ProtectSystem=strict",
                     "ReadWritePaths=/var/lib/kal", "PrivateTmp=true", "MemoryMax=400M", "StartLimitIntervalSec=0"):
            self.assertIn(line, lines)
        restart = [int(x.split("=")[1]) for x in lines if x.startswith("RestartSec=")]
        self.assertTrue(restart and restart[0] <= 10)
        # The only writable place is the data folder, where logs and backups go too.
        self.assertEqual([x for x in lines if x.startswith("ReadWritePaths=")], ["ReadWritePaths=/var/lib/kal"])

    def test_install_keeps_the_clock_right_and_sets_up_alerts(self):
        sh = read("deploy", "install.sh")
        for bit in ("apt-get install -y -qq chrony", "systemd-timesyncd", "timedatectl", "KAL_NTFY_TOPIC=",
                    "SystemMaxUse=", 'KAL_CMD="sudo kal"', "sudo kal health", "sudo kal summary --send",
                    '"$DATA/ntfy_topic"', "umask 077"):
            self.assertIn(bit, sh)
        # The topic goes in the file `kal setup` and notify read, never as an active kal.env line:
        # a topic there would win, and the owner would subscribe to the wrong one.
        self.assertNotIn("\nKAL_NTFY_TOPIC=", sh)
        self.assertTrue(os.access(os.path.join(ROOT, "deploy", "install.sh"), os.X_OK))

    def test_docs_cover_the_basics(self):
        docs = read("docs", "DEPLOY.md")
        for bit in ("ntfy", "KAL_NTFY_TOPIC", "kal set env demo", "kal set env prod", "kal on --budget 300",
                    "kal backup", "kal restore", "kal health", "kal logs", "Always under $20", "US data center",
                    "kal stopped itself", "kal warning: disk", "kal warning: clock", "kal warning: trading",
                    "kal backup failed", "kal on --clear-trip"):
            self.assertIn(bit, docs)
        readme = read("README.md")
        for bit in ("ntfy", "KAL_NTFY_TOPIC", "kal health", "kal logs", "kal backup", "kal restore"):
            self.assertIn(bit, readme)
        # Every command the docs tell the owner to run must exist.
        known = set(cli.parser()._subparsers._group_actions[0].choices)
        import re
        for cmd in set(re.findall(r"sudo kal ([a-z-]+)", docs + readme)):
            self.assertIn(cmd, known, cmd)


if __name__ == "__main__":
    unittest.main()
