"""Which market shapes exist, and what fees they charge (public data only: no key, no orders).

1. Fees: how many series charge less than the bot assumes (quadratic, M = 1).
2. Shapes: every open event, sorted into what the arbitrage strategies cover and what
   they don't. "By date" families (markets that close at different times in one event,
   not mutually exclusive) get a closer look: if "X by an earlier date" can't be likelier
   than "X by a later date", YES on the later one plus NO on the earlier one pays $1.
Probe branch only; never merged.
"""
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal.client import Kalshi, fee, price, ts  # noqa: E402
from kal.strategies import bracket_arb, ladder_arb  # noqa: E402
from kal.strategies.base import plain_payouts  # noqa: E402


def close_of(m):
    try:
        return ts(m["close_time"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def main():
    c = Kalshi()
    t0 = time.time()
    series = c.series()
    fees = collections.Counter((s.get("fee_type"), s.get("fee_multiplier")) for s in series)
    print(f"{len(series)} series. Fee type x multiplier, most common first:")
    for (ft, m), n in fees.most_common(15):
        print(f"  {n:6}  {ft} x {m}")
    info = {s["ticker"]: s for s in series if s.get("ticker")}

    events = c.events(status="open", with_nested_markets="true")
    print(f"\n{len(events)} open events ({time.time() - t0:.0f}s)")
    kinds, active_fees = collections.Counter(), collections.Counter()
    dated, singles = [], collections.defaultdict(list)
    for ev in events:
        ms = [m for m in ev.get("markets") or [] if m.get("status", "active") in ("active", "open")]
        s = info.get(ev.get("series_ticker"), {})
        active_fees[(s.get("fee_type"), s.get("fee_multiplier"))] += 1
        if (ev.get("event_ticker") or "").startswith("KXMVE"):
            kinds["combo (KXMVE), skipped by every strategy"] += 1
            continue
        if len(ms) == 1:
            kinds["one market"] += 1
            singles[ev.get("series_ticker")].append((close_of(ms[0]), ev, ms[0]))
            continue
        if not ms:
            kinds["no open market"] += 1
            continue
        closes = {close_of(m) for m in ms}
        types = {m.get("strike_type") for m in ms}
        if ev.get("mutually_exclusive"):
            if bracket_arb.brackets(dict(ev, markets=ms)):
                kinds["one-winner brackets (basket_arb + bracket_arb)"] += 1
            else:
                kinds["one winner (basket_arb)" + ("" if plain_payouts(ms) else ", payouts not plain: skipped")] += 1
        elif len(closes) == 1 and types <= set(ladder_arb.UP + ladder_arb.DOWN) and len(types) == 1:
            kinds["strike ladder, one close (ladder_arb)"] += 1
        elif len(closes) > 1:
            kinds["several close times, not one-winner (no strategy)"] += 1
            dated.append((ev, ms))
        else:
            kinds[f"other, not one-winner (no strategy): strike types {sorted(map(str, types))}"] += 1
    print("\nOpen events by shape:")
    for k, n in kinds.most_common():
        print(f"  {n:6}  {k}")
    print("\nFees on open events' series:")
    for (ft, m), n in active_fees.most_common(8):
        print(f"  {n:6}  {ft} x {m}")

    # "By date" families: sort by close; an earlier YES bid above a later YES ask breaks
    # "earlier is no likelier". Using the listed bid/ask (no order books), before checks.
    gaps = []
    for ev, ms in dated:
        ms = sorted((m for m in ms if close_of(m)), key=close_of)
        best = None
        for i, a in enumerate(ms):
            for b in ms[i + 1:]:
                if close_of(b) <= close_of(a):
                    continue
                bid, ask = price(a, "yes_bid"), price(b, "yes_ask")
                if 0 < bid < 1 and 0 < ask < 1:
                    cost = ask + (1 - bid) + fee(1, ask) + fee(1, 1 - bid)
                    edge = 1 - cost
                    if best is None or edge > best[0]:
                        best = (edge, a, b, bid, ask)
        if best:
            gaps.append((best, ev, ms))
    gaps.sort(key=lambda g: -g[0][0])
    pos = [g for g in gaps if g[0][0] > 0]
    print(f"\n'Several close times' events with prices: {len(gaps)}. "
          f"With 1 YES later + 1 NO earlier under $1 after fees: {len(pos)}.")
    for (edge, a, b, bid, ask), ev, ms in gaps[:12]:
        print(json.dumps({
            "event": ev.get("event_ticker"), "title": (ev.get("title") or "")[:80], "markets": len(ms),
            "strike_types": sorted({str(m.get("strike_type")) for m in ms}),
            "profit_per_pair_after_fees": round(edge, 4),
            "earlier": {"ticker": a.get("ticker"), "sub": (a.get("yes_sub_title") or a.get("subtitle") or "")[:60],
                        "close": a.get("close_time"), "yes_bid": bid, "rules": (a.get("rules_primary") or "")[:220]},
            "later": {"ticker": b.get("ticker"), "sub": (b.get("yes_sub_title") or b.get("subtitle") or "")[:60],
                      "close": b.get("close_time"), "yes_ask": ask, "rules": (b.get("rules_primary") or "")[:220]},
        }))
    # Same question across events: one-market events in one series with different closes.
    multi = {s: v for s, v in singles.items() if len(v) >= 2 and len({x[0] for x in v}) >= 2}
    print(f"\nSeries with 2+ one-market open events closing at different times: {len(multi)}. Examples:")
    for s in sorted(multi, key=lambda s: -len(multi[s]))[:10]:
        v = sorted(multi[s], key=lambda x: x[0] or 0)
        print(f"  {s}: {len(v)} events, e.g. " + " | ".join(
            f"{(e.get('title') or '')[:50]} (closes {m.get('close_time')}, bid {price(m, 'yes_bid')}, ask {price(m, 'yes_ask')})"
            for _, e, m in v[:3]))
    # One-winner events: why plain_payouts says no, and whether any shows a gap at all.
    from kal.strategies.base import FAIR_PRICE_WORDS
    why, gap_plain, gap_not, clauses, samples = collections.Counter(), 0, 0, collections.Counter(), []
    for ev in events:
        if not ev.get("mutually_exclusive") or (ev.get("event_ticker") or "").startswith("KXMVE"):
            continue
        ms = [m for m in ev.get("markets") or [] if m.get("status", "active") in ("active", "open")]
        if len(ms) < 2:
            continue
        total = sum(price(m, "yes_bid") for m in ms)
        if plain_payouts(ms):
            gap_plain += total > 1
            continue
        gap_not += total > 1
        reasons = set()
        for m in ms:
            if m.get("market_type", "binary") != "binary":
                reasons.add(f"market_type {m.get('market_type')}")
            if m.get("settlement_bounds_type", "default") != "default":
                reasons.add(f"settlement_bounds_type {m.get('settlement_bounds_type')}")
            rules = f"{m.get('rules_primary') or ''} {m.get('rules_secondary') or ''}"
            low = rules.lower()
            for w in FAIR_PRICE_WORDS:
                i = low.find(w)
                if i >= 0:
                    reasons.add(f"rules say '{w}'")
                    clauses[rules[max(0, i - 120):i + 60].strip()] += 1
        for r in reasons:
            why[r] += 1
        if total > 1 and len(samples) < 8:
            samples.append({"event": ev.get("event_ticker"), "title": (ev.get("title") or "")[:70],
                            "markets": len(ms), "yes_bids_add_to": round(total, 3), "why": sorted(reasons)})
    print(f"\nOne-winner events with plain payouts and YES bids over $1 (before books and fees): {gap_plain}")
    print(f"One-winner events skipped for payouts, with YES bids over $1: {gap_not}")
    print("Why skipped (events):")
    for r, n in why.most_common():
        print(f"  {n:6}  {r}")
    print("Most common fair-price clauses (markets):")
    for cl, n in clauses.most_common(6):
        print(f"  {n:6}  ...{cl}...")
    for x in samples:
        print("  GAP " + json.dumps(x))
    print(f"\ndone in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
