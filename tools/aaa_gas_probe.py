"""Idea #20: AAA gas daily vs weekly vs monthly (KXAAAGASD, KXAAAGASW, KXAAAGASM).
Probe branch only, never merged. Public data only (no key, no orders).

Modes:
  rules    every series' terms, every distinct rules wording (open and settled), contract PDFs,
           strike fields vs rules text, and AAA's own page.
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


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "rules"
    if mode == "rules":
        rules()


if __name__ == "__main__":
    main()
