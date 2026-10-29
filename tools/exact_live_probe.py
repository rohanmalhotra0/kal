"""Does exact_link_arb read the live markets? Public data only. Probe branch only."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal import engine  # noqa: E402
from kal.client import Kalshi  # noqa: E402
from kal.strategies import exact_link_arb as x  # noqa: E402

c = engine.RunCache(Kalshi())
c.scope((), 400)   # every release this year
evs = c.events(status="open", with_nested_markets="true")
found = x.pairs(evs)
print(f"{len(found)} pairs read cleanly")
for p in sorted(found, key=lambda p: p["first_close"]):
    ls = x.links(p)
    best = min((x._listed(legs), [f"{s.upper()} {m['ticker']}" for m, s in legs]) for legs in ls) if ls else None
    print(f"  {p['series']:22} {p['exact']['event']:28} {p['above']['event']:20} {x._month(p['exact']['month'])}: "
          f"{len(p['exact']['values'])} values, {len(p['above']['strikes'])} strikes, {len(ls)} links, "
          f"closes in {(p['first_close'] - time.time()) / 86400:.1f} d; cheapest listed {best}")
for ev in evs:
    s = str(ev.get("event_ticker", "")).split("-", 1)[0]
    if s in x.SERIES and not any(p["exact"]["event"] == ev["event_ticker"] for p in found):
        print(f"  NOT PAIRED exact {ev['event_ticker']}: reads={x.exact_event(ev, s) is not None}")
    ladders = {v[0]: k for k, v in x.SERIES.items()}
    if s in ladders and not any(p["above"]["event"] == ev["event_ticker"] for p in found):
        print(f"  NOT PAIRED ladder {ev['event_ticker']}: reads={x.above_event(ev, s) is not None}")
print("\nscan:", [(s.event, round(s.cost, 2), s.payout_min, s.meta["paper_only"]) for s in
                  x.STRATEGY.scan(__import__("kal.strategies.base", fromlist=["Context"]).Context(
                      c, __import__("kal.config", fromlist=["DEFAULTS"]).DEFAULTS, "paper", 100, 100, set(), {}, {}))])

import json  # noqa: E402
print("\n---- the events it couldn't read, in full")
want = {"KXECONSTATCPIYOY-27JAN13", "KXECONSTATCPICORE-27JAN13", "KXECONSTATCPI-27JAN13",
        "KXECONSTATCORECPIYOY-27JAN13", "KXCPI-26DEC", "KXCPICORE-26DEC"}
for ev in evs:
    if ev.get("event_ticker") in want:
        e = {k: v for k, v in ev.items() if k != "markets"}
        print("\nEVENT " + json.dumps(e))
        ms = sorted(ev.get("markets") or [], key=lambda m: m.get("ticker"))
        for m in ms[:2]:
            print("MARKET " + json.dumps(m))
        for m in ms[2:]:
            print("  m", m.get("ticker"), m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike"),
                  json.dumps(m.get("custom_strike")), repr(m.get("yes_sub_title")), m.get("close_time"),
                  " | " + " ".join(str(m.get("rules_primary")).split())[:160])
