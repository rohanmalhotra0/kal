"""The server scripts must at least parse: a typo here breaks installs and updates."""
import glob
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class DeployScriptTests(unittest.TestCase):
    def test_shell_scripts_parse(self):
        scripts = glob.glob(os.path.join(ROOT, "deploy", "*.sh"))
        self.assertTrue(scripts)
        for path in scripts:
            r = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{os.path.basename(path)}: {r.stderr}")

    def test_update_builds_and_tests_in_a_throwaway_copy_before_swapping(self):
        # The new code is built and tested in a staged copy, as a locked-down user with a
        # clean environment, so the tests never see the real key, topic or tokens. The live
        # tree and venv are swapped only after the tests pass.
        with open(os.path.join(ROOT, "deploy", "kal-update.sh")) as f:
            text = f.read()
        self.assertIn("env -i", text)                       # a clean environment
        self.assertIn('runuser -u "$STAGE_USER"', text)     # an unprivileged throwaway user
        self.assertIn("git archive", text)                  # a separate copy of the tree
        # The tests run before the restart, and before the live venv is ever pip-installed.
        self.assertLess(text.index("unittest"), text.index("try-restart kal"))
        self.assertLess(text.index("unittest"), text.index('"$VENV/bin/pip" install'))

    def test_update_service_has_no_environment_file(self):
        # It must never run with the key, tokens or topic in its environment: no active
        # EnvironmentFile= directive (a comment explaining its absence is fine).
        with open(os.path.join(ROOT, "deploy", "kal-update.service")) as f:
            text = f.read()
        self.assertIsNone(re.search(r"^\s*EnvironmentFile\s*=", text, re.M))

    def test_tests_never_see_real_data_or_secrets(self):
        import tests
        self.assertTrue(os.environ["KAL_HOME"].startswith(tempfile.gettempdir()))
        self.assertTrue(tests._HOME.startswith(tempfile.gettempdir()))
        for name in ("KALSHI_API_KEY_ID", "KAL_NTFY_TOPIC", "KAL_GITHUB_TOKEN"):
            self.assertNotIn(name, os.environ)

    def test_install_ships_the_update_units_but_leaves_them_off(self):
        with open(os.path.join(ROOT, "deploy", "install.sh")) as f:
            text = f.read()
        self.assertIn('"$APP/deploy/kal-update.timer" /etc/systemd/system/kal-update.timer', text)
        # Only the printed instructions mention enabling the UPDATE timer; install never does.
        self.assertIsNone(re.search(r"^\s*systemctl enable[^\n]*kal-update", text, re.M))

    def test_install_turns_on_the_watchdog(self):
        # The watchdog only reads and alerts, so install.sh enables it for you.
        with open(os.path.join(ROOT, "deploy", "install.sh")) as f:
            text = f.read()
        self.assertIn('"$APP/deploy/kal-watch.timer" /etc/systemd/system/kal-watch.timer', text)
        self.assertIsNotNone(re.search(r"systemctl enable[^\n]*kal-watch\.timer", text))


if __name__ == "__main__":
    unittest.main()
