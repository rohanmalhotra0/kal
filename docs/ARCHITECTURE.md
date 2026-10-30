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
  scheduler.py    background jobs: trade every few minutes, research daily (in its own
                  process), report hourly
  selftest.py     checks the real API shape; optional zero-risk order probe
  report.py       status report; pushes to the repo's `status` branch
  __main__.py     CLI: app, daemon, run, scan, research, selftest, status, report, on, off, set
state/            $KAL_HOME (gitignored): config.json, kal.db, research/, cache/, kalshi.pem
                  (demo: kal-demo.db, research-demo/, cache-demo/)
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
- Every strategy reads one list per run: `ctx.client.events(status="open",
  with_nested_markets="true")`, spelled exactly so (a test checks). All of Kalshi's open
  events are over 700 MB in Python, so `engine.RunCache` keeps only events with a market
  due within `horizon_days` (max_arb_days + 1, or a research result's hours before close),
  plus any the bot holds or can't read, and drops the fields in `engine.UNREAD`. A trade
  past max_arb_days is never offered (`risk.too_late`), so `kal scan` no longer lists them.
- Research writes JSON to `$KAL_HOME/research/` (`edges.json` is a list of edges;
  `words.json` holds mention-word history). The engine loads each file into
  `ctx.research[<file stem>]` every run. Demo uses `research-demo/` and `cache-demo/`,
  so practice never shapes real trades. Each idea's status, looks and rank are also in
  the db (`research_memory`), which is backed up.

## Facts from the real Kalshi API (checked by tools/api_probe.py)

- Prices come as dollar strings: `yes_bid_dollars: "0.9300"`. Historical candles use
  `close: "0.8000"` (dollars, no suffix). Counts come as `_fp` strings. `client.price`
  reads every shape; `client.price_strict` is for the self-test.
- Settled markets say `status: "finalized"` (also `settled`). `determined` is not final.
- Markets settled before the historical cutoff live under `/historical/...`
  (`client.candles` falls back there on 404).
- One event can hold different questions (both teams' spreads, every player's props),
  so ladders need matching wording, ticker letters and `custom_strike`.
- Some real ladders close in 2040. Locked-in trades must settle within `max_arb_days`
  and return at least `min_arb_apr` a year.
- Price ticks are 1c, 0.1c, or 0.1c near the ends (`price_ranges`). Orders are priced off
  the opposite side's real bid, so they're always on a tick.
- Combo markets (`KXMVE...`) are skipped everywhere.
- **The bot's orders carry a mark.** Every client_order_id is a UUID starting with
  `client.BOT_MARK`. Kalshi keeps orders for good (live, then `/historical/orders`), so
  `ledger.recover` puts back any marked order that bought something and isn't in the
  books. Books rolled back to an older copy get its full result. New books (a lost disk)
  can't see what came before the mark (Oct 30, 2026), so there their gains only make up
  their losses, never more (`ledger.made_up`): a past basket nets out instead of reading
  as a loss of one leg. Each live run looks back to its last check. Every order put back
  (here or by `catch_up`) gets a stand-in signal with its event from Kalshi's market
  data, so `max_per_event` counts it. A ticker can't say its event: player-prop and
  negative-threshold tickers carry extra hyphens.
- **API keys are Ed25519 (Kalshi's default since 2026) or RSA.** Both sign timestamp +
  method + path (no query), base64: Ed25519 signs the text itself, RSA uses RSA-PSS with
  SHA-256 (`client.sign`). The PEM header doesn't say which; `client.parse_key` refuses
  any other kind.
- **Orders use V2 only.** `POST /portfolio/orders` answers 410 `deprecated_v1_order_endpoint`.
  Create: `POST /portfolio/events/orders` on one YES book (`bid`/`ask`): buy YES at p =
  bid p, buy NO at p = ask 1-p; count/price are strings. Cancel:
  `DELETE /portfolio/events/orders/{id}`. Reads stay on `/portfolio/orders`, `/positions`,
  `/fills`, `/settlements`, `/balance` (all checked to exist).
- The demo (fake money) exchange is `https://demo-api.kalshi.co`.
- The public API sits behind CloudFront. `GET /markets` and `/series` are cached up to
  15 s (`Cache-Control: max-age=15`); a cached reply keeps its old `Date` and says how old
  in `Age`. Order books and `/events` are never cached, so prices the bot trades on are
  fresh. The clock check adds `Age` back.

## Failing closed

- Unknown strategy mode -> never traded. Invalid config -> no run.
- An order whose reply was lost is marked `unknown`; the next run looks it up on
  Kalshi (by client_order_id) before trading. Can't tell yet -> no live trading.
- An order left resting -> cancelled, and the bot trips.
- No trade (paper or real) unless the market's series fee, and any event fee change
  Kalshi has scheduled, is a known quadratic type with multiplier 0-1 (`kal/fees.py`).
  Only then is the bot's fee estimate an upper bound. Unreadable fees -> skip.
- Bets (never locked-in trades) are skipped when the last hour of public trades on that
  market went 70%+ the other way, or one big trade (5x the median, $50+) just did
  (`kal/flow.py`). Paper too, so practice matches real. Unreadable trades -> skip.
- No real money until Kalshi accepts two $0.01 test orders (`kal/gate.py`), one per order
  shape on its single YES book, both immediate-or-cancel so they can't fill: buy 1 YES (a
  bid) where the cheapest seller asks $0.10+, and buy 1 NO (an ask at $0.99, the shape
  baskets use) where the best YES buyer bids $0.90 or less. Needed again for a new key
  or exchange, or after Kalshi refuses any order. Until it passes, live strategies trade
  on paper; a failed try waits an hour. If either fills or rests -> trip.
- OFF is re-checked before every live order, and works even with a bad config file.
- Kalshi closed (maintenance) -> run skipped. Network errors, 5xx and 429 never count
  toward the error trip (nothing can be bought then); 6+ hours of that sends one alert.
- Waiting for a lost order to show up isn't counted as an error: a network blip
  mustn't trip the bot. If cancelling a resting order fails, the trip says so, and the
  order is looked up again after Reset. A lookup that finds an order still resting
  cancels it and trips too, so its final count is read after the next Reset.
- No clear reply on a basket's 2nd+ leg -> trip at once. Each live run also checks for
  baskets an earlier run left part-bought (a lost reply, or a crash between legs) and
  trips once for each.
- Each trade run (and each research run or dashboard backtest) keeps one exchange's books
  and files from start to end (`db.pinned`), even if someone switches env part way.
- Kalshi shows more contracts than the books explain on a market the bot traded -> trip.
  Fewer, on a market still trading (someone sold them outside the bot) -> those are
  written off as lost, so the books never earn on contracts they don't hold.
- Account lists (positions, orders, fills) missing their key -> error, not "empty".
- After `kal restore`, the next live run adds the buys Kalshi shows since the backup's
  day before it trades (`ledger.catch_up`). It puts back the bot's own marked orders
  first (`ledger.recover`), so a sale outside the bot is never read as the bot's buy of
  the other side, whatever the fills' times say. A fill it can't read -> trip. Then any
  Kalshi position the books still don't know -> one trip (the owner's own? Reset).
  The marker that asks for this is written into a copy of the backup, and only that copy
  is swapped in, so restored books never come back without it.
- The daily stop reads equity: open positions at their bid, or at what a known result
  will pay. A closed market shows no bid until it settles, so it keeps the last mark seen
  (kv `last_marks`): at cost, a winner's gain left at the close and came back at the
  settlement, a false stop, or the next day a real loss partly hidden.
- The daily stop, the bets cap and bet sizes scale with min(capital, account cash +
  open cost), so a budget typed bigger than the account can't loosen them.
- Baskets skip events with a market closing in under 5 minutes.
- Locked-in trades: binary markets only, return under 25% (more is a misread), a known
  settle date within `max_arb_days`, and at least `min_arb_apr` a year.

## Modes

Each strategy is `off`, `paper` or `live`. `live` drops to `paper` when there's no
API key, the budget is $0, or `proven()` is false. The master switch `enabled` and
the `disabled_reason` latch sit above everything. Reset clears the latch but leaves
the bot off until someone presses ON.
