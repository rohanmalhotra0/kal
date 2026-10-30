"""Does each Kalshi environment have the historical store? (public calls only; probe)"""
import requests

S = requests.Session()
for name, base in (("prod", "https://api.elections.kalshi.com/trade-api/v2"),
                   ("demo", "https://demo-api.kalshi.co/trade-api/v2")):
    for path in ("/historical/cutoff", "/historical/markets?limit=1", "/historical/fills?limit=1",
                 "/historical/orders?limit=1", "/portfolio/fills?limit=1",
                 "/historical/markets/KXHIGHNY-26AUG01-B85.5"):
        try:
            r = S.get(base + path, timeout=30)
            print(f"{name:5} {path:45} {r.status_code} {r.text[:300]!r}")
        except Exception as e:
            print(f"{name:5} {path:45} {e}")
    # a market settled before the cutoff: live vs historical lookups
    try:
        r = S.get(base + "/historical/markets", params={"limit": 2}, timeout=30)
        ms = (r.json() or {}).get("markets") or []
        for m in ms[:2]:
            t = m.get("ticker")
            a = S.get(f"{base}/markets/{t}", timeout=30)
            b = S.get(f"{base}/historical/markets/{t}", timeout=30)
            keep = {k: (b.json().get("market") or {}).get(k) for k in
                    ("ticker", "status", "result", "settlement_value_dollars", "open_time", "close_time")}
            print(f"{name:5} archived {t}: live -> {a.status_code}, historical -> {b.status_code} {keep}")
    except Exception as e:
        print(name, "archived lookup:", e)
