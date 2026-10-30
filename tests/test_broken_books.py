"""The owner always hears about a stop. A damaged kal.db used to stop all trading with no
trip and no alert, while the watchdog stayed green; a stop alert that didn't get through
was never sent again. python -m unittest tests.test_broken_books
"""
import errno
import os
import re
import sqlite3
from contextlib import ExitStack
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
    """ntfy, faked: answers `status` and keeps every alert's title and text. `during`, if
    set, runs while an alert is being sent (another thread acting at that moment)."""

    def __init__(self, status=200):
        self.status, self.titles, self.bodies, self.during = status, [], [], None

    def __call__(self, url, data=None, timeout=None, headers=None):
        self.titles.append(headers["Title"])
        self.bodies.append(data.decode())
        during, self.during = self.during, None
        if during:
            during()
        return SimpleNamespace(status_code=self.status)


def service_kal_cmd():
    """KAL_CMD as deploy/kal.service sets it: alerts from the bot say what the owner types."""
    with open(os.path.join(config.ROOT, "deploy", "kal.service")) as f:
        return re.search(r'^Environment="KAL_CMD=([^"]+)"$', f.read(), re.M).group(1)


class Base(TempHome):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"KAL_NTFY_TOPIC": "test-topic", "KAL_CMD": service_kal_cmd()})
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
        self.phone.bodies.clear()


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
        self.assertIn("sudo kal restore", books["detail"])
        self.assertNotIn("python -m kal", config.load()["disabled_reason"])
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

    def test_a_real_lock_is_not_damage(self):
        p = db.path()
        with sqlite3.connect(p) as c:
            c.execute("PRAGMA journal_mode=DELETE")   # as after a restore: a writer blocks readers
        holder = sqlite3.connect(p, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            with mock.patch.object(maintenance, "OPEN_WAIT", 0.1):
                row = maintenance.books_ok()
            busy = None
            try:
                sqlite3.connect(p, timeout=0).execute("SELECT 1 FROM kv").fetchall()
            except sqlite3.OperationalError as e:
                busy = e
        finally:
            holder.execute("ROLLBACK")
        self.assertEqual(row["level"], "warn", row)
        self.assertTrue(config.load()["enabled"])
        self.assertIsNotNone(busy)
        self.assertFalse(db.damaged(busy))   # a real one, with its SQLite code

    def test_real_corruption_inside_the_file_is_damage(self):
        p = db.path()
        with sqlite3.connect(p) as c:
            c.execute("CREATE TABLE IF NOT EXISTS filler (x TEXT)")
            c.executemany("INSERT INTO filler VALUES (?)", [("x" * 500,)] * 2000)
        with sqlite3.connect(p) as c:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size = os.path.getsize(p)
        with open(p, "r+b") as f:   # garbage over a page in the middle: the header still reads
            f.seek(size // 2 // 4096 * 4096)
            f.write(os.urandom(4096))
        err = None
        try:
            with sqlite3.connect(p) as c:
                rows = c.execute("PRAGMA quick_check").fetchall()
        except sqlite3.DatabaseError as e:
            err, rows = e, []
        self.assertTrue(err is not None or rows != [("ok",)])
        if err is not None:
            self.assertTrue(db.damaged(err))   # SQLITE_CORRUPT
        with free_disk(50_000), kalshi_clock(0):
            maintenance.health()   # (the other checks still read their own pages fine)
        self.assertTrue(config.load()["disabled_reason"].startswith(maintenance.DAMAGED))
        self.assertEqual(self.phone.titles, ["kal stopped itself"])

    def test_the_count_kept_in_memory_starts_again_after_a_good_run(self):
        cfg = config.load()
        locked = sqlite3.OperationalError("database is locked")
        for _ in range(2):
            with mock.patch.object(db, "get", side_effect=locked):
                engine._count_error(cfg, RuntimeError("boom"))
        self.assertTrue(engine.run_once(self.client)["ok"])
        for _ in range(2):
            with mock.patch.object(db, "get", side_effect=locked):
                engine._count_error(cfg, RuntimeError("boom"))
        self.assertTrue(config.load()["enabled"])   # 2 + 2 with a good run between: no stop

    def test_a_run_finding_damage_after_the_health_check_stopped_it_sends_nothing_more(self):
        break_books()
        with free_disk(50_000), kalshi_clock(0), self.assertLogs("kal", "WARNING"):
            maintenance.health()
        engine._count_error(config.load(), sqlite3.DatabaseError("file is not a database"))
        self.assertEqual(self.phone.titles, ["kal stopped itself"])

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

class ReadOnlyDiskTests(Base):
    """A disk remounted read-only after errors: every run fails before it can stop the bot,
    so the health check stops it, by name."""

    def read_only(self):
        """As on a real read-only disk: no new file, and the books (WAL needs its -shm file)
        can't be opened either, so the books and trading checks fail too."""
        stack = ExitStack()
        stack.enter_context(mock.patch.object(maintenance.tempfile, "mkstemp",
                                              side_effect=OSError(errno.EROFS, "Read-only file system")))
        cant = sqlite3.OperationalError("unable to open database file")
        stack.enter_context(mock.patch.object(maintenance, "_open", side_effect=cant))
        stack.enter_context(mock.patch.object(db, "connect", side_effect=cant))
        return stack

    def test_a_read_only_disk_stops_the_bot_and_says_so_once(self):
        with self.read_only(), free_disk(50_000), kalshi_clock(0), self.assertLogs("kal", "WARNING"):
            out = maintenance.health()
        disk = [r for r in out["checks"] if r["check"] == "disk"][0]
        self.assertEqual(disk["level"], "fail")
        self.assertIn("read-only", config.load()["disabled_reason"])
        # One alert: the stop. Not "books busy" or "trading", which only failed on the disk.
        self.assertEqual(self.phone.titles, ["kal stopped itself"])

    def test_a_read_only_disk_when_already_stopped_says_read_only_not_full(self):
        config.trip("lost too much today")
        self.phone.titles.clear()
        self.phone.bodies.clear()
        with self.read_only(), free_disk(50_000), kalshi_clock(0), self.assertLogs("kal", "WARNING"):
            maintenance.health()
        self.assertEqual(self.phone.titles, ["kal warning: disk read-only"])   # its own DEPLOY.md row

    def test_a_disk_that_cant_take_one_more_file_stops_the_bot(self):
        # No free inodes: plenty of bytes, but nothing new can be written (the books' journal).
        with mock.patch.object(maintenance.tempfile, "mkstemp",
                               side_effect=OSError(errno.ENOSPC, "No space left on device")), \
                free_disk(50_000), kalshi_clock(0):
            row = maintenance.disk_ok()
        self.assertEqual(row["level"], "fail")
        self.assertIn("can't take one more file", config.load()["disabled_reason"])

    def test_when_the_stop_cant_be_saved_either_it_says_so_at_most_hourly(self):
        cant = OSError(errno.EROFS, "Read-only file system")
        with self.read_only(), mock.patch.object(config, "_write", side_effect=cant), \
                free_disk(50_000), kalshi_clock(0):
            for _ in range(3):   # 30 minutes of health checks
                out = maintenance.health()
        disk = [r for r in out["checks"] if r["check"] == "disk"][0]
        self.assertEqual((disk["level"], disk["tripped"]), ("fail", True))
        self.assertEqual(self.phone.titles.count("kal stopped itself"), 1)
        self.assertIn("read-only", self.phone.bodies[0])
        self.assertIn("couldn't be saved", self.phone.bodies[0])

    def test_a_folder_this_user_cant_write_is_only_a_warning(self):
        with mock.patch.object(maintenance.tempfile, "mkstemp", side_effect=PermissionError(errno.EACCES, "Permission denied")):
            row = maintenance.disk_ok()
        self.assertEqual(row["level"], "warn")
        self.assertEqual(row["title"], "kal warning: disk can't be written")   # not "getting full"
        self.assertTrue(config.load()["enabled"])   # no stop


class BusyBooksTests(Base):
    def test_books_locked_for_a_moment_warn_under_their_own_title_and_say_dont_restore(self):
        # Locked for every reader (as by an exclusive lock): the trading check can't read
        # them either, and its alert would blame Kalshi or the internet. One alert, the books'.
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(maintenance, "_open", side_effect=locked), \
                mock.patch.object(db, "connect", side_effect=locked), free_disk(50_000), \
                kalshi_clock(0), self.assertLogs("kal", "WARNING"):
            maintenance.health()
        self.assertEqual(self.phone.titles, ["kal warning: books busy"])
        self.assertNotIn("kal warning: books", self.phone.titles)   # that title means damaged: restore
        body = self.phone.bodies[self.phone.titles.index("kal warning: books busy")]
        self.assertIn("Not damaged: don't restore", body)
        self.assertTrue(config.load()["enabled"])


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
        self.phone.bodies.clear()

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

    def test_what_goes_is_what_it_is_stopped_for_now(self):
        self.unsent("first reason")
        config.update({"disabled_reason": "a later stop"})
        with mock.patch.object(notify, "send", wraps=notify.send) as send:
            self.assertTrue(config.resend_stop())
        self.assertIn("a later stop", send.call_args.args[1])
        self.assertFalse(config.resend_stop())   # owed no more

    def test_a_reason_with_crlf_is_still_resent(self):
        # Raw HTTP error pages (CRLF) end up in reasons: the old text match dropped them.
        self.unsent("basket left half-built: <html>\r\n<h1>502</h1>\r\n</html>")
        self.assertTrue(config.resend_stop())
        self.assertEqual(self.phone.titles, ["kal stopped itself"])

    def test_a_resend_that_finishes_late_keeps_a_newer_owed_alert(self):
        self.unsent("first reason")
        token = config._owed()
        with config._locked():
            config._owe()              # another stop was noted meanwhile
        config._settle_owed(token)     # the older resend finishes
        self.assertIsNotNone(config._owed())

    def test_a_slow_alert_that_gets_through_keeps_a_newer_stops_debt(self):
        # Stop A's alert is slow. Meanwhile the owner presses Reset, the bot stops again for
        # B, and B's alert is refused. A's alert then gets through: B is still owed.
        def reset_then_stop_again():
            config.clear_trip()
            self.phone.status = 503
            with self.assertLogs("kal.notify", "WARNING"):
                config.trip("B: the books are damaged")
            self.phone.status = 200
        self.phone.during = reset_then_stop_again
        config.trip("A: the server clock is off")
        self.assertIsNotNone(config._owed())
        self.assertTrue(config.resend_stop())
        self.assertIn("B: the books are damaged", self.phone.bodies[-1])

    def test_a_stop_just_after_a_reset_keeps_its_debt(self):
        self.unsent("A: lost too much today")
        update = config.update

        def update_then_stop(changes):   # the bot stops again right after the Reset
            out = update(changes)
            self.unsent("B: basket left half-built")
            return out
        with mock.patch.object(config, "update", update_then_stop):
            config.clear_trip()
        self.assertTrue(config.resend_stop())
        self.assertIn("B: basket left half-built", self.phone.bodies[-1])

    def test_a_stop_is_owed_before_its_alert_goes_so_dying_mid_send_still_tells(self):
        class Died(BaseException):   # a restart, the OOM killer, a reboot
            pass

        def die():
            raise Died
        self.phone.during = die
        with self.assertRaises(Died):
            config.trip("the server clock is 40 s behind")
        self.assertEqual(config.load()["disabled_reason"], "the server clock is 40 s behind")
        self.assertTrue(config.resend_stop())   # what the restarted bot's health job does
        self.assertIn("the server clock is 40 s behind", self.phone.bodies[-1])
        self.assertFalse(config.resend_stop())

    def test_an_owed_alert_that_went_is_not_resent_when_its_note_cant_be_removed(self):
        self.unsent("lost too much today")
        with mock.patch.object(config.os, "remove", side_effect=OSError(30, "Read-only file system")):
            sent = [config.resend_stop() for _ in range(4)]
        self.assertEqual(sent, [True, False, False, False])
        self.assertEqual(self.phone.titles, ["kal stopped itself"])

    def test_a_stop_that_cant_be_saved_still_says_so(self):
        full = OSError(28, "No space left on device")
        with mock.patch.object(config, "_write", side_effect=full), self.assertRaises(OSError):
            config.trip("3 failed runs in a row, and the books can't be written")
        self.assertEqual(self.phone.titles, ["kal stopped itself"])
        self.assertIn("couldn't be saved (", self.phone.bodies[-1])
        self.assertIn("No space left on device", self.phone.bodies[-1])
        self.assertFalse(config.resend_stop())   # nothing was saved, so nothing is owed
        self.assertIsNone(config._owed())
        self.assertEqual(len(self.phone.titles), 1)

    def test_a_stop_that_cant_be_saved_keeps_an_older_stops_debt(self):
        self.unsent("R0: lost too much today")   # saved; ntfy down, so its alert is owed
        self.phone.status = 503
        broken = OSError(5, "Input/output error")
        with self.assertLogs("kal.notify", "WARNING"), \
                mock.patch.object(config, "_write", side_effect=broken), self.assertRaises(OSError):
            config.trip("R1: 3 failed runs in a row")   # a run already going trips again
        self.phone.status = 200
        self.assertTrue(config.resend_stop())
        self.assertIn("R0: lost too much today", self.phone.bodies[-1])

    def test_a_stop_that_cant_be_saved_says_so_once_an_hour_not_every_run(self):
        full = OSError(28, "No space left on device")
        with mock.patch.object(config, "_write", side_effect=full):
            for _ in range(3):   # each trade run trips again while the disk is full
                with self.assertRaises(OSError):
                    config.trip("3 failed runs in a row")
            self.assertEqual(len(self.phone.titles), 1)
            config._unsaved_sent[config.home()] -= config.UNSAVED_EVERY   # an hour later
            with self.assertRaises(OSError):
                config.trip("3 failed runs in a row")
        self.assertEqual(len(self.phone.titles), 2)

