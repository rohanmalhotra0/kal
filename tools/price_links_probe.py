"""Idea #19: one price at one moment, asked two ways in two series (range vs above/below).
Probe branch only, never merged. Public data only (no key, no orders).

Phase "discover" (this file's first run): find series pairs whose events close at the same
moment and whose rules read the same once the numbers and the comparison words are taken
out. Print each pair's rules, settlement sources, strike grids and boundary wording, how
settled values look (rounded or not), and whether event-level candlesticks work.
"""
import collections
import itertools
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal.client import Kalshi, KalshiError, ts  # noqa: E402

ET = ZoneInfo("America/New_York")
H, DAY = 3600, 86400
CATS = {"Crypto", "Financials", "Commodities"}
WORDS = re.compile(
    r"bitcoin|\bbtc|ethereum|\beth\b|solana|\bsol\b|\bxrp|doge|\bbch\b|litecoin|cardano|"
    r"\bhype\b|\bbnb\b|avax|chainlink|s&p|\bspx\b|\binx|nasdaq|\bndx\b|\bdow\b|djia|russell|\bvix\b|"
    r"stock price|\beur|\busd|\bjpy\b|\byen\b|\bgbp\b|pound|dollar|forex|yuan|peso|gold|silver|copper|"
    r"platinum|palladium|\boil\b|\bwti\b|brent|natural gas|gasoline|gas price|treasury|yield|mortgage", re.I)
FREQ_ORDER = {"hourly": 0, "daily": 1, "weekly": 2, "monthly": 3, "quarterly": 4, "annual": 5, "custom": 6}
DROP = set("above below between and or to over under greater less than more fewer at least most exactly "
           "higher lower equal is be the of a an".split())
MKT_KEYS = ("ticker", "strike_type", "floor_strike", "cap_strike", "yes_sub_title", "no_sub_title",
            "functional_strike", "custom_strike", "open_time", "close_time", "expected_expiration_time",
            "latest_expiration_time", "can_close_early", "early_close_condition", "settlement_timer_seconds",
            "market_type", "settlement_bounds_type", "price_level_structure", "response_price_units",
            "fractional_trading_enabled")


def stamp(t):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)) if t else None


def iso(v):
    try:
        return ts(v) if v else None
    except (TypeError, ValueError, AttributeError):
        return None


def plain(t):
    return " ".join(str(t or "").split())


def template(text):
    """Rules text with numbers and comparison words taken out."""
    t = plain(text).lower()
    t = re.sub(r"\$?\s?-?\d[\d,]*(\.\d+)?%?", " # ", t)
    t = re.sub(r"[^a-z#&' ]+", " ", t)
    return " ".join(w for w in t.split() if w not in DROP and w != "#")


def decimals(v):
    s = str(v)
    return len(s.split(".", 1)[1].rstrip("0")) if "." in s else 0


def event_code(t):
    """Kalshi's hourly crypto event suffix for an ET time: 26OCT2908."""
    d = datetime.fromtimestamp(t, ET)
    return d.strftime("%y%b%d%H").upper()


def discover():
    c = Kalshi()
    t0 = time.time()
    allser = c.series()
    print(f"series total: {len(allser)}")
    cats = collections.Counter(s.get("category") for s in allser)
    print("categories: " + json.dumps(cats.most_common()))
    cands = [s for s in allser if s.get("frequency") != "fifteen_min" and (
        s.get("category") in CATS or WORDS.search(f"{s.get('title') or ''} {s.get('ticker') or ''}"))]
    cands.sort(key=lambda s: (FREQ_ORDER.get(s.get("frequency"), 9), s["ticker"]))
    print(f"candidate series: {len(cands)}; by frequency "
          f"{json.dumps(collections.Counter(s.get('frequency') for s in cands).most_common())}")
    meta = {s["ticker"]: s for s in cands}

    # Open events of each candidate series, summarised.
    events, groups, sample = {}, collections.defaultdict(set), {}
    n_err, done_series = 0, 0
    for i, s in enumerate(cands):
        if time.time() - t0 > 10 * 60:
            print(f"(time limit at series {i} of {len(cands)}; next {s['ticker']} {s.get('frequency')})")
            break
        done_series += 1
        try:
            evs = c.events(series_ticker=s["ticker"], status="open", with_nested_markets="true")
        except Exception:
            n_err += 1
            continue
        for ev in evs:
            ms = ev.get("markets") or []
            if len(ms) < 2:
                continue
            closes = collections.Counter(iso(m.get("close_time")) for m in ms)
            sts = collections.Counter(m.get("strike_type") for m in ms)
            tmpl = collections.Counter(template(m.get("rules_primary")) for m in ms)
            for m in ms:
                sample.setdefault((s["ticker"], m.get("strike_type")), dict(m, _ev=ev["event_ticker"]))
            events[ev["event_ticker"]] = dict(
                series=s["ticker"], title=ev.get("title"), sub=ev.get("sub_title"), strike_date=ev.get("strike_date"),
                n=len(ms), closes=closes, sts=sts, tmpl=tmpl,
                open=min(filter(None, (iso(m.get("open_time")) for m in ms)), default=None))
            for ct in closes:
                for tp in tmpl:
                    groups[(ct, tp)].add((s["ticker"], ev["event_ticker"]))
    print(f"series read: {done_series}; open events with 2+ markets: {len(events)} "
          f"(series errors {n_err}) in {time.time() - t0:.0f}s")
    ser_with = collections.Counter(e["series"] for e in events.values())
    print("series with open events: " + " ".join(f"{s}:{n}" for s, n in sorted(ser_with.items())))

    # Pairs of series that share a close time and a rules template.
    pair_n, pair_ex = collections.Counter(), {}
    for (ct, tp), members in groups.items():
        sers = sorted({s for s, _ in members})
        for a in range(len(sers)):
            for b in range(a + 1, len(sers)):
                k = (sers[a], sers[b])
                pair_n[k] += 1
                pair_ex.setdefault(k, (ct, tp, sorted(members)))
    print(f"\n===== SERIES PAIRS sharing a close time and a rules template: {len(pair_n)}")
    for k, n in pair_n.most_common(150):
        ct, tp, mem = pair_ex[k]
        print(f"  PAIR {k[0]} ~ {k[1]}: {n} close times; e.g. {stamp(ct)} "
              f"{[e for s, e in mem if s in k][:4]}")
        print(f"       template: {tp[:240]}")

    # Same close time, one has 'between', the other greater/less, rules worded differently.
    by_close = collections.defaultdict(list)
    for et, e in events.items():
        for ct in e["closes"]:
            by_close[ct].append(et)
    loose, loose_ex = collections.Counter(), {}
    for ct, ets in by_close.items():
        for x in ets:
            for y in ets:
                ex, ey = events[x], events[y]
                if ex["series"] >= ey["series"] or (ex["series"], ey["series"]) in pair_n:
                    continue
                kinds = set(ex["sts"]) | set(ey["sts"])
                if "between" in kinds and kinds & {"greater", "less", "greater_or_equal", "less_or_equal"}:
                    loose[(ex["series"], ey["series"])] += 1
                    loose_ex.setdefault((ex["series"], ey["series"]), (x, y))
    print(f"\n===== same close time, between + above/below, rules worded differently: {len(loose)} pairs")
    for k, n in loose.most_common(80):
        x, y = loose_ex[k]
        print(f"  LOOSE {k[0]} ~ {k[1]}: {n} | {x}: {events[x]['title']!r} | {y}: {events[y]['title']!r}")

    # Details per series that is in a pair.
    in_pairs = sorted({s for k in pair_n for s in k} | {s for k, n in loose.most_common(30) for s in k})
    print(f"\n===== SERIES DETAILS ({len(in_pairs)} series)")
    for s in in_pairs:
        m0 = meta.get(s, {})
        evs = sorted((e for e in events.values() if e["series"] == s), key=lambda e: min(e["closes"]))
        print(f"\n## {s} | {m0.get('category')} | {m0.get('frequency')} | {m0.get('title')} | "
              f"fee {m0.get('fee_type')} x{m0.get('fee_multiplier')} | open events {len(evs)}")
        print(f"   sources: {json.dumps(m0.get('settlement_sources'))[:300]} | contract {m0.get('contract_url')}")
        if evs:
            e = evs[0]
            print(f"   first open event: n={e['n']} sts={dict(e['sts'])} close={stamp(min(e['closes']))} "
                  f"opened={stamp(e['open'])} strike_date={e['strike_date']} title={e['title']!r} sub={e['sub']!r}")
            lens = sorted((min(x["closes"]) - x["open"]) / H for x in evs if x["open"])
            if lens:
                print(f"   event life (open->close hours), open events: min {lens[0]:.1f} "
                      f"median {lens[len(lens) // 2]:.1f} max {lens[-1]:.1f}; closes "
                      f"{stamp(min(min(x['closes']) for x in evs))} .. {stamp(max(max(x['closes']) for x in evs))}")
        for st in ("between", "greater", "greater_or_equal", "less", "less_or_equal", "functional", "custom", None):
            m = sample.get((s, st))
            if not m:
                continue
            keep = {k: m.get(k) for k in MKT_KEYS if m.get(k) not in (None, "")}
            print(f"   [{st}] {json.dumps(keep)}")
            print(f"      R1 {plain(m.get('rules_primary'))[:700]}")
            print(f"      R2 {plain(m.get('rules_secondary'))[:1100]}")

    # Strike grids at one shared close time per pair.
    print("\n===== GRIDS (one shared close time per pair)")
    for k, n in pair_n.most_common(40):
        ct, tp, mem = pair_ex[k]
        for s, et in mem:
            if s not in k:
                continue
            try:
                ms = c.markets(event_ticker=et)
            except Exception as e:
                print(f"  {et}: failed {e}")
                continue
            rows = collections.defaultdict(list)
            for m in ms:
                rows[m.get("strike_type")].append((m.get("floor_strike"), m.get("cap_strike")))
            for st, xs in rows.items():
                xs.sort(key=lambda x: (x[0] if x[0] is not None else -1e18, x[1] if x[1] is not None else 1e18))
                print(f"  {k[0]}~{k[1]} {et} [{st}] n={len(xs)} first {xs[:4]} last {xs[-3:]}")

    # Settled values: decimals.
    print("\n===== SETTLED VALUES (expiration_value) per series in pairs")
    for s in in_pairs[:50]:
        try:
            ms = c.markets(limit=1000, series_ticker=s, status="settled")
        except Exception as e:
            print(f"  {s}: failed {e}")
            continue
        vals, dec, fields = collections.Counter(), collections.Counter(), set()
        for m in ms:
            fields |= {k for k in m if "value" in k or "expir" in k or "settle" in k}
            v = m.get("expiration_value")
            if v not in (None, ""):
                vals[v] += 1
                dec[decimals(v)] += 1
        print(f"  {s}: settled {len(ms)}; distinct expiration_value {len(vals)}; decimals {dict(dec)}; "
              f"e.g. {list(vals)[:6]}; fields {sorted(fields)}")

    # Event-level candlesticks: one call per event instead of one per market?
    print("\n===== CANDLE ENDPOINTS")
    now = time.time()
    for s in ("KXBTCD", "KXBTC"):
        for back in (0.2, 3, 30, 60, 100):
            t = now - back * DAY
            d0 = datetime.fromtimestamp(t, ET).replace(hour=17, minute=0, second=0, microsecond=0)
            if d0.timestamp() > now:
                d0 -= timedelta(days=1)
            end = int(d0.timestamp())
            et = f"{s}-{event_code(end)}"
            for period in (60, 1):
                for path in (f"/series/{s}/events/{et}/candlesticks", f"/historical/events/{et}/candlesticks"):
                    t1 = time.time()
                    try:
                        d = c.get(path, {"start_ts": end - 26 * H, "end_ts": end, "period_interval": period})
                        mt = d.get("market_tickers") or []
                        mc = d.get("market_candlesticks") or []
                        lens = [len(x or []) for x in mc]
                        first = next((x[-1] for x in mc if x), None)
                        print(f"  {path} p={period}: {time.time() - t1:.1f}s keys={sorted(d)} markets={len(mt)} "
                              f"candles/market max {max(lens, default=0)} sum {sum(lens)} adj_end={d.get('adjusted_end_ts')}")
                        if first and back < 1 and period == 60:
                            print(f"     candle: {json.dumps(first)[:600]}")
                    except KalshiError as e:
                        print(f"  {path} p={period}: {e.status} {str(e)[:160]}")
                    except Exception as e:
                        print(f"  {path} p={period}: error {str(e)[:160]}")
            try:
                ms = c.markets(event_ticker=et)
                print(f"  /markets?event_ticker={et}: {len(ms)} markets, statuses "
                      f"{dict(collections.Counter(m.get('status') for m in ms))}")
            except Exception as e:
                print(f"  /markets?event_ticker={et}: {str(e)[:160]}")
            try:
                ms = c.paginate("/historical/markets", "markets", {"limit": 1000, "event_ticker": et})
                print(f"  /historical/markets?event_ticker={et}: {len(ms)} markets")
            except Exception as e:
                print(f"  /historical/markets?event_ticker={et}: {str(e)[:160]}")
    tick = [m["ticker"] for k, m in list(sample.items())[:3]]
    try:
        d = c.get("/markets/candlesticks", {"market_tickers": ",".join(tick), "start_ts": int(now) - 6 * H,
                                            "end_ts": int(now), "period_interval": 60})
        print(f"  /markets/candlesticks batch: keys={sorted(d)} {json.dumps(d)[:300]}")
    except Exception as e:
        print(f"  /markets/candlesticks batch: {str(e)[:200]}")
    print(f"\ndone in {time.time() - t0:.0f}s")




# ======================= history and live =======================

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from price_links_sets import Linker, cover, disjoint, ivs_of, payout_min, subset  # noqa: E402

from kal import config  # noqa: E402
from kal.client import candle_close, fee, price  # noqa: E402
from kal.fees import _bad as fee_problem  # noqa: E402
from kal.risk import MAX_ARB_ROI, yearly_return  # noqa: E402

N = 30   # contracts per order, for the fee's cent rounding (max_per_arb is $30)
HOURLY_PER_DAY = 3
DAILY_ONLY = {"SHIB"}
CRYPTO = [("BTC", "KXBTC", "KXBTCD"), ("ETH", "KXETH", "KXETHD"), ("SOL", "KXSOLE", "KXSOLD"),
          ("XRP", "KXXRP", "KXXRPD"), ("DOGE", "KXDOGE", "KXDOGED"), ("BNB", "KXBNB", "KXBNBD"),
          ("HYPE", "KXHYPE", "KXHYPED"), ("SHIB", "KXSHIBA", "KXSHIBAD")]


def fee_pc(p):
    return fee(N, p) / N


def suffix(et):
    return str(et or "").split("-", 1)[-1]


def tick_of(markets):
    """The strike grid's step: 10^-d for the most decimals any strike or round_digits shows."""
    d = 0
    for m in markets:
        cs = m.get("custom_strike") if isinstance(m.get("custom_strike"), dict) else {}
        vals = [m.get("floor_strike"), m.get("cap_strike"), cs.get("floor_strike"), cs.get("cap_strike")]
        for v in vals:
            if v in (None, ""):
                continue
            s = str(v)
            if "e" in s.lower():
                s = f"{float(s):.12f}"
            if "." in s:
                d = max(d, len(s.split(".", 1)[1].rstrip("0")))
        try:
            d = max(d, int(cs.get("round_digits")))
        except (TypeError, ValueError):
            pass
    return 10.0 ** -d


def event_markets(c, et):
    try:
        ms = c.markets(event_ticker=et)
    except KalshiError:
        ms = []
    if not ms:
        try:
            ms = c.paginate("/historical/markets", "markets", {"limit": 1000, "event_ticker": et})
        except KalshiError:
            ms = []
    return ms


def event_candles(c, series, et, t0, t1, period):
    """{ticker: [(end_ts, bid, ask, volume)] sorted}, paging on adjusted_end_ts; None on failure."""
    out = collections.defaultdict(dict)
    start, calls = int(t0), 0
    while calls < 30:
        calls += 1
        try:
            d = c.get(f"/series/{series}/events/{et}/candlesticks",
                      {"start_ts": start, "end_ts": int(t1), "period_interval": period})
        except KalshiError:
            return None
        for t, rows in zip(d.get("market_tickers") or [], d.get("market_candlesticks") or []):
            for r in rows or []:
                v = r.get("volume_fp", r.get("volume"))
                try:
                    v = float(v or 0)
                except (TypeError, ValueError):
                    v = 0.0
                out[t][r["end_period_ts"]] = (r["end_period_ts"], candle_close(r, "yes_bid"),
                                              candle_close(r, "yes_ask"), v)
        adj = d.get("adjusted_end_ts")
        if not adj or adj >= int(t1) or adj <= start:
            break
        start = int(adj)
    return {t: sorted(rows.values()) for t, rows in out.items()}


def snapshots(cands, times, stale, step):
    """For each time, {ticker: (bid, ask)} from the last candle that ended by then (no look-ahead),
    and {ticker: contracts traded in the `step` seconds before}."""
    idx = {t: 0 for t in cands}
    for t in times:
        q, vol = {}, {}
        for tk, rows in cands.items():
            i = idx[tk]
            while i + 1 < len(rows) and rows[i + 1][0] <= t:
                i += 1
            idx[tk] = i
            if rows and rows[i][0] <= t and t - rows[i][0] <= stale:
                q[tk] = (rows[i][1], rows[i][2])
                v, j = 0.0, i
                while j >= 0 and rows[j][0] > t - step:
                    v += rows[j][3]
                    j -= 1
                vol[tk] = v
        yield t, q, vol


class Tally:
    def __init__(self, cfg):
        self.cfg = cfg
        self.n = collections.Counter()
        self.hours = collections.defaultdict(set)
        self.pass_hours = collections.defaultdict(set)
        self.best = {}
        self.near = {}

    def add(self, key, fam, mode, pay, legs, t, settle, event, vols, ivs, tick):
        k = (key, fam, mode)
        raw = sum(p for _, _, p in legs)
        cost = raw + sum(fee_pc(p) for _, _, p in legs)
        edge = pay - cost
        self.n[k + ("set-snapshots",)] += 1
        if k not in self.near or edge > self.near[k][0]:
            self.near[k] = (round(edge, 4), event, stamp(t), [(tk, s, p) for tk, s, p in legs])
        if raw < pay:
            self.n[k + ("under before fees",)] += 1
        if cost >= pay:
            return
        lg = [(ivs[tk], s) for tk, s, _ in legs]
        if payout_min(lg, tick, "grid") < pay or (mode == "robust" and payout_min(lg, tick, "robust") < pay):
            self.n[k + ("BUG: set doesn't pay what it should",)] += 1
            return
        self.n[k + ("under after fees",)] += 1
        self.hours[k].add((event, t))
        days = max((settle - t) / DAY, 1 / 24)
        roi = edge / cost
        apr = yearly_return(pay / cost, days)
        ok = days <= self.cfg["max_arb_days"] and apr >= self.cfg["min_arb_apr"] and 0.01 <= roi <= MAX_ARB_ROI
        if roi > MAX_ARB_ROI:
            self.n[k + ("too good to be true (>25%)",)] += 1
        if ok:
            self.n[k + ("passing the bot's rules",)] += 1
            self.pass_hours[k].add((event, t))
        row = dict(edge=round(edge, 4), roi=round(roi, 4), cost=round(cost, 4), pay=pay, t=stamp(t), event=event,
                   legs=[(tk, s, p, vols.get(tk, 0)) for tk, s, p in legs], days=round(days, 3), ok=ok)
        bk = (k, event)
        if bk not in self.best or edge > self.best[bk]["edge"]:
            self.best[bk] = row

    def show(self, keys):
        for key in keys:
            fams = sorted({(f, m) for (k, f, m, *_) in self.n if k == key})
            if not fams:
                continue
            print(f"   -- {key}")
            for f, m in fams:
                row = {name: self.n[(key, f, m, name)] for name in
                       ("set-snapshots", "under before fees", "under after fees", "passing the bot's rules",
                        "too good to be true (>25%)", "BUG: set doesn't pay what it should")
                       if self.n[(key, f, m, name)]}
                hrs, ph = self.hours[(key, f, m)], self.pass_hours[(key, f, m)]
                print(f"      {f:9} {m:6} {json.dumps(row)} | event-times under $ after fees {len(hrs)} "
                      f"(events {len({e for e, _ in hrs})}); passing {len(ph)} (events {len({e for e, _ in ph})})")
                near = self.near.get((key, f, m))
                if near:
                    print(f"         closest: {json.dumps(near)}")
            rows = [r for (k, e), r in self.best.items() if k[0] == key]
            good = sorted((r for r in rows if r["ok"]), key=lambda r: -r["edge"])
            bad = sorted((r for r in rows if not r["ok"]), key=lambda r: -r["edge"])
            for r in good[:12]:
                print(f"      PASS {json.dumps(r)}")
            for r in bad[:4]:
                print(f"      FAILS-RULES {json.dumps(r)}")


def check_results(name, rms, ams, ivs, tick, n, broken):
    """Settled results that can't all be right."""
    def res(m):
        return m.get("result") if m.get("result") in ("yes", "no") else None
    if any(res(m) is None for m in rms + ams):
        n[(name, "event pairs with a result not yes/no")] += 1
        return
    n[(name, "settled event pairs checked")] += 1
    vals = {str(m.get("expiration_value")) for m in rms + ams if m.get("expiration_value") not in (None, "")}
    if len(vals) > 1:
        n[(name, "pairs whose expiration values differ")] += 1
        broken.append((name, "values differ", sorted(vals)[:4], rms[0].get("event_ticker")))
    for m in rms + ams:
        try:
            v = float(str(m.get("expiration_value")).replace(",", "").replace("$", ""))
        except (TypeError, ValueError):
            n[(name, "markets with no numeric expiration value")] += 1
            continue
        for iv in ivs.get(m["ticker"]) or []:
            if iv.fuzzy:
                ok = (iv.lo <= v <= iv.hi) == (res(m) == "yes") or v in (iv.lo, iv.hi)
            else:
                ok = iv.has(v) == (res(m) == "yes")
            if not ok:
                n[(name, "markets whose result disagrees with the value")] += 1
                broken.append((name, "result vs value", m["ticker"], res(m), m.get("expiration_value"), repr(iv)))
                break
    if any(m.get("strike_type") == "between" for m in rms):
        yes_r = sum(1 for m in rms if res(m) == "yes")
        n[(name, f"range events with {yes_r} YES")] += 1
    for mx in rms:
        x = ivs.get(mx["ticker"])
        if not x:
            continue
        for my in ams:
            y = ivs.get(my["ticker"])
            if not y:
                continue
            rx, ry = res(mx) == "yes", res(my) == "yes"
            for mode in ("robust", "grid"):
                bad = None
                if subset(x, y, mode, tick) and rx and not ry:
                    bad = "x YES but its superset y NO"
                elif subset(y, x, mode, tick) and ry and not rx:
                    bad = "y YES but its superset x NO"
                elif disjoint(x, y, mode, tick) and rx and ry:
                    bad = "both YES though they can't be"
                elif cover(x, y, mode, tick) and not rx and not ry:
                    bad = "both NO though one must be YES"
                if bad:
                    n[(name, f"{mode}: links broken")] += 1
                    broken.append((name, mode, bad, mx["ticker"], my["ticker"], mx.get("expiration_value")))


def candidate_events(now, since_days, hourly_per_day):
    """Event suffixes to try, newest first: every day's 5 PM ET event, and a few other hours a day
    (rotating, so every hour of the day gets looked at)."""
    out = []
    day0 = datetime.fromtimestamp(now, ET).replace(minute=0, second=0, microsecond=0)
    for d in range(int(since_days) + 1):
        base = day0 - timedelta(days=d)
        five = base.replace(hour=17)
        if five.timestamp() < now - 600:
            out.append(("5pm", int(five.timestamp()), event_code(five.timestamp())))
        for j in range(hourly_per_day):
            hr = (d * 7 + j * (24 // max(1, hourly_per_day))) % 24
            if hr == 17:
                hr = 18
            t = base.replace(hour=hr)
            if t.timestamp() < now - 600:
                out.append(("hourly", int(t.timestamp()), event_code(t.timestamp())))
    return out


def history(pairs, budget_s, since_days):
    cfg = config.load()
    c = Kalshi()
    t_start = time.time()
    now = time.time()
    tally, n, broken = Tally(cfg), collections.Counter(), []
    for name, rs, as_ in pairs:
        t_pair = time.time()
        for s in (rs, as_):
            try:
                info = c.series_info(s)
                print(f"{s}: fee {info.get('fee_type')} x{info.get('fee_multiplier')} "
                      f"-> {fee_problem(info.get('fee_type'), info.get('fee_multiplier')) or 'the bot can price it'}")
            except Exception as e:
                print(f"{s}: series info failed: {e}")
        cand = candidate_events(now, since_days, 0 if name in DAILY_ONLY else HOURLY_PER_DAY)
        # Newest first, one 5pm event then two hourly ones, so the time budget covers both kinds.
        fives, hours = [x for x in cand if x[0] == "5pm"], [x for x in cand if x[0] == "hourly"]
        order = []
        while fives or hours:
            if fives:
                order.append(fives.pop(0))
            order += hours[:2]
            hours = hours[2:]
        print(f"\n== {name} ({rs} vs {as_}): trying {len(order)} event pairs over {since_days:g} days")
        done = collections.Counter()
        seen_t = []
        for kind0, t_close, suf in order:
            spent = time.time() - t_pair
            if spent > budget_s * 1.4:
                n[(name, "event pairs left out (time)")] += 1
                continue
            results_only = spent > budget_s
            er, ea = {"event_ticker": f"{rs}-{suf}"}, {"event_ticker": f"{as_}-{suf}"}
            rms, ams = event_markets(c, er["event_ticker"]), event_markets(c, ea["event_ticker"])
            if not rms or not ams:
                n[(name, f"{kind0} event pairs missing a side")] += 1
                continue
            closes = {iso(m.get("close_time")) for m in rms + ams}
            if len(closes) != 1:
                n[(name, "event pairs whose markets close at different times")] += 1
                continue
            t_close = closes.pop()
            seen_t.append(t_close)
            tick = tick_of(rms + ams)
            ivs, flags = {}, collections.Counter()
            for m in rms + ams:
                v, fl = ivs_of(m)
                ivs[m["ticker"]] = v
                for f in fl:
                    flags[f] += 1
                    n[(name, f"markets: {f}")] += 1
            if flags.get("strike fields and rules differ") and done["mismatch shown"] < 3:
                done["mismatch shown"] += 1
                ex = next(m for m in rms + ams if len(ivs[m["ticker"]]) > 1)
                print(f"   MISMATCH {ex['ticker']} {ivs[ex['ticker']]} | {plain(ex.get('rules_primary'))[:160]}")
            check_results(name, rms, ams, ivs, tick, n, broken)
            if results_only:
                n[(name, "event pairs checked for results only (time)")] += 1
                continue
            opened = min(filter(None, (iso(m.get("open_time")) for m in rms + ams)), default=None)
            settle = max(filter(None, (iso(m.get("expected_expiration_time")) or iso(m.get("close_time"))
                                       for m in rms + ams)), default=t_close)
            if not opened:
                continue
            life = t_close - opened
            period = 1 if life <= 2 * H else 60
            cr = event_candles(c, rs, er["event_ticker"], opened, t_close, period)
            ca = event_candles(c, as_, ea["event_ticker"], opened, t_close, period)
            if not cr or not ca:
                n[(name, "event pairs without candles")] += 1
                continue
            kind = "hourly events (5-min samples)" if period == 1 else "5pm events (hourly samples)"
            n[(name, f"event pairs priced: {kind}")] += 1
            L = Linker([(m["ticker"], ivs[m["ticker"]]) for m in rms], [(m["ticker"], ivs[m["ticker"]]) for m in ams], tick)
            if done["relations shown"] < 1:
                done["relations shown"] += 1
                print(f"   relations in {suf} (tick {tick:g}): {json.dumps({f'{a}/{b}': v for (a, b), v in L.n_relations().items()})}"
                      f"; A markets of other shapes {len(L.other)}")
            step = 300 if period == 1 else H
            first = (int(opened) // step + 1) * step
            times = list(range(first, int(t_close), step))   # before the close only
            cands = {**cr, **ca}
            for t, q, vol in snapshots(cands, times, 600 if period == 1 else 2 * H, step):
                key = f"{name} {kind}"
                for fam, mode, pay, legs in L.sets(q):
                    tally.add(key, fam, mode, pay, legs, t, settle, er["event_ticker"], vol, ivs, tick)
        if seen_t:
            print(f"   covered {stamp(min(seen_t))} .. {stamp(max(seen_t))}")
        print(f"   {name}: {time.time() - t_pair:.0f}s; " + "; ".join(f"{k[1]}: {v}" for k, v in sorted(n.items()) if k[0] == name))
        tally.show([f"{name} 5pm events (hourly samples)", f"{name} hourly events (5-min samples)"])
        sys.stdout.flush()
    print(f"\n===== BROKEN LINKS / BAD RESULTS: {len(broken)}")
    for b in broken[:40]:
        print("   BROKEN " + json.dumps(b))
    print(f"\nhistory done in {time.time() - t_start:.0f}s")


OTHER = [("SPX 4pm", "KXINX", "KXINXU"), ("NDX 4pm", "KXNASDAQ100", "KXNASDAQ100U"),
         ("SPX year-end", "KXINXY", "KXINXDIRY"), ("NDX year-end", "KXNASDAQ100Y", "KXNDQDIRY"),
         ("WTI", "KXWTIW", "KXWTI"), ("Gold", "KXGOLDW", "KXGOLDMON"), ("Silver", "KXSILVERW", "KXSILVERMON"),
         ("Copper", "KXCOPPERW", "KXCOPPERMON"), ("NatGas", "KXNATGASW", "KXNATGASMON"),
         ("Brent", "KXBRENTW", "KXBRENTMON"), ("UST10 W/M", "KXUST10A", "KXUST10AM"),
         ("UST2 W/M", "KXUST2A", "KXUST2AM"), ("UST5 W/M", "KXUST5A", "KXUST5AM"),
         ("UST7 W/M", "KXUST7A", "KXUST7AM"), ("UST30 W/M", "KXUST30A", "KXUST30AM")]


def book_quote(book, side):
    """(price, size) to buy `side` now from an order book of bids, or (None, 0)."""
    levels = book.get("no" if side == "yes" else "yes") or []
    if not levels:
        return None, 0
    bid, size = max(levels)
    ask = round(1 - bid, 4)
    return (ask, size) if 0 < ask < 1 else (None, 0)


def live(pairs, rounds, gap_s):
    cfg = config.load()
    c = Kalshi()
    t0 = time.time()
    seen_gaps = {}
    closest = {}
    for rnd in range(rounds):
        if rnd:
            time.sleep(gap_s)
        now = time.time()
        stats = collections.Counter()
        for name, rs, as_ in pairs:
            try:
                er = {suffix(e["event_ticker"]): e for e in c.events(series_ticker=rs, status="open", with_nested_markets="true")}
                ea = {suffix(e["event_ticker"]): e for e in c.events(series_ticker=as_, status="open", with_nested_markets="true")}
            except Exception as e:
                print(f"  {name}: listing failed {e}")
                continue
            for suf in sorted(set(er) & set(ea)):
                rms, ams = er[suf].get("markets") or [], ea[suf].get("markets") or []
                closes = {iso(m.get("close_time")) for m in rms + ams}
                if len(closes) != 1 or not rms or not ams:
                    stats[(name, "open pairs skipped (close times differ)")] += 1
                    continue
                t_close = closes.pop()
                settle = max(filter(None, (iso(m.get("expected_expiration_time")) or t_close for m in rms + ams)))
                stats[(name, "open event pairs")] += 1
                tick = tick_of(rms + ams)
                ivs = {m["ticker"]: ivs_of(m)[0] for m in rms + ams}
                L = Linker([(m["ticker"], ivs[m["ticker"]]) for m in rms], [(m["ticker"], ivs[m["ticker"]]) for m in ams], tick)
                q = {m["ticker"]: (price(m, "yes_bid"), price(m, "yes_ask")) for m in rms + ams
                     if m.get("status") in ("active", "open")}
                for fam, mode, pay, legs in L.sets(q):
                    cost = sum(p + fee_pc(p) for _, _, p in legs)
                    edge = pay - cost
                    stats[(name, "sets priced")] += 1
                    k = (name, fam, mode)
                    if k not in closest or edge > closest[k][0]:
                        closest[k] = (round(edge, 4), suf, stamp(now), [(tk, sd, p) for tk, sd, p in legs])
                    if edge <= 0:
                        continue
                    stats[(name, f"{fam}/{mode} under $ after fees (top of book)")] += 1
                    # confirm on the books
                    books, sizes, prices = {}, [], []
                    for tk, sd, _ in legs:
                        try:
                            books[tk] = c.orderbook(tk)
                        except Exception:
                            books[tk] = {}
                        p_, sz = book_quote(books[tk], sd)
                        prices.append(p_)
                        sizes.append(sz)
                    if None in prices:
                        continue
                    pm = payout_min([(ivs[tk], sd) for tk, sd, _ in legs], tick, "robust" if mode == "robust" else "grid")
                    if pm < pay:
                        stats[(name, "BUG: set doesn't pay what it should")] += 1
                        continue
                    cost_b = sum(p + fee_pc(p) for p in prices)
                    edge_b = pay - cost_b
                    days = max((settle - now) / DAY, 1 / 24)
                    roi = edge_b / cost_b
                    ok = (days <= cfg["max_arb_days"] and yearly_return(pay / cost_b, days) >= cfg["min_arb_apr"]
                          and 0.01 <= roi <= MAX_ARB_ROI)
                    row = dict(t=stamp(now), name=name, fam=fam, mode=mode, pay=pay, edge_book=round(edge_b, 4),
                               roi=round(roi, 4), size=min(sizes), days=round(days, 3), ok=ok,
                               legs=[(tk, sd, p_, sz) for (tk, sd, _), p_, sz in zip(legs, prices, sizes)])
                    key = (name, fam, mode, tuple(tk for tk, _, _ in legs))
                    if edge_b > 0 and (key not in seen_gaps or edge_b > seen_gaps[key]["edge_book"]):
                        seen_gaps[key] = row
        print(f"round {rnd + 1} at {stamp(now)}: " + "; ".join(f"{a} {b}: {v}" for (a, b), v in sorted(stats.items())))
        sys.stdout.flush()
    print(f"\n===== LIVE: sets under $ after fees on the books: {len(seen_gaps)}")
    for row in sorted(seen_gaps.values(), key=lambda r: -r["edge_book"])[:30]:
        print("   GAP " + json.dumps(row))
    print("closest per pair and family (edge after fees at top of book; above 0 = a gap):")
    for k, v in sorted(closest.items(), key=lambda kv: -kv[1][0])[:60]:
        print(f"   NEAR {k[0]} {k[1]}/{k[2]}: {json.dumps(v)}")
    print(f"live done in {time.time() - t0:.0f}s")


DOC_SERIES = ["KXBTC", "KXBTCD", "KXETH", "KXETHD", "KXSOLE", "KXSOLD", "KXXRP", "KXXRPD", "KXDOGE", "KXDOGED",
              "KXBNB", "KXBNBD", "KXHYPE", "KXHYPED", "KXSHIBA", "KXSHIBAD", "KXINX", "KXINXU", "KXNASDAQ100",
              "KXNASDAQ100U", "KXINXY", "KXINXDIRY", "KXNASDAQ100Y", "KXNDQDIRY", "KXWTI", "KXWTIW", "KXGOLDW",
              "KXGOLDMON", "KXUST10A", "KXUST10AM", "KXUST10AD"]
DOC_WORDS = re.compile(r"round|decimal|inclusive|exclusive|between|expiration value|average|no data|unavailable|"
                       r"not available|fair|last good|underlying|source agency|closing|end-of-day|end of day|"
                       r"settlement value|official|delay|revis|correct|outage|disrupt", re.I)


def docs():
    import io
    import requests
    c = Kalshi()
    urls = collections.defaultdict(list)
    for s in DOC_SERIES:
        try:
            info = c.series_info(s)
        except Exception as e:
            print(f"{s}: {e}")
            continue
        for k in ("contract_url", "contract_terms_url"):
            if info.get(k):
                urls[info[k]].append(f"{s}.{k}")
        print(f"{s}: {info.get('contract_url')} | terms {info.get('contract_terms_url')} | "
              f"sources {json.dumps(info.get('settlement_sources'))[:160]}")
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None
        print("pypdf missing")
    for url, who in urls.items():
        print(f"\n===== DOC {url} (for {', '.join(who)})")
        try:
            r = requests.get(url, timeout=30)
            print(f"   status {r.status_code}, {len(r.content)} bytes, type {r.headers.get('content-type')}")
            if r.status_code != 200 or PdfReader is None:
                continue
            text = " ".join(" ".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(r.content)).pages).split())
        except Exception as e:
            print(f"   failed: {e}")
            continue
        print(f"   text {len(text)} chars")
        shown, last = 0, -10 ** 9
        for m in DOC_WORDS.finditer(text):
            if m.start() - last < 500:
                continue
            last = m.start()
            print(f"   ...{text[max(0, m.start() - 260):m.start() + 340]}...")
            shown += 1
            if shown >= 28:
                print("   (more not shown)")
                break
    # Rules of the 4 PM index events and of XRP, in full.
    print("\n===== RULES IN FULL")
    for s in ("KXINXU", "KXNASDAQ100U", "KXINX", "KXXRP", "KXXRPD", "KXBTC", "KXDOGE", "KXWTI", "KXWTIW"):
        try:
            evs = c.events(series_ticker=s, status="open", with_nested_markets="true")
        except Exception as e:
            print(f"{s}: {e}")
            continue
        for ev in sorted(evs, key=lambda e: e.get("strike_date") or "")[-2:]:
            ms = ev.get("markets") or []
            if not ms:
                continue
            m = ms[len(ms) // 2]
            print(f"\n## {ev['event_ticker']} ({len(ms)} markets) {m['ticker']} {m.get('strike_type')} "
                  f"{m.get('floor_strike')} {m.get('cap_strike')} {m.get('yes_sub_title')!r}")
            print(f"   R1 {plain(m.get('rules_primary'))}")
            print(f"   R2 {plain(m.get('rules_secondary'))}")
            print(f"   early_close {plain(m.get('early_close_condition'))[:300]} | can_close_early {m.get('can_close_early')}")


FAMILIES = {
    "SPX": ["KXINX", "KXINXU", "KXINXY", "KXINXDIRY"],
    "NDX": ["KXNASDAQ100", "KXNASDAQ100U", "KXNASDAQ100Y", "KXNDQDIRY"],
    "WTI": ["KXWTI", "KXWTIW", "KXWTIH"],
    "Gold": ["KXGOLDD", "KXGOLDH", "KXGOLDW", "KXGOLDMON"],
    "Silver": ["KXSILVERD", "KXSILVERH", "KXSILVERW", "KXSILVERMON"],
    "Copper": ["KXCOPPERD", "KXCOPPERW", "KXCOPPERMON"],
    "NatGas": ["KXNATGASD", "KXNATGASW", "KXNATGASMON"],
    "Brent": ["KXBRENTD", "KXBRENTW", "KXBRENTMON"],
    "UST2": ["KXUST2A", "KXUST2AD", "KXUST2AM"], "UST5": ["KXUST5A", "KXUST5AD", "KXUST5AM"],
    "UST7": ["KXUST7A", "KXUST7AD", "KXUST7AM"], "UST10": ["KXUST10A", "KXUST10AD", "KXUST10AM"],
    "UST30": ["KXUST30A", "KXUST30AD", "KXUST30AM"],
    "EURUSD": ["KXEURUSD", "KXEURUSDAD", "KXEURUSDAW"], "USDJPY": ["KXUSDJPY", "KXUSDJPYAD", "KXUSDJPYAW"],
    "GBPUSD": ["KXGBPUSD", "KXGBPUSDAD", "KXGBPUSDAW"], "AUDUSD": ["KXAUDUSD", "KXAUDUSDAD", "KXAUDUSDAW"],
    "USDCAD": ["KXUSDCADAD", "KXUSDCADAW"], "USDCHF": ["KXUSDCHFAD", "KXUSDCHFAW"],
    "USDMXN": ["KXUSDMXNAD", "KXUSDMXNAW"], "USDINR": ["KXUSDINRAD", "KXUSDINRAW"],
    "USDNOK": ["KXUSDNOKAD", "KXUSDNOKAW"], "USDBRL": ["KXUSDBRLAD", "KXUSDBRLAW"],
    "NZDUSD": ["KXNZDUSD", "KXNZDUSDAD"],
}


def settled_listing(c, s, since, max_pages=15):
    """{event_ticker: (close ts, event)} of settled events of s that closed after `since`."""
    out, pages, order = {}, 0, []
    try:
        for page in c.event_pages(series_ticker=s, status="settled"):
            pages += 1
            for ev in page:
                t = iso(ev.get("strike_date"))
                order.append(t)
                if t and t >= since:
                    out[ev["event_ticker"]] = (t, ev)
            if pages >= max_pages:
                break
    except KalshiError as e:
        print(f"   {s}: listing failed {e}")
    first = [stamp(x) for x in order[:2]]
    return out, pages, first


def families(names, budget_s, since_days):
    cfg = config.load()
    c = Kalshi()
    t0, now = time.time(), time.time()
    tally, n, broken = Tally(cfg), collections.Counter(), []
    for fam in names:
        t_fam = time.time()
        members = FAMILIES[fam]
        listings = {}
        for s in members:
            lst, pages, first = settled_listing(c, s, now - since_days * DAY)
            listings[s] = lst
            print(f"{fam}: {s}: {len(lst)} settled events in range ({pages} pages; first listed {first})")
        by_close = collections.defaultdict(list)
        for s, lst in listings.items():
            for et, (t, ev) in lst.items():
                by_close[t].append((s, et))
        groups = sorted(((t, g) for t, g in by_close.items() if len({s for s, _ in g}) > 1), key=lambda x: -x[0])
        print(f"== {fam}: close times shared by 2+ series: {len(groups)}")
        shown = set()
        for t_close, g in groups:
            if time.time() - t_fam > budget_s:
                n[(fam, "close times left out (time)")] += 1
                continue
            mk = {}
            for s, et in g:
                mk[(s, et)] = event_markets(c, et)
            for (sa, ea), (sb, eb) in itertools.combinations(sorted(g), 2):
                ma, mb = mk[(sa, ea)], mk[(sb, eb)]
                if not ma or not mb or sa == sb:
                    continue
                # R = the side with ranges, if any
                if not any(m.get("strike_type") == "between" for m in ma) and any(m.get("strike_type") == "between" for m in mb):
                    (sa, ea, ma), (sb, eb, mb) = (sb, eb, mb), (sa, ea, ma)
                key = f"{fam} {sa}~{sb}"
                ta = collections.Counter(template(m.get("rules_primary")) for m in ma)
                tb = collections.Counter(template(m.get("rules_primary")) for m in mb)
                if set(ta) != set(tb):
                    n[(key, "event pairs whose rules read differently")] += 1
                    if key not in shown:
                        shown.add(key)
                        print(f"   RULES DIFFER {ea} vs {eb}:\n      A {plain(ma[0].get('rules_primary'))[:260]}\n"
                              f"      B {plain(mb[0].get('rules_primary'))[:260]}")
                    continue
                r2a = {plain(m.get("rules_secondary")) for m in ma}
                r2b = {plain(m.get("rules_secondary")) for m in mb}
                if r2a != r2b:
                    n[(key, "event pairs with different secondary rules (priced anyway)")] += 1
                    if key + "r2" not in shown:
                        shown.add(key + "r2")
                        print(f"   R2 DIFFER {ea} vs {eb}:\n      A {sorted(r2a)[0][:400]}\n      B {sorted(r2b)[0][:400]}")
                tick = tick_of(ma + mb)
                ivs = {}
                for m in ma + mb:
                    v, fl = ivs_of(m)
                    ivs[m["ticker"]] = v
                    for f in fl:
                        n[(key, f"markets: {f}")] += 1
                check_results(key, ma, mb, ivs, tick, n, broken)
                opened = max(min(filter(None, (iso(m.get("open_time")) for m in ma)), default=0),
                             min(filter(None, (iso(m.get("open_time")) for m in mb)), default=0))
                closes = {iso(m.get("close_time")) for m in ma + mb}
                if len(closes) != 1:
                    n[(key, "event pairs whose markets close at different times")] += 1
                    continue
                if not opened:
                    continue
                settle = max(filter(None, (iso(m.get("expected_expiration_time")) or t_close for m in ma + mb)))
                start = max(opened, t_close - 8 * DAY)
                ca = event_candles(c, sa, ea, start, t_close, 60)
                cb = event_candles(c, sb, eb, start, t_close, 60)
                if not ca or not cb:
                    n[(key, "event pairs without candles")] += 1
                    continue
                n[(key, "event pairs priced (hourly samples, last 8 days at most)")] += 1
                L = Linker([(m["ticker"], ivs[m["ticker"]]) for m in ma], [(m["ticker"], ivs[m["ticker"]]) for m in mb], tick)
                if key + "rel" not in shown:
                    shown.add(key + "rel")
                    print(f"   relations {ea}~{eb} (tick {tick:g}): "
                          f"{json.dumps({f'{a}/{b}': v for (a, b), v in L.n_relations().items()})}; other {len(L.other)}")
                times = list(range((int(start) // H + 1) * H, int(t_close), H))
                for t, q, vol in snapshots({**ca, **cb}, times, 2 * H, H):
                    for f_, mode, pay, legs in L.sets(q):
                        tally.add(key, f_, mode, pay, legs, t, settle, ea, vol, ivs, tick)
        keys = sorted({k for (k, *_) in tally.n if k.startswith(fam + " ")})
        print(f"   {fam}: {time.time() - t_fam:.0f}s; " + "; ".join(f"{k[0]} | {k[1]}: {v}" for k, v in sorted(n.items()) if k[0].startswith(fam)))
        tally.show(keys)
        sys.stdout.flush()
    print(f"\n===== BROKEN LINKS / BAD RESULTS: {len(broken)}")
    for b in broken[:40]:
        print("   BROKEN " + json.dumps(b))
    print(f"families done in {time.time() - t0:.0f}s")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "discover"
    if mode == "discover":
        discover()
    elif mode == "docs":
        docs()
    elif mode == "history":
        names = sys.argv[2].split(",") if len(sys.argv) > 2 else [p[0] for p in CRYPTO]
        budget = float(sys.argv[3]) if len(sys.argv) > 3 else 150
        days = float(sys.argv[4]) if len(sys.argv) > 4 else 60
        history([p for p in CRYPTO + OTHER if p[0] in names], budget, days)
    elif mode == "families":
        names = sys.argv[2].split(",") if len(sys.argv) > 2 else list(FAMILIES)
        budget = float(sys.argv[3]) if len(sys.argv) > 3 else 60
        days = float(sys.argv[4]) if len(sys.argv) > 4 else 60
        families(names, budget, days)
    elif mode == "live":
        rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        gap = float(sys.argv[3]) if len(sys.argv) > 3 else 60
        live(CRYPTO + OTHER, rounds, gap)


if __name__ == "__main__":
    main()
