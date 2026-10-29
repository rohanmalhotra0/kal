"""Idea #7, CPI and jobs: does the market already know what public forecasts know?
Probe branch only, never merged. Public data only (no key, no orders).

A. The Fed's study of Kalshi macro markets (Diercks, Katz, Wright 2026): key sentences.
B. Where the Cleveland Fed publishes its inflation nowcast data (links on its page).
C. Kalshi's CPI and jobs markets on real history: price 24 h before close vs the result.
   Brier score, calibration buckets, and simple rules after fees. One release = one event.
"""
import collections
import io
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal.client import Kalshi, fee, ts  # noqa: E402
from kal.research.backtest import candle_close  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (kal research probe)"}
PAPERS = ["https://www.federalreserve.gov/econres/feds/files/2026010pap.pdf",
          "https://www.ecb.europa.eu/press/conferences/shared/pdf/20261005_monpolconf/Wright_paper.pdf"]
KEYS = re.compile(r"headline cpi|core cpi|unemployment|payroll|cleveland|nowcast|efficien|unbiased|bias|"
                  r"trading (?:rule|strategy)|profit|mean absolute error|bloomberg|consensus|favorite|longshot", re.I)
MACRO = re.compile(r"\bcpi\b|inflation|payroll|jobs report|unemployment|nonfarm", re.I)
H, DAY = 3600, 86400
N = 30


def paper():
    try:
        from pypdf import PdfReader
    except ImportError:
        print("pypdf missing")
        return
    for url in PAPERS:
        try:
            r = requests.get(url, timeout=90, headers=UA)
            r.raise_for_status()
            reader = PdfReader(io.BytesIO(r.content))
            text = " ".join((p.extract_text() or "") for p in reader.pages)
        except Exception as e:
            print(f"PAPER {url}: failed: {e}")
            continue
        text = " ".join(text.split())
        print(f"\nPAPER {url}: {len(reader.pages)} pages, {len(text)} chars")
        print("ABSTRACT " + text[:2500])
        seen, shown = set(), 0
        for s in re.split(r"(?<=[.!?])\s+", text):
            if KEYS.search(s) and 40 < len(s) < 700 and s not in seen:
                seen.add(s)
                print("  SENT " + s)
                shown += 1
                if shown >= 90:
                    break
        return


def cleveland():
    for url in ("https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting",):
        try:
            r = requests.get(url, timeout=60, headers=UA)
            print(f"\nCLEVELAND {url}: HTTP {r.status_code}, {len(r.text)} chars")
            links = sorted(set(re.findall(r"""["'(]([^"'()\s]+\.(?:json|csv|xlsx?)(?:\?[^"'()\s]*)?)["')]""", r.text)))
            for x in links[:40]:
                print("  LINK " + x)
            for x in sorted(set(re.findall(r"https?://[^\"'\s<>]*(?:api|data)[^\"'\s<>]*", r.text)))[:40]:
                print("  URL " + x)
        except Exception as e:
            print(f"CLEVELAND {url}: failed: {e}")


def fee_pc(p):
    return fee(N, p) / N


def kalshi():
    c = Kalshi()
    series = [s for s in c.series() if MACRO.search(f"{s.get('ticker', '')} {s.get('title', '')}")
              and str(s.get("ticker", "")).startswith("KX")]
    print(f"\nmacro series: {len(series)}")
    for s in sorted(series, key=lambda s: s["ticker"]):
        print(f"  {s['ticker']} | {s.get('frequency')} | {s.get('title')}")
    want = [s["ticker"] for s in series if re.fullmatch(
        r"KX(CPI|CPIYOY|CPICORE|CPICOREYOY|PAYROLLS|U3|JOBS|NFP|UNEMPLOYMENT)", s["ticker"])]
    print(f"tested: {want}")
    rows = []
    t0 = time.time()
    for s in want:
        try:
            ms = c.settled_markets(s, limit=3000)
        except Exception as e:
            print(f"{s}: failed: {e}")
            continue
        print(f"{s}: {len(ms)} settled markets; rules: {' '.join(str((ms or [{}])[0].get('rules_primary') or '').split())[:300]}")
        for m in ms:
            if time.time() - t0 > 30 * 60:
                print("(time limit)")
                break
            res = m.get("result")
            if res not in ("yes", "no"):
                continue
            try:
                close = ts(m["close_time"])
            except Exception:
                continue
            at = close - DAY
            try:
                cs = c.candles(s, m["ticker"], at - 3 * H, at)
            except Exception:
                continue
            last = None
            for k in sorted(cs or [], key=lambda k: k["end_period_ts"]):
                if k["end_period_ts"] <= at:
                    last = k
            if not last:
                continue
            bid, ask = candle_close(last, "yes_bid"), candle_close(last, "yes_ask")
            if not (0 < bid < ask < 1):
                continue
            rows.append(dict(s=s, ev=m.get("event_ticker"), bid=bid, ask=ask, mid=(bid + ask) / 2, yes=res == "yes"))
    print(f"\npriced markets 24 h before close: {len(rows)} in {len({r['ev'] for r in rows})} releases")
    if not rows:
        return
    brier = sum((r["mid"] - r["yes"]) ** 2 for r in rows) / len(rows)
    base = sum(r["yes"] for r in rows) / len(rows)
    print(f"Brier (market mid): {brier:.4f}; always-the-base-rate {base * (1 - base):.4f}")
    buckets = collections.defaultdict(list)
    for r in rows:
        buckets[min(9, int(r["mid"] * 10))].append(r)
    for b in sorted(buckets):
        xs = buckets[b]
        print(f"  mid {b * 10:>2}-{b * 10 + 10}%: n={len(xs):>4}, won {sum(x['yes'] for x in xs) / len(xs):.3f}, "
              f"avg mid {sum(x['mid'] for x in xs) / len(xs):.3f}, releases {len({x['ev'] for x in xs})}")
    # Rules after fees, at the ask (YES) or 1 - bid (NO). By release: each release's trades pooled.
    rules = {
        "YES ask <= 10c": lambda r: ("yes", r["ask"]) if r["ask"] <= 0.10 else None,
        "NO when YES bid <= 10c": lambda r: ("no", 1 - r["bid"]) if r["bid"] <= 0.10 else None,
        "YES ask >= 85c": lambda r: ("yes", r["ask"]) if r["ask"] >= 0.85 else None,
        "NO when YES bid >= 85c": lambda r: ("no", 1 - r["bid"]) if r["bid"] >= 0.85 else None,
        "NO when YES 30-70c": lambda r: ("no", 1 - r["bid"]) if 0.30 <= r["bid"] <= 0.70 else None,
        "YES when 30-70c": lambda r: ("yes", r["ask"]) if 0.30 <= r["ask"] <= 0.70 else None,
    }
    for name, rule in rules.items():
        per = collections.defaultdict(lambda: [0.0, 0.0])
        n = 0
        for r in rows:
            got = rule(r)
            if not got:
                continue
            side, p = got
            cost = p + fee_pc(p)
            pay = 1.0 if (side == "yes") == r["yes"] else 0.0
            per[r["ev"]][0] += cost
            per[r["ev"]][1] += pay
            n += 1
        if not n:
            print(f"  RULE {name}: no trades")
            continue
        rois = [(pay - cost) / cost for cost, pay in per.values()]
        mean = sum(rois) / len(rois)
        sd = (sum((x - mean) ** 2 for x in rois) / max(1, len(rois) - 1)) ** 0.5
        cost = sum(v[0] for v in per.values())
        pay = sum(v[1] for v in per.values())
        print(f"  RULE {name}: {n} trades in {len(per)} releases; ROI {100 * (pay - cost) / cost:+.1f}% "
              f"(per-release mean {100 * mean:+.1f}%, 95% low {100 * (mean - 1.96 * sd / len(rois) ** 0.5):+.1f}%)")


if __name__ == "__main__":
    t = time.time()
    for part in (paper, cleveland, kalshi):
        try:
            part()
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"{part.__name__}: failed: {e}")
    print(f"\ndone in {time.time() - t:.0f}s")
