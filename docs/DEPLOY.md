# Run kal on a server

This puts kal on a small rented server, so it runs day and night, for months.
It costs about $12 to $14 a month.

You will need:

- A GitHub account with this repo.
- A Kalshi account (and a demo account for practice).
- A phone, for alerts.
- A terminal on your computer (Terminal on Mac, PowerShell on Windows).

Each step is short. Do them in order.

---

## What it costs

| What | Per month |
|---|---|
| Server: 2 GB, in the US (DigitalOcean, Vultr or Linode) | $10 to $12 |
| Server snapshots from the host (optional, step 1) | $1 to $2 |
| Phone alerts (ntfy) | free |
| GitHub (code, reports, checks) | free |
| **Total** | **about $12 to $14. Always under $20.** |

## 1. Rent a server

Any of these is fine. Pick the 2 GB size: a trade run and research together need about 550 MB,
too much for 1 GB.

| Company | Plan | Price |
|---|---|---|
| DigitalOcean | Basic Droplet, 2 GB | $12 a month |
| Vultr | Cloud Compute, 2 GB | $10 to $12 a month |
| Akamai (Linode) | Shared CPU, 2 GB | $12 a month |

When you make it:

- System: **Ubuntu 24.04 LTS**.
- Place: **a US data center**, for example New York or Virginia.
  You trade from the US, and Kalshi is a US exchange.
- Login: add your SSH key if it asks. Otherwise it gives you a root password.
- Backups: optional. Turn on the host's weekly backups if you like ($1 to $2).

Write down the server's IP address. Below it is called `SERVER_IP`.

## 2. Log in and lock the door

On your computer:

```bash
ssh root@SERVER_IP
```

Then, on the server:

```bash
apt update && apt upgrade -y
ufw allow OpenSSH
ufw --force enable
```

This blocks every port except SSH. The dashboard never needs an open port.

## 3. Get the code

```bash
git clone https://github.com/YOU/kal.git /opt/kal
```

Change `YOU` to your GitHub name.
If the repo is private, git asks for a user name and password.
Give your GitHub name, and a token as the password (see step 10 for how to make one).

## 4. Install

```bash
sudo bash /opt/kal/deploy/install.sh
```

It takes a minute or two. It:

- makes a `kal` user (the bot never runs as root),
- installs Python packages, and **chrony**, which keeps the clock right,
- sets up a service that starts on boot and restarts if it crashes,
- turns off SSH password login **if root has an SSH key** (if not, it says so and leaves
  it on, so it can never lock you out),
- turns on a watchdog that alerts your phone if the bot dies or crash-loops,
- adds a `kal` command,
- runs a quick check of Kalshi's API.

At the end it prints a **dashboard password** and a **phone alerts topic**.
Write them down. The password lives in `/etc/kal/kal.env`, the topic in `/var/lib/kal/ntfy_topic`.

The bot is **off** after install. It will not trade until you turn it on.

## 5. Phone alerts

The bot tells your phone when something needs you. It uses **ntfy**: a free app, no account.

1. Install **ntfy** on your phone (App Store or Google Play).
2. Find your topic (install.sh made a long random one):

   ```bash
   sudo cat /var/lib/kal/ntfy_topic
   ```

3. In the app, tap **+**. Type the topic. Tap **Subscribe**.
4. Test it:

   ```bash
   sudo kal summary --send
   ```

   Your phone should show "kal yesterday: ...".

The topic is like a password. Anyone who knows it can read your alerts.
Keep it long and random. To use your own instead, set `KAL_NTFY_TOPIC=...`
in `/etc/kal/kal.env`, then `sudo systemctl restart kal`. That one wins.
To make a random name:

```bash
python3 -c 'import secrets; print("kal-" + secrets.token_urlsafe(18))'
```

## 6. Check it is healthy

```bash
sudo kal health
```

Each line says `ok`, `WARN` or `FAIL`. Right after install, a `WARN` can be normal.
Run it again in 10 minutes.

## 7. Put your Kalshi key on the server

Start with the **demo** exchange. It uses fake money.

Shortcut: do steps 1 to 5 below. Then run `sudo kal setup`. It asks five plain
questions and does the rest for you. At question 2, press Enter to use the key you copied.
If the bot is on, setup turns it off first. It's only on again if you say yes at the end.

1. Make a demo account at [demo.kalshi.co](https://demo.kalshi.co).
2. In the demo site, open your account settings, then **API keys**. Make a new key.
3. It shows a **Key ID** and gives you a **private key** file. Kalshi shows the private key only once. Save it.
4. Copy the key file to the server. On your computer:

   ```bash
   scp ~/Downloads/YOUR-KEY-FILE root@SERVER_IP:/var/lib/kal/kalshi.pem
   ```

5. On the server, make the file private:

   ```bash
   sudo chown kal:kal /var/lib/kal/kalshi.pem
   sudo chmod 600 /var/lib/kal/kalshi.pem
   ```

6. Add the Key ID to the settings file:

   ```bash
   sudo nano /etc/kal/kal.env
   ```

   Find `#KALSHI_API_KEY_ID=`. Remove the `#`. Paste your Key ID after the `=`.
   Save with Ctrl+O, Enter. Leave with Ctrl+X.

7. Restart the bot:

   ```bash
   sudo systemctl restart kal
   ```

Keep the key secret. Never put it in git, chat, or email.
If it leaks, delete it on Kalshi and make a new one.

## 8. The $300 setup

Practice on demo first. Then switch to real money.

**On demo (fake money):**

```bash
sudo kal off
sudo kal set env demo
sudo kal selftest --order-probe   # every line should say ok
sudo kal on --budget 300
```

Let it run for a day or two. Look at `sudo kal status` and the dashboard (step 9).
Demo markets are quiet, so it may find little. That's fine.
The point is to see orders and safety stops work.

The order probe sends **two real orders** that can't fill: buy 1 YES at $0.01, only
where the cheapest seller wants $0.10 or more, and buy 1 NO at $0.01, only where the
best YES buyer pays $0.90 or less. Nobody trades that far off, so they cost nothing.

**Then real money:**

1. On [kalshi.com](https://kalshi.com), make an API key (same as step 7).
2. Copy it to the server as `/var/lib/kal/kalshi.pem` (this replaces the demo key). Do the `chown` and `chmod` again.
3. Edit `/etc/kal/kal.env`: put in the new `KALSHI_API_KEY_ID`.
4. Switch and turn it on:

   ```bash
   sudo kal off
   sudo kal set env prod
   sudo systemctl restart kal
   sudo kal selftest --order-probe
   sudo kal on --budget 300
   ```

The budget is the most the bot can ever lose in total. It may reinvest its own profits.
Demo and real money keep separate books, so demo results never count as real profit.
What you change on demo (budget, caps, modes) is practice too. Back on prod, each goes
back to what it was on prod. On a new server that's a $0 budget, so set it as above.

Tip: Kalshi pays interest on balances of **$250 or more**.

## 9. Open the dashboard (safely)

The dashboard can move your money. So it only listens on the server itself.
You reach it through an **SSH tunnel**, which is encrypted.

On your computer:

```bash
ssh -L 8787:localhost:8787 root@SERVER_IP
```

Leave that window open. Then open **http://localhost:8787** in your browser.

- User name: anything.
- Password: the one from step 4.

Close the window to close the tunnel.

## 10. Status reports on GitHub

Every hour, the bot writes `report.md` and `report.json` to a branch called
`status` in a repo. The check-in that runs every 2 hours reads them.
You can read them on your phone, too.

The report shows your balance and trades, so it only goes to a **private** repo.
The bot refuses a public one. Make a new private repo for it, for example `kal-status`
(GitHub > New repository > Private).

Make a token that can only touch that one repo:

1. On GitHub: **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. Name: `kal status`. Expiration: 90 days. (Set a reminder to make a new one.)
3. Repository access: **Only select repositories** → pick your `kal-status` repo.
4. Permissions → Repository permissions → **Contents: Read and write**. Nothing else.
5. Generate it. Copy it. (It starts with `github_pat_`.)

On the server:

```bash
sudo nano /etc/kal/kal.env
```

Set these two lines (remove the `#`):

```
KAL_GITHUB_TOKEN=github_pat_...
KAL_GITHUB_REPO=YOU/kal-status
```

Then:

```bash
sudo systemctl restart kal
sudo kal report --push        # test it now
```

Last, let the check-in read it. Open **github.com/settings/installations**, click
**Configure** next to **Claude**, add `kal-status` under **Repository access**, and **Save**.
(Without this, the check-in can't see the report and says the server isn't reporting.)

The report never has keys, tokens, passwords or your alert topic.
It does show your balance and profit, which is why the repo must be private.

---

## What it does by itself

| When | What |
|---|---|
| Every 5 minutes | Looks for trades. |
| Every 10 minutes | Checks disk space, the clock, and that trading works. |
| Every 10 minutes | Watchdog (outside the bot): alerts you if the bot is dead or crash-looping. |
| Every hour | Writes the status report to GitHub. |
| Every day, 09:00 UTC | Backs up the books. Deletes history older than 180 days. |
| Every day, 13:00 UTC | Sends yesterday's summary to your phone. |
| Every day | Research: tests new ideas on past markets. |

13:00 UTC is 9 AM in New York in summer, 8 AM in winter.
To change it (the hour is in UTC): `sudo kal set summary_hour_utc 14`

## Alerts: what they mean

| Alert | What it means | What to do |
|---|---|---|
| **kal stopped itself** (loud) | A safety stop. The bot is off. The alert says why. | See the next table. |
| **kal is not running** (loud) | The watchdog found the bot stopped. | Start it: `sudo systemctl start kal`. If it won't stay up: `sudo kal logs` |
| **kal keeps restarting** (loud) | The bot is crash-looping. | `sudo kal logs`. The check-in will look into it. |
| **kal looks unhealthy** (loud) | The bot runs, but a check fails (e.g. the scheduler stopped). | `sudo kal health` |
| **kal made 1 real trade(s)** | It bought something with real money. | Nothing. Look at the dashboard if you like. |
| **kal yesterday: +$1.23** (silent, daily) | Yesterday's results. | Nothing. Just read it. |
| **kal updated** (silent) | The daily auto-update moved to new code and the bot is alive. | Nothing. |
| **kal update rolled back / failed / refused** (loud) | The daily auto-update stayed on the old code (bad tests, unhealthy restart, or code not merged through a pull request). | `sudo journalctl -u kal-update`. "Refused" blocks updates until you look at that commit on GitHub; if it's fine, update by hand (see "Update to new code"). |
| **kal warning: disk** | The disk is getting full. Under 200 MB it stops itself. | Free some space (see below). |
| **kal warning: clock** | The server clock is off. Kalshi may refuse orders. | `timedatectl`. If it says not synchronized: `sudo systemctl restart chrony` |
| **kal warning: trading** | Trade runs keep failing. Kalshi or the internet is down. It keeps trying. | Usually nothing. If it lasts a day: `sudo kal logs` |
| **kal warning: report** | The status report can't reach GitHub. Usually the token ran out (they last 90 days). | Make a new token: step 10 below. Until then the check-in can't see the bot. |
| **kal backup failed** | Last night's backup didn't work. | `sudo kal backup` shows why. |

Warnings and watchdog alerts come at most once a day each.
The watchdog runs every 10 minutes from outside the bot, so you hear even if the bot is
dead. See it: `sudo journalctl -u kal-watch`.

## If it stopped itself

It stays stopped until a person says so. `sudo kal health` shows why.

| The reason says | What to do |
|---|---|
| lost ... today, over the daily limit | It lost about 10% in one day. Look at the trades. Turn it back on tomorrow. |
| failed runs in a row | Something is broken. Look: `sudo kal logs` |
| basket left half-built, or an order stayed on the book | Look at your open positions on Kalshi. |
| capital is used up | The $300 is gone. Stop and think before you add more. |
| Disk almost full | Free some space (below). |
| The server clock is ... Kalshi's clock | Fix the clock: `sudo systemctl restart chrony`, then `timedatectl`. |
| Books restored from the ... backup | See "Restore" below. |

When the cause is fixed: `sudo kal on --clear-trip`

**Free disk space:**

```bash
sudo journalctl --vacuum-size=100M
sudo apt clean
df -h /
```

## Backups

Every day the bot copies its books to `/var/lib/kal/backups/<date>/`.
It keeps the last 14 days. Your trades are never deleted.

- Make one now: `sudo kal backup`
- See them: `sudo kal restore`

Now and then, copy them to your computer. On your computer:

```bash
scp -r root@SERVER_IP:/var/lib/kal/backups ./kal-backups
```

Your key (`kalshi.pem`) is never in a backup. If you lose it, make a new key on Kalshi.

## Restore

Only do this if the books (`kal.db`) are broken.

1. See the days: `sudo kal restore`
2. Put one back: `sudo kal restore 2026-10-24 --yes`
   If it says to stop the service first: `sudo systemctl stop kal`,
   run step 2 again, then `sudo systemctl start kal`.
   If it says it stopped part way, run step 2 again.
3. The bot stops. Turn it back on: `sudo kal on --clear-trip`
4. Before it trades again, it adds the trades Kalshi shows since that day
   to the books, so the $300 limit stays true. If it can't read them, it
   stops again and says so.
5. Some of those trades may be yours, not the bot's. So when they settle,
   only their losses count against the bot. Their wins stay yours: they
   never become money the bot may use.

The old books are kept in `/var/lib/kal/backups/before-restore-.../`.

---

## Everyday commands

| Do this | Command |
|---|---|
| How is it doing? | `sudo kal status` |
| Is the server healthy? | `sudo kal health` |
| Last 50 log lines | `sudo kal logs` |
| Full report | `sudo kal report` |
| Yesterday's summary | `sudo kal summary` |
| Turn it on | `sudo kal on --budget 300` |
| Turn it off | `sudo kal off` |
| Back up now | `sudo kal backup` |
| Change a setting | `sudo kal set max_per_trade 5` |
| Strategy mode | `sudo kal set strategies.ladder_arb.mode live` |
| Check Kalshi's API | `sudo kal selftest` |
| What would it buy? | `sudo kal scan` |
| System log | `sudo journalctl -u kal -f` |
| Restart | `sudo systemctl restart kal` |

## Update to new code

By hand (keeps your settings, key and data):

```bash
cd /opt/kal && sudo git pull && sudo bash deploy/install.sh
```

Re-running `install.sh` also refreshes the `kal` command and the units, so after you
update kal, existing servers pick up any fixes to them.

You can also let it update itself once a day. **This runs whatever is on GitHub `main`,
on your server, so turn it on only if you trust every push to `main`.**

```bash
sudo systemctl enable --now kal-update.timer
```

Each morning (about 09:30 UTC) it fetches `main` and then, to stay safe:

- **Docs-only changes** (like the check-in's journal) fast-forward with no restart.
- **Code must have come through a pull request.** Code pushed straight to `main` is
  refused, and your phone gets "kal update refused" naming the commit. So is a merge
  that would put older code back.
- It **builds the new code and runs every test in a throwaway copy**, as a locked-down
  user that can't read your key, topic or books. Only if the tests pass does it swap the
  new code into `/opt/kal`. If a test fails, it stays put and sends "kal update failed
  its tests" (once per bad version).
- After the restart it **checks the bot is really alive**. If not, it puts the old code
  and packages back and sends "kal update rolled back".

See what it did: `sudo journalctl -u kal-update`. Turn it off:
`sudo systemctl disable --now kal-update.timer`

## Don't lose the data folder

`/var/lib/kal` holds the bot's own books (`kal.db`).
The budget limit is worked out from them.
Never delete it while the bot holds positions.

## Something wrong?

- **Dashboard won't load:** is the SSH tunnel window open? Is the bot running? `sudo systemctl status kal`
- **See errors:** `sudo kal logs`, or older ones: `sudo journalctl -u kal -n 100`
- **selftest fails:** Kalshi may have changed its API. Leave the bot off. The check-in will look into it.
- **"Not starting":** you set a host other than 127.0.0.1 without `KAL_DASHBOARD_PASSWORD`. Use the SSH tunnel instead.
- **No phone alerts:** is the topic in the app the same as `sudo cat /var/lib/kal/ntfy_topic`
  (or `KAL_NTFY_TOPIC` in `/etc/kal/kal.env`, if you set one)? Test: `sudo kal summary --send`

---

## Other way: Docker

Use this instead of steps 4 and 7 if you like Docker.

1. Install Docker: `curl -fsSL https://get.docker.com | sh`
2. Keep the clock right: `sudo apt install -y chrony` (the container uses the server's clock).
3. Make `/opt/kal/deploy/.env` with your settings (it is never committed):

   ```
   KAL_DASHBOARD_PASSWORD=pick-a-long-random-password
   KAL_NTFY_TOPIC=kal-pick-a-long-random-name
   KALSHI_API_KEY_ID=your-key-id
   KAL_GITHUB_TOKEN=github_pat_...
   KAL_GITHUB_REPO=YOU/kal-status
   ```

   Then: `chmod 600 /opt/kal/deploy/.env`

4. Start it:

   ```bash
   cd /opt/kal
   docker compose -f deploy/docker-compose.yml up -d --build
   ```

5. Put the key in its data volume:

   ```bash
   D="docker compose -f deploy/docker-compose.yml"
   $D cp ~/kalshi.pem kal:/data/kalshi.pem
   $D exec -u root kal sh -c 'chown kal:kal /data/kalshi.pem && chmod 600 /data/kalshi.pem'
   $D restart
   ```

6. Commands run inside it: `$D exec kal python -m kal health`
7. Is it healthy? `docker ps` shows `healthy` or `unhealthy`.
8. Copy the backups out: `$D cp kal:/data/backups ./kal-backups`

The port is only open to the server itself (`127.0.0.1:8787`).
Use the same SSH tunnel as step 9.
