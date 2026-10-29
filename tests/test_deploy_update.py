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
CHECKIN = {"email": "noreply@anthropic.com", "name": "Check-in"}   # a check-in session, not GitHub

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

    def _checkin(self):
        """A check-in's own copy of the repo, cloned now."""
        path = os.path.join(self.d, "checkin")
        self._git(self.d, "clone", "-q", "-b", "main", self.origin, path)
        return path

    def _journal(self, repo):
        _write(os.path.join(repo, "docs", "JOURNAL.md"), "# journal\n\nentry\n")
        self._git(repo, "commit", "-q", "-am", "journal", **CHECKIN)

    def _head(self):
        return self._git(self.app, "rev-parse", "HEAD")

    def _active(self):
        open(self.active, "w").close()

    def _refused(self, before):
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(self._head(), before)               # nothing changed
        self.assertIn("kal update refused", _read(self.curl_log))
        self.assertNotIn("try-restart", _read(self.sysctl_log))

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

    def test_a_github_merge_commit_still_updates(self):
        # GitHub's merge button can make a real merge commit. The pull request's own commits
        # aren't GitHub's, and that's fine: GitHub made the merge.
        before = self._head()
        self._git(self.work, "checkout", "-q", "-b", "pr")
        _write(os.path.join(self.work, "kal", "x.py"), "x = 2\n")
        self._git(self.work, "commit", "-q", "-am", "code", **CHECKIN)
        self._git(self.work, "checkout", "-q", "main")
        self._git(self.work, "merge", "-q", "--no-ff", "--no-edit", "pr")
        self._git(self.work, "push", "-q", "origin", "main")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotEqual(self._head(), before)

    def test_a_branch_merged_by_hand_is_refused(self):
        # A merge commit lists no files of its own, so the check skipped it, and the code on
        # the merged branch went live without a pull request (audit round 3).
        self._active()
        before = self._head()
        self._git(self.work, "checkout", "-q", "-b", "feature")
        _write(os.path.join(self.work, "kal", "x.py"), "x = 'UNREVIEWED'\n")
        self._git(self.work, "commit", "-q", "-am", "code", **CHECKIN)
        self._git(self.work, "checkout", "-q", "main")
        self._push_docs()   # main moves on, so the merge below is a real merge commit
        self._git(self.work, "merge", "-q", "--no-edit", "feature", **CHECKIN)
        self._git(self.work, "push", "-q", "origin", "main")
        self._refused(before)

    def test_a_pull_that_merges_in_a_pull_request_still_updates(self):
        # A check-in pulled with a merge before pushing its journal. That merge brings in only
        # what GitHub merged, so this is fine.
        self._active()
        before = self._head()
        checkin = self._checkin()
        self._push_code()   # GitHub merges a pull request meanwhile
        self._journal(checkin)
        self._git(checkin, "pull", "-q", "--no-rebase", "--no-edit", "origin", "main", **CHECKIN)
        self._git(checkin, "push", "-q", "origin", "main")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotEqual(self._head(), before)
        self.assertIn("try-restart kal", _read(self.sysctl_log))
        self.assertIn("kal updated", _read(self.curl_log))

    def test_a_pull_merge_with_code_of_its_own_is_refused(self):
        # The same pull, but the merge itself also changes code (say, a conflict fixed by hand).
        before = self._head()
        checkin = self._checkin()
        self._push_code()
        self._journal(checkin)
        self._git(checkin, "pull", "-q", "--no-rebase", "--no-commit", "origin", "main", **CHECKIN)
        _write(os.path.join(checkin, "kal", "y.py"), "y = 'UNREVIEWED'\n")
        self._git(checkin, "add", "-A")
        self._git(checkin, "commit", "-q", "--no-edit", **CHECKIN)
        self._git(checkin, "push", "-q", "origin", "main")
        self._refused(before)

    def test_a_merge_that_puts_old_code_back_is_refused(self):
        # A merge of a docs-only branch that takes that branch's older code: it would undo a
        # pull request the server already runs.
        checkin = self._checkin()
        self._push_code()
        self.assertEqual(self._run().returncode, 0)   # the server runs the new code
        before = self._head()
        self._git(checkin, "checkout", "-q", "-b", "notes")
        _write(os.path.join(checkin, "docs", "NOTES.md"), "notes\n")
        self._git(checkin, "add", "-A")
        self._git(checkin, "commit", "-q", "-m", "notes", **CHECKIN)
        self._git(checkin, "checkout", "-q", "main")
        self._git(checkin, "pull", "-q", "--ff-only", "origin", "main")
        self._git(checkin, "merge", "-q", "--no-commit", "notes", **CHECKIN)
        self._git(checkin, "checkout", "notes", "--", "kal/x.py")
        self._git(checkin, "commit", "-q", "--no-edit", **CHECKIN)
        self._git(checkin, "push", "-q", "origin", "main")
        self._refused(before)

    def _runs_new_code(self):
        self._push_code()
        self.assertEqual(self._run().returncode, 0)   # the server runs x = 2
        self.assertEqual(_read(os.path.join(self.app, "kal", "x.py")), "x = 2\n")
        return self._head()

    def test_a_pull_that_keeps_its_own_old_code_is_refused(self):
        # `pull -s ours` (meant: -X ours, for a journal conflict) keeps the check-in's older
        # copy of the code. It would undo the pull request the server runs (critic, round 3).
        checkin = self._checkin()
        before = self._runs_new_code()
        self._journal(checkin)
        self._git(checkin, "pull", "-q", "--no-rebase", "--no-edit", "-s", "ours", "origin", "main", **CHECKIN)
        self._git(checkin, "push", "-q", "origin", "main")
        self._refused(before)
        self.assertEqual(_read(os.path.join(self.app, "kal", "x.py")), "x = 2\n")

    def test_the_same_merge_made_from_the_other_side_is_refused(self):
        # Old code back, with the stale branch as the first parent this time.
        checkin = self._checkin()
        before = self._runs_new_code()
        self._git(checkin, "checkout", "-q", "-b", "notes")
        _write(os.path.join(checkin, "docs", "NOTES.md"), "notes\n")
        self._git(checkin, "add", "-A")
        self._git(checkin, "commit", "-q", "-m", "notes", **CHECKIN)
        self._git(checkin, "fetch", "-q", "origin")
        self._git(checkin, "merge", "-q", "--no-commit", "origin/main", **CHECKIN)
        self._git(checkin, "checkout", "notes", "--", "kal/x.py")
        self._git(checkin, "commit", "-q", "--no-edit", **CHECKIN)
        self._git(checkin, "push", "-q", "origin", "HEAD:main")
        self._refused(before)

    def test_a_merge_of_three_that_drops_one_is_refused(self):
        # One merge of the journal, a docs branch and main that keeps the old code: the docs
        # branch brought no code, but main did, and it would be undone.
        checkin = self._checkin()
        before = self._runs_new_code()
        self._git(checkin, "checkout", "-q", "-b", "notes")
        _write(os.path.join(checkin, "docs", "NOTES.md"), "notes\n")
        self._git(checkin, "add", "-A")
        self._git(checkin, "commit", "-q", "-m", "notes", **CHECKIN)
        self._git(checkin, "checkout", "-q", "main")
        self._journal(checkin)
        self._git(checkin, "fetch", "-q", "origin")
        self._git(checkin, "merge", "-q", "--no-edit", "-s", "ours", "notes", "origin/main", **CHECKIN)
        self.assertEqual(len(self._git(checkin, "rev-list", "--parents", "-n", "1", "HEAD").split()), 4)
        self._git(checkin, "push", "-q", "origin", "main")
        self._refused(before)
        self.assertEqual(_read(os.path.join(self.app, "kal", "x.py")), "x = 2\n")

    def _session_pushes_journal(self, repo, name, sneak=False):
        """Another session: its journal, a pull with a merge, a push."""
        _write(os.path.join(repo, "docs", name), name + "\n")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-q", "-m", "journal " + name, **CHECKIN)
        if sneak:   # a conflict "fixed" with new code in the merge
            self._git(repo, "pull", "-q", "--no-rebase", "--no-commit", "origin", "main", **CHECKIN)
            _write(os.path.join(repo, "kal", "sneak.py"), "s = 1\n")
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-q", "--no-edit", **CHECKIN)
        else:
            self._git(repo, "pull", "-q", "--no-rebase", "--no-edit", "origin", "main", **CHECKIN)
        self._git(repo, "push", "-q", "origin", "main")

    def _sessions(self, *names):
        out = []
        for n in names:
            path = os.path.join(self.d, n)
            self._git(self.d, "clone", "-q", "-b", "main", self.origin, path)
            out.append(path)
        return out

    def test_two_sessions_pulling_in_turn_still_update(self):
        # Session B's pull brings in session A's pull merge. Both bring in only what GitHub
        # merged, so this updates. Refusing it blocked every later update (critic, round 3).
        self._active()
        before = self._head()
        a, b, c = self._sessions("a", "b", "c")
        self._push_code()
        self._session_pushes_journal(a, "A.md")
        self._session_pushes_journal(b, "B.md")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotEqual(self._head(), before)
        self.assertEqual(_read(os.path.join(self.app, "kal", "x.py")), "x = 2\n")
        self._session_pushes_journal(c, "C.md")   # the server already runs the code: docs only
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("docs only", r.stdout)

    def test_code_hidden_in_an_earlier_sessions_pull_is_refused(self):
        self._active()
        before = self._head()
        a, b = self._sessions("a", "b")
        self._push_code()
        self._session_pushes_journal(a, "A.md", sneak=True)
        self._session_pushes_journal(b, "B.md")
        self._refused(before)

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
        # The research lock exists for the wait, made as the kal user (research opens it to
        # write; a root-owned one would break research for good).
        self.assertTrue(os.path.exists(os.path.join(self.data, ".research.lock")))

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
        self.assertEqual(_read(os.path.join(self.data, "update_failed")).strip(),   # not retried daily
                         self._git(self.app, "rev-parse", "origin/main"))

    def test_restart_loop_rolls_back(self):
        self._active()
        open(self.loop, "w").close()   # NRestarts climbs on restart
        before = self._head()
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._head(), before)
        self.assertIn("rolled back", _read(self.curl_log))

    def test_a_long_research_run_delays_the_restart_but_never_skips_it(self):
        import fcntl
        self._active()
        self._push_code()
        lock = open(os.path.join(self.data, ".research.lock"), "w")
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX)   # research is running
        r = subprocess.run(["bash", UPDATE], env=dict(os.environ, **self._env(), KAL_RESEARCH_WAIT="1"),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("try-restart kal", _read(self.sysctl_log))   # restarted anyway
        self.assertIn("kal updated", _read(self.curl_log))

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
