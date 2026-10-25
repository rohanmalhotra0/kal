"""Saves the order books the bot looks at, so fill tests can be honest later.

Kalshi keeps no history of order-book depth. Backtests on candles assume we could buy
at the quoted price in any size, which flatters thin markets. Every trade run already
reads the books it needs; this saves the top few levels of each, once per run, in its
own small database (never the money database, so backups stay small and a problem here
can't touch the books that keep the $300 limit). Rows older than KEEP_DAYS are deleted.
"""
import json
import logging
import os
import sqlite3
import time
from contextlib import closing

from . import config, db

KEEP_DAYS = 45
LEVELS = 5            # price levels kept per side
MAX_MB = 300          # stop saving past this size, until the next prune frees room
PRUNE_EVERY = 86400

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (ts REAL, ticker TEXT, yes TEXT, no TEXT);
CREATE INDEX IF NOT EXISTS books_ticker_ts ON books (ticker, ts);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value REAL);
"""


def path(env=None):
    env = env or db.current_env()
    return os.path.join(config.home(), "books.db" if env == "prod" else f"books-{env}.db")


def _levels(side):
    return [[round(float(p), 4), round(float(q), 2)] for p, q in list(side or [])[:LEVELS]]


def save(seen, now=None):
    """seen: {ticker: book} from one run. Never raises: losing a snapshot costs nothing."""
    if not seen:
        return 0
    now = time.time() if now is None else now
    try:
        p = path()
        if os.path.exists(p) and os.path.getsize(p) > MAX_MB * 1024 * 1024:
            _prune(p, now, force=True)
            if os.path.getsize(p) > MAX_MB * 1024 * 1024:
                return 0
        rows = [(now, t, json.dumps(_levels((b or {}).get("yes"))), json.dumps(_levels((b or {}).get("no"))))
                for t, b in seen.items()]
        with closing(sqlite3.connect(p, timeout=10)) as c, c:   # closed, not just committed
            c.executescript(SCHEMA)
            c.executemany("INSERT INTO books VALUES (?, ?, ?, ?)", rows)
        _prune(p, now)
        return len(rows)
    except Exception:
        log.warning("couldn't save order books", exc_info=True)
        return 0


def _prune(p, now, force=False):
    with closing(sqlite3.connect(p, timeout=10)) as c, c:
        c.executescript(SCHEMA)
        last = c.execute("SELECT value FROM meta WHERE key = 'pruned'").fetchone()
        if not force and last and now - last[0] < PRUNE_EVERY:
            return
        gone = c.execute("DELETE FROM books WHERE ts < ?", (now - KEEP_DAYS * 86400,)).rowcount
        c.execute("INSERT OR REPLACE INTO meta VALUES ('pruned', ?)", (now,))
    if gone:
        with closing(sqlite3.connect(p, timeout=10, isolation_level=None)) as c:
            c.execute("VACUUM")


def history(ticker, since=0):
    """[(ts, yes levels, no levels)] for one market, oldest first."""
    p = path()
    if not os.path.exists(p):
        return []
    with closing(sqlite3.connect(p, timeout=10)) as c:
        rows = c.execute("SELECT ts, yes, no FROM books WHERE ticker = ? AND ts >= ? ORDER BY ts",
                         (ticker, since)).fetchall()
    return [(ts, json.loads(y), json.loads(n)) for ts, y, n in rows]
