# Run kal on a server

This puts kal on a small rented server, so it runs day and night.
It costs about $5 to $6 a month.

You will need:

- A GitHub account with this repo.
- A Kalshi account (and a demo account for practice).
- A terminal on your computer (Terminal on Mac, PowerShell on Windows).

Each step is short. Do them in order.

---

## 1. Rent a server

Any of these is fine. Pick the 1 GB size.

| Company | Plan | Price |
|---|---|---|
| DigitalOcean | Basic Droplet, 1 GB | $6 a month |
| Vultr | Cloud Compute, 1 GB | $5 to $6 a month |
| Akamai (Linode) | Nanode, 1 GB | $5 a month |

When you make it:

- System: **Ubuntu 24.04 LTS**.
- Place: **a US data center** (for example New York or Virginia). Kalshi is a US exchange.
- Login: add your SSH key if it asks. Otherwise it gives you a root password.

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
Give your GitHub name, and a token as the password (see step 8 for how to make one).

## 4. Install

```bash
sudo bash /opt/kal/deploy/install.sh
```

It takes a minute or two. It:

- makes a `kal` user (the bot never runs as root),
- installs Python packages,
- sets up a service that starts on boot and restarts if it crashes,
- adds a `kal` command,
- runs a quick check of Kalshi's API.

At the end it prints a **dashboard password**. Write it down.
It also lives in `/etc/kal/kal.env`.

The bot is **off** after install. It will not trade until you turn it on.

## 5. Put your Kalshi key on the server

Start with the **demo** exchange. It uses fake money.

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

## 6. Test on demo first

```bash
sudo kal set env demo         # fake money
sudo kal selftest --order-probe
```

Every line should say `ok`.

The order probe sends **one real order**: buy 1 YES at $0.01, only where the
cheapest seller wants $0.10 or more. Nobody sells that low, so it can't fill.
Kalshi accepting it proves the order format works. It costs nothing.

Then:

```bash
sudo kal scan                 # what it would buy now (never trades)
sudo kal on --budget 300      # turn it on
sudo kal status               # how is it doing?
```

Let it run on demo for a day or two. Look at the dashboard (step 7).
Demo markets are quiet, so it may find little. That's fine.
The point is to see orders and safety stops work.

## 7. Open the dashboard (safely)

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

## 8. Status reports on GitHub

Every hour, the bot writes `report.md` and `report.json` to a branch called
`status` in your repo. The check-in that runs every 2 hours reads them.
You can read them on your phone, too.

Make a token that can only touch this one repo:

1. On GitHub: **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. Name: `kal status`. Expiration: 90 days. (Set a reminder to make a new one.)
3. Repository access: **Only select repositories** → pick your `kal` repo.
4. Permissions → Repository permissions → **Contents: Read and write**. Nothing else.
5. Generate it. Copy it. (It starts with `github_pat_`.)

On the server:

```bash
sudo nano /etc/kal/kal.env
```

Set these two lines (remove the `#`):

```
KAL_GITHUB_TOKEN=github_pat_...
KAL_GITHUB_REPO=YOU/kal
```

Then:

```bash
sudo systemctl restart kal
sudo kal report --push        # test it now
```

The report never has keys, tokens, or passwords.
But it does show your balance and profit. **Keep the repo private** if you
don't want others to see that.

## 9. Switch to real money

Only after demo looked good.

1. On [kalshi.com](https://kalshi.com), make an API key (same as step 5).
2. Copy it to the server as `/var/lib/kal/kalshi.pem` (this replaces the demo key). Do the `chown` and `chmod` again.
3. Edit `/etc/kal/kal.env`: put in the new `KALSHI_API_KEY_ID`.
4. Switch, restart and check:

   ```bash
   sudo kal off
   sudo kal set env prod
   sudo systemctl restart kal
   sudo kal selftest --order-probe
   sudo kal on --budget 300
   ```

Demo and real money keep separate books, so demo results never count as real profit.

The budget is the most the bot can ever lose in total. It may reinvest its own profits.

Tip: Kalshi pays interest on balances of **$250 or more**.

---

## Everyday commands

| Do this | Command |
|---|---|
| How is it doing? | `sudo kal status` |
| Full report | `sudo kal report` |
| Turn it on | `sudo kal on --budget 300` |
| Turn it off | `sudo kal off` |
| Change a setting | `sudo kal set max_per_trade 5` |
| Strategy mode | `sudo kal set strategies.ladder_arb.mode live` |
| Check Kalshi's API | `sudo kal selftest` |
| What would it buy? | `sudo kal scan` |
| Live log | `sudo journalctl -u kal -f` |
| Restart | `sudo systemctl restart kal` |

## If it stopped itself

It stops itself when it loses too much in a day, or when runs keep failing.
It stays stopped until a person says so.

1. `sudo kal status` shows why.
2. Find and fix the cause first.
3. Then: `sudo kal on --clear-trip`

## Update to new code

```bash
cd /opt/kal && sudo git pull && sudo bash deploy/install.sh
```

This keeps your settings, key and data.

## Don't lose the data folder

`/var/lib/kal` holds the bot's own books (`kal.db`).
The budget limit is worked out from them.
Never delete it while the bot holds positions.

Backup (this includes your key, so keep the file safe):

```bash
sudo tar czf ~/kal-backup.tgz -C /var/lib kal
```

## Something wrong?

- **Dashboard won't load:** is the SSH tunnel window open? Is the bot running? `sudo systemctl status kal`
- **See errors:** `sudo journalctl -u kal -n 100`
- **selftest fails:** Kalshi may have changed its API. Leave the bot off. The check-in will look into it.
- **"Not starting":** you set a host other than 127.0.0.1 without `KAL_DASHBOARD_PASSWORD`. Use the SSH tunnel instead.

---

## Other way: Docker

Use this instead of steps 4 and 5 if you like Docker.

1. Install Docker: `curl -fsSL https://get.docker.com | sh`
2. Make `/opt/kal/deploy/.env` with your settings (it is never committed):

   ```
   KAL_DASHBOARD_PASSWORD=pick-a-long-random-password
   KALSHI_API_KEY_ID=your-key-id
   KAL_GITHUB_TOKEN=github_pat_...
   KAL_GITHUB_REPO=YOU/kal
   ```

   Then: `chmod 600 /opt/kal/deploy/.env`

3. Start it:

   ```bash
   cd /opt/kal
   docker compose -f deploy/docker-compose.yml up -d --build
   ```

4. Put the key in its data volume:

   ```bash
   D="docker compose -f deploy/docker-compose.yml"
   $D cp ~/kalshi.pem kal:/data/kalshi.pem
   $D exec -u root kal sh -c 'chown kal:kal /data/kalshi.pem && chmod 600 /data/kalshi.pem'
   $D restart
   ```

5. Commands run inside it: `$D exec kal python -m kal status`

The port is only open to the server itself (`127.0.0.1:8787`).
Use the same SSH tunnel as step 7.
