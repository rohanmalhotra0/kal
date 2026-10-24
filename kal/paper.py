"""Paper trading: same signals, simulated fills, real settlement results."""
import time

from . import db
from .client import settled_result


def record(sig, signal_id):
    """Assume a full fill at the limit price (optimistic: real fills can be worse)."""
    for leg in sig.legs:
        db.insert("paper", signal_id=signal_id, strategy=sig.strategy, event=sig.event,
                  ticker=leg.ticker, side=leg.side, count=leg.count, price=leg.price, cost=leg.cost)


def open_rows():
    return db.query("SELECT * FROM paper WHERE settled = 0")


def held():
    return {r["ticker"] for r in open_rows()}


def settle(client):
    rows = open_rows()
    if not rows:
        return 0
    markets = client.markets_by_ticker({r["ticker"] for r in rows})
    n = 0
    for r in rows:
        res = settled_result(markets.get(r["ticker"], {}))
        if res:
            pnl = r["count"] * (1 if res == r["side"] else 0) - r["cost"]
            db.execute("UPDATE paper SET settled = 1, result = ?, pnl = ?, settled_ts = ? WHERE id = ?",
                       res, pnl, time.time(), r["id"])
            n += 1
    return n


def realized():
    return db.query("SELECT COALESCE(SUM(pnl), 0) AS p FROM paper WHERE settled = 1")[0]["p"]


def cash(cfg):
    open_cost = db.query("SELECT COALESCE(SUM(cost), 0) AS c FROM paper WHERE settled = 0")[0]["c"]
    return cfg["paper_bankroll"] + realized() - open_cost


def equity(cfg):
    """Paper bankroll plus realized P&L (open positions counted at cost)."""
    return cfg["paper_bankroll"] + realized()


def stats(strategy=None):
    """Per-strategy paper results: trades, settled, wins, pnl, cost, roi."""
    where, args = ("WHERE strategy = ?", (strategy,)) if strategy else ("", ())
    rows = db.query(f"""SELECT strategy, COUNT(*) AS trades, SUM(settled) AS settled,
        SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
        COALESCE(SUM(CASE WHEN settled = 1 THEN pnl END), 0) AS pnl,
        COALESCE(SUM(CASE WHEN settled = 1 THEN cost END), 0) AS settled_cost
        FROM paper {where} GROUP BY strategy""", *args)
    for r in rows:
        r["roi"] = r["pnl"] / r["settled_cost"] if r["settled_cost"] else None
    return {r["strategy"]: r for r in rows}
