"""Favorites: buy the side of a market whose price sits in a range that past markets
say wins more often than the price implies.

The ranges ("edges") come from kal.research (edges.json). A new edge is traded on
paper only (Signal.meta "paper_only") until kal.research.promote marks it "live",
after it made money in paper trading too. Bets are sized with a cautious win chance:
the price paid times (1 + the backtest's worst-case ROI), never the hoped-for average.

Bets in one series that close on one UTC day tend to win or lose together: an hourly
BTC series is 24 events a day on one price path. So together they may cost at most
GROUP_SHARE of capital, and kal.research counts them as one.
"""
import logging
import math
import time
from collections import defaultdict
from datetime import datetime, timezone

from .. import sizing
from ..client import fee, num, price, ts
from .base import Leg, Signal, Strategy, held_dollars, scheduled_end

MAX_SPREAD = 0.02       # wider than this and the "price" isn't really known
MIN_VOLUME_24H = 200    # thin markets: our order would be most of the trading
ACTIVE = ("candidate", "live")
FULL_BET = 10.0         # dollars in a full-size bet (max_per_trade's default): backtests price this
GROUP_SHARE = 0.10      # bets in one series closing on one day: at most this share of capital together

log = logging.getLogger(__name__)


def full_size(p, dollars=FULL_BET):
    """Contracts in a full-size bet of `dollars` at price p."""
    return max(1, int(dollars // sizing.unit_cost(p)))


def cost_each(p):
    """What one contract costs in a full-size bet, with Kalshi's fee rounded up per order.
    kal.research.backtest prices its trades the same way, so the cautious win chance below
    matches what was tested. At 95c+ the rounding is a big share of the edge."""
    n = full_size(p)
    return (n * p + fee(n, p)) / n


def group_of(series, close):
    """'KXBTCD:2026-10-25': a series and the UTC day a bet in it closes."""
    return f"{series}:{datetime.fromtimestamp(close, timezone.utc).date().isoformat()}"


def market_group(m, series):
    """The series-day of an open market, or None if its close time can't be read."""
    try:
        return group_of(series, ts(m["close_time"]))
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError):
        return None


def bet_cap(ctx):
    """The most one bet may cost (all of it can be lost)."""
    return min(ctx.cfg["max_per_trade"], ctx.cfg["max_per_event"])


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


TIME_BAND = 1.0  # hours: the edge was measured at one time before close, so trade near it


def near_tested_time(end, now, hours):
    """True if the market is due to end (`end`, its scheduled end) about `hours` from now:
    the moment the backtest measured. A 92c contract 5 minutes before the end is a
    different bet from one 6 hours out."""
    if end is None:
        return False
    left = (end - now) / 3600
    return max(0.0, hours - TIME_BAND) < left <= hours


def market_ends(client, series, markets):
    """{ticker: scheduled end or None} for these open markets (base.scheduled_end), with
    their events' strike dates: the same anchoring the backtest uses. Not close_time: on
    a game that's a deadline two days after it, long after the market really closes."""
    strikes = {}
    try:
        for ev in client.events(status="open", series_ticker=series):
            if isinstance(ev, dict) and ev.get("event_ticker") and ev.get("strike_date"):
                strikes[ev["event_ticker"]] = ev["strike_date"]
    except Exception as e:  # without them, weather and indexes just don't match the window
        log.warning("favorites: %s events failed: %s", series, e)
    return {m.get("ticker"): scheduled_end(m, strikes.get(m.get("event_ticker"))) for m in markets}


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
        best = {}  # event -> (signal, market, side, edge): markets in one event move together, so one bet each
        held = defaultdict(float)  # series-day -> dollars already held there (anyone's)
        held_any_day = defaultdict(float)  # series -> held with no known end: they count on every day
        for series, edges in sorted(by_series.items()):
            try:
                # Every open market: a game's close_time is days after its scheduled end.
                markets = ctx.client.markets(status="open", series_ticker=series, min_close_ts=now)
            except Exception as e:
                log.warning("favorites: %s failed: %s", series, e)
                continue
            ends = market_ends(ctx.client, series, markets)
            # Held markets first, so a failure below can't hide them from their day's cap.
            for m in markets:
                if m.get("ticker") in ctx.held and in_series(m, series):
                    end = ends.get(m.get("ticker"))
                    if end:
                        held[group_of(series, end)] += held_dollars(ctx, m["ticker"])
                    else:
                        held_any_day[series] += held_dollars(ctx, m["ticker"])
            picks = {}
            try:
                for m in markets:
                    pick = self._consider(ctx, m, series, edges, now, ends.get(m.get("ticker")))
                    if pick and (pick[0].event not in picks or pick[0].exp_profit > picks[pick[0].event][0].exp_profit):
                        picks[pick[0].event] = pick
            except Exception as e:
                log.warning("favorites: %s failed, none of its bets this run: %s", series, e)
                continue
            best.update(picks)
        # One series-day rides one price path, so its bets share one cap. Each market already
        # held there counts what it cost (held_dollars). Best first, so the cap leaves room.
        found, spent = [], defaultdict(float)
        for sig, m, side, e in sorted(best.values(), key=lambda x: -x[0].exp_profit):
            g = sig.meta["group"]
            room = GROUP_SHARE * ctx.bankroll - held[g] - held_any_day[e["series"]] - spent[g]
            if sig.cost > room + 1e-9:
                sig = self._signal(ctx, m, side, e, sig.meta["closes"], cap=room)  # smaller, to fit (or none)
            if sig:
                found.append(sig)
                spent[g] += sig.cost
        return sorted(found, key=lambda s: -s.exp_profit)

    def _consider(self, ctx, m, series, edges, now, end):
        """(signal, market, side, edge) for the market, or None. `end`: its scheduled end."""
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
                # Only the side the edge was proven on. An edge with no side came from pooled
                # YES+NO data, which can hide a losing side, so it's never traded.
                if e.get("side") != side:
                    continue
                if e["lo"] <= ask < e["hi"] and near_tested_time(end, now, e["hours_before_close"]):
                    picks.append((side, e))
        if len({side for side, _ in picks}) != 1:
            return None  # nothing matched, or two edges disagree about this market: stay out
        side, e = max(picks, key=lambda x: x[1]["roi_lo"])
        sig = self._signal(ctx, m, side, e, end)
        return (sig, m, side, e) if sig else None

    def _signal(self, ctx, m, side, e, end, cap=math.inf, max_count=None):
        """Price from the order book (fresh, with depth), then size. Never over `cap`
        dollars or `max_count` contracts. `end`: the market's scheduled end."""
        tk = m["ticker"]
        q = quote(ctx.client, tk, side)
        if not q:
            return None
        ask, depth, bid = q
        if not (e["lo"] <= ask < e["hi"]) or not tight(ask, bid):
            return None
        if end is None:
            return None  # no scheduled end: can't tell which day's cap it falls under
        group = group_of(e["series"], end)
        # Cautious: the win chance that pays exactly the backtest's worst-case ROI, on the
        # cost the backtest charged (a full-size order, fee rounded up).
        p = min(cost_each(ask) * (1 + e["roi_lo"]), 0.999)
        cap = min(bet_cap(ctx), ctx.budget_left, cap)
        count = min(sizing.contracts(p, ask, ctx.bankroll, ctx.cfg["kelly_fraction"], cap), int(depth))
        if max_count is not None:
            count = min(count, max_count)
        while count > 0 and Leg(tk, side, ask, count).cost > cap + 1e-9:
            count -= 1  # fees round up to the cent, so sizing's estimate can be a bit low
        if count < 1:
            return None
        leg = Leg(tk, side, ask, count)
        if count * p - leg.cost <= 0:
            return None  # a small order pays more fee per contract: here it eats the whole edge
        sig = Signal(self.name, m.get("event_ticker") or tk, m.get("title") or tk, [leg],
                     note=f"{side.upper()} at ${ask:.2f}: range {e['lo']:.2f}-{e['hi']:.2f} won before "
                          f"(worst case {e['roi_lo']:+.1%})",
                     meta={"edge": e["id"], "group": group, "closes": end, "paper_only": e["status"] != "live"})
        sig.exp_profit = count * p - sig.cost
        return sig

    def recheck(self, ctx, sig):
        """Right before a live order: is the edge still live and the price still in range?
        Re-priced, never bigger than scanned, so the series-day cap still holds."""
        e = {x["id"]: x for x in active_edges(ctx.research)}.get(sig.meta.get("edge"))
        if not e or e["status"] != "live":
            return None
        try:
            end = float(sig.meta["closes"])
        except (KeyError, TypeError, ValueError):
            return None
        leg = sig.legs[0]
        m = ctx.client.market(leg.ticker)
        if not is_open(m) or not near_tested_time(end, int(time.time()), e["hours_before_close"]):
            return None
        return self._signal(ctx, {**m, "ticker": leg.ticker, "event_ticker": sig.event, "title": sig.title},
                            leg.side, e, end, cap=sig.cost, max_count=leg.count)

    def proven(self, ctx):
        live = [e for e in active_edges(ctx.research) if e["status"] == "live"]
        if live:
            return True, f"{len(live)} price range(s) made money in paper trading"
        return False, "no price range has passed paper trading yet"


STRATEGY = Favorites()
