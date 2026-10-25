# kal architecture

```
kal/
  client.py       Kalshi API client + field helpers (price, num, fee, filled, resting, ts)
  config.py       config.json: the only source of truth. validate, trip()/turn_off()
  db.py           SQLite, one file per exchange (kal.db = real, kal-demo.db = demo)
  ledger.py       the bot's own books: capital = budget + realized P&L, open cost
  risk.py         run_lock(), Limits (every money cap), daily_loss()
  sizing.py       Kelly: cautious p (Beta lower bound), uncertainty shrink, open-bets cap,
                  drawdown throttle
  paper.py        paper fills, settlement, stats
  engine.py       run_once(): reconcile -> settle -> safety -> strategies -> Limits -> trade
  strategies/     one module per strategy, each defines STRATEGY = SomeStrategy()
  research/       backtests + daily discovery + promotion; writes $KAL_HOME/research/*.json
  dashboard/      Flask app (the ON switch)
  scheduler.py    background jobs: trade every few minutes, research daily, report hourly
  selftest.py     checks the real API shape; optional zero-risk order probe
  report.py       status report; pushes to the repo's `status` branch
  __main__.py     CLI: app, daemon, run, scan, research, selftest, status, report, on, off, set
state/            $KAL_HOME (gitignored): config.json, kal.db, research/, cache/, kalshi.pem
tests/            unittest; tests/fakes.py has FakeKalshi, tests/helpers.py has TempHome
```

## Contracts between pieces

- A strategy subclasses `kal.strategies.base.Strategy` and implements
  `scan(ctx) -> [Signal]`, optionally `recheck(ctx, sig) -> Signal|None` (re-price
  right before live execution) and `proven(ctx) -> (ok, why)` (live trading is
  refused unless ok). Arbitrage defaults to ok, but may override it to stay on
  paper until verified (bracket_arb does).
- A `Signal` is a list of `Leg(ticker, side, price, count)` plus `payout_min`
  (worst-case payout; > cost means locked-in profit), `exp_profit`, `meta` (stored
  with the signal; `meta["paper_only"]` keeps one signal on paper), and
  `prefix_payouts` (worst-case payout if only the first k legs fill; default $0).
- Legs execute in order with immediate-or-cancel buys. If the first leg fills partly,
  later legs shrink to match. If a later leg comes up short, the bot trips.
- Strategies never place orders or touch the db. The engine does.
- Every money limit is enforced in `risk.Limits.refuse` and `ledger`, never only in a
  strategy: budget left, max_per_trade, max_per_arb, max_per_event (across runs),
  max_exposure (directional bets), and the half-built basket loss (`partial_loss`).
- Research writes JSON to `$KAL_HOME/research/` (`edges.json` is a list of edges;
  `words.json` holds mention-word history). The engine loads each file into
  `ctx.research[<file stem>]` every run.

## Failing closed

- Unknown strategy mode -> never traded. Invalid config -> no run.
- An order whose reply was lost is marked `unknown`; the next run looks it up on
  Kalshi (by client_order_id) before trading. Can't tell yet -> no live trading.
- An order left resting -> cancelled, and the bot trips.
- OFF is re-checked before every live order, and works even with a bad config file.

## Modes

Each strategy is `off`, `paper` or `live`. `live` drops to `paper` when there's no
API key, the budget is $0, or `proven()` is false. The master switch `enabled` and
the `disabled_reason` latch sit above everything. Reset clears the latch but leaves
the bot off until someone presses ON.
