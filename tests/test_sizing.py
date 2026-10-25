import math
import random
import unittest

from kal import sizing
from kal.client import fee_per_contract


def old_contracts(p, price, bankroll, kelly_fraction, max_loss):
    """contracts() as it was before the optional extras, to prove the default is unchanged."""
    cost = price + fee_per_contract(price)
    stake = min(sizing.kelly(p, cost) * kelly_fraction * bankroll, max_loss)
    return max(0, int(stake // cost))


def growth(f, p, cost):
    """Expected log growth of betting fraction f on a contract costing `cost`."""
    return p * math.log(1 + f * (1 - cost) / cost) + (1 - p) * math.log(1 - f)


class KellyTests(unittest.TestCase):
    def test_known_values(self):
        self.assertAlmostEqual(sizing.kelly(0.9, 0.8), 0.5)
        self.assertEqual(sizing.kelly(1.0, 0.5), 1.0)

    def test_no_edge_bets_nothing(self):
        self.assertEqual(sizing.kelly(0.5, 0.6), 0)
        self.assertEqual(sizing.kelly(0.6, 0.6), 0)
        self.assertEqual(sizing.kelly(0.0, 0.1), 0)

    def test_bad_inputs_bet_nothing(self):
        for p, c in [(0.9, 0), (0.9, 1), (0.9, -0.1), (0.9, 1.2), (1.5, 0.5), (-0.1, 0.5),
                     (math.nan, 0.5), (0.9, math.nan), (math.inf, 0.5), (None, 0.5), ("0.9", 0.5)]:
            self.assertEqual(sizing.kelly(p, c), 0.0, (p, c))

    def test_kelly_maximizes_log_growth(self):
        for p, c in [(0.6, 0.5), (0.97, 0.93), (0.2, 0.1), (0.55, 0.3)]:
            f = sizing.kelly(p, c)
            self.assertGreater(f, 0)
            for d in (0.02, -0.02):
                if 0 < f + d < 1:
                    self.assertGreater(growth(f, p, c), growth(f + d, p, c))


class BetaTests(unittest.TestCase):
    def test_closed_forms(self):
        for x in (0.01, 0.3, 0.5, 0.77, 0.99):
            self.assertAlmostEqual(sizing.betainc(7, 1, x), x ** 7, places=12)
            self.assertAlmostEqual(sizing.betainc(1, 4, x), 1 - (1 - x) ** 4, places=12)
            self.assertAlmostEqual(sizing.betainc(3.5, 2.25, x), 1 - sizing.betainc(2.25, 3.5, 1 - x), places=12)
        self.assertAlmostEqual(sizing.betainc(40, 40, 0.5), 0.5, places=12)
        self.assertEqual(sizing.betainc(2, 3, 0), 0.0)
        self.assertEqual(sizing.betainc(2, 3, 1), 1.0)
        with self.assertRaises(ValueError):
            sizing.betainc(0, 1, 0.5)

    def test_quantile_inverts_cdf(self):
        for a, b in [(1, 1), (0.5, 0.5), (11, 1), (3, 9), (900.5, 100.5), (50_001, 49_999)]:
            for q in (0.01, 0.05, 0.5, 0.95):
                x = sizing.beta_quantile(q, a, b)
                self.assertAlmostEqual(sizing.betainc(a, b, x), q, places=6, msg=(a, b, q))

    def test_quantile_known_value(self):
        self.assertAlmostEqual(sizing.beta_quantile(0.05, 21, 1), 0.05 ** (1 / 21), places=10)

    def test_close_to_normal_for_big_samples(self):
        a, b = 901, 101
        mean, sd = a / (a + b), math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1)))
        self.assertAlmostEqual(sizing.beta_quantile(0.05, a, b), mean - 1.645 * sd, delta=0.002)


class ShrunkPTests(unittest.TestCase):
    def test_below_raw_rate(self):
        for wins, trials in [(1, 1), (9, 10), (10, 10), (30, 40), (93, 100), (1, 50), (500, 1000)]:
            self.assertLess(sizing.shrunk_p(wins, trials), wins / trials, (wins, trials))
        self.assertEqual(sizing.shrunk_p(0, 5), 0.0)  # never above a raw rate of 0

    def test_short_record_bets_less(self):
        self.assertAlmostEqual(sizing.shrunk_p(10, 10), 0.05 ** (1 / 11), places=9)
        self.assertAlmostEqual(sizing.shrunk_p(100, 100), 0.05 ** (1 / 101), places=9)
        self.assertLess(sizing.shrunk_p(10, 10), sizing.shrunk_p(100, 100))

    def test_converges_to_raw_rate(self):
        last = 0.0
        for n in (10, 100, 1_000, 10_000, 100_000):
            p = sizing.shrunk_p(0.9 * n, n)
            self.assertGreater(p, last)
            self.assertLess(p, 0.9)
            last = p
        self.assertGreater(last, 0.898)

    def test_more_confidence_means_lower_p(self):
        self.assertLess(sizing.shrunk_p(45, 50, confidence=0.99), sizing.shrunk_p(45, 50, confidence=0.8))

    def test_prior_mean_pulls_a_thin_record(self):
        high = sizing.shrunk_p(4, 5, prior_mean=0.9, prior_weight=10)
        low = sizing.shrunk_p(4, 5, prior_mean=0.2, prior_weight=10)
        self.assertLess(low, high)
        self.assertLessEqual(high, 0.8)

    def test_bad_records_give_zero(self):
        for wins, trials in [(0, 0), (6, 5), (-1, 5), (math.nan, 5), (3, math.inf), (None, 4)]:
            self.assertEqual(sizing.shrunk_p(wins, trials), 0.0, (wins, trials))

    def test_bad_settings_raise(self):
        for kw in ({"confidence": 1}, {"confidence": 0}, {"prior_mean": 0}, {"prior_weight": 0}):
            with self.assertRaises(ValueError):
                sizing.shrunk_p(5, 10, **kw)

    def test_win_rate_se(self):
        self.assertAlmostEqual(sizing.win_rate_se(0, 0), math.sqrt(1 / 12))  # flat prior
        self.assertLess(sizing.win_rate_se(900, 1000), sizing.win_rate_se(9, 10))
        self.assertIsNone(sizing.win_rate_se(5, 3))


class UncertaintyShrinkTests(unittest.TestCase):
    def test_values(self):
        self.assertEqual(sizing.uncertainty_shrink(0.6, 0.5, 0), 1.0)
        self.assertAlmostEqual(sizing.uncertainty_shrink(0.6, 0.5, 0.05), 0.75)  # edge = 2 se
        self.assertEqual(sizing.uncertainty_shrink(0.6, 0.5, 0.1), 0.0)          # edge = 1 se
        self.assertEqual(sizing.uncertainty_shrink(0.6, 0.5, 0.5), 0.0)

    def test_no_edge_or_bad_input(self):
        self.assertEqual(sizing.uncertainty_shrink(0.5, 0.5, 0.01), 0.0)
        self.assertEqual(sizing.uncertainty_shrink(0.4, 0.5, 0.01), 0.0)
        for args in [(0.6, 0.5, -0.01), (0.6, 0.5, math.nan), (math.nan, 0.5, 0.01), (0.6, 0, 0.01)]:
            self.assertEqual(sizing.uncertainty_shrink(*args), 0.0, args)

    def test_more_doubt_bets_less(self):
        ks = [sizing.uncertainty_shrink(0.97, 0.93, se) for se in (0, 0.005, 0.01, 0.02, 0.04, 0.08)]
        self.assertEqual(ks, sorted(ks, reverse=True))
        self.assertTrue(all(0 <= k <= 1 for k in ks))


class SimultaneousTests(unittest.TestCase):
    def test_under_cap_unchanged(self):
        self.assertEqual(sizing.scale_to_cap([0.1, 0.2], 0.5), [0.1, 0.2])

    def test_over_cap_scaled_in_proportion(self):
        out = sizing.scale_to_cap([0.3, 0.6, 0.9], 0.6)
        self.assertAlmostEqual(sum(out), 0.6)
        self.assertAlmostEqual(out[1] / out[0], 2)
        self.assertAlmostEqual(out[2] / out[0], 3)

    def test_bad_entries_and_caps(self):
        self.assertEqual(sizing.scale_to_cap([-0.2, math.nan, 0.4], 0.2), [0.0, 0.0, 0.2])
        self.assertEqual(sizing.scale_to_cap([0.1, 0.2], 0), [0.0, 0.0])
        self.assertEqual(sizing.scale_to_cap([0.1], math.nan), [0.0])
        self.assertEqual(sizing.scale_to_cap([], 0.5), [])

    def test_cap_never_exceeded(self):
        rng = random.Random(7)
        for _ in range(500):
            fs = [rng.random() * 0.3 for _ in range(rng.randint(1, 40))]
            cap = rng.random()
            out = sizing.scale_to_cap(fs, cap)
            self.assertLessEqual(sum(out), cap + 1e-12)
            self.assertTrue(all(0 <= o <= f for o, f in zip(out, fs)))

    def test_exposure_room(self):
        self.assertEqual(sizing.exposure_room(1000, 200, 0.5), 300)
        self.assertEqual(sizing.exposure_room(1000, 600, 0.5), 0)
        self.assertEqual(sizing.exposure_room(1000, -50, 0.5), 500)  # negative exposure isn't room
        self.assertEqual(sizing.exposure_room(1000, math.nan, 0.5), 0)

    def test_sequential_bets_stay_under_cap(self):
        """Sizing bets one by one, each told what's already open, never passes the cap."""
        rng = random.Random(3)
        bankroll, cap = 1000.0, 0.3
        for _ in range(50):
            at_risk = 0.0
            for _ in range(30):
                price = rng.uniform(0.05, 0.95)
                p = min(0.999, price + rng.uniform(0, 0.3))
                n = sizing.contracts(p, price, bankroll, 1.0, 1e9, open_exposure=at_risk, max_exposure=cap)
                at_risk += n * sizing.unit_cost(price)
            self.assertLessEqual(at_risk, cap * bankroll + 1e-9)


class DrawdownTests(unittest.TestCase):
    def test_values(self):
        dm = sizing.drawdown_multiplier
        self.assertEqual(dm(100, 100), 1.0)
        self.assertEqual(dm(120, 100), 1.0)
        self.assertAlmostEqual(dm(90, 100), 0.625)
        self.assertAlmostEqual(dm(80, 100), 0.25)
        self.assertEqual(dm(50, 100), 0.25)
        self.assertEqual(dm(-10, 100), 0.25)
        self.assertEqual(dm(97, 100, start=0.05), 1.0)

    def test_monotone_and_bounded(self):
        out = [sizing.drawdown_multiplier(e, 300) for e in range(400, -1, -1)]
        self.assertEqual(out, sorted(out, reverse=True))
        self.assertTrue(all(0 < m <= 1 for m in out))

    def test_bad_numbers_are_cautious(self):
        for equity, peak in [(100, 0), (100, -5), (math.nan, 100), (100, math.inf), (None, 100)]:
            self.assertEqual(sizing.drawdown_multiplier(equity, peak), 0.25, (equity, peak))

    def test_bad_settings_raise(self):
        for kw in ({"floor": 0}, {"floor": 1.5}, {"full": 0}, {"start": 0.3, "full": 0.2}):
            with self.assertRaises(ValueError):
                sizing.drawdown_multiplier(90, 100, **kw)

    def test_chance_of_drawdown(self):
        self.assertAlmostEqual(sizing.chance_of_drawdown(1, 0.5), 0.5)
        self.assertAlmostEqual(sizing.chance_of_drawdown(0.25, 0.5), 0.5 ** 7)
        self.assertLess(sizing.chance_of_drawdown(0.25, 0.5), 0.01)
        self.assertEqual(sizing.chance_of_drawdown(2, 0.9), 1.0)
        self.assertEqual(sizing.chance_of_drawdown(0.5, 1), 1.0)
        with self.assertRaises(ValueError):
            sizing.chance_of_drawdown(0, 0.5)


class ContractsTests(unittest.TestCase):
    def test_core_expectation(self):
        self.assertEqual(sizing.contracts(0.99, 0.95, 100, 0.25, max_loss=5), 5)

    def test_default_call_unchanged(self):
        rng = random.Random(11)
        for _ in range(3000):
            price = round(rng.uniform(0.01, 0.99), 2)
            p = rng.uniform(0, 1)
            args = (p, price, rng.choice([0, 50, 300, 1000, 25_000]), rng.choice([0.1, 0.25, 0.5, 1]),
                    rng.choice([0, 1, 5, 10, 30, 1e9]))
            self.assertEqual(sizing.contracts(*args), old_contracts(*args), args)
            self.assertEqual(sizing.contracts(*args, throttle=1.0, open_exposure=0.0), old_contracts(*args))

    def test_infinite_max_loss_means_no_cap(self):
        self.assertEqual(sizing.contracts(0.7, 0.5, 1000, 0.25, math.inf), old_contracts(0.7, 0.5, 1000, 0.25, math.inf))

    def test_throttle_only_shrinks(self):
        base = sizing.contracts(0.7, 0.5, 1000, 0.25, 1e9)
        self.assertGreater(base, 0)
        self.assertLess(sizing.contracts(0.7, 0.5, 1000, 0.25, 1e9, throttle=0.5), base)
        self.assertEqual(sizing.contracts(0.7, 0.5, 1000, 0.25, 1e9, throttle=5), base)
        self.assertEqual(sizing.contracts(0.7, 0.5, 1000, 0.25, 1e9, throttle=0), 0)
        self.assertEqual(sizing.contracts(0.7, 0.5, 1000, 0.25, 1e9, throttle=math.nan), 0)

    def test_se_shrinks(self):
        base = sizing.contracts(0.7, 0.5, 1000, 0.25, 1e9)
        self.assertLess(sizing.contracts(0.7, 0.5, 1000, 0.25, 1e9, se=0.05), base)
        self.assertEqual(sizing.contracts(0.7, 0.5, 1000, 0.25, 1e9, se=0.5), 0)

    def test_exposure_cap(self):
        n = sizing.contracts(0.7, 0.5, 1000, 1.0, 1e9, open_exposure=280, max_exposure=0.3)
        self.assertLessEqual(n * sizing.unit_cost(0.5), 20 + 1e-9)
        self.assertGreater(n, 0)
        self.assertEqual(sizing.contracts(0.7, 0.5, 1000, 1.0, 1e9, open_exposure=300, max_exposure=0.3), 0)

    def test_bad_prices_never_crash(self):
        for price in (0, 1, -0.5, 1.5, math.nan, None):
            self.assertEqual(sizing.contracts(0.9, price, 1000, 0.25, 10), 0, price)
        self.assertEqual(sizing.contracts(0.9, 0.5, math.nan, 0.25, 10), 0)
        self.assertEqual(sizing.contracts(0.9, 0.5, 1000, 0.25, math.nan), 0)

    def test_extras_are_keyword_only(self):
        with self.assertRaises(TypeError):
            sizing.contracts(0.7, 0.5, 1000, 0.25, 10, 0.05)


if __name__ == "__main__":
    unittest.main()
