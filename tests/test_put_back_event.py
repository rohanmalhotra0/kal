"""Orders put back without their signal (recover() after a lost disk or a rollback,
catch_up() after a restore) count toward their event's max_per_event. open_by_event() filed
them under the bare market ticker, so the cap saw $0 in their event: a live bet strategy
staked $37.84 in one event against a $20 cap (cross-PR money-path audit). Each now gets a
stand-in signal with its event from Kalshi's market data. The ticker alone can't say: on
Oct 30, 2026, 1,405 of 1,791 open markets (combos aside) had extra hyphens, like these
player props."""
import time

from kal import client, config, db, engine, ledger, risk
from kal.client import new_order_id
from kal.strategies.base import Leg, Signal
from tests.fakes import FakeKalshi
from tests.helpers import TempHome
from tests.test_direction_fields import new_fill
from tests.test_order_memory import order

EVENT = "KXNBAPTSPROT-26OCT30DENPHX"
PROPS = [f"{EVENT}-DENDDEROZAN10-{n}" for n in (10, 15, 20, 25)]   # $4.73 each below


def bet(ticker, event=EVENT):
    return Signal("favorites", event, "t", [Leg(ticker, "yes", 0.30, 15)])


class PutBackEventTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"budget": 300})
        self.c = FakeKalshi(balance=1000)
        self.c.cutoffs = {"orders_updated_ts": time.time() - 15 * 86400,
                          "trades_created_ts": time.time() - 60 * 86400}
        for t in PROPS + [f"{EVENT}-PHXCGILLESPIE12-20"]:
            self.c.market_info[t] = {"ticker": t, "event_ticker": EVENT, "status": "active"}

    def refused(self, sig):
        return risk.Limits(config.load(), 300, ledger.open_by_event(), ledger.open_directional(), 300).refuse(sig)

    def test_recover_files_put_back_orders_under_their_event(self):
        self.c.order_list = [order(new_order_id(), ticker=t, n=15, p=0.30, ago=600) for t in PROPS]
        self.assertEqual(ledger.recover(self.c), 4)
        self.assertAlmostEqual(ledger.open_by_event()[EVENT], 4 * (4.5 + client.fee(15, 0.30)))
        self.assertEqual(self.refused(bet(f"{EVENT}-PHXCGILLESPIE12-20")), "over max_per_event")   # was: bought
        self.assertIsNone(self.refused(bet("KXOTHER-26OCT30-A", "KXOTHER-26OCT30")))
        self.assertEqual(engine.stranded_baskets(), [])   # a stand-in is never a half-built basket

    def test_catch_up_files_them_under_their_event_too(self):
        db.put("restored", {"day": "x", "since": time.time() - 86400, "at": time.time()})
        self.c.fill_list = [new_fill(f"k-{i}", "yes", 15, 0.30, ticker=t) for i, t in enumerate(PROPS)]
        self.assertEqual(ledger.catch_up(self.c), 4)
        self.assertEqual(self.refused(bet(f"{EVENT}-PHXCGILLESPIE12-20")), "over max_per_event")

    def test_a_market_kalshi_doesnt_return_keeps_its_ticker(self):
        self.c.market_info.clear()
        self.c.order_list = [order(new_order_id(), ticker=PROPS[0], n=15, p=0.30, ago=600)]
        self.assertEqual(ledger.recover(self.c), 1)
        self.assertIn(PROPS[0], ledger.open_by_event())   # as before: never a crash or a guess
