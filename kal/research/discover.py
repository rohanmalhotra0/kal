"""Daily discovery: test a rotating slice of Kalshi's series for price ranges that win.

Each UTC day tests a different, fixed slice of all series, so over time every series
gets tested. A few slots go to re-testing series whose edges haven't been checked in
a week. New edges start as "candidate": favorites trades them on paper only, and
kal.research.promote moves them to "live" once paper trading agrees.

Files in $KAL_HOME/research/ (research-demo/ on the demo exchange; the engine loads
them into ctx.research):
  edges.json  a list of {id: "SERIES:lo-hi", series, lo, hi, hours_before_close, roi,
              roi_lo, trades, events, first_seen, last_tested, status, note}
              status: "candidate" | "live" | "retired". events: series-days.
  words.json  {series, margin, roi, roi_lo, trades, events, words, base, by_series,
              promoted, updated}

Those files aren't in the backups, but the db is. So each idea's status, looks and
rank are kept in the db too (memory()): after a rebuild, a loser retired for losing
money stays retired.

Testing hundreds of series will find some "edges" by pure luck, however strict each
test is. That's why nothing found here trades real money until paper trading confirms it.
"""
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from ..client import brief_series, num
from .. import db
from ..strategies import favorites
from . import backtest

MAX_SERIES = 15          # series tested per run (each costs one API call per new settled market)
MAX_MARKETS = 1500       # settled markets per series
HOURS_BEFORE_CLOSE = 6   # decision time the edges are tested at
RETEST_DAYS = 7
MENTION = re.compile(r"\b(say|says|said|mention|mentions|mentioned)\b", re.I)

log = logging.getLogger(__name__)


# ---------- files ----------

def research_dir():
    """$KAL_HOME/research, or research-demo/ on the demo exchange (backtest.folder)."""
    return backtest.folder("research")


def _load(name, default):
    path = os.path.join(research_dir(), name)
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def _save(name, data):
    """Write then rename, so the engine never reads a half-written file."""
    path = os.path.join(research_dir(), name)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, indent=1)
    os.replace(path + ".tmp", path)


def load_edges():
    return recalled("favorites", favorites.edges_of({"edges": _load("edges.json", [])}))


def save_edges(edges):
    _save("edges.json", sorted(edges, key=lambda e: e["id"]))
    remember_all("favorites", edges)


def load_words():
    w = _load("words.json", {})
    return recall(w, memory().get("mentions")) if isinstance(w, dict) else {}


def save_words(words):
    _save("words.json", words)
    remember("mentions", **_kept(memory().get("mentions"), words))


# ---------- what each idea went through, kept in the db ----------

MEMORY = "research_memory"   # db key: {"<strategy>:<id>" or "mentions": {status, retired_by, looks, rank}}


def memory():
    m = db.get(MEMORY)
    return m if isinstance(m, dict) else {}


def remember(key, **fields):
    mem = memory()
    mem[key] = {**(mem.get(key) or {}), **fields}
    db.put(MEMORY, mem)


def remember_all(strategy, ideas):
    """Keep each idea's status, looks and rank in the db, as "<strategy>:<id>" (_kept)."""
    mem = memory()
    for i in ideas:
        key = f"{strategy}:{i['id']}"
        mem[key] = _kept(mem.get(key), i)
    db.put(MEMORY, mem)


def recalled(strategy, ideas):
    """The ideas, each with what the db remembers about it put back (recall)."""
    mem = memory()
    return [recall(i, mem.get(f"{strategy}:{i.get('id')}")) for i in ideas]


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _kept(old, idea):
    """The db record of an idea after a save. A retirement for losing money is never
    undone, and looks (paper checks so far) and rank (kal.research.promote) never go down."""
    out = dict(old or {})
    out["status"] = idea.get("status")
    if out.get("retired_by") != "results":
        out["retired_by"] = idea.get("retired_by") or ""
    for k in ("looks", "rank"):
        n = max(_int(out.get(k)), _int(idea.get(k)))
        if n:
            out[k] = n
    return out


def recall(idea, mem):
    """Put back what the db remembers about an idea, in place: retired for losing money
    stays retired even if its file was lost or replaced, and looks and rank carry on."""
    if not isinstance(mem, dict):
        return idea
    if mem.get("retired_by") == "results" and idea.get("retired_by") != "results":
        idea.update(status="retired", retired_by="results", note="lost money before: stays retired")
    for k in ("looks", "rank"):
        n = max(_int(idea.get(k)), _int(mem.get(k)))
        if n:
            idea[k] = n
    return idea


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def _age_days(iso, now):
    try:
        return (now - datetime.fromisoformat(iso).timestamp()) / 86400
    except (TypeError, ValueError):
        return float("inf")


# ---------- which series to test ----------

def is_mention_series(s):
    return bool(MENTION.search(s.get("title") or "")) or "MENTION" in (s.get("ticker") or "").upper()


def rotation(tickers, day, n, step=None):
    """The day's slice of all series. A fixed shuffle (by hash, so it's the same in every
    process) is walked `step` (default n) at a time, so with n >= step, ceil(N/step) days
    in a row test every series."""
    order = sorted(set(tickers), key=lambda t: hashlib.sha256(t.encode()).hexdigest())
    if not order:
        return []
    start = (day * (n if step is None else step)) % len(order)
    return [order[(start + i) % len(order)] for i in range(min(n, len(order)))]


ACTIVE_POOL = 300  # rotate through the busiest series with open markets


def active_series(client, pool=ACTIVE_POOL):
    """Series with markets open right now, busiest first (by 24h volume).

    Kalshi has 15,000+ series and most are tiny or finished. An edge only helps where
    the bot can still trade, and busy series have the most settled history to test,
    so research spends its daily budget there. Returns [] if the events call fails.

    A page at a time, keeping only each series' volume: all open events with their markets
    is over 700 MB in Python, and research shares the server's memory cap with trading."""
    volume = {}
    try:
        pages = getattr(client, "event_pages", None)
        pages = iter(pages(status="open", with_nested_markets="true") if pages
                     else [client.events(status="open", with_nested_markets="true")])
    except Exception as e:
        log.warning("research: couldn't list open events: %s", e)
        return []
    while True:
        try:
            page = next(pages)
        except StopIteration:
            break
        except Exception as e:   # only the download: odd data raises, as it always did
            log.warning("research: couldn't list open events: %s", e)
            return []
        for ev in page:
            s = ev.get("series_ticker")
            if not s or s.startswith("KXMVE"):  # combo bets: not a series we'd trade
                continue
            v = sum(num(m, "volume_24h") for m in ev.get("markets") or [])
            volume[s] = volume.get(s, 0.0) + v
    busy = sorted((s for s, v in volume.items() if v > 0), key=lambda s: (-volume[s], s))
    return busy[:pool]


def pick_series(tickers, edges, words, day, max_series, now):
    """Re-tests of stale edge/mention series first (up to a fifth of the slots), then the
    rotation fills the rest. The rotation always moves on by the same step each day, so
    re-tests never stop it from reaching every series."""
    known = set(tickers)
    age = {}  # series -> days since it was last tested
    for e in edges:
        if e.get("status") in favorites.ACTIVE:
            age[e["series"]] = min(age.get(e["series"], float("inf")), _age_days(e.get("last_tested"), now))
    for s, info in (words.get("by_series") or {}).items():
        age[s] = min(age.get(s, float("inf")), _age_days(info.get("last_tested"), now))
    stale = sorted((s for s, a in age.items() if a >= RETEST_DAYS and s in known), key=lambda s: (-age[s], s))
    n_retest = max_series // 5
    picks = stale[:n_retest]
    for s in rotation(tickers, day, len(tickers), step=max_series - n_retest):
        if len(picks) >= max_series:
            break
        if s not in picks:
            picks.append(s)
    return picks


# ---------- merging results ----------

def edge_id(series, lo, hi, side=None):
    return f"{series}:{side}:{lo:.2f}-{hi:.2f}" if side else f"{series}:{lo:.2f}-{hi:.2f}"


def merge_edges(edges, series, rows, hours, stamp):
    """Fold one series' calibration rows into the edge list (in place).
    A range that passes is added or refreshed. An active range that now fails with
    enough data is retired, and so is one with no busy market left to test it: its old
    numbers can't be checked any more (a later pass may revive it). One retired for
    losing real or paper money stays retired, even when its file was lost: the db
    remembers (recall)."""
    changes = {"new": [], "retired": [], "revived": []}
    by_id = {e["id"]: e for e in edges}
    tested = set()
    for r in rows:
        eid = edge_id(series, r["lo"], r["hi"], r.get("side"))
        e = by_id.get(eid)
        if e is not None and e.get("hours_before_close") != hours:
            continue  # tested at another decision time: not comparable
        tested.add(eid)
        stats = {k: r[k] for k in ("roi", "roi_lo", "trades", "events")}
        if backtest.is_edge(r):
            if e is None:
                e = recalled("favorites", [{"id": eid, "series": series, "side": r.get("side"), "lo": r["lo"],
                                            "hi": r["hi"], "hours_before_close": hours, "first_seen": stamp,
                                            "status": "candidate", "note": "new: trading on paper first"}])[0]
                edges.append(e)
                by_id[eid] = e
                if e["status"] == "candidate":
                    changes["new"].append(eid)
            elif e.get("status") == "retired" and e.get("retired_by") == "backtest":
                e.update(status="candidate", note="passed the backtest again: back on paper", retired_by="")
                changes["revived"].append(eid)
            e.update(stats, last_tested=stamp)
        elif e is not None:
            e.update(stats, last_tested=stamp)
            if e.get("status") in favorites.ACTIVE and r["trades"] >= backtest.MIN_TRADES:
                e.update(status="retired", retired_by="backtest", note="backtest no longer shows an edge")
                changes["retired"].append(eid)
    for e in edges:
        if (e.get("series") == series and e.get("hours_before_close") == hours
                and e.get("id") not in tested and e.get("status") in favorites.ACTIVE):
            e.update(status="retired", retired_by="backtest", last_tested=stamp,
                     note="no busy markets left to test it: off until a backtest shows it again")
            changes["retired"].append(e["id"])
    return changes


def update_words(words, client, tested, all_series, hours, max_markets, stamp):
    """Re-run the mention test on every mention series we have prices for (from cache),
    and keep the "promoted" switch unless the backtest stopped showing an edge."""
    by_series = dict(words.get("by_series") or {})
    pool = set(words.get("series") or []) | set(tested)
    obs, kept = [], []
    for s in sorted(pool):
        rows = backtest.load_observations(client, s, hours, max_markets=max_markets, cached_only=True)
        if rows:
            obs += rows
            kept.append(s)
            info = by_series.setdefault(s, {})
            info["markets"] = len(rows)
            info["title"] = (all_series.get(s) or {}).get("title", info.get("title", ""))
            if s in tested:
                info["last_tested"] = stamp
    if not obs:
        return None
    res = backtest.mention_backtest(obs)
    best = res["best"]
    promoted = bool(words.get("promoted"))
    note = words.get("note", "")
    if promoted and (best["roi_lo"] is None or best["roi_lo"] <= 0):
        promoted, note = False, "backtest no longer shows an edge: back to paper"
    new = {**words,  # keep what promote wrote (results, dates)
           "series": kept, "hours_before_close": hours, "margin": best["margin"], "roi": best["roi"],
           "roi_lo": best["roi_lo"], "trades": best["trades"], "events": best["events"],
           "margins": res["margins"], "words": res["words"], "base": res["base"],
           "by_series": {s: by_series[s] for s in kept}, "promoted": promoted, "note": note, "updated": stamp}
    backtest.record("mentions", {"series": kept, "hours_before_close": hours},
                    {"best": best, "margins": res["margins"], "observations": len(obs)})
    return new


# ---------- the daily run ----------

def run(client, cfg, quick=False, now=None, series=None):
    """Test today's series (or `series`, if given) and save edges.json / words.json.
    A series that fails is logged and skipped; it never stops the others."""
    now = time.time() if now is None else now
    stamp = _iso(now)
    hours = float(cfg.get("research_hours_before_close", HOURS_BEFORE_CLOSE))
    max_series = int(cfg.get("research_max_series", MAX_SERIES))
    max_markets = int(cfg.get("research_max_markets", MAX_MARKETS))
    if quick:
        max_series, max_markets = min(max_series, 3), min(max_markets, 200)

    edges, words = load_edges(), load_words()
    all_series = {s["ticker"]: s for s in brief_series(client.series()) if s["ticker"]}
    # One-off series have a single event: never enough events to show an edge.
    testable = [t for t, s in all_series.items() if s.get("frequency") != "one_off"]
    active = [t for t in active_series(client) if t in all_series and all_series[t].get("frequency") != "one_off"]
    if active:
        testable = active  # the busiest series we could actually trade
    day = datetime.fromtimestamp(now, timezone.utc).date().toordinal()
    picks = list(series) if series else pick_series(testable, edges, words, day, max_series, now)

    summary = {"day": _iso(now)[:10], "series_total": len(all_series), "tested": [], "failed": {},
               "new_edges": [], "retired": [], "revived": []}
    mention_tested = []
    for s in picks:
        info = all_series.get(s, {"ticker": s})
        try:
            obs = backtest.load_observations(client, s, hours, max_markets=max_markets)
            rows = backtest.calibration(backtest.busy(obs), max_spread=favorites.MAX_SPREAD, by_side=True)
            changes = merge_edges(edges, s, rows, hours, stamp)
            backtest.record("calibration", {"series": s, "hours_before_close": hours, "max_markets": max_markets,
                                            "max_spread": favorites.MAX_SPREAD},
                            {"observations": len(obs), "events": len({o["event"] for o in obs}),
                             "calibration": rows, "edges": [r for r in rows if backtest.is_edge(r)]})
            save_edges(edges)  # after every series, so a crash later keeps this one's work
        except Exception as e:
            log.warning("research: %s failed: %s", s, e, exc_info=True)
            summary["failed"][s] = str(e)[:300]
            continue
        summary["tested"].append(s)
        for k in ("new", "retired", "revived"):
            summary["new_edges" if k == "new" else k] += changes[k]
        if is_mention_series(info) or s in (words.get("series") or []):
            mention_tested.append(s)

    if mention_tested:
        try:
            new_words = update_words(words, client, mention_tested, all_series, hours, max_markets, stamp)
            if new_words:
                if words.get("promoted") and not new_words["promoted"]:
                    summary["retired"].append("mentions")
                save_words(new_words)
                summary["mentions"] = {k: new_words[k] for k in ("series", "margin", "roi_lo", "trades")}
        except Exception as e:
            log.warning("research: mention test failed: %s", e, exc_info=True)
            summary["failed"]["mentions"] = str(e)[:300]

    summary["ok"] = bool(summary["tested"]) or not summary["failed"]
    summary["edges"] = {st: sum(1 for e in edges if e.get("status") == st) for st in ("candidate", "live", "retired")}
    return summary
