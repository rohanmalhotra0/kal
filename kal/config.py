"""Settings the dashboard and CLI edit, stored in $KAL_HOME/config.json.

The file is the only source of truth: no env var can turn the bot on, change
its budget, or switch exchanges behind the dashboard's back.

A safety trip (daily loss limit, repeated errors, capital used up, a stranded
basket) sets `disabled_reason`, and the bot stays off until a person clears it.
Tripping never depends on the rest of the file being valid.
"""
import copy
import fcntl
import json
import math
import os
import threading
import time
from contextlib import ExitStack, contextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_lock = threading.RLock()
SWITCH_WAIT = 60   # seconds a switch of exchange waits for a trade run to finish

DEFAULTS = {
    "enabled": False,            # master switch
    "env": "prod",               # "demo" trades Kalshi's fake-money exchange (separate books)
    "budget": 0.0,               # capital given to the bot: the most it can ever lose in total
    "reinvest_profits": True,    # let the bot trade its own winnings too
    "cash_floor": 0.0,           # never trade the account balance below this
    "max_per_trade": 10.0,       # most a directional trade (or a half-built basket) can lose
    "max_per_arb": 30.0,         # most a locked-in arbitrage basket can cost
    "max_per_event": 20.0,       # markets in one event move together
    "max_exposure": 0.5,         # most of the capital tied up in directional bets at once
    "max_arb_days": 30,          # a locked-in trade must settle within this many days
    "min_arb_apr": 0.20,         # and pay at least this much a year (Kalshi pays ~4% on cash)
    "daily_loss_limit": 0.10,    # fraction of capital; past this the bot turns itself off
    "max_consecutive_errors": 3,
    "kelly_fraction": 0.25,      # bet a quarter of the Kelly size
    "trade_interval_min": 5,
    "research_interval_hours": 24,
    "research_max_series": 15,
    "research_hours_before_close": 6,
    "research_max_markets": 1500,
    "summary_hour_utc": 13,      # the daily phone summary goes out at this hour, UTC (kal.scheduler)
    "auto_promote": True,        # move paper ideas to live once they prove themselves
    "paper_bankroll": 1000.0,
    "strategies": {
        "basket_arb": {"mode": "live"},
        "ladder_arb": {"mode": "paper"},
        "date_ladder_arb": {"mode": "paper"},
        "fed_link_arb": {"mode": "paper"},
        "exact_link_arb": {"mode": "paper"},
        "range_link_arb": {"mode": "paper"},
        "bracket_arb": {"mode": "paper", "verified": False},
        "favorites": {"mode": "paper"},
        "mentions": {"mode": "paper"},
        "longshot_fade": {"mode": "paper"},
    },
    "disabled_reason": "",
}

MODES = ("off", "paper", "live")
_MONEY = ("budget", "cash_floor", "max_per_trade", "max_per_arb", "max_per_event", "paper_bankroll")
_POSITIVE_INTS = ("max_consecutive_errors", "trade_interval_min", "research_interval_hours",
                  "research_max_series", "research_max_markets", "max_arb_days")
# The money limits, and which way is safer (min: smaller is). Like the modes, each is one
# setting for both exchanges, so a switch to prod puts back what prod had (prod_settings).
# A new money limit goes here too.
LIMITS = {"budget": min, "cash_floor": max, "max_per_trade": min, "max_per_arb": min,
          "max_per_event": min, "max_exposure": min, "max_arb_days": min, "min_arb_apr": max,
          "daily_loss_limit": min, "kelly_fraction": min, "max_consecutive_errors": min,
          "reinvest_profits": min, "auto_promote": min}


def home():
    path = os.environ.get("KAL_HOME") or os.path.join(ROOT, "state")
    os.makedirs(path, exist_ok=True)
    return path


def _path():
    return os.path.join(home(), "config.json")


@contextmanager
def _locked():
    """Serialize read-modify-write across threads and processes (dashboard, daemon, CLI)."""
    with _lock:
        with open(os.path.join(home(), ".config.lock"), "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _raw():
    if not os.path.exists(_path()):
        return {}
    with open(_path()) as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _write(data):
    tmp = _path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _path())


def load():
    cfg = _merge(DEFAULTS, _raw())
    if cfg.get("disabled_reason"):
        cfg["enabled"] = False
    return cfg


def save(cfg):
    validate(cfg)
    with _locked():
        _write(cfg)


def update(changes):
    """Apply a partial update, validated, under one lock. Returns the new config."""
    with _locked():
        cfg = _merge(load(), changes)
        validate(cfg)
        _write(cfg)
    return load()


def validate(cfg):
    """Raise ValueError on anything unsafe or nonsensical."""
    for k in _MONEY:
        v = cfg.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"{k} must be a number")
        if v < 0:
            raise ValueError(f"{k} can't be negative")
    for k in _POSITIVE_INTS:
        v = cfg.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 1 or v != int(v):
            raise ValueError(f"{k} must be a whole number, 1 or more")
    v = cfg.get("summary_hour_utc")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v != int(v) \
            or not 0 <= v <= 23:
        raise ValueError("summary_hour_utc must be a whole hour from 0 to 23 (UTC)")
    fractions = {"daily_loss_limit": (0, 1), "kelly_fraction": (0, 1), "max_exposure": (0, 1)}
    for k, (lo, hi) in fractions.items():
        v = cfg.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo < v <= hi:
            raise ValueError(f"{k} must be a fraction above {lo} and at most {hi}")
    v = cfg.get("min_arb_apr")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 10:
        raise ValueError("min_arb_apr must be a yearly fraction between 0 and 10")
    v = cfg.get("research_hours_before_close")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0:
        raise ValueError("research_hours_before_close must be more than 0")
    if cfg.get("env") not in ("prod", "demo"):
        raise ValueError("env must be prod or demo")
    for k in ("enabled", "reinvest_profits", "auto_promote"):
        if not isinstance(cfg.get(k), bool):
            raise ValueError(f"{k} must be true or false")
    if not isinstance(cfg.get("strategies"), dict):
        raise ValueError("strategies must be a mapping")
    for name, s in cfg["strategies"].items():
        if not isinstance(s, dict) or s.get("mode") not in MODES:
            raise ValueError(f"{name}: mode must be one of {', '.join(MODES)}")


# What a stop alert (and the daily summary) says to do. It is read on a phone, where the
# dashboard can't be reached, and the bot is already off: so nothing is urgent.
STOPPED_HELP = ("The bot is off. It won't trade until you turn it back on. No rush.\n"
                "When you can, see why: sudo kal health\n"
                "When it's fixed: sudo kal on --clear-trip")


def trip(reason):
    """Turn the bot off for safety. Stays off until clear_trip(). Works even if the file
    holds invalid values, because it touches only these two keys and skips validation.

    The phone alert is "high", not "urgent": a stop already ends all trading, so it is
    news to read when awake, not an alarm for 3 AM."""
    with _locked():
        try:
            data = _raw()
        except (OSError, ValueError):
            data = {}
        data.update(enabled=False, disabled_reason=str(reason) or "stopped")
        _write(data)
    try:
        from . import notify
        notify.send("kal stopped itself", f"{reason}\n\n{STOPPED_HELP}", priority="high", tags=("no_entry",))
    except Exception:
        pass  # an alert must never block a trip


def turn_off():
    """The OFF switch. Like trip(), it can't be blocked by a bad value elsewhere in the file."""
    with _locked():
        try:
            data = _raw()
        except (OSError, ValueError):
            data = {}
        data["enabled"] = False
        _write(data)


def clear_trip():
    update({"disabled_reason": ""})


def command(rest):
    """How to type a kal command here. The server's `kal` wrapper sets KAL_CMD="sudo kal",
    so the hints match what the owner really types. (Display only: it changes nothing.)"""
    return f"{os.environ.get('KAL_CMD') or 'python -m kal'} {rest}"


# ---------- switching exchange ----------

class Busy(RuntimeError):
    """A trade run is still going, so the exchange can't be switched yet."""


@contextmanager
def no_trade_run(wait=None):
    """Hold the trade-run lock (kal.risk.run_lock), waiting up to `wait` seconds (default
    SWITCH_WAIT) for a run in progress to finish. Raises Busy if it doesn't."""
    from . import risk
    deadline = time.monotonic() + (SWITCH_WAIT if wait is None else wait)
    while True:
        stack = ExitStack()
        try:
            stack.enter_context(risk.run_lock())
            break
        except risk.Locked:
            stack.close()
            if time.monotonic() >= deadline:
                raise Busy("a trade run is still going") from None
            time.sleep(0.25)
    with stack:
        yield


def switch(changes, wait=None):
    """update() for a change that may switch exchange ("env"). Each exchange keeps its own
    books, and a trade run looks up which file to write as it goes, so a switch mid-run
    would split one basket across both. So a switch waits for any run to finish, is
    written while holding the run lock, and needs the bot off (the change itself may turn
    it on). Raises Busy if a run doesn't finish in time, ValueError while the bot is on."""
    if changes.get("env") in (None, load()["env"]):
        return update(changes)
    with no_trade_run(wait):
        cur = load()
        if cur["enabled"]:
            raise ValueError("turn the bot off before you switch")
        if changes["env"] == "prod":
            # Modes and money limits are one setting for both exchanges, so real money is set
            # on prod itself: back on prod, each gets what it had there (saved when leaving).
            # One turned live for practice on demo (fake money) goes back to paper, and a
            # practice budget or cap goes back to the real one. A budget in `changes` wins.
            back = {name: {"mode": mode} for name, mode in prod_modes(cur).items()
                    if mode != (cur["strategies"].get(name) or {}).get("mode")}
            changes = _merge(_merge(prod_settings(cur), {"strategies": back}), changes)
        elif cur["env"] == "prod":
            modes = {name: (s or {}).get("mode") for name, s in cur["strategies"].items()}
            changes = {**changes, "prod_modes": modes, "prod_settings": _settings(cur)}   # for the way back
        return update(changes)


def practice_live(cfg):
    """Strategies live now that aren't live by default (live for practice, if on demo)."""
    default = {name: (s or {}).get("mode") for name, s in DEFAULTS["strategies"].items()}
    return sorted(name for name, s in cfg["strategies"].items()
                  if (s or {}).get("mode") == "live" and default.get(name) != "live")


def prod_modes(cfg):
    """What each strategy's mode becomes on a switch to prod: the one it had on prod when it
    last left (cfg["prod_modes"]), else paper if it's live only for practice, else as it is."""
    saved = cfg.get("prod_modes") or {}
    practice = set(practice_live(cfg))
    return {name: saved.get(name) or ("paper" if name in practice else (s or {}).get("mode"))
            for name, s in cfg["strategies"].items()}


def _settings(cfg):
    """The money limits, and each strategy's settings besides its mode (like bracket_arb's
    hand check, "verified")."""
    return {**{k: cfg[k] for k in LIMITS},
            "strategies": {name: {k: v for k, v in (s or {}).items() if k != "mode"}
                           for name, s in cfg["strategies"].items()}}


def prod_settings(cfg):
    """What _settings() become on a switch to prod: what prod had when it last left
    (cfg["prod_settings"]). With nothing saved (it left prod before they were saved), each
    limit goes to the safer of where it is and its default, so the budget goes to $0, and a
    strategy setting goes to its default (a hand check is undone)."""
    saved, now = cfg.get("prod_settings") or {}, _settings(cfg)
    out = {k: saved[k] if k in saved else safer(now[k], DEFAULTS[k]) for k, safer in LIMITS.items()}
    kept = saved.get("strategies") or {}
    out["strategies"] = {
        name: {k: (kept.get(name) or {}).get(k, (DEFAULTS["strategies"].get(name) or {}).get(k, v))
               for k, v in s.items()}
        for name, s in now["strategies"].items()}
    return out


def budget_on(cfg, env):
    """The budget in force on `env` once switched there, before any new one typed with it:
    on a switch from demo to prod, the one prod had."""
    return prod_settings(cfg)["budget"] if env == "prod" and cfg["env"] != "prod" else cfg["budget"]


def back_on_prod(before, after):
    """{name: new mode} for the strategies a switch from demo to prod changed."""
    if before["env"] == "prod" or after["env"] != "prod":
        return {}
    out = {}
    for name, s in sorted(before["strategies"].items()):
        old, new = (s or {}).get("mode"), (after["strategies"].get(name) or {}).get("mode")
        if old != new:
            out[name] = new
    return out


def back_on_prod_note(moved):
    """The owner's line for back_on_prod(), or ""."""
    if not moved:
        return ""
    return (f"Back to the real-money modes: {', '.join(f'{n} {m}' for n, m in moved.items())}. "
            f"Modes on demo were practice.")


def settings_back_on_prod(before, after):
    """{setting: new value} for the _settings() a switch from demo to prod changed. A
    strategy's own setting is named like "bracket_arb.verified"."""
    if before["env"] == "prod" or after["env"] != "prod":
        return {}
    was, now = _settings(before), _settings(after)
    out = {k: now[k] for k in LIMITS if now[k] != was[k]}
    for name, s in sorted(now["strategies"].items()):
        out.update({f"{name}.{k}": v for k, v in sorted(s.items())
                    if v != (was["strategies"].get(name) or {}).get(k)})
    return out


def _shown(key, v):
    """A setting's value as the owner reads it: $300, 0.25, false."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return json.dumps(v)
    if key in _MONEY:
        from .setup import dollars
        return dollars(v)
    return f"{v:g}"


def back_on_prod_notes(before, after):
    """The owner's lines for a switch from demo to prod: the modes, then the settings that
    went back to their real-money values. [] for any other change."""
    notes = [back_on_prod_note(back_on_prod(before, after))]
    changed = settings_back_on_prod(before, after)
    if changed:
        shown = ", ".join(f"{k} {_shown(k, v)}" for k, v in changed.items())
        notes.append(f"Back to the real-money settings: {shown}. Settings on demo were practice.")
    return [n for n in notes if n]
