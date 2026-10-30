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

from . import books, config, db, fees, flow, gate, ledger, paper, risk, sizing
from .client import (Kalshi, KalshiError, UnknownSettlement, every_order, fee, fill_count, is_transient,
                     new_order_id, resting, ts)
from .strategies import load_all
from .strategies.base import Context

log = logging.getLogger(__name__)
LIVE_MODES = ("paper", "live")
RECONCILE_GRACE = 600   # seconds: an order missing from Kalshi this long after sending never landed


DAY = 86400
OPEN_EVENTS = (("status", "open"), ("with_nested_markets", "true"))   # every strategy's big list
# Market fields no code reads (tests/test_engine_memory.py checks the code never names one).
# The run's copy of all open events drops them: it must fit in the server's memory cap.
UNREAD = ("price_ranges", "early_close_condition", "updated_time", "latest_expiration_time", "no_sub_title",
          "settlement_timer_seconds", "price_level_structure", "response_price_units", "notional_value",
          "notional_value_dollars", "functional_strike", "previous_price", "previous_price_dollars",
          "last_price", "last_price_dollars", "open_interest", "open_interest_fp")


def _hours(research, depth=0):
    """Every hours_before_close in the research outputs (each longshot cell keeps the one it
    was tested at, which can be older than the config's)."""
    if depth > 6:
        return
    if isinstance(research, dict):
        if "hours_before_close" in research:
            yield research["hours_before_close"]
        research = research.values()
    if isinstance(research, (list, tuple, type({}.values()))):
        for v in research:
            if isinstance(v, (dict, list)):
                yield from _hours(v, depth + 1)


def horizon_days(cfg, research=None):
    """How far ahead any strategy can act. A locked-in trade must settle within max_arb_days:
    Limits.refuse turns down later ones, on paper too, and the strategies skip them before they
    take budget (risk.too_late). Bets look a few hours ahead: each research result's hours before
    close. Anything unreadable: no limit."""
    try:
        hours = [float(cfg.get("research_hours_before_close") or 0)] + [float(h) for h in _hours(research)]
        days = max(float(cfg["max_arb_days"]), max(hours) / 24)
    except (KeyError, TypeError, ValueError):
        return float("inf")
    return days + 1


def _due(v, until):
    """True if v is a time no later than `until`. Missing or unreadable: False."""
    try:
        return bool(v) and ts(v) <= until
    except Exception:
        return False


def _wanted(ev, until, keep):
    """Keep an open event if one of its markets is due by `until`: it closes by then, or the
    scheduled end a bet times itself by (the event's strike_date, the market's
    occurrence_datetime: base.scheduled_end) comes by then. Or if the bot holds one of its
    markets (`keep`), or anything can't be read, even no markets at all: then the strategies
    judge it, as before (fed_link_arb won't trade a meeting it can't place among the others)."""
    try:
        markets = ev.get("markets")
        if not isinstance(markets, list) or not markets or _due(ev.get("strike_date"), until):
            return True
        for m in markets:
            if not isinstance(m, dict) or m.get("ticker") in keep:
                return True
            if ts(m["close_time"]) <= until or _due(m.get("occurrence_datetime"), until):
                return True
        return False
    except Exception:
        return True


_UNREAD = frozenset(UNREAD)


def _lean(ev, pool):
    """One open event, slimmed: the unread fields go, and each text is shared with its copies
    (an event's markets repeat the same rules and times). Each market becomes a new dict just
    big enough for what is left: a dict never shrinks when keys go, so slimming one in place
    left half the markets in a table built for 43+ fields (1,584 bytes, not 832: 40 MB of a
    run's 209 MB on 2026-10-30). Anything odd stays as is."""
    markets = ev.get("markets") if isinstance(ev, dict) else None
    if isinstance(markets, list):
        ev["markets"] = [{k: pool.setdefault(v, v) if type(v) is str else v
                          for k, v in m.items() if k not in _UNREAD} if isinstance(m, dict) else m
                         for m in markets]
    return ev


class RunCache:
    """Wraps the client for one run, so identical list calls (all open events, markets,
    series) hit the API once instead of once per strategy. Order books, balances and
    orders always go straight through: those must be fresh.

    All open events with their markets is 350 MB of JSON (2026-10-29), over 700 MB in
    Python, and research runs alongside within the same cap (deploy/kal.service). So the run
    keeps only the events a strategy can act on (scope()), read a page at a time, and slimmed
    (_lean): on 2026-10-30, 11,433 of 16,345 events, about 170 MB in Python (209 MB before
    _lean made each market a compact copy). A run peaks near 320 MB and gives it back when
    it ends (scheduler.give_back_memory)."""

    CACHED = ("events", "markets", "series")

    def __init__(self, client):
        self._client = client
        self._memo = {}
        self._keep, self._horizon = frozenset(), None
        self.calls = 0  # API list calls actually made (for tests and the run summary)
        self.books_seen = {}  # ticker -> the first order book read this run (kal/books.py)

    def scope(self, keep=(), horizon=None):
        """Before the strategies scan: the events with these tickers (the bot's positions,
        which some strategies count) stay whatever their date; the rest only if one of
        their markets closes within `horizon` days (default: horizon_days of the config)."""
        self._keep, self._horizon = frozenset(keep), horizon
        self._memo.pop(("events", (), OPEN_EVENTS), None)

    def _open_events(self):
        days = self._horizon if self._horizon is not None else horizon_days(config.load())
        until = time.time() + days * DAY
        pages = getattr(self._client, "event_pages", None)
        pages = pages(**dict(OPEN_EVENTS)) if pages else [self._client.events(**dict(OPEN_EVENTS))]
        pool, out = {}, []
        for page in pages:
            out += [_lean(ev, pool) for ev in page if _wanted(ev, until, self._keep)]
        return out

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
                self._memo[key] = self._open_events() if key == ("events", (), OPEN_EVENTS) else attr(*args, **kwargs)
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


def _stop_resting(client, ticker, order):
    """Immediate-or-cancel should never rest. If it does, Kalshi isn't doing what we assume:
    pull it and stop everything. Returns the Halted to raise. The caller leaves the order's
    row unresolved, so the run after Reset looks it up and books what filled before the cancel."""
    try:
        client.cancel(order.get("order_id", ""))
        done = "cancelled it and stopped"
    except Exception as e:
        log.error("cancel of resting order %s failed: %s", order.get("order_id"), e)
        done = (f"the cancel FAILED ({type(e).__name__}): cancel order {order.get('order_id', '?')} "
                f"on Kalshi now")
    config.trip(f"an order on {ticker} stayed on the book (status {order.get('status')!r}); {done}")
    return Halted(f"{ticker} order rested instead of filling or cancelling")


def reconcile(client):
    """Find out what happened to orders whose reply we never got. Raises if we can't.
    Kalshi moves done orders to its historical store after about 15 days, so after a long
    outage the lookup reads that too: never call an order that filled "never placed"."""
    rows = ledger.unresolved_orders()
    fixed = 0
    for r in rows:
        def lookup(store=None):
            return [o for o in every_order(client, ticker=r["ticker"], min_ts=r["ts"] - 60, store=store)
                    if o.get("client_order_id") == r["client_order_id"]]
        found = lookup()
        if not found and time.time() - r["ts"] > RECONCILE_GRACE:
            found = lookup(store=True)   # "never placed" can't be taken back: read the store whatever its cutoff says
        if found:
            o = found[0]
            if resting(o):
                # Still on the book: stop, as when a reply shows it. More can fill before the
                # cancel lands, so its count now isn't booked. It's looked up again after Reset.
                raise _stop_resting(client, r["ticker"], o)
            n = fill_count(o, r["count"])
            if n is None:
                # "canceled" alone can hide a part fill: never book it as nothing. It stays
                # 'unknown', and the bot stops for a person to check it.
                config.trip(f"can't tell how much order {r['client_order_id']} on {r['ticker']} filled "
                            f"(Kalshi says only {o.get('status', 'nothing')!r}). Check it on Kalshi.")
                raise Halted(f"{r['ticker']}: the order lookup doesn't say how much filled")
            db.execute("UPDATE orders SET status = ?, filled = ?, order_id = ? WHERE id = ?",
                       f"reconciled:{o.get('status', '?')}", n, o.get("order_id", ""), r["id"])
            fixed += 1
        elif time.time() - r["ts"] > RECONCILE_GRACE:
            db.execute("UPDATE orders SET status = 'never_placed' WHERE id = ?", r["id"])
            fixed += 1
        else:
            raise OrderPending(f"order {r['client_order_id']} on {r['ticker']} not visible yet")
    return fixed


def _mark(sid, status):
    """Mark a half-built basket reported. Called only once its stop is written, so a write
    that fails here (a locked database) just means it's reported once more after Reset:
    never an error that could hide the stop's reason."""
    try:
        db.execute("UPDATE signals SET status = ? WHERE id = ?", status, sid)
    except Exception:
        log.warning("couldn't mark basket %s reported; the next run reports it again", sid, exc_info=True)


def stranded_baskets():
    """Live baskets an earlier run left part-bought: some legs filled, the rest didn't or
    were never sent (a lost reply, or the process dying between legs). Call after
    reconcile(), so every order is resolved. Returns (signal id, event, status) for each:
    the caller stops the bot, then marks them with that 'stranded' status, so each is
    reported once. Marked first, a process killed before the stop left one never reported."""
    out = []
    for s in db.query("SELECT id, event, legs, status FROM signals WHERE mode = 'live' "
                      "AND (status = 'accepted' OR status LIKE 'failed:%')"):
        legs = json.loads(s["legs"] or "[]")
        fills = [r["filled"] or 0 for r in db.query("SELECT filled FROM orders WHERE signal_id = ?", s["id"])]
        bought = bool(fills) and max(fills) > 0
        whole = len(fills) == len(legs) and bought and min(fills) == max(fills)
        if len(legs) > 1 and bought and not whole:
            out.append((s["id"], s["event"], f"stranded: {sum(f > 0 for f in fills)} of {len(legs)} legs bought"))
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
        coid = new_order_id()
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
        # Nothing here can raise before the row is written: a row left with a final status and
        # 0 filled would never be looked up again, whatever the order really bought.
        n = fill_count(order, want)
        on_book = resting(order)
        # 'unknown' makes the next run look the order up, so whatever filled (before the cancel,
        # or in a reply that doesn't say) lands in the books.
        db.execute("UPDATE orders SET status = ?, filled = ?, order_id = ? WHERE id = ?",
                   "unknown" if on_book or n is None else str(order.get("status") or "?"), n or 0,
                   str(order.get("order_id") or ""), oid)
        if on_book:
            raise _stop_resting(client, leg.ticker, order)
        if n is None:
            # The reply doesn't say how much filled ("canceled" alone can hide a part fill).
            # Never read that as nothing.
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
            count = n  # later legs match what the first leg actually got, a part contract too
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
        _errors_here.pop(config.home(), None)
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
            if isinstance(e, LegFailed):   # _trade stops for these itself; this is only a fallback
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


_errors_here = {}   # KAL_HOME -> failed runs in a row, counted here when the books can't take it


def _count_error(cfg, e):
    # One streak: the failed runs the books couldn't take are added to the books' count, so
    # books that take some writes and not others still stop the bot at max_consecutive_errors.
    held, books_n = _errors_here.get(config.home(), 0), 0
    try:
        books_n = db.get("consecutive_errors", 0) or 0
        errors = books_n + held + 1
        db.put("consecutive_errors", errors)
        _errors_here.pop(config.home(), None)   # (now in the books)
    except Exception as books:
        # No order can be recorded on such books, so none is sent; but every run fails, and
        # the count itself can't be kept there. Damaged: stop now, nothing will change.
        # Busy, locked or full: count here, so a lasting problem still stops the bot.
        if db.damaged(books):
            try:
                stopped = bool(config.load().get("disabled_reason"))
            except Exception:
                stopped = False
            if not stopped:   # the health check may have stopped it (and said so) meanwhile
                config.trip(f"the books are damaged ({books}). Nothing can trade. "
                            f"Put a backup back: {config.command('restore')}")
            return
        _errors_here[config.home()] = held + 1
        n = books_n + held + 1
        if n >= cfg.get("max_consecutive_errors", 3):
            config.trip(f"{n} failed runs in a row, and the books can't be written "
                        f"({books}). Last error: {e}. See: {config.command('health')}")
        return
    if errors >= cfg.get("max_consecutive_errors", 3):
        config.trip(f"{errors} failed runs in a row. Last error: {e}")


def _stop_live(cfg, name):
    """Before each real order: why to stop real trading now, or None. OFF stops it; so does
    this strategy leaving live, or any money limit set stricter since the run began (the
    owner cut the budget, say): the run's limits came from the old settings. The next run
    starts on the new ones. A looser setting waits for the next run too."""
    now = config.load()
    if not now["enabled"]:
        return "turned off mid-run: stopped trading"
    if (now["strategies"].get(name) or {}).get("mode") != "live":
        return f"{name} left live mid-run: this run stopped, the next starts on the new settings"
    tighter = [k for k, safer in config.LIMITS.items()
               if now.get(k) != cfg.get(k) and safer(now[k], cfg[k]) == now[k]]
    if tighter:
        return f"{', '.join(tighter)} set stricter mid-run: this run stopped, the next starts on the new settings"
    return None


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
        # recover() before catch_up(): the bot's own (marked) orders must be in the books before
        # catch_up() reads the other fills since a restore. Otherwise catch_up() could read an
        # outside sale first, book it as the bot's buy of the other side and skip the bot's buy
        # as netted; recover() then put that buy back, and the books held both sides of the
        # market: a payout booked on contracts already sold (cross-PR money-path audit).
        try:
            back = ledger.recover(client)
        except ledger.CatchUpError as e:
            reason = f"can't read the bot's own orders on Kalshi to check its books: {e}. Check them on Kalshi."
            config.trip(reason)
            summary["notes"].append(f"STOPPED: {reason}")
            return
        if back:
            note = (f"Kalshi shows {back} order(s) the bot made that its books didn't have (lost or older "
                    f"books?). They're back in, so the $300 limit still counts them.")
            summary["notes"].append(note)
            try:
                from . import notify
                notify.send("kal put back orders its books had lost", note, tags=("warning",))
            except Exception:
                log.warning("recovery alert failed", exc_info=True)
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
            reason = (f"basket left half-built on {', '.join(event for _, event, _ in stranded)} (an earlier "
                      f"run stopped between legs). Check open positions on Kalshi.")
            config.trip(reason)
            for sid, _, status in stranded:   # only after the stop is written
                _mark(sid, status)
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
                      f"(+ = YES, - = NO). Check that market on Kalshi before resetting. If those are your own "
                      f"trades, this clears once that market settles or you close them.")
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
        # Profit so far: realized, plus open bets at their bids, less their cost. Unlike equity,
        # it doesn't move with the budget. The daily stop and "today" (report, dashboard) use it.
        pnl = equity - capital + ledger.realized()
        db.insert("equity", balance=balance, exposure=open_cost, equity=equity,
                  paper_equity=paper.equity(cfg))
        db.put("last_pnl", {"ts": time.time(), "pnl": pnl})
        # The limits that scale with capital (daily stop, bets cap, bet size) use what the bot
        # could really lose, so a budget typed bigger than the account can't loosen them.
        at_risk = min(capital, balance + open_cost)
        reason = risk.daily_loss(cfg, equity, at_risk, pnl=pnl)
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
    if hasattr(client, "scope"):
        client.scope(held_live | held_paper, horizon_days(cfg, research))   # what the run's events list keeps

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
                      open_exposure=limits.directional_open, held_paper=frozenset(held_paper),
                      held_cost=ledger.open_by_ticker() if is_live else paper.open_by_ticker())
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
                stop = _stop_live(cfg, name)
                if stop:
                    summary["notes"].append(stop)
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
            if not reason and sig_live:
                # Again, last: the rechecks above read Kalshi (seconds, at worst), and a stop
                # or a stricter limit saved meanwhile must still stop this order.
                stop = _stop_live(cfg, name)
                if stop:
                    summary["notes"].append(stop)
                    return
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
                if isinstance(e, LegFailed):
                    # Stop first, then mark 'stranded' (reported once). A process killed in
                    # between leaves it unmarked, so the next run finds it and stops again.
                    config.trip(f"basket left half-built: {e}. Check open positions on Kalshi.")
                    _mark(sid, f"stranded: {e}")
                    raise Halted(str(e)) from e
                db.execute("UPDATE signals SET status = ? WHERE id = ?", f"failed: {e}", sid)   # checked next run
                raise
            limits.spend(sig, spent)
            db.execute("UPDATE signals SET status = ? WHERE id = ?",
                       "executed" if spent else "missed (no fill)", sid)
            summary["live_trades"] += bool(spent)
            held_live.update(leg.ticker for leg in sig.legs)
