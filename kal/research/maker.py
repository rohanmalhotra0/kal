"""Maker backtest: would resting bids have made money where buying at the ask doesn't?

Buying at the ask pays the spread and the taker fee, and on real Kalshi data that leaves
about nothing (docs/RESEARCH.md). Studies say the edge goes to makers, whose orders rest
in the book. This tests that on settled markets. Research only: it places nothing.

At the decision time (as in kal.research.backtest: hours before the market's scheduled
end), post one bid on the YES or the NO side: at the best bid ("join"), or 1c above it
when the spread is 2c or more ("improve", which never crosses). It rests until the
market closes or reaches its scheduled end, whichever is first.

It counts as filled only if a later trade printed THROUGH its price: a YES bid at 90c
fills when YES traded below 90c. Every bid at 90c had to be used up first, ours too.
A trade AT our price doesn't count: we may have been at the back of the queue. So fills
come when the price runs through us, often on news. That's the real cost of making,
and this keeps it.

A fill costs our price plus a maker fee. Most series charge makers nothing. This
charges every fill the maker fee Kalshi uses where it does charge one (a quarter of
the taker rate), rounded up to the cent.
"""
import json
import logging
import math
import os
import re
from collections import defaultdict

from ..client import num, price
from ..strategies import favorites
from . import backtest

MAKER_FEE_RATE = 0.0175   # Kalshi's maker rate on series that charge one (taker: 0.07)
TICK = 0.01
PERIOD = 3600             # hourly candles
RULES = ("join", "improve")

log = logging.getLogger(__name__)


def maker_fee(count, p):
    """Maker fee for one order, rounded up to the cent."""
    return math.ceil(round(MAKER_FEE_RATE * count * p * (1 - p) * 100, 6)) / 100


def window_end(o):
    """When our bid stops resting: the market's close or its scheduled end, if earlier."""
    return min(o["close"], o.get("anchor") or o["close"])


def window(client, o):
    """(lowest, highest) trade price after the decision time, while our bid rested.
    (None, None) if nothing traded. Includes the hour the window ends in (fills just after
    a scheduled end are mostly bad ones, so that's the cautious side) and the hour the
    decision falls in (the bid was set from prices before that hour began)."""
    start, end = o["decided"], window_end(o)
    lo = hi = None
    for c in client.candles(o["series"], o["ticker"], start + 1, end + PERIOD):
        t = c.get("end_period_ts")
        if not isinstance(t, (int, float)) or not (start < t and t - PERIOD < end):
            continue
        if num(c, "volume") <= 0:
            continue  # no trades: its price range is empty or carried over
        p = c.get("price") or {}
        low, high = price(p, "low"), price(p, "high")
        if 0 < low < 1:
            lo = low if lo is None else min(lo, low)
        if 0 < high < 1:
            hi = high if hi is None else max(hi, high)
    return lo, hi


def cache_path(series, hours_before_close=6):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", series)
    return os.path.join(backtest.folder("cache"), f"{safe}_maker_close{hours_before_close:g}_v1.json")


def _read(path):
    try:
        with open(path) as f:
            return json.load(f)["markets"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _write(path, markets):
    with open(path + ".tmp", "w") as f:
        json.dump({"version": 1, "markets": markets}, f)
    os.replace(path + ".tmp", path)


def load(client, series, hours_before_close=6, max_markets=1500, refresh=False):
    """backtest.load_observations rows, each with the trade range while our bid rested:
    "lo" and "hi" (None if nothing traded). Settled markets don't change, so the range
    is cached next to the price snapshots."""
    obs = backtest.load_observations(client, series, hours_before_close, max_markets=max_markets,
                                     refresh=refresh)
    path = cache_path(series, hours_before_close)
    cache = {} if refresh else _read(path)
    out, new, errors, last_error = [], 0, 0, None
    try:
        for o in obs:
            tk = o["ticker"]
            if tk not in cache:
                try:
                    cache[tk] = list(window(client, o))
                except Exception as e:  # one market's candles failing shouldn't sink the series
                    errors, last_error = errors + 1, e
                    if errors >= 10 and errors > new:
                        raise RuntimeError(f"candles keep failing: {e}") from e
                    continue
                new += 1
            lo, hi = cache[tk]
            out.append({**o, "lo": lo, "hi": hi})
    finally:
        if new:
            listed = {o["ticker"] for o in obs}
            _write(path, {tk: v for tk, v in cache.items() if tk in listed})
    if errors:
        log.warning("%s: %d markets skipped (candles failed): %s", series, errors, last_error)
    return out


def quotes(o, rule="join"):
    """[(side, price)]: the bids we'd post at the decision time. A NO bid at q is a YES
    ask at 1 - q. "improve" bids 1c above the best bid, only where the spread is 2c+."""
    ask, bid = o["yes_ask"], o["yes_bid"]
    step = 0.0
    if rule == "improve":
        if ask - bid < 2 * TICK - 1e-9:
            return []
        step = TICK
    out = []
    if bid > 0:
        out.append(("yes", round(bid + step, 4)))
    if ask < 1:
        out.append(("no", round(1 - ask + step, 4)))
    return [(side, p) for side, p in out if 0.01 <= p <= 0.99]


def filled(o, side, p, touch=False):
    """Did a later trade go through our bid? A YES bid at p: YES traded below p. A NO bid
    at p (a YES ask at 1 - p): YES traded above 1 - p. touch also counts a trade at our
    price: an upper bound on fills, never used to call an edge."""
    eps = 1e-6
    if side == "yes":
        lo = o.get("lo")
        return lo is not None and (lo < p - eps or (touch and lo <= p + eps))
    hi, a = o.get("hi"), 1 - p
    return hi is not None and (hi > a + eps or (touch and hi >= a - eps))


def maker_trade(event, p, won, dollars=favorites.FULL_BET):
    """(event, cost, pnl, fair_var) for one filled bid, sized like the bot's full bet."""
    n = favorites.full_size(p, dollars)
    cost = n * p + maker_fee(n, p)
    return event, cost, n * float(won) - cost, n * n * p * (1 - p)


def table(obs, rule="join", touch=False, z=2.0):
    """Per side and price range of our bid: bids posted, fills, ROI on the money filled,
    its cautious bound, and the ROI if every bid had filled (the gap is what being
    picked off costs)."""
    posted, fills, every = defaultdict(int), defaultdict(list), defaultdict(list)
    for o in obs:
        for side, p in quotes(o, rule):
            key = (side, backtest.bucket_of(p))
            won = backtest.paid(o, side)
            posted[key] += 1
            every[key].append(maker_trade(backtest.group(o), p, won))
            if filled(o, side, p, touch):
                fills[key].append(every[key][-1])
    rows = []
    for side, b in sorted(posted):
        t = fills[(side, b)]
        roi, lo = backtest.roi_stats(t, z) if t else (None, None)
        if_all, _ = backtest.roi_stats(every[(side, b)], z)
        rows.append({"side": side, "lo": backtest.BUCKETS[b], "hi": backtest.BUCKETS[b + 1],
                     "bids": posted[(side, b)], "fills": len(t), "events": len({x[0] for x in t}),
                     "win": sum(1 for x in t if x[2] > 0) / len(t) if t else None,
                     "roi": roi, "roi_lo": lo, "roi_if_all_filled": if_all})
    return rows


def is_edge(row):
    return (row["roi_lo"] is not None and row["roi_lo"] > 0
            and row["fills"] >= backtest.MIN_TRADES and row["events"] >= backtest.MIN_EVENTS)
