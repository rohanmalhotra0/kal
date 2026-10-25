"""Side-branch probe (probe/xvenue, never merged). Public data only: no key, no orders.

Round 2: collect settled game-winner markets on both venues for the last ~100 days,
so the matching can be built on real names and dates. Writes compact JSON to
probe-data/ (pushed back to this branch) and prints samples and counts.
"""
import gzip
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
NOW = int(time.time())
DAYS = 100
SINCE = NOW - DAYS * 86400
S = requests.Session()
S.headers["User-Agent"] = "kal-research-probe/1.0"
calls = Counter()


def get(base, path, **params):
    code, body = None, None
    for attempt in range(4):
        time.sleep(0.12 + attempt * 1.5)
        try:
            r = S.get(base + path, params=params, timeout=40)
        except requests.RequestException as e:
            code, body = type(e).__name__, str(e)[:300]
            continue
        code = r.status_code
        try:
            body = r.json()
        except ValueError:
            body = r.text[:500]
        if code in (429, 500, 502, 503, 504):
            continue
        break
    calls[f"{base.split('//')[1].split('.')[0]} {code}"] += 1
    return code, body


def short(x, n=600):
    s = json.dumps(x, default=str) if not isinstance(x, str) else x
    return s if len(s) <= n else s[:n] + f"...(+{len(s) - n})"


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def section(t):
    print(f"\n===== {t} =====", flush=True)


os.makedirs("probe-data", exist_ok=True)


def save(name, rows):
    with gzip.open(f"probe-data/{name}.json.gz", "wt") as f:
        json.dump(rows, f, separators=(",", ":"))
    print(f"saved {name}: {len(rows)} rows, {os.path.getsize(f'probe-data/{name}.json.gz')} bytes")


code, sports = get(GAMMA, "/sports")
by_code = {s.get("sport"): s for s in sports}
LEAGUES = ["nfl", "nba", "mlb", "nhl", "cfb", "wnba", "mls", "epl", "lal", "bun", "sea", "fl1", "ucl"]
for c in LEAGUES:
    print(c, short(by_code.get(c), 300))

# ---------- A. filters that skip props events ----------
section("A. /markets filter by sports_market_types")
for c in ("nhl", "mlb", "epl"):
    s = by_code[c]
    tags = [t for t in str(s.get("tags") or "").split(",") if t and t not in ("1", "100639", "100350")]
    for name, params in [
        ("tag+type", {"tag_id": tags[0] if tags else "", "sports_market_types": "moneyline", "closed": "true",
                      "limit": 5, "end_date_min": iso(SINCE)}),
        ("series+type", {"series_id": str(s.get("series")), "sports_market_types": "moneyline", "closed": "true",
                         "limit": 5, "end_date_min": iso(SINCE)}),
        ("type only", {"sports_market_types": "moneyline", "closed": "true", "limit": 5,
                       "end_date_min": iso(NOW - 3 * 86400)}),
    ]:
        code, d = get(GAMMA, "/markets", **params)
        if isinstance(d, list):
            print(f"{c} {name} {params.get('tag_id', '')}: {code} {len(d)} markets; types",
                  Counter(m.get("sportsMarketType") for m in d), [m.get("slug") for m in d][:5])
            if d:
                print("   keys:", sorted(d[0].keys())[:80])
                print("   events field:", short(d[0].get("events"), 600))
        else:
            print(f"{c} {name}: {code} {short(d, 300)}")

section("A2. /events/keyset")
code, d = get(GAMMA, "/events/keyset", series_id=str(by_code["mlb"].get("series")), closed="true", limit=3)
print(code, short(d if not isinstance(d, dict) else {k: (v if k != "events" else [e.get("slug") for e in v])
                                                    for k, v in d.items()}, 800))

section("A3. event teams field")
code, d = get(GAMMA, "/events", series_id=str(by_code["nhl"].get("series")), closed="true", limit=2,
              order="startDate", ascending="false")
for e in d if isinstance(d, list) else []:
    print(e.get("slug"), short(e.get("teams"), 1500))
code, d = get(GAMMA, "/events", series_id=str(by_code["epl"].get("series")), closed="true", limit=60,
              order="startDate", ascending="false")
for e in d if isinstance(d, list) else []:
    ml = [m for m in e.get("markets") or [] if m.get("sportsMarketType") == "moneyline"]
    if ml:
        print("EPL", e.get("slug"), e.get("title"), e.get("eventDate"), e.get("startTime"))
        print("   teams:", short(e.get("teams"), 800))
        for m in ml:
            print("   ML", short({k: m.get(k) for k in ("question", "slug", "outcomes", "outcomePrices",
                                                         "groupItemTitle", "gameStartTime", "negRisk")}, 500))
        break


# ---------- B. all moneyline markets per league, week by week ----------
section("B. Polymarket closed moneyline markets per league")


def compact_team(t):
    return {k: t.get(k) for k in ("id", "name", "abbreviation", "alias", "league")} if isinstance(t, dict) else t


poly = []
for c in LEAGUES:
    s = by_code.get(c)
    if not s:
        continue
    series_id = str(s.get("series") or "").split(",")[0]
    n_events, n_ml, seen = 0, 0, set()
    t = SINCE
    while t < NOW + 86400:
        offset = 0
        while True:
            code, page = get(GAMMA, "/events", series_id=series_id, closed="true", limit=100, offset=offset,
                             end_date_min=iso(t), end_date_max=iso(t + 4 * 86400))
            if code != 200 or not isinstance(page, list):
                print(c, iso(t), "->", code, short(page, 200))
                break
            for e in page:
                if e.get("id") in seen:
                    continue
                seen.add(e.get("id"))
                n_events += 1
                for m in e.get("markets") or []:
                    if m.get("sportsMarketType") != "moneyline":
                        continue
                    n_ml += 1
                    poly.append({
                        "league": c, "event_id": e.get("id"), "event_slug": e.get("slug"), "title": e.get("title"),
                        "eventDate": e.get("eventDate"), "startTime": e.get("startTime"), "endDate": e.get("endDate"),
                        "teams": [compact_team(x) for x in e.get("teams") or []],
                        "id": m.get("id"), "slug": m.get("slug"), "question": m.get("question"),
                        "outcomes": m.get("outcomes"), "clobTokenIds": m.get("clobTokenIds"),
                        "outcomePrices": m.get("outcomePrices"), "groupItemTitle": m.get("groupItemTitle"),
                        "gameStartTime": m.get("gameStartTime"), "umaResolutionStatus": m.get("umaResolutionStatus"),
                        "closedTime": m.get("closedTime"), "createdAt": m.get("createdAt"),
                        "startDate": m.get("startDate"), "negRisk": m.get("negRisk"), "volumeNum": m.get("volumeNum")})
            if len(page) < 100:
                break
            offset += 100
            if offset >= 2000:
                print(c, iso(t), "window too big")
                break
        t += 4 * 86400
    print(f"{c}: {n_events} closed events, {n_ml} moneyline markets", flush=True)
for p in poly[:3]:
    print("PM", short(p, 1200))
save("poly_moneyline", poly)

section("C. Polymarket teams")
teams = []
for c in LEAGUES:
    offset = 0
    while True:
        code, page = get(GAMMA, "/teams", league=c, limit=500, offset=offset)
        if code != 200 or not isinstance(page, list):
            print(c, "teams ->", code, short(page, 200))
            break
        teams += [{**compact_team(x), "league": c} for x in page]
        if len(page) < 500:
            break
        offset += 500
    print(c, sum(1 for x in teams if x["league"] == c), "teams")
save("poly_teams", teams)

# ---------- D. Kalshi settled game markets ----------
section("D. Kalshi settled game markets")
KS = ["KXNFLGAME", "KXNBAGAME", "KXMLBGAME", "KXNHLGAME", "KXNCAAFGAME", "KXWNBAGAME", "KXMLSGAME", "KXEPLGAME",
      "KXLALIGAGAME", "KXBUNDESLIGAGAME", "KXSERIEAGAME", "KXLIGUE1GAME", "KXUCLGAME"]
KEYS = ("ticker", "event_ticker", "title", "yes_sub_title", "open_time", "close_time", "occurrence_datetime",
        "expected_expiration_time", "can_close_early", "result", "settlement_value_dollars", "settlement_value",
        "volume_fp", "volume", "status", "market_type")
kal = []
for tk in KS:
    got = {}
    for path, extra in (("/markets", {"status": "settled", "min_close_ts": SINCE}), ("/historical/markets", {})):
        cursor, pages = None, 0
        while pages < 40:
            params = {"series_ticker": tk, "limit": 1000, **extra}
            if cursor:
                params["cursor"] = cursor
            code, page = get(KALSHI, path, **params)
            pages += 1
            if code != 200 or not isinstance(page, dict):
                print(tk, path, "->", code, short(page, 200))
                break
            ms = page.get("markets") or []
            for m in ms:
                if m.get("ticker") and m["ticker"] not in got:
                    got[m["ticker"]] = {k: m.get(k) for k in KEYS}
            cursor = page.get("cursor")
            if not cursor or not ms:
                break
        print(f"{tk} {path}: {pages} pages, {len(got)} markets so far", flush=True)
    recent = [m for m in got.values() if (m.get("close_time") or "") >= iso(SINCE)]
    print(f"{tk}: {len(got)} markets fetched, {len(recent)} closed since {iso(SINCE)[:10]}")
    kal += recent
for m in kal[:2]:
    print("KM", short(m, 800))
save("kalshi_games", kal)
print("\ncalls:", dict(calls))
print("DONE", flush=True)
