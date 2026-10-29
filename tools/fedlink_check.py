"""Run fed_link_arb's own parsing on the real API. Probe branch only. Public data, no orders."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal import config  # noqa: E402
from kal.client import Kalshi, price  # noqa: E402
from kal.strategies import fed_link_arb as F  # noqa: E402
from kal.strategies.base import Context  # noqa: E402


def show(m):
    return {k: m.get(k) for k in ("ticker", "yes_sub_title", "strike_type", "floor_strike", "close_time",
                                  "status", "yes_bid_dollars", "yes_ask_dollars")}


def main():
    t0 = time.time()
    c = Kalshi()
    for s in (F.RATE, F.DECISION):
        for ev in c.events(series_ticker=s, status="open", with_nested_markets="true")[:2]:
            print(f"\n== {ev['event_ticker']} | {ev.get('title')} | mutually_exclusive={ev.get('mutually_exclusive')}")
            for m in ev.get("markets") or []:
                print("  M " + json.dumps(show(m)))
                if s == F.DECISION or m is ev["markets"][0]:
                    print("    R1 " + " ".join(str(m.get("rules_primary") or "").split()))
                    print("    R2 " + " ".join(str(m.get("rules_secondary") or "").split()))
    evs = c.events(status="open", with_nested_markets="true")
    print(f"\nopen events (shared download): {len(evs)}")
    found = F.next_meeting(evs)
    print("next_meeting:", [e["event_ticker"] for e in found] if found else None)
    meet = F.meeting(*found) if found else None
    print("meeting parsed:", None if not meet else {k: meet[k] for k in ("rate_event", "decision_event", "title")},
          "| strikes:", sorted(meet["strikes"]) if meet else None,
          "| codes:", sorted(meet["decisions"]) if meet else None)
    if meet:
        u = F.rate_before(c.settled_markets(F.RATE), meet)
        print("rate before (quarter points):", u, "=", None if u is None else u / 4)
        if u is not None:
            for legs in F.links(meet, u):
                name = " + ".join(f"{side.upper()} {m['ticker']}" for m, side in legs)
                print(f"  link {F._listed(legs):.4f}  {name}")
            print("too good:", F.too_good(F.links(meet, u), []))
    cfg = config.load()
    ctx = Context(c, cfg, "paper", 30.0, 300.0, set(), {}, {})
    sigs = F.STRATEGY.scan(ctx)
    print(f"\nscan: {len(sigs)} signals")
    for sg in sigs:
        print("  SIG", sg.note, "| cost", round(sg.cost, 4), "| payout_min", sg.payout_min, "| meta", json.dumps(sg.meta))
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
