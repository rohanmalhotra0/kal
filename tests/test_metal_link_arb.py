"""Tests for metal_link_arb.

The key check: for every pair of legs, play out every price that matters (each strike, a cent
and half a cent either side, far beyond both ends), with both events settling on that one
price, and with every strike of both No, and confirm one contract of each leg pays at least $1.
It reads each market's meaning from what the fixture built (its own numbers), never from the
strategy's code, so a bug in the strategy can't hide itself.

The fixtures copy the real markets (2026-10-29): wording, tickers, strike formats (gold: daily
4183, weekly and monthly 4021.99; silver: daily 57.75 and 77.50, weekly and monthly 54.99),
custom_strike (the monthly has no round_digits), and each event's settlement_sources.
"""
import copy
import datetime
import json
import math
import re
import time
import unittest
from datetime import timezone
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from kal import config, db, engine, paper, risk
from kal.client import ts
from kal.strategies import MODULES, metal_link_arb
from kal.strategies.base import Context
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven

STRATEGY = metal_link_arb.STRATEGY
D = Decimal
ET = ZoneInfo("America/New_York")
LONG = "January February March April May June July August September October November December".split()
TICK = D("0.01")
SECOND = ("The settlement value is rounded to the nearest 2 decimal places. When confirming the settlement value, note "
          "the close price for the 1-minute candlestick at a given time is the price at the end of the immediately "
          "preceding one-minute interval. For example, the candlestick timestamped 4:59 PM reflects trading during "
          "4:59:00 PM to 4:59:59 PM and closes at 5:00:00 PM. If no data is published by the specified source agency "
          "for the specified time, then the most recently available published data will be used to resolve the "
          "market.")
GOLD_URL = ("https://app.pyth.com/explore/Metal.Index.1OZGOLD%2FUSD?returnUrl=%2Fexplore%2Fall%3Fsearch%3D3712%26"
            "state%3Dstable")
SILVER_URL = "https://app.pyth.com/explore/Metal.Index.SILVER%2FUSD?returnUrl=%2Fexplore%3Fstate%3Dstable"

# Metal -> its series, its one source, the strikes of each event near the money (one strike and
# the step, as the real grids: gold daily on the dollar every $10, weekly and monthly on .99
# every $20 and $40; silver daily every 25c, weekly and monthly on .99 every $1), and a fair
# price curve: what "above k" is worth in cents.
METALS = {
    "gold": dict(series={"monthly": "KXGOLDMON", "daily": "KXGOLDD", "weekly": "KXGOLDW"}, name="Pyth - Gold",
                 url=GOLD_URL, word="Gold", center=D("4093"), scale=D("25"),
                 grid={"monthly": (D("4092.99"), D("40")), "weekly": (D("4092.99"), D("20")),
                       "daily": (D("4093"), D("10"))}),
    "silver": dict(series={"monthly": "KXSILVERMON", "daily": "KXSILVERD", "weekly": "KXSILVERW"},
                   name="Pyth - Silver", url=SILVER_URL, word="Silver", center=D("55"), scale=D("0.75"),
                   grid={"monthly": (D("54.99"), D("1")), "weekly": (D("54.99"), D("1")),
                         "daily": (D("55"), D("0.25"))}),
}
N = 10   # strikes each side of the money in each event (the real ones list 40 to 50)


def close_at(days=1.0):
    """5 PM ET on the day about `days` from now (EDT or EST, as that day has it)."""
    d = (datetime.datetime.now(ET) + datetime.timedelta(days=days)).date()
    return int(datetime.datetime(d.year, d.month, d.day, 17, tzinfo=ET).timestamp())


def iso(t):
    return datetime.datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ms(t):
    return datetime.datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def text(k):
    """A strike as Kalshi writes it: 4183, 4021.99, 58, 77.50."""
    return str(int(k)) if k == k.to_integral() else format(k, ".2f")


class Pair:
    """A monthly event and its daily or weekly twin, built like the real ones. spec[ticker] = the
    market's strike, in the fixture's own numbers: what it means, for the payout oracle."""

    def __init__(self, metal="gold", twin="daily", days=1.0, close=None, prices=None):
        self.info, self.metal, self.twin = METALS[metal], metal, twin
        self.close = close_at(days) if close is None else close
        local = datetime.datetime.fromtimestamp(self.close, ET)
        self.tz = local.tzname()   # EDT or EST
        suffix = f"{local.year % 100:02d}{LONG[local.month - 1][:3].upper()}{local.day:02d}17"
        self.mt, self.tt = f"{self.info['series']['monthly']}-{suffix}", f"{self.info['series'][twin]}-{suffix}"
        self.day = f"{LONG[local.month - 1]} {local.day:02d}, {local.year}"
        self.short = f"{LONG[local.month - 1][:3]} {local.day}, {local.year}"
        self.prices, self.spec, self.grids = dict(prices or {}), {}, {}
        for role, et in (("monthly", self.mt), (twin, self.tt)):
            first, step = self.info["grid"][role]
            self.grids[role] = [first + i * step for i in range(-N, N + 1)]
            for k in self.grids[role]:
                self.spec[self.ticker(et, k)] = k

    @staticmethod
    def ticker(et, k):
        return f"{et}-T{text(k)}"

    def monthly(self, i=0):
        """The monthly's market i steps from the money (4092.99 for gold, 54.99 for silver)."""
        return self.ticker(self.mt, self.grids["monthly"][N + i])

    def other(self, i=0):
        """The twin's market i steps from the money (daily 4093 or 55, weekly 4092.99 or 54.99)."""
        return self.ticker(self.tt, self.grids[self.twin][N + i])

    def fair(self, k):
        x = float((k - self.info["center"]) / self.info["scale"])
        return max(0, min(100, round(100 / (1 + math.exp(x)))))

    def quote(self, ticker, k):
        """(bid, ask) in dollars: an override, or the fair price 1c either side (None: no quote)."""
        if ticker in self.prices:
            return self.prices[ticker]
        f = self.fair(k)
        return (f - 1) / 100 if f > 1 else None, (f + 1) / 100 if f < 99 else None

    def market(self, et, k):
        t, kt, word = self.ticker(et, k), text(k), self.metal
        cs = {"front_month_contract": "N/A", "strike_date": iso_ms(self.close)}
        if et != self.mt:
            cs = {"front_month_contract": "N/A", "round_digits": "2", "strike_date": iso_ms(self.close)}
        m = {"ticker": t, "event_ticker": et, "status": "active", "market_type": "binary",
             "settlement_bounds_type": "default", "can_close_early": True, "strike_type": "greater",
             "floor_strike": int(k) if k == k.to_integral() else float(k), "custom_strike": cs,
             "rules_primary": f"If the close price of the 1-minute candlestick for {word} on {self.day} at 5:00 PM "
                              f"{self.tz} is above {kt} USD/t.oz, then the market resolves to Yes.",
             "rules_secondary": SECOND, "close_time": iso(self.close), "expected_expiration_time": iso(self.close),
             "occurrence_datetime": iso(self.close), "yes_sub_title": f"Above ${kt}",
             "title": f"Will the {word} close price be above {kt} USD/t.oz on {self.day} at 5:00 PM {self.tz}?"}
        bid, ask = self.quote(t, k)
        m["yes_bid_dollars"] = f"{bid:.4f}" if bid else "0.0000"
        m["yes_ask_dollars"] = f"{ask:.4f}" if ask else "0.0000"
        return m

    def event(self, role):
        et = self.mt if role == "monthly" else self.tt
        when = f"{self.day if role == 'monthly' else self.short} at 5:00 PM {self.tz}"
        return {"event_ticker": et, "series_ticker": et.split("-")[0], "title": f"{self.info['word']} price on {when}?",
                "sub_title": when, "mutually_exclusive": False, "strike_date": iso(self.close), "strike_period": "",
                "product_metadata": {"cadence": role},
                "settlement_sources": [{"name": self.info["name"], "url": self.info["url"]}],
                "markets": [self.market(et, k) for k in self.grids[role]]}

    def events(self):
        return [self.event("monthly"), self.event(self.twin)]


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


def pays(spec, ticker, side, v):
    """What one contract pays if the price is v, read from the fixture's own numbers."""
    return 1 if (v > spec[ticker]) == (side == "yes") else 0


def prices_to_try(p):
    """Every strike, a cent and half a cent either side, and far beyond both ends."""
    xs = set(p.spec.values())
    out = {x + d * TICK for x in xs for d in (-1, D("-0.5"), 0, D("0.5"), 1)}
    return sorted(out | {D(0), TICK / 2, min(xs) / 3, max(xs) * 3})


def worst(p, legs):
    """The least one contract of each leg can pay: over every price (both events on that one
    number, which is also what "no data" gives: the most recent published price) and if every
    strike resolves No."""
    out = sum(side == "no" for _, side in legs)
    for v in prices_to_try(p):
        out = min(out, sum(pays(p.spec, t, side, v) for t, side in legs))
    return out


def tickers(legs):
    return [(m["ticker"], side) for m, side in legs]


def gap_pair(metal="gold", twin="daily", at=0, **kw):
    """Someone bids 9c over the fair price for YES on the twin's strike `at` steps from the money,
    so NO there plus YES on the monthly's strike at or just under it costs about 92c (the shape
    of the real Aug 31 gap: NO daily "above 4452" at 41c + YES monthly "above 4451.99" at 50c)."""
    p = Pair(metal, twin, **kw)
    t = p.other(at)
    f = p.fair(p.spec[t])
    p.prices[t] = ((f + 9) / 100, (f + 11) / 100)
    return p


# Real markets and events from the API (2026-10-29), the fields the strategy reads. The fixtures
# must build exactly these.
REAL_MARKETS = [
    ("gold", "weekly", "2026-10-30T21:00:00Z", "monthly", "4152.99", {
        "ticker": "KXGOLDMON-26OCT3017-T4152.99", "event_ticker": "KXGOLDMON-26OCT3017", "strike_type": "greater",
        "floor_strike": 4152.99, "custom_strike": {"front_month_contract": "N/A", "strike_date": "2026-10-30T21:00:00.000Z"},
        "close_time": "2026-10-30T21:00:00Z", "expected_expiration_time": "2026-10-30T21:00:00Z",
        "market_type": "binary", "settlement_bounds_type": "default", "yes_sub_title": "Above $4152.99",
        "rules_primary": "If the close price of the 1-minute candlestick for gold on October 30, 2026 at 5:00 PM EDT is "
                         "above 4152.99 USD/t.oz, then the market resolves to Yes.", "rules_secondary": SECOND,
        "title": "Will the gold close price be above 4152.99 USD/t.oz on October 30, 2026 at 5:00 PM EDT?"}),
    ("gold", "weekly", "2026-10-30T21:00:00Z", "weekly", "4021.99", {
        "ticker": "KXGOLDW-26OCT3017-T4021.99", "event_ticker": "KXGOLDW-26OCT3017", "floor_strike": 4021.99,
        "custom_strike": {"front_month_contract": "N/A", "round_digits": "2", "strike_date": "2026-10-30T21:00:00.000Z"},
        "yes_sub_title": "Above $4021.99",
        "rules_primary": "If the close price of the 1-minute candlestick for gold on October 30, 2026 at 5:00 PM EDT is "
                         "above 4021.99 USD/t.oz, then the market resolves to Yes."}),
    ("gold", "daily", "2026-10-28T21:00:00Z", "daily", "4183", {
        "ticker": "KXGOLDD-26OCT2817-T4183", "event_ticker": "KXGOLDD-26OCT2817", "floor_strike": 4183,
        "custom_strike": {"front_month_contract": "N/A", "round_digits": "2", "strike_date": "2026-10-28T21:00:00.000Z"},
        "close_time": "2026-10-28T21:00:00Z", "yes_sub_title": "Above $4183",
        "rules_primary": "If the close price of the 1-minute candlestick for gold on October 28, 2026 at 5:00 PM EDT is "
                         "above 4183 USD/t.oz, then the market resolves to Yes."}),
    ("silver", "weekly", "2026-10-30T21:00:00Z", "monthly", "59.99", {
        "ticker": "KXSILVERMON-26OCT3017-T59.99", "event_ticker": "KXSILVERMON-26OCT3017", "floor_strike": 59.99,
        "custom_strike": {"front_month_contract": "N/A", "strike_date": "2026-10-30T21:00:00.000Z"},
        "yes_sub_title": "Above $59.99",
        "rules_primary": "If the close price of the 1-minute candlestick for silver on October 30, 2026 at 5:00 PM EDT "
                         "is above 59.99 USD/t.oz, then the market resolves to Yes."}),
    ("silver", "weekly", "2026-10-30T21:00:00Z", "weekly", "54.99", {
        "ticker": "KXSILVERW-26OCT3017-T54.99", "floor_strike": 54.99, "yes_sub_title": "Above $54.99"}),
    ("silver", "daily", "2026-10-28T21:00:00Z", "daily", "57.75", {
        "ticker": "KXSILVERD-26OCT2817-T57.75", "floor_strike": 57.75, "yes_sub_title": "Above $57.75",
        "rules_primary": "If the close price of the 1-minute candlestick for silver on October 28, 2026 at 5:00 PM EDT "
                         "is above 57.75 USD/t.oz, then the market resolves to Yes."}),
    ("silver", "daily", "2026-04-30T21:00:00Z", "daily", "69.50", {"ticker": "KXSILVERD-26APR3017-T69.50"}),
]
REAL_EVENTS = [
    ("gold", "weekly", "monthly", {
        "event_ticker": "KXGOLDMON-26OCT3017", "series_ticker": "KXGOLDMON", "mutually_exclusive": False,
        "strike_date": "2026-10-30T21:00:00Z", "title": "Gold price on October 30, 2026 at 5:00 PM EDT?",
        "sub_title": "October 30, 2026 at 5:00 PM EDT", "settlement_sources": [{"name": "Pyth - Gold", "url": GOLD_URL}]}),
    ("gold", "weekly", "weekly", {
        "event_ticker": "KXGOLDW-26OCT3017", "series_ticker": "KXGOLDW", "title": "Gold price on Oct 30, 2026 at 5:00 PM EDT?",
        "sub_title": "Oct 30, 2026 at 5:00 PM EDT", "settlement_sources": [{"name": "Pyth - Gold", "url": GOLD_URL}]}),
    ("silver", "weekly", "monthly", {
        "event_ticker": "KXSILVERMON-26OCT3017", "title": "Silver price on October 30, 2026 at 5:00 PM EDT?",
        "settlement_sources": [{"name": "Pyth - Silver", "url": SILVER_URL}]}),
    ("silver", "weekly", "weekly", {
        "event_ticker": "KXSILVERW-26OCT3017", "settlement_sources": [{"name": "Pyth - Silver", "url": SILVER_URL}]}),
]


class RealShapeTests(unittest.TestCase):
    def test_the_fixtures_build_the_real_markets_and_events(self):
        for metal, twin, close, role, k, real in REAL_MARKETS:
            p = Pair(metal, twin, close=ts(close))
            built = p.market(p.mt if role == "monthly" else p.tt, D(k))
            for field, want in real.items():
                self.assertEqual(built.get(field), want, (real["ticker"], field))
        for metal, twin, role, real in REAL_EVENTS:
            built = Pair(metal, twin, close=ts("2026-10-30T21:00:00Z")).event(role)
            for field, want in real.items():
                self.assertEqual(built.get(field), want, (real["event_ticker"], field))

    def test_the_real_october_30_pairs_read_cleanly(self):
        for metal in METALS:
            p = Pair(metal, "weekly", close=ts("2026-10-30T21:00:00Z"))
            why = []
            got = metal_link_arb.pairs(p.events(), why)
            self.assertEqual((len(got), why), (1, []), metal)
            self.assertEqual(got[0]["monthly"]["say"], "5:00 PM EDT on October 30, 2026")


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
        # The money is back 8 days after the close at the latest, so a pair closing over 22
        # days out would tie it up past max_arb_days (30): never offered.
        for days, trades in ((2, True), (20, True), (23.5, False), (40, False)):
            c = fake_for(gap_pair(days=days).events())
            got = self.accepted(c, cut)
            self.assertEqual(got, self.accepted(c, float("inf")), days)
            self.assertEqual(bool(got), trades, days)


class PairingTests(unittest.TestCase):
    def test_each_metal_pairs_with_its_daily_and_its_weekly(self):
        for metal in METALS:
            for twin in ("daily", "weekly"):
                p = Pair(metal, twin)
                why = []
                found = metal_link_arb.pairs(p.events(), why)
                self.assertEqual(why, [], (metal, twin))
                self.assertEqual(len(found), 1)
                got = found[0]
                self.assertEqual((got["monthly"]["event"], got["other"]["event"], got["close"]), (p.mt, p.tt, p.close))
                self.assertEqual(len(got["records"]), 2 * (2 * N + 1))
                self.assertEqual(got["monthly"]["source"], (METALS[metal]["name"], METALS[metal]["url"].split("?")[0]))

    def test_the_real_numbers_are_read_exactly(self):
        # No float rounding: a strike of 4092.99 is 4092.99, a cent under the daily's 4093.
        for metal in METALS:
            p = Pair(metal, "daily")
            got = metal_link_arb.pairs(p.events())[0]
            self.assertEqual(sorted(r["k"] for r in got["monthly"]["markets"]), p.grids["monthly"])
            self.assertEqual(sorted(r["k"] for r in got["other"]["markets"]), p.grids["daily"])
            self.assertEqual(got["records"][p.monthly()]["k"] + TICK, got["records"][p.other()]["k"])

    def test_winter_time_reads_as_est(self):
        # From November the rules say 5:00 PM EST: the close is then 22:00 UTC.
        close = int(datetime.datetime(2026, 12, 31, 17, tzinfo=ET).timestamp())
        p = Pair("gold", "daily", close=close)
        self.assertEqual(p.tz, "EST")
        got = metal_link_arb.pairs(p.events())
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["close"], got[0]["monthly"]["say"]), (close, "5:00 PM EST on December 31, 2026"))
        self.assertEqual(iso(close), "2026-12-31T22:00:00Z")

    def test_a_monthly_with_both_twins_makes_two_pairs(self):
        p = Pair("gold", "daily")
        w = Pair("gold", "weekly", close=p.close)
        got = metal_link_arb.pairs(p.events() + [w.event("weekly")])
        self.assertEqual(sorted(g["other"]["event"] for g in got), sorted([p.tt, w.tt]))

    def test_many_pairs_at_once(self):
        events = [ev for m in METALS for twin, days in (("daily", 2), ("weekly", 4)) for ev in Pair(m, twin, days).events()]
        self.assertEqual(len(metal_link_arb.pairs(events)), 4)

    def test_a_monthly_without_a_twin_yet(self):
        p = Pair()
        why = []
        self.assertEqual(metal_link_arb.pairs(p.events()[:1], why), [])
        self.assertEqual(why, [(p.mt, f"{p.mt}: no daily or weekly event at that minute (yet)")])
        self.assertEqual(metal_link_arb.pairs(p.events()[1:]), [])   # a twin alone is no pair

    def test_only_the_address_of_the_source_counts_not_its_query(self):
        p = Pair("silver", "weekly")
        evs = p.events()
        evs[0]["settlement_sources"][0]["url"] = SILVER_URL.replace("state%3Dstable", "search%3Dsilver%26state%3Dstable")
        self.assertEqual(len(metal_link_arb.pairs(evs)), 1)


class LinkTests(unittest.TestCase):
    def test_every_set_pays_what_it_says_whatever_the_price(self):
        for metal in METALS:
            for twin in ("daily", "weekly"):
                for gap in (None, 0, 3, -2):
                    p = Pair(metal, twin) if gap is None else gap_pair(metal, twin, gap)
                    pair = metal_link_arb.pairs(p.events())[0]
                    every = metal_link_arb.links(pair)
                    self.assertGreater(len(every), 2 * N)
                    for s in every:
                        legs = tickers(s["legs"])
                        self.assertGreaterEqual(worst(p, legs), 1, (metal, twin, legs))
                        self.assertTrue(metal_link_arb.check(pair, s["legs"], 1))
                        (yt, ys), (nt, ns) = legs
                        self.assertEqual((ys, ns), ("yes", "no"))
                        self.assertNotEqual(yt.split("-T")[0], nt.split("-T")[0])   # one leg in each series
                        self.assertLessEqual(p.spec[yt], p.spec[nt])                # YES at or under the NO's strike
                        self.assertEqual(s["set"], "same strike" if p.spec[yt] == p.spec[nt] else "nested")

    def test_the_check_refuses_unsafe_sets(self):
        p = Pair()
        pair = metal_link_arb.pairs(p.events())[0]
        mk = {t: r["m"] for t, r in pair["records"].items()}

        def legs(*ls):
            return [(mk[t], side) for t, side in ls]
        over = [(p.monthly(1), "yes"), (p.other(0), "no")]     # YES above 4132.99 + NO above 4093: $0 between
        self.assertEqual(worst(p, over), 0)                    # the oracle agrees
        self.assertFalse(metal_link_arb.check(pair, legs(*over), 1))
        good = [(p.monthly(0), "yes"), (p.other(0), "no")]     # YES above 4092.99 + NO above 4093
        self.assertEqual(worst(p, good), 1)
        self.assertTrue(metal_link_arb.check(pair, legs(*good), 1))
        self.assertFalse(metal_link_arb.check(pair, legs(*good), 2))   # claims more than it pays
        for bad in ([(p.monthly(0), "yes"), (p.other(4), "yes")],      # YES + YES: both lose under 4092.99
                    [(p.monthly(0), "no"), (p.other(-4), "no")],       # NO + NO: both lose over 4093
                    [(p.monthly(0), "yes"), (p.monthly(1), "no")],     # one series alone: not this link
                    [(p.monthly(0), "yes"), (p.other(0), "no"), (p.other(1), "no")],
                    [(p.other(0), "no"), (p.other(0), "no")], [(p.monthly(0), "yes")]):
            self.assertFalse(metal_link_arb.check(pair, legs(*bad), 1), bad)
        self.assertFalse(metal_link_arb.check(pair, [({"ticker": "KXGOLDD-X-T1"}, "yes"), (mk[p.other(0)], "no")], 1))
        self.assertFalse(metal_link_arb.check(pair, [], 1))

    def test_the_check_plays_prices_between_the_cents(self):
        # A YES strike half a cent over the NO's (the gate never lets one through) pays nothing at
        # exactly 4092.995. Only a play-out at the half cents sees it.
        p = Pair()
        pair = metal_link_arb.pairs(p.events())[0]
        moved = copy.deepcopy(pair)
        moved["records"][p.other(0)]["k"] = D("4092.995")
        mk = {t: r["m"] for t, r in moved["records"].items()}
        self.assertFalse(metal_link_arb.check(moved, [(mk[p.other(0)], "yes"), (mk[p.monthly(0)], "no")], 1))
        self.assertTrue(metal_link_arb.check(pair, [(mk[p.monthly(0)], "yes"), (mk[p.other(0)], "no")], 1))

    def test_the_cheapest_yes_is_chosen_by_listed_price(self):
        # The monthly's 4092.99 has no ask: NO on the daily's 4093 pairs with 4052.99 instead.
        p = Pair()
        p.prices[p.monthly(0)] = (.48, None)
        pair = metal_link_arb.pairs(p.events())[0]
        got = [tickers(s["legs"]) for s in metal_link_arb.links(pair) if s["legs"][1][0]["ticker"] == p.other(0)]
        self.assertEqual(got, [[(p.monthly(-1), "yes"), (p.other(0), "no")]])

    def test_no_data_is_one_shared_number(self):
        # With no data both series take "the most recently available published data": one more
        # number for both, never every strike No. The play-out tries every number that matters,
        # and every strike No too: each set pays $1 either way.
        p = Pair("silver", "weekly")
        pair = metal_link_arb.pairs(p.events())[0]
        for s in metal_link_arb.links(pair):
            legs = tickers(s["legs"])
            self.assertTrue(all(sum(pays(p.spec, t, side, v) for t, side in legs) >= 1 for v in prices_to_try(p)))
            self.assertGreaterEqual(sum(side == "no" for _, side in legs), 1)


class FailsClosedTests(unittest.TestCase):
    """Anything that doesn't read exactly like the real markets: no pair."""

    def assert_no_pair(self, change, metal="gold", twin="daily", why=None):
        p = Pair(metal, twin)
        evs = p.events()
        change(p, evs[0], evs[1])
        reasons = []
        self.assertEqual(metal_link_arb.pairs(evs, reasons), [], (metal, twin))
        self.assertTrue(reasons, (metal, twin))
        if why:
            self.assertIn(why, reasons[0][1])

    @staticmethod
    def m(ev, ticker):
        return next(m for m in ev["markets"] if m["ticker"] == ticker)

    def test_the_unchanged_fixtures_pair(self):
        for twin in ("daily", "weekly"):
            self.assertEqual(len(metal_link_arb.pairs(Pair("gold", twin).events())), 1)

    def test_wording(self):
        def say(old, new, which="monthly", field="rules_primary", i=3):
            def change(p, mon, oth):
                m = (mon if which == "monthly" else oth)["markets"][i]
                self.assertIn(old, m[field])
                m[field] = m[field].replace(old, new)
            return change
        for change in (say("close price", "open price"), say("1-minute", "5-minute", "twin"),
                       say("at 5:00 PM", "at 6:00 PM"), say("USD/t.oz", "USD/ounce", "twin"),
                       say("is above", "is at or above"), say("for gold", "for silver", "twin"),
                       say("for gold on", "for gold using the GCZ6 contract on"),
                       say("resolves to Yes.", "resolves to Yes or No."),
                       say("nearest 2 decimal places", "nearest 0 decimal places", field="rules_secondary"),
                       say("most recently available published data", "next published data", "twin", "rules_secondary"),
                       say("4:59:59 PM", "4:59:30 PM", field="rules_secondary")):
            self.assert_no_pair(change, why="worded")
        self.assert_no_pair(say("Above $", "Over $", "twin", "yes_sub_title"), why="subtitle")
        # How Kalshi worded the monthly in March: another market.
        old = ("If the open gold price for {day} at 5 PM EDT is above $4092.99, then the market resolves to Yes.")
        self.assert_no_pair(lambda p, mon, oth: self.m(mon, p.monthly()).update(
            rules_primary=old.format(day=p.day), rules_secondary="Reference prices can be found at "
            "https://www.tradingview.com/symbols/XAUUSD/?exchange=FX_IDC."), why="worded")
        self.assert_no_pair(lambda p, mon, oth: mon["markets"][0].update(rules_secondary="Note: Settlement value is "
                                                                         "rounded to the nearest 0 decimal places."))
        self.assert_no_pair(lambda p, mon, oth: oth["markets"][0].update(rules_secondary=""))
        self.assert_no_pair(lambda p, mon, oth: mon["markets"][0].update(rules_secondary=SECOND + " Or a fair price."))
        self.assert_no_pair(lambda p, mon, oth: oth["markets"][2].update(rules_primary=None))

    def test_sources(self):
        def source(**kw):
            def change(p, mon, oth):
                mon["settlement_sources"] = [dict(mon["settlement_sources"][0], **kw)]
            return change
        self.assert_no_pair(source(url="https://app.pyth.com/explore/Metal.Index.GOLD%2FUSD?returnUrl=%2Fexplore"),
                            why="source")
        self.assert_no_pair(source(url="https://app.pyth.com/explore/Metal.XAU%2FUSD"), why="source")
        self.assert_no_pair(source(url=GOLD_URL.replace("app.pyth.com", "pythdata.app")), why="source")
        self.assert_no_pair(source(name="Pyth"), why="source")
        self.assert_no_pair(source(url=None), why="source")
        self.assert_no_pair(lambda p, mon, oth: oth.update(settlement_sources=[]), why="source")
        self.assert_no_pair(lambda p, mon, oth: oth.pop("settlement_sources"), why="source")
        self.assert_no_pair(lambda p, mon, oth: mon.update(settlement_sources=mon["settlement_sources"] * 2),
                            why="source")
        self.assert_no_pair(lambda p, mon, oth: oth.update(settlement_sources=[
            {"name": "Trading Economics - Gold", "url": "https://tradingeconomics.com/commodity/gold"}]), why="source")
        # Both on silver's feed: the same, but not gold's.
        self.assert_no_pair(lambda p, mon, oth: [e.update(settlement_sources=[{"name": "Pyth - Silver",
                                                                               "url": SILVER_URL}]) for e in (mon, oth)])

    def test_times(self):
        later = lambda t, s: iso(ts(t) + s)   # noqa: E731
        self.assert_no_pair(lambda p, mon, oth: oth["markets"][5].update(
            close_time=later(oth["markets"][5]["close_time"], 1)), why="one time")
        self.assert_no_pair(lambda p, mon, oth: mon["markets"][0].update(
            expected_expiration_time=later(mon["markets"][0]["expected_expiration_time"], 60)), why="one time")

        def twin_a_second_later(p, mon, oth):   # every market of the twin, in step: only the pair check sees it
            for m in oth["markets"]:
                m["close_time"] = later(m["close_time"], 1)
                m["expected_expiration_time"] = later(m["expected_expiration_time"], 1)
                m["custom_strike"]["strike_date"] = iso_ms(p.close + 1)
            oth["strike_date"] = later(oth["strike_date"], 1)
        self.assert_no_pair(twin_a_second_later)

        def twin_expires_later(p, mon, oth):
            for m in oth["markets"]:
                m["expected_expiration_time"] = later(m["expected_expiration_time"], 3600)
        self.assert_no_pair(twin_expires_later, why="together")

        def both_expire_past_a_week(p, mon, oth):
            for m in mon["markets"] + oth["markets"]:
                m["expected_expiration_time"] = later(m["expected_expiration_time"], 7 * 86400 + 1)
        self.assert_no_pair(both_expire_past_a_week, why="week")

        def both_expire_before_the_close(p, mon, oth):
            for m in mon["markets"] + oth["markets"]:
                m["expected_expiration_time"] = later(m["expected_expiration_time"], -60)
        self.assert_no_pair(both_expire_before_the_close, why="week")
        self.assert_no_pair(lambda p, mon, oth: oth.update(strike_date=later(oth["strike_date"], 1)), why="strike date")
        self.assert_no_pair(lambda p, mon, oth: mon.pop("strike_date"), why="strike date")
        self.assert_no_pair(lambda p, mon, oth: mon["markets"][4]["custom_strike"].update(
            strike_date=iso_ms(p.close + 1)), why="strike day")
        self.assert_no_pair(lambda p, mon, oth: mon["markets"][4]["custom_strike"].update(strike_date="soon"))
        self.assert_no_pair(lambda p, mon, oth: oth["markets"][2].update(close_time="soon"))

        def rules_say_est(p, mon, oth):   # same ticker and close, but "EST" puts the price an hour later
            for m in mon["markets"] + oth["markets"]:
                m["rules_primary"] = m["rules_primary"].replace("PM EDT", "PM EST").replace("PM EST", "PM EDT") \
                    if p.tz == "EST" else m["rules_primary"].replace("PM EDT", "PM EST")
        self.assert_no_pair(rules_say_est, why="another time")

        def the_ticker_names_another_day(p, mon, oth):
            for ev in (mon, oth):
                et = re.sub(r"\d{2}17$", lambda g: f"{(int(g.group()[:2]) % 28) + 1:02d}17", ev["event_ticker"])
                for m in ev["markets"]:
                    m.update(event_ticker=et, ticker=et + m["ticker"][len(ev["event_ticker"]):])
                ev["event_ticker"] = et
        self.assert_no_pair(the_ticker_names_another_day, why="another day")

    def test_strikes_that_disagree(self):
        def strike(i, **kw):
            return lambda p, mon, oth: oth["markets"][i].update(**kw)
        self.assert_no_pair(lambda p, mon, oth: self.m(mon, p.monthly()).update(floor_strike=4092.98),
                            why="rules and strike")
        self.assert_no_pair(lambda p, mon, oth: self.m(oth, p.other()).update(ticker=p.other() + "1"), why="ticker")
        self.assert_no_pair(lambda p, mon, oth: self.m(oth, p.other()).update(ticker=p.other(1)), why="ticker")
        self.assert_no_pair(lambda p, mon, oth: self.m(oth, p.other()).update(yes_sub_title="Above $4092"),
                            why="subtitle")
        self.assert_no_pair(lambda p, mon, oth: self.m(mon, p.monthly()).update(
            rules_primary=self.m(mon, p.monthly())["rules_primary"].replace("4092.99", "4093")), why="rules and strike")
        self.assert_no_pair(strike(3, cap_strike=5000), why="one strike")
        for bad in (True, "NaN", 0, -4093, None, "4,093"):
            self.assert_no_pair(strike(3, floor_strike=bad))

        def off_the_cent(p, mon, oth):   # 4093.005: field, ticker, rules and subtitle agree, but not on the cent
            m = self.m(oth, p.other())
            m.update(floor_strike=4093.005, ticker=p.other().replace("T4093", "T4093.005"),
                     yes_sub_title="Above $4093.005",
                     rules_primary=m["rules_primary"].replace("above 4093 ", "above 4093.005 "))
        self.assert_no_pair(off_the_cent, why="cent")

        def twice(p, mon, oth):   # 4093.00 is 4093 under another ticker
            m = copy.deepcopy(self.m(oth, p.other()))
            m["ticker"] = p.other() + ".00"
            oth["markets"].append(m)
        self.assert_no_pair(twice, why="twice")

    def test_odd_strike_types(self):
        for kind in ("greater_or_equal", "less", "between", "custom", "functional", None):
            self.assert_no_pair(lambda p, mon, oth, k=kind: oth["markets"][2].update(strike_type=k), why="strike type")

    def test_custom_strikes(self):
        def custom(**kw):
            return lambda p, mon, oth: mon["markets"][3]["custom_strike"].update(**kw)
        self.assert_no_pair(custom(front_month_contract="GCZ6"), why="futures contract")
        self.assert_no_pair(custom(front_month_contract=""), why="futures contract")
        self.assert_no_pair(custom(front_month_contract=None), why="futures contract")
        self.assert_no_pair(custom(round_digits="0"), why="cent")
        self.assert_no_pair(custom(Value="4092.99"), why="custom_strike")
        self.assert_no_pair(lambda p, mon, oth: oth["markets"][3].update(custom_strike=["N/A"]), why="custom_strike")
        # No contract named at all, the way older markets were: still fine.
        for cs in (None, {}, {"round_digits": "2"}):
            p = Pair()
            evs = p.events()
            for m in evs[0]["markets"]:
                m["custom_strike"] = copy.deepcopy(cs)
            self.assertEqual(len(metal_link_arb.pairs(evs)), 1, cs)

    def test_payouts_that_arent_plain(self):
        self.assert_no_pair(lambda p, mon, oth: mon["markets"][0].update(market_type="scalar"))
        self.assert_no_pair(lambda p, mon, oth: oth["markets"][1].update(settlement_bounds_type="floor"))
        self.assert_no_pair(lambda p, mon, oth: mon["markets"][2].update(status="closed"), why="not open")
        self.assert_no_pair(lambda p, mon, oth: oth["markets"][2].update(status="determined"), why="not open")

    def test_mixed_events(self):
        self.assert_no_pair(lambda p, mon, oth: mon.update(mutually_exclusive=True))
        self.assert_no_pair(lambda p, mon, oth: oth.update(mutually_exclusive=None))
        self.assert_no_pair(lambda p, mon, oth: oth.update(series_ticker="KXGOLDW"))
        self.assert_no_pair(lambda p, mon, oth: oth.update(markets=[]))
        self.assert_no_pair(lambda p, mon, oth: mon.update(markets=None))
        self.assert_no_pair(lambda p, mon, oth: mon["markets"].append("junk"))
        self.assert_no_pair(lambda p, mon, oth: oth["markets"][0].update(event_ticker=p.mt))
        self.assert_no_pair(lambda p, mon, oth: mon["markets"].append(copy.deepcopy(oth["markets"][5])))
        silver = Pair("silver", "daily").events()[1]["markets"][4]
        self.assert_no_pair(lambda p, mon, oth: oth["markets"].__setitem__(4, copy.deepcopy(silver)))
        p = Pair()
        evs = p.events()
        self.assertEqual(metal_link_arb.pairs(evs + [copy.deepcopy(evs[1])]), [])   # two twins with one ticker
        self.assertEqual(metal_link_arb.pairs(evs + [copy.deepcopy(evs[0])]), [])   # two monthlies
        silver = Pair("silver", "daily", close=p.close).events()
        self.assertEqual(metal_link_arb.pairs([evs[0], silver[1]]), [])             # another metal's twin

    def test_one_odd_event_cant_hide_the_rest(self):
        bad_year = Pair("silver", "daily", days=3).events()
        for m in bad_year[0]["markets"]:
            m["rules_primary"] = re.sub(r", \d{4} at", ", 0000 at", m["rules_primary"])
        late = Pair("gold", "weekly", days=5).events()
        for m in late[1]["markets"]:
            m["close_time"] = "0001-01-01T00:00:00+14:00"
        good = Pair("gold", "daily").events()
        odd = [{"event_ticker": 7, "markets": []}, {"event_ticker": "KXGOLDMON-26DEC3117", "markets": None},
               {"event_ticker": "KXGOLDD-26DEC3117", "markets": None}, None, "junk"]
        evs = bad_year + late + odd + good
        why = []
        self.assertEqual([p["other"]["event"] for p in metal_link_arb.pairs(evs, why)], [good[1]["event_ticker"]])
        self.assertEqual(sorted(et for et, _ in why), sorted([bad_year[0]["event_ticker"], late[0]["event_ticker"],
                                                              "KXGOLDMON-26DEC3117"]))


class ScanTests(TempHome):
    def test_no_gap_no_signal(self):
        for metal in METALS:
            for twin in ("daily", "weekly"):
                self.assertEqual(STRATEGY.scan(ctx(fake_for(Pair(metal, twin).events()))), [], (metal, twin))

    def test_a_gap_is_bought_on_paper(self):
        for metal in METALS:
            for twin in ("daily", "weekly"):
                p = gap_pair(metal, twin)
                sigs = STRATEGY.scan(ctx(fake_for(p.events())))
                self.assertEqual(len(sigs), 1, (metal, twin))
                sig = sigs[0]
                self.assertEqual(sorted((leg.ticker, leg.side) for leg in sig.legs),
                                 sorted([(p.monthly(), "yes"), (p.other(), "no")]))
                self.assertTrue(sig.guaranteed and sig.meta["paper_only"])
                c = sig.legs[0].count
                self.assertTrue(all(leg.count == c for leg in sig.legs))
                self.assertEqual(sig.payout_min, c * 1.0)
                self.assertLess(sig.cost, sig.payout_min)
                self.assertLessEqual(sig.payout_min / sig.cost - 1, 0.25)
                self.assertEqual((sig.event, sig.meta["first_close"]), (p.mt, p.close))
                self.assertEqual((sig.meta["monthly_event"], sig.meta["other_event"]), (p.mt, p.tt))
                self.assertEqual(sig.meta["set"], "nested" if twin == "daily" else "same strike")
                self.assertEqual(sig.meta["source"], METALS[metal]["url"].split("?")[0])
                self.assertIn(f"5:00 PM {p.tz}", sig.note)
                self.assertGreaterEqual(worst(p, [(leg.ticker, leg.side) for leg in sig.legs]), 1)
                self.assertLessEqual(sig.partial_loss, config.DEFAULTS["max_per_trade"] + 1e-9)

    def test_the_money_counts_as_tied_up_until_both_settle(self):
        # Both legs close at 5 PM, but the monthly settles hours or days later: up to 8 days by
        # the terms. Limits and the yearly return see that date, not the close.
        p = gap_pair(days=3)
        sig = STRATEGY.scan(ctx(fake_for(p.events())))[0]
        self.assertEqual(sig.closes_at, p.close + metal_link_arb.SETTLE_DAYS * 86400)
        self.assertEqual(metal_link_arb.SETTLE_DAYS, 8)
        self.assertFalse(risk.too_late(sig.closes_at, config.DEFAULTS))
        self.assertIsNone(risk.Limits(dict(config.DEFAULTS), 100.0).refuse(sig))
        # A pair whose money would come back past max_arb_days is never offered.
        self.assertEqual(STRATEGY.scan(ctx(fake_for(gap_pair(days=23.5).events()))), [])

    def test_too_close_to_the_close_is_skipped(self):
        p = gap_pair(days=0)
        with mock.patch.object(metal_link_arb.time, "time", return_value=p.close - 200):
            self.assertEqual(STRATEGY.scan(ctx(fake_for(p.events()))), [])
        with mock.patch.object(metal_link_arb.time, "time", return_value=p.close - 400):
            self.assertTrue(STRATEGY.scan(ctx(fake_for(p.events()))))

    def test_a_held_leg_is_skipped(self):
        p = gap_pair()
        self.assertEqual(STRATEGY.scan(ctx(fake_for(p.events()), held={p.other()})), [])
        self.assertEqual(STRATEGY.scan(ctx(fake_for(p.events()), held={p.monthly()})), [])

    def test_a_paper_set_leaves_what_is_already_open_on_paper(self):
        # A live scan (some other pair pinned): an unpinned set that reuses a paper holding would
        # take its tickers from the next best set, then the engine would skip it.
        p = gap_pair()
        f = p.fair(p.spec[p.other(4)])
        p.prices[p.other(4)] = ((f + 9) / 100, (f + 11) / 100)   # a second gap: NO 4133 + YES 4132.99
        both = STRATEGY.scan(ctx(fake_for(p.events())))
        self.assertEqual(sorted(leg.ticker for s in both for leg in s.legs if leg.side == "no"),
                         sorted([p.other(0), p.other(4)]))
        held = frozenset({p.other(0)})
        sigs = STRATEGY.scan(ctx(fake_for(p.events()), mode="live", held_paper=held))
        self.assertEqual([leg.ticker for s in sigs for leg in s.legs if leg.side == "no"], [p.other(4)])

    def pin(self, evs, other=None, rules=None):
        pair = metal_link_arb.pairs(evs)[0]
        patcher = mock.patch.dict(metal_link_arb.VERIFIED, {evs[0]["event_ticker"]: {
            "pair": other or evs[1]["event_ticker"], "rules": rules or metal_link_arb.rules_key(pair)}})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_pinned_pair_may_trade_live(self):
        evs = gap_pair().events()
        self.pin(evs)
        sigs = STRATEGY.scan(ctx(fake_for(evs)))
        self.assertTrue(sigs and not sigs[0].meta["paper_only"])
        self.assertEqual(STRATEGY.proven(None), (True, "only event pairs a person checked by hand (VERIFIED)"))

    def test_the_pin_holds_both_sources_and_every_market_with_its_dates(self):
        p = Pair()
        evs = p.events()
        key = metal_link_arb.rules_key(metal_link_arb.pairs(evs)[0])
        for e in evs:
            self.assertIn(f"{e['event_ticker']} sources: {json.dumps(e['settlement_sources'], sort_keys=True)}", key)
            for m in e["markets"]:
                self.assertIn(f"{m['ticker']} {m['close_time']} {m['expected_expiration_time']}: "
                              f"{m['rules_primary']} | {m['rules_secondary']}", key)

    def test_a_pin_for_other_rules_or_another_twin_stays_on_paper(self):
        evs = gap_pair().events()
        self.pin(evs, rules="what the rules said yesterday")
        self.assertTrue(STRATEGY.scan(ctx(fake_for(evs)))[0].meta["paper_only"])
        mock.patch.stopall()
        self.pin(evs, other="KXGOLDW-26JAN0117")
        self.assertTrue(STRATEGY.scan(ctx(fake_for(evs)))[0].meta["paper_only"])

    def test_moved_dates_or_sources_go_back_to_paper(self):
        p = gap_pair()
        evs = p.events()
        self.pin(evs)
        moved = copy.deepcopy(evs)
        for m in moved[0]["markets"] + moved[1]["markets"]:
            m["expected_expiration_time"] = iso(p.close + 3600)   # still within the week: it pairs
        sigs = STRATEGY.scan(ctx(fake_for(moved)))
        self.assertTrue(sigs and all(s.meta["paper_only"] for s in sigs))
        moved = copy.deepcopy(evs)
        moved[1]["settlement_sources"][0]["url"] = GOLD_URL + "%26x%3D1"   # the same feed, listed anew
        sigs = STRATEGY.scan(ctx(fake_for(moved)))
        self.assertTrue(sigs and all(s.meta["paper_only"] for s in sigs))

    def test_too_good_to_be_true_keeps_a_pinned_pair_on_paper(self):
        # Any pair of the two events under 80c (listed or in its book) keeps all of them on paper:
        # here NO on the daily's 4113 at 10c + YES on the monthly's 4092.99 at 51c, 61c.
        p = gap_pair()
        p.prices[p.other(2)] = (.90, .92)
        evs = p.events()
        self.pin(evs)
        sigs = STRATEGY.scan(ctx(fake_for(evs)))
        self.assertTrue(sigs and all(s.meta["paper_only"] for s in sigs))

    def test_too_good_in_the_order_book_alone_keeps_the_pair_on_paper(self):
        p = gap_pair()                     # listed at 92c
        evs = p.events()
        self.pin(evs)
        c = fake_for(evs)
        c.books[p.other()] = {"yes": [(.80, 40)], "no": [(.18, 40)]}   # NO at 20c in the book: 71c
        sigs = STRATEGY.scan(ctx(c))
        self.assertTrue(sigs and all(s.meta["paper_only"] for s in sigs))

    def test_the_scan_plays_out_every_set_even_one_links_got_wrong(self):
        p = Pair()
        p.prices.update({p.monthly(1): (None, .02), p.other(0): (.97, .99)})   # YES 4132.99 + NO 4093: 5c
        real = metal_link_arb.links

        def wrong(pair):
            mk = {t: r["m"] for t, r in pair["records"].items()}
            legs = [(mk[p.monthly(1)], "yes"), (mk[p.other(0)], "no")]
            return [{"set": "nested", "pays": 1, "legs": legs, "listed": .05}] + real(pair)
        with mock.patch.object(metal_link_arb, "links", wrong):
            sigs = STRATEGY.scan(ctx(fake_for(p.events())))
        unsafe = sorted([(p.monthly(1), "yes"), (p.other(0), "no")])
        self.assertNotIn(unsafe, [sorted((leg.ticker, leg.side) for leg in s.legs) for s in sigs])
        # The cheap YES on 4132.99 still makes a real pair, with NO on the daily's 4133 over it.
        self.assertIn(sorted([(p.monthly(1), "yes"), (p.other(4), "no")]),
                      [sorted((leg.ticker, leg.side) for leg in s.legs) for s in sigs])

    def test_recheck_keeps_a_too_good_price_off_real_money(self):
        p = gap_pair()
        evs = p.events()
        self.pin(evs)
        c = fake_for(evs)
        sig = STRATEGY.scan(ctx(c))[0]
        self.assertFalse(sig.meta["paper_only"])
        again = STRATEGY.recheck(ctx(c), sig)
        self.assertEqual(sorted((leg.ticker, leg.side) for leg in again.legs),
                         sorted((leg.ticker, leg.side) for leg in sig.legs))
        self.assertEqual((again.payout_min, again.closes_at), (again.legs[0].count * 1.0, sig.closes_at))
        c.books[p.other()] = {"yes": [(.80, 40)], "no": [(.48, 40)]}   # NO now 20c: 71c < 80c
        self.assertIsNone(STRATEGY.recheck(ctx(c), sig))

    def test_recheck_refuses_near_the_close_or_a_set_it_didnt_make(self):
        c = fake_for(gap_pair().events())
        sig = STRATEGY.scan(ctx(c))[0]
        late = copy.deepcopy(sig)
        late.meta["first_close"] = time.time() + 60
        self.assertIsNone(STRATEGY.recheck(ctx(c), late))
        for odd in (2, None, "1"):
            other = copy.deepcopy(sig)
            other.meta["pays"] = odd
            self.assertIsNone(STRATEGY.recheck(ctx(c), other))
        self.assertIsNone(STRATEGY.recheck(ctx(c, held={sig.legs[0].ticker}), sig))

    def test_a_scan_reads_few_order_books(self):
        # Every market of both twins bid 12c over its odds, for both metals and twins: dozens of
        # pairs under $1 by listed price. The scan reads the books of the best MAX_HOT only.
        cheap = []
        for metal in METALS:
            for twin, days in (("daily", 2), ("weekly", 4)):
                p = Pair(metal, twin, days)
                for i in range(-N, N + 1):
                    k = p.spec[p.other(i)]
                    f = p.fair(k)
                    if 2 < f < 80:
                        p.prices[p.other(i)] = ((f + 12) / 100, (f + 14) / 100)
                cheap.append(p)
        hot = sum(1 for p in cheap for s in metal_link_arb.links(metal_link_arb.pairs(p.events())[0])
                  if s["listed"] is not None and s["listed"] < 1)
        self.assertGreater(hot, metal_link_arb.MAX_HOT)
        c = fake_for([ev for p in cheap for ev in p.events()])
        read, real = [], c.orderbook
        c.orderbook = lambda t: read.append(t) or real(t)
        self.assertTrue(STRATEGY.scan(ctx(c)))
        self.assertLessEqual(len(read), 2 * metal_link_arb.MAX_HOT)
        self.assertEqual(len(read), len(set(read)))   # no book read twice
        for p in cheap:   # no pair of events took more than its share
            self.assertLessEqual(len({t for t in read if t.startswith(p.tt)}), metal_link_arb.PER_PAIR)
        read.clear()
        with mock.patch.object(metal_link_arb, "MAX_HOT", 2):
            STRATEGY.scan(ctx(c))
        self.assertTrue(0 < len(read) <= 4)
        read.clear()
        c2 = fake_for([ev for m in METALS for ev in Pair(m).events()])
        c2.orderbook = lambda t: read.append(t)
        self.assertEqual(STRATEGY.scan(ctx(c2)), [])
        self.assertEqual(read, [])   # nothing under $1 by listed price: no book read at all


class EngineTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        orders_proven()
        patcher = mock.patch.object(engine, "load_all", lambda: {"metal_link_arb": STRATEGY})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_registered_and_starts_on_paper(self):
        self.assertIn("metal_link_arb", MODULES)
        self.assertEqual((STRATEGY.name, STRATEGY.kind), ("metal_link_arb", "arbitrage"))
        self.assertEqual(config.DEFAULTS["strategies"]["metal_link_arb"], {"mode": "paper"})
        self.assertEqual(metal_link_arb.VERIFIED, {})   # no event pair's rules read by a person yet
        self.assertFalse(STRATEGY.proven(None)[0])

    def test_paper_run_records_both_legs_and_buys_nothing(self):
        config.update({"strategies": {"metal_link_arb": {"mode": "paper"}}})
        p = gap_pair()
        c = fake_for(p.events())
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual((s["paper_trades"], c.orders), (1, []))
        rows = paper.open_rows()
        self.assertEqual(sorted((r["ticker"], r["side"]) for r in rows), sorted([(p.monthly(), "yes"), (p.other(), "no")]))
        self.assertEqual({r["event"] for r in rows}, {p.mt})
        row = db.query("SELECT * FROM signals")[0]
        self.assertEqual((row["strategy"], row["status"], row["guaranteed"]), ("metal_link_arb", "accepted", 1))

    def test_live_still_places_no_real_order(self):
        config.update({"strategies": {"metal_link_arb": {"mode": "live"}}})
        c = fake_for(gap_pair().events())
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual((s["live_trades"], s["paper_trades"], c.orders), (0, 1, []))
        self.assertTrue(any("metal_link_arb: paper instead of live" in n and "checked by hand" in n
                            for n in s["notes"]))

    def test_promotion_never_turns_it_live(self):
        from kal.research import promote
        before = config.load()["strategies"]["metal_link_arb"]
        promote.apply(config.load())
        self.assertEqual(config.load()["strategies"]["metal_link_arb"], before)


if __name__ == "__main__":
    unittest.main()
