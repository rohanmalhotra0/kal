"""Behavioural tests for deploy/kal-update.sh, run against a sandbox.

The real script runs in a temp dir with a local bare git "origin", a stub git history,
and fake systemctl/runuser/curl/python on PATH that log their calls. No root, no network:
tests pass here and on GitHub Actions. APP/VENV/DATA/ETC and a few knobs are env vars that
default to the real server paths, so overriding them can't change production behaviour.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATE = os.path.join(ROOT, "deploy", "kal-update.sh")
GIT = "g" + "it"

FAKE_PY = """#!/bin/sh
# fake python/pip for kal-update tests
case "$*" in
  *"-m venv "*)
     for a in "$@"; do d="$a"; done
     mkdir -p "$d/bin"
     cp "$0" "$d/bin/python"
     printf '#!/bin/sh\\nexit 0\\n' > "$d/bin/pip"
     chmod +x "$d/bin/python" "$d/bin/pip"
     exit 0 ;;
  *"-m unittest"*)
     [ -e tests/FAIL ] && exit 1
     exit 0 ;;
  *"-m kal health"*)
     if [ -n "$KAL_HOME" ] && [ -e "$KAL_HOME/health_fail" ]; then exit 1; fi
     exit 0 ;;
  *) exit 0 ;;
esac
"""


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


@unittest.skipUnless(shutil.which("bash") and shutil.which(GIT), "needs bash and git")
class UpdateSandbox(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="kal-upd-")
        self.bin = os.path.join(self.d, "bin")
        os.makedirs(self.bin)
        self.origin = os.path.join(self.d, "origin.git")
        self.work = os.path.join(self.d, "work")
        self.app = os.path.join(self.d, "app")
        self.venv = os.path.join(self.d, "venv")
        self.data = os.path.join(self.d, "data")
        self.etc = os.path.join(self.d, "etc")
        for p in (self.data, self.etc):
            os.makedirs(p)
        _write(os.path.join(self.data, "ntfy_topic"), "kal-default\n")
        self.sysctl_log = os.path.join(self.d, "systemctl.log")
        self.curl_log = os.path.join(self.d, "curl.log")
        self.pip_log = os.path.join(self.d, "pip.log")
        self.active = os.path.join(self.d, "ACTIVE")
        self.nrest = os.path.join(self.d, "nrest")
        self.loop = os.path.join(self.d, "LOOP")
        _write(self.nrest, "0\n")
        self._stubs()
        self._repo()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    # ---- sandbox construction ----
    def _git(self, cwd, *args, email="noreply@github.com", name="GitHub"):
        env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                   GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email,
                   GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
        r = subprocess.run([GIT, *args], cwd=cwd, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"git {args}: {r.stderr}")
        return r.stdout.strip()

    def _stubs(self):
        _write(os.path.join(self.bin, "systemctl"), f"""#!/bin/sh
echo "$*" >> {self.sysctl_log}
if [ "$1" = is-active ]; then [ -e {self.active} ] && exit 0 || exit 1; fi
if [ "$1 $2 $3" = "show kal -p" ]; then cat {self.nrest} 2>/dev/null || echo 0; exit 0; fi
if [ "$1" = try-restart ]; then
  if [ -e {self.loop} ]; then n=$(cat {self.nrest} 2>/dev/null || echo 0); echo $((n+1)) > {self.nrest}; fi
  exit 0
fi
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
        self.fakepy = os.path.join(self.bin, "fakepython")
        _write(self.fakepy, FAKE_PY, x=True)
        os.makedirs(os.path.join(self.venv, "bin"))
        shutil.copy(self.fakepy, os.path.join(self.venv, "bin", "python"))
        _write(os.path.join(self.venv, "bin", "pip"), f"""#!/bin/sh
echo "$*" >> {self.pip_log}
if [ "$1" = freeze ]; then echo "requests==2.31.0"; fi
exit 0
""", x=True)

    def _repo(self):
        self._git(self.d, "init", "-q", "--bare", self.origin)
        os.makedirs(self.work)
        self._git(self.work, "init", "-q")
        self._git(self.work, "symbolic-ref", "HEAD", "refs/heads/main")
        _write(os.path.join(self.work, "README.md"), "# kal\n")
        _write(os.path.join(self.work, "requirements.txt"), "requests\n")
        _write(os.path.join(self.work, "docs", "JOURNAL.md"), "# journal\n")
        _write(os.path.join(self.work, "tests", "placeholder"), "x\n")
        _write(os.path.join(self.work, "kal", "x.py"), "x = 1\n")
        self._git(self.work, "add", "-A")
        self._git(self.work, "commit", "-q", "-m", "base")
        self._git(self.work, "remote", "add", "origin", self.origin)
        self._git(self.work, "push", "-q", "origin", "main")
        self._git(self.d, "clone", "-q", "-b", "main", self.origin, self.app)

    def _env(self):
        return dict(APP=self.app, VENV=self.venv, DATA=self.data, ETC=self.etc,
                    STAGE_USER="nobody", STAGE_PYTHON=self.fakepy, KAL_UPDATE_WAIT="0",
                    PATH=self.bin + ":/usr/bin:/bin")

    def _run(self):
        return subprocess.run(["bash", UPDATE], env=dict(os.environ, **self._env()),
                              capture_output=True, text=True)

    def _push_code(self, email="noreply@github.com", name="GitHub", fail=False):
        _write(os.path.join(self.work, "kal", "x.py"), "x = 2\n")
        if fail:
            _write(os.path.join(self.work, "tests", "FAIL"), "1\n")
        self._git(self.work, "add", "-A")
        self._git(self.work, "commit", "-q", "-m", "code", email=email, name=name)
        self._git(self.work, "push", "-q", "origin", "main")

    def _push_docs(self):
        _write(os.path.join(self.work, "docs", "JOURNAL.md"), "# journal\n\nentry\n")
        self._git(self.work, "add", "-A")
        self._git(self.work, "commit", "-q", "-m", "journal",
                  email="noreply@anthropic.com", name="Claude")
        self._git(self.work, "push", "-q", "origin", "main")

    def _head(self):
        return self._git(self.app, "rev-parse", "HEAD")

    def _active(self):
        open(self.active, "w").close()

    # ---- the tests ----
    def test_docs_only_fast_forwards_without_tests_or_restart(self):
        before = self._head()
        self._push_docs()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(self._head(), before)            # it advanced
        self.assertNotIn("try-restart", _read(self.sysctl_log))  # no restart (finding 3)
        self.assertEqual(_read(self.curl_log), "")           # no alert
        self.assertEqual(_read(self.pip_log), "")            # no pip, no tests

    def test_code_without_a_pull_request_is_refused(self):
        before = self._head()
        self._push_code(email="noreply@anthropic.com", name="Claude")  # a stray check-in push
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._head(), before)               # nothing changed
        self.assertIn("kal update refused", _read(self.curl_log))
        self.assertNotIn("try-restart", _read(self.sysctl_log))
        self.assertEqual(_read(os.path.join(self.data, "update_failed")).strip(),
                         self._git(self.app, "rev-parse", "origin/main"))

    def test_pull_request_code_passes_tests_then_restarts(self):
        self._active()
        before = self._head()
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(self._head(), before)
        self.assertIn("try-restart kal", _read(self.sysctl_log))
        log = _read(self.curl_log)
        self.assertIn("kal updated", log)
        self.assertIn("Priority: low", log)                  # no-action alert is quiet (finding 5)
        plog = _read(self.pip_log)
        self.assertIn("freeze", plog)                        # saved before updating (finding 4)
        self.assertLess(plog.index("freeze"), plog.index("install"))

    def test_failed_tests_never_touch_the_live_tree_or_venv(self):
        self._active()
        before = self._head()
        self._push_code(fail=True)
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._head(), before)               # not swapped in (finding 1)
        self.assertIn("failed its tests", _read(self.curl_log))
        self.assertNotIn("try-restart", _read(self.sysctl_log))
        self.assertNotIn("install", _read(self.pip_log))     # live venv untouched (finding 4)

    def test_unhealthy_after_restart_rolls_back_code_and_packages(self):
        self._active()
        open(os.path.join(self.data, "health_fail"), "w").close()
        before = self._head()
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._head(), before)               # rolled back (finding 2)
        log = _read(self.curl_log)
        self.assertIn("rolled back", log)
        self.assertIn("Priority: high", log)                 # action needed: loud (finding 5)
        self.assertIn("freeze-before.txt", _read(self.pip_log))  # exact old versions (finding 4)

    def test_restart_loop_rolls_back(self):
        self._active()
        open(self.loop, "w").close()   # NRestarts climbs on restart
        before = self._head()
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._head(), before)
        self.assertIn("rolled back", _read(self.curl_log))

    def test_stopped_service_is_not_started_by_an_update(self):
        # Owner stopped it on purpose (no ACTIVE flag): update the code, leave it stopped.
        before = self._head()
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(self._head(), before)
        self.assertNotIn("try-restart", _read(self.sysctl_log))

    def test_alert_uses_the_configured_ntfy_server(self):
        self._active()
        _write(os.path.join(self.etc, "kal.env"),
               "KAL_NTFY_TOPIC=kal-xyz\nKAL_NTFY_SERVER=http://my.ntfy.box\n")
        self._push_code()
        self._run()
        self.assertIn("http://my.ntfy.box/kal-xyz", _read(self.curl_log))

    def test_local_changes_block_the_update(self):
        before = self._head()
        # a local commit on the server's app that isn't on origin -> not a fast-forward
        _write(os.path.join(self.app, "kal", "local.py"), "y = 1\n")
        self._git(self.app, "add", "-A")
        self._git(self.app, "commit", "-q", "-m", "local", email="me@home", name="Me")
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("kal update skipped", _read(self.curl_log))


if __name__ == "__main__":
    unittest.main()
