"""Phone alerts through ntfy (free app, no account: https://ntfy.sh).

Set KAL_NTFY_TOPIC to a long random name (anyone who knows it can read the alerts),
then subscribe to that topic in the ntfy app. KAL_NTFY_SERVER points at a
self-hosted server if you'd rather not use ntfy.sh.

Never raises: a failed alert must never stop a trade run or a safety trip.
"""
import logging
import os

import requests

log = logging.getLogger(__name__)


def topic():
    """KAL_NTFY_TOPIC, or the topic `kal setup` saved in $KAL_HOME/ntfy_topic."""
    t = os.environ.get("KAL_NTFY_TOPIC")
    if t:
        return t.strip()
    try:
        from . import config
        with open(os.path.join(config.home(), "ntfy_topic")) as f:
            return f.read().strip() or None
    except OSError:
        return None


def enabled():
    return bool(topic())


def send(title, message, priority="default", tags=()):
    """Push one alert. Returns True if it was sent."""
    t = topic()
    if not t:
        return False
    topic_name = t
    server = os.environ.get("KAL_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    try:
        r = requests.post(f"{server}/{topic_name}", data=message.encode()[:4000], timeout=10, headers={
            "Title": title.encode("ascii", "replace").decode()[:200],
            "Priority": priority,
            "Tags": ",".join(tags),
        })
        if r.status_code >= 300:
            log.warning("alert not sent: HTTP %s", r.status_code)
        return r.status_code < 300
    except Exception as e:
        # Only the kind of error: its text quotes the URL, and the URL holds the secret topic.
        log.warning("alert not sent: %s", type(e).__name__)
        return False
