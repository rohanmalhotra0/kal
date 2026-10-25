"""Tests for ladder_arb and bracket_arb.

The key check: for every signal, play out every possible value of the number
(each strike region, each edge) and confirm the legs pay more than they cost.
That uses its own reading of strike types (resolves_yes below), not the
strategy's, so a bug in the strategy's logic can't hide itself.
"""
import random
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from kal import config, db, engine, paper
from kal.client import fee, price
from kal.strategies import MODULES, bracket_arb, ladder_arb
from kal.strategies.base import Context, Leg
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven

FUTURE = datetime.fromtimestamp(time.time() + 3 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
UP, DOWN = ladder_arb.UP, ladder_arb.DOWN


# ---------- fixtures ----------

def market(ticker, ev, kind, floor=None, cap=None, bid=None, ask=None, close=FUTURE, status="active"):
    m = {"ticker": ticker, "event_ticker": ev, "strike_type": kind, "close_time": close, "status": status}
    if floor is not None:
        m["floor_strike"] = floor
    if cap is not None:
        m["cap_strike"] = cap
    if bid is not None:
        m["yes_bid_dollars"] = f"{bid:.4f}"
    if ask is not None:
        m["yes_ask_dollars"] = f"{ask:.4f}"
    return m


def ladder_event(kind="greater", strikes=(50, 60, 70), quotes=((.48, .50), (.62, .64), (.20, .22)),
                 ev="LAD", close=FUTURE, exclusive=False):
    """quotes: (yes_bid, yes_ask) per rung."""
    markets = []
    for s, (bid, ask) in zip(strikes, quotes):
        lo, hi = (s, None) if kind in UP else (None, s)
        markets.append(market(f"{ev}-{s}", ev, kind, lo, hi, bid, ask, close))
    return {"event_ticker": ev, "title": "Test ladder", "mutually_exclusive": exclusive, "markets": markets}


def bracket_event(edges=(40, 50, 60), asks=(.20, .25, .25, .20), ev="BR", low="less_or_equal",
                  high="greater", exclusive=True):
    """A bottom bracket, len(edges)-1 'between' brackets, and a top bracket."""
    markets = [market(f"{ev}-B", ev, low, cap=edges[0])]
    for lo, hi in zip(edges, edges[1:]):
        markets.append(market(f"{ev}-{lo}", ev, "between", lo, hi))
    markets.append(market(f"{ev}-T", ev, high, floor=edges[-1]))
    for m, a in zip(markets, asks):
        m["yes_ask_dollars"], m["yes_bid_dollars"] = f"{a:.4f}", f"{max(a - .02, .01):.4f}"
    return {"event_ticker": ev, "title": "Test brackets", "mutually_exclusive": exclusive, "markets": markets}


def fake_for(*events, size=40):
    """A FakeKalshi whose order books match each market's listed bid and ask."""
    c = FakeKalshi()
    c.event_list = list(events)
    for ev in events:
        for m in ev["markets"]:
            set_book(c, m["ticker"], price(m, "yes_bid"), price(m, "yes_ask"), size, size)
    return c


def set_book(c, ticker, yes_bid, yes_ask, bid_size=40, ask_size=40):
    """Kalshi books hold bids only: a YES ask shows up as a NO bid at 1 - ask."""
    c.books[ticker] = {"yes": [(yes_bid, bid_size)] if yes_bid else [],
                       "no": [(round(1 - yes_ask, 4), ask_size)] if yes_ask else []}


def ctx(client, budget=100.0, max_per_arb=30.0, held=(), params=None):
    cfg = dict(config.DEFAULTS, max_per_arb=max_per_arb)
    return Context(client, cfg, "paper", budget, budget, set(held), {}, params or {})


# ---------- the independent outcome check ----------

def resolves_yes(m, x):
    """How a market settles if the number comes in at x. 'between' is read as inclusive."""
    kind, lo, hi = m["strike_type"], m.get("floor_strike"), m.get("cap_strike")
    return {"greater": lambda: x > lo, "greater_or_equal": lambda: x >= lo,
            "less": lambda: x < hi, "less_or_equal": lambda: x <= hi,
            "between": lambda: lo <= x <= hi}[kind]()


def outcomes(markets):
    """Every distinct region: each strike, just either side of it, and far outside."""
    pts = sorted({v for m in markets for v in (m.get("floor_strike"), m.get("cap_strike")) if v is not None})
    xs = {pts[0] - 100, pts[-1] + 100}
    for p in pts:
        xs |= {p, p - 0.25, p + 0.25}
    return sorted(xs)


def payout(sig, markets, x):
    by_ticker = {m["ticker"]: m for m in markets}
    return sum(leg.count for leg in sig.legs if (leg.side == "yes") == resolves_yes(by_ticker[leg.ticker], x))


class OutcomeChecks(unittest.TestCase):
    def assertLockedIn(self, sig, markets):
        self.assertTrue(sig.guaranteed)
        self.assertGreater(sig.payout_min - sig.cost, 0)
        for x in outcomes(markets):
            got = payout(sig, markets, x)
            self.assertGreaterEqual(got, sig.payout_min, f"value {x} pays less than promised")
            self.assertGreater(got - sig.cost, 0, f"value {x} loses money")


# ---------- ladder ----------

class LadderTests(OutcomeChecks):
    def scan(self, *events, **kw):
        c = kw.pop("client", None) or fake_for(*events)
        return ladder_arb.STRATEGY.scan(ctx(c, **kw))

    def test_inverted_ladder_locks_profit_in_every_outcome(self):
        ev = ladder_event()
        sigs = self.scan(ev)
        self.assertEqual(len(sigs), 1)
        sig = sigs[0]
        legs = {leg.side: leg for leg in sig.legs}
        self.assertEqual((legs["yes"].ticker, legs["no"].ticker), ("LAD-50", "LAD-60"))
        self.assertEqual((legs["yes"].price, legs["no"].price), (0.50, 0.38))
        self.assertEqual(sig.payout_min, legs["yes"].count * 1.0)
        self.assertEqual(legs["yes"].count, legs["no"].count)
        self.assertAlmostEqual(sig.exp_profit, sig.payout_min - sig.cost)
        self.assertLockedIn(sig, ev["markets"])

    def test_normal_ladder_finds_nothing(self):
        ev = ladder_event(quotes=((.70, .72), (.50, .52), (.30, .32)))
        self.assertEqual(self.scan(ev), [])

    def test_less_ladder_is_the_mirror(self):
        ev = ladder_event("less", (40, 50, 60), ((.55, .57), (.38, .40), (.80, .82)))
        sigs = self.scan(ev)
        self.assertEqual(len(sigs), 1)
        legs = {leg.side: leg.ticker for leg in sigs[0].legs}
        self.assertEqual(legs, {"yes": "LAD-50", "no": "LAD-40"})  # YES on the higher cap
        self.assertLockedIn(sigs[0], ev["markets"])

    def test_or_equal_ladders_work_too(self):
        for kind in ("greater_or_equal", "less_or_equal"):
            quotes = ((.48, .50), (.62, .64), (.20, .22)) if kind in UP else ((.55, .57), (.38, .40), (.80, .82))
            ev = ladder_event(kind, quotes=quotes)
            sigs = self.scan(ev)
            self.assertEqual(len(sigs), 1, kind)
            self.assertLockedIn(sigs[0], ev["markets"])

    def test_mixed_strike_types_rejected(self):
        ev = ladder_event()
        ev["markets"][1]["strike_type"] = "greater_or_equal"
        self.assertEqual(self.scan(ev), [])
        ev = ladder_event()
        ev["markets"][2].update(strike_type="between", cap_strike=80)
        self.assertEqual(self.scan(ev), [])

    def test_missing_or_bad_strike_rejected(self):
        for bad in (None, "", "abc", float("nan"), True):
            ev = ladder_event()
            ev["markets"][0]["floor_strike"] = bad
            self.assertEqual(self.scan(ev), [], repr(bad))
        ev = ladder_event()
        ev["markets"][0]["cap_strike"] = 99  # a 'greater' market shouldn't have a cap
        self.assertEqual(self.scan(ev), [])

    def test_different_close_times_rejected(self):
        ev = ladder_event()
        ev["markets"][0]["close_time"] = "2098-01-01T00:00:00Z"
        self.assertEqual(self.scan(ev), [])
        ev = ladder_event()
        del ev["markets"][1]["close_time"]
        self.assertEqual(self.scan(ev), [])

    def test_closing_soon_rejected(self):
        soon = datetime.fromtimestamp(time.time() + 60, timezone.utc).isoformat()
        self.assertEqual(self.scan(ladder_event(close=soon)), [])

    def test_duplicate_strikes_rejected(self):
        self.assertEqual(self.scan(ladder_event(strikes=(50, 60, 60))), [])

    def test_mutually_exclusive_flag_rejected(self):
        self.assertEqual(self.scan(ladder_event(exclusive=True)), [])
        ev = ladder_event()
        del ev["mutually_exclusive"]
        self.assertEqual(self.scan(ev), [])

    def test_held_or_inactive_markets_skipped(self):
        self.assertEqual(self.scan(ladder_event(), held={"LAD-60"}), [])
        ev = ladder_event()
        ev["markets"][0]["status"] = "closed"
        self.assertEqual(self.scan(ev), [])

    def test_no_book_means_no_trade(self):
        ev = ladder_event()
        c = fake_for(ev)
        c.books["LAD-50"] = {"yes": [], "no": []}  # the listing says inverted, the book is empty
        self.assertEqual(self.scan(ev, client=c), [])

    def test_fees_kill_a_tiny_arb(self):
        ev = ladder_event(quotes=((.49, .50), (.51, .53), (.20, .22)))
        self.assertLess(0.50 + (1 - 0.51), 1)  # looks like a 1 cent profit before fees
        self.assertEqual(self.scan(ev), [])
        self.assertIsNone(ladder_arb.locked_basket([("A", "yes", .50, 100), ("B", "no", .49, 100)], 1000))

    def test_too_small_to_bother(self):
        # 2 cents profit after fees, but only 1 contract on offer: under MIN_PROFIT.
        self.assertIsNone(ladder_arb.locked_basket([("A", "yes", .30, 1), ("B", "no", .64, 1)], 30))
        self.assertIsNotNone(ladder_arb.locked_basket([("A", "yes", .30, 50), ("B", "no", .64, 50)], 30))

    def test_budget_and_max_per_arb(self):
        for budget, cap in ((100, 30), (5, 30), (100, 7)):
            sigs = self.scan(ladder_event(), budget=budget, max_per_arb=cap)
            self.assertEqual(len(sigs), 1)
            self.assertLessEqual(sigs[0].cost, min(budget, cap))
        self.assertEqual(self.scan(ladder_event(), budget=0.5), [])

    def test_size_capped_by_book_depth(self):
        ev = ladder_event()
        c = fake_for(ev)
        set_book(c, "LAD-50", .48, .50, ask_size=7)
        sig = self.scan(ev, client=c)[0]
        self.assertEqual(sig.legs[0].count, 7)

    def test_thinnest_leg_goes_first(self):
        ev = ladder_event()
        c = fake_for(ev)
        set_book(c, "LAD-50", .48, .50, ask_size=12)     # the YES leg is thinner
        self.assertEqual(self.scan(ev, client=c)[0].legs[0].side, "yes")
        c = fake_for(ev)
        set_book(c, "LAD-60", .62, .64, bid_size=12)     # now the NO leg is thinner
        self.assertEqual(self.scan(ev, client=c)[0].legs[0].side, "no")

    def test_signals_never_share_a_ticker_and_fit_the_budget(self):
        # 60->50 and 70->50 are both inverted and share LAD-50.
        ev = ladder_event(quotes=((.30, .32), (.60, .62), (.55, .57)))
        sigs = self.scan(ev)
        self.assertEqual(len(sigs), 1)
        self.assertEqual({leg.ticker for leg in sigs[0].legs}, {"LAD-50", "LAD-60"})
        # Two separate inversions: 60->50 and 80->70. Both fit, within one budget.
        ev = ladder_event(strikes=(50, 60, 70, 80), quotes=((.30, .32), (.60, .62), (.25, .27), (.50, .52)))
        sigs = self.scan(ev, budget=40)
        tickers = [leg.ticker for s in sigs for leg in s.legs]
        self.assertEqual(len(sigs), 2)
        self.assertEqual(len(tickers), len(set(tickers)))
        self.assertLessEqual(sum(s.cost for s in sigs), 40)
        for s in sigs:
            self.assertLockedIn(s, ev["markets"])

    def test_recheck(self):
        ev = ladder_event()
        c = fake_for(ev)
        strat = ladder_arb.STRATEGY
        sig = strat.scan(ctx(c))[0]
        again = strat.recheck(ctx(c), sig)
        self.assertEqual([leg.__dict__ for leg in again.legs], [leg.__dict__ for leg in sig.legs])
        small = strat.recheck(ctx(c, budget=4), sig)
        self.assertLessEqual(small.cost, 4)
        self.assertLockedIn(small, ev["markets"])
        self.assertIsNone(strat.recheck(ctx(c, held={"LAD-50"}), sig))  # bought earlier this run
        set_book(c, "LAD-60", .45, .47)  # someone pulled the high bid: no inversion now
        self.assertIsNone(strat.recheck(ctx(c), sig))

    def test_random_ladders_never_lose(self):
        rng = random.Random(7)
        found = 0
        for _ in range(400):
            kind = rng.choice(UP + DOWN)
            strikes = sorted(rng.sample(range(0, 200, 5), rng.randint(2, 6)))
            quotes = []
            for _ in strikes:
                bid = round(rng.uniform(.01, .97), 2)
                quotes.append((bid, round(min(.99, bid + rng.choice((.01, .02, .05))), 2)))
            ev = ladder_event(kind, strikes, quotes)
            c = FakeKalshi()
            c.event_list = [ev]
            for m in ev["markets"]:
                set_book(c, m["ticker"], price(m, "yes_bid"), price(m, "yes_ask"),
                         rng.randint(1, 80), rng.randint(1, 80))
            budget, cap = rng.uniform(1, 100), rng.uniform(1, 40)
            sigs = ladder_arb.STRATEGY.scan(ctx(c, budget=budget, max_per_arb=cap))
            tickers = [leg.ticker for s in sigs for leg in s.legs]
            self.assertEqual(len(tickers), len(set(tickers)))
            self.assertLessEqual(sum(s.cost for s in sigs), budget + 1e-9)
            for s in sigs:
                found += 1
                self.assertEqual(sorted(leg.side for leg in s.legs), ["no", "yes"])
                self.assertLessEqual(s.cost, cap + 1e-9)
                self.assertGreaterEqual(s.payout_min - s.cost, ladder_arb.MIN_PROFIT)
                for leg in s.legs:
                    ask, size = ladder_arb.quote(c.books[leg.ticker], leg.side)
                    self.assertEqual(leg.price, ask)
                    self.assertLessEqual(leg.count, size)
                self.assertLockedIn(s, ev["markets"])
        self.assertGreater(found, 20)  # the test only means something if it found arbs


class QuoteTests(unittest.TestCase):
    def test_ask_comes_from_the_other_sides_best_bid(self):
        book = {"yes": [(.40, 3)], "no": [(.30, 5), (.55, 7)]}  # not sorted on purpose
        self.assertEqual(ladder_arb.quote(book, "yes"), (0.45, 7))
        self.assertEqual(ladder_arb.quote(book, "no"), (0.60, 3))

    def test_empty_or_silly_books_give_nothing(self):
        for book in ({"yes": [], "no": []}, {}, {"no": [(0, 9)]}, {"no": [(1.0, 9)]}, {"no": [(.5, 0.4)]}):
            self.assertEqual(ladder_arb.quote(book, "yes"), (None, 0), book)

    def test_cost_matches_the_signal(self):
        b = ladder_arb.locked_basket([("A", "yes", .41, 33), ("B", "no", .37, 50)], 30)
        legs = [Leg("A", "yes", .41, b["count"]), Leg("B", "no", .37, b["count"])]
        self.assertAlmostEqual(b["cost"], sum(leg.cost for leg in legs))
        self.assertAlmostEqual(b["cost"], b["count"] * .78 + fee(b["count"], .41) + fee(b["count"], .37))


# ---------- brackets ----------

class BracketTests(OutcomeChecks):
    def scan(self, *events, **kw):
        c = kw.pop("client", None) or fake_for(*events)
        return bracket_arb.STRATEGY.scan(ctx(c, **kw))

    def test_exhaustive_brackets_lock_profit_whoever_wins(self):
        ev = bracket_event()
        sigs = self.scan(ev)
        self.assertEqual(len(sigs), 1)
        sig = sigs[0]
        self.assertEqual({leg.ticker for leg in sig.legs}, {m["ticker"] for m in ev["markets"]})
        self.assertTrue(all(leg.side == "yes" for leg in sig.legs))
        c = sig.legs[0].count
        self.assertEqual(sig.payout_min, c * 1.0)
        for winner in sig.legs:  # each bracket as the only winner
            self.assertGreater(winner.count - sig.cost, 0)
        self.assertLockedIn(sig, ev["markets"])
        self.assertLessEqual(sig.cost, 30)

    def test_no_arb_when_asks_add_to_a_dollar(self):
        self.assertEqual(self.scan(bracket_event(asks=(.25, .25, .25, .25))), [])
        self.assertEqual(self.scan(bracket_event(asks=(.30, .30, .25, .25))), [])

    def test_gap_rejected(self):
        ev = bracket_event()
        ev["markets"][2]["floor_strike"] = 51  # 50 to 51 is covered by nothing
        self.assertIsNone(bracket_arb.brackets(ev))
        self.assertEqual(self.scan(ev), [])
        ev = bracket_event()
        ev["markets"][-1]["floor_strike"] = 61
        self.assertEqual(self.scan(ev), [])

    def test_overlap_rejected(self):
        ev = bracket_event()
        ev["markets"][2]["floor_strike"] = 45
        self.assertEqual(self.scan(ev), [])

    def test_missing_or_extra_ends_rejected(self):
        ev = bracket_event()
        ev["markets"] = ev["markets"][1:]  # no bottom
        self.assertEqual(self.scan(ev), [])
        ev = bracket_event()
        ev["markets"] = ev["markets"][:-1]  # no top
        self.assertEqual(self.scan(ev), [])
        ev = bracket_event(asks=(.15, .2, .2, .15, .1))
        ev["markets"].append(market("BR-T2", "BR", "greater", floor=60, ask=.1))
        self.assertEqual(self.scan(ev), [])
        ev = bracket_event()
        ev["markets"][1]["strike_type"] = "custom"
        self.assertEqual(self.scan(ev), [])
        ev = bracket_event()
        del ev["markets"][1]["cap_strike"]
        self.assertEqual(self.scan(ev), [])

    def test_edge_types(self):
        two = lambda low, high: bracket_event(edges=(50,), asks=(.45, .45), low=low, high=high)
        self.assertIsNone(bracket_arb.brackets(two("less", "greater")))  # exactly 50 wins neither
        self.assertIsNotNone(bracket_arb.brackets(two("less_or_equal", "greater")))
        self.assertIsNotNone(bracket_arb.brackets(two("less", "greater_or_equal")))
        ev = two("less_or_equal", "greater")
        self.assertLockedIn(self.scan(ev)[0], ev["markets"])

    def test_edges_are_why_it_stays_on_paper(self):
        # If 'between' left out its floor, a value exactly on the edge would win nothing.
        # We can't tell from the API, so the strategy is paper only until checked.
        ev = bracket_event(low="less")
        x = 40
        winners = [m for m in ev["markets"] if m["strike_type"] == "less" and x < m["cap_strike"]
                   or m["strike_type"] == "between" and m["floor_strike"] < x <= m["cap_strike"]
                   or m["strike_type"] == "greater" and x > m["floor_strike"]]
        self.assertEqual(winners, [])

    def test_not_exclusive_or_mixed_close_rejected(self):
        self.assertEqual(self.scan(bracket_event(exclusive=False)), [])
        ev = bracket_event()
        ev["markets"][0]["close_time"] = "2098-01-01T00:00:00Z"
        self.assertEqual(self.scan(ev), [])

    def test_every_range_must_be_buyable(self):
        ev = bracket_event()
        ev["markets"][1]["status"] = "closed"
        self.assertEqual(self.scan(ev), [])
        self.assertEqual(self.scan(bracket_event(), held={"BR-T"}), [])
        ev = bracket_event()
        c = fake_for(ev)
        c.books["BR-40"] = {"yes": [(.2, 5)], "no": []}  # no one selling YES
        self.assertEqual(self.scan(ev, client=c), [])

    def test_fees_kill_a_tiny_arb(self):
        ev = bracket_event(asks=(.24, .25, .25, .25))  # 1 cent under $1 before fees
        self.assertEqual(self.scan(ev), [])

    def test_paper_only_until_verified(self):
        strat = bracket_arb.STRATEGY
        c = fake_for(bracket_event())
        ok, why = strat.proven(ctx(c))
        self.assertFalse(ok)
        self.assertEqual(why, "exhaustive brackets not yet verified on live data")
        self.assertFalse(strat.proven(ctx(c, params={"verified": "yes"}))[0])  # must be True itself
        self.assertTrue(strat.proven(ctx(c, params={"verified": True}))[0])
        self.assertTrue(strat.scan(ctx(c))[0].meta["paper_only"])
        self.assertFalse(strat.scan(ctx(c, params={"verified": True}))[0].meta["paper_only"])

    def test_recheck(self):
        ev = bracket_event()
        c = fake_for(ev)
        strat = bracket_arb.STRATEGY
        sig = strat.scan(ctx(c))[0]
        self.assertIsNotNone(strat.recheck(ctx(c), sig))
        set_book(c, "BR-50", .30, .32)  # one range got dearer: total now over $1
        self.assertIsNone(strat.recheck(ctx(c), sig))

    def test_random_brackets_never_lose(self):
        rng = random.Random(11)
        found = 0
        for _ in range(300):
            edges = sorted(rng.sample(range(0, 100), rng.randint(1, 6)))
            n = len(edges) + 1
            weights = [rng.uniform(.2, 1) for _ in range(n)]
            total = rng.uniform(.75, 1.05)
            asks = [round(max(.01, min(.98, w / sum(weights) * total)), 2) for w in weights]
            low, high = rng.choice(DOWN), rng.choice(UP)
            ev = bracket_event(tuple(edges), tuple(asks), low=low, high=high)
            c = FakeKalshi()
            c.event_list = [ev]
            for m in ev["markets"]:
                set_book(c, m["ticker"], 0, price(m, "yes_ask"), ask_size=rng.randint(1, 120))
            budget = rng.uniform(1, 60)
            for s in bracket_arb.STRATEGY.scan(ctx(c, budget=budget)):
                found += 1
                self.assertLessEqual(s.cost, min(budget, 30) + 1e-9)
                self.assertEqual(len(s.legs), n)
                self.assertLockedIn(s, ev["markets"])
        self.assertGreater(found, 20)


# ---------- through the engine ----------

class MovingFake(FakeKalshi):
    """Order books change after the first `after` reads, like a market moving between
    the scan and the buy."""

    def __init__(self, later, after, **kw):
        super().__init__(**kw)
        self.later, self.after, self.reads = later, after, 0

    def orderbook(self, ticker):
        self.reads += 1
        if self.reads > self.after:
            return self.later.get(ticker, {"yes": [], "no": []})
        return super().orderbook(ticker)


class EngineTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        orders_proven()
        # Only our two strategies, so another module being edited can't break these tests.
        mine = {"ladder_arb": ladder_arb.STRATEGY, "bracket_arb": bracket_arb.STRATEGY}
        patcher = mock.patch.object(engine, "load_all", lambda: mine)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_registered_under_the_right_names(self):
        self.assertIn("ladder_arb", MODULES)
        self.assertIn("bracket_arb", MODULES)
        self.assertEqual(ladder_arb.STRATEGY.name, "ladder_arb")
        self.assertEqual(bracket_arb.STRATEGY.name, "bracket_arb")
        self.assertEqual({ladder_arb.STRATEGY.kind, bracket_arb.STRATEGY.kind}, {"arbitrage"})

    def test_live_ladder_buys_both_legs_thinnest_first(self):
        config.update({"strategies": {"ladder_arb": {"mode": "live"}}})
        ev = ladder_event()
        c = fake_for(ev)
        set_book(c, "LAD-60", .62, .64, bid_size=15)  # NO on LAD-60 is the thin leg
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual(s["live_trades"], 1)
        self.assertEqual([(o[0], o[1]) for o in c.orders], [("LAD-60", "no"), ("LAD-50", "yes")])
        self.assertEqual(c.orders[0][2], c.orders[1][2])
        row = db.query("SELECT * FROM signals")[0]
        self.assertEqual((row["status"], row["guaranteed"]), ("executed", 1))

    def test_live_ladder_skips_when_prices_move_before_buying(self):
        config.update({"strategies": {"ladder_arb": {"mode": "live"}}})
        ev = ladder_event()
        normal = fake_for(ladder_event(quotes=((.70, .72), (.50, .52), (.30, .32)))).books
        c = MovingFake(normal, after=2)  # the scan reads 2 books, recheck sees the new ones
        c.event_list = [ev]
        for m in ev["markets"]:
            set_book(c, m["ticker"], price(m, "yes_bid"), price(m, "yes_ask"))
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual(s["signals"], 1)
        self.assertEqual(c.orders, [])

    def test_brackets_stay_on_paper_even_in_live_mode(self):
        config.update({"strategies": {"bracket_arb": {"mode": "live"}}})
        c = fake_for(bracket_event())
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual(c.orders, [])
        self.assertEqual(s["paper_trades"], 1)
        self.assertTrue(any("not yet verified" in n for n in s["notes"]))
        self.assertEqual(len(paper.open_rows()), 4)
        self.assertEqual(db.query("SELECT mode FROM signals")[0]["mode"], "paper")

    def test_verified_brackets_can_go_live(self):
        config.update({"strategies": {"bracket_arb": {"mode": "live", "verified": True}}})
        c = fake_for(bracket_event())
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual(len(c.orders), 4)
        self.assertTrue(all(o[1] == "yes" for o in c.orders))


if __name__ == "__main__":
    unittest.main()


class RealDataRegressionTests(unittest.TestCase):
    """Shapes seen on the real Kalshi API (market-check, Oct 2026)."""

    def _ev(self, event, rungs, close=FUTURE):
        return {"event_ticker": event, "mutually_exclusive": False,
                "markets": [{"ticker": f"{event}-{suffix}", "event_ticker": event, "strike_type": "greater",
                             "floor_strike": strike, "yes_sub_title": words, "close_time": close,
                             "status": "active"} for suffix, strike, words in rungs]}

    def test_two_teams_spreads_are_not_a_ladder(self):
        # "Cal Poly by 4+" and "Utah Tech by 13+" are different questions. Treating them as one
        # ladder looked like $10.70 locked in to pay $500; really a near-certain loss.
        ev = self._ev("KXNCAAFSPREAD-26OCT24UTUCP", [("CP4", 4.5, "Cal Poly wins by over 4.5 points"),
                                                     ("UTU13", 13.5, "Utah Tech wins by over 13.5 points")])
        self.assertIsNone(ladder_arb.ladder(ev))

    def test_two_players_props_are_not_a_ladder(self):
        ev = self._ev("KXNFLRSHATT-26OCT25PITNO", [("NOTSHOUGH6-7", 6.5, "Tyler Shough: 7+"),
                                                   ("PITJWARREN30-11", 10.5, "Jaylen Warren: 11+")])
        self.assertIsNone(ladder_arb.ladder(ev))

    def test_same_wording_but_different_ticker_letters_is_not_a_ladder(self):
        ev = self._ev("KXNBATEAMTOTAL-26OCT24MIACHA", [("MIA113", 112.5, "Over 112.5 points scored"),
                                                       ("CHA114", 113.5, "Over 113.5 points scored")])
        self.assertIsNone(ladder_arb.ladder(ev))

    def test_a_real_price_ladder_still_counts(self):
        ev = self._ev("KXBTCD-26OCT2517", [("T110000", 110000, "$110,000 or above"),
                                           ("T112000", 112000, "$112,000 or above"),
                                           ("T114000", 114000, "$114,000 or above")])
        kind, rungs = ladder_arb.ladder(ev)
        self.assertEqual([s for s, _ in rungs], [110000, 112000, 114000])

    def test_different_custom_strikes_are_not_one_set(self):
        ev = self._ev("KXEV", [("T1", 1, "1+"), ("T2", 2, "2+")])
        ev["markets"][0]["custom_strike"] = {"Player": "A"}
        ev["markets"][1]["custom_strike"] = {"Player": "B"}
        self.assertIsNone(ladder_arb.ladder(ev))

    def test_too_good_to_be_true_arbitrage_is_refused(self):
        from kal import config, risk
        from kal.strategies.base import Leg, Signal
        cfg = dict(config.DEFAULTS, max_per_arb=30, max_per_trade=30)
        sig = Signal("ladder_arb", "E", "t", [Leg("A", "yes", 0.01, 500), Leg("B", "no", 0.01, 500)],
                     payout_min=500.0)
        self.assertIn("too good to be true", risk.Limits(cfg, 100).refuse(sig))
        fair = Signal("ladder_arb", "E", "t", [Leg("A", "yes", 0.48, 10), Leg("B", "no", 0.49, 10)],
                      payout_min=10.0)
        self.assertIsNone(risk.Limits(cfg, 100).refuse(fair))


class BinaryOnlyTests(unittest.TestCase):
    def test_non_binary_markets_are_never_a_ladder(self):
        ev = RealDataRegressionTests()._ev("KXBTCD-26OCT2517", [("T110000", 110000, "$110,000 or above"),
                                                                ("T112000", 112000, "$112,000 or above")])
        self.assertIsNotNone(ladder_arb.ladder(ev))
        ev["markets"][0]["market_type"] = "scalar"
        self.assertIsNone(ladder_arb.ladder(ev))
