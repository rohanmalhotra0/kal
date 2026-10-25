"""Idea #6 on real data: do "X before an earlier date" markets ever trade above "X before a
later date" by more than fees? Then YES on the later one plus NO on the earlier one locks
in $1 by the earlier date (public data only: no key, no orders). Probe branch only.

Hourly candles of every "before <date>" family: open events, plus settled ones in the
same series. Counts hours with an after-fee gap, and how many pass the bot's own rules
for locked-in trades (settle within max_arb_days, at least min_arb_apr a year).
"""
import calendar
import collections
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal import config  # noqa: E402
from kal.client import Kalshi, fee, ts  # noqa: E402
from kal.research.backtest import candle_close  # noqa: E402
from kal.risk import yearly_return  # noqa: E402

BEFORE = re.compile(r"^\s*before\s+(?:([a-z]{3})[a-z]*\.?\s+(\d{1,2}),\s*)?(\d{4})\s*$", re.I)
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
H = 3600


def close_of(m):
    try:
        return ts(m["close_time"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def deadline(m):
    """The date in "Before Sep 1, 2026" (or "Before 2028": Jan 1), as a sortable tuple, or None.
    Not close_time: Kalshi rewrites it when a market settles early, and the order flips."""
    g = BEFORE.match(m.get("yes_sub_title") or m.get("subtitle") or "")
    if not g:
        return None
    month, day, year = g.groups()
    if month and month.lower() not in MONTHS:
        return None
    return (int(year), MONTHS[month.lower()] if month else 1, int(day) if day else 1)


def deadline_ts(m):
    y, mo, d = deadline(m)
    return calendar.timegm((y, mo, d, 0, 0, 0))   # UTC midnight: within a day of Kalshi's own cutoff


def family(markets):
    ms = [m for m in markets if m.get("ticker")]
    days = [deadline(m) for m in ms]
    if len(ms) < 2 or None in days or len(set(days)) < len(days) or any(close_of(m) is None for m in ms):
        return None   # fail closed: every market's deadline readable, none repeated
    return sorted(ms, key=deadline)


def main():
    cfg = config.load()
    c = Kalshi()
    now = time.time()
    t0 = now
    fams = []   # (series, event, markets sorted by close, settled?)
    series = set()
    for ev in c.events(status="open", with_nested_markets="true"):
        if ev.get("mutually_exclusive"):
            continue
        f = family(ev.get("markets") or [])
        if f:
            fams.append((ev.get("series_ticker"), ev.get("event_ticker"), f, False))
            series.add(ev.get("series_ticker"))
    print(f"open 'before' families: {len(fams)} in {len(series)} series")
    settled_n, broken = 0, []
    for s in sorted(series):
        try:
            got = c.settled_markets(s, limit=300)
        except Exception as e:
            print(f"  {s}: settled markets failed: {e}")
            continue
        by_ev = collections.defaultdict(list)
        for m in got:
            by_ev[m.get("event_ticker")].append(m)
        for ev, ms in by_ev.items():
            f = family(ms)
            if not f:
                continue
            fams.append((s, ev, f, True))
            settled_n += 1
            res = [m.get("result") for m in f]
            if any(a == "yes" and b == "no" for a, b in zip(res, res[1:])):   # earlier YES, later NO
                broken.append(ev)
                continue   # not nested after all: never counted as a gap
    print(f"settled 'before' families: {settled_n}; results that break the nesting: {len(broken)} {broken[:5]}")

    hours = ok_hours = pass_hours = 0
    best, passing = [], collections.Counter()
    for s, ev, f, settled in fams:
        for a, b in zip(f, f[1:]):
            end = min(close_of(a), close_of(b), now)
            start = deadline_ts(a) - cfg["max_arb_days"] * 86400   # the bot holds an arbitrage 30 days at most
            if end <= start:
                continue
            try:
                ca = {x["end_period_ts"]: x for x in c.candles(s, a["ticker"], int(start), int(end)) or []}
                cb = {x["end_period_ts"]: x for x in c.candles(s, b["ticker"], int(start), int(end)) or []}
            except Exception as e:
                print(f"  {a['ticker']}: candles failed: {e}")
                continue
            for t in sorted(set(ca) & set(cb)):
                bid_a, ask_b = candle_close(ca[t], "yes_bid"), candle_close(cb[t], "yes_ask")
                if not (0 < bid_a < 1 and 0 < ask_b < 1):
                    continue
                hours += 1
                cost = ask_b + (1 - bid_a) + fee(1, ask_b) + fee(1, round(1 - bid_a, 4))
                edge = 1 - cost
                if edge <= 0:
                    continue
                ok_hours += 1
                days = max((deadline_ts(a) - t) / 86400, 1 / 24)   # $1 is locked in by the earlier deadline
                apr = yearly_return(1 / cost, days)
                good = days <= cfg["max_arb_days"] and apr >= cfg["min_arb_apr"] and edge / cost >= 0.01
                if good:
                    pass_hours += 1
                    passing[ev] += 1
                best.append((edge, ev, a["ticker"], b["ticker"], time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)),
                             bid_a, ask_b, round(days, 1), round(apr, 2), good, settled))
    best.sort(reverse=True)
    print(f"\npair-hours with both prices: {hours}")
    print(f"hours with YES later + NO earlier under $1 after fees: {ok_hours}")
    print(f"... of those, passing the bot's rules (<= {cfg['max_arb_days']} days, >= {cfg['min_arb_apr']:.0%} a year, "
          f">= 1% return): {pass_hours} in {len(passing)} families")
    for x in best[:15]:
        print("  GAP " + json.dumps(dict(zip(("edge", "event", "earlier", "later", "hour", "earlier_bid", "later_ask",
                                               "days_to_earlier_close", "apr", "passes", "settled"), x))))
    print(f"\ndone in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
