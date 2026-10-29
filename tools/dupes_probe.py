"""Idea #17, the same question in two series, on real data. Probe branch only, never merged.
Public data only (no key, no orders).

Some series look like copies (KXECONSTATCPI vs KXCPI, KXUSNFP vs KXPAYROLLS, KXUE vs KXU3).
If their rules match, the same strike must cost the same: YES on one plus NO on the other
pays exactly $1. Prints each pair's rules side by side, matches events by their ticker
ending and markets by strike, checks settled results agree, and counts hourly gaps after
fees in the 10 days before close, the bot's way (pays within max_arb_days, 20%+ a year, 1%+).
"""
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal import config  # noqa: E402
from kal.client import Kalshi, fee, price, ts  # noqa: E402
from kal.research.backtest import candle_close  # noqa: E402
from kal.risk import yearly_return  # noqa: E402

PAIRS = [("KXECONSTATCPI", "KXCPI"), ("KXECONSTATCPICORE", "KXCPICORE"), ("KXECONSTATCPIYOY", "KXCPIYOY"),
         ("KXECONSTATCORECPIYOY", "KXCPICOREYOY"), ("KXECONSTATU3", "KXU3"), ("KXUE", "KXU3"),
         ("KXUSNFP", "KXPAYROLLS"), ("KXUSEDCAR", "KXUSEDCARCPI"), ("KXCPIUSEDCAR", "KXUSEDCARCPI"),
         ("KXSHELTERCPI", "KXCPISHELTER"), ("KXUSGASCPI", "KXCPIGAS")]
H, DAY = 3600, 86400
N = 30


def fee_pc(p):
    return fee(N, p) / N


def plain(t):
    return " ".join(str(t or "").split())


def suffix(ev):
    return str(ev or "").split("-", 1)[-1]


def key(m):
    st = m.get("strike_type")
    k = m.get("floor_strike") if st in ("greater", "greater_or_equal") else m.get("cap_strike") if st in ("less", "less_or_equal") else None
    if st == "between":
        k = (m.get("floor_strike"), m.get("cap_strike"))
    return (st, json.dumps(k))


def markets_of(c, s):
    out = {}
    try:
        for m in c.settled_markets(s, limit=3000):
            out[m["ticker"]] = m
        for ev in c.events(series_ticker=s, status="open", with_nested_markets="true"):
            for m in ev.get("markets") or []:
                out[m["ticker"]] = m
    except Exception as e:
        print(f"{s}: failed: {e}")
    return list(out.values())


def main():
    cfg = config.load()
    c = Kalshi()
    t0, now = time.time(), time.time()
    tally = collections.Counter()
    gaps, passing, broken, near = [], collections.Counter(), [], {}
    for a, b in PAIRS:
        ma, mb = markets_of(c, a), markets_of(c, b)
        print(f"\n== {a} ({len(ma)} markets) vs {b} ({len(mb)} markets)")
        if not ma or not mb:
            continue
        print("   A RULES " + plain(ma[0].get("rules_primary"))[:400])
        print("   B RULES " + plain(mb[0].get("rules_primary"))[:400])
        print("   A SUB " + plain(ma[0].get("yes_sub_title")) + " | B SUB " + plain(mb[0].get("yes_sub_title")))
        by_b = collections.defaultdict(dict)
        for m in mb:
            by_b[suffix(m.get("event_ticker"))][key(m)] = m
        matched = 0
        for x in ma:
            y = by_b.get(suffix(x.get("event_ticker")), {}).get(key(x))
            if not y:
                continue
            matched += 1
            tally[f"{a}/{b}: matched markets"] += 1
            if x.get("result") in ("yes", "no") and y.get("result") in ("yes", "no") and x["result"] != y["result"]:
                broken.append((a, b, x["ticker"], y["ticker"], x["result"], y["result"]))
            try:
                ca, cb = ts(x["close_time"]), ts(y["close_time"])
            except Exception:
                continue
            pay, end = max(ca, cb), min(ca, cb, now)
            start = end - 10 * DAY
            if end <= start:
                continue
            try:
                kx = {k["end_period_ts"]: k for k in c.candles(a, x["ticker"], int(start), int(end)) or []}
                ky = {k["end_period_ts"]: k for k in c.candles(b, y["ticker"], int(start), int(end)) or []}
            except Exception:
                tally["candle errors"] += 1
                continue
            for t in sorted(set(kx) & set(ky)):
                xb, xa = candle_close(kx[t], "yes_bid"), candle_close(kx[t], "yes_ask")
                yb, ya = candle_close(ky[t], "yes_bid"), candle_close(ky[t], "yes_ask")
                for name, p1, p2 in ((f"YES {x['ticker']} + NO {y['ticker']}", xa, 1 - yb),
                                     (f"YES {y['ticker']} + NO {x['ticker']}", ya, 1 - xb)):
                    if not (0 < p1 < 1 and 0 < p2 < 1):
                        continue
                    tally["pair-hours"] += 1
                    cost = p1 + p2 + fee_pc(p1) + fee_pc(round(p2, 4))
                    if name not in near or 1 - cost > near[name][0]:
                        near[name] = (round(1 - cost, 4), name, time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)), p1, round(p2, 4))
                    if cost >= 1:
                        continue
                    tally["gap hours after fees"] += 1
                    days = max((pay - t) / DAY, 1 / 24)
                    apr = yearly_return(1 / cost, days)
                    ok = days <= cfg["max_arb_days"] and apr >= cfg["min_arb_apr"] and (1 - cost) / cost >= 0.01
                    if ok:
                        tally["passing hours"] += 1
                        passing[name] += 1
                    gaps.append((round(1 - cost, 4), name, time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)), p1, round(p2, 4),
                                 round(days, 1), round(apr, 2), ok))
            if time.time() - t0 > 35 * 60:
                print("(time limit)")
                break
        print(f"   matched markets: {matched}")
    print(f"\ntotals: {dict(tally)}")
    print(f"settled results that disagree: {len(broken)}")
    for x in broken[:15]:
        print("   BROKEN " + json.dumps(x))
    print("closest:")
    for x in sorted(near.values(), reverse=True)[:12]:
        print("   NEAR " + json.dumps(x))
    gaps.sort(reverse=True)
    print(f"gap hours after fees: {len(gaps)}; passing hours: {sum(passing.values())} in {len(passing)} pairs")
    for k, v in passing.most_common(15):
        print(f"   PASS {v:>3}h {k}")
    seen = set()
    for g in gaps:
        if g[1] in seen:
            continue
        seen.add(g[1])
        print("   GAP " + json.dumps(g))
        if len(seen) >= 20:
            break
    print(f"\ndone in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
