"""SQLite store for everything the dashboard shows: runs, signals, orders,
paper positions, equity history, backtests and research results."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, ok INTEGER,
    message TEXT, duration REAL);
CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY, ts REAL, strategy TEXT, event TEXT,
    title TEXT, mode TEXT, guaranteed INTEGER, cost REAL, payout_min REAL, exp_profit REAL,
    legs TEXT, status TEXT, note TEXT, meta TEXT);
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, ts REAL, signal_id INTEGER, strategy TEXT,
    ticker TEXT, side TEXT, count REAL, price REAL, filled REAL, status TEXT, order_id TEXT,
    client_order_id TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS paper (id INTEGER PRIMARY KEY, ts REAL, signal_id INTEGER, strategy TEXT,
    event TEXT, ticker TEXT, side TEXT, count REAL, price REAL, cost REAL,
    settled INTEGER DEFAULT 0, result TEXT, pnl REAL, settled_ts REAL);
CREATE TABLE IF NOT EXISTS equity (ts REAL, balance REAL, exposure REAL, equity REAL,
    paper_equity REAL);
CREATE TABLE IF NOT EXISTS settlements (ticker TEXT PRIMARY KEY, settled_ts REAL, result TEXT,
    revenue REAL, cost REAL, pnl REAL);
CREATE TABLE IF NOT EXISTS backtests (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, params TEXT,
    results TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def path(env=None):
    """One database per exchange, so fake-money (demo) fills can never touch the real ledger."""
    env = env or config.load().get("env", "prod")
    return os.path.join(config.home(), "kal.db" if env == "prod" else f"kal-{env}.db")


@contextmanager
def connect():
    conn = sqlite3.connect(path(), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert(table, **row):
    if table not in ("settlements", "kv"):
        row.setdefault("ts", time.time())
    for k, v in row.items():
        if isinstance(v, (list, dict)):
            row[k] = json.dumps(v)
    cols = ", ".join(row)
    with connect() as c:
        cur = c.execute(f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})",
                        list(row.values()))
        return cur.lastrowid


def query(sql, *args):
    with connect() as c:
        return [dict(r) for r in c.execute(sql, args)]


def execute(sql, *args):
    with connect() as c:
        c.execute(sql, args)


def get(key, default=None):
    rows = query("SELECT value FROM kv WHERE key = ?", key)
    return json.loads(rows[0]["value"]) if rows else default


def put(key, value):
    execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", key, json.dumps(value))
