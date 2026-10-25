"""Move edges between candidate, live and retired, using what really happened.

candidate -> live   at least 30 settled paper trades, and a cautious lower bound on the
                    ROI of its trades (event-clustered, as in the backtest) is above zero.
                    Only when cfg["auto_promote"] is on.
live -> retired     its last 30 settled trades (live and paper together) lost money.
                    This runs even with auto_promote off: stopping a loser is never risky.

The mention strategy has one switch, words.json "promoted", with the same rules.
Paper rows are matched to edges through the signal that made them:
paper.signal_id -> signals.id -> signals.meta["edge"]. Live results come from the
ledger's settlements table (or the client, for markets it hasn't booked yet).
"""
import json
import logging
import time
from collections import defaultdict

from .. import db
from ..client import fee, settled_result
from . import backtest, discover

MIN_PAPER_TRADES = 30
WINDOW = 30          # judge a live edge on this many of its latest settled trades
MIN_TO_JUDGE = 10    # fewer than this is too few to call it a loser

log = logging.getLogger(__name__)


def _meta(raw):
    try:
        m = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return m if isinstance(m, dict) else {}


def paper_trades(strategy):
    rows = db.query("""SELECT p.id, p.ts, p.event, p.ticker, p.count, p.price, p.cost, p.pnl, s.meta
        FROM paper p LEFT JOIN signals s ON s.id = p.signal_id
        WHERE p.strategy = ? AND p.settled = 1 AND p.pnl IS NOT NULL ORDER BY p.ts, p.id""", strategy)
    return [{"ts": r["ts"], "event": r["event"] or r["ticker"], "cost": r["cost"], "pnl": r["pnl"],
             "var": r["count"] ** 2 * r["price"] * (1 - r["price"]), "meta": _meta(r["meta"]), "live": False}
            for r in rows]


def live_trades(strategy, client=None):
    """Filled live orders whose markets have settled, with P&L worked out from our own fills."""
    rows = db.query("""SELECT o.id, o.ts, o.ticker, o.side, o.filled, o.price, s.event, s.meta
        FROM orders o LEFT JOIN signals s ON s.id = o.signal_id
        WHERE o.strategy = ? AND o.filled > 0 ORDER BY o.ts, o.id""", strategy)
    if not rows:
        return []
    results = {r["ticker"]: r["result"] for r in
               db.query("SELECT ticker, result FROM settlements WHERE result IN ('yes', 'no')")}
    unknown = {r["ticker"] for r in rows} - set(results)
    if unknown and client is not None:
        try:
            for tk, m in client.markets_by_ticker(unknown).items():
                if settled_result(m):
                    results[tk] = settled_result(m)
        except Exception as e:
            log.warning("promote: couldn't look up live results: %s", e)
    out = []
    for r in rows:
        res = results.get(r["ticker"])
        if not res:
            continue
        n, p = r["filled"], r["price"]
        cost = n * p + fee(n, p)
        out.append({"ts": r["ts"], "event": r["event"] or r["ticker"], "cost": cost,
                    "pnl": n * (res == r["side"]) - cost, "var": n * n * p * (1 - p),
                    "meta": _meta(r["meta"]), "live": True})
    return out


def _stats(trades, z):
    roi, lo = backtest.roi_stats([(t["event"], t["cost"], t["pnl"], t["var"]) for t in trades], z)
    return {"trades": len(trades), "paper_trades": sum(not t["live"] for t in trades),
            "pnl": round(sum(t["pnl"] for t in trades), 2), "roi": roi, "roi_lo": lo}


def _losing(trades):
    """(lost?, dollars) over the latest WINDOW settled trades."""
    window = sorted(trades, key=lambda t: t["ts"])[-WINDOW:]
    pnl = sum(t["pnl"] for t in window)
    return len(window) >= MIN_TO_JUDGE and pnl < 0, -pnl, len(window)


def _go_live(cfg, strategy):
    """Promotion only lifts the paper-only flag. Real money also needs the strategy's mode
    to be "live", which is the owner's switch, so say which one it is."""
    if (cfg.get("strategies") or {}).get(strategy, {}).get("mode") == "live":
        return "It now uses real money."
    return f"Set {strategy} to live to use real money."


def _ready(stats):
    return stats["paper_trades"] >= MIN_PAPER_TRADES and stats["roi_lo"] is not None and stats["roi_lo"] > 0


def apply(cfg, client=None):
    """Promote and retire. Returns short, plain notes on what changed."""
    return _edges(cfg, client) + _mentions(cfg, client)


def _edges(cfg, client):
    edges = discover.load_edges()
    if not edges:
        return []
    by_edge = defaultdict(list)
    for t in paper_trades("favorites") + live_trades("favorites", client):
        if t["meta"].get("edge"):
            by_edge[t["meta"]["edge"]].append(t)
    # Every candidate checked now is a separate try, so each needs more confidence.
    tries = [e for e in edges if e.get("status") == "candidate"
             and sum(not t["live"] for t in by_edge[e["id"]]) >= MIN_PAPER_TRADES]
    z = backtest.z_for(len(tries))
    notes, stamp = [], discover._iso(time.time())
    for e in edges:
        trades = by_edge.get(e["id"], [])
        if not trades:
            continue
        stats = _stats(trades, z)
        e["results"] = stats
        if e.get("status") == "candidate" and _ready(stats):
            if cfg.get("auto_promote"):
                e.update(status="live", promoted_at=stamp,
                         note=f"won on paper: {stats['paper_trades']} trades, worst case {stats['roi_lo']:+.1%}")
                notes.append(f"{e['id']} won on paper: {stats['paper_trades']} trades, worst case "
                             f"{stats['roi_lo']:+.1%}. {_go_live(cfg, 'favorites')}")
            else:
                notes.append(f"{e['id']} is ready for real money. Auto promote is off.")
        elif e.get("status") == "live":
            lost, dollars, n = _losing(trades)
            if lost:
                e.update(status="retired", retired_by="results", retired_at=stamp,
                         note=f"lost ${dollars:.2f} in its last {n} trades")
                notes.append(f"{e['id']} stopped. It lost ${dollars:.2f} in its last {n} trades.")
    discover.save_edges(edges)
    return notes


def _mentions(cfg, client):
    words = discover.load_words()
    if not words:
        return []
    trades = paper_trades("mentions") + live_trades("mentions", client)
    stats = _stats(trades, backtest.z_for(1))
    words["results"] = stats
    notes, stamp = [], discover._iso(time.time())
    backtest_lo = words.get("roi_lo")
    if not words.get("promoted"):
        if _ready(stats) and backtest_lo is not None and backtest_lo > 0:
            if cfg.get("auto_promote"):
                words.update(promoted=True, promoted_at=stamp,
                             note=f"won on paper: {stats['paper_trades']} trades, worst case {stats['roi_lo']:+.1%}")
                notes.append(f"Mention words won on paper: {stats['paper_trades']} trades, worst case "
                             f"{stats['roi_lo']:+.1%}. {_go_live(cfg, 'mentions')}")
            else:
                notes.append("Mention words are ready for real money. Auto promote is off.")
    else:
        lost, dollars, n = _losing(trades)
        if lost:
            words.update(promoted=False, demoted_at=stamp, note=f"lost ${dollars:.2f} in the last {n} trades")
            notes.append(f"Mention words back to paper. They lost ${dollars:.2f} in the last {n} trades.")
    discover.save_words(words)
    return notes
