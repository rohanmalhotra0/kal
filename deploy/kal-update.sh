#!/usr/bin/env bash
# Daily update (turn on with: sudo systemctl enable --now kal-update.timer).
#
# This runs whatever is on GitHub main on your server. Turn it on only if you trust
# every push to main (see README and docs/DEPLOY.md). It tries to be careful:
#
#  - It refuses code (anything outside docs/) that was not merged through a pull
#    request on GitHub, so a stray push can't slip code in.
#  - It builds the new code and runs every test in a throwaway copy, as a locked-down
#    user that can't read your key, settings or books. Only if the tests pass does it
#    swap the new code into /opt/kal.
#  - It waits for any research run to finish, restarts the bot, then checks the bot is
#    really alive. If not, it puts the old code and packages back.
#  - Docs-only changes (the check-in's journal) fast-forward with no tests and no restart.
set -uo pipefail
umask 022   # like install.sh: only root may change the code and packages

APP="${APP:-/opt/kal}"
VENV="${VENV:-/opt/kal-venv}"
DATA="${DATA:-/var/lib/kal}"
ETC="${ETC:-/etc/kal}"
KAL_USER="${KAL_USER:-kal}"          # the service user; health runs as them
# The locked-down user the tests run as: kal-stage (install.sh makes it), else nobody.
STAGE_USER="${STAGE_USER:-$(id kal-stage >/dev/null 2>&1 && echo kal-stage || echo nobody)}"
STAGE_PYTHON="${STAGE_PYTHON:-python3}"
WAIT="${KAL_UPDATE_WAIT:-120}"       # seconds to let the bot settle before the health check
RESEARCH_LOCK="$DATA/.research.lock" # the file kal/research holds while a research run works
RESEARCH_WAIT="${KAL_RESEARCH_WAIT:-3600}"  # longest wait for that run before restarting anyway

say() { echo "kal-update: $*"; }     # goes to the journal: journalctl -u kal-update

# Read one KEY=VALUE from kal.env without running the file (it is not a shell script, and
# we never want its secrets in our environment). Strips optional surrounding quotes.
kv() {  # file key
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
alert() {  # priority title message
  local topic server
  topic=$(ntfy_topic); [ -n "$topic" ] || return 0
  server=$(ntfy_server)
  curl -s -m 10 -H "Priority: $1" -H "Title: $2" -d "$3" "$server/$topic" >/dev/null || true
}

cd "$APP" || { say "no $APP"; exit 1; }
[ -d .git ] || { say "$APP is not a git checkout (install with git clone to use updates)"; exit 1; }
before=$(git rev-parse HEAD)
git fetch --quiet origin main || { say "fetch failed; will try tomorrow"; exit 0; }
after=$(git rev-parse origin/main)
[ "$before" = "$after" ] && { say "already up to date (${before:0:7})"; exit 0; }

failed="$DATA/update_failed"   # the version whose tests failed: try it once, alert once
if [ "$(cat "$failed" 2>/dev/null)" = "$after" ]; then
  say "skipping ${after:0:7}: its tests failed before"
  exit 0
fi

# The server must only fast-forward. Local commits mean someone changed the server's copy.
if ! git merge-base --is-ancestor "$before" "$after"; then
  alert high "kal update skipped" "The server's copy has its own changes. Nothing changed."
  say "can't fast-forward; left on ${before:0:7}"
  exit 1
fi

# Accident guard: each first-parent commit is judged by what it brings into main, against its
# first parent, so a merge counts with everything it merges in. If that touches code (anything
# outside docs/), GitHub's merge button must have made it (committer noreply@github.com). A
# check-in that pushed code straight to main, or merged a branch itself and pushed that, is
# refused, named, and left for a person.
by_github() { [ "$(git show -s --format='%ce' "$1")" = "noreply@github.com" ]; }
code_differs() {  # rev rev: any change outside docs/? Can't tell (an error, no parent): yes.
  git diff --quiet "$1" "$2" -- . ':(exclude)docs' 2>/dev/null
  [ $? -ne 0 ]
}
# A step that needs no check: it changes no code, and if it's a merge, it left out no code its
# other sides brought (a merge that keeps its own older copy, like `pull -s ours`, would undo a
# pull request the server already runs).
step_ok() {  # rev
  code_differs "$1^1" "$1" && return 1
  local p b
  for p in $(git rev-list --parents -n 1 "$1" | cut -s -d' ' -f3-); do
    b=$(git merge-base "$1^1" "$p") || return 1
    code_differs "$b" "$p" && return 1
  done
  return 0
}
# One exception: a check-in that pulls with a merge before it pushes the journal. Its merge is
# fine if it brings in only what GitHub merged: its code is exactly the pulled side's, that side
# changed code only in commits GitHub made or in other sessions' pulls like this one (walked back
# like main, until it meets our side), and our side changed no code since then.
pulled_in() {  # merge
  local m=$1 c
  c=$(git rev-parse -q --verify "$m^2") || return 1   # not a merge
  git rev-parse -q --verify "$m^3" >/dev/null && return 1   # a pull merges two sides, no more
  code_differs "$c" "$m" && return 1                  # the merge has code of its own
  while ! git merge-base --is-ancestor "$c" "$m^1"; do
    step_ok "$c" || by_github "$c" || pulled_in "$c" || return 1
    c=$(git rev-parse -q --verify "$c^1") || return 1  # never met our side
  done
  ! code_differs "$c" "$m^1"                          # our side changed no code since
}
bad=""
for sha in $(git rev-list --first-parent "$before..$after"); do
  step_ok "$sha" && continue
  by_github "$sha" || pulled_in "$sha" || { bad=$sha; break; }
done
if [ -n "$bad" ]; then
  echo "$after" > "$failed"
  alert high "kal update refused" "Code reached main without a pull request (${bad:0:7}). Nothing changed. If it's OK, update by hand: docs/DEPLOY.md."
  say "refused: ${bad:0:7} changed code but was not merged through a pull request"
  exit 1
fi

# Docs-only (the check-in's journal): fast-forward quietly, no tests, no restart.
if git diff --quiet "$before" "$after" -- . ':(exclude)docs'; then
  git merge --quiet --ff-only "$after" && { say "docs only: ${before:0:7} -> ${after:0:7}, no restart"; exit 0; }
  alert high "kal update skipped" "The server's copy has its own changes. Nothing changed."
  exit 1
fi

was_active=0
systemctl is-active --quiet kal && was_active=1

# Build and test the NEW code in a throwaway copy, as a locked-down user with its own venv.
# This never touches the live venv or the live code, and the tests can't read DATA or ETC.
# The copy belongs to that user; what root writes (the test log, the package list) goes in
# a separate folder only root can open, so nothing the tests leave behind can steer root.
stage=$(mktemp -d)
work=$(mktemp -d)
cleanup() { rm -rf "$stage" "$work"; }
trap cleanup EXIT
git archive "$after" | tar -x -C "$stage"
cat > "$stage/.stage-run.sh" <<RUN
#!/bin/sh
set -e
cd "$stage"
"$STAGE_PYTHON" -m venv venv
venv/bin/pip install --quiet -r requirements.txt
venv/bin/python -m unittest discover -s tests -t . -q
RUN
if [ "$(id -u)" -eq 0 ]; then
  chown -R "$STAGE_USER": "$stage" || { say "can't hand the test copy to $STAGE_USER"; exit 1; }
fi
if runuser -u "$STAGE_USER" -- env -i PATH=/usr/bin:/bin HOME="$stage" sh "$stage/.stage-run.sh" \
     >"$work/tests.log" 2>&1; then
  tests_ok=1
else
  tests_ok=0
  grep -E "^(FAIL|ERROR):|^Ran |^FAILED" "$work/tests.log" | tail -n 20
fi
# Nothing the tests started may outlive them (only for our own user: nobody is shared).
[ "$STAGE_USER" = kal-stage ] && pkill -KILL -u kal-stage 2>/dev/null
if [ "$tests_ok" -ne 1 ]; then
  echo "$after" > "$failed"
  alert high "kal update failed its tests" "It stayed on the old version (${before:0:7}). Nothing changed."
  say "tests failed for ${after:0:7}; left on ${before:0:7}"
  exit 1
fi

# Tests passed. Now, and only now, touch the live tree and the live venv.
freeze="$work/freeze-before.txt"
"$VENV/bin/pip" freeze > "$freeze" 2>/dev/null || : > "$freeze"
if ! git merge --quiet --ff-only "$after"; then
  alert high "kal update skipped" "The server's copy has its own changes. Nothing changed."
  exit 1
fi
"$VENV/bin/pip" install --quiet -r requirements.txt
chmod -R a+rX "$APP"
chmod -R go-w "$APP" "$VENV"

health_ok() {  # the same check the /usr/local/bin/kal wrapper uses, run as the kal user
  runuser -u "$KAL_USER" -- env KAL_HOME="$DATA" "$VENV/bin/python" -m kal health --alive >/dev/null 2>&1
}
restart() {
  # Wait (up to an hour) for a research run to finish, so the restart doesn't kill it. The
  # lock file must belong to the kal user: research opens it to write, and couldn't if root
  # made it. Still running after an hour: restart anyway, research retries after a restart.
  [ -e "$RESEARCH_LOCK" ] || runuser -u "$KAL_USER" -- touch "$RESEARCH_LOCK" 2>/dev/null
  flock -w "$RESEARCH_WAIT" "$RESEARCH_LOCK" systemctl try-restart kal || systemctl try-restart kal
}
roll_back() {
  git reset --quiet --hard "$before"
  "$VENV/bin/pip" install --quiet -r requirements.txt
  [ -s "$freeze" ] && "$VENV/bin/pip" install --quiet -r "$freeze"   # exact old versions
  chmod -R a+rX "$APP"
  chmod -R go-w "$APP" "$VENV"
  [ "$was_active" = 1 ] && restart
  echo "$after" > "$failed"   # don't try the same version again tomorrow
  alert high "kal update rolled back" "The new version wasn't healthy. Back on ${before:0:7}."
  say "rolled back ${after:0:7} -> ${before:0:7}"
}

if [ "$was_active" = 1 ]; then
  restart
  [ "$WAIT" -gt 0 ] 2>/dev/null && sleep "$WAIT"
  # NRestarts counts automatic restarts since the last manual start, and our try-restart is
  # one (systemd 254+, which kal.service needs): it starts again from 0. So any restart now
  # is the new version crashing. (Comparing with the count before missed it: two earlier
  # crashes and one new one read as "fewer".)
  nafter=$(systemctl show kal -p NRestarts --value 2>/dev/null || echo 0)
  if ! health_ok || { [ "$nafter" -gt 0 ] 2>/dev/null; }; then
    roll_back
    exit 1
  fi
else
  say "the bot was stopped; updated the code and left it stopped"
fi

rm -f "$failed"
say "updated ${before:0:7} -> ${after:0:7}"
alert low "kal updated" "Now running $(git log -1 --format='%h %s')."
