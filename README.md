# kal

A bot that trades on [Kalshi](https://kalshi.com), a US prediction market.
It runs on a small server, day and night.

**Profits are not guaranteed.** Only give it money you can afford to lose.

## What it does

- **Every 5 minutes:** looks for trades. Makes the ones its rules allow.
- **Every day:** tests new ideas on past markets (research).
- **Every hour:** writes a short report to the `status` branch on GitHub.
- **Dashboard:** turn it on or off, set the budget, see what it did.

## Strategies, safest first

| Strategy | What it does | Risk | Starts as |
|---|---|---|---|
| `basket_arb` | Only one outcome can win, so NO on several outcomes pays a known amount. Buys when that costs less. | Very low. Profit is locked in. | Live |
| `ladder_arb` | "Above 60" can't win unless "above 50" wins. Buys when that pair costs less than $1. | Very low. Profit is locked in. | Live |
| `bracket_arb` | Ranges that cover every result: one must win. Buys all when they cost less than $1. | Low, once checked. | Paper, until a person checks it |
| `favorites` | Buys price ranges that won more often than their price in past markets. | Medium. A bet, not a sure thing. | Paper |
| `mentions` | "Will X say Y?" markets. Uses how often each word was said before. | Higher. | Paper |

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
- **Zero-risk order test.** `selftest --order-probe` sends one $0.01 bid that can't fill.
- **Dashboard is never open to the internet.** You reach it with an SSH tunnel.

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

See **[docs/DEPLOY.md](docs/DEPLOY.md)**. About $5 to $6 a month.

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
| `python -m kal on --budget 300` | Turn it on. |
| `python -m kal off` | Turn it off. |

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
- The old files at the top (`arb.py`, `bot.py`, `backtest.py`, `kalshi.py`) are
  the first prototype. The `kal` package replaces them.
