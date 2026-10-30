"""Kalshi says which way a fill, order or trade went in outcome_side / book_side (and
taker_outcome_side / taker_book_side on public trades). It deprecated side, action and
taker_side, which it may drop any day since May 2026. The bot reads the new fields, so it
keeps working when the old ones go."""
import time
import unittest

from kal import client, config, db, engine, flow, ledger
from kal.client import outcome_side
from kal.ledger import CatchUpError, _parse_fill
from tests.fakes import FakeKalshi
from tests.helpers import TempHome

NOW = 1_800_000_000


def new_fill(oid, outcome, n=20, p=0.9, ticker="S-1"):
    """A fill as Kalshi sends it once the deprecated side and action are gone. p is the
    price of the outcome side."""
    yes = p if outcome == "yes" else 1 - p
    return {"fill_id": f"f-{oid}", "trade_id": f"t-{oid}", "order_id": oid, "ticker": ticker,
            "market_ticker": ticker, "outcome_side": outcome, "book_side": "bid" if outcome == "yes" else "ask",
            "count_fp": f"{n:.2f}", "yes_price_dollars": f"{yes:.4f}", "no_price_dollars": f"{1 - yes:.4f}",
            "is_taker": True, "fee_cost": "0.0100", "created_time": "2026-10-25T10:00:00Z"}


class OutcomeSideTests(unittest.TestCase):
    def test_either_field_says_the_side(self):
        self.assertEqual(outcome_side({"outcome_side": "no"}), "no")
        self.assertEqual(outcome_side({"book_side": "bid"}), "yes")
        self.assertEqual(outcome_side({"book_side": "ask"}), "no")
        self.assertEqual(outcome_side({"taker_outcome_side": "yes"}, "taker_"), "yes")
        self.assertIsNone(outcome_side({"side": "yes"}))          # the old field alone isn't this one
        self.assertIsNone(outcome_side({"outcome_side": "maybe"}))

    def test_a_fill_with_only_the_new_fields(self):
        self.assertEqual(_parse_fill(new_fill("o1", "no", 20, 0.9))[:6], ("o1", "S-1", "no", "buy", 20, 0.9))
        # Selling YES at 10c leaves the same NO position as buying NO at 90c.
        self.assertEqual(_parse_fill(new_fill("o2", "yes", 5, 0.1))[2:6], ("yes", "buy", 5, 0.1))

    def test_the_old_fields_still_say_exactly_what_was_done(self):
        f = new_fill("o3", "yes", 20, 0.1)
        f.update(side="no", action="sell")      # selling NO is being long YES: they agree
        self.assertEqual(_parse_fill(f)[2:4], ("no", "sell"))

    def test_old_and_new_fields_that_disagree_are_never_guessed(self):
        f = new_fill("o4", "yes")
        f.update(side="no", action="buy")       # buying NO is long NO, not YES
        with self.assertRaises(CatchUpError):
            _parse_fill(f)


def all_off():
    return {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}


class MoneyPathTests(TempHome):
    """The checks that read Kalshi's fills, with fills in the new shape only."""

    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300, "strategies": all_off()})
        db.insert("orders", ticker="S-1", side="no", count=20, price=0.9, filled=20,
                  status="executed", order_id="o1", client_order_id="c1")
        self.cost = 18 + client.fee(20, 0.9)
        self.c = FakeKalshi(balance=1000)
        self.c.fill_list = [new_fill("o1", "no")]   # the bot's own buy
        self.c.market_info["S-1"] = {"ticker": "S-1", "status": "active", "no_bid_dollars": "0.9000"}
        self.c.position_list = [{"ticker": "S-1", "position": -20, "exposure": 18}]

    def settle_no(self):
        self.c.position_list = []
        self.c.market_info["S-1"] = {"ticker": "S-1", "status": "finalized", "result": "no",
                                     "settlement_value_dollars": "0.0000"}
        return engine.run_once(self.c)

    def test_a_payout_settles_without_the_old_fields(self):
        # With only side/action read, every fill looked "different than expected": the
        # bot stopped at its first settlement.
        self.settle_no()
        self.assertEqual(config.load()["disabled_reason"], "")
        self.assertAlmostEqual(ledger.realized(), 20 - self.cost)

    def test_a_sale_outside_the_bot_is_still_seen(self):
        self.c.fill_list.append(new_fill("owner-1", "yes", 20, 0.1))   # sold the 20 NO
        self.settle_no()
        self.assertEqual(config.load()["disabled_reason"], "")
        self.assertAlmostEqual(ledger.realized(), -self.cost)          # never the $20 payout

    def test_a_restore_still_brings_back_every_buy(self):
        db.put("restored", {"day": "x", "since": time.time() - 86400, "at": time.time()})
        self.c.fill_list.append(new_fill("b2", "no", 10, 0.8, ticker="N-1"))
        self.c.position_list.append({"ticker": "N-1", "position": -10, "exposure": 8})
        self.c.market_info["N-1"] = {"ticker": "N-1", "status": "active", "no_bid_dollars": "0.8000"}
        s = engine.run_once(self.c)
        self.assertTrue(any("added 1 order" in n for n in s["notes"]), s)
        self.assertEqual(config.load()["disabled_reason"], "")


class RestoreGapTests(TempHome):
    """After a restore, catch_up adds the gap's buys. A fill on the other side of contracts
    the books hold nets them away (the owner selling them, or buying the other side): it's
    written off, never added as the bot's buy, or the payout would be booked (independent
    review of this change)."""

    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300, "strategies": all_off()})
        db.insert("orders", ticker="T-1", side="yes", count=20, price=0.4, filled=20,
                  status="executed", order_id="o1", client_order_id="c1", ts=time.time() - 3 * 86400)
        self.cost = 8 + client.fee(20, 0.4)   # 8.34
        db.put("restored", {"day": "x", "since": time.time() - 86400, "at": time.time()})
        self.c = FakeKalshi(balance=1000)
        self.c.position_list = []

    def fill(self, oid, outcome, n, p, ticker="T-1", ago=3600, old=None):
        f = new_fill(oid, outcome, n, p, ticker=ticker)
        f["created_time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))
        if old:
            f.update(side=old[0], action=old[1])
        return f

    def settle(self, ticker="T-1", result="yes"):
        self.c.market_info[ticker] = {"ticker": ticker, "status": "finalized", "result": result,
                                      "settlement_value_dollars": "1.0000" if result == "yes" else "0.0000"}
        return engine.run_once(self.c)

    def test_the_owners_sale_in_the_gap_is_written_off(self):
        self.c.fill_list = [self.fill("owner-1", "no", 20, 0.4)]   # sold the 20 YES at 60c
        self.settle()
        self.assertAlmostEqual(ledger.realized(), -self.cost)      # was +3.32: the payout booked

    def test_a_part_sale_leaves_the_rest_paid(self):
        self.c.fill_list = [self.fill("owner-1", "no", 5, 0.4)]
        self.settle()
        self.assertAlmostEqual(ledger.realized(), 15 - self.cost)

    def test_a_buy_and_its_sale_inside_the_gap(self):
        self.c.fill_list = [self.fill("b1", "yes", 20, 0.4, ticker="T-2", ago=7200),     # the bot's buy
                            self.fill("owner-2", "no", 20, 0.1, ticker="T-2", ago=3600)]  # sold at 90c
        self.c.market_info["T-1"] = {"ticker": "T-1", "status": "active", "yes_bid_dollars": "0.4000"}
        self.c.position_list = [{"ticker": "T-1", "position": 20, "exposure": 8}]
        s = self.settle("T-2")
        self.assertTrue(any("added 1 order" in n for n in s["notes"]), s)
        self.assertAlmostEqual(ledger.realized(), -(8 + client.fee(20, 0.4)))

    def test_an_old_style_buy_of_the_other_side_is_written_off_too(self):
        self.c.fill_list = [self.fill("owner-1", "no", 20, 0.4, old=("no", "buy"))]
        self.settle()
        self.assertAlmostEqual(ledger.realized(), -self.cost)

    def test_a_buy_on_the_same_side_still_counts_as_the_bots(self):
        self.c.fill_list = [self.fill("b2", "yes", 10, 0.5)]
        s = self.settle(result="no")
        self.assertTrue(any("added 1 order" in n for n in s["notes"]), s)
        self.assertAlmostEqual(ledger.realized(), -(self.cost + 5 + client.fee(10, 0.5)))


class DisagreeingFieldsTests(unittest.TestCase):
    def test_outcome_and_book_side_that_disagree_are_never_guessed(self):
        with self.assertRaises(ValueError):
            outcome_side({"outcome_side": "yes", "book_side": "ask"})
        self.assertIsNone(outcome_side({"book_side": ["bid"]}))   # odd shapes read as no answer
        f = new_fill("o9", "yes")
        f["book_side"] = "ask"
        with self.assertRaises(CatchUpError):
            _parse_fill(f)

    def test_a_trades_reply_without_its_list_is_an_error_not_no_trades(self):
        k = client.Kalshi()
        k.get = lambda endpoint, params=None: {"data": []}   # Kalshi renamed the list
        from kal.strategies.base import Leg, Signal
        why = flow.veto(k, Signal("favorites", "M", "t", [Leg("M-1", "no", 0.5, 10)]), now=NOW)
        self.assertIn("couldn't read recent trades", why)

def trade(took, count, ago=60, price=0.5, old=True, new=True):
    t = {"count_fp": f"{count:.2f}", "created_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - ago)),
         "yes_price_dollars": f"{price:.4f}", "no_price_dollars": f"{1 - price:.4f}"}
    if old:
        t["taker_side"] = took
    if new:
        t.update(taker_outcome_side=took, taker_book_side="bid" if took == "yes" else "ask")
    return t


class FlowTests(unittest.TestCase):
    def test_trades_with_only_the_new_fields_still_count(self):
        tape = [trade("yes", 10, old=False)] * 4 + [trade("no", 10, old=False)] * 2
        self.assertAlmostEqual(flow.against(tape, "no", NOW)[0], 40 / 60)
        self.assertEqual(flow.against(tape, "no", NOW), flow.against([trade("yes", 10)] * 4 + [trade("no", 10)] * 2,
                                                                     "no", NOW))

    def test_trades_it_cant_read_skip_the_bet(self):
        # With no field it knows, the guard used to see no trades and let every bet through.
        c = FakeKalshi()
        c.trade_list["M-1"] = [trade("yes", 50, old=False, new=False)] * 8
        from kal.strategies.base import Leg, Signal
        sig = Signal("favorites", "M", "t", [Leg("M-1", "no", 0.5, 10)])
        why = flow.veto(c, sig, now=NOW)
        self.assertIn("couldn't read recent trades", why)
        c.trade_list["M-1"] = []                      # no trades at all is still fine
        self.assertIsNone(flow.veto(c, sig, now=NOW))


if __name__ == "__main__":
    unittest.main()
