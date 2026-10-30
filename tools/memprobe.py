"""Probe only: where a trade run's memory goes, and whether the scheduler grows over time.
Paper only (no key). Run by the api-probe workflow, once per MODE:

  default  the real scheduler; one line per trade run: time, peak, memory after
  trim     the same, then malloc_trim(0) after each run (glibc hands freed pages back)
  trace    one trade run under tracemalloc: the kal lines holding the most at the peak
"""
import ctypes
import os
import sys
import threading
import time

MODE = os.environ.get("MODE", "default")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MB = 1024 * 1024


def mem():
    out = {}
    with open("/proc/self/status") as f:
        for line in f:
            k, _, v = line.partition(":")
            if k in ("VmRSS", "VmHWM"):
                out[k] = int(v.split()[0]) // 1024
    return out


def reset_peak():
    with open("/proc/self/clear_refs", "w") as f:
        f.write("5")


from kal import engine, scheduler  # noqa: E402

seen = {}
_open = engine.RunCache._open_events


def field_sizes(events):
    """Python bytes held by each field of the kept events (a shared object counted once)."""
    from collections import Counter
    ids, size = set(), Counter()

    def add(key, v):
        if id(v) in ids:
            return
        ids.add(id(v))
        size[key] += sys.getsizeof(v)
        if isinstance(v, dict):
            for v2 in v.values():
                add(key, v2)
        elif isinstance(v, list):
            for v2 in v:
                add(key, v2)

    for ev in events:
        size["(event dict)"] += sys.getsizeof(ev)
        for k, v in ev.items():
            if k != "markets" or not isinstance(v, list):
                add(f"event.{k}", v)
                continue
            size["(markets list)"] += sys.getsizeof(v)
            for m in v:
                size["(market dict)"] += sys.getsizeof(m)
                for k2, v2 in m.items():
                    add(f"market.{k2}", v2)
    total = sum(size.values())
    print(f"kept events hold {total / MB:.0f} MB of Python objects. Biggest parts:")
    for k, n in size.most_common(25):
        print(f"   {n / MB:7.1f} MB  {k}")


def open_events(self):
    t = time.time()
    out = _open(self)
    seen.update(kept=len(out), load=time.time() - t, rss_loaded=mem()["VmRSS"])
    if MODE == "trace":
        field_sizes(out)
    return out


engine.RunCache._open_events = open_events


def kept_report():
    """What the run kept: by category and series, sized as JSON, and why each was kept."""
    import json
    from collections import Counter
    wanted = engine._wanted
    seen_n, why, cat, series, size, markets = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()

    def counting(ev, until, keep):
        ok = wanted(ev, until, keep)
        seen_n["all"] += 1
        if not ok:
            return ok
        ms = ev.get("markets") if isinstance(ev, dict) else None
        if not isinstance(ms, list) or not ms:
            why["no markets"] += 1
        elif engine._due(ev.get("strike_date"), until):
            why["strike_date due"] += 1
        else:
            why["a market closes or occurs in time"] += 1
        c, st = str(ev.get("category")), str(ev.get("series_ticker"))
        n = len(json.dumps(ev, default=str))
        cat[c] += n
        series[st] += n
        size["all"] += n
        markets[c] += len(ms) if isinstance(ms, list) else 0
        seen_n["kept"] += 1
        return ok

    engine._wanted = counting

    def show():
        print(f"events seen {seen_n['all']}, kept {seen_n['kept']}, kept JSON {size['all'] / MB:.0f} MB "
              f"(before slimming). Why kept: {dict(why)}")
        print("kept by category (JSON MB, markets):")
        for c, n in cat.most_common(12):
            print(f"   {n / MB:7.1f} MB  {markets[c]:6d} markets  {c}")
        print("top series (JSON MB):")
        for st, n in series.most_common(15):
            print(f"   {n / MB:7.1f} MB  {st}")
    return show


def trace_one():
    import tracemalloc
    show = kept_report()
    tracemalloc.start(30)
    best = {"size": 0, "snap": None}
    done = threading.Event()

    def watch():
        while not done.wait(0.5):
            cur = tracemalloc.get_traced_memory()[0]
            if cur > 50 * MB and cur > best["size"] * 1.08:
                best.update(size=cur, snap=tracemalloc.take_snapshot())

    threading.Thread(target=watch, daemon=True).start()
    t = time.time()
    s = engine.run_once()
    done.set()
    cur, peak = tracemalloc.get_traced_memory()
    print(f"run: {time.time() - t:.0f}s ok={s.get('ok')} signals={s.get('signals')} kept={seen.get('kept')}")
    print(f"python-level: peak {peak // MB} MB, after {cur // MB} MB; process rss {mem()}")
    show()
    snap = best["snap"]
    if snap is None:
        print("no snapshot over 50 MB")
        return
    print(f"biggest snapshot: {best['size'] // MB} MB traced. Held by kal line (innermost kal frame):")
    by = {}
    for st in snap.statistics("traceback"):
        frames = [f for f in st.traceback if "/kal/kal/" in f.filename]
        key = f"{frames[-1].filename.split('/kal/kal/')[-1]}:{frames[-1].lineno}" if frames else "(no kal frame)"
        by[key] = by.get(key, 0) + st.size
    for key, size in sorted(by.items(), key=lambda kv: -kv[1])[:15]:
        print(f"   {size / MB:7.1f} MB  {key}")
    print("Top lines overall:")
    for st in snap.statistics("lineno")[:10]:
        print(f"   {st.size / MB:7.1f} MB  {st.traceback[0].filename.split('/')[-1]}:{st.traceback[0].lineno}")


def wrap_trade():
    libc = ctypes.CDLL("libc.so.6")
    run = scheduler.DEFAULT_JOBS["trade"]
    n = [0]

    def trade():
        reset_peak()
        seen.clear()
        before, t = mem()["VmRSS"], time.time()
        try:
            return run()
        finally:
            m = mem()
            line = (f"trade {n[0]:2d}: {time.time() - t:4.0f}s kept={seen.get('kept')} "
                    f"load={seen.get('load', 0):.0f}s rss before {before} peak {m['VmHWM']} "
                    f"after {m['VmRSS']} MB, blocks {sys.getallocatedblocks()}")
            if MODE == "trim":
                libc.malloc_trim(0)
                line += f", after trim {mem()['VmRSS']} MB"
            n[0] += 1
            print(line, flush=True)

    scheduler.DEFAULT_JOBS["trade"] = trade


if __name__ == "__main__":
    print(f"mode={MODE} cpus={os.cpu_count()} MALLOC_ARENA_MAX={os.environ.get('MALLOC_ARENA_MAX')}", flush=True)
    if MODE == "trace":
        trace_one()
    else:
        wrap_trade()
        from kal.scheduler import Scheduler
        Scheduler().serve_forever()
