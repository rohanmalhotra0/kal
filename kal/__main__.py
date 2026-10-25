"""Command line: python -m kal <command>.

  app [--host --port]     dashboard + scheduler (the normal way to run it)
  daemon                  scheduler only, no dashboard
  run                     one trading run now
  scan                    dry run: what each strategy would buy. Never trades.
  research [--quick]      look for new edges now
  selftest [--order-probe]  check Kalshi's API still looks right
  status                  a few lines on how it's doing
  report [--push] [--json]  the full report (and put it on the status branch)
  health [--alive]        disk, clock, timer, backups, last runs, last error
  backup                  back up the books now
  restore [DAY] [--yes]   list backups, or put one back
  logs [-n 50]            the last lines of the log file
  summary [--send]        yesterday's phone summary (--send tests phone alerts)
  setup                   the guided setup: five plain questions
  on [--budget X]         turn the bot on
  off                     turn the bot off
  set KEY VALUE           change one setting

Modules other people own (dashboard, scheduler, research) are imported only by the
command that needs them, so a broken dashboard can't stop `off` from working.
"""
import argparse
import inspect
import json
import logging
import math
import os
import signal
import sys
import time
from contextlib import contextmanager

from . import config

DASHBOARD_PASSWORD = "KAL_DASHBOARD_PASSWORD"
LOCAL = ("127.0.0.1", "localhost", "::1")
SHOW = 10   # opportunities shown per strategy by `scan`
TAGS = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}


def say(*lines):
    for line in lines:
        print(line)


def _cmd(rest):
    """How to type a kal command here. The server's `kal` wrapper sets KAL_CMD="sudo kal",
    so the hints match what the owner really types. (Display only: it changes nothing.)"""
    return f"{os.environ.get('KAL_CMD') or 'python -m kal'} {rest}"


def _positive(text):
    """argparse type: a whole number, 1 or more."""
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number")
    if v < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return v


def _money(text):
    """argparse type: dollars, never negative, never nan/inf."""
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number")
    if not math.isfinite(v) or v < 0:
        raise argparse.ArgumentTypeError("must be a number, 0 or more")
    return v


def _logging(level=logging.WARNING):
    if not logging.getLogger().handlers:
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _log_to_file():
    """The long-running commands also log to $KAL_HOME/logs/kal.log (see `logs`)."""
    from . import logs
    logs.setup(config.home())


# ---------- app / daemon ----------

def _sigterm_is_ctrl_c():
    """systemd and docker stop with SIGTERM. Treat it like Ctrl-C, so the dashboard shuts
    down cleanly and a trade run in progress gets to finish its basket."""
    def handler(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, handler)
    except ValueError:
        pass  # not the main thread (tests)


def cmd_app(args):
    host = args.host or os.environ.get("KAL_DASHBOARD_HOST") or "127.0.0.1"
    if host.strip("[]") not in LOCAL and not os.environ.get(DASHBOARD_PASSWORD):
        say("Not starting. Anyone who opens the page could trade your money.",
            f"To listen on {host}, set {DASHBOARD_PASSWORD} first.",
            "Or keep the default (127.0.0.1) and use an SSH tunnel.")
        return 2
    _log_to_file()
    from kal.dashboard.app import main
    params = inspect.signature(main).parameters
    takes_any = any(p.kind is p.VAR_KEYWORD for p in params.values())
    kwargs = {k: v for k, v in {"host": host, "port": args.port}.items()
              if v is not None and (takes_any or k in params)}
    _sigterm_is_ctrl_c()
    main(**kwargs)
    return 0


def cmd_daemon(args):
    _log_to_file()
    from kal.scheduler import Scheduler
    Scheduler().serve_forever()
    return 0


# ---------- run / scan ----------

def cmd_run(args):
    from . import engine, risk
    _logging()
    try:
        s = engine.run_once()
    except risk.Locked:
        say("Another run is going. Try again in a minute.")
        return 1
    if s.get("skipped"):
        say(f"Skipped: {s['skipped']}.")
        return 0
    say("Run done." if s.get("ok") else f"Run FAILED: {s.get('error', '?')}",
        f"Signals: {s.get('signals', 0)}. Live trades: {s.get('live_trades', 0)}. "
        f"Paper trades: {s.get('paper_trades', 0)}.")
    for note in s.get("notes") or []:
        say(f"- {note}")
    return 0 if s.get("ok") else 1


class NoOrders:
    """Read-only view of a client for `scan`. Market data works; anything that could
    place an order raises. A guard against mistakes, not a sandbox: the real client
    behind it has no key either, so Kalshi would refuse an order anyway."""

    BLOCKED = {"buy", "sell", "cancel", "cancel_order", "amend", "s", "session", "__dict__"}

    def __init__(self, client):
        object.__setattr__(self, "_inner", client)

    def __getattribute__(self, name):
        if name == "can_trade":
            return False
        if name in NoOrders.BLOCKED or (name.startswith("_") and not name.startswith("__")):
            raise RuntimeError(f"scan is a dry run: {name} is not allowed")
        if name.startswith("__"):
            return object.__getattribute__(self, name)
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value):
        raise RuntimeError("scan is a dry run: the client is read-only")


@contextmanager
def _no_db():
    """Scan never writes the db. Make any db use fail loudly instead."""
    from . import db
    real = db.connect

    def refuse(*a, **k):
        raise RuntimeError("scan is a dry run and may not use the database")
    db.connect = refuse
    try:
        yield
    finally:
        db.connect = real


def _research():
    from . import engine
    try:
        return engine.research_outputs()
    except Exception as e:
        say(f"(Couldn't read research files: {e})")
        return {}


def scan_all(client=None, only=None):
    """Every strategy scans in paper mode with a key-less client. Prints what it finds.
    Places no orders and never touches the db. Returns {strategy: [signals]}."""
    from .client import Kalshi
    from .risk import Limits
    from .strategies import load_all
    from .strategies.base import Context
    cfg = config.load()
    from .engine import RunCache
    client = NoOrders(RunCache(client or Kalshi(env=cfg["env"])))  # one events download for all strategies
    money = cfg["budget"] if cfg["budget"] > 0 else cfg["paper_bankroll"]
    research = _research()
    found = {}
    with _no_db():
        for name, strat in load_all().items():
            if only and name not in only:
                continue
            params = cfg["strategies"].get(name, {})
            ctx = Context(client, cfg, "paper", money, money, set(), research, params)
            try:
                signals = list(strat.scan(ctx) or [])
            except Exception as e:
                say(f"{name}: scan failed: {e}", "")
                continue
            found[name] = signals
            say(f"{name} (set to {params.get('mode', 'off')}): {len(signals)} found")
            limits = Limits(cfg, money)
            for sig in signals[:SHOW]:
                why = limits.refuse(sig)
                kind = "locked-in" if sig.guaranteed else "bet"
                say(f"  {sig.event} {sig.title[:60]}".rstrip() + f": cost ${sig.cost:.2f}, worst case pays ${sig.payout_min:.2f}, "
                    f"expected profit ${sig.exp_profit:.2f} ({kind}, {len(sig.legs)} legs)"
                    + (f" [would skip: {why}]" if why else ""))
                if sig.note:
                    say(f"    {sig.note}")
            if len(signals) > SHOW:
                say(f"  ...and {len(signals) - SHOW} more")
            say("")
    say("Dry run. No orders were placed.")
    return found


def cmd_scan(args):
    scan_all(only=args.only or None)
    return 0


# ---------- research / selftest ----------

def cmd_research(args):
    _logging(logging.INFO)
    from kal.research import run_all
    s = run_all(quick=args.quick) or {}
    if s.get("skipped"):
        say(f"Skipped: {s['skipped']}.")
        return 0
    say("Research done." if s.get("ok") else "Research had problems.")
    for k in ("error", "promote_error"):
        if s.get(k):
            say(f"Error: {s[k]}")
    say(f"Series tested: {len(s.get('tested') or [])}. New edges: {len(s.get('new_edges') or [])}. "
        f"Failed: {len(s.get('failed') or {})}.")
    if s.get("edges"):
        say("Edges now: " + ", ".join(f"{n} {k}" for k, n in s["edges"].items()) + ".")
    for change in s.get("changes") or []:
        say(f"- {change}")
    return 0 if s.get("ok") else 1


def cmd_selftest(args):
    from . import selftest
    if args.order_probe:
        say("Order probe: buys 1 YES at $0.01 where nobody sells that low. It can't fill.")
    rows = selftest.run(order_probe=args.order_probe)
    for r in rows:
        say(f"{'ok  ' if r['ok'] else 'FAIL'}  {r['check']}: {r['detail']}")
    bad = [r for r in rows if not r["ok"]]
    say("", "All checks passed." if not bad else f"{len(bad)} check(s) FAILED.")
    return 1 if bad else 0


# ---------- status / report ----------

def cmd_status(args):
    from . import report
    print(report.markdown(report.build(), short=True), end="")
    return 0


def cmd_report(args):
    from . import report
    rep = report.build()
    if args.json:
        print(json.dumps(rep, indent=1))
    else:
        print(report.markdown(rep), end="")
    if args.push:
        try:
            pushed = report.maybe_push(rep)
        except report.PushError as e:
            say(f"Push FAILED: {e}")
            return 1
        say("Pushed to the status branch." if pushed else
            "Not pushed: set KAL_GITHUB_TOKEN and KAL_GITHUB_REPO first.")
    return 0


# ---------- upkeep: health / backup / restore / logs / summary ----------

def _short(text, n=120):
    """Error text cut to one readable line."""
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 3] + "..."


def _say_clean(*lines):
    """Like say(), with secrets blanked: people paste this output when they ask for help."""
    from .logs import scrub
    say(*(scrub(line) for line in lines))


def _show(rows):
    for r in rows:
        _say_clean(f"{TAGS.get(r['level'], '?   ')}  {r['check']}: {r['detail']}")


def cmd_health(args):
    """Read-only: it never trips the bot. The scheduler's own check does that."""
    from . import maintenance
    if args.alive:
        rows = maintenance.alive()
        _show(rows)
        return 1 if any(r["level"] == "fail" for r in rows) else 0
    rows = maintenance.checks()
    _show(rows)
    say("")
    try:
        now = time.time()
        for kind, run in maintenance.last_runs().items():
            _say_clean(f"Last {kind} run: " + (f"{maintenance.ago(now - run['ts'])} ago, "
                                               f"{'ok' if run['ok'] else 'FAILED: ' + _short(run['text'])}"
                                               if run else "never"))
        err = maintenance.last_error()
        _say_clean("Last error: " + (f"{maintenance.ago(now - err['ts'])} ago, {err['kind']}: {_short(err['text'])}"
                                     if err else "none"))
    except Exception as e:
        say(f"Couldn't read the runs: {e}")
    fails = sum(r["level"] == "fail" for r in rows)
    warns = sum(r["level"] == "warn" for r in rows)
    say("", "All good." if not fails + warns else f"{fails} problem(s), {warns} warning(s). See above.")
    return 1 if fails else 0


def cmd_backup(args):
    from . import maintenance
    try:
        b = maintenance.backup()
    except Exception as e:
        say(f"Backup FAILED: {e}")
        return 1
    if not b["files"]:
        say("Nothing to back up yet.")
        return 0
    say(f"Backed up {', '.join(b['files'])} to {b['dir']}.",
        f"Keeping {len(b['days'])} day(s): {b['days'][0]} to {b['days'][-1]}.")
    return 0


def cmd_restore(args):
    from . import maintenance
    if not args.day:
        have = maintenance.backups()
        if not have:
            say(f"No backups yet. Make one: {_cmd('backup')}")
            return 1
        say("Backups, newest first:")
        for day, files in have:
            say(f"  {day}: {', '.join(files)}")
        say("", f"To put one back: {_cmd('restore ' + have[0][0])}")
        return 0
    if not args.yes:
        say(f"This puts back the books from {args.day}.",
            "Trades after that day will be missing from them.",
            "The bot will stop until you press Reset.",
            f"To do it, run: {_cmd('restore ' + args.day + ' --yes')}")
        return 2
    try:
        r = maintenance.restore(args.day)
    except Exception as e:
        say(f"Not restored: {e}")
        return 1
    say(f"Restored {os.path.basename(r['db'])} from {args.day}.")
    if r["saved"]:
        say(f"The books from before are kept in {r['saved']}.")
    say("The bot is stopped. Next:",
        f" 1. On Kalshi, look at your trades since {args.day}. They are not in these books.",
        " 2. Lower the budget by what they lost. Count any still open as lost.",
        f"    For example, $50: {_cmd('set budget 250')}",
        f" 3. Then: {_cmd('on --clear-trip')}")
    return 0


def cmd_logs(args):
    from . import logs
    lines = logs.tail(args.lines)
    if not lines:
        say(f"No log yet at {logs.path()}.", "It starts when the bot runs (app or daemon).")
        return 0
    for line in lines:
        print(line)
    return 0


def cmd_summary(args):
    from . import notify, report
    title, text, priority = report.daily()
    say(title, "", text)
    if not args.send:
        return 0
    if not notify.enabled():
        say("", f"Not sent: no alert topic yet. Run {_cmd('setup')}, or set KAL_NTFY_TOPIC.")
        return 1
    sent = notify.send(title, text, priority=priority, tags=("bar_chart",))
    say("", "Sent. Look at your phone." if sent else "Sending FAILED. Check the topic and the internet.")
    return 0 if sent else 1


def cmd_setup(args):
    """The guided setup (kal.setup): five plain questions."""
    from . import setup
    return setup.run()


# ---------- on / off ----------

def _where(cfg):
    return "demo (fake money)" if cfg["env"] == "demo" else "prod (REAL money)"


def cmd_on(args):
    stored = config.load()
    if stored["disabled_reason"] and not args.clear_trip:
        say(f"It is stopped: {stored['disabled_reason']}",
            f"Find out why first. Then run: {_cmd('on --clear-trip')}")
        return 1
    changes = {"enabled": True}
    if args.budget is not None:
        changes["budget"] = args.budget
    if getattr(args, "env", None):
        changes["env"] = args.env
    if args.clear_trip:
        changes["disabled_reason"] = ""
    try:
        cfg = config.update(changes)
    except ValueError as e:
        say(f"Not changed: {e}")
        return 2
    say(f"Bot is ON. Exchange: {_where(cfg)}.",
        f"Budget: ${cfg['budget']:.2f}. That is the most it can lose in total.")
    if cfg["budget"] <= 0:
        say("Budget is $0, so it can't buy anything real. Add --budget 300 (or what you want).")
    return 0


def cmd_off(args):
    config.turn_off()
    say("Bot is OFF. No new trades. Open positions stay open until they settle.")
    return 0


LOCKED_KEYS = ("enabled", "disabled_reason")


def _parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def cmd_set(args):
    """Change one setting, e.g. `set env demo` or `set strategies.ladder_arb.mode live`."""
    path = args.key.split(".")
    if path[0] in LOCKED_KEYS:
        say("Use `on` / `off` (and `on --clear-trip`) for that.")
        return 2
    value = _parse_value(args.value)
    change = value
    for k in reversed(path):
        change = {k: change}
    cfg = config.load()
    if path[0] == "env" and cfg["enabled"]:
        say(f"Turn the bot off first: {_cmd('off')}")
        return 2
    try:
        new = config.update(change)
    except (ValueError, TypeError, KeyError) as e:
        say(f"Not changed: {e}")
        return 2
    shown = new
    for k in path:
        shown = shown.get(k) if isinstance(shown, dict) else None
    say(f"{args.key} is now {shown}.")
    return 0


# ---------- parsing ----------

def parser():
    p = argparse.ArgumentParser(prog="python -m kal", description="kal: a Kalshi trading bot.")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    a = sub.add_parser("app", help="dashboard + scheduler")
    a.add_argument("--host", default=None, help="default 127.0.0.1 (only this machine)")
    a.add_argument("--port", type=int, default=None, help="default 8787")
    a.set_defaults(fn=cmd_app)

    sub.add_parser("daemon", help="scheduler only, no dashboard").set_defaults(fn=cmd_daemon)
    sub.add_parser("run", help="one trading run now").set_defaults(fn=cmd_run)

    s = sub.add_parser("scan", help="dry run: show what it would buy (never trades)")
    s.add_argument("only", nargs="*", help="strategy names (default: all)")
    s.set_defaults(fn=cmd_scan)

    r = sub.add_parser("research", help="look for new edges now")
    r.add_argument("--quick", action="store_true", help="only a few series")
    r.set_defaults(fn=cmd_research)

    t = sub.add_parser("selftest", help="check Kalshi's API still looks right")
    t.add_argument("--order-probe", action="store_true",
                   help="also send one $0.01 order that can't fill (needs a key)")
    t.set_defaults(fn=cmd_selftest)

    sub.add_parser("status", help="a few lines on how it's doing").set_defaults(fn=cmd_status)

    rp = sub.add_parser("report", help="the full report")
    rp.add_argument("--push", action="store_true", help="also put it on the status branch")
    rp.add_argument("--json", action="store_true", help="print JSON")
    rp.set_defaults(fn=cmd_report)

    h = sub.add_parser("health", help="disk, clock, timer, backups, last runs, last error")
    h.add_argument("--alive", action="store_true",
                   help="only check the timer is running and there is disk space (no network)")
    h.set_defaults(fn=cmd_health)

    sub.add_parser("backup", help="back up the books now").set_defaults(fn=cmd_backup)

    rs = sub.add_parser("restore", help="list backups, or put one back")
    rs.add_argument("day", nargs="?", help="the backup's date, like 2026-10-24 (leave out to list them)")
    rs.add_argument("--yes", action="store_true", help="really do it")
    rs.set_defaults(fn=cmd_restore)

    lg = sub.add_parser("logs", help="the last 50 lines of the log")
    lg.add_argument("-n", "--lines", type=_positive, default=50, help="how many lines (default 50)")
    lg.set_defaults(fn=cmd_logs)

    sm = sub.add_parser("summary", help="yesterday's phone summary")
    sm.add_argument("--send", action="store_true", help="also send it to your phone (tests alerts)")
    sm.set_defaults(fn=cmd_summary)

    sub.add_parser("setup", help="set it up: five plain questions").set_defaults(fn=cmd_setup)

    on = sub.add_parser("on", help="turn the bot on")
    on.add_argument("--budget", type=_money, default=None, help="dollars it may use (and at most lose)")
    on.add_argument("--clear-trip", action="store_true", help="clear a safety stop (find out why first)")
    on.add_argument("--env", choices=("demo", "prod"), default=None,
                    help="demo = Kalshi's fake-money site, prod = real money")
    on.set_defaults(fn=cmd_on)

    sub.add_parser("off", help="turn the bot off").set_defaults(fn=cmd_off)

    st = sub.add_parser("set", help="change a setting: set env demo / set strategies.ladder_arb.mode live")
    st.add_argument("key")
    st.add_argument("value")
    st.set_defaults(fn=cmd_set)
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return 2
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
