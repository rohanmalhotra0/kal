# Journal

Newest first. Each 2-hour check-in adds one entry: what it checked, what it found, links.

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
