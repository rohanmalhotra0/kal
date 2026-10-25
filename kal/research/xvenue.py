"""Cross-venue research (idea #11): do Polymarket's prices tell us when Kalshi's are wrong?

Game-winner markets on both venues, matched game by game. Research only: it reads public
prices and places nothing. Kalshi is the only venue the bot would ever trade.

For each Kalshi market ("X wins", or "Tie" in soccer), at the decision time, 6 hours
before the market's scheduled end (Kalshi lists a game's end 3 hours after its start, so
this is about 3 hours before the start):
- Kalshi: the YES ask and bid of the last hourly candle that ended by then, and the
  contracts traded in the day before (busy = 200+, as the live strategies ask);
- Polymarket: the last price of X's token at or before that candle's end, so both
  prices are as of the same moment.

1. Calibration: whose price was closer to the result? (Brier score: lower is better.)
2. Trading: Polymarket above Kalshi's YES ask by more than a margin: buy YES at the ask.
   Below the YES bid by more than the margin: buy NO at 1 - bid. Kalshi's taker fee is
   paid, rounded up per order (backtest.trade). One league's game-day is one event.

No peeking:
- prices at or before the decision time only, on both venues. A Polymarket point counts
  only once its minute is over; later points are dropped even if the API sends them;
- the decision time comes from times Kalshi set when it listed the game, never from when
  a market really closed (game markets close when the game ends);
- games are matched on what was known before they started (league, teams, start time),
  never on how they ended or how much they traded in all;
- the decision must also come before Polymarket's listed start.
"""
import itertools
import json
import logging
import math
import re
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from ..client import UnknownSettlement, candle_close, ts, yes_payout
from ..strategies import favorites
from . import backtest

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# league -> (Kalshi series, Polymarket sport code)
LEAGUES = {
    "NFL": ("KXNFLGAME", "nfl"), "NBA": ("KXNBAGAME", "nba"), "MLB": ("KXMLBGAME", "mlb"),
    "NHL": ("KXNHLGAME", "nhl"), "CFB": ("KXNCAAFGAME", "cfb"), "WNBA": ("KXWNBAGAME", "wnba"),
    "MLS": ("KXMLSGAME", "mls"), "EPL": ("KXEPLGAME", "epl"), "La Liga": ("KXLALIGAGAME", "lal"),
    "Bundesliga": ("KXBUNDESLIGAGAME", "bun"), "Serie A": ("KXSERIEAGAME", "sea"),
    "Ligue 1": ("KXLIGUE1GAME", "fl1"), "UCL": ("KXUCLGAME", "ucl"),
}
SOCCER = ("MLS", "EPL", "La Liga", "Bundesliga", "Serie A", "Ligue 1", "UCL")   # one row in the tables

# Kalshi team code -> Polymarket abbreviation, where they differ (seen on real data,
# 2026-10-25). Needed where Polymarket names teams by nickname only ("Kings"), which
# Kalshi's city names ("Los Angeles") can't match.
CODES = {
    "nfl": {"jac": "jax", "lar": "la"},
    "mlb": {"ath": "oak", "az": "ari"},
    "nhl": {"cgy": "cal", "la": "lak", "mtl": "mon", "uta": "utah", "vgk": "las"},
    "wnba": {"gs": "gsv", "lv": "las", "ny": "nyl", "pdx": "por"},
}

# Kalshi team names Polymarket never uses (seen on real data, 2026-10-25): Kalshi name ->
# Polymarket name.
NAMES = {"bilbao": "Athletic Club", "santander": "Real Racing Club", "nijmegen": "NEC"}

H = 3600
START_TO_END = 3 * H      # Kalshi lists a game's end 3 hours after its start (99% of real matches)
WINDOW = 2 * H            # both venues' start times must agree this closely...
FAR, REMATCH = 12 * H, 36 * H   # ...unless Kalshi's start is a placeholder (see match)
PRE_START = 1800          # and the decision must come 30+ minutes before Polymarket's start
VOLUME_HOURS = 24         # busy = contracts traded in the day before the decision
BUSY = favorites.MIN_VOLUME_24H   # 200, as the live strategies ask
POINT = 60                # a Polymarket point stands for its minute: usable once that minute is over
MARGINS = (0.02, 0.04, 0.06)
SURE, SOME = 0.99, 0.5    # team-name agreement: certain, and enough
LISTED_BEFORE = 120 * 86400   # Polymarket lists games up to months ahead (NFL: at schedule release)

log = logging.getLogger(__name__)


def _time(v):
    """Unix time from Kalshi's ISO strings or Polymarket's "2026-10-25 14:00:00+00"."""
    if not v or not isinstance(v, str):
        return None
    s = v.strip().replace(" ", "T")
    if re.search(r"[+-]\d\d$", s):
        s += ":00"
    try:
        return ts(s)
    except (TypeError, ValueError, AttributeError):
        return None


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- the scheduled end (decision time anchor) ----------

def scheduled_end(m):
    """When a game market is due to end, from times Kalshi set when it listed the game, or
    None. The bot's rule (base.scheduled_end in PR #12; game events have no strike date),
    plus one fallback:
    1. close_time, if the market says it can't close early;
    2. occurrence_datetime, if on the whole minute (listed as start + 3 hours);
    3. expected_expiration_time, if occurrence_datetime is off the minute and later than it.
       After a long game (overtime) Kalshi rewrites occurrence_datetime to when it really
       ended; expected_expiration_time keeps the listed end. Skipping those games would
       drop the long ones: a filter known only after the game.
    Never close_time otherwise: a game market closes when the game ends."""
    if m.get("can_close_early") is False:
        t = _time(m.get("close_time"))
        if t:
            return t
    occ = _time(m.get("occurrence_datetime"))
    if occ and occ % 60 == 0:
        return occ
    exp = _time(m.get("expected_expiration_time"))
    if occ and exp and exp % 60 == 0 and exp < occ:
        return exp
    return None


# ---------- team names ----------

_SPECIAL = str.maketrans({"ø": "o", "Ø": "o", "ß": "ss", "æ": "ae", "Æ": "ae", "đ": "d", "Đ": "d",
                          "ł": "l", "Ł": "l", "ı": "i", "œ": "oe", "Œ": "oe"})
# Club words that say nothing about which team it is.
CLUB_WORDS = {"fc", "cf", "sc", "afc", "ac", "fk", "sv", "club", "de", "the", "cd", "ud", "rc", "ssc", "as",
              "us", "kv", "sk", "calcio", "fsv", "vfb", "vfl", "bv", "tsg", "fa", "gnk", "nk", "kf", "kks",
              "if", "aif", "ff", "bk", "osc", "cp", "reg", "time"}


def words(name):
    """A team name as plain words: "FK Bodø/Glimt" -> fk bodo glimt, "Be`er Sheva" -> beer
    sheva. "St." is "saint" first (St. Louis) and "state" after (Ohio St.)."""
    s = unicodedata.normalize("NFKD", (name or "").translate(_SPECIAL))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"^reg time:\s*", "", s)        # Kalshi's regulation-time markets
    s = re.sub(r"['`´’]", "", s).replace("&", " and ")
    out = []
    for i, w in enumerate(re.findall(r"[a-z0-9]+", s)):
        out.append(("saint" if i == 0 else "state") if w == "st" else w)
    return out


def _word_hit(w, others):
    for o in others:
        if o == w:
            return True
        if len(w) >= 3 and len(o) >= 3 and (o.startswith(w) or w.startswith(o)):
            return True            # Hamburg / Hamburger SV, Bodoe / Bodo
        if len(w) >= 5 and len(o) >= 5 and (w in o or o in w):
            return True            # M'gladbach / Monchengladbach
    n = len(w)                     # initials: "RB" = Red Bulls, "Los Angeles D" = Dodgers
    return n <= 3 and any("".join(o[0] for o in others[i:i + n]) == w for i in range(len(others) - n + 1))


def name_score(a, b):
    """How well two team names agree, 0 to 1: matched words over the longer name's real
    words (club words like FC don't count). "Texas" vs "Texas" is 1; vs "Texas Tech" 0.5."""
    wa, wb = words(a), words(b)
    ra = [w for w in wa if w not in CLUB_WORDS and not w.isdigit()]
    rb = [w for w in wb if w not in CLUB_WORDS and not w.isdigit()]
    if not ra or not rb:
        return 0.0
    hits = max(sum(_word_hit(w, wb) for w in ra), sum(_word_hit(w, wa) for w in rb))
    return hits / max(len(ra), len(rb))


def side_score(league, kside, pside):
    """How sure we are that a Kalshi market and a Polymarket outcome are the same team, 0 to 1.
    A draw only ever matches a draw."""
    if kside["draw"] or pside["draw"]:
        return 1.0 if kside["draw"] and pside["draw"] else 0.0
    code = kside["code"].lower()
    if pside.get("abbr") and CODES.get(league, {}).get(code, code) == pside["abbr"].lower():
        return 1.0
    mine = [kside["name"], NAMES.get(" ".join(words(kside["name"])))]
    return max([name_score(a, b) for a in mine if a for b in pside["names"] if b] + [0.0])


# ---------- games on each venue ----------

def kalshi_games(markets, league, series):
    """Kalshi's settled game markets grouped by event: [{"league", "series", "event", "end",
    "sides": [{"code", "name", "draw", "market"}]}]. A game whose markets show no scheduled
    end, or disagree on it, is left out."""
    by_event = defaultdict(list)
    for m in markets:
        if m.get("ticker") and m.get("event_ticker") and m["ticker"].startswith(m["event_ticker"] + "-"):
            by_event[m["event_ticker"]].append(m)
    out = []
    for ev, ms in sorted(by_event.items()):
        ends = {scheduled_end(m) for m in ms}
        if len(ends) != 1 or None in ends:
            continue
        sides = []
        for m in sorted(ms, key=lambda m: m["ticker"]):
            code = m["ticker"][len(ev) + 1:]
            sides.append({"code": code, "name": m.get("yes_sub_title") or "", "draw": code.upper() == "TIE",
                          "market": m})
        out.append({"league": league, "series": series, "event": ev, "end": ends.pop(), "sides": sides})
    return out


def _json_list(v):
    if isinstance(v, list):
        return v
    try:
        got = json.loads(v or "[]")
    except (TypeError, ValueError):
        return []
    return got if isinstance(got, list) else []


def _team_for(name, teams):
    """The Polymarket team object called `name` (its name, alias or abbreviation)."""
    w = words(name)
    for t in teams:
        if w and w in (words(t.get("name")), words(t.get("alias")), words(t.get("abbreviation"))):
            return t
    return None


def poly_games(markets, teams=()):
    """Polymarket moneyline markets grouped into games: [{"slug", "title", "start", "sides":
    [{"draw", "abbr", "names", "token", "final"}]}].

    Two shapes: one market whose outcomes are the two teams (US leagues), or one Yes/No
    market per outcome, the draw included (soccer). Each side keeps its own token, so a
    price is never read off the wrong team. `teams` is the league's team list (names,
    nicknames, abbreviations); the event's own team list is used first."""
    games = {}
    for m in markets:
        ev = (m.get("events") or [{}])[0] if isinstance(m.get("events"), list) else {}
        outcomes, tokens = _json_list(m.get("outcomes")), _json_list(m.get("clobTokenIds"))
        finals = _json_list(m.get("outcomePrices"))
        start = _time(m.get("gameStartTime")) or _time(ev.get("startTime"))
        if not start or len(tokens) != len(outcomes) or len(tokens) < 2:
            continue
        key = ev.get("id") or ev.get("slug") or (m.get("slug") or "").rsplit("-", 1)[0]
        g = games.setdefault(key, {"slug": ev.get("slug") or m.get("slug"), "title": ev.get("title") or m.get("question"),
                                   "start": start, "sides": [], "markets": 0})
        g["markets"] += 1
        known = list(ev.get("teams") or []) + list(teams)
        if [str(o).lower() for o in outcomes] == ["yes", "no"]:     # one market per outcome
            slug, base = m.get("slug") or "", ev.get("slug") or ""
            title = m.get("groupItemTitle") or ""
            draw = slug.endswith("-draw") or title.lower().startswith("draw")
            # "epl-hul-bre-2026-10-25-hul": the part after the game's slug is the team's code
            abbr = None if draw else (slug[len(base) + 1:] if base and slug.startswith(base + "-")
                                      else slug.rsplit("-", 1)[-1])
            team = None if draw else (next((t for t in known if (t.get("abbreviation") or "").lower() == abbr), None)
                                      or _team_for(title, known))
            g["sides"].append({"draw": draw, "abbr": abbr, "names": [title] + _names(team), "token": str(tokens[0]),
                               "final": _num(finals[0]) if finals else None})
        else:                                                   # one market, an outcome per team
            for i, (o, tok) in enumerate(zip(outcomes, tokens)):
                o = str(o)
                team = _team_for(o, known)
                g["sides"].append({"draw": False, "abbr": (team or {}).get("abbreviation"), "names": [o] + _names(team),
                                   "token": str(tok), "final": _num(finals[i]) if i < len(finals) else None})
    out = []
    for g in games.values():
        tokens = [s["token"] for s in g["sides"]]
        if len(set(tokens)) == len(tokens) and len(tokens) >= 2:
            out.append(g)
    return sorted(out, key=lambda g: (g["start"], g["slug"] or ""))


def _slug_of(m):
    ev = m.get("events")
    ev = ev[0] if isinstance(ev, list) and ev and isinstance(ev[0], dict) else {}
    return ev.get("slug") or m.get("slug") or ""


def _names(team):
    return [team.get("name"), team.get("alias")] if team else []


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if 0 <= x <= 1 else None


# ---------- matching ----------

def _orientations(league, kgame, pgame):
    """Every way to pair Kalshi's markets with Polymarket's outcomes, best first:
    [(total, scores, pairing)]."""
    ks, ps = kgame["sides"], pgame["sides"]
    out = []
    for perm in itertools.permutations(range(len(ps))):
        scores = [side_score(league, k, ps[j]) for k, j in zip(ks, perm)]
        out.append((sum(scores), scores, perm))
    return sorted(out, key=lambda o: -o[0])


def _accept(kgame, scores):
    """Is this pairing sure enough? Every team must agree at least SOME. In a three-way
    (soccer) game one team may be unknown when the draw and the other team are SURE: in
    one league at one time, a team plays only one opponent."""
    teams = [s for k, s in zip(kgame["sides"], scores) if not k["draw"]]
    if any(k["draw"] for k in kgame["sides"]) and any(s < SURE for k, s in zip(kgame["sides"], scores) if k["draw"]):
        return False
    weak = [s for s in teams if s < SOME]
    if not weak:
        return True
    return len(kgame["sides"]) == 3 and len(weak) == 1 and sum(s >= SURE for s in teams) == len(teams) - 1


def _candidates(league, kg, pgames, start, window):
    """Polymarket games within `window` of `start` whose best pairing with this Kalshi game
    is sure enough and beats every other pairing of the two by 0.5 (so home and away can't
    be swapped): [(score, -distance, game, pairing)], best first."""
    out = []
    for pg in pgames:
        far = abs(pg["start"] - start)
        if far > window or len(pg["sides"]) != len(kg["sides"]):
            continue
        ranked = _orientations(league, kg, pg)
        best = ranked[0]
        margin = best[0] - (ranked[1][0] if len(ranked) > 1 else 0.0)
        if margin >= 0.5 and _accept(kg, best[1]):
            out.append((best[0], -far, pg, best[2]))
    return sorted(out, key=lambda c: (c[0], c[1]), reverse=True)


def match(kgames, pgames, league_code):
    """Pair each Kalshi game with one Polymarket game: same number of outcomes, start times
    within WINDOW (Kalshi's start = its scheduled end - 3 hours), and team names that agree.
    Returns (pairs, why): pairs are (kalshi game, polymarket game, {ticker: polymarket side}),
    and why counts the games left out (and the placeholder matches). Nothing here looks at
    results or volumes.

    The chosen Polymarket game must beat every other one in the window by 0.5, so a
    doubleheader's two games can't be mixed up. Kalshi sometimes lists a placeholder start
    (every game of a college Saturday, or of a La Liga matchday, at one time). Then a game
    up to FAR away is taken, but only if it's the only one these teams play within
    REMATCH: a baseball series or a doubleheader never gets this far. Each Polymarket game
    is used once, surest pairs first."""
    why = Counter()
    found = []
    for kg in kgames:
        start = kg["end"] - START_TO_END
        cands = _candidates(league_code, kg, pgames, start, WINDOW)
        if not cands:
            near = _candidates(league_code, kg, pgames, start, REMATCH)
            if len(near) == 1 and -near[0][1] <= FAR:
                cands = near
                why["matched, Kalshi's start a placeholder"] += 1
        if not cands:
            why["no Polymarket game with those teams near that time"] += 1
            continue
        if len(cands) > 1 and cands[0][0] - cands[1][0] < 0.5:
            why["two Polymarket games fit as well"] += 1
            continue
        found.append((cands[0][0], cands[0][1], kg, cands[0][2], cands[0][3]))
    pairs, used = [], set()
    for score, _, kg, pg, perm in sorted(found, key=lambda f: (-f[0], -f[1], f[2]["event"])):
        if id(pg) in used:
            why["its Polymarket game was taken by a surer match"] += 1
            continue
        used.add(id(pg))
        pairs.append((kg, pg, {k["market"]["ticker"]: pg["sides"][j] for k, j in zip(kg["sides"], perm)}))
    pairs.sort(key=lambda p: (p[0]["end"], p[0]["event"]))
    return pairs, why


# ---------- prices at the decision time ----------

def _volume(c):
    for k in ("volume_fp", "volume"):       # live candles send volume_fp, historical ones volume
        v = c.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def kalshi_quote(client, series, m, t):
    """(yes_ask, yes_bid, volume, at) at decision time t: the last hourly candle that ended
    by t (at most backtest.LOOKBACK_HOURS old), when it ended (`at`), and the contracts
    traded in the VOLUME_HOURS before t. None if there's no usable price. Candles after t
    are dropped even if the API sends them: the no-look-ahead rule."""
    start = t - max(backtest.LOOKBACK_HOURS, VOLUME_HOURS) * H
    candles = [c for c in client.candles(series, m["ticker"], start, t)
               if isinstance(c.get("end_period_ts"), (int, float)) and start <= c["end_period_ts"] <= t]
    for c in sorted(candles, key=lambda c: -c["end_period_ts"]):
        if c["end_period_ts"] < t - backtest.LOOKBACK_HOURS * H:
            return None                      # nothing fresh enough
        ask, bid = candle_close(c, "yes_ask"), candle_close(c, "yes_bid")
        if 0 < ask <= 1 and bid < ask:
            day = t - VOLUME_HOURS * H
            return ask, bid, sum(_volume(x) for x in candles if x["end_period_ts"] > day), int(c["end_period_ts"])
    return None


def poly_price(history, t, max_age=backtest.LOOKBACK_HOURS * H):
    """(price, age in seconds) of the last Polymarket point whose minute was over by t, at
    most max_age old, or None. history: [{"t": unix time, "p": price}]."""
    best = None
    for pt in history or []:
        s, p = (pt.get("t"), pt.get("p")) if isinstance(pt, dict) else (None, None)
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (s, p)):
            continue
        if 0 <= p <= 1 and t - max_age <= s and s + POINT <= t and (best is None or s > best[0]):
            best = (s, p)
    return (float(best[1]), int(t - best[0])) if best else None


# ---------- Polymarket's public API ----------

def http_get(pace=0.15, session=None):
    """get(url, params) -> JSON, paced, retrying network errors, 429 and 5xx."""
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", "kal-research/1.0")
    lock, last = threading.Lock(), [0.0]

    def get(url, params):
        r = None
        for attempt in range(5):
            with lock:
                wait = last[0] + pace - time.monotonic()
                last[0] = max(last[0] + pace, time.monotonic())
            if wait > 0:
                time.sleep(wait)
            try:
                r = s.get(url, params=params, timeout=40)
            except requests.RequestException:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            break
        r.raise_for_status()
        return r.json()
    return get


# Tags every league shares: Sports, Games, Soccer.
SHARED_TAGS = {"1", "100639", "100350"}


class Polymarket:
    """Read-only Polymarket data: no key, no orders. `get(url, params)` returns parsed
    JSON; tests pass a fake."""

    def __init__(self, get=None):
        self.get = get or http_get()

    def sports(self):
        """{sport code: entry}. An entry's tags mark that league's markets."""
        got = self.get(GAMMA + "/sports", {})
        return {s["sport"]: s for s in got if isinstance(s, dict) and s.get("sport")} if isinstance(got, list) else {}

    def teams(self, league):
        out, offset = [], 0
        while offset <= 5000:
            page = self.get(GAMMA + "/teams", {"league": league, "limit": 100, "offset": offset})
            if not isinstance(page, list) or not page:
                break
            out += [t for t in page if isinstance(t, dict)]
            if len(page) < 100:
                break
            offset += 100
        return out

    def league_markets(self, sport, since, until):
        """The game-winner markets of one league (a /sports entry) listed between since and
        until. Tries the league's own tags, primary first (on 2026-10-25 Serie A's and the
        Champions League's primary tags had none), and keeps markets whose game slug starts
        with the league's code."""
        code = sport.get("sport") or ""
        tags = [str(sport.get("primaryTagId") or "")] + str(sport.get("tags") or "").split(",")
        for tag in dict.fromkeys(t.strip() for t in tags):
            if not tag or tag in SHARED_TAGS:
                continue
            got = [m for m in self.moneylines(tag, since, until) if _slug_of(m).startswith(code + "-")]
            if got:
                return got
        return []

    def moneylines(self, tag, since, until, step=15 * 86400):
        """Every game-winner market with this tag listed between since and until, settled or
        not (one not yet settled still has its prices). Fetched in listing-date windows, so
        no window needs more than the API's deepest page."""
        found = {}
        for closed in ("true", "false"):
            lo = since
            while lo < until:
                hi = min(until, lo + step)
                offset = 0
                while offset < 2000:
                    page = self.get(GAMMA + "/markets", {
                        "tag_id": tag, "sports_market_types": "moneyline", "closed": closed, "limit": 100,
                        "offset": offset, "start_date_min": iso(lo), "start_date_max": iso(hi)})
                    if not isinstance(page, list):
                        break
                    for m in page:
                        if isinstance(m, dict) and m.get("id") is not None:
                            found.setdefault(m["id"], m)
                    if len(page) < 100:
                        break
                    offset += 100
                else:
                    log.warning("polymarket tag %s: a window from %s has 2000+ markets", tag, iso(lo))
                lo = hi
        return list(found.values())

    def history(self, token, start, end):
        """A token's price points [{"t", "p"}] between start and end, one a minute."""
        got = self.get(CLOB + "/prices-history", {"market": token, "startTs": int(start), "endTs": int(end)})
        return got.get("history") or [] if isinstance(got, dict) else []


# ---------- rows: both prices at the decision time ----------

def price_rows(client, poly, pairs, hours=6, progress=None):
    """One row per matched Kalshi market with a price on both venues at the decision time.
    Returns (rows, why) where why counts the markets left out.

    Kalshi's price is an hourly candle's close, up to an hour before the decision minute.
    Polymarket's is read at that same moment (the candle's end), not at the decision
    minute: a fresher Polymarket price would have seen news the Kalshi quote hadn't, a
    fake edge. Kalshi and Polymarket are read side by side, one thread each (each client
    stays in its own thread)."""
    jobs, why = [], Counter()
    for kg, pg, sides in pairs:
        for k in kg["sides"]:
            m = k["market"]
            try:
                value = yes_payout(m)
            except UnknownSettlement:
                value = None
            if value is None:
                why["no clear result"] += 1
                continue
            t = backtest.decision_time(m, hours, anchor=kg["end"])
            if t is None:
                why["not open at the decision time"] += 1
                continue
            if t > pg["start"] - PRE_START:
                why["decision too close to Polymarket's start"] += 1
                continue
            jobs.append((kg, pg, k, m, value, t, sides[m["ticker"]]))

    def each(fetch, name):
        out = []
        for i, job in enumerate(jobs):
            try:
                out.append(fetch(job))
            except Exception as e:  # one market failing shouldn't sink the run
                log.warning("%s: %s failed: %s", job[3]["ticker"], name, e)
                out.append(None)
            if progress and (i + 1) % 1000 == 0:
                progress(f"{name}: {i + 1} of {len(jobs)}")
        return out

    def kalshi(job):
        return kalshi_quote(client, job[0]["series"], job[3], job[5])

    def polymarket(job):          # enough history for any candle time in the 12 hours before t
        t = job[5]
        return poly.history(job[6]["token"], t - 2 * backtest.LOOKBACK_HOURS * H, t)

    with ThreadPoolExecutor(max_workers=2) as ex:
        quotes, histories = ex.submit(each, kalshi, "Kalshi prices"), ex.submit(each, polymarket, "Polymarket prices")
        quotes, histories = quotes.result(), histories.result()
    rows = []
    for (kg, pg, k, m, value, t, ps), quote, history in zip(jobs, quotes, histories):
        if not quote:
            why["no Kalshi price"] += 1
            continue
        got = poly_price(history, quote[3])     # at the Kalshi quote's own time
        if not got:
            why["no Polymarket price"] += 1
            continue
        rows.append({"league": kg["league"], "series": kg["series"], "event": kg["event"], "ticker": m["ticker"],
                     "side": k["code"], "poly_game": pg["slug"], "anchor": kg["end"], "decided": t,
                     "priced_at": quote[3],
                     "close": _time(m.get("close_time")) or kg["end"], "yes_ask": quote[0], "yes_bid": quote[1],
                     "volume": quote[2], "poly": got[0], "poly_age": got[1],
                     "result": m["result"] if m.get("result") in ("yes", "no") else "scalar",
                     "yes_value": value, "poly_final": ps.get("final")})
    return rows, why


# ---------- the tests ----------

def busy(rows):
    """The rows the live strategies could have traded: BUSY+ contracts in the day before."""
    return [o for o in rows if (o.get("volume") or 0) >= BUSY]


def group_of(league):
    return "Soccer" if league in SOCCER else league


def _clustered(diffs):
    """(mean, standard error) of per-market values, clustered by event."""
    if not diffs:
        return None, None
    n = len(diffs)
    mean = sum(d for _, d in diffs) / n
    by = defaultdict(float)
    for e, d in diffs:
        by[e] += d - mean
    g = len(by)
    if g < 2:
        return mean, None
    return mean, math.sqrt(sum(v * v for v in by.values()) * g / (g - 1)) / n


def brier(rows):
    """Brier scores (lower is better) of Kalshi's mid and Polymarket's price on the same
    markets, and Kalshi minus Polymarket with a 95% range. The range is clustered by event
    (a game-day): a game's markets mirror each other, and one day's games share news."""
    if not rows:
        return None
    out, diffs = {"markets": len(rows), "games": len({o["event"] for o in rows}),
                  "events": len({backtest.group(o) for o in rows})}, []
    k = p = 0.0
    for o in rows:
        y = backtest.paid(o, "yes")
        bk, bp = ((o["yes_ask"] + o["yes_bid"]) / 2 - y) ** 2, (o["poly"] - y) ** 2
        k, p = k + bk, p + bp
        diffs.append((backtest.group(o), bk - bp))
    mean, se = _clustered(diffs)
    out.update(kalshi=k / len(rows), poly=p / len(rows), diff=mean,
               lo=None if se is None else mean - 1.96 * se, hi=None if se is None else mean + 1.96 * se)
    return out


def trades(rows, margin):
    """{"yes": [...], "no": [...]} of backtest.trade tuples on busy markets: buy YES at the
    ask when Polymarket is above it by more than `margin`; buy NO at 1 - bid when
    Polymarket is below the bid by more than `margin`. Each trade is a full-size order
    paying Kalshi's taker fee, rounded up (backtest.trade)."""
    out = {"yes": [], "no": []}
    for o in busy(rows):
        ask, bid, q, ev = o["yes_ask"], o["yes_bid"], o["poly"], backtest.group(o)
        if 0.01 <= ask <= 0.99 and q - ask > margin + 1e-9:
            out["yes"].append(backtest.trade(ev, ask, backtest.paid(o, "yes")))
        no = round(1 - bid, 4)
        if bid > 0 and 0.01 <= no <= 0.99 and bid - q > margin + 1e-9:
            out["no"].append(backtest.trade(ev, no, backtest.paid(o, "no")))
    return out


def agreement(rows):
    """(agree, differ): markets where both venues settled cleanly to 0 or 1, and whether
    they agreed. A check on the matching only: no test uses Polymarket's result."""
    agree = differ = 0
    for o in rows:
        k, p = o.get("yes_value"), o.get("poly_final")
        if k in (0.0, 1.0) and p in (0.0, 1.0):
            agree, differ = (agree + 1, differ) if k == p else (agree, differ + 1)
    return agree, differ


def gap_shares(rows, margins=MARGINS):
    """How often Polymarket sat outside Kalshi's bid-ask by more than each margin."""
    if not rows:
        return []
    return [{"margin": m, "share": sum(1 for o in rows if o["poly"] - o["yes_ask"] > m + 1e-9
                                       or o["yes_bid"] - o["poly"] > m + 1e-9) / len(rows)} for m in margins]


def results(rows, margins=MARGINS):
    """Brier scores and the trade table, per league group (soccer pooled) and for all.
    The worst case is backtest.roi_stats' cautious bound, with a Bonferroni z over every
    margin x side x group tested."""
    groups = defaultdict(list)
    for o in rows:
        groups[group_of(o["league"])].append(o)
    order = [g for g in ("NFL", "NBA", "MLB", "NHL", "CFB", "WNBA", "Soccer") if g in groups]
    order += sorted(g for g in groups if g not in order)
    names = order + (["ALL"] if rows else [])
    pick = {**groups, "ALL": rows}
    tries = len(names) * len(margins) * 2
    z = backtest.z_for(tries)
    table = []
    for name in names:
        for margin in margins:
            for side, t in trades(pick[name], margin).items():
                roi, lo = backtest.roi_stats(t, z) if t else (None, None)
                events = len({x[0] for x in t})
                table.append({"group": name, "margin": margin, "side": side, "trades": len(t), "events": events,
                              "win": sum(1 for x in t if x[2] > 0) / len(t) if t else None,
                              "roi": roi, "roi_lo": lo,
                              "edge": lo is not None and lo > 0 and len(t) >= backtest.MIN_TRADES
                              and events >= backtest.MIN_EVENTS})
    return {"z": z, "tries": tries, "groups": names,
            "brier": {name: brier(pick[name]) for name in names},
            "brier_busy": {name: brier(busy(pick[name])) for name in names},
            "trades": table}
