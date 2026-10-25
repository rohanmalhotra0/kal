import os
import shutil
import tempfile
import unittest


def orders_proven(env="prod"):
    """Mark the $0.01 test order as passed for the fake client's key (tests/test_gate.py
    covers the probe itself), so engine tests can trade live."""
    from kal import gate
    from tests.fakes import FakeKalshi
    gate.record(FakeKalshi(), env, True, "test order passed")


class TempHome(unittest.TestCase):
    """Gives each test its own empty KAL_HOME."""

    def setUp(self):
        self._old = os.environ.get("KAL_HOME")
        self.home = tempfile.mkdtemp()
        os.environ["KAL_HOME"] = self.home
        for k in ("KAL_ENABLED", "KAL_BUDGET", "KAL_ENV"):
            os.environ.pop(k, None)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)
        if self._old is None:
            os.environ.pop("KAL_HOME", None)
        else:
            os.environ["KAL_HOME"] = self._old
