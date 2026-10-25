"""Side-branch probe (probe/xvenue, never merged). Public data only: no key, no orders.

Round 3: why were some big college games missing? Try /markets with the moneyline
filter (lighter than /events), without the event endDate window. Collect every
moneyline market per league listed since ~5 months ago, closed or not, plus teams.
"""
import gzip
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
NOW = int(time.time())
S = requests.Session()
S.headers["User-Agent"] = "kal-research-probe/1.0"
calls = Counter()


def get(path, **params):
    code, body = None, None
    for attempt in range(4):
        time.sleep(0.1 + attempt * 1.5)
        try:
            r = S.get(GAMMA + path, params=params, timeout=40)
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
    calls[code] += 1
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


code, sports = get("/sports")
by_code = {s.get("sport"): s for s in sports}
LEAGUES = ["nfl", "nba", "mlb", "nhl", "cfb", "wnba", "mls", "epl", "lal", "bun", "sea", "fl1", "ucl"]
for c in LEAGUES:
    s = by_code[c]
    print(c, "primaryTagId", s.get("primaryTagId"), "tags", s.get("tags"), "series", s.get("series"))

section("1. where is Alabama vs Tennessee (Oct 17)? event endDate vs market dates")
for q in ("Alabama Tennessee", "Texas Ole Miss", "Georgia Auburn"):
    code, d = get("/public-search", q=q, limit_per_type=5, events_status="all")
    evs = (d or {}).get("events") if isinstance(d, dict) else None
    print(q, code, [(e.get("slug"), e.get("endDate"), e.get("startTime"), e.get("closed"),
                     [str(x.get("id")) for x in e.get("series") or []][:2]) for e in evs or []][:5])

section("2. /markets moneyline per league, by listing-date windows")
rows, teams_all = [], []
for c in LEAGUES:
    s = by_code[c]
    tag = s.get("primaryTagId") or [t for t in str(s.get("tags")).split(",") if t not in ("1", "100639", "100350")][0]
    got, capped = {}, 0
    for closed in ("true", "false"):
        start = NOW - 150 * 86400
        while start < NOW:
            end = start + 15 * 86400
            offset = 0
            while True:
                code, page = get("/markets", tag_id=tag, sports_market_types="moneyline", closed=closed, limit=100,
                                 offset=offset, start_date_min=iso(start), start_date_max=iso(end))
                if code != 200 or not isinstance(page, list):
                    print(c, closed, iso(start), "->", code, short(page, 200))
                    break
                for m in page:
                    got.setdefault(m.get("id"), m)
                if len(page) < 100:
                    break
                offset += 100
                if offset >= 2000:
                    capped += 1
                    break
            start = end
    games = [m for m in got.values() if m.get("gameStartTime")]
    recent = [m for m in games if (m.get("gameStartTime") or "") >= iso(NOW - 100 * 86400)[:10]]
    print(f"{c} tag {tag}: {len(got)} moneyline markets, {len(games)} with gameStartTime, {len(recent)} since 100 days; "
          f"capped windows {capped}", flush=True)
    if got:
        m = next(iter(got.values()))
        ev = (m.get("events") or [{}])[0]
        print("   event keys in /markets:", sorted(ev.keys()))
    for m in recent:
        ev = (m.get("events") or [{}])[0]
        rows.append({"league": c, "id": m.get("id"), "slug": m.get("slug"), "question": m.get("question"),
                     "outcomes": m.get("outcomes"), "clobTokenIds": m.get("clobTokenIds"),
                     "outcomePrices": m.get("outcomePrices"), "groupItemTitle": m.get("groupItemTitle"),
                     "gameStartTime": m.get("gameStartTime"), "startDate": m.get("startDate"),
                     "endDate": m.get("endDate"), "closed": m.get("closed"),
                     "umaResolutionStatus": m.get("umaResolutionStatus"),
                     "event_id": ev.get("id"), "event_slug": ev.get("slug"), "title": ev.get("title"),
                     "eventDate": ev.get("eventDate"), "startTime": ev.get("startTime"),
                     "event_endDate": ev.get("endDate"),
                     "teams": [{k: t.get(k) for k in ("id", "name", "abbreviation", "alias")} for t in ev.get("teams") or []]})
    # the games round 2 missed
    for want in ("ala", "tenn", "aub"):
        hit = [m.get("slug") for m in recent if f"-{want}-" in (m.get("slug") or "")][:6]
        if c == "cfb":
            print("   ", want, hit)
    offset = 0
    while True:
        code, page = get("/teams", league=c, limit=100, offset=offset)
        if code != 200 or not isinstance(page, list) or not page:
            break
        teams_all += [{"league": c, **{k: t.get(k) for k in ("id", "name", "abbreviation", "alias")}} for t in page]
        offset += 100
        if offset > 3000:
            break
    print(f"   teams: {sum(1 for t in teams_all if t['league'] == c)}")
save("poly_markets", rows)
save("poly_teams_all", teams_all)
print("\ncalls:", dict(calls))
print("DONE", flush=True)
