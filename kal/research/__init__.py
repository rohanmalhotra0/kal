"""Research: find new edges every day and promote the ones paper trading confirms.

run_all() is what the scheduler, cron and `python -m kal research` call. It never
raises: a failing series is logged and skipped, and a failed run is recorded in the
db "runs" table (kind="research") so the dashboard can show it.
"""
import fcntl
import json
import logging
import os
import time
import traceback

from .. import config, db
from ..client import Kalshi
from . import discover, longshot, promote

log = logging.getLogger(__name__)


def run_all(client=None, quick=False):
    """Discover (favorite ranges, then longshot cells), then promote. quick=True tests only a
    few series (for CI and smoke tests)."""
    cfg = config.load()
    t0 = time.time()
    # Research can take an hour on a cold cache; a second copy would just double the API load.
    lock = open(os.path.join(config.home(), ".research.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return {"ok": False, "skipped": "research is already running"}
    summary = {"ok": True, "quick": quick}
    try:
        try:
            client = client or Kalshi.from_env(config.home(), cfg["env"])
            summary.update(discover.run(client, cfg, quick=quick))
        except Exception as e:
            summary.update(ok=False, error=f"discovery failed: {e}"[:500])
            log.error("research discovery failed: %s", traceback.format_exc())
        summary["longshot"] = longshot.run(client, cfg, quick=quick)  # never raises
        try:
            summary["changes"] = promote.apply(cfg, client) + longshot.apply_longshot(cfg, client)
        except Exception as e:
            summary.update(ok=False, promote_error=str(e)[:500])
            log.error("research promote failed: %s", traceback.format_exc())
        summary["duration"] = round(time.time() - t0, 1)
        text = json.dumps(summary, default=str)
        db.insert("runs", kind="research", ok=int(summary["ok"]), duration=summary["duration"], message=text[:4000])
        db.put("last_research", {"ts": time.time(), **json.loads(text)})
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return summary
