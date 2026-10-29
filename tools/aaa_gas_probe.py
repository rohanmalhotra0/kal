"""Idea #20: AAA gas daily vs weekly vs monthly (KXAAAGASD, KXAAAGASW, KXAAAGASM).
Probe branch only, never merged. Public data only (no key, no orders).

Modes:
  rules    every series' terms, every distinct rules wording (open and settled), contract PDFs,
           strike fields vs rules text, and AAA's own page.
  history  every settled market: date named, strike (fields vs rules), result vs value; then every
           date two series share: same value? any set that lost?
  gaps     hourly candles of every shared date: sets under $1 after fees that pass the bot's rules.
  live     open events and order books now.
"""
import collections
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal.client import Kalshi, KalshiError, ts  # noqa: E402

SERIES = ["KXAAAGASD", "KXAAAGASW", "KXAAAGASM"]
H, DAY = 3600, 86400
MONTHS = ("jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|"
          "august|september|october|november|december")
DATE_RX = re.compile(r"\b(" + MONTHS + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)
MONEY_RX = re.compile(r"\$\s?(\d+(?:\.\d+)?)")


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
    t = plain(text)
    t = DATE_RX.sub("<DATE>", t)
    t = MONEY_RX.sub("<$>", t)
    t = re.sub(r"\b\d+(\.\d+)?\b", "#", t)
    return t


def settled_markets(c, s):
    """Every settled market of a series: the live list plus Kalshi's historical store."""
    found = {}
    try:
        for m in c.markets(series_ticker=s, status="settled"):
            found[m["ticker"]] = dict(m, _src="live")
    except KalshiError as e:
        print(f"   {s}: settled list failed {e}")
    try:
        for m in c.paginate("/historical/markets", "markets", {"limit": 1000, "series_ticker": s}):
            found.setdefault(m["ticker"], dict(m, _src="historical"))
    except KalshiError as e:
        print(f"   {s}: historical list failed {e}")
    return list(found.values())


def pdf_text(url):
    import requests
    from pypdf import PdfReader
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        return f"(status {r.status_code})"
    if "pdf" not in (r.headers.get("content-type") or "") and not r.content[:5] == b"%PDF-":
        return f"(not a pdf: {r.headers.get('content-type')}) " + plain(r.text)[:3000]
    return " ".join(" ".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(r.content)).pages).split())


def rules():
    c = Kalshi()
    t0 = time.time()
    try:
        print("historical cutoff: " + json.dumps(c.get("/historical/cutoff")))
    except Exception as e:
        print(f"historical cutoff: {e}")

    # Every series that looks like gas prices.
    allser = c.series()
    gas = [s for s in allser if re.search(r"gas|aaa", f"{s.get('ticker')} {s.get('title')}", re.I)]
    print(f"\n===== SERIES matching gas/AAA: {len(gas)} of {len(allser)}")
    for s in sorted(gas, key=lambda s: s.get("ticker")):
        print(f"  {s.get('ticker')} | {s.get('frequency')} | {s.get('category')} | {s.get('title')}")

    docs = collections.defaultdict(list)
    for s in SERIES:
        print(f"\n\n######## {s}")
        try:
            info = c.series_info(s)
        except Exception as e:
            print(f"series info failed: {e}")
            info = {}
        for k, v in sorted(info.items()):
            v = json.dumps(v) if not isinstance(v, str) else v
            print(f"  series.{k}: {v[:3000]}")
        for k in ("contract_url", "contract_terms_url"):
            if info.get(k):
                docs[info[k]].append(f"{s}.{k}")

        # Open events.
        try:
            evs = c.events(series_ticker=s, status="open", with_nested_markets="true")
        except Exception as e:
            print(f"open events failed: {e}")
            evs = []
        print(f"\n== {s}: open events {len(evs)}")
        full_shown = False
        r1, r2, ec = collections.Counter(), collections.Counter(), collections.Counter()
        r1x, r2x, ecx = {}, {}, {}
        for ev in sorted(evs, key=lambda e: e.get("strike_date") or ""):
            ms = ev.get("markets") or []
            meta = {k: v for k, v in ev.items() if k != "markets"}
            print(f"\n  EVENT {json.dumps(meta)[:1500]}")
            closes = collections.Counter(m.get("close_time") for m in ms)
            exps = collections.Counter(m.get("expected_expiration_time") for m in ms)
            lates = collections.Counter(m.get("latest_expiration_time") for m in ms)
            opens = collections.Counter(m.get("open_time") for m in ms)
            print(f"    markets {len(ms)}; close {dict(closes)}; expected_exp {dict(exps)}; latest_exp {dict(lates)}; "
                  f"open {dict(opens)}")
            for m in sorted(ms, key=lambda m: float(m.get("floor_strike") or m.get("cap_strike") or 0)):
                money = MONEY_RX.findall(plain(m.get("rules_primary")))
                dates = DATE_RX.findall(plain(m.get("rules_primary")))
                print(f"    {m['ticker']} st={m.get('strike_type')} floor={m.get('floor_strike')!r} cap={m.get('cap_strike')!r} "
                      f"custom={json.dumps(m.get('custom_strike'))} func={m.get('functional_strike')!r} "
                      f"yes_sub={m.get('yes_sub_title')!r} no_sub={m.get('no_sub_title')!r} status={m.get('status')} "
                      f"rules$={money} rules_dates={dates} vol={m.get('volume_fp', m.get('volume'))} "
                      f"bid={m.get('yes_bid_dollars')} ask={m.get('yes_ask_dollars')}")
            for m in ms:
                for cnt, ex, field in ((r1, r1x, "rules_primary"), (r2, r2x, "rules_secondary"),
                                       (ec, ecx, "early_close_condition")):
                    tp = template(m.get(field))
                    cnt[tp] += 1
                    ex.setdefault(tp, plain(m.get(field)))
            if ms and not full_shown:
                full_shown = True
                m = ms[len(ms) // 2]
                print(f"\n    FULL MARKET {m['ticker']}:")
                for k, v in sorted(m.items()):
                    v = json.dumps(v) if not isinstance(v, str) else v
                    print(f"      {k}: {v[:4000]}")
        for name, cnt, ex in (("rules_primary", r1, r1x), ("rules_secondary", r2, r2x), ("early_close", ec, ecx)):
            print(f"\n  {s} open {name}: {len(cnt)} distinct wordings")
            for tp, n in cnt.most_common():
                print(f"    [{n}x] TEMPLATE {tp}")
                print(f"          EXAMPLE  {ex[tp]}")

        # Settled markets: every wording ever used, with dates.
        sm = settled_markets(c, s)
        print(f"\n== {s}: settled markets {len(sm)} (live {sum(1 for m in sm if m['_src'] == 'live')}, "
              f"historical {sum(1 for m in sm if m['_src'] == 'historical')}); events "
              f"{len({m.get('event_ticker') for m in sm})}")
        if sm:
            cl = sorted(filter(None, (iso(m.get("close_time")) for m in sm)))
            print(f"   close times {stamp(cl[0])} .. {stamp(cl[-1])}")
        for field in ("rules_primary", "rules_secondary", "early_close_condition"):
            groups = collections.defaultdict(list)
            for m in sm:
                groups[template(m.get(field))].append(m)
            print(f"\n  {s} settled {field}: {len(groups)} distinct wordings")
            for tp, rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
                cts = sorted(filter(None, (iso(m.get("close_time")) for m in rows)))
                print(f"    [{len(rows)}x, {stamp(cts[0]) if cts else None} .. {stamp(cts[-1]) if cts else None}] "
                      f"TEMPLATE {tp}")
                print(f"          EXAMPLE  {plain(rows[-1].get(field))}")
        fields = collections.Counter()
        for m in sm:
            for k, v in m.items():
                if v not in (None, "", [], {}):
                    fields[k] += 1
        print(f"\n  {s} settled fields present (count): {json.dumps(dict(sorted(fields.items())))}")
        res = collections.Counter(m.get("result") for m in sm)
        sts = collections.Counter(m.get("strike_type") for m in sm)
        dec = collections.Counter()
        vals = collections.Counter()
        for m in sm:
            v = m.get("expiration_value")
            if v not in (None, ""):
                vals[v] += 1
                sv = str(v)
                dec[len(sv.split(".", 1)[1]) if "." in sv else 0] += 1
        fdec = collections.Counter()
        for m in sm:
            f = m.get("floor_strike")
            if f is not None:
                sf = repr(f)
                fdec[len(sf.split(".", 1)[1].rstrip("0")) if "." in sf else 0] += 1
        print(f"  {s} results {dict(res)}; strike types {dict(sts)}; expiration_value decimals {dict(dec)}; "
              f"floor_strike decimals {dict(fdec)}; e.g. values {list(vals)[:10]}")
        if sm:
            m = max(sm, key=lambda m: iso(m.get("close_time")) or 0)
            print(f"\n    FULL SETTLED MARKET {m['ticker']}:")
            for k, v in sorted(m.items()):
                v = json.dumps(v) if not isinstance(v, str) else v
                print(f"      {k}: {v[:4000]}")
            m = min(sm, key=lambda m: iso(m.get("close_time")) or 1e18)
            print(f"\n    OLDEST SETTLED MARKET {m['ticker']}: close {m.get('close_time')} rules {plain(m.get('rules_primary'))}"
                  f" | R2 {plain(m.get('rules_secondary'))}")

    # Contract documents in full.
    for url, who in docs.items():
        print(f"\n\n===== DOC {url} (for {', '.join(who)})")
        try:
            text = pdf_text(url)
        except Exception as e:
            print(f"   failed: {e}")
            continue
        print(f"   {len(text)} chars")
        for i in range(0, min(len(text), 16000), 1000):
            print(f"   {text[i:i + 1000]}")

    # AAA's own page, if the runner can read it.
    import requests
    for url in ("https://gasprices.aaa.com/", "https://gasprices.aaa.com/state-gas-price-averages/"):
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            body = plain(re.sub(r"<[^>]+>", " ", r.text))
            print(f"\n===== AAA {url}: status {r.status_code}, {len(r.text)} bytes")
            for rx in (r"National Average[^$]{0,80}\$\s?\d\.\d+", r"Price as of[^A-Za-z]{0,30}",
                       r"(Current|Yesterday|Week Ago|Month Ago|Year Ago) Avg\.?[^A-Za-z]{0,80}"):
                for mm in list(re.finditer(rx, body))[:8]:
                    print(f"   ...{body[max(0, mm.start() - 120):mm.end() + 200]}...")
        except Exception as e:
            print(f"AAA {url}: {e}")
    print(f"\nrules done in {time.time() - t0:.0f}s")


# ======================= history, gaps, live =======================

from decimal import Decimal, InvalidOperation  # noqa: E402

from kal import config  # noqa: E402
from kal.client import candle_close, fee, price  # noqa: E402
from kal.fees import _bad as fee_problem  # noqa: E402
from kal.risk import MAX_ARB_ROI, yearly_return  # noqa: E402

N = 30   # contracts per order, for the fee's cent rounding (max_per_arb is $30)
FAMILY = {"KXAAAGASD": "D", "AAAGASD": "D", "KXAAAGASW": "W", "AAAGASW": "W", "KXAAAGASM": "M", "AAAGASM": "M",
          "KXAAAGASY": "Y", "AAAGASY": "Y", "KXAAAGASED": "ED"}
LISTED = ["KXAAAGASD", "KXAAAGASW", "KXAAAGASM", "KXAAAGASY", "KXAAAGASED", "AAAGASD", "AAAGASW", "AAAGASM", "AAAGASY"]
MON = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
CMP_RX = re.compile(r"\bare (strictly greater than|greater than or equal to|greater than|at least|above|"
                    r"less than or equal to|less than|below|between)\s+\$\s?(\d+(?:\.\d+)?)", re.I)


def dec(v):
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return Decimal(str(v).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None


def fee_pc(p):
    return fee(N, p) / N


def series_of(tk):
    return str(tk or "").split("-")[0]


def rules_date(text):
    g = DATE_RX.search(plain(text))
    if not g:
        return None
    mon = MON.get(g.group(1).lower()[:3])
    return f"{int(g.group(3)):04d}-{mon:02d}-{int(g.group(2)):02d}" if mon else None


def ticker_date(tk):
    g = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(?:-|$)", str(tk or ""))
    if not g or g.group(2).lower() not in MON:
        return None
    return f"20{g.group(1)}-{MON[g.group(2).lower()]:02d}-{g.group(3)}"


def rec(m):
    """One market, read: strike from the fields and from the rules, the date it names, its result."""
    r1 = plain(m.get("rules_primary"))
    g = CMP_RX.search(r1)
    st = m.get("strike_type")
    k_field = dec(m.get("floor_strike")) if st == "greater" else None
    k_rules = dec(g.group(2)) if g else None
    flags = []
    if not g:
        flags.append("rules unreadable")
    elif g.group(1).lower() != "strictly greater than":
        flags.append(f"rules say {g.group(1)!r}")
    if st != "greater":
        flags.append(f"strike_type {st!r}")
    if k_field is not None and k_rules is not None and k_field != k_rules:
        flags.append("strike fields and rules differ")
    d_r, d_t = rules_date(r1), ticker_date(m.get("ticker"))
    if d_r and d_t and d_r != d_t:
        flags.append("ticker date differs from rules date")
    return dict(tk=m["ticker"], et=m.get("event_ticker"), s=series_of(m["ticker"]),
                fam=FAMILY.get(series_of(m["ticker"]), "?"), date=d_r or d_t, k=k_rules if k_rules is not None else k_field,
                kf=k_field, flags=flags, res=m.get("result"), v=dec(m.get("expiration_value")),
                vs=str(m.get("expiration_value") or ""), close=iso(m.get("close_time")),
                exp=iso(m.get("expected_expiration_time")), late=iso(m.get("latest_expiration_time")),
                settled=iso(m.get("settlement_ts")), opened=iso(m.get("open_time")), status=m.get("status"),
                vol=float(m.get("volume_fp") or m.get("volume") or 0), src=m.get("_src"),
                bid=price(m, "yes_bid"), ask=price(m, "yes_ask"), r1=r1)


def load(c, statuses=("settled",)):
    """{(family, date): {event_ticker: [rec]}} over every listed series."""
    seen = {}
    for s in LISTED:
        rows = []
        if "settled" in statuses:
            rows += settled_markets(c, s)
        for st in statuses:
            if st == "settled":
                continue
            try:
                rows += [dict(m, _src=st) for m in c.markets(series_ticker=s, status=st)]
            except KalshiError as e:
                print(f"   {s} {st}: {e}")
        for m in rows:
            seen.setdefault(m["ticker"], m)
        print(f"   {s}: {len(rows)} markets read")
    ev = collections.defaultdict(lambda: collections.defaultdict(list))
    for m in seen.values():
        r = rec(m)
        ev[(r["fam"], r["date"])][r["et"]].append(r)
    return ev


def feasible(rs):
    """(lo, hi): the results say lo < value <= hi."""
    lo = max((r["k"] for r in rs if r["res"] == "yes" and r["k"] is not None), default=None)
    hi = min((r["k"] for r in rs if r["res"] == "no" and r["k"] is not None), default=None)
    return lo, hi


def breaks(ra, rb):
    """Pairs (YES x in one series + NO y in the other, k_x <= k_y) that both lost."""
    out = []
    for xs, ys in ((ra, rb), (rb, ra)):
        for x in xs:
            for y in ys:
                if x["k"] is None or y["k"] is None or x["k"] > y["k"]:
                    continue
                if x["res"] == "no" and y["res"] == "yes":
                    out.append((x["tk"], y["tk"]))
    return out


def history():
    c = Kalshi()
    t0 = time.time()
    ev = load(c)
    n = collections.Counter()
    flags = collections.Counter()
    flag_ex = {}
    per_month = collections.defaultdict(collections.Counter)
    for (fam, date), ets in ev.items():
        if len(ets) > 1:
            n[f"{fam}: dates with 2+ events"] += 1
            print(f"   MANY EVENTS {fam} {date}: {sorted(ets)}")
        for et, rs in ets.items():
            n[f"{fam}: events"] += 1
            for r in rs:
                for f in r["flags"]:
                    flags[(fam, f)] += 1
                    flag_ex.setdefault((fam, f), (r["tk"], r["r1"][:200], str(r["kf"]), str(r["k"])))
                if r["vs"]:
                    per_month[(fam, (date or "?")[:7])][len(r["vs"].split(".", 1)[1]) if "." in r["vs"] else 0] += 1
            vals = {r["v"] for r in rs if r["v"] is not None}
            if len(vals) > 1:
                n[f"{fam}: events with 2+ values"] += 1
                print(f"   VALUES DIFFER INSIDE {et}: {sorted(map(str, vals))}")
            if not vals:
                n[f"{fam}: events with no value"] += 1
            res = collections.Counter(r["res"] for r in rs)
            if set(res) - {"yes", "no"}:
                n[f"{fam}: events with results other than yes/no"] += 1
                print(f"   ODD RESULTS {et}: {dict(res)}")
            lo, hi = feasible(rs)
            if lo is not None and hi is not None and lo >= hi:
                n[f"{fam}: events whose own ladder broke"] += 1
                print(f"   LADDER BROKE {et}: yes up to {lo}, no from {hi}")
            for r in rs:
                if r["v"] is None or r["k"] is None or r["res"] not in ("yes", "no"):
                    continue
                if (r["v"] > r["k"]) != (r["res"] == "yes"):
                    n[f"{fam}: markets whose result disagrees with value > strike"] += 1
                    print(f"   RESULT VS VALUE {r['tk']}: value {r['vs']} strike {r['k']} result {r['res']}")
                if r["v"] == r["k"]:
                    n[f"{fam}: markets settled exactly on the strike"] += 1
                    print(f"   TIE {r['tk']}: value {r['vs']} = strike, result {r['res']}")
    print("\n===== EVENTS AND MARKET CHECKS")
    for k, v in sorted(n.items()):
        print(f"   {k}: {v}")
    for (fam, f), v in sorted(flags.items()):
        print(f"   FLAG {fam} {f}: {v} e.g. {flag_ex[(fam, f)]}")
    print("\n===== expiration_value decimals by month (family month: {decimals: markets})")
    for (fam, mo), cnt in sorted(per_month.items()):
        print(f"   {fam} {mo}: {dict(sorted(cnt.items()))}")

    # Same date in two or more families.
    by_date = collections.defaultdict(dict)
    for (fam, date), ets in ev.items():
        if date:
            rs = [r for et in sorted(ets) for r in ets[et]]
            by_date[date][fam] = rs
    shared = sorted(d for d, f in by_date.items() if len(f) > 1)
    print(f"\n===== SAME-DATE PAIRS: {len(shared)} dates named by 2+ families")
    pn, pbreak, pdiff, pnum = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
    for d in shared:
        fams = sorted(by_date[d])
        for i in range(len(fams)):
            for j in range(i + 1, len(fams)):
                a, b = fams[i], fams[j]
                ra, rb = by_date[d][a], by_date[d][b]
                key = f"{a}-{b}"
                pn[key] += 1
                va = sorted({str(r["v"]) for r in ra if r["v"] is not None})
                vb = sorted({str(r["v"]) for r in rb if r["v"] is not None})
                na_ = {r["v"].normalize() for r in ra if r["v"] is not None}
                nb_ = {r["v"].normalize() for r in rb if r["v"] is not None}
                same = na_ == nb_ and len(na_) == 1
                if not same:
                    pnum[key] += 1
                br = breaks(ra, rb)
                if va != vb:
                    pdiff[key] += 1
                if br:
                    pbreak[key] += 1
                ca = sorted({stamp(r["close"]) for r in ra})
                cb = sorted({stamp(r["close"]) for r in rb})
                ea = sorted({stamp(r["exp"]) for r in ra})
                eb = sorted({stamp(r["exp"]) for r in rb})
                sa = [stamp(min(filter(None, (r["settled"] for r in ra)), default=None)),
                      stamp(max(filter(None, (r["settled"] for r in ra)), default=None))]
                sb = [stamp(min(filter(None, (r["settled"] for r in rb)), default=None)),
                      stamp(max(filter(None, (r["settled"] for r in rb)), default=None))]
                la = sorted({stamp(r["late"]) for r in ra})
                lb = sorted({stamp(r["late"]) for r in rb})
                print(f"   {d} {key}: values {va} vs {vb} {'SAME' if same else 'DIFFERENT'}; "
                      f"broken sets {len(br)}{' e.g. ' + str(br[:2]) if br else ''}; markets {len(ra)}/{len(rb)}; "
                      f"close {ca}/{cb}; expected {ea}/{eb}; latest {la}/{lb}; settled {sa}/{sb}; "
                      f"events {sorted({r['et'] for r in ra})}/{sorted({r['et'] for r in rb})}")
    print("\n===== SAME-DATE SUMMARY (pairs, values written differently, values that differ as numbers, broken sets)")
    for k in sorted(pn):
        print(f"   {k}: {pn[k]} pairs, {pdiff[k]} written differently, {pnum[k]} differ as numbers, "
              f"{pbreak[k]} with a broken set")
    # Each daily value next to the days around it, to see whether a differing value is another day's.
    dv = {d: sorted({r["v"].normalize() for r in by_date[d].get("D", []) if r["v"] is not None}) for d in by_date}
    print("\n===== DAILY VALUES around pairs whose values differ as numbers")
    for d in shared:
        fams = sorted(by_date[d])
        vals = {f: sorted({str(r["v"].normalize()) for r in by_date[d][f] if r["v"] is not None}) for f in fams}
        if len({tuple(v) for v in vals.values()}) > 1:
            from datetime import date as _date, timedelta as _td
            y, m_, dd = map(int, d.split("-"))
            around = []
            for k in range(-3, 4):
                x = (_date(y, m_, dd) + _td(days=k)).isoformat()
                around.append(f"{x}: {[str(v) for v in dv.get(x, [])]}")
            print(f"   {d} {json.dumps(vals)} | daily nearby: {'; '.join(around)}")
    print(f"history done in {time.time() - t0:.0f}s")


CUTOFF = ts("2026-08-30T00:00:00Z")   # Kalshi's historical cutoff (markets settled before it moved)


def hist_candles(c, tk, t0, t1):
    try:
        d = c.get(f"/historical/markets/{tk}/candlesticks",
                  {"start_ts": int(t0), "end_ts": int(t1), "period_interval": 60})
    except KalshiError:
        return []
    return d.get("candlesticks") or []


def candles_for(c, et, tickers, t0, t1, settled_ts=None):
    """{ticker: [(end_ts, bid, ask, volume)] sorted}. The event endpoint first, then each market
    (live store, then historical). Markets settled before the cutoff go straight to historical."""
    out = collections.defaultdict(dict)
    series = series_of(et)
    t_in = time.time()
    if settled_ts and settled_ts < CUTOFF:
        for tk in tickers:
            if time.time() - t_in > 90:
                print(f"      (candle time cap in {et})")
                break
            for r in hist_candles(c, tk, t0, t1):
                out[tk][r["end_period_ts"]] = r
        tickers = []
    start, calls = int(t0), 0
    while tickers and calls < 20:
        calls += 1
        try:
            d = c.get(f"/series/{series}/events/{et}/candlesticks",
                      {"start_ts": start, "end_ts": int(t1), "period_interval": 60})
        except KalshiError:
            break
        for tk, rows in zip(d.get("market_tickers") or [], d.get("market_candlesticks") or []):
            for r in rows or []:
                out[tk][r["end_period_ts"]] = r
        adj = d.get("adjusted_end_ts")
        if not adj or adj >= int(t1) or adj <= start:
            break
        start = int(adj)
    for tk in tickers:
        if out.get(tk):
            continue
        if time.time() - t_in > 90:
            print(f"      (candle time cap in {et})")
            break
        try:
            rows = c.candles(series_of(tk), tk, t0, t1, 60)
        except KalshiError:
            rows = []
        for r in rows or []:
            out[tk][r["end_period_ts"]] = r
    res = {}
    for tk, rows in out.items():
        lst = []
        for t, r in sorted(rows.items()):
            v = r.get("volume_fp", r.get("volume"))
            try:
                v = float(v or 0)
            except (TypeError, ValueError):
                v = 0.0
            lst.append((t, candle_close(r, "yes_bid"), candle_close(r, "yes_ask"), v))
        res[tk] = lst
    return res


def quote_at(rows, t, stale=2 * H):
    """(bid, ask, volume in the hour ending at t) from the last candle that ended by t."""
    best = None
    for row in rows:
        if row[0] <= t:
            best = row
        else:
            break
    if not best or t - best[0] > stale:
        return None
    vol = sum(r[3] for r in rows if t - H < r[0] <= t)
    return best[1], best[2], vol


def sets_at(ra, rb, q):
    """Every 2-leg set across the two series: YES on strike x + NO on strike y >= x. Pays $1 at least."""
    out = []
    for xs, ys in ((ra, rb), (rb, ra)):
        for x in xs:
            qx = q.get(x["tk"])
            if not qx or not 0 < qx[1] < 1 or x["k"] is None:
                continue
            for y in ys:
                qy = q.get(y["tk"])
                if not qy or y["k"] is None or x["k"] > y["k"] or not 0 < qy[0] < 1:
                    continue
                legs = [(x["tk"], "yes", qx[1], qx[2]), (y["tk"], "no", round(1 - qy[0], 4), qy[2])]
                out.append(("same strike" if x["k"] == y["k"] else "lower YES + higher NO", legs))
    return out


def judge(legs, t, settle, cfg):
    raw = sum(p for _, _, p, _ in legs)
    cost = raw + sum(fee_pc(p) for _, _, p, _ in legs)
    edge = 1 - cost
    days = max((settle - t) / DAY, 1 / 24)
    roi = edge / cost if cost > 0 else 0
    apr = yearly_return(1 / cost, days) if cost > 0 else -1
    ok = edge > 0 and days <= cfg["max_arb_days"] and apr >= cfg["min_arb_apr"] and 0.01 <= roi <= MAX_ARB_ROI
    return dict(raw=round(raw, 4), cost=round(cost, 4), edge=round(edge, 4), roi=round(roi, 4), days=round(days, 3),
                ok=ok, too_good=roi > MAX_ARB_ROI)


def trades_in(c, tk, t0, t1):
    """Public trades in one market between t0 and t1: live store, then historical."""
    out = []
    for path in ("/markets/trades", "/historical/trades"):
        try:
            rows = c.paginate(path, "trades", {"ticker": tk, "min_ts": int(t0), "max_ts": int(t1), "limit": 1000})
        except KalshiError:
            rows = []
        out += rows
        if out:
            break
    return out


def gaps(since_days, families):
    cfg = dict(config.DEFAULTS)
    c = Kalshi()
    t_start = time.time()
    for s in ("KXAAAGASD", "KXAAAGASW", "KXAAAGASM", "KXAAAGASY", "KXAAAGASED"):
        try:
            info = c.series_info(s)
            print(f"{s}: fee {info.get('fee_type')} x{info.get('fee_multiplier')} -> "
                  f"{fee_problem(info.get('fee_type'), info.get('fee_multiplier')) or 'the bot can price it'}")
        except Exception as e:
            print(f"{s}: series info failed: {e}")
    ev = load(c, ("settled", "closed", "open"))
    since = time.time() - since_days * DAY
    by_date = collections.defaultdict(dict)
    for (fam, date), ets in ev.items():
        if date and fam in families:
            by_date[date][fam] = [r for et in sorted(ets) for r in ets[et]]
    shared = sorted((d for d, f in by_date.items() if len(f) > 1), reverse=True)
    print(f"\n===== GAPS over {len(shared)} shared dates (families {families})")
    tally = collections.Counter()
    hours = collections.defaultdict(set)
    passing = collections.defaultdict(list)
    best_rows = []
    for d in shared:
        if time.time() - t_start > 16 * 60:
            tally[("all", "dates left out (time)")] += 1
            continue
        fams = sorted(by_date[d])
        for i in range(len(fams)):
            for j in range(i + 1, len(fams)):
                a, b = fams[i], fams[j]
                key = f"{a}-{b}"
                ra, rb = by_date[d][a], by_date[d][b]
                close = min(filter(None, (r["close"] for r in ra + rb)), default=None)
                if not close or close < since:
                    tally[(key, "pairs older than the window")] += 1
                    continue
                opened = max(min(filter(None, (r["opened"] for r in ra)), default=0),
                             min(filter(None, (r["opened"] for r in rb)), default=0))
                last_close = min(max(filter(None, (r["close"] for r in ra))), max(filter(None, (r["close"] for r in rb))))
                settle = max(filter(None, (r["exp"] or r["close"] for r in ra + rb)))
                end = min(last_close, int(time.time()))
                if not opened or end <= opened:
                    tally[(key, "pairs never open together")] += 1
                    continue
                t_pair = time.time()
                sa_ = max(filter(None, (r["settled"] for r in ra)), default=None)
                sb_ = max(filter(None, (r["settled"] for r in rb)), default=None)
                ca = candles_for(c, ra[0]["et"], [r["tk"] for r in ra], opened, end, sa_)
                cb = candles_for(c, rb[0]["et"], [r["tk"] for r in rb], opened, end, sb_)
                if not ca or not cb:
                    tally[(key, "pairs without candles")] += 1
                    continue
                tally[(key, "pairs priced")] += 1
                cand = {**ca, **cb}
                closes = {r["tk"]: r["close"] for r in ra + rb}
                run, runs = 0, []
                for t in range((int(opened) // H + 1) * H, int(end) + 1, H):
                    q = {}
                    for tk, rows in cand.items():
                        if closes.get(tk) and t > closes[tk]:
                            continue
                        x = quote_at(rows, t)
                        if x:
                            q[tk] = x
                    ss = sets_at(ra, rb, q)
                    if not ss:
                        continue
                    tally[(key, "pair-hours with prices")] += 1
                    best, best_ok, any_good = None, None, False
                    for kind, legs in ss:
                        j_ = judge(legs, t, settle, cfg)
                        if j_["raw"] < 1:
                            tally[(key, "sets under $1 before fees")] += 1
                        if j_["edge"] > 0:
                            tally[(key, "sets under $1 after fees")] += 1
                            if j_["too_good"]:
                                any_good = True
                        row = dict(j_, date=d, pair=key, kind=kind, t=stamp(t), legs=legs)
                        if best is None or j_["edge"] > best["edge"]:
                            best = row
                        if j_["ok"] and (best_ok is None or j_["edge"] > best_ok["edge"]):
                            best_ok = row
                    if best and best["edge"] > 0:
                        hours[(key, "under $1 after fees")].add((d, t))
                    if any_good:
                        hours[(key, "too good (>25%)")].add((d, t))
                    if best_ok:
                        hours[(key, "passing")].add((d, t))
                        passing[key].append(best_ok)
                        if all(l[3] > 0 for l in best_ok["legs"]):
                            hours[(key, "passing, both legs traded that hour")].add((d, t))
                        run += 1
                    elif run:
                        runs.append(run)
                        run = 0
                    if best:
                        best_rows.append(best)
                if run:
                    runs.append(run)
                for r_ in runs:
                    tally[(key, f"gap runs lasting {r_}h")] += 1
                ph = sorted(t for (dd, t) in hours[(key, "passing")] if dd == d)
                bp = max((r for r in passing[key] if r["date"] == d), key=lambda r: r["edge"], default=None)
                print(f"   {d} {key}: candles {len(ca)}/{len(cb)} markets, {stamp(opened)}..{stamp(end)}; passing hours "
                      f"{len(ph)} runs {runs}; best passing {json.dumps(bp) if bp else None} ({time.time() - t_pair:.0f}s)")
                sys.stdout.flush()
    print("\n===== GAP SUMMARY")
    for k, v in sorted(tally.items()):
        print(f"   {k[0]} | {k[1]}: {v}")
    for (key, what), hs in sorted(hours.items()):
        print(f"   {key} | hours {what}: {len(hs)} (dates {len({d for d, _ in hs})}: {sorted({d for d, _ in hs})[:30]})")
    for key, rows in sorted(passing.items()):
        rows.sort(key=lambda r: -r["edge"])
        print(f"\n   -- {key}: best passing sets (one per hour, best first)")
        for r in rows[:15]:
            print("      PASS " + json.dumps(r))
        vol = [r for r in rows if all(l[3] > 0 for l in r["legs"])]
        print(f"   -- {key}: best passing with trades on both legs that hour ({len(vol)})")
        for r in vol[:10]:
            print("      PASSVOL " + json.dumps(r))
    near = sorted(best_rows, key=lambda r: -r["edge"])[:10]
    print("\n   closest sets overall:")
    for r in near:
        print("      NEAR " + json.dumps(r))
    # Did trades really go through at gap prices? Check the top passing hours with volume.
    print("\n===== TRADES in the hour before the best passing snapshots")
    tops = sorted((r for rows in passing.values() for r in rows), key=lambda r: -r["edge"])
    checked, seen_dates = 0, set()
    for r in tops:
        if checked >= 14:
            break
        if (r["pair"], r["date"]) in seen_dates:
            continue
        seen_dates.add((r["pair"], r["date"]))
        checked += 1
        t1 = ts(r["t"].replace(" ", "T") + ":00Z")
        legs_out = []
        for tk, side, p, vol in r["legs"]:
            tr = trades_in(c, tk, t1 - H, t1)
            paid = []
            for x in tr:
                if x.get("taker_side") != side:
                    continue
                pp = price(x, f"{side}_price")
                paid.append((pp, float(x.get("count_fp") or x.get("count") or 0), x.get("created_time")))
            legs_out.append((tk, side, p, len(tr), min(paid)[:2] if paid else None, sum(q for _, q, _ in paid)))
        print(f"   {r['pair']} {r['date']} at {r['t']} edge {r['edge']}: " + json.dumps(legs_out))
    print(f"\ngaps done in {time.time() - t_start:.0f}s")


def book_quote(book, side):
    """(price, size) to buy `side` now from an order book of bids, or (None, 0)."""
    levels = book.get("no" if side == "yes" else "yes") or []
    if not levels:
        return None, 0
    bid, size = max(levels)
    ask = round(1 - bid, 4)
    return (ask, size) if 0 < ask < 1 else (None, 0)


def live(rounds, gap_s):
    cfg = dict(config.DEFAULTS)
    c = Kalshi()
    for rnd in range(rounds):
        if rnd:
            time.sleep(gap_s)
        now = time.time()
        ev = load(c, ("open", "unopened", "closed"))
        by_date = collections.defaultdict(dict)
        print(f"\n== round {rnd + 1} at {stamp(now)}")
        for (fam, date), ets in sorted(ev.items(), key=lambda kv: (kv[0][1] or "", kv[0][0])):
            rs = [r for et in sorted(ets) for r in ets[et]]
            st = collections.Counter(r["status"] for r in rs)
            print(f"   {fam} {date}: {sorted(ets)} markets {len(rs)} {dict(st)} open {stamp(min(filter(None, (r['opened'] for r in rs)), default=None))} "
                  f"close {sorted({stamp(r['close']) for r in rs})} expected {sorted({stamp(r['exp']) for r in rs})}")
            if date:
                by_date[date][fam] = [r for r in rs if r["status"] in ("active", "open")]
        shared = [d for d, f in by_date.items() if sum(1 for x in f.values() if x) > 1]
        print(f"   shared open dates: {shared}")
        for d in shared:
            fams = sorted(f for f, rs in by_date[d].items() if rs)
            for i in range(len(fams)):
                for j in range(i + 1, len(fams)):
                    ra, rb = by_date[d][fams[i]], by_date[d][fams[j]]
                    q, sizes = {}, {}
                    for r in ra + rb:
                        try:
                            bk = c.orderbook(r["tk"])
                        except KalshiError:
                            continue
                        ya, ys = book_quote(bk, "yes")
                        na, ns = book_quote(bk, "no")
                        q[r["tk"]] = (round(1 - na, 4) if na else 0.0, ya or 0.0, 0)
                        sizes[(r["tk"], "yes")], sizes[(r["tk"], "no")] = ys, ns
                    settle = max(filter(None, (r["exp"] or r["close"] for r in ra + rb)))
                    rows = []
                    for kind, legs in sets_at(ra, rb, q):
                        j_ = judge(legs, now, settle, cfg)
                        rows.append(dict(j_, kind=kind, legs=[(tk, sd, p, sizes.get((tk, sd))) for tk, sd, p, _ in legs]))
                    rows.sort(key=lambda r: -r["edge"])
                    print(f"   {d} {fams[i]}-{fams[j]}: {len(rows)} sets on the books; under $1 after fees "
                          f"{sum(1 for r in rows if r['edge'] > 0)}; passing {sum(1 for r in rows if r['ok'])}")
                    for r in rows[:6]:
                        print("      BOOK " + json.dumps(r))


def detail(items):
    """Every market of the named events, in full enough to see what happened."""
    c = Kalshi()
    for et in items:
        ms = []
        try:
            ms = c.markets(event_ticker=et)
        except KalshiError as e:
            print(f"{et}: {e}")
        if not ms:
            try:
                ms = c.paginate("/historical/markets", "markets", {"limit": 1000, "event_ticker": et})
            except KalshiError as e:
                print(f"{et}: historical {e}")
        print(f"\n===== {et}: {len(ms)} markets")
        try:
            e = c.get(f"/events/{et}")
            meta = {k: v for k, v in (e.get("event") or {}).items() if k != "markets"}
            print(f"   EVENT {json.dumps(meta)[:1500]}")
        except KalshiError as e:
            print(f"   event: {e}")
        shown = set()
        for m in sorted(ms, key=lambda m: float(m.get("floor_strike") or 0)):
            print(f"   {m['ticker']} status={m.get('status')} result={m.get('result')!r} value={m.get('expiration_value')!r} "
                  f"settle$={m.get('settlement_value_dollars')} settled={m.get('settlement_ts')} close={m.get('close_time')} "
                  f"exp={m.get('expected_expiration_time')} late={m.get('latest_expiration_time')} st={m.get('strike_type')} "
                  f"floor={m.get('floor_strike')} vol={m.get('volume_fp')} oi={m.get('open_interest_fp')} "
                  f"last={m.get('last_price_dollars')} sub={m.get('yes_sub_title')!r}")
            for k in ("rules_primary", "rules_secondary", "early_close_condition", "subtitle", "title"):
                t = template(m.get(k))
                if t and (k, t) not in shown:
                    shown.add((k, t))
                    print(f"      {k}: {plain(m.get(k))}")
        extra = sorted({k for m in ms for k, v in m.items() if v not in (None, "", [], {})} - set(MKT_SEEN))
        if extra:
            print(f"   other fields: {extra}")
            for k in extra:
                vals = collections.Counter(json.dumps(m.get(k))[:200] for m in ms)
                print(f"      {k}: {dict(vals.most_common(3))}")


MKT_SEEN = ("ticker", "status", "result", "expiration_value", "settlement_value_dollars", "settlement_ts", "close_time",
            "expected_expiration_time", "latest_expiration_time", "strike_type", "floor_strike", "volume_fp",
            "open_interest_fp", "last_price_dollars", "yes_sub_title", "rules_primary", "rules_secondary",
            "early_close_condition", "subtitle", "title", "event_ticker", "_src")


def values(series, since, until):
    """Every settled event's value (and settle time) between two dates, one line each."""
    c = Kalshi()
    for s in series:
        rows = collections.defaultdict(list)
        for m in settled_markets(c, s) + [dict(m, _src="closed") for m in c.markets(series_ticker=s, status="closed")]:
            r = rec(m)
            if r["date"] and since <= r["date"] <= until:
                rows[(r["date"], r["et"])].append(r)
        print(f"\n===== {s} values {since}..{until}")
        for (d, et), rs in sorted(rows.items()):
            vals = sorted({r["vs"] for r in rs})
            st = sorted({stamp(r["settled"]) for r in rs if r["settled"]})
            res = collections.Counter(r["res"] for r in rs)
            print(f"   {d} {et}: values {vals} settled {st} results {dict(res)} status "
                  f"{dict(collections.Counter(r['status'] for r in rs))}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "rules"
    if mode == "rules":
        rules()
    elif mode == "history":
        history()
    elif mode == "gaps":
        days = float(sys.argv[2]) if len(sys.argv) > 2 else 400
        fams = sys.argv[3].split(",") if len(sys.argv) > 3 else ["D", "W", "M", "Y", "ED"]
        gaps(days, fams)
    elif mode == "detail":
        detail(sys.argv[2].split(","))
    elif mode == "values":
        values(sys.argv[2].split(","), sys.argv[3], sys.argv[4])
    elif mode == "live":
        rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        gap = float(sys.argv[3]) if len(sys.argv) > 3 else 60
        live(rounds, gap)


if __name__ == "__main__":
    main()
