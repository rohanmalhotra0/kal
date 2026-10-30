"""Behavioural tests for deploy/kal-update.sh, run against a sandbox.

The real script runs in a temp dir with a local bare git "origin", a stub git history,
and fake systemctl/runuser/curl/python on PATH that log their calls. No root, no network:
tests pass here and on GitHub Actions. APP/VENV/DATA/ETC and a few knobs are env vars that
default to the real server paths, so overriding them can't change production behaviour.

The fake systemctl is a small model of systemd 255 (checked against a real systemd 255
user manager): the bot's state moves on a fake clock that only the fake `sleep` moves, so
the script runs with its real waits in no time. What the bot does depends on the code it
started with, as on the server.
"""
import json
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
     [ -x {hook} ] && {hook}   # something happens while the tests run (a person acts, say)
     [ -e tests/FAIL ] && exit 1
     exit 0 ;;
  *"-m kal health"*)
     if [ -n "$KAL_HOME" ] && [ -e "$KAL_HOME/health_fail" ]; then exit 1; fi
     grep -q UNHEALTHY {state} 2>/dev/null && exit 1   # the code running now is unhealthy
     exit 0 ;;
  *) exit 0 ;;
esac
"""

# systemd 255, for one unit (kal), as kal-update.sh sees it:
# - every start is a new InvocationID (automatic restarts too);
# - a start runs the code on disk then; code with CRASH crashes a second after it starts,
#   EXIT0 exits cleanly (Restart=always starts it again), LATE crashes once, late_after
#   seconds after its first start, and CHILD's crash leaves a child that takes `cleanup`
#   seconds to stop. With the file ALL_CRASH, every start crashes (a cause outside the code);
# - a crash sets Result=exit-code; the bot waits restart_delay seconds ("activating",
#   SubState auto-restart), then starts again and NRestarts goes up by one;
# - a start resets Result. A manual (re)start of a running or stopped bot starts NRestarts
#   from 0; try-restart while it waits to restart after a crash keeps it;
# - try-restart restarts a running bot, or one waiting to restart; it does nothing to one
#   that is stopped or being stopped ("deactivating"), as systemd does.
# A person acts with `person-stop`, `person-restart SECS` (the stop takes SECS),
# `person-start-at SECS` and `person-restart-at SECS`. A stopped bot keeps its last
# InvocationID (kal is enabled, so systemd keeps the unit loaded).
FAKE_SYSTEMCTL = """#!/usr/bin/env python3
import json, os, sys
D, APP = {d!r}, {app!r}
S = os.path.join(D, "sd.json")
with open(os.path.join(D, "systemctl.log"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
with open(S) as f:
    s = json.load(f)


def code():
    try:
        with open(os.path.join(APP, "kal", "x.py")) as f:
            return f.read()
    except OSError:
        return ""


def at(secs, what):   # from now: the time of the event being applied, else the clock
    s["events"].append([s.get("t", s["clock"]) + secs, what])


def start(kind):   # manual: count from 0; auto: one more; keep: as it is
    s["inv"] += 1
    s["result"] = "success"
    s["nrest"] = {{"manual": 0, "auto": s["nrest"] + 1, "keep": s["nrest"]}}[kind]
    s["code"] = code()
    s["state"], s["sub"] = "active", "running"
    s["events"] = [e for e in s["events"] if e[1] in ("person_start",)]
    if "LATE" in s["code"] and not s.get("late_done"):
        s["late_done"] = True
        at(s["late_after"], "crash")
    elif "CRASH" in s["code"] or os.path.exists(os.path.join(D, "ALL_CRASH")):
        at(1, "crash")
    elif "EXIT0" in s["code"]:
        at(1, "exit0")


def wait_to_restart():
    s["state"], s["sub"] = "activating", "auto-restart"
    at(s["restart_delay"], "auto")


def apply(what):
    if what == "crash":
        s["result"] = "exit-code"
        if "CHILD" in s["code"]:
            s["state"], s["sub"] = "deactivating", "stop-sigterm"
            at(s["cleanup"], "cleaned")
        else:
            wait_to_restart()
    elif what in ("exit0", "cleaned"):
        wait_to_restart()
    elif what == "auto":
        start("auto")
    elif what == "stopped":
        s["state"], s["sub"] = "inactive", "dead"
    elif what == "restarted":
        start("manual")
    elif what == "person_start" and s["state"] == "inactive":
        start("manual")
    elif what == "person_stop":
        person_stop(0)


def advance():
    while True:
        due = sorted(e for e in s["events"] if e[0] <= s["clock"])
        if not due:
            s["t"] = s["clock"]
            return
        s["events"].remove(due[0])
        s["t"] = due[0][0]
        apply(due[0][1])


def person_stop(secs):
    s["events"] = [e for e in s["events"] if e[1] == "person_start"]
    s["state"], s["sub"] = "deactivating", "stop-sigterm"
    at(secs, "stopped")


a = sys.argv[1:]
advance()
out, rc = None, 0
if a[0] == "sleep":
    s["clock"] += float(a[1])
    advance()
elif a[0] == "is-active":
    out = s["state"]
    rc = 0 if out == "active" else 3
    if "--quiet" in a:
        out = None
elif a[0] == "show":
    out = {{"InvocationID": "inv%d" % s["inv"], "NRestarts": s["nrest"], "SubState": s["sub"],
           "Result": s["result"], "ActiveState": s["state"]}}[a[a.index("-p") + 1]]
elif a[0] == "try-restart" and s.get("try_fails", 0) > 0:
    s["try_fails"] -= 1   # systemctl failed (a D-Bus timeout, say): nothing restarted
    rc = 1
elif a[0] == "try-restart":
    if s["state"] in ("active", "activating"):
        start("manual" if s["state"] == "active" else "keep")
        if os.path.exists(os.path.join(D, "STOP_AFTER")):   # a person stops it right after
            at(1, "person_stop")
elif a[0] == "person-stop":
    person_stop(0)
elif a[0] == "person-restart":
    person_stop(float(a[1]))
    s["events"] = [e for e in s["events"] if e[1] != "stopped"]
    at(float(a[1]), "restarted")
elif a[0] == "person-start-at":
    at(float(a[1]), "person_start")
elif a[0] == "person-restart-at":
    at(float(a[1]), "restarted")
advance()
with open(S, "w") as f:
    json.dump(s, f)
if out is not None:
    print(out)
sys.exit(rc)
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
        self.state = os.path.join(self.d, "sd.json")   # the fake systemd's state
        self.hook = os.path.join(self.d, "hook.sh")      # runs while the update's tests run
        self.stop_after = os.path.join(self.d, "STOP_AFTER")   # a person stops it right after the restart
        self.all_crash = os.path.join(self.d, "ALL_CRASH")     # every start crashes (outside cause)
        self._sd(state="inactive", sub="dead")
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
        _write(os.path.join(self.bin, "systemctl"), FAKE_SYSTEMCTL.format(d=self.d, app=self.app), x=True)
        _write(os.path.join(self.bin, "sleep"), f'#!/bin/sh\nexec {self.bin}/systemctl sleep "$1"\n', x=True)
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
        _write(self.fakepy, FAKE_PY.format(hook=self.hook, state=self.state), x=True)
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
                    STAGE_USER="nobody", STAGE_PYTHON=self.fakepy,
                    PATH=self.bin + ":/usr/bin:/bin")   # real waits: only the fake clock moves

    def _run(self):
        return subprocess.run(["bash", UPDATE], env=dict(os.environ, **self._env()),
                              capture_output=True, text=True)

    def _push_code(self, email="noreply@github.com", name="GitHub", fail=False, code="x = 2"):
        _write(os.path.join(self.work, "kal", "x.py"), code + "\n")
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

    def _sd(self, **kw):
        """Set the fake systemd's state (it starts a stopped bot on the clock's 0)."""
        try:
            with open(self.state) as f:
                sd = json.load(f)
        except OSError:
            sd = dict(clock=0, state="inactive", sub="dead", result="success", inv=1, nrest=0,
                      code="", events=[], restart_delay=5, late_after=30, cleanup=10)
        sd.update(kw)
        with open(self.state, "w") as f:
            json.dump(sd, f)

    def _sd_now(self):
        with open(self.state) as f:
            return json.load(f)

    def _active(self, **kw):
        """The old version runs."""
        self._sd(state="active", sub="running", code=_read(os.path.join(self.app, "kal", "x.py")), **kw)

    def _during_tests(self, *commands):
        """Run these fake-systemctl commands (a person acting, say) while the update tests."""
        _write(self.hook, "#!/bin/sh\n" + "".join(f"{self.bin}/systemctl {c}\n" for c in commands), x=True)

    def _blamed(self):
        return os.path.exists(os.path.join(self.data, "update_failed"))

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

    def test_an_update_leaves_no_code_or_package_others_can_change(self):
        # The bot runs this code with the key. A venv made world-writable once (a umask of
        # 000 when it was installed) is put right by the next update.
        import stat
        self._active()
        for p in (self.app, os.path.join(self.app, "kal"), self.venv, os.path.join(self.venv, "bin", "pip")):
            os.chmod(p, 0o777)
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        loose = [p for top in (self.app, self.venv) for p, _, fs in os.walk(top)
                 for p in [p] + [os.path.join(p, f) for f in fs]
                 if not os.path.islink(p) and os.stat(p).st_mode & (stat.S_IWGRP | stat.S_IWOTH)]
        self.assertEqual(loose, [])

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
        before = self._head()
        self._push_code(code="x = 'UNHEALTHY'")
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._head(), before)               # rolled back (finding 2)
        log = _read(self.curl_log)
        self.assertIn("rolled back", log)
        self.assertIn("Priority: high", log)                 # action needed: loud (finding 5)
        self.assertIn("freeze-before.txt", _read(self.pip_log))  # exact old versions (finding 4)
        self.assertEqual(_read(os.path.join(self.data, "update_failed")).strip(),   # not retried daily
                         self._git(self.app, "rev-parse", "origin/main"))
        self.assertEqual(self._sd_now()["code"], "x = 1\n")   # the old version runs again

    def test_restart_loop_rolls_back(self):
        self._active()
        before = self._head()
        self._push_code(code="x = 'CRASH'")
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._head(), before)
        self.assertIn("rolled back", _read(self.curl_log))
        self.assertIn("crashed", _read(self.curl_log))
        self.assertTrue(self._blamed())                      # the old version runs fine: to blame
        self.assertEqual(self._sd_now()["state"], "active")

    def test_a_crash_after_earlier_crashes_still_rolls_back(self):
        # Two crashes since the last manual start, then the new version crashes: the update's
        # restart started the count from 0, so it can read fewer than the 2 before it.
        self._active(nrest=2)
        before = self._head()
        self._push_code(code="x = 'CRASH'")
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._head(), before)
        self.assertIn("rolled back", _read(self.curl_log))

    # One test per sign of a crash: each is the only one showing when the check looks.
    def test_a_crash_seen_only_as_a_rise_in_restarts_rolls_back(self):
        # It crashed once during the check and runs again: only NRestarts shows it.
        self._active()
        self._sd(late_after=60)
        self._push_code(code="x = 'LATE'")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("The new version crashed.", _read(self.curl_log))

    def test_a_crash_seen_only_as_waiting_to_restart_rolls_back(self):
        # It exits cleanly (Result stays success) and waits to be started again.
        self._active()
        self._sd(restart_delay=300)
        self._push_code(code="x = 'EXIT0'")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("The new version crashed.", _read(self.curl_log))

    def test_a_crash_still_cleaning_up_when_checked_rolls_back(self):
        # Crashed near the end of the check; a child still stops ("deactivating"), and the
        # count hasn't moved: only Result shows it. Before, this read as a person's stop.
        self._active()
        self._sd(late_after=118, cleanup=30)
        self._push_code(code="x = 'LATE CHILD'")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("The new version crashed.", _read(self.curl_log))

    def test_a_restart_while_it_waited_to_restart_is_not_a_crash(self):
        # systemd keeps the count when the update's restart lands while the old version waits
        # to restart after a crash. A healthy new version must not be rolled back for it.
        self._sd(state="activating", sub="auto-restart", result="exit-code", nrest=3,
                 events=[[300, "auto"]])
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("try-restart kal", _read(self.sysctl_log))   # checked, not "left stopped"
        self.assertIn("kal updated", _read(self.curl_log))

    def test_a_crash_looping_bot_still_gets_the_check(self):
        # It reads "activating" while it waits to restart: before, the new code went live
        # unchecked ("left it stopped"). Every start crashes (a port taken, say), the old
        # version's too after the rollback: so the new one isn't blamed, it is tried again.
        open(self.all_crash, "w").close()
        self._sd(state="activating", sub="auto-restart", result="exit-code", nrest=4,
                 events=[[300, "auto"]])
        before = self._head()
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._head(), before)
        self.assertIn("The old version fails too", _read(self.curl_log))
        self.assertFalse(self._blamed())

    def test_a_cause_that_begins_during_the_update_doesnt_blame_the_new_version(self):
        # Healthy when the update starts; then every fresh start crashes (a kal.env edit not
        # yet restarted, say). The rollback shows the old version fails too.
        self._active()
        _write(self.hook, f"#!/bin/sh\ntouch {self.all_crash}\n", x=True)
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("The old version fails too", _read(self.curl_log))
        self.assertFalse(self._blamed())

    def test_a_check_failing_on_the_old_version_after_the_rollback_too_doesnt_blame(self):
        self._active()
        _write(self.hook, f"#!/bin/sh\ntouch {self.data}/health_fail\n", x=True)
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("wasn't healthy. The old version fails too", _read(self.curl_log))
        self.assertFalse(self._blamed())

    def test_a_check_that_failed_before_the_update_never_rolls_it_back(self):
        # Damaged books (say) fail the check before and after: not the new version's fault.
        self._active()
        open(os.path.join(self.data, "health_fail"), "w").close()
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("kal updated", _read(self.curl_log))

    def test_a_person_stopping_it_during_the_check_is_no_verdict(self):
        # Stopped (not crashed) during the check: the new code stays, unchecked, not blamed.
        self._active()
        open(self.stop_after, "w").close()
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._head(), self._git(self.app, "rev-parse", "origin/main"))
        self.assertIn("stopped during the update", _read(self.curl_log))
        self.assertFalse(self._blamed())

    def test_a_person_stopping_it_during_the_tests_is_no_verdict(self):
        self._active()
        self._during_tests("person-stop")
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("stopped during the update", _read(self.curl_log))
        self.assertEqual(self._sd_now()["state"], "inactive")   # left stopped
        self.assertFalse(self._blamed())

    def test_a_bot_being_stopped_when_the_update_starts_still_gets_the_check(self):
        self._sd(state="deactivating", sub="stop-sigterm", events=[[20, "restarted"]])
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("try-restart kal", _read(self.sysctl_log))
        self.assertIn("kal updated", _read(self.curl_log))

    def test_a_restart_by_a_person_under_way_is_waited_for_then_checked(self):
        # A person ran `systemctl restart kal` during the tests, and a trade run makes the stop
        # take 200 seconds: longer than the update's wait for the bot to be up and its check. try-restart does nothing to a bot being stopped, and the person's
        # restart then starts the new code: it must still be checked (a bad one rolled back).
        self._active()
        self._during_tests("person-restart 200")
        before = self._head()
        self._push_code(code="x = 'CRASH'")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(self._head(), before)
        self.assertIn("The new version crashed.", _read(self.curl_log))
        self.assertTrue(self._blamed())

    def test_a_restart_that_didnt_happen_rolls_back_without_blame(self):
        # systemctl failed: the old code still runs, and the new one would go live unchecked
        # at the next start. Back to the old code; tried again tomorrow.
        self._active(try_fails=2)
        before = self._head()
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(self._head(), before)
        self.assertIn("didn't restart", _read(self.curl_log))
        self.assertFalse(self._blamed())

    def test_a_restart_by_a_person_during_the_check_is_judged(self):
        # The update's restart failed, then a person restarted it during the check: the new
        # code runs, so it gets the verdict.
        self._active(try_fails=2)
        self._during_tests("person-restart-at 90")
        before = self._head()
        self._push_code(code="x = 'CRASH'")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(self._head(), before)
        self.assertIn("The new version crashed.", _read(self.curl_log))
        self.assertTrue(self._blamed())

    def test_a_start_by_a_person_during_the_check_is_judged(self):
        # Stopped by a person during the tests, so the update's restart does nothing, then
        # started by them during the check: the new code runs, so it gets the verdict.
        self._active()
        self._during_tests("person-stop", "person-start-at 100")
        before = self._head()
        self._push_code(code="x = 'CRASH'")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(self._head(), before)
        self.assertIn("The new version crashed.", _read(self.curl_log))

    def test_a_version_failing_again_with_the_old_one_failing_too_is_blamed(self):
        # A release that broke the bot's data makes the old version fail too: one more try,
        # then it is blamed.
        self._active()
        _write(self.hook, f"#!/bin/sh\ntouch {self.all_crash}\n", x=True)
        self._push_code()
        self.assertEqual(self._run().returncode, 1)
        self.assertFalse(self._blamed())                    # first time: tried again
        self.assertIn("tries again tomorrow", _read(self.curl_log))
        r = self._run()                                      # the next day, same version
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertTrue(self._blamed())
        self.assertIn("isn't tried again", _read(self.curl_log))

    def test_a_version_tried_again_that_works_is_kept(self):
        self._active()
        _write(self.hook, f"#!/bin/sh\ntouch {self.all_crash}\n", x=True)
        self._push_code()
        self.assertEqual(self._run().returncode, 1)
        os.remove(self.hook)
        os.remove(self.all_crash)                           # the outside cause is fixed
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("kal updated", _read(self.curl_log))
        self.assertFalse(os.path.exists(os.path.join(self.data, "update_retry")))

    def test_the_update_never_makes_the_research_lock_itself(self):
        # Made by root, research (as kal) couldn't open it again: if the kal user can't make
        # it, the restart goes ahead without it.
        _write(os.path.join(self.bin, "runuser"), """#!/bin/sh
shift 2 2>/dev/null
[ "$1" = "--" ] && shift
[ "$1" = touch ] && exit 1
exec "$@"
""", x=True)
        self._active()
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("try-restart kal", _read(self.sysctl_log))
        self.assertFalse(os.path.exists(os.path.join(self.data, ".research.lock")))

    def test_after_its_verdict_the_update_sets_where_kal_watch_counts_from(self):
        # A rollback that lands while the bad version waits to restart keeps the restart
        # count: without this, kal-watch would say the healthy old version keeps restarting.
        for code in ("x = 2", "x = 'CRASH'"):
            with self.subTest(code=code):
                self._active()
                self._push_code(code=code)
                self._run()
                sd = self._sd_now()
                self.assertEqual(_read(os.path.join(self.data, "watch_nrestarts")).split(),
                                 [str(sd["nrest"]), "inv%d" % sd["inv"]])

    def test_earlier_crashes_alone_never_roll_back(self):
        self._active(nrest=2)
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("kal updated", _read(self.curl_log))
        self.assertNotIn(".  http", _read(self.curl_log))   # no stray space after the message

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
        # a local commit on the server's app that isn't on origin -> not a fast-forward
        _write(os.path.join(self.app, "kal", "local.py"), "y = 1\n")
        self._git(self.app, "add", "-A")
        self._git(self.app, "commit", "-q", "-m", "local", email="me@home", name="Me")
        local = self._head()
        self._push_code()
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("kal update skipped", _read(self.curl_log))
        self.assertEqual(self._head(), local)   # still the server's own commit: nothing changed


if __name__ == "__main__":
    unittest.main()
