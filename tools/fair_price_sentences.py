"""Real data: the fair-price sentences in open events' rules that aren't a known cancel clause
(kal/strategies/base.py CANCELLATION_CLAUSES), most common first. Public data only."""
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal.client import Kalshi  # noqa: E402
from kal.strategies import base  # noqa: E402


def shape(s):
    """The sentence with names, numbers and dates blanked, so one clause written for many
    games counts once."""
    s = re.sub(r"\d+(?:[:.,]\d+)*", "#", s)
    return s


def main():
    events = list(Kalshi().events(status="open", with_nested_markets="true"))
    count, series, example, evs = collections.Counter(), collections.defaultdict(set), {}, collections.Counter()
    skipped = 0
    for ev in events:
        ms = ev.get("markets") or []
        if not ms or base.plain_payouts(ms) or base.fair_price_only_if_cancelled(ms):
            continue
        if any(m.get("market_type", "binary") != "binary" or m.get("settlement_bounds_type", "default") != "default"
               for m in ms):
            continue
        skipped += 1
        sk = ev.get("series_ticker") or ev.get("event_ticker", "").split("-")[0]
        seen = set()
        for m in ms[:3]:
            for field in ("rules_primary", "rules_secondary"):
                for s in map(base._plain, base._sentences(m.get(field))):
                    if base.FAIR_PRICE.search(s) and not any(c.fullmatch(s) for c in base.CANCELLATION_CLAUSES):
                        k = shape(s)
                        if k in seen:
                            continue
                        seen.add(k)
                        count[k] += 1
                        series[k].add(sk)
                        example.setdefault(k, s)
                        if ev.get("mutually_exclusive"):
                            evs[k] += 1
    print(f"open events: {len(events)}; skipped for a fair-price sentence: {skipped}")
    for k, n in count.most_common(40):
        print(f"\n[{n} events, {evs[k]} one-winner, {len(series[k])} series: {', '.join(sorted(series[k])[:6])}]")
        print(f"  {example[k]!r}")


if __name__ == "__main__":
    main()
