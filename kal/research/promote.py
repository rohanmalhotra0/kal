"""Move edges between candidate, live and retired, using what really happened.

candidate -> live   at least 30 settled paper trades on 20+ series-days, and a cautious
                    lower bound on the ROI of its trades (clustered by series-day, as in
                    the backtest) is above zero. Only when cfg["auto_promote"] is on, and
                    only on the real exchange: demo fills on thin books prove nothing.
live -> retired     its last 30 settled trades (live and paper together) lost money.
                    This runs even with auto_promote off: stopping a loser is never risky.

The bar rises with every look at one idea, and with every idea ever judged (z_look):
one 5% budget for false promotions, spent over looks and ideas, for good. Each idea's
looks, rank and status live in the db as well as in research/*.json (discover.memory),
so a rebuild from backups can't reset them.

The mention strategy has one switch, words.json "promoted", with the same rules.
Paper rows are matched to edges through the signal that made them:
paper.signal_id -> signals.id -> signals.meta["edge"]. Live results come from the
ledger's settlements table (or the client, for markets it hasn't booked yet).
"""
import json
import logging
import math
import statistics
import time
from collections import defaultdict

from .. import config, db
from .. import ledger
from ..client import UnknownSettlement, fee, yes_payout
from ..strategies import favorites
from . import backtest, discover

MIN_PAPER_TRADES = 30   # distinct markets, not rows: re-buying one market adds nothing
MIN_EVENTS = 20         # series-days: trades in one series closing on one day win or lose together
ALPHA = 0.05
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
    return [{"ts": r["ts"], "event": r["event"] or r["ticker"], "ticker": r["ticker"],
             "cost": r["cost"], "pnl": r["pnl"],
             "var": r["count"] ** 2 * r["price"] * (1 - r["price"]), "meta": _meta(r["meta"]), "live": False}
            for r in rows]


def live_trades(strategy, client=None):
    """Filled live orders whose markets have settled, with P&L worked out from our own fills."""
    rows = db.query("""SELECT o.id, o.ts, o.ticker, o.side, o.filled, o.price, s.event, s.meta
        FROM orders o LEFT JOIN signals s ON s.id = o.signal_id
        WHERE o.strategy = ? AND o.filled > 0 ORDER BY o.ts, o.id""", strategy)
    if not rows:
        return []
    results = {r["ticker"]: r["result"] for r in db.query("SELECT ticker, result FROM settlements")}
    unknown = {r["ticker"] for r in rows} - set(results)
    if unknown and client is not None:
        try:
            for tk, m in client.markets_by_ticker(unknown).items():
                try:
                    paid = yes_payout(m)
                except UnknownSettlement:
                    continue
                if paid is not None:
                    results[tk] = "yes" if paid == 1 else "no" if paid == 0 else f"{paid:.4f}"
        except Exception as e:
            log.warning("promote: couldn't look up live results: %s", e)
    out = []
    for r in rows:
        res = results.get(r["ticker"])
        if not res:
            continue
        try:
            per = ledger.payout_of(res, r["side"])
        except (TypeError, ValueError):
            continue
        n, p = r["filled"], r["price"]
        cost = n * p + fee(n, p)
        out.append({"ts": r["ts"], "event": r["event"] or r["ticker"], "ticker": r["ticker"], "cost": cost,
                    "pnl": n * per - cost, "var": n * n * p * (1 - p),
                    "meta": _meta(r["meta"]), "live": True})
    return out


def _group(t):
    """The series and close day a trade counts under, from its signal (meta "group").
    Older signals didn't say: then the event's series and the day it was bought."""
    if t["meta"].get("group"):
        return str(t["meta"]["group"])
    try:
        return favorites.group_of(str(t["event"]).split("-")[0], float(t["ts"]))
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return str(t["event"])


def _clusters(trades):
    """A label per trade. Trades that share an event or a series-day share a label: they
    win or lose together (an hourly series' day rides one price path), so they count once."""
    parent = {}

    def root(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    for t in trades:
        parent[root(("event", t["event"]))] = root(("group", _group(t)))
    return [root(("event", t["event"])) for t in trades]


def _stats(trades, z):
    labels = _clusters(trades)
    roi, lo = backtest.roi_stats([(g, t["cost"], t["pnl"], t["var"]) for g, t in zip(labels, trades)], z)
    paper = [(g, t) for g, t in zip(labels, trades) if not t["live"]]
    return {"trades": len(trades),
            "paper_trades": len({t.get("ticker") or (t["event"], i) for i, (_, t) in enumerate(paper)}),
            "events": len({g for g, _ in paper}),
            "pnl": round(sum(t["pnl"] for t in trades), 2), "roi": roi, "roi_lo": lo}


def z_look(look, tries=1, rank=None):
    """z for the `look`-th time we test the same idea. Spending alpha * 6 / (pi^2 * k^2) on
    look k keeps the total false-promotion chance under alpha however many days we
    re-check (a union bound), unlike a fixed z daily.

    `rank` i (this is the i-th idea ever judged, see _rank) spends alpha across ideas over
    time the same way: 6 / (pi^2 * i^2) of it. Otherwise ideas that show up one at a time
    would each get all of it: 20 dud ideas in a year, a 64% chance one goes live by luck.
    The `tries` ideas judged together split it too; the stricter of the two counts."""
    share = 1 / max(1, tries)
    if rank:
        share = min(share, 6 / (math.pi ** 2 * max(1, rank) ** 2))
    alpha = ALPHA * share * 6 / (math.pi ** 2 * max(1, look) ** 2)
    return statistics.NormalDist().inv_cdf(1 - alpha)


def _rank(key):
    """This idea's place among every idea ever judged (1, 2, 3, ...), kept in the db. Given
    at its first look and never changed, so no idea gets a fresh share of the budget."""
    mem = discover.memory()
    rank = discover._int((mem.get(key) or {}).get("rank"))
    if not rank:
        rank = 1 + max((discover._int(m.get("rank")) for m in mem.values() if isinstance(m, dict)), default=0)
        discover.remember(key, rank=rank)
    return rank


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


def _ready(stats, env=None):
    """Proof enough for real money. Never on the demo exchange: practice fills on its thin
    books say nothing about the real one."""
    env = env or config.load().get("env", "prod")
    return (env == "prod" and stats["paper_trades"] >= MIN_PAPER_TRADES and stats["events"] >= MIN_EVENTS
            and stats["roi_lo"] is not None and stats["roi_lo"] > 0)


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
    # Every candidate checked is a separate try, now or ever, so each needs more confidence.
    tries = [e for e in edges if e.get("status") == "candidate"
             and sum(not t["live"] for t in by_edge[e["id"]]) >= MIN_PAPER_TRADES]
    notes, stamp = [], discover._iso(time.time())
    for e in edges:
        trades = by_edge.get(e["id"], [])
        if not trades:
            continue
        if e.get("status") == "candidate" and e in tries:
            e["looks"] = int(e.get("looks", 0)) + 1
            e["rank"] = _rank(f"favorites:{e['id']}")
        stats = _stats(trades, z_look(e.get("looks", 1), len(tries), e.get("rank")))
        e["results"] = stats
        if e.get("status") == "candidate" and _ready(stats, cfg.get("env")):
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
    if not words.get("promoted") and sum(not t["live"] for t in trades) >= MIN_PAPER_TRADES:
        words["looks"] = int(words.get("looks", 0)) + 1
        words["rank"] = _rank("mentions")
    stats = _stats(trades, z_look(words.get("looks", 1), rank=words.get("rank")))
    words["results"] = stats
    notes, stamp = [], discover._iso(time.time())
    backtest_lo = words.get("roi_lo")
    if not words.get("promoted"):
        if _ready(stats, cfg.get("env")) and backtest_lo is not None and backtest_lo > 0:
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
