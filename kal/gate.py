"""Real money waits for proof that Kalshi accepts our orders.

The order format can only be checked for real with a real key. So before the first live
trade for a key + exchange, the engine sends one zero-risk order: buy 1 YES at $0.01,
immediate-or-cancel, in a busy market where nobody sells below $0.10. It can't fill.
If Kalshi accepts it, live trading may start. Any refused order later clears the pass,
so the next run checks again. Until then, live strategies trade on paper.
"""
import time

from . import config, db
from .client import KalshiError, filled, is_transient, num, resting

KEY = "order_probe"
PROBE_PRICE = 0.01
PROBE_MIN_ASK = 0.10      # only where the cheapest seller asks at least this much
PROBE_TRIES = 10          # busy markets to look at
RETRY_SECONDS = 3600      # after a failed probe, wait this long before the next one


def _fingerprint(client, env):
    return f"{env}:{getattr(client, 'key_id', None) or 'no-key'}"


def status():
    return db.get(KEY) or {}


def passed(client, env):
    rec = status()
    return bool(rec.get("ok")) and rec.get("fingerprint") == _fingerprint(client, env)


def due(client, env):
    """True if a probe should run now: not passed, and no failed try in the last hour
    (a new key or exchange is due at once)."""
    rec = status()
    if passed(client, env):
        return False
    return rec.get("fingerprint") != _fingerprint(client, env) or time.time() - rec.get("ts", 0) >= RETRY_SECONDS


def record(client, env, ok, detail):
    db.put(KEY, {"ok": bool(ok), "fingerprint": _fingerprint(client, env), "detail": str(detail)[:300],
                 "ts": time.time()})


def clear(reason):
    """Forget a pass (after Kalshi refused an order), so the next run probes again at once."""
    rec = status()
    if rec.get("ok"):
        db.put(KEY, {"ok": False, "fingerprint": None, "detail": str(reason)[:300], "ts": 0})


def judge(ticker, ask, order):
    """(ok, detail, alarming). Alarming means the order filled or rested: Kalshi isn't
    doing what we assume, so the bot must stop."""
    if not isinstance(order, dict):
        return False, f"{ticker}: the order reply is not a dict", False
    status_ = order.get("status")
    n = filled(order, 1)
    if n > 0:
        return False, (f"the $0.01 test order on {ticker} FILLED ({n:g}). Orders aren't priced the way "
                       "the bot thinks. It cost at most $0.02; check it on Kalshi."), True
    if resting(order):
        return False, f"the $0.01 test order on {ticker} stayed on the book: immediate-or-cancel was ignored.", True
    known = "fill_count_fp" in order or "fill_count" in order or status_ in ("canceled", "cancelled", "executed")
    if not known:
        return False, f"{ticker}: can't tell from the reply how much filled (keys: {sorted(order)[:12]})", False
    return True, f"{ticker}: accepted, 0 filled (YES ask was ${ask:.2f}, status {status_})", False


def _busy(markets):
    def vol(m):
        try:
            return num(m, "volume_24h") or num(m, "volume")
        except (TypeError, ValueError):
            return 0
    ok = [m for m in markets or [] if isinstance(m, dict) and m.get("ticker")
          and m.get("status", "active") in ("active", "open") and not m["ticker"].startswith("KXMVE")]
    return sorted(ok, key=vol, reverse=True)


def probe(client, env):
    """Send the zero-risk order and record the result. Returns (ok, detail).
    Network trouble raises (the run is skipped and the probe tried again next run)."""
    for m in _busy(client.markets(limit=200, status="open", mve_filter="exclude"))[:PROBE_TRIES]:
        t = m["ticker"]
        book = client.orderbook(t)  # fresh, right before the order
        no_bids = [lv for lv in (book or {}).get("no") or [] if 0 < lv[0] < 1]
        if not no_bids:
            continue
        ask = round(1 - max(no_bids)[0], 4)
        if ask < PROBE_MIN_ASK:
            continue
        try:
            order = client.buy(t, "yes", 1, PROBE_PRICE)
        except KalshiError as e:
            if is_transient(e) or not e.rejected:
                raise
            detail = f"Kalshi refused the $0.01 test order on {t}: {e}"
            record(client, env, False, detail)
            return False, detail
        ok, detail, alarming = judge(t, ask, order)
        if alarming:
            if resting(order):
                try:
                    client.cancel(order.get("order_id", ""))
                except Exception as e:
                    detail += f" Cancelling it failed too ({e}): cancel it on Kalshi."
            config.trip(detail)
        record(client, env, ok, detail)
        return ok, detail
    detail = f"no busy market with a YES ask of ${PROBE_MIN_ASK:.2f} or more right now"
    record(client, env, False, detail)
    return False, detail
