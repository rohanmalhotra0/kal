"""Are anchor times rewritten when a market closes early? Settled data, sibling test.

Markets in one event share their listing times. Take each field's most common value in
an event as its listing value. A market that closed 1h+ before that value closed early.
If the field was rewritten on the early close, that market's value equals its own close
instead of the shared one. Count, per field: early closers that kept the shared value,
that show their own close, and that show something else.
Public data only: no key, no orders.
"""
import collections
import json
import time
from datetime import datetime, timezone

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = ["KXMLBTOTAL", "KXNBAPTS", "KXNBAREB", "KXNBA3PT", "KXNBAAST", "KXNFLTD", "KXNFLANYTD",
          "KXNFLGAME", "KXNFLSPREAD", "KXNBAGAME", "KXNHLGAME", "KXMLBGAME", "KXEPLGAME",
          "KXFEDMENTION", "KXRAIN", "KXDJI", "KXLLM1", "KXRT", "KXHURCAT", "KXHIGHNY", "KXBTCD",
          "KXINX", "KXTRUMPAPPROVE"]
calls = collections.Counter()


def get(path, **params):
    code = None
    for attempt in range(4):
        time.sleep(0.6 + 2 * attempt)
        try:
            r = requests.get(BASE + path, params=params, timeout=30)
        except requests.RequestException as e:
            code = type(e).__name__
            continue
        code = r.status_code
        if code == 429:
            continue
        calls[f"{path} {code}"] += 1
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


_, d = get("/series", category="Mentions")
SERIES += [x["ticker"] for x in (d or {}).get("series") or [] if x.get("ticker")][:15]

total = collections.defaultdict(collections.Counter)
per_series = {}
examples = collections.defaultdict(list)
for s in SERIES:
    ms = {}
    for path, extra in (("/markets", {"status": "settled"}), ("/historical/markets", {})):
        _, d = get(path, series_ticker=s, limit=1000, **extra)
        for m in (d or {}).get("markets") or []:
            ms.setdefault(m.get("ticker"), m)
    _, d = get("/events", series_ticker=s, status="settled", limit=200)
    strike = {e.get("event_ticker"): e.get("strike_date") for e in (d or {}).get("events") or []}
    by_event = collections.defaultdict(list)
    for m in ms.values():
        by_event[m.get("event_ticker")].append(m)
    out = collections.defaultdict(collections.Counter)
    for ev, group in by_event.items():
        for f in ("occurrence_datetime", "expected_expiration_time"):
            vals = [ts(m.get(f)) for m in group]
            known = [v for v in vals if v]
            if len(known) < 2:
                continue
            listed, n = collections.Counter(known).most_common(1)[0]
            if n < 2:
                continue  # no shared value to compare with
            for m, v in zip(group, vals):
                close = ts(m.get("close_time"))
                if not v or not close or close >= listed - 3600:
                    continue
                kind = "kept the shared value" if v == listed else (
                    "SHOWS ITS OWN CLOSE" if abs(v - close) < 1 else "something else")
                out[f]["early closers"] += 1
                out[f][kind] += 1
                if kind != "kept the shared value" and len(examples[f]) < 6:
                    examples[f].append({"ticker": m.get("ticker"), "close": m.get("close_time"), f: m.get(f),
                                        "shared": datetime.fromtimestamp(listed, timezone.utc).isoformat(),
                                        "result": m.get("result")})
        st = ts(strike.get(ev))
        closes = sorted(c for c in (ts(m.get("close_time")) for m in group) if c)
        if st and len(closes) >= 2:
            # strike_date is one value per event. If some markets closed early and it equals
            # the earliest of those closes, it may have been moved to the news time.
            if closes[-1] - closes[0] > 3600:
                out["strike_date"]["events where some markets closed 1h+ before others"] += 1
                out["strike_date"]["...and strike_date EQUALS THE EARLIEST CLOSE" if abs(st - closes[0]) < 1
                                   else "...and strike_date is not the earliest close"] += 1
            else:
                out["strike_date"]["events whose markets closed together"] += 1
    # A time set after the fact looks like 19:56:17 (seconds), a minute after the real close.
    # A listing time looks like 03:15:00. Count both shapes per field.
    for f in ("occurrence_datetime", "expected_expiration_time"):
        for m in ms.values():
            v, close = ts(m.get(f)), ts(m.get("close_time"))
            if not v or not close:
                continue
            out[f + " shape"]["has seconds" if v % 60 else "whole minute"] += 1
            if 0 <= v - close <= 300:
                out[f + " shape"]["0-5 min after the close"] += 1
                if v % 60:
                    out[f + " shape"]["0-5 min after the close, with seconds"] += 1
    per_series[s] = {f: dict(c) for f, c in out.items()}
    for f, c in out.items():
        total[f].update(c)
    print(f"{s}: {len(ms)} settled markets, {len(by_event)} events: {json.dumps(per_series[s])}", flush=True)

print("\n===== TOTAL\n" + json.dumps({f: dict(c) for f, c in total.items()}, indent=1))
print("\n===== examples of early closers that didn't keep the shared value\n" + json.dumps(examples, indent=1))
print(json.dumps(dict(calls)))
