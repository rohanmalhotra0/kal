"""Settings the dashboard edits, stored in $KAL_HOME/config.json.

KAL_ENABLED / KAL_BUDGET env vars override the file (handy for servers).
A safety trip (daily loss limit, repeated errors) sets `disabled_reason`,
and the bot stays off until a person clears it, whatever the env says.
"""
import copy
import json
import os
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_lock = threading.Lock()

DEFAULTS = {
    "enabled": False,            # master switch
    "env": "prod",               # "demo" trades Kalshi's fake-money exchange
    "budget": 0.0,               # capital given to the bot: the most it can ever lose in total
    "reinvest_profits": True,    # let the bot trade its own winnings too
    "cash_floor": 0.0,           # never trade the balance below this
    "max_per_trade": 10.0,       # most a directional trade can lose
    "max_per_arb": 30.0,         # most a locked-in arbitrage basket can cost
    "max_per_event": 20.0,       # markets in one event move together
    "daily_loss_limit": 0.10,    # fraction of budget; past this the bot turns itself off
    "max_consecutive_errors": 3,
    "kelly_fraction": 0.25,      # bet a quarter of the Kelly size
    "trade_interval_min": 5,
    "research_interval_hours": 24,
    "auto_promote": True,        # move paper strategies to live once they prove themselves
    "paper_bankroll": 1000.0,
    "strategies": {
        "basket_arb": {"mode": "live"},
        "ladder_arb": {"mode": "live"},
        "bracket_arb": {"mode": "paper"},
        "favorites": {"mode": "paper"},
        "mentions": {"mode": "paper"},
    },
    "disabled_reason": "",
}

MODES = ("off", "paper", "live")


def home():
    path = os.environ.get("KAL_HOME") or os.path.join(ROOT, "state")
    os.makedirs(path, exist_ok=True)
    return path


def _path():
    return os.path.join(home(), "config.json")


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(apply_env=True):
    stored = {}
    if os.path.exists(_path()):
        with open(_path()) as f:
            stored = json.load(f)
    cfg = _merge(DEFAULTS, stored)
    if apply_env:
        if os.environ.get("KAL_ENABLED"):
            cfg["enabled"] = os.environ["KAL_ENABLED"].strip().lower() in ("1", "true", "yes", "on")
        if os.environ.get("KAL_BUDGET"):
            cfg["budget"] = float(os.environ["KAL_BUDGET"])
        if os.environ.get("KAL_ENV"):
            cfg["env"] = os.environ["KAL_ENV"]
    if cfg["disabled_reason"]:
        cfg["enabled"] = False
    return cfg


def save(cfg):
    with _lock:
        tmp = _path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=1)
        os.replace(tmp, _path())


def update(changes):
    """Apply a partial update (validated) and return the new config."""
    with _lock:
        stored = load(apply_env=False)
    cfg = _merge(stored, changes)
    validate(cfg)
    save(cfg)
    return load()


def validate(cfg):
    for k in ("budget", "cash_floor", "max_per_trade", "max_per_arb", "max_per_event", "paper_bankroll"):
        if float(cfg[k]) < 0:
            raise ValueError(f"{k} can't be negative")
    if not 0 <= float(cfg["daily_loss_limit"]) <= 1:
        raise ValueError("daily_loss_limit is a fraction between 0 and 1")
    if not 0 < float(cfg["kelly_fraction"]) <= 1:
        raise ValueError("kelly_fraction must be in (0, 1]")
    if cfg["env"] not in ("prod", "demo"):
        raise ValueError("env must be prod or demo")
    for name, s in cfg["strategies"].items():
        if s.get("mode") not in MODES:
            raise ValueError(f"{name}: mode must be one of {MODES}")


def trip(reason):
    """Turn the bot off for safety. Stays off until clear_trip()."""
    update({"enabled": False, "disabled_reason": reason})


def clear_trip():
    update({"disabled_reason": ""})
