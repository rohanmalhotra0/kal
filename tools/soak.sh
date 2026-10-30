#!/usr/bin/env bash
# Probe only: run the real scheduler for 40 minutes on live Kalshi data, paper only (no key,
# so no order can be sent), then show what it did. Run by the api-probe workflow.
set -u
export KAL_HOME="$(mktemp -d)"
python -m kal on --budget 300
/usr/bin/time -v timeout 2400 python -m kal daemon > "$KAL_HOME/daemon.out" 2> "$KAL_HOME/time.txt"
echo "== daemon exit (124 = stopped by the 40-minute timeout): $?"
grep -E "Maximum resident set size" "$KAL_HOME/time.txt"
echo "== status";  python -m kal status
echo "== health";  python -m kal health
echo "== report (first 60 lines)"; python -m kal report | head -60
echo "== runs by kind"
python - <<'PY'
import os, sqlite3
db = sqlite3.connect(os.path.join(os.environ["KAL_HOME"], "kal.db"))
for kind, ok, n in db.execute("SELECT kind, ok, COUNT(*) FROM runs GROUP BY kind, ok"):
    print(f"   {kind:12s} ok={ok} runs={n}")
for msg, in db.execute("SELECT message FROM runs WHERE ok = 0 ORDER BY id DESC LIMIT 5"):
    print("   failed:", (msg or "")[:300])
print("   signals:", db.execute("SELECT mode, COUNT(*) FROM signals GROUP BY mode").fetchall())
print("   paper trades:", db.execute("SELECT COUNT(*), SUM(settled) FROM paper").fetchone())
print("   live orders (must be 0 with no key):", db.execute("SELECT COUNT(*) FROM orders").fetchone()[0])
PY
echo "== log errors"
grep -c "Traceback" "$KAL_HOME"/*.log "$KAL_HOME/daemon.out" 2>/dev/null
grep -h -E "ERROR|Traceback" "$KAL_HOME"/*.log "$KAL_HOME/daemon.out" 2>/dev/null | sort | uniq -c | sort -rn | head -20
