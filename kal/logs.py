"""Log lines go to a file that rotates, and to stderr.

A server left alone for months must not fill its disk with logs, so the file rotates
at 5 MB and keeps 5 old ones: 30 MB at most. stderr gets the same lines, for
systemd's journal and `docker logs`.

Times are UTC, like everything else kal shows. Secret values (the API key ID, tokens,
the dashboard password, the alert topic) are blanked out of every line, so the log is
safe to paste when asking for help.
"""
import collections
import json
import logging
import logging.handlers
import os
import re
import sys
import time

from . import config

MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5
LINES = 50
FORMAT = "%(asctime)s UTC %(levelname)s %(name)s: %(message)s"
DATE = "%Y-%m-%d %H:%M:%S"
QUIET = ("werkzeug",)   # a line per dashboard request would bury everything else
SECRET_ENV = ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KAL_GITHUB_TOKEN", "KAL_DASHBOARD_PASSWORD",
              "KAL_NTFY_TOPIC")
SECRET_FILES = ("kalshi_key_id", "ntfy_topic")   # what `kal setup` saves in $KAL_HOME
PEM = re.compile(r"-----BEGIN [A-Z ]+-----.*?(-----END [A-Z ]+-----|$)", re.S)
_files = {}   # path -> ((mtime, size), value): re-read only when the file changes


def path(home=None):
    return os.path.join(home or config.home(), "logs", "kal.log")


def _file_secret(file):
    try:
        st = os.stat(file)
    except OSError:
        return None
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _files.get(file)
    if hit is None or hit[0] != stamp:
        try:
            with open(file, encoding="utf-8", errors="replace") as f:
                hit = (stamp, f.read().strip())
        except OSError:
            hit = (stamp, None)
        _files[file] = hit
    return hit[1]


def secret_values():
    """Every secret value this process can know: env vars, plus the files `kal setup` writes."""
    values = [os.environ.get(var) for var in SECRET_ENV]
    try:
        home = config.home()
        values += [_file_secret(os.path.join(home, name)) for name in SECRET_FILES]
    except OSError:
        pass
    return [v for v in values if v and len(v) >= 6]


def scrub(text):
    """Remove any PEM block and every secret value. Belt and braces."""
    text = PEM.sub("[removed key]", text)
    for v in secret_values():
        text = text.replace(v, "[removed]")
        text = text.replace(json.dumps(v)[1:-1], "[removed]")  # as it looks inside JSON
    return text


class Formatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record):
        return scrub(super().format(record))


def setup(home=None, level=logging.INFO):
    """Log to $KAL_HOME/logs/kal.log (5 MB x 5) and to stderr. Calling it again adds nothing.

    Never raises: if the file can't be opened, stderr still works, and the bot matters
    more than its log. Returns the handlers it added."""
    root = logging.getLogger()
    root.setLevel(level)
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
    fmt = Formatter(FORMAT, DATE)
    added = []
    file = os.path.abspath(path(home))
    if not any(getattr(h, "baseFilename", None) == file for h in root.handlers):
        try:
            os.makedirs(os.path.dirname(file), mode=0o700, exist_ok=True)
            h = logging.handlers.RotatingFileHandler(file, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                                     encoding="utf-8")
        except OSError as e:
            print(f"kal: can't write the log file {file}: {e}", file=sys.stderr)
        else:
            h.setFormatter(fmt)
            root.addHandler(h)
            added.append(h)
    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(fmt)
        root.addHandler(h)
        added.append(h)
    return added


def _last(file, n):
    try:
        with open(file, encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in collections.deque(f, maxlen=n)]
    except OSError:
        return []


def tail(n=LINES, home=None):
    """The last n log lines, oldest first. Reads the last rotated file too when the current
    one is short, so a rotation never hides what happened just before it."""
    if n <= 0:
        return []
    file = path(home)
    lines = _last(file, n)
    if len(lines) < n:
        lines = _last(file + ".1", n - len(lines)) + lines
    return [scrub(line) for line in lines]
