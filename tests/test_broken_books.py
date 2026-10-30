"""The owner always hears about a stop. A damaged kal.db used to stop all trading with no
trip and no alert, while the watchdog stayed green; a stop alert that didn't get through
was never sent again. python -m unittest tests.test_broken_books
"""
import os
import sqlite3
import time
from types import SimpleNamespace
from unittest import mock

from kal import config, db, engine, maintenance, notify
from tests.helpers import TempHome, orders_proven
from tests.test_core import fake_with_arb
from tests.test_maintenance import free_disk, kalshi_clock


def break_books():
    """Overwrite the first page of kal.db: SQLite can no longer read any of it."""
    p = db.path()
    for extra in ("-wal", "-shm"):
        if os.path.exists(p + extra):
            os.remove(p + extra)
    with open(p, "r+b") as f:
        f.write(b"\x00not a database" * 64)


class Phone:
    """ntfy, faked: answers `status` and keeps every alert's title."""

    def __init__(self, status=200):
        self.status, self.titles = status, []

    def __call__(self, url, data=None, timeout=None, headers=None):
        self.titles.append(headers["Title"])
        return SimpleNamespace(status_code=self.status)


class Base(TempHome):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"KAL_NTFY_TOPIC": "test-topic"})
        env.start()
        self.addCleanup(env.stop)
        super().setUp()
        self.phone = Phone()
        post = mock.patch.object(notify.requests, "post", self.phone)
        post.start()
        self.addCleanup(post.stop)
        maintenance._clock_bad["ts"] = None
        config.update({"enabled": True, "budget": 60,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})
        orders_proven()
        self.client = fake_with_arb()
        self.assertTrue(engine.run_once(self.client)["ok"])   # good books first
        self.phone.titles.clear()


class BrokenBooksTests(Base):
    def test_a_trade_run_on_damaged_books_stops_the_bot_and_says_so(self):
        break_books()
        with self.assertLogs("kal", "ERROR"), self.assertRaises(sqlite3.DatabaseError):
            engine.run_once(self.client)   # it can't even record the run
        cfg = config.load()
        self.assertFalse(cfg["enabled"])
        self.assertTrue(cfg["disabled_reason"].startswith(maintenance.DAMAGED), cfg["disabled_reason"])
        self.assertIn("restore", cfg["disabled_reason"])
        self.assertEqual(self.phone.titles, ["kal stopped itself"])
        self.assertEqual(engine.run_once(self.client)["skipped"], cfg["disabled_reason"])
        self.assertEqual(len(self.phone.titles), 1)   # once, not every run

    def test_the_health_check_finds_damaged_books_and_sends_one_alert(self):
        break_books()
        with free_disk(50_000), kalshi_clock(0), self.assertLogs("kal", "WARNING"):
            out = maintenance.health()
        books = [r for r in out["checks"] if r["check"] == "books"][0]
        self.assertEqual(books["level"], "fail")
        self.assertIn("sudo kal restore", books["detail"].replace("python -m kal", "sudo kal"))
        self.assertIn("the books are damaged", config.load()["disabled_reason"])
        # One alert: the stop. Not a second about the trading check, which failed on the books.
        self.assertEqual(self.phone.titles, ["kal stopped itself"])
        with free_disk(50_000), kalshi_clock(0), self.assertLogs("kal", "WARNING"):
            maintenance.health()
        self.assertEqual(self.phone.titles, ["kal stopped itself"])   # not again 10 min later

    def test_damaged_books_when_already_stopped_still_alert_once_a_day(self):
        config.trip("an earlier stop")
        self.phone.titles.clear()
        break_books()
        for _ in range(3):
            with free_disk(50_000), kalshi_clock(0), self.assertLogs("kal", "WARNING"):
                maintenance.health()
        self.assertEqual(self.phone.titles, ["kal warning: books"])

    def test_the_watchdog_sees_damaged_books(self):
        break_books()
        maintenance.beat()
        rows = {r["check"]: r["level"] for r in maintenance.alive()}
        self.assertEqual(rows["books"], "fail")
        self.assertEqual(rows["timer"], "ok")

    def test_good_books_and_no_books_yet_are_fine(self):
        self.assertEqual(maintenance.books_ok()["level"], "ok")
        os.remove(db.path())
        self.assertEqual(maintenance.books_ok()["level"], "ok")
        self.assertFalse(os.path.exists(db.path()))   # the check never makes one


    def test_a_trade_run_then_the_health_check_send_one_alert(self):
        break_books()
        with self.assertLogs("kal", "ERROR"), self.assertRaises(sqlite3.DatabaseError):
            engine.run_once(self.client)
        with free_disk(50_000), kalshi_clock(0), self.assertLogs("kal", "WARNING"):
            maintenance.health()
        self.assertEqual(self.phone.titles, ["kal stopped itself"])

    def test_with_the_status_report_set_up_still_one_alert(self):
        # In production a GitHub token is set, so the report check runs: it fails on the
        # damaged books too, and must not send its own warning.
        with mock.patch.dict(os.environ, {"KAL_GITHUB_TOKEN": "t", "KAL_GITHUB_REPO": "o/r"}):
            break_books()
            with free_disk(50_000), kalshi_clock(0), self.assertLogs("kal", "WARNING"):
                out = maintenance.health()
        self.assertIn("report", {r["check"] for r in out["checks"]})
        self.assertEqual(self.phone.titles, ["kal stopped itself"])

    def test_busy_or_locked_books_are_not_damaged(self):
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(maintenance, "_open", side_effect=locked):
            row = maintenance.books_ok()
        self.assertEqual(row["level"], "warn")
        self.assertTrue(config.load()["enabled"])   # never stopped, never sent to restore
        self.assertFalse(db.damaged(locked))
        self.assertFalse(db.damaged(sqlite3.OperationalError("disk I/O error")))

    def test_a_run_that_cant_write_its_count_stops_after_the_usual_tries(self):
        cfg = config.load()
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(db, "get", side_effect=locked):
            for _ in range(2):
                engine._count_error(cfg, RuntimeError("boom"))
                self.assertTrue(config.load()["enabled"])
            engine._count_error(cfg, RuntimeError("boom"))
        reason = config.load()["disabled_reason"]
        self.assertIn("can't be written", reason)
        self.assertNotIn("restore", reason)   # a backup wouldn't fix a lock or a full disk

    def test_an_empty_books_file_is_none_yet(self):
        os.remove(db.path())
        open(db.path(), "w").close()
        self.assertEqual(maintenance.books_ok()["level"], "ok")
        self.assertTrue(config.load()["enabled"])


class StopAlertTests(Base):
    def test_a_stop_alert_that_did_not_go_is_sent_again_until_it_does(self):
        self.phone.status = 503
        with self.assertLogs("kal.notify", "WARNING") as logs:
            config.trip("lost too much today")
        self.assertIn("HTTP 503", "\n".join(logs.output))
        with free_disk(50_000), kalshi_clock(0), self.assertLogs("kal.notify", "WARNING"):
            maintenance.health()   # ntfy still down
        self.assertEqual(self.phone.titles, ["kal stopped itself"] * 2)
        self.phone.status = 200
        with free_disk(50_000), kalshi_clock(0):
            maintenance.health()
        self.assertEqual(self.phone.titles, ["kal stopped itself"] * 3)
        with free_disk(50_000), kalshi_clock(0):
            maintenance.health()
        self.assertEqual(len(self.phone.titles), 3)   # delivered: not again

    def test_an_unsent_stop_alert_is_dropped_once_the_bot_is_back_on(self):
        self.phone.status = 503
        with self.assertLogs("kal.notify", "WARNING"):
            config.trip("lost too much today")
        config.clear_trip()
        self.phone.status = 200
        with free_disk(50_000), kalshi_clock(0):
            maintenance.health()
        self.assertEqual(self.phone.titles, ["kal stopped itself"])
        self.assertFalse(os.path.exists(os.path.join(config.home(), config.UNSENT_STOP)))

    def test_a_delivered_stop_alert_is_not_repeated(self):
        config.trip("lost too much today")
        with free_disk(50_000), kalshi_clock(0):
            maintenance.health()
        self.assertEqual(self.phone.titles, ["kal stopped itself"])

class AlertMemoryTests(Base):
    def test_books_that_read_but_cant_write_still_alert_once_a_day(self):
        with mock.patch.object(db, "put", side_effect=sqlite3.OperationalError("database or disk is full")), \
                self.assertLogs("kal", "WARNING"):
            for _ in range(3):
                maintenance._alert("disk:warn", "kal warning: disk", "almost full")
        self.assertEqual(self.phone.titles, ["kal warning: disk"])



class StaleStopTests(Base):
    def unsent(self, reason):
        self.phone.status = 503
        with self.assertLogs("kal.notify", "WARNING"):
            config.trip(reason)
        self.phone.status = 200
        self.phone.titles.clear()

    def test_a_newer_stop_that_got_through_drops_the_old_one(self):
        self.unsent("the books are damaged: kal.db")
        config.trip("Books restored from the 2026-10-29 backup")
        self.assertFalse(config.resend_stop())
        self.assertEqual(self.phone.titles, ["kal stopped itself"])   # only the new stop

    def test_a_newer_stop_that_didnt_get_through_is_the_one_resent(self):
        self.unsent("first reason")
        self.unsent("second reason")
        with mock.patch.object(notify, "send", wraps=notify.send) as send:
            self.assertTrue(config.resend_stop())
        self.assertIn("second reason", send.call_args.args[1])

    def test_a_stop_for_another_reason_since_drops_the_unsent_one(self):
        self.unsent("first reason")
        config.update({"disabled_reason": "someone else stopped it"})   # e.g. a later trip
        self.assertFalse(config.resend_stop())
        self.assertFalse(os.path.exists(os.path.join(config.home(), config.UNSENT_STOP)))

