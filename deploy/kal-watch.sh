#!/usr/bin/env bash
# Watches the bot from outside its own process, every 10 minutes. Every other alert is
# sent from inside the bot, so the phone stays silent if the bot is dead, crash-looping,
# or the server is off. This only reads and alerts; it never changes the bot. install.sh
# turns it on for you.
#
# It alerts when: the service isn't running, it is restarting over and over (a crash
# loop keeps the heartbeat fresh, so we also watch the restart count; kal-update sets the
# count it starts from after an update), or `kal health --alive` fails (e.g. the
# scheduler loop stopped). At most one alert per kind per day.
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

# One alert per kind per day: remember the day we last sent each kind. In $DATA; when the
# disk can't take it (read-only after errors), under /run (memory), or it would buzz every
# 10 minutes.
RUN_DIR="${KAL_WATCH_RUN:-/run/kal-watch}"
sent_today() {  # kind
  local today; today=$(date -u +%Y-%m-%d)
  [ "$(cat "$DATA/watch_$1" 2>/dev/null)" = "$today" ] || [ "$(cat "$RUN_DIR/watch_$1" 2>/dev/null)" = "$today" ]
}
mark_sent() {  # kind
  date -u +%Y-%m-%d > "$DATA/watch_$1" 2>/dev/null && return 0
  mkdir -p "$RUN_DIR" 2>/dev/null; date -u +%Y-%m-%d > "$RUN_DIR/watch_$1" 2>/dev/null || true
}
alert() {  # kind title message
  sent_today "$1" && { say "already alerted '$1' today"; return 0; }
  local topic server
  topic=$(ntfy_topic); [ -n "$topic" ] || { say "no alert topic set"; return 0; }
  server=$(ntfy_server)
  # Marked as sent only once it really went out: a failed send is tried again next time.
  if curl -fsS -m 10 -H "Priority: high" -H "Title: $2" -d "$3" "$server/$topic" >/dev/null; then
    mark_sent "$1"
    say "alerted: $3"
  else
    say "couldn't send the alert, trying again next time: $3"
  fi
}

# A restart (an update, a re-install, a crash) leaves the bot down for a few seconds, and
# a fresh start has no heartbeat for a few more. A false alarm would also use up that
# day's alert. So a problem must still be there on a second look.
twice() {  # cmd... : true if the command fails now, and again a little later
  "$@" && return 1
  sleep "${KAL_WATCH_PAUSE:-30}"
  ! "$@"
}

# kal-update is restarting the bot and judging it, and says what happened itself: look next
# time (its crashes would use up the day's alert). A note over 30 minutes old is from an
# update that died: then look as usual.
CHECKING="${KAL_UPDATE_CHECKING:-/run/kal-update.checking}"
if [ -e "$CHECKING" ] && [ -z "$(find "$CHECKING" -mmin +30 2>/dev/null)" ]; then
  say "an update is checking the bot: looking next time"
  exit 0
fi

if twice systemctl is-active --quiet kal; then
  if [ "$(systemctl is-active kal 2>/dev/null)" = deactivating ]; then
    say "the bot is stopping (a trade run finishing, say): looking next time"
    exit 0
  fi
  case "$(systemctl show kal -p SubState --value 2>/dev/null)" in
    auto-restart*)   # it crashed and waits to start again: starting it won't help
      alert loop "kal keeps restarting" "The bot is crash-looping. Look: sudo kal logs" ;;
    *) alert down "kal is not running" "The bot is stopped. Start it: sudo systemctl start kal" ;;
  esac
  exit 0
fi

# A crash loop: automatic restarts since the last check. The count isn't 0, and it moved or
# the bot started again since (InvocationID changes at every start). A manual restart starts
# the count from 0, so a crash after one can read the same as, or lower than, before.
nown=$(systemctl show kal -p NRestarts --value 2>/dev/null || echo 0)
inv=$(systemctl show kal -p InvocationID --value 2>/dev/null || true)
read -r prev pinv 2>/dev/null < "$DATA/watch_nrestarts" || { prev=$nown; pinv=$inv; }
echo "$nown ${inv:-}" > "$DATA/watch_nrestarts" 2>/dev/null || true
if [ "$nown" -gt 0 ] 2>/dev/null && { [ "$nown" != "$prev" ] || [ "${inv:-}" != "${pinv:-${inv:-}}" ]; }; then
  alert loop "kal keeps restarting" "The bot is crash-looping. Look: sudo kal logs"
  exit 0
fi

if twice runuser -u "$KAL_USER" -- env KAL_HOME="$DATA" "$VENV/bin/python" -m kal health --alive >/dev/null 2>&1; then
  alert health "kal looks unhealthy" "The bot is running but not well. Look: sudo kal health"
  exit 0
fi

say "ok"
