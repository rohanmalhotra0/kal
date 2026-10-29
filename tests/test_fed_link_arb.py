"""Tests for fed_link_arb.

The key check: for every signal, play out every move the Fed could make (a 75bp cut to a
75bp hike), with every decision market that could be the YES one for it (a 50bp cut as "Cut
25bps" or as "Cut >25bps"), and confirm the legs pay at least payout_min, and more than they
cost. It reads each market's strike or code from its ticker (resolves_yes below), not with the
strategy's code, so a bug in the strategy can't hide itself.
"""
import datetime
import random
import time
import unittest
from datetime import timezone
from unittest import mock

from kal import config, db, engine, paper
from kal.client import fee, price, ts
from kal.strategies import MODULES, fed_link_arb
from kal.strategies.base import Context
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven

DAY = 86400
STRATEGY = fed_link_arb.STRATEGY
CODES = ("C26", "C25", "H0", "H25", "H26")
SHORT = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
LONG = "January February March April May June July August September October November December".split()

# The wording on the real markets (2026-10-29), with only the strike and date filled in.
RATE_RULES = ("If the upper bound of the target federal funds rate published on the Federal Reserve's official "
              "website is greater than {k}% following the Federal Reserve's {d} meeting, then the market "
              "resolves to Yes.")
RATE_EXPIRY = ("This market will expire the first 2:05 PM ET following the release of a Federal Reserve statement "
               "for their {d} meeting or one week following the last day of that meeting.")
DECISION_RULES = "If the Federal Reserve does a {what} on {d}, then the market resolves to Yes."
DECISION_TERMS = ("The outcomes are mutually exclusive: at most one can resolve to Yes. For example, a "
                  "50-basis-point hike resolves \u201cHike 25 bps\u201d to Yes and \u201cHike 25 bps\u201d to No. If "
                  "the scheduled FOMC meeting is canceled and does not occur on its scheduled date, \u201cFed "
                  "maintains rate\u201d resolves to Yes and all other outcomes resolve to No.")
LABELS = {"C26": "Cut >25bps", "C25": "Cut 25bps", "H0": "Fed maintains rate", "H25": "Hike 25bps",
          "H26": "Hike >25bps"}
# What each decision market's own rules say. On Kalshi the "Cut >25bps" market reads "Cut of 25bps"
# (its ">" is lost). The hold market's wording wasn't seen: "Hike of 0bps" is a guess.
WHAT = {"C26": "Cut of 25bps", "C25": "Cut of 25bps", "H0": "Hike of 0bps", "H25": "Hike of 25bps",
        "H26": "Hike of 25bps"}
U = 4.00
STRIKES = (3.25, 3.50, 3.75, 4.00, 4.25, 4.50, 4.75)
# A normal market with the rate at 4.00%: nothing under $1. (yes_bid, yes_ask)
RATES = {3.25: (.98, .99), 3.50: (.97, .99), 3.75: (.94, .96), 4.00: (.19, .21), 4.25: (.01, .03),
         4.50: (.01, .02), 4.75: (.01, .02)}
DECISIONS = {"C26": (.01, .02), "C25": (.03, .05), "H0": (.74, .76), "H25": (.17, .19), "H26": (.01, .03)}
# One pair under $1: NO "above 4.00%" at 56c + NO "hold" at 33c (like Aug 24, 2026, at 3.75%).
GAP = {"rates": {4.00: (.44, .46)}, "decisions": {"H0": (.67, .69), "H25": (.38, .40), "H26": (.03, .05)}}


def iso(t):
    return datetime.datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_in(days):
    return (datetime.datetime.now(timezone.utc) + datetime.timedelta(days=days)).date()


def suffix(day):
    return f"{day.year % 100:02d}{SHORT[day.month - 1].upper()}"


def close_of(day):
    """KXFED closes at 1:55 PM ET on the meeting day (18:55 UTC in winter)."""
    return datetime.datetime(day.year, day.month, day.day, 18, 55, tzinfo=timezone.utc).timestamp()


def short(day):
    return f"{SHORT[day.month - 1]} {day.day}, {day.year}"       # Dec 9, 2026


def long(day):
    return f"{LONG[day.month - 1]} {day.day:02d}, {day.year}"    # December 09, 2026


def rate_market(day, k, bid, ask):
    ev = f"KXFED-{suffix(day)}"
    return {"ticker": f"{ev}-T{k:.2f}", "event_ticker": ev, "status": "active", "market_type": "binary",
            "strike_type": "greater", "floor_strike": k, "cap_strike": None, "yes_sub_title": f"Above {k:.2f}%",
            "rules_primary": RATE_RULES.format(k=f"{k:.2f}", d=short(day)),
            "rules_secondary": RATE_EXPIRY.format(d=short(day)), "can_close_early": True,
            "close_time": iso(close_of(day)), "expected_expiration_time": iso(close_of(day) + 600),
            "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}"}


def decision_market(day, code, bid, ask):
    ev = f"KXFEDDECISION-{suffix(day)}"
    return {"ticker": f"{ev}-{code}", "event_ticker": ev, "status": "active", "market_type": "binary",
            "strike_type": "custom", "yes_sub_title": LABELS[code],
            "rules_primary": DECISION_RULES.format(what=WHAT[code], d=long(day)), "rules_secondary": DECISION_TERMS,
            "can_close_early": True, "close_time": iso(close_of(day) + 240),
            "expected_expiration_time": iso(close_of(day) + 600),
            "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}"}


def meeting(days=20, rates=None, decisions=None):
    """[rate event, decision event] of one meeting `days` from now, priced (yes_bid, yes_ask)."""
    day = day_in(days)
    r, d = dict(RATES), dict(DECISIONS)
    r.update(rates or {})
    d.update(decisions or {})
    month = f"{SHORT[day.month - 1]} {day.year}"
    return [{"event_ticker": f"KXFED-{suffix(day)}", "series_ticker": "KXFED", "mutually_exclusive": False,
             "title": f"Fed funds rate after {month} meeting?",
             "markets": [rate_market(day, k, *r[k]) for k in sorted(r)]},
            {"event_ticker": f"KXFEDDECISION-{suffix(day)}", "series_ticker": "KXFEDDECISION",
             "mutually_exclusive": True, "title": f"Fed decision in {month}?",
             "markets": [decision_market(day, c, *d[c]) for c in CODES]}]


def gap_meeting(days=20):
    return meeting(days, **GAP)


def settled_meeting(days_ago, after, strikes=STRIKES, prefix="KXFED", status="finalized"):
    """A settled KXFED event `days_ago` days back, whose rate came out at `after`."""
    day = day_in(-days_ago)
    ev = f"{prefix}-{suffix(day)}"
    return [{"ticker": f"{ev}-T{k:.2f}", "event_ticker": ev, "status": status, "result": "yes" if k < after else "no",
             "strike_type": "greater", "floor_strike": k, "cap_strike": None, "close_time": iso(close_of(day))}
            for k in strikes]


def history(last=U, days_ago=22, prefix="KXFED"):
    """The last two settled meetings: 3.75% six weeks before the last one, then `last`."""
    return settled_meeting(days_ago + 42, 3.75, prefix=prefix) + settled_meeting(days_ago, last, prefix=prefix)


def set_book(c, ticker, yes_bid, yes_ask, bid_size=40, ask_size=40):
    """Kalshi books hold bids only: a YES ask shows up as a NO bid at 1 - ask."""
    c.books[ticker] = {"yes": [(yes_bid, bid_size)] if yes_bid else [],
                       "no": [(round(1 - yes_ask, 4), ask_size)] if yes_ask else []}


def fake_for(events, settled=None, size=40):
    c = FakeKalshi()
    c.event_list = list(events)
    for ev in events:
        for m in ev["markets"]:
            set_book(c, m["ticker"], price(m, "yes_bid"), price(m, "yes_ask"), size, size)
    c.settled["KXFED"] = history() if settled is None else settled
    return c


def ctx(client, budget=100.0, max_per_arb=30.0, held=()):
    cfg = dict(config.DEFAULTS, max_per_arb=max_per_arb)
    return Context(client, cfg, "paper", budget, budget, set(held), {}, {})


def rate_ticker(events, k):
    return f"{events[0]['event_ticker']}-T{k:.2f}"


def code_ticker(events, code):
    return f"{events[1]['event_ticker']}-{code}"


def market(events, ticker):
    return next(m for ev in events for m in ev["markets"] if m["ticker"] == ticker)


# ---------- the independent outcome check ----------

def resolves_yes(ticker, rate_before, move, decided):
    """A market's result if the Fed moves the rate by `move` and `decided` is the decision market
    that says so. "KXFED-..-T4.00" is YES if the rate after is above 4.00%."""
    tail = ticker.rsplit("-", 1)[1]
    if ticker.startswith("KXFED-"):
        return rate_before + move > float(tail[1:]) + 1e-9
    return tail == decided


def outcomes():
    """Every move from a 75bp cut to a 75bp hike, with each decision market that could say it."""
    for move in (-0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75):
        for decided in (("C26", "C25") if move < 0 else ("H0",) if move == 0 else ("H25", "H26")):
            yield move, decided


def payout(sig, rate_before, move, decided):
    return sum(leg.count for leg in sig.legs
               if (leg.side == "yes") == resolves_yes(leg.ticker, rate_before, move, decided))


class ScanTests(unittest.TestCase):
    def scan(self, events, settled=None, client=None, **kw):
        return STRATEGY.scan(ctx(client or fake_for(events, settled), **kw))

    def assertLockedIn(self, sig, rate_before=U):
        self.assertTrue(sig.guaranteed)
        for move, decided in outcomes():
            got = payout(sig, rate_before, move, decided)
            self.assertGreaterEqual(got, sig.payout_min, f"a {move:+.2f} move ({decided}) pays less than promised")
            self.assertGreater(got - sig.cost, 0, f"a {move:+.2f} move ({decided}) loses money")

    def test_a_gap_gives_the_right_legs_counts_and_payout(self):
        evs = gap_meeting()
        sigs = self.scan(evs, client=fake_for(evs, size=10))
        self.assertEqual(len(sigs), 1)
        sig = sigs[0]
        legs = {(leg.ticker, leg.side): leg for leg in sig.legs}
        f, hold = rate_ticker(evs, 4.00), code_ticker(evs, "H0")
        self.assertEqual(set(legs), {(f, "no"), (hold, "no")})
        self.assertEqual((legs[(f, "no")].price, legs[(hold, "no")].price), (0.56, 0.33))
        self.assertEqual([leg.count for leg in sig.legs], [10, 10])
        self.assertEqual(sig.payout_min, 10.0)
        cost = sum(leg.count * leg.price + fee(leg.count, leg.price) for leg in sig.legs)
        self.assertAlmostEqual(sig.cost, cost)                      # Kalshi's taker fee on each order
        self.assertGreater(cost, 10 * (0.56 + 0.33))
        self.assertAlmostEqual(sig.exp_profit, sig.payout_min - sig.cost)
        self.assertEqual(sig.closes_at, close_of(day_in(20)) + 240)  # the later of the two closes
        self.assertEqual((sig.strategy, sig.event), ("fed_link_arb", evs[0]["event_ticker"]))
        self.assertEqual((sig.meta["rate_before"], sig.meta["strike"]), (4.0, 4.0))
        self.assertTrue(sig.meta["paper_only"])
        self.assertIsNone(sig.prefix_payouts)                       # a part-bought set pays $0
        self.assertIn("Fed meeting", sig.note)
        self.assertLockedIn(sig)

    def test_normal_prices_find_nothing(self):
        self.assertEqual(self.scan(meeting()), [])

    def test_fees_can_eat_a_thin_gap(self):
        # NO "above 4.00%" at 59c + NO hold at 40c: 99c before fees, over $1 after.
        evs = meeting(rates={4.00: (.41, .43)}, decisions={"H0": (.60, .62), "H25": (.36, .38), "H26": (.03, .05)})
        self.assertEqual(self.scan(evs), [])

    def test_a_four_leg_set(self):
        # YES "above 4.00%" + YES on every decision that isn't a hike: exactly one pays.
        evs = meeting(decisions={"H0": (.64, .66), "H25": (.21, .23), "H26": (.03, .05)})
        sigs = self.scan(evs)
        self.assertEqual(len(sigs), 1)
        sig = sigs[0]
        want = {(rate_ticker(evs, 4.00), "yes")} | {(code_ticker(evs, c), "yes") for c in ("C26", "C25", "H0")}
        self.assertEqual({(leg.ticker, leg.side) for leg in sig.legs}, want)
        self.assertEqual(len({leg.count for leg in sig.legs}), 1)
        self.assertEqual(sig.payout_min, sig.legs[0].count * 1.0)
        self.assertLockedIn(sig)

    def test_every_set_and_pair_found_pays(self):
        # A ladder priced as if the rate were 4.25%: many links look cheap at the real 4.00%.
        evs = meeting(rates={4.00: (.94, .96), 3.75: (.97, .99)}, decisions={"H0": (.20, .22), "H25": (.70, .72)})
        sigs = self.scan(evs)
        self.assertGreater(len(sigs), 0)
        used = [leg.ticker for sig in sigs for leg in sig.legs]
        self.assertEqual(len(used), len(set(used)))                 # no market in two signals
        for sig in sigs:
            self.assertLockedIn(sig)

    def test_rate_before_comes_from_the_last_settled_meeting(self):
        # The last meeting left the rate at 4.25%, not 4.00%. Then NO "above 4.00%" + NO hold is
        # no link at all (a hold keeps it above 4.00%), and every signal is built on 4.25%.
        evs = gap_meeting()
        sigs = self.scan(evs, settled=history(last=4.25))
        self.assertTrue(sigs)
        pair = {(rate_ticker(evs, 4.00), "no"), (code_ticker(evs, "H0"), "no")}
        for sig in sigs:
            self.assertNotEqual({(leg.ticker, leg.side) for leg in sig.legs}, pair)
            self.assertEqual(sig.meta["rate_before"], 4.25)
            self.assertLockedIn(sig, rate_before=4.25)

    def test_old_FED_tickers_count_too(self):
        # Settled events before 2025 are named FED-25DEC (no KX); they come back from KXFED.
        self.assertEqual(len(self.scan(gap_meeting(), settled=history(prefix="FED"))), 1)

    def test_the_last_meeting_must_have_settled_cleanly(self):
        # history(): the older meeting's markets, then the last one's (3.25% ... 4.75%, so its
        # 3.75% market is rows[-5] and its 4.00% market rows[-4]).
        cases = {
            "not final yet": lambda rows: rows[-4].update(status="determined"),
            "no result": lambda rows: rows[-4].update(result=""),
            "a hole at the rate": lambda rows: rows.pop(-4),
            "a range market": lambda rows: rows[-7].update(strike_type="between", cap_strike=3.5),
            "same strike twice": lambda rows: rows[-6].update(floor_strike=3.25),
            "off the grid": lambda rows: rows[-6].update(floor_strike=3.6),
            "out of order": lambda rows: (rows[-5].update(result="no"), rows[-4].update(result="yes")),
            "an older meeting it can't place in time": lambda rows: [m.pop("close_time") for m in rows[:-7]],
        }
        for name, change in cases.items():
            rows = history()
            change(rows)
            self.assertEqual(self.scan(gap_meeting(), settled=rows), [], name)
        self.assertEqual(self.scan(gap_meeting(), settled=history(last=5.00)), [])   # all YES: rate unknown
        self.assertEqual(self.scan(gap_meeting(), settled=[]), [])
        self.assertEqual(len(self.scan(gap_meeting(), settled=history())), 1)        # and the control trades

    def test_the_rate_before_must_be_recent(self):
        # The meeting is in 20 days. A last result 54 days ago is 74 days before it: fine.
        self.assertEqual(len(self.scan(gap_meeting(), settled=history(days_ago=54))), 1)
        self.assertEqual(self.scan(gap_meeting(), settled=history(days_ago=56)), [])   # 76 days

    def test_nothing_may_have_settled_after_the_last_meeting(self):
        later = settled_meeting(-60, 4.25)                             # "settled" after the next meeting
        self.assertEqual(self.scan(gap_meeting(), settled=history() + later), [])
        evs = gap_meeting()
        mine = settled_meeting(0, 4.0)
        for m in mine:                                                 # the next meeting itself, settled already?
            m.update(event_ticker=evs[0]["event_ticker"], close_time=iso(time.time() - 60))
        self.assertEqual(self.scan(evs, settled=history() + mine), [])

    def test_a_meeting_in_between_that_isnt_settled_means_no_trade(self):
        target = gap_meeting(50)
        self.assertEqual(len(self.scan(target)), 1)                    # on its own it trades (72 days)
        early = meeting(8)
        self.assertEqual(self.scan(target + early[:1]), [])            # an open rate event first
        self.assertEqual(self.scan(target + early[1:]), [])            # an open decision event first
        self.assertEqual(self.scan(target + early), [])                # then that's the next meeting (no gap)

    def test_only_the_next_meeting_trades(self):
        self.assertEqual(self.scan(meeting(20) + gap_meeting(62)), [])

    def test_the_decision_event_needs_exactly_the_five_codes(self):
        def drop(evs):
            evs[1]["markets"].pop()

        def extra(evs):
            m = dict(evs[1]["markets"][-1], ticker=evs[1]["markets"][-1]["ticker"][:-3] + "H50")
            evs[1]["markets"].append(m)

        def twice(evs):
            evs[1]["markets"][-1] = dict(evs[1]["markets"][1])

        def renamed(evs):
            evs[1]["markets"][-1]["ticker"] = evs[1]["markets"][-1]["ticker"][:-3] + "H75"

        def relabelled(evs):
            evs[1]["markets"][-1]["yes_sub_title"] = "Hike 50bps"
        for change in (drop, extra, twice, renamed, relabelled):
            evs = gap_meeting()
            change(evs)
            self.assertEqual(self.scan(evs), [], change.__name__)

    def test_every_rate_market_must_be_a_plain_above(self):
        def ladder(evs, k):
            return market(evs, rate_ticker(evs, k))
        cases = {
            "greater_or_equal": lambda evs: ladder(evs, 4.50).update(strike_type="greater_or_equal"),
            "a range": lambda evs: ladder(evs, 4.50).update(strike_type="between", cap_strike=4.75),
            "a cap": lambda evs: ladder(evs, 4.50).update(cap_strike=4.75),
            "no strike": lambda evs: ladder(evs, 4.50).update(floor_strike=None),
            "ticker disagrees": lambda evs: ladder(evs, 4.50).update(floor_strike=4.75),
            "no U - 0.25": lambda evs: evs[0]["markets"].remove(ladder(evs, 3.75)),
            "no U": lambda evs: evs[0]["markets"].remove(ladder(evs, 4.00)),
        }
        for name, change in cases.items():
            evs = gap_meeting()
            change(evs)
            self.assertEqual(self.scan(evs), [], name)
        day = day_in(20)
        evs = gap_meeting()                                            # off the quarter-point grid
        evs[0]["markets"].append(rate_market(day, 4.90, .01, .02))
        self.assertEqual(self.scan(evs), [])
        evs = gap_meeting()                                            # and never read as a nearby strike
        evs[0]["markets"].remove(market(evs, rate_ticker(evs, 4.00)))
        evs[0]["markets"].append(rate_market(day, 4.10, .44, .46))
        self.assertEqual(self.scan(evs), [])

    def test_the_wording_must_match_kalshis(self):
        day = day_in(20)
        other, later = short(day + datetime.timedelta(days=1)), long(day + datetime.timedelta(days=1))

        def edit(ticker_of, field, old, new):
            def change(evs):
                m = market(evs, ticker_of(evs))
                self.assertIn(old, m[field])
                m[field] = m[field].replace(old, new)
            return change
        f = lambda evs: rate_ticker(evs, 4.00)                         # noqa: E731
        top = lambda evs: rate_ticker(evs, 4.75)                       # noqa: E731
        cases = [
            edit(f, "rules_primary", "upper bound", "lower bound"),
            edit(f, "rules_primary", "4.00%", "4.25%"),                # rules and strike disagree
            edit(f, "rules_primary", short(day), other),
            edit(f, "rules_secondary", "one week", "two weeks"),
            edit(f, "rules_secondary", short(day), other),
            edit(f, "yes_sub_title", "Above 4.00%", "Above 4%"),
            edit(top, "rules_secondary", "meeting.", "meeting. If the meeting is moved, it resolves to a fair price."),
            edit(lambda evs: code_ticker(evs, "C25"), "rules_primary", "Cut of 25bps", "Hike of 25bps"),
            edit(lambda evs: code_ticker(evs, "H0"), "rules_primary", "Hike of 0bps", "Cut of 25bps"),
            edit(lambda evs: code_ticker(evs, "H0"), "rules_primary", "Hike of 0bps", "Hike of 25bps"),
            edit(lambda evs: code_ticker(evs, "C25"), "rules_primary", "Cut of 25bps", "Maintain"),
            edit(lambda evs: code_ticker(evs, "H25"), "rules_primary", "Hike of 25bps", "Hike of 50bps"),
            edit(lambda evs: code_ticker(evs, "H26"), "rules_primary", "Hike of 25bps", "Hike of 25bps or more"),
            edit(lambda evs: code_ticker(evs, "C26"), "rules_primary", "does a", "does not do a"),
            edit(lambda evs: code_ticker(evs, "C26"), "rules_primary", long(day), later),
            edit(lambda evs: code_ticker(evs, "H0"), "rules_secondary", "at most one", "at most two"),
            edit(lambda evs: code_ticker(evs, "H0"), "rules_secondary", "\u201cFed maintains rate\u201d resolves",
                 "\u201cCut 25bps\u201d resolves"),
            edit(lambda evs: code_ticker(evs, "H0"), "yes_sub_title", "Fed maintains rate", "Hold"),
        ]
        for i, change in enumerate(cases):
            evs = gap_meeting()
            change(evs)
            self.assertEqual(self.scan(evs), [], i)
        evs = gap_meeting()
        market(evs, code_ticker(evs, "H25"))["rules_secondary"] = ""
        self.assertEqual(self.scan(evs), [])

    def test_the_other_ways_kalshi_may_word_a_decision(self):
        # The ">" back in ">25bps", other words for a hold, the typo fixed, curly quotes or
        # straight, a doubled space: all still the same five outcomes.
        cases = [("C26", "rules_primary", "Cut of 25bps", "Cut of >25bps"),
                 ("C26", "rules_primary", "Cut of 25bps", "Cut of greater than 25bps"),
                 ("H26", "rules_primary", "Hike of 25bps", "Hike of >25bps"),
                 ("H0", "rules_primary", "Hike of 0bps", "Fed maintains rate"),
                 ("H0", "rules_primary", "Hike of 0bps", "Maintain"),
                 ("H0", "rules_primary", "Hike of 0bps", "Hold"),
                 ("H25", "rules_secondary", "\u201cHike 25 bps\u201d to Yes", "\u201cHike >25 bps\u201d to Yes"),
                 ("H25", "rules_secondary", "\u201cHike 25 bps\u201d to No", '"Hike 25 bps" to No'),
                 ("H25", "rules_secondary", "at most one", "at  most one")]
        for code, field, old, new in cases:
            evs = gap_meeting()
            m = market(evs, code_ticker(evs, code))
            self.assertIn(old, m[field])
            m[field] = m[field].replace(old, new)
            self.assertEqual(len(self.scan(evs)), 1, new)

    def test_event_flags_dates_and_close_times_must_fit(self):
        def flag(i, value):
            def change(evs):
                if value == "missing":
                    del evs[i]["mutually_exclusive"]
                else:
                    evs[i]["mutually_exclusive"] = value
            return change

        def closes(i, shift):
            def change(evs):
                for m in evs[i]["markets"]:
                    m["close_time"] = iso(ts(m["close_time"]) + shift)
            return change

        def one_close(evs):
            evs[0]["markets"][-1]["close_time"] = iso(close_of(day_in(20)) + 60)

        def renamed(evs):
            for m in evs[1]["markets"]:
                m["ticker"] = m["ticker"].replace(evs[1]["event_ticker"], "KXFEDDECISION-99JAN")
                m["event_ticker"] = "KXFEDDECISION-99JAN"
            evs[1]["event_ticker"] = "KXFEDDECISION-99JAN"

        def bounds(evs):
            market(evs, code_ticker(evs, "H0"))["settlement_bounds_type"] = "floor"

        def series(evs):
            evs[0]["series_ticker"] = "KXFEDX"

        def both_a_day_late(evs):
            closes(0, DAY)(evs)
            closes(1, DAY)(evs)
        cases = [flag(1, False), flag(1, "missing"), flag(0, True), flag(0, "missing"),
                 closes(0, DAY), closes(1, 2 * 3600), both_a_day_late, one_close, renamed, bounds, series]
        for i, change in enumerate(cases):
            evs = gap_meeting()
            change(evs)
            self.assertEqual(self.scan(evs), [], i)

    def test_junk_data_means_no_signals_not_a_crash(self):
        evs = gap_meeting()
        c = fake_for(evs)
        c.event_list = [None, 7, "x"] + evs
        self.assertEqual(len(self.scan(None, client=c)), 1)            # stray entries are ignored
        evs[0]["markets"] = "oops"
        self.assertEqual(self.scan(None, client=c), [])
        evs = gap_meeting()
        evs[1]["markets"][0]["ticker"] = 5
        self.assertEqual(self.scan(evs), [])
        for settled in ({"KXFED-26OCT": 1}, [None], [{"ticker": 5, "close_time": 3}], history() + ["junk"]):
            self.assertEqual(self.scan(gap_meeting(), settled=settled), [], settled)

    def test_held_or_closing_markets_are_skipped(self):
        evs = gap_meeting()
        self.assertEqual(self.scan(evs, held={rate_ticker(evs, 4.00)}), [])
        self.assertEqual(self.scan(evs, held={code_ticker(evs, "H0")}), [])
        evs = gap_meeting()
        market(evs, rate_ticker(evs, 4.00))["status"] = "closed"
        self.assertEqual(self.scan(evs), [])
        first = close_of(day_in(20))
        with mock.patch.object(fed_link_arb.time, "time", return_value=first - 120):   # 2 minutes left
            self.assertEqual(self.scan(gap_meeting()), [])
        with mock.patch.object(fed_link_arb.time, "time", return_value=first - 3600):
            self.assertEqual(len(self.scan(gap_meeting())), 1)

    def test_budget_and_caps_hold(self):
        sig = self.scan(gap_meeting(), max_per_arb=5.0)[0]
        self.assertLessEqual(sig.cost, 5.0 + 1e-9)
        sig = self.scan(gap_meeting(), budget=4.0)[0]
        self.assertLessEqual(sig.cost, 4.0 + 1e-9)
        sig = self.scan(gap_meeting())[0]
        self.assertLessEqual(sig.partial_loss, config.DEFAULTS["max_per_trade"] + 1e-9)   # a half-bought pair
        self.assertLessEqual(sig.cost, 30.0 + 1e-9)

    def test_never_uses_U_plus_025_or_U_minus_05(self):
        # Whether a 50bp move counts as "25bps" or ">25bps" decides those links, and the rules
        # don't say. So big "gaps" there are left alone.
        crazy = {"rates": {4.25: (.90, .92), 3.50: (.03, .05)}}
        self.assertEqual(self.scan(meeting(**crazy)), [])
        evs = meeting(rates={**GAP["rates"], **crazy["rates"]}, decisions=GAP["decisions"])
        sigs = self.scan(evs)
        self.assertEqual(len(sigs), 1)
        off = {rate_ticker(evs, 4.25), rate_ticker(evs, 3.50)}
        self.assertFalse({leg.ticker for leg in sigs[0].legs} & off)

    def test_too_good_to_be_true_prices_are_spotted(self):
        normal = fed_link_arb.meeting(*meeting())
        self.assertFalse(fed_link_arb.too_good(fed_link_arb.links(normal, 16), []))
        cheap = fed_link_arb.meeting(*meeting(rates={3.75: (.04, .06)}, decisions={"C25": (.24, .26)}))
        self.assertTrue(fed_link_arb.too_good(fed_link_arb.links(cheap, 16), []))   # a 3-leg set at 34c

    def test_signals_stay_on_paper(self):
        sigs = self.scan(gap_meeting())
        self.assertTrue(sigs and all(s.meta["paper_only"] for s in sigs))
        self.assertEqual(STRATEGY.proven(None), (False, fed_link_arb.UNVERIFIED))
        self.assertEqual(fed_link_arb.VERIFIED, {})

    def test_api_calls_stay_few(self):
        def counted(c):
            calls = []
            for name in ("events", "settled_markets", "orderbook", "markets", "market"):
                real = getattr(c, name)
                setattr(c, name, lambda *a, _n=name, _r=real, **k: calls.append((_n, a, k)) or _r(*a, **k))
            return c, calls
        evs = gap_meeting()
        c, calls = counted(fake_for(evs))
        self.assertEqual(len(STRATEGY.scan(ctx(c))), 1)
        events = [k for n, _, k in calls if n == "events"]
        self.assertEqual(events, [{"status": "open", "with_nested_markets": "true"}])   # the run's shared download
        self.assertEqual([a for n, a, _ in calls if n == "settled_markets"], [("KXFED",)])
        books = [a[0] for n, a, _ in calls if n == "orderbook"]
        self.assertEqual(sorted(books), sorted([rate_ticker(evs, 4.00), code_ticker(evs, "H0")]))  # the cheap pair's
        self.assertFalse([n for n, _, _ in calls if n in ("markets", "market")])
        c, calls = counted(fake_for(meeting()))                       # nothing cheap: no order books
        self.assertEqual(STRATEGY.scan(ctx(c)), [])
        self.assertEqual([n for n, _, _ in calls], ["events", "settled_markets"])
        c, calls = counted(fake_for([]))                              # no Fed events: nothing else
        self.assertEqual(STRATEGY.scan(ctx(c)), [])
        self.assertEqual([n for n, _, _ in calls], ["events"])

    def test_random_prices_never_lose(self):
        rng = random.Random(11)
        found = 0
        for _ in range(300):
            rate = rng.choice((3.50, 3.75, 4.00, 4.25, 4.50))
            rates = {}
            for k in STRIKES:
                bid = round(rng.uniform(0.01, 0.95), 2)
                rates[k] = (bid, round(min(bid + rng.choice((0.01, 0.02, 0.04)), 0.99), 2))
            decisions = {}
            for code in CODES:
                bid = round(rng.uniform(0.01, 0.9), 2)
                decisions[code] = (bid, round(min(bid + rng.choice((0.01, 0.02, 0.04)), 0.99), 2))
            evs = meeting(rates=rates, decisions=decisions)
            c = fake_for(evs, settled=history(last=rate), size=rng.randint(1, 60))
            allowed = {rate_ticker(evs, rate), rate_ticker(evs, rate - 0.25)} | {code_ticker(evs, x) for x in CODES}
            for sig in STRATEGY.scan(ctx(c)):
                found += 1
                self.assertLockedIn(sig, rate_before=rate)
                self.assertLessEqual({leg.ticker for leg in sig.legs}, allowed)
                self.assertTrue(any(leg.ticker.startswith("KXFED-") for leg in sig.legs))
                cost = sum(leg.count * leg.price + fee(leg.count, leg.price) for leg in sig.legs)
                self.assertAlmostEqual(sig.cost, cost, places=6)
                self.assertEqual(sig.payout_min, sig.legs[0].count * 1.0)
        self.assertGreater(found, 50)   # the test saw real signals, not none


class RecheckTests(unittest.TestCase):
    def test_recheck_resizes_or_drops(self):
        evs = gap_meeting()
        c = fake_for(evs)
        cx = ctx(c)
        sig = STRATEGY.scan(cx)[0]
        f, hold = rate_ticker(evs, 4.00), code_ticker(evs, "H0")
        set_book(c, f, .44, .46, bid_size=5)                     # NO on "above 4.00%" thins out
        fresh = STRATEGY.recheck(cx, sig)
        self.assertEqual([leg.count for leg in fresh.legs], [5, 5])
        self.assertEqual((fresh.closes_at, fresh.meta), (sig.closes_at, sig.meta))
        set_book(c, hold, .50, .52)                              # the gap is gone: 56c + 50c
        self.assertIsNone(STRATEGY.recheck(cx, sig))
        set_book(c, hold, .67, .69)
        self.assertIsNotNone(STRATEGY.recheck(cx, sig))
        cx.held.add(f)                                           # bought by an earlier signal
        self.assertIsNone(STRATEGY.recheck(cx, sig))
        cx.held.clear()
        with mock.patch.object(fed_link_arb.time, "time", return_value=sig.meta["first_close"] - 60):
            self.assertIsNone(STRATEGY.recheck(cx, sig))         # too close to the bell


class EngineTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        orders_proven()
        patcher = mock.patch.object(engine, "load_all", lambda: {"fed_link_arb": STRATEGY})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_registered_and_starts_on_paper(self):
        self.assertIn("fed_link_arb", MODULES)
        self.assertEqual((STRATEGY.name, STRATEGY.kind), ("fed_link_arb", "arbitrage"))
        self.assertEqual(config.DEFAULTS["strategies"]["fed_link_arb"], {"mode": "paper"})
        self.assertEqual(fed_link_arb.VERIFIED, {})   # no meeting's rules read by a person yet

    def test_paper_run_records_both_legs_and_buys_nothing(self):
        config.update({"strategies": {"fed_link_arb": {"mode": "paper"}}})
        c = fake_for(gap_meeting())
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual((s["paper_trades"], c.orders), (1, []))
        self.assertEqual(len(paper.open_rows()), 2)
        row = db.query("SELECT * FROM signals")[0]
        self.assertEqual((row["strategy"], row["status"], row["guaranteed"]), ("fed_link_arb", "accepted", 1))

    def test_live_still_places_no_real_order(self):
        config.update({"strategies": {"fed_link_arb": {"mode": "live"}}})
        c = fake_for(gap_meeting())
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual((s["live_trades"], s["paper_trades"], c.orders), (0, 1, []))
        self.assertTrue(any("fed_link_arb: paper instead of live" in n and "checked by hand" in n for n in s["notes"]))

    def pin(self, evs, rate_before=U, rules=None):
        """Put the meeting on VERIFIED, as a person would after reading its rules."""
        meet = fed_link_arb.meeting(*evs)
        key = rules or fed_link_arb.rules_key(meet, round(U * 4))
        patcher = mock.patch.dict(fed_link_arb.VERIFIED, {evs[0]["event_ticker"]: {"rate_before": rate_before,
                                                                                  "rules": key}})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_a_pinned_meeting_with_the_same_rate_and_wording_trades_live(self):
        config.update({"strategies": {"fed_link_arb": {"mode": "live"}}})
        evs = gap_meeting()
        self.pin(evs, rate_before=4.25)                        # pinned at another rate before it
        c = fake_for(evs)
        s = engine.run_once(c)
        self.assertEqual((s["live_trades"], s["paper_trades"], c.orders), (0, 1, []))
        self.pin(evs, rules="If the Fed does something, then the market resolves to Yes.")
        c = fake_for(evs)
        s = engine.run_once(c)                                 # Kalshi's wording changed since
        self.assertEqual((s["live_trades"], c.orders), (0, []))

    def test_a_pinned_meeting_trades_live(self):
        config.update({"strategies": {"fed_link_arb": {"mode": "live"}}})
        evs = gap_meeting()
        self.pin(evs)
        c = fake_for(evs)
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual((s["live_trades"], len(c.orders)), (1, 2))
        self.assertEqual({(o[0], o[1]) for o in c.orders},
                         {(rate_ticker(evs, 4.00), "no"), (code_ticker(evs, "H0"), "no")})
        self.assertEqual(c.orders[0][2], c.orders[1][2])

    def test_a_pinned_meeting_that_looks_too_good_stays_on_paper(self):
        # An emergency cut 4.00 -> 3.75 that the settled history can't show: the market now
        # prices a hold at 3.75. Some links look far too good, the sign U is wrong, so even a
        # pinned meeting buys nothing real (critic, round 3).
        config.update({"strategies": {"fed_link_arb": {"mode": "live"}}})
        rates = {3.50: (.74, .76), 3.75: (.04, .06), 4.00: (.01, .02), 4.25: (.01, .02)}
        decisions = {"C26": (.01, .02), "C25": (.24, .26), "H0": (.69, .71), "H25": (.04, .06), "H26": (.01, .02)}
        evs = meeting(20, rates=rates, decisions=decisions)
        self.pin(evs)
        c = fake_for(evs)
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual((s["live_trades"], c.orders), (0, []))

    def test_a_meeting_too_far_off_is_refused(self):
        config.update({"strategies": {"fed_link_arb": {"mode": "paper"}}})
        s = engine.run_once(fake_for(gap_meeting(40)))
        self.assertTrue(s["ok"], s)
        self.assertEqual(s["paper_trades"], 0)
        self.assertIn("locks money up", db.query("SELECT status FROM signals")[0]["status"])

    def test_promotion_never_turns_it_live(self):
        from kal.research import promote
        before = config.load()["strategies"]["fed_link_arb"]
        promote.apply(config.load())
        self.assertEqual(config.load()["strategies"]["fed_link_arb"], before)


if __name__ == "__main__":
    unittest.main()
