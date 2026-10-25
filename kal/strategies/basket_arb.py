"""Locked-in NO-basket arbitrage.

In an event where at most one market can resolve YES (Kalshi's
`mutually_exclusive` flag), buying c NO on any set S of its markets pays at
least c * (|S| - 1): every NO pays except the winner's. So when the YES bids
add up to more than $1 (net of fees), the profit is locked in. This usually
happens because people overpay for longshots.

Nearly every game and weather event says a cancelled game (or missing data) settles
at a "fair price". Those baskets are found too, but go on paper only: if that happens,
Kalshi picks the price, so the profit isn't locked in.
"""
import time

from ..client import fee, price, ts
from .base import Leg, Signal, Strategy, fair_price_only_if_cancelled, plain_payouts
from .ladder_arb import MIN_TIME_LEFT, yearly

MIN_PROFIT = 0.05   # dollars per basket, after fees
MIN_ROI = 0.01
MAX_CONTRACTS = 500


def no_basket(legs, budget):
    """legs: [(ticker, yes_bid, size)]. Pick the count c and set S with the most locked-in
    profit within budget. Each leg adds c * yes_bid - fee, so only positive ones go in."""
    best = None
    top = min(MAX_CONTRACTS, int(max((s for _, _, s in legs), default=0)))
    for c in range(1, top + 1):
        S = [(t, b) for t, b, s in legs if s >= c and 0 < b < 1 and c * b - fee(c, 1 - b) > 0]
        if len(S) < 2:
            continue
        cost = sum(c * (1 - b) + fee(c, 1 - b) for _, b in S)
        profit = c * (len(S) - 1) - cost
        if cost <= budget and (best is None or profit > best["profit"]):
            best = {"count": c, "legs": S, "cost": cost, "profit": profit, "payout": c * (len(S) - 1)}
    if best and best["profit"] >= MIN_PROFIT and best["profit"] / best["cost"] >= MIN_ROI:
        return best
    return None


def to_signal(event, title, basket):
    # Biggest YES bid first: that's usually the mispriced one, most likely to vanish.
    legs = [Leg(t, "no", round(1 - b, 4), basket["count"]) for t, b in sorted(basket["legs"], key=lambda x: -x[1])]
    c = basket["count"]
    sig = Signal("basket_arb", event, title, legs, payout_min=basket["payout"], note=(
        f"buy {c} NO on {len(legs)} markets; pays at least ${basket['payout']:.2f}"),
        prefix_payouts=[c * (k - 1) for k in range(1, len(legs))])  # k NO legs pay >= c*(k-1)
    sig.exp_profit = sig.payout_min - sig.cost
    return sig


def latest_close(markets):
    """When the last market in the basket closes, or None if any close time is unreadable."""
    try:
        return max(ts(m["close_time"]) for m in markets)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def first_close(markets):
    """When the first market in the basket closes, or None if any close time is unreadable."""
    try:
        return min(ts(m["close_time"]) for m in markets)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def time_left(first):
    """Enough time to buy every leg before any market closes? A basket half-bought at the
    bell can't be finished: the closed legs get refused and the rest are left on their own."""
    return first is not None and first - time.time() > MIN_TIME_LEFT


class BasketArb(Strategy):
    name = "basket_arb"
    title = "Basket arbitrage"
    kind = "arbitrage"
    description = ("In events where only one outcome can win, buy NO on several outcomes when "
                   "the total costs less than the guaranteed payout.")

    def _budget(self, ctx):
        return min(ctx.budget_left, ctx.cfg["max_per_arb"])

    def scan(self, ctx):
        found = []
        for ev in ctx.client.events(status="open", with_nested_markets="true"):
            if not ev.get("mutually_exclusive"):
                continue
            plain = plain_payouts(ev.get("markets", []))
            if not plain and not fair_price_only_if_cancelled(ev.get("markets", [])):
                continue  # the math needs every market to pay exactly $0 or $1
            markets = [m for m in ev.get("markets", []) if m.get("status", "active") in ("active", "open")]
            if sum(price(m, "yes_bid") for m in markets) <= 1:
                continue  # quick filter: no basket can lock in a profit
            first = first_close(markets)
            if not time_left(first):
                continue
            legs = [(m["ticker"], *ctx.client.best_bid(m["ticker"], "yes")) for m in markets
                    if m["ticker"] not in ctx.held]
            basket = no_basket(legs, self._budget(ctx))
            if basket:
                sig = to_signal(ev["event_ticker"], ev.get("title", ""), basket)
                sig.closes_at = latest_close(markets)
                sig.meta["first_close"] = first
                if not plain:
                    # A cancelled game settles at a price Kalshi picks: not locked in, so
                    # paper only, to learn how often these show up and fill.
                    sig.meta["paper_only"] = True
                    sig.note += "; paper only: a cancelled game settles at a price Kalshi picks"
                found.append(sig)
        return sorted(found, key=lambda s: -yearly(s))  # money back soonest, per dollar, first

    def recheck(self, ctx, sig):
        if not time_left(sig.meta.get("first_close")):
            return None
        legs = [(leg.ticker, *ctx.client.best_bid(leg.ticker, "yes")) for leg in sig.legs]
        basket = no_basket(legs, self._budget(ctx))
        if not basket:
            return None
        fresh = to_signal(sig.event, sig.title, basket)
        fresh.closes_at = sig.closes_at
        fresh.meta["first_close"] = sig.meta.get("first_close")
        return fresh


STRATEGY = BasketArb()
