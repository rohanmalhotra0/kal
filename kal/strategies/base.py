"""What every strategy implements, and what it hands the engine."""
import re
from dataclasses import dataclass, field

from ..client import fee

# Rules that let Kalshi settle at the last traded price or a "fair" price of its choosing
# (seen on real markets: death clauses on "leader leaves office" events). Then a market
# can pay anything from $0 to $1, and a "locked-in" basket can lose.
FAIR_PRICE_WORDS = ("last traded price", "fair price", "fair value")


def plain_payouts(markets):
    """True if every market pays exactly $0 or $1, or a split that keeps the at-most-one-YES
    total (a cancelled match paid $0.33 on each of its three outcomes). Locked-in trades need
    this. Fails closed: a payout floor or cap (any settlement_bounds_type but "default", the
    only value on 10,148 real markets) or fair-price wording in the rules means no."""
    for m in markets:
        if m.get("market_type", "binary") != "binary":
            return False
        if m.get("settlement_bounds_type", "default") != "default":
            return False
        rules = f"{m.get('rules_primary') or ''} {m.get('rules_secondary') or ''}".lower()
        if any(w in rules for w in FAIR_PRICE_WORDS):
            return False
    return True


# The fair-price clause nearly every game and weather market has (2,257 of 5,658 one-winner
# events on 2026-10-25): "if the game is cancelled or not started within 48 hours, the
# market will resolve to a fair price", or for weather "if no data is available".
CANCELLATION = re.compile(r"cancel|postpone|reschedul|not (?:be )?started|walkover|forfeit|no data|abandon|suspend",
                          re.I)


def fair_price_only_if_cancelled(markets):
    """True if every market is binary with no payout floor or cap, and every sentence of its
    rules that names a fair or last traded price is about a cancelled, postponed or unstarted
    game, or missing data. Such a market pays $0 or $1 unless that happens; if it does,
    Kalshi picks the price, so a basket of them is not locked in. Fails closed: any other
    fair-price sentence (a death clause, the exchange's own discretion) means no."""
    for m in markets:
        if m.get("market_type", "binary") != "binary":
            return False
        if m.get("settlement_bounds_type", "default") != "default":
            return False
        rules = f"{m.get('rules_primary') or ''} {m.get('rules_secondary') or ''}"
        for sentence in re.split(r"(?<=[.!?])\s+", rules):
            if any(w in sentence.lower() for w in FAIR_PRICE_WORDS) and not CANCELLATION.search(sentence):
                return False
    return True


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
    # Worst-case payout if execution stops after the first k legs (k = 1 .. len(legs) - 1).
    # None means "assume $0", the safe default. A NO basket sets it, since a partial NO
    # basket still pays at least c * (k - 1).
    prefix_payouts: list = None
    # Unix time the signal's markets close (the latest, for a basket). Locked-in trades
    # must say, so money isn't tied up for years for a cent of profit.
    closes_at: float = None

    @property
    def cost(self):
        return sum(leg.cost for leg in self.legs)

    @property
    def guaranteed(self):
        return self.payout_min > self.cost

    @property
    def max_loss(self):
        return max(0.0, self.cost - self.payout_min)

    @property
    def partial_loss(self):
        """Worst loss if a multi-leg signal stops part way (a leg doesn't fill)."""
        worst, spent = 0.0, 0.0
        for k, leg in enumerate(self.legs[:-1], start=1):
            spent += leg.cost
            payout = self.prefix_payouts[k - 1] if self.prefix_payouts else 0.0
            worst = max(worst, spent - payout)
        return worst


@dataclass
class Context:
    """Everything a strategy may look at while scanning."""
    client: object             # kal.client.Kalshi (or a fake in tests)
    cfg: dict
    mode: str                  # "paper" or "live" for this scan
    budget_left: float         # dollars this strategy may still spend this run
    bankroll: float            # dollars for bet sizing (capital, cut after drawdowns)
    held: set                  # tickers already held (live) or open (paper)
    research: dict             # outputs of kal.research, keyed by file name
    params: dict               # this strategy's settings from config
    open_exposure: float = 0.0  # dollars already in open directional bets (for sizing caps)


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
