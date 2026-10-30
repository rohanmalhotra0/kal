"""Upkeep for a server nobody watches: backups, pruning, and the disk and clock checks
that stop the bot before either can hurt it.

  backup()    copies each SQLite db with SQLite's own backup API (safe while the bot
              writes) to $KAL_HOME/backups/<UTC date>/, and keeps the newest 14 days.
  restore()   puts a backup back, and leaves the bot stopped: trades made after the
              backup are missing from those books, and the $300 limit is worked out
              from the books.
  prune()     deletes history older than 180 days that no money rule reads, then VACUUMs.
  disk_ok()   trips the bot under 200 MB free: a SQLite write that fails mid-trade could
              leave an order the books don't know about.
  clock_ok()  compares our clock with Kalshi's. Kalshi refuses signed requests with a
              wrong timestamp, so a basket could stop half-built.
  books_ok()  stops the bot if its books (kal.db) fail SQLite's check: every trade run
              would fail on them, and nothing else would say so.
  health()    disk, clock, books, trading and the status report: the scheduler runs it
              every 10 minutes. It also resends a stop alert that didn't get through.
  daily()     disk, backup, then prune: the scheduler runs it once a day.

A check result is a row: {"check", "ok", "level" ("ok" / "warn" / "fail"), "detail"}.
"""
import errno
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from . import config, db, notify, risk
from .client import API, BASES, PACER, Kalshi
from .logs import scrub

KEEP_DAYS = 14
PRUNE_DAYS = 180
DISK_TRIP_MB = 200
DISK_WARN_MB = 1024
DISK_TRIP_FILES = 5000  # room for this few more files (inodes) trips it, while the stop can still be saved
CLOCK_WARN = 5          # seconds off Kalshi's clock before we say so
CLOCK_TRIP = 30         # seconds off, on two checks in a row, before the bot stops
CLOCK_TRIES = 3         # readings when the first looks off (a cached reply can look old)
STRIKE_WINDOW = 1800    # the second bad clock reading must come within this many seconds
LOCK_WAIT = 300         # how long prune waits for a trade run to finish
ALIVE = 300             # a heartbeat older than this means the scheduler loop stopped
OUTAGE = 7200           # trade runs failing this long (network, Kalshi down) is worth a warning
REPORT_STALE = 10800    # 3 h with no status report reaching GitHub (a token is set) is worth a warning
REPORT_TRIES = 3        # ...once this many tries in a row failed (or they span those 3 h)
BACKUP_STALE_DAYS = 2
WARN_EVERY = 86400      # at most one phone alert a day for each warning
MB = 1024 * 1024
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BOOKS = ("orders", "settlements", "signals", "paper")   # tables a real kal db always has
TRIPS = "trips.jsonl"
HEARTBEAT = ".heartbeat"
BOOT_ID = "/proc/sys/kernel/random/boot_id"   # new at every boot (Linux)

log = logging.getLogger(__name__)
_trip_log = threading.Lock()
_clock_bad = {"ts": None}   # when the clock was last over CLOCK_TRIP, in this process


class MaintenanceError(RuntimeError):
    pass


class RestoreUnfinished(MaintenanceError):
    """A restore stopped once it had begun to swap the books: they may be the backup's now."""


def _row(check, level, detail, **extra):
    return {"check": check, "ok": level == "ok", "level": level, "detail": str(detail)[:300], **extra}


def ago(seconds):
    """'45 s', '12 min', '3 h', '2 days': how long ago, in plain words."""
    s = max(0.0, float(seconds))
    if s < 120:
        return f"{s:.0f} s"
    if s < 7200:
        return f"{s / 60:.0f} min"
    if s < 2 * 86400:
        return f"{s / 3600:.0f} h"
    return f"{s / 86400:.0f} days"


def _env():
    try:
        env = config.load().get("env")
    except Exception:
        env = None
    return env if env in BASES else "prod"


def _trip_once(reason):
    """Trip, unless the bot is already stopped. The first reason is the one to fix, and a
    trip every 10 minutes would bury it (and buzz the phone each time)."""
    try:
        if config.load().get("disabled_reason"):
            return False
    except Exception:
        pass  # can't read the config: trip anyway, trip() doesn't need a valid file
    config.trip(reason)
    return True


_sent_here = {}   # (KAL_HOME, key) -> when this process last sent it: when the books can't say


def _alert(key, title, message, priority="high", every=WARN_EVERY):
    """One phone alert per key per `every` seconds, so a lasting problem doesn't buzz all day.
    It goes even when the books can't be read (that may be the problem it reports): then
    this process remembers what it sent instead."""
    if not notify.enabled():
        return False
    try:
        sent = db.get("alerts", {}) or {}
        last = float(sent.get(key) or 0)
    except Exception:
        sent, last = None, 0.0
    last = max(last, float(_sent_here.get((config.home(), key)) or 0))   # the books may not take writes
    if time.time() - last < every:
        return False
    try:
        ok = notify.send(scrub(title), scrub(message), priority=priority, tags=("warning",))
    except Exception:
        log.warning("alert %s not sent", key, exc_info=True)
        return False
    if ok:
        _note_sent(key, sent)
    return ok


def _note_sent(key, sent=None):
    """Remember that an alert about `key` went: in this process, and in the books when they
    can be written."""
    now = _sent_here[(config.home(), key)] = time.time()
    try:
        sent = (db.get("alerts", {}) or {}) if sent is None else sent
        sent[key] = now
        db.put("alerts", sent)
    except Exception:
        log.warning("could not save that alert %s was sent", key, exc_info=True)


# ---------- SQLite files ----------

OPEN_WAIT = 30          # seconds to wait for a locked db before giving up


@contextmanager
def _open(path, **kw):
    c = sqlite3.connect(path, timeout=OPEN_WAIT, **kw)
    try:
        yield c
    finally:
        c.close()


def _dbs():
    """Every exchange's books that exist: kal.db (real money) and kal-demo.db. Only these:
    a copy the owner saved next to them is theirs, and is never pruned."""
    return sorted(p for p in (db.path(env) for env in BASES) if os.path.isfile(p))


def _problem(path):
    """None if `path` is a healthy kal database, else what's wrong. An empty file passes
    SQLite's own check, so the tables are checked too: restoring one would wipe the books."""
    if not os.path.isfile(path):
        return "no such file"
    try:
        with _open(path) as c:
            got = [r[0] for r in c.execute("PRAGMA quick_check")]
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.Error as e:
        return str(e)
    if got != ["ok"]:
        return "; ".join(map(str, got))[:200]
    missing = [t for t in BOOKS if t not in tables]
    return f"no {', '.join(missing)} table" if missing else None


DAMAGED = "the books are damaged"   # how every stop for damaged books begins (engine's too)


def books_ok(trip=True, quick=False):
    """This exchange's books (kal.db) open and pass SQLite's check. Damaged books (corrupt,
    or not a database) fail every trade run before it can buy, while the heartbeat stays
    fresh: so it stops the bot and says how to restore. Busy, locked or unreadable for a
    moment is only a warning: a backup wouldn't fix that, and would drop newer rows. None
    yet, or an empty file, is fine (the first run makes them). quick: open it and read its
    schema only, for the watchdog's frequent check."""
    p = db.path(_env())
    name = os.path.basename(p)
    if not os.path.isfile(p) or os.path.getsize(p) == 0:
        return _row("books", "ok", "none yet (the first run makes them)")
    try:
        with _open(p) as c:
            c.execute("SELECT count(*) FROM sqlite_master").fetchone()
            got = ["ok"] if quick else [r[0] for r in c.execute("PRAGMA quick_check")]
    except sqlite3.Error as e:
        if not db.damaged(e):
            return _row("books", "warn", f"couldn't check {name}: {e}. Not damaged: don't restore. "
                        f"It looks again in 10 minutes.", title="kal warning: books busy")
        got = [str(e)]
    if got == ["ok"]:
        return _row("books", "ok", f"{name} reads fine")
    text = (f"{name} is damaged ({'; '.join(map(str, got))[:200]}). Nothing can trade. "
            f"Put a backup back: {config.command('restore')}")
    # A trade run may have stopped the bot for this already: its alert said it.
    tripped = bool(trip and (_trip_once(f"{DAMAGED}: {text}") or _stopped_for(DAMAGED)))
    return _row("books", "fail", text, tripped=tripped)


def _stopped_for(prefix):
    try:
        return str(config.load().get("disabled_reason") or "").startswith(prefix)
    except Exception:
        return False


def _tmp(path):
    """A temp name only this process uses, so `kal backup` and the scheduler can't collide."""
    return f"{path}.{os.getpid()}.tmp"


def _copy(src, dst):
    """A consistent copy of a live db (SQLite's backup API), checked before it replaces dst,
    so a bad copy never overwrites a good one."""
    tmp = _tmp(dst)
    if os.path.exists(tmp):
        os.remove(tmp)
    with _open(src) as s, _open(tmp) as d:
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")   # one self-contained file, no -wal beside it
    problem = _problem(tmp)
    if problem:
        os.remove(tmp)
        raise MaintenanceError(f"the copy of {os.path.basename(src)} failed its check: {problem}")
    os.replace(tmp, dst)


@contextmanager
def _run_lock(wait):
    """risk.run_lock(), waiting up to `wait` seconds for a trade run to finish first."""
    deadline = time.monotonic() + wait
    while True:
        stack = ExitStack()
        try:
            stack.enter_context(risk.run_lock())
            break
        except risk.Locked:
            stack.close()
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
    with stack:
        yield


# ---------- backup / restore ----------

def backups_dir():
    return os.path.join(config.home(), "backups")


def _day(t):
    return datetime.fromtimestamp(t, timezone.utc).date().isoformat()


def day_start(day):
    """'2026-10-24' -> that UTC midnight as a timestamp, or None if it isn't a real date."""
    try:
        return datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def backups():
    """[(day, [db file names])] for every dated backup, newest first."""
    root = backups_dir()
    try:
        names = os.listdir(root)
    except OSError:
        return []
    out = []
    for d in sorted((n for n in names if DAY.match(n) and day_start(n) is not None), reverse=True):
        try:
            files = sorted(f for f in os.listdir(os.path.join(root, d)) if f.endswith(".db"))
        except OSError:
            continue
        if files:
            out.append((d, files))
    return out


def backup(now=None, keep=KEEP_DAYS, wait=LOCK_WAIT):
    """Copy every db to backups/<UTC date>/, then keep only the newest `keep` days.
    Returns {"day", "dir", "files", "days"}. Raises MaintenanceError if a copy fails;
    then nothing old is deleted, and a failed copy never replaces a good one.

    Copies only between trade runs (waiting up to `wait` seconds for one to finish; Locked
    after that). A copy taken between a basket's legs, put back later, read as a basket
    left half-built: a false stop (cross-PR money-path audit)."""
    with _run_lock(wait):
        return _backup(now, keep)


def _backup(now, keep):
    now = time.time() if now is None else now
    day, dbs = _day(now), _dbs()
    if not dbs:
        return {"day": day, "dir": None, "files": [], "days": [d for d, _ in backups()][::-1]}
    if shutil.disk_usage(config.home()).free < sum(os.path.getsize(p) for p in dbs) + DISK_TRIP_MB * MB:
        raise MaintenanceError("not enough free disk space for a backup")
    os.makedirs(backups_dir(), mode=0o700, exist_ok=True)
    dest = os.path.join(backups_dir(), day)
    os.makedirs(dest, mode=0o700, exist_ok=True)
    files, errors = [], []
    for p in dbs:
        try:
            _copy(p, os.path.join(dest, os.path.basename(p)))
            files.append(os.path.basename(p))
        except (OSError, sqlite3.Error, MaintenanceError) as e:
            errors.append(f"{os.path.basename(p)}: {e}")
    if errors:
        if not os.listdir(dest):
            os.rmdir(dest)   # an empty folder would count as a day and push out a good one
        raise MaintenanceError("; ".join(errors))
    days = [d for d, _ in backups()][::-1]   # oldest first
    for old in days[:-keep]:
        shutil.rmtree(os.path.join(backups_dir(), old), ignore_errors=True)
    return {"day": day, "dir": dest, "files": files, "days": days[-keep:]}


def restore(day, env=None, wait=120):
    """Put one exchange's books back from backups/<day>/. Returns {"db", "from", "saved"}.

    The bot is tripped first and stays stopped: trades made after the backup are missing
    from those books, so a person must check Kalshi before pressing Reset. The books as
    they were are kept in backups/before-restore-<time>/ (or beside them, if broken).
    RestoreUnfinished means it stopped part way: the books may be the backup's, so run it again."""
    if not isinstance(day, str) or not DAY.match(day):
        raise MaintenanceError("give the day as YYYY-MM-DD, like 2026-10-24")
    live = db.path(env or _env())
    name = os.path.basename(live)
    src = os.path.join(backups_dir(), day, name)
    if not os.path.isfile(src):
        raise MaintenanceError(f"there is no backup of {name} from {day}")
    problem = _problem(src)
    if problem:
        raise MaintenanceError(f"the {day} backup of {name} is damaged: {problem}")
    age = heartbeat_age()
    if (not os.path.exists(live) or _problem(live)) and age is not None and age <= ALIVE:
        # Broken books get swapped as files, which is only safe while nothing has them open.
        raise MaintenanceError("the books are broken, so the bot's service must be stopped first. "
                               "Run: sudo systemctl stop kal. Then this again. Then: sudo systemctl start kal")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    with _run_lock(wait):   # no trade run in flight, and none can start until we're done
        try:
            before = str(config.load().get("disabled_reason") or "")[:200]
        except Exception:
            before = ""
        earlier = f" (It had stopped before: {before})" if before else ""
        config.trip(f"Books restored from the {day} backup. When you press Reset, the bot first adds the "
                    "trades Kalshi shows since then." + earlier)
        again = "The books may be the backup's now. Run the restore again before you press Reset."
        try:
            saved = _put_back(src, live, name, stamp, day)
        except RestoreUnfinished as e:
            config.trip(f"Restoring the {day} backup stopped part way: {e}. {again}{earlier}")
            raise
        except Exception as e:
            config.trip(f"Restoring the {day} backup failed: {e}. The books were not changed.{earlier}")
            raise
        except BaseException:   # Ctrl-C: it may have stopped anywhere
            config.trip(f"Restoring the {day} backup was cut off. {again}{earlier}")
            raise
    return {"db": live, "from": src, "saved": saved}


def _waiting(c):
    """Where a catch-up still waiting in these books starts (their 'restored' row), else None.
    Unreadable: 0, so it looks at every fill Kalshi has."""
    kv = c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'kv'").fetchone()
    row = kv and c.execute("SELECT value FROM kv WHERE key = 'restored'").fetchone()
    if not row:
        return None
    try:
        return float(json.loads(row[0])["since"])
    except (TypeError, ValueError, KeyError):
        return 0


def _mark_restored(path, day, live=None):
    """Tell the next live run to bring the books up to date: every buy Kalshi shows since the
    backup's day (an hour early, for clock slack) that these books don't have gets added
    (ledger.catch_up). Until then the bot doesn't trade: the restore tripped it. A catch-up
    still waiting, in the backup or in the `live` books it replaces, keeps its earlier start."""
    since = day_start(day) - 3600
    waiting = []
    if live:
        with _open(live) as c:
            waiting.append(_waiting(c))
    with _open(path) as c:
        waiting.append(_waiting(c))
        since = min([since] + [w for w in waiting if w is not None])
        c.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('restored', ?)",
                  (json.dumps({"day": day, "since": since, "at": time.time()}),))
        c.commit()


def _put_back(src, live, name, stamp, day):
    """Swap the backup in for the live books. Returns where the old books went.

    The catch-up marker goes into a checked copy of the backup first, and only that copy is
    swapped in, in one step. So the backup's books are never there without the marker, which
    would drop every buy since the backup from the books. Until the swap begins the live
    books are untouched. A failure after that raises RestoreUnfinished."""
    healthy = os.path.exists(live) and not _problem(live)
    tmp = _tmp(live)
    try:
        for p in (tmp, tmp + "-journal"):   # left by a crash: an old journal would be played into the copy
            if os.path.exists(p):
                os.remove(p)
        shutil.copyfile(src, tmp)
        _mark_restored(tmp, day, live if healthy else None)
        problem = _problem(tmp)
        if problem:
            raise MaintenanceError(f"the copy of the backup failed its check: {problem}")
        if healthy:
            saved = os.path.join(backups_dir(), f"before-restore-{stamp}")
            os.makedirs(saved, mode=0o700, exist_ok=True)
            _copy(live, os.path.join(saved, name))
        elif os.path.exists(live):
            saved = f"{live}.broken-{stamp}"   # a second name keeps them: the books never go missing
            if not (os.path.exists(saved) and os.path.samefile(live, saved)):   # else a cut-off run did
                try:
                    os.link(live, saved)
                except OSError:
                    shutil.copyfile(live, saved)   # no links here, or the name is taken
        else:
            saved = None
        try:   # the swap: from here on the books may be the backup's
            if healthy:
                # Through SQLite, not a file copy, so a process that has the db open stays
                # consistent. One write: the books and the marker land together, or not at all.
                with _open(tmp) as s, _open(live) as d:
                    s.backup(d)
            else:
                for suffix in ("-wal", "-shm"):   # a stale -wal beside the new file would corrupt it
                    if os.path.exists(live + suffix):
                        os.replace(live + suffix, f"{live}{suffix}.broken-{stamp}")
                os.replace(tmp, live)   # one step, the marker already inside
            problem = _problem(live)
            if problem:
                raise MaintenanceError(f"the restored books failed their check: {problem}")
        except Exception as e:
            raise RestoreUnfinished(str(e)) from e
        return saved
    finally:
        try:
            os.remove(tmp)   # already gone if it was swapped in as a file
        except OSError:
            pass


# ---------- prune ----------

PRUNE_SQL = {
    "runs": "DELETE FROM runs WHERE ts < ?",
    "backtests": "DELETE FROM backtests WHERE ts < ?",
    # IS NOT NULL matters: NOT IN a list holding a NULL is never true, so nothing would go.
    "signals": """DELETE FROM signals WHERE ts < ?
        AND id NOT IN (SELECT signal_id FROM orders WHERE signal_id IS NOT NULL)
        AND id NOT IN (SELECT signal_id FROM paper WHERE signal_id IS NOT NULL)""",
    "equity": """DELETE FROM equity WHERE ts < ?
        AND rowid NOT IN (SELECT rowid FROM equity ORDER BY equity DESC LIMIT 1)
        AND rowid NOT IN (SELECT rowid FROM equity ORDER BY paper_equity DESC LIMIT 1)""",
}


def prune(now=None, days=PRUNE_DAYS, wait=LOCK_WAIT):
    """Delete history older than `days` from every db, then VACUUM. Returns rows deleted.

    Kept at any age, because the money rules read them:
    - orders, paper trades, settlements and kv: they are the books.
    - signals any order or paper trade points to: the ledger reads their event and
      locked-in flag to cap open positions, and promotion reads their edge.
    - the equity rows holding the highest equity and paper equity: bets stay smaller
      until equity is back at its peak, so losing the peak would make the bot bolder.
    Waits for a trade run to finish, so it never deletes under one."""
    cutoff = (time.time() if now is None else now) - days * 86400
    out = {}
    with _run_lock(wait):
        for path in _dbs():
            with _open(path) as c:
                counts = {table: c.execute(sql, (cutoff,)).rowcount for table, sql in PRUNE_SQL.items()}
                c.commit()
            if any(counts.values()) and _room_for_vacuum(path):
                with _open(path, isolation_level=None) as c:
                    c.execute("VACUUM")
                    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                counts["vacuumed"] = True
            out[os.path.basename(path)] = counts
    return out


def _room_for_vacuum(path):
    """VACUUM writes a full copy before it frees anything."""
    return shutil.disk_usage(config.home()).free > 2 * os.path.getsize(path) + DISK_TRIP_MB * MB


# ---------- disk / clock ----------

def _write_error(path):
    """None if a new file can be made in `path`, else the OSError. A disk remounted read-only
    after errors takes nothing: no order record, no heartbeat, no stop."""
    try:
        fd, p = tempfile.mkstemp(prefix=".write-check-", dir=path)
        os.close(fd)
        os.remove(p)
        return None
    except OSError as e:
        return e


def _free_files(path):
    """How many more files (inodes) the disk can take, or None where it has no fixed count
    (btrfs says 0 of 0)."""
    try:
        st = os.statvfs(path)
    except (OSError, AttributeError):   # AttributeError: no statvfs here (Windows)
        return None
    return st.f_favail if st.f_files else None


def disk_ok(trip=True):
    """Free space where the books live. Under 200 MB the bot trips; under 1 GB, a warning.
    A read-only disk trips too: every run fails before it can stop the bot itself."""
    err = _write_error(config.home())
    free = shutil.disk_usage(config.home()).free // MB
    if err is not None and (err.errno == errno.EROFS or (err.errno == errno.ENOSPC and free >= DISK_TRIP_MB)):
        # Read-only after errors, or no room for one more file with space left (all inodes
        # used): nothing can be saved, so no run can trade, and none can stop the bot itself.
        if err.errno == errno.EROFS:
            what, title = "is read-only (it had errors)", "kal warning: disk read-only"
        else:
            what, title = "can't take one more file (too many files)", "kal warning: disk full of files"
        reason = (f"The disk {what}, so nothing can be saved and trading has stopped. Look: "
                  f"{config.command('health')}")
        try:
            tripped = trip and _trip_once(reason)
        except OSError:
            tripped = True   # it couldn't be saved; trip() said so (at most once an hour)
        return _row("disk", "fail", f"{what}: nothing can be saved, so trading has stopped",
                    tripped=tripped, readonly=True, title=title, free_mb=free)
    if err is not None and err.errno != errno.ENOSPC:   # (a full disk is told below)
        return _row("disk", "warn", f"can't write to {config.home()} here: {err.strerror or err}",
                    title="kal warning: disk can't be written", free_mb=free)
    if free < DISK_TRIP_MB:
        tripped = trip and _trip_once(f"Disk almost full: {free:,} MB free. Free up space, then press Reset.")
        return _row("disk", "fail", f"only {free:,} MB free. The bot stops under {DISK_TRIP_MB} MB.",
                    free_mb=free, tripped=tripped)
    files = _free_files(config.home())
    if files is not None and files < DISK_TRIP_FILES:
        # Before none are left: at none, the stop can't be saved and freeing files would
        # turn the bot back on.
        tripped = trip and _trip_once(f"Disk almost out of files: room for {files:,} more (too many files). "
                                      "Delete some, then press Reset.")
        return _row("disk", "fail", f"room for only {files:,} more files. The bot stops under {DISK_TRIP_FILES:,}.",
                    free_mb=free, tripped=tripped, title="kal warning: disk full of files")
    if free < DISK_WARN_MB:
        return _row("disk", "warn", f"{free:,} MB free. The bot stops itself under {DISK_TRIP_MB} MB.",
                    free_mb=free)
    return _row("disk", "ok", f"{free:,} MB free", free_mb=free)


def _base(client=None):
    base = getattr(client, "base", None)
    if isinstance(base, str) and base:
        return base.rstrip("/")
    k = Kalshi(env=_env())   # the same address the trading client would use
    k.s.close()
    return k.base.rstrip("/")


def _cache_age(headers):
    """Seconds a cache held this reply (the HTTP Age header), else 0. Kalshi's public API
    sits behind CloudFront: GET /markets is cached up to 15 s, and a cached reply keeps the
    Date it was stored with. Seen on the real API: Date 7 s old, with "Age: 7"."""
    try:
        return min(max(float(headers.get("Age") or 0), 0.0), 3600.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _clock_reading(url, clock, timeout):
    """(drift, best guess, when) in seconds from one public request; drift is our clock
    minus Kalshi's."""
    PACER.wait()
    t0 = clock()
    r = requests.get(url, params={"limit": 1}, timeout=timeout)
    t1 = clock()
    server = parsedate_to_datetime(r.headers["Date"])
    if server.tzinfo is None:
        server = server.replace(tzinfo=timezone.utc)
    server = server.timestamp() + _cache_age(r.headers)
    # The header is in whole seconds, stamped somewhere between t0 and t1, so our clock
    # minus Kalshi's lies in [lo, hi]. Judge by the smallest drift that fits: a slow
    # reply must never look like a bad clock.
    lo, hi = t0 - server - 1, t1 - server
    return (lo if lo > 0 else hi if hi < 0 else 0.0), (lo + hi) / 2, t1


def clock_ok(client=None, trip=True, clock=time.time, timeout=10):
    """Our clock against Kalshi's, from the Date header of a public request
    (GET /markets?limit=1), corrected by the cache's Age. Over 5 s off is a warning. Over
    30 s off on two checks in a row trips the bot: one reading can be a fluke, and right
    after boot the clock can be off for a minute until chrony fixes it. A reading that
    looks off is taken up to 3 times, and the closest counts: a cache can only make
    Kalshi's clock look older, never newer."""
    url = _base(client) + API + "/markets"
    readings = []
    for _ in range(CLOCK_TRIES):
        try:
            readings.append(_clock_reading(url, clock, timeout))
        except requests.RequestException as e:
            if not readings:
                return _row("clock", "warn", f"couldn't reach Kalshi to check it ({type(e).__name__})",
                            unknown=True)
            break
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            if not readings:
                return _row("clock", "warn", "Kalshi's reply had no readable Date header", unknown=True)
            break
        if abs(readings[-1][0]) <= CLOCK_WARN:
            break
    drift, shown, t1 = min(readings, key=lambda d: abs(d[0]))
    text = f"{abs(shown):.0f} s {'ahead of' if drift > 0 else 'behind'} Kalshi's clock"
    if abs(drift) <= CLOCK_TRIP and trip:
        _clock_bad["ts"] = None
    if abs(drift) <= CLOCK_WARN:
        return _row("clock", "ok", f"within {CLOCK_WARN} s of Kalshi's", drift=round(drift, 1))
    if abs(drift) <= CLOCK_TRIP:
        return _row("clock", "warn", f"{text}. Kalshi may refuse orders. Check: timedatectl",
                    drift=round(drift, 1))
    if not trip:
        return _row("clock", "fail", f"{text}. Kalshi refuses orders like this. Fix: timedatectl",
                    drift=round(drift, 1))
    prev, _clock_bad["ts"] = _clock_bad["ts"], t1
    if prev is not None and t1 - prev <= STRIKE_WINDOW:
        tripped = _trip_once(f"The server clock is {text}. Kalshi refuses orders with a wrong time. "
                             "Fix the clock (timedatectl), then press Reset.")
        return _row("clock", "fail", f"{text}. The bot stopped.", drift=round(drift, 1), tripped=tripped)
    return _row("clock", "fail", f"{text}. The bot stops if it is still off at the next check.",
                drift=round(drift, 1))


# ---------- trip history (for the daily summary) ----------

def _trip_file():
    return os.path.join(config.home(), TRIPS)


def _trip_lines():
    try:
        with open(_trip_file(), encoding="utf-8") as f:
            raw = f.readlines()[-500:]
    except OSError:
        return []
    out = []
    for line in raw:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def note_trips(now=None):
    """Write each new safety stop to $KAL_HOME/trips.jsonl once, for the daily summary.
    config.trip() keeps no history, so this compares disabled_reason with the last line
    (the health job runs it every 10 minutes). A Reset is written too, so the same reason
    tripping again counts as a new stop. Returns the line written, or None."""
    try:
        reason = config.load().get("disabled_reason") or ""
    except Exception:
        return None
    now = time.time() if now is None else now
    with _trip_log:
        lines = _trip_lines()
        last = lines[-1] if lines else None
        if reason and (last is None or last.get("cleared") or last.get("reason") != reason):
            entry = {"ts": now, "reason": reason}
        elif not reason and last is not None and not last.get("cleared"):
            entry = {"ts": now, "cleared": True}
        else:
            return None
        with open(_trip_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    return entry


def trips(start, end):
    """Safety stops first seen in [start, end), oldest first: [{"ts", "reason"}]."""
    out = []
    for d in _trip_lines():
        try:
            t = float(d.get("ts"))
        except (TypeError, ValueError):
            continue
        if d.get("reason") and start <= t < end:
            out.append({"ts": t, "reason": str(d["reason"])})
    return out


# ---------- heartbeat ----------

def boot_id():
    """This boot's id, or "" where there is none."""
    try:
        with open(BOOT_ID) as f:
            return f.read().strip()
    except OSError:
        return ""


def beat():
    """The scheduler loop touches this file about once a minute. Its age tells `kal health`
    (and Docker's health check) whether the loop is still running. It holds this boot's id,
    for clean_stop_check."""
    path = os.path.join(config.home(), HEARTBEAT)
    boot = boot_id()
    try:
        with open(path, errors="replace") as f:   # garbled by disk damage: written again
            same = f.read() == boot
    except OSError:
        same = False
    if not same:
        with open(path, "w") as f:
            f.write(boot)
            f.flush()
            os.fsync(f.fileno())   # ext4 writes it up to ~30 s later: a disk error by then lost it
        config.sync_dir(config.home())
    os.utime(path, None)


def clean_stop_check():
    """Before the first run: stop the bot if it didn't stop cleanly before the server went
    down. Its heartbeat, which a clean stop removes, is then from an earlier boot.
    A disk that went read-only can't save its stop, nor remove the heartbeat. Without this,
    the reboot that fixes the disk turns the bot back on, and it trades within seconds."""
    try:
        with open(os.path.join(config.home(), HEARTBEAT), errors="replace") as f:
            then = f.read().strip()
    except OSError:
        return False   # none: the last run stopped cleanly (or none ran here)
    now = boot_id()
    if not then or not now or then == now:
        return False   # from an older kal, no way to tell, or a crash this boot (systemd restarts it)
    try:
        if not config.load().get("enabled"):
            return False   # it was off: it wasn't trading, so it missed nothing
    except Exception:
        pass   # can't tell: stop it
    return _trip_once("kal didn't shut down cleanly before the server restarted (a disk error, a power "
                      "cut, or a forced restart), so its last steps may not have been saved. "
                      "Look at your positions on Kalshi.")


def unbeat():
    """A clean stop removes the heartbeat, so `restore` can tell the service is really off."""
    try:
        os.remove(os.path.join(config.home(), HEARTBEAT))
    except OSError:
        pass


def heartbeat_age(now=None):
    """Seconds since the scheduler loop last ticked, or None if it never has here."""
    try:
        t = os.path.getmtime(os.path.join(config.home(), HEARTBEAT))
    except OSError:
        return None
    return max(0.0, (time.time() if now is None else now) - t)


# ---------- what the scheduler and `kal health` run ----------

def trading_ok(now=None):
    """Are trade runs working? Network blips and Kalshi outages don't stop the bot (they
    can't lose money), so a long one would look just like a quiet market. Measured from
    the first failed run after the last good one, so turning the bot on is never flagged."""
    try:
        cfg = config.load()
    except Exception as e:
        return _row("trading", "warn", f"config.json can't be read: {e}")
    if not cfg.get("enabled"):
        return _row("trading", "ok", "the bot is off")
    now = time.time() if now is None else now
    first = db.query("""SELECT MIN(ts) AS t FROM runs WHERE kind = 'trade' AND ok = 0 AND id >
        (SELECT COALESCE(MAX(id), 0) FROM runs WHERE kind = 'trade' AND ok = 1)""")[0]["t"]
    if first is None or now - first < OUTAGE:
        return _row("trading", "ok", "trade runs are working")
    last = db.get("last_transient_error") or {}
    why = f" Last error: {last['error']}" if isinstance(last, dict) and last.get("error") else ""
    return _row("trading", "warn", f"trade runs have failed for {ago(now - first)}. It keeps trying.{why}")


def _when(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def report_ok(now=None):
    """Does the hourly status report reach GitHub? The 2-hourly check-in reads it, so a
    GitHub token that ran out (they last 90 days) would leave it blind. Warns when it is
    still failing, nothing has gone through for 3 hours, and it isn't a blip: 3 failed
    tries in a row, or 3 hours of them. So a fresh start (no good push yet) is never
    flagged, and a server back from a long stop isn't flagged before it tries again.
    The token itself is never shown."""
    if not os.environ.get("KAL_GITHUB_TOKEN"):
        return _row("report", "ok", "not set up (no GitHub token)")
    if not os.environ.get("KAL_GITHUB_REPO"):
        return _row("report", "warn", "kal can't post its status report: KAL_GITHUB_REPO is not set. "
                                      "See docs/DEPLOY.md step 10")
    from . import report
    st = db.get(report.PUSH_KEY) or {}
    st = st if isinstance(st, dict) else {}
    now = time.time() if now is None else now
    ok_ts, fail_ts, first, fails = (_when(st.get(k)) for k in ("ok_ts", "fail_ts", "first_fail_ts", "fails"))
    error = " ".join(str(st.get("error") or "").split()).rstrip(". ")[:150]
    if not error:
        return _row("report", "ok", f"sent {ago(now - ok_ts)} ago" if ok_ts else "not sent yet")
    stale = ok_ts is None or now - ok_ts >= REPORT_STALE
    lasting = (fails or 0) >= REPORT_TRIES or now - (first or now) >= REPORT_STALE
    trying = fail_ts is not None and now - fail_ts < REPORT_STALE
    if not (stale and lasting and trying):
        return _row("report", "ok", f"the last try failed ({error}). It tries again every hour.")
    if error.startswith("couldn't reach GitHub"):
        fix = "It keeps trying."
    elif "KAL_GITHUB_REPO" in error or "is public" in error:
        fix = "See docs/DEPLOY.md step 10"
    else:   # 401 and 403: the token ran out or can't write there
        fix = "Make a new GitHub token: docs/DEPLOY.md step 10"
    return _row("report", "warn", f"kal can't post its status report: {error}. {fix}")


def _report_rows():
    """The status report's row, where one is set up (a GitHub token is set)."""
    return [_safe("report", report_ok)] if os.environ.get("KAL_GITHUB_TOKEN") else []


def _safe(check, fn, *args, **kw):
    """Run one check; a bug in it becomes a warning row, never a crash that hides the others.
    One that failed on the books says so (books=True): with damaged books, that's the alert."""
    try:
        return fn(*args, **kw)
    except Exception as e:
        log.warning("%s check crashed", check, exc_info=True)
        return _row(check, "warn", f"couldn't check: {type(e).__name__}: {e}",
                    books=isinstance(e, sqlite3.DatabaseError))


def health(trip=True, client=None):
    """Disk, clock, books, trading and the status report: the scheduler runs this every 10
    minutes. A hard failure trips the bot (config.trip alerts the phone); a warning alerts
    the phone once a day. A stop alert that didn't get through is sent again."""
    rows = [_safe("disk", disk_ok, trip=trip), _safe("clock", clock_ok, client, trip=trip),
            _safe("books", books_ok, trip=trip), _safe("trading", trading_ok)] + _report_rows()
    try:
        note_trips()
    except Exception:
        log.warning("could not write the trip history", exc_info=True)
    try:
        config.resend_stop()
    except Exception:
        log.warning("could not resend the stop alert", exc_info=True)
    # One alert for one cause. Damaged or busy books: the books row says it, not each check
    # that couldn't read them. A disk that takes nothing: its stop says it, books too.
    books_bad = any(r["check"] == "books" and r["level"] != "ok" for r in rows)
    readonly = any(r.get("readonly") for r in rows)
    for r in rows:
        if r["level"] == "ok" or r.get("unknown") or ((books_bad or readonly) and r.get("books")) \
                or (readonly and r["check"] == "books"):
            continue
        key = f"{r['check']}:{r['level']}"
        if r.get("tripped"):
            _note_sent(key)   # its stop alert just went: no second one about it for a day
        else:
            _alert(key, r.get("title") or f"kal warning: {r['check']}", r["detail"],
                   priority="high" if r["level"] == "fail" else "default")
    try:
        db.put("health", {"ts": time.time(), "checks": rows})
    except Exception:
        log.warning("could not save the health result", exc_info=True)
    bad = [f"{r['check']}: {r['detail']}" for r in rows if r["level"] == "fail"]
    out = {"ok": not bad, "checks": rows}
    if bad:
        out["error"] = "; ".join(bad)
    return out


def daily(now=None):
    """The once-a-day chores: disk check, backup, then prune. Prune runs only after a good
    backup, so old rows are never deleted without a fresh copy. Records a "maintenance" run."""
    t0 = time.time()
    out, problems = {"ok": True}, []
    disk = disk_ok()
    out["disk"] = disk["detail"]
    if disk["level"] == "fail":
        problems.append(f"disk: {disk['detail']}")
    b = None
    if disk.get("readonly"):
        out["backup"] = "skipped: the disk takes nothing"   # its stop says so: no second alert
    else:
        try:
            b = backup(now)
            out["backup"] = (f"{', '.join(b['files'])} to backups/{b['day']}, {len(b['days'])} days kept"
                             if b["files"] else "nothing to back up yet")
        except Exception as e:
            b = None
            problems.append(f"backup failed: {e}")
            log.error("backup failed", exc_info=True)
            _alert("backup", "kal backup failed", f"{e}\n\nTry it by hand: sudo kal backup")
    if b is None:
        out["pruned"] = "skipped: no fresh backup"
    else:
        try:
            out["pruned"] = prune(now)
        except Exception as e:
            problems.append(f"prune failed: {e}")
            log.error("prune failed", exc_info=True)
    if problems:
        out.update(ok=False, error="; ".join(problems)[:500])
    try:
        db.insert("runs", kind="maintenance", ok=int(out["ok"]), duration=time.time() - t0,
                  message=json.dumps(out, default=str)[:4000])
    except Exception:
        log.error("could not record the maintenance run", exc_info=True)
    return out


def _timer_row(now=None):
    age = heartbeat_age(now)
    if age is None:
        return _row("timer", "warn", "not running here (it runs inside `app` or `daemon`)")
    if age > ALIVE:
        return _row("timer", "fail", f"stopped: last tick {ago(age)} ago. Restart: sudo systemctl restart kal")
    return _row("timer", "ok", f"running, last tick {ago(age)} ago")


def _backup_row(now=None):
    have = backups()
    if not have:
        return _row("backup", "warn", "none yet (one is made every day)")
    day = have[0][0]
    days = ((time.time() if now is None else now) - day_start(day)) / 86400
    if days > BACKUP_STALE_DAYS + 1:
        return _row("backup", "warn", f"the newest is from {day}, {days:.0f} days ago")
    return _row("backup", "ok", f"newest from {day}, {len(have)} kept")


def _bot_row():
    try:
        cfg = config.load()
    except Exception as e:
        return _row("bot", "fail", f"config.json can't be read: {e}")
    if cfg.get("disabled_reason"):
        return _row("bot", "fail", f"stopped itself: {cfg['disabled_reason']}")
    where = "demo (fake money)" if cfg.get("env") == "demo" else "real money"
    if not cfg.get("enabled"):
        return _row("bot", "ok", f"off ({where})")
    return _row("bot", "ok", f"on, {where}, budget ${cfg.get('budget', 0):,.2f}")


def checks(client=None):
    """Everything `kal health` shows, as rows. Never trips: a person is looking."""
    return [_safe("disk", disk_ok, trip=False), _safe("clock", clock_ok, client, trip=False),
            _safe("books", books_ok, trip=False), _safe("timer", _timer_row), _safe("trading", trading_ok),
            *_report_rows(), _safe("backup", _backup_row), _safe("bot", _bot_row)]


def alive():
    """The quick local check (no network) for Docker's health check and kal-watch: disk,
    books and timer. Only a "fail" row counts as unhealthy; a disk warning is not a reason
    to flag it."""
    rows = [_safe("disk", disk_ok, trip=False), _safe("books", books_ok, trip=False, quick=True),
            _safe("timer", _timer_row)]
    for r in rows:
        if r["level"] == "warn" and r["check"] == "timer":
            r.update(ok=False, level="fail")   # here, no heartbeat at all means it isn't running
    return rows


def _run_text(message):
    try:
        m = json.loads(message or "{}")
    except ValueError:
        return str(message or "")[:200]
    if not isinstance(m, dict):
        return str(m)[:200]
    for k in ("error", "promote_error", "skipped"):
        if m.get(k):
            return str(m[k])[:200]
    return "ok" if m.get("ok", True) else "failed"


def last_runs(kinds=("trade", "research", "maintenance")):
    """{kind: {"ts", "ok", "text"} or None}: the latest recorded run of each kind."""
    out = {}
    for kind in kinds:
        rows = db.query("SELECT ts, ok, message FROM runs WHERE kind = ? ORDER BY id DESC LIMIT 1", kind)
        out[kind] = ({"ts": rows[0]["ts"], "ok": bool(rows[0]["ok"]), "text": _run_text(rows[0]["message"])}
                     if rows else None)
    return out


def last_error():
    """The latest failed run: {"ts", "kind", "text"}, or None."""
    rows = db.query("SELECT ts, kind, message FROM runs WHERE ok = 0 ORDER BY id DESC LIMIT 1")
    if not rows:
        return None
    return {"ts": rows[0]["ts"], "kind": rows[0]["kind"], "text": _run_text(rows[0]["message"])}
