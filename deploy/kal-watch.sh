#!/usr/bin/env bash
# Watches the bot from outside its own process, every 10 minutes. Every other alert is
# sent from inside the bot, so the phone stays silent if the bot is dead, crash-looping,
# or the server is off. This only reads and alerts; it never changes the bot. install.sh
# turns it on for you.
#
# It alerts when: the service isn't running, it is restarting over and over (a crash
# loop keeps the heartbeat fresh, so we also watch the restart count), or `kal health
# --alive` fails (e.g. the scheduler loop stopped). At most one alert per kind per day.
set -uo pipefail

APP="${APP:-/opt/kal}"
VENV="${VENV:-/opt/kal-venv}"
DATA="${DATA:-/var/lib/kal}"
ETC="${ETC:-/etc/kal}"
KAL_USER="${KAL_USER:-kal}"

say() { echo "kal-watch: $*"; }
# `python -m kal` finds the code from the current folder, as the kal wrapper does.
cd "$APP" || { say "no $APP"; exit 1; }

kv() {  # file key -- read KEY=VALUE without running the file
  [ -r "$1" ] || return 0
  local line v
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "$2"=*) v=${line#*=} ;;
      *) continue ;;
    esac
    v=${v%$'\r'}
    case "$v" in
      \"*\") v=${v#\"}; v=${v%\"} ;;
      \'*\') v=${v#\'}; v=${v%\'} ;;
    esac
    printf '%s' "$v"
    return 0
  done < "$1"
}

ntfy_topic() {
  local t="${KAL_NTFY_TOPIC:-$(kv "$ETC/kal.env" KAL_NTFY_TOPIC)}"
  [ -n "$t" ] || t=$(cat "$DATA/ntfy_topic" 2>/dev/null || true)
  printf '%s' "$t"
}
ntfy_server() {
  local s="${KAL_NTFY_SERVER:-$(kv "$ETC/kal.env" KAL_NTFY_SERVER)}"
  printf '%s' "${s:-https://ntfy.sh}"
}

# One alert per kind per day: remember the day we last sent each kind.
once_a_day() {  # kind -> 0 if we may alert today, 1 if already sent
  local marker="$DATA/watch_$1" today
  today=$(date -u +%Y-%m-%d)
  [ "$(cat "$marker" 2>/dev/null)" = "$today" ] && return 1
  echo "$today" > "$marker" 2>/dev/null || true
  return 0
}
alert() {  # kind title message
  once_a_day "$1" || { say "already alerted '$1' today"; return 0; }
  local topic server
  topic=$(ntfy_topic); [ -n "$topic" ] || { say "no alert topic set"; return 0; }
  server=$(ntfy_server)
  curl -s -m 10 -H "Priority: high" -H "Title: $2" -d "$3" "$server/$topic" >/dev/null || true
  say "alerted: $3"
}

if ! systemctl is-active --quiet kal; then
  alert down "kal is not running" "The bot is stopped. Start it: sudo systemctl start kal"
  exit 0
fi

# A crash loop: the restart count climbs between two checks.
nown=$(systemctl show kal -p NRestarts --value 2>/dev/null || echo 0)
prev=$(cat "$DATA/watch_nrestarts" 2>/dev/null || echo "$nown")
echo "$nown" > "$DATA/watch_nrestarts" 2>/dev/null || true
if [ "$nown" -gt "$prev" ] 2>/dev/null; then
  alert loop "kal keeps restarting" "The bot is crash-looping. Look: sudo kal logs"
  exit 0
fi

if ! runuser -u "$KAL_USER" -- env KAL_HOME="$DATA" "$VENV/bin/python" -m kal health --alive >/dev/null 2>&1; then
  alert health "kal looks unhealthy" "The bot is running but not well. Look: sudo kal health"
  exit 0
fi

say "ok"
