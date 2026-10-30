"""Probe: is a Kalshi market's event always its ticker less the last "-" part? Public data
only (no key, no orders). The bot would file put-back orders under that event."""
import time

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
s = requests.Session()


def pages(path, key, params, cap):
    params, n = dict(params), 0
    while n < cap:
        for attempt in range(5):
            r = s.get(BASE + path, params=params, timeout=30)
            if r.status_code != 429:
                break
            time.sleep(2 ** attempt)
        r.raise_for_status()
        d = r.json()
        n += 1
        yield d.get(key) or []
        if not d.get("cursor"):
            return
        params["cursor"] = d["cursor"]
        time.sleep(0.15)


def check(label, path, params, cap):
    total = prefix_ok = rsplit_ok = mve = 0
    bad = []
    for page in pages(path, "markets", params, cap):
        for m in page:
            t, e = m.get("ticker") or "", m.get("event_ticker") or ""
            total += 1
            if t.startswith("KXMVE"):
                mve += 1
                continue
            prefix_ok += t.startswith(e + "-")
            if t.rsplit("-", 1)[0] == e:
                rsplit_ok += 1
            elif len(bad) < 25:
                bad.append((t, e))
    print(f"{label}: {total} markets ({mve} KXMVE skipped); ticker starts with event+'-': {prefix_ok}; "
          f"ticker less last part == event: {rsplit_ok}; misses: {total - mve - rsplit_ok}")
    for t, e in bad:
        print(f"   miss: ticker={t!r} event={e!r}")


check("open", "/markets", {"limit": 1000, "status": "open"}, cap=400)
check("closed (sample)", "/markets", {"limit": 1000, "status": "closed"}, cap=40)
check("settled (sample)", "/markets", {"limit": 1000, "status": "settled"}, cap=40)
check("historical (sample)", "/historical/markets", {"limit": 1000}, cap=20)
