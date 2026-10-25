"""Paper trading: same signals, simulated fills, real settlement results."""
import json
import logging
import time

from . import db
from .client import UnknownSettlement, yes_payout

log = logging.getLogger(__name__)


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
        try:
            paid = yes_payout(markets.get(r["ticker"], {}))
        except UnknownSettlement as e:
            # Paper isn't money: close it out without a result, so it never counts toward
            # promotion, rather than guess.
            log.warning("paper: %s", e)
            db.execute("UPDATE paper SET settled = 1, result = 'unknown', pnl = NULL, settled_ts = ? WHERE id = ?",
                       time.time(), r["id"])
            continue
        if paid is None:
            continue
        per = paid if r["side"] == "yes" else 1 - paid
        result = "yes" if paid == 1 else "no" if paid == 0 else f"{paid:.4f}"
        db.execute("UPDATE paper SET settled = 1, result = ?, pnl = ?, settled_ts = ? WHERE id = ?",
                   result, r["count"] * per - r["cost"], time.time(), r["id"])
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


def open_by_event():
    rows = db.query("SELECT event, COALESCE(SUM(cost), 0) AS c FROM paper WHERE settled = 0 GROUP BY event")
    return {r["event"]: r["c"] for r in rows}


def open_cost():
    return db.query("SELECT COALESCE(SUM(cost), 0) AS c FROM paper WHERE settled = 0")[0]["c"]


def peak_equity():
    rows = db.query("SELECT MAX(paper_equity) AS p FROM equity")
    return rows[0]["p"] if rows and rows[0]["p"] is not None else None


def open_cost_by_kind():
    """{meta paper_kind: open paper cost}, for kinds of signal that may hold only a share of
    the paper money (see Signal meta "paper_share")."""
    out = {}
    for r in db.query("""SELECT p.cost, sg.meta FROM paper p JOIN signals sg ON sg.id = p.signal_id
            WHERE p.settled = 0"""):
        try:
            kind = (json.loads(r["meta"] or "{}") or {}).get("paper_kind")
        except (TypeError, ValueError, AttributeError):
            continue
        if kind:
            out[kind] = out.get(kind, 0.0) + (r["cost"] or 0.0)
    return out


def open_directional():
    rows = db.query("""SELECT COALESCE(SUM(p.cost), 0) AS c FROM paper p
        LEFT JOIN signals sg ON sg.id = p.signal_id
        WHERE p.settled = 0 AND COALESCE(sg.guaranteed, 0) = 0""")
    return rows[0]["c"]
