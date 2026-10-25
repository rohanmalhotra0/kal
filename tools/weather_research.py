"""Weather research on real data: do NBM forecasts beat Kalshi's daily high-temperature prices?

Public data only: Kalshi's settled markets and candles, and NBM forecast runs kept by the
Iowa Environmental Mesonet. No key, no orders. See kal/research/weather.py for the rules.
Prints, per decision time: how well the forecast and the market each called the results
(Brier score, lower is better), then whether buying where they disagree made money.

  python tools/weather_research.py --hours 18 30 --markets-per-series 900
"""
import argparse
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal.client import Kalshi  # noqa: E402
from kal.research import backtest, weather  # noqa: E402
from kal.research.discover import research_dir  # noqa: E402


def pct(x):
    return "   n/a" if x is None else f"{x:+6.1%}"


def fetcher():
    session = requests.Session()

    def fetch(url, params):
        for attempt in range(3):
            time.sleep(0.3 + 2 * attempt)   # a free service: go slowly
            r = session.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.text
        r.raise_for_status()
        return r.text
    return fetch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, nargs="+", default=[18, 30])
    ap.add_argument("--margins", type=float, nargs="+", default=[0.05, 0.10])
    ap.add_argument("--markets-per-series", type=int, default=900)
    args = ap.parse_args()
    t0, client, fetch = time.time(), Kalshi(), fetcher()
    tries = len(args.hours) * len(args.margins) * 2   # every decision time x margin x side
    z = backtest.z_for(tries)
    summary = {"z": z, "tries": tries, "hours": {}}
    for h in args.hours:
        rows = []
        for series, station in weather.STATIONS.items():
            try:
                obs = backtest.load_observations(client, series, h, max_markets=args.markets_per_series)
                markets = client.settled_markets(series, args.markets_per_series)
                hushed = weather.quiet(client, series, obs, markets, h)   # what the volume filter left out
                obs = obs + hushed
                events, tiled = weather.tiling(markets)
                wants = {o["event"]: (weather.run_for(o["decided"]), weather.day_of(o["anchor"])) for o in obs}
                cache = os.path.join(backtest.folder("cache"), f"nbm_{station}.json")
                runs = weather.forecasts(fetch, station, wants.values(), cache)
                got = weather.priced(obs, markets, {e: runs.get(w) for e, w in wants.items()})
            except Exception as e:
                print(f"{h:g}h {series}: skipped: {e}", flush=True)
                continue
            s = weather.scores(got) or {}
            print(f"{h:g}h {series:11} {len(obs):5} priced ({len(hushed)} quiet, "
                  f"{sum(1 for o in hushed if o['result'] == 'yes')} of them won), {len(got):5} with a fitted "
                  f"forecast. Brackets tile every degree in {tiled} of {events} events. Brier: forecast "
                  f"{s.get('brier_forecast', float('nan')):.4f}, market mid {s.get('brier_market', float('nan')):.4f}",
                  flush=True)
            rows += got
        s = weather.scores(rows) or {}
        print(f"\n===== {h:g} hours before the scheduled end: {len(rows)} markets, "
              f"{len({str(weather.day_of(o['anchor'])) for o in rows})} days")
        print(f"Brier (lower is better; a guide only, trades must beat the ask): forecast "
              f"{s.get('brier_forecast', float('nan')):.4f}, market mid {s.get('brier_market', float('nan')):.4f}")
        days, far = weather.far_misses(rows)
        if far is not None:
            print(f"Results beyond 2 spreads of the forecast: {far:.1%} of {days} days (a Normal error: under 4.6%)")
        print("forecast chance   markets  forecast  market mid  YES won")
        for r in weather.calibration(rows):
            print(f"  {r['lo']:.2f}-{r['hi']:.2f}     {r['markets']:7}  {r['forecast']:8.3f}  {r['market']:10.3f}  "
                  f"{r['won']:7.3f}")
        print("margin side  trades days   win   ROI    worst  edge?")
        out = {"scores": s, "trades": [], "calibration": weather.calibration(rows), "far_misses": far}
        for margin in args.margins:
            for side, t in weather.trades(rows, margin).items():
                roi, lo = backtest.roi_stats(t, z) if t else (None, None)
                units = len({x[0] for x in t})
                win = sum(1 for x in t if x[2] > 0) / len(t) if t else None
                edge = lo is not None and lo > 0 and len(t) >= backtest.MIN_TRADES and units >= backtest.MIN_EVENTS
                print(f"{margin:5.2f}  {side:4} {len(t):6} {units:5} {'  n/a' if win is None else f'{win:5.0%}'} "
                      f"{pct(roi)} {pct(lo)}  {'EDGE' if edge else ''}")
                out["trades"].append({"margin": margin, "side": side, "trades": len(t), "days": units,
                                      "win": win, "roi": roi, "roi_lo": lo, "edge": edge})
        summary["hours"][f"{h:g}"] = out
    print(f"\nz = {z:.2f} for {tries} tries, {(time.time() - t0) / 60:.0f} min. Trades pay the real taker fee; "
          "a day's trades in all cities count as one unit.\nOne look: an EDGE here still has to pass "
          "paper trading before any money.")
    os.makedirs(research_dir(), exist_ok=True)
    path = os.path.join(research_dir(), "weather.json")
    with open(path, "w") as f:
        json.dump(summary, f, indent=1)
    print(f"Saved {path}")


if __name__ == "__main__":
    main()
