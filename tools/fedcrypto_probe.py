"""Idea #6, Fed and crypto links, on real data. Probe branch only, never merged.
Public data only (no key, no orders).

Crypto: one-touch markets ("ever above X", "ever below X") nest. If market A's window sits
inside market B's window and A's line is at least as hard to reach, a YES on A means a YES on
B. YES on B plus NO on A then pays $1 by the end of A's window: A misses and its NO pays, or A
hits and B hits in the same minute. (B's leftover value after A misses is not counted.)

Fed: the rate after a meeting (KXFED, "above K") and the decision at that meeting
(KXFEDDECISION) must agree, given the rate before it (U, from the last meeting's KXFED
results). A move between meetings breaks this.

Prints: rules wording per series, settled results that break a link, gaps now, and hourly
gaps in the 45 days before payout, counted the bot's way (pays within max_arb_days, at least
min_arb_apr a year, at least 1% return). Fees are per contract on a 30-contract order.
"""
import collections
import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal import config  # noqa: E402
from kal.client import Kalshi, fee, price, ts  # noqa: E402
from kal.research.backtest import candle_close  # noqa: E402
from kal.risk import yearly_return  # noqa: E402

ET = ZoneInfo("America/New_York")
H, DAY = 3600, 86400
N = 30                      # contracts per order, for fees
CHUNK = 20 * DAY            # candle cache chunk
STOP_AFTER = 40 * 60        # stop fetching candles after this many seconds
ONE_TOUCH = re.compile(r"^KX(BTC|ETH|SOL|XRP|DOGE|BITCOIN)(MAX|MIN)")
MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
DATE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (\d{1,2}),? (\d{4})"
                  r"|\b(\d{1,2})/(\d{1,2})/(\d{4})")
TIME = re.compile(r"(\d{1,2}):(\d{2}) ?([AaPp])\.?[Mm]")
FED_LO = {"C26": None, "C25": -0.25, "H0": 0.0, "H25": 0.25, "H26": 0.5}    # rate change range
FED_HI = {"C26": -0.5, "C25": -0.25, "H0": 0.0, "H25": 0.25, "H26": None}  # None = open-ended


def fee_pc(p):
    return fee(N, p) / N


def iso_ts(v):
    try:
        return ts(v)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def stamp(t):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)) if t else None


def _clock(text, s, e):
    """(hour, minute) of the time next to a date at text[s:e], or None."""
    after = TIME.search(text[e:e + 20])
    before = list(TIME.finditer(text[max(0, s - 25):s]))
    m = after or (before[-1] if before else None)
    if not m:
        return None
    hh, mm, ap = int(m.group(1)) % 12, int(m.group(2)), m.group(3).lower()
    return hh + (12 if ap == "p" else 0), mm


def window(m):
    """(start, end) epoch seconds of a one-touch market, from its rules; None if unreadable."""
    text = m.get("rules_primary") or ""
    start, end = None, None
    for d in DATE.finditer(text):
        if d.group(1):
            mon, day, yr = MONTHS[d.group(1).lower()[:3]], int(d.group(2)), int(d.group(3))
        else:
            mon, day, yr = int(d.group(4)), int(d.group(5)), int(d.group(6))
        is_start = re.search(r"(starting|beginning|from|after)\s*$", text[max(0, d.start() - 14):d.start()])
        clock = _clock(text, d.start(), d.end())
        hh, mm = clock if clock else ((0, 0) if is_start else (23, 59))
        try:
            t = datetime(yr, mon, day, hh, mm, 59 if (hh, mm) == (23, 59) else 0, tzinfo=ET).timestamp()
        except ValueError:
            continue
        if is_start:
            start = t
        else:
            end = t
    opened = iso_ts(m.get("open_time"))
    if opened:
        start = max(start or 0, opened)
    return start, end


def wording(m):
    t = " ".join(str(m.get("rules_secondary") or "").lower().split())
    if "cumulative dataset" in t:
        return "per-minute trimmed mean ('cumulative dataset')"
    if "from market issuance until" in t:
        return "trimmed mean ('from market issuance until')"
    if "sixty second period" in t:
        return "60-second trimmed mean ('any sixty second period')"
    return "other: " + t[:70]


class Candles:
    def __init__(self, c):
        self.c, self.cache, self.calls, self.errors, self.t0 = c, {}, 0, 0, time.time()

    def out_of_time(self):
        return time.time() - self.t0 > STOP_AFTER

    def get(self, series, ticker, t0, t1):
        """{end_period_ts: candle} for hours in [t0, t1], fetched in cached chunks."""
        out = {}
        for k in range(int(t0 // CHUNK), int(t1 // CHUNK) + 1):
            key = (ticker, k)
            if key not in self.cache:
                if self.out_of_time():
                    return None
                try:
                    self.calls += 1
                    rows = self.c.candles(series, ticker, k * CHUNK, (k + 1) * CHUNK) or []
                    self.cache[key] = {r["end_period_ts"]: r for r in rows}
                except Exception:
                    self.errors += 1
                    self.cache[key] = {}
            out.update({t: r for t, r in self.cache[key].items() if t0 <= t <= t1})
        return out


class Tally:
    def __init__(self, cfg, now):
        self.cfg, self.now = cfg, now
        self.n = collections.Counter()
        self.near, self.gaps, self.passing = {}, [], collections.Counter()

    def hour(self, cat, name, t, cost, raw, pay, extra):
        """One hour of one pair or basket. raw = cost before fees."""
        self.n[f"{cat}: pair-hours"] += 1
        if raw < 1:
            self.n[f"{cat}: hours under $1 before fees"] += 1
        if name not in self.near or 1 - cost > self.near[name][0]:
            self.near[name] = (round(1 - cost, 4), cat, name, stamp(t), extra)
        if cost >= 1:
            return
        self.n[f"{cat}: gap hours after fees"] += 1
        days = max((pay - t) / DAY, 1 / 24)
        apr = yearly_return(1 / cost, days)
        good = (days <= self.cfg["max_arb_days"] and apr >= self.cfg["min_arb_apr"]
                and (1 - cost) / cost >= 0.01)
        if good:
            self.n[f"{cat}: passing hours"] += 1
            self.passing[(cat, name)] += 1
        self.gaps.append((round(1 - cost, 4), cat, name, stamp(t), round(days, 1), round(apr, 2), good, extra))

    def show(self, title):
        print(f"\n===== {title}")
        for k in sorted(self.n):
            print(f"  {k}: {self.n[k]}")
        def best(rows):
            """The best hour of each pair, best pairs first."""
            seen, out = set(), []
            for x in sorted(rows, key=lambda x: -x[0]):
                if x[2] not in seen:
                    seen.add(x[2])
                    out.append(x)
            return out
        print("  closest pairs (best hour each; edge after fees, above 0 = a gap):")
        for x in best(self.near.values())[:12]:
            print("    NEAR " + json.dumps(x))
        print(f"  gap hours after fees: {len(self.gaps)} in {len(best(self.gaps))} pairs; "
              f"passing: {sum(self.passing.values())} hours in {len(self.passing)} pairs")
        for k, v in self.passing.most_common(25):
            print(f"    PASS {v:>4}h {k}")
        for g in best(self.gaps)[:25]:
            print("    GAP " + json.dumps(g))


# ---------------- crypto ----------------

def crypto(c, cfg, now, cand):
    series = sorted(s["ticker"] for s in c.series() if ONE_TOUCH.match(s.get("ticker", "")))
    print(f"one-touch series: {len(series)}: {' '.join(series)}")
    markets = {}
    for s in series:
        try:
            for m in c.settled_markets(s, limit=3000):
                markets[m["ticker"]] = dict(m, _series=s)
            for ev in c.events(series_ticker=s, status="open", with_nested_markets="true"):
                for m in ev.get("markets") or []:
                    markets[m["ticker"]] = dict(m, _series=s)
        except Exception as e:
            print(f"{s}: failed: {e}")
    rows, words, unread = [], collections.defaultdict(collections.Counter), collections.Counter()
    examples = {}
    for m in markets.values():
        st = m.get("strike_type")
        k = m.get("floor_strike") if st == "greater" else m.get("cap_strike") if st == "less" else None
        if k is None:
            continue
        start, end = window(m)
        if not start or not end or end <= start:
            unread[m["_series"]] += 1
            continue
        w = wording(m)
        words[m["_series"]][w] += 1
        examples.setdefault(w, (m["ticker"], m.get("rules_primary"), m.get("rules_secondary")))
        asset = ONE_TOUCH.match(m["_series"]).group(1).replace("BITCOIN", "BTC")
        rows.append(dict(t=m["ticker"], s=m["_series"], ev=m.get("event_ticker"), asset=asset,
                         up=st == "greater", k=float(k), start=start, end=end, w=w,
                         status=m.get("status"), result=m.get("result"),
                         closed=iso_ts(m.get("close_time")) or end,
                         bid=price(m, "yes_bid"), ask=price(m, "yes_ask")))
    print(f"one-touch markets read: {len(rows)}; windows unreadable: {dict(unread)}")
    print("rules wording per series:")
    for s in sorted(words):
        print(f"  {s}: {dict(words[s])}")
    for w, (t, r1, r2) in examples.items():
        print(f"  EXAMPLE [{w}] {t}\n    R1 {' '.join(str(r1).split())[:400]}\n    R2 {' '.join(str(r2).split())[:600]}")

    # Pairs: A inside B, A at least as hard to reach. Keep the tightest B per (A, B's event).
    by = collections.defaultdict(list)
    for r in rows:
        by[(r["asset"], r["up"])].append(r)
    pairs = {}
    for group in by.values():
        for a in group:
            for b in group:
                if a is b or a["start"] < b["start"] or a["end"] > b["end"]:
                    continue
                if (a["k"] < b["k"]) if a["up"] else (a["k"] > b["k"]):
                    continue
                if a["ev"] == b["ev"] and a["k"] == b["k"]:
                    continue
                key = (a["t"], b["ev"])
                best = pairs.get(key)
                if best is None or ((b["k"] > best["k"]) if a["up"] else (b["k"] < best["k"])):
                    pairs[key] = b
    print(f"nested pairs: {len(pairs)}")
    idx = {r["t"]: r for r in rows}
    tally, broken, now_gaps = Tally(cfg, now), [], []
    stopped = 0
    for (ta, _), b in sorted(pairs.items(), key=lambda kv: -idx[kv[0][0]]["end"]):
        a = idx[ta]
        cat = "same event" if a["ev"] == b["ev"] else "cross event"
        flags = []
        if a["ev"] != b["ev"] and (b["end"] - a["end"] < 300 or a["start"] - b["start"] < 300):
            flags.append("windows touch")
        if a["w"] != b["w"]:
            flags.append("worded differently")
        if a["s"] != b["s"]:
            flags.append("other series")
        name = f"{a['t']} => {b['t']}" + (f" [{', '.join(flags)}]" if flags else "")
        tally.n[f"{cat}: pairs"] += 1
        if a["result"] == "yes" and b["result"] == "no":
            broken.append(name)
        if a["status"] == "active" and b["status"] == "active" and 0 < a["bid"] < 1 and 0 < b["ask"] < 1:
            raw = b["ask"] + (1 - a["bid"])
            cost = raw + fee_pc(b["ask"]) + fee_pc(round(1 - a["bid"], 4))
            now_gaps.append((round(1 - cost, 4), cat, name, a["bid"], b["ask"], stamp(a["end"])))
        t0 = max(a["start"], b["start"], a["end"] - 45 * DAY)
        t1 = min(a["closed"], b["closed"], now)
        if t1 <= t0:
            continue
        ca = cand.get(a["s"], a["t"], t0, t1)
        cb = cand.get(b["s"], b["t"], t0, t1) if ca is not None else None
        if ca is None or cb is None:
            stopped += 1
            continue
        tally.n[f"{cat}: pairs with prices"] += 1
        for t in sorted(set(ca) & set(cb)):
            bid_a, ask_b = candle_close(ca[t], "yes_bid"), candle_close(cb[t], "yes_ask")
            if not (0 < bid_a < 1 and 0 < ask_b < 1):
                continue
            raw = ask_b + (1 - bid_a)
            cost = raw + fee_pc(ask_b) + fee_pc(round(1 - bid_a, 4))
            tally.hour(cat, name, t, cost, raw, a["end"], {"A_yes_bid": bid_a, "B_yes_ask": ask_b})
    if stopped:
        print(f"(time limit: {stopped} pairs not priced)")
    tally.show("CRYPTO one-touch nests")
    print(f"  settled results that break a link (A yes, B no): {len(broken)}")
    for x in broken[:30]:
        print("    BROKEN " + x)
    now_gaps.sort(key=lambda x: -x[0])
    print(f"  open pairs priced now: {len(now_gaps)}; under $1 after fees: {sum(1 for g in now_gaps if g[0] > 0)}")
    for g in now_gaps[:15]:
        print("    NOW " + json.dumps(g))


# ---------------- Fed ----------------

def fed(c, cfg, now, cand):
    def grouped(s):
        evs = collections.defaultdict(dict)
        for m in c.settled_markets(s, limit=5000):
            evs[m.get("event_ticker")][m["ticker"]] = dict(m, _series=s)
        for ev in c.events(series_ticker=s, status="open", with_nested_markets="true"):
            for m in ev.get("markets") or []:
                evs[ev["event_ticker"]][m["ticker"]] = dict(m, _series=s)
        return evs

    rate_evs, dec_evs = grouped("KXFED"), grouped("KXFEDDECISION")
    print(f"\nKXFED events: {len(rate_evs)}; KXFEDDECISION events: {len(dec_evs)}")

    def when(ms):
        return max(filter(None, (iso_ts(m.get("close_time")) for m in ms)), default=None)

    def rate_after(ms):
        """The upper bound after the meeting, from settled 'above K' results; None if unclear."""
        res = sorted((float(m["floor_strike"]), m.get("result")) for m in ms
                     if m.get("floor_strike") is not None and m.get("strike_type") == "greater")
        if not res or any(r not in ("yes", "no") for _, r in res):
            return None
        no = [k for k, r in res if r == "no"]
        yes = [k for k, r in res if r == "yes"]
        if not no or not yes or max(yes) >= min(no) or round(min(no) - max(yes), 4) != 0.25:
            return None
        return min(no)

    order = sorted((when(ms.values()), ev) for ev, ms in rate_evs.items() if when(ms.values()))
    tally, broken, now_gaps, checked = Tally(cfg, now), [], [], 0
    last_rate, last_t, pending = None, None, False
    skipped_codes = collections.Counter()
    for t_meet, ev in order:
        ms = list(rate_evs[ev].values())
        settled = all(m.get("result") in ("yes", "no") for m in ms)
        # The rate before = the rate after the last settled meeting, unless a meeting between
        # them hasn't happened yet (then it's unknown) or there's a long hole in the data.
        U = last_rate if (not pending and last_t and t_meet - last_t < 75 * DAY) else None
        if settled:
            last_rate, last_t = rate_after(ms), t_meet
        else:
            pending = True
        suffix = ev.split("-", 1)[1]
        dec = dec_evs.get(f"KXFEDDECISION-{suffix}")
        if U is None or not dec:
            tally.n["meetings skipped (rate before unknown or no decision event)"] += 1
            continue
        codes = {m["ticker"].rsplit("-", 1)[1]: m for m in dec.values()}
        if set(codes) != set(FED_LO):
            tally.n["meetings skipped (decision outcomes differ)"] += 1
            skipped_codes[" ".join(sorted(codes))] += 1
            continue
        strikes = {round(float(m["floor_strike"]), 4): m for m in ms
                   if m.get("floor_strike") is not None and m.get("strike_type") == "greater"}
        if not strikes:
            tally.n["meetings skipped (no 'above' strikes: old range markets)"] += 1
            continue
        tally.n["meetings checked"] += 1
        print(f"  meeting {ev}: rate before {U}, after {rate_after(ms) if settled else 'open'}, "
              f"decision {[k for k, m in codes.items() if m.get('result') == 'yes'] or 'open'}")
        if settled:
            checked += 1
            yes = [k for k, m in codes.items() if m.get("result") == "yes"]
            r = rate_after(ms)
            if len(yes) != 1 or r is None:
                broken.append(f"{ev}: unclear results (decision yes={yes}, rate={r})")
            else:
                d, ch = yes[0], round(r - U, 4)
                lo, hi = FED_LO[d], FED_HI[d]
                if (lo is not None and ch < lo) or (hi is not None and ch > hi):
                    broken.append(f"{ev}: rate before {U}, decision {d}, rate after {r}")
        # Relations at each strike near U.
        legs = []
        for K in (U - 0.5, U - 0.25, U, U + 0.25):
            f = strikes.get(round(K, 4))
            if not f:
                continue
            s_yes = [d for d in FED_LO if FED_LO[d] is not None and U + FED_LO[d] > K]
            s_no = [d for d in FED_LO if FED_HI[d] is not None and U + FED_HI[d] <= K]
            for d in s_yes:
                legs.append(("pair", f"YES {f['ticker']} + NO {d}", [("yes", f), ("no", codes[d])]))
            for d in s_no:
                legs.append(("pair", f"NO {f['ticker']} + NO {d}", [("no", f), ("no", codes[d])]))
            if len(s_yes) + len(s_no) == len(FED_LO):
                legs.append(("basket", f"YES {f['ticker']} + YES {'+'.join(s_no)}",
                             [("yes", f)] + [("yes", codes[d]) for d in s_no]))
                legs.append(("basket", f"NO {f['ticker']} + YES {'+'.join(s_yes)}",
                             [("no", f)] + [("yes", codes[d]) for d in s_yes]))
        pay = max(filter(None, (iso_ts(m.get("expected_expiration_time")) or iso_ts(m.get("close_time"))
                                for m in ms)), default=t_meet)
        t0, t1 = t_meet - 45 * DAY, min(t_meet, now)
        cache = {}
        for cat, name, ls in legs:
            tally.n[f"{cat}: count"] += 1
            if all(m.get("status") == "active" for _, m in ls):
                raw = sum(price(m, "yes_ask") if side == "yes" else 1 - price(m, "yes_bid") for side, m in ls)
                ps = [price(m, "yes_ask") if side == "yes" else round(1 - price(m, "yes_bid"), 4) for side, m in ls]
                if all(0 < p < 1 for p in ps):
                    now_gaps.append((round(1 - raw - sum(fee_pc(p) for p in ps), 4), cat, name, ps, stamp(pay)))
            if t1 <= t0:
                continue
            cs = []
            for side, m in ls:
                if m["ticker"] not in cache:
                    cache[m["ticker"]] = cand.get(m["_series"], m["ticker"], t0, t1)
                cs.append((side, cache[m["ticker"]]))
            if any(x is None for _, x in cs):
                continue
            for t in sorted(set.intersection(*(set(x) for _, x in cs))):
                ps = []
                for side, x in cs:
                    p = candle_close(x[t], "yes_ask") if side == "yes" else round(1 - candle_close(x[t], "yes_bid"), 4)
                    ps.append(p)
                if not all(0 < p < 1 for p in ps):
                    continue
                raw = sum(ps)
                vol = [float(x[t].get("volume_fp") or x[t].get("volume") or 0) for _, x in cs]
                tally.hour(cat, name, t, raw + sum(fee_pc(p) for p in ps), raw, pay, {"legs": ps, "volume": vol})
    tally.show("FED rate level vs decision")
    print(f"  decision outcome sets skipped: {dict(skipped_codes)}")
    print(f"  settled meetings checked: {checked}; results that break the link: {len(broken)}")
    for x in broken:
        print("    BROKEN " + x)
    now_gaps.sort(key=lambda x: -x[0])
    print(f"  open pairs/baskets priced now: {len(now_gaps)}")
    for g in now_gaps[:12]:
        print("    NOW " + json.dumps(g))


def main():
    cfg = config.load()
    c = Kalshi()
    now, t0 = time.time(), time.time()
    cand = Candles(c)
    for part in (fed,):
        try:
            part(c, cfg, now, cand)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"{part.__name__}: failed: {e}")
    print(f"\ncandle calls: {cand.calls}, errors: {cand.errors}; done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
