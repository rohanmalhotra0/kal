"""Tests for date_ladder_arb.

The key check: for every signal, play out every day the thing could happen (before each
deadline, on it, after the last one, never) and confirm the legs pay more than they cost.
It reads "Before <date>" on its own (resolves_yes below), not with the strategy's code,
so a bug in the strategy can't hide itself.
"""
import datetime
import random
import time
import unittest
from datetime import timezone
from unittest import mock

from kal import config, db, engine, paper
from kal.client import fee, price
from kal.strategies import MODULES, date_ladder_arb
from kal.strategies.base import Context
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven

DAY = 86400
RULES = "If Drake releases a new studio album before {d}, then the market resolves to Yes."


def iso(t):
    return datetime.datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def market(ticker, ev, when, bid, ask, close=None, rules=RULES, status="active"):
    """A "Before <when>" market. Closes at its deadline unless told otherwise."""
    words = f"{when:%b} {when.day}, {when.year}"
    stamp = datetime.datetime(when.year, when.month, when.day, 4, tzinfo=timezone.utc).timestamp()
    m = {"ticker": ticker, "event_ticker": ev, "status": status, "yes_sub_title": f"Before {words}",
         "close_time": iso(close or stamp), "rules_primary": rules.format(d=words),
         "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}"}
    return m


def soon(days):
    return (datetime.datetime.now(timezone.utc) + datetime.timedelta(days=days)).date()


def date_event(quotes=((.40, .42), (.30, .32), (.60, .62)), days=(10, 20, 25), ev="KXALBUM-NEWDRA",
               exclusive=False):
    """quotes: (yes_bid, yes_ask) per deadline, earliest first. The default has the 10-day
    market bid 40c and the 20-day market offered at 32c: upside down."""
    markets = [market(f"{ev}-{soon(d):%y%b%d}".upper(), ev, soon(d), b, a) for d, (b, a) in zip(days, quotes)]
    return {"event_ticker": ev, "title": "New Drake album?", "mutually_exclusive": exclusive, "markets": markets}


def set_book(c, ticker, yes_bid, yes_ask, bid_size=40, ask_size=40):
    """Kalshi books hold bids only: a YES ask shows up as a NO bid at 1 - ask."""
    c.books[ticker] = {"yes": [(yes_bid, bid_size)] if yes_bid else [],
                       "no": [(round(1 - yes_ask, 4), ask_size)] if yes_ask else []}


def fake_for(*events, size=40):
    c = FakeKalshi()
    c.event_list = list(events)
    for ev in events:
        for m in ev["markets"]:
            set_book(c, m["ticker"], price(m, "yes_bid"), price(m, "yes_ask"), size, size)
    return c


def ctx(client, budget=100.0, max_per_arb=30.0, held=()):
    cfg = dict(config.DEFAULTS, max_per_arb=max_per_arb)
    return Context(client, cfg, "paper", budget, budget, set(held), {}, {})


# ---------- the independent outcome check ----------

def resolves_yes(m, happened):
    """"Before Sep 1, 2026" is YES if it happened on Aug 31 or earlier. None: never happened."""
    words = m["yes_sub_title"][len("Before "):]
    deadline = datetime.datetime.strptime(words, "%b %d, %Y").date()
    return happened is not None and happened < deadline


def outcomes(markets):
    days = [datetime.datetime.strptime(m["yes_sub_title"][len("Before "):], "%b %d, %Y").date() for m in markets]
    out = {None, min(days) - datetime.timedelta(days=30), max(days) + datetime.timedelta(days=1)}
    for d in days:
        out |= {d - datetime.timedelta(days=1), d}
    return out


def payout(sig, markets, happened):
    by_ticker = {m["ticker"]: m for m in markets}
    return sum(leg.count for leg in sig.legs if (leg.side == "yes") == resolves_yes(by_ticker[leg.ticker], happened))


class DateLadderTests(unittest.TestCase):
    def scan(self, *events, **kw):
        c = kw.pop("client", None) or fake_for(*events)
        return date_ladder_arb.STRATEGY.scan(ctx(c, **kw))

    def assertLockedIn(self, sig, markets):
        self.assertTrue(sig.guaranteed)
        self.assertGreater(sig.payout_min - sig.cost, 0)
        for happened in outcomes(markets):
            got = payout(sig, markets, happened)
            self.assertGreaterEqual(got, sig.payout_min, f"happening on {happened} pays less than promised")
            self.assertGreater(got - sig.cost, 0, f"happening on {happened} loses money")

    def test_upside_down_ladder_locks_profit_whenever_it_happens(self):
        ev = date_event()
        sigs = self.scan(ev)
        self.assertEqual(len(sigs), 1)
        sig = sigs[0]
        early, mid = ev["markets"][0], ev["markets"][1]
        legs = {leg.side: leg for leg in sig.legs}
        self.assertEqual((legs["yes"].ticker, legs["no"].ticker), (mid["ticker"], early["ticker"]))
        self.assertEqual((legs["yes"].price, legs["no"].price), (0.32, 0.60))
        self.assertEqual(legs["yes"].count, legs["no"].count)
        self.assertEqual(sig.payout_min, legs["yes"].count * 1.0)
        self.assertAlmostEqual(sig.exp_profit, sig.payout_min - sig.cost)
        self.assertEqual(sig.closes_at, date_ladder_arb.close_ts(early))   # $1 by the earlier close
        self.assertEqual(sig.strategy, "date_ladder_arb")
        self.assertIn(f"by {soon(10):%b} {soon(10).day}", sig.note)
        self.assertLockedIn(sig, ev["markets"])

    def test_normal_ladder_finds_nothing(self):
        ev = date_event(quotes=((.20, .22), (.30, .32), (.40, .42)))
        self.assertEqual(self.scan(ev), [])

    def test_order_comes_from_the_wording_not_close_time(self):
        # Listed last-deadline-first, and the earlier market's close_time is after the later
        # one's (Kalshi rewrites close_time). The earlier DEADLINE is still the NO leg.
        ev = date_event()
        early, mid, late = ev["markets"]
        early["close_time"] = iso(time.time() + 40 * DAY)
        ev["markets"] = [late, mid, early]
        sigs = self.scan(ev)
        self.assertEqual(len(sigs), 1)
        legs = {leg.side: leg.ticker for leg in sigs[0].legs}
        self.assertEqual(legs, {"yes": mid["ticker"], "no": early["ticker"]})
        self.assertLockedIn(sigs[0], ev["markets"])

    def test_a_later_market_that_cant_close_early_pays_at_its_own_close(self):
        ev = date_event()
        ev["markets"][1]["can_close_early"] = False
        sig = self.scan(ev)[0]
        self.assertEqual(sig.closes_at, date_ladder_arb.close_ts(ev["markets"][1]))

    def test_fees_can_eat_a_thin_gap(self):
        # 31c bid vs 30c ask: 1c before fees, less than nothing after.
        ev = date_event(quotes=((.31, .33), (.29, .30), (.60, .62)))
        self.assertEqual(self.scan(ev), [])

    def test_every_pair_is_checked_not_just_neighbours(self):
        # Only the first and last deadlines are upside down (the middle one's spread is wide).
        ev = date_event(quotes=((.50, .52), (.40, .55), (.43, .45)))
        sigs = self.scan(ev)
        self.assertEqual(len(sigs), 1)
        legs = {leg.side: leg.ticker for leg in sigs[0].legs}
        self.assertEqual(legs, {"yes": ev["markets"][2]["ticker"], "no": ev["markets"][0]["ticker"]})
        self.assertLockedIn(sigs[0], ev["markets"])

    # ---- anything odd and the event is skipped ----

    def test_mutually_exclusive_events_are_not_ladders(self):
        self.assertEqual(self.scan(date_event(exclusive=True)), [])
        ev = date_event()
        del ev["mutually_exclusive"]   # unknown counts as no
        self.assertEqual(self.scan(ev), [])

    def test_wording_must_be_exactly_a_date(self):
        for words in ("Drake before Sep 1, 2026", "Before Sep 1, 2026 (ET)", "By Sep 1, 2026",
                      "Before Sep 1", "Before Feb 30, 2026", "On Sep 1, 2026", ""):
            ev = date_event()
            ev["markets"][0]["yes_sub_title"] = words
            self.assertEqual(self.scan(ev), [], words)

    def test_two_markets_with_one_deadline_are_skipped(self):
        ev = date_event()
        ev["markets"][1]["yes_sub_title"] = ev["markets"][0]["yes_sub_title"]
        self.assertEqual(self.scan(ev), [])

    def test_rules_must_match_once_dates_are_taken_out(self):
        ev = date_event()
        ev["markets"][1]["rules_primary"] = ev["markets"][1]["rules_primary"].replace("studio album", "single")
        self.assertEqual(self.scan(ev), [])

    def test_other_numbers_in_the_rules_still_count(self):
        ev = date_event()
        rules = "If Bitcoin trades above ${p} before {d}, then the market resolves to Yes."
        for m, p in zip(ev["markets"], ("2,050", "2,060", "2,050")):
            m["rules_primary"] = rules.format(p=p, d=m["yes_sub_title"][len("Before "):])
        self.assertEqual(self.scan(ev), [])

    def test_dates_written_other_ways_still_match(self):
        ev = date_event()
        for m in ev["markets"]:
            d = datetime.datetime.strptime(m["yes_sub_title"][len("Before "):], "%b %d, %Y").date()
            m["rules_primary"] = (f"If it happens before {d:%B} {d.day}th, {d.year} (by the end of "
                                  f"{d:%B} {d.year}), the market resolves to Yes.")
        self.assertEqual(len(self.scan(ev)), 1)

    def test_no_rules_no_trade(self):
        ev = date_event()
        for m in ev["markets"]:
            m["rules_primary"] = ""
        self.assertEqual(self.scan(ev), [])

    def test_a_fair_price_clause_or_payout_bounds_mean_no(self):
        ev = date_event()
        ev["markets"][0]["rules_secondary"] = "If the source is unavailable, the market will resolve to a fair price."
        ev["markets"][1]["rules_secondary"] = ev["markets"][0]["rules_secondary"]
        ev["markets"][2]["rules_secondary"] = ev["markets"][0]["rules_secondary"]
        self.assertEqual(self.scan(ev), [])
        ev = date_event()
        ev["markets"][2]["settlement_bounds_type"] = "floor"
        self.assertEqual(self.scan(ev), [])

    def test_different_subjects_are_not_a_ladder(self):
        ev = date_event()
        ev["markets"][0]["custom_strike"] = {"person": "A"}
        self.assertEqual(self.scan(ev), [])

    def test_held_or_closing_markets_are_skipped(self):
        ev = date_event()
        self.assertEqual(self.scan(ev, held={ev["markets"][0]["ticker"]}), [])
        ev = date_event()
        ev["markets"][0]["close_time"] = iso(time.time() + 120)   # 2 minutes left
        self.assertEqual(self.scan(ev), [])
        ev = date_event()
        ev["markets"][1]["close_time"] = iso(time.time() + 120)   # the later leg closes first
        self.assertEqual(self.scan(ev), [])
        ev = date_event()
        ev["markets"][0]["status"] = "closed"
        self.assertEqual(self.scan(ev), [])

    def test_budget_and_arb_cap_hold(self):
        sig = self.scan(date_event(), max_per_arb=5.0)[0]
        self.assertLessEqual(sig.cost, 5.0 + 1e-9)
        sig = self.scan(date_event(), budget=4.0)[0]
        self.assertLessEqual(sig.cost, 4.0 + 1e-9)

    def test_random_ladders_never_lose(self):
        rng = random.Random(7)
        found = 0
        for i in range(300):
            n = rng.randint(2, 5)
            days = sorted(rng.sample(range(2, 29), n))
            quotes = []
            for _ in range(n):
                bid = round(rng.uniform(0.02, 0.9), 2)
                quotes.append((bid, round(min(bid + rng.choice((0.01, 0.02, 0.05)), 0.99), 2)))
            ev = date_event(quotes=tuple(quotes), days=tuple(days), ev=f"KXR{i}")
            c = fake_for(ev, size=rng.randint(1, 60))
            for sig in date_ladder_arb.STRATEGY.scan(ctx(c)):
                found += 1
                self.assertLockedIn(sig, ev["markets"])
                cost = sum(leg.count * leg.price + fee(leg.count, leg.price) for leg in sig.legs)
                self.assertAlmostEqual(sig.cost, cost, places=6)
        self.assertGreater(found, 20)   # the test saw real signals, not none


class RecheckTests(unittest.TestCase):
    def test_recheck_resizes_or_drops(self):
        ev = date_event()
        c = fake_for(ev)
        cx = ctx(c)
        sig = date_ladder_arb.STRATEGY.scan(cx)[0]
        set_book(c, ev["markets"][1]["ticker"], .30, .32, ask_size=5)   # the YES leg thins out
        fresh = date_ladder_arb.STRATEGY.recheck(cx, sig)
        self.assertEqual([leg.count for leg in fresh.legs], [5, 5])
        self.assertEqual(fresh.closes_at, sig.closes_at)
        set_book(c, ev["markets"][1]["ticker"], .30, .45)                 # the gap is gone
        self.assertIsNone(date_ladder_arb.STRATEGY.recheck(cx, sig))
        cx.held.add(ev["markets"][0]["ticker"])
        self.assertIsNone(date_ladder_arb.STRATEGY.recheck(cx, sig))


class EngineTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        orders_proven()
        patcher = mock.patch.object(engine, "load_all", lambda: {"date_ladder_arb": date_ladder_arb.STRATEGY})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_registered_and_starts_on_paper(self):
        self.assertIn("date_ladder_arb", MODULES)
        self.assertEqual(date_ladder_arb.STRATEGY.name, "date_ladder_arb")
        self.assertEqual(date_ladder_arb.STRATEGY.kind, "arbitrage")
        self.assertEqual(config.DEFAULTS["strategies"]["date_ladder_arb"], {"mode": "paper"})

    def test_paper_run_records_both_legs_and_buys_nothing(self):
        config.update({"strategies": {"date_ladder_arb": {"mode": "paper"}}})
        c = fake_for(date_event())
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual((s["paper_trades"], c.orders), (1, []))
        self.assertEqual(len(paper.open_rows()), 2)
        row = db.query("SELECT * FROM signals")[0]
        self.assertEqual((row["strategy"], row["status"], row["guaranteed"]), ("date_ladder_arb", "accepted", 1))

    def test_live_by_hand_buys_both_legs_thinnest_first(self):
        config.update({"strategies": {"date_ladder_arb": {"mode": "live"}}})
        ev = date_event()
        c = fake_for(ev)
        set_book(c, ev["markets"][0]["ticker"], .40, .42, bid_size=15)   # NO on the earlier date is thin
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual(s["live_trades"], 1)
        self.assertEqual([(o[0], o[1]) for o in c.orders],
                         [(ev["markets"][0]["ticker"], "no"), (ev["markets"][1]["ticker"], "yes")])
        self.assertEqual(c.orders[0][2], c.orders[1][2])

    def test_money_locked_past_max_arb_days_is_refused(self):
        config.update({"strategies": {"date_ladder_arb": {"mode": "paper"}}})
        c = fake_for(date_event(days=(40, 50, 55)))
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual(s["paper_trades"], 0)
        self.assertIn("locks money up", db.query("SELECT status FROM signals")[0]["status"])

    def test_too_good_to_be_true_is_refused(self):
        # 70c bid on the earlier date, 10c ask on the later: "locked in" at 40% is a misread.
        config.update({"strategies": {"date_ladder_arb": {"mode": "paper"}}})
        c = fake_for(date_event(quotes=((.70, .72), (.08, .10), (.80, .82))))
        s = engine.run_once(c)
        self.assertEqual(s["paper_trades"], 0)
        self.assertIn("too good to be true", db.query("SELECT status FROM signals")[0]["status"])


if __name__ == "__main__":
    unittest.main()
