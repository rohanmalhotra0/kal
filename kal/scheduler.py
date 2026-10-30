"""Background jobs: trade every few minutes, research every day, report every hour,
check disk and clock every 10 minutes, back up and prune once a day, and send the
morning summary to the phone.

The dashboard runs a Scheduler in a thread; a server with no dashboard calls
serve_forever(). Config is re-read every tick, so a change made on the dashboard
(on/off, intervals) applies without a restart.

A job that crashes is logged and recorded, and never kills the scheduler: a dead
scheduler looks exactly like a quiet market, so nobody would notice it had stopped.
For the same reason the loop touches a heartbeat file, which `kal health` reads.
Each job runs in its own worker thread, so an hour of research never delays trading.
Research also runs in its own process (in_child): its memory goes back when it ends,
and if it grows too big, the kernel stops it, not trading. After every job, what the job
freed goes back to the system too (give_back_memory).
At most one copy of each job runs at a time.
"""
import ctypes
import importlib
import json
import logging
import multiprocessing
import signal
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import config, db, engine, risk

log = logging.getLogger(__name__)

JOBS = ("trade", "research", "report", "health", "maintenance", "summary")
REPORT_EVERY = 3600
HEALTH_EVERY = 600            # disk and clock
DAY = 86400
MAINTENANCE_HOUR = 9          # UTC (4-5 AM in New York): backup and prune while little trades
SUMMARY_HOUR = 13             # UTC (8-9 AM in New York): yesterday's summary to the phone
MIN_TRADE_EVERY = 60          # faster than this just burns API rate limit
MIN_RESEARCH_EVERY = 3600
RESEARCH_RETRY = 600          # a research run a restart cut off starts again this soon after
RESEARCH_PACE = 0.3           # seconds between research's API calls: what trading leaves of Kalshi's 20 a second
TICK = 15                     # seconds between due-checks
BEAT_EVERY = 60               # seconds between heartbeat touches
STOP_WAIT = 300               # how long stop() waits for a trade run to finish


# ---------- the jobs ----------

def give_back_memory():
    """Hand the memory a job freed back to the system. A trade run holds every open event a
    strategy can act on (11,433 events, 257 MB in Python on 2026-10-30) and frees it at the
    end, but glibc keeps freed pages for reuse, so the process stayed near its 550 MB peak
    between runs. That left research, in its own process, less room under the cap
    (deploy/kal.service). True if any memory went back. Not glibc (macOS, musl): nothing to
    do. Never raises."""
    try:
        return bool(ctypes.CDLL(None).malloc_trim(0))
    except Exception:
        return False


def run_trade():
    try:
        return engine.run_once()
    except risk.Locked:
        return {"ok": True, "skipped": "another run is in progress"}


def _optional(name):
    """Import kal.<name>, or None if that module doesn't exist (yet). Other import errors raise."""
    try:
        return importlib.import_module(f"{__package__}.{name}")
    except ModuleNotFoundError as e:
        if e.name != f"{__package__}.{name}":
            raise
        return None


def run_research():
    research = _optional("research")
    if research is None:
        return {"ok": True, "skipped": "research is not installed"}
    return research.run_all()


def _child(conn, job):
    """In the research process: ask the kernel to stop this process first if memory runs
    out (raising your own score needs no rights), then run the job and send back what it
    returned, or why it crashed."""
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except OSError:
        pass  # not Linux: it still runs, just without going first
    try:
        from . import logs
        logs.setup()  # its lines go to kal's log too
    except Exception:
        pass
    try:
        out = {"result": json.loads(json.dumps(job(), default=str))}
    except Exception as e:
        log.error("research crashed: %s", traceback.format_exc())
        out = {"crashed": str(e)[:300] or e.__class__.__name__}
    try:
        conn.send(out)
    finally:
        conn.close()


def in_child(job):
    """Run job() in a new process and return what it returned. Raises RuntimeError if it
    crashed or the process died, so the scheduler records it like any crash.

    Spawned, not forked: this process has threads, and a fork copies their locks mid-use.
    Its memory all goes back when it ends. Out of memory, the kernel stops it first, and
    deploy/kal.service (OOMPolicy=continue) keeps the rest of kal running."""
    ctx = multiprocessing.get_context("spawn")
    recv, send = ctx.Pipe(duplex=False)
    p = ctx.Process(target=_child, args=(send, job), name="kal-research", daemon=True)
    p.start()
    send.close()  # so recv() ends if the child dies without answering
    try:
        out = recv.recv()
    except EOFError:
        out = {}
    finally:
        recv.close()
    p.join()
    if "result" in out:
        return out["result"]
    if "crashed" in out:
        raise RuntimeError(out["crashed"])
    if p.exitcode == -signal.SIGKILL:
        raise RuntimeError("research was stopped, likely for using too much memory. Trading goes on.")
    raise RuntimeError(f"research stopped (exit code {p.exitcode})")


def _research_slowly():
    """Research, paced to leave trading its share of Kalshi's rate limit. In its own process
    it no longer shares the trade loop's pacer (kal.client.PACER), so it slows itself."""
    from . import client
    client.PACER.gap = max(client.PACER.gap, RESEARCH_PACE)
    return run_research()


def run_research_apart():
    """Research in its own process (in_child), at its own slower pace."""
    return in_child(_research_slowly)


def run_report():
    report = _optional("report")
    if report is None:
        return {"ok": True, "skipped": "report is not installed"}
    return report.maybe_push()


def run_health():
    """Disk, clock and trading. Trips the bot on a hard failure."""
    from . import maintenance
    return maintenance.health()


def run_maintenance():
    """Backup, then prune old rows."""
    from . import maintenance
    return maintenance.daily()


def run_summary():
    """Yesterday in a few lines, to the phone."""
    from . import maintenance, notify, report
    if not notify.enabled():
        return {"ok": True, "skipped": "phone alerts are not set up"}
    maintenance.note_trips()   # so a stop since the last health check is counted
    title, text, priority = report.daily()
    log.info("daily summary: %s | %s", title, text.replace("\n", " | "))
    if not notify.send(title, text, priority=priority, tags=("bar_chart",)):
        return {"ok": False, "error": "the summary did not send"}
    return {"ok": True}


DEFAULT_JOBS = {"trade": run_trade, "research": run_research_apart, "report": run_report,
                "health": run_health, "maintenance": run_maintenance, "summary": run_summary}


def _number(v, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def intervals(cfg):
    """Seconds between runs of each job, from config. Junk or tiny values fall back to safe ones."""
    d = config.DEFAULTS
    trade = _number(cfg.get("trade_interval_min"), d["trade_interval_min"]) * 60
    research = _number(cfg.get("research_interval_hours"), d["research_interval_hours"]) * 3600
    return {"trade": max(MIN_TRADE_EVERY, trade),
            "research": max(MIN_RESEARCH_EVERY, research),
            "report": REPORT_EVERY, "health": HEALTH_EVERY, "maintenance": DAY, "summary": DAY}


def _hour(v, default):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v) or not 0 <= v <= 23:
        return default
    return int(v)


def daily_hours(cfg):
    """The UTC hour each once-a-day job runs at. The summary can be moved with
    `kal set summary_hour_utc 14`; anything odd falls back to the default."""
    return {"maintenance": MAINTENANCE_HOUR, "summary": _hour(cfg.get("summary_hour_utc"), SUMMARY_HOUR)}


def last_slot(now, hour):
    """The latest moment at or before `now` when the UTC clock read hour:00."""
    t = datetime.fromtimestamp(now, timezone.utc)
    slot = t.replace(hour=hour, minute=0, second=0, microsecond=0)
    if slot > t:
        slot -= timedelta(days=1)
    return slot.timestamp()


def describe(result):
    """A short plain line about what a job returned, for the dashboard."""
    if result is None or result is True:
        return "ok"
    if result is False:
        return "skipped: not set up"   # e.g. report.maybe_push() with no GitHub token
    if not isinstance(result, dict):
        return str(result)[:200]
    if result.get("skipped"):
        return f"skipped: {result['skipped']}"
    if result.get("error"):
        return f"error: {result['error']}"[:300]
    if result.get("ok") is False:
        return "failed"
    return "ok"


class Scheduler:
    def __init__(self, jobs=None, clock=time.time, tick=TICK, intervals=intervals, threaded=True,
                 hours=daily_hours):
        """jobs, clock, intervals, hours and threaded are there so tests can drive it by hand."""
        self.jobs = dict(jobs or DEFAULT_JOBS)
        self.clock = clock
        self.tick_seconds = tick
        self.intervals = intervals
        self.hours = hours
        self.threaded = threaded
        self.last = {}            # job -> when it last started (persisted, so a restart keeps the schedule)
        self.status = {}          # job -> {"started", "finished", "ok", "text"}
        self._workers = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._loaded = False
        self._beat_at = 0.0
        self._beat_failed = False

    # ---------- schedule ----------

    def _load(self):
        """Last-run times from the db. Research also counts runs started from the CLI or cron.

        A research run that started but never finished (a restart killed it) is tried again
        soon. Waiting a day would start it at the same hour, and it could land in the daily
        update's restart every time: no promotions, and a losing edge never retired."""
        if self._loaded:
            return
        self._loaded = True
        try:
            saved = db.get("scheduler", {}) or {}
            self.last.update({k: float(v) for k, v in saved.items() if k in self.jobs})
            done = float((db.get("last_research") or {}).get("ts") or 0)
            started = self.last.get("research", 0)
            if started > done and "research" in self.jobs:
                every = self.intervals(self._config()).get("research", DAY)
                self.last["research"] = min(started, self.clock() - every + RESEARCH_RETRY)
            elif done:
                self.last["research"] = max(started, done)
        except Exception:
            log.error("could not load the schedule: %s", traceback.format_exc())

    def _save(self):
        try:
            db.put("scheduler", self.last)
        except Exception:
            log.error("could not save the schedule: %s", traceback.format_exc())

    def _config(self):
        try:
            return config.load()
        except Exception:
            log.error("could not read config: %s", traceback.format_exc())
            return dict(config.DEFAULTS)

    def running(self, kind):
        w = self._workers.get(kind)
        return bool(w and w.is_alive())

    def _hours(self, cfg):
        try:
            return dict(self.hours(cfg) or {})
        except Exception:
            log.error("could not work out the daily job hours: %s", traceback.format_exc())
            return {}

    def due(self, now=None, cfg=None):
        """Jobs whose time has come and aren't already running. A once-a-day job is due when
        it hasn't run since its UTC hour last came round, so a server that was down at that
        hour catches up once, not once per missed day."""
        self._load()
        now = self.clock() if now is None else now
        cfg = cfg or self._config()
        every, at = self.intervals(cfg), self._hours(cfg)

        def is_due(k):
            last = self.last.get(k, 0)
            if k in at:
                return last < last_slot(now, at[k])
            return now - last >= every.get(k, REPORT_EVERY)
        return [k for k in self.jobs if not self.running(k) and is_due(k)]

    def tick(self):
        """Start every job that is due. Returns the ones started."""
        started = []
        for kind in self.due():
            if self._start(kind):
                started.append(kind)
        return started

    def run_now(self, kind):
        """Start one job right away (the dashboard's buttons). Doesn't wait for it to finish."""
        if kind not in self.jobs:
            return {"started": False, "why": f"Unknown job: {kind}"}
        self._load()
        if not self._start(kind):
            return {"started": False, "why": "It is already running."}
        return {"started": True}

    # ---------- running a job ----------

    def _start(self, kind):
        with self._lock:
            if self.running(kind):
                return False
            self.last[kind] = self.clock()
            self.status[kind] = {**self.status.get(kind, {}), "started": self.last[kind], "running": True}
            self._save()
            if self.threaded:
                # Trade runs aren't daemons: on shutdown we'd rather finish a basket than cut it in half.
                w = threading.Thread(target=self._run, args=(kind,), name=f"kal-{kind}",
                                     daemon=kind != "trade")
                self._workers[kind] = w
                w.start()
                return True
        self._run(kind)
        return True

    def _run(self, kind):
        t0 = time.time()
        try:
            result = self.jobs[kind]()
            # "Skipped" (say, research already running elsewhere) is not a failure.
            ok = not (isinstance(result, dict) and result.get("ok") is False and not result.get("skipped"))
            text = describe(result)
        except Exception as e:
            log.error("%s job crashed: %s", kind, traceback.format_exc())
            ok, text = False, f"crashed: {e}"[:300]
            try:
                db.insert("runs", kind=kind, ok=0, duration=time.time() - t0,
                          message=json.dumps({"ok": False, "error": text}))
            except Exception:
                log.error("could not record the crash: %s", traceback.format_exc())
        with self._lock:
            self.status[kind] = {"started": self.status.get(kind, {}).get("started"),
                                 "finished": self.clock(), "ok": ok, "text": text, "running": False}
        give_back_memory()

    def state(self):
        """For the dashboard: when each job last ran, how it went, and when it runs next."""
        self._load()
        cfg = self._config()
        every, at = self.intervals(cfg), self._hours(cfg)
        now = self.clock()
        with self._lock:
            out = {}
            for k in self.jobs:
                last = self.last.get(k)
                if k in at:
                    slot = last_slot(now, at[k])
                    gap, nxt = DAY, (slot + DAY) if last and last >= slot else now
                else:
                    gap = every.get(k, REPORT_EVERY)
                    nxt = (last + gap) if last else now
                out[k] = {"every": gap, "last": last, "next": nxt, **self.status.get(k, {}),
                          "running": self.running(k)}
            out["alive"] = bool(self._thread and self._thread.is_alive())
            return out

    # ---------- the loop ----------

    def _loop(self):
        # Beat at once, before the first tick (which can take a while): until then the
        # heartbeat on disk may be a crashed process's, and a clean stop must take it away.
        self._beat()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.error("scheduler tick failed: %s", traceback.format_exc())
            self._beat()
            self._stop.wait(self.tick_seconds)

    def _beat(self):
        """Touch the heartbeat file about once a minute. Never raises: a full disk must not
        stop the loop, and it logs only the first failure in a row."""
        now = time.time()
        if now - self._beat_at < BEAT_EVERY:
            return
        self._beat_at = now
        try:
            from . import maintenance
            maintenance.beat()
            self._beat_failed = False
        except Exception as e:
            if not self._beat_failed:
                log.warning("could not touch the heartbeat file: %s", e)
            self._beat_failed = True

    def start(self):
        """Run the loop in a background thread (the dashboard does this)."""
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="kal-scheduler", daemon=True)
        self._thread.start()
        return self

    def stop(self, wait=STOP_WAIT):
        """Stop starting new jobs, and give a trade run in progress time to finish."""
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        trade = self._workers.get("trade")
        if trade and trade.is_alive():
            trade.join(timeout=wait)
        if self._beat_at or self._thread:   # this process ran the loop: a clean stop takes the heartbeat away
            try:
                from . import maintenance
                maintenance.unbeat()
            except Exception:
                log.warning("could not remove the heartbeat file", exc_info=True)

    def serve_forever(self):
        """Headless mode, for a server with no dashboard. Runs until Ctrl-C or SIGTERM."""
        if not logging.getLogger().handlers:
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        try:
            signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        except ValueError:
            pass  # not the main thread: the caller handles signals
        log.info("scheduler running")
        try:
            self._loop()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
            log.info("scheduler stopped")


def serve_forever():
    Scheduler().serve_forever()
