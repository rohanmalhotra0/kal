"""`kal doctor`: one plain checklist for the first hour on the server, and any time after.

Read-only. It never trips the bot, changes a setting or sends an order. The $0.01 test
orders are only reported: the engine sends them itself before its first real trade.
"""
from . import config, gate, ledger, maintenance, notify
from .maintenance import _row, _safe
from .setup import dollars, kalshi_balance

SPARE = 5.0   # dollars of account cash past the bot's own before it's worth a word (interest)
KEY_COPIES = ("/tmp/kalshi.pem",)   # where the old setup steps had people put the key file


def _key(cfg, balance_fn):
    has_key, balance = balance_fn(cfg["env"])
    where = "real money" if cfg["env"] == "prod" else "practice (Kalshi's demo site)"
    if not has_key:
        return [_row("key", "fail", "no Kalshi key yet. Run: sudo kal setup")], None
    if balance is None:
        return [_row("key", "fail", "Kalshi didn't answer with this key. Check its ID and file: sudo kal setup")], None
    return [_row("key", "ok", f"Kalshi answers ({where}). Account cash: {dollars(balance)}")], balance


def _money(cfg, balance):
    if cfg["budget"] <= 0:
        return _row("money", "fail", "the budget is $0, so it can't trade. Run: sudo kal setup")
    own = ledger.available(cfg)   # the budget, plus or minus what it made, minus what's in bets
    extra = balance - own
    if extra > SPARE:
        return _row("money", "warn", f"the account holds {dollars(extra)} more than the bot's own {dollars(own)}. "
                    "It never spends that, but keep only the bot's money there, so even a bug can't touch more.")
    return _row("money", "ok", f"budget {dollars(cfg['budget'])}; free to use now: {dollars(min(own, balance))}")


def _strategies(cfg):
    modes = {name: (p or {}).get("mode", "off") for name, p in cfg["strategies"].items()}
    live = [n for n, m in modes.items() if m == "live"]
    paper = [n for n, m in modes.items() if m == "paper"]
    money = "real money" if cfg["env"] == "prod" else "demo money"
    return _row("strategies", "ok", f"{money}: {', '.join(live) or 'none'}. Practice only: {', '.join(paper) or 'none'}")


def _test_orders(cfg, client_fn):
    client = client_fn(cfg["env"])
    if gate.passed(client, cfg["env"]):
        return _row("test orders", "ok", "passed: Kalshi accepts its orders")
    rec = gate.status()
    # A failed try for this key, or a pass cleared after Kalshi refused an order (no key then).
    if not rec.get("ok") and rec.get("detail") and rec.get("fingerprint") in (None, gate._fingerprint(client, cfg["env"])):
        return _row("test orders", "warn", f"not passed yet: {rec['detail']}. It tries again every hour")
    return _row("test orders", "ok", "not sent yet: before its first real trade it sends two $0.01 orders "
                "(one YES, one NO) that can't fill")


def _alerts():
    if notify.enabled():
        return _row("alerts", "ok", "phone alerts are on. Test them: sudo kal summary --send")
    return _row("alerts", "warn", "phone alerts are off: you won't hear if it stops itself. Run: sudo kal setup")


def _key_copies():
    """A warning for each copy of the key left where other users can read it, else nothing."""
    import os
    return [_row("key copy", "warn", f"a copy of your Kalshi key is still at {p}. Delete it: sudo rm {p}")
            for p in KEY_COPIES if os.path.exists(p)]


def _client(env):
    from .client import Kalshi
    return Kalshi.from_env(config.home(), env)


def rows(balance_fn=None, client_fn=None, health_fn=None):
    """The checklist, as `kal health`-style rows: first-run checks, then the health rows."""
    try:
        cfg = config.load()
    except Exception as e:
        return [_row("config", "fail", f"config.json can't be read: {e}")]
    out, balance = _key(cfg, balance_fn or kalshi_balance)
    if balance is not None:
        out.append(_safe("money", _money, cfg, balance))
        out.append(_safe("test orders", _test_orders, cfg, client_fn or _client))
    out += _key_copies()
    out += [_safe("strategies", _strategies, cfg), _safe("alerts", _alerts)]
    return out + (health_fn or maintenance.checks)()
