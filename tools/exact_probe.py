"""Idea #17: "exactly X%" (KXECONSTAT*) vs "above X%" (KXCPI* / KXU3) on real data.
Probe branch only. Public data only (no key, no orders).

Values are reported to one decimal, so exactly(X) = above(X - 0.1) and not above(X).
YES exactly(X) + YES above(X) + NO above(X - 0.1) pays exactly $1 whatever the number.
Matches release months, checks settled results agree, and counts hourly gaps after fees
in the 10 days before close, the bot's way.
"""
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

PAIRS = [("KXECONSTATCPI", "KXCPI"), ("KXECONSTATCPICORE", "KXCPICORE"), ("KXECONSTATCPIYOY", "KXCPIYOY"),
         ("KXECONSTATCORECPIYOY", "KXCPICOREYOY"), ("KXECONSTATU3", "KXU3")]
H, DAY, N = 3600, 86400, 30
MON = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
MONTH = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (\d{4})", re.I)


def fee_pc(p):
    return fee(N, p) / N


def month_of(m):
    """(year, month) the number is for, from the rules text."""
    g = MONTH.search(str(m.get("rules_primary") or ""))
    return (int(g[2]), MON[g[1].lower()[:3]]) if g else None


def value(text, kind):
    g = re.search(r"exactly (-?\d+(?:\.\d+)?)%" if kind == "exact" else r"(?:more than|above) (-?\d+(?:\.\d+)?)%",
                  str(text or ""), re.I)
    return round(float(g[1]), 1) if g else None


def all_markets(c, s):
    out = {}
    for m in c.settled_markets(s, limit=3000):
        out[m["ticker"]] = m
    for ev in c.events(series_ticker=s, status="open", with_nested_markets="true"):
        for m in ev.get("markets") or []:
            out[m["ticker"]] = m
    return list(out.values())


def main():
    cfg = config.load()
    c = Kalshi()
    t0, now = time.time(), time.time()
    tally, broken, near, gaps, passing = collections.Counter(), [], {}, [], collections.Counter()
    for a, b in PAIRS:
        try:
            ma, mb = all_markets(c, a), all_markets(c, b)
        except Exception as e:
            print(f"{a}/{b}: failed: {e}")
            continue
        ex = collections.defaultdict(dict)    # month -> value -> market
        ab = collections.defaultdict(dict)
        for m in ma:
            mo, v = month_of(m), value(m.get("rules_primary"), "exact")
            if mo and v is not None:
                ex[mo][v] = m
        for m in mb:
            mo, v = month_of(m), value(m.get("rules_primary"), "above")
            if mo and v is not None:
                ab[mo][v] = m
        months = sorted(set(ex) & set(ab))
        print(f"\n== {a} vs {b}: months with both {len(months)} (exact {len(ex)}, above {len(ab)})")
        if months:
            x = next(iter(ex[months[-1]].values()))
            y = next(iter(ab[months[-1]].values()))
            print("   A " + " ".join(str(x.get('rules_primary')).split())[:300])
            print("   A2 " + " ".join(str(x.get('rules_secondary')).split())[:300])
            print("   B " + " ".join(str(y.get('rules_primary')).split())[:300])
        for mo in months:
            for v, e in ex[mo].items():
                hi, lo = ab[mo].get(v), ab[mo].get(round(v - 0.1, 1))
                if not hi or not lo:
                    continue
                tally[f"{a}: baskets"] += 1
                r = (e.get("result"), hi.get("result"), lo.get("result"))
                if all(x in ("yes", "no") for x in r):
                    tally["settled baskets"] += 1
                    pays = (r[0] == "yes") + (r[1] == "yes") + (r[2] == "no")
                    if pays != 1:
                        broken.append((e["ticker"], hi["ticker"], lo["ticker"], r))
                try:
                    closes = [ts(m["close_time"]) for m in (e, hi, lo)]
                except Exception:
                    continue
                pay, end = max(closes), min(min(closes), now)
                start = end - 10 * DAY
                try:
                    ks = [{k["end_period_ts"]: k for k in c.candles(s, m["ticker"], int(start), int(end)) or []}
                          for s, m in ((a, e), (b, hi), (b, lo))]
                except Exception:
                    tally["candle errors"] += 1
                    continue
                name = f"YES {e['ticker']} + YES {hi['ticker']} + NO {lo['ticker']}"
                for t in sorted(set(ks[0]) & set(ks[1]) & set(ks[2])):
                    p = [candle_close(ks[0][t], "yes_ask"), candle_close(ks[1][t], "yes_ask"),
                         round(1 - candle_close(ks[2][t], "yes_bid"), 4)]
                    if not all(0 < x < 1 for x in p):
                        continue
                    tally["basket-hours"] += 1
                    cost = sum(p) + sum(fee_pc(x) for x in p)
                    if name not in near or 1 - cost > near[name][0]:
                        near[name] = (round(1 - cost, 4), name, time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)), p)
                    if cost >= 1:
                        continue
                    tally["gap hours after fees"] += 1
                    days = max((pay - t) / DAY, 1 / 24)
                    apr = yearly_return(1 / cost, days)
                    ok = days <= cfg["max_arb_days"] and apr >= cfg["min_arb_apr"] and (1 - cost) / cost >= 0.01
                    if ok:
                        tally["passing hours"] += 1
                        passing[name] += 1
                    gaps.append((round(1 - cost, 4), name, time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)), p,
                                 round(days, 1), round(apr, 2), ok))
            if time.time() - t0 > 35 * 60:
                print("(time limit)")
                break
    print(f"\ntotals: {dict(tally)}")
    print(f"settled baskets that didn't pay exactly $1: {len(broken)}")
    for x in broken[:15]:
        print("   BROKEN " + json.dumps(x))
    print("closest:")
    for x in sorted(near.values(), reverse=True)[:12]:
        print("   NEAR " + json.dumps(x))
    print(f"gap hours after fees: {len(gaps)}; passing: {sum(passing.values())} hours in {len(passing)} baskets")
    for k, v in passing.most_common(15):
        print(f"   PASS {v:>3}h {k}")
    seen = set()
    for g in sorted(gaps, reverse=True):
        if g[1] not in seen:
            seen.add(g[1])
            print("   GAP " + json.dumps(g))
        if len(seen) >= 15:
            break
    print(f"\ndone in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
