"""Real data: which open events does the stricter fair-price check (kal/strategies/base.py)
treat differently from the old exact-text one? Public data only: no key, no orders."""
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal.client import Kalshi  # noqa: E402
from kal.strategies import base  # noqa: E402

OLD_WORDS = ("last traded price", "fair price", "fair value")


def old_plain(markets):
    for m in markets:
        if m.get("market_type", "binary") != "binary":
            return False
        if m.get("settlement_bounds_type", "default") != "default":
            return False
        rules = f"{m.get('rules_primary') or ''} {m.get('rules_secondary') or ''}".lower()
        if any(w in rules for w in OLD_WORDS):
            return False
    return True


def old_cancel_only(markets):
    for m in markets:
        if m.get("market_type", "binary") != "binary":
            return False
        if m.get("settlement_bounds_type", "default") != "default":
            return False
        for field in ("rules_primary", "rules_secondary"):
            for sentence in base._sentences(m.get(field)):
                if any(w in sentence.lower() for w in OLD_WORDS) and \
                        not any(c.fullmatch(sentence) for c in base.CANCELLATION_CLAUSES):
                    return False
    return True


def snippet(markets):
    for m in markets:
        text = base._plain(f"{m.get('rules_primary') or ''} {m.get('rules_secondary') or ''}")
        hit = base.FAIR_PRICE.search(text)
        if hit:
            return text[max(0, hit.start() - 90):hit.end() + 60]
    return ""


def main():
    events = list(Kalshi().events(status="open", with_nested_markets="true"))
    print(f"open events: {len(events)}")
    flips = collections.Counter()
    shown = collections.Counter()
    why_shown = collections.Counter()
    for ev in events:
        ms = ev.get("markets") or []
        if not ms:
            continue
        me = "one-winner" if ev.get("mutually_exclusive") else "other"
        a, b = old_plain(ms), base.plain_payouts(ms)
        c, d = old_cancel_only(ms), base.fair_price_only_if_cancelled(ms)
        flips[(me, "events")] += 1
        if a and not b:
            flips[(me, "plain -> not plain")] += 1
        if b and not a:
            flips[(me, "not plain -> plain (must be 0)")] += 1
        if c != d:
            flips[(me, f"cancel-only {c} -> {d}")] += 1
        key = (a, b, c, d)
        if (a != b or c != d) and shown[key] < 4:
            shown[key] += 1
            print(f"- {ev.get('event_ticker')} [{me}] plain {a}->{b}, cancel-only {c}->{d}: ...{snippet(ms)}...")
        if c and not d:
            series = ev.get("series_ticker") or ev.get("event_ticker", "").split("-")[0]
            if why_shown[series] < 1 and sum(why_shown.values()) < 12:
                why_shown[series] += 1
                for m in ms:
                    for field in ("rules_primary", "rules_secondary"):
                        for sent in base._sentences(base._plain(m.get(field))):
                            if base.FAIR_PRICE.search(sent) and not any(cl.fullmatch(sent) for cl in base.CANCELLATION_CLAUSES):
                                print(f"  WHY {series} {field}: {sent!r}")
                                raw = m.get(field) or ""
                                i = raw.lower().find("fair")
                                print(f"  RAW {raw[max(0, i - 400):i + 200]!r}")
                                break
                        else:
                            continue
                        break
                    else:
                        continue
                    break
    for k, v in sorted(flips.items()):
        print(f"{k[0]:10} {k[1]:32} {v}")


if __name__ == "__main__":
    main()
