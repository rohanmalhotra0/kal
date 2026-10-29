"""Locked-in Fed links: the rate after a meeting vs the Fed's decision at it.

Kalshi asks two things about each Fed meeting:
  - KXFED, "Above 4.00%": is the top of the Fed's target range above 4.00% after it?
  - KXFEDDECISION: what the Fed does. Five markets, at most one YES: cut more than 25bps
    (C26), cut 25bps (C25), hold (H0), hike 25bps (H25), hike more than 25bps (H26).
Call the rate before the meeting U (the last meeting's KXFED result). A hold keeps the rate
at U, any hike lifts it above U, any cut takes it to U - 0.25 or lower. So:
  - "above U" is YES exactly when the Fed hikes;
  - "above U - 0.25" is YES exactly when it doesn't cut.
That gives sets where exactly one contract pays $1, if the rate moves only at this meeting:
  YES "above U" + YES C26, C25, H0;      NO "above U" + YES H25, H26;
  YES "above U - 0.25" + YES C25, C26;   NO "above U - 0.25" + YES H0, H25, H26.
And pairs that pay at least $1: YES "above K" + NO on a decision that puts the rate above K,
or NO "above K" + NO on one that doesn't (K = U or U - 0.25).
Only K = U and K = U - 0.25. Those links only need to know cut, hold or hike, so they hold
however Kalshi counts a 50bp move (its rules have a typo there). K = U + 0.25 or U - 0.5
would need that, so they are never used. It assumes each move is a multiple of 25bp.

Real Kalshi data (docs/RESEARCH.md, idea #6, 2026-10-29): 14 settled meetings, 0 broken
links. In 2026's meetings, 17 hours had a gap after fees that passes the bot's rules.

Paper only. The links need the rate to move only at this meeting, on its date. An emergency
move between meetings makes U wrong, and the settled history can't show it. If the meeting is
cancelled or moved, "Fed maintains rate" resolves YES whatever the Fed does, so a rate change
on the new date breaks four of the shapes (NO "above U" + NO hold among them). And the
decision rules have a typo. Real money only ever goes to a meeting a person pinned in
VERIFIED, with its U, its rules word for word and its dates (a moved meeting is back on
paper), and even then never while a link looks too good to be true (too_good, at scan and
again just before buying: the sign U is wrong). It is empty.

It fails closed. It looks at the next meeting only, and trades it only if:
  - no other Fed event comes first, and the last meeting settled cleanly (one clear rate)
    less than 75 days before. Scheduled meetings two apart are 84+ days apart, so a
    meeting that closed but hasn't settled yet can't slip through;
  - every market reads exactly as Kalshi words them now, with only the date and strike
    different, and pays plainly ($0 or $1);
  - the decision event has exactly the five codes.
Anything odd, or anything it can't read: no signals.
"""
import datetime
import re
import time

from .. import risk
from ..client import price, settled_result
from .base import Leg, Signal, Strategy, plain_payouts
from .ladder_arb import (MIN_TIME_LEFT, allocate, close_ts, locked_basket, quote, strike, thinnest_first,
                         tradeable)

RATE, DECISION = "KXFED", "KXFEDDECISION"
CODES = ("C26", "C25", "H0", "H25", "H26")
# Each decision market's yes_sub_title, as on the real markets (2026-10-29).
LABELS = {"C26": "Cut >25bps", "C25": "Cut 25bps", "H0": "Fed maintains rate", "H25": "Hike 25bps",
          "H26": "Hike >25bps"}
# K, in quarter points from U -> the decisions that leave the rate above K. Only these two K.
ABOVE = {0: ("H25", "H26"), -1: ("H0", "H25", "H26")}
MAX_GAP_DAYS = 75     # U from a meeting this long before the next one is too old
SAME_MEETING = 3600   # seconds: both events close within this of each other (4 minutes, real)
DAY = 86400

# Real money only for these: rate event ticker -> {"rate_before": U, "rules": rules_key(...)}.
# Add one only after reading every market's rules in full, by pull request. A pin covers its
# meeting only while it's held on its date with the rate at the pinned U: take it out if the
# Fed moves between meetings. A moved date changes rules_key, so that pin stops by itself.
# Empty: every meeting trades on paper.
VERIFIED = {}
UNVERIFIED = "no Fed meeting checked by hand yet (fed_link_arb.VERIFIED is empty)"

MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
          r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_DATE = rf"{_MONTH}\.? \d{{1,2}}, (?:19|20)\d\d"    # "Dec 9, 2026", "December 09, 2026"
_RATE_EVENT = re.compile(r"KXFED-(\d\d[A-Z]{3})")
_DECISION_EVENT = re.compile(r"KXFEDDECISION-(\d\d[A-Z]{3})")
# The wording on the real markets (2026-10-29). Only the strike and the date may differ.
_RATE_RULES = re.compile(
    r"if the upper bound of the target federal funds rate published on the federal reserve's official "
    rf"website is greater than (?P<k>\d{{1,2}}(?:\.\d{{1,4}})?)% following the federal reserve's (?P<d>{_DATE}) "
    r"meeting, then the market resolves to yes\.", re.I)
_RATE_EXPIRY = re.compile(
    r"this market will expire the first 2:05 pm et following the release of a federal reserve statement "
    rf"for their (?P<d>{_DATE}) meeting or one week following the last day of that meeting\.", re.I)
_DECISION_RULES = re.compile(
    rf"if the federal reserve does a (?P<what>[a-z0-9 >]+?) on (?P<d>{_DATE}), then the market resolves to yes\.",
    re.I)
# Word for word, typo and all: it says a 50bp hike resolves "Hike 25 bps" to both Yes and No.
# (The first was surely "Hike >25 bps", which is taken too, in case Kalshi fixes it.)
_DECISION_TERMS = re.compile(
    r'the outcomes are mutually exclusive: at most one can resolve to yes\. for example, a 50-basis-point hike '
    r'resolves "hike >?25 bps" to yes and "hike 25 bps" to no\. if the scheduled fomc meeting is canceled and '
    r'does not occur on its scheduled date, "fed maintains rate" resolves to yes and all other outcomes resolve '
    r'to no\.', re.I)
# What each decision market's own rules say it is, as on the real markets (2026-10-29). Kalshi
# loses the ">" of ">25bps": "Cut >25bps" reads "does a Cut of 25bps" and "Hike >25bps" reads
# "does a Hike of 25bps", so either is taken. The hold market reads "does a Hike of 0bps".
# The links only need cut, hold or hike, which these keep apart.
_MORE = r"(?:>|more than |greater than |over )?"
_OUTCOMES = {
    "C26": rf"cut of {_MORE}25 ?bps",
    "C25": r"cut of 25 ?bps",
    "H0": r"(?:hike|cut) of 0 ?bps",
    "H25": r"hike of 25 ?bps",
    "H26": rf"hike of {_MORE}25 ?bps",
}
_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})


def _plain(text):
    """One line, single spaces, straight quotes."""
    return " ".join(str(text or "").translate(_QUOTES).split())


def _date(text):
    """datetime.date from "Dec 9, 2026" or "December 09, 2026", or None."""
    g = re.fullmatch(r"([a-z]+)\.? (\d{1,2}), (\d{4})", str(text or "").lower())
    try:
        return datetime.date(int(g[3]), MONTHS.index(g[1][:3]) + 1, int(g[2]))
    except (TypeError, ValueError):
        return None


def _utc_day(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date()


def _suffix(day):
    """The ticker part Kalshi gives a meeting: Dec 9, 2026 -> "26DEC"."""
    return f"{day.year % 100:02d}{MONTHS[day.month - 1].upper()}"


def _closes(ev):
    """The close times of an event's markets, or None if it has none or any can't be read."""
    markets = ev.get("markets")
    if not isinstance(markets, list) or not markets or not all(isinstance(m, dict) for m in markets):
        return None
    times = {close_ts(m) for m in markets}
    return None if None in times else times


# ---------- the next meeting ----------

def next_meeting(events):
    """(rate event, decision event) of the next Fed meeting: the first open KXFED event, which
    must have an open KXFEDDECISION event with the same ending (26DEC), the first of those too.
    None if not, or if anything can't be read."""
    found = {RATE: {}, DECISION: {}}
    for ev in events if isinstance(events, list) else []:
        if not isinstance(ev, dict):
            continue
        for series, pattern in ((RATE, _RATE_EVENT), (DECISION, _DECISION_EVENT)):
            g = pattern.fullmatch(str(ev.get("event_ticker") or ""))
            if not g:
                continue
            if ev.get("series_ticker") not in (None, series) or found[series].get(g[1], ev) != ev:
                return None   # another series' event, or two different events for one meeting
            found[series][g[1]] = ev
    order = {}
    for series, evs in found.items():
        order[series] = []
        for s, ev in evs.items():
            closes = _closes(ev)
            if closes is None:
                return None   # can't tell when it is, so can't tell which meeting is next
            order[series].append((min(closes), s))
        order[series].sort()
    rates, decisions = order[RATE], order[DECISION]
    if not rates or not decisions:
        return None
    s = rates[0][1]
    for first in (rates, decisions):
        if first[0][1] != s or (len(first) > 1 and first[1][0] == first[0][0]):
            return None   # the first decision event is another meeting's, or a tie
    return found[RATE][s], found[DECISION][s]


def _rung(m, ev):
    """(strike in quarter points, meeting date) of one KXFED market, or None if its type,
    strike, ticker or wording is off."""
    k = strike(m, "floor_strike")
    if m.get("strike_type") != "greater" or k is None or k < 0 or m.get("cap_strike") not in (None, ""):
        return None
    q = round(k * 4)
    if abs(k * 4 - q) > 1e-9 or m.get("ticker") != f"{ev}-T{k:.2f}" or m.get("event_ticker") not in (None, ev):
        return None   # off the quarter-point grid, or a ticker that disagrees with its strike
    if _plain(m.get("yes_sub_title")).lower() != f"above {k:.2f}%":
        return None
    rules = _RATE_RULES.fullmatch(_plain(m.get("rules_primary")))
    expiry = _RATE_EXPIRY.fullmatch(_plain(m.get("rules_secondary")))
    if not rules or not expiry or abs(float(rules["k"]) - k) > 1e-9:
        return None
    day = _date(rules["d"])
    return (q, day) if day and _date(expiry["d"]) == day else None


def _decision(m, ev):
    """(code, meeting date) of one KXFEDDECISION market, or None if its ticker, wording or
    rules are off."""
    t = str(m.get("ticker") or "")
    code = t[len(ev) + 1:] if t.startswith(ev + "-") else None
    if code not in CODES or m.get("event_ticker") not in (None, ev):
        return None
    if _plain(m.get("yes_sub_title")).lower() != LABELS[code].lower():
        return None
    rules = _DECISION_RULES.fullmatch(_plain(m.get("rules_primary")))
    if not rules or not re.fullmatch(_OUTCOMES[code], rules["what"], re.I):
        return None
    if not _DECISION_TERMS.fullmatch(_plain(m.get("rules_secondary"))):
        return None
    day = _date(rules["d"])
    return (code, day) if day else None


def meeting(rate_ev, dec_ev):
    """What the links need about one meeting, or None if anything about its two events is odd:
    flags, codes, strikes, wording, dates or close times."""
    rt, dt = str(rate_ev.get("event_ticker")), str(dec_ev.get("event_ticker"))
    suffix = rt.split("-", 1)[-1]
    if dt.split("-", 1)[-1] != suffix:
        return None
    if rate_ev.get("mutually_exclusive") is not False or dec_ev.get("mutually_exclusive") is not True:
        return None   # a strike ladder has many YES; the decisions at most one
    rates, decisions = rate_ev.get("markets") or [], dec_ev.get("markets") or []
    if not all(isinstance(m, dict) for m in rates + decisions) or not plain_payouts(rates + decisions):
        return None
    rungs = [_rung(m, rt) for m in rates]
    codes = [_decision(m, dt) for m in decisions]
    if not rungs or None in rungs or None in codes or sorted(c for c, _ in codes) != sorted(CODES):
        return None   # exactly the five codes, once each
    if len({q for q, _ in rungs}) != len(rungs):
        return None
    days = {d for _, d in rungs} | {d for _, d in codes}
    if len(days) != 1:
        return None
    day = days.pop()
    rate_close, dec_close = _closes(rate_ev), _closes(dec_ev)
    if _suffix(day) != suffix or not rate_close or not dec_close or len(rate_close) != 1 or len(dec_close) != 1:
        return None
    rc, dc = rate_close.pop(), dec_close.pop()
    if abs(rc - dc) > SAME_MEETING or _utc_day(rc) != day or _utc_day(dc) != day:
        return None   # both close on the meeting day, minutes apart
    return {"day": day, "suffix": suffix, "rate_event": rt, "decision_event": dt,
            "title": rate_ev.get("title") or f"Fed meeting {_day(day)}",
            "strikes": {q: m for (q, _), m in zip(rungs, rates)},
            "decisions": {c: m for (c, _), m in zip(codes, decisions)},
            "first_close": min(rc, dc), "last_close": max(rc, dc)}


# ---------- the rate before it (U) ----------

def rate_before(settled, meet):
    """U, the rate before the meeting, in quarter points (4.00% -> 16): the result of the last
    settled KXFED event, the lowest strike that settled NO, with the highest YES just 0.25
    below it. None unless that event is fully settled, every market is a plain "above K",
    it closed less than MAX_GAP_DAYS before this meeting, and nothing settled after it."""
    events = {}
    for m in settled if isinstance(settled, list) else []:
        if not isinstance(m, dict) or not m.get("ticker"):
            return None
        events.setdefault(str(m.get("event_ticker") or str(m["ticker"]).rsplit("-", 1)[0]), []).append(m)
    when = {}
    for ev, ms in events.items():
        times = [t for t in map(close_ts, ms) if t is not None]
        if not times or ev.split("-", 1)[-1] == meet["suffix"]:
            return None   # a meeting we can't place in time, or this meeting settled already
        when[ev] = max(times)
    if not when:
        return None
    last = max(when.values())
    latest = [ev for ev, t in when.items() if t == last]
    if len(latest) != 1 or not 0 < meet["first_close"] - last < MAX_GAP_DAYS * DAY:
        return None   # a tie, a meeting after this one, or too old
    results = {}
    for m in events[latest[0]]:
        k, res = strike(m, "floor_strike"), settled_result(m)
        if m.get("strike_type") != "greater" or k is None or m.get("cap_strike") not in (None, "") or res is None:
            return None   # not final yet ("determined"), or not a plain "above K"
        q = round(k * 4)
        if abs(k * 4 - q) > 1e-9 or q in results:
            return None
        results[q] = res
    yes = [q for q, r in results.items() if r == "yes"]
    no = [q for q, r in results.items() if r == "no"]
    if not yes or not no or min(no) - max(yes) != 1:
        return None   # no clear rate: all one way, a hole at the rate, or out of order
    return min(no)


# ---------- the links ----------

def links(meet, u):
    """Every set and pair at K = U and K = U - 0.25, each [(market, side)]: one contract of
    each pays at least $1, if the rate moves only at this meeting. [] if either strike is
    missing."""
    out, dec = [], meet["decisions"]
    for off, above in ABOVE.items():
        f = meet["strikes"].get(u + off)
        if f is None:
            return []
        below = [c for c in CODES if c not in above]
        out.append([(f, "yes")] + [(dec[c], "yes") for c in below])   # exactly one of these is YES
        out.append([(f, "no")] + [(dec[c], "yes") for c in above])    # and of these
        out += [[(f, "yes"), (dec[c], "no")] for c in above]           # c YES means f YES
        out += [[(f, "no"), (dec[c], "no")] for c in below]            # c YES means f NO
    return out


def _listed(legs):
    """What the legs cost by the event's listed prices, before fees: a quick look before
    paying for order books. 2 if a price is missing."""
    total = 0.0
    for m, side in legs:
        p = price(m, "yes_ask") if side == "yes" else round(1 - price(m, "yes_bid"), 4)
        if not 0 < p < 1:
            return 2.0
        total += p
    return total


def too_good(all_links, quoted):
    """True if any link costs less than 1 / (1 + risk.MAX_ARB_ROI), by listed prices or by
    its order book: too good to be true. The likeliest reason is a wrong U (the Fed moved
    between meetings, which the settled history can't show). Then even a pinned meeting
    trades on paper only."""
    floor = 1 / (1 + risk.MAX_ARB_ROI)
    if any(_listed(legs) < floor for legs in all_links):
        return True
    return any(all(q[2] is not None for q in quotes) and sum(q[2] for q in quotes) < floor
               for _, quotes in quoted)


def rules_key(meet, u):
    """The rules VERIFIED pins for a meeting: every market the links at U can trade, word
    for word (spacing aside), and its dates, so a meeting that moves goes back to paper."""
    markets = [meet["strikes"][u], meet["strikes"][u - 1]] + [meet["decisions"][c] for c in CODES]
    return "\n".join(f"{m['ticker']} {m.get('close_time')} {m.get('expected_expiration_time')}: "
                     f"{' '.join(str(m.get('rules_primary') or '').split())} | "
                     f"{' '.join(str(m.get('rules_secondary') or '').split())}" for m in markets)


def _day(day):
    return f"{MONTHS[day.month - 1].capitalize()} {day.day}"


def to_signal(event, title, basket, meta, closes_at):
    c = basket["count"]
    legs = [Leg(t, side, p, c) for t, side, p, _ in thinnest_first(basket["quotes"])]
    what = " + ".join(f"{leg.side.upper()} {leg.ticker}" for leg in legs)
    day = datetime.date.fromisoformat(meta["meeting"])
    sig = Signal("fed_link_arb", event, title, legs, payout_min=c * 1.0, meta=dict(meta), note=(
        f"buy {c} of each: {what}. Pays at least ${c:.2f} after the {_day(day)} Fed meeting."))
    sig.exp_profit = sig.payout_min - sig.cost
    sig.closes_at = closes_at
    return sig


class FedLinkArb(Strategy):
    name = "fed_link_arb"
    title = "Fed rate vs decision"
    kind = "arbitrage"
    description = ("Kalshi asks two things about each Fed meeting: the rate after it, and what the "
                   "Fed does (cut, hold or hike). They must agree. When a set that must pay $1 "
                   "costs less, buy the set. Paper only: the rate can move between meetings.")

    def proven(self, ctx):
        if VERIFIED:
            return True, "only Fed meetings a person checked by hand (VERIFIED)"
        return False, UNVERIFIED

    def _budget(self, ctx):
        return min(ctx.budget_left, float(ctx.cfg["max_per_arb"]))

    def scan(self, ctx):
        # The same download as the other arbitrage strategies: the run fetches it once.
        found = next_meeting(ctx.client.events(status="open", with_nested_markets="true"))
        meet = meeting(*found) if found else None
        if not meet or meet["first_close"] - time.time() <= MIN_TIME_LEFT:
            return []
        u = rate_before(ctx.client.settled_markets(RATE), meet)
        if u is None or u not in meet["strikes"] or u - 1 not in meet["strikes"]:
            return []
        hot = [legs for legs in links(meet, u)
               if all(tradeable(m, ctx) for m, _ in legs) and _listed(legs) < 1]
        books = {}
        for legs in hot:
            for m, _ in legs:
                if m["ticker"] not in books:
                    books[m["ticker"]] = ctx.client.orderbook(m["ticker"])
        quoted = [(legs, [(m["ticker"], side, *quote(books[m["ticker"]], side)) for m, side in legs])
                  for legs in hot]
        pin = VERIFIED.get(meet["rate_event"])
        checked = (isinstance(pin, dict) and pin.get("rate_before") == u / 4
                   and pin.get("rules") == rules_key(meet, u) and not too_good(links(meet, u), quoted))
        candidates = []
        for legs, quotes in quoted:
            if not checked and any(m["ticker"] in ctx.held_paper for m, _ in legs):
                continue   # paper only, and already open on paper: the engine would skip it
            meta = {"rate_event": meet["rate_event"], "decision_event": meet["decision_event"],
                    "meeting": meet["day"].isoformat(), "rate_before": u / 4,
                    "strike": strike(legs[0][0], "floor_strike"),
                    "link": " + ".join(f"{side.upper()} {m['ticker']}" for m, side in legs),
                    "paper_only": not checked, "first_close": meet["first_close"]}
            candidates.append((quotes, lambda bk, meta=meta: to_signal(
                meet["rate_event"], meet["title"], bk, meta, meet["last_close"])))
        return allocate(ctx, candidates)

    def recheck(self, ctx, sig):
        first = sig.meta.get("first_close")
        if not isinstance(first, (int, float)) or first - time.time() <= MIN_TIME_LEFT:
            return None   # too close to the bell to buy every leg
        if any(leg.ticker in ctx.held for leg in sig.legs):
            return None   # bought by an earlier signal this run
        quotes = [(leg.ticker, leg.side, *quote(ctx.client.orderbook(leg.ticker), leg.side))
                  for leg in sig.legs]
        if any(q[2] is None for q in quotes) or sum(q[2] for q in quotes) < 1 / (1 + risk.MAX_ARB_ROI):
            return None   # too good to be true now: scan would keep the meeting on paper
        basket = locked_basket(quotes, self._budget(ctx), float(ctx.cfg["max_per_trade"]))
        if not basket:
            return None
        return to_signal(sig.event, sig.title, basket, sig.meta, sig.closes_at)


STRATEGY = FedLinkArb()
