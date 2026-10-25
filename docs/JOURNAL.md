# Journal

Newest first. Each 2-hour check-in adds one entry: what it checked, what it found, links.

## 2026-10-25 16:50 UTC — check-in: all green; interest can't stretch the $300 rule

- Checked: CI and market-check green on main 0ec62f8 (with #13, #14, #15). Self-test
  passes. Real-data scan: 0 trades in all six strategies, nothing suspicious. No server
  yet, so no status report.
- Merged since 16:00:
  - #13: text says two test orders;
  - #14: basket_arb sees game and weather events, paper only, as $10 bets, at most 25%
    of paper money; the exact wordings cover 2,270 of 2,313 such events;
  - #15: LIP research, verdict "not yet".
- #14 caused a base.py conflict in #9, #10 and #11. It was additive, resolved by merge
  commits; all four open PRs are green and mergeable.
- **Idea #12 (interest on cash):** the books are built from the bot's own fills only, so
  interest or a deposit never raises what it may spend. A new test pins it (it fails if the
  books ever read the account balance). Kept as the owner's money: ~$10 a year isn't worth a
  money-path change. https://github.com/rohanmalhotra0/kal/pull/16
- Running: Polymarket study (agent), 2 arbitrage watches (to ~20:15), taker reruns
  (~17:55), #9's before/after check (21:05).

## 2026-10-25 16:00 UTC — where arbitrage could grow, measured on 14,640 open events

- **Review of #12** (independent agent) found 2 real gaps. Both fixed with tests:
  - a live edge with nothing busy to re-test it kept betting on its old numbers;
  - the coverage guard couldn't see the busy filter.
  Real data on the branch: 336 busy markets in the guard's 3 series (floor 25).
  https://github.com/rohanmalhotra0/kal/pull/12#issuecomment-6399822048
- **New #13:** the gate sends two $0.01 test orders, but 4 places said one. Text fixed; a test ties them together.
  https://github.com/rohanmalhotra0/kal/pull/13
- **Arbitrage shapes** (probe, no orders):
  - Fees: only ~43 open events (0.3%) charge less than the bot assumes. Using each
    series' real fee isn't worth a money-path change.
  - "Before <date>" ladders (AGI by 2030 vs 2031, ski openings, Trump runs) exist, but
    none was mispriced. Every apparent gap was a false pair: different cities or
    people, or "as of" dates (a state, not a deadline).
  - **2,257 one-winner events (40%) are skipped by basket_arb** for one standard clause:
    "if the game is cancelled or not started within 48 hours, the market resolves to a
    fair price". That is nearly every sports game and weather bracket. 5 had YES bids
    adding to $1.01-1.02, too little after fees. A watch (run 40209367984) counts
    real after-fee baskets there until ~20:10 UTC. A cancelled game settles at a price
    Kalshi picks, so any change stays paper-only unless the owner says otherwise.
- Running: arbitrage watch (live rules, to ~20:15), taker reruns at 1/6/24 h (~17:55),
  LIP research (agent), #9's before/after check at 21:05.

## 2026-10-25 14:50 UTC — check-in: weather test says the market knows more

- Checked: CI green on main. No server yet, so no status report.
- **Weather (idea #7), with the supervisor's fixes: rejected.** Kalshi's prices beat the
  National Weather Service's NBM forecast in all 7 cities (Brier 0.095 vs 0.121). The
  forecast is overconfident in the likeliest bracket. Every trading rule lost (YES about
  -50%, NO -11% to -17%). The strike mapping checks out: every day's brackets cover
  every degree once. https://github.com/rohanmalhotra0/kal/pull/11#issuecomment-6399095695
- **Maker (idea #10), rerun with each series' real fee:** still no lead.
  https://github.com/rohanmalhotra0/kal/pull/10#issuecomment-6399096100
- Honest state: three real-data tests today (taker, maker, weather), no edge after fees.
  The bot stays safe and mostly idle until one shows up. #9's check is at 21:05.

## 2026-10-25 12:50 UTC — check-in: two real-data answers, both "no edge"

- Checked: CI green on main, market-check green. No server yet, so no status report.
- **Research with #9's fix priced 24,878 real markets** (main prices 904). No price
  range has an edge after fees in any category. Best: NO at 95c+ with spreads of 2c or
  less, +0.2% (worst case -2.7%). Idea #5 (NO where YES is over-bet): rejected for a
  taker. https://github.com/rohanmalhotra0/kal/pull/9#issuecomment-6396866572
- **Maker test: no lead, even in a maker's best case** (4,103 markets). Best: NO bids
  at 90-95c, +2.3% (worst -20%). Not worth building resting orders now.
  https://github.com/rohanmalhotra0/kal/pull/10#issuecomment-6397845896
- Crypto prices 0 markets on purpose: its markets open 15-60 minutes before they
  close, so a 6-hour test can't apply.
- Honest state: no strategy beats fees yet. Turned on, the bot will mostly wait and
  take only locked-in arbitrage, which is rare. The weather test (#11) is still running.

## 2026-10-25 10:47 UTC — check-in: two new backtests, each attacked by a reviewer

- Checked: CI and market-check green on main. No server yet, so no status report.
- **Maker test (PR https://github.com/rohanmalhotra0/kal/pull/10):** do resting bids keep
  the edge that buying at the ask gives away? A reviewer found ways it could fake an edge
  (a big bid "filled" by one small trade; related series counted as separate tries).
  Fixed: one-contract bids, a maker's best case, so a range that loses there loses at
  any size. Real-data run in progress.
- **Weather test (PR https://github.com/rohanmalhotra0/kal/pull/11):** do the National
  Weather Service's NBM forecasts beat Kalshi's daily-high prices in 7 cities? A reviewer
  found two leaks; both are fixed. A forecast run that started after the decision time
  could be used. The usual volume filter dropped quiet markets, mostly losers, using
  volume known only after settling. Real-data run in progress.
- That volume filter is in the shared backtest too (main and #9). Fix after #9 merges.
- Next: results posted on #10 and #11 by 13:00. #9's before/after check at 21:05.
  No new PRs until #9 merges.

## 2026-10-25 08:50 UTC — check-in: research fix in review (PR #9)

- Checked: no server yet, so no status report. CI green on main (1ad9202). Real-market
  check green (eccb8b3): self-test passed, 0 locked-in trades found, research 0 new edges.
- Found: main's research is blind. Same settings on real data: 0 priced markets in all
  12 categories (https://github.com/rohanmalhotra0/kal/actions/runs/40177134669).
  Cause: #6's early-close rule; Kalshi marks every market as able to close early.
- Fix: https://github.com/rohanmalhotra0/kal/pull/9. An independent review found a leak
  in its first version (which markets got tested depended on when research ran); now
  each market is timed from its own listed end. It also fixes a favorites cap bug that
  is on main: one API error could add a bet past the day's 10% cap. 585 tests pass.
- Waiting on: the coverage table (before #6 / after #6 / with #9), then a before/after
  check of 6,851 markets recorded while open, after tonight's games. Then it merges.
- Follow-up: markets that close after tonight get their before/after check later.

## 2026-10-25 07:55 UTC — all 8 PRs merged; research now tests almost nothing

- Merged: https://github.com/rohanmalhotra0/kal/pull/6 (honest research),
  https://github.com/rohanmalhotra0/kal/pull/7 (tests the NO order shape before real money),
  https://github.com/rohanmalhotra0/kal/pull/8 (research keeps one exchange per run).
  CI and market-check green on main (eccb8b3).
- Problem (from PR #6): research only tests events that have a market that can't close
  early. On real Kalshi data almost none do. Deep research priced 26,676 markets before
  PR #6 and 904 after. 10 of 12 categories went to zero, including sports, weather,
  economics, crypto and financials. It's safe (no fake edges), but no new idea can pass.
  Before: https://github.com/rohanmalhotra0/kal/actions/runs/40151203251
  After: https://github.com/rohanmalhotra0/kal/actions/runs/40172569277
- A plain swap won't work: expected_expiration_time can sit at the contract's end
  (Oscars markets closed in Jan and Mar; the field says Dec 31).
- Next check-in: a real-data probe (side branch probe/early-close) checks which time
  field is set before the event. Then fix the anchor in a PR, with tests.

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
