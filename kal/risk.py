"""Safety checks that sit between strategies and real money."""
import fcntl
import math
import os
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config, db
from .client import price


class Locked(RuntimeError):
    pass


@contextmanager
def run_lock():
    """Only one trading run at a time (cron overlap, dashboard click, scheduler)."""
    f = open(os.path.join(config.home(), ".run.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise Locked("another run is in progress")
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


# Real arbitrage on Kalshi pays cents on the dollar. A "locked-in" return above this is
# almost surely a misread market (real data once showed $10.70 "locked in" to pay $500),
# so refuse it rather than trust it.
MAX_ARB_ROI = 0.25


def yearly_return(ratio, days):
    """Compounded yearly return of turning $1 into $ratio over `days`. Capped so a short,
    fat trade can't overflow a float (1.09 ** 8760 does) and crash the run."""
    if ratio <= 0 or days <= 0:
        return -1.0
    return math.exp(min(math.log(ratio) * 365 / days, 50.0)) - 1


class Limits:
    """Tracks spending within one run so no signal can push past a limit."""

    def __init__(self, cfg, budget_left, open_by_event=None, directional_open=0.0, capital=None):
        self.cfg = cfg
        self.budget_left = budget_left
        # Start from what's already open, so caps hold across runs, not just within one.
        self.per_event = defaultdict(float, open_by_event or {})
        self.directional_open = directional_open
        self.capital = capital

    def refuse(self, sig):
        """A reason to refuse this signal, or None if it's within every limit."""
        cfg = self.cfg
        if not sig.legs:
            return "no legs"
        for leg in sig.legs:
            if leg.side not in ("yes", "no") or leg.count < 1 or not 0 < leg.price < 1:
                return f"bad leg {leg}"
        if sig.cost > self.budget_left + 1e-9:
            return f"over budget (${sig.cost:.2f} > ${self.budget_left:.2f} left)"
        if len(sig.legs) > 1 and sig.partial_loss > cfg["max_per_trade"] + 1e-9:
            return f"a half-filled basket could lose ${sig.partial_loss:.2f}, over max_per_trade"
        if sig.guaranteed:
            if sig.cost > cfg["max_per_arb"] + 1e-9:
                return f"arbitrage cost ${sig.cost:.2f} over max_per_arb"
            if sig.payout_min / sig.cost - 1 > MAX_ARB_ROI:
                return (f"too good to be true: ${sig.cost:.2f} 'locked in' to pay ${sig.payout_min:.2f}. "
                        f"Probably a misread market")
            if not sig.closes_at:
                return "locked-in trade with no known settle date"
            days = max((sig.closes_at - time.time()) / 86400, 1 / 24)
            if days > cfg["max_arb_days"]:
                return f"locks money up for {days:.0f} days (max {cfg['max_arb_days']})"
            apr = yearly_return(sig.payout_min / sig.cost, days)
            if apr < cfg["min_arb_apr"]:
                return f"pays {apr:.0%} a year, under the {cfg['min_arb_apr']:.0%} minimum"
        else:
            if sig.max_loss > cfg["max_per_trade"] + 1e-9:
                return f"could lose ${sig.max_loss:.2f}, over max_per_trade"
            if self.per_event[sig.event] + sig.cost > cfg["max_per_event"] + 1e-9:
                return "over max_per_event"
            if self.capital is not None and \
                    self.directional_open + sig.cost > cfg["max_exposure"] * self.capital + 1e-9:
                return "over max_exposure (too much already riding on bets)"
        return None

    def spend(self, sig, amount=None):
        amount = sig.cost if amount is None else amount
        self.budget_left -= amount
        self.per_event[sig.event] += amount
        if not sig.guaranteed:
            self.directional_open += amount


def mark_to_market(client, positions):
    """Value of open positions at what they'd sell for now (best bid)."""
    if not positions:
        return 0.0
    markets = client.markets_by_ticker(p["ticker"] for p in positions)
    value = 0.0
    for p in positions:
        m = markets.get(p["ticker"], {})
        bid = price(m, "yes_bid") if p["position"] > 0 else price(m, "no_bid")
        value += abs(p["position"]) * bid
    return value


def daily_loss(cfg, equity, at_risk=None):
    """A reason to stop if today's loss passed the limit, else None. Day = UTC date.
    `at_risk` (what the bot could really lose: capital, capped by what the account holds)
    caps the base, so a budget typed bigger than the account can't loosen the stop."""
    today = datetime.now(timezone.utc).date().isoformat()
    start = db.get("day_start")
    if not start or start["day"] != today:
        db.put("day_start", {"day": today, "equity": equity})
        return None
    loss = start["equity"] - equity
    base = max(cfg["budget"], start["equity"])
    if at_risk is not None:
        base = min(base, at_risk)
    limit = cfg["daily_loss_limit"] * base
    if cfg["budget"] > 0 and loss > limit:
        return f"lost ${loss:.2f} today, over the ${limit:.2f} daily limit"
    return None
