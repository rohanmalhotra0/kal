"""YES-basket arbitrage on bracket events. Paper only until a person checks it.

Some events split one number into ranges that cover everything: "below 40",
"40 to 50", "50 to 60", "above 60". Exactly one range wins, so 1 YES on every
range pays exactly $1. If the YES asks plus fees add up to less than $1, the
profit is locked in.

The catch is the edges. A Kalshi number can be whole or decimal, and an edge
can be inside a range or just outside it. If the ranges leave a gap and the
value lands in it, nothing wins and the whole basket is lost. So:
  - the check is strict: one bottom ("less"), ranges ("between") that each start
    exactly where the last one ended, and one top ("greater"). Any gap, overlap,
    extra market or missing strike and the event is skipped.
  - proven() stays False, so the engine keeps this on paper, until a person has
    checked settled events on live data and set "verified": true in its config.
"""
from ..client import price
from .base import Leg, Signal, Strategy
from .ladder_arb import (allocate, locked_basket, one_close_time, own_markets, quote, same_custom_strike,
                         strike, thinnest_first, tradeable)

LOW = ("less", "less_or_equal")          # bottom bracket: YES below cap_strike
HIGH = ("greater", "greater_or_equal")   # top bracket: YES above floor_strike
UNVERIFIED = "exhaustive brackets not yet verified on live data"


def brackets(ev):
    """The event's markets bottom to top if they cover every value with no gap, else None."""
    markets = ev.get("markets") or []
    if len(markets) < 2 or ev.get("mutually_exclusive") is not True:
        return None  # if ranges cover each value once, Kalshi must flag them exclusive
    if not own_markets(ev) or not one_close_time(ev) or not same_custom_strike(ev):
        # Brackets of two different things (two players) also fail the one-bottom,
        # one-top, no-overlap checks below; custom_strike catches the rest.
        return None
    bottom, middle, top = [], [], []
    for m in markets:
        kind, lo, hi = m.get("strike_type"), strike(m, "floor_strike"), strike(m, "cap_strike")
        if kind in LOW and hi is not None and m.get("floor_strike") in (None, ""):
            bottom.append((hi, m))
        elif kind in HIGH and lo is not None and m.get("cap_strike") in (None, ""):
            top.append((lo, m))
        elif kind == "between" and lo is not None and hi is not None and lo < hi:
            middle.append((lo, hi, m))
        else:
            return None
    if len(bottom) != 1 or len(top) != 1:
        return None
    edge = bottom[0][0]
    for lo, hi, _ in sorted(middle, key=lambda b: b[0]):
        if lo != edge:
            return None  # a gap or an overlap
        edge = hi
    if top[0][0] != edge:
        return None
    if not middle and bottom[0][1]["strike_type"] == "less" and top[0][1]["strike_type"] == "greater":
        return None  # "below 50" and "above 50": exactly 50 wins neither
    return [bottom[0][1]] + [m for *_, m in sorted(middle, key=lambda b: b[0])] + [top[0][1]]


def to_signal(event, title, basket, verified):
    c = basket["count"]
    legs = [Leg(t, side, p, c) for t, side, p, _ in thinnest_first(basket["quotes"])]
    sig = Signal("bracket_arb", event, title, legs, payout_min=c * 1.0,
                 meta={"brackets": len(legs), "paper_only": not verified}, note=(
                     f"buy {c} YES on all {len(legs)} ranges. One must win: pays ${c:.2f}."))
    sig.exp_profit = sig.payout_min - sig.cost
    return sig


class BracketArb(Strategy):
    name = "bracket_arb"
    title = "Bracket arbitrage"
    kind = "arbitrage"
    description = ("When ranges cover every result, one range must win. If YES on all of "
                   "them costs less than $1, buy them all. Paper only until checked by hand.")

    def _verified(self, ctx):
        return ctx.params.get("verified") is True

    def proven(self, ctx):
        if self._verified(ctx):
            return True, "edges checked by hand on live data"
        return False, UNVERIFIED

    def scan(self, ctx):
        candidates = []
        for ev in ctx.client.events(status="open", with_nested_markets="true"):
            markets = brackets(ev)
            if not markets or not all(tradeable(m, ctx) for m in markets):
                continue  # we need every range: one we can't buy leaves a hole
            asks = [price(m, "yes_ask") for m in markets]
            if not all(0 < a < 1 for a in asks) or sum(asks) >= 1:
                continue  # quick check on the listing before paying for order books
            quotes = [(m["ticker"], "yes", *quote(ctx.client.orderbook(m["ticker"]), "yes"))
                      for m in markets]
            candidates.append((quotes, lambda bk, ev=ev: to_signal(
                ev["event_ticker"], ev.get("title", ""), bk, self._verified(ctx))))
        return allocate(ctx, candidates)

    def recheck(self, ctx, sig):
        if any(leg.ticker in ctx.held for leg in sig.legs):
            return None
        quotes = [(leg.ticker, "yes", *quote(ctx.client.orderbook(leg.ticker), "yes"))
                  for leg in sig.legs]
        basket = locked_basket(quotes, min(ctx.budget_left, float(ctx.cfg["max_per_arb"])),
                               float(ctx.cfg["max_per_trade"]))
        return to_signal(sig.event, sig.title, basket, self._verified(ctx)) if basket else None


STRATEGY = BracketArb()
