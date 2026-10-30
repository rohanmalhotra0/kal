#!/usr/bin/env bash
# Probe only: memory of the real scheduler on live Kalshi data, paper only (no key, so no
# order can be sent). MODE comes from the workflow matrix (see tools/memprobe.py).
set -u
[ -n "${ARENA:-}" ] && export MALLOC_ARENA_MAX="$ARENA"
export KAL_HOME="$(mktemp -d)"
python -m kal on --budget 300 > /dev/null
[ "$MODE" = long ] || python -m kal set trade_interval_min 2 > /dev/null
if [ "$MODE" = trace ] || [ "$MODE" = keys ]; then
  python tools/memprobe.py
  exit 0
fi
LIMIT=2400; [ "$MODE" = long ] && LIMIT=10800
timeout $LIMIT python tools/memprobe.py > "$KAL_HOME/probe.out" 2> "$KAL_HOME/probe.err"
echo "== exit (124 = stopped by the $((LIMIT / 60))-minute timeout): $?"
grep -E "^(mode|trade|t=)" "$KAL_HOME/probe.out"
if [ "$MODE" = long ]; then
  echo "== status"; python -m kal status
  echo "== health"; python -m kal health
  echo "== report (first 80 lines)"; python -m kal report | head -80
  echo "== research files"; ls -la "$KAL_HOME/research" 2>&1 | head -30
  python - <<'PY2'
import os, sqlite3
db = sqlite3.connect(os.path.join(os.environ["KAL_HOME"], "kal.db"))
for kind, n, avg, mx in db.execute("SELECT kind, COUNT(*), AVG(duration), MAX(duration) FROM runs GROUP BY kind"):
    print(f"   {kind:12s} runs={n} avg={avg or 0:.0f}s max={mx or 0:.0f}s")
for kind, msg in db.execute("SELECT kind, message FROM runs WHERE kind != 'trade' ORDER BY id DESC LIMIT 8"):
    print(f"   last {kind}: {(msg or '')[:400]}")
print("   signals by strategy/mode:", db.execute("SELECT strategy, mode, COUNT(*) FROM signals GROUP BY strategy, mode").fetchall())
print("   paper trades:", db.execute("SELECT strategy, COUNT(*), SUM(settled) FROM paper GROUP BY strategy").fetchall())
PY2
fi
echo "== failed runs"
python - <<'PY'
import os, sqlite3
db = sqlite3.connect(os.path.join(os.environ["KAL_HOME"], "kal.db"))
print("   ", db.execute("SELECT kind, ok, COUNT(*) FROM runs GROUP BY kind, ok").fetchall())
for msg, in db.execute("SELECT message FROM runs WHERE ok = 0 ORDER BY id DESC LIMIT 5"):
    print("   failed:", (msg or "")[:300])
print("   live orders (must be 0 with no key):", db.execute("SELECT COUNT(*) FROM orders").fetchone()[0])
PY
echo "== errors"; grep -h -E "ERROR|Traceback" "$KAL_HOME"/*.log "$KAL_HOME/probe.err" 2>/dev/null | sort | uniq -c | sort -rn | head -10
echo "== restarts on a real runner (the clean-stop check)"
H="$KAL_HOME/.heartbeat"
stopped() { python -m kal status 2>&1 | grep -q "didn't shut down cleanly"; }
echo "   after the soak's clean stop: heartbeat $([ -e "$H" ] && echo 'still there (WRONG)' || echo gone)"
python -m kal daemon > /dev/null 2>&1 & P=$!; sleep 25; kill -9 $P; wait $P 2>/dev/null
echo "   killed mid-run: heartbeat holds '$(cat "$H" 2>/dev/null)', this boot is '$(cat /proc/sys/kernel/random/boot_id)'"
timeout -s TERM 25 python -m kal daemon > /dev/null 2>&1
if stopped; then echo "   FAIL: a crash on this boot stopped the bot"; else echo "   PASS: a crash on this boot is not judged"; fi
python -m kal daemon > /dev/null 2>&1 & P=$!; sleep 25; kill -9 $P; wait $P 2>/dev/null
echo "an-earlier-boot" > "$H"   # as after a reboot that followed an unclean stop
timeout -s TERM 25 python -m kal daemon > /dev/null 2>&1
if stopped; then echo "   PASS: a heartbeat from an earlier boot stopped the bot"; else echo "   FAIL: not stopped"; fi
python -m kal status 2>&1 | head -6
