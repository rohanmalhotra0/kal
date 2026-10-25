# Journal

Newest first. Each 2-hour check-in adds one entry: what it checked, what it found, links.

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
