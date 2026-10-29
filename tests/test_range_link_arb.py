"""Tests for range_link_arb.

The key check: for every set, play out every price that matters (each strike, a tick and half
a tick either side, far beyond both ends), with each bracket's ends counted in and out, and
with no data at all (every strike No), and confirm one contract of each leg pays at least what
the set says. It reads each market's meaning from what the fixture built (its own numbers),
never from the strategy's code, so a bug in the strategy can't hide itself.

The fixtures copy the real markets (2026-10-29): wording, tickers, tick sizes, strike fields
(DOGE and SHIB keep theirs in custom_strike) and number formats per coin.
"""
import copy
import datetime
import re
import time
import unittest
from datetime import timezone
from decimal import ROUND_HALF_UP, Decimal
from unittest import mock

from kal import config, db, engine, paper, risk
from kal.strategies import MODULES, ladder_arb, range_link_arb
from kal.strategies.base import Context
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven

STRATEGY = range_link_arb.STRATEGY
D = Decimal
SHORT = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()

# The second paragraph of the rules, as Kalshi words it per coin.
NOT_ALL = ("Not all cryptocurrency price data is the same. While checking a source like Google or Coinbase may help "
           "guide your decision, the price used to determine this market is based on CF Benchmarks' corresponding "
           "Real Time Index (RTI).")
SIXTY = NOT_ALL + (" At the last minute before expiration, 60 RTI prices are collected. The official and final value "
                   "is the average of these prices.")
XRP_SECOND = ("The market resolves based on a simple average of the CF Benchmarks index for the 60 seconds prior to "
              "the specified time. The price must meet the criterion at exactly the specified time on the target "
              "date. If no data is available or incomplete at the expiration time, affected strikes resolve to No. "
              "For cryptocurrencies with multiple versions, the Exchange will specify which version or ticker is "
              "being tracked. The CF Benchmarks Real-Time Index provides continuous pricing data for major "
              "cryptocurrencies.")
SECONDS = "the simple average of the sixty seconds of CF Benchmarks'"


def plain(x):
    return format(x.normalize(), "f")


def fixed(places):
    return lambda x, role: format(x, f".{places}f")


def xrp(x, role):
    """XRP writes a bracket's floor (and the bottom tail) to 5 decimals, the rest to 4."""
    return format(x, ".5f" if role in ("floor", "low") else ".4f")


# Range series -> what its markets look like on the real API: the ladder series, the tick's
# decimals, a bracket's width, a first floor, a bracket label's decimals, the rules' index name,
# "$" before the numbers, the second paragraph, strikes in custom_strike, how numbers are written.
COINS = {
    "KXBTC": dict(ladder="KXBTCD", digits=2, step="100", floor="71700", places=0, dollar="",
                  index=f"{SECONDS} Bitcoin Real-Time Index (BRTI)", second=SIXTY, custom=False,
                  fmt=lambda x, role: plain(x), name="BTC"),
    "KXETH": dict(ladder="KXETHD", digits=2, step="5", floor="1605", places=0, dollar="",
                  index=f"{SECONDS} Ethereum Real-Time Index (ERTI)", second=SIXTY, custom=False,
                  fmt=lambda x, role: plain(x), name="ETH"),
    "KXSOLE": dict(ladder="KXSOLD", digits=4, step="0.25", floor="62.75", places=3, dollar="$",
                   index=f"{SECONDS} SOLUSD_RTI", second=SIXTY, custom=False, fmt=lambda x, role: plain(x),
                   name="SOL"),
    "KXXRP": dict(ladder="KXXRPD", digits=4, step="0.02", floor="0.58", places=7, dollar="",
                  index=f"{SECONDS} Ripple-Dollar Real Time Index (XRPUSD_RTI)", second=XRP_SECOND, custom=False,
                  fmt=xrp, name="XRP"),
    "KXDOGE": dict(ladder="KXDOGED", digits=7, step="0.005", floor="0.0100000", places=3, dollar="",
                   index="there is a 60 second average of CF Benchmarks' Dogecoin Real-Time Index (DOGEUSD_RTI)",
                   second=NOT_ALL, custom=True, fmt=fixed(7), name="Dogecoin"),
    "KXBNB": dict(ladder="KXBNBD", digits=2, step="5", floor="565", places=0, dollar="$",
                  index=f"{SECONDS} BNBUSD_RTI", second=SIXTY, custom=False, fmt=lambda x, role: plain(x),
                  name="BNB"),
    "KXHYPE": dict(ladder="KXHYPED", digits=4, step="0.25", floor="43.75", places=3, dollar="$",
                   index=f"{SECONDS} HYPEUSD_RTI", second=SIXTY, custom=False, fmt=lambda x, role: plain(x),
                   name="HYPE"),
    "KXSHIBA": dict(ladder="KXSHIBAD", digits=9, step="0.0000005", floor="0.000001000", places=7, dollar="",
                    index="the simple average of the 60 seconds of CF Benchmarks' Shiba Inu-Dollar Spot Rate "
                          "(SHIBUSD_RTI)", second=SIXTY, custom=True, fmt=fixed(9), name="Shiba Inu"),
}
HOURLY = [s for s in COINS if s != "KXSHIBA"]      # SHIB has 5 PM events only
# How likely each bracket is, in cents (10 brackets), and each tail. Both series quote the fair
# price 1c either side, so no set is under its payout.
ODDS = [1, 2, 5, 10, 20, 25, 17, 10, 5, 3]
TAIL = 1
N = len(ODDS)


def tick_of(series):
    return D(1).scaleb(-COINS[series]["digits"])


def close_at(days=1.0, hour=None):
    """A close on the whole hour about `days` from now (or the next `hour` ET after that):
    (unix time, local ET datetime). The rules say EDT, 4 hours behind UTC."""
    t = int(time.time() + days * 86400)
    t += 3600 - t % 3600
    if hour is not None:
        while (t // 3600 - 4) % 24 != hour:
            t += 3600
    return t, datetime.datetime.fromtimestamp(t - 4 * 3600, timezone.utc)


def iso(t):
    return datetime.datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def commas(x):
    return format(x.normalize(), ",f")


class Pair:
    """A range event and its ladder, built like the real ones. spec[ticker] = (kind, lo, hi) in
    the fixture's own numbers: what each market means, for the payout oracle."""

    def __init__(self, series="KXBTC", days=1.0, hour=None, prices=None, close=None):
        coin = self.coin = COINS[series]
        self.series, self.ladder_series = series, coin["ladder"]
        self.tick, step, floor = tick_of(series), D(coin["step"]), D(coin["floor"])
        self.close, local = close_at(days, hour) if close is None else (
            close, datetime.datetime.fromtimestamp(close - 4 * 3600, timezone.utc))
        suffix = f"{local.year % 100:02d}{SHORT[local.month - 1].upper()}{local.day:02d}{local.hour:02d}"
        self.et, self.lt = f"{series}-{suffix}", f"{coin['ladder']}-{suffix}"
        h12, ap = local.hour % 12 or 12, "AM" if local.hour < 12 else "PM"
        self.when = f"{h12} {ap} EDT"
        self.day = f"{SHORT[local.month - 1]} {local.day}, {local.year}"
        self.spec, self.prices = {}, dict(prices or {})
        self.brackets = [(floor + i * step, floor + (i + 1) * step - self.tick) for i in range(N)]
        self.low, self.high = floor, self.brackets[-1][1]
        self.rungs = [floor + j * step - self.tick for j in range(-1, N + 2)]   # one past each end
        fair = {}
        for i, (lo, hi) in enumerate(self.brackets):
            fair[self.bracket(i)] = ODDS[i]
            self.spec[self.bracket(i)] = ("between", lo, hi)
        fair[self.bottom()], fair[self.top()] = TAIL, TAIL
        self.spec[self.bottom()] = ("less", None, self.low)
        self.spec[self.top()] = ("greater", self.high, None)
        for j, k in enumerate(self.rungs):
            fair[self.rung(j - 1)] = 100 if j == 0 else 0 if j == N + 2 else sum(ODDS[j - 1:]) + TAIL
            self.spec[self.rung(j - 1)] = ("greater", k, None)
        self.fair = fair

    # Tickers, the way Kalshi writes them.
    def bracket(self, i):
        lo, hi = self.brackets[i]
        mid = ((lo + hi) / 2).quantize(D(1).scaleb(-self.coin["places"]), rounding=ROUND_HALF_UP)
        return f"{self.et}-B{format(mid, 'f')}"

    def bottom(self):
        return f"{self.et}-T{self.coin['fmt'](self.low, 'low')}"

    def top(self):
        return f"{self.et}-T{self.coin['fmt'](self.high, 'high')}"

    def rung(self, j):
        """The ladder's rungs: j = 0 is one tick under the first bracket, j = N its top."""
        return f"{self.lt}-T{self.coin['fmt'](self.rungs[j + 1], 'rung')}"

    def quote(self, ticker):
        """(bid, ask) in dollars: an override, or the fair price 1c either side (None: no quote)."""
        if ticker in self.prices:
            return self.prices[ticker]
        f = self.fair[ticker]
        return (f - 1) / 100 if f > 1 else None, (f + 1) / 100 if f < 99 else None

    def market(self, ticker, event):
        coin, (kind, lo, hi) = self.coin, self.spec[ticker]
        fmt, d = coin["fmt"], coin["dollar"]
        if kind == "between":
            cond, sub = f"between {d}{fmt(lo, 'floor')}-{fmt(hi, 'cap')}", f"${commas(lo)} to {commas(hi)}"
        elif kind == "less":
            cond, sub = f"below {d}{fmt(hi, 'low')}", f"${commas(hi - self.tick)} or below"
        else:
            role = "rung" if event == self.lt else "high"
            cond, sub = f"above {d}{fmt(lo, role)}", f"${commas(lo + self.tick)} or above"
        m = {"ticker": ticker, "event_ticker": event, "status": "active", "market_type": "binary",
             "settlement_bounds_type": "default", "can_close_early": True,
             "title": f"{coin['name']} price{' range' if event == self.et else ''} on {self.day}?",
             "yes_sub_title": sub, "subtitle": sub,
             "rules_primary": f"If {coin['index']} before {self.when} is {cond} at {self.when} on {self.day}, "
                              "then the market resolves to Yes.",
             "rules_secondary": coin["second"], "close_time": iso(self.close),
             "expected_expiration_time": iso(self.close + 300), "open_time": iso(self.close - 3600)}
        if coin["custom"]:
            m.update(strike_type="custom", custom_strike={
                "cap_strike": "" if hi is None else fmt(hi, "cap"),
                "floor_strike": "" if lo is None else fmt(lo, "floor"),
                "round_digits": str(coin["digits"]), "strike_type": kind})
        else:
            m.update(strike_type=kind, custom_strike={"round_digits": str(coin["digits"])})
            for field, v in (("floor_strike", lo), ("cap_strike", hi)):
                if v is not None:
                    m[field] = int(v) if v == v.to_integral() else float(v)   # JSON numbers
        bid, ask = self.quote(ticker)
        m["yes_bid_dollars"] = f"{bid:.4f}" if bid else "0.0000"
        m["yes_ask_dollars"] = f"{ask:.4f}" if ask else "0.0000"
        return m

    def events(self):
        rng = [self.market(self.bottom(), self.et)] + [self.market(self.bracket(i), self.et) for i in range(N)] + \
              [self.market(self.top(), self.et)]
        lad = [self.market(self.rung(j), self.lt) for j in range(-1, N + 2)]
        word = "Shiba Inu" if self.series == "KXSHIBA" else self.coin["name"]
        return [{"event_ticker": self.et, "series_ticker": self.series, "title": f"{word} price range on {self.day}?",
                 "mutually_exclusive": True, "strike_date": iso(self.close), "markets": rng},
                {"event_ticker": self.lt, "series_ticker": self.ladder_series, "title": f"{word} price on {self.day}?",
                 "mutually_exclusive": False, "strike_date": iso(self.close), "markets": lad}]


def fake_for(events, size=40):
    from kal.client import price
    c = FakeKalshi()
    c.event_list = list(events)
    for ev in events:
        for m in ev["markets"]:
            bid, ask = price(m, "yes_bid"), price(m, "yes_ask")
            c.books[m["ticker"]] = {"yes": [(bid, size)] if bid else [],
                                    "no": [(round(1 - ask, 4), size)] if ask else []}
    return c


def ctx(client, budget=100.0, held=(), mode="paper", held_paper=frozenset()):
    return Context(client, dict(config.DEFAULTS, max_per_arb=30.0), mode, budget, budget, set(held), {}, {},
                   held_paper=held_paper)


def pays(spec, ticker, side, v, lo_in=True, hi_in=True):
    """What one contract pays if the price is v, read from the fixture's own numbers."""
    kind, lo, hi = spec[ticker]
    if kind == "greater":
        yes = v > lo
    elif kind == "less":
        yes = v < hi
    else:
        yes = (v > lo or (lo_in and v == lo)) and (v < hi or (hi_in and v == hi))
    return 1 if yes == (side == "yes") else 0


def prices_to_try(p):
    """Every strike, a tick and half a tick either side (71799.995), and far beyond both ends."""
    xs = {x for kind, lo, hi in p.spec.values() for x in (lo, hi) if x is not None}
    out = {x + d * p.tick for x in xs for d in (-1, D("-0.5"), 0, D("0.5"), 1)}
    return sorted(out | {D(0), p.tick / 2, min(xs) / 3, max(xs) * 3})


def worst(p, legs):
    """The least one contract of each leg can pay: over every price, both readings of each
    bracket end, and no data (every strike No)."""
    out = sum(side == "no" for _, side in legs)
    for v in prices_to_try(p):
        for lo_in in (True, False):
            for hi_in in (True, False):
                out = min(out, sum(pays(p.spec, t, side, v, lo_in, hi_in) for t, side in legs))
    return out


def tickers(legs):
    return [(m["ticker"], side) for m, side in legs]


# Gaps, one per kind of set (bid, ask in dollars), each 4-16% after fees.
def sell3_gap(p):     # someone bids 40c for a 25c bracket: sell it, and hedge on the ladder
    return {p.bracket(5): (.40, .42)}


def superset_gap(p):  # a 3c top bracket bid at 20c: the ladder's YES above its floor is 5c
    return {p.bracket(9): (.20, .22)}


def disjoint_gap(p):  # a 5c bracket bid at 16c, while NO above its top costs 10c
    return {p.bracket(2): (.16, .18)}


def tail_gap(p):      # someone bids 10c for the ladder's top rung: YES on the same top tail is 2c
    return {p.rung(N): (.10, .12)}


def gap_pair(series="KXBTC", gap=sell3_gap, **kw):
    p = Pair(series, **kw)
    p.prices.update(gap(p))
    return p


# Real markets from the API (2026-10-29, closing 2 PM EDT; SHIB 5 PM), the fields the strategy
# reads. The fixtures must build exactly these.
REAL = [
    {"ticker": "KXBTC-26OCT2914-B71750", "event_ticker": "KXBTC-26OCT2914", "strike_type": "between",
     "floor_strike": 71700, "cap_strike": 71799.99, "custom_strike": {"round_digits": "2"},
     "yes_sub_title": "$71,700 to 71,799.99", "close_time": "2026-10-29T18:00:00Z",
     "expected_expiration_time": "2026-10-29T18:05:00Z", "rules_secondary": SIXTY,
     "rules_primary": "If the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time Index (BRTI) "
                      "before 2 PM EDT is between 71700-71799.99 at 2 PM EDT on Oct 29, 2026, then the market "
                      "resolves to Yes."},
    {"ticker": "KXBTC-26OCT2914-T71700", "event_ticker": "KXBTC-26OCT2914", "strike_type": "less",
     "cap_strike": 71700, "custom_strike": {"round_digits": "2"}, "yes_sub_title": "$71,699.99 or below",
     "rules_primary": "If the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time Index (BRTI) "
                      "before 2 PM EDT is below 71700 at 2 PM EDT on Oct 29, 2026, then the market resolves to Yes."},
    {"ticker": "KXBTCD-26OCT2914-T71599.99", "event_ticker": "KXBTCD-26OCT2914", "strike_type": "greater",
     "floor_strike": 71599.99, "custom_strike": {"round_digits": "2"}, "yes_sub_title": "$71,600 or above",
     "rules_primary": "If the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time Index (BRTI) "
                      "before 2 PM EDT is above 71599.99 at 2 PM EDT on Oct 29, 2026, then the market resolves to "
                      "Yes."},
    {"ticker": "KXXRP-26OCT2914-B0.5899500", "event_ticker": "KXXRP-26OCT2914", "strike_type": "between",
     "floor_strike": 0.58, "cap_strike": 0.5999, "custom_strike": {"round_digits": "4"},
     "yes_sub_title": "$0.58 to 0.5999", "rules_secondary": XRP_SECOND,
     "rules_primary": "If the simple average of the sixty seconds of CF Benchmarks' Ripple-Dollar Real Time Index "
                      "(XRPUSD_RTI) before 2 PM EDT is between 0.58000-0.5999 at 2 PM EDT on Oct 29, 2026, then the "
                      "market resolves to Yes."},
    {"ticker": "KXXRP-26OCT2914-T0.58000", "event_ticker": "KXXRP-26OCT2914", "strike_type": "less",
     "cap_strike": 0.58, "custom_strike": {"round_digits": "4"},
     "rules_primary": "If the simple average of the sixty seconds of CF Benchmarks' Ripple-Dollar Real Time Index "
                      "(XRPUSD_RTI) before 2 PM EDT is below 0.58000 at 2 PM EDT on Oct 29, 2026, then the market "
                      "resolves to Yes."},
    {"ticker": "KXDOGE-26OCT2914-B0.012", "event_ticker": "KXDOGE-26OCT2914", "strike_type": "custom",
     "custom_strike": {"cap_strike": "0.0149999", "floor_strike": "0.0100000", "round_digits": "7",
                       "strike_type": "between"},
     "yes_sub_title": "$0.01 to 0.0149999", "rules_secondary": NOT_ALL,
     "rules_primary": "If there is a 60 second average of CF Benchmarks' Dogecoin Real-Time Index (DOGEUSD_RTI) "
                      "before 2 PM EDT is between 0.0100000-0.0149999 at 2 PM EDT on Oct 29, 2026, then the market "
                      "resolves to Yes."},
    {"ticker": "KXDOGED-26OCT2914-T0.0049999", "event_ticker": "KXDOGED-26OCT2914", "strike_type": "custom",
     "custom_strike": {"cap_strike": "", "floor_strike": "0.0049999", "round_digits": "7", "strike_type": "greater"},
     "yes_sub_title": "$0.005 or above",
     "rules_primary": "If there is a 60 second average of CF Benchmarks' Dogecoin Real-Time Index (DOGEUSD_RTI) "
                      "before 2 PM EDT is above 0.0049999 at 2 PM EDT on Oct 29, 2026, then the market resolves to "
                      "Yes."},
    {"ticker": "KXSHIBA-26OCT2917-B0.0000012", "event_ticker": "KXSHIBA-26OCT2917", "strike_type": "custom",
     "custom_strike": {"cap_strike": "0.000001499", "floor_strike": "0.000001000", "round_digits": "9",
                       "strike_type": "between"},
     "yes_sub_title": "$0.000001 to 0.000001499", "close_time": "2026-10-29T21:00:00Z", "rules_secondary": SIXTY,
     "rules_primary": "If the simple average of the 60 seconds of CF Benchmarks' Shiba Inu-Dollar Spot Rate "
                      "(SHIBUSD_RTI) before 5 PM EDT is between 0.000001000-0.000001499 at 5 PM EDT on Oct 29, "
                      "2026, then the market resolves to Yes."},
]


class RealShapeTests(unittest.TestCase):
    def test_the_fixtures_build_the_real_markets(self):
        two_pm = int(datetime.datetime(2026, 10, 29, 18, tzinfo=timezone.utc).timestamp())
        for real in REAL:
            series = real["event_ticker"].split("-")[0]
            series = next(r for r, c in COINS.items() if series in (r, c["ladder"]))
            p = Pair(series, close=two_pm + (3 * 3600 if series == "KXSHIBA" else 0))
            built = p.market(real["ticker"], real["event_ticker"])
            for field, want in real.items():
                self.assertEqual(built.get(field), want, (real["ticker"], field))
            for field in ("floor_strike", "cap_strike"):
                self.assertEqual(field in built, field in real, (real["ticker"], field))


class LeanEventsTests(TempHome):
    """The run's lean events list (engine.RunCache) changes no trade, and the fixture trades when near."""

    def accepted(self, client, horizon):
        rc = engine.RunCache(client)
        rc.scope((), horizon)
        cfg = dict(config.DEFAULTS)
        limits, out = risk.Limits(cfg, 100.0), []
        for sig in STRATEGY.scan(Context(rc, cfg, "paper", 100.0, 100.0, set(), {}, {})):
            if not limits.refuse(sig):
                limits.spend(sig)
                legs = sorted((leg.ticker, leg.side, leg.count) for leg in sig.legs)
                out.append((sig.event, round(sig.cost, 4), legs))
        return out

    def test_the_same_trades_with_and_without_the_filter(self):
        cut = engine.horizon_days(config.DEFAULTS)
        for days, trades in ((2, True), (29.5, True), (30.5, False), (40, False)):
            c = fake_for(gap_pair(days=days).events())
            got = self.accepted(c, cut)
            self.assertEqual(got, self.accepted(c, float("inf")), days)
            self.assertEqual(bool(got), trades, days)


class PairingTests(unittest.TestCase):
    def test_every_coin_pairs_hourly_and_at_5_pm(self):
        for series in COINS:
            for hour in ([None, 17] if series in HOURLY else [17]):
                p = Pair(series, hour=hour)
                why = []
                found = range_link_arb.pairs(p.events(), why)
                self.assertEqual(why, [], series)
                self.assertEqual(len(found), 1, series)
                got = found[0]
                self.assertEqual((got["range"]["event"], got["above"]["event"], got["close"]), (p.et, p.lt, p.close))
                self.assertEqual(len(got["brackets"]), N)
                self.assertEqual(len(got["ladder"]), N + 3)
                self.assertEqual(got["tick"], p.tick)

    def test_the_real_numbers_are_read_exactly(self):
        # No float rounding: a strike of 71799.99 is 71799.99, DOGE's are strings.
        for series in ("KXBTC", "KXXRP", "KXDOGE", "KXSHIBA"):
            p = Pair(series, hour=17)
            got = range_link_arb.pairs(p.events())[0]
            self.assertEqual([(b["lo"], b["hi"]) for b in got["brackets"]], p.brackets)
            self.assertEqual([k for k, _ in got["ladder"]], p.rungs)
            self.assertEqual((got["low"]["hi"], got["high"]["lo"]), (p.low, p.high))

    def test_many_pairs_at_once(self):
        events = [ev for s in COINS for h in (5, 17) for ev in Pair(s, hour=h).events()]
        self.assertEqual(len(range_link_arb.pairs(events)), 2 * len(COINS))


class LinkTests(unittest.TestCase):
    def test_every_set_pays_what_it_says_whatever_the_price(self):
        for series in COINS:
            for gap in (lambda p: {}, sell3_gap, superset_gap, disjoint_gap, tail_gap):
                p = gap_pair(series, gap, hour=17)
                pair = range_link_arb.pairs(p.events())[0]
                every = range_link_arb.links(pair)
                self.assertGreater(len(every), 3 * N - 3)
                for s in every:
                    legs = [(m["ticker"], side) for m, side in s["legs"]]
                    self.assertGreaterEqual(worst(p, legs), s["pays"], (series, s["set"], legs))
                    self.assertTrue(range_link_arb.check(pair, s["legs"], s["pays"]))

    def test_only_the_four_safe_kinds(self):
        # Never YES on a bracket (YES bracket + NO above + YES above needs the price on the
        # cent) and never YES on the bottom tail (YES below + YES above loses if no data comes).
        for series in COINS:
            p = Pair(series, hour=17)
            pair = range_link_arb.pairs(p.events())[0]
            for s in range_link_arb.links(pair):
                self.assertIn(s["set"], ("superset", "disjoint", "sell3", "tail"))
                for m, side in s["legs"]:
                    if side == "yes" and m["event_ticker"] == p.et:
                        self.assertEqual(m["ticker"], p.top(), s)   # only the top tail, the same as a rung

    def test_cheap_unsafe_sets_are_never_offered(self):
        # Make the two unsafe shapes nearly free: YES on a bracket and on both tails at 1c.
        p = Pair()
        p.prices.update({p.bracket(4): (None, .01), p.rung(4): (.98, .99), p.rung(5): (None, .01),
                         p.bottom(): (None, .01), p.rung(-1): (None, .01)})
        sigs = STRATEGY.scan(ctx(fake_for(p.events())))
        for sig in sigs:
            self.assertIn(sig.meta["set"], ("superset", "disjoint", "sell3", "tail"))
            for leg in sig.legs:
                self.assertFalse(leg.side == "yes" and leg.ticker in (p.bracket(4), p.bottom()), sig.note)

    def test_the_check_refuses_unsafe_sets(self):
        p = Pair()
        pair = range_link_arb.pairs(p.events())[0]
        mk = {t: r["m"] for t, r in pair["records"].items()}

        def legs(*ls):
            return [(mk[t], side) for t, side in ls]
        # YES a bracket + NO the rung under it + YES the rung at its top: $1 on the cent only.
        buy3 = [(p.bracket(4), "yes"), (p.rung(4), "no"), (p.rung(5), "yes")]
        # YES the bottom tail + YES a rung under it: both lose if no data comes.
        cover = [(p.bottom(), "yes"), (p.rung(-1), "yes")]
        for unsafe in (buy3, cover):
            self.assertEqual(worst(p, unsafe), 0)    # the oracle agrees: it can pay nothing
            self.assertFalse(range_link_arb.check(pair, legs(*unsafe), 1))
        good = [(p.rung(4), "yes"), (p.bracket(4), "no")]   # rung 4 is one tick under bracket 4
        self.assertEqual(worst(p, good), 1)
        self.assertTrue(range_link_arb.check(pair, legs(*good), 1))
        self.assertFalse(range_link_arb.check(pair, legs(*good), 2))                    # claims more than it pays
        self.assertFalse(range_link_arb.check(pair, legs((p.bracket(4), "no"), (p.bracket(4), "no")), 1))
        self.assertFalse(range_link_arb.check(pair, [], 1))
        # A YES whose strike is the bracket's own floor (not under it) loses at the floor.
        moved = copy.deepcopy(pair)
        moved["records"][p.rung(4)]["lo"] = p.brackets[4][0]
        mk2 = {t: r["m"] for t, r in moved["records"].items()}
        self.assertFalse(range_link_arb.check(moved, [(mk2[p.rung(4)], "yes"), (mk2[p.bracket(4)], "no")], 1))

    def test_the_check_plays_each_bracket_end_in_and_out(self):
        p = Pair()
        pair = range_link_arb.pairs(p.events())[0]
        lo, hi = p.brackets[4]
        mk = {t: r["m"] for t, r in pair["records"].items()}
        # Made-up markets: below the floor, below the top, above the floor.
        for t, kind, a, b in (("X-LO", "less", None, lo), ("X-HI", "less", None, hi), ("X-UP", "greater", lo, None)):
            pair["records"][t] = {"m": {"ticker": t}, "kind": kind, "lo": a, "hi": b}
        floor_in = [({"ticker": "X-LO"}, "yes"), (mk[p.bracket(4)], "yes"), ({"ticker": "X-HI"}, "no")]
        top_in = [(mk[p.bracket(4)], "yes"), ({"ticker": "X-UP"}, "no"), (mk[p.rung(5)], "yes")]
        self.assertFalse(range_link_arb.check(pair, floor_in, 1))   # pays only if the floor counts in
        self.assertFalse(range_link_arb.check(pair, top_in, 1))     # pays only if the top counts in

    def test_the_check_plays_prices_between_the_cents(self):
        # NO below 71700 + NO above 71699.99: fine on every cent, nothing at 71699.995.
        p = Pair()
        pair = range_link_arb.pairs(p.events())[0]
        mk = {t: r["m"] for t, r in pair["records"].items()}
        legs = [(p.bottom(), "no"), (p.rung(0), "no")]
        self.assertEqual(worst(p, legs), 0)
        self.assertFalse(range_link_arb.check(pair, [(mk[t], side) for t, side in legs], 1))
        self.assertNotIn(sorted(legs), [sorted(tickers(s["legs"])) for s in range_link_arb.links(pair)])

    def test_a_rung_on_a_brackets_floor_is_not_under_it(self):
        # An extra rung exactly at bracket 5's floor, and the cheapest YES of all: "above" it
        # misses the floor itself, so it can't back NO on that bracket.
        p = Pair()
        f = p.brackets[5][0]
        t = f"{p.lt}-T{plain(f)}"
        p.spec[t], p.fair[t] = ("greater", f, None), sum(ODDS[5:]) + TAIL
        p.prices[t] = (None, .05)
        evs = p.events()
        evs[1]["markets"].append(p.market(t, p.lt))
        pair = range_link_arb.pairs(evs)[0]
        self.assertIn(t, pair["records"])
        for s in range_link_arb.links(pair):
            legs = tickers(s["legs"])
            self.assertGreaterEqual(worst(p, legs), s["pays"], legs)

    def test_the_cheapest_rung_is_chosen_by_listed_price(self):
        # The rung just under a bracket has no ask; the next one down is used instead.
        p = Pair()
        p.prices[p.rung(5)] = (.60, None)
        pair = range_link_arb.pairs(p.events())[0]
        sup = [s for s in range_link_arb.links(pair)
               if s["set"] == "superset" and s["legs"][1][0]["ticker"] == p.bracket(5)]
        self.assertEqual(tickers(sup[0]["legs"]), [(p.rung(4), "yes"), (p.bracket(5), "no")])


class FailsClosedTests(unittest.TestCase):
    """Anything that doesn't read exactly like the real markets: no pair."""

    def assert_no_pair(self, change, series="KXBTC", why=None):
        p = Pair(series, hour=17)
        evs = p.events()
        change(p, evs[0], evs[1])
        reasons = []
        self.assertEqual(range_link_arb.pairs(evs, reasons), [], series)
        self.assertTrue(reasons, series)
        if why:
            self.assertIn(why, reasons[0][1])

    def m(self, ev, ticker):
        return next(m for m in ev["markets"] if m["ticker"] == ticker)

    def test_wording(self):
        def say(old, new, which="range", field="rules_primary"):
            def change(p, rng, lad):
                ev = rng if which == "range" else lad
                m = ev["markets"][3]
                m[field] = m[field].replace(old, new)
            return change
        for change in (say("sixty seconds", "thirty seconds"), say("is between", "is from"),
                       say("is above", "is at or above", "ladder"), say("resolves to Yes.", "resolves to Yes or No."),
                       say("Bitcoin Real-Time Index (BRTI)", "Bitcoin Reference Rate (BRR)", "ladder"),
                       say("60 RTI prices", "30 RTI prices", field="rules_secondary"),
                       say("Not all", "Some", "ladder", "rules_secondary"),
                       say(" to ", " up to ", field="yes_sub_title"),
                       say("or above", "or more", "ladder", "yes_sub_title")):
            self.assert_no_pair(change)
        self.assert_no_pair(lambda p, rng, lad: rng["markets"][0].update(rules_secondary=SIXTY + " Or a fair price."))
        self.assert_no_pair(lambda p, rng, lad: lad["markets"][0].update(rules_secondary=""))
        # "$" where the coin has none, and none where it has one.
        self.assert_no_pair(lambda p, rng, lad: lad["markets"][2].update(
            rules_primary=lad["markets"][2]["rules_primary"].replace("is above ", "is above $")))
        self.assert_no_pair(lambda p, rng, lad: rng["markets"][4].update(
            rules_primary=rng["markets"][4]["rules_primary"].replace("$", "")), "KXSOLE")
        # Another coin's wording on this coin's market.
        self.assert_no_pair(lambda p, rng, lad: rng["markets"][3].update(rules_secondary=XRP_SECOND))

    def test_times(self):
        later = lambda t, s: iso(range_link_arb.close_ts({"close_time": t}) + s)   # noqa: E731
        self.assert_no_pair(lambda p, rng, lad: lad["markets"][5].update(
            close_time=later(lad["markets"][5]["close_time"], 1)), why="one time")
        self.assert_no_pair(lambda p, rng, lad: rng["markets"][0].update(
            expected_expiration_time=later(rng["markets"][0]["expected_expiration_time"], 60)))

        def an_hour_later(p, rng, lad):   # the ladder closes an hour after the range event, everything in step
            for m in lad["markets"]:
                m["close_time"] = later(m["close_time"], 3600)
                m["expected_expiration_time"] = later(m["expected_expiration_time"], 3600)
        self.assert_no_pair(an_hour_later)

        def rules_say_another_hour(p, rng, lad):
            for m in rng["markets"] + lad["markets"]:
                m["rules_primary"] = m["rules_primary"].replace(p.when, "3 AM EDT")
        self.assert_no_pair(rules_say_another_hour, why="another time")

        def the_ticker_names_another_hour(p, rng, lad):
            for ev in (rng, lad):
                et = ev["event_ticker"][:-2] + "03"
                for m in ev["markets"]:
                    m.update(event_ticker=et, ticker=et + m["ticker"][len(ev["event_ticker"]):])
                ev["event_ticker"] = et
        self.assert_no_pair(the_ticker_names_another_hour, why="another hour")
        self.assert_no_pair(lambda p, rng, lad: rng.update(strike_date=later(rng["strike_date"], 3600)))
        self.assert_no_pair(lambda p, rng, lad: rng["markets"][2].update(close_time="soon"))

    def test_grid_off_by_a_tick(self):
        def shift(field, d, which="range", i=4):
            def change(p, rng, lad):
                m = (rng if which == "range" else lad)["markets"][i]
                m[field] = float(D(str(m[field])) + d * p.tick)
            return change
        self.assert_no_pair(shift("floor_strike", 1))                    # brackets no longer end to end
        self.assert_no_pair(shift("cap_strike", -1))
        self.assert_no_pair(shift("floor_strike", 1, "ladder"))          # its ticker and rules say otherwise

        def rung_moves(p, rng, lad):   # a rung moves a tick, ticker and rules with it: off the grid
            m = lad["markets"][4]
            old = m["floor_strike"]
            new = D(str(old)) + p.tick
            m.update(floor_strike=float(new), ticker=m["ticker"].replace(str(old), plain(new)),
                     rules_primary=m["rules_primary"].replace(f"above {old}", f"above {plain(new)}"))
        self.assert_no_pair(rung_moves, why="grid")
        self.assert_no_pair(lambda p, rng, lad: rng["markets"][3].update(floor_strike=71900.005))   # off the tick

    def test_strikes_that_disagree(self):
        self.assert_no_pair(lambda p, rng, lad: lad["markets"][3].update(ticker=lad["markets"][3]["ticker"] + "9"))
        self.assert_no_pair(lambda p, rng, lad: lad["markets"][3].update(ticker=lad["markets"][4]["ticker"]))
        self.assert_no_pair(lambda p, rng, lad: self.m(rng, p.bracket(3)).update(ticker=p.bracket(4)))
        self.assert_no_pair(lambda p, rng, lad: self.m(rng, p.bracket(3)).update(
            ticker=p.bracket(3).replace("-B", "-T")))
        self.assert_no_pair(lambda p, rng, lad: self.m(rng, p.bracket(3)).update(
            rules_primary=self.m(rng, p.bracket(3))["rules_primary"].replace("-72099.99", "-72099.98")))
        self.assert_no_pair(lambda p, rng, lad: self.m(rng, p.bracket(3)).update(
            yes_sub_title="$72,000 to 72,100"))
        self.assert_no_pair(lambda p, rng, lad: self.m(rng, p.top()).update(floor_strike=90000))
        self.assert_no_pair(lambda p, rng, lad: lad["markets"][6].update(
            custom_strike={"round_digits": "2", "front_month_contract": "N/A"}))
        self.assert_no_pair(lambda p, rng, lad: lad["markets"][6].update(custom_strike={"round_digits": "3"}))
        # DOGE: strikes in custom_strike only, as strings.
        self.assert_no_pair(lambda p, rng, lad: rng["markets"][3].update(floor_strike=0.025), "KXDOGE")
        self.assert_no_pair(lambda p, rng, lad: rng["markets"][3].update(strike_type="between"), "KXDOGE")
        self.assert_no_pair(lambda p, rng, lad: rng["markets"][3]["custom_strike"].update(floor_strike="0.0249999"),
                            "KXDOGE")

    def test_a_rung_off_the_tick(self):
        def extra(p, rng, lad):   # 71650.005: ticker, rules and strike agree, but not on the cent
            t = f"{p.lt}-T71650.005"
            p.spec[t], p.fair[t] = ("greater", D("71650.005"), None), 99
            lad["markets"].append(p.market(t, p.lt))
        self.assert_no_pair(extra, why="tick")

    def test_tails_that_dont_meet_the_brackets(self):
        def bottom_moves(p, rng, lad):   # "below 71600": ticker, rules and strike all agree
            m = self.m(rng, p.bottom())
            m.update(ticker=m["ticker"].replace("T71700", "T71600"), cap_strike=71600,
                     yes_sub_title="$71,599.99 or below",
                     rules_primary=m["rules_primary"].replace("below 71700", "below 71600"))
        self.assert_no_pair(bottom_moves, why="tails")

        def top_moves(p, rng, lad):
            m = self.m(rng, p.top())
            m.update(ticker=m["ticker"].replace("T72699.99", "T72799.99"), floor_strike=72799.99,
                     rules_primary=m["rules_primary"].replace("above 72699.99", "above 72799.99"))
        self.assert_no_pair(top_moves, why="tails")

    def test_the_two_events_must_end_together(self):
        def ladder_expires_later(p, rng, lad):   # every rung in step, so only the pair check sees it
            for m in lad["markets"]:
                m["expected_expiration_time"] = iso(p.close + 360)
        self.assert_no_pair(ladder_expires_later, why="together")

        def ladder_says_est(p, rng, lad):        # same ticker, but "EST" puts it an hour later
            for m in lad["markets"]:
                m["rules_primary"] = m["rules_primary"].replace("EDT", "EST")
                m["close_time"] = iso(p.close + 3600)
                m["expected_expiration_time"] = iso(p.close + 3900)
            lad["strike_date"] = iso(p.close + 3600)
        self.assert_no_pair(ladder_says_est, why="together")

    def test_a_strike_twice(self):
        def twice(p, rng, lad):   # 71999.990 is 71999.99 under another ticker
            m = copy.deepcopy(self.m(lad, p.rung(3)))
            m["ticker"] = m["ticker"] + "0"
            lad["markets"].append(m)
        self.assert_no_pair(twice, why="twice")

    def test_a_bracket_in_the_ladder(self):
        def bracket_in_ladder(p, rng, lad):
            m = copy.deepcopy(self.m(rng, p.bracket(4)))
            m.update(event_ticker=lad["event_ticker"],
                     ticker=lad["event_ticker"] + m["ticker"][len(rng["event_ticker"]):])
            lad["markets"].append(m)
        self.assert_no_pair(bracket_in_ladder, why="rung")

    def test_a_market_missing(self):
        self.assert_no_pair(lambda p, rng, lad: rng["markets"].pop(5))           # a bracket: they don't tile
        self.assert_no_pair(lambda p, rng, lad: rng["markets"].pop(0))           # the bottom tail
        self.assert_no_pair(lambda p, rng, lad: rng["markets"].pop())            # the top tail
        self.assert_no_pair(lambda p, rng, lad: lad["markets"].pop(6))           # a rung a bracket ends on
        self.assert_no_pair(lambda p, rng, lad: lad.update(markets=[]))
        self.assert_no_pair(lambda p, rng, lad: lad.clear())

    def test_odd_strike_types(self):
        for kind in ("greater_or_equal", "less_or_equal", "custom", "functional", None):
            self.assert_no_pair(lambda p, rng, lad, k=kind: lad["markets"][2].update(strike_type=k))
        self.assert_no_pair(lambda p, rng, lad: rng["markets"][4].update(strike_type="greater"))
        self.assert_no_pair(lambda p, rng, lad: lad["markets"][2].update(cap_strike=72000))   # a rung with a cap
        self.assert_no_pair(lambda p, rng, lad: rng["markets"][4].update(cap_strike=True))
        self.assert_no_pair(lambda p, rng, lad: rng["markets"][4].update(cap_strike="NaN"))

    def test_payouts_that_arent_plain(self):
        self.assert_no_pair(lambda p, rng, lad: rng["markets"][0].update(market_type="scalar"))
        self.assert_no_pair(lambda p, rng, lad: lad["markets"][1].update(settlement_bounds_type="floor"))
        self.assert_no_pair(lambda p, rng, lad: rng["markets"][2].update(status="closed"))

    def test_mixed_events(self):
        self.assert_no_pair(lambda p, rng, lad: rng.update(mutually_exclusive=False))
        self.assert_no_pair(lambda p, rng, lad: lad.update(mutually_exclusive=True))
        self.assert_no_pair(lambda p, rng, lad: lad.update(series_ticker="KXETHD"))
        self.assert_no_pair(lambda p, rng, lad: rng["markets"].append(copy.deepcopy(lad["markets"][5])))
        self.assert_no_pair(lambda p, rng, lad: lad["markets"].append(copy.deepcopy(lad["markets"][5])))
        other = Pair("KXETH", hour=17).events()[0]["markets"][4]
        self.assert_no_pair(lambda p, rng, lad: rng["markets"].__setitem__(4, copy.deepcopy(other)))
        p = Pair(hour=17)
        evs = p.events()
        self.assertEqual(range_link_arb.pairs(evs + [copy.deepcopy(evs[1])]), [])   # two ladders with one ticker
        self.assertEqual(range_link_arb.pairs(evs + [copy.deepcopy(evs[0])]), [])
        self.assertEqual(range_link_arb.pairs(evs[:1]), [])                        # no ladder at all
        self.assertEqual(range_link_arb.pairs(evs[1:]), [])
        eth = Pair("KXETH", hour=17).events()
        self.assertEqual(range_link_arb.pairs([evs[0], eth[1]]), [])               # another coin's ladder


class ScanTests(TempHome):
    def test_no_gap_no_signal(self):
        for series in COINS:
            self.assertEqual(STRATEGY.scan(ctx(fake_for(Pair(series).events()))), [], series)

    def test_each_kind_of_gap_is_bought_on_paper(self):
        want = {sell3_gap: ("sell3", 2), superset_gap: ("superset", 1), disjoint_gap: ("disjoint", 1),
                tail_gap: ("tail", 1)}
        for series in COINS:
            for gap, (name, pay) in want.items():
                p = gap_pair(series, gap, hour=17)
                sigs = STRATEGY.scan(ctx(fake_for(p.events())))
                self.assertTrue(sigs, (series, name))
                sig = sigs[0]
                self.assertEqual((sig.meta["set"], sig.meta["pays"]), (name, pay), series)
                self.assertTrue(sig.guaranteed and sig.meta["paper_only"])
                c = sig.legs[0].count
                self.assertEqual(sig.payout_min, c * pay)
                self.assertLess(sig.cost, sig.payout_min)
                self.assertLessEqual(sig.payout_min / sig.cost - 1, 0.25)
                self.assertEqual((sig.event, sig.closes_at, sig.meta["first_close"]), (p.et, p.close, p.close))
                self.assertEqual((sig.meta["range_event"], sig.meta["above_event"]), (p.et, p.lt))
                self.assertIn(p.when, sig.note)
                self.assertGreaterEqual(worst(p, [(leg.ticker, leg.side) for leg in sig.legs]), pay)

    def test_a_sell3_set_is_sized_for_its_2_dollars(self):
        p = gap_pair()
        sig = STRATEGY.scan(ctx(fake_for(p.events())))[0]
        self.assertEqual(sorted((leg.ticker, leg.side) for leg in sig.legs),
                         sorted([(p.bracket(5), "no"), (p.rung(5), "yes"), (p.rung(6), "no")]))
        c = sig.legs[0].count
        self.assertTrue(all(leg.count == c for leg in sig.legs))
        self.assertAlmostEqual(sig.exp_profit, 2 * c - sig.cost)
        # Sized like any basket: the most profit within max_per_trade if it stops half built.
        self.assertLessEqual(sig.partial_loss, config.DEFAULTS["max_per_trade"] + 1e-9)
        bigger = [(leg.ticker, leg.side, leg.price, 40) for leg in sig.legs]
        self.assertGreater(ladder_arb.partial_cost(bigger, c + 1), config.DEFAULTS["max_per_trade"])

    def test_too_close_to_the_close_is_skipped(self):
        p = gap_pair(days=0)
        with mock.patch.object(range_link_arb.time, "time", return_value=p.close - 200):
            self.assertEqual(STRATEGY.scan(ctx(fake_for(p.events()))), [])
        with mock.patch.object(range_link_arb.time, "time", return_value=p.close - 400):
            self.assertTrue(STRATEGY.scan(ctx(fake_for(p.events()))))

    def test_a_held_leg_is_skipped(self):
        p = gap_pair()
        self.assertEqual(STRATEGY.scan(ctx(fake_for(p.events()), held={p.bracket(5)})), [])

    def test_a_paper_set_leaves_what_is_already_open_on_paper(self):
        # A live scan (some other pair pinned): an unpinned set that reuses a paper holding
        # would take its tickers from the next best set, then the engine would skip it.
        p = gap_pair()
        p.prices.update(tail_gap(p))
        both = STRATEGY.scan(ctx(fake_for(p.events())))
        self.assertEqual(sorted(s.meta["set"] for s in both), ["sell3", "tail"])
        held = frozenset({p.rung(5)})   # the sell3 set's YES leg, open on paper
        sigs = STRATEGY.scan(ctx(fake_for(p.events()), mode="live", held_paper=held))
        self.assertEqual([s.meta["set"] for s in sigs], ["tail"])
        self.assertFalse([leg.ticker for s in sigs for leg in s.legs if leg.ticker in held])

    def pin(self, evs, above=None, rules=None):
        pair = range_link_arb.pairs(evs)[0]
        patcher = mock.patch.dict(range_link_arb.VERIFIED, {evs[0]["event_ticker"]: {
            "above": above or evs[1]["event_ticker"], "rules": rules or range_link_arb.rules_key(pair)}})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_a_disjoint_set_of_a_pinned_pair_may_trade_live(self):
        # If one event got no data (every strike No) while the other settled on the price, only
        # NO + NO still pays. So the other sets stay on paper even for a pinned pair.
        for gap, live in ((disjoint_gap, True), (sell3_gap, False), (superset_gap, False), (tail_gap, False)):
            mock.patch.stopall()
            evs = gap_pair(gap=gap).events()
            self.pin(evs)
            sigs = STRATEGY.scan(ctx(fake_for(evs)))
            self.assertTrue(sigs, gap.__name__)
            self.assertEqual(not sigs[0].meta["paper_only"], live, (gap.__name__, sigs[0].meta["set"]))
        self.assertEqual(STRATEGY.proven(None), (True, "only event pairs a person checked by hand (VERIFIED)"))

    def test_sure_needs_every_set_to_survive_one_event_without_data(self):
        p = Pair()
        pair = range_link_arb.pairs(p.events())[0]
        for s in range_link_arb.links(pair):
            self.assertTrue(range_link_arb.check(pair, s["legs"], s["pays"]), s["set"])
            self.assertEqual(range_link_arb.sure(pair, s["legs"], s["pays"]), s["set"] == "disjoint", s["set"])

    def test_a_pin_for_other_rules_or_another_ladder_stays_on_paper(self):
        evs = gap_pair().events()
        self.pin(evs, rules="what the rules said yesterday")
        self.assertTrue(STRATEGY.scan(ctx(fake_for(evs)))[0].meta["paper_only"])
        mock.patch.stopall()
        self.pin(evs, above="KXBTCD-26JAN0117")
        self.assertTrue(STRATEGY.scan(ctx(fake_for(evs)))[0].meta["paper_only"])

    def test_moved_dates_go_back_to_paper(self):
        p = gap_pair()
        evs = p.events()
        self.pin(evs)
        moved = copy.deepcopy(evs)
        for m in moved[0]["markets"] + moved[1]["markets"]:
            m["expected_expiration_time"] = iso(p.close + 3600)
        sigs = STRATEGY.scan(ctx(fake_for(moved)))
        self.assertTrue(sigs and all(s.meta["paper_only"] for s in sigs))

    def test_too_good_to_be_true_keeps_a_pinned_pair_on_paper(self):
        # Any set of the pair under 80c a dollar (listed or in its book) keeps all of it on paper:
        # a $1 set at 79c, or a $2 set at $1.59. A real disjoint gap there then trades on paper too.
        for which, bid in ((9, .26), (5, .68)):   # superset at 79c; sell3 at $1.59
            p = Pair()
            p.prices.update(disjoint_gap(p))
            p.prices[p.bracket(which)] = (bid, bid + .02)
            evs = p.events()
            mock.patch.stopall()
            self.pin(evs)
            sigs = STRATEGY.scan(ctx(fake_for(evs)))
            self.assertTrue(sigs and all(s.meta["paper_only"] for s in sigs), which)

    def test_a_2_dollar_set_is_not_too_good_at_a_1_dollar_bar(self):
        # sell3 at $1.87 listed is over 2 x 80c: it doesn't mark the pair too good, so the
        # pair's disjoint gap may still trade live.
        p = gap_pair()
        p.prices.update(disjoint_gap(p))
        evs = p.events()
        self.pin(evs)
        sigs = STRATEGY.scan(ctx(fake_for(evs)))
        self.assertEqual({s.meta["set"]: s.meta["paper_only"] for s in sigs}.get("disjoint"), False)
        self.assertIn("sell3", [s.meta["set"] for s in sigs])

    def test_stale_quotes_on_one_pair_cant_take_every_book_slot(self):
        # Two hours of DOGE with "too good" stale quotes (refused anyway) and one real BTC gap:
        # the BTC set still gets its order books read and trades on paper.
        btc = gap_pair()
        stale = []
        for hour in (14, 15):
            d = Pair("KXDOGE", hour=hour)
            d.prices.update({d.bracket(i): (.60, .62) for i in range(N)})
            stale += d.events()
        c = fake_for(btc.events() + stale)
        read, real = [], c.orderbook
        c.orderbook = lambda t: read.append(t) or real(t)
        sigs = STRATEGY.scan(ctx(c))
        self.assertIn(btc.et, [s.event for s in sigs])
        self.assertTrue(any(t.startswith("KXBTC") for t in read))
        self.assertLessEqual(len({t for t in read if t.startswith("KXDOGE")}), 3 * range_link_arb.PER_PAIR * 2)

    def test_too_good_by_listed_price_alone_keeps_the_pair_on_paper(self):
        # A $2 set at $1.59 listed, never quoted (its bracket is held), still keeps the pair on paper.
        p = Pair()
        p.prices.update(tail_gap(p))
        p.prices[p.bracket(5)] = (.68, .70)
        evs = p.events()
        self.pin(evs)
        sigs = STRATEGY.scan(ctx(fake_for(evs), held={p.bracket(5)}))
        self.assertEqual([s.meta["set"] for s in sigs], ["tail"])
        self.assertTrue(sigs[0].meta["paper_only"])

    def test_too_good_in_the_order_book_alone_keeps_the_pair_on_paper(self):
        p = gap_pair()                     # sell3 listed at $1.87
        evs = p.events()
        self.pin(evs)
        c = fake_for(evs)
        c.books[p.bracket(5)] = {"yes": [(.72, 40)], "no": [(.58, 40)]}   # NO at 28c in the book: $1.55
        sigs = STRATEGY.scan(ctx(c))
        self.assertTrue(sigs and all(s.meta["paper_only"] for s in sigs))

    def test_the_scan_plays_out_every_set_even_one_links_got_wrong(self):
        p = Pair()
        p.prices.update({p.bracket(4): (None, .01), p.rung(4): (.98, .99), p.rung(5): (None, .01)})   # 3c
        real = range_link_arb.links

        def wrong(pair):
            mk = {t: r["m"] for t, r in pair["records"].items()}
            buy3 = [(mk[p.bracket(4)], "yes"), (mk[p.rung(4)], "no"), (mk[p.rung(5)], "yes")]
            return [range_link_arb._set("superset", 1, buy3)] + real(pair)
        with mock.patch.object(range_link_arb, "links", wrong):
            sigs = STRATEGY.scan(ctx(fake_for(p.events())))
        self.assertFalse([s for s in sigs if any(leg.ticker == p.bracket(4) and leg.side == "yes" for leg in s.legs)])

    def test_recheck_keeps_a_too_good_price_off_real_money(self):
        p = gap_pair(gap=disjoint_gap)
        evs = p.events()
        self.pin(evs)
        c = fake_for(evs)
        sig = STRATEGY.scan(ctx(c))[0]
        self.assertEqual((sig.meta["set"], sig.meta["paper_only"]), ("disjoint", False))
        again = STRATEGY.recheck(ctx(c), sig)
        self.assertEqual(sorted((leg.ticker, leg.side) for leg in again.legs),
                         sorted((leg.ticker, leg.side) for leg in sig.legs))
        self.assertEqual(again.payout_min, again.legs[0].count * 1)
        c.books[p.bracket(2)] = {"yes": [(.40, 40)], "no": [(.58, 40)]}   # NO bracket now 60c: 70c < 80c
        self.assertIsNone(STRATEGY.recheck(ctx(c), sig))

    def test_recheck_refuses_near_the_close_or_a_set_it_didnt_make(self):
        c = fake_for(gap_pair().events())
        sig = STRATEGY.scan(ctx(c))[0]
        late = copy.deepcopy(sig)
        late.meta["first_close"] = time.time() + 60
        self.assertIsNone(STRATEGY.recheck(ctx(c), late))
        odd = copy.deepcopy(sig)
        odd.meta["pays"] = 3
        self.assertIsNone(STRATEGY.recheck(ctx(c), odd))
        self.assertIsNone(STRATEGY.recheck(ctx(c, held={sig.legs[0].ticker}), sig))

    def test_one_odd_event_cant_hide_the_rest(self):
        bad_year = Pair("KXETH", hour=17).events()
        for m in bad_year[0]["markets"]:
            m["rules_primary"] = re.sub(r", \d{4}, then", ", 0000, then", m["rules_primary"])
        late = Pair("KXSOLE", hour=17).events()
        for m in late[1]["markets"]:
            m["close_time"] = "0001-01-01T00:00:00+14:00"
        good = Pair(hour=17).events()
        odd = [{"event_ticker": 7, "markets": []}, {"event_ticker": "KXXRP-26OCT2914", "markets": None}, None, "junk"]
        evs = bad_year + late + odd + good
        why = []
        self.assertEqual([p["series"] for p in range_link_arb.pairs(evs, why)], ["KXBTC"])
        self.assertEqual(sorted(et for et, _ in why), sorted([bad_year[0]["event_ticker"], late[0]["event_ticker"],
                                                              "KXXRP-26OCT2914"]))

    def test_a_scan_reads_few_order_books(self):
        # Every bracket of three coins bid 12c over its odds: dozens of sets under their payout
        # by listed price. The scan reads the books of the best MAX_HOT sets only.
        cheap = []
        for series in ("KXBTC", "KXETH", "KXSOLE"):
            p = Pair(series)
            p.prices.update({p.bracket(i): (round(ODDS[i] / 100 + .12, 2), round(ODDS[i] / 100 + .14, 2))
                             for i in range(N)})
            cheap.append(p)
        hot = sum(1 for p in cheap for s in range_link_arb.links(range_link_arb.pairs(p.events())[0])
                  if s["listed"] is not None and s["listed"] < s["pays"])
        self.assertGreater(hot, range_link_arb.MAX_HOT)
        evs = [ev for p in cheap for ev in p.events()] + [ev for s in ("KXXRP", "KXDOGE") for ev in Pair(s).events()]
        c = fake_for(evs)
        read, real = [], c.orderbook
        c.orderbook = lambda t: read.append(t) or real(t)
        self.assertTrue(STRATEGY.scan(ctx(c)))
        self.assertLessEqual(len(read), 3 * range_link_arb.MAX_HOT)
        self.assertEqual(len(read), len(set(read)))   # no book read twice
        read.clear()
        with mock.patch.object(range_link_arb, "MAX_HOT", 2):
            STRATEGY.scan(ctx(c))
        self.assertTrue(0 < len(read) <= 6)
        read.clear()
        c2 = fake_for([ev for s in COINS for ev in Pair(s).events()])
        c2.orderbook = lambda t: read.append(t)
        self.assertEqual(STRATEGY.scan(ctx(c2)), [])
        self.assertEqual(read, [])   # nothing under its payout by listed price: no book read at all


class SizingTests(unittest.TestCase):
    def test_a_basket_that_pays_2_dollars_is_sized_for_it(self):
        quotes = [("A", "no", .60, 50), ("B", "yes", .62, 50), ("C", "no", .65, 50)]   # $1.87 for $2
        self.assertIsNone(ladder_arb.locked_basket(quotes, 30.0))
        b = ladder_arb.locked_basket(quotes, 30.0, pay=2)
        self.assertIsNotNone(b)
        self.assertAlmostEqual(b["profit"], 2 * b["count"] - b["cost"])
        self.assertLessEqual(b["cost"], 30.0)
        # Too small after fees at $2 is still None.
        self.assertIsNone(ladder_arb.locked_basket([("A", "no", .67, 50), ("B", "yes", .67, 50), ("C", "no", .65, 50)],
                                                   30.0, pay=2))

    def test_allocate_passes_each_set_its_payout(self):
        made = []

        def make(pay):
            def go(basket):
                from kal.strategies.base import Leg, Signal
                legs = [Leg(t, s, p, basket["count"]) for t, s, p, _ in basket["quotes"]]
                sig = Signal("x", "E", "t", legs, payout_min=basket["count"] * pay)
                sig.closes_at = time.time() + 3600
                made.append(sig)
                return sig
            return go
        two = [("A", "no", .60, 50), ("B", "yes", .62, 50), ("C", "no", .65, 50)]
        one = [("D", "yes", .40, 50), ("E", "no", .55, 50)]
        c = Context(None, dict(config.DEFAULTS, max_per_arb=30.0), "paper", 100.0, 100.0, set(), {}, {})
        sigs = ladder_arb.allocate(c, [(two, make(2), 2), (one, make(1))])
        self.assertEqual(sorted(len(s.legs) for s in sigs), [2, 3])
        three = next(s for s in sigs if len(s.legs) == 3)
        self.assertGreater(three.payout_min, three.cost)
        self.assertEqual(ladder_arb.allocate(c, [(two, make(1))]), [])   # the same legs at $1: no profit


class EngineTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        orders_proven()
        patcher = mock.patch.object(engine, "load_all", lambda: {"range_link_arb": STRATEGY})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_registered_and_starts_on_paper(self):
        self.assertIn("range_link_arb", MODULES)
        self.assertEqual((STRATEGY.name, STRATEGY.kind), ("range_link_arb", "arbitrage"))
        self.assertEqual(config.DEFAULTS["strategies"]["range_link_arb"], {"mode": "paper"})
        self.assertEqual(range_link_arb.VERIFIED, {})   # no event pair's rules read by a person yet
        self.assertFalse(STRATEGY.proven(None)[0])

    def test_paper_run_records_every_leg_and_buys_nothing(self):
        config.update({"strategies": {"range_link_arb": {"mode": "paper"}}})
        c = fake_for(gap_pair().events())
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual((s["paper_trades"], c.orders), (1, []))
        self.assertEqual(len(paper.open_rows()), 3)
        row = db.query("SELECT * FROM signals")[0]
        self.assertEqual((row["strategy"], row["status"], row["guaranteed"]), ("range_link_arb", "accepted", 1))

    def test_live_still_places_no_real_order(self):
        config.update({"strategies": {"range_link_arb": {"mode": "live"}}})
        c = fake_for(gap_pair().events())
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual((s["live_trades"], s["paper_trades"], c.orders), (0, 1, []))
        self.assertTrue(any("range_link_arb: paper instead of live" in n and "checked by hand" in n
                            for n in s["notes"]))

    def test_promotion_never_turns_it_live(self):
        from kal.research import promote
        before = config.load()["strategies"]["range_link_arb"]
        promote.apply(config.load())
        self.assertEqual(config.load()["strategies"]["range_link_arb"], before)


if __name__ == "__main__":
    unittest.main()
