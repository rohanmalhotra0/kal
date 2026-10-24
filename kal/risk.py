"""Safety checks that sit between strategies and real money."""
import fcntl
import os
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


class Limits:
    """Tracks spending within one run so no signal can push past a limit."""

    def __init__(self, cfg, budget_left):
        self.cfg = cfg
        self.budget_left = budget_left
        self.per_event = defaultdict(float)

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
        if sig.guaranteed:
            if sig.cost > cfg["max_per_arb"] + 1e-9:
                return f"arbitrage cost ${sig.cost:.2f} over max_per_arb"
        else:
            if sig.max_loss > cfg["max_per_trade"] + 1e-9:
                return f"could lose ${sig.max_loss:.2f}, over max_per_trade"
            if self.per_event[sig.event] + sig.cost > cfg["max_per_event"] + 1e-9:
                return "over max_per_event"
        return None

    def spend(self, sig, amount=None):
        amount = sig.cost if amount is None else amount
        self.budget_left -= amount
        self.per_event[sig.event] += amount


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


def daily_loss(cfg, equity):
    """A reason to stop if today's loss passed the limit, else None. Day = UTC date."""
    today = datetime.now(timezone.utc).date().isoformat()
    start = db.get("day_start")
    if not start or start["day"] != today:
        db.put("day_start", {"day": today, "equity": equity})
        return None
    loss = start["equity"] - equity
    limit = cfg["daily_loss_limit"] * cfg["budget"]
    if cfg["budget"] > 0 and loss > limit:
        return f"lost ${loss:.2f} today, over the ${limit:.2f} daily limit"
    return None
