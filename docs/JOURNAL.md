# Journal

Newest first. Each 2-hour check-in adds one entry: what it checked, what it found, links.

## 2026-10-30 00:50 UTC — check-in: gold and silver links merged; paper books a set together

- Merged [PR #38](https://github.com/rohanmalhotra0/kal/pull/38): `metal_link_arb` (gold and
  silver month ends), paper only for good. An independent review found no set that pays less
  than it says if both series settle on one number, and 4 gaps, all fixed with tests: a YES now
  sits at least a cent under its NO; it can never use real money (no pin list); a set too good
  to be true in its order book isn't booked on paper (here and in `range_link_arb`); a price too
  big for a float now reads as no price instead of crashing.
- Merged [PR #39](https://github.com/rohanmalhotra0/kal/pull/39): paper books all legs of a set
  in the same run, once each has a result. Before, one leg's gain showed days before the other
  leg's loss: a false equity peak that made every paper bet smaller. 1028 tests.
- Checked: CI and market-check green on main 8af43c8 (all 11 strategies ran, 0 trades found;
  `metal_link_arb` ran clean). No status report yet: `kal-status` doesn't exist or isn't shared
  with Claude.
- Research: no idea can be built now. Only #18 is left, and it needs 4+ weeks of paper results
  from the server.

## 2026-10-29 22:50 UTC — check-in: AAA gas rejected; gold and silver built on paper

- Merged [PR #37](https://github.com/rohanmalhotra0/kal/pull/37): idea #20 (AAA gas daily vs
  weekly vs monthly) rejected. Same words, but Kalshi settles each series on its own: on Jul 13
  the weekly took the day before's figure (3.876), the daily 3.872. The lesson is now in
  RESEARCH.md: the same words aren't enough for a link across series.
- Idea #21 on real data: gold and silver month ends with the same words matched 10 of 10;
  copper broke on Aug 31 (6.69 vs 6.58) and natural gas twice, so those are out. Built as
  `metal_link_arb`, paper only, on branch `metal-link-arb` (46 tests); an independent review
  runs before its PR. No real money planned: the two series settle hours apart and Kalshi
  moves the price source outside the words.
- Checked: CI green on main 50e9347; market-check green today. No status report yet:
  `kal-status` doesn't exist or isn't shared with Claude.

## 2026-10-29 20:50 UTC — check-in: crypto range links merged; a faster pace finds nothing more

- Merged [PR #35](https://github.com/rohanmalhotra0/kal/pull/35): `range_link_arb` (crypto
  "between" vs "above" at one minute), paper only. An independent review found no way to lose
  money and 3 gaps, all fixed with tests: if only one series gets no data, only NO + NO still
  pays, so only that set may ever use real money; one coin's stale quotes could take every
  order-book slot; a too-good set could take budget from a real one (shared code: real sets
  now go first). 982 tests.
- Merged [PR #36](https://github.com/rohanmalhotra0/kal/pull/36): DEPLOY step 10 now says to
  give Claude's GitHub app access to `kal-status`. Without it the check-in can't read the report.
- Every arbitrage scan once a minute for 4 hours (240 scans, branch `probe/arb-watch-1min`):
  0 gaps, 0 failed scans, 25 s a scan. With Oct 25's 9-hour watch: 13 hours, 0 chances. So the
  5-minute pace stays: a faster one finds nothing more. Real-money arbitrage is very rare.
- Checked: CI green on main 5ede476; market-check green on 45e2974 (0 trades found;
  `range_link_arb` ran clean in 0.5 s). No status report yet: `kal-status` doesn't exist or
  isn't shared with Claude.
- Next: idea #20 (AAA gas daily vs weekly vs monthly).

## 2026-10-29 18:50 UTC — check-in: review holes fixed; crypto range links built on paper

- Merged [PR #34](https://github.com/rohanmalhotra0/kal/pull/34): an independent review of
  `exact_link_arb` found no hole in its math, but 5 in the guards around a pin. Fixed:
  `fees.check` missed a fee change on a ticker with a negative strike when a set spans two
  events; recheck didn't redo the too-good check; a pin didn't cover dates (a moved release or
  meeting kept it); paper-only sets could crowd out others; one odd event hid the rest. None
  could lose real money (every pin list is empty). 9 tests, each fails on the old code.
- Idea #19 (one price asked two ways) on real data: crypto "between" vs "above" at the same
  minute is safe (0 broken in about 4,000 settled pairs), but gaps are rare and small (5 in
  the last 30 days, 1-2c each). S&P, Nasdaq and natural gas rejected: the two series settle
  on different numbers. Built as `range_link_arb`, paper only, on branch `range-link-arb`
  (50 tests). An independent review runs before its PR. Idea #20 (AAA gas) added.
- Checked: CI, install-check and market-check green on main c0e6d59 (0 trades found, nothing
  suspicious). No status report yet: `kal-status` doesn't exist or isn't shared with Claude.

## 2026-10-29 16:50 UTC — check-in: all green; three checks running

- Checked: CI green on main eabc082; market-check green at 16:32 (0 trades found, nothing
  suspicious). No status report yet: `kal-status` doesn't exist or isn't shared with Claude.
- Running: idea #19 research (one price at one moment asked two ways, branch
  `probe/price-links`); an independent review of `exact_link_arb`; and every arbitrage scan
  once a minute for 4 hours (branch `probe/arb-watch-1min`), to see if a faster pace than
  5 minutes would catch gaps.

## 2026-10-29 16:37 UTC — memory fix, real install test, exact-value links on paper

- Merged [PR #30](https://github.com/rohanmalhotra0/kal/pull/30): idea #16, known results,
  rejected. A result that is already known is already priced at $1.
- Merged [PR #31](https://github.com/rohanmalhotra0/kal/pull/31): `install-check` runs the
  real install on a fresh Ubuntu 24.04 machine on every code change and every Monday. It
  found the Python packages world-writable (any user could change the code the bot runs
  with the key) and watchdog false alarms. Both fixed.
- Merged [PR #32](https://github.com/rohanmalhotra0/kal/pull/32): memory. Each trade run
  held all 15,601 open events (733 MB) under a 400 MB cap with no swap, so once on, every
  trade run would have been killed. Now it keeps only events due within `max_arb_days` + 1
  (156 MB). In the real service, a trade run and research together peaked at 497 MB; a
  trade run takes 17 s. The cap is 60% of RAM, and the docs say 2 GB server. A test runs
  every arbitrage strategy 2 to 400 days out: same accepted trades with and without it.
- Merged [PR #33](https://github.com/rohanmalhotra0/kal/pull/33): idea #17, `exact_link_arb`,
  paper only. CPI and jobs "exactly X%" vs "above X%": 533 settled sets all paid $1, and 84
  had gaps that pass the rules. Paper only: if a report never comes out, a set can pay $0.
- Started idea #19: one price at one moment, asked two ways (e.g. Bitcoin "between" vs
  "above" at 5 PM).
- Checked (also the 14:58 check-in): CI green on main b6a3bf3. Market-check with the new
  code on the real API: self-test, scan and research pass. No status report yet: the
  private status repo `kal-status` doesn't exist or isn't shared with Claude.
- Owner: update the server with `cd /opt/kal && sudo git pull && sudo bash deploy/install.sh`.
  Still waiting on the owner's `sudo kal doctor` output.

## 2026-10-29 14:40 UTC — Fed links built on paper; CPI and jobs rejected; new ideas

- Merged [PR #28](https://github.com/rohanmalhotra0/kal/pull/28): `fed_link_arb`, paper only.
  It buys a set of Fed markets that must pay $1 (rate after the meeting vs the decision)
  when it costs less. 34 tests. Checked on the live API: it reads the Dec meeting right
  (rate before 4.00%, no gap today). Critics' points fixed: a too-good price keeps even a
  pinned meeting on paper, and the docs say a moved meeting breaks some sets.
- Merged [PR #29](https://github.com/rohanmalhotra0/kal/pull/29): idea #7, CPI and jobs,
  rejected. The Fed's study finds Kalshi as good as or better than the experts. Our check
  of 682 markets in 143 releases: well calibrated, no rule beats the fees. Added ideas
  #16-#18 so the check-ins have work.
- Checked: CI green on main f1773d9. Market-check green; its real-data scan runs the new
  strategy. Waiting on the owner's `sudo kal doctor` output.

## 2026-10-29 12:55 UTC — money audit round 3 merged; Fed links look real; crypto links rejected

- Merged [PR #27](https://github.com/rohanmalhotra0/kal/pull/27): 18 money holes closed.
  12 from the audit, 6 more from critics checking the fixes together. Each has a test that
  fails on the old code; 827 tests pass. CI green on main 0cfe8ba.
- Merged [PR #26](https://github.com/rohanmalhotra0/kal/pull/26): GitHub actions moved off
  Node 20 (the warning on every run is gone).
- Idea #6, Fed and crypto links, on real data (branch `probe/fed-crypto`):
  - Fed rate level vs Fed decision: 14 settled meetings, 0 broken links. In 2026, 17 hours
    had a gap after fees that passes the bot's rules, with real trading in those hours.
    Building it as a paper-only strategy next.
  - Crypto "how high/low this month/year": rejected. Rules differ between series (some
    count only weekday 8 AM-7 PM prices), and 2 settled BTC pairs in 2024 broke the link.
- Waiting on the owner's `sudo kal doctor` output.

## 2026-10-29 10:50 UTC — check-in: all green; money audit round 3 in progress

- Checked: CI green on main 1029811. Latest market-check is the clean daily run of
  2026-10-28 18:53 UTC. No status report yet: the server is still being set up.
- Third independent money audit (4 reviewers, 1 critic): 15 findings, 12 confirmed.
  Worst: after practice on demo, going back to real money restores the modes but keeps the
  demo budget and caps. Also: the daily stop misses a loss booked in the first run of a day;
  auto-update misses code brought in by merge commits; a restore can lose track of trades if
  interrupted. Fixes with tests are being built; they go in one PR.
- Waiting on the owner's `sudo kal doctor` output.

## 2026-10-29 08:50 UTC — check-in: all green; idea #6 sports links tested, rejected

- Checked: CI green on main fa4853a. Latest market-check is the clean daily run of
  2026-10-28 18:53 UTC. No status report yet: the server is still being set up.
- Research is back on (the usage limit reset at 09:00 UTC). Idea #6, other links: sports
  title chains, where YES on one series means YES on another (champion, then conference
  champion, then playoffs; conference champion, then title game). 45 pairs picked by hand,
  65 seasons, 1,397 team pairs. 105,974 hours of history: never under $1 after fees, never
  crossed even before fees. Today: 0 gaps in 434 open pairs. Rejected. Probe on branch
  `probe/links`; notes in [PR #25](https://github.com/rohanmalhotra0/kal/pull/25).
  Fed and crypto links are still untested.
- Waiting on the owner's `sudo kal doctor` output.

## 2026-10-29 06:50 UTC — check-in: all green, nothing new

- Checked: CI green on main bf7f34e. Latest market-check is the clean daily run of
  2026-10-28 18:53 UTC. No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit (resets today 09:00 UTC).
  Waiting on the owner's `sudo kal doctor` output.

## 2026-10-29 04:50 UTC — check-in: all green, nothing new

- Checked: CI green on main 2e8f65e. Latest market-check is the clean daily run of
  2026-10-28 18:53 UTC. No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit (resets today 09:00 UTC).
  Waiting on the owner's `sudo kal doctor` output.

## 2026-10-29 02:50 UTC — check-in: all green, nothing new

- Checked: CI green on main 3caebf3. Latest market-check is the clean daily run of
  2026-10-28 18:53 UTC. No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit (resets today 09:00 UTC).
  Waiting on the owner's `sudo kal doctor` output.

## 2026-10-29 00:50 UTC — check-in: all green, nothing new

- Checked: CI green on main 30edae7. Latest market-check is the clean daily run of
  2026-10-28 18:53 UTC. No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit (resets today 09:00 UTC).
  Waiting on the owner's `sudo kal doctor` output.

## 2026-10-28 22:50 UTC — check-in: all green, nothing new

- Checked: CI green on main ad3b11c. Latest market-check is today's clean daily run
  (18:53 UTC), already read at 20:50. No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit (resets Thursday 09:00 UTC).
  Waiting on the owner's `sudo kal doctor` output.

## 2026-10-28 20:50 UTC — check-in: all green, nothing new

- Checked: CI green on main 30ad7b8. Today's daily market-check
  ([run 40749343146](https://github.com/rohanmalhotra0/kal/actions/runs/40749343146)) passed
  with no warnings: scan found 0 trades, quick research 0 errors, coverage ok.
  No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-28 19:05 UTC — check-in: all green

- Checked: CI green on main 3a91085. The last market-check was just over 24 h old, so I
  started one ([run 40748692502](https://github.com/rohanmalhotra0/kal/actions/runs/40748692502)).
  The daily one fired 4 minutes later; both passed. Self-test all ok, scan found 0 trades
  (dry run), quick research 0 errors, coverage ok (331 busy markets).
- One warning, harmless: research skipped 3 KXALLSVENSKANGAME markets after Kalshi said
  "too many requests" (429). The client retried with backoff first, as designed. Likely
  cause: two runs at once. Next time, only start one by hand if none ran in 26 h.
- No status report yet: the server is still being set up. Research waits for the weekly
  usage limit. Waiting on the owner's `sudo kal doctor` output.

## 2026-10-28 16:50 UTC — check-in: all green; 28-hour gap in check-ins

- Gap: no check-ins reached this session between 2026-10-27 12:50 and 2026-10-28 16:47 UTC.
  The scheduled task is still on and fired on time at 16:47.
- Checked: CI green on main 1fd70e5. Daily market-check of 2026-10-27 18:44 UTC
  ([run 40562746036](https://github.com/rohanmalhotra0/kal/actions/runs/40562746036)):
  self-test all ok, scan found 0 trades (dry run), quick research 0 errors, coverage ok
  (327 busy markets). No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-27 12:50 UTC — check-in: all green, nothing new

- Checked: CI green on main 0d9a8cd. Latest market-check is the clean scheduled run of
  2026-10-26 19:22 UTC. No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-27 10:50 UTC — check-in: all green, nothing new

- Checked: CI green on main 04cc487. Latest market-check is the clean scheduled run of
  2026-10-26 19:22 UTC. No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-27 08:50 UTC — check-in: all green, nothing new

- Checked: CI green on main 684ab27. Latest market-check is the clean scheduled run of
  2026-10-26 19:22 UTC. No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-27 06:50 UTC — check-in: all green, nothing new

- Checked: CI green on main 4f8c7ef. Latest market-check is the clean scheduled run of
  2026-10-26 19:22 UTC. No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-27 04:50 UTC — check-in: all green, nothing new

- Checked: CI green on main 16f2e44. Latest market-check is the clean scheduled run of
  2026-10-26 19:22 UTC. No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-27 02:50 UTC — check-in: all green, nothing new

- Checked: CI green on main a0d992a. Latest market-check is the clean scheduled run of
  2026-10-26 19:22 UTC. No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-27 00:50 UTC — check-in: all green, nothing new

- Checked: CI green on main dd0a64c. Latest market-check is the clean scheduled run of
  2026-10-26 19:22 UTC. No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-26 22:50 UTC — check-in: all green, nothing new

- Checked: CI green on main 61182b9. Latest market-check is today's clean scheduled run
  (19:22 UTC), already read at 20:50. No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-26 20:50 UTC — check-in: daily market-check now runs on its own, all green

- Checked: CI green on main 52b9794. GitHub's daily market-check schedule fired for the first
  time (19:22 UTC, [run 40386202483](https://github.com/rohanmalhotra0/kal/actions/runs/40386202483)):
  self-test all ok, scan found 0 trades (dry run), quick research 0 errors, coverage ok
  (337 busy markets). Check-ins no longer need to start it by hand. No status report yet:
  the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-26 18:50 UTC — check-in: all green, nothing new

- Checked: CI green on main 29fa59c. No code change since the clean market-check on c32c0c4
  (2026-10-25 21:28 UTC). No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-26 16:50 UTC — check-in: all green, nothing new

- Checked: CI green on main 23d6296. No code change since the clean market-check on c32c0c4
  (2026-10-25 21:28 UTC). It turns 24 h old at 21:28 UTC; the first check-in after that
  runs it again. No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-26 14:50 UTC — check-in: all green, nothing new

- Checked: CI green on main 3db0bb8. No code change since the clean market-check on c32c0c4
  (2026-10-25 21:28 UTC). No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-26 13:00 UTC — check-in: all green, nothing new

- Checked: CI green on main c8bdbfe. No code change since the clean market-check on c32c0c4
  (2026-10-25 21:28 UTC). No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-26 10:50 UTC — check-in: all green, nothing new

- Checked: CI green on main e54a339. No code change since the clean market-check on c32c0c4
  (2026-10-25 21:28 UTC). No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-26 08:50 UTC — check-in: all green, nothing new

- Checked: CI green on main a1b35da. No code change since the clean market-check on c32c0c4
  (2026-10-25 21:28 UTC). No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-26 06:50 UTC — check-in: all green, nothing new

- Checked: CI green on main 8e06133. No code change since the clean market-check on c32c0c4
  (2026-10-25 21:28 UTC). No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-26 04:50 UTC — check-in: all green, nothing new

- Checked: CI green on main 970e66b. No code change since the clean market-check on c32c0c4
  (2026-10-25 21:28 UTC). No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-26 02:50 UTC — check-in: all green, nothing new

- Checked: CI green on main 6aefd9e. No code change since the clean market-check on c32c0c4
  (2026-10-25 21:28 UTC). No status report yet: the server is still being set up.
- No new idea: research waits for the weekly usage limit. Waiting on the owner's
  `sudo kal doctor` output.

## 2026-10-26 00:50 UTC — check-in: all green, nothing new

- Checked: CI green on main a3939d9. No code change since the clean market-check on c32c0c4
  (21:28 UTC: self-test passes, 0 trades found, research 0 failures). No status report yet:
  the server is still being set up.
- No new idea: research waits for the weekly usage limit (resets Thursday). Waiting on the
  owner's `sudo kal doctor` output.

## 2026-10-25 22:50 UTC — check-in: all green; new research paused for the usage limit

- Checked: CI green on main 772a322. market-check green on c32c0c4 (PR #24's code): self-test
  passes, the scan found 0 trades in every strategy (nothing suspicious), research ran
  3 series with 0 failures, 338 busy markets. No status report yet: the server is still being set up.
- No new idea this check-in: the account is near its weekly usage limit (resets Thursday), so
  research and swarms wait. Next: the owner's `sudo kal doctor` output from the server.

## 2026-10-25 21:35 UTC — PR #24 merged: 5 more holes closed after a review of PR #23

- An independent review of PR #23's new code proved 5 holes, each with a failing test. All
  fixed in PR #24 (c32c0c4), 784 tests pass, CI green:
  - a part sale was written off twice at settlement;
  - with no key, live payouts were booked without checking Kalshi's fills;
  - a demo round trip ignored the modes chosen on prod (basket_arb paper could come back live);
  - an order lookup that said only "canceled" was read as 0 filled;
  - a restored winning bet settling tripped the daily stop.
- The $0.01 test-order pass is now version 3, so older passes are proved again.
- Paused new swarms and research: the account is near its weekly limit (resets Thursday).
- Next: the owner's `sudo kal doctor` output from the server.
  https://github.com/rohanmalhotra0/kal/pull/24

## 2026-10-25 21:10 UTC — anchors hold after closes; 9 hours of arbitrage watches found nothing

- PR #23 merged (8ce7926). CI and market-check green on it.
- Anchor check ([run](https://github.com/rohanmalhotra0/kal/actions/runs/40233993719)): of 6,851 markets
  recorded while open this morning, 1,441 have closed (121 early, 1h+ before their anchor). No
  anchor field changed on any of them (occurrence_datetime, expected/latest expiration,
  strike_date). So research keeps using them.
  https://github.com/rohanmalhotra0/kal/pull/9#issuecomment-6403310583
- Arbitrage watches (public data, no orders, a scan every 5 min):
  - live rules, 300 min, 60 scans: 0 gaps in basket, ladder or bracket;
  - paper-only cancel-clause baskets too, 250 min, 50 scans: 0 baskets.
  So live arbitrage is rare. Expect the bot to trade real money seldom, and only when a gap passes every rule.
  https://github.com/rohanmalhotra0/kal/pull/14#issuecomment-6403311361

## 2026-10-25 21:00 UTC — check-in: all green; fresh money-path audit, 5 holes fixed (PR #23)

- Checked: CI green on main 84f0d0d. market-check green on f793218 (19:05): self-test passes,
  every strategy scanned with no errors, 0 trades found (normal). No status report yet: the
  server is still being set up.
- Fix this check-in: a fresh audit (2 reviewers, 1 critic; each finding needed a failing test)
  found 5 money-path holes. All fixed in PR #23, with 27 new tests (13 fail on main):
  - after a restore, the owner's own winning bets became the bot's capital (past $300);
  - contracts sold outside the bot were booked as paid at settlement;
  - a same-day budget raise hid the day's loss from the daily stop;
  - a strategy turned live for demo practice stayed live on real money;
  - the fair-price guard was exact text. On real data, 224 open one-winner events counted as
    locked in despite a "fair market price" clause (mostly elections). Now off real money.
  https://github.com/rohanmalhotra0/kal/pull/23

## 2026-10-25 19:05 UTC — date ladders merged, paper only

- Merged PR #21 (`date_ladder_arb`, idea #6) as f793218. CI green on its last commit. 745 tests pass.
- A third review found more wordings that flip the question ("nothing is released",
  "refrains from", "the night before", "vetoed"). Word lists can't make rules safe for money.
- So real money now needs the event in `date_ladder_arb.VERIFIED`: its rules pinned word for
  word, added by PR after a person reads them. It starts empty, so nothing trades live.
- Next: the owner's `sudo kal doctor` output from the server.
  https://github.com/rohanmalhotra0/kal/pull/21

## 2026-10-25 18:47 UTC — check-in: all green; date ladders answered the coordinator's 5 checks

- Checked: CI green on main b3e2fff. market-check green on 0e04a90:
  - self-test passes;
  - scan finds 0 trades in all 6 strategies, nothing suspicious;
  - research: 3 series, 0 new edges, 0 failures;
  - coverage: 336 busy markets (floor 25).
  No status report yet: the server is still being set up.
- Idea this check-in: #6 date ladders, PR #21 (CI green, a second independent review running).
  - Live needs `mode: live` and `verified`. Promotion never writes the config.
  - A short second leg trips the bot, and the naked leg stays under `max_per_trade` (new tests).
  - Real Drake gap: +18c a pair before fees, +15c after both legs' fees (17.6%).
  - 6 of the 7 real gap hours pass the stricter checks. Hegseth is refused for its
    death "fair price" rule.
  https://github.com/rohanmalhotra0/kal/pull/21#issuecomment-6401661021

## 2026-10-25 18:40 UTC — server up, installer fixed, all finished PRs merged, date ladders built

- **Server:** the owner made the droplet (2 GB, NYC1, Ubuntu 24.04, $12 a month). The owner is doing
  install and setup in its Web Console. No status report yet.
- **Installer, tested on a fresh Ubuntu 24.04** (side branch `probe/install`):
  - main's install worked, and `kal doctor` showed only "no key yet";
  - but `apt-get` stops at once while Ubuntu's own first-boot updates hold its locks.
  - Fixed in #19: it waits and says so once. With both locks held for 60 s it finished in 91 s.
  https://github.com/rohanmalhotra0/kal/pull/19
- **Key file** (#20): it goes straight to `/var/lib/kal/kalshi.pem`, so no copy is left in
  `/tmp`. `kal doctor` warns if one is. https://github.com/rohanmalhotra0/kal/pull/20
- **Merged at the owner's request ("push all PRs to main once done"):** #9, #10, #11, #12,
  #19, #20 (#18 by the owner). Each was tested on the main it landed on, with CI green.
  #12's merge dropped weather's `quiet()` (it used a removed constant), and a test now
  catches any stale `backtest.<name>`.
- **Idea #5 rerun** (busy markets only, 1 h / 6 h / 24 h, bar raised for 3x the tries): no
  edge at any horizon. Best: 24 h, NO 95c+ with tight spreads, +0.1% (worst -1.6%).
  https://github.com/rohanmalhotra0/kal/pull/12#issuecomment-6401349260
- **Idea #11 (Polymarket):** no edge. 3,132 matched games, a Brier tie.
  https://github.com/rohanmalhotra0/kal/pull/18
- **Idea #6, date ladders:** 8 hours of real locked gaps in 88,407 pair-hours, in 5 events.
  - Built `date_ladder_arb`, paper only until a person checks it (`verified`).
  - Independent review found a blocker: negated rules ("has not released before") passed.
    Fixed: the rules must say "if it happens before <its own date>".
  - "After issuance" windows pair only in listing order.
  - Real data: 307 of 405 open ladders pass, and 4 of the 5 gap events.
  https://github.com/rohanmalhotra0/kal/pull/21

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
