"""Skip a bet when the last hour of trades says someone knows more.

Research on Kalshi trade data (Bartlett & O'Hara; Kalshi's own studies) finds informed
traders take liquidity, and prices then move their way. A small taker can't out-guess
them. So before any bet (never a locked-in trade, which can't lose that way), the engine
reads the market's public trades from the last hour. If most of the volume took the other
side, or one trade far bigger than the rest just did, it skips the bet.

The thresholds come from the research, not from fitting: kal has no history to fit yet.
"""
import statistics
import time

from .client import KalshiError, num, price, ts

WINDOW = 3600          # look at the last hour
MIN_TRADES = 5         # fewer trades than this say nothing
AGAINST_SHARE = 0.70   # this much of the volume on the other side: skip
SWEEP_X = 5            # one trade this many times the median size...
SWEEP_DOLLARS = 50.0   # ...worth at least this much, on the other side: skip


def against(trades, side, now, window=WINDOW):
    """(share of contracts whose taker bought the other side, biggest such trade in dollars
    if it was a sweep else 0, how many trades) over the last `window` seconds."""
    recent = []
    for t in trades or []:
        try:
            when, count, took = ts(t["created_time"]), num(t, "count"), t.get("taker_side")
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if now - window <= when <= now + 60 and count > 0 and took in ("yes", "no"):
            recent.append((took, count, price(t, f"{took}_price")))
    if len(recent) < MIN_TRADES:
        return 0.0, 0.0, len(recent)
    total = sum(c for _, c, _ in recent)
    other = [(c, p) for took, c, p in recent if took != side]
    share = sum(c for c, _ in other) / total
    median = statistics.median(c for _, c, _ in recent)
    sweep = max((c * p for c, p in other if c >= SWEEP_X * median and c * p >= SWEEP_DOLLARS), default=0.0)
    return share, sweep, len(recent)


def veto(client, sig, now=None):
    """Why to skip this bet, or None. Never raises: if the trades can't be read, the bet
    is skipped (no bet is urgent), and the reason says so."""
    now = time.time() if now is None else now
    for leg in sig.legs:
        try:
            trades = client.trades(leg.ticker, min_ts=now - WINDOW)
        except (KalshiError, OSError, ValueError, TypeError, AttributeError) as e:
            return f"couldn't read recent trades on {leg.ticker}: {e}"
        share, sweep, n = against(trades, leg.side, now)
        if sweep:
            return f"a ${sweep:,.0f} trade just bet against {leg.side.upper()} on {leg.ticker}"
        if n >= MIN_TRADES and share >= AGAINST_SHARE:
            return f"{share:.0%} of the last hour's {n} trades on {leg.ticker} bet against {leg.side.upper()}"
    return None
