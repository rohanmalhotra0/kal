"""The dashboard: a JSON API plus one page. static/app.js polls /api/state for the Home
tab, and each other tab (Trades, Research, Test an idea) fetches its own data only
while it is open.

Anyone who can open this page can turn on real-money trading, so:
- It listens on 127.0.0.1 unless told otherwise.
- KAL_DASHBOARD_PASSWORD turns on HTTP basic auth for every route. Without it, the
  dashboard refuses to listen anywhere but localhost, and answers only requests
  addressed to localhost (so a web page can't reach it by DNS tricks).
- Every POST must send JSON. A web page on another site can't send JSON here without
  the browser asking first, and we never say yes, so it can't flip the switch.

Only the ON switch can lead to real trades, and it goes through config like the CLI.
The backtester and the series search use a client with no key, so they can't trade.
"""
import collections
import glob
import hmac
import importlib
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request
from werkzeug.serving import make_server

from .. import config, db, gate, ledger, notify, paper
from ..client import Kalshi, fee, load_key
from ..scheduler import Scheduler
from ..strategies import load_all

log = logging.getLogger(__name__)

DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8787
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
RECENT = 25
CHART_DAYS = 30
CHART_POINTS = 400
TOP_EDGES = 12
TRADES_SHOWN, TRADES_MAX = 200, 5000
BACKTESTS_SHOWN = 30
MAX_TEST_SERIES = 10          # each series can mean thousands of API calls
MAX_TEST_HOURS = 24 * 30
SERIES_TTL = 3600             # Kalshi's list of series barely changes: download it at most hourly
SERIES_SHOWN = 10            # a short list to pick from; more words narrow it down
ALERT_GAP = 10                # seconds: a double tap sends one test alert, not two
EDGE_STRATEGY = "favorites"   # the strategy that trades edges.json (the one promote judges)
TICKER = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,63}$")  # tickers go into API paths: nothing else gets in
MAX_FAILS, FAIL_WINDOW = 20, 900       # wrong passwords per address per 15 minutes
MIN_INTERVAL = {"trade_interval_min": 1, "research_interval_hours": 1, "max_consecutive_errors": 1}
CSP = ("default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")


def _password():
    return os.environ.get("KAL_DASHBOARD_PASSWORD") or ""


def _hostname(host):
    """'127.0.0.1:8787' -> '127.0.0.1', '[::1]:8787' -> '::1'."""
    try:
        return (urlsplit("//" + (host or "")).hostname or "").lower()
    except ValueError:
        return ""


def is_local(host):
    return _hostname(host) in LOCAL_HOSTS or (host or "").strip("[]") in LOCAL_HOSTS


def bind_problem(host, password):
    """Why the dashboard must not listen on `host`, or None if it's fine."""
    if is_local(host) or password:
        return None
    return ("Not starting. Anyone who opens this page could trade your money.\n"
            f"To listen on {host}, set KAL_DASHBOARD_PASSWORD first.\n"
            "Or keep the default (127.0.0.1) and use an SSH tunnel.")


class BadRequest(ValueError):
    pass


class NotInstalled(RuntimeError):
    """A part the page uses (like the backtester) failed to load."""


# ---------- small readers ----------

def _utc_midnight(now):
    d = datetime.fromtimestamp(now, timezone.utc).date()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def _key_id():
    try:
        return load_key(config.home())[0]
    except Exception:
        return None  # a key file we can't read is no key at all


def _has_key():
    return _key_id() is not None


def order_test(cfg, key_id):
    """The $0.01 test order that real money waits for (kal/gate.py): has it passed for this
    key and exchange? A pass for another key or exchange doesn't count."""
    rec = gate.status()
    ok = gate.passed_for(key_id, cfg["env"])
    return {"passed": ok, "detail": str(rec.get("detail") or "")[:200], "ts": _float(rec.get("ts")) or None}


def _float(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _int(v):
    v = _float(v)
    return int(v) if v is not None else None


def _json(raw):
    """A JSON column from the db as a dict. Junk, or anything but a dict, is {}."""
    try:
        v = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _module(name):
    """Import kal.<name> only when asked, so a broken research module can't take the page down."""
    try:
        return importlib.import_module(f"{__package__.rsplit('.', 1)[0]}.{name}")
    except Exception as e:
        log.error("could not load %s", name, exc_info=True)
        raise NotInstalled(f"This part can't load right now: {e}") from e


def _market_client():
    """A client with no key. Market history is public, and with no key it can't trade."""
    return Kalshi(env=config.load()["env"])


# ---------- the Home tab ----------

def books(cfg):
    """The money panel, straight from the bot's own ledger, for the exchange in use.
    These are the numbers that cap its losses, so the page shows exactly those."""
    budget = float(cfg.get("budget") or 0)
    return {"budget": budget, "realized": ledger.realized(), "capital": ledger.capital(cfg),
            "open_cost": ledger.open_cost(), "available": ledger.available(cfg),
            "reinvest_profits": bool(cfg.get("reinvest_profits", True)), "max_loss": budget,
            "cash_floor": float(cfg.get("cash_floor") or 0)}


def account(cfg, now):
    rows = db.query("SELECT * FROM equity ORDER BY ts DESC LIMIT 1")
    latest = rows[0] if rows else None
    acct = None
    if latest:
        acct = {"ts": latest["ts"], "cash": latest["balance"], "value": latest["equity"],
                "in_bets": latest["exposure"], "paper_value": latest["paper_equity"],
                # With no API key the engine writes zeros: there is no real account to show.
                "live": any(latest[k] for k in ("balance", "equity", "exposure"))}
    today = {"pnl": None, "paper_pnl": None}
    start = db.get("day_start")
    midnight = _utc_midnight(now)
    if (start and latest and start.get("day") == datetime.fromtimestamp(now, timezone.utc).date().isoformat()
            and latest["ts"] >= midnight):
        today["pnl"] = latest["equity"] - float(start.get("equity") or 0)
    today["paper_pnl"] = db.query("SELECT COALESCE(SUM(pnl), 0) AS p FROM paper WHERE settled = 1 "
                                  "AND settled_ts >= ?", midnight)[0]["p"]
    positions = {"live": len(ledger.open_positions()),
                 "paper": db.query("SELECT COUNT(DISTINCT ticker) AS n FROM paper WHERE settled = 0")[0]["n"]}
    money = books(cfg)
    money.update(paper_profit=paper.realized(), paper_cash=paper.cash(cfg), paper_value=paper.equity(cfg),
                 paper_open_cost=paper.open_cost())
    return acct, today, positions, money


def strategies(cfg, errors):
    try:
        loaded = load_all()
    except Exception as e:
        errors.append(f"strategies failed to load: {e}")
        loaded = {}
    stats = paper.stats()
    live = {r["strategy"]: r for r in db.query(
        """SELECT strategy, COUNT(*) AS orders, SUM(CASE WHEN filled > 0 THEN 1 ELSE 0 END) AS filled,
           COALESCE(SUM(filled * price), 0) AS spent FROM orders GROUP BY strategy""")}
    names = list(loaded) + [n for n in cfg["strategies"] if n not in loaded]
    out = []
    for name in names:
        s = loaded.get(name)
        out.append({"name": name, "title": (s.title if s else "") or name, "kind": s.kind if s else None,
                    "description": s.description if s else "Not installed.", "installed": s is not None,
                    "mode": cfg["strategies"].get(name, {}).get("mode", "off"),
                    "paper": stats.get(name), "live": live.get(name)})
    return out


def _edge_list(data):
    """edges.json is a list; tolerate {"edges": [...]} and {id: entry} too."""
    if isinstance(data, dict):
        data = data.get("edges", data)
    if isinstance(data, dict):
        data = [dict(v, id=v.get("id", k)) for k, v in data.items() if isinstance(v, dict)]
    return [e for e in data or [] if isinstance(e, dict)]


EDGE_ORDER = {"live": 0, "candidate": 1, "retired": 2}


def _edge_sort(e):
    """Live edges first, then candidates, then retired; the best worst case first in each."""
    lo = _float(e.get("roi_lo"))
    return EDGE_ORDER.get(e.get("status"), 3), -(lo if lo is not None else -9)


def _edges_summary(data):
    edges = sorted(_edge_list(data), key=_edge_sort)
    counts = collections.Counter(str(e.get("status") or "unknown") for e in edges)
    top = [{"id": str(e.get("id", "")), "series": str(e.get("series", "")), "status": str(e.get("status", "")),
            "note": str(e.get("note", ""))[:200], "last_tested": str(e.get("last_tested", "")),
            **{k: _float(e.get(k)) for k in ("lo", "hi", "hours_before_close", "roi", "roi_lo",
                                             "trades", "events")}}
           for e in edges[:TOP_EDGES]]
    return {"total": len(edges), "counts": dict(counts), "top": top}


def _words_summary(data):
    if not isinstance(data, dict):
        return None
    return {"promoted": bool(data.get("promoted")), "note": str(data.get("note", ""))[:200],
            "updated": str(data.get("updated", "")), "series": len(data.get("series") or []),
            "words": len(data.get("words") or {}),
            **{k: _float(data.get(k)) for k in ("roi", "roi_lo", "trades", "events", "margin")}}


def _last_research():
    last = db.get("last_research")
    if not isinstance(last, dict):
        return None
    return {"ts": last.get("ts"), "ok": last.get("ok"), "text": run_text("research", last),
            "tested": len(last.get("tested") or []), "new": len(last.get("new_edges") or []),
            "failed": len(last.get("failed") or {})}


def _research_folder():
    """The exchange in use's research files: research/, or research-demo/ on demo
    (kal.research.backtest.folder)."""
    env = config.load().get("env", "prod")
    return os.path.join(config.home(), "research" if env == "prod" else f"research-{env}")


def research_summary():
    out = {"files": [], "edges": None, "words": None, "last": None}
    for path in sorted(glob.glob(os.path.join(_research_folder(), "*.json"))):
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            out["files"].append({"name": name, "ok": False})
            continue
        out["files"].append({"name": name, "ok": True, "updated": os.path.getmtime(path)})
        if name == "edges":
            out["edges"] = _edges_summary(data)
        elif name == "words":
            out["words"] = _words_summary(data)
    out["last"] = _last_research()
    return out


def run_text(kind, m):
    """One plain line about a run, from its summary dict."""
    if not isinstance(m, dict):
        return str(m)[:200]
    if m.get("skipped"):
        return f"Skipped: {m['skipped']}"
    parts = []
    for k in ("error", "promote_error"):
        if m.get(k):
            parts.append(f"Error: {m[k]}")
    if kind == "trade" and "signals" in m:
        parts.append(f"{m.get('signals', 0)} found, {m.get('live_trades', 0)} real, "
                     f"{m.get('paper_trades', 0)} practice")
        settled = (m.get("settled") or 0) + (m.get("paper_settled") or 0)
        if settled:
            parts.append(f"{settled} settled")
    if not parts:
        parts.append("OK" if m.get("ok", True) else "Failed")
    return ". ".join(str(p) for p in parts)[:300]


def recent():
    trades = db.query(f"""SELECT id, ts, strategy, event, title, mode, guaranteed, cost, payout_min,
        exp_profit, status, note FROM signals WHERE status NOT LIKE 'skipped%' ORDER BY id DESC LIMIT {RECENT}""")
    orders = db.query(f"""SELECT id, ts, strategy, ticker, side, count, price, filled, status, error
        FROM orders ORDER BY id DESC LIMIT {RECENT}""")
    runs = []
    for r in db.query(f"SELECT id, ts, kind, ok, message, duration FROM runs ORDER BY id DESC LIMIT {RECENT}"):
        try:
            m = json.loads(r["message"] or "{}")
        except ValueError:
            m = {"error": r["message"]}
        notes = m.get("notes") if isinstance(m, dict) else None
        runs.append({"id": r["id"], "ts": r["ts"], "kind": r["kind"], "ok": bool(r["ok"]),
                     "duration": r["duration"], "text": run_text(r["kind"], m),
                     "notes": [str(n)[:300] for n in notes[:10]] if isinstance(notes, list) else []})
    return trades, orders, runs


def equity_history(now):
    rows = db.query("SELECT ts, equity, paper_equity FROM equity WHERE ts >= ? ORDER BY ts",
                    now - CHART_DAYS * 86400)
    step = max(1, math.ceil(len(rows) / CHART_POINTS))
    picked = rows[::step]
    if rows and picked[-1] is not rows[-1]:
        picked.append(rows[-1])
    return picked


def build_state(sched):
    now = time.time()
    errors = []
    cfg = config.load()
    key_id = _key_id()
    s = {"now": now, "config": cfg, "has_key": key_id is not None, "errors": errors,
         "alerts": {"enabled": notify.enabled()}, "order_test": order_test(cfg, key_id)}
    parts = {
        "money": lambda: dict(zip(("account", "today", "positions", "money"), account(cfg, now))),
        "strategies": lambda: {"strategies": strategies(cfg, errors)},
        "research": lambda: {"research": research_summary()},
        "recent": lambda: dict(zip(("trades", "orders", "runs"), recent())),
        "equity": lambda: {"equity": equity_history(now)},
        "scheduler": lambda: {"scheduler": sched.state()},
    }
    empty = {"money": {"account": None, "today": {}, "positions": {}, "money": {}},
             "strategies": {"strategies": []}, "research": {"research": None},
             "recent": {"trades": [], "orders": [], "runs": []}, "equity": {"equity": []},
             "scheduler": {"scheduler": None}}
    for name, fn in parts.items():
        try:
            s.update(fn())
        except Exception as e:
            log.error("state part %s failed", name, exc_info=True)
            errors.append(f"{name}: {e}")
            s.update(empty[name])
    return s


# ---------- the Trades tab ----------

def _tally():
    return {"trades": 0, "open": 0, "settled": 0, "wins": 0, "pnl": 0.0, "cost": 0.0, "open_cost": 0.0}


def _paid(result, side):
    """What one contract on `side` paid, once booked: 1, 0, or a split ("0.3300": a called-off
    market). None while open, or when paper couldn't read the result ("unknown")."""
    if result in (None, "", "unknown"):
        return None
    try:
        return ledger.payout_of(result, side)
    except (TypeError, ValueError):
        return None


def _count(t, cost, result, side, pnl):
    """Add one trade to a tally. `result` is set only once its market has settled."""
    t["trades"] += 1
    t["cost"] += cost or 0.0
    paid = _paid(result, side)
    if paid is not None:
        t["settled"] += 1
        t["wins"] += paid == 1
        t["pnl"] += pnl or 0.0
    else:
        t["open"] += 1
        t["open_cost"] += cost or 0.0


def _fill_cost(n, p):
    """What a fill cost, the way the ledger counts it: notional plus the fee, at our limit."""
    return n * p + fee(n, p) if n > 0 else 0.0


def _live_pnl(filled, price, side, result):
    """Profit of one filled live order once its market settled, else None."""
    paid = _paid(result, side)
    if paid is None or not filled > 0:
        return None
    return filled * paid - _fill_cost(filled, price)


def _settled_state(paid):
    return "won" if paid == 1 else "lost" if paid == 0 else "split"


def _live_state(status, filled, result, side):
    status = str(status or "")
    if status in ("sending", "unknown"):
        return "checking"       # the reply was lost; the next run looks the order up on Kalshi
    if status == "rejected":
        return "refused"
    if status == "never_placed":
        return "never_placed"
    if not filled > 0:
        return "no_fill"
    paid = _paid(result, side)
    return "open" if paid is None else _settled_state(paid)


def _live_trade(r):
    n, p = float(r["filled"] or 0), float(r["price"] or 0)
    return {"kind": "live", "id": r["id"], "ts": r["ts"], "strategy": r["strategy"], "title": r["title"] or "",
            "event": r["event"] or "", "ticker": r["ticker"], "side": r["side"], "price": p,
            "count": _float(r["count"]), "filled": n, "status": r["status"],
            "state": _live_state(r["status"], n, r["result"], r["side"]), "cost": _fill_cost(n, p),
            "pnl": _live_pnl(n, p, r["side"], r["result"]), "error": str(r["error"] or "")[:200]}


def _paper_trade(r):
    paid = _paid(r["result"], r["side"]) if r["settled"] else None
    settled = paid is not None
    return {"kind": "paper", "id": r["id"], "ts": r["ts"], "strategy": r["strategy"], "title": r["title"] or "",
            "event": r["event"] or "", "ticker": r["ticker"], "side": r["side"], "price": r["price"],
            "count": _float(r["count"]), "filled": _float(r["count"]), "status": "settled" if settled else "open",
            "state": _settled_state(paid) if settled else "unknown" if r["settled"] else "open",
            "cost": r["cost"], "pnl": r["pnl"] if settled else None, "error": ""}


def trade_list(limit):
    """Every live order and paper trade, newest first. Returns (rows, more to show?)."""
    live = db.query("""SELECT o.id, o.ts, o.strategy, o.ticker, o.side, o.count, o.price, o.filled, o.status,
        o.error, sg.title, sg.event, st.result FROM orders o
        LEFT JOIN signals sg ON sg.id = o.signal_id LEFT JOIN settlements st ON st.ticker = o.ticker
        ORDER BY o.ts DESC, o.id DESC LIMIT ?""", limit + 1)
    pap = db.query("""SELECT p.id, p.ts, p.strategy, p.event, p.ticker, p.side, p.count, p.price, p.cost,
        p.settled, p.result, p.pnl, sg.title FROM paper p LEFT JOIN signals sg ON sg.id = p.signal_id
        ORDER BY p.ts DESC, p.id DESC LIMIT ?""", limit + 1)
    rows = [_live_trade(r) for r in live] + [_paper_trade(r) for r in pap]
    rows.sort(key=lambda t: (t["ts"] or 0, t["id"]), reverse=True)
    return rows[:limit], len(rows) > limit


def trade_totals():
    """Per strategy, real and practice kept apart. Counts every trade, not just those shown.
    Live fills are summed here, fee by fee, as the ledger does; paper rows (there can be
    many) are summed by SQLite, with the same rules as _count."""
    live = collections.defaultdict(_tally)
    for r in db.query("""SELECT o.strategy, o.side, o.filled, o.price, st.result FROM orders o
            LEFT JOIN settlements st ON st.ticker = o.ticker WHERE o.filled > 0"""):
        _count(live[r["strategy"] or "?"], _fill_cost(r["filled"], r["price"]), r["result"], r["side"],
               _live_pnl(r["filled"], r["price"], r["side"], r["result"]))
    done = "settled = 1 AND pnl IS NOT NULL"   # booked: yes, no or a split; not "unknown"
    pap = {}
    for r in db.query(f"""SELECT COALESCE(strategy, '?') AS strategy, COUNT(*) AS trades,
            SUM(CASE WHEN {done} THEN 1 ELSE 0 END) AS settled,
            SUM(CASE WHEN {done} AND result = side THEN 1 ELSE 0 END) AS wins,   -- a split is no win
            COALESCE(SUM(CASE WHEN {done} THEN pnl END), 0) AS pnl, COALESCE(SUM(cost), 0) AS cost,
            COALESCE(SUM(CASE WHEN NOT ({done}) THEN cost END), 0) AS open_cost
            FROM paper GROUP BY COALESCE(strategy, '?')"""):
        pap[r.pop("strategy")] = {**r, "open": r["trades"] - r["settled"]}
    return {"live": dict(live), "paper": pap}


# ---------- the Research tab ----------

def _research_file(name):
    """(data, problem) for <name>.json in the exchange's research folder. A missing file is no problem."""
    path = os.path.join(_research_folder(), f"{name}.json")
    if not os.path.exists(path):
        return None, None
    try:
        with open(path) as f:
            return json.load(f), None
    except (OSError, ValueError) as e:
        return None, f"{name}.json can't be read: {e}"


def edge_results():
    """Practice and real results per research edge. Each trade is matched to its edge
    through the signal that made it (signals.meta["edge"]), as promote matches them.
    Practice also counts the distinct markets and events that settled, since that is
    what promotion needs (re-buying one market proves nothing new)."""
    out = collections.defaultdict(lambda: {"paper": {**_tally(), "markets": set(), "events": []},
                                           "live": _tally()})
    for r in db.query("""SELECT p.ts, p.ticker, p.event, p.side, p.cost, p.settled, p.result, p.pnl, s.meta
            FROM paper p JOIN signals s ON s.id = p.signal_id WHERE p.strategy = ?""", EDGE_STRATEGY):
        meta = _json(r["meta"])
        eid = meta.get("edge")
        if not eid:
            continue
        t = out[str(eid)]["paper"]
        result = r["result"] if r["settled"] else None
        _count(t, r["cost"], result, r["side"], r["pnl"])
        if _paid(result, r["side"]) is not None:
            t["markets"].add(r["ticker"])
            t["events"].append({"event": r["event"] or r["ticker"], "ts": r["ts"], "meta": meta})
    for r in db.query("""SELECT o.side, o.filled, o.price, st.result, s.meta FROM orders o
            JOIN signals s ON s.id = o.signal_id LEFT JOIN settlements st ON st.ticker = o.ticker
            WHERE o.strategy = ? AND o.filled > 0""", EDGE_STRATEGY):
        eid = _json(r["meta"]).get("edge")
        if eid:
            _count(out[str(eid)]["live"], _fill_cost(r["filled"], r["price"]), r["result"], r["side"],
                   _live_pnl(r["filled"], r["price"], r["side"], r["result"]))
    for v in out.values():
        v["paper"]["markets"], v["paper"]["events"] = len(v["paper"]["markets"]), _groups(v["paper"]["events"])
    return out


def _groups(trades):
    """How many independent groups promotion counts these trades as: a series closing on one
    day wins or loses together (promote._clusters), so its trades count once."""
    try:
        return len(set(_module("research.promote")._clusters(trades)))
    except Exception:
        return len({t["event"] for t in trades})


def _verdict(results):
    """What promote last worked out for an edge (edges.json "results"), cleaned up."""
    if not isinstance(results, dict):
        return None
    return {k: _float(results.get(k)) for k in ("trades", "paper_trades", "events", "pnl", "roi", "roi_lo")}


def research_detail(cfg):
    data, problem = _research_file("edges")
    results = edge_results()
    edges = []
    for e in sorted(_edge_list(data), key=_edge_sort):
        eid = str(e.get("id", ""))
        r = results.get(eid) or {"paper": {**_tally(), "markets": 0, "events": 0}, "live": _tally()}
        # No side means the edge came from YES and NO pooled together: favorites never trades it.
        edges.append({"id": eid, "series": str(e.get("series", "")), "status": str(e.get("status", "")),
                      "side": _side(e.get("side")), "note": str(e.get("note", ""))[:200],
                      **{k: str(e.get(k) or "") for k in ("first_seen", "last_tested", "promoted_at", "retired_at")},
                      **{k: _float(e.get(k)) for k in ("lo", "hi", "hours_before_close", "roi", "roi_lo",
                                                       "trades", "events")},
                      "paper": r["paper"], "live": r["live"], "verdict": _verdict(e.get("results"))})
    words, words_problem = _research_file("words")
    try:
        promote = _module("research.promote")
        need = {"markets": getattr(promote, "MIN_PAPER_TRADES", None), "events": getattr(promote, "MIN_EVENTS", None)}
    except NotInstalled:
        need = {"markets": None, "events": None}
    return {"edges": edges, "counts": dict(collections.Counter(e["status"] or "unknown" for e in edges)),
            "words": _words_summary(words), "last": _last_research(), "need": need,
            "favorites_mode": (cfg["strategies"].get(EDGE_STRATEGY) or {}).get("mode", "off"),
            "problems": [p for p in (problem, words_problem) if p]}


# ---------- the Test an idea tab ----------

def parse_series(value):
    """'kxhighny, KXFED' (or a list) -> ['KXHIGHNY', 'KXFED']."""
    if isinstance(value, str):
        parts = re.split(r"[\s,]+", value)
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        parts = value
    else:
        raise BadRequest("Type one or more series, like KXHIGHNY.")
    out = []
    for p in parts:
        t = p.strip().upper()
        if not t:
            continue
        if not TICKER.match(t):
            raise BadRequest(f"{t[:40]} doesn't look like a series name. Use letters, numbers and dashes.")
        if t not in out:
            out.append(t)
    if not out:
        raise BadRequest("Type one or more series, like KXHIGHNY.")
    if len(out) > MAX_TEST_SERIES:
        raise BadRequest(f"Test at most {MAX_TEST_SERIES} series at a time.")
    return out


def parse_hours(v):
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise BadRequest("Hours before close must be a number.")
    try:
        h = float(v)
    except ValueError:
        raise BadRequest("Hours before close must be a number.")
    if not math.isfinite(h) or not 0 < h <= MAX_TEST_HOURS:
        raise BadRequest(f"Hours before close must be more than 0 and at most {MAX_TEST_HOURS}.")
    return int(h) if h.is_integer() else h


class Backtests:
    """Runs one manual backtest at a time, in a background thread: a test can take many
    minutes of API calls, and the page has to stay quick meanwhile. A test only reports
    and records. It never trades, and it never changes edges.json."""

    def __init__(self):
        self.lock = threading.Lock()
        self.job = None       # the test running now
        self.last = None      # how the last one ended
        self.thread = None

    def start(self, series, hours, client_factory):
        """Start a test. False if one is already running."""
        with self.lock:
            if self.job:
                return False
            self.job = {"series": list(series), "hours_before_close": hours, "started": time.time()}
            # A daemon thread: stopping the server may cut a test short, which is harmless.
            self.thread = threading.Thread(target=self._work, args=(dict(self.job), client_factory),
                                           name="kal-backtest", daemon=True)
            self.thread.start()
        return True

    def _work(self, job, client_factory):
        try:
            bt = _module("research.backtest")
            cfg = config.load()
            res = bt.run(job["series"], hours_before_close=job["hours_before_close"], client=client_factory(),
                         max_markets=int(cfg.get("research_max_markets") or config.DEFAULTS["research_max_markets"]))
            end = {"ok": True, "observations": res.get("observations"), "edges": len(res.get("edges") or [])}
        except Exception as e:
            log.warning("backtest of %s failed", job["series"], exc_info=True)
            end = {"ok": False, "error": str(e)[:300] or e.__class__.__name__}
        with self.lock:
            self.job = None
            self.last = {**job, **end, "finished": time.time()}

    def state(self):
        with self.lock:
            return {"running": dict(self.job) if self.job else None, "last": dict(self.last) if self.last else None}


def _side(v):
    """'yes' or 'no', or None when a result mixes both sides."""
    return v if v in ("yes", "no") else None


def _calibration_row(bt, row):
    out = {k: _float(row.get(k)) for k in ("lo", "hi", "trades", "events", "win_rate", "roi", "roi_lo")}
    out["side"] = _side(row.get("side"))
    try:
        out["edge"] = bool(bt.is_edge(row))
    except (KeyError, TypeError, ValueError):
        out["edge"] = False  # a row we can't judge is never called an edge
    return out


def _mention(best):
    if not isinstance(best, dict):
        return None
    out = {k: _float(best.get(k)) for k in ("margin", "roi", "roi_lo", "trades", "events")}
    out["enough"] = bool(best.get("enough", out["roi_lo"] is not None))
    return out


def backtest_results(bt, kind, limit):
    """Past backtests, newest first, in one shape whoever ran them: this page ("manual"),
    or daily research (one "calibration" row per series, and "mentions")."""
    where, args = ("", ()) if kind == "all" else ("WHERE kind = ?", (kind,))
    rows = db.query(f"SELECT id, ts, kind, params, results FROM backtests {where} ORDER BY id DESC LIMIT ?",
                    *args, limit)
    out = []
    for r in rows:
        params, res = _json(r["params"]), _json(r["results"])
        series = params.get("series")
        series = [series] if isinstance(series, str) else [s for s in series or [] if isinstance(s, str)]
        calib = [_calibration_row(bt, x) for x in res.get("calibration") or [] if isinstance(x, dict)]
        mentions = res if r["kind"] == "mentions" else res.get("mentions")
        out.append({"id": r["id"], "ts": r["ts"], "kind": r["kind"], "series": series,
                    "hours_before_close": _float(params.get("hours_before_close")),
                    "observations": _int(res.get("observations")), "events": _int(res.get("events")),
                    "calibration": calib, "edges": sum(x["edge"] for x in calib),
                    "mentions": _mention(mentions.get("best") if isinstance(mentions, dict) else None)})
    return out


class SeriesList:
    """Kalshi's list of every series, kept for an hour: it's thousands of rows, it barely
    changes, and each search shouldn't download it again. It answers .series() like the
    client does, so kal.research.backtest.find_series can search it as it is."""

    def __init__(self, client_factory, ttl=SERIES_TTL, clock=time.time):
        self.client_factory, self.ttl, self.clock = client_factory, ttl, clock
        self.lock = threading.Lock()
        self.cache = {}  # env -> (when fetched, rows)

    def series(self, category=None):
        env = config.load()["env"]
        with self.lock:
            hit = self.cache.get(env)
        if hit and self.clock() - hit[0] < self.ttl:
            return hit[1]
        rows = [s for s in self.client_factory().series() or []
                if isinstance(s, dict) and isinstance(s.get("ticker"), str)]
        with self.lock:
            self.cache[env] = (self.clock(), rows)
        return rows


def _series_rank(s, q):
    """Exact ticker first, then tickers that start with it, then whole words in the title."""
    ticker, title = s["ticker"].lower(), str(s.get("title") or "").lower()
    return ticker != q, not ticker.startswith(q), q not in re.split(r"\W+", title), title


# ---------- changing the config ----------

def _number(key, v):
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise BadRequest(f"{key} must be a number")
    try:
        v = float(v)
    except ValueError:
        raise BadRequest(f"{key} must be a number")
    if not math.isfinite(v):
        raise BadRequest(f"{key} must be a number")
    if key in MIN_INTERVAL and v < MIN_INTERVAL[key]:
        raise BadRequest(f"{key} must be at least {MIN_INTERVAL[key]}")
    return int(v) if isinstance(config.DEFAULTS[key], int) and v.is_integer() else v


def clean(changes, known_strategies):
    """Check types and keys of a dashboard change. Raises BadRequest."""
    if not isinstance(changes, dict) or not changes:
        raise BadRequest("Send the settings to change.")
    out = {}
    for key, value in changes.items():
        if key == "disabled_reason":
            raise BadRequest("Use the Reset button to clear a stop.")
        if key == "strategies":
            if not isinstance(value, dict):
                raise BadRequest("strategies must be an object")
            out["strategies"] = {}
            for name, s in value.items():
                if name not in known_strategies:
                    raise BadRequest(f"Unknown strategy: {name}")
                # Only the mode is set from here. Other strategy settings (like a hand
                # check that unlocks real money) stay in config.json on purpose.
                if not isinstance(s, dict) or set(s) != {"mode"}:
                    raise BadRequest(f"{name}: only mode can be changed here")
                if s["mode"] not in config.MODES:
                    raise BadRequest(f"{name}: mode must be off, paper or live")
                out["strategies"][name] = {"mode": s["mode"]}
            continue
        if key not in config.DEFAULTS:
            raise BadRequest(f"Unknown setting: {key}")
        default = config.DEFAULTS[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise BadRequest(f"{key} must be true or false")
        elif isinstance(default, (int, float)):
            value = _number(key, value)
        elif isinstance(default, str):
            if not isinstance(value, str):
                raise BadRequest(f"{key} must be text")
            value = value.strip()
        out[key] = value
    return out


def _merged(base, changes):
    out = json.loads(json.dumps(base))
    for k, v in changes.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merged(out[k], v)
        else:
            out[k] = v
    return out


def apply_config(changes, sched):
    """Validate and save a change. Returns (new config, notes for the user)."""
    now = config.load()
    try:
        known = set(now["strategies"]) | set(load_all())
    except Exception:
        known = set(now["strategies"])
    changes = clean(changes, known)
    if changes.get("enabled") is False:
        config.turn_off()  # OFF must always work, whatever else is in the request or file
        changes = {k: v for k, v in changes.items() if k != "enabled"}
        now = config.load()
    after = _merged(now, changes)

    if changes.get("enabled") is True and now["disabled_reason"]:
        raise BadRequest(f"The bot stopped itself: {now['disabled_reason']} Press Reset first.")
    if "env" in changes and changes["env"] != now["env"] and (now["enabled"] or after["enabled"]):
        raise BadRequest("Turn the bot off before you switch.")
    live = [n for n, s in after["strategies"].items() if s.get("mode") == "live"]
    if after["enabled"] and live and float(after["budget"]) <= 0:
        if now["enabled"]:
            raise BadRequest("The amount must be more than $0 while the bot is on. To stop, turn it off.")
        raise BadRequest("First, pick how much money the bot can use.")

    try:
        # switch(): a change of exchange waits for a trade run to finish, so no run's books
        # end up split between the two exchanges.
        new = config.switch(changes) if changes else config.load()
    except config.Busy:
        raise BadRequest("A trade run is still going. Try again in a minute.")
    except (ValueError, TypeError, KeyError) as e:
        raise BadRequest(str(e))
    if new["enabled"] and not now["enabled"]:
        sched.run_now("trade")  # don't make someone who just pressed ON wait 5 minutes
    return new, []


# ---------- the app ----------

class _Fails:
    """Wrong-password counter per address, so a password can't be guessed by brute force."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seen = collections.defaultdict(collections.deque)

    def blocked(self, addr):
        with self.lock:
            q = self.seen[addr]
            while q and q[0] < time.time() - FAIL_WINDOW:
                q.popleft()
            return len(q) >= MAX_FAILS

    def add(self, addr):
        with self.lock:
            self.seen[addr].append(time.time())


def create_app(scheduler=None, market_client=None):
    """The Flask app. `scheduler` is shared with the background loop; main() starts it.
    `market_client()` makes the key-less client the backtester and series search use
    (tests pass a fake)."""
    app = Flask(__name__)
    app.json.sort_keys = False
    sched = scheduler or Scheduler()
    market_client = market_client or _market_client
    runner, series_list = Backtests(), SeriesList(market_client)
    app.extensions.update(kal_scheduler=sched, kal_backtests=runner, kal_series=series_list)
    password = _password()
    fails = _Fails()
    alert_lock, last_alert = threading.Lock(), [0.0]

    def err(message, code):
        return jsonify(error=message), code

    @app.before_request
    def guard():
        if password:
            addr = request.remote_addr or "?"
            if fails.blocked(addr):
                return "Too many wrong passwords. Wait 15 minutes.", 429
            auth = request.authorization
            given = (auth.password or "") if auth and auth.type == "basic" else ""
            if not hmac.compare_digest(given.encode(), password.encode()):
                if auth:
                    fails.add(addr)
                return "Password needed.", 401, {"WWW-Authenticate": 'Basic realm="kal", charset="UTF-8"'}
        else:
            if not is_local(request.host):
                return "Open this page from http://127.0.0.1, or set KAL_DASHBOARD_PASSWORD.", 403
            if request.headers.get("X-Forwarded-For") or request.headers.get("Forwarded"):
                return "This is behind a proxy. Set KAL_DASHBOARD_PASSWORD first.", 403
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc not in (request.host, request.headers.get("X-Forwarded-Host")):
                return err("Blocked: that came from another site.", 403)

    @app.after_request
    def headers(resp):
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(BadRequest)
    def bad(e):
        return err(str(e), 400)

    @app.errorhandler(NotInstalled)
    def not_installed(e):
        return err(str(e), 501)

    @app.errorhandler(404)
    def not_found(e):
        return err("Not found.", 404) if request.path.startswith("/api/") else ("Not found.", 404)

    @app.errorhandler(405)
    def not_allowed(e):
        return err("Wrong method.", 405)

    @app.errorhandler(500)
    def crashed(e):
        return err("Something broke. Check the server log.", 500)

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise BadRequest("Send JSON.")
        return data

    def int_arg(name, default, lo, hi):
        raw = request.args.get(name)
        if raw is None:
            return default
        try:
            v = int(raw)
        except ValueError:
            raise BadRequest(f"{name} must be a whole number")
        return min(hi, max(lo, v))

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def state():
        return jsonify(build_state(sched))

    @app.post("/api/config")
    def set_config():
        new, notes = apply_config(body(), sched)
        return jsonify(ok=True, config=new, notes=notes)

    @app.post("/api/reset-trip")
    def reset_trip():
        body()  # JSON only, so another site can't clear a safety stop
        config.clear_trip()
        return jsonify(ok=True, config=config.load())

    @app.post("/api/run")
    def run():
        kind = body().get("kind")
        if kind not in ("trade", "research"):
            raise BadRequest("kind must be trade or research")
        result = sched.run_now(kind)
        return jsonify(result), 200 if result["started"] else 409

    @app.get("/api/trades")
    def trades():
        rows, more = trade_list(int_arg("limit", TRADES_SHOWN, 1, TRADES_MAX))
        return jsonify(trades=rows, more=more, totals=trade_totals(), env=config.load()["env"])

    @app.get("/api/research")
    def research():
        return jsonify(research_detail(config.load()))

    @app.get("/api/backtests")
    def backtests():
        kind = request.args.get("kind", "manual")
        if kind not in ("manual", "calibration", "mentions", "all"):
            raise BadRequest("kind must be manual, calibration, mentions or all")
        bt = _module("research.backtest")
        return jsonify(now=time.time(), **runner.state(),
                       results=backtest_results(bt, kind, int_arg("limit", BACKTESTS_SHOWN, 1, 200)),
                       min_trades=getattr(bt, "MIN_TRADES", None), min_events=getattr(bt, "MIN_EVENTS", None),
                       max_series=MAX_TEST_SERIES, hours_default=config.load()["research_hours_before_close"])

    @app.post("/api/backtest")
    def start_backtest():
        data = body()
        series = parse_series(data.get("series"))
        hours = parse_hours(data.get("hours_before_close", config.load()["research_hours_before_close"]))
        _module("research.backtest")  # if it can't load, say so now rather than from the thread
        if not runner.start(series, hours, market_client):
            return err("A test is already running. Wait for it to finish.", 409)
        return jsonify(ok=True, **runner.state()), 202

    @app.get("/api/series")
    def find_series():
        q = " ".join((request.args.get("q") or "").split())
        if len(q) < 2:
            raise BadRequest("Type at least 2 letters.")
        if len(q) > 60:
            raise BadRequest("That's too long. Try a word or two.")
        bt = _module("research.backtest")
        try:
            found = bt.find_series(series_list, q)
        except Exception as e:
            log.warning("series search failed", exc_info=True)
            return err(f"Couldn't get the list from Kalshi. Try again soon. ({str(e)[:150]})", 502)
        found.sort(key=lambda s: _series_rank(s, q.lower()))
        return jsonify(query=q, total=len(found), series=[
            {"ticker": s["ticker"], "title": str(s.get("title") or "")[:200],
             "category": str(s.get("category") or "")[:80], "frequency": str(s.get("frequency") or "")[:40]}
            for s in found[:SERIES_SHOWN]])

    @app.post("/api/test-alert")
    def test_alert():
        body()  # JSON only, so another site can't buzz the owner's phone
        if not notify.enabled():
            return err("Phone alerts are not set up yet. Do the 3 steps first.", 400)
        with alert_lock:
            if time.time() - last_alert[0] < ALERT_GAP:
                return err("Sent a moment ago. Wait a few seconds.", 429)
            last_alert[0] = time.time()
        sent = notify.send("kal test alert", "It works. You will get an alert like this if the bot "
                                             "stops itself or trades real money.", tags=("white_check_mark",))
        if not sent:
            return err("The alert did not send. Check the topic name, and that the server is online.", 502)
        return jsonify(ok=True, sent=True)

    @app.get("/api/selftest")
    def selftest():
        name = f"{__package__.rsplit('.', 1)[0]}.selftest"
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name != name:
                raise
            return err("Self-test is not installed yet.", 501)
        try:
            # Never the order probe from a button: it would send a real order.
            checks = mod.run(order_probe=False)
        except Exception as e:
            log.error("self-test crashed", exc_info=True)
            return err(f"Self-test crashed: {e}", 500)
        checks = [dict(c) for c in checks or []]
        return jsonify(checks=checks, ok=all(c.get("ok") for c in checks))

    return app


def main(host=None, port=None, scheduler=True):
    """Serve the dashboard (and run the scheduler) until Ctrl-C."""
    host = host or os.environ.get("KAL_DASHBOARD_HOST") or DEFAULT_HOST
    port = int(port or os.environ.get("KAL_DASHBOARD_PORT") or DEFAULT_PORT)
    problem = bind_problem(host, _password())
    if problem:
        raise SystemExit(problem)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sched = Scheduler()
    app = create_app(sched)
    server = make_server(host, port, app, threaded=True)
    if scheduler:
        sched.start()
    shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    print(f"Dashboard: http://{shown}:{port}")
    if not is_local(host):
        print("Password is on. Use HTTPS or an SSH tunnel, so nobody can read it on the way.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        sched.stop()
