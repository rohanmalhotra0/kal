#!/usr/bin/env bash
# Install kal on an Ubuntu server (24.04 or newer) as a systemd service.
#
#   sudo git clone https://github.com/YOU/kal.git /opt/kal
#   sudo bash /opt/kal/deploy/install.sh
#
# Safe to run again after `git pull`: it updates the Python packages, the service, and
# the `kal` command, and never touches your settings, key or data.
#
# Code: /opt/kal   Python: /opt/kal-venv   Data: /var/lib/kal   Settings: /etc/kal/kal.env
set -euo pipefail

APP="${APP:-/opt/kal}"
VENV="${VENV:-/opt/kal-venv}"
DATA="${DATA:-/var/lib/kal}"
ETC="${ETC:-/etc/kal}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n== %s\n' "$*"; }
die() { printf '\nSTOP: %s\n' "$*" >&2; exit 1; }
random() { python3 -c 'import secrets; print(secrets.token_urlsafe(18))'; }

# A phone-friendly alert topic: lowercase letters and digits, no look-alikes (l 1 o 0),
# so it can't be mistyped into the ntfy app. Same alphabet as the dashboard.
ntfy_topic_name() {
  python3 - <<'PY'
import secrets
abc = "abcdefghijkmnpqrstuvwxyz23456789"   # no l, 1, o or 0
print("kal-" + "".join(secrets.choice(abc) for _ in range(20)))
PY
}

# The `kal` command. It NEVER executes kal.env (a typo there must not stop `sudo kal off`);
# it reads KEY=VALUE lines, skips comments and bad lines, and exports only real settings.
kal_wrapper() {
  cat <<'WRAP'
#!/bin/sh
# Runs `python -m kal ...` as the kal user, with the service's settings.
# Use it with sudo:  sudo kal status
set -e
[ "$(id -u)" -eq 0 ] || { echo "Use: sudo kal $*"; exit 1; }
ENV_FILE="${KAL_ENV_FILE:-/etc/kal/kal.env}"
if [ -r "$ENV_FILE" ]; then
  CR=$(printf '\r')
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac   # blank or comment
    case "$line" in *=*) : ;; *) continue ;; esac  # must have an '='
    name=${line%%=*}
    case "$name" in [A-Z_]*) : ;; *) echo "kal: skipping a bad settings line" >&2; continue ;; esac
    case "$name" in *[!A-Z0-9_]*) echo "kal: skipping a bad settings line" >&2; continue ;; esac
    val=${line#*=}
    val=${val%"$CR"}
    case "$val" in \"*\") val=${val#\"}; val=${val%\"} ;; \'*\') val=${val#\'}; val=${val%\'} ;; esac
    export "$name=$val"
  done < "$ENV_FILE"
fi
export KAL_HOME="${KAL_DATA:-/var/lib/kal}"
cd "${KAL_APP:-/opt/kal}"
exec runuser -u "${KAL_USER:-kal}" -- env KAL_HOME="${KAL_DATA:-/var/lib/kal}" KAL_CMD="sudo kal" \
  "${KAL_VENV:-/opt/kal-venv}/bin/python" -m kal "$@"
WRAP
}

# Turn off SSH password login, but only if root already has an SSH key (never lock you out).
harden_ssh() {
  local keys="${KAL_ROOT_AUTHKEYS:-/root/.ssh/authorized_keys}"
  local dir="${KAL_SSHD_DIR:-/etc/ssh/sshd_config.d}"
  if [ -r "$keys" ] && grep -qE '^[[:space:]]*(ssh-|ecdsa-sha2-|sk-ssh-|sk-ecdsa-)' "$keys"; then
    mkdir -p "$dir"
    cat > "$dir/10-kal.conf" <<'SSHD'
# Written by kal's deploy/install.sh. You log in with an SSH key, so password login is
# off: it stops password-guessing attacks on root.
PasswordAuthentication no
KbdInteractiveAuthentication no
SSHD
    chmod 644 "$dir/10-kal.conf"
    systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
    say "SSH: password login is off (you log in with a key)."
  else
    say "SSH: no SSH key found for root, so password login is STILL ON."
    echo "   !! A password-only server can be guessed into. Add an SSH key and turn"
    echo "   !! password login off. See README step 3."
  fi
}

# A new server runs Ubuntu's own updates in its first minutes. They hold apt's locks, and
# apt-get gives up at once when a lock is held. So wait for them (up to 10 minutes).
apt_get() { apt-get -o DPkg::Lock::Timeout=600 "$@"; }
apt_update() {
  local i tries="${KAL_APT_TRIES:-40}"
  for i in $(seq 1 "$tries"); do
    apt_get update -qq && return 0
    [ "$i" = 1 ] && echo "Ubuntu is busy updating itself. Waiting for it (up to 10 minutes)..."
    [ "$i" = "$tries" ] || sleep "${KAL_APT_PAUSE:-15}"
  done
  die "apt-get update keeps failing. Wait 5 minutes, then run this again."
}

# Tests call one helper without needing root or a full install.
case "${1:-}" in
  --emit-topic)   ntfy_topic_name; exit 0 ;;
  --emit-wrapper) kal_wrapper; exit 0 ;;
  --harden-ssh)   harden_ssh; exit 0 ;;
  --apt-update)   apt_update; exit 0 ;;
esac

[ "$(id -u)" -eq 0 ] || die "Run it with sudo: sudo bash $0"
command -v apt-get >/dev/null || die "This script is for Ubuntu (it needs apt-get)."
[ -f "$SRC/kal/__main__.py" ] || die "Can't find the kal code next to this script."

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt_update
apt_get install -y -qq python3 python3-venv python3-pip git curl ca-certificates >/dev/null

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Python 3.11 or newer is needed. Use Ubuntu 24.04."

say "Keeping the clock right (Kalshi refuses orders sent with a wrong time)"
if apt_get install -y -qq chrony >/dev/null 2>&1; then
  systemctl enable --now chrony >/dev/null 2>&1 || true
else
  echo "(Couldn't install chrony. Using systemd-timesyncd instead.)"
  apt_get install -y -qq systemd-timesyncd >/dev/null 2>&1 || true
  timedatectl set-ntp true >/dev/null 2>&1 || true
fi
if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" = "yes" ]; then
  echo "Clock: in sync."
else
  echo "Clock: not in sync yet. It should be in a minute. Check with: timedatectl"
fi

say "Keeping system logs small"
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/kal.conf <<'JOURNAL'
# Written by kal's deploy/install.sh. A small server: keep the system log under 200 MB.
# (kal keeps its own log too, in /var/lib/kal/logs: 30 MB at most.)
[Journal]
SystemMaxUse=200M
JOURNAL
systemctl restart systemd-journald >/dev/null 2>&1 || true

say "Locking down SSH"
harden_ssh

say "Putting the code in $APP"
if [ "$SRC" != "$APP" ]; then
  mkdir -p "$APP"
  # Code only: never copy keys, settings or old state into a folder others can read.
  tar -C "$SRC" --exclude=./state --exclude='*.pem' --exclude='*.key' --exclude='.env' \
      --exclude='*/.env' -cf - . | tar -C "$APP" -xf -
fi

say "Making the kal user"
if ! id kal >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin kal
fi
# The auto-update runs new code's tests as this user: it can't read the key or settings.
if ! id kal-stage >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin kal-stage
fi
runuser -u kal -- test -r "$APP/kal/__main__.py" \
  || die "The kal user can't read $APP. Run: sudo chmod -R a+rX $APP"
mkdir -p "$DATA" "$ETC"
chown kal:kal "$DATA"
chmod 700 "$DATA"
chmod 700 "$ETC"

say "Installing Python packages in $VENV"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$APP/requirements.txt"

if [ ! -f "$ETC/kal.env" ]; then
  say "Writing $ETC/kal.env (your settings)"
  PASSWORD="$(random)"
  cat > "$ETC/kal.env" <<ENV
# kal settings. A line that starts with # is off.
# After a change: sudo systemctl restart kal

# Dashboard password (user name: anything).
KAL_DASHBOARD_PASSWORD=$PASSWORD

# Your Kalshi API key ID. Put the key file at $DATA/kalshi.pem
# (or run: sudo kal setup, which saves both for you).
#KALSHI_API_KEY_ID=

# Phone alerts go to the topic in $DATA/ntfy_topic. A topic set here wins over it.
#KAL_NTFY_TOPIC=

# Demo (fake money) vs real money is a setting, not an env var:
#   kal set env demo    /    kal set env prod

# Hourly status report on the "status" branch of a repo. Must be a PRIVATE repo.
# See docs/DEPLOY.md, step 10.
#KAL_GITHUB_TOKEN=
#KAL_GITHUB_REPO=you/kal-status
ENV
  chmod 600 "$ETC/kal.env"
  NEW_PASSWORD="$PASSWORD"
fi

say "Setting up phone alerts"
# The same file `kal setup` uses, so both always show the same topic.
if [ ! -s "$DATA/ntfy_topic" ] && ! grep -qs '^KAL_NTFY_TOPIC=.' "$ETC/kal.env"; then
  (umask 077 && ntfy_topic_name > "$DATA/ntfy_topic")
  NEW_TOPIC=1
fi
if [ -f "$DATA/ntfy_topic" ]; then
  chown kal:kal "$DATA/ntfy_topic"
  chmod 600 "$DATA/ntfy_topic"
fi

say "Adding the 'kal' command"
kal_wrapper > /usr/local/bin/kal
chmod 755 /usr/local/bin/kal

if [ -f "$DATA/kalshi.pem" ]; then
  chown kal:kal "$DATA/kalshi.pem"
  chmod 600 "$DATA/kalshi.pem"
fi

say "Installing the service"
install -m 644 "$APP/deploy/kal.service" /etc/systemd/system/kal.service
# Daily updates: installed, but OFF until you turn them on (see the end).
install -m 644 "$APP/deploy/kal-update.service" /etc/systemd/system/kal-update.service
install -m 644 "$APP/deploy/kal-update.timer" /etc/systemd/system/kal-update.timer
# Watchdog: alerts you if the bot dies or crash-loops. On by default (it only reads and alerts).
install -m 644 "$APP/deploy/kal-watch.service" /etc/systemd/system/kal-watch.service
install -m 644 "$APP/deploy/kal-watch.timer" /etc/systemd/system/kal-watch.timer
systemctl daemon-reload
systemctl enable kal >/dev/null 2>&1
systemctl enable --now kal-watch.timer >/dev/null 2>&1
systemctl restart kal

say "Quick check of Kalshi's API (no key needed)"
kal selftest || echo "(Some checks failed. Read the lines above.)"

cat <<NEXT

Done. The bot is installed but OFF. It will not trade until you turn it on.

Next steps (README.md, "Turn it on"):
 1. Send your key file. On your computer:
                        scp ~/Downloads/YOUR_KEY_FILE root@THIS_SERVER:/tmp/kalshi.pem
 2. Answer 5 questions: sudo kal setup
    Then:               sudo rm /tmp/kalshi.pem
 3. Phone alerts:       install the ntfy app, subscribe to the topic below.
    Test it:            sudo kal summary --send
 4. Dashboard, from your computer:
                        ssh -L 8787:localhost:8787 root@THIS_SERVER
                        then open http://localhost:8787
 5. Optional, daily auto-update. This runs whatever is on GitHub main, on your server.
    Turn it on only if you trust every push to main:
                        sudo systemctl enable --now kal-update.timer
Is it healthy? sudo kal health. Settings: $ETC/kal.env (then: sudo systemctl restart kal)
NEXT
if [ -n "${NEW_PASSWORD:-}" ]; then
  echo
  echo "Dashboard password: $NEW_PASSWORD"
  echo "(It is saved in $ETC/kal.env. Keep it secret.)"
fi
if [ -n "${NEW_TOPIC:-}" ]; then
  topic=$(cat "$DATA/ntfy_topic")
  echo
  echo "Phone alerts topic (type it into the ntfy app exactly):"
  echo "   $topic"
  echo "To read it, in groups of 4 (the spaces are NOT part of the name):"
  echo "   $(printf '%s' "$topic" | sed -E 's/(.{4})/\1 /g')"
  echo "(Saved in $DATA/ntfy_topic. Keep it secret. Test: sudo kal summary --send)"
fi
