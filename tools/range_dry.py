"""Dry run of range_link_arb on the real API (public data only: no key, no orders).

Reads the open crypto events, pairs them, plays every set out, then runs the strategy's scan
a few times with a client that counts order book reads. Nothing is bought or stored.
"""
import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal import config  # noqa: E402
from kal.client import Kalshi  # noqa: E402
from kal.strategies import range_link_arb as rl  # noqa: E402
from kal.strategies.base import Context  # noqa: E402


class Counting:
    """events() gives the crypto events read once a round; order books come from the real API."""

    def __init__(self, client, events):
        self.client, self.event_list, self.books = client, events, 0

    def events(self, **params):
        assert params == {"status": "open", "with_nested_markets": "true"}, params
        return self.event_list

    def orderbook(self, ticker):
        self.books += 1
        return self.client.orderbook(ticker)


def crypto_events(c):
    out = []
    for r, coin in rl.COINS.items():
        for s in (r, coin["above"]):
            out += c.events(series_ticker=s, status="open", with_nested_markets="true")
    return out


def main():
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    gap = float(sys.argv[3]) if len(sys.argv) > 3 else 60
    c = Kalshi()
    for rnd in range(rounds):
        t0 = time.time()
        evs = crypto_events(c)
        t1 = time.time()
        why = []
        found = rl.pairs(evs, why)
        t2 = time.time()
        by = collections.Counter(p["series"] for p in found)
        print(f"\n== round {rnd} at {time.strftime('%H:%M:%S', time.gmtime())}: {len(evs)} events, "
              f"{sum(len(e.get('markets') or []) for e in evs)} markets; read {t1 - t0:.1f}s, paired {t2 - t1:.2f}s")
        print(f"   pairs by series: {dict(by)}")
        for et, reason in why:
            print(f"   SKIPPED {et}: {reason}")
        sets = bad = hot = 0
        kinds = collections.Counter()
        t3 = time.time()
        for p in found:
            every = rl.links(p)
            for s in every:
                sets += 1
                kinds[s["set"]] += 1
                if not rl.check(p, s["legs"], s["pays"]):
                    bad += 1
                    print("   CHECK FAILED", p["range"]["event"], s["set"], [(m["ticker"], side) for m, side in s["legs"]])
                if s["listed"] is not None and s["listed"] < s["pays"]:
                    hot += 1
                    print(f"   HOT {p['range']['event']} {s['set']} listed {s['listed']:.4f} for ${s['pays']}: "
                          + " + ".join(f"{side.upper()} {m['ticker']}" for m, side in s["legs"]))
            if rnd == 0:
                print(f"   pair {p['range']['event']} ~ {p['above']['event']}: {len(p['brackets'])} brackets, "
                      f"{len(p['ladder'])} rungs, {len(every)} sets, closes {time.strftime('%m-%d %H:%M', time.gmtime(p['close']))}, "
                      f"at {p['range']['say']}")
        t4 = time.time()
        print(f"   sets {sets} {dict(kinds)}; every one played out in {t4 - t3:.1f}s: {bad} failed; "
              f"{hot} under their payout by listed price (before fees)")
        client = Counting(c, evs)
        cfg = dict(config.DEFAULTS, max_per_arb=30.0)
        ctx = Context(client, cfg, "paper", 100.0, 100.0, set(), {}, {})
        t5 = time.time()
        sigs = rl.STRATEGY.scan(ctx)
        print(f"   scan: {client.books} order books, {len(sigs)} signals, {time.time() - t5:.1f}s")
        for s in sigs:
            print(f"   SIGNAL {s.meta['set']} cost ${s.cost:.2f} pays ${s.payout_min:.2f} "
                  f"({s.payout_min / s.cost - 1:+.2%}) paper_only={s.meta['paper_only']}: {s.note}")
        if rnd < rounds - 1:
            time.sleep(gap)


if __name__ == "__main__":
    main()
