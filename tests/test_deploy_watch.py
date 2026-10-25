"""Behavioural tests for deploy/kal-watch.sh (the watchdog), run against a sandbox.

It alerts the phone from OUTSIDE the bot process when the bot is dead, crash-looping, or
unhealthy, with at most one alert per kind per day. Fake systemctl/runuser/curl/python
stubs log the calls; no root, no network.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCH = os.path.join(ROOT, "deploy", "kal-watch.sh")


def _write(p, s, x=False):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(s)
    if x:
        os.chmod(p, 0o755)


def _read(p):
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return ""


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class WatchSandbox(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="kal-watch-")
        self.bin = os.path.join(self.d, "bin")
        self.venv = os.path.join(self.d, "venv")
        self.data = os.path.join(self.d, "data")
        self.etc = os.path.join(self.d, "etc")
        for p in (self.bin, os.path.join(self.venv, "bin"), self.data, self.etc):
            os.makedirs(p)
        _write(os.path.join(self.data, "ntfy_topic"), "kal-default\n")
        self.curl_log = os.path.join(self.d, "curl.log")
        self.active = os.path.join(self.d, "ACTIVE")
        self.nrest = os.path.join(self.d, "nrest")
        _write(self.nrest, "0\n")
        _write(os.path.join(self.bin, "systemctl"), f"""#!/bin/sh
if [ "$1" = is-active ]; then [ -e {self.active} ] && exit 0 || exit 1; fi
if [ "$1 $2 $3" = "show kal -p" ]; then cat {self.nrest} 2>/dev/null || echo 0; exit 0; fi
exit 0
""", x=True)
        _write(os.path.join(self.bin, "runuser"), """#!/bin/sh
shift 2 2>/dev/null
[ "$1" = "--" ] && shift
exec "$@"
""", x=True)
        _write(os.path.join(self.bin, "curl"), f"""#!/bin/sh
echo "$*" >> {self.curl_log}
exit 0
""", x=True)
        _write(os.path.join(self.venv, "bin", "python"), """#!/bin/sh
case "$*" in
  *"-m kal health"*)
     if [ -n "$KAL_HOME" ] && [ -e "$KAL_HOME/health_fail" ]; then exit 1; fi
     exit 0 ;;
  *) exit 0 ;;
esac
""", x=True)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _run(self):
        env = dict(os.environ, APP=os.path.join(self.d, "app"), VENV=self.venv,
                   DATA=self.data, ETC=self.etc, PATH=self.bin + ":/usr/bin:/bin")
        return subprocess.run(["bash", WATCH], env=env, capture_output=True, text=True)

    def test_dead_service_alerts_high(self):
        r = self._run()
        log = _read(self.curl_log)
        self.assertIn("not running", log)
        self.assertIn("Priority: high", log)
        self.assertEqual(r.returncode, 0)

    def test_at_most_one_alert_per_kind_per_day(self):
        self._run()
        first = _read(self.curl_log)
        self._run()
        self.assertEqual(_read(self.curl_log), first)   # no second buzz the same day

    def test_crash_loop_alerts(self):
        open(self.active, "w").close()
        _write(self.nrest, "5\n")
        _write(os.path.join(self.data, "watch_nrestarts"), "2\n")
        self._run()
        self.assertIn("crash-looping", _read(self.curl_log))

    def test_unhealthy_but_running_alerts(self):
        open(self.active, "w").close()
        open(os.path.join(self.data, "health_fail"), "w").close()
        self._run()
        self.assertIn("unhealthy", _read(self.curl_log))

    def test_healthy_is_quiet(self):
        open(self.active, "w").close()
        r = self._run()
        self.assertEqual(_read(self.curl_log), "")
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
