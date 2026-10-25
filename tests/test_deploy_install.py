"""Behavioural tests for deploy/install.sh helpers, run against a sandbox.

install.sh exposes its small, risky pieces as `--emit-topic`, `--emit-wrapper` and
`--harden-ssh` so they can be tested without root or a full install. The generated `kal`
wrapper is run directly with stub id/runuser/python. No root, no network.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "deploy", "install.sh")


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
class InstallHelpers(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="kal-inst-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _emit(self, flag, env=None):
        return subprocess.run(["bash", INSTALL, flag],
                              env=dict(os.environ, **(env or {})),
                              capture_output=True, text=True)

    # ---- finding 8: the alert topic must be easy to type ----
    def test_topic_has_no_look_alike_characters(self):
        topic = self._emit("--emit-topic").stdout.strip()
        self.assertRegex(topic, r"^kal-[a-z2-9]{20}$")
        # the random part has no look-alike characters (the "kal-" prefix is fixed)
        self.assertEqual(set("l1o0") & set(topic[len("kal-"):]), set())

    # ---- finding 7: SSH password login off, but never lock the owner out ----
    def _harden(self, keys_content):
        sshd = os.path.join(self.d, "sshd_config.d")
        keys = os.path.join(self.d, "authorized_keys")
        if keys_content is not None:
            _write(keys, keys_content)
        bindir = os.path.join(self.d, "bin")
        _write(os.path.join(bindir, "systemctl"),
               f'#!/bin/sh\necho "$*" >> {os.path.join(self.d, "sysctl.log")}\nexit 0\n', x=True)
        env = dict(PATH=bindir + ":/usr/bin:/bin", KAL_SSHD_DIR=sshd, KAL_ROOT_AUTHKEYS=keys)
        r = self._emit("--harden-ssh", env)
        return sshd, r

    def test_ssh_password_login_off_when_root_has_a_key(self):
        sshd, r = self._harden("ssh-ed25519 AAAAC3Nz test@host\n")
        conf = _read(os.path.join(sshd, "10-kal.conf"))
        self.assertIn("PasswordAuthentication no", conf)
        self.assertIn("KbdInteractiveAuthentication no", conf)
        self.assertIn("reload", _read(os.path.join(self.d, "sysctl.log")))

    def test_ssh_left_alone_when_root_has_no_key(self):
        sshd, r = self._harden("# only a comment, no key\n")
        self.assertFalse(os.path.exists(os.path.join(sshd, "10-kal.conf")))
        self.assertIn("STILL ON", r.stdout)
        self.assertNotIn("reload", _read(os.path.join(self.d, "sysctl.log")))

    def test_ssh_left_alone_when_authorized_keys_missing(self):
        sshd, r = self._harden(None)
        self.assertFalse(os.path.exists(os.path.join(sshd, "10-kal.conf")))
        self.assertIn("STILL ON", r.stdout)

    # ---- a new server's own updates hold apt's locks: wait, don't stop ----
    def _apt(self, fails):
        """Run --apt-update with a fake apt-get that fails `fails` times, then works."""
        bindir = os.path.join(self.d, "bin")
        calls = os.path.join(self.d, "apt.log")
        _write(os.path.join(bindir, "apt-get"),
               f'#!/bin/sh\necho "$*" >> {calls}\n'
               f'n=$(wc -l < {calls})\n[ "$n" -gt {fails} ] && exit 0\n'
               'echo "E: Could not get lock /var/lib/apt/lists/lock" >&2\nexit 100\n', x=True)
        env = dict(PATH=bindir + ":/usr/bin:/bin", KAL_APT_TRIES="4", KAL_APT_PAUSE="0")
        r = self._emit("--apt-update", env)
        return r, _read(calls).splitlines()

    def test_apt_waits_while_ubuntu_updates_itself(self):
        r, calls = self._apt(fails=2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(calls), 3)
        for c in calls:   # every try also waits on the lock itself
            self.assertEqual(c, "-o DPkg::Lock::Timeout=600 update -qq")
        self.assertIn("Ubuntu is busy updating itself", r.stdout)

    def test_apt_gives_up_in_plain_words(self):
        r, calls = self._apt(fails=99)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(calls), 4)
        self.assertIn("STOP: apt-get update keeps failing. Wait 5 minutes", r.stderr)

    def test_no_apt_call_skips_the_wait(self):
        with open(INSTALL) as f:
            lines = [ln for ln in f if not ln.lstrip().startswith("#")]
        bare = [ln for ln in lines if re.search(r"^\s*(if\s+)?apt-get\s", ln)]
        self.assertEqual(bare, [])
        self.assertTrue(any(re.search(r"^\s*apt_get install .*python3-venv", ln) for ln in lines))

    # ---- finding 6: the kal wrapper must never die on a bad settings line ----
    def _wrapper(self):
        path = os.path.join(self.d, "kal")
        with open(path, "w") as f:
            f.write(self._emit("--emit-wrapper").stdout)
        os.chmod(path, 0o755)
        return path

    def _wrapper_env(self):
        bindir = os.path.join(self.d, "bin")
        app = os.path.join(self.d, "app")
        venv = os.path.join(self.d, "venv", "bin")
        for p in (bindir, app, venv):
            os.makedirs(p, exist_ok=True)
        _write(os.path.join(bindir, "id"), "#!/bin/sh\necho 0\n", x=True)  # pretend to be root
        _write(os.path.join(bindir, "runuser"),
               '#!/bin/sh\nshift 2 2>/dev/null\n[ "$1" = "--" ] && shift\nexec "$@"\n', x=True)
        _write(os.path.join(venv, "python"),
               f'#!/bin/sh\necho "$*" >> {os.path.join(self.d, "ran.log")}\n', x=True)
        return dict(PATH=bindir + ":/usr/bin:/bin",
                    KAL_ENV_FILE=os.path.join(self.d, "kal.env"),
                    KAL_APP=app, KAL_VENV=os.path.join(self.d, "venv"),
                    KAL_DATA=os.path.join(self.d, "data"))

    def test_off_still_runs_with_a_broken_settings_line(self):
        env = self._wrapper_env()
        # the exact shape that used to crash: an unquoted note after the value
        _write(env["KAL_ENV_FILE"],
               "KALSHI_API_KEY_ID=abc-123\n"
               "KAL_GITHUB_REPO=you/kal-status   (must be a private repo)\n"
               "a line with no equals\n"
               "lowercase=ignored\n")
        wrapper = self._wrapper()
        r = subprocess.run(["sh", wrapper, "off"], env=dict(os.environ, **env),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-m kal off", _read(os.path.join(self.d, "ran.log")))

    def test_wrapper_exports_only_valid_settings(self):
        env = self._wrapper_env()
        _write(env["KAL_ENV_FILE"], 'KAL_NTFY_TOPIC="kal-quoted"\n# comment\nbad=skipme\n')
        wrapper = self._wrapper()
        # a probe that prints one exported var, so we can see what the wrapper passed through
        _write(os.path.join(env["KAL_VENV"], "bin", "python"),
               f'#!/bin/sh\necho "topic=$KAL_NTFY_TOPIC bad=$bad" >> {os.path.join(self.d, "ran.log")}\n', x=True)
        r = subprocess.run(["sh", wrapper, "status"], env=dict(os.environ, **env),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = _read(os.path.join(self.d, "ran.log"))
        self.assertIn("topic=kal-quoted", line)   # quotes stripped
        self.assertIn("bad=", line)                # lowercase name never exported
        self.assertNotIn("skipme", line)


if __name__ == "__main__":
    unittest.main()
