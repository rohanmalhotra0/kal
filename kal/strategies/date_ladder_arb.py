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
  - every market's first rules paragraph is just "If <it happens> before <that same date>,
    then the market resolves to Yes", with no "not", "no", "yet to", "still", "in effect",
    "in the 7 days before" and the like: those flip or break which date implies which;
  - nothing in the rules lets the outcome be undone (retracted, rescinded, repealed...);
  - a pair counts only if the later market was listed no later than the earlier one ("after
    issuance" windows start at each market's own listing);
  - the ladder isn't upside down nearly everywhere (a question read the wrong way round is);
  - the rules and custom_strike match across markets once each market's own deadline is
    taken out (any other date must be the same everywhere).
Anything odd and the event is skipped.

Those checks keep paper results clean. They can't make rules text safe for real money: three
rounds of review each found new wordings that read the question the wrong way round ("nothing
is released", "refrains from", "the night before", "vetoed"). So real money only ever goes to
an event in VERIFIED, whose rules a person has read in full, pinned word for word. Any
change to the wording, and the event is back on paper.
"""
import datetime
import re
import time

from ..client import price, ts
from .base import Leg, Signal, Strategy
from .ladder_arb import (MIN_TIME_LEFT, allocate, close_ts, locked_basket, looks_inverted, own_markets,
                         quote, thinnest_first, tradeable)

# Real money only for these: event ticker -> its rules text exactly as rules_key() gives it
# (each market's own deadline taken out). Add one only after reading every market's rules in
# full, by pull request. Empty: every date ladder trades on paper.
VERIFIED = {}
UNVERIFIED = "no date ladder's rules checked by hand yet (date_ladder_arb.VERIFIED is empty)"
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

    def mine(g):
        d = _value_date(g.group(0))
        if d == own:
            return "<deadline>"
        lead = text[max(0, g.start() - 14):g.start()]
        if own and d == own - datetime.timedelta(days=1) and re.search(r"(?:on or before|by|through)\s+$", lead):
            return "<deadline>"   # "on or before Aug 31" is "before Sep 1"
        return g.group(0)
    text = _DATES.sub(mine, text)
    return re.sub(r"\s+", " ", text)


# The one wording trusted: "If <it happens> before <this market's deadline>, then the market
# resolves to Yes." A negation ("has not released") or a state ("is still", "in effect", "as
# of") breaks which date implies which, so those are refused.
_CLAIM = re.compile(r"^\s*if\s+(?P<thing>.+?)\s+before\s+(?P<date>[^,.]+(?:,\s*(?:19|20)\d\d)?)"
                    r"(?:\s+at\s+12:00\s*am\s+(?:et|est|edt))?\s*[,.]\s*"
                    r"(?:then\s+)?the market (?:will )?resolves? to yes"
                    r"(?:\s*\.\s*otherwise,?\s+the market (?:will )?resolves? to no)?\s*\.?\s*$", re.I | re.S)
# Anywhere in the rules: an outcome that can be undone. "Unless retracted before expiration"
# gives a later market longer to undo it, so an earlier YES can come with a later NO.
_UNDONE = re.compile(r"\b(?:retract|withdr[ae]w|revers|revert|overturn|rescind|revok|revocation|repeal|annul"
                     r"|invalidat|void|veto|cancel|called off|pulled)", re.I)
# Refused in <it happens>: negations ("not", "no new album", "yet to", "avoids"), states ("still",
# "in effect", "as of"), and windows that end at each market's own date ("in the 7 days before",
# "within the week before", "on the day before"): those don't nest either.
_NOT_A_HAPPENING = re.compile(
    r"\b(?:not|no|none|nothing|zero|neither|nor|no longer|never|cannot|without|fails?|failed|yet to"
    r"|avoids?|avoided|refrains?|refrained|shortly|immediately|night|period|seven|thirty"
    r"|still|remains?|remained|continues?|continued|in effect|as of|until|unless"
    r"|within (?:the |a |one |\d+ )?(?:last |past |final )?(?:hours?|days?|weeks?|months?|years?))\b|n't\b"
    # "<it happens> in the 7 days | on the day | in the week" right before "before <date>"
    r"|\b(?:the|a|one|few|last|final|\d+)\s+(?:hours?|days?|weeks?|months?|years?)\s*$", re.I)
def _claim(m):
    g = _CLAIM.match(m.get("rules_primary") or "")
    if not g or _NOT_A_HAPPENING.search(g["thing"]) or _value_date(g["date"].strip()) != deadline(m):
        return None
    return g


def claims_it_happens_before(m):
    """True if the market's rules read "If <it happens> before <its own deadline>, then the
    market resolves to Yes", and <it happens> is a plain event, not a negation or a state."""
    return _claim(m) is not None


def _opened(m):
    try:
        return ts(m["open_time"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def nested(earlier, later):
    """True if YES on `earlier` must mean YES on `later`, whatever the rules say about when
    counting starts: only when the later market was listed no later than the earlier one.
    Rules like "after issuance and before <date>" count from each market's own listing, and
    that wording can hide anywhere ("after the contract opens", the second rules paragraph).
    If the later market was listed later, something between the two listings is YES on the
    earlier market only, and the pair pays nothing. Real data: 410 of 3,864 pairs fail this."""
    a, b = _opened(earlier), _opened(later)
    return a is not None and b is not None and b <= a


def rules_key(markets):
    """The rules text VERIFIED pins for an event: the same for every market once its own deadline
    is taken out (ladder() checks that), else None."""
    keys = {_rules(m) for m in markets}
    return keys.pop() if len(keys) == 1 and None not in keys else None


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
    if _UNDONE.search(rules.pop()):
        return None   # an outcome that can be undone later
    if any(close_ts(m) is None for m in markets):
        return None
    return sorted(markets, key=deadline)


def _mid(m):
    bid, ask = price(m, "yes_bid"), price(m, "yes_ask")
    return (bid + ask) / 2 if 0 < ask <= 1 else None


def mostly_upside_down(rungs):
    """True if the price falls at most steps to a later deadline. A real gap is one spot (a fresh
    listing, a stale quote); a question read the wrong way round falls everywhere. Mid-prices,
    so wide spreads can't hide it."""
    mids = [_mid(m) for m in rungs]
    steps = [(a, b) for a, b in zip(mids, mids[1:]) if a is not None and b is not None]
    return len(steps) >= 2 and sum(b < a for a, b in steps) * 2 > len(steps)


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
        if VERIFIED:
            return True, "only events whose rules a person read, word for word (VERIFIED)"
        return False, UNVERIFIED

    def _budget(self, ctx):
        return min(ctx.budget_left, float(ctx.cfg["max_per_arb"]))

    def scan(self, ctx):
        now, candidates = time.time(), []
        for ev in ctx.client.events(status="open", with_nested_markets="true"):
            rungs = ladder(ev)
            if not rungs or mostly_upside_down(rungs):
                continue
            hot = [(a, b) for i, a in enumerate(rungs) for b in rungs[i + 1:]
                   if min(close_ts(a), close_ts(b)) - now > MIN_TIME_LEFT and tradeable(a, ctx)
                   and tradeable(b, ctx) and nested(a, b) and looks_inverted(a, b)]
            books = {m["ticker"]: ctx.client.orderbook(m["ticker"]) for pair in hot for m in pair}
            key = rules_key(rungs)
            checked = key is not None and VERIFIED.get(ev["event_ticker"]) == key   # else paper only
            for earlier, later in hot:
                e, lt = earlier["ticker"], later["ticker"]
                if not checked and (e in ctx.held_paper or lt in ctx.held_paper):
                    continue   # paper only, and already open on paper: the engine would skip it
                quotes = [(lt, "yes", *quote(books[lt], "yes")), (e, "no", *quote(books[e], "no"))]
                meta = {"earlier": e, "later": lt, "paper_only": not checked,
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
