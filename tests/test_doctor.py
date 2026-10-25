"""kal doctor, the first-hour checklist: python -m unittest tests.test_doctor"""
import io
import os
from contextlib import redirect_stdout
from unittest import mock

from kal import __main__ as cli, config, doctor, gate
from tests.fakes import FakeKalshi
from tests.helpers import TempHome, orders_proven


def healthy():
    return [{"check": "disk", "ok": True, "level": "ok", "detail": "fine"}]


class DoctorTests(TempHome):
    def setUp(self):
        super().setUp()
        config.update({"budget": 300.0})
        quiet = mock.patch.object(doctor, "KEY_COPIES", ())   # this machine's own /tmp stays out of it
        quiet.start()
        self.addCleanup(quiet.stop)

    def rows(self, balance=(True, 300.0), client=None):
        return doctor.rows(balance_fn=lambda env: balance, client_fn=lambda env: client or FakeKalshi(),
                           health_fn=healthy)

    def by(self, rows):
        return {r["check"]: r for r in rows}

    def test_no_key_is_the_first_thing_it_says(self):
        r = self.rows(balance=(False, None))
        self.assertEqual((r[0]["check"], r[0]["level"]), ("key", "fail"))
        self.assertIn("sudo kal setup", r[0]["detail"])
        self.assertNotIn("money", self.by(r))       # nothing to compare without Kalshi

    def test_a_key_kalshi_refuses_fails(self):
        self.assertEqual(self.by(self.rows(balance=(True, None)))["key"]["level"], "fail")

    def test_a_good_first_hour_reads_ready(self):
        with mock.patch.object(doctor.notify, "enabled", return_value=True):
            r = self.by(self.rows())
        self.assertEqual({k: v["level"] for k, v in r.items()},
                         {"key": "ok", "money": "ok", "test orders": "ok", "strategies": "ok", "alerts": "ok",
                          "disk": "ok"})
        self.assertIn("real money", r["key"]["detail"])
        self.assertIn("not sent yet", r["test orders"]["detail"])
        self.assertIn("real money: basket_arb.", r["strategies"]["detail"])
        self.assertIn("Practice only: ladder_arb", r["strategies"]["detail"])

    def test_more_money_in_the_account_than_the_bot_owns_is_flagged(self):
        r = self.by(self.rows(balance=(True, 1000.0)))["money"]
        self.assertEqual(r["level"], "warn")
        self.assertIn("$700 more than the bot's own $300", r["detail"])
        self.assertEqual(self.by(self.rows(balance=(True, 302.0)))["money"]["level"], "ok")   # a bit of interest

    def test_a_zero_budget_fails(self):
        config.update({"budget": 0.0})
        self.assertEqual(self.by(self.rows())["money"]["level"], "fail")

    def test_test_orders_passed_or_refused(self):
        orders_proven()
        self.assertIn("passed", self.by(self.rows())["test orders"]["detail"])
        gate.record(FakeKalshi(), "prod", False, "Kalshi refused the $0.01 NO test order")
        r = self.by(self.rows())["test orders"]
        self.assertEqual(r["level"], "warn")
        self.assertIn("refused", r["detail"])
        gate.record(FakeKalshi(), "demo", False, "another exchange's try")   # not this key + exchange
        self.assertEqual(self.by(self.rows())["test orders"]["level"], "ok")

    def test_a_key_copy_left_in_tmp_is_a_warning(self):
        import tempfile
        copy = os.path.join(tempfile.mkdtemp(), "kalshi.pem")
        with mock.patch.object(doctor, "KEY_COPIES", (copy,)):
            self.assertNotIn("key copy", self.by(self.rows()))   # nothing there: no row at all
            with open(copy, "w") as f:
                f.write("-----BEGIN PRIVATE KEY-----")
            row = self.by(self.rows())["key copy"]
        self.assertEqual(row["level"], "warn")
        self.assertIn(f"sudo rm {copy}", row["detail"])
        os.remove(copy)

    def test_alerts_off_is_a_warning(self):
        with mock.patch.object(doctor.notify, "enabled", return_value=False):
            self.assertEqual(self.by(self.rows())["alerts"]["level"], "warn")

    def test_it_never_sends_an_order_or_changes_a_setting(self):
        c = FakeKalshi()
        before = config.load()
        self.rows(client=c)
        self.assertEqual(list(c.orders), [])
        self.assertEqual(config.load(), before)

    def test_the_command_ends_with_ready_or_not(self):
        out = io.StringIO()
        with mock.patch.object(doctor, "rows", return_value=healthy()), redirect_stdout(out):
            self.assertEqual(cli.main(["doctor"]), 0)
        self.assertIn("Ready.", out.getvalue())
        bad = healthy() + [{"check": "key", "ok": False, "level": "fail", "detail": "no key"}]
        out = io.StringIO()
        with mock.patch.object(doctor, "rows", return_value=bad), redirect_stdout(out):
            self.assertEqual(cli.main(["doctor"]), 1)
        self.assertIn("Not ready: fix the FAIL lines above.", out.getvalue())
