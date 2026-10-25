# Journal

Newest first. Each 2-hour check-in adds one entry: what it checked, what it found, links.

## 2026-10-25 07:25 UTC — round 3 done: 5 PRs merged, 1 open

- Merged: https://github.com/rohanmalhotra0/kal/pull/1 (fee guard),
  https://github.com/rohanmalhotra0/kal/pull/2 (longshot fade, paper),
  https://github.com/rohanmalhotra0/kal/pull/3 (money-path holes + chaos test),
  https://github.com/rohanmalhotra0/kal/pull/4 (safe self-update, watchdog, SSH keys),
  https://github.com/rohanmalhotra0/kal/pull/5 (setup and CLI traps, report health).
  CI and market-check (real Kalshi API) green on main.
- Open: https://github.com/rohanmalhotra0/kal/pull/6, research that can't fool itself:
  hourly series count once per day (luck had passed the real-money test ~8x too often),
  demo keeps its own research files, no early-close look-ahead in backtests, fees
  rounded the real way, retired ideas remembered across rebuilds, research in its own
  process (memory), retried after a restart. All stricter; no defaults changed.
- Honest state: nothing has traded for real yet (no server). No strategy has a proven
  edge after fees; only locked-in arbitrage is live by default. Expect break-even-ish
  until the paper results say otherwise.

## 2026-10-25 06:50 UTC — check-in: three reviewers tried to break it; fixes

- Checked: CI and market-check green on main. No server yet, so no status report.
- Three reviewers (money, operations, owner) attacked the code and proved 32 problems.
  None lost money yet (nothing trades for real), but several could have. Fixes so far:
  - **PR https://github.com/rohanmalhotra0/kal/pull/3:** half-built baskets after a lost
    reply now stop the bot; one network blip no longer trips it; contracts sold outside
    the bot are written off; a restore re-adds trades made after the backup; a budget
    typo can't loosen the daily stop. New chaos test: hundreds of runs of random lost
    replies, partial fills and failed cancels, with a careless owner pressing Reset: the
    bot never had more than its budget out, and its books matched to the cent.
  - **PR https://github.com/rohanmalhotra0/kal/pull/4:** the daily auto-update (still off)
    tests new code as a locked-down user, takes code only from merged pull requests, and
    rolls back if the bot isn't healthy after. New watchdog alerts if the bot dies.
    SSH key login; alert names with no look-alike letters.
  - **PR https://github.com/rohanmalhotra0/kal/pull/2** (longshot): its research used
    489 MB on a big cache, over the server's 400 MB cap. Now 95 MB.
- Still running: research fixes (luck on hourly series could pass the real-money test;
  demo practice wrote prod's research files) and setup fixes (re-running setup on a
  running bot, `kal set` typos). Two more PRs next.

## 2026-10-25 04:50 UTC — check-in: fee guard, deep backtest done

- Checked: CI and market-check green on main (the clock fix worked). No server yet, so
  no status report. The 02:47 check-in was missed: the session hit its usage limit.
- **Fix (PR https://github.com/rohanmalhotra0/kal/pull/1):** trade only where the fee
  math is an upper bound. Correction to the 01:35 entry: the fee estimate is safe only for
  Kalshi's quadratic fee types with multiplier 0-1 (Kalshi has used a flat fee before).
  Today every series qualifies, so no trade is lost.
- **Deep backtest v3 (26,676 real markets, 5,028 events, sports, economics, weather):**
  no price range has a real edge for a taker 6 h before close. Best: 95c+ with spreads of
  2c or less, +0.4% to +1% (worst case -2% to -3%). YES under 10c loses 35-95%. Favorites
  and longshot fading stay on paper, as designed.

## 2026-10-25 02:20 UTC — real-API facts and safety fixes

- **Cancelled games really happen:** a called-off 3-way eSoccer match settled every outcome
  at $0.33. Backtests now count these at what they paid (they had been left out, which
  made favorites look better than they are). The dashboard shows them as "Split payout".
- **Locked-in trades skip "last traded price" rules:** about 1% of markets in exclusive
  events (death clauses on "leader leaves office") can settle at any price.
- **Kalshi's market list is cached up to 15 s** (CloudFront). Order books are never
  cached, so trades use fresh prices. The clock check had misread a cached reply as a
  13 s clock error; fixed.
- **New: skip a bet when the last hour of trades went 70%+ the other way** (or one big
  trade did). Research says informed traders take liquidity on Kalshi.
- Research swarm so far: plain longshot fading is about break-even after fees for us;
  favorites close to the end and the NO side where people over-bet YES are the best
  leads; arbitrage is rare. Status reports only go to a private repo now.

## 2026-10-25 01:35 UTC — test order before real money

- **No real money until Kalshi accepts one $0.01 test order** for this key and exchange
  (buy 1 YES where sellers ask $0.10+, so it can't fill). A new key, a new exchange, or
  any refused order means a new test. Until then, live strategies trade on paper.
- Real API (9,000 settled markets): every one was a plain yes/no binary. No voided or
  scalar settlements seen; the bot still books them by payout, or stops if unreadable.
- Fees: 15,197 series use the normal taker fee; 32 pay half or none. The bot always
  assumes the full fee, so it never overrates a trade. Order books come as
  `orderbook_fp` with dollar strings; the bot reads both shapes.

## 2026-10-25 01:20 UTC — first deep backtest (real history)

- 1,677 priced markets, 332 events, 19 categories (random series; many had no data).
- After fees: YES under 10c lost 88-100%. YES at 95c+ lost ~13%. NO at 80-95c with a
  tight spread (<= 2c) made +7% to +12%. NO at 95c+ made ~+2%.
- Nothing is significant yet (z = 3.7 for 480 tries). Lead to chase: **NO at 80-95c,
  tight spreads** (fits the favorite-longshot and YES-overbetting research).
- Running v2 on the busiest active series with more markets.

## 2026-10-25 01:10 UTC — order endpoint moved

- **Kalshi retired the V1 order endpoint** (`POST /portfolio/orders` -> 410). Every real
  order would have failed. Switched to V2 (`/portfolio/events/orders`, bid/ask on the YES
  book). All other endpoints the bot uses checked: they exist.
- Kalshi maintenance and network errors no longer trip the bot; a 6-hour outage alerts.
- New safety check: Kalshi positions must match the bot's books, or it trips.
- Demo exchange address confirmed. Price ticks confirmed (orders always land on a tick).

## 2026-10-25 01:00 UTC — first real-data checks

- Built the full system: 6 strategies, research, dashboard, safety limits.
  21 review findings fixed.
- **Real Kalshi data caught a bad bug:** ladder arbitrage saw 50 "locked-in" trades
  (e.g. $10.70 to win $500). They were fake. Kalshi puts different teams/players in
  one event. Fixed: rungs must have the same wording, ticker letters and
  custom_strike. Any "locked-in" return over 25% is now refused.
- Real API facts (api-probe): historical candles send `"0.8000"` strings; settled
  markets say `finalized`; `tickers=` works for normal markets; some real ladders
  close in 2040. Locked-in trades must now settle within 30 days and pay 20%+ a year.
- Research now tests the busiest active series first.
- Scan today: 0 opportunities in every strategy. Expected: real arbitrage is rare.
- Running: deep-research workflow (calibration by price range and category).
