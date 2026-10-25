import os
import sqlite3
from unittest import mock

from kal import books, config, db, engine
from tests.helpers import TempHome, orders_proven
from tests.test_core import fake_with_arb

DAY = 86400


class BooksTests(TempHome):
    def test_a_run_saves_each_book_it_read_once(self):
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"basket_arb": {"mode": "paper"}}})
        orders_proven()
        c = fake_with_arb()
        s = engine.run_once(c)
        self.assertGreaterEqual(s["books_saved"], 4)
        rows = books.history("EV1-0")
        self.assertEqual(len(rows), 1)  # scan and recheck both read it: saved once per run
        ts, yes, no = rows[0]
        self.assertEqual(yes, [[0.55, 40.0]])
        self.assertEqual(no, [])

    def test_only_the_top_levels_are_kept(self):
        book = {"yes": [(0.5 - i / 100, 10 + i) for i in range(12)], "no": [(0.4, 3)]}
        books.save({"T": book}, now=1000)
        _, yes, no = books.history("T")[0]
        self.assertEqual(len(yes), books.LEVELS)
        self.assertEqual(yes[0], [0.5, 10.0])
        self.assertEqual(no, [[0.4, 3.0]])

    def test_old_snapshots_are_deleted(self):
        books.save({"T": {"yes": [(0.5, 1)], "no": []}}, now=1000)
        books.save({"T": {"yes": [(0.6, 1)], "no": []}}, now=1000 + (books.KEEP_DAYS + 2) * DAY)
        self.assertEqual([round(y[0][0], 2) for _, y, _ in books.history("T")], [0.6])

    def test_never_touches_the_money_database(self):
        books.save({"T": {"yes": [(0.5, 1)], "no": []}})
        db.put("x", 1)  # make sure kal.db exists
        with sqlite3.connect(db.path()) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("books", tables)
        self.assertTrue(os.path.exists(books.path()))
        from kal import maintenance
        self.assertNotIn(books.path(), maintenance._dbs())  # never in the daily backups
        config.update({"env": "demo"})
        self.assertTrue(books.path().endswith("books-demo.db"))

    def test_a_failure_never_stops_a_run(self):
        os.makedirs(books.path())  # a folder where the file should be: sqlite can't open it
        self.assertEqual(books.save({"T": {"yes": [], "no": []}}), 0)
        with mock.patch("kal.books.sqlite3.connect", side_effect=OSError("disk full")):
            self.assertEqual(books.save({"T": {"yes": [], "no": []}}), 0)

    def test_stops_saving_when_too_big(self):
        books.save({"T": {"yes": [(0.5, 1)], "no": []}}, now=1000)
        with mock.patch.object(books, "MAX_MB", 0):
            self.assertEqual(books.save({"U": {"yes": [(0.5, 1)], "no": []}}, now=1001), 0)
        self.assertEqual(books.history("U"), [])
