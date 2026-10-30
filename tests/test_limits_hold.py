"""Limits hold for every real order, not just at the start of a run. A swarm review of the
money path found (each finding checked by a separate skeptic):
- a budget cut, a lower cap or a strategy moved off live mid-run were ignored until the
  run ended: only OFF was re-checked before each real order;
- the legs a half-built basket left stayed "locked-in", so the cap on open bets never
  counted them. python -m unittest tests.test_limits_hold
"""
from kal import config, db, engine, ledger
from kal.client import fee
from tests.helpers import TempHome, orders_proven
from tests.test_core import arb_event, fake_with_arb


def two_baskets():
    """Two live baskets (EV1, EV3), each locked in, each bought in one run."""
    c = fake_with_arb()
    c.event_list = [arb_event("EV1"), arb_event("EV3")]
    for ev in c.event_list:
        for m in ev["markets"]:
            b = float(m["yes_bid_dollars"])
            c.books[m["ticker"]] = {"yes": [(b, 40)], "no": []}
            c.market_info[m["ticker"]] = {"ticker": m["ticker"], "status": "active",
                                          "yes_bid_dollars": f"{b:.4f}", "no_bid_dollars": f"{1 - b - .01:.4f}"}
    return c


def baskets_bought(c):
    return {o[0].split("-")[0] for o in c.orders}


class MidRunTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})
        orders_proven()

    def run_editing(self, change):
        """A run where the owner saves `change` from the dashboard right after the first buy."""
        c = two_baskets()
        real_buy = c.buy

        def buy_then_edit(*a, **k):
            r = real_buy(*a, **k)
            if len(c.orders) == 1:
                config.update(change)
            return r
        c.buy = buy_then_edit
        return c, engine.run_once(c)

    def test_a_budget_cut_mid_run_stops_real_orders(self):
        # The skeptic's repro: both baskets were bought, $57.60, with the budget cut to $10.
        c, s = self.run_editing({"budget": 10})
        self.assertEqual(len(baskets_bought(c)), 1)
        self.assertIn("budget set stricter mid-run", " ".join(s["notes"]))

    def test_a_lower_cap_mid_run_stops_real_orders(self):
        c, s = self.run_editing({"max_per_arb": 5})
        self.assertEqual(len(baskets_bought(c)), 1)

    def test_a_strategy_moved_off_live_mid_run_stops_its_real_orders(self):
        for mode in ("paper", "off"):
            with self.subTest(mode):
                config.update({"strategies": {"basket_arb": {"mode": "live"}}})
                db.execute("DELETE FROM orders")
                db.execute("DELETE FROM signals")
                c, s = self.run_editing({"strategies": {"basket_arb": {"mode": mode}}})
                self.assertEqual(len(baskets_bought(c)), 1)
                self.assertIn("basket_arb left live mid-run", " ".join(s["notes"]))

    def test_a_looser_setting_mid_run_changes_nothing(self):
        c, s = self.run_editing({"budget": 200, "max_per_arb": 40})
        self.assertEqual(len(baskets_bought(c)), 2)
        self.assertNotIn("mid-run", " ".join(s["notes"]))


class StrandedLegTests(TempHome):
    def order(self, sid, ticker, filled, price=0.45):
        db.insert("orders", signal_id=sid, strategy="basket_arb", ticker=ticker, side="no", count=10,
                  price=price, filled=filled, status="executed", order_id=f"o-{ticker}")

    def signal(self, status):
        return db.insert("signals", strategy="basket_arb", event="EV1", title="basket", mode="live",
                         guaranteed=1, cost=20.0, payout_min=21.0, exp_profit=1.0, legs=[], status=status)

    def test_a_half_built_baskets_legs_count_as_bets(self):
        whole = self.signal("executed")
        self.order(whole, "EV9-0", 10)
        self.order(whole, "EV9-1", 10)
        self.assertEqual(ledger.open_directional(), 0)   # a whole basket is locked in
        stranded = self.signal("stranded: leg 2/4 EV1-1 filled 0/10; basket incomplete")
        self.order(stranded, "EV1-0", 10)
        self.assertAlmostEqual(ledger.open_directional(), 10 * 0.45 + fee(10, 0.45))
