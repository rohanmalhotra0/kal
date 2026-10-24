"""The bot's own books, kept separate from whatever else is in the Kalshi account.

The rule: the bot may never lose more than the capital it was given (`budget`).
So its spendable money is budget + its realized profit (profit only counts if
reinvest_profits is on; losses always count), minus what's tied up in its open
positions. Built only from our own fills plus public settlement results, so a
deposit, a manual trade, or a schema change in Kalshi's settlement feed can't
inflate it.
"""
import time

from . import db
from .client import fee, price, settled_result


def _open_fills():
    """Filled live orders on markets we haven't seen settle yet, grouped by ticker and side."""
    return db.query("""SELECT o.ticker, o.side, SUM(o.filled) AS count,
        SUM(o.filled * o.price) AS notional, GROUP_CONCAT(o.filled || ':' || o.price) AS parts
        FROM orders o LEFT JOIN settlements s ON s.ticker = o.ticker
        WHERE o.filled > 0 AND s.ticker IS NULL GROUP BY o.ticker, o.side""")


def _cost(row):
    """Notional plus the fee on each fill. Recorded at our limit price, so if Kalshi filled us
    at a better price this overstates cost: the safe direction."""
    total = 0.0
    for part in (row["parts"] or "").split(","):
        if part:
            n, p = part.split(":")
            total += float(n) * float(p) + fee(float(n), float(p))
    return total


def open_positions():
    rows = _open_fills()
    for r in rows:
        r["cost"] = _cost(r)
    return rows


def open_cost():
    return sum(r["cost"] for r in open_positions())


def realized():
    return db.query("SELECT COALESCE(SUM(pnl), 0) AS p FROM settlements")[0]["p"]


def settle(client):
    """Book P&L for bot positions whose markets have settled. Returns how many settled."""
    rows = open_positions()
    if not rows:
        return 0
    markets = client.markets_by_ticker({r["ticker"] for r in rows})
    by_ticker = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)
    n = 0
    for ticker, legs in by_ticker.items():
        res = settled_result(markets.get(ticker, {}))
        if not res:
            continue
        revenue = sum(r["count"] for r in legs if r["side"] == res)
        cost = sum(r["cost"] for r in legs)
        db.insert("settlements", ticker=ticker, settled_ts=time.time(), result=res,
                  revenue=revenue, cost=cost, pnl=revenue - cost)
        n += 1
    return n


def capital(cfg):
    """What the bot has to work with: its starting budget plus (or minus) what it has made."""
    r = realized()
    return cfg["budget"] + (r if cfg.get("reinvest_profits", True) else min(0.0, r))


def available(cfg):
    return max(0.0, capital(cfg) - open_cost())


def equity(client, cfg):
    """Capital with open positions marked at their best bid instead of at cost."""
    rows = open_positions()
    if not rows:
        return capital(cfg)
    markets = client.markets_by_ticker({r["ticker"] for r in rows})
    value = sum(r["count"] * price(markets.get(r["ticker"], {}), f"{r['side']}_bid") for r in rows)
    return capital(cfg) - open_cost() + value
