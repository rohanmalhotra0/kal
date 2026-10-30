"""Behavioural tests for deploy/kal-watch.sh (the watchdog), run against a sandbox.

It alerts the phone from OUTSIDE the bot process when the bot is dead, crash-looping, or
unhealthy, with at most one alert per kind per day. Fake systemctl/runuser/curl/python
stubs log the calls; no root, no network.
"""
import os
import re
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
        _write(os.path.join(self.d, "app", "kal", "__main__.py"), "")
        self.curl_log = os.path.join(self.d, "curl.log")
        self.active = os.path.join(self.d, "ACTIVE")
        self.coming = os.path.join(self.d, "COMING")      # a restart in progress: up by the next look
        self.curl_fail = os.path.join(self.d, "CURL_FAIL")
        self.nrest = os.path.join(self.d, "nrest")
        _write(self.nrest, "0\n")
        _write(os.path.join(self.bin, "systemctl"), f"""#!/bin/sh
if [ "$1" = is-active ]; then
  [ -e {self.active} ] && exit 0
  [ -e {self.coming} ] && touch {self.active}
  exit 1
fi
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
[ -e {self.curl_fail} ] && exit 22   # what curl -f says when the server refuses
exit 0
""", x=True)
        _write(os.path.join(self.venv, "bin", "python"), """#!/bin/sh
case "$*" in
  *"-m kal health"*)
     [ -f kal/__main__.py ] || exit 1   # like python -m kal: the code must be in this folder
     if [ -n "$KAL_HOME" ] && [ -e "$KAL_HOME/health_fail" ]; then exit 1; fi
     # A fresh start: no heartbeat on the first look, one by the next.
     if [ -n "$KAL_HOME" ] && [ -e "$KAL_HOME/health_once" ]; then rm "$KAL_HOME/health_once"; exit 1; fi
     exit 0 ;;
  *) exit 0 ;;
esac
""", x=True)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _run(self):
        env = dict(os.environ, APP=os.path.join(self.d, "app"), VENV=self.venv,
                   DATA=self.data, ETC=self.etc, PATH=self.bin + ":/usr/bin:/bin", KAL_WATCH_PAUSE="0")
        # cwd: systemd starts it in /, not in the code folder.
        return subprocess.run(["bash", WATCH], env=env, capture_output=True, text=True, cwd=self.d)

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

    def test_a_crash_after_a_manual_restart_alerts_too(self):
        # A manual restart starts the count from 0: one crash since reads 1, below the 2 seen.
        open(self.active, "w").close()
        _write(self.nrest, "1\n")
        _write(os.path.join(self.data, "watch_nrestarts"), "2\n")
        self._run()
        self.assertIn("crash-looping", _read(self.curl_log))

    def test_a_manual_restart_alone_or_old_crashes_are_quiet(self):
        open(self.active, "w").close()
        for now, seen in (("0", "2"), ("2", "2")):
            with self.subTest(now=now, seen=seen):
                _write(self.nrest, now + "\n")
                _write(os.path.join(self.data, "watch_nrestarts"), seen + "\n")
                self._run()
                self.assertEqual(_read(self.curl_log), "")

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

    def test_a_restart_in_progress_is_not_an_outage(self):
        # An update, a re-install or the install's own first look: down for a moment, then
        # up. A false alarm here would also use up the day's real "down" alert.
        open(self.coming, "w").close()
        r = self._run()
        self.assertEqual(_read(self.curl_log), "")
        self.assertIn("kal-watch: ok", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.data, "watch_down")))

    def test_a_fresh_start_without_a_heartbeat_yet_is_not_unhealthy(self):
        open(self.active, "w").close()
        open(os.path.join(self.data, "health_once"), "w").close()
        r = self._run()
        self.assertEqual(_read(self.curl_log), "")
        self.assertIn("kal-watch: ok", r.stdout)

    def test_a_failed_send_is_tried_again_next_time(self):
        open(self.curl_fail, "w").close()
        r = self._run()
        self.assertIn("couldn't send", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.data, "watch_down")))
        os.remove(self.curl_fail)
        r = self._run()
        self.assertIn("alerted", r.stdout)
        self.assertEqual(_read(self.curl_log).count("not running"), 2)   # sent on the second try
        self._run()
        self.assertEqual(_read(self.curl_log).count("not running"), 2)   # and then once a day again

    def test_the_second_look_comes_after_a_restart_is_done(self):
        # The pause must outlast systemd's wait before a restart (RestartSec), plus the start.
        with open(WATCH) as f:
            pause = int(re.search(r"KAL_WATCH_PAUSE:-(\d+)", f.read()).group(1))
        with open(os.path.join(ROOT, "deploy", "kal.service")) as f:
            restart = int(re.search(r"^RestartSec=(\d+)$", f.read(), re.M).group(1))
        self.assertGreaterEqual(pause, restart + 20)


if __name__ == "__main__":
    unittest.main()
