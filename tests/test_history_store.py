"""Kalshi moves old records out of its live lists into a historical store: done orders after
about 15 days, fills and settled markets after about 2 months (its /historical/cutoff, Oct
2026). The money path reads both, so an old record is never read as missing."""
import time
import unittest
from datetime import datetime, timezone

from kal import client, config, db, engine, ledger
from kal.client import KalshiError, every_fill, every_order
from tests.fakes import FakeKalshi
from tests.helpers import TempHome

DAY = 86400


def _all_off():
    return {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cut(now):
    return {"orders_updated_ts": now - 15 * DAY, "trades_created_ts": now - 60 * DAY,
            "market_settled_ts": now - 60 * DAY}


class EveryRecordTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.c = FakeKalshi()
        self.c.cutoffs = _cut(self.now)
        self.c.fill_list = [{"trade_id": "t1", "order_id": "o1", "ticker": "A"}]
        self.c.archived_fills = [{"trade_id": "t0", "order_id": "o0", "ticker": "A"},
                                 {"trade_id": "t1", "order_id": "o1", "ticker": "A"}]   # mid-move: in both
        self.c.order_list = [{"order_id": "o1", "ticker": "A"}]
        self.c.archived_orders = [{"order_id": "o0", "ticker": "A"}, {"order_id": "o1", "ticker": "A"}]

    def test_a_window_inside_the_live_lists_never_reads_the_store(self):
        self.assertEqual(every_fill(self.c, min_ts=self.now - DAY), self.c.fill_list)
        self.assertEqual(every_order(self.c, ticker="A", min_ts=self.now - DAY), self.c.order_list)
        self.assertEqual(self.c.archive_reads, 0)

    def test_a_window_past_the_cutoff_reads_the_store_each_record_once(self):
        fills = every_fill(self.c, min_ts=self.now - 90 * DAY)
        self.assertEqual(sorted(f["trade_id"] for f in fills), ["t0", "t1"])
        orders = every_order(self.c, ticker="A", min_ts=self.now - 20 * DAY)
        self.assertEqual(sorted(o["order_id"] for o in orders), ["o0", "o1"])

    def test_each_kind_has_its_own_cutoff(self):
        # 20 days back: past the orders' cutoff (15 days), not the fills' (60 days).
        every_fill(self.c, min_ts=self.now - 20 * DAY)
        self.assertEqual(self.c.archive_reads, 0)
        every_order(self.c, min_ts=self.now - 20 * DAY)
        self.assertEqual(self.c.archive_reads, 1)

    def test_all_history_or_a_cutoff_it_cant_read_reads_the_store(self):
        self.assertEqual(len(every_fill(self.c)), 2)            # min_ts None: from the start
        self.c.cutoffs = KalshiError("GET /historical/cutoff -> 503", 503)
        self.assertEqual(len(every_fill(self.c, min_ts=self.now - DAY)), 2)
        self.c.cutoffs = {}                                     # a field it couldn't read
        self.assertEqual(len(every_order(self.c, min_ts=self.now - DAY)), 2)

    def test_copies_that_share_any_id_are_one_record(self):
        # Counting a fill twice is never safe: the owner's buy-back counted twice hides a sale.
        self.c.fill_list = [{"fill_id": "f1", "trade_id": "t1", "order_id": "o1"}]
        self.c.archived_fills = [{"trade_id": "t1", "order_id": "o1"}, {"fill_id": "f1"}]
        self.assertEqual(len(every_fill(self.c)), 1)
        self.c.order_list = [{"order_id": "o1", "client_order_id": "c1"}]
        self.c.archived_orders = [{"client_order_id": "c1"}, {"order_id": "o1"}]
        self.assertEqual(len(every_order(self.c)), 1)

    def test_two_fills_of_one_trade_both_count(self):
        # A self-trade: one trade, two of the account's orders. Neither is a copy.
        self.c.fill_list = [{"trade_id": "t5", "order_id": "a"}, {"trade_id": "t5", "order_id": "b"}]
        self.c.archived_fills = []
        self.assertEqual(len(every_fill(self.c)), 2)


class ClientStoreTests(unittest.TestCase):
    def test_archive_reads_the_historical_endpoints(self):
        k = client.Kalshi()
        seen = []
        k.paginate = lambda endpoint, key, params=None, limit=None, strict=False: seen.append((endpoint, key)) or []
        k.fills(archive=True)
        k.orders(ticker="A", archive=True)
        k.fills()
        k.orders()
        self.assertEqual(seen, [("/historical/fills", "fills"), ("/historical/orders", "orders"),
                                ("/portfolio/fills", "fills"), ("/portfolio/orders", "orders")])

    def test_cutoff_reads_times_and_drops_what_it_cant(self):
        k = client.Kalshi()
        k.get = lambda endpoint, params=None: {   # the real reply, Oct 30 2026, plus two odd fields
            "market_positions_last_updated_ts": "2026-08-30T00:00:00Z", "market_settled_ts": "2026-08-30T00:00:00Z",
            "orders_updated_ts": "2026-10-15T00:00:00Z", "trades_created_ts": "2026-08-30T00:00:00Z",
            "new_field": None, "junk": "soon"}
        cut = k.cutoff()
        self.assertEqual(cut["orders_updated_ts"], int(datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(cut["trades_created_ts"], int(datetime(2026, 8, 30, tzinfo=timezone.utc).timestamp()))
        self.assertNotIn("new_field", cut)
        self.assertNotIn("junk", cut)

    def test_a_market_settled_before_the_cutoff_is_found_in_the_store(self):
        k = client.Kalshi()
        k.markets = lambda limit=None, **p: []

        def market(t, archive=False):
            if t == "OLD" and not archive:
                raise KalshiError("GET /markets/OLD -> 404", 404)
            return {"ticker": t, "status": "finalized", "result": "no", "settlement_value_dollars": "0.0000"}
        k.market = market
        self.assertEqual(set(k.markets_by_ticker(["OLD", "NEW"])), {"OLD", "NEW"})


class LongOutageTests(TempHome):
    """The server is down for weeks with the bot's books waiting on Kalshi."""

    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300, "strategies": _all_off()})
        self.now = time.time()
        self.c = FakeKalshi(balance=1000)
        self.c.cutoffs = _cut(self.now)

    def test_an_order_moved_to_the_store_is_booked_not_called_never_placed(self):
        # A lost reply, then 20 days down: Kalshi has moved the filled order to its store, and
        # its market settled NO meanwhile, so no position shows it either. Reading only the
        # live list, the bot booked it "never placed": $2.51 lost, left out of the $300 count.
        db.insert("orders", strategy="favorites", ticker="OLD-1", side="yes", count=5, price=0.5,
                  filled=0, status="unknown", client_order_id="lost-1", ts=self.now - 20 * DAY)
        self.c.archived_orders = [{"client_order_id": "lost-1", "order_id": "o9", "status": "executed",
                                   "fill_count_fp": "5.00", "ticker": "OLD-1"}]
        self.c.position_list = []
        self.c.market_info["OLD-1"] = {"ticker": "OLD-1", "status": "finalized", "result": "no",
                                       "settlement_value_dollars": "0.0000"}
        s = engine.run_once(self.c)
        row = db.query("SELECT status, filled, order_id FROM orders")[0]
        self.assertEqual((row["status"], row["filled"], row["order_id"]), ("reconciled:executed", 5, "o9"), s)
        self.assertAlmostEqual(ledger.realized(), -(2.5 + client.fee(5, 0.5)))

    def test_one_still_held_is_booked_and_matches_kalshi(self):
        db.insert("orders", strategy="favorites", ticker="OLD-2", side="yes", count=5, price=0.5,
                  filled=0, status="unknown", client_order_id="lost-2", ts=self.now - 20 * DAY)
        self.c.archived_orders = [{"client_order_id": "lost-2", "order_id": "o8", "status": "executed",
                                   "fill_count_fp": "5.00", "ticker": "OLD-2"}]
        self.c.position_list = [{"ticker": "OLD-2", "position": 5, "exposure": 2.5}]
        self.c.market_info["OLD-2"] = {"ticker": "OLD-2", "status": "active", "yes_bid_dollars": "0.5000"}
        engine.run_once(self.c)
        self.assertAlmostEqual(ledger.open_cost(), 2.5 + client.fee(5, 0.5))
        self.assertEqual(config.load()["disabled_reason"], "")   # books and Kalshi agree

    def test_a_backup_older_than_the_fills_cutoff_still_gets_every_buy(self):
        # Books put back from an old copy (here 70 days): the buys since then that Kalshi has
        # moved to its store must come back too, or the $300 count starts short.
        # This one settled YES since, so no position shows it: only the fill does.
        db.put("restored", {"day": "old", "since": self.now - 70 * DAY, "at": self.now})
        self.c.archived_fills = [{"order_id": "b1", "ticker": "N", "side": "no", "action": "buy",
                                  "count_fp": "10.00", "no_price_dollars": "0.9000",
                                  "created_time": _iso(self.now - 65 * DAY)}]
        self.c.position_list = []
        self.c.market_info["N"] = {"ticker": "N", "status": "finalized", "result": "yes",
                                   "settlement_value_dollars": "1.0000"}
        s = engine.run_once(self.c)
        self.assertTrue(any("added 1 order" in n for n in s["notes"]), s)
        self.assertAlmostEqual(ledger.realized(), -(9 + client.fee(10, 0.9)))

    def test_a_sale_kalshi_moved_to_its_store_is_still_written_off(self):
        # The owner sold the bot's 20 NO two months before the market settled. That fill is
        # in the store now. Reading only the live list, the bot booked a $20 payout it never got.
        db.insert("orders", ticker="S-1", side="no", count=20, price=0.9, filled=20,
                  status="executed", order_id="o1", client_order_id="c1")
        cost = 18 + client.fee(20, 0.9)
        sold = {"trade_id": "t2", "order_id": "owner-1", "ticker": "S-1", "side": "no", "action": "sell",
                "count_fp": "20.00", "no_price_dollars": "0.9000", "created_time": _iso(self.now - 70 * DAY)}
        self.c.archived_fills = [sold]
        self.c.position_list = []
        self.c.market_info["S-1"] = {"ticker": "S-1", "status": "finalized", "result": "no",
                                     "settlement_value_dollars": "0.0000", "open_time": _iso(self.now - 90 * DAY)}
        engine.run_once(self.c)
        self.assertAlmostEqual(ledger.realized(), -cost)

    def test_a_market_that_opened_after_the_cutoff_reads_only_the_live_fills(self):
        db.put("orders_checked", self.now)   # lost orders checked just now: only settling could read the store
        db.insert("orders", ticker="S-2", side="no", count=10, price=0.9, filled=10,
                  status="executed", order_id="o2", client_order_id="c2")
        self.c.market_info["S-2"] = {"ticker": "S-2", "status": "finalized", "result": "no",
                                     "settlement_value_dollars": "0.0000", "open_time": _iso(self.now - 10 * DAY)}
        self.c.position_list = []
        engine.run_once(self.c)
        self.assertAlmostEqual(ledger.realized(), 10 - 9 - client.fee(10, 0.9))
        self.assertEqual(self.c.archive_reads, 0)

    def test_a_settled_position_is_booked_from_the_store(self):
        # Down so long the market moved to the store: its payout still gets booked, instead of
        # the cost sitting in "open" for good.
        db.insert("orders", ticker="GONE-1", side="no", count=10, price=0.9, filled=10,
                  status="executed", order_id="o3", client_order_id="c3")
        k = client.Kalshi()
        k.markets = lambda limit=None, **p: []
        k.market = lambda t, archive=False: ({"ticker": t, "status": "finalized", "result": "no",
                                              "settlement_value_dollars": "0.0000"} if archive else {})
        markets = k.markets_by_ticker(["GONE-1"])
        self.c.markets_by_ticker = lambda tickers: {t: markets[t] for t in tickers if t in markets}
        self.c.position_list = []
        engine.run_once(self.c)
        self.assertAlmostEqual(ledger.realized(), 10 - 9 - client.fee(10, 0.9))
        self.assertAlmostEqual(ledger.open_cost(), 0)


class ReviewFollowUpTests(TempHome):
    """From the independent review: the open time never filters, "never placed" always looks
    in the store first, the cutoff is read after the live list, and store errors stop a run."""

    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300, "strategies": _all_off()})
        self.now = time.time()
        self.c = FakeKalshi(balance=1000)
        self.c.cutoffs = _cut(self.now)
        self.c.honor_min_ts = True

    def lost(self, ago, ticker="OLD-1", coid="lost-1"):
        db.insert("orders", strategy="favorites", ticker=ticker, side="yes", count=5, price=0.5,
                  filled=0, status="unknown", client_order_id=coid, ts=self.now - ago)

    def test_a_live_sale_dated_before_the_open_time_still_counts(self):
        # A market's open_time is only a hint for the store. If a fill ever predates it (a
        # wrong or moved open_time), the owner's sale must still be seen.
        db.insert("orders", ticker="S-3", side="no", count=20, price=0.9, filled=20,
                  status="executed", order_id="o1", client_order_id="c1")
        cost = 18 + client.fee(20, 0.9)
        self.c.fill_list = [{"trade_id": "t1", "order_id": "owner-1", "ticker": "S-3", "side": "no",
                             "action": "sell", "count_fp": "20.00", "no_price_dollars": "0.9000",
                             "created_time": _iso(self.now - 7 * DAY)}]
        self.c.position_list = []
        self.c.market_info["S-3"] = {"ticker": "S-3", "status": "finalized", "result": "no",
                                     "settlement_value_dollars": "0.0000", "open_time": _iso(self.now - 5 * DAY)}
        engine.run_once(self.c)
        self.assertAlmostEqual(ledger.realized(), -cost)

    def test_never_placed_only_after_the_store_says_so_too(self):
        # The cutoff says a 20-minute-old order is still live, but it isn't there. Before
        # calling it never placed (for good), the store is read anyway: here it is.
        self.lost(20 * 60)
        self.c.archived_orders = [{"client_order_id": "lost-1", "order_id": "o9", "status": "executed",
                                   "fill_count_fp": "5.00", "ticker": "OLD-1"}]
        self.c.position_list = [{"ticker": "OLD-1", "position": 5, "exposure": 2.5}]
        self.c.market_info["OLD-1"] = {"ticker": "OLD-1", "status": "active", "yes_bid_dollars": "0.5000"}
        engine.run_once(self.c)
        row = db.query("SELECT status, filled FROM orders")[0]
        self.assertEqual((row["status"], row["filled"]), ("reconciled:executed", 5))

    def test_a_young_order_is_never_looked_for_in_the_store(self):
        self.lost(30)   # inside the grace: it may just not be visible yet
        s = engine.run_once(self.c)
        self.assertTrue(s.get("waiting"), s)
        self.assertEqual(self.c.archive_reads, 0)

    def test_the_cutoff_is_read_after_the_live_list(self):
        seen = []
        c = FakeKalshi()
        live, cut = c.fills, c.cutoff
        c.fills = lambda min_ts=None, ticker=None, archive=False: seen.append("store" if archive else "live") or []
        c.cutoff = lambda: seen.append("cutoff") or _cut(self.now)
        every_fill(c, min_ts=self.now - 90 * DAY)
        self.assertEqual(seen, ["live", "cutoff", "store"])

    def test_a_store_outage_waits_and_changes_nothing(self):
        self.lost(20 * DAY)
        self.c.archive_error = KalshiError("GET /historical/orders -> 503: down", 503)
        s = engine.run_once(self.c)
        self.assertTrue(s.get("transient"), s)
        self.assertEqual(db.query("SELECT status FROM orders")[0]["status"], "unknown")
        self.assertEqual(db.get("consecutive_errors", 0), 0)

    def test_a_store_error_or_odd_reply_is_an_error_not_a_guess(self):
        for err in (KalshiError("GET /historical/orders -> 400: bad limit", 400),
                    KalshiError("/historical/orders: reply has no 'orders' (got: data)")):
            self.lost(20 * DAY, coid=f"lost-{err.status}")
            self.c.archive_error = err
            s = engine.run_once(self.c)
            self.assertFalse(s["ok"], s)
            self.assertFalse(s.get("transient"), s)
            self.assertNotIn("never_placed", {r["status"] for r in db.query("SELECT status FROM orders")})
            db.execute("DELETE FROM orders")
        self.assertEqual(db.get("consecutive_errors", 0), 2)

    def test_a_store_outage_holds_an_old_settlement(self):
        db.insert("orders", ticker="S-4", side="no", count=10, price=0.9, filled=10,
                  status="executed", order_id="o4", client_order_id="c4")
        self.c.market_info["S-4"] = {"ticker": "S-4", "status": "finalized", "result": "no",
                                     "settlement_value_dollars": "0.0000", "open_time": _iso(self.now - 90 * DAY)}
        self.c.position_list = []
        self.c.archive_error = KalshiError("GET /historical/fills -> 502", 502)
        s = engine.run_once(self.c)
        self.assertTrue(s.get("transient"), s)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM settlements")[0]["n"], 0)   # booked next time

    def test_setup_checks_the_key_can_read_the_store(self):
        from kal import selftest
        from tests.test_ops import GoodFake, rows_by_check
        good = GoodFake()
        self.assertTrue(rows_by_check(selftest.run(good))["history store"]["ok"])
        bad = GoodFake()
        bad.archive_error = KalshiError("GET /historical/fills -> 403: forbidden", 403)
        row = rows_by_check(selftest.run(bad))["history store"]
        self.assertFalse(row["ok"])
        self.assertIn("403", row["detail"])


if __name__ == "__main__":
    unittest.main()
