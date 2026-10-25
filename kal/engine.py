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

from . import books, config, db, fees, flow, gate, ledger, paper, risk, sizing
from .client import Kalshi, KalshiError, UnknownSettlement, fee, fill_known, filled, is_transient, resting
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
        self.books_seen = {}  # ticker -> the first order book read this run (kal/books.py)

    def orderbook(self, ticker):
        book = self._client.orderbook(ticker)
        self.books_seen.setdefault(ticker, book)
        return book

    def best_bid(self, ticker, side="yes"):
        """Like the client's, but through orderbook() above, so the book is saved too."""
        levels = self.orderbook(ticker)[side]
        return levels[0] if levels else (0.0, 0.0)

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


class OrderPending(OrderUnknown):
    """An order we sent isn't visible on Kalshi yet. Nothing trades until it is (or until
    RECONCILE_GRACE says it never landed), but waiting isn't a failure."""


class Halted(RuntimeError):
    """The bot already tripped itself with a specific reason. Just stop."""


def research_outputs():
    """Everything kal.research saved for the exchange in use, keyed by file name (e.g.
    'edges'). Demo keeps its own (research-demo/, like kal-demo.db), so a range proven on
    demo's thin books never trades real money (kal.research.backtest.folder)."""
    env = db.current_env()   # the run's own exchange (db.pinned), like its books
    folder = os.path.join(config.home(), "research" if env == "prod" else f"research-{env}")
    out = {}
    for path in glob.glob(os.path.join(folder, "*.json")):
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
            raise OrderPending(f"order {r['client_order_id']} on {r['ticker']} not visible yet")
    return fixed


def stranded_baskets():
    """Live baskets an earlier run left part-bought: some legs filled, the rest didn't or
    were never sent (a lost reply, or the process dying between legs). Each is marked
    'stranded' so it's reported once. Call after reconcile(), so every order is resolved.
    Returns their events."""
    out = []
    for s in db.query("SELECT id, event, legs, status FROM signals WHERE mode = 'live' "
                      "AND (status = 'accepted' OR status LIKE 'failed:%')"):
        legs = json.loads(s["legs"] or "[]")
        fills = [r["filled"] or 0 for r in db.query("SELECT filled FROM orders WHERE signal_id = ?", s["id"])]
        bought = bool(fills) and max(fills) > 0
        whole = len(fills) == len(legs) and bought and min(fills) == max(fills)
        if len(legs) > 1 and bought and not whole:
            db.execute("UPDATE signals SET status = ? WHERE id = ?",
                       f"stranded: {sum(f > 0 for f in fills)} of {len(legs)} legs bought", s["id"])
            out.append(s["event"])
        elif s["status"] == "accepted":   # a run died after its last order: just finish the record
            db.execute("UPDATE signals SET status = ? WHERE id = ?",
                       "executed" if bought else "missed (no fill)", s["id"])
    return out


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
        except Exception as e:
            if isinstance(e, KalshiError) and e.rejected:
                db.execute("UPDATE orders SET status = 'rejected', error = ? WHERE id = ?", str(e)[:500], oid)
                if not is_transient(e):
                    gate.clear(f"Kalshi refused an order: {e}")  # re-prove the format on the next run
                if i == 0:
                    raise
                raise LegFailed(f"leg {i + 1}/{len(sig.legs)} {leg.ticker} rejected after earlier legs filled: {e}")
            # No clear answer: the order may exist. The next run looks it up.
            db.execute("UPDATE orders SET status = 'unknown', error = ? WHERE id = ?", str(e)[:500], oid)
            if i == 0:
                raise OrderUnknown(f"{leg.ticker}: {e}") from e
            # Earlier legs filled and this one may not have: the basket is part-bought.
            raise LegFailed(f"leg {i + 1}/{len(sig.legs)} {leg.ticker} got no clear reply after earlier "
                            f"legs filled ({e})") from e
        n = filled(order, want)
        db.execute("UPDATE orders SET status = ?, filled = ?, order_id = ? WHERE id = ?",
                   order.get("status", "?"), n, order.get("order_id", ""), oid)
        if resting(order):
            # Immediate-or-cancel should never rest. If it does, Kalshi isn't doing what we
            # assume: pull it and stop everything. 'unknown' makes the next run look the order
            # up, so whatever filled before the cancel lands in the books.
            db.execute("UPDATE orders SET status = 'unknown' WHERE id = ?", oid)
            try:
                client.cancel(order.get("order_id", ""))
                done = "cancelled it and stopped"
            except Exception as e:
                log.error("cancel of resting order %s failed: %s", order.get("order_id"), e)
                done = (f"the cancel FAILED ({type(e).__name__}): cancel order {order.get('order_id', '?')} "
                        f"on Kalshi now")
            config.trip(f"an order on {leg.ticker} stayed on the book (status {order.get('status')!r}); {done}")
            raise Halted(f"{leg.ticker} order rested instead of filling or cancelling")
        if not fill_known(order):
            # The reply doesn't say how much filled ("canceled" alone can hide a part fill).
            # Never read that as nothing: 'unknown' makes the next run look the order up.
            db.execute("UPDATE orders SET status = 'unknown' WHERE id = ?", oid)
            if i == 0:
                raise OrderUnknown(f"{leg.ticker}: Kalshi's reply doesn't say how much filled")
            raise LegFailed(f"leg {i + 1}/{len(sig.legs)} {leg.ticker}: Kalshi's reply doesn't say how much "
                            f"filled after earlier legs filled")
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
        with db.pinned(cfg.get("env", "prod")):  # this run's books, even if env changes meanwhile
            return _run_once(client, cfg, t0)


def _run_once(client, cfg, t0):
    summary = {"ok": True, "signals": 0, "live_trades": 0, "paper_trades": 0, "notes": []}
    try:
        config.validate(cfg)
        client = client or Kalshi.from_env(config.home(), cfg["env"])
        _run(client, cfg, summary)
        db.put("consecutive_errors", 0)
        db.put("last_ok_run", time.time())
    except (LegFailed, OrderUnknown, Halted) as e:
        summary.update(ok=False, error=str(e))
        if isinstance(e, OrderPending) or (isinstance(e, OrderUnknown) and is_transient(e.__cause__)):
            # A lost reply or an order not visible yet: the next runs look it up, and nothing
            # trades until they do. A network blip isn't a failure, so it isn't counted.
            summary["waiting"] = True
            log.warning("run waiting on an order: %s", e)
        else:
            log.error("run stopped: %s", traceback.format_exc())
            if isinstance(e, LegFailed):
                config.trip(f"basket left half-built: {e}. Check open positions on Kalshi.")
            elif not isinstance(e, Halted):
                _count_error(cfg, e)
    except Exception as e:
        summary.update(ok=False, error=str(e))
        if is_transient(e):
            # Kalshi down, maintenance, network blip: try again next run. Not a reason to
            # stop, since nothing can be bought while it lasts.
            summary["transient"] = True
            log.warning("run skipped, Kalshi unreachable: %s", e)
            db.put("last_transient_error", {"ts": time.time(), "error": str(e)[:300]})
            _maybe_alert_outage(e)
        else:
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
        notify.send(f"kal made {summary['live_trades']} real trade(s)", "\n".join(lines),
                    priority="low", tags=("moneybag",))  # nothing to do, so no buzz at night
    except Exception:
        log.warning("trade alert failed", exc_info=True)


OUTAGE_ALERT_AFTER = 6 * 3600


def _maybe_alert_outage(e):
    """One phone alert when Kalshi has been unreachable for 6+ hours (then at most every 6)."""
    now = time.time()
    last_ok = db.get("last_ok_run") or now
    last_alert = db.get("last_outage_alert") or 0
    if now - last_ok >= OUTAGE_ALERT_AFTER and now - last_alert >= OUTAGE_ALERT_AFTER:
        db.put("last_outage_alert", now)
        try:
            from . import notify
            notify.send("kal can't reach Kalshi", f"No good run for {(now - last_ok) / 3600:.0f} hours. "
                        f"It keeps trying and won't trade until it can. Last error: {str(e)[:200]}",
                        priority="high", tags=("warning",))
        except Exception:
            log.warning("outage alert failed", exc_info=True)


def _alert_once(key, title, message, every=86400):
    """A phone alert at most once a day per kind."""
    last = (db.get("alerted") or {}).get(key, 0)
    if time.time() - last < every:
        return
    db.put("alerted", {**(db.get("alerted") or {}), key: time.time()})
    try:
        from . import notify
        notify.send(title, message, priority="high", tags=("warning",))
    except Exception:
        log.warning("alert failed", exc_info=True)


def _count_error(cfg, e):
    errors = (db.get("consecutive_errors", 0) or 0) + 1
    db.put("consecutive_errors", errors)
    if errors >= cfg.get("max_consecutive_errors", 3):
        config.trip(f"{errors} failed runs in a row. Last error: {e}")


def _still_on():
    return config.load()["enabled"]


def _write_off_sold(client, positions, summary):
    """Kalshi holds fewer of the bot's contracts than its books say on a market that's still
    trading: someone sold them outside the bot. Count them as lost, so the books never earn
    on contracts they don't hold. (Closed markets are skipped: Kalshi may be paying out.)"""
    short = ledger.shortfalls(positions)
    if not short:
        return 0
    markets = client.markets_by_ticker({t for t, _, _ in short})
    done = 0
    for ticker, side, n in short:
        if markets.get(ticker, {}).get("status") not in ledger.OPEN:
            continue
        lost = ledger.write_off(ticker, side, n, "Kalshi holds fewer than the books: sold outside the bot?")
        done += 1
        note = (f"Kalshi shows {n:.0f} fewer {side.upper()} on {ticker} than the bot bought. "
                f"It counted them as lost (${lost:.2f}). If you sold them, that's fine.")
        summary["notes"].append(note)
        try:
            from . import notify
            notify.send("kal wrote off some contracts", note, tags=("warning",))
        except Exception:
            log.warning("write-off alert failed", exc_info=True)
    return done


def _run(client, cfg, summary):
    cache = RunCache(client)
    try:
        return _trade(cache, cfg, summary)
    finally:
        summary["books_saved"] = books.save(cache.books_seen)


def _trade(client, cfg, summary):
    status = client.exchange_status() if hasattr(client, "exchange_status") else {}
    if status and not status.get("trading_active", True):
        summary["skipped"] = "Kalshi is closed for trading right now (maintenance or off hours)"
        return
    live = client.can_trade
    positions, balance = [], 0.0
    if live:
        summary["reconciled"] = reconcile(client)
        try:
            added = ledger.catch_up(client)
        except ledger.CatchUpError as e:
            reason = (f"the books were restored, but the bot can't read Kalshi's fills to bring them up "
                      f"to date: {e}. Check the trades since the backup on Kalshi.")
            config.trip(reason)
            summary["notes"].append(f"STOPPED: {reason}")
            return
        if added:
            summary["notes"].append(f"added {added} order(s) from Kalshi that the restored books were missing")
        stranded = stranded_baskets()
        if stranded:
            reason = (f"basket left half-built on {', '.join(stranded)} (an earlier run stopped between "
                      f"legs). Check open positions on Kalshi.")
            config.trip(reason)
            summary["notes"].append(f"STOPPED: {reason}")
            return
    try:
        summary["settled"] = ledger.settle(client)
    except UnknownSettlement as e:
        reason = f"a market settled in a way the bot doesn't understand: {e}. Check it on Kalshi."
        config.trip(reason)
        summary["notes"].append(f"STOPPED: {reason}")
        return
    summary["paper_settled"] = paper.settle(client)
    capital = ledger.capital(cfg)
    at_risk = capital
    if live:
        balance = client.balance()
        positions = client.positions()
        odd = ledger.unexplained(positions)
        if odd:
            t, held, mine = odd[0]
            reason = (f"Kalshi shows {held:+.0f} contracts on {t} but the bot's books show {mine:+.0f} "
                      f"(+ = YES, - = NO). Check that market on Kalshi before resetting.")
            config.trip(reason)
            summary["notes"].append(f"STOPPED: {reason}")
            return
        if db.get("restore_check"):
            db.execute("DELETE FROM kv WHERE key = 'restore_check'")   # one look, then Reset decides
            unknown = ledger.strangers(positions)
            if unknown:
                reason = (f"after the restore, Kalshi shows positions the books don't know: "
                          f"{', '.join(unknown[:5])}. If they're yours, not the bot's, press Reset.")
                config.trip(reason)
                summary["notes"].append(f"STOPPED: {reason}")
                return
        if _write_off_sold(client, positions, summary):
            capital = ledger.capital(cfg)
        equity = ledger.equity(client, cfg)
        open_cost = ledger.open_cost()
        db.insert("equity", balance=balance, exposure=open_cost, equity=equity,
                  paper_equity=paper.equity(cfg))
        # The limits that scale with capital (daily stop, bets cap, bet size) use what the bot
        # could really lose, so a budget typed bigger than the account can't loosen them.
        at_risk = min(capital, balance + open_cost)
        reason = risk.daily_loss(cfg, equity, at_risk, pnl=equity - capital + ledger.realized())
        if not reason and cfg["budget"] > 0 and capital < 1:
            reason = f"the ${cfg['budget']:.2f} of capital is used up"
        if reason:
            config.trip(reason)
            summary["notes"].append(f"STOPPED: {reason}")
            return
    else:
        db.insert("equity", balance=0, exposure=0, equity=0, paper_equity=paper.equity(cfg))
        summary["notes"].append("no API key: live strategies run as paper")

    # Real money waits until Kalshi has accepted both zero-risk test orders for this key.
    wants_live = live and cfg["budget"] > 0 and any(
        (s or {}).get("mode") == "live" for s in cfg["strategies"].values())
    proven_orders = gate.passed(client, cfg["env"])
    if wants_live and not proven_orders:
        detail = gate.status().get("detail", "")
        if gate.due(client, cfg["env"]):
            proven_orders, detail = gate.probe(client, cfg["env"])
            summary["order_probe"] = detail
            if not config.load()["enabled"]:
                summary["notes"].append(f"STOPPED: {detail}")
                return
        if not proven_orders:
            summary["notes"].append(f"real trading waits for the $0.01 test orders: {detail}")
            _alert_once("probe", "kal is trading on paper for now",
                        f"Its $0.01 test orders didn't pass yet: {detail}. It tries again every hour.")

    research = research_outputs()
    # The ledger caps spending at the bot's capital; the account balance and cash floor cap
    # it again in case the account holds less. A $0 budget means no real trades at all.
    live_room = 0.0 if cfg["budget"] <= 0 else max(0.0, min(ledger.available(cfg), balance - cfg["cash_floor"]))
    live_limits = risk.Limits(cfg, live_room, ledger.open_by_event(), ledger.open_directional(), at_risk)
    paper_capital = cfg["paper_bankroll"] + paper.realized()
    paper_limits = risk.Limits(cfg, paper.cash(cfg), paper.open_by_event(), paper.open_directional(),
                               paper_capital)
    held_live, held_paper = {p["ticker"] for p in positions}, paper.held()
    paper_kinds = paper.open_cost_by_kind()   # kinds that may hold only a share of paper money

    # Bet smaller after losses (Grossman-Zhou style); arbitrage sizing doesn't use this.
    live_peak = max(ledger.peak_equity() or 0, cfg["budget"])
    live_bankroll = at_risk * sizing.drawdown_multiplier(ledger.equity(client, cfg), live_peak) if live else 0.0
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
            if not (ok and live and cfg["budget"] > 0 and proven_orders):
                why = (why if not ok else "no API key" if not live else "budget is $0" if cfg["budget"] <= 0
                       else "the $0.01 test orders haven't passed")
                summary["notes"].append(f"{name}: paper instead of live ({why})")
                mode = "paper"
        is_live = mode == "live"
        limits = live_limits if is_live else paper_limits
        ctx = Context(client, cfg, mode, limits.budget_left, live_bankroll if is_live else paper_bankroll,
                      held_live if is_live else held_paper, research, params,
                      open_exposure=limits.directional_open, held_paper=frozenset(held_paper))
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
            if not reason and not sig.guaranteed:
                reason = flow.veto(client, sig)  # paper too, so practice results match real ones
            if not reason:
                reason = fees.check(client, sig)
            kind, share = sig.meta.get("paper_kind"), sig.meta.get("paper_share")
            if not reason and not sig_live and kind and share is not None:
                if paper_kinds.get(kind, 0.0) + sig.cost > float(share) * paper_capital + 1e-9:
                    reason = f"over its {float(share):.0%} share of the paper money"
            sid = db.insert("signals", strategy=name, event=sig.event, title=sig.title,
                            mode="live" if sig_live else "paper",
                            guaranteed=int(sig.guaranteed), cost=sig.cost, payout_min=sig.payout_min,
                            exp_profit=sig.exp_profit, note=sig.note, meta=sig.meta,
                            legs=[leg.__dict__ for leg in sig.legs],
                            status=f"skipped: {reason}" if reason else "accepted")
            if reason:
                continue
            if not sig_live:
                if any(leg.ticker in held_paper for leg in sig.legs):
                    db.execute("UPDATE signals SET status = 'skipped: already held on paper' WHERE id = ?", sid)
                    continue  # a copy would count as a second win or loss and fool promotion
                paper.record(sig, sid)
                limits.spend(sig)
                if kind:
                    paper_kinds[kind] = paper_kinds.get(kind, 0.0) + sig.cost
                summary["paper_trades"] += 1
                held_paper.update(leg.ticker for leg in sig.legs)
                continue
            try:
                spent = execute(client, sig, sid)
            except (LegFailed, OrderUnknown, Halted) as e:
                # 'stranded' is already reported (the trip); 'failed' gets checked next run.
                kind = "stranded" if isinstance(e, LegFailed) else "failed"
                db.execute("UPDATE signals SET status = ? WHERE id = ?", f"{kind}: {e}", sid)
                raise
            limits.spend(sig, spent)
            db.execute("UPDATE signals SET status = ? WHERE id = ?",
                       "executed" if spent else "missed (no fill)", sid)
            summary["live_trades"] += bool(spent)
            held_live.update(leg.ticker for leg in sig.legs)
