from unittest import mock

from kal import config, db, engine, fees
from kal.client import KalshiError
from kal.strategies.base import Leg, Signal
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven
from tests.test_core import fake_with_arb

NOW = 1_800_000_000


def sig(ticker="KXHIGHNY-25OCT24-T60", event="KXHIGHNY-25OCT24"):
    return Signal("t", event, "t", [Leg(ticker, "no", 0.9, 1)])


class FeeCheckTests(TempHome):
    def test_known_quadratic_fees_pass(self):
        c = FakeKalshi()
        self.assertIsNone(fees.check(c, sig(), now=NOW))
        for fee_type, m in (("quadratic_with_maker_fees", 1), ("quadratic", 0.5), ("quadratic", 0),
                            ("quadratic_with_combo_maker_fees", 1)):
            db.put(fees.SERIES_KEY, {})
            c.series_fees = {"KXHIGHNY": {"fee_type": fee_type, "fee_multiplier": m}}
            self.assertIsNone(fees.check(c, sig(), now=NOW), (fee_type, m))

    def test_fees_the_bot_cannot_price_are_refused(self):
        # A flat fee ($0.01 a contract, like the 2025 Super Bowl) costs more than the formula
        # at the tails; an unknown type or a multiplier over 1 is a guess.
        for fee_type, m, word in (("flat", 1, "flat"), ("margin_market_maker_program_fees", 1, "margin"),
                                  (None, 1, "None"), ("quadratic", 1.5, "above"), ("quadratic", None, "number"),
                                  ("quadratic", -1, "above")):
            db.put(fees.SERIES_KEY, {})
            c = FakeKalshi()
            c.series_fees = {"KXHIGHNY": {"fee_type": fee_type, "fee_multiplier": m}}
            why = fees.check(c, sig(), now=NOW)
            self.assertIn("fees: KXHIGHNY", why)
            self.assertIn(word, why)

    def test_an_unknown_series_is_refused(self):
        c = FakeKalshi()
        c.series_info = mock.Mock(side_effect=KalshiError("not found", 404))
        self.assertIn("couldn't read its fees", fees.check(c, sig(), now=NOW))

    def test_event_overrides_count_even_before_they_start(self):
        c = FakeKalshi()
        c.fee_overrides = [{"event_ticker": "KXHIGHNY-25OCT24", "fee_type_override": "flat",
                            "fee_multiplier_override": 1, "scheduled_ts": "2026-10-28T00:00:00Z"}]
        self.assertIn("an event fee change", fees.check(c, sig(), now=NOW))
        self.assertIsNone(fees.check(c, sig("KXOTHER-1-A", "KXOTHER-1"), now=NOW))  # another event

    def test_an_override_on_any_event_of_the_signal_counts(self):
        # A negative strike has its own "-", and exact_link_arb's sets span two events: the
        # override is on the ladder's event, not the signal's, and must still be found.
        c = FakeKalshi()
        c.series_fees = {s: {"fee_type": "quadratic", "fee_multiplier": 1} for s in ("KXCPI", "KXECONSTATCPI")}
        c.fee_overrides = [{"event_ticker": "KXCPI-26OCT", "fee_type_override": "quadratic",
                            "fee_multiplier_override": 3}]
        two = Signal("t", "KXECONSTATCPI-26OCT", "t", [Leg("KXCPI-26OCT-T-0.2", "no", 0.35, 1),
                                                         Leg("KXCPI-26OCT-T-0.1", "yes", 0.3, 1),
                                                         Leg("KXECONSTATCPI-26OCT-T-0.1", "yes", 0.28, 1)])
        self.assertIn("fees: KXCPI-26OCT: an event fee change", fees.check(c, two, now=NOW))
        self.assertEqual(fees.events_of("KXCPI-26OCT-T-0.1"), ["KXCPI-26OCT", "KXCPI-26OCT-T"])
        self.assertEqual(fees.events_of("KXCPI-26OCT-T0.2"), ["KXCPI-26OCT"])
        self.assertEqual(fees.events_of("KXELONMARS-99"), [])   # falls back to the signal's event

    def test_harmless_overrides_pass(self):
        db.put(fees.OVERRIDES_KEY, {})
        c = FakeKalshi()
        c.fee_overrides = [  # real shapes: 6 seen on 2026-10-25, all like these
            {"event_ticker": "KXHIGHNY-25OCT24", "fee_type_override": "quadratic_with_maker_fees",
             "fee_multiplier_override": 1},
            {"event_ticker": "KXHIGHNY-25OCT24", "fee_type_override": None, "fee_multiplier_override": None}]
        self.assertIsNone(fees.check(c, sig(), now=NOW))

    def test_unreadable_overrides_skip_the_trade(self):
        c = FakeKalshi()
        c.event_fee_changes = mock.Mock(side_effect=KalshiError("503", 503))
        self.assertIn("event fee changes", fees.check(c, sig(), now=NOW))

    def test_fees_are_cached(self):
        c = FakeKalshi()
        c.series_info = mock.Mock(return_value={"fee_type": "quadratic", "fee_multiplier": 1})
        c.event_fee_changes = mock.Mock(return_value=[])
        for _ in range(3):
            fees.check(c, sig(), now=NOW)
        self.assertEqual((c.series_info.call_count, c.event_fee_changes.call_count), (1, 1))
        fees.check(c, sig(), now=NOW + fees.SERIES_TTL + 1)
        self.assertEqual((c.series_info.call_count, c.event_fee_changes.call_count), (2, 2))


class EngineFeeTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"enabled": True, "budget": 60, "max_per_arb": 30,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"basket_arb": {"mode": "live"}}})
        orders_proven()

    def test_a_basket_on_a_flat_fee_series_is_never_bought(self):
        c = fake_with_arb()
        c.series_fees = {"EV1": {"fee_type": "flat", "fee_multiplier": 1}}
        s = engine.run_once(c)
        self.assertTrue(s["ok"], s)
        self.assertEqual(c.orders, [])
        status = db.query("SELECT status FROM signals WHERE event = 'EV1'")[0]["status"]
        self.assertIn("skipped: fees: EV1", status)
        self.assertTrue(config.load()["enabled"])  # a refusal, not an emergency

    def test_normal_fees_trade(self):
        c = fake_with_arb()
        s = engine.run_once(c)
        self.assertEqual(s["live_trades"], 1)
