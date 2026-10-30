"""Orders put back without their signal (recover() after a lost disk or a rollback,
catch_up() after a restore) count toward their event's max_per_event. open_by_event() filed
them under the bare market ticker, so the cap saw $0 in their event: a live bet strategy
staked $37.84 in one event against a $20 cap (cross-PR money-path audit). Each now gets a
stand-in signal with its event from Kalshi's market data. The ticker alone can't say: on
Oct 30, 2026, 1,405 of 1,791 open markets (combos aside) had extra hyphens, like these
player props."""
import time

import requests

from kal import client, config, db, engine, ledger, risk
from kal.client import KalshiError, new_order_id
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

    def test_an_outage_during_the_lookup_writes_nothing(self):
        # markets_by_ticker() swallows errors: an outage filed every order under its ticker
        # for good, where the cap can't see it (independent review of this change).
        self.c.order_list = [order(new_order_id(), ticker=t, n=15, p=0.30, ago=600) for t in PROPS]
        healthy = self.c.market
        for trouble in (KalshiError("GET /markets/x -> 503", 503), KalshiError("429", 429),
                        requests.ConnectionError("reset")):
            with self.subTest(trouble=trouble):
                def down(ticker, archive=False, e=trouble):
                    raise e
                self.c.market = down
                with self.assertRaises(type(trouble)):
                    ledger.recover(self.c)
                self.assertEqual(db.query("SELECT COUNT(*) AS n FROM orders")[0]["n"], 0)
                self.assertIsNone(db.get("orders_checked"))
        self.c.market = healthy   # the next run, Kalshi back
        self.assertEqual(ledger.recover(self.c), 4)
        self.assertEqual(self.refused(bet(f"{EVENT}-PHXCGILLESPIE12-20")), "over max_per_event")

    def test_an_event_from_the_historical_store(self):
        t = PROPS[0]
        self.c.archived_markets = {t: self.c.market_info.pop(t)}   # settled long ago, moved
        self.c.order_list = [order(new_order_id(), ticker=t, n=15, p=0.30, ago=600)]
        ledger.recover(self.c)
        self.assertIn(EVENT, ledger.open_by_event())

    def test_a_market_kalshi_has_no_record_of_keeps_its_ticker(self):
        def missing(ticker, archive=False):
            raise KalshiError(f"GET /markets/{ticker} -> 404", 404)
        self.c.market = missing
        self.c.order_list = [order(new_order_id(), ticker=PROPS[0], n=15, p=0.30, ago=600)]
        self.assertEqual(ledger.recover(self.c), 1)
        self.assertIn(PROPS[0], ledger.open_by_event())

    def test_a_never_placed_row_keeps_its_own_signal(self):
        mine, new = new_order_id(), new_order_id()
        sid = db.insert("signals", strategy="favorites", event=EVENT, title="t", mode="live", guaranteed=0,
                        cost=4.73, payout_min=0, exp_profit=0, legs=[], status="missed (no fill)")
        db.insert("orders", signal_id=sid, strategy="favorites", ticker=PROPS[0], side="yes", count=15, price=0.30,
                  filled=0, status="never_placed", client_order_id=mine, ts=time.time() - 1800)
        db.put("orders_checked", time.time() - 600)
        self.c.order_list = [order(mine, ticker=PROPS[0], n=15, p=0.30, ago=1800),
                             order(new, ticker=PROPS[1], n=15, p=0.30, ago=1200)]
        self.assertEqual(ledger.recover(self.c), 2)
        rows = {r["ticker"]: r for r in db.query("SELECT ticker, signal_id, strategy FROM orders")}
        self.assertEqual(rows[PROPS[0]]["signal_id"], sid)                       # corrected in place
        self.assertNotEqual(rows[PROPS[1]]["signal_id"], sid)                    # a stand-in of its own
        self.assertEqual(db.query("SELECT event FROM signals WHERE id = ?", rows[PROPS[1]]["signal_id"])[0]["event"],
                         EVENT)

    def test_an_order_it_cant_read_writes_none_of_them(self):
        bad = order(new_order_id(), ticker=PROPS[1], n=15, p=0.30, ago=600)
        bad["fill_count_fp"] = "lots"
        self.c.order_list = [order(new_order_id(), ticker=PROPS[0], n=15, p=0.30, ago=1200), bad]
        with self.assertRaises(ledger.CatchUpError):
            ledger.recover(self.c)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM orders")[0]["n"], 0)   # all or nothing
