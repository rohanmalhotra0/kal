"""Trade only where the bot's fee math is right.

The bot prices every order at Kalshi's taker fee, round_up(0.07 x M x C x P x (1 - P)),
with M = 1. That is an upper bound only for the "quadratic" fee types with M <= 1. Kalshi
has used a flat fee before (the 2025 Super Bowl: $0.01 a contract, more than the formula
at the tails), and lists other fee types. A fee the bot can't price could turn a
"locked-in" profit into a loss, or make the books show less lost than Kalshi took.

So before any trade (paper too), each market's series fee, and any event-level override
Kalshi has scheduled, must be a known quadratic type with 0 <= M <= 1. Seen on the real
API (2026-10-25): every one of ~15,400 series passes; 6 event overrides, all quadratic.
"""
import time

from . import db
from .client import KalshiError

QUADRATIC = ("quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees")
SERIES_KEY, OVERRIDES_KEY = "series_fees", "event_fee_overrides"
SERIES_TTL = 6 * 3600     # series fees change rarely, and Kalshi schedules changes ahead
OVERRIDES_TTL = 3600


def series_of(ticker):
    """KXHIGHNY-25OCT24-T60 -> KXHIGHNY. If that isn't a real series, Kalshi says 404 and
    the market is refused."""
    return (ticker or "").split("-")[0]


def _bad(fee_type, mult):
    if fee_type not in QUADRATIC:
        return f"fee type {fee_type!r} isn't one the bot can price"
    try:
        m = float(mult)
    except (TypeError, ValueError):
        return f"fee multiplier {mult!r} isn't a number"
    if not 0 <= m <= 1:
        return f"fee multiplier {m:g} is above what the bot prices"
    return None


def _cached(key, ttl, now):
    rec = db.get(key) or {}
    return rec if now - rec.get("ts", 0) < ttl else None


def _series_fees(client, series, now):
    """(fee_type, multiplier), or a reason string if they can't be read."""
    cache = db.get(SERIES_KEY) or {}
    hit = cache.get(series)
    if hit and now - hit[2] < SERIES_TTL:
        return hit[0], hit[1]
    try:
        info = client.series_info(series)
    except (KalshiError, OSError, ValueError) as e:
        return f"couldn't read its fees ({e})"
    if not isinstance(info, dict) or "fee_type" not in info:
        return "Kalshi sent no fee type"
    cache[series] = [info.get("fee_type"), info.get("fee_multiplier"), now]
    db.put(SERIES_KEY, cache)
    return info.get("fee_type"), info.get("fee_multiplier")


def _overrides(client, now):
    """{event_ticker: [(fee_type, multiplier), ...]}, or a reason string."""
    rec = _cached(OVERRIDES_KEY, OVERRIDES_TTL, now)
    if rec is None:
        try:
            rows = client.event_fee_changes()
        except (KalshiError, OSError, ValueError) as e:
            return f"couldn't read Kalshi's event fee changes ({e})"
        by_event = {}
        for r in rows if isinstance(rows, list) else []:
            if isinstance(r, dict) and r.get("event_ticker"):
                by_event.setdefault(r["event_ticker"], []).append(
                    [r.get("fee_type_override"), r.get("fee_multiplier_override")])
        rec = {"ts": now, "events": by_event}
        db.put(OVERRIDES_KEY, rec)
    return rec["events"]


def check(client, sig, now=None):
    """Why this signal's fees can't be trusted, or None. Never raises."""
    now = time.time() if now is None else now
    overrides = _overrides(client, now)
    if isinstance(overrides, str):
        return f"fees: {overrides}"
    for leg in sig.legs:
        series = series_of(leg.ticker)
        got = _series_fees(client, series, now)
        if isinstance(got, str):
            return f"fees: {series}: {got}"
        why = _bad(*got)
        if why:
            return f"fees: {series}: {why}"
        # An override can be scheduled for later; any one that would break the math
        # counts now (null fields only clear an override, so they are fine).
        for event in dict.fromkeys(events_of(leg.ticker) + [sig.event]):
            for fee_type, mult in overrides.get(event, []):
                if fee_type is None and mult is None:
                    continue
                why = _bad(fee_type if fee_type is not None else got[0], mult if mult is not None else got[1])
                if why:
                    return f"fees: {event}: an event fee change: {why}"
    return None


def events_of(ticker):
    """Every event a market ticker can belong to: each "-" prefix past the series. A
    negative strike has a "-" of its own (KXCPI-26OCT-T-0.1 is in KXCPI-26OCT), so the
    last "-" isn't always where the event ends, and a signal can span two events."""
    parts = str(ticker).split("-")
    return ["-".join(parts[:i]) for i in range(2, len(parts))]
