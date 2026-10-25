"""Deep research on real Kalshi history (public data, no key, no orders).

Question: if you bought every contract in a price range some hours before its event's
scheduled close, did you make money after fees? Answered per category and per side
(YES vs NO), with the same event-clustered, Bonferroni-corrected lower bound the bot
uses. Prints tables; saves a summary JSON.

  python tools/deep_research.py --series-per-category 10 --markets-per-series 150
"""
import argparse
import collections
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal import config  # noqa: E402
from kal.client import Kalshi  # noqa: E402
from kal.research import backtest  # noqa: E402

MIN_PRICE, MAX_PRICE = 0.01, 0.99


def side_trades(obs, max_spread):
    """{(side, bucket): [trade]} where side is 'yes' (buy YES at ask) or 'no' (buy NO at 1 - bid)."""
    out = collections.defaultdict(list)
    for o in obs:
        if max_spread is not None and not (0 < o["yes_bid"] and o["yes_ask"] - o["yes_bid"] <= max_spread + 1e-9):
            continue
        for side, p, won in (("yes", o["yes_ask"], o["result"] == "yes"),
                             ("no", round(1 - o["yes_bid"], 4) if o["yes_bid"] > 0 else None, o["result"] == "no")):
            if p is not None and MIN_PRICE <= p <= MAX_PRICE:
                out[(side, backtest.bucket_of(p))].append(backtest.trade(o["event"], p, won))
    return out


def table(obs, max_spread, z):
    rows = []
    for (side, b), t in sorted(side_trades(obs, max_spread).items()):
        roi, lo = backtest.roi_stats(t, z)
        rows.append({"side": side, "lo": backtest.BUCKETS[b], "hi": backtest.BUCKETS[b + 1], "trades": len(t),
                     "events": len({x[0] for x in t}), "win": sum(1 for x in t if x[2] > 0) / len(t),
                     "avg_price": sum(x[1] for x in t) / len(t), "roi": roi, "roi_lo": lo})
    return rows


def pct(x):
    return "   n/a" if x is None else f"{x:+6.1%}"


def show(title, rows):
    print(f"\n### {title}")
    print("side  price      trades events   win   ROI    worst  edge?")
    for r in rows:
        edge = r["roi_lo"] is not None and r["roi_lo"] > 0 and r["trades"] >= 30 and r["events"] >= 10
        print(f"{r['side']:4} {r['lo']:.2f}-{r['hi']:.2f} {r['trades']:7} {r['events']:6} {r['win']:6.1%} "
              f"{pct(r['roi'])} {pct(r['roi_lo'])}  {'EDGE' if edge else ''}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series-per-category", type=int, default=10)
    ap.add_argument("--markets-per-series", type=int, default=150)
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--max-minutes", type=float, default=150)
    args = ap.parse_args()
    t0 = time.time()
    client = Kalshi()
    by_cat = collections.defaultdict(list)
    for s in client.series():
        if s.get("ticker") and s.get("category") and not s["ticker"].startswith("KXMVE"):
            by_cat[s["category"]].append(s["ticker"])
    rng = random.Random(args.seed)
    obs_by_cat = {}
    for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
        picked = rng.sample(sorted(by_cat[cat]), min(args.series_per_category, len(by_cat[cat])))
        obs = []
        for s in picked:
            if time.time() - t0 > args.max_minutes * 60:
                print(f"time budget reached; stopping at {cat}/{s}")
                break
            try:
                got = backtest.load_observations(client, s, args.hours, max_markets=args.markets_per_series)
                obs += got
                print(f"{cat:22} {s:30} {len(got):5} priced markets", flush=True)
            except Exception as e:
                print(f"{cat:22} {s:30} skipped: {e}", flush=True)
        obs_by_cat[cat] = obs
    # Each table tests 2 sides x 12 buckets; across categories that's many tries.
    tries = 2 * (len(backtest.BUCKETS) - 1) * (len(obs_by_cat) + 1)
    z = backtest.z_for(tries)
    summary = {"hours_before_close": args.hours, "z": z, "tries": tries, "categories": {}}
    everything = [o for obs in obs_by_cat.values() for o in obs]
    print(f"\n{len(everything)} priced markets, {len({o['event'] for o in everything})} events, "
          f"{len(obs_by_cat)} categories, z = {z:.2f} for {tries} tries, {(time.time() - t0) / 60:.0f} min")
    for name, obs in [("ALL", everything)] + sorted(obs_by_cat.items()):
        if len(obs) < 30:
            continue
        rows = table(obs, None, z)
        tight = table(obs, 0.02, z)
        show(f"{name}: every market ({len(obs)} markets)", rows)
        show(f"{name}: tight spread only (<= 2c)", tight)
        summary["categories"][name] = {"markets": len(obs), "all": rows, "tight": tight}
    os.makedirs(os.path.join(config.home(), "research"), exist_ok=True)
    path = os.path.join(config.home(), "research", f"deep_{int(args.hours)}h.json")
    with open(path, "w") as f:
        json.dump(summary, f, indent=1)
    print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
