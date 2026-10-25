"""Backtests on Kalshi's settled markets: would a simple rule have made money after fees?

1. Calibration: buy every contract whose price sat in a range at the decision time
   (e.g. 6 hours before close). A range counts as an edge only if a cautious lower
   bound on its ROI is above zero.
2. Mention words: walk forward through time, price each word from earlier events
   only, and trade when history and the market disagree.

Prices come from hourly candles that ended at or before the decision time, so a test
can never peek at the future. Settled markets don't change, so each market's price
snapshot is cached under $KAL_HOME/cache/ and a re-run only fetches new markets.
Every backtest is recorded in the db "backtests" table.
"""
import json
import logging
import os
import re
import statistics
from collections import defaultdict

from .. import config, db
from ..client import Kalshi, UnknownSettlement, candle_close, fee_per_contract, num, ts, yes_payout
from ..strategies.mentions import MIN_HISTORY, mention_side, word_estimate, word_of

BUCKETS = [0, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 1.0]
MIN_VOLUME = 100            # settled markets almost nobody traded tell us little
MIN_TRADES, MIN_EVENTS = 30, 10
MARGINS = [0.10, 0.15, 0.20, 0.25]
ALPHA = 0.05                # 95% confidence, before the Bonferroni split
LOOKBACK_HOURS = 12         # an older price is too stale to call "the price at decision time"

log = logging.getLogger(__name__)


# ---------- prices at decision time ----------

def decision_time(m, hours_before_close=6, hours_after_open=None, anchor=None):
    """Unix time we'd have decided at, or None if the market wasn't open then.

    `anchor` is the event's scheduled close (the latest close among its markets). Using
    each market's own close_time would leak the outcome: a mention market closes early
    when the word is said, so its "6 hours before close" would sit right before the YES."""
    try:
        open_t, close_t = ts(m["open_time"]), ts(m["close_time"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if hours_after_open is not None:
        t = open_t + hours_after_open * 3600
    else:
        t = (anchor if anchor is not None else close_t) - hours_before_close * 3600
    return int(t) if open_t < t < close_t else None


def event_anchors(markets):
    """{event: scheduled close} = the latest close_time among the event's settled markets.
    Siblings that ran to schedule reveal it even when some markets closed early."""
    out = {}
    for m in markets:
        try:
            t = ts(m["close_time"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        ev = m.get("event_ticker") or m.get("ticker")
        out[ev] = max(out.get(ev, t), t)
    return out


def snapshot(client, series, m, hours_before_close=6, hours_after_open=None, anchor=None):
    """(yes_ask, yes_bid) from the last candle that ENDED by the decision time.
    Later candles are dropped even if the API sends them: that's the no-look-ahead rule."""
    t = decision_time(m, hours_before_close, hours_after_open, anchor)
    if t is None:
        return None
    start = t - LOOKBACK_HOURS * 3600
    candles = [c for c in client.candles(series, m["ticker"], start, t)
               if isinstance(c.get("end_period_ts"), (int, float)) and start <= c["end_period_ts"] <= t]
    for c in sorted(candles, key=lambda c: -c["end_period_ts"]):
        ask, bid = candle_close(c, "yes_ask"), candle_close(c, "yes_bid")
        if 0 < ask <= 1 and bid < ask:
            return ask, bid
    return None


def _when(hours_before_close, hours_after_open):
    if hours_after_open is not None:
        return f"open{hours_after_open:g}"
    return f"close{hours_before_close:g}"


def cache_path(series, hours_before_close=6, hours_after_open=None):
    d = os.path.join(config.home(), "cache")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", series)
    # v2: decision time anchored to the event's scheduled close (older caches leaked outcomes)
    return os.path.join(d, f"{safe}_{_when(hours_before_close, hours_after_open)}_v2.json")


def _read_cache(path):
    try:
        with open(path) as f:
            return json.load(f)["markets"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}  # missing or corrupt: rebuild it


def _write_cache(path, markets):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"version": 1, "markets": markets}, f)
    os.replace(tmp, path)


def load_observations(client, series, hours_before_close=6, hours_after_open=None, max_markets=1500,
                      refresh=False, cached_only=False):
    """One row per settled market: its price at decision time and how it settled.
    Progress is saved even if the API fails partway, so the next run picks up from there."""
    path = cache_path(series, hours_before_close, hours_after_open)
    cache = {} if refresh else _read_cache(path)
    if not cached_only:
        new, errors, last_error = 0, 0, None
        try:
            settled = list(client.settled_markets(series, max_markets))
            anchors = event_anchors(settled)
            sizes = defaultdict(int)
            for m in settled:
                sizes[m.get("event_ticker") or m.get("ticker")] += 1
            for m in settled:
                tk = m.get("ticker")
                try:
                    value = yes_payout(m)  # 1, 0, or in between: cancelled games really pay $0.33
                except UnknownSettlement:
                    value = None
                if not tk or tk in cache or value is None or num(m, "volume") < MIN_VOLUME:
                    continue
                res = m["result"] if m.get("result") in ("yes", "no") else "scalar"
                ev = m.get("event_ticker") or tk
                if sizes[ev] == 1 and m.get("can_close_early") is True:
                    continue  # no sibling shows the scheduled close; its own close may be the outcome
                try:
                    snap = snapshot(client, series, m, hours_before_close, hours_after_open, anchors.get(ev))
                except Exception as e:  # one market's candles failing shouldn't sink the series
                    errors, last_error = errors + 1, e
                    if errors >= 10 and errors > new:
                        raise RuntimeError(f"candles keep failing: {e}") from e
                    continue
                new += 1
                cache[tk] = None if not snap else {
                    "series": series, "event": ev, "ticker": tk,
                    "word": word_of(m), "close": ts(m["close_time"]),
                    "decided": decision_time(m, hours_before_close, hours_after_open, anchors.get(ev)),
                    "yes_ask": snap[0], "yes_bid": snap[1], "result": res, "yes_value": value}
        finally:
            if new:
                _write_cache(path, cache)
        if errors:
            log.warning("%s: %d markets skipped (candles failed): %s", series, errors, last_error)
    return sorted((o for o in cache.values() if o), key=lambda o: (o["close"], o["ticker"]))


# ---------- statistics ----------

def trade(event, p, won, count=1):
    """(event, cost, pnl, fair_var) for buying `count` contracts at `p`, fees included.
    fair_var is the P&L variance if the price were exactly right."""
    cost = count * (p + fee_per_contract(p))
    return event, cost, count * float(won) - cost, count * count * p * (1 - p)


def roi_stats(trades, z):
    """(roi, cautious lower bound) for (event, cost, pnl[, fair_var]) trades.

    Trades in the same event count as one unit, since they tend to win or lose
    together. The spread used is never smaller than a fairly priced market's own
    noise, so a lucky streak (say 30 wins in a row at 93c) can't look like proof."""
    by_event = defaultdict(lambda: [0.0, 0.0, 0.0])
    for t in trades:
        e = by_event[t[0]]
        e[0] += t[1]
        e[1] += t[2]
        # Add standard deviations, not variances: assume trades in one event win or lose
        # together (nested ladder rungs do), so the noise floor isn't understated.
        e[2] += (t[3] if len(t) > 3 else 0.0) ** 0.5
    cost = sum(c for c, _, _ in by_event.values())
    g = len(by_event)
    if g < 2 or cost <= 0:
        return None, None
    roi = sum(p for _, p, _ in by_event.values()) / cost
    var = sum((p - roi * c) ** 2 for c, p, _ in by_event.values()) / cost ** 2 * g / (g - 1)
    floor = sum(sd * sd for _, _, sd in by_event.values()) / cost ** 2
    return roi, roi - z * max(var, floor) ** 0.5


def z_for(tests):
    """One-sided z for 95% confidence across `tests` tries (Bonferroni)."""
    return statistics.NormalDist().inv_cdf(1 - ALPHA / max(1, tests))


def bucket_of(p):
    for i in range(len(BUCKETS) - 1):
        if BUCKETS[i] <= p < BUCKETS[i + 1]:
            return i
    return None


def paid(o, side):
    """What one contract on `side` paid. Usually 1 or 0; a cancelled market can pay a split
    (all three outcomes of a called-off match paid $0.33), which counts as a partial loss."""
    v = o.get("yes_value")
    if v is None:
        v = 1.0 if o["result"] == "yes" else 0.0
    return v if side == "yes" else 1 - v


def buy_trades(o):
    """Both sides of a market as (price paid, payout per contract). The NO ask is 1 - YES bid."""
    yield o["yes_ask"], paid(o, "yes")
    if o["yes_bid"] > 0:
        yield round(1 - o["yes_bid"], 4), paid(o, "no")


def calibration(obs, max_spread=None, by_side=False):
    """For each price range: if you bought every contract there, did you make money?
    max_spread keeps only markets as tight as the live strategy would trade.

    by_side splits YES buys from NO buys. Real Kalshi data shows they differ a lot at the
    same price (YES favorites lost while NO favorites made money), so an edge found by
    pooling the two could send a strategy to buy the losing side."""
    sides = ("yes", "no") if by_side else (None,)
    z = z_for(len(sides) * (len(BUCKETS) - 1))  # every bucket (and side) is a separate try
    trades = defaultdict(list)
    for o in obs:
        if max_spread is not None and not (0 < o["yes_bid"] and o["yes_ask"] - o["yes_bid"] <= max_spread + 1e-9):
            continue
        for side, (p, won) in zip(("yes", "no"), buy_trades(o)):
            if 0.01 <= p <= 0.99:
                trades[(side if by_side else None, bucket_of(p))].append(trade(o["event"], p, won))
    rows = []
    for side, b in sorted(trades, key=lambda k: (k[0] or "", k[1])):
        t = trades[(side, b)]
        roi, lo = roi_stats(t, z)
        row = {"lo": BUCKETS[b], "hi": BUCKETS[b + 1], "trades": len(t),
               "events": len({x[0] for x in t}), "win_rate": sum(1 for x in t if x[2] > 0) / len(t),
               "roi": roi, "roi_lo": lo}
        if by_side:
            row["side"] = side
        rows.append(row)
    return rows


def is_edge(row):
    return (row["roi_lo"] is not None and row["roi_lo"] > 0
            and row["trades"] >= MIN_TRADES and row["events"] >= MIN_EVENTS)


def mention_test(obs, margin):
    """Walk forward through time: price each word from events that had already closed by
    the decision time, trade when history and the market disagree by `margin`, and only
    learn an event's results once it has closed."""
    hist = defaultdict(lambda: [0, 0])    # (series, word) -> [hits, n]
    base = defaultdict(lambda: [0, 0])    # series -> [hits, n]
    trades = []
    by_event = defaultdict(list)
    for o in obs:
        by_event[o["event"]].append(o)

    def decided(e):
        # Without a recorded decision time, decide just before the first close, so an
        # event can never learn from itself.
        return min(o.get("decided") or o["close"] - 1 for o in by_event[e])

    def closed(e):
        return max(o["close"] for o in by_event[e])

    pending = sorted(by_event, key=lambda e: (closed(e), e))  # not yet learned, by close
    for event in sorted(by_event, key=lambda e: (decided(e), e)):
        now = decided(event)
        while pending and closed(pending[0]) <= now:
            for o in by_event[pending.pop(0)]:
                if o["result"] not in ("yes", "no"):
                    continue  # a cancelled market says nothing about the word
                hit = o["result"] == "yes"
                hist[(o["series"], o["word"])][0] += hit
                hist[(o["series"], o["word"])][1] += 1
                base[o["series"]][0] += hit
                base[o["series"]][1] += 1
        for o in by_event[event]:
            hits, n = hist[(o["series"], o["word"])]
            bh, bn = base[o["series"]]
            if n < MIN_HISTORY:
                continue
            pick = mention_side(word_estimate(hits, n, bh / bn if bn else 0.5), o["yes_ask"], o["yes_bid"], margin)
            if pick:
                side, p = pick
                trades.append(trade(event, p, paid(o, side)))
    for e in pending:  # everything has closed by now: final history for live use
        for o in by_event[e]:
            if o["result"] not in ("yes", "no"):
                continue
            hit = o["result"] == "yes"
            hist[(o["series"], o["word"])][0] += hit
            hist[(o["series"], o["word"])][1] += 1
            base[o["series"]][0] += hit
            base[o["series"]][1] += 1
    return trades, hist, base


def mention_backtest(obs):
    """Try every margin; report the best one that had enough trades to judge.
    roi_lo is None when no margin had enough trades: that's "not proven", not "zero"."""
    z = z_for(len(MARGINS))
    results, hist, base = [], {}, {}
    for margin in MARGINS:
        trades, hist, base = mention_test(obs, margin)
        roi, lo = roi_stats(trades, z)
        events = len({t[0] for t in trades})
        results.append({"margin": margin, "roi": roi, "roi_lo": lo, "trades": len(trades), "events": events,
                        "enough": len(trades) >= MIN_TRADES and events >= MIN_EVENTS})
    pool = [r for r in results if r["enough"]] or results
    best = dict(max(pool, key=lambda r: r["roi_lo"] if r["roi_lo"] is not None else -9))
    if not best["enough"]:
        best["roi_lo"] = None
    return {"best": best, "margins": results,
            "words": {f"{s}|{w}": v for (s, w), v in hist.items()}, "base": dict(base)}


# ---------- records and manual runs ----------

def record(kind, params, results):
    """Every backtest goes in the db so the dashboard can show what was tried."""
    return db.insert("backtests", kind=kind, params=params, results=results)


def find_series(client, text):
    q = text.lower()
    return [s for s in client.series() if q in (s.get("title") or "").lower() or q in s.get("ticker", "").lower()]


def run(series, hours_before_close=6, hours_after_open=None, client=None, refresh=False, max_markets=3000):
    """A manual backtest of one or more series together (for `python -m kal backtest`).
    Only reports and records; daily discovery is what updates edges.json."""
    if client is None:
        client = Kalshi.from_env(config.home(), config.load()["env"])
    obs = []
    for s in series:
        obs += load_observations(client, s, hours_before_close, hours_after_open, max_markets, refresh)
    rows = calibration(obs)
    mentions = mention_backtest(obs)
    results = {"observations": len(obs), "events": len({o["event"] for o in obs}), "calibration": rows,
               "edges": [r for r in rows if is_edge(r)],
               "mentions": {"best": mentions["best"], "margins": mentions["margins"]}}
    record("manual", {"series": list(series), "hours_before_close": hours_before_close,
                      "hours_after_open": hours_after_open, "max_markets": max_markets}, results)
    return results


def _pct(x):
    return "  n/a" if x is None else f"{x:+.1%}"


def report(results):
    """Short plain text for the terminal."""
    lines = [f"{results['observations']} markets, {results['events']} events.", "",
             "Buy every price in a range:",
             "  range       trades  win    ROI    worst case"]
    for r in results["calibration"]:
        lines.append(f"  {r['lo']:.2f}-{r['hi']:.2f}  {r['trades']:6}  {r['win_rate']:4.0%}  "
                     f"{_pct(r['roi']):>6}  {_pct(r['roi_lo']):>6}{'  <- edge' if is_edge(r) else ''}")
    b = results["mentions"]["best"]
    lines += ["", f"Mention words: best gap {b['margin']:.2f}, {b['trades']} trades, "
                  f"ROI {_pct(b['roi'])}, worst case {_pct(b['roi_lo'])}.",
              "", "Worst case = a cautious 95% bound. Below 0 means it could be luck."]
    return "\n".join(lines)
