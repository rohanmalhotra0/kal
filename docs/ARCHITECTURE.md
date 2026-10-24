# kal architecture

```
kal/
  client.py       Kalshi API client + field helpers (price, num, fee, filled, ts, settled_result)
  config.py       config.json (+ env overrides), validate, trip()/clear_trip() safety latch
  db.py           SQLite: runs, signals, orders, paper, equity, settlements, backtests, kv
  risk.py         run_lock(), Limits (budget / per-trade / per-arb / per-event), daily_loss()
  sizing.py       Kelly sizing
  paper.py        paper fills + settlement + stats
  engine.py       run_once(): safety -> each strategy scans -> Limits -> live or paper
  strategies/     one module per strategy, each defines STRATEGY = SomeStrategy()
  research/       backtests + daily discovery; writes $KAL_HOME/research/<name>.json
  dashboard/      Flask app + scheduler (the "turn on" button)
  __main__.py     CLI: python -m kal {app,run,research,backtest,selftest,status}
state/            $KAL_HOME (gitignored): config.json, kal.db, research/, kalshi.pem
tests/            unittest; tests/fakes.py has FakeKalshi, tests/helpers.py has TempHome
```

## Contracts between pieces

- A strategy subclasses `kal.strategies.base.Strategy` and implements
  `scan(ctx) -> [Signal]`, optionally `recheck(ctx, sig) -> Signal|None` (re-price
  right before live execution) and `proven(ctx) -> (ok, why)` (live trading is
  refused unless ok; arbitrage is always ok).
- A `Signal` is a list of `Leg(ticker, side, price, count)` plus `payout_min`
  (worst-case payout; > cost means locked-in profit) and `exp_profit`.
- Legs execute in order with immediate-or-cancel buys. Multi-leg signals stop at the
  first leg that doesn't fully fill.
- Strategies never place orders or touch the db. The engine does.
- Research writes JSON files to `$KAL_HOME/research/`. The engine loads them all into
  `ctx.research[<file stem>]` each run.
- Every money limit is enforced in `risk.Limits.refuse`, never only in a strategy.

## Modes

Each strategy is `off`, `paper` or `live` in config. `live` silently drops to
`paper` when there's no API key or `proven()` is false. The master switch
`enabled` and the `disabled_reason` latch sit above everything.
