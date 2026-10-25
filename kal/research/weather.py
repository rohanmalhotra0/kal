"""Weather research: does a public forecast beat Kalshi's prices on daily high temperatures?

Kalshi lists six markets a day for each city's high temperature: two tails and four 2°F
brackets, settled on the National Weather Service's daily climate report for one station.
The National Blend of Models (NBM) forecasts that high, with a spread, every hour, and
the Iowa Environmental Mesonet (IEM) keeps every run. This prices each market from the
forecast and tests whether buying where the forecast disagrees with the price made money
after fees. Research only: it places nothing.

No peeking:
- the price is the backtest's usual one: the last hourly candle that ended by the
  decision time, H hours before the market's scheduled end;
- every market counts, even quiet ones: the backtest's usual volume filter uses volume
  known only after a market settles, and dead brackets (mostly losers) are the quiet ones;
- the forecast is the latest NBM run that started 2+ hours before the decision time
  (a run takes about an hour to publish);
- a city's forecast bias and spread are fitted only on days that settled before the
  decision day.
"""
import csv
import io
import json
import logging
import math
import os
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from ..client import UnknownSettlement, num, yes_payout
from ..strategies.favorites import cost_each
from . import backtest

# Kalshi series -> the station its rules settle on (CLINYC is Central Park, CLIMDW Midway).
STATIONS = {"KXHIGHNY": "KNYC", "KXHIGHCHI": "KMDW", "KXHIGHMIA": "KMIA", "KXHIGHLAX": "KLAX",
            "KXHIGHDEN": "KDEN", "KXHIGHAUS": "KAUS", "KXHIGHPHIL": "KPHL"}
IEM_MOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/mos.py"
LATENCY = 2 * 3600        # use a run only once it's surely published
LOOK_BACK = 6 * 3600      # if that run isn't archived, the latest one up to 6 hours older
CHUNK_DAYS = 7            # one archive request covers a week of runs
TAILS = [(w, k) for w in (0.0, 0.05, 0.1, 0.2, 0.3) for k in (2.0, 3.0, 4.0)]   # (share, width) of a wider error
FIT_DAYS, MIN_FIT = 120, 20   # bias and spread from up to 120 earlier days; skip a day with under 20

log = logging.getLogger(__name__)


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bracket(m):
    """The whole-degree highs that pay YES, (lo, hi), None for an open end. None if unknown.
    ">64°" pays on 65 and up; "<57°" on 56 and down; "63-64°" on 63 or 64."""
    t, f, c = m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike")
    try:
        if t == "greater" and f is not None:
            return int(round(float(f))) + 1, None
        if t == "less" and c is not None:
            return None, int(round(float(c))) - 1
        if t == "between" and f is not None and c is not None:
            return int(round(float(f))), int(round(float(c)))
    except (TypeError, ValueError):
        pass
    return None


def _normal(rng, mu, s):
    lo, hi = rng
    up = 1.0 if hi is None else phi((hi + 0.5 - mu) / s)
    down = 0.0 if lo is None else phi((lo - 0.5 - mu) / s)
    return max(0.0, up - down)


def prob(rng, mu, s, w=0.0, k=1.0):
    """Chance the reported (whole-degree) high lands in rng. The error is Normal(0, s),
    except on a share w of days it's k times wider: fronts can put the calendar-day high
    outside the forecast's daytime window, and those days make the tails fat."""
    return (1 - w) * _normal(rng, mu, s) + (w * _normal(rng, mu, k * s) if w else 0.0)


def day_of(anchor):
    """The day a market is about: it ends at local midnight standard time, the next day in UTC."""
    return datetime.fromtimestamp(anchor - 12 * 3600, timezone.utc).date()


def run_for(decided):
    """Start time of the latest NBM run surely published by `decided`: on the hour."""
    t = int(decided) - LATENCY
    return t - t % 3600


def _utc(text):
    try:
        return int(datetime.strptime((text or "")[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def parse_max(text, day, latest):
    """(forecast high, spread or None, run start) for `day`, from the latest run in an IEM
    NBS CSV that started by `latest` (unix time), or None. The day's high is the txn valid
    at 00 UTC after it (the 12 daytime hours ending then); xnd is its spread."""
    want = (datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    best = None
    for row in csv.DictReader(io.StringIO(text)):
        run = _utc(row.get("runtime"))
        if not (row.get("ftime") or "").startswith(want) or run is None or run > latest:
            continue  # another time, or a run that started too late to use
        try:
            mu = float(row.get("txn") or "")
        except ValueError:
            continue
        try:
            sd = float(row.get("xnd") or "")
        except ValueError:
            sd = None
        if -60 < mu < 140 and (best is None or run > best[2]):
            best = (mu, sd if sd and 0 < sd < 30 else None, run)
    return best


def forecasts(fetch, station, wants, cache_file=None):
    """{(runtime, day): (high, spread, run) or None} for each wanted pair: the latest
    archived run that started by `runtime` (up to LOOK_BACK older), via fetch(url, params)
    -> CSV text. One request covers CHUNK_DAYS of wanted runs. Cached in cache_file (runs
    never change). An empty reply isn't cached: it may be a hiccup, so it's asked again."""
    cache = {}
    if cache_file and os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
    def stamp(t):
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")

    todo = [(r, d) for r, d in sorted(set(wants)) if f"{r}|{d}" not in cache]
    new = 0
    try:
        while todo:
            chunk = [x for x in todo if x[0] < todo[0][0] + CHUNK_DAYS * 86400]
            todo = todo[len(chunk):]
            try:
                text = fetch(IEM_MOS, {"station": station, "model": "NBS", "sts": stamp(chunk[0][0] - LOOK_BACK),
                                       "ets": stamp(chunk[-1][0]), "format": "csv"})
            except Exception as e:  # one failed request: skip those days, try again next time
                log.warning("NBM %s %s: %s", station, stamp(chunk[0][0]), e)
                continue
            if len((text or "").strip().splitlines()) < 2:
                continue  # no rows at all: not cached
            for runtime, day in chunk:
                got = parse_max(text, day, runtime)   # only runs that started by this one's time
                cache[f"{runtime}|{day}"] = list(got) if got else None
                new += 1
        out = {}
        for runtime, day in set(wants):
            hit = cache.get(f"{runtime}|{day}")
            if f"{runtime}|{day}" in cache:
                out[(runtime, day)] = tuple(hit) if hit else None
    finally:
        if cache_file and new:
            with open(cache_file + ".tmp", "w") as f:
                json.dump(cache, f)
            os.replace(cache_file + ".tmp", cache_file)
    return out


def winners(markets):
    """{event: (lo, hi)} for each settled event with exactly one winning, readable market."""
    won = defaultdict(list)
    for m in markets:
        if m.get("result") == "yes":
            won[m.get("event_ticker")].append(bracket(m))
    return {e: b[0] for e, b in won.items() if len(b) == 1 and b[0]}


def fit(days):
    """(bias, spread, w, k) that best explain past days, by likelihood. Bias and spread
    first (a coarse grid, bias -8..8°F and spread 1..10°F, then a finer one), then how fat
    the tails are (TAILS; w = 0 is a plain Normal), then the spread again for those tails.
    days: [(mu, (lo, hi))]."""
    def ll(b, s, w=0.0, k=1.0):
        return sum(math.log(max(prob(rng, mu + b, s, w, k), 1e-9)) for mu, rng in days)
    b0, s0 = max(((b, s / 2) for b in range(-8, 9) for s in range(2, 21)), key=lambda a: ll(*a))
    b, s = max(((b0 + i / 4, round(s0 + j / 10, 2)) for i in range(-3, 4) for j in range(-4, 5)
                if s0 + j / 10 >= 0.5), key=lambda a: ll(*a))
    w, k = max(TAILS, key=lambda t: ll(b, s, *t))
    s = max((round(s + j / 10, 2) for j in range(-6, 3) if s + j / 10 >= 0.5), key=lambda x: ll(b, x, w, k))
    return b, s, w, k


def tiling(markets):
    """(events, events whose brackets cover every whole degree exactly once, with exactly
    one winner). A gap or an overlap means the strike mapping is off, and the tails are
    where the model and the market disagree most."""
    by_event = defaultdict(list)
    for m in markets:
        by_event[m.get("event_ticker")].append(m)
    ok = 0
    for ms in by_event.values():
        rngs = [bracket(m) for m in ms]
        if None in rngs:
            continue
        rngs.sort(key=lambda r: -math.inf if r[0] is None else r[0])
        ok += (rngs[0][0] is None and rngs[-1][1] is None
               and all(a[1] is not None and b[0] == a[1] + 1 for a, b in zip(rngs, rngs[1:]))
               and sum(1 for m in ms if m.get("result") == "yes") == 1)
    return len(by_event), ok


def quiet(client, series, obs, markets, hours_before_close):
    """The markets load_observations skipped for low lifetime volume, priced the same way.
    That filter uses volume known only after settling, and the brackets far from the
    result (the losers) are the quiet ones, so leaving them out flatters YES."""
    have, anchor = {o["ticker"] for o in obs}, {o["event"]: o["anchor"] for o in obs}
    out = []
    for m in markets:
        tk, ev = m.get("ticker"), m.get("event_ticker")
        if not tk or tk in have or ev not in anchor or num(m, "volume") >= backtest.MIN_VOLUME:
            continue
        try:
            value = yes_payout(m)
        except UnknownSettlement:
            continue
        if value is None or m.get("result") not in ("yes", "no"):
            continue
        t = backtest.decision_time(m, hours_before_close, anchor=anchor[ev])
        snap = t and backtest.snapshot(client, series, m, hours_before_close, anchor=anchor[ev])
        if snap:
            out.append({"series": series, "event": ev, "ticker": tk, "close": backtest._close(m) or anchor[ev],
                        "anchor": anchor[ev],
                        "decided": t, "yes_ask": snap[0], "yes_bid": snap[1], "result": m["result"],
                        "yes_value": value})
    return out


def priced(obs, markets, fc):
    """Each priced market with its forecast chance of YES, from fits on earlier days only.
    obs: price rows for one series (load_observations plus quiet). markets: its settled
    markets. fc: {event: (mu, ...) or None}. Returns rows with "p" (and "mu", "bias", "s")."""
    info = {m["ticker"]: m for m in markets}
    won = winners(markets)
    days = {}   # event -> (day, mu)
    for o in obs:
        if fc.get(o["event"]) and o["event"] not in days:
            days[o["event"]] = (day_of(o["anchor"]), fc[o["event"]][0])
    history = sorted((d, mu, won[e]) for e, (d, mu) in days.items() if e in won)
    fits, out = {}, []
    for o in obs:
        rng = bracket(info.get(o["ticker"]) or {})
        if not rng or o["event"] not in days:
            continue
        day, mu = days[o["event"]]
        week = day.toordinal() // 7   # refit weekly, on days settled before the week began
        if week not in fits:
            start = date.fromordinal(week * 7)
            past = [(m, r) for d, m, r in history if start - timedelta(days=FIT_DAYS) <= d < start - timedelta(days=1)]
            fits[week] = fit(past) if len(past) >= MIN_FIT else None
        if not fits[week]:
            continue
        b, s, w, k = fits[week]
        out.append({**o, "p": prob(rng, mu + b, s, w, k), "mu": mu, "bias": b, "s": s, "w": w, "k": k,
                    "won_range": won.get(o["event"])})
    return out


def trades(rows, margin):
    """{side: [trade]} buying where the forecast beats the price by `margin` per contract.
    The cost is a full-size order's, fee rounded up per order, as backtest.trade scores it.
    Trades on one day count as one unit (cities' errors move together)."""
    out = {"yes": [], "no": []}
    for o in rows:
        unit = str(day_of(o["anchor"]))
        ask, bid, p = o["yes_ask"], o["yes_bid"], o["p"]
        if 0.01 <= ask <= 0.99 and p - cost_each(ask) >= margin:
            out["yes"].append(backtest.trade(unit, ask, backtest.paid(o, "yes")))
        no = round(1 - bid, 4)
        if bid > 0 and 0.01 <= no <= 0.99 and (1 - p) - cost_each(no) >= margin:
            out["no"].append(backtest.trade(unit, no, backtest.paid(o, "no")))
    return out


CALIBRATION = [0, 0.02, 0.05, 0.10, 0.20, 0.40, 0.60, 1.01]


def calibration(rows):
    """Per range of the forecast's chance: markets, mean forecast, mean market mid, and how
    often YES won. Fat tails show as low ranges that win more often than forecast."""
    out = []
    for lo, hi in zip(CALIBRATION, CALIBRATION[1:]):
        r = [o for o in rows if lo <= o["p"] < hi]
        if r:
            out.append({"lo": lo, "hi": min(hi, 1.0), "markets": len(r),
                        "forecast": statistics.fmean(o["p"] for o in r),
                        "market": statistics.fmean((o["yes_ask"] + o["yes_bid"]) / 2 for o in r),
                        "won": statistics.fmean(backtest.paid(o, "yes") for o in r)})
    return out


def far_misses(rows):
    """(days, share whose result landed wholly beyond 2 spreads of the forecast). Under a
    Normal error that share is under 4.6%; far above it means fat tails."""
    days = {}
    for o in rows:
        rng = o.get("won_range")
        if rng and o["event"] not in days:
            mu, s = o["mu"] + o["bias"], o["s"]
            beyond = (rng[0] is not None and rng[0] - 0.5 > mu + 2 * s) or \
                     (rng[1] is not None and rng[1] + 0.5 < mu - 2 * s)
            days[o["event"]] = beyond
    return len(days), (sum(days.values()) / len(days) if days else None)


def scores(rows):
    """Brier scores of the forecast and of the market's mid price on the same markets. Only a
    guide to who called the results better: a trade has to beat the ask, not the mid."""
    if not rows:
        return None
    won = [backtest.paid(o, "yes") for o in rows]
    model = statistics.fmean((o["p"] - w) ** 2 for o, w in zip(rows, won))
    market = statistics.fmean(((o["yes_ask"] + o["yes_bid"]) / 2 - w) ** 2 for o, w in zip(rows, won))
    return {"markets": len(rows), "brier_forecast": model, "brier_market": market}
