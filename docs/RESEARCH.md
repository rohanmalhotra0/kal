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
- 2026-10-25 audit (PR #23): 224 more one-winner events say "fair market price", which the old
  exact-text check missed, so they counted as locked in. Most are elections ("postponed beyond
  one year or cancelled entirely"); also golf and cricket. Now off real money. Elections trade
  on paper like games, and so do esports and darts, whose clauses only cover a match that
  never happens. Tennis (about 130 events) stays off even on paper: its rules leave "can't be
  settled" to the exchange's sole discretion.

**2. Ladder arbitrage** - built, paper (switch to live by hand once trusted)
- What: if "above 70" wins, "above 60" wins too. Buy YES broad + NO narrow when under $1.
- Evidence: strong (math). Risk: low. Odd-looking events are skipped.
- Test: did every settled ladder trade pay at least its cost?

**3. Favorites (favorite-longshot bias)** - built, paper until promoted
- What: buy in price ranges that won more often than the price said.
- Evidence: strong that the bias exists; weak that a taker keeps much. Bürgi, Deng & Whelan (300,000+ Kalshi contracts): under 10c loses over 60%. But most of the bias goes to makers: a taker at the real ask keeps about 0-1% at 90-99c (research swarm, 2026-10-25). The gap is shrinking.
- Risk: medium. The edge is a few cents. One upset wipes out many wins. Cancelled games pay a split ($0.33 each on a 3-way), now counted in backtests.
- Test: built in (backtest, then 30 paper trades). Tried 1, 6 and 24 hours before close on
  busy markets only (2026-10-25, PR #12): no price range clears the bar at any horizon. Best:
  NO 95c+ with spreads of 2c or less, +0.1% to +0.6% (worst case -1.6% to -2.9%). Next: count
  its paper trades; nothing to promote.

**4. Bracket arbitrage** - built, paper
- What: brackets cover every value, so one must win. Buy YES on all when under $1.
- Evidence: strong, if there's no gap. Risk: high if there is one. All is lost.
- Test: a person checks 20+ settled events. One bracket won each time? Set "verified".

**5. NO on over-bet YES, by market type** - tested 2026-10-25: no edge for a taker
- What: people over-bet YES on "will X happen?" markets that mostly end NO. Buy NO.
- Evidence: medium. Bartlett & O'Hara (41.6M Kalshi trades) found it, mostly in single-name markets (one person or company). A summary says YES was bought 61% of the time there, but won 32%.
- Risk: medium-high. Those same markets have the most insiders.
- Test: reuse `backtest.calibration`, split by market type and side. Need a lower bound above 0 after fees. The research swarm ranks this the second most likely edge for a small taker, after near-resolution favorites.
- Result (deep research on PR #9, 24,878 markets, 12 categories, 6 h before the end): no NO range has an edge in any category. Best: NO 95c+ with spreads of 2c or less, +0.2% (worst case -2.7%); politics NO 95c+, +0.7% (worst -4.1%).
- Rerun on busy markets only (PR #12: 200+ contracts in the day before, as live strategies
  ask), at 1 h, 6 h and 24 h before the end: 15,605 to 19,752 markets each, 576 tries per
  run, and the bar raised for 3x the tries (z = 4.02). No row clears it at any horizon. No
  row has even a lower bound above 0. Best: NO 95c+ with tight spreads at 24 h, +0.1% (worst
  -1.6%). The verdict stands: no taker edge.

**6. Related markets that must agree** - date ladders built, paper until checked (`date_ladder_arb`); Fed rate vs decision built, paper only (`fed_link_arb`); sports title chains and crypto price links rejected 2026-10-29
- What: linked prices must fit. E.g. "Fed cuts in Dec" vs "rate below X in Jan".
- Evidence: medium. Saguillo et al. found this on Polymarket. Finding the links is the hard part.
- Risk: medium-high. If the rules differ (source, time, rounding), both legs can lose.
- Test: pick 3 pairs where the rules text proves the link. Log gaps on paper for 2 weeks.
- Date ladders, tested 2026-10-25 on real data: one question with several deadlines
  ("Before Aug 1" / "Before Sep 1"). YES on the later date plus NO on the earlier one pays
  $1 by the earlier date. Hourly prices of 536 such events (405 open, 131 settled):
  - 88,407 pair-hours with both prices. 8 hours had a gap after fees that passes the bot's
    rules (settles within 30 days, 20%+ a year), in 5 events. One more was too small.
  - 1 of the 8 (29c, 41% return) is refused by the "too good to be true" cap of 25%.
  - The rest: a Drake album ladder at about 15c for 3 hours, and 1-2c gaps in 3 others.
  - 0 of 131 settled events broke the order (an earlier date YES, a later one NO).
  - Those are a ceiling: the count used the written deadlines and none of the strategy's
    checks. With the merged ones (open events, 2026-10-25): 266 of 405 ladders pass.
    Skipped: 46 with a "fair price if the person dies" rule, 43 whose rules let the outcome
    be undone (e.g. "not retracted before expiration"), 34 whose rules don't say just "if it
    happens before <date>", 9 whose rules differ between dates ("meet (including phone
    calls)" vs "meet"), 7 with mixed data. 6 of the 8 gap hours pass (Hegseth has the death
    rule; the 29c one hits the cap).
  - Every pair needs the later market listed no later than the earlier one, so "after
    issuance" windows (the Drake ladder) nest: 360 of 3,614 pairs fail that.
  - So: rare but real. Built as `date_ladder_arb` on paper.
  - Three rounds of independent review each found new rule wordings that read the question
    the wrong way round ("nothing is released", "refrains from", "the night before"). So the
    word checks only keep paper results clean. Real money goes only to an event listed in
    `date_ladder_arb.VERIFIED`, after a person reads its rules in full, pinned word for word
    (any change and it's back on paper). It starts empty. Next: count paper signals for 4
    weeks; then a check-in can read the rules of the ladders that gap and propose a few by PR.
- Sports title chains, tested 2026-10-29 on real data: rejected. The market keeps them in line.
  - What: YES on one series means YES on another. A champion won its conference. A
    conference champion played in its title game. A 1 seed made the playoffs. So YES on the
    narrower market plus NO on the broader one pays $1 at least.
  - 45 pairs picked by hand, with each series' rules read (NBA, NFL, NHL, MLB, MLS, college
    football and basketball). 65 seasons matched by team name: 1,397 team pairs.
  - History: 105,974 hours with both prices, in the 45 days before each season paid out.
    0 hours under $1 after fees. 0 hours crossed even before fees (closest: a tie at 3c).
  - Today: 434 open team pairs, 0 under $1 after fees. The closest is a 1c cross at 1-2c
    prices, which fees turn into a loss.
  - 0 settled results broke a link.
  - Not built. The probe is on branch `probe/links` (never merged).
- Fed decision vs rate level, tested 2026-10-29 on real data: built as `fed_link_arb`, paper only.
  - What: for one meeting, KXFED asks "above 4.00% after it?" and KXFEDDECISION asks what the
    Fed does (cut >25bps, cut 25bps, hold, hike 25bps, hike >25bps). Call the rate before the
    meeting U (the last meeting's KXFED result). "Above U" is YES only on a hike. "Above
    U - 0.25" is NO only on a cut. So some sets always pay $1, like NO "above U" + NO "hold".
  - 14 settled meetings (Dec 2024 - Oct 2026): the decision matched U and the rate after it
    every time. 0 broken links.
  - 2026's meetings: 17 hours had a gap after fees that passes the bot's rules (pays within
    30 days, 20%+ a year, 1%+ return), with real volume. Best: Aug 24, NO "above 3.75%" (Sep)
    at 56c + NO "hold" at 33c, with 6,736 and 3,686 contracts traded that hour. Also Oct 7
    (78c + 17c) and Feb 23 (YES "above 3.50%" at 91c + NO "hold" at 6c).
  - Today (Dec meeting, U 4.00%): nothing under $1.
  - It uses only "above U" and "above U - 0.25". Those hold however a 50bp move is counted.
  - Why paper: a rate move between meetings (an emergency cut) makes U wrong. And the
    decision rules have a typo: a 50bp hike resolves "Hike 25 bps" to Yes and "Hike 25 bps"
    to No. The "Cut >25bps" market's own rules even say "Cut of 25bps" (the ">" is lost).
    Real money needs a person to pin the meeting in `fed_link_arb.VERIFIED`.
  - Probe: branch `probe/fed-crypto` (never merged).
- Crypto price links, tested 2026-10-29 on real data: rejected.
  - What: one-touch "how high (or low) this month or year" series nest in theory. A hit in a
    short window is a hit in the long one too.
  - But their rules differ: some count only weekday 8 AM-7 PM prices, some use a simple
    average, and one ticker's strike disagrees with its rules.
  - 2 settled 2024 BTC pairs broke the link: BTCMAXY-24DEC31-75000 YES while -70000 NO, and
    BTCMAXM-24MAY-69999.99 YES while BTCMAXY-24DEC31-70000 NO.
  - 1,498 nested pairs, 31 passing hours, none safe. Not built. Probe: branch `probe/fed-crypto`.

**7. Scheduled data releases (weather, CPI, jobs)** - weather tested 2026-10-25: rejected
- What: compare the price with a public forecast before the report comes out.
- Evidence: medium-low. A Fed study found Kalshi macro prices about as good as expert surveys. Weather "edges" online come from sellers, not studies.
- Risk: medium. Model error, thin markets, news moves first.
- Test: daily high temps (settle on the NWS Daily Climate Report). Save forecast + price 6h before close for 60+ days. Compare with results.
- Backtest: `tools/weather_research.py` (`kal/research/weather.py`). Daily highs in 7 cities. It uses the latest NBM forecast run published 2+ hours before the decision time (the Iowa Environmental Mesonet keeps every run). Each city's bias and spread are fitted only on earlier days. It buys where the forecast and the price disagree by 5c or 10c after the taker fee, and counts one day in all cities as one unit. It also compares how well the forecast and the market called the results (Brier score).
- Result (4,686 markets, 112 days, 7 cities): Kalshi's prices beat the NBM forecast in every city (Brier 0.095 vs 0.121 at 18 h before the end; 0.098 vs 0.122 at 30 h). The market is calibrated at every level. The forecast is overconfident in the likeliest bracket: at 60%+ it won 50%. Every trading rule lost: YES about -50%, NO -11% to -17%. The brackets map right (every event tiles), and the tails aren't fat (2.7% of results beyond 2 spreads). Rejected: the public forecast knows less than the market.

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

**10. Maker quotes + Liquidity Incentive Program** - tested 2026-10-25: no lead
- What: rest orders instead of taking. Most series charge makers nothing; series with fee_type `quadratic_with_maker_fees` charge 0.0175 x M (a quarter of taker), combos 0.035. Kalshi's LIP pays for resting orders near the best price, even unfilled.
- Evidence: medium. Makers beat takers (Bürgi et al.). In single-name markets, makers earn ~2x per contract vs broad ones (Bartlett & O'Hara).
- Risk: high. Better-informed traders pick off resting orders. The engine can't cancel orders. LIP terms change often: check they still apply.
- Test: paper only. Log would-be quotes. Count a fill when trades go through our price. Score at settlement, plus our LIP share.
- Backtest: `tools/maker_research.py` (`kal/research/maker.py`). At the decision time, rest a one-contract bid at the best bid (or 1c better). It fills only when a later trade reached it, and every fill pays a maker fee. One contract is a maker's best case, so a price range that loses here loses at any size. A range that wins is only a lead: check it with real trade sizes first.
- Result (4,633 markets, 224 category-days, 6 h before the end, each series' own maker fee): no lead. Best: NO bids at 90-95c, +2.4% (worst case -20%). Most ranges lose even in this best case. Fills come when the price runs through the bid (a 95c+ NO bid filled about 1 time in 10). Not worth the engine work now.
- LIP (docs/LIP.md, 2026-10-25): not yet. It ends by 2027-01-01. The best public test scales to about +$2 a day at $300, and about $0 once rivals compete. Resting quotes need safe cancels the engine doesn't have. Look again only if Kalshi extends it, starting with a logger that sends no orders.

**11. Other venues as a fair-price guide** - tested 2026-10-25: no edge, the prices match
- What: use Polymarket or sportsbook prices to judge Kalshi's. Trade only Kalshi.
- Evidence: weak-medium. A 2024 election study found Polymarket led Kalshi.
- Risk: medium. Venues can settle the same event differently.
- Test: log both prices. Which was closer to the result?
- Result (`tools/xvenue_research.py`): 3,132 games in 13 leagues, 90 days, 6 h before the end. Same price (over 2c apart on 0.3% of markets), same skill (Brier 0.1984 Kalshi, 0.1985 Polymarket), same results on all 6,836 markets. Buying Kalshi on a 2-6c gap: 4-11 trades, all lost.

**12. Interest on positions** - checked 2026-10-25: kept as the owner's money (accounting, not a strategy)
- What: Kalshi pays variable interest (seen 3.25-4%) on cash and open positions. Monthly, $250+ only.
- Evidence: strong (Kalshi help page). Small: ~$10 a year on $300. Risk: low.
- Test: find it in account history. Ask for the ledger to count it. Never loosen risk rules for it.
- Result: the ledger is built from the bot's own fills and settlements only, so interest (like
  a deposit) never raises what the bot may spend. A test pins that. Counting it would add about
  $10 a year to what the bot may reinvest: not worth a money-path change.

**13. Longshot fade (NO on cheap YES)** - built, paper until promoted (`longshot_fade`)
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
