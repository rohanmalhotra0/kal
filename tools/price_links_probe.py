"""Idea #19: one price at one moment, asked two ways in two series (range vs above/below).
Probe branch only, never merged. Public data only (no key, no orders).

Phase "discover" (this file's first run): find series pairs whose events close at the same
moment and whose rules read the same once the numbers and the comparison words are taken
out. Print each pair's rules, settlement sources, strike grids and boundary wording, how
settled values look (rounded or not), and whether event-level candlesticks work.
"""
import collections
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


def main():
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


if __name__ == "__main__":
    main()
