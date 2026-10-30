#!/usr/bin/env bash
# Probe only: memory of the real scheduler on live Kalshi data, paper only (no key, so no
# order can be sent). MODE comes from the workflow matrix (see tools/memprobe.py).
set -u
[ -n "${ARENA:-}" ] && export MALLOC_ARENA_MAX="$ARENA"
export KAL_HOME="$(mktemp -d)"
python -m kal on --budget 300 > /dev/null
python -m kal set trade_interval_min 2 > /dev/null
if [ "$MODE" = trace ] || [ "$MODE" = keys ]; then
  python tools/memprobe.py
  exit 0
fi
timeout 2400 python tools/memprobe.py > "$KAL_HOME/probe.out" 2> "$KAL_HOME/probe.err"
echo "== exit (124 = stopped by the 40-minute timeout): $?"
grep -E "^(mode|trade)" "$KAL_HOME/probe.out"
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
