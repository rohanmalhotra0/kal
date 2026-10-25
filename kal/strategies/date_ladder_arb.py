"""Locked-in date-ladder arbitrage.

Some events ask one question with several deadlines: "Before Aug 1, 2026",
"Before Sep 1, 2026". If it happens before an earlier deadline, it happened before
every later one too. So YES on a later deadline plus NO on an earlier one pays at
least $1:
  - it happens before the earlier deadline: the later YES pays;
  - it doesn't: the earlier NO pays once that deadline passes.
  (If it happens between the two deadlines, both pay: $2.)
When the pair costs less than $1 after fees, the profit is locked in. That needs the
ladder upside down: someone bids more for YES on the earlier date than YES on the
later date is offered at.

Real Kalshi data (docs/RESEARCH.md, idea #6, 2026-10-25): rare. 8 hours with a gap
after fees in 88,407 priced pair-hours, in 5 events. None of 131 settled events broke
the order. So it starts on paper.

The deadline comes from each market's own wording ("Before Sep 1, 2026"), never its
close_time: Kalshi rewrites close_time when a market settles early, and the order
flips. We only trust an event where every market reads exactly "Before <date>", each
with its own date, and every market's rules and custom_strike match once the dates are
taken out. Anything odd and the event is skipped.
"""
import datetime
import re
import time

from .base import Leg, Signal, Strategy
from .ladder_arb import (MIN_TIME_LEFT, allocate, close_ts, locked_basket, looks_inverted, own_markets,
                         quote, thinnest_first, tradeable)

_MONTHS = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
           r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
BEFORE = re.compile(rf"^\s*before\s+(?:({_MONTHS})\.?\s+(\d{{1,2}}),?\s+)?((?:19|20)\d\d)\s*$", re.I)
# Dates in rules text, taken out before comparing: "Sep 1, 2026", "September 1st 2026",
# "September 2026", "before 2028". Other numbers stay: "$2,050" vs "$2,060" is another question.
_DATES = re.compile(rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+(?:19|20)\d\d)?\b"
                    rf"|\b(?:{_MONTHS})\.?,?\s+(?:19|20)\d\d\b"
                    r"|(?<=before )(?:19|20)\d\d\b", re.I)


def _date(text, before=True):
    """The date in "Before Sep 1, 2026" ("Before 2028" is Jan 1, 2028), or None. With
    before=False, a bare "Sep 1, 2026" counts too."""
    g = BEFORE.match(text or "") or (None if before else BEFORE.match(f"before {text or ''}"))
    if not g:
        return None
    month, day, year = g.groups()
    try:
        return datetime.date(int(year), MONTHS[month[:3].lower()] if month else 1, int(day) if day else 1)
    except ValueError:
        return None   # "Before Feb 30, 2026"


_ISO = re.compile(r"^\s*((?:19|20)\d\d)-(\d\d)-(\d\d)(?:[T ][\d:.]+(?:Z|[+-]\d\d:?\d\d)?)?\s*$")
_SHORT = re.compile(r"^\s*(\d\d)([a-z]{3})(\d\d)\s*$", re.I)    # "27Jan01", Kalshi's ticker style


def _value_date(v):
    """A date written any way Kalshi writes one in custom_strike: "Sep 1, 2026", "Before Sep 1,
    2026", "2026-09-01", "2026-09-01T14:00:00.000Z" or "26Sep01". None if it isn't one."""
    d = _date(v, before=False)
    if d is not None:
        return d
    g, short = _ISO.match(v), _SHORT.match(v)
    try:
        if g:
            return datetime.date(int(g[1]), int(g[2]), int(g[3]))
        if short and short[2].lower() in MONTHS:
            return datetime.date(2000 + int(short[1]), MONTHS[short[2].lower()], int(short[3]))
    except ValueError:
        pass
    return None


def deadline(m):
    """The date in a market's wording, "Before Sep 1, 2026", or None."""
    return _date(m.get("yes_sub_title") or m.get("subtitle"))


def _about(m):
    """What the market is about besides its deadline, from its custom_strike, comparable across
    markets. Kalshi puts the deadline there too, written many ways: {"Date": "Sep 1, 2026",
    "Player": "Kade Anderson"}, {"deadline": "2026-10-15"}, {"Policy": "27Jan01"}. None if a
    date there disagrees with the wording: then trust neither."""
    cs = m.get("custom_strike") or {}
    if not isinstance(cs, dict):
        return None
    out = []
    for k, v in sorted(cs.items()):
        d = _value_date(v) if isinstance(v, str) else None
        if d is not None:
            if d != deadline(m):
                return None
            v = "<deadline>"
        out.append((k, v))
    return repr(out)


def _rules(m):
    """A market's rules with every date taken out, or None if it has no rules."""
    text = f"{m.get('rules_primary') or ''} {m.get('rules_secondary') or ''}".strip().lower()
    if not text:
        return None
    return re.sub(r"\s+", " ", _DATES.sub("<date>", text))


def ladder(ev):
    """The event's markets, earliest deadline first, if it is clearly one date ladder, else None."""
    markets = ev.get("markets") or []
    if len(markets) < 2 or ev.get("mutually_exclusive") is not False:
        return None   # several deadlines can all be YES, so a ladder is never mutually exclusive
    if not own_markets(ev):
        return None
    days = [deadline(m) for m in markets]
    if None in days or len(set(days)) != len(days):
        return None
    about = {_about(m) for m in markets}
    if len(about) != 1 or None in about:
        return None   # another player, team or thing, or a date that disagrees
    rules = {_rules(m) for m in markets}
    if len(rules) != 1 or None in rules:
        return None   # the same question at every date, or no trade
    if any(close_ts(m) is None for m in markets):
        return None
    return sorted(markets, key=deadline)


def settles_by(earlier, later):
    """When the $1 is sure to come: when the earlier market closes. Its NO pays then, or the
    thing already happened and the later market closed early with a YES. A later market that
    can't close early pays only at its own close."""
    t = close_ts(earlier)
    if later.get("can_close_early") is False:
        t = max(t, close_ts(later))
    return t


def to_signal(event, title, basket, meta, closes_at):
    c = basket["count"]
    legs = [Leg(t, side, p, c) for t, side, p, _ in thinnest_first(basket["quotes"])]
    sig = Signal("date_ladder_arb", event, title, legs, payout_min=c * 1.0, meta=dict(meta), note=(
        f"buy {c} YES on {meta['later']} and {c} NO on {meta['earlier']}. "
        f"Pays at least ${c:.2f} by {meta['earlier_date']}."))
    sig.exp_profit = sig.payout_min - sig.cost
    sig.closes_at = closes_at
    return sig


class DateLadderArb(Strategy):
    name = "date_ladder_arb"
    title = "Date-ladder arbitrage"
    kind = "arbitrage"
    description = ("Some markets ask one question with different deadlines, like 'before Aug 1' "
                   "and 'before Sep 1'. If the earlier date's YES sells for more than the later "
                   "date's, buy YES on the later date and NO on the earlier. One of them always pays $1.")

    def _budget(self, ctx):
        return min(ctx.budget_left, float(ctx.cfg["max_per_arb"]))

    def scan(self, ctx):
        now, candidates = time.time(), []
        for ev in ctx.client.events(status="open", with_nested_markets="true"):
            rungs = ladder(ev)
            if not rungs:
                continue
            hot = [(a, b) for i, a in enumerate(rungs) for b in rungs[i + 1:]
                   if min(close_ts(a), close_ts(b)) - now > MIN_TIME_LEFT and tradeable(a, ctx)
                   and tradeable(b, ctx) and looks_inverted(a, b)]
            books = {m["ticker"]: ctx.client.orderbook(m["ticker"]) for pair in hot for m in pair}
            for earlier, later in hot:
                e, lt = earlier["ticker"], later["ticker"]
                quotes = [(lt, "yes", *quote(books[lt], "yes")), (e, "no", *quote(books[e], "no"))]
                when = deadline(earlier)
                meta = {"earlier": e, "later": lt, "earlier_date": f"{when:%b} {when.day}, {when.year}",
                        "deadlines": {e: deadline(earlier).isoformat(), lt: deadline(later).isoformat()}}
                candidates.append((quotes, lambda bk, ev=ev, meta=meta, at=settles_by(earlier, later): to_signal(
                    ev["event_ticker"], ev.get("title", ""), bk, meta, at)))
        return allocate(ctx, candidates)

    def recheck(self, ctx, sig):
        if any(leg.ticker in ctx.held for leg in sig.legs):
            return None   # bought by an earlier signal this run
        quotes = [(leg.ticker, leg.side, *quote(ctx.client.orderbook(leg.ticker), leg.side))
                  for leg in sig.legs]
        basket = locked_basket(quotes, self._budget(ctx), float(ctx.cfg["max_per_trade"]))
        if not basket:
            return None
        return to_signal(sig.event, sig.title, basket, sig.meta, sig.closes_at)


STRATEGY = DateLadderArb()
