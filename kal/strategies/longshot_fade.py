"""Longshot fade: buy NO on cheap YES longshots, only in market types (Kalshi categories)
and YES price ranges where past longshots lost more often than their price said.

The evidence: cheap YES on Kalshi loses more than its price implies. Bürgi, Deng & Whelan
(300,000+ contracts) found contracts under 10c lose over 60% of what's put in, worst at
prices takers accept. Bartlett & O'Hara found YES over-bet, mostly in single-name markets.

Why this is NOT free money:
- A NO at 95c wins 5c and loses 95c. One longshot that hits wipes out about 19 wins.
- After the spread and fees the edge is a few cents a dollar at best. Research on
  2025-2026 Kalshi data found plain longshot fading is about zero or worse for a taker;
  the one niche that looked good (busy weather brackets the evening before) didn't hold.
- A cancelled market can pay in between: a called-off 3-way match paid $0.33 on each
  outcome, so a NO bought at 95c there lost 28c.
So kal.research.longshot decides which cells (category + price range) to try, paper
trading decides which go live (apply_longshot there), and every guard here only removes
trades:
- a tight book (YES ask <= YES bid + 2c) and enough contracts at the NO price
- busy markets, in series the research tested for that very cell
- only in the hour after the tested time before the market's scheduled end
- no fair-price or death-clause rules, no combos (KXMVE), no CPI/Fed brackets
- one bet per event, losing at most min(max_per_trade, $5, 2% of capital)
- bets in one series closing on one day: at most 10% of capital together
- no longshot whose YES bid jumped in the last day: one-sided buying ahead of news is how
  informed traders show up (Bartlett & O'Hara). It reads Kalshi's previous_yes_bid, so it
  costs no API call; a market with no history passes. The engine's flow.veto checks the
  last hour of trades on top.
"""
import logging
import math
import re
import time
from collections import defaultdict
from datetime import datetime, timezone

from .. import sizing
from ..client import fee, num, price
from .base import Leg, Signal, Strategy, plain_payouts, scheduled_end
from .favorites import is_open

NAME = "longshot_fade"
BUCKETS = ((0.01, 0.05), (0.05, 0.10), (0.10, 0.20))   # YES bid ranges a cell can cover
MAX_YES = 0.20          # never a "longshot" above this
MAX_SPREAD = 0.02       # YES ask at most this over the YES bid: no stub books
MIN_VOLUME_24H = 200    # contracts traded in the last day
MAX_PER_TRADE = 5.0     # dollars one bet can lose, whatever max_per_trade says
CAPITAL_SHARE = 0.02    # ...and at most this share of capital
GROUP_SHARE = 0.10      # bets in one series closing on one day: at most this share together
TIME_BAND = 1.0         # hours: trade only this soon after the tested decision time
MAX_RISE = 0.02         # a YES bid up this much in a day, and by half or more, looks like news
ACTIVE = ("candidate", "live")

# Fed and CPI brackets track futures and forecasts closely (Diercks, Katz & Wright found
# Kalshi macro prices about as good as expert surveys), so cheap YES there isn't over-bet.
WELL_PRICED = re.compile(r"\b(cpi|inflation|pce|fed|fomc|federal reserve|fed funds|"
                         r"interest rates?|rate (cuts?|hikes?))\b", re.I)
WELL_PRICED_TICKER = re.compile(r"^(KX)?(CPI|FED|FOMC|PCE|INFL|RATE)")

log = logging.getLogger(__name__)


def excluded(series):
    """True for series the fade never trades: combos, and Economics series on CPI or the
    Fed. `series` is a series record (ticker, category, title) or the same keys from an event."""
    ticker = str(series.get("ticker") or series.get("series_ticker") or "").upper()
    if ticker.startswith("KXMVE"):
        return True
    if str(series.get("category") or "").strip().lower() != "economics":
        return False
    return bool(WELL_PRICED_TICKER.match(ticker) or WELL_PRICED.search(str(series.get("title") or "")))


def cells_of(research):
    """Cells from longshot.json: a list, or {"cells": [...]}, or {id: cell}."""
    raw = (research or {}).get("longshot") or []
    if isinstance(raw, dict):
        raw = raw.get("cells", raw)
    if isinstance(raw, dict):
        raw = [dict(v, id=v.get("id", k)) for k, v in raw.items() if isinstance(v, dict)]
    return [c for c in raw if isinstance(c, dict)]


def active_cells(research):
    """Candidate and live cells with every field we need and a positive worst case.
    Anything malformed is skipped: a cell we can't read is a cell we don't trade."""
    out = []
    for c in cells_of(research):
        try:
            lo, hi = float(c["lo"]), float(c["hi"])
            hours, roi_lo = float(c["hours_before_close"]), float(c["roi_lo"])
        except (KeyError, TypeError, ValueError):
            continue
        series = [s for s in c.get("series") or [] if isinstance(s, str) and s]
        if not all(math.isfinite(x) for x in (lo, hi, hours, roi_lo)):
            continue
        if (c.get("status") in ACTIVE and c.get("id") and c.get("category") and series
                and 0.01 <= lo < hi <= MAX_YES and 0 < hours and 0 < roi_lo < 1):
            out.append({**c, "lo": lo, "hi": hi, "hours_before_close": hours, "roi_lo": roi_lo,
                        "series": series})
    return out


def tight(bid, ask):
    """A real two-sided book: YES bid and ask both there, at most 2c apart."""
    return 0 < bid < ask < 1 and ask - bid <= MAX_SPREAD + 1e-9


MIN_PROFIT = 0.01   # dollars of expected profit per bet, at the cautious win chance
MIN_ROI = 0.005     # and at least this share of the cost


def full_size(p):
    """Contracts a full-size bet buys at price p."""
    return max(1, int(MAX_PER_TRADE // sizing.unit_cost(p)))


def cost_each(p):
    """What one contract costs in a full-size bet, with Kalshi's fee rounded up per order.
    The backtest prices trades the same way: at 99c the rounding is a fifth of the profit,
    so the smooth fee estimate would flatter the edge."""
    n = full_size(p)
    return (n * p + fee(n, p)) / n


def group_of(series, anchor):
    """Bets in one series that close on the same UTC day share one cap: hourly BTC events,
    or one city's weather, tend to win or lose together."""
    day = datetime.fromtimestamp(anchor, timezone.utc).date().isoformat()
    return f"{series}:{day}"


def in_window(anchor, now, hours):
    """True in the hour after the tested decision time, `hours` before the event's scheduled
    close. That's when the backtest priced it; a longshot at 3c ten minutes before the end
    is a different bet."""
    left = (anchor - now) / 3600
    return max(0.0, hours - TIME_BAND) < left <= hours


def rising(m):
    """True if the YES bid jumped in the last day (up 2c+ and by half or more)."""
    before, now = price(m, "previous_yes_bid"), price(m, "yes_bid")
    return before > 0 and now - before >= MAX_RISE - 1e-9 and now >= 1.5 * before


def bet_cap(ctx):
    """Most one bet may lose. One bet per event, so it's also the most one event may lose.
    ctx.bankroll stands in for capital: the engine sets it to capital cut after drawdowns,
    so 2% of it is never more than 2% of capital."""
    cfg = ctx.cfg
    return max(0.0, min(cfg["max_per_trade"], cfg["max_per_event"], MAX_PER_TRADE,
                        CAPITAL_SHARE * ctx.bankroll))


def event_info(ev, series_wanted):
    """What we need from one open event, or None if it's not one to trade.

    "ends" holds each market's scheduled end (base.scheduled_end, None if it shows none),
    the same anchoring the backtest uses (kal.research.backtest.anchors). Not its
    close_time: on a game that's a deadline two days after it, long after the market
    really closes."""
    if not isinstance(ev, dict) or not ev.get("event_ticker"):
        return None
    et = ev["event_ticker"]
    series = ev.get("series_ticker") or et.split("-")[0]
    if series not in series_wanted or et.upper().startswith("KXMVE"):
        return None
    if excluded({"ticker": series, "category": ev.get("category"), "title": ev.get("title")}):
        return None
    markets = [m for m in ev.get("markets") or [] if isinstance(m, dict) and m.get("ticker")]
    if not markets:
        return None
    return {"ticker": et, "series": series, "title": ev.get("title") or et, "markets": markets,
            "tickers": {m["ticker"] for m in markets},
            "ends": {m["ticker"]: scheduled_end(m, ev.get("strike_date")) for m in markets}}


class LongshotFade(Strategy):
    name = NAME
    title = "Longshot fade"
    kind = "statistical"
    description = ("Buy NO on cheap YES bets, only in market types where cheap YES lost "
                   "more often than its price said. Small bets. Paper first.")

    def scan(self, ctx):
        cells = active_cells(ctx.research)
        if not cells:
            return []
        by_series = defaultdict(list)
        for c in cells:
            for s in c["series"]:
                by_series[s].append(c)
        now = time.time()
        # The same call the arbitrage strategies make, so the run's cache serves it.
        events = [e for e in (event_info(ev, by_series)
                              for ev in ctx.client.events(status="open", with_nested_markets="true")) if e]
        held = defaultdict(int)  # group -> tickers already held there (anyone's)
        held_any_day = defaultdict(int)  # series -> held tickers with no known end: they count on every day
        for e in events:
            for tk in e["tickers"] & ctx.held:
                end = e["ends"].get(tk)
                if end:
                    held[group_of(e["series"], end)] += 1
                else:
                    held_any_day[e["series"]] += 1
        picks = []
        for e in events:
            if e["tickers"] & ctx.held:
                continue  # one bet per event, across runs too
            pick = self._pick(ctx, by_series[e["series"]], e, now)
            if pick:
                picks.append((e, *pick))
        # Best evidence first, so the caps leave room for it.
        picks.sort(key=lambda x: (-x[2]["roi_lo"], x[1]["ticker"]))
        found, spent, group_spent = [], 0.0, defaultdict(float)
        for e, m, cell, offer in picks:
            end = e["ends"][m["ticker"]]
            g = group_of(e["series"], end)
            # Each held ticker counts as a full-size bet: the most a longshot bet can be.
            n_held = held[g] + held_any_day[e["series"]]
            room = GROUP_SHARE * ctx.bankroll - n_held * MAX_PER_TRADE - group_spent[g]
            cap = min(bet_cap(ctx), ctx.budget_left - spent, room)
            if cap <= 0:
                continue
            sig = self._signal(ctx, e, m, end, g, cell, offer, cap, ctx.open_exposure + spent)
            if sig:
                found.append(sig)
                spent += sig.cost
                group_spent[g] += sig.cost
        return sorted(found, key=lambda s: -s.exp_profit)

    def _pick(self, ctx, cells, e, now):
        """(market, cell, offer) for the event's best-backed longshot whose book checks out."""
        fits = [(m, c) for m in e["markets"] for c in [self._fit(cells, e, m, now, e["ends"].get(m["ticker"]))] if c]
        for m, c in sorted(fits, key=lambda x: (-x[1]["roi_lo"], x[0]["ticker"])):
            try:
                offer = self._offer(ctx, m["ticker"], c)
            except Exception as err:  # one bad book shouldn't sink the scan
                log.warning("longshot_fade: book for %s failed: %s", m["ticker"], err)
                continue
            if offer:
                return m, c, offer
        return None

    def _fit(self, cells, e, m, now, end):
        """The cell a market fits, judged from its listing (the order book checks again).
        `end`: its scheduled end. None (no time set at listing) never fits."""
        tk = m["ticker"]
        if end is None:
            return None
        if tk.upper().startswith("KXMVE") or not is_open(m) or not plain_payouts([m]):
            return None
        if m.get("event_ticker") not in (None, e["ticker"]):
            return None
        if num(m, "volume_24h") < MIN_VOLUME_24H or rising(m):
            return None
        bid, ask = price(m, "yes_bid"), price(m, "yes_ask")
        if not tight(bid, ask):
            return None
        fits = [c for c in cells if c["lo"] <= bid < c["hi"] and in_window(end, now, c["hours_before_close"])]
        return max(fits, key=lambda c: c["roi_lo"]) if fits else None

    def _offer(self, ctx, ticker, cell):
        """(NO price, YES bid, contracts there) from a fresh order book, or None.
        The book holds bids only: the NO ask is 1 - best YES bid, the YES ask 1 - best NO bid."""
        book = ctx.client.orderbook(ticker) or {}
        yes = [lv for lv in book.get("yes") or [] if 0 < lv[0] < 1 and lv[1] > 0]
        no = [lv for lv in book.get("no") or [] if 0 < lv[0] < 1 and lv[1] > 0]
        if not yes or not no:
            return None  # one-sided: no real price
        bid, depth = max(yes)
        ask = round(1 - max(no)[0], 4)
        if not tight(bid, ask) or not cell["lo"] <= bid < cell["hi"]:
            return None
        return round(1 - bid, 4), bid, depth

    def _signal(self, ctx, e, m, end, group, cell, offer, cap, open_exposure, max_count=None):
        """Size one NO bet with a cautious win chance and every cap, or None."""
        no_p, bid, depth = offer
        tk = m["ticker"]
        # Cautious: the win chance that pays exactly the backtest's worst-case ROI.
        p = min(cost_each(no_p) * (1 + cell["roi_lo"]), 0.999)
        count = sizing.contracts(p, no_p, ctx.bankroll, ctx.cfg["kelly_fraction"], cap,
                                 open_exposure=open_exposure, max_exposure=ctx.cfg["max_exposure"])
        count = min(count, int(depth))  # never more than the book really has at our price
        if max_count is not None:
            count = min(count, max_count)
        while count > 0 and Leg(tk, "no", no_p, count).cost > cap + 1e-9:
            count -= 1  # fees round up to the cent, so sizing's estimate can be a bit low
        if count < 1:
            return None
        leg = Leg(tk, "no", no_p, count)
        exp_profit = count * p - leg.cost
        if exp_profit < max(MIN_PROFIT, MIN_ROI * leg.cost):
            return None  # a small order pays more fee per contract: dust isn't worth a 95c risk
        sig = Signal(NAME, e["ticker"], m.get("title") or e["title"], [leg],
                     note=(f"NO at ${no_p:.2f} on a {bid * 100:.0f}c longshot. Cheap YES in "
                           f"{cell['category']} lost before (worst case {cell['roi_lo']:+.1%})"),
                     meta={"cell": cell["id"], "group": group, "closes": end,
                           "paper_only": cell["status"] != "live"},
                     closes_at=end)
        sig.exp_profit = exp_profit
        return sig

    def recheck(self, ctx, sig):
        """Right before a live order: cell still live, market still in the window and the
        book still good? Re-priced, never bigger than scanned (so the caps still hold)."""
        cell = {c["id"]: c for c in active_cells(ctx.research)}.get(sig.meta.get("cell"))
        if not cell or cell["status"] != "live" or len(sig.legs) != 1:
            return None
        try:
            end = float(sig.meta["closes"])
        except (KeyError, TypeError, ValueError):
            return None
        group = sig.meta.get("group")
        leg = sig.legs[0]
        m = ctx.client.market(leg.ticker)
        if not m or not group:
            return None
        m = {**m, "ticker": leg.ticker}
        e = {"ticker": sig.event, "title": sig.title}
        if not self._fit([cell], e, m, time.time(), end):
            return None
        offer = self._offer(ctx, leg.ticker, cell)
        if not offer:
            return None
        cap = min(bet_cap(ctx), ctx.budget_left, sig.cost)
        return self._signal(ctx, e, m, end, group, cell, offer, cap, ctx.open_exposure, max_count=leg.count)

    def proven(self, ctx):
        live = [c for c in active_cells(ctx.research) if c["status"] == "live"]
        if live:
            return True, f"{len(live)} longshot range(s) made money in paper trading"
        return False, "no longshot range has passed paper trading yet"


STRATEGY = LongshotFade()
