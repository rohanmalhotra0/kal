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


def enabled():
    return bool(os.environ.get("KAL_NTFY_TOPIC"))


def send(title, message, priority="default", tags=()):
    """Push one alert. Returns True if it was sent."""
    topic = os.environ.get("KAL_NTFY_TOPIC")
    if not topic:
        return False
    server = os.environ.get("KAL_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    try:
        r = requests.post(f"{server}/{topic}", data=message.encode()[:4000], timeout=10, headers={
            "Title": title.encode("ascii", "replace").decode()[:200],
            "Priority": priority,
            "Tags": ",".join(tags),
        })
        return r.status_code < 300
    except Exception as e:
        log.warning("alert not sent: %s", e)
        return False
