"""Dry run of metal_link_arb on the real API (public data only: no key, no orders).

Mode "series": reads the open gold and silver events (six series), pairs them, plays every set
out, then runs the strategy's scan a few times with a client that counts order book reads.
Mode "full": one round on the bot's own lean list of all open events (engine.RunCache), to see
the fields the strategy needs survive it. Nothing is bought or stored.
"""
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal import config, engine  # noqa: E402
from kal.client import Kalshi  # noqa: E402
from kal.strategies import metal_link_arb as ml  # noqa: E402
from kal.strategies.base import Context  # noqa: E402

SERIES = [s for mon, info in ml.METALS.items() for s in (mon, *info["others"])]


class Counting:
    """events() gives the events read once a round; order books come from the real API."""

    def __init__(self, client, events):
        self.client, self.event_list, self.books = client, events, 0

    def events(self, **params):
        assert params == {"status": "open", "with_nested_markets": "true"}, params
        return self.event_list

    def orderbook(self, ticker):
        self.books += 1
        return self.client.orderbook(ticker)


def metal_events(c):
    out = []
    for s in SERIES:
        out += c.events(series_ticker=s, status="open", with_nested_markets="true")
    return out


def look(c, evs, rnd):
    t1 = time.time()
    why = []
    found = ml.pairs(evs, why)
    mine = [e for e in evs if isinstance(e, dict) and str(e.get("event_ticker", "")).split("-")[0] in SERIES]
    print(f"   metal events in the list: {len(mine)} "
          f"{dict(collections.Counter(e['event_ticker'].split('-')[0] for e in mine))}; pairs {len(found)} "
          f"({time.time() - t1:.2f}s)")
    for e in mine:
        if rnd == 0:
            ms = e.get("markets") or []
            print(f"   EVENT {e['event_ticker']}: markets {len(ms)}, statuses "
                  f"{dict(collections.Counter(m.get('status') for m in ms))}, strike_date {e.get('strike_date')}, "
                  f"sources {json.dumps(e.get('settlement_sources'))}")
    for et, reason in why:
        print(f"   SKIPPED {et}: {reason}")
    sets = bad = hot = 0
    kinds = collections.Counter()
    for p in found:
        every = ml.links(p)
        best = None
        for s in every:
            sets += 1
            kinds[s["set"]] += 1
            if not ml.check(p, s["legs"], s["pays"]):
                bad += 1
                print("   CHECK FAILED", p["monthly"]["event"], [(m["ticker"], side) for m, side in s["legs"]])
            if s["listed"] is not None and (best is None or s["listed"] < best["listed"]):
                best = s
            if s["listed"] is not None and s["listed"] < s["pays"]:
                hot += 1
                print(f"   HOT {p['monthly']['event']} {s['set']} listed {s['listed']:.4f}: "
                      + " + ".join(f"{side.upper()} {m['ticker']}" for m, side in s["legs"]))
        mon, oth = p["monthly"], p["other"]
        print(f"   PAIR {mon['event']} ~ {oth['event']}: {len(mon['markets'])}/{len(oth['markets'])} markets, "
              f"{len(every)} sets, at {mon['say']}, source {mon['source'][1]}; cheapest listed "
              + (f"{best['listed']:.4f}: " + " + ".join(f"{side.upper()} {m['ticker']}" for m, side in best["legs"])
                 if best else "none"))
    print(f"   sets {sets} {dict(kinds)}: {bad} failed the play-out; {hot} under $1 by listed price (before fees)")
    client = Counting(c, evs)
    cfg = dict(config.DEFAULTS, max_per_arb=30.0)
    ctx = Context(client, cfg, "paper", 100.0, 100.0, set(), {}, {})
    t5 = time.time()
    sigs = ml.STRATEGY.scan(ctx)
    print(f"   scan: {client.books} order books, {len(sigs)} signals, {time.time() - t5:.1f}s")
    for s in sigs:
        print(f"   SIGNAL {s.meta['set']} cost ${s.cost:.2f} pays ${s.payout_min:.2f} "
              f"({s.payout_min / s.cost - 1:+.2%}) paper_only={s.meta['paper_only']} closes_at "
              f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(s.closes_at))}: {s.note}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "series"
    c = Kalshi()
    if mode == "full":
        t0 = time.time()
        rc = engine.RunCache(c)
        rc.scope((), engine.horizon_days(config.DEFAULTS))
        evs = rc.events(status="open", with_nested_markets="true")
        print(f"\n== the bot's lean list: {len(evs)} events, {sum(len(e.get('markets') or []) for e in evs)} markets "
              f"in {time.time() - t0:.0f}s")
        look(c, evs, 0)
        return
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    gap = float(sys.argv[3]) if len(sys.argv) > 3 else 60
    for rnd in range(rounds):
        t0 = time.time()
        evs = metal_events(c)
        print(f"\n== round {rnd} at {time.strftime('%H:%M:%S', time.gmtime())} UTC: {len(evs)} events, "
              f"{sum(len(e.get('markets') or []) for e in evs)} markets; read {time.time() - t0:.1f}s")
        look(c, evs, rnd)
        if rnd < rounds - 1:
            time.sleep(gap)


if __name__ == "__main__":
    main()
