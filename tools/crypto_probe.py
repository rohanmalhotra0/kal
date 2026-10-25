"""Side branch only (probe/crypto). Why does research price 0 crypto markets? For a few
crypto series, show settled markets' times, their event's strike_date, the anchor and
decision time the backtest computes, and whether a price snapshot exists.
Public data only: no key, no orders."""
import json

from kal.client import Kalshi, num, ts
from kal.research import backtest
from kal.strategies.base import scheduled_end

c = Kalshi()
for series in ("KXBTCD", "KXETHD", "KXBTC15M", "KXBTC"):
    ms = c.settled_markets(series, 60)
    print(f"\n== {series}: {len(ms)} settled listed; volume>=100: {sum(1 for m in ms if num(m, 'volume') >= 100)}")
    strikes = backtest.strike_dates(c, series, ms)
    print(f"strike dates found for {len(strikes)} of {len({m.get('event_ticker') for m in ms})} events")
    shown = 0
    for m in sorted(ms, key=lambda m: -num(m, "volume")):
        if shown >= 4:
            break
        shown += 1
        sd = strikes.get(m.get("event_ticker"))
        end = scheduled_end(m, sd)
        t = backtest.decision_time(m, 6, anchor=end) if end else None
        snap = backtest.snapshot(c, series, m, 6, anchor=end) if t else None
        print(json.dumps({k: m.get(k) for k in ("ticker", "open_time", "close_time", "can_close_early",
                                                 "occurrence_datetime", "expected_expiration_time",
                                                 "status", "result", "volume_fp")}))
        print(f"   strike_date={sd} anchor={end} decision={t} snapshot={snap}")
