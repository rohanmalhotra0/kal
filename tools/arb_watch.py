"""Watch for locked-in arbitrage on the real exchange (public data only: no key, no orders).

Runs the three arbitrage scans every few minutes for a few hours. Prints each find, then
a summary: how often a locked-in trade showed up, how long it lasted, and what taking
each one once would have made if every leg filled. Probe branch only; never merged.

  python tools/arb_watch.py --minutes 300 --every 5
"""
import argparse
import contextlib
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal import config  # noqa: E402
from kal.__main__ import scan_all  # noqa: E402
from kal.risk import Limits  # noqa: E402

ARBS = ["basket_arb", "ladder_arb", "bracket_arb"]


def clock(t=None):
    return time.strftime("%H:%M:%S", time.gmtime(t))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=300)
    ap.add_argument("--every", type=float, default=5)
    args = ap.parse_args()
    cfg = config.load()
    money = cfg["budget"] if cfg["budget"] > 0 else cfg["paper_bankroll"]
    t0, scans, took, failed = time.time(), 0, [], 0
    hits = {k: 0 for k in ARBS}         # scans with at least one find
    gaps = {}                            # (strategy, event) -> what we saw
    while time.time() - t0 < args.minutes * 60:
        start = time.time()
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                found = scan_all(only=ARBS)
        except Exception as e:
            failed += 1
            print(f"{clock()} scan failed: {e}", flush=True)
            found = {}
        for line in out.getvalue().splitlines():
            if "scan failed" in line:
                failed += 1
                print(f"{clock()} {line}", flush=True)
        scans += 1
        took.append(time.time() - start)
        print(f"{clock()} scan {scans}: " + ", ".join(f"{k} {len(found.get(k) or [])}" for k in ARBS)
              + f" ({took[-1]:.0f}s)", flush=True)
        for name, sigs in found.items():
            if sigs:
                hits[name] += 1
            for s in sigs:
                key = (name, s.event)
                row = {"cost": round(s.cost, 2), "payout_min": round(s.payout_min, 2), "profit": round(s.exp_profit, 2),
                       "legs": [(l.ticker, l.side, l.price, l.count) for l in s.legs]}
                g = gaps.get(key)
                if g is None:
                    why = Limits(cfg, money).refuse(s)
                    g = gaps[key] = {"strategy": name, "event": s.event, "title": s.title[:70], "first": clock(),
                                     "first_profit": row["profit"], "first_cost": row["cost"], "seen": 0,
                                     "max_profit": row["profit"], "would_skip": why or ""}
                    print("  NEW " + json.dumps({**g, **row}), flush=True)
                g["seen"] += 1
                g["last"] = clock()
                g["max_profit"] = max(g["max_profit"], row["profit"])
        time.sleep(max(0.0, args.every * 60 - (time.time() - start)))

    taken = [g for g in gaps.values() if not g["would_skip"]]
    summary = {
        "scans": scans, "minutes": round((time.time() - t0) / 60), "every_minutes": args.every,
        "seconds_per_scan": round(sum(took) / max(1, len(took)), 1), "failed_scans": failed,
        "scans_with_a_find": hits, "distinct_gaps": len(gaps),
        "gaps_the_limits_allow": len(taken),
        "profit_if_each_taken_once": round(sum(g["first_profit"] for g in taken), 2),
        "money_spent_if_each_taken_once": round(sum(g["first_cost"] for g in taken), 2),
        "gaps": sorted(gaps.values(), key=lambda g: g["first"]),
    }
    print("\nSUMMARY " + json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
