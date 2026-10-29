import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kal import client, config, notify, risk, setup
from tests.helpers import TempHome


def make_pem(path):
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with open(path, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()))


GOOD = [{"check": "markets", "ok": True, "detail": "ok"}]


class SetupTests(TempHome):
    def setUp(self):
        # Before TempHome: cleanups run after tearDown, and a later snapshot would put back
        # a deleted KAL_HOME. Each test's KALSHI_API_KEY_ID / KAL_CMD go away after it.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        super().setUp()
        for k in ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PATH", "KAL_CMD", "KAL_NTFY_TOPIC"):
            os.environ.pop(k, None)
        downloads = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, downloads, True)
        self.src = os.path.join(downloads, "key.pem")
        make_pem(self.src)
        self.prompts = []
        self.on_at_selftest = []

    def _run(self, answers, rows=None, balance=10_000.0, has_key=True, interactive=True):
        """Setup with typed answers. The Kalshi balance is given (no network), and so is
        whether someone is at a terminal to confirm a big budget."""
        said = []
        feed = iter(answers)

        def answer(q):
            self.prompts.append(q)
            return next(feed)

        def selftest(order_probe):
            self.on_at_selftest.append(config.load()["enabled"])
            return rows if rows is not None else GOOD
        with mock.patch.object(notify.requests, "post") as post:
            post.return_value.status_code = 200
            code = setup.run(input_fn=answer, print_fn=lambda *a: said.extend(a), selftest_fn=selftest,
                             balance_fn=lambda env: (has_key, balance), interactive=interactive)
        return code, "\n".join(said)

    def running_bot(self, budget="300"):
        """A first setup that turned the bot on, with real money."""
        code, text = self._run(["abcd-1234-efgh", self.src, "real", budget, "no", "yes"])
        self.assertEqual(code, 0, text)
        self.assertTrue(config.load()["enabled"])
        self.prompts.clear()
        self.on_at_selftest.clear()

    def test_full_setup_saves_key_and_turns_on(self):
        code, text = self._run(["abcd-1234-efgh", self.src, "real", "300", "yes", "yes"])
        self.assertEqual(code, 0, text)
        cfg = config.load()
        self.assertEqual((cfg["env"], cfg["budget"], cfg["enabled"]), ("prod", 300.0, True))
        pem = os.path.join(config.home(), "kalshi.pem")
        self.assertEqual(stat.S_IMODE(os.stat(pem).st_mode), 0o600)
        key_id, key = client.load_key(config.home())
        self.assertEqual(key_id, "abcd-1234-efgh")
        self.assertIsNotNone(key)
        self.assertTrue(notify.enabled())
        self.assertIn("ntfy", text)
        self.assertIn("The bot is ON (REAL money, $300).", text)

    def test_a_key_copy_made_elsewhere_gets_a_reminder_to_delete_it(self):
        # A copy left in /tmp can be read by other users (and by the auto-update's test user).
        code, text = self._run(["abcd-1234-efgh", self.src, "real", "300", "yes", "no"])
        self.assertIn(f"Now delete the copy you gave it, so nothing else can read your key: sudo rm {self.src}", text)

    def test_a_key_already_in_place_needs_no_reminder(self):
        os.makedirs(config.home(), exist_ok=True)
        shutil.copy(self.src, os.path.join(config.home(), "kalshi.pem"))   # sent straight to the bot's folder
        code, text = self._run(["abcd-1234-efgh", "", "real", "300", "yes", "no"])   # Enter keeps it
        self.assertIn("Saved. Only you can read it.", text)
        self.assertNotIn("delete the copy", text)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(config.home(), "kalshi.pem")).st_mode), 0o600)

    def test_bad_answers_are_asked_again(self):
        junk = os.path.join(os.path.dirname(self.src), "junk.txt")
        with open(junk, "w") as f:
            f.write("not a key")
        code, text = self._run(["!", "abcd-1234-efgh", junk, self.src, "maybe", "practice", "lots", "-5", "250",
                                "no", "no"])
        self.assertEqual(code, 0, text)
        cfg = config.load()
        self.assertEqual((cfg["env"], cfg["budget"], cfg["enabled"]), ("demo", 250.0, False))
        self.assertIn("doesn't look like a key ID", text)
        self.assertIn("Can't use that file", text)

    def test_failed_selftest_never_turns_on(self):
        code, text = self._run(["abcd-1234-efgh", self.src, "real", "300", "no"],
                               rows=[{"check": "order probe", "ok": False, "detail": "rejected"}])
        self.assertEqual(code, 1)
        self.assertFalse(config.load()["enabled"])
        self.assertIn("FAIL", text)
        self.assertTrue(text.endswith("The bot is OFF."), text)

    def test_rerun_keeps_answers_with_enter(self):
        self._run(["abcd-1234-efgh", self.src, "real", "300", "yes", "no"])
        topic = notify.topic()
        code, text = self._run(["", "", "", "", "", "no"])
        self.assertEqual(code, 0, text)
        self.assertEqual(config.load()["budget"], 300.0)
        self.assertEqual(notify.topic(), topic)  # same alert topic as before

    # ---------- the alert topic ----------

    TOPIC = r"^kal-[abcdefghijkmnpqrstuvwxyz23456789]{20}$"   # as install.sh and the dashboard make it

    def test_a_new_topic_has_no_look_alikes(self):
        """Typed into the ntfy app by hand: no l/1 or o/0 to mix up."""
        for _ in range(200):
            self.assertRegex(setup.new_topic(), self.TOPIC)
        self.assertGreater(len({setup.new_topic() for _ in range(50)}), 49)   # random, not fixed
        code, text = self._run(["abcd-1234-efgh", self.src, "real", "300", "yes", "no"])
        self.assertEqual(code, 0, text)
        self.assertRegex(notify.topic(), self.TOPIC)
        self.assertIn(f"      {notify.topic()}", text)

    def test_a_saved_topic_is_never_changed(self):
        """The phone is subscribed to it. Old ones (made with hex) may hold 0 and 1: kept anyway."""
        old = "kal-0123456789abcdef0110"
        with open(os.path.join(config.home(), "ntfy_topic"), "w") as f:
            f.write(old + "\n")
        self._run(["abcd-1234-efgh", self.src, "real", "300", "yes", "no"])
        self._run(["", "", "", "", "", "no"])
        self.assertEqual(notify.topic(), old)

    def test_a_topic_in_the_settings_file_is_the_one_shown(self):
        """KAL_NTFY_TOPIC wins over the saved file, so that's where alerts really go."""
        os.environ["KAL_NTFY_TOPIC"] = "kal-from-the-env-file-22"
        code, text = self._run(["abcd-1234-efgh", self.src, "real", "300", "yes", "no"])
        self.assertEqual(code, 0, text)
        self.assertIn("      kal-from-the-env-file-22", text)
        self.assertFalse(os.path.exists(os.path.join(config.home(), "ntfy_topic")))

    # ---------- a bot that is on (critic: "Not turned on" while it stayed on) ----------

    def test_rerun_on_a_running_bot_turns_it_off_and_keeps_it_off(self):
        """The owner slips at the budget (3000 for 300), then presses Enter at "Turn the bot
        on now?". It must really be off, and say so."""
        self.running_bot()
        code, text = self._run(["", "", "", "3000", "3000", "", ""], balance=300.0)
        self.assertEqual(code, 0, text)
        cfg = config.load()
        self.assertEqual((cfg["budget"], cfg["enabled"]), (3000.0, False))
        self.assertEqual(self.on_at_selftest, [False])   # off before anything changed
        self.assertIn("The bot is OFF while you change things.", text)
        self.assertIn("The bot is OFF. When you're ready: python -m kal on", text)
        self.assertNotIn("The bot is ON", text)

    def test_a_failed_selftest_leaves_a_running_bot_off(self):
        self.running_bot()
        code, text = self._run(["", "", "", "", "", ""], rows=[{"check": "balance", "ok": False, "detail": "401"}])
        self.assertEqual(code, 1, text)
        self.assertFalse(config.load()["enabled"])
        self.assertIn("The bot is OFF while you change things.", text)
        self.assertTrue(text.endswith("The bot is OFF."), text)

    def test_only_yes_turns_it_back_on(self):
        self.running_bot()
        code, text = self._run(["", "", "", "", "", "yes"])
        self.assertEqual(code, 0, text)
        self.assertTrue(config.load()["enabled"])
        self.assertIn("The bot is ON (REAL money, $300).", text)

    # ---------- the budget: a slip must be typed twice ----------

    def test_a_budget_over_the_balance_is_typed_twice(self):
        code, text = self._run(["abcd-1234-efgh", self.src, "real", "3000", "300", "250", "no", "no"],
                               balance=300.0)
        self.assertEqual(code, 0, text)
        self.assertEqual(config.load()["budget"], 250.0)
        self.assertIn("   Your Kalshi account has $300. Type 3000 again to confirm: ", self.prompts)
        self.assertIn("That doesn't match", text)

    def test_more_than_double_is_typed_twice(self):
        self.running_bot()
        code, text = self._run(["", "", "", "700", "$700", "", ""])
        self.assertEqual(code, 0, text)
        self.assertEqual(config.load()["budget"], 700.0)
        self.assertTrue(any("more than double the budget now ($300)" in p for p in self.prompts), self.prompts)

    def test_a_balance_it_cant_read_asks_anyway(self):
        self._run(["abcd-1234-efgh", self.src, "real", "300", "300", "no", "no"], balance=None)
        self.assertTrue(any("Can't check your Kalshi balance" in p for p in self.prompts), self.prompts)
        self.assertEqual(config.load()["budget"], 300.0)

    def test_lowering_or_keeping_the_budget_never_asks(self):
        self.running_bot()
        self._run(["", "", "", "", "", ""], balance=None)
        self._run(["", "", "", "100", "", ""], balance=None)
        self.assertEqual(config.load()["budget"], 100.0)
        self.assertFalse([p for p in self.prompts if "again to confirm" in p])

    def test_real_money_offers_the_real_budget_not_the_practice_one(self):
        self.running_bot()                                                     # $300 of real money
        code, text = self._run(["", "", "practice", "1000", "1000", "", "no"])   # practice with $1000
        self.assertEqual((code, config.load()["budget"]), (0, 1000.0), text)
        self.prompts.clear()
        code, text = self._run(["", "", "real", "", "", "no"])                 # Enter keeps the budget
        self.assertEqual(code, 0, text)
        cfg = config.load()
        self.assertEqual((cfg["env"], cfg["budget"]), ("prod", 300.0))
        self.assertIn("4. How many dollars can the bot use? It can never lose more than this [300]: ",
                      self.prompts)
        self.assertIn("Back to the real-money settings: budget $300.", text)

    def test_with_no_terminal_a_big_budget_is_refused(self):
        self.running_bot()
        code, text = self._run(["", "", "", "3000"], balance=300.0, interactive=False)
        self.assertEqual(code, 2, text)
        cfg = config.load()
        self.assertEqual((cfg["budget"], cfg["enabled"]), (300.0, False))
        self.assertIn("Not changed. Your Kalshi account has $300.", text)

    def test_the_balance_is_read_from_kalshi_for_the_chosen_exchange(self):
        sites = []

        def balance(k):
            sites.append(k.base)
            return 300.0
        with mock.patch("kal.client.Kalshi.balance", autospec=True, side_effect=balance):
            self.assertEqual(setup.kalshi_balance("prod"), (False, None))   # no key: nothing to read
            os.environ["KALSHI_API_KEY_ID"] = "abcd-1234-efgh"
            os.environ["KALSHI_PRIVATE_KEY_PATH"] = self.src
            self.assertEqual(setup.kalshi_balance("demo"), (True, 300.0))
        self.assertEqual(sites, [client.BASES["demo"]])
        with mock.patch("kal.client.Kalshi.balance", side_effect=RuntimeError("401")):
            self.assertEqual(setup.kalshi_balance("prod"), (True, None))   # can't read it: ask anyway

    # ---------- the key ID in kal.env wins over the saved one ----------

    def test_an_old_key_id_in_the_settings_file_stops_setup(self):
        """client.load_key reads KALSHI_API_KEY_ID first, so a new ID saved by setup would
        never be used: the real key would pair with the old demo ID (401s forever)."""
        os.environ.update(KALSHI_API_KEY_ID="demo-key-id-0000", KAL_CMD="sudo kal")
        code, text = self._run(["real-key-id-1111"])
        self.assertEqual(code, 1, text)
        self.assertIn("Delete the KALSHI_API_KEY_ID line in /etc/kal/kal.env, then: sudo systemctl restart kal",
                      text)
        self.assertIn("Then run this again: sudo kal setup", text)
        self.assertIn("[demo-key-id-0000]", self.prompts[0])   # it shows the ID the bot really uses
        self.assertFalse(os.path.exists(os.path.join(config.home(), "kalshi_key_id")))   # nothing saved
        code, text = self._run(["", self.src, "real", "300", "no", "no"])   # keeping that ID is fine
        self.assertEqual(code, 0, text)

    # ---------- what it tells the owner to type ----------

    def test_hints_match_how_the_owner_types_commands(self):
        """The server has no `python`; its wrapper sets KAL_CMD="sudo kal"."""
        os.environ["KAL_CMD"] = "sudo kal"
        _, failed = self._run(["abcd-1234-efgh", self.src, "real", "300", "no"],
                              rows=[{"check": "order probe", "ok": False, "detail": "rejected"}])
        self.assertIn("Fix those, then run: sudo kal setup", failed)
        _, worked = self._run(["", "", "", "", "no", "no"])
        self.assertIn("When you're ready: sudo kal on", worked)
        config.trip("lost too much")
        _, stopped = self._run(["", "", "", "", "no"])
        self.assertIn("Find out why, then run: sudo kal on --clear-trip", stopped)
        self.assertNotIn("python -m kal", failed + worked + stopped)

    def test_a_key_file_it_cant_read_says_how_to_fix_it(self):
        """scp leaves /tmp/kalshi.pem owned by root, mode 600: the kal user can't read it."""
        real, calls = setup._load_pem, []

        def load(path):
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError(13, "Permission denied")
            return real(path)
        with mock.patch.object(setup, "_load_pem", load):
            code, text = self._run(["abcd-1234-efgh", "/tmp/kalshi.pem", self.src, "real", "300", "no", "no"])
        self.assertEqual(code, 0, text)
        self.assertIn("The bot can't read that file. Run: sudo chmod 644 /tmp/kalshi.pem\n   Then type the path again.",
                      text)
        self.assertNotIn("PermissionError", text)

    def test_a_read_only_key_file_left_there_is_replaced(self):
        pem = os.path.join(config.home(), "kalshi.pem")
        with open(pem, "w") as f:
            f.write("old")
        os.chmod(pem, 0o444)
        code, text = self._run(["abcd-1234-efgh", self.src, "real", "300", "no", "no"])
        self.assertEqual(code, 0, text)
        self.assertEqual(stat.S_IMODE(os.stat(pem).st_mode), 0o600)
        with open(pem, "rb") as f, open(self.src, "rb") as g:
            self.assertEqual(f.read(), g.read())

    # ---------- switching exchange ----------

    def test_switching_exchange_waits_for_a_trade_run(self):
        self.running_bot()
        with mock.patch.object(config, "SWITCH_WAIT", 0.3), risk.run_lock():
            code, text = self._run(["", "", "practice", "", ""])
        self.assertEqual(code, 1, text)
        self.assertIn("A trade run is still going. Try again in a minute: python -m kal setup", text)
        cfg = config.load()
        self.assertEqual((cfg["env"], cfg["enabled"]), ("prod", False))
        self.assertEqual(self.on_at_selftest, [])   # no test order on the wrong exchange


if __name__ == "__main__":
    unittest.main()
