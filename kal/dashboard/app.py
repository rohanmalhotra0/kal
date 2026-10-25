"""The dashboard: a JSON API plus one page (static/app.js polls /api/state).

Anyone who can open this page can turn on real-money trading, so:
- It listens on 127.0.0.1 unless told otherwise.
- KAL_DASHBOARD_PASSWORD turns on HTTP basic auth for every route. Without it, the
  dashboard refuses to listen anywhere but localhost, and answers only requests
  addressed to localhost (so a web page can't reach it by DNS tricks).
- Every POST must send JSON. A web page on another site can't send JSON here without
  the browser asking first, and we never say yes, so it can't flip the switch.
"""
import collections
import glob
import hmac
import importlib
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request
from werkzeug.serving import make_server

from .. import config, db, ledger, paper
from ..client import load_key
from ..scheduler import Scheduler
from ..strategies import load_all

log = logging.getLogger(__name__)

DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8787
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
RECENT = 25
CHART_DAYS = 30
CHART_POINTS = 400
TOP_EDGES = 12
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


# ---------- reading the state ----------

def _utc_midnight(now):
    d = datetime.fromtimestamp(now, timezone.utc).date()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def _has_key():
    try:
        return load_key(config.home())[0] is not None
    except Exception:
        return False  # a key file we can't read is no key at all


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
    money = {"capital": ledger.capital(cfg), "available": ledger.available(cfg),
             "paper_profit": paper.realized(), "paper_cash": paper.cash(cfg)}
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


def _float(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _edge_list(data):
    """edges.json is a list; tolerate {"edges": [...]} and {id: entry} too."""
    if isinstance(data, dict):
        data = data.get("edges", data)
    if isinstance(data, dict):
        data = [dict(v, id=v.get("id", k)) for k, v in data.items() if isinstance(v, dict)]
    return [e for e in data or [] if isinstance(e, dict)]


def _edges_summary(data):
    edges = _edge_list(data)
    counts = collections.Counter(str(e.get("status") or "unknown") for e in edges)
    order = {"live": 0, "candidate": 1, "retired": 2}
    edges.sort(key=lambda e: (order.get(e.get("status"), 3), -(_float(e.get("roi_lo")) or -9)))
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


def research_summary():
    out = {"files": [], "edges": None, "words": None, "last": None}
    for path in sorted(glob.glob(os.path.join(config.home(), "research", "*.json"))):
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
    last = db.get("last_research")
    if isinstance(last, dict):
        out["last"] = {"ts": last.get("ts"), "ok": last.get("ok"), "text": run_text("research", last)}
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
    s = {"now": now, "config": cfg, "has_key": _has_key(), "errors": errors}
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
        new = config.update(changes) if changes else config.load()
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


def create_app(scheduler=None):
    """The Flask app. `scheduler` is shared with the background loop; main() starts it."""
    app = Flask(__name__)
    app.json.sort_keys = False
    sched = scheduler or Scheduler()
    app.extensions["kal_scheduler"] = sched
    password = _password()
    fails = _Fails()

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
