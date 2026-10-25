"""Do the anchor times hold their listing values when a market closes early?

`snap`: record every open market whose event is due in the next 30 hours (its times,
and its event's strike_date), as "SNAP {json}" lines. `compare FILE`: fetch the same
markets again later and count, per field, how many changed, and how many changed to
equal the market's actual (early) close. Public data only: no key, no orders.
"""
import collections
import json
import sys
import time
from datetime import datetime, timezone

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
FIELDS = ("occurrence_datetime", "expected_expiration_time", "latest_expiration_time", "close_time")
calls = collections.Counter()


def get(path, **params):
    code = None
    for attempt in range(4):
        time.sleep(0.6 + 2 * attempt)  # stay under the public rate limit
        try:
            r = requests.get(BASE + path, params=params, timeout=30)
        except requests.RequestException as e:
            code = type(e).__name__
            continue
        code = r.status_code
        if code == 429:
            continue
        calls[f"{path.split('/')[1]} {code}"] += 1
        try:
            return code, r.json()
        except ValueError:
            return code, {}
    calls[f"{path.split('/')[1]} {code}"] += 1
    return code, {}


def ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() if v else None
    except ValueError:
        return None


def snap():
    now, cursor, n_events, kept = time.time(), None, 0, 0
    for _ in range(60):
        params = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        code, d = get("/events", **params)
        evs = (d or {}).get("events") or []
        n_events += len(evs)
        for e in evs:
            if str(e.get("event_ticker", "")).startswith("KXMVE"):
                continue
            ms = [m for m in e.get("markets") or [] if isinstance(m, dict)]
            due = [ts(m.get("occurrence_datetime") or m.get("close_time")) for m in ms]
            if not any(t and now < t < now + 30 * 3600 for t in due):
                continue
            for m in ms:
                kept += 1
                print("SNAP " + json.dumps({
                    "ticker": m.get("ticker"), "event": e.get("event_ticker"), "series": e.get("series_ticker"),
                    "category": e.get("category"), "strike_date": e.get("strike_date"),
                    "can_close_early": m.get("can_close_early"),
                    "early_close_condition": str(m.get("early_close_condition"))[:70],
                    **{f: m.get(f) for f in FIELDS}}, separators=(",", ":")))
        cursor = (d or {}).get("cursor")
        if code != 200 or not cursor:
            break
    print(f"\nsnapshot: {kept} markets from {n_events} open events, at {datetime.now(timezone.utc).isoformat()}")
    print(json.dumps(dict(calls)))


def compare(path):
    rows = [json.loads(line) for line in open(path) if line.strip()]
    now = time.time()
    by_ticker = {r["ticker"]: r for r in rows}
    current = {}
    tickers = list(by_ticker)
    for i in range(0, len(tickers), 100):
        _, d = get("/markets", tickers=",".join(tickers[i:i + 100]), limit=100)
        for m in (d or {}).get("markets") or []:
            current[m.get("ticker")] = m
    for tk in tickers:  # settled ones may have moved to /historical
        if tk not in current:
            code, d = get(f"/historical/markets/{tk}")
            if code == 200 and (d or {}).get("market"):
                current[tk] = d["market"]
    events = {}
    for ev in sorted({r["event"] for r in rows if r["strike_date"]}):
        code, d = get(f"/events/{ev}")
        if code == 200:
            events[ev] = (d or {}).get("event") or {}

    out = collections.defaultdict(collections.Counter)
    examples = collections.defaultdict(list)
    for tk, was in by_ticker.items():
        m = current.get(tk)
        if not m:
            out["all"]["not found again"] += 1
            continue
        close = ts(m.get("close_time"))
        if not close or close > now:
            out["all"]["still open"] += 1
            continue
        anchor = ts(was["strike_date"]) or ts(was["occurrence_datetime"])  # the bot's anchor, as listed
        kind = ("no anchor" if not anchor else "closed 1h+ before its anchor" if close < anchor - 3600
                else "closed within 1h of its anchor, or later")
        out[kind]["markets"] += 1
        checks = {f: (was[f], m.get(f)) for f in ("occurrence_datetime", "expected_expiration_time",
                                                     "latest_expiration_time")}
        if was["strike_date"]:
            checks["strike_date"] = (was["strike_date"], (events.get(was["event"]) or {}).get("strike_date"))
        for f, (before, after) in checks.items():
            if before is None:
                continue
            if ts(after) == ts(before):
                out[kind][f"{f}: same as listed"] += 1
            else:
                to_close = ts(after) is not None and abs(ts(after) - close) < 1
                out[kind][f"{f}: changed" + (" TO THE ACTUAL CLOSE" if to_close else "")] += 1
                if len(examples[f]) < 5:
                    examples[f].append({"ticker": tk, "listed": before, "now": after, "close": m.get("close_time"),
                                        "result": m.get("result")})
    print(json.dumps({k: dict(v) for k, v in out.items()}, indent=1))
    print("examples of changes:\n" + json.dumps(examples, indent=1))
    print(json.dumps(dict(calls)))


if __name__ == "__main__":
    compare(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[1] == "compare" else snap()
