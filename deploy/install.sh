#!/usr/bin/env bash
# Install kal on an Ubuntu server (24.04 or newer) as a systemd service.
#
#   sudo git clone https://github.com/YOU/kal.git /opt/kal
#   sudo bash /opt/kal/deploy/install.sh
#
# Safe to run again after `git pull`: it updates the Python packages and the
# service, and never touches your settings, key or data.
#
# Code: /opt/kal   Python: /opt/kal-venv   Data: /var/lib/kal   Settings: /etc/kal/kal.env
set -euo pipefail

APP=/opt/kal
VENV=/opt/kal-venv
DATA=/var/lib/kal
ETC=/etc/kal
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n== %s\n' "$*"; }
die() { printf '\nSTOP: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run it with sudo: sudo bash $0"
command -v apt-get >/dev/null || die "This script is for Ubuntu (it needs apt-get)."
[ -f "$SRC/kal/__main__.py" ] || die "Can't find the kal code next to this script."

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates >/dev/null

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Python 3.11 or newer is needed. Use Ubuntu 24.04."

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
  PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  cat > "$ETC/kal.env" <<ENV
# kal settings. A line that starts with # is off.
# After a change: sudo systemctl restart kal

# Dashboard password (user name: anything).
KAL_DASHBOARD_PASSWORD=$PASSWORD

# Your Kalshi API key ID. Put the key file at $DATA/kalshi.pem
#KALSHI_API_KEY_ID=

# Demo (fake money) vs real money is a setting, not an env var:
#   kal set env demo    /    kal set env prod

# Hourly status report on the repo's "status" branch (see docs/DEPLOY.md).
#KAL_GITHUB_TOKEN=
#KAL_GITHUB_REPO=you/kal
ENV
  chmod 600 "$ETC/kal.env"
  NEW_PASSWORD="$PASSWORD"
fi

say "Adding the 'kal' command"
cat > /usr/local/bin/kal <<'WRAP'
#!/bin/sh
# Runs `python -m kal ...` as the kal user, with the service's settings.
# Use it with sudo:  sudo kal status
set -e
[ "$(id -u)" -eq 0 ] || { echo "Use: sudo kal $*"; exit 1; }
set -a
[ -r /etc/kal/kal.env ] && . /etc/kal/kal.env
set +a
export KAL_HOME=/var/lib/kal
cd /opt/kal
exec runuser -u kal -- env KAL_HOME=/var/lib/kal /opt/kal-venv/bin/python -m kal "$@"
WRAP
chmod 755 /usr/local/bin/kal

if [ -f "$DATA/kalshi.pem" ]; then
  chown kal:kal "$DATA/kalshi.pem"
  chmod 600 "$DATA/kalshi.pem"
fi

say "Installing the service"
install -m 644 "$APP/deploy/kal.service" /etc/systemd/system/kal.service
systemctl daemon-reload
systemctl enable kal >/dev/null 2>&1
systemctl restart kal

say "Quick check of Kalshi's API (no key needed)"
kal selftest || echo "(Some checks failed. Read the lines above.)"

cat <<NEXT

Done. The bot is installed but OFF. It will not trade until you turn it on.

Next steps:
 1. Put your Kalshi key on the server (see docs/DEPLOY.md, step 5).
 2. Edit settings:      sudo nano $ETC/kal.env
    Then restart:       sudo systemctl restart kal
 3. Check it works:     sudo kal selftest --order-probe
 4. See the dashboard from your computer:
                        ssh -L 8787:localhost:8787 ${SUDO_USER:-you}@THIS_SERVER
                        then open http://localhost:8787
 5. Turn it on:         sudo kal on --budget 300
 6. Watch it:           sudo kal status
                        sudo journalctl -u kal -f
NEXT
if [ -n "${NEW_PASSWORD:-}" ]; then
  echo
  echo "Dashboard password: $NEW_PASSWORD"
  echo "(It is saved in $ETC/kal.env. Keep it secret.)"
fi
