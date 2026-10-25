"""Command line: python -m kal <command>.

  app [--host --port]     dashboard + scheduler (the normal way to run it)
  daemon                  scheduler only, no dashboard
  run                     one trading run now
  scan                    dry run: what each strategy would buy. Never trades.
  research [--quick]      look for new edges now
  selftest [--order-probe]  check Kalshi's API still looks right
  status                  a few lines on how it's doing
  report [--push] [--json]  the full report (and put it on the status branch)
  on [--budget X]         turn the bot on
  off                     turn the bot off

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
from contextlib import contextmanager

from . import config

DASHBOARD_PASSWORD = "KAL_DASHBOARD_PASSWORD"
LOCAL = ("127.0.0.1", "localhost", "::1")
SHOW = 10   # opportunities shown per strategy by `scan`


def say(*lines):
    for line in lines:
        print(line)


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
    from kal.dashboard.app import main
    params = inspect.signature(main).parameters
    takes_any = any(p.kind is p.VAR_KEYWORD for p in params.values())
    kwargs = {k: v for k, v in {"host": host, "port": args.port}.items()
              if v is not None and (takes_any or k in params)}
    _sigterm_is_ctrl_c()
    main(**kwargs)
    return 0


def cmd_daemon(args):
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
    client = NoOrders(client or Kalshi(env=cfg["env"]))
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


# ---------- on / off ----------

def _where(cfg):
    return "demo (fake money)" if cfg["env"] == "demo" else "prod (REAL money)"


def cmd_on(args):
    stored = config.load()
    if stored["disabled_reason"] and not args.clear_trip:
        say(f"It is stopped: {stored['disabled_reason']}",
            "Find out why first. Then run: python -m kal on --clear-trip")
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
        say("Turn the bot off first: python -m kal off")
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
