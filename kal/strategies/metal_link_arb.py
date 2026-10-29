"""Linked gold and silver prices at month end: the monthly series vs the daily or weekly one.

Kalshi asks "is gold above $X at 5 PM ET?" in three series: KXGOLDD (Monday to Thursday),
KXGOLDW (Fridays) and KXGOLDMON (the month's last trading day). On that day the monthly and
the daily (on a Friday, the weekly) ask about the same minute, in the same words, from the
same Pyth feed. Silver the same: KXSILVERD, KXSILVERW and KXSILVERMON. If both settle on one
number, this pair pays at least $1:
  YES "above x" in one series + NO "above y" in the other, with y >= x.
The NO pays unless the price is over y, and then it's over x too, so the YES pays. "Above"
means strictly greater (the COMMODITIES terms), so a price on a strike counts as not above
it in both. A price between x and y pays both legs: $2.

Real Kalshi data (docs/RESEARCH.md, idea #21, 2026-10-29): 14 gold and silver month-end pairs
settled from March to September 2026, 12 on the same number. Both misses (Mar 31) came under
other words and another source ("open gold price", TradingView): gold 4667.88 vs 4668.88.
With today's words, 10 of 10 matched, and no pair lost. Gaps after fees that pass the bot's
rules came at every gold month end (184 hours; 39 with trades on both legs), e.g. Aug 31: NO
daily "above 4452" at 41c + YES monthly "above 4451.99" at 50c, +5.5c. Prices came from
candles, which show no depth.

Real money is not planned (VERIFIED is empty), because:
  - The two series settle apart: the daily or weekly about an hour after 5 PM, the monthly 3
    to 68 hours later, in one batch with every commodity monthly. Each is read on its own
    (AAA gas's weekly once settled on the day before's figure, #20).
  - The words never name the source. Only each event's settlement_sources does, and Kalshi
    moved each series to a new feed on its own date (gold: four sources in seven months, the
    monthly weeks behind the daily). A move outside the words can come at any time.
  - The terms use a correction issued "within twenty-four (24) hours" of the first number. One
    can land after the daily settles and before the monthly does.
  - Even the metadata differs: the daily's custom_strike says round_digits "2", the monthly's
    says nothing (both rules say 2 decimals, so that's allowed here).
Two different numbers can make both legs lose (copper, Aug 31 2026: the monthly read a futures contract,
the daily an index, 6.58 vs 6.69). A pin in VERIFIED would let a pair trade for real; even
then never while a pair looks too good to be true (too_good, at scan and again just before
buying).

No data: both rules say "the most recently available published data will be used to resolve
the market", so each series still settles on a number, never "every strike No" (some crypto
rules do that). That number is one more price, and check() plays out every price that
matters, so the case is covered as long as both read the same number; that they might not is
the risk above. check() also plays "every strike of both events No", which YES + NO survives.

The money comes back when both settle: at the latest 8 days after the close (SETTLE_DAYS). The
terms let the expiration move up to one week after the close and settle a day after that; the
monthly took up to 68 hours. The run's events list leaves out the latest expiration Kalshi
lists, so the bound is the terms'. Signals carry it as closes_at, so risk.too_late and the
yearly return see when the money is really free.

It fails closed. A pair trades only if:
  - every market of both events reads exactly as Kalshi words them now (only the date and the
    strike differ), pays plainly ($0 or $1), is open, and is a "greater" strike on the cent,
    the same in its strike field, its rules, its ticker and its subtitle;
  - every market of both events closes at the minute its rules name (5:00 PM ET), with one
    expected expiration, both events' strike_date is that minute, and both tickers name the
    day;
  - both events name one and the same source: the Pyth feed Kalshi names now for that metal;
  - no market names a futures contract (front_month_contract "N/A", or no such key).
Exact decimals, never floats. Anything odd, or anything it can't read: no signals. Every pair
is played out over every price that matters before it's offered (check()).
"""
import bisect
import datetime
import json
import re
import time
from decimal import Decimal

from .. import risk
from ..client import price, ts
from .base import Leg, Signal, Strategy, plain_payouts
from .ladder_arb import MIN_TIME_LEFT, OPEN, allocate, close_ts, locked_basket, quote, thinnest_first, tradeable

MONTHS = ("january february march april may june july august september october november december").split()
DAY = 86400
SETTLE_DAYS = 8          # the latest a pair's money comes back, after the close (see the docstring)
TICK = Decimal("0.01")   # the rules round the settled price to 2 decimals
MAX_HOT = 20             # pairs of legs whose order books one scan reads (the best by listed price)
PER_PAIR = 6             # of those, at most this many from one pair of events

# Monthly series -> what its pairs look like on the real markets (2026-10-29): the metal its
# rules name, the series asking the same at the same minute (daily, weekly), and the one source
# both must name (the feed's address without its query string; the name Kalshi gives it).
METALS = {
    "KXGOLDMON": dict(metal="gold", others=("KXGOLDD", "KXGOLDW"), source="Pyth - Gold",
                      feed="https://app.pyth.com/explore/Metal.Index.1OZGOLD%2FUSD"),
    "KXSILVERMON": dict(metal="silver", others=("KXSILVERD", "KXSILVERW"), source="Pyth - Silver",
                        feed="https://app.pyth.com/explore/Metal.Index.SILVER%2FUSD"),
}

# The rules, word for word (lowercase) as Kalshi has them since May 2026: only the date, the
# time zone and the strike may differ. "The open gold price", another time, or "rounded to the
# nearest 0 decimal places" (all seen in older months) is another market.
_FIRST = re.compile(
    rf"if the close price of the 1-minute candlestick for (?P<metal>gold|silver) on (?P<mon>{'|'.join(MONTHS)}) "
    r"(?P<day>0?[1-9]|[12]\d|3[01]), (?P<year>\d{4}) at 5:00 pm (?P<tz>edt|est) is above (?P<k>\d+(?:\.\d+)?) "
    r"usd/t\.oz, then the market resolves to yes\.")
_SECOND = ("the settlement value is rounded to the nearest 2 decimal places. when confirming the settlement value, "
           "note the close price for the 1-minute candlestick at a given time is the price at the end of the "
           "immediately preceding one-minute interval. for example, the candlestick timestamped 4:59 pm reflects "
           "trading during 4:59:00 pm to 4:59:59 pm and closes at 5:00:00 pm. if no data is published by the "
           "specified source agency for the specified time, then the most recently available published data will "
           "be used to resolve the market.")
_UTC_OFFSET = {"edt": 4, "est": 5}   # hours behind UTC, as the rules name them
_TICKER = re.compile(r"T(?P<k>\d+(?:\.\d+)?)")
_SUBTITLE = re.compile(r"above \$(?P<k>\d+(?:\.\d+)?)")
# What custom_strike may hold on these markets: the contract (none: "N/A"), the tick, the day.
_CUSTOM = ("front_month_contract", "round_digits", "strike_date")

# Real money only for these: monthly event ticker -> {"pair": the daily or weekly event's
# ticker, "rules": rules_key(...)}. Add one only by pull request, after reading every market's
# rules in full and both events' sources. Empty: every pair trades on paper.
VERIFIED = {}
UNVERIFIED = "no event pair checked by hand yet (metal_link_arb.VERIFIED is empty)"
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


class _Skip(Exception):
    """Why a pair of events isn't trusted (pairs() can collect these for a look by hand)."""


def _plain(text):
    """Lowercase, one line, single spaces, straight quotes."""
    return " ".join(str(text or "").translate(_QUOTES).split()).lower()


def _dec(v):
    """An exact decimal from a strike: JSON numbers by their shortest text (4021.99, never
    4021.98999...), strings as written. None unless it's a plain number above 0."""
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


def _custom(m):
    """The strike day custom_strike names (None if it names none). Raises _Skip if it names a
    futures contract, another tick, or anything else."""
    cs = m.get("custom_strike")
    if cs is None:
        return None   # older markets had none at all
    if not isinstance(cs, dict) or set(cs) - set(_CUSTOM):
        raise _Skip(f"{m.get('ticker')}: more in custom_strike than these markets have")
    if cs.get("front_month_contract", "N/A") != "N/A":
        raise _Skip(f"{m.get('ticker')}: names a futures contract")
    if cs.get("round_digits", "2") != "2":
        raise _Skip(f"{m.get('ticker')}: not rounded to the cent")
    if "strike_date" not in cs:
        return None
    day = _when(cs["strike_date"])
    if day is None:
        raise _Skip(f"{m.get('ticker')}: a strike day it can't read")
    return day


def _market(m, et, metal):
    """One market, checked: (its strike, (when its rules take the price, (year, month, day),
    that time as they say it), the strike day custom_strike names or None). Raises _Skip."""
    t = m.get("ticker")
    if m.get("event_ticker") not in (None, et) or m.get("status") not in OPEN:
        raise _Skip(f"{t}: not open in this event")
    if m.get("strike_type") != "greater":
        raise _Skip(f"{t}: strike type {m.get('strike_type')!r}")
    k = _dec(m.get("floor_strike"))
    if k is None or m.get("cap_strike") not in (None, ""):
        raise _Skip(f"{t}: not one strike")
    if k % TICK:
        raise _Skip(f"{t}: a strike off the cent")
    day = _custom(m)
    g = _FIRST.fullmatch(_plain(m.get("rules_primary")))
    if not g or g["metal"] != metal or _plain(m.get("rules_secondary")) != _SECOND:
        raise _Skip(f"{t}: rules worded differently")
    if Decimal(g["k"]) != k:
        raise _Skip(f"{t}: rules and strike disagree")
    label = _TICKER.fullmatch(t[len(et) + 1:]) if isinstance(t, str) and t.startswith(et + "-") else None
    if not label or Decimal(label["k"]) != k:
        raise _Skip(f"{t}: ticker doesn't fit its strike")
    sub = _SUBTITLE.fullmatch(_plain(m.get("yes_sub_title")))
    if not sub or Decimal(sub["k"]) != k:
        raise _Skip(f"{t}: subtitle doesn't fit")
    local = (int(g["year"]), MONTHS.index(g["mon"]) + 1, int(g["day"]))
    at = datetime.datetime(*local, 17, tzinfo=datetime.timezone.utc) + datetime.timedelta(hours=_UTC_OFFSET[g["tz"]])
    say = f"5:00 PM {g['tz'].upper()} on {g['mon'].capitalize()} {local[2]}, {local[0]}"
    return k, (int(at.timestamp()), local, say), day


def _source(ev, info):
    """The one source the event names, as (name, address without the query string), if it is
    the feed Kalshi names now for this metal. Raises _Skip."""
    ss = ev.get("settlement_sources")
    if not isinstance(ss, list) or len(ss) != 1 or not isinstance(ss[0], dict):
        raise _Skip(f"{ev.get('event_ticker')}: not one source")
    name, url = ss[0].get("name"), ss[0].get("url")
    got = (name, url.split("#", 1)[0].split("?", 1)[0] if isinstance(url, str) else None)
    if got != (info["source"], info["feed"]):
        raise _Skip(f"{ev.get('event_ticker')}: a source other than the feed it names now")
    return got


def _event(ev, series, info):
    """One event of a pair, read and checked market by market: {"event", "series", "title",
    "markets": [{"m", "k", "event"}], "close", "expires", "local", "say", "source",
    "sources"}. Raises _Skip."""
    et = ev.get("event_ticker")
    markets = ev.get("markets")
    if ev.get("series_ticker") not in (None, series) or ev.get("mutually_exclusive") is not False:
        raise _Skip(f"{et}: not the kind of event it should be")
    if not isinstance(markets, list) or not markets or not all(isinstance(m, dict) for m in markets):
        raise _Skip(f"{et}: no markets")
    if not plain_payouts(markets):
        raise _Skip(f"{et}: a market that doesn't pay plainly")
    source = _source(ev, info)
    out, said, closes, expires, days = [], set(), set(), set(), set()
    for m in markets:
        k, when, day = _market(m, et, info["metal"])
        said.add(when)
        days.add(day)
        closes.add(close_ts(m))
        expires.add(_when(m.get("expected_expiration_time")))
        out.append({"m": m, "k": k, "event": et})
    if len(said) != 1 or len(closes) != 1 or len(expires) != 1 or None in closes | expires:
        raise _Skip(f"{et}: its markets don't all end at one time")
    at, local, say = said.pop()
    close, expected = closes.pop(), expires.pop()
    if at != close:
        raise _Skip(f"{et}: closes at another time than its rules name")
    if not close <= expected <= close + 7 * DAY:
        raise _Skip(f"{et}: expected to expire outside the week the terms allow")
    if days - {None, close}:
        raise _Skip(f"{et}: a strike day that isn't its close")
    year, month, day = local
    if et != f"{series}-{year % 100:02d}{MONTHS[month - 1][:3].upper()}{day:02d}17":
        raise _Skip(f"{et}: ticker names another day")
    if _when(ev.get("strike_date")) != close:
        raise _Skip(f"{et}: strike date isn't its close")
    if len({r["k"] for r in out}) != len(out):
        raise _Skip(f"{et}: a strike twice")
    return {"event": et, "series": series, "title": ev.get("title") or et, "markets": out, "close": close,
            "expires": expected, "local": local, "say": say, "source": source,
            "sources": json.dumps(ev.get("settlement_sources"), sort_keys=True)}


def _pair(mon_ev, other_ev, series, other, info):
    mon, oth = _event(mon_ev, series, info), _event(other_ev, other, info)
    if (mon["close"], mon["expires"], mon["local"]) != (oth["close"], oth["expires"], oth["local"]):
        raise _Skip(f"{mon['event']}: the two events don't end together")
    if mon["source"] != oth["source"]:
        raise _Skip(f"{mon['event']}: the two events name other sources")
    records = {r["m"]["ticker"]: r for r in mon["markets"] + oth["markets"]}
    if len(records) != len(mon["markets"]) + len(oth["markets"]):
        raise _Skip(f"{mon['event']}: a ticker twice")
    return {"series": series, "monthly": mon, "other": oth, "close": mon["close"], "records": records}


def pairs(events, why=None):
    """Every (monthly event, its daily or weekly twin) pair that reads cleanly. Anything
    doubtful is left out; give `why` a list to hear what (event ticker, reason)."""
    by_ticker = {}
    for ev in events if isinstance(events, list) else []:
        if isinstance(ev, dict) and isinstance(ev.get("event_ticker"), str) and ev["event_ticker"]:
            by_ticker.setdefault(ev["event_ticker"], []).append(ev)
    out = []
    for et, evs in by_ticker.items():
        series = et.split("-", 1)[0]
        info = METALS.get(series)
        if info is None:
            continue   # a daily or weekly (it's read with its monthly) or another market altogether
        twins = [(o, by_ticker[o + et[len(series):]]) for o in info["others"] if o + et[len(series):] in by_ticker]
        if not twins and why is not None:
            why.append((et, f"{et}: no daily or weekly event at that minute (yet)"))
        for other, oevs in twins:
            try:
                if len(evs) != 1 or len(oevs) != 1:
                    raise _Skip(f"{et}: not one monthly event and one twin")
                pair, reason = _pair(evs[0], oevs[0], series, other, info), None
            except _Skip as e:
                pair, reason = None, str(e)
            except (ArithmeticError, AttributeError, LookupError, TypeError, ValueError) as e:
                pair, reason = None, f"{et}: unreadable ({type(e).__name__})"   # one odd event mustn't hide the rest
            if pair:
                out.append(pair)
            elif why is not None:
                why.append((et, reason))
    return out


def _listed(m, side):
    """What one contract of `side` costs by the listed prices, before fees; None if there's none."""
    p = price(m, "yes_ask") if side == "yes" else round(1 - price(m, "yes_bid"), 4)
    return p if 0 < p < 1 else None


def links(pair):
    """For each market, the cheapest pair it can be the NO leg of: YES on a strike at or under
    it in the other event, by the listed prices. [{"set", "pays", "legs": [(market, side)],
    "listed"}]. check() plays each one out."""
    out = []
    for yes_ev, no_ev in ((pair["monthly"], pair["other"]), (pair["other"], pair["monthly"])):
        rungs = sorted(yes_ev["markets"], key=lambda r: r["k"])
        strikes = [r["k"] for r in rungs]
        under, best = [None], None   # under[i]: cheapest YES among rungs[:i] (ties: the higher strike)
        for r in rungs:
            p = _listed(r["m"], "yes")
            if p is not None and (best is None or p <= best[0]):
                best = (p, r)
            under.append(best)
        for n in no_ev["markets"]:
            yes = under[bisect.bisect_right(strikes, n["k"])]   # strikes at or under the NO's
            if yes:
                legs = [(yes[1]["m"], "yes"), (n["m"], "no")]
                prices = [_listed(m, side) for m, side in legs]
                out.append({"set": "same strike" if yes[1]["k"] == n["k"] else "nested", "pays": 1, "legs": legs,
                            "listed": None if None in prices else sum(prices)})
    return out


def _prices(pair):
    """Every price that matters: each strike of both events, a cent and half a cent either side
    (a price the rounding words forgot), and far beyond both ends."""
    half = TICK / 2
    xs = {r["k"] for r in pair["records"].values()}
    out = {x + d for x in xs for d in (-TICK, -half, 0, half, TICK)}
    return sorted(out | {Decimal(0), half, min(xs) / 2, max(xs) * 2, max(xs) * 1000})


def check(pair, legs, pays):
    """True only if the legs are YES in one event and NO in the other and one contract of each
    pays at least `pays` dollars at every price that matters, both events on that one price, and
    also if every strike of both resolves No. YES above a strike over the NO's fails."""
    recs = [(pair["records"].get(m.get("ticker")), side) for m, side in legs]
    if pays != 1 or len(recs) != 2 or any(r is None for r, _ in recs):
        return False
    if {side for _, side in recs} != {"yes", "no"} or recs[0][0]["event"] == recs[1][0]["event"]:
        return False   # one leg in each series, one YES and one NO
    if sum(side == "no" for _, side in recs) < pays:
        return False   # every strike No
    return all(sum((v > r["k"]) == (side == "yes") for r, side in recs) >= pays for v in _prices(pair))


def too_good(every, quoted):
    """True if any pair costs less than 1 / (1 + risk.MAX_ARB_ROI), listed or in its order book:
    too good to be true (a misread, or a quote nobody stands behind). Then even a pinned pair
    of events trades on paper only."""
    floor = 1 / (1 + risk.MAX_ARB_ROI)
    if any(s["listed"] is not None and s["listed"] < s["pays"] * floor for s in every):
        return True
    return any(all(q[2] is not None for q in quotes) and sum(q[2] for q in quotes) < s["pays"] * floor
               for s, quotes in quoted)


def rules_key(pair):
    """What VERIFIED pins: both events' sources as Kalshi lists them, and every market of both,
    word for word (spacing aside), with its dates. A moved source or time goes back to paper."""
    evs = (pair["monthly"], pair["other"])
    markets = [r["m"] for e in evs for r in e["markets"]]
    return "\n".join([f"{e['event']} sources: {e['sources']}" for e in evs] + [
        f"{m['ticker']} {m.get('close_time')} {m.get('expected_expiration_time')}: "
        f"{' '.join(str(m.get('rules_primary') or '').split())} | "
        f"{' '.join(str(m.get('rules_secondary') or '').split())}"
        for m in sorted(markets, key=lambda m: m["ticker"])])


def to_signal(event, title, basket, meta, closes_at):
    c = basket["count"]
    legs = [Leg(t, side, p, c) for t, side, p, _ in thinnest_first(basket["quotes"])]
    what = " + ".join(f"{leg.side.upper()} {leg.ticker}" for leg in legs)
    sig = Signal("metal_link_arb", event, title, legs, payout_min=c * 1.0, meta=dict(meta), note=(
        f"buy {c} of each: {what}. Pays at least ${c:.2f} if both settle on the price at {meta['at']}."))
    sig.exp_profit = sig.payout_min - sig.cost
    sig.closes_at = closes_at
    return sig


class MetalLinkArb(Strategy):
    name = "metal_link_arb"
    title = "Gold and silver month end"
    kind = "arbitrage"
    description = ("At month end Kalshi asks the gold (and silver) price at 5 PM twice: in its monthly "
                   "series and in its daily or weekly one. They should agree. When a pair that must pay $1 "
                   "costs less, buy the pair. Paper only: the two settle apart, and the source can change.")

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
                continue   # too close to the bell to buy both legs
            every = links(pair)
            pin = VERIFIED.get(pair["monthly"]["event"])
            pinned = (isinstance(pin, dict) and pin.get("pair") == pair["other"]["event"]
                      and pin.get("rules") == rules_key(pair))
            # Listed under the too-good bar: refused anyway, so it mustn't take a book slot. An
            # unpinned pair is all paper: leave out what's already open on paper.
            hot += [(s["pays"] - s["listed"], len(found), i) for i, s in enumerate(every)
                    if s["listed"] is not None and s["pays"] * floor <= s["listed"] < s["pays"]
                    and all(tradeable(m, ctx) for m, _ in s["legs"])
                    and (pinned or not any(m["ticker"] in ctx.held_paper for m, _ in s["legs"]))]
            found.append((pair, every, pinned))
        # Order books only for the best few by listed price, so a scan reads few of them.
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
            mon, oth = pair["monthly"], pair["other"]
            live = pinned and not too_good(every, sets)
            for s, quotes in sets:
                if not check(pair, s["legs"], s["pays"]):
                    continue   # never offer a pair that could pay less than it says
                if not live and any(m["ticker"] in ctx.held_paper for m, _ in s["legs"]):
                    continue   # paper only, and already open on paper: the engine would skip it
                meta = {"monthly_event": mon["event"], "other_event": oth["event"], "set": s["set"], "pays": 1,
                        "link": " + ".join(f"{side.upper()} {m['ticker']}" for m, side in s["legs"]),
                        "at": mon["say"], "source": mon["source"][1], "paper_only": not live,
                        "first_close": pair["close"]}
                candidates.append((quotes, lambda bk, meta=meta, mon=mon, settle=pair["close"] + SETTLE_DAYS * DAY:
                                   to_signal(mon["event"], mon["title"], bk, meta, settle), 1))
        return allocate(ctx, candidates)

    def recheck(self, ctx, sig):
        first = sig.meta.get("first_close")
        if not isinstance(first, (int, float)) or first - time.time() <= MIN_TIME_LEFT or sig.meta.get("pays") != 1:
            return None   # too close to the bell to buy both legs, or not a pair this scan made
        if any(leg.ticker in ctx.held for leg in sig.legs):
            return None   # bought by an earlier signal this run
        quotes = [(leg.ticker, leg.side, *quote(ctx.client.orderbook(leg.ticker), leg.side)) for leg in sig.legs]
        if any(q[2] is None for q in quotes) or sum(q[2] for q in quotes) < 1 / (1 + risk.MAX_ARB_ROI):
            return None   # too good to be true now: scan would keep the pair on paper
        basket = locked_basket(quotes, self._budget(ctx), float(ctx.cfg["max_per_trade"]))
        if not basket:
            return None
        return to_signal(sig.event, sig.title, basket, sig.meta, sig.closes_at)


STRATEGY = MetalLinkArb()
