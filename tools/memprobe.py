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


def open_events(self):
    t = time.time()
    out = _open(self)
    seen.update(kept=len(out), load=time.time() - t, rss_loaded=mem()["VmRSS"])
    return out


engine.RunCache._open_events = open_events


def trace_one():
    import tracemalloc
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
    snap = best["snap"]
    if snap is None:
        print("no snapshot over 50 MB")
        return
    print(f"biggest snapshot: {best['size'] // MB} MB traced. Held by kal line (innermost kal frame):")
    by = {}
    for st in snap.statistics("traceback"):
        frames = [f for f in st.traceback if "/kal/" in f.filename]
        key = f"{frames[0].filename.split('/kal/')[-1]}:{frames[0].lineno}" if frames else "(no kal frame)"
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
