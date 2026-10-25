"""`python -m kal setup`: the whole setup as a few plain questions.

Saves the Kalshi key in $KAL_HOME (kalshi_key_id + kalshi.pem, readable only by you),
picks practice (demo) or real money, sets the budget, sets up phone alerts, runs the
self-test with the zero-risk order probe, and offers to turn the bot on.
Safe to run again: Enter keeps each current answer.
"""
import os
import re
import secrets

from . import config

UUIDISH = re.compile(r"^[A-Za-z0-9-]{8,80}$")


def _write_secret(path, data):
    """Write a file only the owner can read (0600), whatever the umask."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)


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


def run(input_fn=input, print_fn=print, selftest_fn=None):
    say = print_fn
    home = config.home()

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

    say("kal setup. Answer 5 questions. Press Enter to keep the answer in [brackets].", "")
    cfg = config.load()

    # 1. key ID
    id_path = os.path.join(home, "kalshi_key_id")
    key_id = ask("1. Your Kalshi API key ID (kalshi.com > Account > API keys)",
                 _read(id_path) or os.environ.get("KALSHI_API_KEY_ID", ""),
                 lambda v: (bool(UUIDISH.match(v)), v, "That doesn't look like a key ID. Copy it from Kalshi."))

    # 2. key file
    pem_path = os.path.join(home, "kalshi.pem")

    def pem_ok(v):
        try:
            return True, _load_pem(v), ""
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

    # 4. budget
    def money(v):
        try:
            x = float(v.replace("$", "").replace(",", ""))
        except ValueError:
            return False, None, "Type a number, like 300."
        if not 1 <= x <= 100_000:
            return False, None, "Pick between $1 and $100,000."
        return True, x, ""
    budget = ask("4. How many dollars can the bot use? It can never lose more than this",
                 int(cfg["budget"]) if cfg["budget"] else 300, money)

    # 5. phone alerts
    alerts = ask("5. Phone alerts when it trades or stops? (yes / no)", "yes",
                 lambda v: (v.lower() in ("yes", "y", "no", "n"), v.lower().startswith("y"), "Type yes or no."))
    topic_path = os.path.join(home, "ntfy_topic")
    if alerts:
        topic = _read(topic_path) or "kal-" + secrets.token_hex(10)
        _write_secret(topic_path, (topic + "\n").encode())
        say("   Install the free ntfy app on your phone. Tap +, and subscribe to:",
            f"      {topic}",
            "   Keep that name secret: anyone with it can read your alerts.")
        from . import notify
        notify.send("kal alerts work", "This is a test from kal setup.")

    if cfg["enabled"] and env != cfg["env"]:
        config.turn_off()
        say("   The bot was on. It's off now, so it can switch sites safely.")
    config.update({"env": env, "budget": budget})

    say("", "Testing your key with Kalshi (one $0.01 order that can't fill)...")
    if selftest_fn is None:
        from .selftest import run as selftest_fn
    rows = selftest_fn(order_probe=True)
    for r in rows:
        say(f"   {'ok  ' if r['ok'] else 'FAIL'}  {r['check']}: {r['detail']}")
    bad = [r for r in rows if not r["ok"]]
    if bad:
        say("", f"{len(bad)} check(s) failed. Fix those, then run: python -m kal setup")
        return 1

    if config.load()["disabled_reason"]:
        say("", f"It stopped itself before: {config.load()['disabled_reason']}",
            "Find out why, then run: python -m kal on --clear-trip")
        return 1
    on = ask(f"Everything works. Turn the bot on now, with ${budget:,.0f}? (yes / no)", "no",
             lambda v: (v.lower() in ("yes", "y", "no", "n"), v.lower().startswith("y"), "Type yes or no."))
    if on:
        config.update({"enabled": True})
        say("", f"The bot is ON ({'practice' if env == 'demo' else 'REAL money'}, ${budget:,.0f}).")
    else:
        say("", "Not turned on. When you're ready: python -m kal on")
    say("See it: from your computer run  ssh -L 8787:localhost:8787 root@YOUR_SERVER",
        "then open http://localhost:8787")
    return 0
