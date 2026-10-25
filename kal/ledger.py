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
    """Capital with open positions marked at their best bid instead of at cost.

    A market with no bid at all (thin books, or a lookup that failed) is valued at cost,
    not $0: no buyer right now isn't a loss, and treating it as one would trip the daily
    loss limit for nothing. Real losses still land when the market settles."""
    rows = open_positions()
    if not rows:
        return capital(cfg)
    markets = client.markets_by_ticker({r["ticker"] for r in rows})
    value = 0.0
    for r in rows:
        bid = price(markets.get(r["ticker"], {}), f"{r['side']}_bid")
        value += r["count"] * bid if bid > 0 else r["cost"]
    return capital(cfg) - open_cost() + value


def open_by_event():
    """Dollars in open bot positions per event (from the signal each order belonged to)."""
    rows = db.query("""SELECT COALESCE(sg.event, o.ticker) AS event, o.filled, o.price
        FROM orders o LEFT JOIN settlements s ON s.ticker = o.ticker
        LEFT JOIN signals sg ON sg.id = o.signal_id
        WHERE o.filled > 0 AND s.ticker IS NULL""")
    out = {}
    for r in rows:
        out[r["event"]] = out.get(r["event"], 0.0) + r["filled"] * r["price"] + fee(r["filled"], r["price"])
    return out


def unresolved_orders():
    """Orders whose outcome we don't know: the send may or may not have reached Kalshi."""
    return db.query("SELECT * FROM orders WHERE status IN ('sending', 'unknown')")


def open_directional():
    """Dollars in open bot positions that came from bets, not locked-in arbitrage."""
    rows = db.query("""SELECT o.filled, o.price FROM orders o
        LEFT JOIN settlements s ON s.ticker = o.ticker
        LEFT JOIN signals sg ON sg.id = o.signal_id
        WHERE o.filled > 0 AND s.ticker IS NULL AND COALESCE(sg.guaranteed, 0) = 0""")
    return sum(r["filled"] * r["price"] + fee(r["filled"], r["price"]) for r in rows)


def peak_equity():
    rows = db.query("SELECT MAX(equity) AS p FROM equity WHERE balance > 0 OR exposure > 0 OR equity > 0")
    return rows[0]["p"] if rows and rows[0]["p"] is not None else None
