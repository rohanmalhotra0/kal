"""Bet sizing.

Kelly: for a contract costing c (price + fee) that pays $1 with probability p,
the growth-optimal stake is f* = (p - c) / (1 - c) of the bankroll. Full Kelly
is far too aggressive when p is an estimate, so the bot bets less, in layers:

  shrunk_p             a cautious p: the low end of what the win record allows, so
                       a short record bets less than a long one with the same rate.
  uncertainty_shrink   or, given p's standard error, Baker & McHale's shrink factor.
                       Both answer the same doubt, so use one, not both.
  kelly_fraction       config: bet a fixed fraction (0.25) of Kelly on top.
  scale_to_cap /       bets open at the same time share one bankroll, so cap
  exposure_room        what they risk together.
  drawdown_multiplier  bet smaller after losses, until equity is back at its peak.

Arbitrage is not Kelly-sized: its profit is locked in, so only the money caps in
kal.risk apply. Every function here is pure and returns 0 (bet nothing) on bad
input rather than guessing. docs/RESEARCH.md has the sources.
"""
import math

from .client import fee_per_contract


def _finite(*xs):
    return all(isinstance(x, (int, float)) and math.isfinite(x) for x in xs)


def _unit(x):
    """Clamp to [0, 1]; anything odd counts as 0, so a bad multiplier can't grow a bet."""
    return min(1.0, max(0.0, x)) if _finite(x) else 0.0


def unit_cost(price):
    """What one contract really costs: price plus the (unrounded) taker fee."""
    return price + fee_per_contract(price)


def kelly(p, cost):
    """Growth-optimal fraction of bankroll for one binary contract. 0 if there's no edge
    or the inputs make no sense (p outside [0, 1], cost outside (0, 1), NaN)."""
    if not _finite(p, cost) or not 0 < cost < 1 or not 0 <= p <= 1 or p <= cost:
        return 0.0
    return (p - cost) / (1 - cost)


# ---------- cautious win chance from a record ----------

def _beta_cf(a, b, x, max_iter=10_000, eps=1e-15):
    """Continued fraction for the incomplete beta (modified Lentz, Numerical Recipes 6.4).
    It needs about sqrt(max(a, b)) steps, so even 100,000 trials stay quick."""
    tiny = 1e-300
    c, d = 1.0, 1 - (a + b) * x / (a + 1)
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        for aa in (m * (b - m) * x / ((a - 1 + m2) * (a + m2)),
                   -(a + m) * (a + b + m) * x / ((a + m2) * (a + 1 + m2))):
            d = 1 + aa * d
            d = 1 / (d if abs(d) > tiny else tiny)
            c = 1 + aa / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1) < eps:
            break
    return h


def betainc(a, b, x):
    """Regularized incomplete beta I_x(a, b): the chance a Beta(a, b) draw is <= x."""
    if not (_finite(a, b) and a > 0 and b > 0):
        raise ValueError("a and b must be > 0")
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                     + a * math.log(x) + b * math.log1p(-x))
    # The fraction converges fast below the mean; above it, use I_x(a,b) = 1 - I_{1-x}(b,a).
    if x < (a + 1) / (a + b + 2):
        return front * _beta_cf(a, b, x) / a
    return 1 - front * _beta_cf(b, a, 1 - x) / b


def beta_quantile(q, a, b):
    """The x with I_x(a, b) = q, by bisection. Returns the low end of the last bracket,
    so as a lower bound it can only be a hair too cautious, never too bold."""
    if not 0 <= q <= 1:
        raise ValueError("q must be in [0, 1]")
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if betainc(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12:
            break
    return lo


def _posterior(wins, trials, prior_mean, prior_weight):
    """Beta(a, b) after `wins` of `trials`, from a prior worth `prior_weight` trials
    centred on `prior_mean`. None if the record makes no sense."""
    if not 0 < prior_mean < 1 or not (_finite(prior_weight) and prior_weight > 0):
        raise ValueError("prior_mean must be in (0, 1) and prior_weight > 0")
    if not _finite(wins, trials) or not 0 <= wins <= trials:
        return None
    return wins + prior_mean * prior_weight, trials - wins + (1 - prior_mean) * prior_weight


def shrunk_p(wins, trials, confidence=0.95, prior_mean=0.5, prior_weight=2.0):
    """A cautious win chance from a record of `wins` out of `trials`.

    Exact Beta posterior (not a normal approximation): a flat Beta(1, 1) prior by
    default, plus the record, then the lower (1 - confidence) quantile. So 10 wins out
    of 10 gives 0.76 and 100 out of 100 gives 0.97: same rate, but the short record bets
    far less. It moves up to the raw rate as trials grow. Never above wins/trials, so a
    thin record can't look better than it is. 0 for no trials or a record that makes no
    sense. Use prior_mean (e.g. a series' base rate) to pull a thin record toward it."""
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in (0, 1)")
    post = _posterior(wins, trials, prior_mean, prior_weight)
    if post is None or trials <= 0:
        return 0.0
    return min(beta_quantile(1 - confidence, *post), wins / trials)


def win_rate_se(wins, trials, prior_mean=0.5, prior_weight=2.0):
    """Standard error of a win rate: the spread of the same Beta posterior shrunk_p uses.
    With no record it's the prior's own spread, which is large. None for a bad record."""
    post = _posterior(wins, trials, prior_mean, prior_weight)
    if post is None:
        return None
    a, b = post
    n = a + b
    return math.sqrt(a * b / (n * n * (n + 1)))


def uncertainty_shrink(p, cost, se):
    """Baker & McHale (2013): when p is an estimate, bet k times its Kelly size.

    Expanding log growth to second order around the true Kelly fraction f* gives
    k = f*^2 / (f*^2 + var(f)), where f is Kelly worked out from the estimate. Kelly is
    linear in p, so in terms of the edge (p - cost): k = edge^2 / (edge^2 + se^2). The
    true edge is unknown, and the estimated edge squared overstates it by se^2 on
    average, so we put edge^2 - se^2 in its place: k = 1 - (se / edge)^2. An edge less
    than one standard error above the cost bets nothing; two standard errors bets 75%."""
    if not _finite(p, cost, se) or se < 0 or not 0 < cost < 1:
        return 0.0
    edge = p - cost
    if edge <= 0:
        return 0.0
    return max(0.0, 1 - (se / edge) ** 2)


# ---------- many bets at once ----------

def scale_to_cap(fractions, cap):
    """Shrink the stakes (fractions of bankroll) of bets open at the same time so they add
    up to at most `cap`. All shrink by one factor: Whitrow (2007) found the growth-optimal
    stakes for many simultaneous bets are close to the single-bet Kelly stakes scaled down
    in proportion. Bets in one event move together; kal.risk's max_per_event caps those.
    Negative or bad entries count as 0."""
    clean = [f if _finite(f) and f > 0 else 0.0 for f in fractions]
    if not (_finite(cap) and cap > 0):
        return [0.0] * len(clean)
    total = sum(clean)
    scale = min(1.0, cap / total) if total > 0 else 1.0
    return [f * scale for f in clean]


def exposure_room(bankroll, open_exposure, max_exposure):
    """Dollars a new bet may still put at risk, when `open_exposure` dollars already are
    and all open bets together may risk `max_exposure` of the bankroll. The one-at-a-time
    form of scale_to_cap, for strategies that size bets as they scan."""
    if not _finite(bankroll, open_exposure, max_exposure):
        return 0.0
    return max(0.0, max_exposure * bankroll - max(0.0, open_exposure))


# ---------- after losses ----------

def drawdown_multiplier(equity, peak, start=0.0, full=0.20, floor=0.25):
    """A size multiplier in [floor, 1] that falls as equity drops below its peak.

    1 at or above the peak (or within `start` of it), then straight down to `floor` at a
    `full` drawdown, and `floor` beyond. Defaults: 25% size at a 20% drop. This is a
    simple form of Grossman & Zhou (1993): risk in proportion to the cushion left above
    the loss you won't accept. It never reaches 0: kal.risk and the ledger stop trading.
    Bad equity or peak numbers give `floor`, the cautious end."""
    if not (0 <= start < full <= 1 and 0 < floor <= 1):
        raise ValueError("need 0 <= start < full <= 1 and 0 < floor <= 1")
    if not _finite(equity, peak) or peak <= 0:
        return floor
    dd = 1 - equity / peak
    if dd <= start:
        return 1.0
    if dd >= full:
        return floor
    return 1 - (1 - floor) * (dd - start) / (full - start)


def chance_of_drawdown(kelly_multiple, level):
    """Chance equity ever falls to `level` times what it is now, betting `kelly_multiple`
    of full Kelly on a real edge (Thorp 2006, continuous approximation): level^(2/c - 1).
    Full Kelly halves your money at some point half the time; quarter Kelly, under 1%.
    At 2x Kelly or more growth is gone and a fall is certain. If p is overestimated the
    real multiple is bigger than you think, so treat this as a best case."""
    if not _finite(kelly_multiple, level) or kelly_multiple <= 0:
        raise ValueError("kelly_multiple must be > 0")
    if level >= 1 or kelly_multiple >= 2:
        return 1.0
    if level <= 0:
        return 0.0
    return level ** (2 / kelly_multiple - 1)


# ---------- the call strategies make ----------

def contracts(p, price, bankroll, kelly_fraction, max_loss, *, se=None, throttle=1.0,
              open_exposure=0.0, max_exposure=None):
    """How many contracts to buy at `price` when the win chance is (conservatively) p.

    The extras are all off by default, so an old call sizes exactly as before:
      se             standard error of p: scale by uncertainty_shrink. Leave it out if
                     p already came from shrunk_p, or the doubt is counted twice.
      throttle       a multiplier in [0, 1], e.g. drawdown_multiplier(equity, peak).
                     Clamped, so it can only make a bet smaller.
      max_exposure   all open bets together may risk at most this fraction of bankroll;
      open_exposure  dollars already at risk in open bets.
    Fees round up to the cent per order, so the true cost can be a cent or so more than
    this assumes: callers should still check Leg.cost against their cap."""
    no_cap = max_loss == math.inf
    if not _finite(price, bankroll, kelly_fraction) or not (no_cap or _finite(max_loss)):
        return 0
    cost = unit_cost(price)
    if not 0 < cost < 1:
        return 0
    f = kelly(p, cost) * kelly_fraction
    if se is not None:
        f *= uncertainty_shrink(p, cost, se)
    stake = min(f * bankroll * _unit(throttle), max_loss)
    if max_exposure is not None:
        stake = min(stake, exposure_room(bankroll, open_exposure, max_exposure))
    if stake <= 0:
        return 0
    return int(stake // cost)
