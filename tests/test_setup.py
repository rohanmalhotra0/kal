import os
import stat
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kal import client, config, notify, setup
from tests.helpers import TempHome


def make_pem(path):
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with open(path, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()))


class SetupTests(TempHome):
    def _run(self, answers, rows=None):
        said = []
        feed = iter(answers)
        with mock.patch.object(notify.requests, "post"):
            code = setup.run(input_fn=lambda q: next(feed), print_fn=lambda *a: said.extend(a),
                             selftest_fn=lambda order_probe: rows if rows is not None else
                             [{"check": "markets", "ok": True, "detail": "ok"}])
        return code, "\n".join(said)

    def test_full_setup_saves_key_and_turns_on(self):
        src = os.path.join(tempfile.mkdtemp(), "key.pem")
        make_pem(src)
        code, text = self._run(["abcd-1234-efgh", src, "real", "300", "yes", "yes"])
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

    def test_bad_answers_are_asked_again(self):
        src = os.path.join(tempfile.mkdtemp(), "key.pem")
        make_pem(src)
        junk = os.path.join(tempfile.mkdtemp(), "junk.txt")
        open(junk, "w").write("not a key")
        code, text = self._run(["!", "abcd-1234-efgh", junk, src, "maybe", "practice", "lots", "-5", "250",
                                "no", "no"])
        self.assertEqual(code, 0, text)
        cfg = config.load()
        self.assertEqual((cfg["env"], cfg["budget"], cfg["enabled"]), ("demo", 250.0, False))
        self.assertIn("doesn't look like a key ID", text)
        self.assertIn("Can't use that file", text)

    def test_failed_selftest_never_turns_on(self):
        src = os.path.join(tempfile.mkdtemp(), "key.pem")
        make_pem(src)
        code, text = self._run(["abcd-1234-efgh", src, "real", "300", "no"],
                               rows=[{"check": "order probe", "ok": False, "detail": "rejected"}])
        self.assertEqual(code, 1)
        self.assertFalse(config.load()["enabled"])
        self.assertIn("FAIL", text)

    def test_rerun_keeps_answers_with_enter(self):
        src = os.path.join(tempfile.mkdtemp(), "key.pem")
        make_pem(src)
        self._run(["abcd-1234-efgh", src, "real", "300", "yes", "no"])
        topic = notify.topic()
        code, text = self._run(["", "", "", "", "", "no"])
        self.assertEqual(code, 0, text)
        self.assertEqual(config.load()["budget"], 300.0)
        self.assertEqual(notify.topic(), topic)  # same alert topic as before


if __name__ == "__main__":
    unittest.main()
