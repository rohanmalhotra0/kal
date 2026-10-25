"""Maker research on real Kalshi history (public data, no key, no orders).

Question: if you rested a one-contract bid instead of buying at the ask, some hours
before each market's scheduled end, did the fills make money after the maker fee? See
kal/research/maker.py for the fill rule and why one contract is the best case. Busiest
active series. The cautious bound counts fills in one category on one day as one unit,
and splits 95% confidence over every side, price range, rule, table and --hours value.

  python tools/maker_research.py --series-per-category 10 --markets-per-series 150 --hours 6
"""
import argparse
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal.client import Kalshi  # noqa: E402
from kal.research import backtest, maker  # noqa: E402
from kal.research.discover import active_series, research_dir  # noqa: E402
from kal.strategies.favorites import group_of  # noqa: E402


def pct(x):
    return "   n/a" if x is None else f"{x:+6.1%}"


def show(title, rows):
    print(f"\n### {title}")
    print("side  bid range    bids  fills units   win   ROI    worst  lead?")
    for r in rows:
        win = "  n/a" if r["win"] is None else f"{r['win']:5.0%}"
        print(f"{r['side']:4} {r['lo']:.2f}-{r['hi']:.2f} {r['bids']:7} {r['fills']:6} {r['units']:5} {win} "
              f"{pct(r['roi'])} {pct(r['roi_lo'])}  {'LEAD' if maker.is_lead(r) else ''}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series-per-category", type=int, default=10)
    ap.add_argument("--markets-per-series", type=int, default=150)
    ap.add_argument("--hours", type=float, nargs="+", default=[6])
    ap.add_argument("--max-minutes", type=float, default=150)
    args = ap.parse_args()
    t0 = time.time()
    deadline = t0 + args.max_minutes * 60
    client = Kalshi()
    info = {s["ticker"]: s for s in client.series() if s.get("ticker")}
    by_cat = collections.defaultdict(list)
    for t in active_series(client, pool=2000):   # busiest first: where an edge could be traded
        s = info.get(t, {})
        if s.get("category") and s.get("frequency") != "one_off":
            by_cat[s["category"]].append(t)
    loaded = {}   # hours -> category -> rows
    for h in args.hours:
        loaded[h] = {}
        for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
            obs = []
            for s in by_cat[cat][:args.series_per_category]:
                if time.time() > deadline:
                    break
                try:
                    got = maker.load(client, s, h, max_markets=args.markets_per_series, deadline=deadline)
                    for o in got:   # related series move together: one category's day is one unit
                        o["unit"] = group_of(cat, o["anchor"])
                    obs += got
                    traded = sum(1 for o in got if o["lo"] is not None)
                    print(f"{h:g}h {cat:22} {s:30} {len(got):5} priced, {traded:5} traded after the decision",
                          flush=True)
                except Exception as e:
                    print(f"{h:g}h {cat:22} {s:30} skipped: {e}", flush=True)
            loaded[h][cat] = obs
    if time.time() > deadline:
        print("time budget reached: the tables use what loaded in time")
    tables = {h: {name: obs for name, obs in [("ALL", [o for v in cats.values() for o in v])]
                  + sorted(cats.items()) if len(obs) >= 30} for h, cats in loaded.items()}
    # Every side x price range x rule x table x hours value is a separate try.
    tries = 2 * (len(backtest.BUCKETS) - 1) * len(maker.RULES) * max(1, sum(len(t) for t in tables.values()))
    z = backtest.z_for(tries)
    print(f"\nz = {z:.2f} for {tries} tries, {(time.time() - t0) / 60:.0f} min")
    print("One-contract bids, filled only when a later trade went through them (or, for 'improve',")
    print("traded at them). That's a maker's best case: a LEAD needs a check with real trade sizes.")
    os.makedirs(research_dir(), exist_ok=True)
    for h, by_name in tables.items():
        everything = by_name.get("ALL", [])
        print(f"\n===== {h:g} hours before the scheduled end: {len(everything)} priced markets, "
              f"{len({o['unit'] for o in everything})} category-days")
        summary = {"hours_before_close": h, "z": z, "tries": tries, "tables": {}}
        for name, obs in by_name.items():
            summary["tables"][name] = {"markets": len(obs)}
            for rule in maker.RULES:
                rows = maker.table(obs, rule, z=z)
                show(f"{h:g}h {name}: {rule} the best bid ({len(obs)} markets)", rows)
                summary["tables"][name][rule] = rows
        path = os.path.join(research_dir(), f"maker_{h:g}h.json")
        with open(path, "w") as f:
            json.dump(summary, f, indent=1)
        print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
