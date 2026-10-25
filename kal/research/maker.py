"""Maker backtest: would resting bids have made money where buying at the ask doesn't?

Buying at the ask pays the spread and the taker fee, and on real Kalshi data that leaves
about nothing (docs/RESEARCH.md). Studies say the edge goes to makers, whose orders rest
in the book. This screens for that on settled markets. Research only: it places nothing.

Each market gets one bid on the YES or the NO side, set from the last hourly candle
before the decision time (as in kal.research.backtest): at the best bid ("join"), or 1c
above it when the spread is 2c or more ("improve", which never crosses). It rests until
the market closes, or until the hour its scheduled end falls in begins (a live maker
would cancel by then), whichever is first.

The bid is for ONE contract. It fills when a later trade went through its price: every
bid there was used up, ours too. A trade AT our price fills an "improve" bid (it was
alone at that price), not a "join" bid (it may have been at the back of the queue).
Fills come when the price runs through us, often on news. That's the cost of making,
and this keeps it.

Why one contract: a bigger bid fills only as far as the volume that went through its
price. Small dips (often in markets that go on to win) fill a little of it; news runs
(often in markets that go on to lose) fill all of it. So one contract is the best case
per dollar. A price range that loses money here loses at any size. One that makes money
is a lead, not an edge: it needs a check with real trade sizes before any strategy.

Every fill pays the maker fee Kalshi charges on series that charge makers (a quarter of
the taker rate). Most series charge nothing.
"""
import json
import logging
import os
import re
import time
from collections import defaultdict

from ..client import price
from . import backtest

MAKER_FEE_RATE = 0.0175   # Kalshi's maker rate on series that charge one (taker: 0.07)
TICK = 0.01
PERIOD = 3600             # hourly candles, which end on the hour
RULES = ("join", "improve")

log = logging.getLogger(__name__)


def maker_fee(p):
    """Maker fee for one contract at p, before rounding (the most any series charges)."""
    return MAKER_FEE_RATE * p * (1 - p)


def window_end(o):
    """When our bid stops resting: the market's close, or the start of the hour its
    scheduled end falls in, whichever is first."""
    anchor = o.get("anchor") or o["close"]
    return min(o["close"], anchor - anchor % PERIOD)


def _volume(c):
    for k in ("volume_fp", "volume"):   # live candles send volume_fp, historical ones volume
        v = c.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def window(client, o):
    """(lowest, highest) trade price while our bid rested; None for each if nothing
    traded. The hour the decision falls in counts: the bid was set from prices before
    that hour began. The hour a market closes in counts: news runs are how bids get
    picked off. Returns None if Kalshi sent no candles at all (it sends one an hour even
    when nothing traded), so the market is tried again later instead of written off."""
    start, end = o["decided"], window_end(o)
    candles = client.candles(o["series"], o["ticker"], start + 1, end + PERIOD)
    if not candles:
        return None
    lo = hi = None
    for c in candles:
        t = c.get("end_period_ts")
        if not isinstance(t, (int, float)) or not (start < t and t - PERIOD < end):
            continue
        if _volume(c) <= 0:
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
    return os.path.join(backtest.folder("cache"), f"{safe}_maker_close{hours_before_close:g}_v2.json")


def _read(path):
    try:
        with open(path) as f:
            return json.load(f)["markets"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _write(path, markets):
    with open(path + ".tmp", "w") as f:
        json.dump({"version": 2, "markets": markets}, f)
    os.replace(path + ".tmp", path)


def load(client, series, hours_before_close=6, max_markets=1500, refresh=False, deadline=None):
    """The newest max_markets backtest.load_observations rows, each with the trade range
    while our bid rested: "lo" and "hi" (None if nothing traded). A cached range is used
    only for the same decision time and window end. Stops fetching at `deadline` (unix
    time) and returns what it has."""
    obs = backtest.load_observations(client, series, hours_before_close, max_markets=max_markets,
                                     refresh=refresh)[-max_markets:]
    path = cache_path(series, hours_before_close)
    cache = {} if refresh else _read(path)
    out, new, errors, last_error = [], 0, 0, None
    try:
        for o in obs:
            tk, when = o["ticker"], {"decided": o["decided"], "end": window_end(o)}
            hit = cache.get(tk)
            if not (isinstance(hit, dict) and all(hit.get(k) == v for k, v in when.items())):
                if deadline and time.time() > deadline:
                    break
                try:
                    got = window(client, o)
                except Exception as e:  # one market's candles failing shouldn't sink the series
                    errors, last_error = errors + 1, e
                    if errors >= 10 and errors > new:
                        raise RuntimeError(f"candles keep failing: {e}") from e
                    continue
                if got is None:
                    continue
                hit = cache[tk] = {"lo": got[0], "hi": got[1], **when}
                new += 1
            out.append({**o, "lo": hit["lo"], "hi": hit["hi"]})
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


def filled(o, side, p, at_price=False):
    """Did a later trade fill our bid? A YES bid at p: YES traded below p (or at p, with
    at_price). A NO bid at p is a YES ask at 1 - p: YES traded above it (or at it)."""
    eps = 1e-6
    if side == "yes":
        lo = o.get("lo")
        return lo is not None and (lo < p - eps or (at_price and lo <= p + eps))
    hi, a = o.get("hi"), 1 - p
    return hi is not None and (hi > a + eps or (at_price and hi >= a - eps))


def maker_trade(unit, p, won):
    """(unit, cost, pnl, fair_var) for one filled one-contract bid."""
    cost = p + maker_fee(p)
    return unit, cost, won - cost, p * (1 - p)


def table(obs, rule="join", z=2.0):
    """Per side and price range of our bid: bids posted, fills, ROI on fills and its
    cautious bound. Fills that win or lose together count as one unit: o["unit"] if set
    (the tool uses category and day: related series move together), else series and day."""
    posted, fills = defaultdict(int), defaultdict(list)
    for o in obs:
        for side, p in quotes(o, rule):
            key = (side, backtest.bucket_of(p))
            posted[key] += 1
            if filled(o, side, p, at_price=rule == "improve"):
                fills[key].append(maker_trade(o.get("unit") or backtest.group(o), p, backtest.paid(o, side)))
    rows = []
    for side, b in sorted(posted):
        t = fills[(side, b)]
        roi, lo = backtest.roi_stats(t, z) if t else (None, None)
        rows.append({"side": side, "lo": backtest.BUCKETS[b], "hi": backtest.BUCKETS[b + 1],
                     "bids": posted[(side, b)], "fills": len(t), "units": len({x[0] for x in t}),
                     "win": sum(1 for x in t if x[2] > 0) / len(t) if t else None, "roi": roi, "roi_lo": lo})
    return rows


def is_lead(row):
    """Worth a check with real trade sizes. Never an edge by itself: one contract is the best case."""
    return (row["roi_lo"] is not None and row["roi_lo"] > 0
            and row["fills"] >= backtest.MIN_TRADES and row["units"] >= backtest.MIN_EVENTS)
