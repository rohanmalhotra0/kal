"""Background jobs: trade every few minutes, research every day, report every hour.

The dashboard runs a Scheduler in a thread; a server with no dashboard calls
serve_forever(). Config is re-read every tick, so a change made on the dashboard
(on/off, intervals) applies without a restart.

A job that crashes is logged and recorded, and never kills the scheduler: a dead
scheduler looks exactly like a quiet market, so nobody would notice it had stopped.
Each job runs in its own worker thread, so an hour of research never delays trading.
At most one copy of each job runs at a time.
"""
import importlib
import json
import logging
import signal
import threading
import time
import traceback

from . import config, db, engine, risk

log = logging.getLogger(__name__)

JOBS = ("trade", "research", "report")
REPORT_EVERY = 3600
MIN_TRADE_EVERY = 60          # faster than this just burns API rate limit
MIN_RESEARCH_EVERY = 3600
TICK = 15                     # seconds between due-checks
STOP_WAIT = 300               # how long stop() waits for a trade run to finish


# ---------- the jobs ----------

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


def run_report():
    report = _optional("report")
    if report is None:
        return {"ok": True, "skipped": "report is not installed"}
    return report.maybe_push()


DEFAULT_JOBS = {"trade": run_trade, "research": run_research, "report": run_report}


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
            "report": REPORT_EVERY}


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
    def __init__(self, jobs=None, clock=time.time, tick=TICK, intervals=intervals, threaded=True):
        """jobs, clock, intervals and threaded are there so tests can drive it by hand."""
        self.jobs = dict(jobs or DEFAULT_JOBS)
        self.clock = clock
        self.tick_seconds = tick
        self.intervals = intervals
        self.threaded = threaded
        self.last = {}            # job -> when it last started (persisted, so a restart keeps the schedule)
        self.status = {}          # job -> {"started", "finished", "ok", "text"}
        self._workers = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._loaded = False

    # ---------- schedule ----------

    def _load(self):
        """Last-run times from the db. Research also counts runs started from the CLI or cron."""
        if self._loaded:
            return
        self._loaded = True
        try:
            saved = db.get("scheduler", {}) or {}
            self.last.update({k: float(v) for k, v in saved.items() if k in self.jobs})
            research = db.get("last_research") or {}
            if research.get("ts"):
                self.last["research"] = max(self.last.get("research", 0), float(research["ts"]))
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

    def due(self, now=None, cfg=None):
        """Jobs whose time has come and aren't already running."""
        self._load()
        now = self.clock() if now is None else now
        every = self.intervals(cfg or self._config())
        return [k for k in self.jobs
                if not self.running(k) and now - self.last.get(k, 0) >= every.get(k, REPORT_EVERY)]

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

    def state(self):
        """For the dashboard: when each job last ran, how it went, and when it runs next."""
        self._load()
        every = self.intervals(self._config())
        with self._lock:
            out = {}
            for k in self.jobs:
                last = self.last.get(k)
                out[k] = {"every": every.get(k, REPORT_EVERY), "last": last,
                          "next": (last + every.get(k, REPORT_EVERY)) if last else self.clock(),
                          **self.status.get(k, {}), "running": self.running(k)}
            out["alive"] = bool(self._thread and self._thread.is_alive())
            return out

    # ---------- the loop ----------

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.error("scheduler tick failed: %s", traceback.format_exc())
            self._stop.wait(self.tick_seconds)

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
