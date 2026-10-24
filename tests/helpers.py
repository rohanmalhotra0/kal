import os
import shutil
import tempfile
import unittest


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
