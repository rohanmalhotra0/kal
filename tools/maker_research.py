"""Maker research on real Kalshi history (public data, no key, no orders).

Question: if you rested a bid instead of buying at the ask, some hours before each
market's scheduled end, did the fills make money after the maker fee? Busiest active
series, the same cautious bound as tools/deep_research.py (Bonferroni over every
side, price range, rule and table). See kal/research/maker.py for the fill rule.

  python tools/maker_research.py --series-per-category 10 --markets-per-series 150
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


def pct(x):
    return "   n/a" if x is None else f"{x:+6.1%}"


def show(title, rows):
    print(f"\n### {title}")
    print("side  bid range    bids  fills events   win   ROI    worst  if-all  edge?")
    for r in rows:
        win = "  n/a" if r["win"] is None else f"{r['win']:5.0%}"
        print(f"{r['side']:4} {r['lo']:.2f}-{r['hi']:.2f} {r['bids']:7} {r['fills']:6} {r['events']:6} {win} "
              f"{pct(r['roi'])} {pct(r['roi_lo'])} {pct(r['roi_if_all_filled'])}  {'EDGE' if maker.is_edge(r) else ''}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series-per-category", type=int, default=10)
    ap.add_argument("--markets-per-series", type=int, default=150)
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--max-minutes", type=float, default=150)
    args = ap.parse_args()
    t0 = time.time()
    client = Kalshi()
    info = {s["ticker"]: s for s in client.series() if s.get("ticker")}
    by_cat = collections.defaultdict(list)
    for t in active_series(client, pool=2000):   # busiest first: where an edge could be traded
        s = info.get(t, {})
        if s.get("category") and s.get("frequency") != "one_off":
            by_cat[s["category"]].append(t)
    obs_by_cat = {}
    for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
        obs = []
        for s in by_cat[cat][:args.series_per_category]:
            if time.time() - t0 > args.max_minutes * 60:
                print(f"time budget reached; stopping at {cat}/{s}")
                break
            try:
                got = maker.load(client, s, args.hours, max_markets=args.markets_per_series)
                obs += got
                traded = sum(1 for o in got if o["lo"] is not None)
                print(f"{cat:22} {s:30} {len(got):5} priced, {traded:5} traded after the decision", flush=True)
            except Exception as e:
                print(f"{cat:22} {s:30} skipped: {e}", flush=True)
        obs_by_cat[cat] = obs
    tables = {name: obs for name, obs in [("ALL", [o for v in obs_by_cat.values() for o in v])]
              + sorted(obs_by_cat.items()) if len(obs) >= 30}
    # Every side x price range x rule x table is a separate try.
    tries = 2 * (len(backtest.BUCKETS) - 1) * len(maker.RULES) * max(1, len(tables))
    z = backtest.z_for(tries)
    everything = tables.get("ALL", [])
    print(f"\n{len(everything)} priced markets, {len({o['event'] for o in everything})} events, "
          f"{len(tables) - 1} categories, z = {z:.2f} for {tries} tries, {(time.time() - t0) / 60:.0f} min")
    print("Fills: only when a later trade went THROUGH the bid. 'if-all' = ROI if every bid had filled.")
    summary = {"hours_before_close": args.hours, "z": z, "tries": tries, "tables": {}}
    for name, obs in tables.items():
        out = {}
        for rule in maker.RULES:
            rows = maker.table(obs, rule, z=z)
            show(f"{name}: {rule} the best bid ({len(obs)} markets)", rows)
            out[rule] = rows
        # Upper bound on fills (a trade AT the bid counts too). Information only, never an edge.
        out["join_touch"] = maker.table(obs, "join", touch=True, z=z)
        if name == "ALL":
            show(f"{name}: join, also counting trades AT the bid (upper bound, not a test)", out["join_touch"])
        summary["tables"][name] = {"markets": len(obs), **out}
    os.makedirs(research_dir(), exist_ok=True)
    path = os.path.join(research_dir(), f"maker_{int(args.hours)}h.json")
    with open(path, "w") as f:
        json.dump(summary, f, indent=1)
    print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
