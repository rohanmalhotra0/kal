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

from ..client import UnknownSettlement, fee_per_contract, num, yes_payout
from . import backtest

# Kalshi series -> the station its rules settle on (CLINYC is Central Park, CLIMDW Midway).
STATIONS = {"KXHIGHNY": "KNYC", "KXHIGHCHI": "KMDW", "KXHIGHMIA": "KMIA", "KXHIGHLAX": "KLAX",
            "KXHIGHDEN": "KDEN", "KXHIGHAUS": "KAUS", "KXHIGHPHIL": "KPHL"}
IEM_MOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/mos.py"
LATENCY = 2 * 3600        # use a run only once it's surely published
LOOK_BACK = 6 * 3600      # if that run isn't archived, the latest one up to 6 hours older
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


def prob(rng, mu, s):
    """Chance the reported (whole-degree) high lands in rng, if the high is Normal(mu, s)."""
    lo, hi = rng
    up = 1.0 if hi is None else phi((hi + 0.5 - mu) / s)
    down = 0.0 if lo is None else phi((lo - 0.5 - mu) / s)
    return max(0.0, up - down)


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
    -> CSV text. Cached in cache_file (runs never change). An empty reply isn't cached:
    it may be a hiccup, so it's asked again next time."""
    cache = {}
    if cache_file and os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
    out, new = {}, 0
    try:
        for runtime, day in sorted(set(wants)):
            key = f"{runtime}|{day}"
            if key not in cache:
                stamp = datetime.fromtimestamp(runtime, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
                since = datetime.fromtimestamp(runtime - LOOK_BACK, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
                try:
                    text = fetch(IEM_MOS, {"station": station, "model": "NBS", "sts": since, "ets": stamp,
                                           "format": "csv"})
                except Exception as e:  # one failed run: skip that day, try again next time
                    log.warning("NBM %s %s: %s", station, stamp, e)
                    continue
                if len((text or "").strip().splitlines()) < 2:
                    continue  # no rows at all: not cached
                got = parse_max(text, day, runtime)
                cache[key] = list(got) if got else None
                new += 1
            out[(runtime, day)] = tuple(cache[key]) if cache[key] else None
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
    """(bias, spread) in °F that best explain past days, by likelihood: a coarse grid (bias
    -8..8, spread 1..10), then a finer one around the best. days: [(mu, (lo, hi))]."""
    def ll(a):
        return sum(math.log(max(prob(rng, mu + a[0], a[1]), 1e-9)) for mu, rng in days)
    b0, s0 = max(((b, s / 2) for b in range(-8, 9) for s in range(2, 21)), key=ll)
    return max(((b0 + i / 4, round(s0 + j / 10, 2)) for i in range(-3, 4) for j in range(-4, 5)
                if s0 + j / 10 >= 0.5), key=ll)


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
        b, s = fits[week]
        out.append({**o, "p": prob(rng, mu + b, s), "mu": mu, "bias": b, "s": s})
    return out


def trades(rows, margin):
    """{side: [trade]} buying where the forecast beats the price by `margin` per contract,
    after the taker fee. Trades on one day count as one unit (cities' errors move together)."""
    out = {"yes": [], "no": []}
    for o in rows:
        unit = str(day_of(o["anchor"]))
        ask, bid, p = o["yes_ask"], o["yes_bid"], o["p"]
        if 0.01 <= ask <= 0.99 and p - ask - fee_per_contract(ask) >= margin:
            out["yes"].append(backtest.trade(unit, ask, backtest.paid(o, "yes")))
        no = round(1 - bid, 4)
        if bid > 0 and 0.01 <= no <= 0.99 and (1 - p) - no - fee_per_contract(no) >= margin:
            out["no"].append(backtest.trade(unit, no, backtest.paid(o, "no")))
    return out


def scores(rows):
    """Brier scores of the forecast and of the market's mid price on the same markets. Only a
    guide to who called the results better: a trade has to beat the ask, not the mid."""
    if not rows:
        return None
    won = [backtest.paid(o, "yes") for o in rows]
    model = statistics.fmean((o["p"] - w) ** 2 for o, w in zip(rows, won))
    market = statistics.fmean(((o["yes_ask"] + o["yes_bid"]) / 2 - w) ** 2 for o, w in zip(rows, won))
    return {"markets": len(rows), "brier_forecast": model, "brier_market": market}
