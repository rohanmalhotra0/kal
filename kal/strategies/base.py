"""What every strategy implements, and what it hands the engine."""
from dataclasses import dataclass, field

from ..client import fee


@dataclass
class Leg:
    ticker: str
    side: str          # "yes" or "no"
    price: float       # limit price in dollars (what we pay per contract)
    count: int

    @property
    def cost(self):
        return self.count * self.price + fee(self.count, self.price)


@dataclass
class Signal:
    """A trade a strategy wants. The engine checks risk limits, then buys every leg."""
    strategy: str
    event: str
    title: str
    legs: list
    payout_min: float = 0.0    # dollars the legs pay in the worst case (locked-in for arbitrage)
    exp_profit: float = 0.0    # expected profit in dollars, after fees
    note: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def cost(self):
        return sum(leg.cost for leg in self.legs)

    @property
    def guaranteed(self):
        return self.payout_min > self.cost

    @property
    def max_loss(self):
        return max(0.0, self.cost - self.payout_min)


@dataclass
class Context:
    """Everything a strategy may look at while scanning."""
    client: object             # kal.client.Kalshi (or a fake in tests)
    cfg: dict
    mode: str                  # "paper" or "live" for this scan
    budget_left: float         # dollars this strategy may still spend this run
    bankroll: float            # dollars for bet sizing (budget, or paper bankroll)
    held: set                  # tickers already held (live) or open (paper)
    research: dict             # outputs of kal.research, keyed by file name
    params: dict               # this strategy's settings from config


class Strategy:
    name = ""
    title = ""
    kind = "arbitrage"         # "arbitrage" | "statistical" | "speculative"
    description = ""

    def scan(self, ctx):
        """Return a list of Signals, best first."""
        raise NotImplementedError

    def recheck(self, ctx, signal):
        """Re-price a signal right before live execution. Return it (maybe resized) or None."""
        return signal

    def proven(self, ctx):
        """(ok, why). Live trading is refused unless ok. Arbitrage is proven by math."""
        return (True, "locked-in by math") if self.kind == "arbitrage" else (False, "no evidence yet")
