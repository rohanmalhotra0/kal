"""Where a command's memory goes: RSS over time and the biggest allocation sites at the peak.
Probe branch only.   python tools/mem_profile.py scan|research"""
import os
import resource
import sys
import threading
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MB = 1024 * 1024


def rss():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0


def main(what):
    tracemalloc.start(8)
    best = {"cur": 0, "snap": None, "t": 0}
    t0 = time.time()
    stop = threading.Event()

    def watch():
        while not stop.wait(2):
            cur, _ = tracemalloc.get_traced_memory()
            if cur > best["cur"] + 5 * MB:
                best.update(cur=cur, snap=tracemalloc.take_snapshot(), t=time.time() - t0)
            print(f"  t={time.time() - t0:5.0f}s rss={rss():5.0f} MB python={cur / MB:5.0f} MB", flush=True)
    threading.Thread(target=watch, daemon=True).start()
    if what == "scan":
        from kal.__main__ import scan_all
        scan_all()
    else:
        from kal.research import run_all
        run_all(quick=True)
    stop.set()
    print(f"\nDONE {what}: max rss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB, "
          f"python peak {tracemalloc.get_traced_memory()[1] / MB:.0f} MB")
    if best["snap"]:
        print(f"biggest sites at t={best['t']:.0f}s ({best['cur'] / MB:.0f} MB traced):")
        for st in best["snap"].statistics("traceback")[:12]:
            print(f"  {st.size / MB:6.1f} MB  {st.count:8d} blocks")
            for line in st.traceback.format()[-6:]:
                print("      " + line.strip())


if __name__ == "__main__":
    main(sys.argv[1])
