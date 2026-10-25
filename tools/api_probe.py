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
    checks = [("POST", "/trade-api/v2/portfolio/orders"), ("POST", "/trade-api/v2/portfolio/events/orders"),
              ("DELETE", "/trade-api/v2/portfolio/events/orders/00000000-0000-0000-0000-000000000000"),
              ("GET", "/trade-api/v2/portfolio/balance"), ("GET", "/trade-api/v2/portfolio/positions"),
              ("GET", "/trade-api/v2/portfolio/orders"), ("GET", "/trade-api/v2/portfolio/fills"),
              ("GET", "/trade-api/v2/portfolio/settlements"), ("GET", "/trade-api/v2/portfolio/events/orders")]
    for method, path in checks:
        if host != "https://api.elections.kalshi.com" and method == "GET":
            continue
        try:
            r = requests.request(method, host + path, json={} if method == "POST" else None, timeout=20)
            show(f"endpoint {method} {host}{path}", {"code": r.status_code, "body": r.text[:200]})
        except Exception as e:
            show(f"endpoint {method} {host}{path}", {"error": str(e)[:200]})

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

# What do non-yes/no settlements look like? (voids, scalars) Sample many settled markets.
results = collections.Counter()
odd = []
cursor = None
for page in range(8):
    params = {"limit": 1000}
    if cursor:
        params["cursor"] = cursor
    code, d = get("/historical/markets", **params)
    if not isinstance(d, dict):
        break
    for m in d.get("markets", []):
        key = (m.get("status"), m.get("result"), m.get("market_type"))
        results[str(key)] += 1
        if m.get("result") not in ("yes", "no") and len(odd) < 12:
            odd.append(pick(m))
    cursor = d.get("cursor")
    if not cursor:
        break
code, d = get("/markets", status="settled", limit=1000, mve_filter="exclude")
for m in (d.get("markets", []) if isinstance(d, dict) else []):
    results["recent " + str((m.get("status"), m.get("result"), m.get("market_type")))] += 1
    if m.get("result") not in ("yes", "no") and len(odd) < 20:
        odd.append(pick(m))
show("settlement shapes (status, result, market_type)", dict(results.most_common(25)))
show("non-yes/no settled examples", odd, 8000)

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

# ---- What can break a "locked-in" NO basket? (settlement floors, fair-price rules, netting)
events, cursor = [], None
for _ in range(6):
    params = {"status": "open", "with_nested_markets": "true", "limit": 200}
    if cursor:
        params["cursor"] = cursor
    code, d = get("/events", **params)
    if code != 200 or not isinstance(d, dict):
        show("events page failed", {"code": code, "body": str(d)[:300]})
        break
    events += d.get("events") or []
    cursor = d.get("cursor")
    if not cursor:
        break
excl = [e for e in events if e.get("mutually_exclusive")]
markets = [m for e in excl for m in e.get("markets") or []]
show("open events sampled", {"events": len(events), "mutually_exclusive": len(excl), "their markets": len(markets)})
event_keys, market_keys = collections.Counter(), collections.Counter()
for e in events:
    event_keys.update(k for k in e if k != "markets")
for e in events:
    for m in e.get("markets") or []:
        market_keys.update(m.keys())
show("event keys (count of events having each)", dict(event_keys.most_common()))
show("market keys (count of markets having each)", dict(market_keys.most_common()), limit=6000)
risky = [k for k in market_keys if any(w in k for w in ("settlement", "floor", "bound", "cap", "collateral", "fee"))]
values = {}
for k in risky:
    values[k] = collections.Counter(str(m.get(k))[:40] for e in events for m in e.get("markets") or [] if k in m).most_common(8)
show("values of settlement / floor / bound / collateral market fields", values, limit=6000)
show("collateral_return_type on events", collections.Counter(str(e.get("collateral_return_type")) for e in events).most_common(10))
words = ["last traded", "fair price", "fair value", "postpone", "cancel", "abandon", "suspend", "tie", "draw",
         "death", "dies", "outcome review", "settle at", "floor"]
hits = collections.Counter()
examples = collections.defaultdict(list)
for m in markets:
    text = f"{m.get('rules_primary') or ''} {m.get('rules_secondary') or ''}".lower()
    for w in words:
        if w in text:
            hits[w] += 1
            if len(examples[w]) < 2:
                i = text.find(w)
                examples[w].append(f"{m.get('ticker')}: ...{text[max(0, i - 160): i + 160]}...")
show(f"rule wording in {len(markets)} markets of mutually exclusive events", dict(hits.most_common()))
show("rule wording examples", dict(examples), limit=8000)
sys.exit(0)
