# kal

A bot that trades on [Kalshi](https://kalshi.com), a US prediction market.
It runs by itself on a $12-a-month server.

**Profits are not guaranteed.** Only give it money you can afford to lose.

## Turn it on (9 steps, about 20 minutes)

1. **Put exactly $300 in your Kalshi account.** Then even a bug can't lose more.
2. **Make a Kalshi API key.** kalshi.com > Account > API Keys > Create.
   Save the key file. Copy the Key ID.
3. **Rent a server.** [DigitalOcean](https://www.digitalocean.com) > Create > Droplet >
   Ubuntu 24.04 > Basic > **$12/month (2 GB)** > New York.
   For **Authentication, pick SSH key** (not Password). Make one and add it:
   ```bash
   ssh-keygen -t ed25519       # on your computer; press Enter for every question
   cat ~/.ssh/id_ed25519.pub   # paste this line into DigitalOcean's "New SSH Key" box
   ```
   Create the droplet. Copy its IP address. (Install then turns off password login.)
4. **Log in** from your computer's terminal: `ssh root@SERVER_IP`
   (Or click **Web Console** on the droplet's page. It needs no SSH.)
5. **Install** (paste both lines):
   ```bash
   git clone https://github.com/rohanmalhotra0/kal.git /opt/kal
   sudo bash /opt/kal/deploy/install.sh
   ```
   Write down the **dashboard password** and **alerts topic** it prints.
6. **Send it your key file.** It goes straight into the bot's own folder, which only the bot can open.
   In a *second* terminal on your computer:
   `scp ~/Downloads/YOUR_KEY_FILE root@SERVER_IP:/var/lib/kal/kalshi.pem`
   In the Web Console instead: open the key file in a text editor and copy all of it.
   Then type `nano /var/lib/kal/kalshi.pem`, paste, and press Ctrl+O, Enter, Ctrl+X.
7. **Answer 5 questions.** Back on the server: `sudo kal setup`
   - Key ID: paste it. Key file: press Enter (it's already in place).
   - Practice or real: **real** (new ideas still start on paper).
   - Budget: **300**. Alerts: yes.

   It checks everything, then asks to turn the bot on. Say yes.
   Check it any time: `sudo kal doctor` (one list; it ends with "Ready." when all is well).
8. **Phone alerts.** Install the free **ntfy** app. Subscribe to the topic from step 5.
   Then on the server: `sudo kal summary --send`. No buzz? The name is wrong.
9. **Watch it.** On your computer: `ssh -L 8787:localhost:8787 root@SERVER_IP`,
   then open **http://localhost:8787** (password from step 5).

**Stop it:** `sudo kal off`. **From your phone:** delete the API key in the Kalshi app.

**Optional: update itself daily.** This runs whatever is on GitHub `main`, on your server.
Turn it on only if you trust every push to `main`:
`sudo systemctl enable --now kal-update.timer`

Before its first real trade, the bot sends two $0.01 test orders (one YES, one NO) that
can't fill, to prove Kalshi accepts its orders. Want to practice with fake money first? See
[docs/DEPLOY.md](docs/DEPLOY.md), step 7.

## What it does

- **Every 5 minutes:** looks for trades. Makes the ones its rules allow.
- **Every 10 minutes:** checks disk space, the clock, and that trading works.
- **Every hour:** writes a short report to the `status` branch on GitHub.
- **Every day:** tests new ideas on past markets (research), backs up its books,
  and sends yesterday's summary to your phone.
- **Dashboard:** turn it on or off, set the budget, see what it did.

## Strategies, safest first

| Strategy | What it does | Risk | Starts as |
|---|---|---|---|
| `basket_arb` | Only one outcome can win, so NO on several outcomes pays a known amount. Buys when that costs less. | Very low. Profit is locked in. | Live |
| `ladder_arb` | "Above 60" can't win unless "above 50" wins. Buys when that pair costs less than $1. | Very low. Profit is locked in. | Paper |
| `date_ladder_arb` | "Before Aug 1" can't win unless "before Sep 1" wins. Buys when that pair costs less than $1. | Low, once checked. | Paper. Real money only for events whose rules a person has read |
| `fed_link_arb` | If the Fed holds, the rate stays the same. So the Fed's decision and the rate after the meeting must agree. Buys a set that must pay $1 when it costs less. | Low, once checked. A rate move off the meeting date breaks it. | Paper. Real money only for meetings a person has checked |
| `exact_link_arb` | "CPI exactly 0.3%" and "CPI above 0.2%" ask about one number. They must agree. Buys a set that must pay $1 when it costs less. | Low, once checked. A report that never comes out breaks it. | Paper. Real money only for releases a person has checked |
| `range_link_arb` | "BTC between $71,700 and $71,799.99 at 2 PM" and "BTC above $71,699.99 at 2 PM" ask about one price. They must agree. Buys a set that must pay when it costs less. 8 coins. | Low, once checked. Gaps are rare and small. | Paper. Real money only for pairs a person has checked |
| `bracket_arb` | Ranges that cover every result: one must win. Buys all when they cost less than $1. | Low, once checked. | Paper, until a person checks it |
| `metal_link_arb` | At month end, the monthly series and the daily (or Friday's weekly) one both ask "gold above $X at 5 PM?". They should agree. Buys a pair that must pay $1 when it costs less. Gold and silver. | Medium. The two settle apart, and Kalshi can change the price source. | Paper only, always |
| `favorites` | Buys price ranges that won more often than their price in past markets. | Medium. A bet, not a sure thing. | Paper |
| `mentions` | "Will X say Y?" markets. Uses how often each word was said before. | Higher. | Paper |
| `longshot_fade` | Buys NO on cheap YES bets, only where cheap YES lost more often than its price in past markets. | High. One longshot that hits wipes out about 19 wins. | Paper |

**Paper** means pretend trades: no money, real results.

For `favorites` and `mentions`, each new idea is **promoted from paper by itself**,
but only after it makes money on paper (at least 30 settled trades, and a cautious
test says the profit is real). It goes back to paper if it starts to lose.
Real money also needs **you** to set that strategy to `live` on the dashboard.
Even then, only promoted ideas use real money.

"Locked in" still has one small risk: a price can move between two orders.
The bot checks prices again first, and stops at the first order that doesn't fill.

## Safety

- **Master switch.** Off until you turn it on.
- **Budget.** The most it can ever lose, in total. It may reinvest its own profits.
- **Caps per trade.** $10 a bet, $30 an arbitrage, $20 per event (you can change these).
- **Daily loss limit.** Lose over 10% of the budget in a day, and it turns itself off.
- **3 failed runs in a row** and it turns itself off.
- **Stays off** after a safety stop, until a person says so.
- **Paper first.** New ideas never start with real money.
- **Demo mode.** Practice on Kalshi's fake-money exchange.
- **Zero-risk order test.** No real money until Kalshi accepts two $0.01 orders (one YES, one NO) that can't fill.
- **Dashboard is never open to the internet.** You reach it with an SSH tunnel.
- **Disk and clock checks.** Under 200 MB of disk, or a clock 30 s off Kalshi's, it stops itself.
- **Phone alerts** (free ntfy app) when it stops itself, trades real money, or needs a look.
- **Watchdog.** A second timer, outside the bot, alerts you if the bot dies or crash-loops.
- **Daily backups** of its books, 14 days kept. `restore` puts one back.

## Quick start (on your computer)

You need Python 3.11 or newer.

```bash
pip install -r requirements.txt
python -m kal selftest     # does Kalshi's API look right?
python -m kal scan         # what would it buy right now? (never trades)
python -m kal app          # dashboard: http://127.0.0.1:8787
```

None of these need a key. To trade, add your Kalshi API key:

```bash
export KALSHI_API_KEY_ID=your-key-id
mkdir -p state && cp ~/Downloads/YOUR-KEY-FILE state/kalshi.pem && chmod 600 state/kalshi.pem
python -m kal set env demo           # fake money first
python -m kal selftest --order-probe
python -m kal on --budget 300
python -m kal app
```

## Run it on a server

See **[docs/DEPLOY.md](docs/DEPLOY.md)**. About $12 to $14 a month, always under $20:

| What | Per month |
|---|---|
| 2 GB server in the US (DigitalOcean, Vultr or Linode) | $10 to $12 |
| Host snapshots (optional) | $1 to $2 |
| Phone alerts (ntfy), GitHub | free |

The $300 setup, in short:

```bash
sudo kal set env demo              # practice with fake money
sudo kal selftest --order-probe    # test
sudo kal on --budget 300           # watch it for a day or two, then:
sudo kal off
sudo kal set env prod              # real money (with your real Kalshi key)
sudo kal on --budget 300
```

## Phone alerts

1. Install the free **ntfy** app.
2. Pick a long random name: set `KAL_NTFY_TOPIC`, or run `python -m kal setup`.
   (The server install makes one for you.) Anyone who knows the name can read your alerts.
3. Subscribe to that name in the app.
4. Test: `python -m kal summary --send` (on the server: `sudo kal summary --send`).

docs/DEPLOY.md says what each alert means and what to do.

## Commands

| Command | What it does |
|---|---|
| `python -m kal app` | Dashboard + scheduler. The normal way to run it. |
| `python -m kal daemon` | Scheduler only, no dashboard. |
| `python -m kal run` | One trading run now. |
| `python -m kal scan` | Dry run. Shows what it would buy. Never trades. |
| `python -m kal research` | Look for new edges now. `--quick` for a short one. |
| `python -m kal selftest` | Check Kalshi's API. `--order-probe` also tests orders. |
| `python -m kal status` | A few lines on how it's doing. |
| `python -m kal report` | The full report. `--push` puts it on GitHub. |
| `python -m kal health` | Disk, clock, timer, backups, last runs, last error. |
| `python -m kal logs` | The last 50 log lines (`-n 200` for more). |
| `python -m kal backup` | Back up the books now. |
| `python -m kal restore` | List backups. `restore 2026-10-24 --yes` puts one back. |
| `python -m kal summary` | Yesterday's phone summary. `--send` tests alerts. |
| `python -m kal setup` | Guided setup: five plain questions (key, demo or real, budget, alerts). |
| `python -m kal on --budget 300` | Turn it on. |
| `python -m kal off` | Turn it off. |
| `python -m kal set env demo` | Change a setting (here: fake money). |

## Good to know

- Kalshi pays interest on balances of **$250 or more**.
- Arbitrage chances are rare and small: cents to a few dollars each.
- Bets (`favorites`, `mentions`) can lose, even after they pass every test.
- Never commit your key (`kalshi.pem`), `.env` files, or tokens.

## For developers

- Map of the code: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Rules: [CLAUDE.md](CLAUDE.md).
- Tests: `python -m unittest discover -s tests -t .`
- CI runs the tests on every push. `market-check` runs `selftest`, `scan` and a
  quick research against the real Kalshi API every day (no keys, no orders).
- `install-check` runs the real `deploy/install.sh` on a fresh Ubuntu 24.04 machine,
  then checks the service, the dashboard, the `kal` command, alerts and the watchdog.
- The old files at the top (`arb.py`, `bot.py`, `backtest.py`, `kalshi.py`) are
  the first prototype. The `kal` package replaces them.
