"""Mention words ("What will X say?"): buy YES when a word gets said far more often
than its price implies, NO when far less.

A word's chance is its own hit rate in past events, shrunk toward the series'
average, so a word with little history counts mostly as average. kal.research tests
this rule on past events (walking forward in time, so it never sees the future) and
writes words.json. Trades stay on paper until kal.research.promote sets
words.json "promoted" after paper trading made money too. Riskier than favorites,
so each trade is capped at $5.
"""
import logging

from ..client import fee_per_contract, price
from .base import Leg, Signal, Strategy
from .favorites import in_series, is_open, market_group

PRIOR_WEIGHT = 3      # past events' worth of pull toward the series average
MIN_HISTORY = 3       # need this many past events for a word before trading it
MAX_PER_TRADE = 5.0

log = logging.getLogger(__name__)


def word_of(m):
    """The word or phrase a mention market is about, e.g. "tariff"."""
    for k in ("yes_sub_title", "subtitle"):
        if m.get(k):
            return m[k].strip().lower()
    return m["ticker"].rsplit("-", 1)[-1].lower()


def word_estimate(hits, n, base_rate):
    """Chance the word gets said: its own history shrunk toward the series average."""
    return (hits + PRIOR_WEIGHT * base_rate) / (n + PRIOR_WEIGHT)


def mention_side(est, yes_ask, yes_bid, margin):
    """(side, price) to buy when history and the market disagree by `margin`, else None.
    The NO ask is 1 - YES bid, so a missing bid (0) can never look cheap."""
    if 0 < yes_ask < 0.99 and est - yes_ask >= margin:
        return "yes", round(yes_ask, 4)
    if 0.01 < yes_bid < 1 and yes_bid - est >= margin:
        return "no", round(1 - yes_bid, 4)
    return None


def words_of(research):
    w = (research or {}).get("words")
    return w if isinstance(w, dict) else {}


class Mentions(Strategy):
    name = "mentions"
    title = "Mention words"
    kind = "speculative"
    description = ("Buy YES on words said far more often than the price says, "
                   "NO on words said far less. Uses each word's past events.")

    def scan(self, ctx):
        words = words_of(ctx.research)
        if not words.get("margin"):
            return []
        found = []
        for s in words.get("series") or []:
            try:
                markets = ctx.client.markets(series_ticker=s, status="open")
            except Exception as e:
                log.warning("mentions: markets for %s failed: %s", s, e)
                continue
            for m in markets:
                if not m.get("ticker") or m["ticker"] in ctx.held or not in_series(m, s) or not is_open(m):
                    continue
                # The listing is a cheap first filter; the order book sets the real price and
                # size, so paper results (which decide promotion) match what live could get.
                if self._signal(ctx, words, s, m, price(m, "yes_ask"), price(m, "yes_bid")):
                    sig = self._from_book(ctx, words, s, m)
                    if sig:
                        found.append(sig)
        return sorted(found, key=lambda x: -x.exp_profit)

    def _from_book(self, ctx, words, s, m):
        book = ctx.client.orderbook(m["ticker"])
        yes_bid = max(book.get("yes") or [(0.0, 0.0)])
        no_bid = max(book.get("no") or [(0.0, 0.0)])
        yes_ask = round(1 - no_bid[0], 4) if no_bid[0] else 0.0
        pick = self._signal(ctx, words, s, m, yes_ask, yes_bid[0])
        if not pick:
            return None
        depth = no_bid[1] if pick.legs[0].side == "yes" else yes_bid[1]
        return self._signal(ctx, words, s, m, yes_ask, yes_bid[0], depth)

    def recheck(self, ctx, sig):
        """Re-price from the order book right before a live order."""
        words, leg = words_of(ctx.research), sig.legs[0]
        m = ctx.client.market(leg.ticker)
        s = next((s for s in words.get("series") or [] if m and in_series(m, s)), None)
        if not s or not is_open(m):
            return None
        return self._from_book(ctx, words, s, m)

    def _signal(self, ctx, words, s, m, yes_ask, yes_bid, depth=None):
        word = word_of(m)
        hits, n = (words.get("words") or {}).get(f"{s}|{word}", (0, 0))
        if n < MIN_HISTORY:
            return None
        bh, bn = (words.get("base") or {}).get(s, (0, 0))
        est = word_estimate(hits, n, bh / bn if bn else 0.5)
        pick = mention_side(est, yes_ask, yes_bid, float(words["margin"]))
        if not pick:
            return None
        side, p = pick
        cap = min(MAX_PER_TRADE, ctx.cfg["max_per_trade"], ctx.budget_left)
        count = int(cap // (p + fee_per_contract(p)))
        if depth is not None:
            count = min(count, int(depth))
        while count > 0 and Leg(m["ticker"], side, p, count).cost > cap + 1e-9:
            count -= 1  # fees round up to the cent, so the estimate above can be a bit low
        if count < 1:
            return None
        leg = Leg(m["ticker"], side, p, count)
        if count - leg.cost <= 0:
            return None  # even a win wouldn't cover the cost
        win = est if side == "yes" else 1 - est
        meta = {"paper_only": not words.get("promoted")}
        group = market_group(m, s)
        if group:
            meta["group"] = group  # words said on one day in one series count once in promotion
        sig = Signal(self.name, m.get("event_ticker") or m["ticker"], m.get("title") or m["ticker"], [leg],
                     note=f"{side.upper()} on '{word}': said {hits}/{n} times, history says {est:.0%}",
                     meta=meta)
        sig.exp_profit = count * win - sig.cost
        return sig

    def proven(self, ctx):
        words = words_of(ctx.research)
        lo = words.get("roi_lo")
        if not words.get("promoted"):
            return False, "not proven in paper trading yet"
        if lo is None or lo <= 0:
            return False, "backtest no longer shows an edge"
        return True, f"made money in paper trading; backtest worst case {lo:+.1%}"


STRATEGY = Mentions()
