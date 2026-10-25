# Kalshi's liquidity rewards (LIP)

Research only (2026-10-25). No code changed. **Verdict: not yet.** See the end.

## What it is

- Kalshi pays a fixed cash pool, per market, for resting orders near the best price. Even unfilled.
- Who: any Kalshi member (the help page says US only). No application, agreement or minimum balance.
  Not: Kalshi affiliates, broker (IB or FCM) clients, or signed market makers in their own markets.
- Which markets: Kalshi picks contracts that are new or have wide spreads. The full method is secret.
- Where: a "Rewards" badge on the market page, kalshi.com/incentives, and the public API (no key):
  `GET /incentive_programs?status=active&type=liquidity` gives each market's times, pool (`period_reward`, in centi-cents: divide by 10,000 for $), `target_size_fp` and `discount_factor_bps`.
- How big: one trader's mid-October scan found 6,657 live programs and $775k of pools (about $116
  each, per period). August to December: about $13.4M. Mostly gas prices, weather, streaming, mentions.

## How it pays (numbers)

- Each second, at a random moment, Kalshi snapshots the book. YES and NO bids are scored apart.
  From the best bid down, whole price levels count until they hold the market's **Target Size**.
- At or above the level that holds 1/5 of the Target Size, a bid scores its size. Each tick
  (usually 1c) below that halves its score (the Discount Factor, 0.5). Lower bids get nothing.
- **Both sides rule:** a snapshot counts only if the whole book holds the Target Size on both
  sides. We don't need that size ourselves. Quoting one side earns at most half.
- Payout = our average share (half our YES share + half our NO share) x pool x (snapshots that
  counted / all snapshots). Under $1.00 for a market's period: nothing. Rounded down to the cent.
- Paid in US dollars to the Kalshi balance, within 1 day after the period ends. Can be withdrawn.
- Tax: Kalshi sends a 1099-MISC for "credits and rewards" (LIP not confirmed). Treat it as income.

## What changes on 2026-10-28

| | Until 10-28 | From 10-28 |
|---|---|---|
| Pool per market | $1 to $1,000 a calendar day | $10 to $10,000 a business day (calmer markets: lower, secret caps) |
| Target Size | over 100, under 20,000 | over 100, under 10,000 |
| Discount Factor | up to 1.0, set per market | 0.5 everywhere |
| Period | up to 31 days | 1 to 7 days |
| Markets | "all Kalshi markets" | listed subcategories in 11 categories |

- New: terms posted 24+ hours ahead. Payout within 1 day written in. Affiliates and signed market makers out again.
- Same: per-second snapshots, the scoring, the both-sides rule, the $1 minimum.
- Filed 2026-10-14, the 6th version since August 2025. Still ends by **2027-01-01** (68 days away) unless renewed.

## What a $300 bot could expect

- A resting bid holds its full cost. A YES plus a NO bid costs just under $1. So $300 rests about 300 pairs.
- Best public test: one trader's two 8-hour "shadow" runs (no real orders, about $28,000 of quotes,
  30 markets). Rewards $243-281. Fill losses $183-207. Net +$60-74, about 0.2% of capital.
- Scaled to $300 for 24 hours (x 300/28,000 x 3): rewards about $8 a day, losses about $6, net
  about **+$2 a day**. Best case: rivals couldn't see those quotes to compete.
- Same losses, but rivals take half the rewards: about **-$2 a day**. The middle is near $0.
- It depends on: our real share, bad fills (the price running through us), news, maker fees, the $1 minimum, the new pools.
- Our own maker test (no LIP, 4,633 markets): no edge. Best: NO bids at 90-95c, +2.4%, worst case -19.8%.
- **Without directional risk? No.** Any resting bid can fill, mostly when better-informed
  traders push the price through it. Both sides filling locks in $1. Usually only one side fills.

## Risks

- The engine can't rest or cancel orders. An order we can't cancel is pure directional risk.
- Kalshi picks thin, wide-spread markets: the worst fills. In the trader's test, one market lost $153 in 2 hours.
- Terms change often. Kalshi can end it at any time. Demo has no programs (per the trader).
- Orders under a spoofing or wash-trading inquiry earn nothing.

## Verdict

**Not yet.** Don't build resting quotes now, because:
1. LIP ends by 2027-01-01. Engine work plus a paper test would take longer than that.
2. The best public evidence is about +$2 a day at our size before rivals react, about $0 after.
3. Resting orders need safe cancels: the riskiest money change we could make, for about $0.

Look again only if LIP is extended past 2027-01-01. First a read-only logger (public books, no
orders) that scores $300-sized quotes. Quote only if rewards beat fill losses for 20+ market-days.

**Not found:** live pool sizes per market (Kalshi's site and API were blocked here), the secret
caps and picking method, any filing past 2027-01-01, LIP on the 1099-MISC. Help pages: via search only.

## Sources

- Kalshi's filings: [10-28 version](https://kalshi-public-docs.s3.amazonaws.com/regulatory/notices/Liquidity%20Incentive%20Program%20Amendment.pdf), [Jul 15, 2026](https://kalshi-public-docs.s3.amazonaws.com/regulatory/notices/Liquidity%20Incentive%20Program%20-%20July%2015%2C%202026%20Update.pdf), [Jul 30, 2026](https://kalshi-public-docs.s3.amazonaws.com/regulatory/notices/Liquidity%20Incentive%20Program%20-%20July%2030%2C%202026%20Modification%20to%20July%2015%2C%202026%20Update.pdf), [Feb 2026](https://kalshi-public-docs.s3.amazonaws.com/regulatory/notices/Liquidity%20Incentive%20Program%20-%20February%2011%2C%202026%20Update.pdf), [Sep 2025](https://kalshi-public-docs.s3.amazonaws.com/regulatory/notices/Copy%20of%20Liquidity%20Incentive%20Program%20September%208%202025%20Backup%20Filing%20%281%29.pdf), [market makers](https://kalshi-public-docs.s3.amazonaws.com/regulatory/notices/Designated%20Liquidity%20Provider%20Market%20Making%20Program%20Amendment.pdf)
- Kalshi help (via search): [LIP](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program), [where to find](https://help.kalshi.com/en/articles/16076644-where-to-find-rewards-and-incentive-info), [tax forms](https://help.kalshi.com/en/articles/13823849-what-tax-documentation-does-kalshi-provide)
- API: [Get Incentives](https://docs.kalshi.com/api-reference/incentive-programs/get-incentives). Fields checked in Kalshi's `kalshi_python_sync` 3.34.0 (PyPI).
- [Trader's test](https://github.com/geniuswang05/kalshi_alpha_search) (README and report). [Our maker test](https://github.com/rohanmalhotra0/kal/pull/10#issuecomment-6399096100).
