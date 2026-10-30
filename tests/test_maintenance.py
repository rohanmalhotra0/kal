"""Months alone on a server: backups, prune, disk and clock checks, logs, the daily
summary, and the scheduler's new jobs. python -m unittest tests.test_maintenance"""
import io
import itertools
import json
import logging
import logging.handlers
import os
import sqlite3
import threading
import time
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from email.utils import formatdate
from types import SimpleNamespace
from unittest import mock

import requests

import kal.__main__ as cli
from kal import config, db, ledger, logs, maintenance, paper, report, risk, scheduler
from kal.client import fee
from kal.scheduler import Scheduler
from tests.helpers import TempHome

DAY = 86400
OLD = time.time() - 200 * DAY   # past the 180-day prune line


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def kalshi_reply(behind=0.0):
    """A reply whose Date header is `behind` seconds behind our clock (negative: ahead)."""
    return SimpleNamespace(status_code=200, headers={"Date": formatdate(time.time() - behind, usegmt=True)})


def free_disk(mb):
    return mock.patch("kal.maintenance.shutil.disk_usage",
                      return_value=SimpleNamespace(total=10 ** 12, used=0, free=int(mb * maintenance.MB)))


def kalshi_clock(behind=0.0):
    return mock.patch("kal.maintenance.requests.get", return_value=kalshi_reply(behind))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Recorder:
    def __init__(self, exc=None):
        self.calls, self.exc = 0, exc

    def __call__(self):
        self.calls += 1
        if self.exc:
            raise self.exc
        return {"ok": True}


class Base(TempHome):
    def setUp(self):
        # Snapshot the environment before TempHome sets KAL_HOME: cleanups run after
        # tearDown, so a later snapshot would point KAL_HOME back at a deleted folder.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in ("KAL_NTFY_TOPIC", "KALSHI_API_BASE"):
            os.environ.pop(var, None)
        post = mock.patch("kal.notify.requests.post")   # never a real phone alert from a test
        post.start().return_value.status_code = 200
        self.addCleanup(post.stop)
        super().setUp()
        maintenance._clock_bad["ts"] = None

    def fill(self, ticker="A", n=5, p=0.5, **kw):
        return db.insert("orders", strategy="basket_arb", ticker=ticker, side="no", count=n, price=p,
                         filled=n, status="executed", **kw)


# ---------- backup / restore ----------

class BackupTests(Base):
    def seed(self):
        self.fill("A")
        db.insert("settlements", ticker="A", settled_ts=time.time(), result="no", revenue=5, cost=2.6, pnl=2.4)

    def bought_after_a_backup(self):
        self.fill("A")
        day = maintenance.backup()["day"]
        self.fill("B")   # bought after the backup: only the catch-up can bring it back
        return day

    def test_copies_only_between_trade_runs(self):
        # A copy taken between a basket's legs, put back later, read as a basket left
        # half-built: a false stop (cross-PR money-path audit).
        self.fill("A")
        with risk.run_lock():
            with self.assertRaises(risk.Locked):
                maintenance.backup(wait=0)
        self.assertEqual(maintenance.backups(), [])   # nothing copied under a run
        held, release = threading.Event(), threading.Event()

        def trade_run():
            with risk.run_lock():
                held.set()
                release.wait(5)
        t = threading.Thread(target=trade_run)
        t.start()
        held.wait(5)
        threading.Timer(0.3, release.set).start()
        t0 = time.time()
        self.assertEqual(maintenance.backup(wait=10)["files"], ["kal.db"])
        self.assertGreater(time.time() - t0, 0.25)   # it waited for the run to finish
        t.join(5)

    def tickers(self):
        with closing(sqlite3.connect(db.path())) as c:
            return sorted(r[0] for r in c.execute("SELECT ticker FROM orders"))

    def trouble_after_the_swap(self, trouble):
        """_problem(), but once the live books are the backup's, `trouble` is raised (or
        reported, if it's text): the check after the swap goes wrong."""
        real = maintenance._problem

        def check(path):
            if path == db.path() and self.tickers() == ["A"]:
                if isinstance(trouble, BaseException):
                    raise trouble
                return trouble
            return real(path)
        return mock.patch.object(maintenance, "_problem", side_effect=check)

    def test_copies_every_db_and_keeps_the_newest_14_days(self):
        self.seed()
        config.update({"env": "demo"})
        self.seed()
        config.update({"env": "prod"})
        start = utc(2026, 10, 1, 9)
        for i in range(20):
            b = maintenance.backup(now=start + i * DAY)
        days = sorted(os.listdir(maintenance.backups_dir()))
        self.assertEqual(len(days), 14)
        self.assertEqual((days[0], days[-1]), ("2026-10-07", "2026-10-20"))
        self.assertEqual(b["files"], ["kal-demo.db", "kal.db"])
        self.assertEqual(b["days"], days)
        for d in days:
            self.assertEqual(sorted(os.listdir(os.path.join(maintenance.backups_dir(), d))),
                             ["kal-demo.db", "kal.db"])

    def test_backup_is_a_usable_db_that_restores(self):
        self.seed()
        before = (ledger.realized(), ledger.open_cost(), ledger.capital(config.load()))
        b = maintenance.backup()
        copy = os.path.join(b["dir"], "kal.db")
        self.assertFalse(os.path.exists(copy + "-wal"))  # one self-contained file
        with closing(sqlite3.connect(copy)) as c:
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
        self.fill("B")   # a trade after the backup
        maintenance.beat()   # healthy books restore through SQLite, safe with the service running
        r = maintenance.restore(b["day"])
        self.assertEqual([x["ticker"] for x in db.query("SELECT ticker FROM orders")], ["A"])
        self.assertEqual((ledger.realized(), ledger.open_cost(), ledger.capital(config.load())), before)
        db.insert("runs", kind="trade", ok=1, duration=0, message="{}")   # the restored db still writes
        cfg = config.load()
        self.assertFalse(cfg["enabled"])
        self.assertIn("restored from the", cfg["disabled_reason"])
        with closing(sqlite3.connect(os.path.join(r["saved"], "kal.db"))) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 2)  # kept the newer books

    def test_restore_keeps_an_earlier_stop_reason(self):
        self.seed()
        b = maintenance.backup()
        config.trip("basket left half-built")
        maintenance.restore(b["day"])
        self.assertIn("basket left half-built", config.load()["disabled_reason"])

    def test_restore_over_a_broken_db(self):
        self.seed()
        b = maintenance.backup()
        with open(db.path(), "wb") as f:
            f.write(b"not a database " * 100)
        maintenance.beat()   # the service is running: swapping files under it isn't safe
        with self.assertRaises(maintenance.MaintenanceError) as e:
            maintenance.restore(b["day"])
        self.assertIn("systemctl stop kal", str(e.exception))
        self.assertEqual(config.load()["disabled_reason"], "")
        maintenance.unbeat()   # what a clean stop does
        r = maintenance.restore(b["day"])
        self.assertEqual(len(db.query("SELECT * FROM orders")), 1)
        with open(r["saved"], "rb") as f:
            self.assertTrue(f.read().startswith(b"not a database"))   # set aside, not deleted

    def test_restore_refuses_bad_input_without_stopping_anything(self):
        self.seed()
        b = maintenance.backup()
        for day in ("yesterday", "2026-1-1", None, "1999-01-01"):
            with self.assertRaises(maintenance.MaintenanceError):
                maintenance.restore(day)
        open(os.path.join(b["dir"], "kal.db"), "w").close()   # an empty file passes SQLite's own check
        with self.assertRaises(maintenance.MaintenanceError) as e:
            maintenance.restore(b["day"])
        self.assertIn("damaged", str(e.exception))
        self.assertEqual(len(db.query("SELECT * FROM orders")), 1)
        self.assertEqual(config.load()["disabled_reason"], "")

    def test_a_restore_that_fails_before_the_swap_changes_nothing(self):
        # Audit round 3: the books were swapped first and the catch-up marker written after. When
        # that write failed it said "Not restored", but B had dropped out of the books for good.
        # Now the marker goes into a copy of the backup first, and only that copy is swapped in.
        day = self.bought_after_a_backup()
        for trouble, says in ((sqlite3.OperationalError("database is locked"), "books were not changed"),
                              (KeyboardInterrupt(), "Run the restore again")):
            with self.subTest(trouble=type(trouble).__name__):
                with mock.patch.object(maintenance, "_mark_restored", side_effect=trouble), \
                        self.assertRaises(type(trouble)):
                    maintenance.restore(day)
                self.assertEqual(self.tickers(), ["A", "B"])
                self.assertIsNone(db.get("restored"))
                self.assertIn(says, config.load()["disabled_reason"])

    def test_a_restore_that_stops_after_the_swap_keeps_the_marker(self):
        # From the swap on, the books may be the backup's. They already hold the marker, so B
        # still comes back, and neither the stop nor the command says "Not restored".
        day = self.bought_after_a_backup()
        out = io.StringIO()
        with self.trouble_after_the_swap("disk I/O error"), redirect_stdout(out):
            code = cli.main(["restore", day, "--yes"])
        self.assertEqual(self.tickers(), ["A"])
        self.assertEqual((db.get("restored") or {}).get("day"), day)   # the catch-up will run
        self.assertEqual(code, 1)
        self.assertNotIn("Not restored", out.getvalue())
        self.assertIn(f"restore {day} --yes", out.getvalue())          # run it again
        reason = config.load()["disabled_reason"]
        self.assertIn("stopped part way", reason)
        self.assertIn("Run the restore again", reason)

    def test_a_restore_cut_off_after_the_swap_keeps_the_marker(self):
        day = self.bought_after_a_backup()
        with self.trouble_after_the_swap(KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
            maintenance.restore(day)   # Ctrl-C, or the SSH window closed
        self.assertEqual(self.tickers(), ["A"])
        self.assertEqual((db.get("restored") or {}).get("day"), day)
        self.assertIn("Run the restore again", config.load()["disabled_reason"])

    def test_restoring_broken_books_never_leaves_them_missing(self):
        # Broken books are swapped as files. The new file (marker inside) replaces them in one
        # step, so a cut-off restore never leaves no books, which the bot would start afresh.
        self.seed()
        b = maintenance.backup()
        with open(db.path(), "wb") as f:
            f.write(b"not a database " * 100)
        real = os.replace

        def replace(src, dst):
            if dst == db.path():
                raise KeyboardInterrupt   # cut off right at the swap
            return real(src, dst)
        with mock.patch("kal.maintenance.os.replace", side_effect=replace), self.assertRaises(KeyboardInterrupt):
            maintenance.restore(b["day"])
        with open(db.path(), "rb") as f:
            self.assertTrue(f.read().startswith(b"not a database"))   # still there
        maintenance.restore(b["day"])   # run again, as the stop says
        self.assertEqual(len(db.query("SELECT * FROM orders")), 1)
        self.assertEqual(db.get("restored")["day"], b["day"])

    def test_restore_waits_for_a_trade_run(self):
        self.seed()
        b = maintenance.backup()
        with risk.run_lock():
            with self.assertRaises(risk.Locked):
                maintenance.restore(b["day"], wait=0)
        self.assertEqual(config.load()["disabled_reason"], "")   # no false "restored" stop

    def test_never_copies_the_key(self):
        with open(os.path.join(self.home, "kalshi.pem"), "w") as f:
            f.write("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
        self.seed()
        maintenance.backup()
        for _, _, files in os.walk(maintenance.backups_dir()):
            self.assertFalse([f for f in files if not f.endswith(".db")], files)

    def test_a_failed_backup_deletes_nothing(self):
        self.seed()
        start = utc(2026, 10, 1, 9)
        for i in range(14):
            maintenance.backup(now=start + i * DAY)
        with mock.patch.object(maintenance, "_copy", side_effect=maintenance.MaintenanceError("disk error")):
            with self.assertRaises(maintenance.MaintenanceError):
                maintenance.backup(now=start + 20 * DAY)
        self.assertEqual(len(maintenance.backups()), 14)
        self.assertNotIn("2026-10-21", os.listdir(maintenance.backups_dir()))   # no empty day left behind
        with free_disk(100), self.assertRaises(maintenance.MaintenanceError):
            maintenance.backup(now=start + 21 * DAY)
        self.assertEqual(len(maintenance.backups()), 14)

    def test_nothing_to_back_up_yet(self):
        self.assertEqual(maintenance.backup()["files"], [])
        self.assertEqual(maintenance.backups(), [])


# ---------- prune ----------

class PruneTests(Base):
    def signal(self, event, ts, guaranteed=0):
        return db.insert("signals", ts=ts, strategy="favorites", event=event, title="t", mode="live",
                         guaranteed=guaranteed, cost=1.0, payout_min=0.0, exp_profit=0.0, legs=[],
                         status="executed", note="", meta={"edge": f"KXA:{event}"})

    def money(self):
        cfg = config.load()
        return (ledger.capital(cfg), ledger.open_cost(), ledger.open_by_event(), ledger.open_directional(),
                ledger.peak_equity(), paper.peak_equity(), paper.open_directional(), paper.open_by_event(),
                paper.realized())

    def test_never_deletes_rows_the_money_rules_read(self):
        config.update({"budget": 300})
        bet, arb, settled = self.signal("EV-BET", OLD), self.signal("EV-ARB", OLD, 1), self.signal("EV-DONE", OLD)
        paper_open, paper_done = self.signal("EV-P1", OLD), self.signal("EV-P2", OLD)
        skipped, fresh = self.signal("EV-SKIP", OLD), self.signal("EV-NEW", time.time())
        self.fill("T-BET", 10, 0.9, ts=OLD, signal_id=bet)        # an old bet, market still open
        self.fill("T-ARB", 10, 0.4, ts=OLD, signal_id=arb)        # an old locked-in basket, still open
        self.fill("T-DONE", 10, 0.9, ts=OLD, signal_id=settled)   # settled: promotion still reads it
        self.fill("T-NULL", 0, 0.5, ts=OLD)                       # no signal_id must not block the rest
        db.insert("settlements", ticker="T-DONE", settled_ts=OLD, result="yes", revenue=10, cost=9.1, pnl=0.9)
        db.insert("paper", ts=OLD, signal_id=paper_open, strategy="favorites", event="EV-P1", ticker="P1",
                  side="yes", count=5, price=0.9, cost=4.6)
        db.insert("paper", ts=OLD, signal_id=paper_done, strategy="favorites", event="EV-P2", ticker="P2",
                  side="yes", count=5, price=0.9, cost=4.6, settled=1, result="yes", pnl=0.4, settled_ts=OLD)
        for ts in (OLD, OLD, time.time()):
            db.insert("runs", ts=ts, kind="trade", ok=1, duration=0, message="{}")
        db.insert("equity", ts=OLD, balance=400, exposure=0, equity=400, paper_equity=900)        # live peak
        db.insert("equity", ts=OLD - 1, balance=90, exposure=0, equity=90, paper_equity=1200)     # paper peak
        db.insert("equity", ts=OLD + 1, balance=50, exposure=0, equity=50, paper_equity=50)
        db.insert("equity", ts=time.time(), balance=300, exposure=0, equity=300, paper_equity=1000)
        db.insert("backtests", ts=OLD, kind="calibration", params={}, results={})
        before = self.money()

        out = maintenance.prune()

        self.assertEqual(self.money(), before)
        self.assertEqual({r["id"] for r in db.query("SELECT id FROM signals")},
                         {bet, arb, settled, paper_open, paper_done, fresh})
        self.assertNotIn(skipped, {r["id"] for r in db.query("SELECT id FROM signals")})
        self.assertEqual(len(db.query("SELECT * FROM runs")), 1)
        self.assertEqual(sorted(r["equity"] for r in db.query("SELECT equity FROM equity")), [90, 300, 400])
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM backtests")[0]["n"], 0)
        self.assertEqual(len(db.query("SELECT * FROM orders")), 4)    # the books are never pruned
        self.assertEqual(len(db.query("SELECT * FROM paper")), 2)
        self.assertEqual(out["kal.db"]["signals"], 1)

    def test_vacuum_shrinks_the_file(self):
        for _ in range(300):
            db.insert("runs", ts=OLD, kind="trade", ok=1, duration=0, message="x" * 2000)
        size = os.path.getsize(db.path())
        out = maintenance.prune()
        self.assertTrue(out["kal.db"]["vacuumed"])
        self.assertLess(os.path.getsize(db.path()), size)
        self.assertNotIn("vacuumed", maintenance.prune()["kal.db"])   # nothing deleted: no VACUUM

    def test_prunes_the_demo_books_too(self):
        config.update({"env": "demo"})
        db.insert("runs", ts=OLD, kind="trade", ok=1, duration=0, message="{}")
        config.update({"env": "prod"})
        db.insert("runs", ts=OLD, kind="trade", ok=1, duration=0, message="{}")
        out = maintenance.prune()
        self.assertEqual((out["kal.db"]["runs"], out["kal-demo.db"]["runs"]), (1, 1))

    def test_leaves_the_owners_own_copies_alone(self):
        db.insert("runs", ts=OLD, kind="trade", ok=1, duration=0, message="{}")
        mine = os.path.join(self.home, "kal-copy.db")
        with open(db.path(), "rb") as f, open(mine, "wb") as g:
            g.write(f.read())
        before = os.path.getsize(mine), os.path.getmtime(mine)
        self.assertEqual(list(maintenance.prune()), ["kal.db"])
        self.assertEqual(maintenance.backup()["files"], ["kal.db"])
        self.assertEqual((os.path.getsize(mine), os.path.getmtime(mine)), before)

    def test_waits_for_a_trade_run_and_never_runs_under_one(self):
        with risk.run_lock():
            with self.assertRaises(risk.Locked):
                maintenance.prune(wait=0)
        held, release = threading.Event(), threading.Event()

        def trade_run():
            with risk.run_lock():
                held.set()
                release.wait(5)
        t = threading.Thread(target=trade_run)
        t.start()
        held.wait(5)
        threading.Timer(0.3, release.set).start()
        t0 = time.time()
        maintenance.prune(wait=10)
        self.assertGreater(time.time() - t0, 0.25)
        t.join(5)


# ---------- disk ----------

class DiskTests(Base):
    def test_low_disk_trips(self):
        config.update({"enabled": True, "budget": 300})
        with free_disk(150):
            r = maintenance.disk_ok()
        self.assertEqual((r["level"], r["tripped"]), ("fail", True))
        cfg = config.load()
        self.assertFalse(cfg["enabled"])
        self.assertIn("Disk almost full", cfg["disabled_reason"])

    def test_warns_then_ok(self):
        with free_disk(500):
            self.assertEqual(maintenance.disk_ok()["level"], "warn")
        with free_disk(50_000):
            self.assertEqual(maintenance.disk_ok()["level"], "ok")
        self.assertEqual(config.load()["disabled_reason"], "")

    def test_keeps_the_first_stop_reason(self):
        config.trip("basket left half-built")
        with free_disk(10):
            r = maintenance.disk_ok()
        self.assertFalse(r["tripped"])
        self.assertEqual(config.load()["disabled_reason"], "basket left half-built")

    def test_a_person_checking_never_trips(self):
        with free_disk(10):
            self.assertEqual(maintenance.disk_ok(trip=False)["level"], "fail")
        self.assertEqual(config.load()["disabled_reason"], "")


# ---------- clock ----------

class ClockTests(Base):
    def check(self, behind, **kw):
        with kalshi_clock(behind) as get:
            return maintenance.clock_ok(**kw), get

    def test_in_sync_uses_one_public_request(self):
        r, get = self.check(0)
        self.assertEqual(r["level"], "ok")
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.args[0], "https://api.elections.kalshi.com/trade-api/v2/markets")
        self.assertEqual(get.call_args.kwargs["params"], {"limit": 1})
        self.assertNotIn("headers", get.call_args.kwargs)   # no key, no signature

    def test_demo_checks_the_demo_exchange(self):
        config.update({"env": "demo"})
        _, get = self.check(0)
        self.assertIn("demo-api.kalshi.co", get.call_args.args[0])

    def test_over_5_seconds_warns_without_tripping(self):
        for behind in (10, -10):
            r, _ = self.check(behind)
            self.assertEqual(r["level"], "warn")
        self.assertIn("behind", r["detail"])
        self.assertEqual(config.load()["disabled_reason"], "")

    def test_over_30_seconds_twice_in_a_row_trips(self):
        config.update({"enabled": True, "budget": 300})
        r, _ = self.check(45)
        self.assertEqual(r["level"], "fail")
        self.assertTrue(config.load()["enabled"])   # one reading can be a fluke (or a fresh boot)
        r, _ = self.check(45)
        self.assertTrue(r["tripped"])
        self.assertIn("clock", config.load()["disabled_reason"])

    def test_a_good_reading_in_between_resets_it(self):
        self.check(-45)
        self.check(0)
        self.check(-45)
        self.assertEqual(config.load()["disabled_reason"], "")

    def test_a_person_checking_never_trips(self):
        for _ in range(3):
            r, _ = self.check(120, trip=False)
        self.assertEqual(r["level"], "fail")
        self.assertEqual(config.load()["disabled_reason"], "")

    def test_a_slow_reply_is_not_a_bad_clock(self):
        reply = SimpleNamespace(headers={"Date": formatdate(1000.0, usegmt=True)})
        with mock.patch("kal.maintenance.requests.get", return_value=reply):
            times = iter([1000.0, 1040.0])   # a 40 s round trip: the Date fits anywhere in it
            self.assertEqual(maintenance.clock_ok(clock=lambda: next(times))["level"], "ok")
            times = itertools.cycle([1050.0, 1051.0])   # 49 s ahead even at the far end, every try
            self.assertEqual(maintenance.clock_ok(clock=lambda: next(times))["level"], "fail")

    def test_a_cached_reply_is_not_a_bad_clock(self):
        # Real Kalshi: GET /markets comes through CloudFront, cached up to 15 s, and a cached
        # reply keeps its old Date. "Age" says how old. This one is 9 s old.
        cached = SimpleNamespace(headers={"Date": formatdate(time.time() - 9, usegmt=True), "Age": "9",
                                          "X-Cache": "Hit from cloudfront"})
        with mock.patch("kal.maintenance.requests.get", return_value=cached) as get:
            r = maintenance.clock_ok()
        self.assertEqual(r["level"], "ok")
        self.assertEqual(get.call_count, 1)

    def test_a_stale_reading_is_tried_again(self):
        stale = SimpleNamespace(headers={"Date": formatdate(time.time() - 12, usegmt=True)})  # no Age
        fresh = SimpleNamespace(headers={"Date": formatdate(time.time(), usegmt=True)})
        with mock.patch("kal.maintenance.requests.get", side_effect=[stale, fresh]) as get:
            r = maintenance.clock_ok()
        self.assertEqual((r["level"], get.call_count), ("ok", 2))

    def test_a_real_skew_shows_on_every_try(self):
        r, get = self.check(-45, trip=False)
        self.assertEqual((r["level"], get.call_count), ("fail", maintenance.CLOCK_TRIES))

    def test_no_answer_is_not_a_bad_clock(self):
        with mock.patch("kal.maintenance.requests.get", side_effect=requests.ConnectionError("down")):
            r = maintenance.clock_ok()
        self.assertEqual((r["level"], r["unknown"]), ("warn", True))
        for headers in ({}, {"Date": "not a date"}):
            with mock.patch("kal.maintenance.requests.get", return_value=SimpleNamespace(headers=headers)):
                self.assertTrue(maintenance.clock_ok()["unknown"])
        self.assertEqual(config.load()["disabled_reason"], "")


# ---------- trip history ----------

class TripLogTests(Base):
    def test_each_stop_is_written_once(self):
        self.assertIsNone(maintenance.note_trips(now=50))
        config.trip("lost too much")
        self.assertEqual(maintenance.note_trips(now=100)["reason"], "lost too much")
        self.assertIsNone(maintenance.note_trips(now=200))     # the same stop, seen again
        config.clear_trip()
        self.assertTrue(maintenance.note_trips(now=300)["cleared"])
        config.trip("lost too much")                            # same reason, but a new stop
        maintenance.note_trips(now=400)
        self.assertEqual([t["ts"] for t in maintenance.trips(0, 1000)], [100, 400])
        self.assertEqual([t["ts"] for t in maintenance.trips(150, 1000)], [400])


# ---------- health / daily ----------

class HealthTests(Base):
    def test_checks_disk_clock_and_trading_and_saves_them(self):
        with free_disk(50_000), kalshi_clock(0):
            out = maintenance.health()
        self.assertTrue(out["ok"], out)
        self.assertEqual([r["check"] for r in out["checks"]], ["disk", "clock", "trading"])
        self.assertEqual([r["check"] for r in db.get("health")["checks"]], ["disk", "clock", "trading"])

    def test_a_hard_failure_trips_and_is_logged_for_the_summary(self):
        config.update({"enabled": True, "budget": 300})
        with free_disk(100), kalshi_clock(0):
            out = maintenance.health()
        self.assertFalse(out["ok"])
        self.assertIn("disk", out["error"])
        self.assertIn("Disk almost full", config.load()["disabled_reason"])
        self.assertEqual(len(maintenance.trips(0, time.time() + 1)), 1)

    def test_warnings_reach_the_phone_once_a_day(self):
        os.environ["KAL_NTFY_TOPIC"] = "kal-test-topic"
        with free_disk(500), kalshi_clock(10), mock.patch("kal.notify.send", return_value=True) as send:
            maintenance.health()
            maintenance.health()
        self.assertEqual(sorted(c.args[0] for c in send.call_args_list), ["kal warning: clock", "kal warning: disk"])

    def test_a_long_outage_warns(self):
        config.update({"enabled": True, "budget": 300})
        now = time.time()
        self.assertEqual(maintenance.trading_ok(now)["level"], "ok")   # just turned on: not an outage
        db.insert("runs", ts=now - 5 * 3600, kind="trade", ok=1, duration=0, message="{}")
        db.insert("runs", ts=now - 3 * 3600, kind="trade", ok=0, duration=0, message="{}")
        db.insert("runs", ts=now - 60, kind="trade", ok=0, duration=0, message="{}")
        db.put("last_transient_error", {"ts": now - 60, "error": "GET /markets -> 503"})
        r = maintenance.trading_ok(now)
        self.assertEqual(r["level"], "warn")
        self.assertIn("3 h", r["detail"])
        self.assertIn("503", r["detail"])
        db.insert("runs", ts=now, kind="trade", ok=1, duration=0, message="{}")
        self.assertEqual(maintenance.trading_ok(now)["level"], "ok")
        config.turn_off()
        self.assertEqual(maintenance.trading_ok(now)["detail"], "the bot is off")


class DailyTests(Base):
    def test_backs_up_then_prunes_and_records_a_run(self):
        db.insert("runs", ts=OLD, kind="trade", ok=1, duration=0, message="{}")
        with free_disk(50_000):
            out = maintenance.daily()
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(maintenance.backups()), 1)
        self.assertEqual(out["pruned"]["kal.db"]["runs"], 1)
        self.assertEqual(len(db.query("SELECT * FROM runs WHERE kind = 'maintenance' AND ok = 1")), 1)

    def test_never_prunes_without_a_fresh_backup(self):
        db.insert("runs", ts=OLD, kind="trade", ok=1, duration=0, message="{}")
        with mock.patch.object(maintenance, "backup", side_effect=maintenance.MaintenanceError("no space")), \
                self.assertLogs("kal.maintenance", "ERROR"):
            out = maintenance.daily()
        self.assertFalse(out["ok"])
        self.assertIn("backup failed", out["error"])
        self.assertEqual(len(db.query("SELECT * FROM runs WHERE kind = 'trade'")), 1)


# ---------- logs ----------

class LogsTests(Base):
    def setUp(self):
        super().setUp()
        root, web = logging.getLogger(), logging.getLogger("werkzeug")
        saved = (root.level, list(root.handlers), web.level)

        def put_back():
            for h in list(root.handlers):
                if h not in saved[1]:
                    root.removeHandler(h)
                    h.close()
            root.setLevel(saved[0])
            web.setLevel(saved[2])
        self.addCleanup(put_back)

    def test_rotating_file_5mb_x5_in_utc(self):
        added = logs.setup(self.home)
        files = [h for h in added if isinstance(h, logging.handlers.RotatingFileHandler)]
        self.assertEqual(len(files), 1)
        self.assertEqual((files[0].maxBytes, files[0].backupCount), (5 * 1024 * 1024, 5))
        self.assertEqual(files[0].baseFilename, os.path.join(self.home, "logs", "kal.log"))
        self.assertEqual(logs.setup(self.home), [])   # a second call adds nothing
        logging.getLogger("kal.test").info("hello there")
        line = logs.tail(5)[-1]
        self.assertIn("UTC INFO kal.test: hello there", line)
        self.assertEqual(logging.getLogger("werkzeug").level, logging.WARNING)   # no line per page view

    def test_secrets_never_reach_the_file(self):
        os.environ.update(KAL_NTFY_TOPIC="kal-topic-SECRET-123", KAL_GITHUB_TOKEN="github_pat_SECRET_99")
        logs.setup(self.home)
        logging.getLogger("kal.test").error("POST https://ntfy.sh/kal-topic-SECRET-123 with github_pat_SECRET_99")
        with open(logs.path()) as f:
            text = f.read()
        self.assertNotIn("SECRET", text)
        self.assertIn("[removed]", text)

    def test_secrets_saved_by_setup_are_scrubbed_too(self):
        """`kal setup` keeps the key ID and alert topic in files, not env vars."""
        for name, value in (("ntfy_topic", "kal-file-SECRET-topic"), ("kalshi_key_id", "kid-file-SECRET-1")):
            with open(os.path.join(self.home, name), "w") as f:
                f.write(value + "\n")
        logs.setup(self.home)
        logging.getLogger("kal.test").warning("to https://ntfy.sh/kal-file-SECRET-topic as kid-file-SECRET-1")
        with open(logs.path()) as f:
            self.assertNotIn("SECRET", f.read())
        with open(os.path.join(self.home, "ntfy_topic"), "w") as f:   # a new topic is picked up
            f.write("kal-new-SECRET-name-2\n")
        self.assertEqual(logs.scrub("x kal-new-SECRET-name-2 y"), "x [removed] y")

    def test_tail_reads_across_a_rotation(self):
        os.makedirs(os.path.dirname(logs.path()))
        with open(logs.path() + ".1", "w") as f:
            f.write("".join(f"old {i}\n" for i in range(100)))
        with open(logs.path(), "w") as f:
            f.write("new 0\nnew 1\n")
        self.assertEqual(logs.tail(5), ["old 97", "old 98", "old 99", "new 0", "new 1"])
        self.assertEqual(logs.tail(1), ["new 1"])

    def test_no_log_file_is_not_fatal(self):
        with mock.patch("kal.logs.os.makedirs", side_effect=PermissionError("read-only")), \
                mock.patch("sys.stderr", io.StringIO()) as err:
            added = logs.setup(self.home)
        self.assertFalse([h for h in added if isinstance(h, logging.FileHandler)])
        self.assertIn("can't write the log file", err.getvalue())
        self.assertEqual(logs.tail(), [])


# ---------- the daily summary ----------

class SummaryTests(Base):
    def test_yesterdays_numbers(self):
        now, yesterday = utc(2026, 10, 25, 13), utc(2026, 10, 24, 15)
        config.update({"enabled": True, "budget": 300})
        db.insert("settlements", ticker="A", settled_ts=yesterday, result="yes", revenue=10, cost=8.77, pnl=1.23)
        db.insert("settlements", ticker="B", settled_ts=now - 60, result="no", revenue=0, cost=5, pnl=-5)
        db.insert("paper", strategy="favorites", event="E", ticker="P1", side="yes", count=10, price=0.9,
                  cost=9.1, settled=1, result="no", pnl=-9.1, settled_ts=yesterday)
        db.insert("paper", strategy="favorites", event="E", ticker="P2", side="yes", count=1, price=0.5, cost=0.51)
        self.fill("OPEN")
        os.makedirs(os.path.join(self.home, "research"))
        with open(os.path.join(self.home, "research", "edges.json"), "w") as f:
            json.dump([{"id": "KXA:0.9-0.95", "status": "live", "promoted_at": "2026-10-24T12:00:00+00:00"},
                       {"id": "KXB:0.8-0.85", "status": "live", "promoted_at": "2026-10-20T12:00:00+00:00"}], f)
        with open(os.path.join(self.home, maintenance.TRIPS), "w") as f:
            f.write(json.dumps({"ts": yesterday, "reason": "Disk almost full: 150 MB free."}) + "\n")

        title, text, priority = report.daily(now)

        self.assertEqual(title, "kal yesterday: +$1.23")
        for bit in ("Yesterday (Sat Oct 24, UTC):", "Real money: +$1.23 from 1 settled bet\n",
                    "Paper: -$9.10 from 1 settled bet\n", "Capital now: $296.23 (budget $300.00)",
                    f"Open bets: 1 real (${2.5 + fee(5, 0.5):.2f}), 1 paper", "Safety stops: 1 (Disk almost full",
                    "Ideas promoted: 1 (KXA:0.9-0.95)", "Bot now: ON"):
            self.assertIn(bit, text)
        self.assertEqual(priority, "low")

    def test_a_stopped_bot_comes_first_and_buzzes(self):
        config.trip("lost $40 today")
        title, text, priority = report.daily()
        self.assertTrue(text.startswith("STOPPED: lost $40 today"))
        self.assertIn("Safety stops: none", text)   # the health job hasn't noticed it yet
        self.assertEqual(priority, "high")

    def test_demo_says_demo(self):
        config.update({"env": "demo"})
        title, text, _ = report.daily()
        self.assertIn("demo", title)
        self.assertIn("Demo money", text)


# ---------- the scheduler's new jobs ----------

class SchedulerJobTests(Base):
    def test_health_every_10_minutes_daily_jobs_at_their_hour(self):
        clock = Clock(utc(2026, 10, 25, 8, 0))
        jobs = {k: Recorder() for k in ("health", "maintenance", "summary")}
        s = Scheduler(jobs=jobs, clock=clock, threaded=False)
        self.assertEqual(sorted(s.tick()), ["health", "maintenance", "summary"])   # first start: once each
        steps = [((2026, 10, 25, 8, 5), []), ((2026, 10, 25, 8, 10), ["health"]),
                 ((2026, 10, 25, 8, 59), ["health"]), ((2026, 10, 25, 9, 0), ["maintenance"]),
                 ((2026, 10, 25, 9, 5), []), ((2026, 10, 25, 13, 0), ["health", "summary"]),
                 ((2026, 10, 26, 8, 0), ["health"]), ((2026, 10, 26, 9, 1), ["health", "maintenance"])]
        for when, want in steps:
            clock.t = utc(*when)
            self.assertEqual(sorted(s.tick()), want, when)
        self.assertEqual((jobs["maintenance"].calls, jobs["summary"].calls, jobs["health"].calls), (3, 2, 6))
        self.assertEqual(s.state()["maintenance"]["next"], utc(2026, 10, 27, 9))

    def test_a_server_that_was_down_catches_up_once(self):
        db.put("scheduler", {"maintenance": utc(2026, 10, 20, 9)})
        clock = Clock(utc(2026, 10, 25, 15))
        s = Scheduler(jobs={"maintenance": Recorder()}, clock=clock, threaded=False)
        self.assertEqual(s.tick(), ["maintenance"])
        clock.t += 3600
        self.assertEqual(s.tick(), [])

    def test_survives_each_new_job_raising(self):
        clock = Clock(utc(2026, 10, 25, 13))
        jobs = {k: Recorder(exc=RuntimeError(f"{k} broke")) for k in ("health", "maintenance", "summary")}
        jobs["trade"] = Recorder()
        s = Scheduler(jobs=jobs, clock=clock, threaded=False)
        with self.assertLogs("kal.scheduler", "ERROR"):
            self.assertEqual(sorted(s.tick()), ["health", "maintenance", "summary", "trade"])
        st = s.state()
        for k in ("health", "maintenance", "summary"):
            self.assertFalse(st[k]["ok"])
            self.assertIn(f"{k} broke", st[k]["text"])
        self.assertTrue(st["trade"]["ok"])
        self.assertEqual({r["kind"] for r in db.query("SELECT kind FROM runs WHERE ok = 0")},
                         {"health", "maintenance", "summary"})
        clock.t += DAY
        with self.assertLogs("kal.scheduler", "ERROR"):
            s.tick()
        self.assertEqual([jobs[k].calls for k in ("health", "maintenance", "summary", "trade")], [2, 2, 2, 2])

    def test_the_loop_keeps_going_and_beats(self):
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("kaboom")
        s = Scheduler(jobs={"health": boom}, tick=0.01, intervals=lambda cfg: {"health": 0.02},
                      hours=lambda cfg: {})
        self.assertIsNone(maintenance.heartbeat_age())
        with self.assertLogs("kal.scheduler", "ERROR"):
            s.start()
            try:
                deadline = time.time() + 5
                while len(calls) < 3 and time.time() < deadline:
                    time.sleep(0.01)
                self.assertLess(maintenance.heartbeat_age(), 60)
            finally:
                s.stop(wait=5)
        self.assertGreaterEqual(len(calls), 3)
        self.assertIsNone(maintenance.heartbeat_age())   # a clean stop takes it away

    def test_the_loop_beats_before_its_first_tick(self):
        # The first tick can take a while; until the loop beats, the heartbeat on disk may be
        # a crashed process's.
        seen = []
        s = Scheduler(jobs={"health": lambda: seen.append(maintenance.heartbeat_age())}, tick=0.01,
                      intervals=lambda cfg: {"health": 60}, hours=lambda cfg: {})
        s.start()
        try:
            deadline = time.time() + 5
            while not seen and time.time() < deadline:
                time.sleep(0.01)
        finally:
            s.stop(wait=5)
        self.assertTrue(seen and seen[0] is not None, seen)

    def test_summary_hour_is_read_from_config_each_tick(self):
        db.put("scheduler", {"summary": utc(2026, 10, 24, 13)})
        clock = Clock(utc(2026, 10, 25, 12))
        s = Scheduler(jobs={"summary": Recorder()}, clock=clock, threaded=False)
        self.assertEqual(s.tick(), [])
        config.update({"summary_hour_utc": 11})
        self.assertEqual(s.tick(), ["summary"])
        for bad in (24, -1, "11", True, 7.5, None):
            self.assertEqual(scheduler.daily_hours({"summary_hour_utc": bad})["summary"], scheduler.SUMMARY_HOUR)

    def test_default_jobs(self):
        self.assertLessEqual({"trade", "research", "report", "health", "maintenance", "summary"},
                             set(scheduler.DEFAULT_JOBS))
        self.assertEqual(scheduler.intervals({})["health"], 600)

    def test_job_functions(self):
        with mock.patch("kal.maintenance.health", return_value={"ok": True}) as h:
            self.assertEqual(scheduler.run_health(), {"ok": True})
        h.assert_called_once_with()
        with mock.patch("kal.maintenance.daily", return_value={"ok": True}) as d:
            self.assertEqual(scheduler.run_maintenance(), {"ok": True})
        d.assert_called_once_with()

    def test_summary_job_sends_only_with_a_topic(self):
        with mock.patch("kal.notify.requests.post") as post:
            self.assertIn("skipped", scheduler.run_summary())
        post.assert_not_called()
        os.environ["KAL_NTFY_TOPIC"] = "kal-test-topic"
        with mock.patch("kal.notify.requests.post") as post:
            post.return_value.status_code = 200
            self.assertEqual(scheduler.run_summary(), {"ok": True})
            headers = post.call_args.kwargs["headers"]
            self.assertIn("yesterday", headers["Title"])
            self.assertEqual(headers["Priority"], "low")
            post.return_value.status_code = 500
            self.assertFalse(scheduler.run_summary()["ok"])


if __name__ == "__main__":
    import unittest
    unittest.main()
