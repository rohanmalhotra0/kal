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
- Both are wired in: the engine passes the equity peak (drawdown cut) and the money
  already in open bets (`ctx.open_exposure`) to every strategy.
- `favorites`: bets in one series that close on one day ride one price path (an hourly
  series is 24 events a day). Together they may cost at most 10% of capital.

## How research keeps from fooling itself

- One series on one day counts as one "event", in backtests and in paper results.
  Promotion needs 20 of those days, not 20 hourly events.
- Backtest trades pay the real fee: a full-size order, rounded up to the cent.
- The decision time comes from times Kalshi sets when it lists a market, never from
  when it really closed (that may be the news itself). In order: a close that can't
  move (the market must say so), the event's strike_date (weather, indexes, crypto),
  occurrence_datetime on the whole minute (games). Not expected_expiration_time: it can
  equal an early close. Not an occurrence time with seconds: Kalshi sets those after
  the event (Fed mentions). Kalshi marks almost every market "can close early", even
  weather, so that flag alone tells nothing.
- Each market has its own decision time. An event's latest end would depend on which
  of its markets had settled when research ran, and that picks winners.
- A market already closed at the decision time is skipped, as the bot would have found
  it closed. Mention words are learned an hour after the event's scheduled end.
- No filter on a market's total volume: that's known only once it settles, and markets
  far from the result go quiet, so it dropped mostly losers (a fair market showed +50%).
  "Busy" means 200+ contracts traded in the day before the decision, the same check
  the live strategies make. Research for live strategies uses busy markets only. An edge
  with no busy market left to re-test it is switched off until a backtest shows it again.
- The 5% chance of promoting luck is spent over looks and over ideas, for good. The
  20th idea ever judged needs far more proof than the first.
- Demo keeps its own research (`research-demo/`, `cache-demo/`). Nothing is promoted
  on demo.
- Each idea's status, looks and rank are kept in the db too (it is backed up). A loser
  retired for losing money stays retired after a rebuild.
- Daily research runs in its own process. Out of memory, only research is stopped.

## Ideas, best first

**1. Basket arbitrage** - built, live
- What: in a one-winner event, buy NO on several outcomes when YES bids add to over $1.
- Evidence: strong (math). Saguillo et al. found ~$40M taken from gaps like this and 6 on Polymarket.
- Risk: low. A basket can half-fill. The engine stops at the first leg that misses.
- Test: count signals vs full fills in live logs. Many half fills? Buy the thinnest leg first.
- Games and weather (2,257 of 5,658 one-winner events on 2026-10-25) say a cancelled game, or
  missing data, settles at a "fair price" Kalshi picks. Those baskets aren't locked in, so they
  trade on paper only, as $10 bets, using at most 25% of the paper money. Paper counts every leg
  as filled at the prices it saw: it shows how often they come up and what they'd make, not real
  fills. Real money for them is the owner's call.

**2. Ladder arbitrage** - built, paper (switch to live by hand once trusted)
- What: if "above 70" wins, "above 60" wins too. Buy YES broad + NO narrow when under $1.
- Evidence: strong (math). Risk: low. Odd-looking events are skipped.
- Test: did every settled ladder trade pay at least its cost?

**3. Favorites (favorite-longshot bias)** - built, paper until promoted
- What: buy in price ranges that won more often than the price said.
- Evidence: strong that the bias exists; weak that a taker keeps much. Bürgi, Deng & Whelan (300,000+ Kalshi contracts): under 10c loses over 60%. But most of the bias goes to makers: a taker at the real ask keeps about 0-1% at 90-99c (research swarm, 2026-10-25). The gap is shrinking.
- Risk: medium. The edge is a few cents. One upset wipes out many wins. Cancelled games pay a split ($0.33 each on a 3-way), now counted in backtests.
- Test: built in (backtest, then 30 paper trades). Next: try 1, 3 and 24 hours before close (the research says the last 24 h is best); keep spreads at 2c or less.

**4. Bracket arbitrage** - built, paper
- What: brackets cover every value, so one must win. Buy YES on all when under $1.
- Evidence: strong, if there's no gap. Risk: high if there is one. All is lost.
- Test: a person checks 20+ settled events. One bracket won each time? Set "verified".

**5. NO on over-bet YES, by market type** - untested (top pick for the next check-in)
- What: people over-bet YES on "will X happen?" markets that mostly end NO. Buy NO.
- Evidence: medium. Bartlett & O'Hara (41.6M Kalshi trades) found it, mostly in single-name markets (one person or company). A summary says YES was bought 61% of the time there, but won 32%.
- Risk: medium-high. Those same markets have the most insiders.
- Test: reuse `backtest.calibration`, split by market type and side. Need a lower bound above 0 after fees. The research swarm ranks this the second most likely edge for a small taker, after near-resolution favorites.

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
- Note: mention markets are listed to end at the event (occurrence_datetime), so the
  backtest decides 6 hours before that, never just before the word was said.

**9. One-sided order flow guard** - built (`kal/flow.py`, a filter, not a strategy)
- What: skip a bet when 70%+ of the last hour's volume took the other side, or one trade 5x the median ($50+) just did. Paper and live alike. Never for locked-in trades.
- Evidence: medium. Bartlett & O'Hara: one-sided flow predicts maker losses in single-name markets.
- Risk: low. It only removes trades. Next test: did skipped trades do worse than taken ones? (Log the would-be result of each skip.)

**10. Maker quotes + Liquidity Incentive Program** - untested, needs engine work
- What: rest orders instead of taking. Most series charge makers nothing; series with fee_type `quadratic_with_maker_fees` charge 0.0175 x M (a quarter of taker), combos 0.035. Kalshi's LIP pays for resting orders near the best price, even unfilled.
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

**13. Longshot fade (NO on cheap YES)** - being built, paper only
- What: buy NO where YES costs under 20c.
- Evidence: weak for a taker. The research swarm found it about break-even or worse after spread and fees in 2025-2026 Kalshi data; one niche (liquid weather brackets the evening before) didn't hold up elsewhere.
- Risk: high. A NO at 95c loses 95c when the longshot hits.
- Test: the backtest and 30 paper trades decide. Strict guards: spread 2c or less, real depth, within the tested hours of the scheduled close, $6 a market, 10% across one city or day.

**14. Order book recorder** - built (`kal/books.py`)
- What: saves the top 5 levels of every order book a run reads, once per run, in its own `books.db` (not the money database, not in backups). 45 days kept, 300 MB at most.
- Why: Kalshi has no history of depth. Without it, backtests assume we could fill. 4-8 weeks of saved books makes fill and slippage tests honest.
- Next: once 4+ weeks are saved, test whether paper fills at our sizes were really there (`books.history(ticker)`).

**15. What the research says not to build**
- In-game sports momentum, economic-release tails, generic mention fading, parlays and combos (YES side), cross-venue arbitrage: about zero or negative after costs for a slow taker.

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
- Kalshi fee rounding: https://docs.kalshi.com/getting_started/fee_rounding
