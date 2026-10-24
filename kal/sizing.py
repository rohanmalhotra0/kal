"""Bet sizing.

Kelly: for a contract costing c (price + fee) that pays $1 with probability p,
the growth-optimal stake is f* = (p - c) / (1 - c) of the bankroll. Full Kelly
is far too aggressive when p is an estimate, so we bet a fraction of it and
feed in a conservative p (a lower confidence bound, not the point estimate).
"""
from .client import fee_per_contract


def kelly(p, cost):
    """Growth-optimal fraction of bankroll for one binary contract."""
    if not 0 < cost < 1 or p <= cost:
        return 0.0
    return (p - cost) / (1 - cost)


def contracts(p, price, bankroll, kelly_fraction, max_loss):
    """How many contracts to buy at `price` when the win chance is (conservatively) p."""
    cost = price + fee_per_contract(price)
    stake = min(kelly(p, cost) * kelly_fraction * bankroll, max_loss)
    return max(0, int(stake // cost))
