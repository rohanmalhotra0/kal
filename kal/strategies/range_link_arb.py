"""Locked-in crypto links: "between X and Y" vs "above X" on one price at one moment.

Kalshi asks each crypto price two ways, from the same CF Benchmarks index over the same 60
seconds: a range event ("BTC between 71700-71799.99 at 2 PM?", a bracket per step and a
tail at each end) and an "above" ladder ("BTC above 71699.99 at 2 PM?"). Eight pairs:
KXBTC/KXBTCD, KXETH/KXETHD, KXSOLE/KXSOLD, KXXRP/KXXRPD, KXDOGE/KXDOGED, KXBNB/KXBNBD and
KXHYPE/KXHYPED (hourly and 5 PM), KXSHIBA/KXSHIBAD (5 PM). Both settle on one number. Each
bracket starts one tick above a ladder strike and ends on one, and "above" means strictly
greater. So these sets pay whatever the price, with a bracket's ends counted in or out (the
terms say in; nothing here needs it), and if no data comes and every strike of both events
resolves No:
  superset:  YES "above K" + NO a bracket (or the top tail) above K           pays at least $1
  disjoint:  NO "above K" + NO a bracket (or the bottom tail) at or under K   pays at least $1
  sell3:     NO a bracket + YES "above" under it + NO "above" at its top      pays at least $2
  tail:      YES the top tail + NO the same strike on the ladder              pays $1
Never the two that look alike but aren't safe:
  YES bracket + NO "above" + YES "above" pays only if the price lands on the cent, and the
    rules never say it's rounded (BTC 71799.995 is in no bracket);
  YES "below" + YES "above" loses both if no data comes (SOL, XRP, BNB and HYPE: "affected
    strikes resolve to No").
If only ONE event's strikes went No while the other settled on the price, only disjoint
still pays. So only a set that survives that too (sure()) may ever use real money; the rest
stay on paper even for a pinned pair.

Real Kalshi data (docs/RESEARCH.md, idea #19, 2026-10-29): 0 broken links in about 4,000
settled event pairs, every pair on the same number. Gaps are rare and small: 69 hours in 60
days passed the bot's rules (most in thin coins in mid-September), 5 in the last 30 days,
1-2c each. Prices came from candles, which show no depth.

Paper only: nothing yet shows these fill at size. Real money only goes to a disjoint set of an
event pair a person pinned in VERIFIED, with every market's rules word for word and its dates,
and even then never while a set looks too good to be true (too_good, at scan and again just
before buying). It is empty. One risk no check here can see: if one series ever rounded the
settled price and the other didn't, any set could lose (0 of about 4,000 settled pairs did).

It fails closed. A pair of events trades only if:
  - every market reads exactly as Kalshi words them now for that coin, with only the numbers
    and the time different, pays plainly ($0 or $1) and is open;
  - every market of both events closes at the one time its rules name, and both event
    tickers name that hour;
  - the range event tiles its grid (brackets end to end, a tail at each end), every value is
    on the coin's tick and agrees with its ticker, and every bracket starts one tick above a
    ladder strike and ends on one. Exact decimals, never floats.
Anything odd, or anything it can't read: no signals. Every set is played out over every
price that matters before it's offered (check()).
"""
import bisect
import datetime
import re
import time
from decimal import Decimal

from .. import risk
from ..client import price, ts
from .base import Leg, Signal, Strategy, plain_payouts
from .ladder_arb import MIN_TIME_LEFT, OPEN, allocate, close_ts, locked_basket, quote, thinnest_first, tradeable

MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
MAX_HOT = 20   # sets whose order books one scan reads (the best by listed price): 60 books at most
PER_PAIR = 4   # of those, at most this many from one event pair, so one coin's stale quotes can't take them all

# The second paragraph of the rules, word for word (lowercase) as each coin has it (2026-10-29).
_NOT_ALL = ("not all cryptocurrency price data is the same. while checking a source like google or coinbase may "
            "help guide your decision, the price used to determine this market is based on cf benchmarks' "
            "corresponding real time index (rti).")
_SIXTY = _NOT_ALL + (" at the last minute before expiration, 60 rti prices are collected. the official and final "
                     "value is the average of these prices.")
_XRP = ("the market resolves based on a simple average of the cf benchmarks index for the 60 seconds prior to the "
        "specified time. the price must meet the criterion at exactly the specified time on the target date. if no "
        "data is available or incomplete at the expiration time, affected strikes resolve to no. for "
        "cryptocurrencies with multiple versions, the exchange will specify which version or ticker is being "
        "tracked. the cf benchmarks real-time index provides continuous pricing data for major cryptocurrencies.")
_SIXTY_SECONDS = "the simple average of the sixty seconds of cf benchmarks'"
_N = r"\d+(?:\.\d+)?"


def _first(index, dollar):
    """A coin's first paragraph, as Kalshi words it: only the strike and the time may differ."""
    d = re.escape(dollar)
    return re.compile(
        rf"if {re.escape(index)} before (?P<h>1[0-2]|[1-9]) (?P<ap>am|pm) (?P<tz>edt|est) is "
        rf"(?:between {d}(?P<a>{_N})-(?P<b>{_N})|above {d}(?P<above>{_N})|below {d}(?P<below>{_N})) "
        rf"at (?P=h) (?P=ap) (?P=tz) on (?P<mon>{'|'.join(MONTHS)}) (?P<day>[1-9]|[12]\d|3[01]), (?P<year>\d{{4}}), "
        r"then the market resolves to yes\.")


# Range series -> how both of its events read on the real markets (2026-10-29): the ladder
# series; the first paragraph (the index as the rules name it, "$" before the number or not);
# the second, word for word; the tick (Kalshi's round_digits); whether the strikes sit in
# custom_strike (DOGE, SHIB).
COINS = {
    "KXBTC": dict(above="KXBTCD", first=_first(f"{_SIXTY_SECONDS} bitcoin real-time index (brti)", ""),
                  second=_SIXTY, digits=2, custom=False),
    "KXETH": dict(above="KXETHD", first=_first(f"{_SIXTY_SECONDS} ethereum real-time index (erti)", ""),
                  second=_SIXTY, digits=2, custom=False),
    "KXSOLE": dict(above="KXSOLD", first=_first(f"{_SIXTY_SECONDS} solusd_rti", "$"),
                   second=_SIXTY, digits=4, custom=False),
    "KXXRP": dict(above="KXXRPD", first=_first(f"{_SIXTY_SECONDS} ripple-dollar real time index (xrpusd_rti)", ""),
                  second=_XRP, digits=4, custom=False),
    "KXDOGE": dict(above="KXDOGED", first=_first("there is a 60 second average of cf benchmarks' dogecoin real-time "
                                                 "index (dogeusd_rti)", ""), second=_NOT_ALL, digits=7, custom=True),
    "KXBNB": dict(above="KXBNBD", first=_first(f"{_SIXTY_SECONDS} bnbusd_rti", "$"),
                  second=_SIXTY, digits=2, custom=False),
    "KXHYPE": dict(above="KXHYPED", first=_first(f"{_SIXTY_SECONDS} hypeusd_rti", "$"),
                   second=_SIXTY, digits=4, custom=False),
    "KXSHIBA": dict(above="KXSHIBAD", first=_first("the simple average of the 60 seconds of cf benchmarks' shiba "
                                                   "inu-dollar spot rate (shibusd_rti)", ""),
                    second=_SIXTY, digits=9, custom=True),
}
_UTC_OFFSET = {"edt": 4, "est": 5}   # hours behind UTC, as the rules name them
_TICKER = re.compile(rf"(?P<letter>[BT])(?P<label>{_N})")
_SUBTITLE = {"between": re.compile(r"\$(?P<a>[\d,]+(?:\.\d+)?) to (?P<b>[\d,]+(?:\.\d+)?)"),
             "greater": re.compile(r"\$[\d,]+(?:\.\d+)? or above"), "less": re.compile(r"\$[\d,]+(?:\.\d+)? or below")}
# Which strike fields each kind has: "between" a floor and a cap, "greater" a floor, "less" a cap.
_FIELDS = {"between": (True, True), "greater": (True, False), "less": (False, True)}

# Real money only for these: range event ticker -> {"above": its ladder's event ticker,
# "rules": rules_key(...)}. Add one only by pull request, after reading every market's rules
# in full. Empty: every pair trades on paper.
VERIFIED = {}
UNVERIFIED = "no event pair checked by hand yet (range_link_arb.VERIFIED is empty)"
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


class _Skip(Exception):
    """Why an event pair isn't trusted (pairs() can collect these for a look by hand)."""


def _plain(text):
    """Lowercase, one line, single spaces, straight quotes."""
    return " ".join(str(text or "").translate(_QUOTES).split()).lower()


def _dec(v):
    """An exact decimal from a strike: JSON numbers by their shortest text (71799.99, never
    71799.98999...), strings as written. None unless it's a plain number above 0."""
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    text = repr(v) if isinstance(v, float) else str(v)
    if not re.fullmatch(r"\d+(?:\.\d+)?(?:[eE]-?\d+)?", text):
        return None
    d = Decimal(text)
    return d if d.is_finite() and d > 0 else None


def _when(v):
    try:
        return ts(v) if isinstance(v, str) and v else None
    except (TypeError, ValueError, AttributeError):
        return None


def _strikes(m, coin):
    """(kind, floor, cap) from a market's strike fields, checked for the coin's shape: its tick
    in custom_strike, and DOGE's and SHIB's strikes there too (kind "custom")."""
    cs = m.get("custom_strike")
    if not isinstance(cs, dict) or cs.get("round_digits") != str(coin["digits"]):
        raise _Skip(f"{m.get('ticker')}: not the coin's tick")
    if coin["custom"]:
        if m.get("strike_type") != "custom" or set(cs) != {"cap_strike", "floor_strike", "round_digits", "strike_type"}:
            raise _Skip(f"{m.get('ticker')}: strikes not where this coin keeps them")
        if m.get("floor_strike") not in (None, "") or m.get("cap_strike") not in (None, ""):
            raise _Skip(f"{m.get('ticker')}: strikes in two places")
        kind, lo, hi = cs["strike_type"], cs["floor_strike"], cs["cap_strike"]
    else:
        if set(cs) != {"round_digits"}:
            raise _Skip(f"{m.get('ticker')}: more in custom_strike than the tick")
        kind, lo, hi = m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike")
    has = _FIELDS.get(kind)
    if has is None:
        raise _Skip(f"{m.get('ticker')}: strike type {kind!r}")
    got = []
    for wanted, v in zip(has, (lo, hi)):
        if not wanted and v not in (None, ""):
            raise _Skip(f"{m.get('ticker')}: a strike its kind doesn't have")
        d = _dec(v) if wanted else None
        if wanted and d is None:
            raise _Skip(f"{m.get('ticker')}: a strike that isn't a number")
        got.append(d)
    return kind, got[0], got[1]


def _label(m, et, kind, lo, hi):
    """The ticker must say the same: T and the strike for a tail or a rung, B and the
    bracket's middle (rounded to the label's decimals) for a bracket."""
    t = m.get("ticker")
    g = _TICKER.fullmatch(t[len(et) + 1:]) if isinstance(t, str) and t.startswith(et + "-") else None
    if not g or (g["letter"] == "B") != (kind == "between"):
        raise _Skip(f"{t}: ticker doesn't fit its strikes")
    x = Decimal(g["label"])
    if kind == "between":
        places = len(g["label"].split(".")[1]) if "." in g["label"] else 0
        ok = lo <= x <= hi and abs(x - (lo + hi) / 2) <= Decimal(5).scaleb(-places - 1)
    else:
        ok = x == (lo if kind == "greater" else hi)
    if not ok:
        raise _Skip(f"{t}: ticker doesn't fit its strikes")


def _subtitle(m, kind, lo, hi):
    """'$71,700 to 71,799.99' for a bracket (its own numbers), '... or above' / '... or below'
    for the rest (their numbers follow no one grid, so only the words count)."""
    g = _SUBTITLE[kind].fullmatch(_plain(m.get("yes_sub_title")))
    if not g or (kind == "between" and tuple(Decimal(g[k].replace(",", "")) for k in "ab") != (lo, hi)):
        raise _Skip(f"{m.get('ticker')}: subtitle doesn't fit")


def _rules(m, coin, kind, lo, hi):
    """When the rules say the price is taken, if they read exactly as Kalshi words them with
    these strikes: (unix time, (year, month, day, hour 0-23), the time as they say it)."""
    g = coin["first"].fullmatch(_plain(m.get("rules_primary")))
    if not g or _plain(m.get("rules_secondary")) != coin["second"]:
        raise _Skip(f"{m.get('ticker')}: rules worded differently")
    if g["a"] is not None:
        said = ("between", Decimal(g["a"]), Decimal(g["b"]))
    elif g["above"] is not None:
        said = ("greater", Decimal(g["above"]), None)
    else:
        said = ("less", None, Decimal(g["below"]))
    if said != (kind, lo, hi):
        raise _Skip(f"{m.get('ticker')}: rules and strikes disagree")
    hour = int(g["h"]) % 12 + (12 if g["ap"] == "pm" else 0)
    local = (int(g["year"]), MONTHS.index(g["mon"]) + 1, int(g["day"]), hour)
    at = datetime.datetime(*local, tzinfo=datetime.timezone.utc) + datetime.timedelta(hours=_UTC_OFFSET[g["tz"]])
    say = f"{g['h']} {g['ap'].upper()} {g['tz'].upper()} on {g['mon'].capitalize()} {g['day']}, {g['year']}"
    return int(at.timestamp()), local, say


def _event(ev, series, coin, ranged):
    """One event of a pair, read and checked market by market: {"event", "title", "markets":
    [{"m", "kind", "lo", "hi"}], "close", "expires", "local", "say"}. Raises _Skip."""
    et = ev.get("event_ticker")
    markets = ev.get("markets")
    if ev.get("series_ticker") not in (None, series) or ev.get("mutually_exclusive") is not ranged:
        raise _Skip(f"{et}: not the kind of event it should be")
    if not isinstance(markets, list) or not markets or not all(isinstance(m, dict) for m in markets):
        raise _Skip(f"{et}: no markets")
    if not plain_payouts(markets):
        raise _Skip(f"{et}: a market that doesn't pay plainly")
    out, said, closes, expires = [], set(), set(), set()
    for m in markets:
        if m.get("event_ticker") not in (None, et) or m.get("status") not in OPEN:
            raise _Skip(f"{m.get('ticker')}: not open in this event")
        kind, lo, hi = _strikes(m, coin)
        if not ranged and kind != "greater":
            raise _Skip(f"{m.get('ticker')}: not a rung of the ladder")
        _label(m, et, kind, lo, hi)
        _subtitle(m, kind, lo, hi)
        said.add(_rules(m, coin, kind, lo, hi))
        closes.add(close_ts(m))
        expires.add(_when(m.get("expected_expiration_time")))
        out.append({"m": m, "kind": kind, "lo": lo, "hi": hi})
    if len(said) != 1 or len(closes) != 1 or len(expires) != 1 or None in closes | expires:
        raise _Skip(f"{et}: its markets don't all end at one time")
    at, local, say = said.pop()
    close = closes.pop()
    if at != close:
        raise _Skip(f"{et}: closes at another time than its rules name")
    year, month, day, hour = local
    if et != f"{series}-{year % 100:02d}{MONTHS[month - 1].upper()}{day:02d}{hour:02d}":
        raise _Skip(f"{et}: ticker names another hour")
    if ev.get("strike_date") not in (None, "") and _when(ev.get("strike_date")) != close:
        raise _Skip(f"{et}: strike date isn't its close")
    tick = Decimal(1).scaleb(-coin["digits"])
    if any(x is not None and x % tick for r in out for x in (r["lo"], r["hi"])):
        raise _Skip(f"{et}: a strike off the coin's tick")
    return {"event": et, "title": ev.get("title") or et, "markets": out, "close": close, "expires": expires.pop(),
            "local": local, "say": say}


def _pair(et, evs, by_ticker):
    series = et.split("-", 1)[0]
    coin = COINS.get(series)
    if coin is None:
        return None   # a ladder (it's read with its range event) or another market altogether
    above = by_ticker.get(coin["above"] + et[len(series):]) or []
    if len(evs) != 1 or len(above) != 1:
        raise _Skip(f"{et}: not one range event and one ladder")
    rng, lad = _event(evs[0], series, coin, True), _event(above[0], coin["above"], coin, False)
    if (rng["close"], rng["expires"], rng["local"]) != (lad["close"], lad["expires"], lad["local"]):
        raise _Skip(f"{et}: the two events don't end together")
    tick = Decimal(1).scaleb(-coin["digits"])
    brackets = sorted((r for r in rng["markets"] if r["kind"] == "between"), key=lambda r: r["lo"])
    low = [r for r in rng["markets"] if r["kind"] == "less"]
    high = [r for r in rng["markets"] if r["kind"] == "greater"]
    if not brackets or len(low) != 1 or len(high) != 1 or not all(r["lo"] < r["hi"] for r in brackets):
        raise _Skip(f"{et}: not brackets with a tail at each end")
    if any(b["lo"] != a["hi"] + tick for a, b in zip(brackets, brackets[1:])):
        raise _Skip(f"{et}: brackets don't run end to end")
    if low[0]["hi"] != brackets[0]["lo"] or high[0]["lo"] != brackets[-1]["hi"]:
        raise _Skip(f"{et}: tails don't meet the brackets")
    ladder = sorted(((r["lo"], r) for r in lad["markets"]), key=lambda kr: kr[0])
    strikes = [k for k, _ in ladder]
    if len(set(strikes)) != len(strikes):
        raise _Skip(f"{lad['event']}: a strike twice")
    have = set(strikes)
    if any(b["lo"] - tick not in have or b["hi"] not in have for b in brackets):
        raise _Skip(f"{et}: a bracket off the ladder's grid")
    records = {r["m"]["ticker"]: r for r in rng["markets"] + lad["markets"]}
    if len(records) != len(rng["markets"]) + len(lad["markets"]):
        raise _Skip(f"{et}: a ticker twice")
    return {"series": series, "range": rng, "above": lad, "close": rng["close"], "tick": tick, "brackets": brackets,
            "low": low[0], "high": high[0], "ladder": ladder, "records": records}


def pairs(events, why=None):
    """Every (range event, its ladder) pair that reads cleanly. Anything doubtful is left out;
    give `why` a list to hear what (event ticker, reason)."""
    by_ticker = {}
    for ev in events if isinstance(events, list) else []:
        if isinstance(ev, dict) and isinstance(ev.get("event_ticker"), str) and ev["event_ticker"]:
            by_ticker.setdefault(ev["event_ticker"], []).append(ev)
    out = []
    for et, evs in by_ticker.items():
        try:
            pair = _pair(et, evs, by_ticker)
        except _Skip as e:
            pair, reason = None, str(e)
        except (ArithmeticError, AttributeError, LookupError, TypeError, ValueError) as e:
            pair, reason = None, f"{et}: unreadable ({type(e).__name__})"   # one odd event mustn't hide the rest
        if pair:
            out.append(pair)
        elif why is not None and et.split("-", 1)[0] in COINS:
            why.append((et, reason))
    return out


def _listed(m, side):
    """What one contract of `side` costs by the listed prices, before fees; None if there's none."""
    p = price(m, "yes_ask") if side == "yes" else round(1 - price(m, "yes_bid"), 4)
    return p if 0 < p < 1 else None


def _set(name, pays, legs):
    prices = [_listed(m, side) for m, side in legs]
    return {"set": name, "pays": pays, "legs": legs, "listed": None if None in prices else sum(prices)}


def links(pair):
    """For each market of the range event, the cheapest set of each kind by the listed prices:
    [{"set", "pays", "legs": [(market, side)], "listed"}]. check() plays each one out."""
    ladder = pair["ladder"]
    strikes = [k for k, _ in ladder]
    under, best = [None], None   # under[i]: cheapest YES among ladder[:i] (ties: the higher strike)
    for _, r in ladder:
        p = _listed(r["m"], "yes")
        if p is not None and (best is None or p <= best[0]):
            best = (p, r["m"])
        under.append(best)
    over, best = [None], None    # over[i]: cheapest NO among ladder[i:] (ties: the lower strike)
    for _, r in reversed(ladder):
        p = _listed(r["m"], "no")
        if p is not None and (best is None or p <= best[0]):
            best = (p, r["m"])
        over.append(best)
    over.reverse()
    out = []
    for b in pair["brackets"]:
        yes, no = under[bisect.bisect_left(strikes, b["lo"])], over[bisect.bisect_left(strikes, b["hi"])]
        if yes:
            out.append(_set("superset", 1, [(yes[1], "yes"), (b["m"], "no")]))
        if no:
            out.append(_set("disjoint", 1, [(no[1], "no"), (b["m"], "no")]))
        if yes and no:
            out.append(_set("sell3", 2, [(b["m"], "no"), (yes[1], "yes"), (no[1], "no")]))
    top, bottom = pair["high"], pair["low"]
    yes = under[bisect.bisect_right(strikes, top["lo"])]     # rungs at or under the top tail
    if yes:
        out.append(_set("superset", 1, [(yes[1], "yes"), (top["m"], "no")]))
    no = over[bisect.bisect_left(strikes, bottom["hi"])]     # rungs at or over the bottom tail
    if no:
        out.append(_set("disjoint", 1, [(no[1], "no"), (bottom["m"], "no")]))
    same = next((r["m"] for k, r in ladder if k == top["lo"]), None)
    if same:
        out.append(_set("tail", 1, [(top["m"], "yes"), (same, "no")]))
    return out


def _yes(r, v, lo_in, hi_in):
    """Does this market resolve YES if the price is v (a bracket's ends in or out as given)?"""
    if r["kind"] == "greater":
        return v > r["lo"]
    if r["kind"] == "less":
        return v < r["hi"]
    return (v > r["lo"] or (lo_in and v == r["lo"])) and (v < r["hi"] or (hi_in and v == r["hi"]))


def _prices(pair):
    """Every price that matters: each strike of both events, a tick and half a tick either side
    (a price between the cents, like 71799.995), and far beyond both ends."""
    tick, half = pair["tick"], pair["tick"] / 2
    xs = {x for r in pair["records"].values() for x in (r["lo"], r["hi"]) if x is not None}
    out = {x + d for x in xs for d in (-tick, -half, 0, half, tick)}
    return sorted(out | {Decimal(0), half, min(xs) / 2, max(xs) * 2, max(xs) * 1000})


def check(pair, legs, pays):
    """True only if one contract of each leg pays at least `pays` dollars at every price that
    matters, with the bracket's ends counted in and out, and also if no data comes and every
    strike resolves No. Sets that need the price on the cent, or YES on both tails, fail."""
    recs = [(pair["records"].get(m.get("ticker")), side) for m, side in legs]
    if not legs or any(r is None or side not in ("yes", "no") for r, side in recs):
        return False
    if len({r["m"]["ticker"] for r, _ in recs}) != len(recs) or sum(r["kind"] == "between" for r, _ in recs) > 1:
        return False   # one bracket at most: its two ends are all that can be in or out
    if sum(side == "no" for _, side in recs) < pays:
        return False   # no data: every strike No
    for v in _prices(pair):
        for lo_in in (True, False):
            for hi_in in (True, False):
                if sum(_yes(r, v, lo_in, hi_in) == (side == "yes") for r, side in recs) < pays:
                    return False
    return True


def sure(pair, legs, pays):
    """check(), and also if only one of the two events gets no data (every strike of it No)
    while the other settles on the price. Only disjoint (NO + NO) passes. Real money needs it."""
    if not check(pair, legs, pays):
        return False
    recs = [(pair["records"][m["ticker"]], side) for m, side in legs]
    for event in (pair["range"], pair["above"]):
        dead = {r["m"]["ticker"] for r in event["markets"]}   # this event's strikes all resolve No
        for v in _prices(pair):
            for lo_in in (True, False):
                for hi_in in (True, False):
                    if sum((r["m"]["ticker"] not in dead and _yes(r, v, lo_in, hi_in)) == (side == "yes")
                           for r, side in recs) < pays:
                        return False
    return True


def too_good(every, quoted):
    """True if any set costs less than pays / (1 + risk.MAX_ARB_ROI), listed or in its order
    book: too good to be true (a misread, or a quote nobody stands behind). Then even a pinned
    pair trades on paper only. A $2 set's bar is twice a $1 set's."""
    floor = 1 / (1 + risk.MAX_ARB_ROI)
    if any(s["listed"] is not None and s["listed"] < s["pays"] * floor for s in every):
        return True
    return any(all(q[2] is not None for q in quotes) and sum(q[2] for q in quotes) < s["pays"] * floor
               for s, quotes in quoted)


def rules_key(pair):
    """The rules VERIFIED pins: every market of both events, word for word (spacing aside),
    and its dates, so a pair whose times move goes back to paper."""
    markets = [r["m"] for r in pair["range"]["markets"] + pair["above"]["markets"]]
    return "\n".join(f"{m['ticker']} {m.get('close_time')} {m.get('expected_expiration_time')}: "
                     f"{' '.join(str(m.get('rules_primary') or '').split())} | "
                     f"{' '.join(str(m.get('rules_secondary') or '').split())}"
                     for m in sorted(markets, key=lambda m: m["ticker"]))


def to_signal(event, title, basket, meta, closes_at):
    c, pays = basket["count"], meta["pays"]
    legs = [Leg(t, side, p, c) for t, side, p, _ in thinnest_first(basket["quotes"])]
    what = " + ".join(f"{leg.side.upper()} {leg.ticker}" for leg in legs)
    sig = Signal("range_link_arb", event, title, legs, payout_min=c * float(pays), meta=dict(meta), note=(
        f"buy {c} of each: {what}. Pays at least ${c * pays:.2f} at {meta['at']}."))
    sig.exp_profit = sig.payout_min - sig.cost
    sig.closes_at = closes_at
    return sig


class RangeLinkArb(Strategy):
    name = "range_link_arb"
    title = "Crypto range vs above"
    kind = "arbitrage"
    description = ("Kalshi asks each crypto price twice for the same minute: is it between X and Y, "
                   "and is it above X? They must agree. When a set that must pay costs less, buy the set. "
                   "Paper only until a person checks a pair.")

    def proven(self, ctx):
        if VERIFIED:
            return True, "only event pairs a person checked by hand (VERIFIED)"
        return False, UNVERIFIED

    def _budget(self, ctx):
        return min(ctx.budget_left, float(ctx.cfg["max_per_arb"]))

    def scan(self, ctx):
        # The same download as the other arbitrage strategies: the run fetches it once.
        floor = 1 / (1 + risk.MAX_ARB_ROI)
        found, hot = [], []
        for pair in pairs(ctx.client.events(status="open", with_nested_markets="true")):
            if pair["close"] - time.time() <= MIN_TIME_LEFT:
                continue   # too close to the bell to buy every leg
            every = links(pair)
            pin = VERIFIED.get(pair["range"]["event"])
            pinned = (isinstance(pin, dict) and pin.get("above") == pair["above"]["event"]
                      and pin.get("rules") == rules_key(pair))
            # Listed under the too-good bar: refused anyway, so it mustn't take a book slot. An
            # unpinned pair is all paper: leave out what's already open on paper.
            hot += [(s["pays"] - s["listed"], len(found), i) for i, s in enumerate(every)
                    if s["listed"] is not None and s["pays"] * floor <= s["listed"] < s["pays"]
                    and all(tradeable(m, ctx) for m, _ in s["legs"])
                    and (pinned or not any(m["ticker"] in ctx.held_paper for m, _ in s["legs"]))]
            found.append((pair, every, pinned))
        # Order books only for the best few sets by listed price, so a scan reads few of them.
        books, quoted, per = {}, {}, {}
        for _, f, i in sorted(hot, key=lambda h: (-h[0], h[1], h[2])):
            if sum(per.values()) >= MAX_HOT:
                break
            if per.get(f, 0) >= PER_PAIR:
                continue
            per[f] = per.get(f, 0) + 1
            s = found[f][1][i]
            for m, _ in s["legs"]:
                if m["ticker"] not in books:
                    books[m["ticker"]] = ctx.client.orderbook(m["ticker"])
            quoted.setdefault(f, []).append(
                (s, [(m["ticker"], side, *quote(books[m["ticker"]], side)) for m, side in s["legs"]]))
        candidates = []
        for f, sets in quoted.items():
            pair, every, pinned = found[f]
            rng, lad = pair["range"], pair["above"]
            checked = pinned and not too_good(every, sets)
            for s, quotes in sets:
                if not check(pair, s["legs"], s["pays"]):
                    continue   # never offer a set that could pay less than it says
                live = checked and sure(pair, s["legs"], s["pays"])
                if not live and any(m["ticker"] in ctx.held_paper for m, _ in s["legs"]):
                    continue   # paper only, and already open on paper: the engine would skip it
                if all(q[2] is not None for q in quotes) and sum(q[2] for q in quotes) < s["pays"] * floor:
                    continue   # too good to be true in its book: not even on paper
                meta = {"range_event": rng["event"], "above_event": lad["event"], "set": s["set"], "pays": s["pays"],
                        "link": " + ".join(f"{side.upper()} {m['ticker']}" for m, side in s["legs"]),
                        "at": rng["say"], "paper_only": not live, "first_close": pair["close"]}
                candidates.append((quotes, lambda bk, meta=meta, rng=rng, close=pair["close"]: to_signal(
                    rng["event"], rng["title"], bk, meta, close), s["pays"]))
        return allocate(ctx, candidates)

    def recheck(self, ctx, sig):
        first, pays = sig.meta.get("first_close"), sig.meta.get("pays")
        if not isinstance(first, (int, float)) or first - time.time() <= MIN_TIME_LEFT or pays not in (1, 2):
            return None   # too close to the bell to buy every leg, or not a set this scan made
        if any(leg.ticker in ctx.held for leg in sig.legs):
            return None   # bought by an earlier signal this run
        quotes = [(leg.ticker, leg.side, *quote(ctx.client.orderbook(leg.ticker), leg.side)) for leg in sig.legs]
        if any(q[2] is None for q in quotes) or sum(q[2] for q in quotes) < pays / (1 + risk.MAX_ARB_ROI):
            return None   # too good to be true now: scan would keep the pair on paper
        basket = locked_basket(quotes, self._budget(ctx), float(ctx.cfg["max_per_trade"]), pays)
        if not basket:
            return None
        return to_signal(sig.event, sig.title, basket, sig.meta, sig.closes_at)


STRATEGY = RangeLinkArb()
