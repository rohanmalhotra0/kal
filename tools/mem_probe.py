"""How much memory the open-events download takes, and the real runs' peaks.
Probe branch only. Public data only (no key, no orders).

  python tools/mem_probe.py events     the download every trade run and research makes
  python tools/mem_probe.py run CMD..  a command's peak memory (max RSS)
"""
import collections
import json
import os
import resource
import subprocess
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal.client import Kalshi, ts  # noqa: E402

MB = 1024 * 1024


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def events():
    c = Kalshi()
    now = time.time()
    tracemalloc.start()
    params = {"limit": 200, "status": "open", "with_nested_markets": "true"}
    out, pages, size, t0 = [], 0, 0, time.time()
    while True:
        r = c._req("GET", "/events", params=params)
        size += len(r.content)
        d = r.json()
        out += d.get("events") or []
        pages += 1
        if pages % 25 == 0:
            cur, peak = tracemalloc.get_traced_memory()
            print(f"  page {pages}: {len(out)} events, {size / MB:.0f} MB of JSON, "
                  f"python {cur / MB:.0f} MB (peak {peak / MB:.0f}), rss {rss_mb():.0f} MB, {time.time() - t0:.0f}s")
        if not d.get("cursor"):
            break
        params["cursor"] = d["cursor"]
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    ms = [m for ev in out for m in ev.get("markets") or []]
    print(f"\nALL: {pages} pages, {len(out)} events, {len(ms)} markets, {size / MB:.0f} MB of JSON, "
          f"python {cur / MB:.0f} MB held (peak {peak / MB:.0f}), max rss {rss_mb():.0f} MB, {time.time() - t0:.0f}s")

    by = collections.Counter()
    for ev in out:
        s = str(ev.get("series_ticker") or "")
        k = "KXMVE" if s.startswith("KXMVE") else "other"
        by[k + " events"] += 1
        by[k + " markets"] += len(ev.get("markets") or [])
    print("split: " + json.dumps(dict(by)))

    def closes(m):
        try:
            return ts(m["close_time"])
        except Exception:
            return None
    for days in (2, 7, 30, 90, 365):
        n = sum(1 for m in ms if (closes(m) or 9e18) - now <= days * 86400)
        e = sum(1 for ev in out if any((closes(m) or 9e18) - now <= days * 86400 for m in ev.get("markets") or []))
        print(f"  closing within {days:>3} days: {n} markets in {e} events")

    sizes = collections.Counter()
    for m in ms[:: max(1, len(ms) // 20000)]:
        for k, v in m.items():
            sizes[k] += len(json.dumps(v))
    total = sum(sizes.values()) or 1
    print("heaviest market fields (share of JSON):")
    for k, v in sizes.most_common(15):
        print(f"  {k:32} {100 * v / total:5.1f}%")
    print("fields per market: " + str(len(ms[0]) if ms else 0))


def run(cmd):
    t0 = time.time()
    p = subprocess.run(cmd)
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    print(f"\nPEAK {peak:.0f} MB max rss for: {' '.join(cmd)} (exit {p.returncode}, {time.time() - t0:.0f}s)")


def lean():
    """The run's copy, the new way: RunCache with the default scope."""
    from kal import engine
    tracemalloc.start()
    t0 = time.time()
    rc = engine.RunCache(Kalshi())
    rc.scope((), engine.horizon_days(__import__("kal.config", fromlist=["load"]).load()))
    evs = rc.events(status="open", with_nested_markets="true")
    cur, peak = tracemalloc.get_traced_memory()
    ms = sum(len(ev.get("markets") or []) for ev in evs)
    print(f"LEAN: {len(evs)} events, {ms} markets kept; python {cur / MB:.0f} MB held (peak {peak / MB:.0f}), "
          f"max rss {rss_mb():.0f} MB, {time.time() - t0:.0f}s")


if __name__ == "__main__":
    if sys.argv[1] == "events":
        events()
    elif sys.argv[1] == "lean":
        lean()
    else:
        run(sys.argv[2:])
