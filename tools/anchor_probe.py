"""Which time field could anchor a backtest's decision time without peeking?

A settled market that can close early shows, as close_time, when it actually closed:
maybe on the news. Research since PR #6 skips every event whose markets can all close
early, and on 2026-10-25 that left 904 of 26,676 priced markets (10 of 12 categories
went to zero). A field set before the event (a schedule) could anchor those instead.

Per series this prints how close_time compares with expected_expiration_time,
latest_expiration_time and the event's strike_date, on settled and on open markets,
and how results differ between markets that closed early and the rest.
Public data only: no key, no orders.
"""
import collections
import json
import time
from datetime import datetime

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = ["KXMLBGAME", "KXNFLGAME", "KXNBAGAME", "KXNHLGAME", "KXNCAAFGAME", "KXNFLSPREAD",
          "KXHIGHNY", "KXHIGHCHI", "KXRAIN", "KXBTCD", "KXETHD", "KXINX", "KXINXU",
          "KXAAAGASD", "KXWTI", "KXTRUMPAPPROVE", "KXFEDDECISION", "KXRT", "KXLLM1"]
NOW = time.time()
calls = collections.Counter()


def get(path, **params):
    code = None
    for attempt in range(3):
        time.sleep(0.6 + 2 * attempt)  # stay under the public rate limit
        try:
            r = requests.get(BASE + path, params=params, timeout=30)
        except requests.RequestException as e:
            code = type(e).__name__
            continue
        code = r.status_code
        if code == 429:
            continue
        calls[f"{path.split('?')[0]} {code}"] += 1
        try:
            return code, r.json()
        except ValueError:
            return code, {}
    calls[f"{path} {code}"] += 1
    return code, {}


def ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() if v else None
    except ValueError:
        return None


def spread(seconds):
    """min / p10 / median / p90 / max in hours, plus how many were exactly 0."""
    xs = sorted(seconds)
    if not xs:
        return None
    q = lambda p: round(xs[min(len(xs) - 1, int(p * len(xs)))] / 3600, 2)
    return {"n": len(xs), "exactly 0": sum(abs(x) < 1 for x in xs), "min": q(0), "p10": q(0.1),
            "median": q(0.5), "p90": q(0.9), "max": round(xs[-1] / 3600, 2)}


out = {}
for s in SERIES:
    _, d = get("/markets", series_ticker=s, status="settled", limit=200)
    settled = (d or {}).get("markets") or []
    _, d = get("/events", series_ticker=s, status="settled", limit=200)
    strike_raw = {e.get("event_ticker"): e.get("strike_date") for e in (d or {}).get("events") or []}
    strike = {k: ts(v) for k, v in strike_raw.items()}
    _, d = get("/markets", series_ticker=s, status="open", limit=200)
    open_ = (d or {}).get("markets") or []

    r = {"settled": len(settled), "open": len(open_),
         "can_close_early (settled)": dict(collections.Counter(str(m.get("can_close_early")) for m in settled)),
         "can_close_early (open)": dict(collections.Counter(str(m.get("can_close_early")) for m in open_))}
    vs_exp, vs_latest, vs_strike, exp_after_open = [], [], [], 0
    results = collections.defaultdict(collections.Counter)
    for m in settled:
        close, exp, latest = ts(m.get("close_time")), ts(m.get("expected_expiration_time")), ts(m.get("latest_expiration_time"))
        opened, st = ts(m.get("open_time")), strike.get(m.get("event_ticker"))
        if close and exp:
            vs_exp.append(close - exp)
            when = "closed 1h+ before expected" if close < exp - 3600 else (
                "closed 1h+ after expected" if close > exp + 3600 else "closed within 1h of expected")
            results[when][str(m.get("result"))] += 1
        if close and latest:
            vs_latest.append(close - latest)
        if close and st:
            vs_strike.append(close - st)
        if opened and exp and exp > opened:
            exp_after_open += 1
    r["settled: close - expected_expiration (hours)"] = spread(vs_exp)
    r["settled: close - latest_expiration (hours)"] = spread(vs_latest)
    r["settled: close - event strike_date (hours)"] = spread(vs_strike)
    r["settled: expected_expiration after open_time"] = exp_after_open
    r["settled: result by when it closed"] = {k: dict(v) for k, v in results.items()}
    r["open: close - expected_expiration (hours)"] = spread(
        [ts(m["close_time"]) - ts(m["expected_expiration_time"]) for m in open_
         if ts(m.get("close_time")) and ts(m.get("expected_expiration_time"))])
    r["open: expected_expiration - now (hours)"] = spread(
        [ts(m["expected_expiration_time"]) - NOW for m in open_ if ts(m.get("expected_expiration_time"))])
    keys = ("ticker", "can_close_early", "open_time", "close_time", "expected_expiration_time",
            "latest_expiration_time", "result")
    r["samples"] = ([{k: m.get(k) for k in keys} | {"strike_date": strike_raw.get(m.get("event_ticker"))}
                     for m in settled[:2]] +
                    [{k: m.get(k) for k in keys[:-1]} | {"status": "open"} for m in open_[:1]])
    out[s] = r
    print(f"\n===== {s}\n" + json.dumps(r, indent=1, default=str), flush=True)

print("\n===== API calls (path, HTTP code)\n" + json.dumps(dict(calls), indent=1))
