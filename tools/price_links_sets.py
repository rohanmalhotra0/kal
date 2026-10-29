"""Set engine for idea #19 (probe branch only, never merged).

A market's YES set is an interval on one value. Each market can carry more than one reading
of that interval: the strike fields Kalshi sends, and what its rules text says. When they
differ, a relation must hold for every reading.

Two modes:
- robust: any real value; a "between" end may be open or closed (worst case).
- grid: the value is a multiple of `tick`, and "between" includes both ends (the plain
  reading). A set that pays only in grid mode is safe only if the value is rounded to the
  strike grid.
"""
import itertools
import math
import re

INF = math.inf
EPS = 1e-9


class Iv:
    __slots__ = ("lo", "lo_in", "hi", "hi_in", "fuzzy")

    def __init__(self, lo, lo_in, hi, hi_in, fuzzy=False):
        self.lo, self.lo_in, self.hi, self.hi_in, self.fuzzy = lo, lo_in, hi, hi_in, fuzzy

    def key(self):
        return (self.lo, self.lo_in, self.hi, self.hi_in, self.fuzzy)

    def __repr__(self):
        return (f"{'[' if self.lo_in else '('}{self.lo:g},{self.hi:g}{']' if self.hi_in else ')'}"
                f"{'~' if self.fuzzy else ''}")

    def has(self, v):
        return ((self.lo < v or (self.lo_in and v == self.lo)) and
                (v < self.hi or (self.hi_in and v == self.hi)))


def num(v):
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        v = float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _iv(st, f, c):
    if st == "greater" and f is not None:
        return Iv(f, False, INF, False)
    if st == "greater_or_equal" and f is not None:
        return Iv(f, True, INF, False)
    if st == "less" and c is not None:
        return Iv(-INF, False, c, False)
    if st == "less_or_equal" and c is not None:
        return Iv(-INF, False, c, True)
    if st == "between" and f is not None and c is not None and f < c:
        return Iv(f, True, c, True, fuzzy=True)
    return None


def meta_iv(m):
    """From the strike fields; 'custom' markets (DOGE, SHIB) keep them in custom_strike."""
    st = m.get("strike_type")
    f, c = num(m.get("floor_strike")), num(m.get("cap_strike"))
    cs = m.get("custom_strike") if isinstance(m.get("custom_strike"), dict) else {}
    if st == "custom" and cs.get("strike_type"):
        st, f, c = cs.get("strike_type"), num(cs.get("floor_strike")), num(cs.get("cap_strike"))
    return _iv(st, f, c)


NUMBER = r"\$?\s?(-?[\d,]*\.?\d+)"
RULES = [
    (re.compile(r"\bis between " + NUMBER + r"\s*(?:-|–|and|to)\s*" + NUMBER, re.I), "between"),
    (re.compile(r"\bis (?:at or above|at least|greater than or equal to|or more than) " + NUMBER, re.I), "greater_or_equal"),
    (re.compile(r"\bis (?:at or below|at most|less than or equal to) " + NUMBER, re.I), "less_or_equal"),
    (re.compile(r"\bis (?:above|greater than|more than|over|higher than) " + NUMBER, re.I), "greater"),
    (re.compile(r"\bis (?:below|less than|under|lower than) " + NUMBER, re.I), "less"),
]


def rules_iv(m):
    """From the rules text ("is above X", "is between X-Y"), or None."""
    text = " ".join(str(m.get("rules_primary") or "").split())
    for rx, st in RULES:
        g = rx.search(text)
        if not g:
            continue
        if st == "between":
            return _iv(st, num(g.group(1)), num(g.group(2)))
        k = num(g.group(1))
        return _iv(st, k, None) if st.startswith("greater") else _iv(st, None, k)
    return None


def ivs_of(m):
    """([Iv...], flags). Every reading of the market's YES set; flags say what disagreed."""
    a, b = meta_iv(m), rules_iv(m)
    flags = []
    if a is None:
        flags.append("no strike fields")
    if b is None:
        flags.append("rules unreadable")
    out = [x for x in (a, b) if x is not None]
    if a is not None and b is not None and a.key() != b.key():
        flags.append("strike fields and rules differ")
    uniq = {x.key(): x for x in out}
    return list(uniq.values()), flags


def _r(x, tick):
    if not math.isfinite(x):
        return x
    d = max(0, -int(math.floor(math.log10(tick) + EPS)))
    return round(x, d + 2)


def gridify(x, tick):
    """The closed grid interval of x: strict ends move to the next grid value; between ends stay."""
    lo, hi = x.lo, x.hi
    if math.isfinite(lo):
        if not x.lo_in and not x.fuzzy:
            lo = _r(math.floor(lo / tick + EPS) * tick + tick, tick)
        else:
            lo = _r(math.ceil(lo / tick - EPS) * tick, tick)
    if math.isfinite(hi):
        if not x.hi_in and not x.fuzzy:
            hi = _r(math.ceil(hi / tick - EPS) * tick - tick, tick)
        else:
            hi = _r(math.floor(hi / tick + EPS) * tick, tick)
    return Iv(lo, math.isfinite(lo), hi, math.isfinite(hi))


def _outer(x):
    return Iv(x.lo, True, x.hi, True) if x.fuzzy else x


def _inner(x):
    return Iv(x.lo, False, x.hi, False) if x.fuzzy else x


def _contains(y, x):
    left = y.lo < x.lo or (y.lo == x.lo and (y.lo_in or not x.lo_in))
    right = x.hi < y.hi or (x.hi == y.hi and (y.hi_in or not x.hi_in))
    return left and right


def _subset1(x, y, mode, tick):
    if mode == "grid":
        a, b = _g(x, tick), _g(y, tick)
        tol = tick / 100
        return b.lo <= a.lo + tol and a.hi <= b.hi + tol
    return _contains(_inner(y), _outer(x))


def _disjoint1(x, y, mode, tick):
    if mode == "grid":
        a, b = _g(x, tick), _g(y, tick)
        tol = tick / 100
        return a.hi < b.lo - tol or b.hi < a.lo - tol
    a, b = _outer(x), _outer(y)
    for p, q in ((a, b), (b, a)):
        if p.hi < q.lo or (p.hi == q.lo and not (p.hi_in and q.lo_in)):
            return True
    return False


def _cover1(x, y, mode, tick):
    if mode == "grid":
        a, b = _g(x, tick), _g(y, tick)
        for p, q in ((a, b), (b, a)):
            if p.lo == -INF and q.hi == INF and math.isfinite(p.hi) and math.isfinite(q.lo):
                if q.lo <= _r(p.hi + tick, tick) + tick / 100:
                    return True
        return False
    a, b = _inner(x), _inner(y)
    for p, q in ((a, b), (b, a)):
        if p.lo == -INF and q.hi == INF:
            if q.lo < p.hi or (q.lo == p.hi and (p.hi_in or q.lo_in)):
                return True
    return False


_GRID = {}


def _g(x, tick):
    k = (x.key(), tick)
    g = _GRID.get(k)
    if g is None:
        if len(_GRID) > 200000:
            _GRID.clear()
        g = _GRID[k] = gridify(x, tick)
    return g


def _all(fn, xs, ys, mode, tick):
    return bool(xs) and bool(ys) and all(fn(x, y, mode, tick) for x in xs for y in ys)


def subset(xs, ys, mode, tick):
    """YES x always means YES y, for every reading of both."""
    return _all(_subset1, xs, ys, mode, tick)


def disjoint(xs, ys, mode, tick):
    return _all(_disjoint1, xs, ys, mode, tick)


def cover(xs, ys, mode, tick):
    return _all(_cover1, xs, ys, mode, tick)


def payout_min(legs, tick, mode):
    """Brute force: the least a set pays. legs = [([Iv...], 'yes'|'no')]."""
    ends = sorted({e for ivs, _ in legs for iv in ivs for e in (iv.lo, iv.hi) if math.isfinite(e)})
    if not ends:
        ends = [0.0]
    if mode == "grid":
        vals = set()
        for e in ends:
            g = round(e / tick)
            for k in (g - 2, g - 1, g, g + 1, g + 2):
                vals.add(_r(k * tick, tick))
        vals |= {_r(round(min(ends) / tick) * tick - 10 * tick, tick), _r(round(max(ends) / tick) * tick + 10 * tick, tick)}
        choices = [[gridify(iv, tick) for iv in ivs] for ivs, _ in legs]
    else:
        d = tick / 1000
        vals = {v for e in ends for v in (e - d, e, e + d)}
        vals |= {min(ends) - 1, max(ends) + 1} | {(a + b) / 2 for a, b in zip(ends, ends[1:])}
        choices = []
        for ivs, _ in legs:
            opts = []
            for iv in ivs:
                if iv.fuzzy:
                    opts += [Iv(iv.lo, a, iv.hi, b) for a in (True, False) for b in (True, False)]
                else:
                    opts.append(iv)
            choices.append(opts)
    worst = INF
    for combo in itertools.product(*choices):
        for v in vals:
            pay = sum(1 for iv, (_, side) in zip(combo, legs) if iv.has(v) == (side == "yes"))
            worst = min(worst, pay)
    return worst


# ---------------- pricing across two events ----------------

def _dir(ivs):
    if ivs and all(iv.hi == INF and iv.lo > -INF for iv in ivs):
        return "up"
    if ivs and all(iv.lo == -INF and iv.hi < INF for iv in ivs):
        return "dn"
    return None


class Linker:
    """All cross-series sets between event R (any strike types) and event A (above/below
    markets). Relations are time-independent and found once; sets() then prices the cheapest
    set of each family for one moment's quotes.

    Families (x is an R market, y an A market):
      superset  YES y + NO x   (x inside y)          pays 1
      disjoint  NO y + NO x    (never both)          pays 1
      subset    YES x + NO y   (y inside x)          pays 1
      cover     YES x + YES y  (always one)          pays 1
      sell3     NO x + YES y1 + NO y2 (y1 superset, y2 disjoint, same direction)  pays 2
      buy3      YES x + NO up(lo) + YES up(hi), lo >= floor - tick, hi <= cap      pays 1 on the grid only
    Each family is found in robust mode, then in grid mode for the A markets only grid mode
    allows (those sets are labelled grid)."""

    def __init__(self, r_mkts, a_mkts, tick):
        """r_mkts, a_mkts: [(ticker, [Iv...])]."""
        self.tick = tick
        self.R = [(t, ivs) for t, ivs in r_mkts if ivs]
        self.ivs = dict(r_mkts + a_mkts)
        ups = [(t, ivs) for t, ivs in a_mkts if _dir(ivs) == "up"]
        dns = [(t, ivs) for t, ivs in a_mkts if _dir(ivs) == "dn"]
        self.other = [t for t, ivs in a_mkts if _dir(ivs) is None]
        self.lists = {"up": sorted(ups, key=lambda x: (min(i.lo for i in x[1]), x[0])),
                      "dn": sorted(dns, key=lambda x: (max(i.hi for i in x[1]), x[0]))}
        self.rel = {}
        fns = {"superset": lambda x, y, m: subset(x, y, m, tick), "disjoint": lambda x, y, m: disjoint(x, y, m, tick),
               "subset": lambda x, y, m: subset(y, x, m, tick), "cover": lambda x, y, m: cover(x, y, m, tick)}
        for i, (_, x) in enumerate(self.R):
            xd = _dir(x)
            for side, lst in self.lists.items():
                for fam, fn in fns.items():
                    # an infinite y can't sit inside a finite x; only a one-sided x can cover with y
                    if fam == "subset" and xd != side:
                        continue
                    if fam == "cover" and (xd is None or xd == side):
                        continue
                    rob = [j for j, (_, y) in enumerate(lst) if fn(x, y, "robust")]
                    rs = set(rob)
                    grid = [j for j, (_, y) in enumerate(lst) if j not in rs and fn(x, y, "grid")]
                    if rob:
                        self.rel[("robust", i, fam, side)] = rob
                    if grid:
                        self.rel[("grid", i, fam, side)] = grid
        self.buy3 = {}
        ups_l = self.lists["up"]
        for i, (tx, xs) in enumerate(self.R):
            if len(xs) != 1 or not xs[0].fuzzy:
                continue
            x = xs[0]
            pairs = []
            for t1, y1s in ups_l:
                if len(y1s) != 1 or y1s[0].lo_in or not (x.lo - tick - tick / 100 <= y1s[0].lo < x.hi):
                    continue
                for t2, y2s in ups_l:
                    if len(y2s) != 1 or y2s[0].lo_in or not (y1s[0].lo < y2s[0].lo <= x.hi + tick / 100):
                        continue
                    legs = [(xs, "yes"), (y1s, "no"), (y2s, "yes")]
                    if payout_min(legs, tick, "grid") >= 1:
                        pairs.append((t1, t2))
            if pairs:
                self.buy3[i] = pairs

    def n_relations(self):
        out = {}
        for (mode, _, fam, _), idx in self.rel.items():
            out[(mode, fam)] = out.get((mode, fam), 0) + len(idx)
        out[("grid", "buy3")] = sum(len(v) for v in self.buy3.values())
        return out

    def sets(self, q):
        """Yields (family, mode, payout, legs) with legs [(ticker, side, price)]: the cheapest
        set of each family for each R market at quotes q = {ticker: (yes_bid, yes_ask)}."""
        def yes(t):
            ba = q.get(t)
            return ba[1] if ba and 0 < ba[1] < 1 else None

        def no(t):
            ba = q.get(t)
            return round(1 - ba[0], 4) if ba and 0 < ba[0] < 1 else None

        pre = {}
        for side, lst in self.lists.items():
            for kind, fn in (("yes", yes), ("no", no)):
                arr = [fn(t) for t, _ in lst]
                pm, best = [], None
                for j, p in enumerate(arr):
                    if p is not None and (best is None or p < best[1]):
                        best = (lst[j][0], p)
                    pm.append(best)
                sm, best = [None] * len(arr), None
                for j in range(len(arr) - 1, -1, -1):
                    p = arr[j]
                    if p is not None and (best is None or p < best[1]):
                        best = (lst[j][0], p)
                    sm[j] = best
                pre[(side, kind)] = (pm, sm)

        def best_of(idx, side, kind):
            lst = self.lists[side]
            a, b = idx[0], idx[-1] + 1
            if b - a == len(idx):
                pm, sm = pre[(side, kind)]
                if a == 0:
                    return pm[b - 1]
                if b == len(lst):
                    return sm[a]
            fn = yes if kind == "yes" else no
            pick = None
            for j in idx:
                p = fn(lst[j][0])
                if p is not None and (pick is None or p < pick[1]):
                    pick = (lst[j][0], p)
            return pick

        for i, (tx, _) in enumerate(self.R):
            px_yes, px_no = yes(tx), no(tx)
            for mode in ("robust", "grid"):
                sup, dis = {}, {}
                for side in ("up", "dn"):
                    for fam in ("superset", "disjoint", "subset", "cover"):
                        idx = self.rel.get((mode, i, fam, side))
                        if not idx:
                            continue
                        kind = "yes" if fam in ("superset", "cover") else "no"
                        pick = best_of(idx, side, kind)
                        if pick is None:
                            continue
                        if fam == "superset":
                            sup[side] = pick
                            if px_no is not None:
                                yield fam, mode, 1, [(pick[0], "yes", pick[1]), (tx, "no", px_no)]
                        elif fam == "disjoint":
                            dis[side] = pick
                            if px_no is not None:
                                yield fam, mode, 1, [(pick[0], "no", pick[1]), (tx, "no", px_no)]
                        elif fam == "subset":
                            if px_yes is not None:
                                yield fam, mode, 1, [(tx, "yes", px_yes), (pick[0], "no", pick[1])]
                        elif px_yes is not None:
                            yield fam, mode, 1, [(tx, "yes", px_yes), (pick[0], "yes", pick[1])]
                if mode == "robust" and px_no is not None:
                    for side in ("up", "dn"):
                        if side in sup and side in dis:
                            yield "sell3", mode, 2, [(tx, "no", px_no), (sup[side][0], "yes", sup[side][1]),
                                                     (dis[side][0], "no", dis[side][1])]
            if i in self.buy3 and px_yes is not None:
                best = None
                for t1, t2 in self.buy3[i]:
                    p1, p2 = no(t1), yes(t2)
                    if p1 is None or p2 is None:
                        continue
                    cost = px_yes + p1 + p2
                    if best is None or cost < best[0]:
                        best = (cost, [(tx, "yes", px_yes), (t1, "no", p1), (t2, "yes", p2)])
                if best:
                    yield "buy3", "grid", 1, best[1]


# ---------------- self tests ----------------

def _unit():
    A = lambda k: [Iv(k, False, INF, False)]       # noqa: E731
    B = lambda k: [Iv(-INF, False, k, False)]      # noqa: E731
    R = lambda f, c: [Iv(f, True, c, True, True)]  # noqa: E731
    t = 0.01
    r = R(71700, 71799.99)
    for mode in ("robust", "grid"):
        assert subset(r, A(71699.99), mode, t) and disjoint(r, A(71799.99), mode, t)
        assert not subset(r, A(71700), mode, t)
    assert cover(A(71699.99), B(71700), "robust", t) and cover(A(71699.99), B(71700), "grid", t)
    assert not cover(A(71700), B(71700), "robust", t) and not cover(A(71700), B(71700), "grid", t)
    assert not disjoint(B(71700), A(71699.99), "robust", t) and disjoint(B(71700), A(71699.99), "grid", t)
    assert payout_min([(r, "yes"), (A(71699.99), "no"), (A(71799.99), "yes")], t, "grid") == 1
    assert payout_min([(r, "yes"), (A(71699.99), "no"), (A(71799.99), "yes")], t, "robust") == 0
    assert payout_min([(r, "no"), (A(71699.99), "yes"), (A(71799.99), "no")], t, "robust") == 2
    assert payout_min([(r, "no"), (A(71700), "yes")], t, "robust") == 0
    assert gridify(A(71699.99)[0], t).lo == 71700.0 and gridify(B(71700)[0], t).hi == 71699.99
    t2 = 1e-7
    rd = R(0.08, 0.0809999)
    assert subset(rd, A(0.0799999), "robust", t2) and disjoint(rd, A(0.0809999), "robust", t2)
    assert payout_min([(rd, "yes"), (A(0.0799999), "no"), (A(0.0809999), "yes")], t2, "grid") == 1
    # two readings (KXINXU: strike fields say >= 7550, rules say > 7549.9999)
    two = [Iv(7550, True, INF, False), Iv(7549.9999, False, INF, False)]
    assert subset(R(7550, 7574.9999), two, "robust", 0.0001)
    assert not subset(R(7549.9999, 7574.9999), two, "robust", 0.0001)
    # rules parsing
    m = {"strike_type": "greater_or_equal", "floor_strike": 7550,
         "rules_primary": "If the S&P 500 index value on Oct 29, 2026 at 1pm EDT is above 7549.9999, then"}
    ivs, flags = ivs_of(m)
    assert len(ivs) == 2 and "strike fields and rules differ" in flags, (ivs, flags)
    m = {"strike_type": "between", "floor_strike": 71700, "cap_strike": 71799.99,
         "rules_primary": "BRTI before 1 PM EDT is between 71700-71799.99 at 1 PM EDT on Oct 29, 2026"}
    ivs, flags = ivs_of(m)
    assert len(ivs) == 1 and not flags, (ivs, flags)
    m = {"strike_type": "custom", "custom_strike": {"cap_strike": "0.0100000", "floor_strike": "", "strike_type": "less"},
         "rules_primary": "If there is a 60 second average of X before 1 PM EDT is below 0.0100000 at 1 PM"}
    ivs, flags = ivs_of(m)
    assert len(ivs) == 1 and ivs[0].hi == 0.01 and not flags, (ivs, flags)
    m = {"strike_type": "between", "floor_strike": 28200, "cap_strike": 28299.99,
         "rules_primary": "If the end-of-day Nasdaq 100 index value on October 29, 2026 is between 28200 and 28299.9900, then"}
    ivs, flags = ivs_of(m)
    assert len(ivs) == 1 and not flags, (ivs, flags)
    m = {"strike_type": "greater", "floor_strike": 2.0399,
         "rules_primary": "XRPUSD_RTI before 1 PM EDT is above 2.0399 at 1 PM EDT on Oct 29, 2026"}
    assert len(ivs_of(m)[0]) == 1


def _selftest():
    import random
    A = lambda k: [Iv(k, False, INF, False)]       # noqa: E731
    B = lambda k: [Iv(-INF, False, k, False)]      # noqa: E731
    R = lambda f, c: [Iv(f, True, c, True, True)]  # noqa: E731
    t = 0.01
    rm = [("R-T100", B(100))] + [(f"R-B{k}", R(k, round(k + 99.99, 2))) for k in range(100, 1000, 100)] + \
         [("R-T1000", A(999.99))]
    am = [(f"A-T{k}", A(round(k - 0.01, 2))) for k in range(0, 1200, 50)] + [("A-L500", B(500))]
    L = Linker(rm, am, t)
    ivs = dict(rm + am)
    random.seed(1)
    for _ in range(200):
        q = {tk: (round(random.uniform(0, 0.6), 2), round(random.uniform(0.3, 1), 2)) for tk in ivs}
        got = {}
        for fam, mode, pay, legs in L.sets(q):
            key = (legs[0][0] if fam in ("subset", "cover", "buy3", "sell3") else legs[1][0], fam, mode, pay)
            got[key] = min(got.get(key, 9), sum(p for _, _, p in legs))
            lg = [(ivs[tk], side) for tk, side, _ in legs]
            pm = payout_min(lg, t, "robust" if mode == "robust" else "grid")
            assert pm >= pay, (fam, mode, legs, pm)
            if mode == "grid":
                assert payout_min(lg, t, "robust") < pay, (fam, legs)
        for tx, x in rm:
            for ty, y in am:
                for sx in ("yes", "no"):
                    for sy in ("yes", "no"):
                        if payout_min([(x, sx), (y, sy)], t, "robust") < 1:
                            continue
                        px = q[tx][1] if sx == "yes" else round(1 - q[tx][0], 4)
                        py = q[ty][1] if sy == "yes" else round(1 - q[ty][0], 4)
                        if not (0 < px < 1 and 0 < py < 1):
                            continue
                        fam = {("no", "yes"): "superset", ("no", "no"): "disjoint", ("yes", "no"): "subset",
                               ("yes", "yes"): "cover"}[(sx, sy)]
                        k = (tx, fam, "robust", 1)
                        assert k in got and got[k] <= px + py + 1e-9, (k, got.get(k), px + py)


if __name__ == "__main__":
    _unit()
    _selftest()
    print("ok")
