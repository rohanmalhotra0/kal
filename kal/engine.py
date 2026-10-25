"""One trading run: check safety, let each strategy scan, then trade (live or paper).

Called by the scheduler, cron, or `python -m kal run`. Fails closed: anything it
doesn't understand (a bad mode, an order it can't account for, a stranded basket)
stops live trading rather than guessing.
"""
import glob
import json
import logging
import os
import time
import traceback
import uuid

from . import config, db, ledger, paper, risk, sizing
from .client import Kalshi, KalshiError, fee, filled, resting
from .strategies import load_all
from .strategies.base import Context

log = logging.getLogger(__name__)
LIVE_MODES = ("paper", "live")
RECONCILE_GRACE = 600   # seconds: an order missing from Kalshi this long after sending never landed


class RunCache:
    """Wraps the client for one run, so identical list calls (all open events, markets,
    series) hit the API once instead of once per strategy. Order books, balances and
    orders always go straight through: those must be fresh."""

    CACHED = ("events", "markets", "series")

    def __init__(self, client):
        self._client = client
        self._memo = {}
        self.calls = 0  # API list calls actually made (for tests and the run summary)

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if name not in self.CACHED or not callable(attr):
            return attr

        def cached(*args, **kwargs):
            key = (name, args, tuple(sorted(kwargs.items())))
            if key not in self._memo:
                self.calls += 1
                self._memo[key] = attr(*args, **kwargs)
            return list(self._memo[key])
        return cached


class LegFailed(RuntimeError):
    """A multi-leg basket stopped part way. A person should look at what's held."""


class OrderUnknown(RuntimeError):
    """An order may or may not exist. Stop and reconcile before trading again."""


class Halted(RuntimeError):
    """The bot already tripped itself with a specific reason. Just stop."""


def research_outputs():
    """Everything kal.research saved, keyed by file name (e.g. 'edges')."""
    out = {}
    for path in glob.glob(os.path.join(config.home(), "research", "*.json")):
        try:
            with open(path) as f:
                out[os.path.splitext(os.path.basename(path))[0]] = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("skipping unreadable research file %s: %s", path, e)
    return out


def reconcile(client):
    """Find out what happened to orders whose reply we never got. Raises if we can't."""
    rows = ledger.unresolved_orders()
    fixed = 0
    for r in rows:
        found = [o for o in client.orders(ticker=r["ticker"], min_ts=r["ts"] - 60)
                 if o.get("client_order_id") == r["client_order_id"]]
        if found:
            o = found[0]
            if resting(o):
                client.cancel(o["order_id"])
            n = filled(o, r["count"])
            db.execute("UPDATE orders SET status = ?, filled = ?, order_id = ? WHERE id = ?",
                       f"reconciled:{o.get('status', '?')}", n, o.get("order_id", ""), r["id"])
            fixed += 1
        elif time.time() - r["ts"] > RECONCILE_GRACE:
            db.execute("UPDATE orders SET status = 'never_placed' WHERE id = ?", r["id"])
            fixed += 1
        else:
            raise OrderUnknown(f"order {r['client_order_id']} on {r['ticker']} not visible yet")
    return fixed


def execute(client, sig, signal_id):
    """Buy every leg in order. Returns dollars spent.

    Multi-leg signals (arbitrage) only work as a set. If the first leg fills partly, the
    later legs shrink to match. If a later leg comes up short, the basket is stranded:
    raise LegFailed so the bot stops for a person to look."""
    spent, count = 0.0, None
    for i, leg in enumerate(sig.legs):
        want = leg.count if count is None else count
        coid = str(uuid.uuid4())
        # Written before sending, so a crash mid-request still leaves a trace to reconcile.
        oid = db.insert("orders", signal_id=signal_id, strategy=sig.strategy, ticker=leg.ticker,
                        side=leg.side, count=want, price=leg.price, filled=0, status="sending",
                        client_order_id=coid)
        try:
            order = client.buy(leg.ticker, leg.side, want, leg.price, client_order_id=coid)
        except KalshiError as e:
            if not e.rejected:
                db.execute("UPDATE orders SET status = 'unknown', error = ? WHERE id = ?", str(e)[:500], oid)
                raise OrderUnknown(f"{leg.ticker}: {e}") from e
            db.execute("UPDATE orders SET status = 'rejected', error = ? WHERE id = ?", str(e)[:500], oid)
            if i == 0:
                raise
            raise LegFailed(f"leg {i + 1}/{len(sig.legs)} {leg.ticker} rejected after earlier legs filled: {e}")
        except Exception as e:
            db.execute("UPDATE orders SET status = 'unknown', error = ? WHERE id = ?", str(e)[:500], oid)
            raise OrderUnknown(f"{leg.ticker}: {e}") from e
        n = filled(order, want)
        db.execute("UPDATE orders SET status = ?, filled = ?, order_id = ? WHERE id = ?",
                   order.get("status", "?"), n, order.get("order_id", ""), oid)
        if resting(order):
            # Immediate-or-cancel should never rest. If it does, Kalshi isn't doing what we
            # assume: pull it and stop everything.
            try:
                client.cancel(order.get("order_id", ""))
            finally:
                config.trip(f"an order on {leg.ticker} stayed on the book (status "
                            f"{order.get('status')!r}); cancelled it and stopped")
            raise Halted(f"{leg.ticker} order rested instead of filling or cancelling")
        spent += n * leg.price + (fee(n, leg.price) if n else 0)
        if len(sig.legs) == 1:
            break
        if i == 0:
            if n == 0:
                return 0.0  # nothing bought: the chance was gone, no harm done
            count = int(n)  # later legs match what the first leg actually got
        elif n < want:
            raise LegFailed(f"leg {i + 1}/{len(sig.legs)} {leg.ticker} filled {n}/{want}; "
                            f"basket incomplete")
    return spent


def run_once(client=None):
    if not config.load()["enabled"]:
        cfg = config.load()
        return {"ok": True, "skipped": cfg["disabled_reason"] or "bot is off"}
    t0 = time.time()
    with risk.run_lock():
        cfg = config.load()  # re-read under the lock: someone may have pressed OFF meanwhile
        if not cfg["enabled"]:
            return {"ok": True, "skipped": cfg["disabled_reason"] or "bot is off"}
        summary = {"ok": True, "signals": 0, "live_trades": 0, "paper_trades": 0, "notes": []}
        try:
            config.validate(cfg)
            client = client or Kalshi.from_env(config.home(), cfg["env"])
            _run(client, cfg, summary)
            db.put("consecutive_errors", 0)
        except (LegFailed, OrderUnknown, Halted) as e:
            summary.update(ok=False, error=str(e))
            log.error("run stopped: %s", traceback.format_exc())
            if isinstance(e, Halted):
                pass
            elif isinstance(e, LegFailed):
                config.trip(f"basket left half-built: {e}. Check open positions on Kalshi.")
            else:
                _count_error(cfg, e)
        except Exception as e:
            summary.update(ok=False, error=str(e))
            log.error("run failed: %s", traceback.format_exc())
            _count_error(cfg, e)
        db.insert("runs", kind="trade", ok=int(summary["ok"]), duration=time.time() - t0,
                  message=json.dumps(summary)[:4000])
        if summary.get("live_trades"):
            _alert_trades(summary)
        return summary


def _alert_trades(summary):
    try:
        from . import notify
        rows = db.query("SELECT strategy, title, cost, exp_profit, guaranteed FROM signals "
                        "WHERE status = 'executed' ORDER BY id DESC LIMIT ?", summary["live_trades"])
        lines = [f"{'locked-in' if r['guaranteed'] else 'bet'}: ${r['cost']:.2f} on {r['title'][:50]} "
                 f"(expect +${r['exp_profit']:.2f})" for r in rows]
        notify.send(f"kal made {summary['live_trades']} real trade(s)", "\n".join(lines), tags=("moneybag",))
    except Exception:
        log.warning("trade alert failed", exc_info=True)


def _count_error(cfg, e):
    errors = (db.get("consecutive_errors", 0) or 0) + 1
    db.put("consecutive_errors", errors)
    if errors >= cfg.get("max_consecutive_errors", 3):
        config.trip(f"{errors} failed runs in a row. Last error: {e}")


def _still_on():
    return config.load()["enabled"]


def _run(client, cfg, summary):
    client = RunCache(client)
    live = client.can_trade
    positions, balance = [], 0.0
    if live:
        summary["reconciled"] = reconcile(client)
    summary["settled"] = ledger.settle(client)
    summary["paper_settled"] = paper.settle(client)
    capital = ledger.capital(cfg)
    if live:
        balance = client.balance()
        positions = client.positions()
        equity = ledger.equity(client, cfg)
        db.insert("equity", balance=balance, exposure=ledger.open_cost(), equity=equity,
                  paper_equity=paper.equity(cfg))
        reason = risk.daily_loss(cfg, equity)
        if not reason and cfg["budget"] > 0 and capital < 1:
            reason = f"the ${cfg['budget']:.2f} of capital is used up"
        if reason:
            config.trip(reason)
            summary["notes"].append(f"STOPPED: {reason}")
            return
    else:
        db.insert("equity", balance=0, exposure=0, equity=0, paper_equity=paper.equity(cfg))
        summary["notes"].append("no API key: live strategies run as paper")

    research = research_outputs()
    # The ledger caps spending at the bot's capital; the account balance and cash floor cap
    # it again in case the account holds less. A $0 budget means no real trades at all.
    live_room = 0.0 if cfg["budget"] <= 0 else max(0.0, min(ledger.available(cfg), balance - cfg["cash_floor"]))
    live_limits = risk.Limits(cfg, live_room, ledger.open_by_event(), ledger.open_directional(), capital)
    paper_capital = cfg["paper_bankroll"] + paper.realized()
    paper_limits = risk.Limits(cfg, paper.cash(cfg), paper.open_by_event(), paper.open_directional(),
                               paper_capital)
    held_live, held_paper = {p["ticker"] for p in positions}, paper.held()

    # Bet smaller after losses (Grossman-Zhou style); arbitrage sizing doesn't use this.
    live_peak = max(ledger.peak_equity() or 0, cfg["budget"])
    live_bankroll = capital * sizing.drawdown_multiplier(ledger.equity(client, cfg), live_peak) if live else 0.0
    paper_peak = max(paper.peak_equity() or 0, cfg["paper_bankroll"])
    paper_bankroll = paper_capital * sizing.drawdown_multiplier(paper.equity(cfg), paper_peak)

    for name, strat in load_all().items():
        params = cfg["strategies"].get(name, {})
        mode = params.get("mode")
        if mode not in LIVE_MODES:
            continue  # "off", or anything unexpected: never trade it
        probe = Context(client, cfg, mode, 0, paper_bankroll, set(), research, params)
        if mode == "live":
            ok, why = strat.proven(probe)
            if not (ok and live and cfg["budget"] > 0):
                summary["notes"].append(f"{name}: paper instead of live "
                                        f"({why if not ok else 'no API key' if not live else 'budget is $0'})")
                mode = "paper"
        is_live = mode == "live"
        limits = live_limits if is_live else paper_limits
        ctx = Context(client, cfg, mode, limits.budget_left, live_bankroll if is_live else paper_bankroll,
                      held_live if is_live else held_paper, research, params,
                      open_exposure=limits.directional_open)
        try:
            signals = strat.scan(ctx)
        except Exception as e:
            summary["notes"].append(f"{name}: scan failed: {e}")
            log.error("%s scan failed: %s", name, traceback.format_exc())
            continue
        for sig in signals:
            summary["signals"] += 1
            # A strategy can keep an unproven idea (e.g. a newly discovered edge) on paper.
            sig_live = is_live and not sig.meta.get("paper_only")
            if sig_live:
                if not _still_on():
                    summary["notes"].append("turned off mid-run: stopped trading")
                    return
                ctx.budget_left = live_limits.budget_left
                sig = strat.recheck(ctx, sig)
                if sig is None:
                    continue
            limits = live_limits if sig_live else paper_limits
            reason = limits.refuse(sig)
            sid = db.insert("signals", strategy=name, event=sig.event, title=sig.title,
                            mode="live" if sig_live else "paper",
                            guaranteed=int(sig.guaranteed), cost=sig.cost, payout_min=sig.payout_min,
                            exp_profit=sig.exp_profit, note=sig.note, meta=sig.meta,
                            legs=[leg.__dict__ for leg in sig.legs],
                            status=f"skipped: {reason}" if reason else "accepted")
            if reason:
                continue
            if not sig_live:
                paper.record(sig, sid)
                limits.spend(sig)
                summary["paper_trades"] += 1
                held_paper.update(leg.ticker for leg in sig.legs)
                continue
            try:
                spent = execute(client, sig, sid)
            except (LegFailed, OrderUnknown, Halted) as e:
                db.execute("UPDATE signals SET status = ? WHERE id = ?", f"failed: {e}", sid)
                raise
            limits.spend(sig, spent)
            db.execute("UPDATE signals SET status = ? WHERE id = ?",
                       "executed" if spent else "missed (no fill)", sid)
            summary["live_trades"] += bool(spent)
            held_live.update(leg.ticker for leg in sig.legs)
