#!/usr/bin/env bash
# CI only. Runs the real server install (deploy/install.sh) on a fresh Ubuntu machine, the
# way the README says, then checks the bot came up right: the service, the files and who
# can read them, the dashboard, the `kal` command, alerts, the watchdog, a crash, a clean
# stop, and a second install on top (as after a `git pull`).
#
# It stops and kills the bot on purpose, so it refuses to run anywhere but a throwaway
# GitHub Actions machine. Run by .github/workflows/install-check.yml. No key, no secrets:
# the bot stays off, and no alert leaves the machine.
set -uo pipefail

[ "${GITHUB_ACTIONS:-}" = true ] || { echo "CI only: this stops and kills the bot on purpose." >&2; exit 2; }

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP=/opt/kal DATA=/var/lib/kal ETC=/etc/kal
URL=http://127.0.0.1:8787
CATCH=8799   # a stand-in alert server on this machine
OUT="${RUNNER_TEMP:-/tmp}/install-check"
mkdir -p "$OUT"
fails=0 LAST="" CATCHER=""

say()    { printf '\n== %s\n' "$*"; }
ok()     { echo "ok    $*"; }
bad()    { echo "FAIL  $*"; echo "::error::$*"; fails=$((fails + 1)); }
check()  { local n=$1; shift; if "$@" >/dev/null 2>&1; then ok "$n"; else bad "$n"; fi; }   # passes if it works
refuse() { local n=$1; shift; if "$@" >/dev/null 2>&1; then bad "$n"; else ok "$n"; fi; }   # passes if it fails
same()   { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1: got '$2', want '$3'"; fi; }
within() {  # seconds cmd... : wait until the command works
  local n=$1 i; shift
  for i in $(seq 1 "$n"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  return 1
}
owner()  { sudo stat -c '%U:%G %a' "$1" 2>/dev/null || echo missing; }
code()   { curl -s -o /dev/null -w '%{http_code}' "$@"; }
prop()   { systemctl show kal -p "$1" --value; }
up()     { systemctl is-active --quiet kal; }
indent() { sed 's/^/      /'; }
json()   {  # dotted.key < file: one value, as JSON
  python3 -c 'import json, sys
v = json.load(sys.stdin)
for k in sys.argv[1].split("."):
    v = v[k]
print(json.dumps(v))' "$1"
}
kal_run() {  # want args... : `sudo kal args`, the way the owner types it
  local want=$1 got; shift
  LAST=$(sudo kal "$@" 2>&1); got=$?
  printf '%s\n' "$LAST" | indent
  same "sudo kal $* exits $want" "$got" "$want"
  case "$LAST" in *Traceback*) bad "sudo kal $*: printed a Python error" ;; esac
}
run_install() {  # log
  sudo bash "$APP/deploy/install.sh" > "$1" 2>&1
  local got=$?
  indent < "$1"
  return "$got"
}
watch_lines() { sudo journalctl -u kal-watch.service --since "@$1" -o cat --no-pager | grep '^kal-watch:'; }
watch_once() {  # run the watchdog now; its lines from this run go to $OUT/watch.txt
  local t0
  t0=$(date +%s)
  sudo systemctl start kal-watch.service
  within 10 watch_lines "$t0"   # the journal can lag a moment
  watch_lines "$t0" > "$OUT/watch.txt"
  indent < "$OUT/watch.txt"
}
finish() {
  say "The bot's log (last lines)"
  sudo journalctl -u kal --no-pager -o cat -n 60 | indent
  [ -n "$CATCHER" ] && kill "$CATCHER" 2>/dev/null
  echo
  if [ "$fails" -eq 0 ]; then echo "All install checks passed."; exit 0; fi
  echo "$fails install check(s) FAILED."
  exit 1
}

say "1. The code in $APP, owned by root (the README's git clone)"
# No alert may leave this machine: ntfy.sh goes nowhere, and the checks below use a
# stand-in server instead.
echo "127.0.0.1 ntfy.sh" | sudo tee -a /etc/hosts >/dev/null
sudo rm -rf "$APP"
sudo mkdir -p "$APP"
sudo cp -a "$SRC/." "$APP/"
sudo chown -R root:root "$APP"
check "the code is in place" sudo test -f "$APP/kal/__main__.py"

say "2. Install: sudo bash $APP/deploy/install.sh"
if run_install "$OUT/install-1.log"; then ok "install.sh finished"; else bad "install.sh failed"; fi
for line in "Done. The bot is installed but OFF." "Dashboard password: " "Phone alerts topic"; do
  check "it says: $line" grep -qF "$line" "$OUT/install-1.log"
done
refuse "no Python errors in what it printed" grep -q Traceback "$OUT/install-1.log"
grep -qF "All checks passed." "$OUT/install-1.log" ||
  echo "::warning::kal selftest didn't pass during install. Kalshi's API may be down (market-check watches that)."
[ "$fails" -eq 0 ] || finish

say "3. The service"
check "kal is running" within 30 up
sleep 20   # a crash at start would show as a restart by now (RestartSec=5)
same "no restarts since it started" "$(prop NRestarts)" 0
same "it runs as the kal user" "$(prop User)" kal
same "memory cap" "$(prop MemoryMax)" 419430400
same "it starts on boot" "$(systemctl is-enabled kal 2>&1)" enabled
same "the watchdog timer is on" "$(systemctl is-enabled kal-watch.timer 2>&1)" enabled
check "the watchdog timer is running" systemctl is-active --quiet kal-watch.timer
same "the daily update is installed but off" "$(systemctl is-enabled kal-update.timer 2>&1)" disabled
units="kal.service kal-watch.service kal-watch.timer kal-update.service kal-update.timer"
problems=$(cd /etc/systemd/system && sudo systemd-analyze verify $units 2>&1 | grep -E 'kal(-watch|-update)?\.(service|timer)')
same "systemd finds no problems in kal's unit files" "$problems" ""
systemd-analyze security kal --no-pager 2>/dev/null | tail -n 1 | indent

say "4. Files, and who can read them"
same "settings folder: root only" "$(owner "$ETC")" "root:root 700"
same "settings file: root only" "$(owner "$ETC/kal.env")" "root:root 600"
same "data folder: the bot only" "$(owner "$DATA")" "kal:kal 700"
same "alerts topic file: the bot only" "$(owner "$DATA/ntfy_topic")" "kal:kal 600"
same "the kal command" "$(owner /usr/local/bin/kal)" "root:root 755"
check "a dashboard password was made" sudo grep -qE '^KAL_DASHBOARD_PASSWORD=.{20,}$' "$ETC/kal.env"
check "an alerts topic was made" sudo grep -qxE 'kal-[a-km-np-z2-9]{20}' "$DATA/ntfy_topic"
check "the bot can read its code" sudo runuser -u kal -- test -r "$APP/kal/__main__.py"
refuse "the bot can't change its code" sudo runuser -u kal -- test -w "$APP/kal/__main__.py"
refuse "the bot can't add code" sudo runuser -u kal -- test -w "$APP/kal"
refuse "the bot can't change its Python packages" \
  sudo runuser -u kal -- sh -c 'test -w /opt/kal-venv/lib/python3*/site-packages'
refuse "the bot can't read the settings file" sudo runuser -u kal -- cat "$ETC/kal.env"
refuse "the update's test user can't read the bot's data" sudo runuser -u kal-stage -- ls "$DATA"
same "the kal user can't log in" "$(getent passwd kal | cut -d: -f7)" /usr/sbin/nologin
same "the kal-stage user can't log in" "$(getent passwd kal-stage | cut -d: -f7)" /usr/sbin/nologin
check "the system log is capped" sudo grep -q '^SystemMaxUse=' /etc/systemd/journald.conf.d/kal.conf

say "5. The dashboard"
check "it answers" within 30 curl -s -o /dev/null "$URL/"
same "it listens on this machine only" "$(sudo ss -Hltn 'sport = :8787' | awk '{print $4}' | sort -u | paste -sd' ')" \
  "127.0.0.1:8787"
same "it needs the password" "$(code "$URL/")" 401
same "a wrong password is refused" "$(code -u kal:wrong "$URL/")" 401
PW=$(sudo sed -n 's/^KAL_DASHBOARD_PASSWORD=//p' "$ETC/kal.env")
same "the password opens it" "$(code -u "kal:$PW" "$URL/")" 200
curl -s -u "kal:$PW" "$URL/api/state" > "$OUT/state.json"
same "it shows the bot off" "$(json config.enabled < "$OUT/state.json")" false
same "it shows no key yet" "$(json has_key < "$OUT/state.json")" false
same "it shows no errors" "$(json errors < "$OUT/state.json")" "[]"

say "6. The kal command"
check "sudo kal health --alive (the heartbeat is fresh)" within 60 sudo kal health --alive
kal_run 0 status
kal_run 0 health
kal_run 1 doctor
same "doctor: the only FAIL is the missing key" "$(grep '^FAIL' <<<"$LAST")" \
  "FAIL  key: no Kalshi key yet. Run: sudo kal setup"
kal_run 0 off
kal_run 0 backup
kal_run 0 logs

say "7. Alerts, to a stand-in alert server on this machine"
sudo systemctl stop kal-watch.timer   # from here on the checks run the watchdog themselves
python3 -u - "$CATCH" "$OUT/alerts.txt" <<'PY' > "$OUT/catcher.log" 2>&1 &
import http.server, sys
port, out = int(sys.argv[1]), sys.argv[2]

class Catch(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
        with open(out, "a") as f:
            f.write(f"{self.path} | {self.headers.get('Title', '')} | {' '.join(body.split())}\n")
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass

http.server.HTTPServer(("127.0.0.1", port), Catch).serve_forever()
PY
CATCHER=$!
check "the stand-in alert server is up" within 10 curl -s -o /dev/null -X POST "http://127.0.0.1:$CATCH/ping"
: > "$OUT/alerts.txt"
TOPIC=$(sudo cat "$DATA/ntfy_topic")
echo "KAL_NTFY_SERVER=http://127.0.0.1:$CATCH" | sudo tee -a "$ETC/kal.env" >/dev/null
sudo systemctl restart kal
check "kal is back after a settings change" within 30 up
kal_run 0 summary --send
check "the summary reached it, on the topic install made" grep -q "^/$TOPIC | " "$OUT/alerts.txt"

say "8. The watchdog"
refuse "it raised no false alarm while installing" \
  sh -c "sudo journalctl -u kal-watch.service -o cat --no-pager | grep -qE 'kal-watch: (alerted|couldn)'"
check "the heartbeat is fresh" within 60 sudo kal health --alive
: > "$OUT/alerts.txt"
watch_once
check "a healthy bot: it says ok" grep -qx "kal-watch: ok" "$OUT/watch.txt"
same "a healthy bot: no alert" "$(cat "$OUT/alerts.txt")" ""
sudo systemctl stop kal
watch_once
check "a stopped bot: it alerts" grep -q "^/$TOPIC | kal is not running | " "$OUT/alerts.txt"
sudo systemctl start kal
check "kal is back" within 30 up
check "the heartbeat is fresh again" within 60 sudo kal health --alive

say "9. A crash"
pid=$(prop MainPID) n=$(prop NRestarts)
sudo kill -9 "$pid"
check "systemd starts it again" within 30 \
  sh -c "[ \"\$(systemctl show kal -p NRestarts --value)\" = $((n + 1)) ] && systemctl is-active --quiet kal"
refuse "as a new process" [ "$(prop MainPID)" = "$pid" ]
watch_once
check "the watchdog alerts on the crash" grep -q "^/$TOPIC | kal keeps restarting | " "$OUT/alerts.txt"
check "the heartbeat is fresh after the crash" within 60 sudo kal health --alive

say "10. A clean stop"
t0=$(date +%s)
sudo systemctl stop kal
took=$(($(date +%s) - t0))
check "it stops within 30 s (took ${took} s)" [ "$took" -le 30 ]
same "it stopped cleanly" "$(prop Result)" success
refuse "a clean stop removes the heartbeat" sudo test -e "$DATA/.heartbeat"

say "11. Install again on top (as after a git pull)"
# A key file sent with scp as root lands owned by root. A stand-in, not a key.
echo "not a real key" | sudo tee "$DATA/kalshi.pem" >/dev/null
sudo chmod 644 "$DATA/kalshi.pem"
env_before=$(sudo sha256sum "$ETC/kal.env")
topic_before=$(sudo cat "$DATA/ntfy_topic")
if run_install "$OUT/install-2.log"; then ok "install.sh finished again"; else bad "install.sh failed the second time"; fi
same "it keeps the settings" "$(sudo sha256sum "$ETC/kal.env")" "$env_before"
same "it keeps the alerts topic" "$(sudo cat "$DATA/ntfy_topic")" "$topic_before"
refuse "it makes no new password" grep -qF "Dashboard password:" "$OUT/install-2.log"
same "the key file is the bot's only" "$(owner "$DATA/kalshi.pem")" "kal:kal 600"
check "kal is running again" within 30 up
check "the heartbeat is fresh" within 60 sudo kal health --alive
kal_run 1 doctor
same "doctor: still only the missing key" "$(grep '^FAIL' <<<"$LAST")" \
  "FAIL  key: no Kalshi key yet. Run: sudo kal setup"
sudo rm -f "$DATA/kalshi.pem"

say "12. The bot's own log"
sudo journalctl -u kal --no-pager -o cat > "$OUT/journal.txt"
refuse "no Python errors in the service log" grep -q Traceback "$OUT/journal.txt"
refuse "no Python errors in kal's log files" sudo grep -rqs Traceback "$DATA/logs"
prop MemoryCurrent | awk '{printf "      memory now: %d MB (cap 400 MB)\n", $1 / 1048576}'

finish
