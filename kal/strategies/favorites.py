"""Favorites: buy the side of a market whose price sits in a range that past markets
say wins more often than the price implies.

The ranges ("edges") come from kal.research (edges.json). A new edge is traded on
paper only (Signal.meta "paper_only") until kal.research.promote marks it "live",
after it made money in paper trading too. Bets are sized with a cautious win chance:
the price paid times (1 + the backtest's worst-case ROI), never the hoped-for average.
"""
import logging
import time
from collections import defaultdict

from .. import sizing
from ..client import fee_per_contract, num, price, ts
from .base import Leg, Signal, Strategy

MAX_SPREAD = 0.02       # wider than this and the "price" isn't really known
MIN_VOLUME_24H = 200    # thin markets: our order would be most of the trading
ACTIVE = ("candidate", "live")

log = logging.getLogger(__name__)


def edges_of(research):
    """Edge entries from edges.json: a list, or {"edges": [...]}, or {id: entry}."""
    raw = (research or {}).get("edges") or []
    if isinstance(raw, dict):
        raw = raw.get("edges", raw)
    if isinstance(raw, dict):
        raw = [dict(v, id=v.get("id", k)) for k, v in raw.items() if isinstance(v, dict)]
    return [e for e in raw if isinstance(e, dict)]


def active_edges(research):
    """Candidate and live edges with every field we need. Anything malformed is skipped."""
    out = []
    for e in edges_of(research):
        try:
            lo, hi = float(e["lo"]), float(e["hi"])
            hours, roi_lo = float(e["hours_before_close"]), float(e["roi_lo"])
        except (KeyError, TypeError, ValueError):
            continue
        if e.get("status") in ACTIVE and e.get("series") and e.get("id") and 0 <= lo < hi <= 1 and hours > 0:
            out.append({**e, "lo": lo, "hi": hi, "hours_before_close": hours, "roi_lo": roi_lo})
    return out


def in_series(m, series):
    """Kalshi event tickers start with their series ticker (KXHIGHNY-25OCT24)."""
    if m.get("series_ticker"):
        return m["series_ticker"] == series
    ev = m.get("event_ticker") or ""
    return ev == series or ev.startswith(series + "-")


def is_open(m):
    return bool(m) and m.get("status") in ("active", "open")


def closes_within(m, now, hours):
    try:
        close = ts(m["close_time"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    return now < close <= now + hours * 3600


def quote(client, ticker, side):
    """(ask, contracts at the ask, best bid) for one side, or None.
    The book only has bids: the YES ask is 1 - best NO bid, and the other way round."""
    book = client.orderbook(ticker)
    other = book.get("no" if side == "yes" else "yes") or []
    own = book.get(side) or []
    if not other:
        return None
    p, size = max(other)
    return round(1 - p, 4), size, (max(own)[0] if own else 0.0)


def tight(ask, bid):
    return 0 < bid < ask < 1 and ask - bid <= MAX_SPREAD + 1e-9


class Favorites(Strategy):
    name = "favorites"
    title = "Favorites"
    kind = "statistical"
    description = ("Buy contracts in price ranges where past markets won more often "
                   "than the price said. New ranges trade on paper first.")

    def scan(self, ctx):
        now = int(time.time())
        by_series = defaultdict(list)
        for e in active_edges(ctx.research):
            by_series[e["series"]].append(e)
        best = {}  # event -> signal: markets in one event move together, so one bet each
        for series, edges in sorted(by_series.items()):
            hours = max(e["hours_before_close"] for e in edges)
            try:
                markets = ctx.client.markets(status="open", series_ticker=series, min_close_ts=now,
                                             max_close_ts=now + int(hours * 3600))
                for m in markets:
                    sig = self._consider(ctx, m, series, edges, now)
                    if sig and (sig.event not in best or sig.exp_profit > best[sig.event].exp_profit):
                        best[sig.event] = sig
            except Exception as e:
                log.warning("favorites: %s failed: %s", series, e)
        return sorted(best.values(), key=lambda s: -s.exp_profit)

    def _consider(self, ctx, m, series, edges, now):
        tk = m.get("ticker")
        if not tk or tk in ctx.held or not in_series(m, series) or not is_open(m):
            return None
        if num(m, "volume_24h") < MIN_VOLUME_24H:
            return None
        picks = []
        for side in ("yes", "no"):
            ask, bid = price(m, f"{side}_ask"), price(m, f"{side}_bid")
            if not tight(ask, bid):
                continue
            for e in edges:
                if e["lo"] <= ask < e["hi"] and closes_within(m, now, e["hours_before_close"]):
                    picks.append((side, e))
        if len({side for side, _ in picks}) != 1:
            return None  # nothing matched, or two edges disagree about this market: stay out
        side, e = max(picks, key=lambda x: x[1]["roi_lo"])
        return self._signal(ctx, m, side, e)

    def _signal(self, ctx, m, side, e):
        """Price from the order book (fresh, with depth), then size."""
        tk = m["ticker"]
        q = quote(ctx.client, tk, side)
        if not q:
            return None
        ask, depth, bid = q
        if not (e["lo"] <= ask < e["hi"]) or not tight(ask, bid):
            return None
        cost_each = ask + fee_per_contract(ask)
        p = min(cost_each * (1 + e["roi_lo"]), 0.999)
        cap = min(ctx.cfg["max_per_trade"], ctx.cfg["max_per_event"], ctx.budget_left)
        count = min(sizing.contracts(p, ask, ctx.bankroll, ctx.cfg["kelly_fraction"], cap), int(depth))
        while count > 0 and Leg(tk, side, ask, count).cost > cap + 1e-9:
            count -= 1  # fees round up to the cent, so sizing's estimate can be a bit low
        if count < 1:
            return None
        sig = Signal(self.name, m.get("event_ticker") or tk, m.get("title") or tk,
                     [Leg(tk, side, ask, count)],
                     note=f"{side.upper()} at ${ask:.2f}: range {e['lo']:.2f}-{e['hi']:.2f} won before "
                          f"(worst case {e['roi_lo']:+.1%})",
                     meta={"edge": e["id"], "paper_only": e["status"] != "live"})
        sig.exp_profit = count * p - sig.cost
        return sig

    def recheck(self, ctx, sig):
        """Right before a live order: is the edge still live and the price still in range?"""
        e = {x["id"]: x for x in active_edges(ctx.research)}.get(sig.meta.get("edge"))
        if not e or e["status"] != "live":
            return None
        leg = sig.legs[0]
        m = ctx.client.market(leg.ticker)
        if not is_open(m) or not closes_within(m, int(time.time()), e["hours_before_close"]):
            return None
        return self._signal(ctx, {**m, "ticker": leg.ticker, "event_ticker": sig.event, "title": sig.title},
                            leg.side, e)

    def proven(self, ctx):
        live = [e for e in active_edges(ctx.research) if e["status"] == "live"]
        if live:
            return True, f"{len(live)} price range(s) made money in paper trading"
        return False, "no price range has passed paper trading yet"


STRATEGY = Favorites()
