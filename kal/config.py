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
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_lock = threading.RLock()

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
    "auto_promote": True,        # move paper ideas to live once they prove themselves
    "paper_bankroll": 1000.0,
    "strategies": {
        "basket_arb": {"mode": "live"},
        "ladder_arb": {"mode": "paper"},
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
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 1 or v != int(v):
            raise ValueError(f"{k} must be a whole number, 1 or more")
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


def trip(reason):
    """Turn the bot off for safety. Stays off until clear_trip(). Works even if the file
    holds invalid values, because it touches only these two keys and skips validation."""
    with _locked():
        try:
            data = _raw()
        except (OSError, ValueError):
            data = {}
        data.update(enabled=False, disabled_reason=str(reason) or "stopped")
        _write(data)
    try:
        from . import notify
        notify.send("kal stopped itself", f"{reason}\n\nOpen the dashboard, find out why, then press Reset.",
                    priority="urgent", tags=("rotating_light",))
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
