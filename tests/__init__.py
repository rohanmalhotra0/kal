"""Tests never touch real data or real secrets.

Before any test module loads, KAL_HOME points at a new temp folder and the secret
settings are removed from the environment. tests.helpers.TempHome still gives each test
its own folder; this guards the tests that forget it. On a server, or a laptop with the
real key set, a test can't write to the live books, send a phone alert, or push a report.
"""
import atexit
import os
import shutil
import tempfile

_HOME = tempfile.mkdtemp(prefix="kal-tests-")
os.environ["KAL_HOME"] = _HOME
for _name in ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PATH", "KAL_NTFY_TOPIC",
              "KAL_GITHUB_TOKEN", "KAL_GITHUB_REPO", "KAL_DASHBOARD_PASSWORD"):
    os.environ.pop(_name, None)
atexit.register(shutil.rmtree, _HOME, True)
