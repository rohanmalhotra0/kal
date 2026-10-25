"""How many one-winner events pass basket_arb's exact cancellation wordings, and which
fair-price sentences don't (public data only: no key, no orders). Probe branch only."""
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal.client import Kalshi  # noqa: E402
from kal.strategies.base import FAIR_PRICE_WORDS, CANCELLATION_CLAUSES, _sentences, fair_price_only_if_cancelled, plain_payouts  # noqa: E402

events = Kalshi().events(status="open", with_nested_markets="true")
n = plain = passed = 0
failing = collections.Counter()
for ev in events:
    if not ev.get("mutually_exclusive") or (ev.get("event_ticker") or "").startswith("KXMVE"):
        continue
    ms = [m for m in ev.get("markets") or [] if m.get("status", "active") in ("active", "open")]
    if len(ms) < 2:
        continue
    n += 1
    if plain_payouts(ms):
        plain += 1
        continue
    if fair_price_only_if_cancelled(ms):
        passed += 1
        continue
    for m in ms:
        for field in ("rules_primary", "rules_secondary"):
            for s in _sentences(m.get(field)):
                if any(w in s.lower() for w in FAIR_PRICE_WORDS) and not any(c.fullmatch(s) for c in CANCELLATION_CLAUSES):
                    failing[s[:240]] += 1
print(f"one-winner events: {n}; plain: {plain}; not plain: {n - plain}; pass the exact wordings: {passed}")
print("fair-price sentences that don't match (markets), most common first:")
for s, k in failing.most_common(15):
    print(f"  {k:6}  {s}")
