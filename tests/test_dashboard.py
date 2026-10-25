import base64
import json
import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

from kal import config, db, risk, scheduler
from kal.dashboard import app as dash
from kal.scheduler import Scheduler
from tests.helpers import TempHome

KEY_VARS = ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PATH", "KAL_DASHBOARD_PASSWORD")


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


class Base(TempHome):
    def setUp(self):
        super().setUp()
        self._saved = {k: os.environ.pop(k, None) for k in KEY_VARS}
        self.jobs = {"trade": Recorder(), "research": Recorder(), "report": Recorder()}
        self.sched = Scheduler(jobs=self.jobs, threaded=False)
        self.client = self.make_client()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def make_client(self):
        return dash.create_app(self.sched).test_client()

    def post(self, path, body, **kw):
        return self.client.post(path, data=json.dumps(body), content_type="application/json", **kw)


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
        os.makedirs(os.path.join(self.home, "research"))
        edges = [{"id": "KXA:0.9-0.96", "series": "KXA", "lo": 0.9, "hi": 0.96, "hours_before_close": 6,
                  "roi": 0.04, "roi_lo": 0.01, "trades": 80, "events": 30, "status": "candidate"},
                 {"id": "KXB:0.8-0.9", "series": "KXB", "lo": 0.8, "hi": 0.9, "roi": "junk", "status": "live"},
                 "not an edge"]
        with open(os.path.join(self.home, "research", "edges.json"), "w") as f:
            json.dump(edges, f)
        with open(os.path.join(self.home, "research", "words.json"), "w") as f:
            json.dump({"promoted": False, "roi": 0.1, "words": {"a|b": [1, 2]}, "series": ["X"]}, f)
        with open(os.path.join(self.home, "research", "broken.json"), "w") as f:
            f.write("{nope")

    def test_shape(self):
        self.seed()
        r = self.client.get("/api/state")
        self.assertEqual(r.status_code, 200)
        s = r.get_json()
        for key in ("config", "account", "today", "positions", "money", "strategies", "research",
                    "trades", "orders", "runs", "equity", "scheduler", "has_key", "errors"):
            self.assertIn(key, s)
        self.assertEqual(s["errors"], [])
        self.assertFalse(s["has_key"])
        self.assertAlmostEqual(s["account"]["cash"], 90)
        self.assertAlmostEqual(s["account"]["value"], 99.5)
        self.assertAlmostEqual(s["today"]["pnl"], -0.5)
        self.assertAlmostEqual(s["today"]["paper_pnl"], 5.4)
        self.assertEqual(s["positions"]["paper"], 1)
        self.assertEqual(len(s["equity"]), 2)

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
        import json
        path = os.path.join(config.home(), "config.json")
        data = json.load(open(path))
        data["kelly_fraction"] = 5  # invalid hand edit
        json.dump(data, open(path, "w"))
        r = self.post("/api/config", {"enabled": False})
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
        return dash.create_app(self.sched).test_client()

    @staticmethod
    def basic(user, pw):
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}

    def test_every_route_needs_the_password(self):
        for path in ("/", "/api/state", "/static/app.js", "/api/selftest"):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 401)
                self.assertIn("Basic", r.headers["WWW-Authenticate"])
        self.assertEqual(self.post("/api/config", {"enabled": True}).status_code, 401)
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
