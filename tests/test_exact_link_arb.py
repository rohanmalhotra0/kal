"""Tests for exact_link_arb.

The key check: for every link, play out every value the number could take (on the 0.1 grid,
past both ends of the listed values) and confirm one contract of each leg pays at least $1.
It reads each market's value from its ticker (pays below), not with the strategy's code, so
a bug in the strategy can't hide itself.
"""
import copy
import datetime
import re
import time
import unittest
from datetime import timezone
from unittest import mock

from kal import config, db, engine, paper
from kal.client import price
from kal.strategies import MODULES, exact_link_arb
from kal.strategies.base import Context
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven

STRATEGY = exact_link_arb.STRATEGY
SHORT = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
LONG = "January February March April May June July August September October November December".split()

# The wording on the real markets (2026-10-29), with only the number and the dates filled in.
SHUTDOWN = ("In the case of a delay in data caused by a federal government shutdown impacting the reliability of "
            "the Source Agency, the market’s latest Expiration Date will be extended to the sooner of the "
            "release of the Underlying or six months after the end of the government shutdown")
EXACT = {"KXECONSTATCPI": "CPI month-over-month", "KXECONSTATCPICORE": "CPI core month-over-month",
         "KXECONSTATCPIYOY": "CPI year-over-year", "KXECONSTATCORECPIYOY": "CPI core year-over-year",
         "KXECONSTATU3": "Unemployment rate"}
LADDER = {
    "KXECONSTATCPI": (
        "KXCPI", "If the Consumer Price Index (CPI) increases by more than {x}% (single-decimal) in {month} {year}, "
                 "then the market resolves to Yes.",
        "The market will always close at 8:29 AM ET on the scheduled day of the data release ({release}). Please "
        "note: the Expiration Value is the single-decimal value published at the Source Agency. " + SHUTDOWN + "."),
    "KXECONSTATCPICORE": (
        "KXCPICORE", "If the seasonally adjusted Consumer Price Index for All Urban Consumers: All Items less Food "
                     "and Energy for {month} {year}, as published by the Bureau of Labor Statistics, increases by "
                     "above {x}%, then the market resolves to Yes.",
        "Please note that the value of the Underlying is the single-decimal value reported by the BLS. "
        + SHUTDOWN + "."),
    "KXECONSTATCPIYOY": (
        "KXCPIYOY", "If the Consumer Price Index (CPI) increases by more than {x}% in the twelve months ending "
                    "{month} {year} (as represented by the one-decimal place value reported by the Bureau of Labor "
                    "Statistics), then the market resolves to Yes.", SHUTDOWN),
    "KXECONSTATCORECPIYOY": (
        "KXCPICOREYOY", "If the Consumer Price Index (CPI) for All Urban Consumers: All Items less Food and Energy "
                        "increases by more than {x}% in the twelve months ending {month} {year} (as represented by "
                        "the one-decimal place value reported by the Bureau of Labor Statistics), then the market "
                        "resolves to Yes.", SHUTDOWN + "."),
    "KXECONSTATU3": (
        "KXU3", "If the seasonally adjusted unemployment rate (U-3) reported by the Bureau of Labor Statistics in the "
                "Employment Situation Report is above {x}% in {month} {year}, then the market resolves to Yes.", ""),
}
# How likely each value is: the fair prices both series quote, 1c each side.
ODDS = {-1: .02, 0: .05, 1: .15, 2: .35, 3: .30, 4: .10, 5: .03}    # tenths -> chance
STRIKES = range(-2, 11)                                              # the ladder: above -0.2% .. above 1.0%


def x_text(t):
    return f"{t / 10:.1f}"


def release(days=20):
    """The release day `days` from now, and its close: 8:29 AM ET (13:29 UTC)."""
    day = (datetime.datetime.now(timezone.utc) + datetime.timedelta(days=days)).date()
    close = datetime.datetime(day.year, day.month, day.day, 13, 29, tzinfo=timezone.utc).timestamp()
    return day, close


def iso(t):
    return datetime.datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def data_month(day):
    """The month the number is for: the one before its release."""
    first = day.replace(day=1) - datetime.timedelta(days=1)
    return first.year, first.month


def quoted(fair):
    return max(.01, round(fair - .01, 2)), min(.99, round(fair + .01, 2))


def exact_event(series="KXECONSTATCPI", days=20, prices=None, values=tuple(ODDS)):
    day, close = release(days)
    year, month = data_month(day)
    et = f"{series}-{year % 100:02d}{SHORT[month - 1].upper()}"
    markets = []
    for t in values:
        bid, ask = (prices or {}).get(t) or quoted(ODDS.get(t, .01))
        x = x_text(t)
        markets.append({"ticker": f"{et}-T{x}", "event_ticker": et, "status": "active", "market_type": "binary",
                        "strike_type": "custom", "custom_strike": {"Value": x}, "yes_sub_title": f"Exactly {x}%",
                        "rules_primary": f"If the {EXACT[series]} is exactly {x}% in {SHORT[month - 1]} {year}, "
                                         "then the market resolves to Yes.",
                        "rules_secondary": "", "can_close_early": True, "close_time": iso(close),
                        "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}"})
    return {"event_ticker": et, "series_ticker": series, "title": f"{EXACT[series]} (exact value)",
            "mutually_exclusive": True, "markets": markets}


def ladder_event(series="KXECONSTATCPI", days=20, prices=None, close_offset=-240):
    ladder, first, second = LADDER[series]
    day, close = release(days)
    year, month = data_month(day)
    et = f"{ladder}-{year % 100:02d}{SHORT[month - 1].upper()}"
    markets = []
    for t in STRIKES:
        fair = sum(p for v, p in ODDS.items() if v > t)
        bid, ask = (prices or {}).get(t) or quoted(fair)
        x = x_text(t)
        markets.append({"ticker": f"{et}-T{x}", "event_ticker": et, "status": "active", "market_type": "binary",
                        "strike_type": "greater", "floor_strike": t / 10, "cap_strike": None,
                        "yes_sub_title": f"Above {x}%",
                        "rules_primary": first.format(x=x, month=LONG[month - 1], year=year),
                        "rules_secondary": second.format(release=f"{LONG[day.month - 1]} {day.day}, {day.year}"),
                        "can_close_early": True, "close_time": iso(close + close_offset),
                        "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}"})
    return {"event_ticker": et, "series_ticker": ladder, "title": f"{ladder} in {LONG[month - 1]}",
            "mutually_exclusive": False, "markets": markets}


# One set under $1: YES "exactly 0.2%" offered at 20c, though the ladder says 35c.
GAP = {2: (.18, .20)}


def fake_for(events, size=40):
    c = FakeKalshi()
    c.event_list = list(events)
    for ev in events:
        for m in ev["markets"]:
            bid, ask = price(m, "yes_bid"), price(m, "yes_ask")
            c.books[m["ticker"]] = {"yes": [(bid, size)] if bid else [], "no": [(round(1 - ask, 4), size)] if ask else []}
    return c


def ctx(client, budget=100.0, held=()):
    return Context(client, dict(config.DEFAULTS, max_per_arb=30.0), "paper", budget, budget, set(held), {}, {})


def pays(ticker, side, v):
    """What one contract pays if the number comes out v (tenths): read from the ticker alone."""
    x = round(float(ticker.rsplit("-T", 1)[1]) * 10)
    yes = v == x if ticker.startswith("KXECONSTAT") else v > x
    return 1.0 if yes == (side == "yes") else 0.0


class LinkTests(unittest.TestCase):
    def test_every_series_pair_is_found(self):
        events = [ev for s in EXACT for ev in (exact_event(s), ladder_event(s))]
        found = exact_link_arb.pairs(events)
        self.assertEqual(sorted(p["series"] for p in found), sorted(EXACT))
        for p in found:
            self.assertEqual(sorted(p["exact"]["values"]), sorted(ODDS))
            self.assertEqual(sorted(p["above"]["strikes"]), list(STRIKES))

    def test_every_link_pays_at_least_a_dollar_whatever_the_number(self):
        for s in EXACT:
            pair = exact_link_arb.pairs([exact_event(s), ladder_event(s)])[0]
            every = exact_link_arb.links(pair)
            self.assertEqual(len(every), 3 * len(ODDS))   # -0.1..0.5 all have both strikes
            for legs in every:
                for v in range(-5, 16):
                    paid = sum(pays(m["ticker"], side, v) for m, side in legs)
                    self.assertGreaterEqual(paid, 1.0, (s, [(m["ticker"], side) for m, side in legs], v))
                    if len(legs) == 3:
                        self.assertEqual(paid, 1.0)   # the set: exactly one pays

    def test_a_value_without_both_strikes_makes_only_the_links_it_can(self):
        # The ladder runs from "above -0.2%" to "above 1.0%": -0.2% has no "above -0.3%", 1.1% no "above 1.1%".
        ex = exact_event(values=(-2, 0, 11))
        pair = exact_link_arb.pairs([ex, ladder_event()])[0]
        got = [tuple(m["ticker"].rsplit("-T", 1)[1] + side for m, side in legs) for legs in exact_link_arb.links(pair)]
        self.assertEqual(got, [("-0.2no", "-0.2no"),                                       # NO above + NO exactly
                               ("0.0yes", "0.0yes", "-0.1no"), ("-0.1yes", "0.0no"), ("0.0no", "0.0no"),
                               ("1.0yes", "1.1no")])                                      # YES above 1.0 + NO 1.1


class FailsClosedTests(unittest.TestCase):
    """Anything that doesn't read exactly like the real markets: no pair."""

    def assert_no_pair(self, change, series="KXECONSTATCPI"):
        ex, lad = exact_event(series), ladder_event(series)
        change(ex, lad)
        self.assertEqual(exact_link_arb.pairs([ex, lad]), [])

    def test_wording(self):
        def say(field, old, new, which=0):
            def change(ex, lad):
                m = (ex, lad)[which]["markets"][3]
                m[field] = m[field].replace(old, new)
            return change
        for change in (say("rules_primary", "is exactly", "is at least"),
                       say("rules_primary", "month-over-month", "year-over-year"),
                       say("yes_sub_title", "Exactly", "About"),
                       say("rules_primary", "more than", "at least", 1),
                       say("rules_primary", "(single-decimal)", "(two decimals)", 1),
                       say("rules_secondary", "six months", "one year", 1),
                       say("rules_secondary", "single-decimal value", "value", 1),
                       say("yes_sub_title", "Above", "Over", 1)):
            self.assert_no_pair(change)

    def test_extra_rules_text(self):
        self.assert_no_pair(lambda ex, lad: ex["markets"][0].update(rules_secondary="Kalshi may settle it early."))
        self.assert_no_pair(lambda ex, lad: lad["markets"][0].update(
            rules_secondary=lad["markets"][0]["rules_secondary"] + " Or at a fair price."))
        self.assert_no_pair(lambda ex, lad: lad["markets"][0].update(rules_secondary=""), "KXECONSTATCPIYOY")
        self.assert_no_pair(lambda ex, lad: lad["markets"][0].update(rules_secondary=SHUTDOWN), "KXECONSTATU3")

    def test_numbers_that_disagree(self):
        self.assert_no_pair(lambda ex, lad: ex["markets"][2].update(custom_strike={"Value": "0.2"}))
        self.assert_no_pair(lambda ex, lad: ex["markets"][2].update(ticker=ex["markets"][2]["ticker"] + "5"))
        self.assert_no_pair(lambda ex, lad: lad["markets"][2].update(floor_strike=0.15))
        self.assert_no_pair(lambda ex, lad: lad["markets"][2].update(cap_strike=0.9))
        self.assert_no_pair(lambda ex, lad: lad["markets"][2].update(ticker=lad["markets"][3]["ticker"]))
        self.assert_no_pair(lambda ex, lad: ex["markets"].append(copy.deepcopy(ex["markets"][2])))   # a value twice

    def test_events_that_dont_line_up(self):
        def next_month(ex, lad):   # the "exactly" event is about the month after the ladder's
            for m in ex["markets"]:
                mon = m["rules_primary"].rsplit(" in ", 1)[1][:3]
                m["rules_primary"] = m["rules_primary"].replace(f" in {mon} ", f" in {SHORT[(SHORT.index(mon) + 1) % 12]} ")
        self.assert_no_pair(next_month)
        self.assert_no_pair(lambda ex, lad: [m.update(close_time=iso(time.time() + 21 * 86400)) for m in lad["markets"]])
        self.assert_no_pair(lambda ex, lad: lad["markets"][0].update(close_time=iso(time.time() + 86400)))
        self.assert_no_pair(lambda ex, lad: ex.update(mutually_exclusive=False))
        self.assert_no_pair(lambda ex, lad: lad.update(mutually_exclusive=True))
        self.assert_no_pair(lambda ex, lad: ex.update(series_ticker="KXOTHER"))

    def test_a_release_date_off_its_close_day(self):
        def change(ex, lad):
            for m in lad["markets"]:
                m["rules_secondary"] = re.sub(r"\([A-Z][a-z]+ \d+, \d{4}\)", "(January 1, 2030)", m["rules_secondary"])
        self.assert_no_pair(change)

    def test_payouts_that_arent_plain(self):
        self.assert_no_pair(lambda ex, lad: ex["markets"][0].update(market_type="scalar"))
        self.assert_no_pair(lambda ex, lad: lad["markets"][1].update(settlement_bounds_type="floor"))

    def test_two_events_with_one_ticker(self):
        ex, lad = exact_event(), ladder_event()
        self.assertEqual(exact_link_arb.pairs([ex, lad, copy.deepcopy(lad)]), [])
        self.assertEqual(exact_link_arb.pairs([ex, copy.deepcopy(ex), lad]), [])


class ScanTests(TempHome):
    def events(self, prices=GAP, days=20):
        return [exact_event(prices=prices, days=days), ladder_event(days=days)]

    def test_no_gap_no_signal(self):
        self.assertEqual(STRATEGY.scan(ctx(fake_for([exact_event(), ladder_event()]))), [])

    def test_a_cheap_set_is_bought_on_paper(self):
        sigs = STRATEGY.scan(ctx(fake_for(self.events())))
        self.assertEqual(len(sigs), 1)
        sig = sigs[0]
        self.assertEqual(sorted((leg.ticker.rsplit("-T", 1)[1], leg.side) for leg in sig.legs),
                         [("0.1", "no"), ("0.2", "yes"), ("0.2", "yes")])
        self.assertTrue(sig.guaranteed)
        self.assertTrue(sig.meta["paper_only"])
        self.assertEqual(sig.payout_min, sig.legs[0].count * 1.0)
        self.assertLess(sig.cost, sig.payout_min)
        ex, lad = self.events()
        self.assertEqual(sig.closes_at, max(m["close_time"] and exact_link_arb.close_ts(m)
                                            for m in ex["markets"] + lad["markets"]))
        self.assertIn(sig.meta["month"], sig.note)

    def test_too_close_to_the_release_is_skipped(self):
        self.assertEqual(STRATEGY.scan(ctx(fake_for(self.events(days=0.001)))), [])

    def test_a_held_leg_is_skipped(self):
        held = {exact_event()["markets"][3]["ticker"]}
        self.assertEqual(STRATEGY.scan(ctx(fake_for(self.events()), held=held)), [])

    def pin(self, evs, above=None, rules=None):
        pair = exact_link_arb.pairs(evs)[0]
        key = rules or exact_link_arb.rules_key(pair)
        patcher = mock.patch.dict(exact_link_arb.VERIFIED, {evs[0]["event_ticker"]: {
            "above": above or evs[1]["event_ticker"], "rules": key}})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_a_pinned_release_with_the_same_rules_may_trade_live(self):
        evs = self.events()
        self.pin(evs)
        self.assertFalse(STRATEGY.scan(ctx(fake_for(evs)))[0].meta["paper_only"])

    def test_a_pin_for_other_rules_or_another_ladder_stays_on_paper(self):
        evs = self.events()
        self.pin(evs, rules="what the rules said last month")
        self.assertTrue(STRATEGY.scan(ctx(fake_for(evs)))[0].meta["paper_only"])
        mock.patch.stopall()
        self.pin(evs, above="KXCPI-26JAN")
        self.assertTrue(STRATEGY.scan(ctx(fake_for(evs)))[0].meta["paper_only"])

    def test_too_good_to_be_true_stays_on_paper_even_if_pinned(self):
        evs = [exact_event(prices={2: (.01, .02)}), ladder_event()]
        self.pin(evs)
        sigs = STRATEGY.scan(ctx(fake_for(evs)))
        self.assertTrue(sigs and all(s.meta["paper_only"] for s in sigs))

    def test_recheck_rebuilds_from_fresh_books(self):
        c = fake_for(self.events())
        sig = STRATEGY.scan(ctx(c))[0]
        again = STRATEGY.recheck(ctx(c), sig)
        self.assertEqual([(leg.ticker, leg.side) for leg in again.legs], [(leg.ticker, leg.side) for leg in sig.legs])
        sig.meta["first_close"] = time.time() + 60
        self.assertIsNone(STRATEGY.recheck(ctx(c), sig))


class EngineTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        orders_proven()
        patcher = mock.patch.object(engine, "load_all", lambda: {"exact_link_arb": STRATEGY})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_registered_and_starts_on_paper(self):
        self.assertIn("exact_link_arb", MODULES)
        self.assertEqual((STRATEGY.name, STRATEGY.kind), ("exact_link_arb", "arbitrage"))
        self.assertEqual(config.DEFAULTS["strategies"]["exact_link_arb"], {"mode": "paper"})
        self.assertEqual(exact_link_arb.VERIFIED, {})   # no release's rules read by a person yet

    def test_paper_run_records_every_leg_and_buys_nothing(self):
        config.update({"strategies": {"exact_link_arb": {"mode": "paper"}}})
        c = fake_for([exact_event(prices=GAP), ladder_event()])
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual((s["paper_trades"], c.orders), (1, []))
        self.assertEqual(len(paper.open_rows()), 3)
        row = db.query("SELECT * FROM signals")[0]
        self.assertEqual((row["strategy"], row["status"], row["guaranteed"]), ("exact_link_arb", "accepted", 1))

    def test_live_still_places_no_real_order(self):
        config.update({"strategies": {"exact_link_arb": {"mode": "live"}}})
        c = fake_for([exact_event(prices=GAP), ladder_event()])
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual((s["live_trades"], s["paper_trades"], c.orders), (0, 1, []))
        self.assertTrue(any("exact_link_arb: paper instead of live" in n and "checked by hand" in n
                            for n in s["notes"]))

    def test_promotion_never_turns_it_live(self):
        from kal.research import promote
        before = config.load()["strategies"]["exact_link_arb"]
        promote.apply(config.load())
        self.assertEqual(config.load()["strategies"]["exact_link_arb"], before)


if __name__ == "__main__":
    unittest.main()
