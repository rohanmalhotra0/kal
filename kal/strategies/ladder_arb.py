"""Locked-in strike-ladder arbitrage.

Some events are a ladder of strikes on one number: "above 50", "above 60",
"above 70". YES on a higher rung means YES on every lower rung. So take a
narrow market A (higher strike) and a broad market B (lower strike). Buying
YES on B and NO on A pays at least $1 whatever happens:
  - A is YES: then B is YES too, and our YES on B pays.
  - A is NO: our NO on A pays.
  (If the value lands between the strikes, both pay: $2.)
So when YES on B plus NO on A costs less than $1 after fees, the profit is
locked in. That only happens when the ladder is upside down: someone bids more
for YES on the narrow market than YES on the broad one is offered at.

"less" ladders ("below 40", "below 50") are the mirror image: there the lower
cap is the narrow market.

The implication holds whether strikes are inclusive or not, as long as every
rung is the same kind of market on the same number. So we only trust events
that look exactly like one ladder: one strike_type, a numeric strike on every
market, no repeated strikes, one close time. Anything odd and the event is
skipped. bracket_arb reuses the helpers here.
"""
import math
import re
import time

from ..client import fee, price, ts
from ..risk import too_late, yearly_return
from .base import Leg, Signal, Strategy, plain_payouts

MIN_PROFIT = 0.05      # dollars per signal, after fees
MIN_ROI = 0.01
MAX_CONTRACTS = 500
MIN_TIME_LEFT = 300    # seconds before close: a basket half-bought at the bell can't be finished
OPEN = ("active", "open")
UP = ("greater", "greater_or_equal")     # YES above floor_strike
DOWN = ("less", "less_or_equal")         # YES below cap_strike


# ---------- helpers (shared with bracket_arb) ----------

def strike(m, field):
    """A finite number from a strike field, else None. Never guess a strike."""
    v = m.get(field)
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def close_ts(m):
    try:
        return ts(m["close_time"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def one_close_time(ev):
    """True if every market closes at the same, readable, not-too-soon time."""
    closes = {close_ts(m) for m in ev.get("markets") or []}
    if len(closes) != 1 or None in closes:
        return False
    return closes.pop() - time.time() > MIN_TIME_LEFT


def own_markets(ev):
    """The event has a ticker, and so does every market, which all belong to it, and every
    market pays plainly ($0 or $1: see base.plain_payouts), which the locked-in math needs."""
    markets = ev.get("markets") or []
    return bool(ev.get("event_ticker")) and plain_payouts(markets) and all(
        m.get("ticker") and m.get("event_ticker") in (None, ev["event_ticker"]) for m in markets)


def _wording(m):
    """A market's YES wording with the numbers taken out. Rungs of one ladder share it
    ("$110,000 or above" -> "$# or above"); different things don't ("Cal Poly wins by
    over 4.5" vs "Utah Tech wins by over 13.5"). None if the market has no wording."""
    text = m.get("yes_sub_title") or m.get("subtitle") or ""
    text = re.sub(r"\d[\d,]*(\.\d+)?", "#", text).strip().lower()
    return re.sub(r"\s+", " ", text) or None


def _ticker_kind(m, event_ticker):
    """The letters in a market's ticker after the event part, digits removed:
    'T' for KXBTCD-..-T110000, but 'CP' vs 'UTU' for a spread's two teams."""
    tail = m["ticker"][len(event_ticker):] if m["ticker"].startswith(event_ticker) else m["ticker"]
    return re.sub(r"[\d.]+", "", tail.lstrip("-")).upper()


def same_underlying(ev):
    """True only if every market is clearly the same question at a different number.

    Kalshi puts different things in one event: both teams' spreads, every player's
    rushing attempts. Same strike_type and same close time don't make those a ladder.
    Real data showed it: "Cal Poly by 4+" vs "Utah Tech by 13+" looked like a $10 ticket
    to $500 "locked in", and was really a near-certain loss. So: same wording (numbers
    removed), same ticker letters, and same custom_strike. Any doubt means no."""
    markets = ev.get("markets") or []
    wordings = {_wording(m) for m in markets}
    if len(wordings) != 1:
        return False
    kinds = {_ticker_kind(m, ev["event_ticker"]) for m in markets}
    if len(kinds) != 1:
        return False
    return same_custom_strike(ev)


def same_custom_strike(ev):
    """Kalshi's custom_strike names the thing a market is about (a team, a player).
    Every market in a ladder or bracket set must name the same thing, or none."""
    customs = {repr(sorted((m.get("custom_strike") or {}).items())) for m in ev.get("markets") or []}
    return len(customs) == 1


def with_close(sig, ev):
    """Stamp a signal with its event's (single) close time."""
    sig.closes_at = close_ts((ev.get("markets") or [{}])[0])
    return sig


def tradeable(m, ctx):
    return m.get("status") in OPEN and m["ticker"] not in ctx.held


def quote(book, side):
    """(price, size) to buy `side` now, or (None, 0). The book lists bids only, so
    the YES ask is 1 - best NO bid, and its size is that bid's size."""
    levels = book.get("no" if side == "yes" else "yes") or []
    if not levels:
        return None, 0
    bid, size = max(levels)
    ask = round(1 - bid, 4)
    if not 0 < bid < 1 or not 0 < ask < 1 or size < 1:
        return None, 0
    return ask, int(size)


def partial_cost(quotes, c):
    """Most we could lose if buying stops part way: the legs bought before the last one,
    in execution order. A partial YES/NO ladder or bracket basket pays nothing for sure."""
    ordered = thinnest_first(quotes)
    return sum(c * p + fee(c, p) for _, _, p, _ in ordered[:-1])


def locked_basket(quotes, budget, max_partial=float("inf")):
    """quotes: [(ticker, side, ask, size)] where one contract of each, together, pays
    at least $1 in every outcome. Pick the count with the most locked-in profit that
    fits the budget, and whose half-built worst case fits max_partial (risk.Limits
    refuses anything bigger), or None if fees eat it or it's too small to bother."""
    if len(quotes) < 2 or any(p is None or s < 1 for _, _, p, s in quotes):
        return None
    best = None
    for c in range(1, min(MAX_CONTRACTS, min(int(s) for *_, s in quotes)) + 1):
        cost = sum(c * p + fee(c, p) for _, _, p, _ in quotes)
        if cost > budget or partial_cost(quotes, c) > max_partial:
            break  # both only grow with c
        if best is None or c - cost > best["profit"]:
            best = {"count": c, "quotes": quotes, "cost": cost, "profit": c - cost}
    if best and best["profit"] >= MIN_PROFIT and best["profit"] / best["cost"] >= MIN_ROI:
        return best
    return None


def thinnest_first(quotes):
    """Leg order. We take the biggest share of the thinnest quote, so it's the leg most
    likely to be gone. Going first means a miss costs nothing: no other leg was bought."""
    return sorted(quotes, key=lambda q: (q[3], q[1] != "no", q[0]))


def allocate(ctx, candidates):
    """candidates: [(quotes, make)] with make(basket) -> Signal. Best first, no ticker in two
    signals (they'd count the same liquidity twice), and together they fit budget_left."""
    cap, partial = float(ctx.cfg["max_per_arb"]), float(ctx.cfg["max_per_trade"])
    left, used, ranked, out = ctx.budget_left, set(), [], []
    for quotes, make in candidates:
        basket = locked_basket(quotes, min(cap, left), partial)
        if basket:
            sig = make(basket)
            if too_late(sig.closes_at, ctx.cfg):
                continue   # Limits.refuse turns it down: it mustn't take budget from the rest first
            ranked.append((yearly(sig), quotes, make))
    for _, quotes, make in sorted(ranked, key=lambda r: -r[0]):
        tickers = {q[0] for q in quotes}
        basket = locked_basket(quotes, min(cap, left), partial)  # resized to what's left
        if tickers & used or not basket:
            continue
        used |= tickers
        left -= basket["cost"]
        out.append(make(basket))
    return sorted(out, key=lambda s: -yearly(s))


def yearly(sig):
    """Rank locked-in trades by yearly return, not dollars: with $300, 40c locked up for six
    months is worse than 20c back by Friday."""
    if not sig.closes_at or sig.cost <= 0:
        return -1.0
    days = max((sig.closes_at - time.time()) / 86400, 1 / 24)
    return yearly_return(sig.payout_min / sig.cost, days)


# ---------- the ladder ----------

def ladder(ev):
    """(strike_type, [(strike, market)] lowest first) if the event is clearly one ladder, else None."""
    markets = ev.get("markets") or []
    if len(markets) < 2 or ev.get("mutually_exclusive") is not False:
        return None  # a ladder can't be mutually exclusive: several rungs win together
    kinds = {m.get("strike_type") for m in markets}
    kind = kinds.pop() if len(kinds) == 1 else None
    if kind in UP:
        field, other = "floor_strike", "cap_strike"
    elif kind in DOWN:
        field, other = "cap_strike", "floor_strike"
    else:
        return None  # mixed types, brackets, or custom rules
    if not own_markets(ev) or not one_close_time(ev) or not same_underlying(ev):
        return None
    rungs = []
    for m in markets:
        s = strike(m, field)
        if s is None or m.get(other) not in (None, ""):
            return None
        rungs.append((s, m))
    if len({s for s, _ in rungs}) != len(rungs):
        return None
    return kind, sorted(rungs, key=lambda r: r[0])


def pairs(kind, rungs):
    """(narrow, broad) markets where YES on narrow means YES on broad."""
    for i in range(len(rungs)):
        for j in range(i + 1, len(rungs)):
            lo, hi = rungs[i][1], rungs[j][1]
            yield (hi, lo) if kind in UP else (lo, hi)


def looks_inverted(narrow, broad):
    """Quick check on the listing before paying for order books."""
    return 0 < price(broad, "yes_ask") < price(narrow, "yes_bid") < 1


def to_signal(event, title, basket, meta):
    c = basket["count"]
    legs = [Leg(t, side, p, c) for t, side, p, _ in thinnest_first(basket["quotes"])]
    yes = next(leg.ticker for leg in legs if leg.side == "yes")
    no = next(leg.ticker for leg in legs if leg.side == "no")
    sig = Signal("ladder_arb", event, title, legs, payout_min=c * 1.0, meta=dict(meta), note=(
        f"buy {c} YES on {yes} and {c} NO on {no}. Pays at least ${c:.2f}."))
    sig.exp_profit = sig.payout_min - sig.cost
    return sig


class LadderArb(Strategy):
    name = "ladder_arb"
    title = "Ladder arbitrage"
    kind = "arbitrage"
    description = ("Some markets are steps on one ladder, like 'above 50' and 'above 60'. "
                   "If the higher step's YES sells for more than the lower step's, buy YES "
                   "low and NO high. One of them always pays $1.")

    def _budget(self, ctx):
        return min(ctx.budget_left, float(ctx.cfg["max_per_arb"]))

    def scan(self, ctx):
        candidates = []
        for ev in ctx.client.events(status="open", with_nested_markets="true"):
            lad = ladder(ev)
            if not lad:
                continue
            kind, rungs = lad
            hot = [(a, b) for a, b in pairs(kind, rungs)
                   if tradeable(a, ctx) and tradeable(b, ctx) and looks_inverted(a, b)]
            books = {m["ticker"]: ctx.client.orderbook(m["ticker"]) for pair in hot for m in pair}
            strikes = {m["ticker"]: s for s, m in rungs}
            for narrow, broad in pairs(kind, rungs):
                n, b = narrow["ticker"], broad["ticker"]
                if n not in books or b not in books:
                    continue
                quotes = [(b, "yes", *quote(books[b], "yes")), (n, "no", *quote(books[n], "no"))]
                meta = {"narrow": n, "broad": b, "strike_type": kind,
                        "strikes": {n: strikes[n], b: strikes[b]}}
                candidates.append((quotes, lambda bk, ev=ev, meta=meta: with_close(to_signal(
                    ev["event_ticker"], ev.get("title", ""), bk, meta), ev)))
        return allocate(ctx, candidates)

    def recheck(self, ctx, sig):
        if any(leg.ticker in ctx.held for leg in sig.legs):
            return None  # bought by an earlier signal this run
        quotes = [(leg.ticker, leg.side, *quote(ctx.client.orderbook(leg.ticker), leg.side))
                  for leg in sig.legs]
        basket = locked_basket(quotes, self._budget(ctx), float(ctx.cfg["max_per_trade"]))
        if not basket:
            return None
        fresh = to_signal(sig.event, sig.title, basket, sig.meta)
        fresh.closes_at = sig.closes_at
        return fresh


STRATEGY = LadderArb()
