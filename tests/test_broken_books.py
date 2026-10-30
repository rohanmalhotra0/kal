"""The owner always hears about a stop. A damaged kal.db used to stop all trading with no
trip and no alert, while the watchdog stayed green; a stop alert that didn't get through
was never sent again. python -m unittest tests.test_broken_books
"""
import os
import sqlite3
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
        maintenance._sent_here.clear()
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
        self.assertIn("can't be used", cfg["disabled_reason"])
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
