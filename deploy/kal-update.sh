#!/usr/bin/env bash
# Daily safe update (turn on with: sudo systemctl enable --now kal-update.timer).
# Fetches the latest main, runs every test, and only then restarts the bot. If anything
# fails, it stays on the version that was running and sends a phone alert.
set -uo pipefail
APP=/opt/kal
VENV=/opt/kal-venv
DATA=/var/lib/kal

say() { echo "kal-update: $*"; }   # goes to the journal: journalctl -u kal-update
alert() {  # title, message
  local topic="${KAL_NTFY_TOPIC:-$(cat "$DATA/ntfy_topic" 2>/dev/null || true)}"
  [ -n "$topic" ] || return 0
  curl -s -m 10 -H "Title: $1" -d "$2" "https://ntfy.sh/$topic" >/dev/null || true
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

if ! git merge --quiet --ff-only origin/main; then
  alert "kal update skipped" "The server's copy has its own changes, so it can't update. Nothing changed."
  exit 1
fi
"$VENV/bin/pip" install --quiet -r requirements.txt
# A clean environment: the tests must never see the real key, alert topic, tokens or data.
scratch=$(mktemp -d)
env -i PATH=/usr/bin:/bin HOME="$scratch" KAL_HOME="$scratch/state" \
  "$VENV/bin/python" -m unittest discover -s tests -t . -q >"$scratch/tests.log" 2>&1
passed=$?
[ "$passed" -eq 0 ] || grep -E "^(FAIL|ERROR):|^Ran |^FAILED" "$scratch/tests.log" | tail -n 20
rm -rf "$scratch"
if [ "$passed" -ne 0 ]; then
  git reset --quiet --hard "$before"
  "$VENV/bin/pip" install --quiet -r requirements.txt
  echo "$after" > "$failed"
  alert "kal update failed its tests" "It stayed on the old version (${before:0:7}). Nothing changed."
  exit 1
fi
rm -f "$failed"
chmod -R a+rX "$APP"
systemctl restart kal
say "updated ${before:0:7} -> ${after:0:7}"
alert "kal updated" "Now running $(git log -1 --format='%h %s')."
