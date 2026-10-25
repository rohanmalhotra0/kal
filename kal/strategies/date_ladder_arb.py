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
flips. We only trust an event where:
  - every market reads exactly "Before <date>", each with its own date;
  - every market's rules say "If <it happens> before <that same date>, then the market
    resolves to Yes", with no "not", "still", "in effect" and the like: those flip which
    date implies which. "After issuance" windows pair only when the later market was listed
    no later than the earlier one;
  - the rules and custom_strike match across markets once each market's own deadline is
    taken out (any other date must be the same everywhere).
Anything odd and the event is skipped. The lock rests on reading rules text, so real money
also needs a person to check it first (strategies.date_ladder_arb.verified), as bracket_arb does.
"""
import datetime
import re
import time

from ..client import ts
from .base import Leg, Signal, Strategy
from .ladder_arb import (MIN_TIME_LEFT, allocate, close_ts, locked_basket, looks_inverted, own_markets,
                         quote, thinnest_first, tradeable)

UNVERIFIED = "date ladders not yet checked by hand on live data"
_MONTHS = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
           r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
BEFORE = re.compile(rf"^\s*before\s+(?:({_MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+)?((?:19|20)\d\d)\s*$",
                    re.I)
# Dates in rules text: "Sep 1, 2026", "September 1st 2026", "September 2026", "before 2028".
# Only a market's own deadline is taken out before comparing; other numbers and dates stay.
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
_AT_TIME = re.compile(r"\s+at\s+12:00\s*am\s+(?:et|est|edt)\s*$", re.I)


def _value_date(v):
    """A date written any way Kalshi writes one in custom_strike: "Sep 1, 2026", "Before Sep 1,
    2026", "2026-09-01", "2026-09-01T14:00:00.000Z" or "26Sep01". None if it isn't one."""
    v = _AT_TIME.sub("", v)   # "before December 1, 2026 at 12:00 AM ET": midnight starting that day
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


def same_about(markets):
    """True if the markets are about the same thing besides their deadlines, by custom_strike.
    Kalshi puts the deadline there too, written many ways: {"Date": "Sep 1, 2026", "Player":
    "Kade Anderson"}, {"deadline": "2026-10-15"}, {"Policy": "27Jan01"}. A value the same on
    every market is fine as it is. One that differs must be each market's own deadline."""
    customs = [m.get("custom_strike") or {} for m in markets]
    if not all(isinstance(cs, dict) for cs in customs):
        return len({repr(cs) for cs in customs}) == 1
    for k in set().union(*customs):
        values = [cs.get(k) for cs in customs]
        if all(v == values[0] for v in values):
            continue
        if not all(isinstance(v, str) and _value_date(v) == deadline(m) for m, v in zip(markets, values)):
            return False   # another player, team or thing, or a date that disagrees
    return True


def _rules(m):
    """A market's rules with only its own deadline taken out ("on or before Aug 31" counts as
    before Sep 1), or None if it has no rules. Every other date stays, so markets whose windows
    start on different days ("between <start> and <deadline>") don't look alike."""
    text = f"{m.get('rules_primary') or ''} {m.get('rules_secondary') or ''}".strip().lower()
    if not text:
        return None
    own = deadline(m)
    near = {own, own - datetime.timedelta(days=1)} if own else set()
    text = _DATES.sub(lambda g: "<deadline>" if _value_date(g.group(0)) in near else g.group(0), text)
    return re.sub(r"\s+", " ", text)


# The one wording trusted: "If <it happens> before <this market's deadline>, then the market
# resolves to Yes." A negation ("has not released") or a state ("is still", "in effect", "as
# of") breaks which date implies which, so those are refused.
_CLAIM = re.compile(r"^\s*if\s+(?P<thing>.+?)\s+before\s+(?P<date>[^,.]+(?:,\s*(?:19|20)\d\d)?)\s*[,.]\s*"
                    r"(?:then\s+)?the market (?:will )?resolves? to yes\b", re.I | re.S)
_NOT_A_HAPPENING = re.compile(r"\b(?:not|no longer|never|cannot|without|fails?|failed|still|remains?|remained"
                              r"|continues?|continued|in effect|as of|until|unless)\b|n't\b", re.I)
# "after issuance and before <deadline>": each market counts from when it was listed. That is
# nested only if the later deadline's market was listed no later than the earlier one's. Any
# word that could point at the market's own listing counts: a false alarm only adds that check.
_FROM_LISTING = re.compile(r"\b(?:issuance|issued|issue date|listing|listed|launch(?:ed|es)?|creat(?:ion|ed)"
                           r"|this market|the market)\b", re.I)


def _claim(m):
    g = _CLAIM.match(m.get("rules_primary") or "")
    if not g or _NOT_A_HAPPENING.search(g["thing"]) or _value_date(g["date"].strip()) != deadline(m):
        return None
    return g


def claims_it_happens_before(m):
    """True if the market's rules read "If <it happens> before <its own deadline>, then the
    market resolves to Yes", and <it happens> is a plain event, not a negation or a state."""
    return _claim(m) is not None


def counts_from_listing(m):
    """True if the market only counts what happens after it was listed ("after issuance")."""
    g = _claim(m)
    return bool(g) and bool(_FROM_LISTING.search(g["thing"]))


def _opened(m):
    try:
        return ts(m["open_time"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def nested(earlier, later):
    """True if YES on `earlier` must mean YES on `later`. With "after issuance" windows, only when
    the later market was listed no later than the earlier one: else something between the two
    listings is YES on the earlier market only, and the pair pays nothing."""
    if not (counts_from_listing(earlier) or counts_from_listing(later)):
        return True
    a, b = _opened(earlier), _opened(later)
    return a is not None and b is not None and b <= a


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
    if not same_about(markets) or not all(claims_it_happens_before(m) for m in markets):
        return None
    rules = {_rules(m) for m in markets}
    if len(rules) != 1 or None in rules:
        return None   # the same question at every date, or no trade
    if any(close_ts(m) is None for m in markets):
        return None
    return sorted(markets, key=deadline)


def settles_by(earlier, later):
    """When the $1 is sure to come: when the earlier market closes. Its NO pays then, or the
    thing already happened and the later market closed early with a YES. A later market that
    may not close early (or doesn't say) pays only at its own close."""
    t = close_ts(earlier)
    if later.get("can_close_early") is not True:
        t = max(t, close_ts(later))
    return t


def _day(t):
    d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    return f"{d:%b} {d.day}, {d.year}"


def to_signal(event, title, basket, meta, closes_at):
    c = basket["count"]
    legs = [Leg(t, side, p, c) for t, side, p, _ in thinnest_first(basket["quotes"])]
    sig = Signal("date_ladder_arb", event, title, legs, payout_min=c * 1.0, meta=dict(meta), note=(
        f"buy {c} YES on {meta['later']} and {c} NO on {meta['earlier']}. "
        f"Pays at least ${c:.2f} by {_day(closes_at)}."))
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

    def proven(self, ctx):
        if ctx.params.get("verified") is True:
            return True, "date ladders checked by hand on live data"
        return False, UNVERIFIED

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
                   and tradeable(b, ctx) and nested(a, b) and looks_inverted(a, b)]
            books = {m["ticker"]: ctx.client.orderbook(m["ticker"]) for pair in hot for m in pair}
            for earlier, later in hot:
                e, lt = earlier["ticker"], later["ticker"]
                quotes = [(lt, "yes", *quote(books[lt], "yes")), (e, "no", *quote(books[e], "no"))]
                meta = {"earlier": e, "later": lt,
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
