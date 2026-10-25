"""Side-branch probe (probe/xvenue, never merged). Public data only: no key, no orders.

1. Can a GitHub runner reach Polymarket's public APIs (gamma, clob, data-api)?
2. What do settled game markets look like on both venues (fields, team names, dates)?
3. Does clob prices-history return hourly points for closed markets?
"""
import json
import sys
import time
from datetime import datetime, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
NOW = int(time.time())
DAYS = 95


def get(base, path, **params):
    code, body = None, None
    for attempt in range(3):
        time.sleep(0.25 + attempt)
        try:
            r = requests.get(base + path, params=params, timeout=30,
                             headers={"User-Agent": "kal-research-probe/1.0"})
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
        return code, body
    return code, body


def short(x, n=600):
    s = json.dumps(x, default=str) if not isinstance(x, str) else x
    return s if len(s) <= n else s[:n] + f"...(+{len(s) - n})"


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def section(t):
    print(f"\n===== {t} =====", flush=True)


# ---------- 1. reachability ----------
section("reachability")
for base, path, params in [(GAMMA, "/sports", {}), (GAMMA, "/events", {"limit": 1}),
                           (CLOB, "/time", {}), (DATA, "/trades", {"limit": 1}),
                           (KALSHI, "/exchange/status", {})]:
    code, body = get(base, path, **params)
    print(f"{base}{path} -> {code}: {short(body, 300)}")

code, sports = get(GAMMA, "/sports")
if code != 200 or not isinstance(sports, list):
    print("POLYMARKET GAMMA NOT REACHABLE. Stop.")
    sys.exit(0)

section("gamma /sports (all)")
for s in sports:
    print(short({k: s.get(k) for k in ("id", "sport", "series", "tags", "ordering", "resolution")}, 250))

WANT = ["nfl", "nba", "mlb", "nhl", "cfb", "ncaaf", "epl", "lal", "laliga", "bun", "sea", "fl1", "ucl", "mls",
        "wnba", "cbb"]
by_code = {s.get("sport"): s for s in sports}
picked = [c for c in WANT if c in by_code]
print("picked:", picked)

# ---------- 2. teams ----------
section("gamma /teams samples")
for c in picked[:6]:
    code, teams = get(GAMMA, "/teams", league=c, limit=5)
    print(c, code, short(teams, 900))

# ---------- 3. closed game events per league ----------
section("gamma closed events per league")
samples = {}
for c in picked:
    s = by_code[c]
    series_id = str(s.get("series") or "").split(",")[0]
    params = {"series_id": series_id, "closed": "true", "limit": 100, "order": "startDate", "ascending": "false",
              "end_date_min": iso(NOW - DAYS * 86400)}
    events, offset = [], 0
    for _ in range(30):
        code, page = get(GAMMA, "/events", offset=offset, **params)
        if code != 200 or not isinstance(page, list):
            print(c, "events page", offset, "->", code, short(page, 200))
            break
        events += page
        if len(page) < params["limit"]:
            break
        offset += params["limit"]
    ml = 0
    for e in events:
        if any(m.get("sportsMarketType") == "moneyline" for m in e.get("markets") or []):
            ml += 1
    print(f"{c}: series {series_id}: {len(events)} closed events since {iso(NOW - DAYS * 86400)[:10]}, "
          f"{ml} with a moneyline market")
    samples[c] = events[:40]
    for e in events[:3]:
        keys = ("id", "ticker", "slug", "title", "startDate", "endDate", "startTime", "eventDate", "gameId",
                "closed", "score", "period", "ended", "volume")
        print("  EVENT", short({k: e.get(k) for k in keys}, 700))
        mkts = e.get("markets") or []
        print("   market types:", sorted({str(m.get("sportsMarketType")) for m in mkts}), len(mkts))
        for m in mkts:
            if m.get("sportsMarketType") in ("moneyline", None) or len(mkts) <= 3:
                mk = ("id", "question", "slug", "conditionId", "outcomes", "outcomePrices", "clobTokenIds",
                      "sportsMarketType", "gameStartTime", "groupItemTitle", "closedTime", "umaResolutionStatus",
                      "startDate", "endDate", "volumeNum", "line", "teamAID", "teamBID")
                print("   MKT", short({k: m.get(k) for k in mk}, 900))
                break
    # all keys once
    if events:
        print("   event keys:", sorted(events[0].keys()))
        if events[0].get("markets"):
            print("   market keys:", sorted(events[0]["markets"][0].keys()))

# ---------- 4. prices-history on closed moneyline tokens ----------
section("clob prices-history on closed markets")
tried = 0
for c in picked:
    for e in samples.get(c, [])[:20]:
        m = next((m for m in e.get("markets") or [] if m.get("sportsMarketType") == "moneyline"), None)
        if not m:
            continue
        try:
            tokens = json.loads(m.get("clobTokenIds") or "[]")
        except ValueError:
            tokens = []
        if not tokens:
            continue
        start = m.get("gameStartTime") or e.get("startTime")
        try:
            st = int(datetime.fromisoformat(str(start).replace("Z", "+00:00").replace(" ", "T")).timestamp())
        except ValueError:
            st = NOW - 3 * 86400
        print(f"\n{c} {e.get('slug')} start {start} token {tokens[0][:16]}...")
        variants = [("start/end fid60", {"startTs": st - 48 * 3600, "endTs": st, "fidelity": 60}),
                    ("start/end fid10", {"startTs": st - 12 * 3600, "endTs": st, "fidelity": 10}),
                    ("start/end nofid", {"startTs": st - 48 * 3600, "endTs": st}),
                    ("interval max fid60", {"interval": "max", "fidelity": 60}),
                    ("start/end fid720", {"startTs": st - 7 * 86400, "endTs": st, "fidelity": 720})]
        for name, p in variants:
            code, d = get(CLOB, "/prices-history", market=tokens[0], **p)
            h = d.get("history") if isinstance(d, dict) else None
            if isinstance(h, list) and h:
                print(f"  {name}: {code} {len(h)} pts first {h[0]} last {h[-1]}")
            else:
                print(f"  {name}: {code} {short(d, 200)}")
        code, d = get(DATA, "/trades", market=m.get("conditionId"), limit=5)
        print("  data-api trades:", code, short(d, 500))
        tried += 1
        break
    if tried >= 6:
        break

# ---------- 5. Kalshi game series ----------
section("Kalshi sports series with GAME")
code, d = get(KALSHI, "/series", category="Sports")
series = d.get("series") if isinstance(d, dict) else []
games = sorted(s["ticker"] for s in series or [] if s.get("ticker", "").endswith("GAME"))
print(code, len(series or []), "sports series;", len(games), "GAME series")
print(" ".join(games))
KS = ["KXNFLGAME", "KXNBAGAME", "KXMLBGAME", "KXNHLGAME", "KXNCAAFGAME", "KXEPLGAME", "KXLALIGAGAME",
      "KXSERIEAGAME", "KXBUNDESLIGAGAME", "KXLIGUE1GAME", "KXUCLGAME", "KXMLSGAME", "KXWNBAGAME"]
for s in [s for s in series or [] if s.get("ticker") in KS]:
    print(short({k: s.get(k) for k in ("ticker", "title", "frequency", "fee_type", "fee_multiplier", "tags")}, 300))

section("Kalshi settled game markets (last 95 days)")
code, cut = get(KALSHI, "/historical/cutoff")
print("historical cutoff:", code, short(cut, 300))
for tk in KS:
    if tk not in games:
        print(tk, "not a series")
        continue
    out, cursor = [], None
    for _ in range(20):
        params = {"series_ticker": tk, "status": "settled", "limit": 1000, "min_close_ts": NOW - DAYS * 86400}
        if cursor:
            params["cursor"] = cursor
        code, page = get(KALSHI, "/markets", **params)
        if code != 200:
            print(tk, "markets ->", code, short(page, 200))
            break
        out += page.get("markets") or []
        cursor = page.get("cursor")
        if not cursor:
            break
    events = {m.get("event_ticker") for m in out}
    print(f"{tk}: {len(out)} settled markets, {len(events)} events")
    for m in out[:2]:
        keys = ("ticker", "event_ticker", "title", "subtitle", "yes_sub_title", "no_sub_title", "open_time",
                "close_time", "occurrence_datetime", "expected_expiration_time", "can_close_early", "result",
                "settlement_value_dollars", "volume_fp", "volume", "custom_strike", "status", "market_type")
        print("  MKT", short({k: m.get(k) for k in keys}, 900))
        print("  rules:", short(m.get("rules_primary") or "", 300))
    if out:
        m = out[0]
        t1 = m.get("occurrence_datetime") or m.get("close_time")
        try:
            t = int(datetime.fromisoformat(t1.replace("Z", "+00:00")).timestamp()) - 6 * 3600
            code, c = get(KALSHI, f"/series/{tk}/markets/{m['ticker']}/candlesticks",
                          start_ts=t - 24 * 3600, end_ts=t, period_interval=60)
            cs = c.get("candlesticks") if isinstance(c, dict) else None
            print("  candles:", code, len(cs or []), short((cs or [None])[-1], 500))
        except Exception as e:
            print("  candles failed:", e)

print("\nDONE", flush=True)
