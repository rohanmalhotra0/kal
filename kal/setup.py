"""`python -m kal setup`: the whole setup as a few plain questions.

Saves the Kalshi key in $KAL_HOME (kalshi_key_id + kalshi.pem, readable only by you),
picks practice (demo) or real money, sets the budget, sets up phone alerts, runs the
self-test with the zero-risk order probe, and offers to turn the bot on.
Safe to run again: Enter keeps each current answer. A bot that is on is turned off
first, and only a "yes" at the end turns it on again.
"""
import math
import os
import re
import secrets
import shlex
import sys
import threading

from . import config

UUIDISH = re.compile(r"^[A-Za-z0-9-]{8,80}$")
ENV_FILE = "/etc/kal/kal.env"   # the server's settings file (deploy/install.sh)
BALANCE_WAIT = 15               # seconds to wait for Kalshi's balance before asking anyway
TOPIC_LETTERS = "abcdefghijkmnpqrstuvwxyz23456789"   # no l, 1, o or 0: nothing to misread on a phone


def new_topic():
    """A new secret alert topic, the way install.sh and the dashboard make one:
    "kal-" + 20 letters and digits that can't be mixed up."""
    return "kal-" + "".join(secrets.choice(TOPIC_LETTERS) for _ in range(20))


def _write_secret(path, data):
    """Write a file only the owner can read (0600), whatever the umask. It is written
    beside the old one and renamed over it, so a copy that root left there (scp) can't
    stop it."""
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _load_pem(path):
    from cryptography.hazmat.primitives import serialization
    with open(os.path.expanduser(path), "rb") as f:
        data = f.read()
    serialization.load_pem_private_key(data, password=None)  # raises if it isn't a key
    return data


def _tty():
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


# ---------- the budget ----------

def parse_money(text):
    """'$3,000' -> 3000.0, or None if it isn't a number."""
    try:
        v = float(str(text).strip().replace("$", "").replace(",", ""))
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def same_money(text, value):
    v = parse_money(text)
    return v is not None and abs(v - value) < 0.005


def plain_money(v):
    """How to type an amount: 3000.0 -> '3000', 250.5 -> '250.5'."""
    return f"{v:.2f}".rstrip("0").rstrip(".")


def dollars(v):
    """$300 or $299.50."""
    s = f"${v:,.2f}"
    return s[:-3] if s.endswith(".00") else s


def kalshi_balance(env):
    """(is a key set, the Kalshi account's cash in dollars, or None if it can't be read now)."""
    from .client import Kalshi
    try:
        c = Kalshi.from_env(config.home(), env)
    except Exception:
        return True, None   # a key that won't load is still a key: ask, don't guess
    if not c.can_trade:
        return False, None
    got = []

    def fetch():
        try:
            got.append(float(c.balance()))
        except Exception:
            pass
    t = threading.Thread(target=fetch, name="kal-balance", daemon=True)
    t.start()
    t.join(BALANCE_WAIT)   # a hung request mustn't hang setup: then it asks anyway
    if got and math.isfinite(got[0]) and got[0] >= 0:
        return True, got[0]
    return True, None


def big_budget(new, old, env, balance_fn=None):
    """Why this new budget must be typed twice, or None. The daily stop and the caps on
    bets grow with the budget, so a slip (3000 for 300) quietly loosens them. Lowering it
    never asks. Raising it asks when it is more than the Kalshi account holds (looked up
    when a key is set; if it can't be, it asks anyway), or more than double the old one."""
    if not new > old + 0.005:
        return None
    has_key, balance = (balance_fn or kalshi_balance)(env)
    if balance is not None and new > balance + 0.005:
        return f"Your Kalshi account has {dollars(balance)}."
    if old > 0 and new > 2 * old:
        return f"That's more than double the budget now ({dollars(old)})."
    if has_key and balance is None:
        return "Can't check your Kalshi balance right now."
    return None


# ---------- the questions ----------

def run(input_fn=input, print_fn=print, selftest_fn=None, balance_fn=None, interactive=None):
    """The guided setup. Returns 0 when it all worked. `interactive` is whether someone can
    answer (default: stdin is a terminal); without one, a big budget is refused."""
    say = print_fn
    home = config.home()
    if interactive is None:
        interactive = _tty()

    def ask(question, default=None, check=None):
        while True:
            shown = f" [{default}]" if default not in (None, "") else ""
            raw = input_fn(f"{question}{shown}: ").strip()
            if not raw and default not in (None, ""):
                raw = str(default)
            if check is None:
                return raw
            ok, value, why = check(raw)
            if ok:
                return value
            say(f"   {why}")

    def yes_no(v):
        return v.lower() in ("yes", "y", "no", "n"), v.lower().startswith("y"), "Type yes or no."

    def end(code, hint=""):
        """Say how the bot really is now, however setup ended."""
        cfg = config.load()
        if cfg["enabled"]:
            where = "practice" if cfg["env"] == "demo" else "REAL money"
            say("", f"The bot is ON ({where}, {dollars(cfg['budget'])}).")
        else:
            say("", f"The bot is OFF. {hint}".strip())
        return code

    say("kal setup. Answer 5 questions. Press Enter to keep the answer in [brackets].", "")
    if config.load()["enabled"]:
        config.turn_off()
        say("The bot is OFF while you change things.", "")
    cfg = config.load()

    # 1. key ID. client.load_key reads KALSHI_API_KEY_ID before the saved file, so that's
    # the one shown, and a different one typed here would never be used.
    id_path = os.path.join(home, "kalshi_key_id")
    env_id = os.environ.get("KALSHI_API_KEY_ID")
    key_id = ask("1. Your Kalshi API key ID (kalshi.com > Account > API keys)",
                 (env_id or "").strip() or _read(id_path),
                 lambda v: (bool(UUIDISH.match(v)), v, "That doesn't look like a key ID. Copy it from Kalshi."))
    if env_id and key_id != env_id:
        if os.environ.get("KAL_CMD"):   # the server's `kal` wrapper: the ID comes from its settings file
            say(f"   {ENV_FILE} has a different key ID. The bot uses that one, not this.",
                f"   Delete the KALSHI_API_KEY_ID line in {ENV_FILE}, then: sudo systemctl restart kal",
                f"   Then run this again: {config.command('setup')}")
        else:
            say("   KALSHI_API_KEY_ID is set to a different key ID. The bot uses that one, not this.",
                f"   Remove KALSHI_API_KEY_ID, then run this again: {config.command('setup')}")
        return end(1)

    # 2. key file
    pem_path = os.path.join(home, "kalshi.pem")

    def pem_ok(v):
        try:
            return True, _load_pem(v), ""
        except PermissionError:
            path = shlex.quote(os.path.abspath(os.path.expanduser(v)))
            return False, None, (f"The bot can't read that file. Run: sudo chmod 644 {path}\n"
                                 "   Then type the path again.")
        except (OSError, ValueError, TypeError) as e:
            return False, None, f"Can't use that file ({type(e).__name__}). Give the path to the key you downloaded."
    pem = ask("2. Path to the key file you downloaded from Kalshi",
              pem_path if os.path.exists(pem_path) else None, pem_ok)
    _write_secret(pem_path, pem)
    _write_secret(id_path, (key_id + "\n").encode())
    say("   Saved. Only you can read it.")

    # 3. practice or real
    env = ask("3. Practice money or real money? (practice / real)",
              "practice" if cfg["env"] == "demo" else "real",
              lambda v: (v.lower() in ("practice", "demo", "real", "prod"), v.lower(), "Type practice or real."))
    env = "demo" if env in ("practice", "demo") else "prod"
    if env == "demo":
        say("   Practice uses Kalshi's demo site. It needs a demo key from demo.kalshi.co.")

    # 4. budget. A big raise is typed twice: a slip (3000 for 300) loosens every money limit.
    def money(v):
        x = parse_money(v)
        if x is None:
            return False, None, "Type a number, like 300."
        if not 1 <= x <= 100_000:
            return False, None, "Pick between $1 and $100,000."
        return True, x, ""
    old = float(cfg["budget"] or 0)
    while True:
        budget = ask("4. How many dollars can the bot use? It can never lose more than this",
                     plain_money(old) if old else 300, money)
        why = big_budget(budget, old, env, balance_fn)
        if not why:
            break
        if not interactive:
            say(f"   Not changed. {why}",
                f"   To use {dollars(budget)}, run this in a terminal: {config.command('setup')}")
            return end(2)
        if same_money(input_fn(f"   {why} Type {plain_money(budget)} again to confirm: "), budget):
            break
        say("   That doesn't match. Type the amount again.")

    # 5. phone alerts
    alerts = ask("5. Phone alerts when it trades or stops? (yes / no)", "yes", yes_no)
    topic_path = os.path.join(home, "ntfy_topic")
    if alerts:
        # A KAL_NTFY_TOPIC setting wins over the file (kal.notify), so that's the one to show.
        # A topic already saved is never changed: the phone is subscribed to it.
        topic = (os.environ.get("KAL_NTFY_TOPIC") or "").strip()
        if not topic:
            topic = _read(topic_path) or new_topic()
            _write_secret(topic_path, (topic + "\n").encode())
        say("   Install the free ntfy app on your phone. Tap +, and subscribe to:",
            f"      {topic}",
            "   Keep that name secret: anyone with it can read your alerts.")
        from . import notify
        notify.send("kal alerts work", "This is a test from kal setup.")

    try:
        # A switch of exchange waits for a trade run to finish: each exchange has its own books.
        config.switch({"env": env, "budget": budget})
    except config.Busy:
        say("", f"A trade run is still going. Try again in a minute: {config.command('setup')}")
        return end(1)
    except ValueError as e:
        say("", f"Not changed: {e}")
        return end(1)

    say("", "Testing your key with Kalshi (two $0.01 orders that can't fill)...")
    if selftest_fn is None:
        from .selftest import run as selftest_fn
    rows = selftest_fn(order_probe=True)
    for r in rows:
        say(f"   {'ok  ' if r['ok'] else 'FAIL'}  {r['check']}: {r['detail']}")
    bad = [r for r in rows if not r["ok"]]
    if bad:
        say("", f"{len(bad)} check(s) failed. Fix those, then run: {config.command('setup')}")
        return end(1)

    stopped = config.load()["disabled_reason"]
    if stopped:
        say("", f"It is stopped: {stopped}", f"Find out why, then run: {config.command('on --clear-trip')}")
        return end(1)
    if ask(f"Everything works. Turn the bot on now, with {dollars(budget)}? (yes / no)", "no", yes_no):
        config.update({"enabled": True})
    code = end(0, f"When you're ready: {config.command('on')}")
    say("See it: from your computer run  ssh -L 8787:localhost:8787 root@YOUR_SERVER",
        "then open http://localhost:8787")
    return code
