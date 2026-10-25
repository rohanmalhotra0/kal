"""A short status report: is the bot on, how is it doing, what went wrong.

build() collects it from config and the db. maybe_push() puts it on the repo's
"status" branch, so the 2-hourly check-in (and the owner, on a phone) can read it
without logging in to the server.

Nothing secret goes in: no API key ID, no PEM, no tokens, no password. Config keys
are copied from an allowlist, and as a last guard the final text is scrubbed of
every secret value this process knows about.
"""
import base64
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import requests

from . import config, db, ledger, paper
from .client import fee

GITHUB_API = "https://api.github.com"
BRANCH = "status"
RUNS = 20
SECRET_ENV = ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KAL_GITHUB_TOKEN", "KAL_DASHBOARD_PASSWORD")
CONFIG_KEYS = ("enabled", "env", "budget", "reinvest_profits", "cash_floor", "max_per_trade",
               "max_per_arb", "max_per_event", "daily_loss_limit", "max_consecutive_errors",
               "kelly_fraction", "trade_interval_min", "research_interval_hours", "auto_promote",
               "paper_bankroll", "disabled_reason")
PEM = re.compile(r"-----BEGIN [A-Z ]+-----.*?(-----END [A-Z ]+-----|$)", re.S)
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

log = logging.getLogger(__name__)


class PushError(RuntimeError):
    pass


# ---------- building it ----------

def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if t else None


def _day_start(now):
    d = datetime.fromtimestamp(now, timezone.utc).date()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def _has_key():
    """Whether a key is set up. Says yes or no, never what the key is."""
    if not os.environ.get("KALSHI_API_KEY_ID"):
        return False
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH") or os.path.join(config.home(), "kalshi.pem")
    return bool(os.environ.get("KALSHI_PRIVATE_KEY")) or os.path.exists(path)


def _config(cfg):
    out = {k: cfg.get(k) for k in CONFIG_KEYS}
    out["strategies"] = {name: (s or {}).get("mode", "off") for name, s in (cfg.get("strategies") or {}).items()}
    out["has_api_key"] = _has_key()
    return out


def _equity():
    rows = db.query("SELECT * FROM equity ORDER BY ts DESC LIMIT 1")
    if not rows:
        return None
    r = rows[0]
    return {"ts": _iso(r["ts"]), "balance": r["balance"], "exposure": r["exposure"],
            "equity": r["equity"], "paper_equity": r["paper_equity"]}


def _today(now):
    start = _day_start(now)
    live = db.query("SELECT COALESCE(SUM(pnl), 0) AS p, COUNT(*) AS n FROM settlements WHERE settled_ts >= ?",
                    start)[0]
    pap = db.query("SELECT COALESCE(SUM(pnl), 0) AS p, COUNT(*) AS n FROM paper "
                   "WHERE settled = 1 AND settled_ts >= ?", start)[0]
    out = {"live_settled_pnl": round(live["p"], 2), "live_settled": live["n"],
           "paper_settled_pnl": round(pap["p"], 2), "paper_settled": pap["n"], "equity_change": None}
    day = db.get("day_start") or {}
    latest = db.query("SELECT equity FROM equity WHERE ts >= ? AND equity > 0 ORDER BY ts DESC LIMIT 1", start)
    today = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    if day.get("day") == today and latest:
        out["equity_change"] = round(latest[0]["equity"] - day["equity"], 2)
    return out


def _live_stats():
    """Per strategy, from our own fills: what was bought, and how the settled part did."""
    rows = db.query("""SELECT o.strategy, o.ticker, o.side, o.filled, o.price, s.result
        FROM orders o LEFT JOIN settlements s ON s.ticker = o.ticker WHERE o.filled > 0""")
    out = {}
    for r in rows:
        st = out.setdefault(r["strategy"] or "?", {"fills": 0, "spent": 0.0, "settled": 0, "wins": 0, "pnl": 0.0})
        cost = r["filled"] * r["price"] + fee(r["filled"], r["price"])
        st["fills"] += 1
        st["spent"] += cost
        if r["result"] in ("yes", "no"):
            won = r["result"] == r["side"]
            st["settled"] += 1
            st["wins"] += won
            st["pnl"] += r["filled"] * won - cost
    for st in out.values():
        st["spent"], st["pnl"] = round(st["spent"], 2), round(st["pnl"], 2)
    return out


def _strategies(cfg, now):
    modes = {name: (s or {}).get("mode", "off") for name, s in (cfg.get("strategies") or {}).items()}
    papers, lives = paper.stats(), _live_stats()
    sigs = db.query("SELECT strategy, COUNT(*) AS n FROM signals WHERE ts >= ? GROUP BY strategy", now - 86400)
    sigs = {r["strategy"]: r for r in sigs}
    out = {}
    for name in sorted(set(modes) | set(papers) | set(lives)):
        p = papers.get(name) or {}
        out[name] = {
            "mode": modes.get(name, "off"),
            "paper": {"trades": p.get("trades", 0), "settled": p.get("settled") or 0, "wins": p.get("wins") or 0,
                      "pnl": round(p.get("pnl") or 0, 2),
                      "roi": None if p.get("roi") is None else round(p["roi"], 4)},
            "live": lives.get(name, {"fills": 0, "spent": 0.0, "settled": 0, "wins": 0, "pnl": 0.0}),
            "signals_24h": (sigs.get(name) or {}).get("n", 0),
        }
    return out


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _research():
    d = os.path.join(config.home(), "research")
    out = {"edges": {}, "top_edges": [], "mentions": None, "last_run": None}
    raw = _read_json(os.path.join(d, "edges.json"))
    if isinstance(raw, dict):
        raw = raw.get("edges", list(raw.values()))
    edges = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
    for e in edges:
        st = str(e.get("status", "?"))
        out["edges"][st] = out["edges"].get(st, 0) + 1

    def rank(e):
        try:
            return (e.get("status") == "live", float(e.get("roi_lo") or 0))
        except (TypeError, ValueError):
            return (False, 0)
    for e in sorted((e for e in edges if e.get("status") in ("live", "candidate")), key=rank, reverse=True)[:5]:
        out["top_edges"].append({k: e.get(k) for k in ("id", "status", "roi", "roi_lo", "trades", "last_tested")})
    words = _read_json(os.path.join(d, "words.json"))
    if isinstance(words, dict):
        out["mentions"] = {k: words.get(k) for k in ("promoted", "roi", "roi_lo", "trades", "updated")}
    last = db.get("last_research")
    if isinstance(last, dict):
        out["last_run"] = {"ts": _iso(last.get("ts")), "ok": last.get("ok"), "error": last.get("error"),
                           "tested": len(last.get("tested") or []), "new_edges": len(last.get("new_edges") or []),
                           "changes": (last.get("changes") or [])[:10]}
    return out


def _runs():
    out = []
    for r in db.query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", RUNS):
        try:
            msg = json.loads(r["message"] or "{}")
        except ValueError:
            msg = {"error": (r["message"] or "")[:300]}
        if not isinstance(msg, dict):
            msg = {}
        row = {"ts": _iso(r["ts"]), "kind": r["kind"], "ok": bool(r["ok"]),
               "seconds": round(r["duration"] or 0, 1)}
        for k in ("error", "skipped", "signals", "live_trades", "paper_trades"):
            if msg.get(k) not in (None, ""):
                row[k] = msg[k] if not isinstance(msg[k], str) else msg[k][:300]
        notes = [n for n in msg.get("notes") or [] if isinstance(n, str)]
        if notes:
            row["notes"] = [n[:200] for n in notes[:5]]
        out.append(row)
    return out


def _health(cfg, now):
    """Plain warnings, worst first. Empty when all looks fine."""
    out = []
    if cfg.get("disabled_reason"):
        out.append(f"Stopped itself: {cfg['disabled_reason']}")
    elif not cfg.get("enabled"):
        out.append("Bot is off.")
    if not _has_key():
        out.append("No Kalshi API key: everything is paper only.")
    errors = db.get("consecutive_errors", 0) or 0
    if errors:
        out.append(f"{errors} trade runs failed in a row.")
    trades = db.query("SELECT ts FROM runs WHERE kind = 'trade' ORDER BY id DESC LIMIT 1")
    every = max(60, float(cfg.get("trade_interval_min") or 5) * 60)
    if cfg.get("enabled") and (not trades or now - trades[0]["ts"] > 3 * every + 600):
        out.append("No trade run lately. Is the scheduler running?")
    last = db.get("last_research") or {}
    if not last.get("ts"):
        out.append("Research has not run yet.")
    elif now - float(last["ts"]) > 2 * 86400:
        out.append("No research in 2 days.")
    elif last.get("ok") is False:
        out.append("Last research run failed.")
    return out


def build(now=None):
    """The whole report, as plain JSON-ready data."""
    now = time.time() if now is None else now
    cfg = config.load()
    rep = {
        "generated": _iso(now),
        "code": _version(),
        "health": _health(cfg, now),
        "tripped": cfg.get("disabled_reason") or "",
        "config": _config(cfg),
        "equity": _equity(),
        "today": _today(now),
        "ledger": {"capital": round(ledger.capital(cfg), 2), "open_cost": round(ledger.open_cost(), 2),
                   "available": round(ledger.available(cfg), 2), "realized": round(ledger.realized(), 2)},
        "paper": {"bankroll": cfg.get("paper_bankroll"), "realized": round(paper.realized(), 2),
                  "open_trades": len(paper.open_rows())},
        "strategies": _strategies(cfg, now),
        "research": _research(),
        "consecutive_errors": db.get("consecutive_errors", 0) or 0,
        "runs": _runs(),
    }
    return _clean(rep)


# ---------- showing it ----------

def _money(v):
    return "-" if v is None else f"${v:,.2f}"


def markdown(rep, short=False):
    """Plain, short text. short=True is the few lines `python -m kal status` prints."""
    c = rep["config"]
    state = "STOPPED" if rep["tripped"] else ("ON" if c["enabled"] else "OFF")
    money = "real money" if c["env"] == "prod" else "demo (fake money)"
    eq, today, led = rep.get("equity") or {}, rep["today"], rep["ledger"]
    made = f"Made {rep['generated']}" + (f" by code {rep['code']}" if rep.get("code") else "")
    lines = [f"# kal status: {state}", "", made + ".", "",
             f"- Bot: {state}. Exchange: {money}. API key: {'yes' if c['has_api_key'] else 'no'}.",
             f"- Budget: {_money(c['budget'])}. Capital now: {_money(led['capital'])}. "
             f"Free to trade: {_money(led['available'])}.",
             f"- Today: live {_money(today['live_settled_pnl'])} settled"
             + (f", equity {today['equity_change']:+.2f}" if today["equity_change"] is not None else "")
             + f". Paper {_money(today['paper_settled_pnl'])} settled.",
             (f"- Equity: {_money(eq['equity'])} (Kalshi balance {_money(eq['balance'])}). "
              f"Paper: {_money(eq['paper_equity'])}." if eq else "- Equity: no runs yet.")]
    if rep["health"]:
        lines += ["", "## Needs a look", ""] + [f"- {h}" for h in rep["health"]]
    else:
        lines += ["", "All looks fine."]
    if short:
        return "\n".join(lines) + "\n"
    lines += ["", "## Strategies", "", "| Strategy | Mode | Paper trades | Paper P&L | Live fills | Live P&L |",
              "|---|---|---|---|---|---|"]
    for name, s in rep["strategies"].items():
        lines.append(f"| {name} | {s['mode']} | {s['paper']['trades']} | {_money(s['paper']['pnl'])} "
                     f"| {s['live']['fills']} | {_money(s['live']['pnl'])} |")
    r = rep["research"]
    lines += ["", "## Research", "",
              "- Edges: " + (", ".join(f"{n} {k}" for k, n in sorted(r["edges"].items())) or "none yet") + "."]
    for e in r["top_edges"]:
        lines.append(f"  - {e['id']} ({e['status']}): worst-case ROI {e['roi_lo']}, {e['trades']} trades")
    if r["mentions"]:
        m = r["mentions"]
        lines.append(f"- Mentions: {'live' if m['promoted'] else 'paper only'}, worst-case ROI {m['roi_lo']}.")
    if r["last_run"]:
        lr = r["last_run"]
        lines.append(f"- Last research: {lr['ts']}, {'ok' if lr['ok'] else 'FAILED'}, "
                     f"{lr['tested']} series tested, {lr['new_edges']} new edges.")
    lines += ["", f"## Last {len(rep['runs'])} runs" if rep["runs"] else "## Runs", ""]
    if not rep["runs"]:
        lines.append("- None yet.")
    for run in rep["runs"]:
        what = run.get("error") or run.get("skipped") or (
            f"{run.get('signals', 0)} signals, {run.get('live_trades', 0)} live, {run.get('paper_trades', 0)} paper"
            if run["kind"] == "trade" else "ok")
        lines.append(f"- {run['ts']} {run['kind']} {'ok' if run['ok'] else 'FAILED'}: {what}")
    return scrub("\n".join(lines) + "\n")


def scrub(text):
    """Remove any PEM block and the value of every secret env var. Belt and braces."""
    text = PEM.sub("[removed key]", text)
    for var in SECRET_ENV:
        v = os.environ.get(var)
        if v and len(v) >= 6:
            text = text.replace(v, "[removed]")
            text = text.replace(json.dumps(v)[1:-1], "[removed]")  # as it looks inside JSON
    return text


def _clean(obj):
    """scrub() every string in the report (error messages could quote anything)."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return scrub(str(obj))


def _version():
    """The git commit the bot is running, so a check-in knows which code it is looking at.
    Read from .git by hand: running git as the service user trips its ownership check."""
    git = os.path.join(config.ROOT, ".git")
    try:
        with open(os.path.join(git, "HEAD")) as f:
            head = f.read().strip()
        if not head.startswith("ref: "):
            return head[:10] or None
        ref = head[5:]
        if os.path.exists(os.path.join(git, ref)):
            with open(os.path.join(git, ref)) as f:
                return f.read().strip()[:10] or None
        with open(os.path.join(git, "packed-refs")) as f:
            for line in f:
                if line.rstrip().endswith(" " + ref):
                    return line.split()[0][:10]
    except OSError:
        pass
    return None


# ---------- pushing it to GitHub ----------

class _GitHub:
    def __init__(self, token, repo):
        self.repo = repo
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"}

    def call(self, method, path, ok=(200, 201), **kw):
        r = requests.request(method, f"{GITHUB_API}/repos/{self.repo}{path}", headers=self.headers,
                             timeout=30, **kw)
        if r.status_code not in ok:
            raise PushError(f"GitHub said {r.status_code} to {method} {path}: {r.text[:200]}")
        return r

    def branch_exists(self):
        return self.call("GET", f"/git/ref/heads/{BRANCH}", ok=(200, 404)).status_code == 200

    def create_branch(self, files, message):
        """A branch with no history, holding only the report files (no code)."""
        tree = [{"path": p, "mode": "100644", "type": "blob", "content": text} for p, text in files.items()]
        tree_sha = self.call("POST", "/git/trees", json={"tree": tree}).json()["sha"]
        commit = self.call("POST", "/git/commits", json={"message": message, "tree": tree_sha, "parents": []})
        r = self.call("POST", "/git/refs", ok=(201, 422),
                      json={"ref": f"refs/heads/{BRANCH}", "sha": commit.json()["sha"]})
        return r.status_code == 201  # 422: someone made it first

    def put(self, path, text, message):
        for attempt in range(2):
            got = self.call("GET", f"/contents/{path}", ok=(200, 404), params={"ref": BRANCH})
            body = {"message": message, "branch": BRANCH,
                    "content": base64.b64encode(text.encode()).decode()}
            if got.status_code == 200:
                body["sha"] = got.json()["sha"]
            r = self.call("PUT", f"/contents/{path}", ok=(200, 201, 409, 422), json=body)
            if r.status_code in (200, 201):
                return
        raise PushError(f"GitHub kept saying {r.status_code} to PUT /contents/{path}: {r.text[:200]}")


def maybe_push(rep=None):
    """Write report.json and report.md to the "status" branch. False (and no network)
    unless KAL_GITHUB_TOKEN and KAL_GITHUB_REPO (owner/repo) are set. Raises PushError
    if GitHub refuses, so the scheduler records the failure instead of hiding it."""
    token, repo = os.environ.get("KAL_GITHUB_TOKEN"), os.environ.get("KAL_GITHUB_REPO")
    if not token or not repo:
        return False
    if not REPO.match(repo):
        raise PushError("KAL_GITHUB_REPO must look like owner/repo")
    rep = rep or build()
    files = {"report.json": scrub(json.dumps(rep, indent=1, default=str)) + "\n", "report.md": markdown(rep)}
    message = f"Status report {rep.get('generated') or ''}".strip()
    gh = _GitHub(token, repo)
    try:
        if not gh.branch_exists() and gh.create_branch(files, message):
            return True
        for path, text in files.items():
            gh.put(path, text, message)
    except requests.RequestException as e:
        raise PushError(f"couldn't reach GitHub: {type(e).__name__}") from None
    return True
