"""One trading run: check safety, let each strategy scan, then trade (live or paper).

Called by the dashboard's scheduler, cron, or `python -m kal run`.
"""
import glob
import json
import logging
import os
import time
import traceback
import uuid

from . import config, db, ledger, paper, risk
from .client import Kalshi, fee, filled
from .strategies import load_all
from .strategies.base import Context

log = logging.getLogger(__name__)


class LegFailed(RuntimeError):
    pass


def research_outputs():
    """Everything kal.research saved, keyed by file name (e.g. 'edges')."""
    out = {}
    for path in glob.glob(os.path.join(config.home(), "research", "*.json")):
        with open(path) as f:
            out[os.path.splitext(os.path.basename(path))[0]] = json.load(f)
    return out


def execute(client, sig, signal_id):
    """Buy every leg in order. A multi-leg signal (arbitrage basket) stops at the first leg
    that doesn't fully fill, since the rest only make sense together. Returns dollars spent."""
    spent = 0.0
    for i, leg in enumerate(sig.legs):
        coid = str(uuid.uuid4())
        # Written before sending, so a crash mid-request still leaves a trace.
        oid = db.insert("orders", signal_id=signal_id, strategy=sig.strategy, ticker=leg.ticker,
                        side=leg.side, count=leg.count, price=leg.price, filled=0, status="sending",
                        client_order_id=coid)
        try:
            order = client.buy(leg.ticker, leg.side, leg.count, leg.price, client_order_id=coid)
        except Exception as e:
            db.execute("UPDATE orders SET status = 'error', error = ? WHERE id = ?", str(e)[:500], oid)
            if i == 0:
                raise
            raise LegFailed(f"leg {i + 1}/{len(sig.legs)} {leg.ticker} errored after earlier legs filled: {e}")
        n = filled(order, leg.count)
        db.execute("UPDATE orders SET status = ?, filled = ?, order_id = ? WHERE id = ?",
                   order.get("status", "?"), n, order.get("order_id", ""), oid)
        spent += n * leg.price + (fee(n, leg.price) if n else 0)
        if n < leg.count and len(sig.legs) > 1:
            if i == 0 and n == 0:
                return 0.0  # nothing bought: the opportunity was gone, no harm done
            raise LegFailed(f"leg {i + 1}/{len(sig.legs)} {leg.ticker} filled {n}/{leg.count}; "
                            f"basket incomplete")
    return spent


def run_once(client=None):
    cfg = config.load()
    t0 = time.time()
    if not cfg["enabled"]:
        why = cfg["disabled_reason"] or "bot is off"
        return {"ok": True, "skipped": why}
    with risk.run_lock():
        summary = {"ok": True, "signals": 0, "live_trades": 0, "paper_trades": 0, "notes": []}
        try:
            client = client or Kalshi.from_env(config.home(), cfg["env"])
            _run(client, cfg, summary)
            db.put("consecutive_errors", 0)
        except Exception as e:
            summary.update(ok=False, error=str(e))
            log.error("run failed: %s", traceback.format_exc())
            errors = db.get("consecutive_errors", 0) + 1
            db.put("consecutive_errors", errors)
            if errors >= cfg["max_consecutive_errors"]:
                config.trip(f"{errors} failed runs in a row. Last error: {e}")
        db.insert("runs", kind="trade", ok=int(summary["ok"]), duration=time.time() - t0,
                  message=json.dumps(summary)[:4000])
        return summary


def _run(client, cfg, summary):
    live = client.can_trade
    positions, balance = [], 0.0
    summary["settled"] = ledger.settle(client)
    summary["paper_settled"] = paper.settle(client)
    if live:
        balance = client.balance()
        positions = client.positions()
        equity = ledger.equity(client, cfg)
        db.insert("equity", balance=balance, exposure=ledger.open_cost(), equity=equity,
                  paper_equity=paper.equity(cfg))
        reason = risk.daily_loss(cfg, equity)
        if not reason and cfg["budget"] > 0 and ledger.capital(cfg) < 1:
            reason = f"the ${cfg['budget']:.2f} of capital is used up"
        if reason:
            config.trip(reason)
            summary["notes"].append(f"STOPPED: {reason}")
            return
    else:
        db.insert("equity", balance=0, exposure=0, equity=0, paper_equity=paper.equity(cfg))
        summary["notes"].append("no API key: live strategies run as paper")

    research = research_outputs()
    # The bot's own ledger caps spending at its capital; the account balance and cash floor
    # cap it again in case the account holds less than the ledger thinks.
    live_limits = risk.Limits(cfg, max(0.0, min(ledger.available(cfg), balance - cfg["cash_floor"])))
    paper_limits = risk.Limits(cfg, paper.cash(cfg))
    held_live, held_paper = {p["ticker"] for p in positions}, paper.held()
    bankroll = ledger.capital(cfg) if cfg["budget"] > 0 else cfg["paper_bankroll"]

    for name, strat in load_all().items():
        params = cfg["strategies"].get(name, {})
        mode = params.get("mode", "off")
        if mode == "off":
            continue
        probe = Context(client, cfg, mode, 0, bankroll, set(), research, params)
        if mode == "live":
            ok, why = strat.proven(probe)
            if not (ok and live):
                summary["notes"].append(f"{name}: paper instead of live ({why if not ok else 'no API key'})")
                mode = "paper"
        limits = live_limits if mode == "live" else paper_limits
        held = held_live if mode == "live" else held_paper
        ctx = Context(client, cfg, mode, limits.budget_left, bankroll, held, research, params)
        try:
            signals = strat.scan(ctx)
        except Exception as e:
            summary["notes"].append(f"{name}: scan failed: {e}")
            log.error("%s scan failed: %s", name, traceback.format_exc())
            continue
        for sig in signals:
            summary["signals"] += 1
            if mode == "live" and not sig.meta.get("paper_only"):
                ctx.budget_left = live_limits.budget_left
                sig = strat.recheck(ctx, sig)
                if sig is None:
                    continue
            # A strategy can keep an unproven idea (e.g. a newly discovered edge) on paper.
            sig_mode = "paper" if sig.meta.get("paper_only") else mode
            limits = live_limits if sig_mode == "live" else paper_limits
            reason = limits.refuse(sig)
            sid = db.insert("signals", strategy=name, event=sig.event, title=sig.title, mode=sig_mode,
                            guaranteed=int(sig.guaranteed), cost=sig.cost, payout_min=sig.payout_min,
                            exp_profit=sig.exp_profit, note=sig.note, meta=sig.meta,
                            legs=[leg.__dict__ for leg in sig.legs],
                            status=f"skipped: {reason}" if reason else "accepted")
            if reason:
                continue
            if sig_mode == "paper":
                paper.record(sig, sid)
                limits.spend(sig)
                summary["paper_trades"] += 1
            else:
                try:
                    spent = execute(client, sig, sid)
                except LegFailed as e:
                    db.execute("UPDATE signals SET status = ? WHERE id = ?", f"failed: {e}", sid)
                    limits.spend(sig)  # count it all: we don't know exactly what's held
                    raise
                limits.spend(sig, spent)
                db.execute("UPDATE signals SET status = ? WHERE id = ?",
                           "executed" if spent else "missed (no fill)", sid)
                summary["live_trades"] += bool(spent)
            (held_live if sig_mode == "live" else held_paper).update(leg.ticker for leg in sig.legs)
