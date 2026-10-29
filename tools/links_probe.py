"""Idea #6, other links, on real data: series whose rules nest, so their prices must agree.
Public data only (no key, no orders). Probe branch only, never merged.

Each pair (A, B) below is picked by hand: a YES on A means a YES on B (the champion won its
conference; a conference champion played in its title game). So YES on B plus NO on A pays
at least $1 whatever happens. A gap is an hour where that pair cost under $1 after fees.

For each pair, open and settled seasons are matched by team name (yes_sub_title, exact).
Prints: rules of each series (first market), settled results that break the nesting,
today's gaps, and hourly gaps in the last 45 days before each season's final settlement,
counted as the bot's rules count locked-in trades (pays within max_arb_days, at least
min_arb_apr a year, at least 1% return).
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

PAIRS = [   # (A, B): YES on A means YES on B
    ("KXNBA", "KXNBAEAST"), ("KXNBA", "KXNBAWEST"), ("KXNBA", "KXNBAPLAYOFF"),
    ("KXNBAEAST", "KXNBAECFQUAL"), ("KXNBAWEST", "KXNBAWCFQUAL"),
    ("KXNBAECFQUAL", "KXNBAPLAYOFF"), ("KXNBAWCFQUAL", "KXNBAPLAYOFF"),
    ("KXNBAEAST1SEED", "KXNBAPLAYOFF"), ("KXNBAWEST1SEED", "KXNBAPLAYOFF"),
    ("KXSB", "KXNFLAFCCHAMP"), ("KXSB", "KXNFLNFCCHAMP"), ("KXSB", "KXNFLPLAYOFF"),
    ("KXNFLAFCCHAMP", "KXNFLPLAYOFF"), ("KXNFLNFCCHAMP", "KXNFLPLAYOFF"),
    ("KXNFL1SEED", "KXNFLPLAYOFF"), ("KXNFL1SEED", "KXNFLPLAYOFFHOST"), ("KXNFLPLAYOFFHOST", "KXNFLPLAYOFF"),
    ("KXNHL", "KXNHLPLAYOFF"), ("KXNHL", "KXNHLEAST"), ("KXNHL", "KXNHLWEST"),
    ("KXMLB", "KXMLBAL"), ("KXMLB", "KXMLBNL"), ("KXMLB", "KXMLBPLAYOFF"),
    ("KXMLSCUP", "KXMLSEAST"), ("KXMLSCUP", "KXMLSWEST"),
    ("KXNCAAF", "KXNCAAFSF"), ("KXNCAAF", "KXNCAAFQF"), ("KXNCAAF", "KXNCAAFPLAYOFF"),
    ("KXNCAAFSF", "KXNCAAFQF"), ("KXNCAAFSF", "KXNCAAFPLAYOFF"), ("KXNCAAFQF", "KXNCAAFPLAYOFF"),
    ("KXNCAAFTOPSEED", "KXNCAAFPLAYOFF"),
    ("KXNCAAFAAC", "KXNCAAFAACQUAL"), ("KXNCAAFACC", "KXNCAAFACCQUAL"), ("KXNCAAFB10", "KXNCAAFB10QUAL"),
    ("KXNCAAFB12", "KXNCAAFB12QUAL"), ("KXNCAAFCUSA", "KXNCAAFCUSAQUAL"), ("KXNCAAFMAC", "KXNCAAFMACQUAL"),
    ("KXNCAAFMWC", "KXNCAAFMWCQUAL"), ("KXNCAAFPAC12", "KXNCAAFPAC12QUAL"), ("KXNCAAFSBELT", "KXNCAAFSBELTQUAL"),
    ("KXNCAAFSEC", "KXNCAAFSECQ"),
    ("KXNCAAMBACC", "KXNCAAMBACCQUAL"), ("KXNCAAMBBIG12", "KXNCAAMBBIG12QUAL"),
    ("KXNCAAMBBIGEAST", "KXNCAAMBBIGEASTQUAL"), ("KXNCAAMBBIG10", "KXNCAAMBBIGTENQUAL"),
]
H, DAY = 3600, 86400


def close_of(m):
    try:
        return ts(m["close_time"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def seasons(c, s):
    """({event: [markets]} open, {event: [markets]} settled) for a series."""
    live, done = collections.defaultdict(list), collections.defaultdict(list)
    for ev in c.events(series_ticker=s, status="open", with_nested_markets="true"):
        live[ev["event_ticker"]] += ev.get("markets") or []
    for m in c.settled_markets(s, limit=600):
        done[m.get("event_ticker")].append(m)
    return live, done


def match(ea, eb):
    """Pair each settled season of A with the settled season of B it nests in: B settles no
    later than A (plus a day) and no more than 250 days before it."""
    out = []
    for ka, ma in ea.items():
        ca = max(filter(None, (close_of(m) for m in ma)), default=None)
        if ca is None:
            continue
        best = None
        for kb, mb in eb.items():
            cb = max(filter(None, (close_of(m) for m in mb)), default=None)
            if cb is not None and ca - 250 * DAY <= cb <= ca + DAY and (best is None or cb > best[0]):
                best = (cb, kb)
        if best:
            out.append((ka, best[1]))
    return out


def main():
    cfg = config.load()
    c = Kalshi()
    now, t0 = time.time(), time.time()
    cache, rules_shown = {}, set()
    totals = collections.Counter()
    gaps_now, gap_hours, near, near_now = [], [], [], []
    per_pair = collections.Counter()
    broken = []
    for a, b in PAIRS:
        for s in (a, b):
            if s not in cache:
                try:
                    cache[s] = seasons(c, s)
                except Exception as e:
                    print(f"{s}: failed: {e}")
                    cache[s] = ({}, {})
        (la, ea), (lb, eb) = cache[a], cache[b]
        if not (la or ea) or not (lb or eb):
            print(f"pair {a} -> {b}: missing")
            continue
        # Open seasons pair with open seasons (Kalshi gives open futures placeholder close
        # times, years out), settled ones by when they settled.
        both = [(ka, kb, la, lb) for ka in la for kb in lb] if len(la) == 1 and len(lb) == 1 else []
        both += [(ka, kb, ea, eb) for ka, kb in match(ea, eb)]
        totals["open season pairs"] += len(both) - len(match(ea, eb))
        for ka, kb, ea, eb in both:
            ma = {m.get("yes_sub_title"): m for m in ea[ka] if m.get("yes_sub_title")}
            mb = {m.get("yes_sub_title"): m for m in eb[kb] if m.get("yes_sub_title")}
            teams = set(ma) & set(mb)
            totals["season pairs"] += 1
            totals["team pairs"] += len(teams)
            for team in teams:
                x, y = ma[team], mb[team]
                if x.get("result") == "yes" and y.get("result") == "no":
                    broken.append((a, b, ka, team))
                    continue
                if x.get("status") in (None, "active", "open") and y.get("status") in (None, "active", "open"):
                    bid_a, ask_b = price(x, "yes_bid"), price(y, "yes_ask")
                    if bid_a and ask_b and 0 < bid_a < 1 and 0 < ask_b < 1:
                        totals["team pairs priced now"] += 1
                        near_now.append((round(bid_a - ask_b, 4), a, b, team, bid_a, ask_b))
                        cost = ask_b + (1 - bid_a) + fee(1, ask_b) + fee(1, round(1 - bid_a, 4))
                        if cost < 1:
                            gaps_now.append((round(1 - cost, 4), a, b, team, bid_a, ask_b, x.get("close_time")))
                ca, cb = close_of(x), close_of(y)
                if ca is None or cb is None:
                    continue
                pay = max(ca, cb)                    # $1 is locked in once both have paid
                end = min(ca, cb, now)               # both must still trade
                start = pay - 45 * DAY
                if end <= start:
                    continue
                try:
                    cx = {k["end_period_ts"]: k for k in c.candles(a, x["ticker"], int(start), int(end)) or []}
                    cy = {k["end_period_ts"]: k for k in c.candles(b, y["ticker"], int(start), int(end)) or []}
                except Exception as e:
                    totals["candle errors"] += 1
                    continue
                totals["candle pairs"] += 1
                for t in sorted(set(cx) & set(cy)):
                    bid_a, ask_b = candle_close(cx[t], "yes_bid"), candle_close(cy[t], "yes_ask")
                    if not (0 < bid_a < 1 and 0 < ask_b < 1):
                        continue
                    totals["pair-hours"] += 1
                    if bid_a > ask_b:
                        totals["hours crossed before fees"] += 1
                    near.append((round(bid_a - ask_b, 4), a, b, ka, team,
                                 time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)), bid_a, ask_b))
                    if len(near) > 5000:
                        near.sort(reverse=True)
                        del near[200:]
                    cost = ask_b + (1 - bid_a) + fee(1, ask_b) + fee(1, round(1 - bid_a, 4))
                    if cost >= 1:
                        continue
                    totals["gap hours"] += 1
                    days = max((pay - t) / DAY, 1 / 24)
                    apr = yearly_return(1 / cost, days)
                    good = days <= cfg["max_arb_days"] and apr >= cfg["min_arb_apr"] and (1 - cost) / cost >= 0.01
                    if good:
                        totals["passing hours"] += 1
                        per_pair[(a, b, ka, team)] += 1
                    gap_hours.append((round(1 - cost, 4), a, b, ka, team, time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)),
                                      bid_a, ask_b, round(days, 1), round(apr, 2), good))
    print(f"\ntotals: {dict(totals)}")
    near.sort(reverse=True)
    near_now.sort(reverse=True)
    print("closest hours (champion's YES bid minus the narrower YES ask, before fees; above 0 = crossed):")
    for x in near[:10]:
        print("  NEAR " + json.dumps(dict(zip(("bid_minus_ask", "A", "B", "season", "team", "hour", "A_yes_bid", "B_yes_ask"), x))))
    print("closest open pairs now:")
    for x in near_now[:10]:
        print("  NEARNOW " + json.dumps(dict(zip(("bid_minus_ask", "A", "B", "team", "A_yes_bid", "B_yes_ask"), x))))
    print(f"settled results that break a link (A yes, B no): {len(broken)} {broken[:10]}")
    gaps_now.sort(reverse=True)
    print(f"\ngaps now (open markets, after fees): {len(gaps_now)}")
    for g in gaps_now[:20]:
        print("  NOW " + json.dumps(dict(zip(("edge", "A", "B", "team", "A_yes_bid", "B_yes_ask", "A_close"), g))))
    gap_hours.sort(reverse=True)
    print(f"\nhistory: {totals['gap hours']} gap hours; {totals['passing hours']} pass the bot's rules, "
          f"in {len(per_pair)} team-seasons")
    for k, n in per_pair.most_common(20):
        print(f"  PASS {n:>4}h {k}")
    for g in gap_hours[:30]:
        print("  GAP " + json.dumps(dict(zip(("edge", "A", "B", "season", "team", "hour", "A_yes_bid", "B_yes_ask",
                                               "days_to_pay", "apr", "passes"), g))))
    print(f"\ndone in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
