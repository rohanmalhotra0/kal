"""The daily stop reads equity: open positions at what they're worth now. A market shows no
bid once it closes, a while before it settles. Valued at cost then, a winner's gain left
equity at the close and came back at the settlement: a false stop ("lost $57.32 today" for
a winner), or, settling the next day, a real loss partly hidden from that day's stop (an
independent review of the lost-disk fix found both, on main too). Now a closed market
keeps its last mark, and a known result counts at what it will pay."""
import time
from datetime import datetime, timezone
from unittest import mock

from kal import config, db, engine, ledger, risk
from kal.client import new_order_id
from tests.fakes import FakeKalshi
from tests.helpers import TempHome


class ClosedMarketTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 300,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        self.c = FakeKalshi(balance=1000)
        self.c.cutoffs = {"orders_updated_ts": time.time() - 15 * 86400,
                          "trades_created_ts": time.time() - 60 * 86400}
        self.c.position_list = []

    def holds(self, ticker, side, n, p, bid):
        db.insert("orders", ts=0, strategy="favorites", ticker=ticker, side=side, count=n, price=p, filled=n,
                  status="executed", order_id=f"k{ticker}", client_order_id=new_order_id())
        self.c.market_info[ticker] = {"ticker": ticker, "status": "active", f"{side}_bid_dollars": f"{bid:.4f}"}
        self.c.position_list.append({"ticker": ticker, "position": n if side == "yes" else -n,
                                     "exposure": n * p})

    def run_at(self, *when):
        with mock.patch.object(risk, "datetime", **{"now.return_value": datetime(*when, tzinfo=timezone.utc)}):
            engine.run_once(self.c)
        return db.get("last_pnl")["pnl"]

    def test_a_winner_whose_market_closes_keeps_its_gain(self):
        self.holds("Y", "yes", 100, 0.40, 0.99)
        before = self.run_at(2026, 10, 29, 10, 0)
        self.c.market_info["Y"] = {"ticker": "Y", "status": "closed", "result": ""}
        self.assertAlmostEqual(self.run_at(2026, 10, 29, 10, 15), before)   # was: $59 less, a false stop
        self.assertEqual(config.load()["disabled_reason"], "")

    def test_a_lookup_that_fails_keeps_the_last_mark_too(self):
        self.holds("Y", "yes", 100, 0.40, 0.99)
        before = self.run_at(2026, 10, 29, 10, 0)
        real = self.c.markets_by_ticker
        self.c.markets_by_ticker = lambda tickers: {t: m for t, m in real(tickers).items() if t != "Y"}
        self.assertAlmostEqual(self.run_at(2026, 10, 29, 10, 15), before)

    def test_a_winner_settling_the_next_day_never_hides_that_days_loss(self):
        self.holds("Y", "yes", 30, 0.40, 0.99)
        self.holds("Z", "no", 76, 0.50, 0.50)
        self.run_at(2026, 10, 28, 10, 0)
        self.c.market_info["Y"] = {"ticker": "Y", "status": "closed", "result": ""}
        self.run_at(2026, 10, 28, 23, 0)
        self.run_at(2026, 10, 29, 0, 30)
        for t in ("Y", "Z"):
            self.c.market_info[t] = {"ticker": t, "status": "finalized", "result": "yes",
                                     "settlement_value_dollars": "1.0000"}
        self.c.position_list = []
        self.run_at(2026, 10, 29, 12, 0)
        self.assertIn("today", config.load()["disabled_reason"])   # was: $20.51 of Z's $38 seen

    def test_a_known_loss_counts_before_the_market_settles(self):
        self.holds("Y", "yes", 100, 0.40, 0.50)
        before = self.run_at(2026, 10, 29, 10, 0)
        self.c.market_info["Y"] = {"ticker": "Y", "status": "determined", "result": "no"}
        self.assertAlmostEqual(before - self.run_at(2026, 10, 29, 10, 15), 50)   # its $50 bid is gone

    def test_a_scalar_result_counts_at_its_settlement_value(self):
        # Only yes or no results counted: a scalar one kept the old bid until final, and the
        # difference landed on settlement day (independent review of this change).
        self.holds("Y", "yes", 100, 0.40, 0.50)
        before = self.run_at(2026, 10, 29, 10, 0)
        self.c.market_info["Y"] = {"ticker": "Y", "status": "determined", "result": "scalar",
                                   "settlement_value_dollars": "0.9000"}
        self.assertAlmostEqual(self.run_at(2026, 10, 29, 10, 15) - before, 40)   # 0.50 -> 0.90 a contract

    def test_a_result_on_a_market_still_trading_is_ignored(self):
        self.holds("Y", "yes", 100, 0.40, 0.99)
        before = self.run_at(2026, 10, 29, 10, 0)
        self.c.market_info["Y"]["result"] = "no"   # still active: its bid says what it's worth
        self.assertAlmostEqual(self.run_at(2026, 10, 29, 10, 15), before)
        self.assertEqual(config.load()["disabled_reason"], "")

    def test_a_market_that_never_had_a_bid_stays_at_cost(self):
        self.holds("Y", "yes", 100, 0.40, 0.0)
        self.run_at(2026, 10, 29, 10, 0)
        self.assertAlmostEqual(ledger.equity(self.c, config.load()), ledger.capital(config.load()))

    def test_marks_are_kept_only_for_open_positions(self):
        self.holds("Y", "yes", 100, 0.40, 0.99)
        self.run_at(2026, 10, 29, 10, 0)
        self.assertEqual(db.get("last_marks"), {"Y:yes": 0.99})
        self.c.market_info["Y"] = {"ticker": "Y", "status": "finalized", "result": "yes",
                                   "settlement_value_dollars": "1.0000"}
        self.c.position_list = []
        self.run_at(2026, 10, 29, 12, 0)
        self.assertEqual(db.get("last_marks") or {}, {})
