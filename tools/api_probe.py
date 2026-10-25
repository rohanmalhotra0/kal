"""Print how the real Kalshi API behaves, so strategies rest on facts, not guesses.

Run by .github/workflows/api-probe.yml (public endpoints only, no key, no orders).
"""
import collections
import json
import sys

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
FIELDS = ["ticker", "event_ticker", "status", "strike_type", "floor_strike", "cap_strike", "custom_strike",
          "yes_sub_title", "no_sub_title", "subtitle", "can_close_early", "open_time", "close_time",
          "expected_expiration_time", "latest_expiration_time", "yes_bid_dollars", "yes_ask_dollars",
          "no_bid_dollars", "no_ask_dollars", "last_price_dollars", "volume_24h_fp", "volume_fp",
          "result", "settlement_value_dollars", "market_type", "rules_primary"]


def get(path, **params):
    r = requests.get(BASE + path, params=params, timeout=30)
    try:
        body = r.json()
    except ValueError:
        body = r.text[:300]
    return r.status_code, body


def show(title, obj, limit=3000):
    print(f"\n===== {title}")
    print(json.dumps(obj, indent=1, default=str)[:limit])


def pick(m):
    out = {k: m.get(k) for k in FIELDS if k in m}
    if "rules_primary" in out and out["rules_primary"]:
        out["rules_primary"] = out["rules_primary"][:240]
    return out


code, d = get("/exchange/status")
show("exchange status", {"code": code, "body": d})

# Which order endpoints exist? With no key, an existing endpoint answers 401 and a removed
# one 404. Nothing can be placed without a key, so this is harmless.
for host in ("https://api.elections.kalshi.com", "https://external-api.kalshi.com"):
    for path in ("/trade-api/v2/portfolio/orders", "/trade-api/v2/portfolio/events/orders",
                 "/trade-api/v2/portfolio/balance"):
        try:
            if path.endswith("balance"):
                r = requests.get(host + path, timeout=20)
            else:
                r = requests.post(host + path, json={}, timeout=20)
            show(f"endpoint {'GET' if path.endswith('balance') else 'POST'} {host}{path}",
                 {"code": r.status_code, "body": r.text[:200]})
        except Exception as e:
            show(f"endpoint {host}{path}", {"error": str(e)[:200]})

# The demo (fake money) exchange the setup guide tells people to try first
for demo in ("https://demo-api.kalshi.co/trade-api/v2", "https://demo-api.elections.kalshi.com/trade-api/v2"):
    try:
        r = requests.get(demo + "/exchange/status", timeout=20)
        show(f"demo exchange {demo}", {"code": r.status_code, "body": r.text[:300]})
        r = requests.get(demo + "/markets", params={"limit": 2, "status": "open"}, timeout=20)
        show(f"demo markets {demo}", {"code": r.status_code,
                                      "tickers": [m.get("ticker") for m in r.json().get("markets", [])]
                                      if r.ok else r.text[:200]})
    except Exception as e:
        show(f"demo exchange {demo}", {"error": str(e)[:300]})

code, d = get("/markets", limit=3, status="open")
ms = d.get("markets", []) if isinstance(d, dict) else []
show("market keys (first open market)", sorted(ms[0].keys()) if ms else d)
show("open market sample", [pick(m) for m in ms])

if len(ms) >= 2:
    code, d2 = get("/markets", tickers=f"{ms[0]['ticker']},{ms[1]['ticker']}", limit=1000)
    got = [m["ticker"] for m in d2.get("markets", [])] if isinstance(d2, dict) else d2
    show("tickers= filter", {"code": code, "asked": [ms[0]["ticker"], ms[1]["ticker"]], "got_count": len(got),
                             "got_first": got[:5]})

code, d = get("/events", limit=200, status="open", with_nested_markets="true")
evs = d.get("events", []) if isinstance(d, dict) else []
show("event keys", sorted(evs[0].keys()) if evs else d)
types = collections.Counter(m.get("strike_type") for e in evs for m in e.get("markets", []))
show("strike_type counts (200 open events)", dict(types))
excl = collections.Counter(str(e.get("mutually_exclusive")) for e in evs)
show("mutually_exclusive counts", dict(excl))

# Batch and single lookups on NORMAL (non-combo) markets: the ledger depends on them
normal = [m["ticker"] for e in evs if not e["event_ticker"].startswith("KXMVE")
          for m in e.get("markets", [])][:3]
if normal:
    code, d3 = get("/markets", tickers=",".join(normal), limit=1000)
    got = [m["ticker"] for m in d3.get("markets", [])] if isinstance(d3, dict) else d3
    show("tickers= filter on normal markets", {"code": code, "asked": normal, "got": got})
    code, d4 = get(f"/markets/{normal[0]}")
    show("single market lookup", {"code": code, "keys": list(d4.keys()) if isinstance(d4, dict) else d4,
                                  "ticker": (d4.get("market") or {}).get("ticker") if isinstance(d4, dict) else None})
    ev0 = next(e for e in evs if any(m["ticker"] == normal[0] for m in e.get("markets", [])))
    code, d5 = get(f"/series/{ev0['series_ticker']}/markets/{normal[0]}/candlesticks",
                   start_ts=int(__import__("time").time()) - 86400, end_ts=int(__import__("time").time()),
                   period_interval=60)
    c0 = (d5.get("candlesticks") or [None])[-1] if isinstance(d5, dict) else None
    show("live candle format", {"code": code, "last": c0})
# settled normal markets: do they come back from status=settled?
code, d6 = get("/markets", limit=5, status="settled", mve_filter="exclude")
show("settled (no combos) sample", {"code": code, "statuses": [m.get("status") for m in d6.get("markets", [])]
                                     if isinstance(d6, dict) else d6,
                                     "results": [m.get("result") for m in d6.get("markets", [])]
                                     if isinstance(d6, dict) else None})

# Price ticks: which markets accept sub-cent prices?
structs = collections.Counter()
examples = {}
for e in evs:
    for m in e.get("markets", []):
        key = json.dumps(m.get("price_level_structure"), sort_keys=True, default=str)[:200]
        structs[key] += 1
        examples.setdefault(key, {"ticker": m.get("ticker"), "price_ranges": m.get("price_ranges"),
                                  "yes_bid": m.get("yes_bid_dollars"), "yes_ask": m.get("yes_ask_dollars")})
show("price_level_structure counts", dict(structs.most_common(10)))
show("price_level_structure examples", examples, 4000)

# Ladders: events with >= 3 'greater' markets, show wording and tickers
ladders = [e for e in evs if sum(m.get("strike_type") in ("greater", "greater_or_equal") for m in e.get("markets", [])) >= 3]
for e in ladders[:4]:
    show(f"ladder-ish event {e['event_ticker']} ({e.get('title')}) excl={e.get('mutually_exclusive')}",
         [pick(m) for m in e["markets"][:6]], 2500)

# Brackets: events mixing between/less/greater
brk = [e for e in evs if {"between"} & {m.get("strike_type") for m in e.get("markets", [])}]
for e in brk[:3]:
    show(f"bracket-ish event {e['event_ticker']} ({e.get('title')}) excl={e.get('mutually_exclusive')}",
         [pick(m) for m in e["markets"][:8]], 2500)

# Mutually exclusive: sum of YES bids per event (basket arb needs > $1)
sums = []
for e in evs:
    if e.get("mutually_exclusive") is True:
        bids = [float(m.get("yes_bid_dollars") or 0) for m in e.get("markets", []) if m.get("status") == "active"]
        sums.append((round(sum(bids), 4), len(bids), e["event_ticker"]))
sums.sort(reverse=True)
show("mutually exclusive events: top YES-bid sums", sums[:15])

# Settled markets
code, d = get("/markets", limit=5, status="settled")
show("settled sample", {"code": code, "markets": [pick(m) for m in (d.get("markets", []) if isinstance(d, dict) else [])]})

# Historical store
code, d = get("/historical/cutoff")
show("historical cutoff", {"code": code, "body": d})
code, d = get("/historical/markets", limit=2)
show("historical markets", {"code": code, "body": d if not isinstance(d, dict) else {
    "keys": list(d.keys()), "first": pick((d.get("markets") or [{}])[0])}})
old = "KXINTLFRIENDLYGAME-26JUN06MYAGUM-MYA"
for path in (f"/series/KXINTLFRIENDLYGAME/markets/{old}/candlesticks", f"/historical/markets/{old}/candlesticks"):
    code, d = get(path, start_ts=1780000000, end_ts=1781000000, period_interval=60)
    show(f"candles {path}", {"code": code, "n": len(d.get("candlesticks", [])) if isinstance(d, dict) else None,
                             "body_head": str(d)[:300]})

# Order book shape
if ms:
    code, d = get(f"/markets/{ms[0]['ticker']}/orderbook")
    show("orderbook shape", {"code": code, "keys": list(d.keys()) if isinstance(d, dict) else d,
                             "body_head": json.dumps(d)[:600]})

# Fees per series
code, d = get("/series", limit=2000)
ser = d.get("series", []) if isinstance(d, dict) else []
show("series keys", sorted(ser[0].keys()) if ser else d)
fee_types = collections.Counter((s.get("fee_type"), s.get("fee_multiplier")) for s in ser)
show("fee_type / fee_multiplier counts", {str(k): v for k, v in fee_types.most_common(20)})
cats = collections.Counter(s.get("category") for s in ser)
show("series categories", dict(cats.most_common(30)))
sys.exit(0)
