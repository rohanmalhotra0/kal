"""Idea #21: gold and silver (and other commodities), daily vs monthly at month end.
Probe branch only, never merged. Public data only (no key, no orders).

Modes (the workflow's `args`; several can run in one go, split by " ; "):
  discover              every commodity series: frequency, sources, contract docs, open/settled events.
  rules S,S,...         each series' terms, every distinct rules wording (open and settled) with dates,
                        fields, strike grids, settled values, contract PDFs.
  history F,F,...       every event of each family's series; events in two series naming the same
                        moment; their values, results, settle times and rules; sets that lost.
  gaps F,F,... [DAYS]   hourly candles of every pair: sets under $1 after fees that pass the bot's rules.
  live F,F,... [ROUNDS] [GAP_S]  open pairs now, priced on the real order books.
  detail E,E,...        every market of the named events in full.
A family is a name from FAMILIES, or name=S1+S2+S3.
"""
import collections
import io
import itertools
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kal import config  # noqa: E402
from kal.client import Kalshi, KalshiError, candle_close, fee, price, ts  # noqa: E402
from kal.fees import _bad as fee_problem  # noqa: E402
from kal.risk import MAX_ARB_ROI, yearly_return  # noqa: E402
from price_links_sets import INF, Iv, meta_iv, payout_min  # noqa: E402

ET = ZoneInfo("America/New_York")
H, DAY = 3600, 86400
N = 30   # contracts per order, for the fee's cent rounding (max_per_arb is $30)

COMMOD = re.compile(
    r"gold|silver|copper|platinum|palladium|brent|\bwti\b|crude|\boil\b|natural gas|natgas|gasoline|"
    r"aluminum|aluminium|nickel|zinc|lithium|uranium|iron ore|steel|corn|wheat|soybean|coffee|cocoa|"
    r"sugar|cotton|cattle|\bhogs?\b|lumber|orange juice|commodit|metal", re.I)
FAMILIES = {
    "gold": ["KXGOLDD", "KXGOLDW", "KXGOLDMON"],
    "silver": ["KXSILVERD", "KXSILVERW", "KXSILVERMON"],
    "copper": ["KXCOPPERD", "KXCOPPERW", "KXCOPPERMON"],
    "brent": ["KXBRENTD", "KXBRENTW", "KXBRENTMON"],
}

MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
          "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
MON = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
DATE_RX = re.compile(r"\b(" + MONTHS + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)
MONYEAR_RX = re.compile(r"\b(" + MONTHS + r")\.?,?\s+(\d{4})\b", re.I)
ISO_RX = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
WD_RX = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
MONEY_RX = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")
NUM_RX = re.compile(r"(?<![\w:.,$])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d{3,})(?![\w:])")


def stamp(t):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(t)) if t else None


def et_stamp(t):
    return datetime.fromtimestamp(t, ET).strftime("%a %Y-%m-%d %H:%M ET") if t else None


def iso(v):
    try:
        return ts(v) if v else None
    except (TypeError, ValueError, AttributeError):
        return None


def plain(t):
    return " ".join(str(t or "").split())


def template(text):
    """Rules text with dates, weekdays, dollar amounts and strike-like numbers taken out.
    Times (5:00 PM), small counts (7 days) and codes (GCZ6) stay."""
    t = plain(text)
    t = DATE_RX.sub("<DATE>", t)
    t = ISO_RX.sub("<DATE>", t)
    t = MONYEAR_RX.sub("<MONTH>", t)
    t = WD_RX.sub("<WD>", t)
    t = MONEY_RX.sub("<$>", t)
    t = NUM_RX.sub("<N>", t)
    return t


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


def family_list(spec):
    out = []
    for f in spec.split(","):
        if "=" in f:
            name, ss = f.split("=", 1)
            out.append((name, ss.split("+")))
        elif f in FAMILIES:
            out.append((f, FAMILIES[f]))
        else:
            print(f"unknown family {f}")
    return out


# ---------- strikes: every reading of a market's YES set ----------

NUMBER = r"\$?\s?(-?\d[\d,]*(?:\.\d+)?)"
RULES = [
    (re.compile(r"\b(?:is|are|be|settles?|closes?)\s+(?:strictly\s+)?between\s+" + NUMBER +
                r"\s*(?:-|–|and|to)\s*" + NUMBER, re.I), "between"),
    (re.compile(r"\b(?:is|are|be|settles?|closes?)\s+(?:at or above|at least|greater than or equal to|"
                r"equal to or greater than|or more than|or higher than)\s+" + NUMBER, re.I), "greater_or_equal"),
    (re.compile(r"\b(?:is|are|be|settles?|closes?)\s+(?:at or below|at most|less than or equal to|"
                r"equal to or less than)\s+" + NUMBER, re.I), "less_or_equal"),
    (re.compile(r"\b(?:is|are|be|settles?|closes?)\s+(?:strictly\s+)?(?:above|greater than|more than|over|"
                r"higher than)\s+" + NUMBER, re.I), "greater"),
    (re.compile(r"\b(?:is|are|be|settles?|closes?)\s+(?:strictly\s+)?(?:below|less than|under|lower than)\s+"
                + NUMBER, re.I), "less"),
]


def num(v):
    d = dec(v)
    return float(d) if d is not None else None


def rules_reading(m):
    """(strike_type, Iv) from the rules text, or (None, None)."""
    text = plain(m.get("rules_primary"))
    hits = []
    for rx, st in RULES:
        g = rx.search(text)
        if g:
            hits.append((g.start(), rx, st, g))
    if not hits:
        return None, None
    _, rx, st, g = min(hits, key=lambda h: h[0])
    if st == "between":
        f, c = num(g.group(1)), num(g.group(2))
        if f is None or c is None or f >= c:
            return st, None
        return st, Iv(f, True, c, True, fuzzy=True)
    k = num(g.group(1))
    if k is None:
        return st, None
    return st, {"greater": Iv(k, False, INF, False), "greater_or_equal": Iv(k, True, INF, False),
                "less": Iv(-INF, False, k, False), "less_or_equal": Iv(-INF, False, k, True)}[st]


def readings(m):
    """([Iv], flags): the strike fields' reading and the rules text's reading of YES."""
    a = meta_iv(m)
    st, b = rules_reading(m)
    flags = []
    if a is None:
        flags.append(f"no strike fields (type {m.get('strike_type')!r})")
    if b is None:
        flags.append("rules unreadable")
    if a is not None and b is not None and a.key() != b.key():
        flags.append("strike fields and rules differ")
    uniq = {x.key(): x for x in (a, b) if x is not None}
    return list(uniq.values()), flags


def tick_of(markets):
    d = 0
    for m in markets:
        for v in (m.get("floor_strike"), m.get("cap_strike")):
            if v in (None, ""):
                continue
            s = str(v)
            if "e" in s.lower():
                s = f"{float(s):.12f}"
            if "." in s:
                d = max(d, len(s.split(".", 1)[1].rstrip("0")))
    return 10.0 ** -d


def holds(iv, v):
    """YES under this reading at value v: True/False, or None when v sits on a 'between' end
    (the rules may count it either way)."""
    if iv.fuzzy and v in (iv.lo, iv.hi):
        return None
    return iv.has(v)


# ---------- loading ----------

def settled_markets(c, s):
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


def all_markets(c, s, statuses=("settled", "closed", "open", "unopened")):
    rows = {}
    for st in statuses:
        if st == "settled":
            for m in settled_markets(c, s):
                rows.setdefault(m["ticker"], m)
            continue
        try:
            for m in c.markets(series_ticker=s, status=st):
                rows.setdefault(m["ticker"], dict(m, _src=st))
        except KalshiError as e:
            print(f"   {s} {st}: {e}")
    return list(rows.values())


_HIST_EVENTS_OK = [True]


def all_events(c, s):
    """{event_ticker: event} for every status, plus Kalshi's historical store if it has one."""
    out = {}
    for st in ("open", "closed", "settled", "unopened"):
        try:
            for ev in c.events(series_ticker=s, status=st):
                out.setdefault(ev["event_ticker"], dict(ev, _status=st))
        except KalshiError as e:
            print(f"   {s} events {st}: {e}")
    if _HIST_EVENTS_OK[0]:
        try:
            for ev in c.paginate("/historical/events", "events", {"limit": 200, "series_ticker": s}):
                out.setdefault(ev["event_ticker"], dict(ev, _status="historical"))
        except KalshiError as e:
            print(f"   /historical/events: {str(e)[:160]} (not used again)")
            _HIST_EVENTS_OK[0] = False
    return out


def pdf_text(url):
    import requests
    from pypdf import PdfReader
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        return f"(status {r.status_code})"
    if "pdf" not in (r.headers.get("content-type") or "") and not r.content[:5] == b"%PDF-":
        return f"(not a pdf: {r.headers.get('content-type')}) " + plain(r.text)[:3000]
    return " ".join(" ".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(r.content)).pages).split())


# ======================= discover =======================

SUFFIXES = ("MON", "MAX", "MIN", "AD", "AW", "AM", "D", "H", "W", "M", "Y", "Q", "A", "E")


def stem(tk):
    t = tk[2:] if tk.startswith("KX") else tk
    for suf in SUFFIXES:
        if t.endswith(suf) and len(t) > len(suf) + 2:
            return t[: -len(suf)]
    return t


def discover():
    c = Kalshi()
    t0 = time.time()
    allser = c.series()
    hits = [s for s in allser if isinstance(s.get("ticker"), str) and (
        s.get("category") == "Commodities" or COMMOD.search(f"{s.get('ticker')} {s.get('title') or ''}"))]
    print(f"series total {len(allser)}; commodity-like {len(hits)}; categories "
          f"{json.dumps(collections.Counter(s.get('category') for s in hits).most_common())}")
    for s in sorted(hits, key=lambda s: s["ticker"]):
        src = "; ".join(f"{x.get('name')} <{x.get('url')}>" for x in s.get("settlement_sources") or [])
        print(f"  {s['ticker']} | {s.get('frequency')} | {s.get('category')} | {s.get('title')} | fee "
              f"{s.get('fee_type')} x{s.get('fee_multiplier')} | src {src} | contract {s.get('contract_url')} | "
              f"terms {s.get('contract_terms_url')}")
    groups = collections.defaultdict(list)
    for s in hits:
        groups[stem(s["ticker"])].append(s)
    print(f"\n===== GROUPS by ticker stem ({len(groups)})")
    for g, ss in sorted(groups.items()):
        print(f"  {g}: " + ", ".join(f"{s['ticker']}({s.get('frequency')})" for s in sorted(ss, key=lambda s: s["ticker"])))
    print("\n===== EVENTS per series (open now; settled, up to 5 pages)")
    for s in sorted(hits, key=lambda s: s["ticker"]):
        tk = s["ticker"]
        if s.get("frequency") in ("fifteen_min",):
            continue
        if time.time() - t0 > 9 * 60:
            print(f"  (time cap; stopped before {tk})")
            break
        try:
            op = c.events(series_ticker=tk, status="open")
        except KalshiError as e:
            print(f"  {tk}: open events failed {e}")
            op = []
        sd = sorted(filter(None, (e.get("strike_date") for e in op)))
        settled, pages = [], 0
        try:
            for page in c.event_pages(series_ticker=tk, status="settled"):
                settled += page
                pages += 1
                if pages >= (1 if s.get("frequency") == "hourly" else 5):
                    break
        except KalshiError as e:
            print(f"  {tk}: settled events failed {e}")
        ssd = sorted(filter(None, (e.get("strike_date") for e in settled)))
        print(f"  {tk} ({s.get('frequency')}): open {len(op)} {sd[:2]}..{sd[-2:]} | settled {len(settled)}"
              f"{'+' if pages >= 5 or (s.get('frequency') == 'hourly' and pages) else ''} "
              f"{ssd[:1]}..{ssd[-1:]} (no strike_date: {sum(1 for e in settled if not e.get('strike_date'))})")
    print(f"\ndiscover done in {time.time() - t0:.0f}s")


# ======================= rules =======================

def rules(series_list):
    c = Kalshi()
    t0 = time.time()
    docs = collections.defaultdict(list)
    for s in series_list:
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
        try:
            evs = c.events(series_ticker=s, status="open", with_nested_markets="true")
        except Exception as e:
            print(f"open events failed: {e}")
            evs = []
        print(f"\n== {s}: open events {len(evs)}")
        full = False
        for ev in sorted(evs, key=lambda e: e.get("strike_date") or ""):
            ms = ev.get("markets") or []
            meta = {k: v for k, v in ev.items() if k != "markets"}
            print(f"\n  EVENT {json.dumps(meta)[:1200]}")
            print(f"    markets {len(ms)}; strike types {dict(collections.Counter(m.get('strike_type') for m in ms))}; "
                  f"close {dict(collections.Counter(m.get('close_time') for m in ms))}; expected "
                  f"{dict(collections.Counter(m.get('expected_expiration_time') for m in ms))}; latest "
                  f"{dict(collections.Counter(m.get('latest_expiration_time') for m in ms))}; open "
                  f"{dict(collections.Counter(m.get('open_time') for m in ms))}; tick {tick_of(ms):g}")
            grid = sorted(((m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike")) for m in ms),
                          key=lambda x: (str(x[0]), num(x[1]) if x[1] is not None else -1e18,
                                         num(x[2]) if x[2] is not None else 1e18))
            print(f"    grid first {grid[:5]} last {grid[-4:]}")
            flags = collections.Counter(f for m in ms for f in readings(m)[1])
            if flags:
                print(f"    reading flags {dict(flags)}")
            if not full and ms:
                full = True
                for m in sorted(ms, key=lambda m: num(m.get("floor_strike") or m.get("cap_strike")) or 0):
                    print(f"    {m['ticker']} st={m.get('strike_type')} floor={m.get('floor_strike')!r} "
                          f"cap={m.get('cap_strike')!r} yes_sub={m.get('yes_sub_title')!r} "
                          f"readings={readings(m)[0]} vol={m.get('volume_fp', m.get('volume'))} "
                          f"bid={m.get('yes_bid_dollars')} ask={m.get('yes_ask_dollars')}")
                m = ms[len(ms) // 2]
                print(f"\n    FULL MARKET {m['ticker']}:")
                for k, v in sorted(m.items()):
                    v = json.dumps(v) if not isinstance(v, str) else v
                    print(f"      {k}: {v[:5000]}")

        ms_all = all_markets(c, s)
        print(f"\n== {s}: markets in all statuses {len(ms_all)} "
              f"{dict(collections.Counter(m.get('_src') for m in ms_all))}; events "
              f"{len({m.get('event_ticker') for m in ms_all})}")
        for field in ("rules_primary", "rules_secondary", "early_close_condition"):
            groups = collections.defaultdict(list)
            for m in ms_all:
                groups[template(m.get(field))].append(m)
            print(f"\n  {s} {field}: {len(groups)} distinct wordings")
            for tp, rows in sorted(groups.items(), key=lambda kv: min(iso(m.get("close_time")) or 0 for m in kv[1])):
                cts = sorted(filter(None, (iso(m.get("close_time")) for m in rows)))
                evn = len({m.get("event_ticker") for m in rows})
                print(f"    [{len(rows)} markets, {evn} events, closes {stamp(cts[0]) if cts else None} .. "
                      f"{stamp(cts[-1]) if cts else None}] TEMPLATE {tp}")
                print(f"          EXAMPLE ({rows[-1]['ticker']}) {plain(rows[-1].get(field))}")
        sm = [m for m in ms_all if m.get("_src") in ("live", "historical")]
        fields = collections.Counter(k for m in sm for k, v in m.items() if v not in (None, "", [], {}))
        print(f"\n  {s} settled fields present (count of {len(sm)}): {json.dumps(dict(sorted(fields.items())))}")
        res = collections.Counter(m.get("result") for m in sm)
        sts = collections.Counter(m.get("strike_type") for m in sm)
        decs = collections.Counter()
        for m in sm:
            v = m.get("expiration_value")
            if v not in (None, ""):
                sv = str(v)
                decs[len(sv.split(".", 1)[1]) if "." in sv else 0] += 1
        flags = collections.Counter(f for m in sm for f in readings(m)[1])
        print(f"  {s} results {dict(res)}; strike types {dict(sts)}; expiration_value decimals {dict(decs)}; "
              f"reading flags {dict(flags)}")
        ex = collections.defaultdict(list)
        for m in sm:
            for f in readings(m)[1]:
                if len(ex[f]) < 3:
                    ex[f].append((m["ticker"], m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike"),
                                  plain(m.get("rules_primary"))[:240]))
        for f, rows in ex.items():
            print(f"    FLAG {f}: {json.dumps(rows)}")
        if sm:
            m = max(sm, key=lambda m: iso(m.get("close_time")) or 0)
            print(f"\n    NEWEST SETTLED MARKET {m['ticker']}:")
            for k, v in sorted(m.items()):
                v = json.dumps(v) if not isinstance(v, str) else v
                print(f"      {k}: {v[:5000]}")
            m = min(sm, key=lambda m: iso(m.get("close_time")) or 1e18)
            print(f"\n    OLDEST SETTLED MARKET {m['ticker']}: close {m.get('close_time')} | R1 "
                  f"{plain(m.get('rules_primary'))} | R2 {plain(m.get('rules_secondary'))}")
        sys.stdout.flush()

    for url, who in docs.items():
        print(f"\n\n===== DOC {url} (for {', '.join(who)})")
        try:
            text = pdf_text(url)
        except Exception as e:
            print(f"   failed: {e}")
            continue
        print(f"   {len(text)} chars")
        for i in range(0, min(len(text), 20000), 1000):
            print(f"   {text[i:i + 1000]}")
    print(f"\nrules done in {time.time() - t0:.0f}s")


# ======================= history =======================

def summarize(et, ms, ev):
    vals = sorted({str(m.get("expiration_value")) for m in ms if m.get("expiration_value") not in (None, "")})
    st = [iso(m.get("settlement_ts")) for m in ms]
    r1 = collections.Counter(template(m.get("rules_primary")) for m in ms)
    r2 = collections.Counter(template(m.get("rules_secondary")) for m in ms)
    dates = collections.Counter(rules_date(m.get("rules_primary")) for m in ms)
    return dict(
        et=et, s=series_of(et), ms=ms, ev=ev or {}, strike_date=iso((ev or {}).get("strike_date")),
        close=sorted({iso(m.get("close_time")) for m in ms} - {None}),
        exp=sorted({iso(m.get("expected_expiration_time")) for m in ms} - {None}),
        late=sorted({iso(m.get("latest_expiration_time")) for m in ms} - {None}),
        opened=min(filter(None, (iso(m.get("open_time")) for m in ms)), default=None),
        settled=(min(filter(None, st), default=None), max(filter(None, st), default=None)),
        n_settled=sum(1 for x in st if x), vals=vals, res=collections.Counter(m.get("result") for m in ms),
        status=collections.Counter(m.get("status") for m in ms), r1=r1, r2=r2, rdate=dates.most_common(1)[0][0],
        tick=tick_of(ms))


def load_family(c, members):
    """{series: {event_ticker: summary}}"""
    out = {}
    for s in members:
        t = time.time()
        ms = all_markets(c, s)
        evs = all_events(c, s)
        by = collections.defaultdict(list)
        for m in ms:
            by[m.get("event_ticker")].append(m)
        out[s] = {et: summarize(et, rows, evs.get(et)) for et, rows in by.items()}
        print(f"   {s}: {len(ms)} markets, {len(by)} events with markets, {len(evs)} events listed "
              f"({sum(1 for e in out[s].values() if e['strike_date'])} with a strike_date) in {time.time() - t:.0f}s")
        sys.stdout.flush()
    return out


def moment_keys(e):
    keys = set()
    if e["strike_date"]:
        keys.add(("strike_date", e["strike_date"]))
    if len(e["close"]) == 1:
        keys.add(("close", e["close"][0]))
    if e["rdate"]:
        keys.add(("rules date", e["rdate"]))
    return keys


def pairs_of(fam):
    """[(match kinds, event a, event b)] across two series of one family."""
    idx = collections.defaultdict(list)
    for s, evs in fam.items():
        for e in evs.values():
            for k in moment_keys(e):
                idx[k].append(e)
    found = collections.defaultdict(set)
    for k, es in idx.items():
        for a, b in itertools.combinations(es, 2):
            if a["s"] == b["s"]:
                continue
            if a["s"] > b["s"]:
                a, b = b, a
            found[(a["et"], b["et"])].add(k[0])
    evs = {e["et"]: e for s in fam.values() for e in s.values()}
    return sorted(((sorted(kinds), evs[a], evs[b]) for (a, b), kinds in found.items()),
                  key=lambda x: (x[1]["strike_date"] or (x[1]["close"] or [0])[-1]))


def res_of(m):
    return m.get("result") if m.get("result") in ("yes", "no") else None


def check_pair(a, b, tally, key, verbose=True):
    """Settled values, results vs values, and every 2-leg set that should pay $1 if both settle on
    one number: did any lose?"""
    out = {}
    va = {dec(v).normalize() for v in a["vals"] if dec(v) is not None}
    vb = {dec(v).normalize() for v in b["vals"] if dec(v) is not None}
    out["same"] = bool(va) and va == vb and len(va) == 1
    out["va"], out["vb"] = sorted(map(str, va)), sorted(map(str, vb))
    settled = all(res_of(m) for m in a["ms"]) and all(res_of(m) for m in b["ms"])
    out["settled"] = settled
    if not settled:
        return out
    tally[(key, "settled pairs")] += 1
    if not out["same"]:
        tally[(key, "settled pairs whose values differ")] += 1
    bad, ties = [], []
    for e in (a, b):
        v = sorted(va if e is a else vb)
        v = float(v[0]) if len(v) == 1 else None
        for m in e["ms"]:
            ivs, _ = readings(m)
            if v is None:
                continue
            for iv in ivs:
                h = holds(iv, v)
                if (math_isfinite(iv.lo) and v == iv.lo) or (math_isfinite(iv.hi) and v == iv.hi):
                    ties.append((m["ticker"], m.get("strike_type"), str(v), repr(iv), res_of(m)))
                if h is not None and h != (res_of(m) == "yes"):
                    bad.append((m["ticker"], str(v), repr(iv), res_of(m)))
                    break
    out["bad"], out["ties"] = bad, ties
    if bad:
        tally[(key, "settled pairs with a result that disagrees with its value")] += 1
    if ties:
        tally[(key, "settled pairs with a value exactly on a strike")] += 1
    tick = min(a["tick"], b["tick"])
    rd = {m["ticker"]: readings(m)[0] for m in a["ms"] + b["ms"]}
    lost, n_sets = [], 0
    for x in a["ms"]:
        for y in b["ms"]:
            ix, iy = rd[x["ticker"]], rd[y["ticker"]]
            if not ix or not iy:
                continue
            for sx in ("yes", "no"):
                for sy in ("yes", "no"):
                    if payout_min([(ix, sx), (iy, sy)], tick, "robust") < 1:
                        continue
                    n_sets += 1
                    paid = (res_of(x) == sx) + (res_of(y) == sy)
                    if paid < 1:
                        lost.append((x["ticker"], sx, y["ticker"], sy))
    out["sets"], out["lost"] = n_sets, lost
    tally[(key, "2-leg sets that pay $1 on one shared number")] += n_sets
    if lost:
        tally[(key, "settled pairs where such a set lost")] += 1
        tally[(key, "sets that lost")] += len(lost)
    return out


def math_isfinite(x):
    return x not in (INF, -INF)


STRIKE_RX = re.compile(r"\b(above|below|between|at least|at most|greater than|less than)\s+\$?\s?-?[\d,]*\.?\d+"
                       r"(?:\s*(?:-|–|and|to)\s*\$?\s?-?[\d,]*\.?\d+)?", re.I)


def canon(text):
    """Rules text with only the date and the strike taken out: what must match word for word."""
    t = plain(text)
    t = DATE_RX.sub("<DATE>", t)
    t = ISO_RX.sub("<DATE>", t)
    t = MONYEAR_RX.sub("<MONTH>", t)
    t = WD_RX.sub("<WD>", t)
    return STRIKE_RX.sub(lambda g: g.group(1) + " <K>", t)


def rules_diff(a, b):
    """How the two events' rules differ (dates and strikes taken out)."""
    out = []
    for field in ("rules_primary", "rules_secondary"):
        ta = {canon(m.get(field)) for m in a["ms"]}
        tb = {canon(m.get(field)) for m in b["ms"]}
        if ta != tb:
            out.append((field, sorted(ta - tb)[:2], sorted(tb - ta)[:2]))
    return out


def sources(e):
    ss = (e.get("ev") or {}).get("settlement_sources") or []
    return [f"{x.get('name')} <{x.get('url')}>" for x in ss]


def feeds(e):
    """The settlement source URLs without their query strings: which feed the event names."""
    ss = (e.get("ev") or {}).get("settlement_sources") or []
    return tuple(sorted(str(x.get("url") or "").split("?")[0].replace("pythdata.app", "app.pyth.com") for x in ss))


def contracts(e):
    out = collections.Counter()
    for m in e["ms"]:
        cs = m.get("custom_strike") if isinstance(m.get("custom_strike"), dict) else {}
        out[cs.get("front_month_contract")] += 1
    return dict(out)


def decimals_of(e):
    return sorted({len(v.split(".", 1)[1]) if "." in v else 0 for v in e["vals"]})


def history(fams):
    c = Kalshi()
    t0 = time.time()
    for name, members in fams:
        print(f"\n\n######## FAMILY {name}: {members}")
        fam = load_family(c, members)
        for s, evs in fam.items():
            print(f"\n== {s}: {len(evs)} events")
            rows = sorted(evs.values(), key=lambda e: e["strike_date"] or (e["close"] or [0])[-1])
            # Runs of events that share a source, a contract and a rules wording.
            runs, last = [], None
            for e in rows:
                key = (tuple(sources(e)), json.dumps(contracts(e)),
                       tuple(sorted({canon(m.get("rules_primary")) for m in e["ms"]})),
                       tuple(sorted({canon(m.get("rules_secondary")) for m in e["ms"]})))
                if key != last:
                    runs.append([key, e, e, 0])
                    last = key
                runs[-1][2] = e
                runs[-1][3] += 1
            print(f"   {s}: {len(runs)} runs of events with the same source, contract and rules:")
            for key, first, lastev, n in runs:
                print(f"   RUN {first['et']} .. {lastev['et']} ({n} events): sources {list(key[0])}; contracts {key[1]}; "
                      f"R1 {[k[:200] for k in key[2]]}; R2 {[k[:160] for k in key[3]]}")
            for e in rows:
                odd = (decimals_of(e) not in ([2], [0, 2]) or not e["vals"] or len(e["vals"]) > 1 or
                       set(e["res"]) - {"yes", "no"})
                if len(rows) > 60 and not odd:
                    continue
                print(f"   {e['et']}: sd {et_stamp(e['strike_date'])} | close {[et_stamp(t) for t in e['close']]} | "
                      f"late {[stamp(t) for t in e['late']]} | values {e['vals']} | settled "
                      f"{[et_stamp(t) for t in e['settled']]} | res {dict(e['res'])} | status {dict(e['status'])} | "
                      f"n {len(e['ms'])}{' | ODD' if odd else ''}")
        pairs = pairs_of(fam)
        print(f"\n===== {name}: event pairs naming the same moment: {len(pairs)}")
        tally = collections.Counter()
        for kinds, a, b in pairs:
            key = f"{a['s']}~{b['s']}"
            tally[(key, "pairs")] += 1
            tally[(key, "matched by " + "+".join(kinds))] += 1
            r = check_pair(a, b, tally, key)
            diff = rules_diff(a, b)
            if diff:
                tally[(key, "pairs whose rules read differently")] += 1
            same_close = a["close"] == b["close"]
            if not same_close:
                tally[(key, "pairs whose close times differ")] += 1
            print(f"\n   PAIR {a['et']} ~ {b['et']} [{'+'.join(kinds)}]: values {r['va']} vs {r['vb']} "
                  f"{'SAME' if r['same'] else 'DIFFERENT'}; settled {r['settled']}")
            print(f"      strike_date {et_stamp(a['strike_date'])} / {et_stamp(b['strike_date'])}; close "
                  f"{[et_stamp(t) for t in a['close']]} / {[et_stamp(t) for t in b['close']]}; exp "
                  f"{[et_stamp(t) for t in a['exp']]} / {[et_stamp(t) for t in b['exp']]}; late "
                  f"{[stamp(t) for t in a['late']]} / {[stamp(t) for t in b['late']]}")
            print(f"      settled at {[et_stamp(t) for t in a['settled']]} / {[et_stamp(t) for t in b['settled']]}; "
                  f"opened {et_stamp(a['opened'])} / {et_stamp(b['opened'])}; markets {len(a['ms'])}/{len(b['ms'])}; "
                  f"results {dict(a['res'])}/{dict(b['res'])}; rules dates {a['rdate']}/{b['rdate']}")
            if r.get("settled"):
                print(f"      2-leg sets {r['sets']}, lost {len(r['lost'])} {r['lost'][:4]}; bad results {r['bad'][:4]}; "
                      f"ties {r['ties'][:6]}")
            sa, sb = sources(a), sources(b)
            print(f"      sources {'SAME' if sa == sb else 'DIFFER'}: {sa} / {sb}; contracts {contracts(a)} / {contracts(b)}; "
                  f"value decimals {decimals_of(a)} / {decimals_of(b)}")
            if sa != sb:
                tally[(key, "pairs whose event sources differ")] += 1
            if not diff:
                print(f"      RULES SAME: R1 {sorted({canon(m.get('rules_primary')) for m in a['ms']})[:1]} | R2 "
                      f"{sorted({canon(m.get('rules_secondary')) for m in a['ms']})[:1]}")
            for field, xa, xb in diff:
                print(f"      RULES DIFFER {field}:\n         A {xa}\n         B {xb}")
            sys.stdout.flush()
        print(f"\n===== {name} SUMMARY")
        for k, v in sorted(tally.items()):
            print(f"   {k[0]} | {k[1]}: {v}")
    print(f"\nhistory done in {time.time() - t0:.0f}s")


def event_markets(c, et):
    ms = []
    try:
        ms = [dict(m, _src="live") for m in c.markets(event_ticker=et)]
    except KalshiError:
        ms = []
    if not ms:
        try:
            ms = [dict(m, _src="historical") for m in
                  c.paginate("/historical/markets", "markets", {"limit": 1000, "event_ticker": et})]
        except KalshiError:
            ms = []
    return ms


def event_meta(c, et):
    try:
        return (c.get(f"/events/{et}").get("event") or {})
    except KalshiError:
        return {}


def hourly(pairs):
    """For each monthly event, the hourly series' event at the same moment (same ticker suffix)."""
    c = Kalshi()
    for hs, mser in pairs:
        print(f"\n\n######## HOURLY {hs} vs {mser}")
        mevs = all_events(c, mser)
        by = collections.defaultdict(list)
        for m in all_markets(c, mser):
            by[m.get("event_ticker")].append(m)
        tally = collections.Counter()
        for et_m in sorted(by):
            et_h = f"{hs}-{et_m.split('-', 1)[1]}"
            ms_h = event_markets(c, et_h)
            if not ms_h:
                print(f"   {et_m}: no {et_h}")
                tally["no hourly event"] += 1
                continue
            a = summarize(et_h, ms_h, event_meta(c, et_h))
            b = summarize(et_m, by[et_m], mevs.get(et_m))
            key = f"{hs}~{mser}"
            r = check_pair(a, b, tally, key)
            diff = rules_diff(a, b)
            print(f"\n   PAIR {et_h} ~ {et_m}: values {r['va']} vs {r['vb']} {'SAME' if r['same'] else 'DIFFERENT'}; "
                  f"settled {r['settled']}; markets {len(a['ms'])}/{len(b['ms'])} strike types "
                  f"{dict(collections.Counter(m.get('strike_type') for m in a['ms']))}")
            print(f"      close {[et_stamp(t) for t in a['close']]} / {[et_stamp(t) for t in b['close']]}; settled at "
                  f"{[et_stamp(t) for t in a['settled']]} / {[et_stamp(t) for t in b['settled']]}; sources {sources(a)} / "
                  f"{sources(b)}")
            if r.get("settled"):
                print(f"      2-leg sets {r['sets']}, lost {len(r['lost'])} {r['lost'][:4]}; bad results {r['bad'][:4]}; "
                      f"ties {r['ties'][:4]}")
            if not diff:
                print(f"      RULES SAME: R1 {sorted({canon(m.get('rules_primary')) for m in a['ms']})[:1]}")
            for field, xa, xb in diff:
                print(f"      RULES DIFFER {field}:\n         A {xa}\n         B {xb}")
            sys.stdout.flush()
        print(f"\n   {hs} summary: {json.dumps({str(k): v for k, v in tally.items()})}")


def pyth(dates, series):
    """Pyth's own data next to Kalshi's settled values: which feed (and which minute) each series
    settled on. Public endpoints only."""
    import requests
    s = requests.Session()
    for q in ("gold", "silver", "XAU", "XAG", "copper", "brent"):
        try:
            r = s.get("https://hermes.pyth.network/v2/price_feeds", params={"query": q}, timeout=30)
            rows = r.json() if r.status_code == 200 else []
            print(f"\nHERMES query {q}: status {r.status_code}, {len(rows)} feeds")
            for f in rows[:25]:
                a = f.get("attributes") or {}
                print(f"   {f.get('id')} | {a.get('symbol')} | {a.get('asset_type')} | {a.get('description')} | "
                      f"{a.get('generic_symbol')} | {json.dumps({k: v for k, v in a.items() if k not in ('symbol', 'asset_type', 'description', 'generic_symbol')})[:200]}")
        except Exception as e:
            print(f"HERMES {q}: {e}")
    for q in ("XAU", "XAG", "GOLD", "SILVER", "1OZGOLD"):
        try:
            r = s.get("https://benchmarks.pyth.network/v1/shims/tradingview/search", params={"query": q, "limit": 30}, timeout=30)
            print(f"\nTV search {q}: status {r.status_code} {plain(r.text)[:1500]}")
        except Exception as e:
            print(f"TV search {q}: {e}")
    syms = ["Metal.XAU/USD", "Metal.XAG/USD", "Metal.Index.1OZGOLD/USD", "Metal.Index.SILVER/USD", "Metal.Index.GOLD/USD",
            "Commodities.Index.CU/USD", "Commodities.Index.BRENT/USD"]
    kal = {}
    c = Kalshi()
    for ser in series:
        for m in all_markets(c, ser, statuses=("settled",)):
            d = rules_date(m.get("rules_primary"))
            if d in dates and m.get("expiration_value") not in (None, ""):
                kal.setdefault((ser, d), set()).add(str(m.get("expiration_value")))
    print("\nKALSHI values: " + json.dumps({f"{k[0]} {k[1]}": sorted(v) for k, v in sorted(kal.items())}))
    for d in dates:
        y, mo, dd = map(int, d.split("-"))
        t5 = int(datetime(y, mo, dd, 17, 0, tzinfo=ET).timestamp())
        print(f"\n== {d} (5 PM ET = {t5})")
        for sym in syms:
            try:
                r = s.get("https://benchmarks.pyth.network/v1/shims/tradingview/history",
                          params={"symbol": sym, "resolution": "1", "from": t5 - 300, "to": t5 + 240}, timeout=30)
                j = r.json() if r.status_code == 200 else {}
            except Exception as e:
                print(f"   {sym}: {e}")
                continue
            if j.get("s") != "ok":
                print(f"   {sym}: status {r.status_code} {plain(r.text)[:160]}")
                continue
            rows = [(datetime.fromtimestamp(t, ET).strftime("%H:%M"), o, h, l, cl)
                    for t, o, h, l, cl in zip(j["t"], j["o"], j["h"], j["l"], j["c"])]
            print(f"   {sym}: " + "; ".join(f"{hm} o{o} c{cl}" for hm, o, h, l, cl in rows))
        sys.stdout.flush()


FEEDS = {
    "Metal.XAU/USD": "765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2",
    "Metal.Index.GOLD/USD": "fa0f57505be633c026896e15afef2c7ce2cf8ff9a45349d1da737f4f01266b01",
    "Metal.Index.1OZGOLD/USD": "7fd2c87083dd8fd5af2486a43f40b8e444acd77fda4e96263a53421cd079c387",
    "Metal.XAG/USD": "f2fb02c32b055c805e7238d628e5e9dadef274376114eb1f012337cabe93871e",
    "Metal.Index.SILVER/USD": "6afca47e1c79ecd0d2844327efb463478417a9d498dfb0915ce5e88ae6d952d3",
}


def hermes(dates, offsets):
    """Each Pyth feed's price at given seconds after 5 PM ET (negative = before) on each date, from
    Pyth's public Hermes API. Paced to stay under its rate limit."""
    import requests
    s = requests.Session()
    for d in dates:
        y, mo, dd = map(int, d.split("-"))
        t5 = int(datetime(y, mo, dd, 17, 0, tzinfo=ET).timestamp())
        print(f"\n== {d}")
        for name, fid in FEEDS.items():
            row = []
            for off in offsets:
                t = t5 + off
                for attempt in range(4):
                    try:
                        r = s.get(f"https://hermes.pyth.network/v2/updates/price/{t}",
                                  params={"ids[]": fid, "parsed": "true"}, timeout=30)
                    except Exception as e:
                        row.append(f"{off}: {e}")
                        break
                    if r.status_code == 429:
                        time.sleep(3 + 3 * attempt)
                        continue
                    if r.status_code != 200:
                        row.append(f"{off}: status {r.status_code} {plain(r.text)[:80]}")
                        break
                    p = ((r.json().get("parsed") or [{}])[0]).get("price") or {}
                    try:
                        v = int(p["price"]) * 10 ** int(p["expo"])
                        pt = datetime.fromtimestamp(int(p["publish_time"]), ET).strftime("%H:%M:%S")
                        row.append(f"{off:+d}s -> {v:.4f} (published {pt})")
                    except (KeyError, TypeError, ValueError):
                        row.append(f"{off}: {plain(r.text)[:120]}")
                    break
                time.sleep(0.5)
            print(f"   {name}: " + "; ".join(row))
            sys.stdout.flush()


# ======================= gaps =======================

def candles_for(c, e, t0, t1):
    """{ticker: [(end_ts, bid, ask, volume)] sorted}: the event endpoint, then each market, then the
    historical store."""
    out = collections.defaultdict(dict)
    s, et = e["s"], e["et"]
    start, calls = int(t0), 0
    while calls < 20:
        calls += 1
        try:
            d = c.get(f"/series/{s}/events/{et}/candlesticks",
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
    t_in = time.time()
    for m in e["ms"]:
        tk = m["ticker"]
        if out.get(tk):
            continue
        if time.time() - t_in > 120:
            print(f"      (candle time cap in {et})")
            break
        rows = []
        for path in (f"/series/{s}/markets/{tk}/candlesticks", f"/historical/markets/{tk}/candlesticks"):
            try:
                rows = c.get(path, {"start_ts": int(t0), "end_ts": int(t1), "period_interval": 60}).get("candlesticks") or []
            except KalshiError:
                rows = []
            if rows:
                break
        for r in rows:
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


def safe_sets(a, b):
    """Every 2-leg set across the two events that pays at least $1 at any one shared value, for every
    reading of both markets: [(x ticker, side, y ticker, side, kind)]."""
    tick = min(a["tick"], b["tick"])
    rd = {m["ticker"]: readings(m)[0] for m in a["ms"] + b["ms"]}
    out = []
    for x in a["ms"]:
        for y in b["ms"]:
            ix, iy = rd[x["ticker"]], rd[y["ticker"]]
            if not ix or not iy:
                continue
            for sx in ("yes", "no"):
                for sy in ("yes", "no"):
                    if payout_min([(ix, sx), (iy, sy)], tick, "robust") >= 1:
                        kx = "/".join(sorted({repr(i) for i in ix}))
                        ky = "/".join(sorted({repr(i) for i in iy}))
                        same = kx == ky
                        out.append((x["ticker"], sx, y["ticker"], sy, "same strike" if same else "nested"))
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
                apr=round(apr, 2) if apr < 1e6 else "huge", ok=ok, too_good=roi > MAX_ARB_ROI)


def trades_in(c, tk, t0, t1):
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


def gaps(fams, since_days):
    cfg = dict(config.DEFAULTS)
    c = Kalshi()
    t_start = time.time()
    since = time.time() - since_days * DAY
    for name, members in fams:
        print(f"\n\n######## GAPS {name}: {members}")
        for s in members:
            try:
                info = c.series_info(s)
                print(f"   {s}: fee {info.get('fee_type')} x{info.get('fee_multiplier')} -> "
                      f"{fee_problem(info.get('fee_type'), info.get('fee_multiplier')) or 'the bot can price it'}")
            except Exception as e:
                print(f"   {s}: series info failed: {e}")
        fam = load_family(c, members)
        pairs = pairs_of(fam)
        tally = collections.Counter()
        hours = collections.defaultdict(set)
        passing = collections.defaultdict(list)
        near = []
        table = []
        for kinds, a, b in reversed(pairs):
            key = f"{a['s']}~{b['s']}"
            if time.time() - t_start > 17 * 60:
                tally[(key, "pairs left out (time)")] += 1
                continue
            end = min(max(a["close"] or [0]), max(b["close"] or [0]), int(time.time()))
            opened = max(a["opened"] or 0, b["opened"] or 0)
            if end < since:
                tally[(key, "pairs older than the window")] += 1
                continue
            if not opened or end <= opened:
                tally[(key, "pairs never open together")] += 1
                continue
            settle = max(filter(None, a["exp"] + b["exp"] + a["close"] + b["close"]))
            sets = safe_sets(a, b)
            tally[(key, "pairs priced")] += 1
            tally[(key, "2-leg safe sets (sum over pairs)")] += len(sets)
            t_pair = time.time()
            ca = candles_for(c, a, opened, end)
            cb = candles_for(c, b, opened, end)
            if not ca or not cb:
                tally[(key, "pairs without candles")] += 1
                print(f"   {a['et']} ~ {b['et']}: no candles ({len(ca)}/{len(cb)} markets)")
                continue
            cand = {**ca, **cb}
            closes = {m["ticker"]: iso(m.get("close_time")) for m in a["ms"] + b["ms"]}
            n_hours, pass_hours, best_pair = 0, [], None
            n_under, n_vol, best_vol = 0, 0, None
            for t in range((int(opened) // H + 1) * H, int(end) + 1, H):
                q = {}
                for tk, rows in cand.items():
                    if closes.get(tk) and t > closes[tk]:
                        continue
                    x = quote_at(rows, t)
                    if x:
                        q[tk] = x
                rows_t = []
                for xt, sx, yt, sy, kind in sets:
                    qx, qy = q.get(xt), q.get(yt)
                    if not qx or not qy:
                        continue
                    px = qx[1] if sx == "yes" else round(1 - qx[0], 4)
                    py = qy[1] if sy == "yes" else round(1 - qy[0], 4)
                    if not (0 < px < 1 and 0 < py < 1):
                        continue
                    legs = [(xt, sx, px, qx[2]), (yt, sy, py, qy[2])]
                    j = judge(legs, t, settle, cfg)
                    rows_t.append(dict(j, pair=key, a=a["et"], b=b["et"], kind=kind, t=stamp(t), legs=legs))
                if not rows_t:
                    continue
                n_hours += 1
                tally[(key, "pair-hours with prices")] += 1
                best = max(rows_t, key=lambda r: r["edge"])
                near.append(best)
                if best["raw"] < 1:
                    tally[(key, "pair-hours under $1 before fees")] += 1
                if best["edge"] > 0:
                    hours[(key, "under $1 after fees")].add((a["et"], t))
                    n_under += 1
                if any(r["too_good"] and r["edge"] > 0 for r in rows_t):
                    hours[(key, "too good (>25%)")].add((a["et"], t))
                ok = [r for r in rows_t if r["ok"]]
                if ok:
                    bo = max(ok, key=lambda r: r["edge"])
                    hours[(key, "passing")].add((a["et"], t))
                    passing[key].append(bo)
                    pass_hours.append(t)
                    if all(l[3] > 0 for l in bo["legs"]):
                        hours[(key, "passing, both legs traded that hour")].add((a["et"], t))
                    if best_pair is None or bo["edge"] > best_pair["edge"]:
                        best_pair = bo
                    okv = [r for r in ok if all(l[3] > 0 for l in r["legs"])]
                    if okv:
                        n_vol += 1
                        bv = max(okv, key=lambda r: r["edge"])
                        if best_vol is None or bv["edge"] > best_vol["edge"]:
                            best_vol = bv
            same_rules = not rules_diff(a, b)
            same_src = feeds(a) == feeds(b)
            table.append(dict(pair=f"{a['et']}~{b['et']}", rules="same" if same_rules else "DIFF",
                              feed="same" if same_src else "DIFF", hours=n_hours, under=n_under,
                              passing=len(pass_hours), passing_vol=n_vol,
                              best=(round(best_pair["edge"], 4), best_pair["t"], [(l[0].split("-", 1)[1], l[1], l[2], l[3])
                                                                              for l in best_pair["legs"]]) if best_pair else None,
                              best_vol=(round(best_vol["edge"], 4), best_vol["t"], [(l[0].split("-", 1)[1], l[1], l[2], l[3])
                                                                                for l in best_vol["legs"]]) if best_vol else None,
                              by_month=(a["strike_date"] or max(a["close"] or [0]))))
            tally[(key, f"pairs with rules {'same' if same_rules else 'different'}, sources "
                        f"{'same' if same_src else 'different'}")] += 1
            if pass_hours:
                tally[(key, f"passing hours, rules {'same' if same_rules else 'different'}, sources "
                            f"{'same' if same_src else 'different'}")] += len(pass_hours)
            print(f"   {a['et']} ~ {b['et']}: rules {'same' if same_rules else 'DIFFERENT'}, sources "
                  f"{'same' if same_src else 'DIFFERENT'}; {len(sets)} safe sets; candles {len(ca)}/{len(cb)} markets; "
                  f"{et_stamp(opened)}..{et_stamp(end)}; hours with prices {n_hours}; passing hours {len(pass_hours)}; "
                  f"best passing {json.dumps(best_pair) if best_pair else None} ({time.time() - t_pair:.0f}s)")
            sys.stdout.flush()
        print(f"\n===== {name} PAIR TABLE (hours priced, under $1 after fees, passing, passing with trades on both legs)")
        for r in sorted(table, key=lambda r: r["by_month"]):
            print(f"   TABLE {r['pair']:<42} rules {r['rules']:<4} feed {r['feed']:<4} hours {r['hours']:>3} under {r['under']:>3} "
                  f"pass {r['passing']:>3} pass+vol {r['passing_vol']:>3} | best {json.dumps(r['best'])} | best+vol "
                  f"{json.dumps(r['best_vol'])}")
        print(f"\n===== {name} GAP SUMMARY")
        for k, v in sorted(tally.items()):
            print(f"   {k[0]} | {k[1]}: {v}")
        for (key, what), hs in sorted(hours.items()):
            evs = sorted({e for e, _ in hs})
            print(f"   {key} | hours {what}: {len(hs)} (pairs {len(evs)}: {evs[:40]})")
        for key, rows in sorted(passing.items()):
            rows.sort(key=lambda r: -r["edge"])
            print(f"\n   -- {key}: best passing sets (one per hour, best first)")
            for r in rows[:15]:
                print("      PASS " + json.dumps(r))
            vol = [r for r in rows if all(l[3] > 0 for l in r["legs"])]
            print(f"   -- {key}: best passing with trades on both legs that hour ({len(vol)})")
            for r in vol[:12]:
                print("      PASSVOL " + json.dumps(r))
        print("\n   closest sets overall:")
        for r in sorted(near, key=lambda r: -r["edge"])[:8]:
            print("      NEAR " + json.dumps(r))
        print("\n===== TRADES in the hour before the best passing snapshots")
        tops = sorted((r for rows in passing.values() for r in rows), key=lambda r: -r["edge"])
        seen = set()
        for r in tops:
            if len(seen) >= 12:
                break
            if r["a"] in seen:
                continue
            seen.add(r["a"])
            t1 = ts(r["t"].replace(" ", "T") + ":00Z")
            legs_out = []
            for tk, side, p, vol in r["legs"]:
                tr = trades_in(c, tk, t1 - H, t1)
                paid = []
                for x in tr:
                    if x.get("taker_side") != side:
                        continue
                    paid.append((price(x, f"{side}_price"), float(x.get("count_fp") or x.get("count") or 0)))
                legs_out.append((tk, side, p, len(tr), min(paid) if paid else None, sum(q for _, q in paid)))
            print(f"   {r['a']} ~ {r['b']} at {r['t']} edge {r['edge']}: " + json.dumps(legs_out))
    print(f"\ngaps done in {time.time() - t_start:.0f}s")


# ======================= live =======================

def book_quote(book, side):
    """(price, size) to buy `side` now from a book of bids, or (None, 0)."""
    levels = book.get("no" if side == "yes" else "yes") or []
    if not levels:
        return None, 0
    bid, size = max(levels)
    ask = round(1 - bid, 4)
    return (ask, size) if 0 < ask < 1 else (None, 0)


def live(fams, rounds, gap_s):
    cfg = dict(config.DEFAULTS)
    c = Kalshi()
    for rnd in range(rounds):
        if rnd:
            time.sleep(gap_s)
        now = time.time()
        print(f"\n== round {rnd + 1} at {et_stamp(now)}")
        for name, members in fams:
            fam = {}
            for s in members:
                ms = []
                for st in ("open", "unopened", "closed"):
                    try:
                        ms += [dict(m, _src=st) for m in c.markets(series_ticker=s, status=st)]
                    except KalshiError as e:
                        print(f"   {s} {st}: {e}")
                evs = {}
                try:
                    for ev in c.events(series_ticker=s, status="open"):
                        evs[ev["event_ticker"]] = ev
                except KalshiError:
                    pass
                by = collections.defaultdict(list)
                for m in ms:
                    by[m.get("event_ticker")].append(m)
                fam[s] = {et: summarize(et, rows, evs.get(et)) for et, rows in by.items()}
                for et, e in sorted(fam[s].items(), key=lambda kv: kv[1]["strike_date"] or 0):
                    if rnd == 0:
                        print(f"   {name} {et}: sd {et_stamp(e['strike_date'])} close {[et_stamp(t) for t in e['close']]} "
                              f"exp {[et_stamp(t) for t in e['exp']]} status {dict(e['status'])} n {len(e['ms'])} "
                              f"opened {et_stamp(e['opened'])} rules date {e['rdate']}")
            pairs = pairs_of(fam)
            print(f"   {name}: open pairs {[(a['et'], b['et'], k) for k, a, b in pairs]}")
            for kinds, a, b in pairs:
                live_a = [m for m in a["ms"] if m.get("status") in ("active", "open")]
                live_b = [m for m in b["ms"] if m.get("status") in ("active", "open")]
                if not live_a or not live_b:
                    print(f"   {a['et']} ~ {b['et']}: not both open ({len(live_a)}/{len(live_b)} markets open)")
                    continue
                diff = rules_diff(a, b)
                for field, xa, xb in diff:
                    print(f"      RULES DIFFER {field}:\n         A {xa}\n         B {xb}")
                q, sizes = {}, {}
                for m in live_a + live_b:
                    try:
                        bk = c.orderbook(m["ticker"])
                    except KalshiError:
                        continue
                    ya, ys = book_quote(bk, "yes")
                    na, ns = book_quote(bk, "no")
                    q[m["ticker"]] = (round(1 - na, 4) if na else 0.0, ya or 0.0, 0)
                    sizes[(m["ticker"], "yes")], sizes[(m["ticker"], "no")] = ys, ns
                a2, b2 = dict(a, ms=live_a), dict(b, ms=live_b)
                settle = max(filter(None, a["exp"] + b["exp"] + a["close"] + b["close"]))
                rows = []
                for xt, sx, yt, sy, kind in safe_sets(a2, b2):
                    qx, qy = q.get(xt), q.get(yt)
                    if not qx or not qy:
                        continue
                    px = qx[1] if sx == "yes" else round(1 - qx[0], 4)
                    py = qy[1] if sy == "yes" else round(1 - qy[0], 4)
                    if not (0 < px < 1 and 0 < py < 1):
                        continue
                    j = judge([(xt, sx, px, 0), (yt, sy, py, 0)], now, settle, cfg)
                    rows.append(dict(j, kind=kind, legs=[(xt, sx, px, sizes.get((xt, sx))), (yt, sy, py, sizes.get((yt, sy)))]))
                rows.sort(key=lambda r: -r["edge"])
                print(f"   {a['et']} ~ {b['et']} [{'+'.join(kinds)}]: books {len(q)}; sets priced {len(rows)}; under $1 "
                      f"before fees {sum(1 for r in rows if r['raw'] < 1)}; after fees {sum(1 for r in rows if r['edge'] > 0)}; "
                      f"passing {sum(1 for r in rows if r['ok'])}")
                for r in rows[:6]:
                    print("      BOOK " + json.dumps(r))
            sys.stdout.flush()


# ======================= detail =======================

def detail(items):
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
        for path in (f"/events/{et}", f"/historical/events/{et}"):
            try:
                e = c.get(path)
                meta = {k: v for k, v in (e.get("event") or {}).items() if k != "markets"}
                print(f"   EVENT ({path}) {json.dumps(meta)[:2000]}")
                break
            except KalshiError as ex:
                print(f"   {path}: {str(ex)[:120]}")
        shown = set()
        for m in sorted(ms, key=lambda m: num(m.get("floor_strike") or m.get("cap_strike")) or 0):
            print(f"   {m['ticker']} status={m.get('status')} result={m.get('result')!r} value={m.get('expiration_value')!r} "
                  f"settle$={m.get('settlement_value_dollars')} settled={m.get('settlement_ts')} close={m.get('close_time')} "
                  f"exp={m.get('expected_expiration_time')} st={m.get('strike_type')} floor={m.get('floor_strike')} "
                  f"cap={m.get('cap_strike')} vol={m.get('volume_fp')} last={m.get('last_price_dollars')} "
                  f"sub={m.get('yes_sub_title')!r}")
            for k in ("rules_primary", "rules_secondary", "early_close_condition"):
                t = template(m.get(k))
                if t and (k, t) not in shown:
                    shown.add((k, t))
                    print(f"      {k}: {plain(m.get(k))}")


def main():
    args = sys.argv[1:] or ["discover"]
    cmds, cur = [], []
    for a in args:
        if a == ";":
            if cur:
                cmds.append(cur)
            cur = []
        else:
            cur.append(a)
    if cur:
        cmds.append(cur)
    for cmd in cmds:
        mode, rest = cmd[0], cmd[1:]
        print(f"\n\n================================ {' '.join(cmd)} ================================")
        sys.stdout.flush()
        if mode == "discover":
            discover()
        elif mode == "rules":
            rules(rest[0].split(","))
        elif mode == "history":
            history(family_list(rest[0] if rest else ",".join(FAMILIES)))
        elif mode == "gaps":
            gaps(family_list(rest[0] if rest else ",".join(FAMILIES)), float(rest[1]) if len(rest) > 1 else 800)
        elif mode == "live":
            live(family_list(rest[0] if rest else ",".join(FAMILIES)), int(rest[1]) if len(rest) > 1 else 1,
                 float(rest[2]) if len(rest) > 2 else 60)
        elif mode == "detail":
            detail(rest[0].split(","))
        elif mode == "hourly":
            hourly([tuple(p.split("~")) for p in rest[0].split(",")])
        elif mode == "pyth":
            pyth(rest[0].split(","), rest[1].split(","))
        elif mode == "hermes":
            hermes(rest[0].split(","), [int(x) for x in (rest[1] if len(rest) > 1 else "-61,-1,0").split(",")])
        else:
            print(f"unknown mode {mode}")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
