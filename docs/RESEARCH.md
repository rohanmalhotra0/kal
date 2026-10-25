# Research notes

The 2-hourly check-ins work through this list. Take the top idea marked
**untested**. Research it. Build it paper-only, with tests, in a PR. Then set its
status: **testing** (add the PR link), **built**, or **rejected** (say why in one line).

## How the bot sizes bets

**Arbitrage:** profit is locked in. Only `max_per_arb` and the budget cap it.

**Everything else:** Kelly, then cut down a lot. Code: `kal/sizing.py`.

- Kelly: bet `(p - c) / (1 - c)` of the bankroll. `p` = win chance. `c` = price + fee.
- We bet 1/4 of that (`kelly_fraction` 0.25). Why:
  - Full Kelly halves your money at some point half the time. Quarter Kelly: under 1%.
    It keeps 44% of full Kelly's growth (Thorp; MacLean, Thorp & Ziemba).
  - If we think the edge is 2x what it is, full Kelly earns nothing. Quarter Kelly
    becomes half Kelly, which still grows.
- `p` is a cautious guess, never the average.
  - `favorites`: price x (1 + the backtest's worst-case ROI).
  - `shrunk_p(wins, trials)`: the low end (5%) of what the record allows.
    10 of 10 gives 0.76. 100 of 100 gives 0.97. A short record bets less.
  - Or `uncertainty_shrink`: bet `1 - (se / edge)^2` of Kelly (Baker & McHale).
    Edge under 1 standard error: no bet. Use this **or** `shrunk_p`, not both.
- Bets open at once share one bankroll. `scale_to_cap` shrinks all by one factor so
  the total at risk stays under a cap (Whitrow).
- After losses, `drawdown_multiplier` cuts size: full at the peak, down in a straight
  line to 1/4 size at a 20% drop (after Grossman & Zhou).
- **Not wired in yet:** the drawdown cut and the open-bets cap need the engine to pass
  the equity peak and the money at risk. Until then they do nothing.

## Ideas, best first

**1. Basket arbitrage** - built, live
- What: in a one-winner event, buy NO on several outcomes when YES bids add to over $1.
- Evidence: strong (math). Saguillo et al. found ~$40M taken from gaps like this and 6 on Polymarket.
- Risk: low. A basket can half-fill. The engine stops at the first leg that misses.
- Test: count signals vs full fills in live logs. Many half fills? Buy the thinnest leg first.

**2. Ladder arbitrage** - built, live
- What: if "above 70" wins, "above 60" wins too. Buy YES broad + NO narrow when under $1.
- Evidence: strong (math). Risk: low. Odd-looking events are skipped.
- Test: did every settled ladder trade pay at least its cost?

**3. Favorites (favorite-longshot bias)** - built, paper until promoted
- What: buy in price ranges that won more often than the price said.
- Evidence: strong. Bürgi, Deng & Whelan (300,000+ Kalshi contracts): under 10c loses over 60%. Over 50c earns a small plus. Worse for takers. The gap is shrinking.
- Risk: medium. The edge is a few cents. One upset wipes out many wins.
- Test: built in (backtest, then 30 paper trades). Next: try other hours before close.

**4. Bracket arbitrage** - built, paper
- What: brackets cover every value, so one must win. Buy YES on all when under $1.
- Evidence: strong, if there's no gap. Risk: high if there is one. All is lost.
- Test: a person checks 20+ settled events. One bracket won each time? Set "verified".

**5. NO on over-bet YES, by market type** - untested
- What: people over-bet YES on "will X happen?" markets that mostly end NO. Buy NO.
- Evidence: medium. Bartlett & O'Hara (41.6M Kalshi trades) found it, mostly in single-name markets (one person or company). A summary says YES was bought 61% of the time there, but won 32%.
- Risk: medium-high. Those same markets have the most insiders.
- Test: reuse `backtest.calibration`, split by market type and side. Need a lower bound above 0 after fees.

**6. Related markets that must agree** - untested
- What: linked prices must fit. E.g. "Fed cuts in Dec" vs "rate below X in Jan".
- Evidence: medium. Saguillo et al. found this on Polymarket. Finding the links is the hard part.
- Risk: medium-high. If the rules differ (source, time, rounding), both legs can lose.
- Test: pick 3 pairs where the rules text proves the link. Log gaps on paper for 2 weeks.

**7. Scheduled data releases (weather, CPI, jobs)** - untested
- What: compare the price with a public forecast before the report comes out.
- Evidence: medium-low. A Fed study found Kalshi macro prices about as good as expert surveys. Weather "edges" online come from sellers, not studies.
- Risk: medium. Model error, thin markets, news moves first.
- Test: daily high temps (settle on the NWS Daily Climate Report). Save forecast + price 6h before close for 60+ days. Compare with results.

**8. Mention words** - built, paper
- What: YES on words said more often than the price says. NO on less.
- Evidence: weak-medium (our own backtest only). Risk: high. Capped at $5 a trade.
- Test: needs 30 paper trades. Try `shrunk_p` with the series' base rate as the prior.

**9. One-sided order flow guard** - untested (a filter, not a strategy)
- What: skip a trade when recent trades all go one way. Someone may know something.
- Evidence: medium. Bartlett & O'Hara: one-sided flow predicts maker losses in single-name markets.
- Risk: low. It only removes trades. Test: did paper trades after one-sided flow do worse?

**10. Maker quotes + Liquidity Incentive Program** - untested, needs engine work
- What: rest orders instead of taking. Maker fee is 1/4 of taker (0.0175 vs 0.07). Kalshi's LIP pays for resting orders near the best price, even unfilled.
- Evidence: medium. Makers beat takers (Bürgi et al.). In single-name markets, makers earn ~2x per contract vs broad ones (Bartlett & O'Hara).
- Risk: high. Better-informed traders pick off resting orders. The engine can't cancel orders. LIP terms change often: check they still apply.
- Test: paper only. Log would-be quotes. Count a fill when trades go through our price. Score at settlement, plus our LIP share.

**11. Other venues as a fair-price guide** - untested
- What: use Polymarket or sportsbook prices to judge Kalshi's. Trade only Kalshi.
- Evidence: weak-medium. A 2024 election study found Polymarket led Kalshi.
- Risk: medium. Venues can settle the same event differently.
- Test: log both prices. Which was closer to the result?

**12. Interest on positions** - untested (accounting, not a strategy)
- What: Kalshi pays variable interest (seen 3.25-4%) on cash and open positions. Monthly, $250+ only.
- Evidence: strong (Kalshi help page). Small: ~$10 a year on $300. Risk: low.
- Test: find it in account history. Ask for the ledger to count it. Never loosen risk rules for it.

## Sources

Numbers come from abstracts and summaries. Check the paper before relying on one.

- Bürgi, Deng & Whelan, Makers and Takers: https://www.ucd.ie/economics/t4media/WP2025_19.pdf
- Bartlett & O'Hara, Adverse Selection ... Kalshi: https://law.stanford.edu/publications/adverse-selection-in-prediction-markets-evidence-from-kalshi/
- Saguillo et al., Unravelling the Probabilistic Forest: https://arxiv.org/abs/2508.03474
- Baker & McHale, Optimal Betting Under Parameter Uncertainty: https://pubsonline.informs.org/doi/10.1287/deca.2013.0271
- MacLean, Thorp & Ziemba, Good and bad Kelly: https://www.stat.berkeley.edu/~aldous/157/Papers/Good_Bad_Kelly.pdf
- Thorp, Kelly Criterion in Blackjack ...: https://sites.oxy.edu/lengyel/M330/thorp/paper.htm
- Whitrow, Many simultaneous events: https://ideas.repec.org/a/bla/jorssc/v56y2007i5p607-623.html
- Grossman & Zhou, Controlling Drawdowns: https://ideas.repec.org/a/bla/mathfi/v3y1993i3p241-276.html
- Diercks, Katz & Wright, Kalshi and Macro Markets: https://www.federalreserve.gov/econres/feds/kalshi-and-the-rise-of-macro-markets.htm
- Kalshi fees: https://kalshi.com/docs/kalshi-fee-schedule.pdf
- Kalshi LIP: https://help.kalshi.com/incentive-programs/liquidity-incentive-program
- Kalshi interest: https://help.kalshi.com/navigating-the-exchange/your-portfolio/apy-on-kalshi
- Kalshi weather: https://help.kalshi.com/markets/popular-markets/weather-markets
- Cross-venue summary: https://quantpedia.com/systematic-edges-in-prediction-markets/
