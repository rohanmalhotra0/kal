"""The server scripts must at least parse: a typo here breaks installs and updates."""
import glob
import os
import re
import shutil
import subprocess
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

    def test_update_runs_tests_in_a_clean_environment(self):
        # The server's settings hold the real key ID, alert topic and tokens. Tests that
        # saw them could send real alerts, or worse.
        with open(os.path.join(ROOT, "deploy", "kal-update.sh")) as f:
            text = f.read()
        self.assertIn("env -i", text)
        self.assertIn('KAL_HOME="$scratch/state"', text)
        self.assertLess(text.index("unittest discover"), text.index("systemctl restart kal"))

    def test_install_ships_the_update_units_but_leaves_them_off(self):
        with open(os.path.join(ROOT, "deploy", "install.sh")) as f:
            text = f.read()
        self.assertIn('"$APP/deploy/kal-update.timer" /etc/systemd/system/kal-update.timer', text)
        # Only the printed instructions mention enabling it; install.sh never does it itself.
        self.assertIsNone(re.search(r"^\s*systemctl enable[^\n]*kal-update", text, re.M))


if __name__ == "__main__":
    unittest.main()
