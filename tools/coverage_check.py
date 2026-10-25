"""Real-data guard: research must still price markets in a few steady series.

PR #6 made research skip every market (Kalshi marks all of them as able to close early)
and every unit test still passed: fake markets don't behave like real ones. This runs the
real backtest on real settled markets and fails if a steady series prices too few, so the
next rule that starves research turns market-check red the day it lands.
Public data only: no key, no orders.

  python tools/coverage_check.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal.client import Kalshi  # noqa: E402
from kal.research import backtest  # noqa: E402

# One series per kind of schedule: weather and oil end at their event's strike date,
# games at their listed occurrence time. Each priced 200+ markets on real data (2026-10-25).
SERIES = {"KXHIGHNY": 25, "KXWTI": 25, "KXNFLGAME": 25}   # series: fewest priced markets allowed
MAX_MARKETS = 60   # per listing: Kalshi returns up to this many recent plus as many older ones
# Research tests only busy markets (backtest.busy: volume read from candles). Game markets
# are always busy, so a total this low means candle volume isn't being read at all.
MIN_BUSY = 25


def check(client, series=SERIES, hours=6, min_busy=MIN_BUSY):
    """(ok, lines): how many settled markets each series priced, and how many of them
    research would test (busy ones), from scratch."""
    ok, lines, busy = True, [], 0
    for s, least in series.items():
        try:
            obs = backtest.load_observations(client, s, hours, max_markets=MAX_MARKETS, refresh=True)
        except Exception as e:  # a failing series is a failed check, not a crash
            obs, note = [], f" ({type(e).__name__}: {e})"
        else:
            note = ""
        n, b = len(obs), len(backtest.busy(obs))
        busy += b
        good = n >= least
        ok = ok and good
        lines.append(f"{'ok  ' if good else 'LOW '} {s}: {n} priced markets, {b} busy (need {least} priced){note}")
    good = busy >= min_busy
    lines.append(f"{'ok  ' if good else 'LOW '} busy markets in all: {busy} (need {min_busy})")
    if not good:
        ok = False
        lines.append("Research tests only busy markets and found too few. Candle volume may not be read "
                     "any more, so research would test nothing.")
    if not ok:
        lines.append("Research can't price enough real markets. A rule may be skipping them all "
                     "(see docs/RESEARCH.md, 'How research keeps from fooling itself').")
    return ok, lines


def main():
    ok, lines = check(Kalshi())
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
