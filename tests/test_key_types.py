"""Kalshi makes Ed25519 API keys by default ("Ed25519 is selected by default; choose RSA if
your client supports only RSA-PSS", docs.kalshi.com, API Keys, Oct 2026). The bot signs
with either, and refuses any other kind of key with a plain message."""
import base64
import os
import tempfile
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from kal import client, setup

PATH = "/trade-api/v2/portfolio/balance"


def pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


class KeyTypeTests(unittest.TestCase):
    def test_an_ed25519_key_signs_the_text_itself(self):
        k = ed25519.Ed25519PrivateKey.generate()
        sig = base64.b64decode(client.sign(k, "1700000000000", "GET", PATH))
        k.public_key().verify(sig, f"1700000000000GET{PATH}".encode())   # raises if wrong
        self.assertEqual(len(sig), 64)

    def test_an_rsa_key_still_signs_with_pss(self):
        k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        sig = base64.b64decode(client.sign(k, "1700000000000", "POST", "/trade-api/v2/portfolio/events/orders"))
        k.public_key().verify(sig, b"1700000000000POST/trade-api/v2/portfolio/events/orders",
                              padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                              hashes.SHA256())

    def test_every_signed_request_works_with_kalshis_default_key(self):
        # Before: an Ed25519 key loaded fine, then the first signed request crashed (TypeError).
        k = ed25519.Ed25519PrivateKey.generate()
        home = tempfile.mkdtemp()
        with open(os.path.join(home, "kalshi.pem"), "wb") as f:
            f.write(pem(k))
        with open(os.path.join(home, "kalshi_key_id"), "w") as f:
            f.write("key-id-1\n")
        old = {v: os.environ.pop(v, None) for v in ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PATH")}
        try:
            kalshi = client.Kalshi.from_env(home)
        finally:
            os.environ.update({v: x for v, x in old.items() if x is not None})
        sent = {}

        class Reply:
            status_code, text = 200, '{"balance_dollars": "300.0000"}'

            def json(self):
                return {"balance_dollars": "300.0000"}

        def request(method, url, params=None, json=None, headers=None, timeout=None):
            sent.update(headers)
            return Reply()
        kalshi.s.request = request
        self.assertEqual(kalshi.balance(), 300.0)
        self.assertEqual(sent["KALSHI-ACCESS-KEY"], "key-id-1")
        k.public_key().verify(base64.b64decode(sent["KALSHI-ACCESS-SIGNATURE"]),
                              (sent["KALSHI-ACCESS-TIMESTAMP"] + "GET" + PATH).encode())

    def test_setup_takes_either_kind_and_refuses_others(self):
        d = tempfile.mkdtemp()
        for name, key in (("ed.pem", ed25519.Ed25519PrivateKey.generate()),
                          ("rsa.pem", rsa.generate_private_key(public_exponent=65537, key_size=2048))):
            path = os.path.join(d, name)
            with open(path, "wb") as f:
                f.write(pem(key))
            self.assertEqual(setup._load_pem(path), pem(key))
        other = os.path.join(d, "ec.pem")
        with open(other, "wb") as f:
            f.write(pem(ec.generate_private_key(ec.SECP256R1())))
        with self.assertRaises(ValueError) as e:
            setup._load_pem(other)
        self.assertIn("Ed25519 or RSA", str(e.exception))


if __name__ == "__main__":
    unittest.main()
