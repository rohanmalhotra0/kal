"""Locked-in data links: "exactly X%" vs "above X%" on the same economic number.

Kalshi asks about one number (CPI, core CPI, their 12-month rates, the unemployment rate)
in two series:
  - KXECONSTAT*: "Is it exactly X%?" One market per value, at most one YES.
  - KXCPI, KXCPICORE, KXCPIYOY, KXCPICOREYOY, KXU3: "Is it above X%?" A ladder.
The number is published to one decimal, so for each listed value X exactly one of
"exactly X", "above X" and "not above X - 0.1" is true. That gives:
  YES "exactly X" + YES "above X" + NO "above X - 0.1":  exactly one pays $1;
  YES "above X - 0.1" + NO "exactly X":                   at least one pays $1;
  NO "above X" + NO "exactly X":                          at least one pays $1.

Real Kalshi data (docs/RESEARCH.md, idea #17, 2026-10-29): of 533 sets that settled, every
one paid exactly $1. 84 sets had hours with a gap after fees that passes the bot's rules
(1,032 hours in all). Prices came from hourly candles, which show no depth.

Paper only. In a month whose number never comes out (the 2025 shutdown cancelled October's
CPI and jobs reports), the two series follow different rules. An "exactly" market expires
at most three months after the planned release, and if no value comes, Kalshi sets the
payouts. An "above" market waits up to six months after the shutdown, and Kalshi settled
October 2025's on values it chose. Then the first set can pay $0. Real money only goes to a
release a person pinned in VERIFIED, with every market's rules word for word and its dates
(a moved release is back on paper), after checking that no shutdown threatens it, and even
then never while a link looks too good to be true (too_good, at scan and again just before
buying). It is empty.

It fails closed. A pair of events trades only if:
  - every market reads exactly as Kalshi words them now, with only the number and the
    dates different, and pays plainly ($0 or $1);
  - both events name the same month, each closes at one time, and they close on the same
    day minutes apart; the "exactly" event is mutually exclusive and the ladder is not;
  - every value is on the 0.1 grid, appears once, and agrees with its ticker.
Anything odd, or anything it can't read: no signals.
"""
import datetime
import re
import time

from .. import risk
from ..client import price
from .base import Leg, Signal, Strategy, plain_payouts
from .ladder_arb import MIN_TIME_LEFT, allocate, close_ts, locked_basket, quote, strike, thinnest_first, tradeable

MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_MON = r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
_MONTH = (r"(?P<month>january|february|march|april|may|june|july|august|september|october|november"
          r"|december)")
_X = r"(?P<x>-?\d{1,2}\.\d)"
_SHUTDOWN = (r"in the case of a delay in data caused by a federal government shutdown impacting the reliability "
             r"of the source agency, the market's latest expiration date will be extended to the sooner of the "
             r"release of the underlying or six months after the end of the government shutdown")
SAME_RELEASE = 900   # seconds: both events close this close together (4 minutes apart, real)

# The wording on the real markets (2026-10-29): only the number, the month and the release
# date may differ. Kalshi lists two forms. Events listed before mid-September 2026 say "is
# exactly 0.2% in Oct 2026", carry the data month in the event ticker (KXECONSTATCPI-26OCT)
# and are "custom" strikes (T0.2). Newer ones, from December's data on, say "for Dec 2026 is
# exactly 0.2%", carry the release day (KXECONSTATCPI-27JAN13), are "between" strikes (E0.2),
# and core CPI's add a definition. KXCPI's December ladder is worded anew too.
_KXCPI_OCT = (rf"if the consumer price index \(cpi\) increases by more than {_X}% \(single-decimal\) in {_MONTH} "
              rf"(?P<year>\d{{4}}), then the market resolves to yes\.",
              rf"the market will always close at 8:29 am et on the scheduled day of the data release "
              rf"\((?P<release>[a-z]+ \d{{1,2}}, \d{{4}})\)\. please note: the expiration value is the "
              rf"single-decimal value published at the source agency\. {_SHUTDOWN}\.")
_KXCPI_DEC = (rf"if the consumer price index \(cpi\) increases by more than {_X}% in {_MONTH} (?P<year>\d{{4}}), as "
              rf"reported to one decimal place, then the market resolves to yes\.",
              rf"please note: the expiration value is the single-decimal value published at the source agency\. "
              rf"{_SHUTDOWN}\.")
_MEANS = (r'"{0}" means the {1} percent change in the cpi-u for all items less food and energy, as first reported '
          r"by the bureau of labor statistics and rounded to one decimal place\. subsequent revisions will not "
          r"affect settlement\.")
# "Exactly" series -> its ladder; what its rules call the number in the old and the new form
# (None: no new form seen yet); the new form's rules_secondary; the ladder's (rules_primary,
# rules_secondary) forms.
SERIES = {
    "KXECONSTATCPI": {
        "ladder": "KXCPI", "old": "the cpi month-over-month", "new": "the cpi month-over-month", "means": "",
        "forms": [_KXCPI_OCT, _KXCPI_DEC]},
    "KXECONSTATCPICORE": {
        "ladder": "KXCPICORE", "old": "the cpi core month-over-month", "new": "the cpi core month-over-month",
        "means": _MEANS.format("cpi core", "seasonally adjusted one-month"),
        "forms": [(rf"if the seasonally adjusted consumer price index for all urban consumers: all items less food "
                   rf"and energy for {_MONTH} (?P<year>\d{{4}}), as published by the bureau of labor statistics, "
                   rf"increases by above {_X}%, then the market resolves to yes\.",
                   rf"please note that the value of the underlying is the single-decimal value reported by the "
                   rf"bls\. {_SHUTDOWN}\.")]},
    "KXECONSTATCPIYOY": {
        "ladder": "KXCPIYOY", "old": "the cpi year-over-year", "new": "the cpi year-over-year", "means": "",
        "forms": [(rf"if the consumer price index \(cpi\) increases by more than {_X}% in the twelve months ending "
                   rf"{_MONTH} (?P<year>\d{{4}}) \(as represented by the one-decimal place value reported by the "
                   rf"bureau of labor statistics\), then the market resolves to yes\.",
                   rf"{_SHUTDOWN}\.?")]},
    "KXECONSTATCORECPIYOY": {
        "ladder": "KXCPICOREYOY", "old": "the cpi core year-over-year", "new": "cpi core year-over-year",
        "means": _MEANS.format("core cpi", "12-month"),
        "forms": [(rf"if the consumer price index \(cpi\) for all urban consumers: all items less food and energy "
                   rf"increases by more than {_X}% in the twelve months ending {_MONTH} (?P<year>\d{{4}}) \(as "
                   rf"represented by the one-decimal place value reported by the bureau of labor statistics\), then "
                   rf"the market resolves to yes\.",
                   rf"{_SHUTDOWN}\.")]},
    "KXECONSTATU3": {
        "ladder": "KXU3", "old": "the unemployment rate", "new": None, "means": "",
        "forms": [(rf"if the seasonally adjusted unemployment rate \(u-3\) reported by the bureau of labor "
                   rf"statistics in the employment situation report is above {_X}% in {_MONTH} "
                   rf"(?P<year>\d{{4}}), then the market resolves to yes\.", r"")]},
}

# Real money only for these: "exactly" event ticker -> {"above": its ladder's event ticker,
# "rules": rules_key(...)}. Add one only by pull request, after reading every market's rules
# in full and checking that no government shutdown threatens that release. Take it out if
# one comes. Empty: every release trades on paper.
VERIFIED = {}
UNVERIFIED = "no release checked by hand yet (exact_link_arb.VERIFIED is empty)"
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def _plain(text):
    """Lowercase, one line, single spaces, straight quotes."""
    return " ".join(str(text or "").translate(_QUOTES).split()).lower()


def _tenths(text):
    return round(float(text) * 10)


def _utc_day(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date()


def _one_close(markets):
    """The one close time all these markets share, or None."""
    times = {close_ts(m) for m in markets}
    return times.pop() if len(times) == 1 and None not in times else None


def _markets(ev, exclusive):
    """The event's markets if it is the right kind (mutually exclusive or not), every market
    is a dict of its own and pays plainly. Else None."""
    markets = ev.get("markets") if isinstance(ev, dict) else None
    if ev.get("mutually_exclusive") is not exclusive or not isinstance(markets, list) or not markets:
        return None
    if not all(isinstance(m, dict) for m in markets) or not plain_payouts(markets):
        return None
    return markets


def _suffix(day, with_day=False):
    """The ticker part Kalshi gives a month or a day: Oct 2026 -> "26OCT", Jan 13, 2027 -> "27JAN13"."""
    return f"{day.year % 100:02d}{MONTHS[day.month - 1].upper()}" + (f"{day.day:02d}" if with_day else "")


def _exact_market(m, et, info):
    """(form, value text, year, month) of one "exactly" market, or None if its wording, strike
    or ticker is off. The old form: "is exactly 0.2% in Oct 2026", a custom strike, T0.2. The
    new: "for Dec 2026 is exactly 0.2%", a between strike from 0.2 to 0.2, E0.2."""
    rules, second = _plain(m.get("rules_primary")), _plain(m.get("rules_secondary"))
    g = re.fullmatch(rf"if {re.escape(info['old'])} is exactly {_X}% in {_MON} (?P<year>\d{{4}}), then the market "
                     r"resolves to yes\.", rules)
    if g and not second and m.get("strike_type") == "custom" and m.get("custom_strike") == {"Value": g["x"]} \
            and m.get("ticker") == f"{et}-T{g['x']}":
        return "old", g["x"], int(g["year"]), MONTHS.index(g["mon"]) + 1
    g = info["new"] and re.fullmatch(rf"if {re.escape(info['new'])} for {_MON} (?P<year>\d{{4}}) is exactly {_X}%, "
                                     r"then the market resolves to yes\.", rules)
    if not g or not re.fullmatch(info["means"], second) or m.get("strike_type") != "between":
        return None
    lo, hi = strike(m, "floor_strike"), strike(m, "cap_strike")
    if lo is None or hi is None or abs(lo * 10 - _tenths(g["x"])) > 1e-9 or abs(hi * 10 - _tenths(g["x"])) > 1e-9:
        return None   # from the value to the value: exactly it
    if m.get("custom_strike") not in (None, {}) or m.get("ticker") != f"{et}-E{g['x']}":
        return None
    return "new", g["x"], int(g["year"]), MONTHS.index(g["mon"]) + 1


def exact_event(ev, series):
    """What the links need from one "exactly" event: {value in tenths: market}, its month
    and close. None if anything about it is off, or its markets mix the two forms."""
    et = str(ev.get("event_ticker") or "")
    markets = _markets(ev, True)
    if not markets or not et.startswith(series + "-") or ev.get("series_ticker") not in (None, series):
        return None
    values, seen = {}, set()
    for m in markets:
        got = _exact_market(m, et, SERIES[series])
        if not got or m.get("event_ticker") not in (None, et) or _plain(m.get("yes_sub_title")) != f"exactly {got[1]}%":
            return None
        form, x, year, month = got
        if _tenths(x) in values:
            return None
        values[_tenths(x)] = m
        seen.add((form, year, month))
    close = _one_close(markets)
    if len(seen) != 1 or close is None:
        return None
    form, year, month = seen.pop()
    # The old form's ticker names the data month; the new form's, the release day (its close).
    want = _suffix(datetime.date(year, month, 1)) if form == "old" else _suffix(_utc_day(close), True)
    if et != f"{series}-{want}":
        return None
    return {"event": et, "title": ev.get("title") or et, "values": values, "month": (year, month), "close": close}


def above_event(ev, series):
    """What the links need from one "above" ladder: {strike in tenths: market}, its month
    and close. None if anything about it is off."""
    et = str(ev.get("event_ticker") or "")
    markets = _markets(ev, False)
    if not markets or not et.startswith(series + "-") or ev.get("series_ticker") not in (None, series):
        return None
    forms = [tuple(map(re.compile, f)) for f in next(v["forms"] for v in SERIES.values() if v["ladder"] == series)]
    strikes, months, releases = {}, set(), set()
    for m in markets:
        rules, second = _plain(m.get("rules_primary")), _plain(m.get("rules_secondary"))
        g, g2 = next(((a.fullmatch(rules), b.fullmatch(second)) for a, b in forms
                      if a.fullmatch(rules) and b.fullmatch(second)), (None, None))
        if not g or m.get("event_ticker") not in (None, et):
            return None
        x, k = g["x"], strike(m, "floor_strike")
        if m.get("strike_type") != "greater" or k is None or abs(k * 10 - _tenths(x)) > 1e-9:
            return None   # the strike must be the number in the rules
        if (m.get("cap_strike") not in (None, "", k) or m.get("ticker") != f"{et}-T{x}"
                or _plain(m.get("yes_sub_title")) != f"above {x}%"):
            return None
        if _tenths(x) in strikes:
            return None
        strikes[_tenths(x)] = m
        months.add((int(g["year"]), MONTHS.index(g["month"][:3]) + 1))
        if "release" in g2.groupdict():
            releases.add(g2["release"])
    close = _one_close(markets)
    if len(months) != 1 or close is None or len(releases) > 1:
        return None
    if releases and _release_day(releases.pop()) != _utc_day(close):
        return None   # it closes on its release day
    year, month = months.pop()
    if et != f"{series}-{_suffix(datetime.date(year, month, 1))}":
        return None
    return {"event": et, "strikes": strikes, "month": (year, month), "close": close}


def _release_day(text):
    g = re.fullmatch(r"([a-z]+) (\d{1,2}), (\d{4})", text)
    try:
        return datetime.date(int(g[3]), MONTHS.index(g[1][:3]) + 1, int(g[2]))
    except (TypeError, ValueError):
        return None


def pairs(events):
    """Every ("exactly" event, its ladder) pair that reads cleanly: same series pair, same
    month, closing together. Each "exactly" event once; anything doubtful is left out."""
    by_ticker = {}
    for ev in events if isinstance(events, list) else []:
        if isinstance(ev, dict) and isinstance(ev.get("event_ticker"), str) and ev["event_ticker"]:
            by_ticker.setdefault(ev["event_ticker"], []).append(ev)
    out = []
    for et, evs in by_ticker.items():
        try:
            pair = _pair(et, evs, by_ticker)
        except (ArithmeticError, AttributeError, LookupError, TypeError, ValueError):
            pair = None   # odd data (a year 0, a date out of range): one event mustn't hide the rest
        if pair:
            out.append(pair)
    return out


def _pair(et, evs, by_ticker):
    series = et.split("-", 1)[0]
    if series not in SERIES or len(evs) != 1:
        return None
    ex = exact_event(evs[0], series)
    if not ex:
        return None
    name = SERIES[series]["ladder"]
    ladder = by_ticker.get(f"{name}-{_suffix(datetime.date(*ex['month'], 1))}") or []
    ab = above_event(ladder[0], name) if len(ladder) == 1 else None
    if not ab or ab["month"] != ex["month"]:
        return None
    if abs(ab["close"] - ex["close"]) > SAME_RELEASE or _utc_day(ab["close"]) != _utc_day(ex["close"]):
        return None
    return {"exact": ex, "above": ab, "series": series, "first_close": min(ab["close"], ex["close"]),
            "last_close": max(ab["close"], ex["close"])}


def links(pair):
    """Every set and pair, each [(market, side)]: one contract of each pays at least $1."""
    out, ex, ab = [], pair["exact"]["values"], pair["above"]["strikes"]
    for x, e in sorted(ex.items()):
        hi, lo = ab.get(x), ab.get(x - 1)
        if hi and lo:
            out.append([(e, "yes"), (hi, "yes"), (lo, "no")])   # exactly one of these is true
        if lo:
            out.append([(lo, "yes"), (e, "no")])                # above X - 0.1 is true when it's X
        if hi:
            out.append([(hi, "no"), (e, "no")])                 # not above X is true when it's X
    return out


def _listed(legs):
    """What the legs cost by the listed prices, before fees: a quick look before paying for
    order books. 2 if a price is missing."""
    total = 0.0
    for m, side in legs:
        p = price(m, "yes_ask") if side == "yes" else round(1 - price(m, "yes_bid"), 4)
        if not 0 < p < 1:
            return 2.0
        total += p
    return total


def too_good(all_links, quoted):
    """True if any link costs less than 1 / (1 + risk.MAX_ARB_ROI), listed or in its order
    book: too good to be true (a misread, or a release in doubt). Then even a pinned
    release trades on paper only."""
    floor = 1 / (1 + risk.MAX_ARB_ROI)
    if any(_listed(legs) < floor for legs in all_links):
        return True
    return any(all(q[2] is not None for q in quotes) and sum(q[2] for q in quotes) < floor
               for _, quotes in quoted)


def rules_key(pair):
    """The rules VERIFIED pins: every market of both events, word for word (spacing aside),
    and its dates, so a release that moves (a shutdown) goes back to paper."""
    markets = list(pair["exact"]["values"].values()) + list(pair["above"]["strikes"].values())
    return "\n".join(f"{m['ticker']} {m.get('close_time')} {m.get('expected_expiration_time')}: "
                     f"{' '.join(str(m.get('rules_primary') or '').split())} | "
                     f"{' '.join(str(m.get('rules_secondary') or '').split())}"
                     for m in sorted(markets, key=lambda m: m["ticker"]))


def _month(month):
    year, mo = month
    return f"{MONTHS[mo - 1].capitalize()} {year}"


def to_signal(event, title, basket, meta, closes_at):
    c = basket["count"]
    legs = [Leg(t, side, p, c) for t, side, p, _ in thinnest_first(basket["quotes"])]
    what = " + ".join(f"{leg.side.upper()} {leg.ticker}" for leg in legs)
    sig = Signal("exact_link_arb", event, title, legs, payout_min=c * 1.0, meta=dict(meta), note=(
        f"buy {c} of each: {what}. Pays at least ${c:.2f} when the {meta['month']} number comes out."))
    sig.exp_profit = sig.payout_min - sig.cost
    sig.closes_at = closes_at
    return sig


class ExactLinkArb(Strategy):
    name = "exact_link_arb"
    title = "Exact value vs above"
    kind = "arbitrage"
    description = ("Kalshi asks about one number (CPI, jobs) twice: is it exactly X%, and is it "
                   "above X%? They must agree. When a set that must pay $1 costs less, buy the set. "
                   "Paper only: a report that never comes out could break it.")

    def proven(self, ctx):
        if VERIFIED:
            return True, "only releases a person checked by hand (VERIFIED)"
        return False, UNVERIFIED

    def _budget(self, ctx):
        return min(ctx.budget_left, float(ctx.cfg["max_per_arb"]))

    def scan(self, ctx):
        # The same download as the other arbitrage strategies: the run fetches it once.
        candidates = []
        for pair in pairs(ctx.client.events(status="open", with_nested_markets="true")):
            if pair["first_close"] - time.time() <= MIN_TIME_LEFT:
                continue
            every = links(pair)
            hot = [legs for legs in every if all(tradeable(m, ctx) for m, _ in legs) and _listed(legs) < 1]
            books = {}
            for legs in hot:
                for m, _ in legs:
                    if m["ticker"] not in books:
                        books[m["ticker"]] = ctx.client.orderbook(m["ticker"])
            quoted = [(legs, [(m["ticker"], side, *quote(books[m["ticker"]], side)) for m, side in legs])
                      for legs in hot]
            ex, ab = pair["exact"], pair["above"]
            pin = VERIFIED.get(ex["event"])
            checked = (isinstance(pin, dict) and pin.get("above") == ab["event"] and pin.get("rules") == rules_key(pair)
                       and not too_good(every, quoted))
            for legs, quotes in quoted:
                if not checked and any(m["ticker"] in ctx.held_paper for m, _ in legs):
                    continue   # paper only, and already open on paper: the engine would skip it
                meta = {"exact_event": ex["event"], "above_event": ab["event"], "month": _month(ex["month"]),
                        "link": " + ".join(f"{side.upper()} {m['ticker']}" for m, side in legs),
                        "paper_only": not checked, "first_close": pair["first_close"]}
                candidates.append((quotes, lambda bk, meta=meta, pair=pair: to_signal(
                    pair["exact"]["event"], pair["exact"]["title"], bk, meta, pair["last_close"])))
        return allocate(ctx, candidates)

    def recheck(self, ctx, sig):
        first = sig.meta.get("first_close")
        if not isinstance(first, (int, float)) or first - time.time() <= MIN_TIME_LEFT:
            return None   # too close to the release to buy every leg
        if any(leg.ticker in ctx.held for leg in sig.legs):
            return None   # bought by an earlier signal this run
        quotes = [(leg.ticker, leg.side, *quote(ctx.client.orderbook(leg.ticker), leg.side)) for leg in sig.legs]
        if any(q[2] is None for q in quotes) or sum(q[2] for q in quotes) < 1 / (1 + risk.MAX_ARB_ROI):
            return None   # too good to be true now: scan would keep the release on paper
        basket = locked_basket(quotes, self._budget(ctx), float(ctx.cfg["max_per_trade"]))
        if not basket:
            return None
        return to_signal(sig.event, sig.title, basket, sig.meta, sig.closes_at)


STRATEGY = ExactLinkArb()
