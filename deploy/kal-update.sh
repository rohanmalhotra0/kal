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

APP="${APP:-/opt/kal}"
VENV="${VENV:-/opt/kal-venv}"
DATA="${DATA:-/var/lib/kal}"
ETC="${ETC:-/etc/kal}"
KAL_USER="${KAL_USER:-kal}"          # the service user; health runs as them
STAGE_USER="${STAGE_USER:-nobody}"   # throwaway user that can't read DATA/ETC
STAGE_PYTHON="${STAGE_PYTHON:-python3}"
WAIT="${KAL_UPDATE_WAIT:-120}"       # seconds to let the bot settle before the health check
RESEARCH_LOCK="$DATA/.research.lock" # the file kal/research holds while a research run works

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

# Accident guard: every first-parent commit that touches code (anything outside docs/) must
# have come through GitHub's merge button (committer noreply@github.com). A check-in that
# pushed code straight to main by mistake is refused, named, and left for a person.
bad=""
for sha in $(git rev-list --first-parent "$before..$after"); do
  files=$(git diff-tree --no-commit-id --name-only -r --first-parent "$sha")
  [ -n "$files" ] || continue
  if printf '%s\n' "$files" | grep -qvE '^docs/'; then
    if [ "$(git show -s --format='%ce' "$sha")" != "noreply@github.com" ]; then
      bad=$sha
      break
    fi
  fi
done
if [ -n "$bad" ]; then
  echo "$after" > "$failed"
  alert high "kal update refused" "Code reached main without a pull request (${bad:0:7}). Nothing changed."
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
stage=$(mktemp -d)
cleanup() { rm -rf "$stage"; }
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
chmod -R a+rwX "$stage"
if runuser -u "$STAGE_USER" -- env -i PATH=/usr/bin:/bin HOME="$stage" sh "$stage/.stage-run.sh" \
     >"$stage/tests.log" 2>&1; then
  tests_ok=1
else
  tests_ok=0
  grep -E "^(FAIL|ERROR):|^Ran |^FAILED" "$stage/tests.log" | tail -n 20
fi
if [ "$tests_ok" -ne 1 ]; then
  echo "$after" > "$failed"
  alert high "kal update failed its tests" "It stayed on the old version (${before:0:7}). Nothing changed."
  say "tests failed for ${after:0:7}; left on ${before:0:7}"
  exit 1
fi

# Tests passed. Now, and only now, touch the live tree and the live venv.
freeze="$stage/freeze-before.txt"
"$VENV/bin/pip" freeze > "$freeze" 2>/dev/null || : > "$freeze"
if ! git merge --quiet --ff-only "$after"; then
  alert high "kal update skipped" "The server's copy has its own changes. Nothing changed."
  exit 1
fi
"$VENV/bin/pip" install --quiet -r requirements.txt
chmod -R a+rX "$APP"

health_ok() {  # the same check the /usr/local/bin/kal wrapper uses, run as the kal user
  runuser -u "$KAL_USER" -- env KAL_HOME="$DATA" "$VENV/bin/python" -m kal health --alive >/dev/null 2>&1
}
roll_back() {
  git reset --quiet --hard "$before"
  "$VENV/bin/pip" install --quiet -r requirements.txt
  [ -s "$freeze" ] && "$VENV/bin/pip" install --quiet -r "$freeze"   # exact old versions
  chmod -R a+rX "$APP"
  [ "$was_active" = 1 ] && flock -w 3600 "$RESEARCH_LOCK" systemctl try-restart kal
  alert high "kal update rolled back" "The new version wasn't healthy. Back on ${before:0:7}."
  say "rolled back ${after:0:7} -> ${before:0:7}"
}

if [ "$was_active" = 1 ]; then
  nbefore=$(systemctl show kal -p NRestarts --value 2>/dev/null || echo 0)
  # Wait for any research run to finish before the restart would kill it.
  flock -w 3600 "$RESEARCH_LOCK" systemctl try-restart kal
  [ "$WAIT" -gt 0 ] 2>/dev/null && sleep "$WAIT"
  nafter=$(systemctl show kal -p NRestarts --value 2>/dev/null || echo 0)
  if ! health_ok || { [ "$nafter" -gt "$nbefore" ] 2>/dev/null; }; then
    roll_back
    exit 1
  fi
else
  say "the bot was stopped; updated the code and left it stopped"
fi

rm -f "$failed"
say "updated ${before:0:7} -> ${after:0:7}"
alert low "kal updated" "Now running $(git log -1 --format='%h %s')."
