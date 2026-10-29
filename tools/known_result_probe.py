"""Idea #16, a result already known while the market trades: daily high temperatures.
Probe branch only, never merged. Public data only (no key, no orders).

Kalshi's daily-high markets settle on the NWS climate report (midnight to midnight, local
standard time), published the next morning. By evening the day's high so far is public
(IEM's ASOS archive). Once it has passed a bracket's top, that bracket can't win, since a
daily high only goes up. So its NO is nearly sure to pay $1. If the market still sells that
NO under $1 after fees, that's close to locked in. This counts how often, at what price,
with how much trading, and checks every such "dead" bracket's real result.
"""
import collections
import csv
import io
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal.client import Kalshi, fee, ts  # noqa: E402
from kal.research.backtest import candle_close  # noqa: E402
from kal.research.weather import STATIONS, bracket  # noqa: E402

LST = {"KNYC": -5, "KMDW": -6, "KMIA": -5, "KLAX": -8, "KDEN": -7, "KAUS": -6, "KPHL": -5}   # standard time
ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
DAYS = 75
CHECKS = (16, 18, 20, 22)          # local standard hours to look at the market
SLACKS = (0.0, 0.5, 1.0)           # degrees F the high so far must clear the line by
N = 20                             # contracts per order, for fees
H, DAY = 3600, 86400
MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()


def event_day(ev):
    try:
        s = ev.rsplit("-", 1)[1]
        return date(2000 + int(s[:2]), MONTHS.index(s[2:5]) + 1, int(s[5:7]))
    except (ValueError, IndexError):
        return None


def obs(station, start, end):
    """[(utc epoch, tmpf)] for one ASOS station."""
    params = {"station": station[1:], "data": "tmpf", "tz": "Etc/UTC", "format": "onlycomma", "latlon": "no",
              "elev": "no", "missing": "M", "trace": "T", "direct": "no", "report_type": ["3", "4"],
              "year1": start.year, "month1": start.month, "day1": start.day,
              "year2": end.year, "month2": end.month, "day2": end.day}
    r = requests.get(ASOS, params=params, timeout=120)
    r.raise_for_status()
    out = []
    for row in csv.DictReader(io.StringIO(r.text)):
        try:
            t = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
            out.append((t, float(row["tmpf"])))
        except (KeyError, ValueError):
            continue
    return sorted(out)


def main():
    c = Kalshi()
    t0, now = time.time(), time.time()
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=DAYS)
    closes = collections.Counter()
    n = collections.Counter()
    best = []
    broken = []
    by_slack = {s: collections.defaultdict(list) for s in SLACKS}
    for series, station in STATIONS.items():
        try:
            temps = obs(station, start - timedelta(days=1), today + timedelta(days=1))
        except Exception as e:
            print(f"{series}: ASOS failed: {e}")
            continue
        try:
            markets = c.settled_markets(series, limit=3000)
        except Exception as e:
            print(f"{series}: markets failed: {e}")
            continue
        events = collections.defaultdict(list)
        for m in markets:
            d = event_day(str(m.get("event_ticker") or ""))
            if d and start <= d < today:
                events[(m["event_ticker"], d)].append(m)
        print(f"{series} ({station}): {len(temps)} obs, {len(events)} days")
        off = LST[station] * H
        for (ev, d), ms in sorted(events.items()):
            day0 = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() - off   # local midnight, UTC
            for m in ms:
                try:
                    ct = ts(m["close_time"])
                    closes[round(((ct + off) - day0) / H)] += 1   # local standard hour after the day began
                except Exception:
                    pass
            for hour in CHECKS:
                at = day0 + hour * H
                so_far = [f for t, f in temps if day0 <= t <= at]
                if not so_far:
                    continue
                high = max(so_far)
                for m in ms:
                    b = bracket(m)
                    if not b:
                        continue
                    lo, hi = b
                    try:
                        ct = ts(m["close_time"])
                    except Exception:
                        continue
                    if ct <= at:
                        continue   # already closed then
                    for slack in SLACKS:
                        dead = hi is not None and high >= hi + 1 + slack        # this bracket can't win
                        sure = hi is None and lo is not None and high >= lo + slack   # the top tail already won
                        if not (dead or sure):
                            continue
                        key = (m["ticker"], hour)
                        if (m.get("result") == "yes") == dead or m.get("result") not in ("yes", "no"):
                            broken.append((slack, m["ticker"], hour, round(high, 1), b, m.get("result")))
                        by_slack[slack][key].append((dead, m))
        if time.time() - t0 > 35 * 60:
            print("(time limit)")
            break
    print(f"\nmarkets close at (local standard hours after the day began): {dict(sorted(closes.items()))}")
    for slack in SLACKS:
        bad = [b for b in broken if b[0] == slack]
        print(f"\n== slack {slack}F: {len(by_slack[slack])} market-hours where the result looked known; "
              f"real results against it: {len(bad)}")
        for b in bad[:10]:
            print("   BROKEN " + json.dumps(b))
    # Prices only for the strictest useful slack (0.5F), one candle call per market.
    slack = 0.5
    seen = {}
    for (ticker, hour), items in sorted(by_slack[slack].items()):
        dead, m = items[0]
        series = ticker.split("-", 1)[0]
        d = event_day(m["event_ticker"])
        station = STATIONS[series]
        day0 = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() - LST[station] * H
        at = day0 + hour * H
        if ticker not in seen:
            try:
                seen[ticker] = {k["end_period_ts"]: k for k in c.candles(series, ticker, day0 + 12 * H, day0 + 30 * H) or []}
            except Exception:
                seen[ticker] = {}
                n["candle errors"] += 1
        cands = [t for t in seen[ticker] if t <= at]
        if not cands:
            n["no price"] += 1
            continue
        k = seen[ticker][max(cands)]
        bid, ask = candle_close(k, "yes_bid"), candle_close(k, "yes_ask")
        vol = float(k.get("volume_fp") or k.get("volume") or 0)
        p = round(1 - bid, 4) if dead else ask       # what the sure side costs
        if not 0 < p < 1:
            n["sure side not offered (price $1)"] += 1
            continue
        cost = p + fee(N, p) / N
        edge = 1 - cost
        n["sure side offered under $1"] += 1
        if edge > 0:
            n["edge after fees"] += 1
            if edge >= 0.01:
                n["edge 1c+"] += 1
            if edge >= 0.02:
                n["edge 2c+"] += 1
        best.append((round(edge, 4), ticker, hour, "NO" if dead else "YES", p, vol))
    print(f"\nprices at slack {slack}F: {dict(n)}")
    best.sort(reverse=True)
    for b in best[:25]:
        print("   OFFER " + json.dumps(dict(zip(("edge", "ticker", "local_hour", "side", "price", "hour_volume"), b))))
    edges = [b[0] for b in best if b[0] > 0]
    if edges:
        print(f"   edges after fees: {len(edges)}, mean {sum(edges) / len(edges):.4f}, "
              f"days {len({b[1].rsplit('-', 1)[0] for b in best if b[0] > 0})}")
    print(f"\ndone in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
