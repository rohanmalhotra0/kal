"""Real money waits for proof that Kalshi accepts our orders.

The order format can only be checked for real with a real key. So before the first live
trade for a key + exchange, the engine sends two zero-risk orders, one for each shape real
orders take on Kalshi's single YES book, in a busy market:
  buy 1 YES at $0.01: a bid, where nobody sells YES below $0.10;
  buy 1 NO at $0.01: an ask at $0.99, where nobody bids YES above $0.90 (baskets buy NO).
Immediate-or-cancel, so neither can fill. If Kalshi accepts both, live trading may start.
Any refused order later clears the pass, so the next run checks again. Until then, live
strategies trade on paper.

The result names the key by a short hash of its ID, never the ID itself: the db is copied
into every backup.
"""
import hashlib
import logging
import time

from . import config, db
from .client import KalshiError, fill_known, filled, is_transient, num, resting

KEY = "order_probe"
PROBE_PRICE = 0.01
PROBE_MIN_ASK = 0.10      # YES test: only where the cheapest YES seller asks at least this much
PROBE_MAX_BID = 0.90      # NO test: only where the best YES buyer bids at most this much
PROBE_TRIES = 10          # busy markets to look at
RETRY_SECONDS = 3600      # after a failed probe, wait this long before the next one
VERSION = 3               # a pass counts only from this version: 2 = YES and NO both tested,
                          # 3 = the replies said how much filled

log = logging.getLogger(__name__)


def fingerprint(key_id, env):
    """Which key and exchange a result is for: "prod:" + the first 16 hex of sha256(key ID)."""
    return f"{env}:{hashlib.sha256((key_id or 'no-key').encode()).hexdigest()[:16]}"


def _fingerprint(client, env):
    return fingerprint(getattr(client, "key_id", None), env)


def status():
    rec = db.get(KEY) or {}
    if isinstance(rec.get("fingerprint"), str) and not rec.get("hashed"):
        rec = _upgrade(rec)
    return rec


def _upgrade(rec):
    """Results saved before the hash say "prod:<key ID>". Hash that in place: the plain ID
    leaves the books, and a pass still counts, so nobody is sent back to paper by it."""
    env, _, key_id = rec["fingerprint"].partition(":")
    rec = {**rec, "fingerprint": fingerprint(key_id, env), "hashed": True}
    try:
        db.put(KEY, rec)
    except Exception:
        log.warning("could not rewrite the old test order result", exc_info=True)
    return rec


def _counts(rec, fp):
    """A pass for this key + exchange, with both order shapes tested. A pass saved before
    the NO test existed proved only YES, so it's tested again (two orders that can't fill)."""
    try:
        version = int(rec.get("version") or 1)
    except (TypeError, ValueError):
        version = 1
    return bool(rec.get("ok")) and rec.get("fingerprint") == fp and version >= VERSION


def passed(client, env):
    return _counts(status(), _fingerprint(client, env))


def passed_for(key_id, env):
    """passed(), from a key ID (the dashboard has no client)."""
    return key_id is not None and _counts(status(), fingerprint(key_id, env))


def due(client, env):
    """True if a probe should run now: not passed, and no failed try in the last hour
    (a new key or exchange is due at once)."""
    rec = status()
    if passed(client, env):
        return False
    return rec.get("fingerprint") != _fingerprint(client, env) or time.time() - rec.get("ts", 0) >= RETRY_SECONDS


def record(client, env, ok, detail):
    db.put(KEY, {"ok": bool(ok), "fingerprint": _fingerprint(client, env), "hashed": True,
                 "version": VERSION, "detail": str(detail)[:300], "ts": time.time()})


def clear(reason):
    """Forget a pass (after Kalshi refused an order), so the next run probes again at once."""
    rec = status()
    if rec.get("ok"):
        db.put(KEY, {"ok": False, "fingerprint": None, "detail": str(reason)[:300], "ts": 0})


def judge(ticker, ask, order, side="yes", bid=None):
    """(ok, detail, alarming). Alarming means the order filled or rested: Kalshi isn't
    doing what we assume, so the bot must stop."""
    what = f"$0.01 {side.upper()} test order"
    if not isinstance(order, dict):
        return False, f"{ticker}: the {what}'s reply is not a dict", False
    status_ = order.get("status")
    n = filled(order, 1)
    if n > 0:
        return False, (f"the {what} on {ticker} FILLED ({n:g}). Orders aren't priced the way "
                       "the bot thinks. It cost at most $0.02; check it on Kalshi."), True
    if resting(order):
        return False, f"the {what} on {ticker} stayed on the book: immediate-or-cancel was ignored.", True
    if not fill_known(order):   # real money waits until replies say how much filled
        return False, f"{ticker}: can't tell from the {what}'s reply how much filled (keys: {sorted(order)[:12]})", False
    book = f"YES ask was ${ask:.2f}" if side == "yes" else f"YES bid was ${bid or 0:.2f}"
    return True, f"{ticker}: {side.upper()} accepted, 0 filled ({book}, status {status_})", False


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
    """Send the two zero-risk orders (YES, then NO) and record the result. Returns
    (ok, detail): ok only if Kalshi accepted both, unfilled. Network trouble raises (the
    run is skipped and the probe tried again next run)."""
    for m in _busy(client.markets(limit=200, status="open", mve_filter="exclude"))[:PROBE_TRIES]:
        t = m["ticker"]
        book = client.orderbook(t)  # fresh, right before the orders
        no_bids = [lv for lv in (book or {}).get("no") or [] if 0 < lv[0] < 1]
        yes_bids = [lv for lv in (book or {}).get("yes") or [] if 0 < lv[0] < 1]
        if not no_bids:
            continue
        ask = round(1 - max(no_bids)[0], 4)                 # the cheapest YES seller
        bid = round(max(yes_bids)[0], 4) if yes_bids else 0.0   # the best YES buyer
        if ask < PROBE_MIN_ASK or bid > PROBE_MAX_BID:
            continue
        details = []
        for side in ("yes", "no"):
            try:
                order = client.buy(t, side, 1, PROBE_PRICE)
            except KalshiError as e:
                if is_transient(e) or not e.rejected:
                    raise
                detail = f"Kalshi refused the $0.01 {side.upper()} test order on {t}: {e}"
                record(client, env, False, detail)
                return False, detail
            ok, detail, alarming = judge(t, ask, order, side, bid)
            if alarming:
                if resting(order):
                    try:
                        client.cancel(order.get("order_id", ""))
                    except Exception as e:
                        detail += f" Cancelling it failed too ({e}): cancel it on Kalshi."
                config.trip(detail)
            if not ok:
                record(client, env, False, detail)
                return False, detail
            details.append(detail)
        detail = "; ".join(details)
        record(client, env, True, detail)
        return True, detail
    detail = (f"no busy market with a YES ask of ${PROBE_MIN_ASK:.2f} or more and a YES bid of "
              f"${PROBE_MAX_BID:.2f} or less right now")
    record(client, env, False, detail)
    return False, detail
