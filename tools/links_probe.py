"""Idea #6, other links: which Kalshi series are tied together, so their prices must agree?
Discovery only (public data: no key, no orders). Probe branch only, never merged.

1. Series by family (sports futures, Fed, crypto), with open events, markets and samples.
2. Sports futures: pairs of series that share 5+ outcome names (teams). For a champion
   series and a conference / pennant / playoff series, "wins the title" means "won the
   conference" (or "made the playoffs"), so YES(conference) + NO(champion) pays $1 at least.
   Prints today's after-fee gaps on those pairs.
3. Fed: open rate-level and decision events, with prices, to see how they line up.
"""
import collections
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal.client import Kalshi, fee, price  # noqa: E402

FAMILY = {
    "sports": re.compile(r"champion|finals|super bowl|world series|stanley cup|pennant|conference|playoff|division", re.I),
    "fed": re.compile(r"\bfed\b|fomc|federal funds|rate cut|rate hike|interest rate", re.I),
    "crypto": re.compile(r"bitcoin|\bbtc\b|ethereum|\beth\b|solana|crypto", re.I),
}
NARROW = re.compile(r"conference|pennant|playoff|division|\b(?:east|west|afc|nfc|al|nl)\b", re.I)


def name(m):
    return re.sub(r"[^a-z0-9 ]", "", (m.get("yes_sub_title") or "").lower()).strip()


def main():
    c = Kalshi()
    t0 = time.time()
    series = {s["ticker"]: s for s in c.series() if s.get("ticker")}
    events = list(c.events(status="open", with_nested_markets="true"))
    print(f"series: {len(series)}; open events: {len(events)}")
    evs = collections.defaultdict(list)
    for ev in events:
        evs[ev.get("series_ticker")].append(ev)

    picked = {}
    for fam, rx in FAMILY.items():
        hits = sorted(t for t, s in series.items() if evs.get(t) and rx.search(s.get("title") or ""))
        picked[fam] = hits
        print(f"\n== {fam}: {len(hits)} series with open events")
        for t in hits[:150]:
            s = series[t]
            ms = [m for e in evs[t] for m in e.get("markets") or []]
            src = [x.get("name") for x in s.get("settlement_sources") or []][:2]
            print(f"  {t:<24} {(s.get('frequency') or '')[:10]:<10} ev={len(evs[t]):<4} mk={len(ms):<5} "
                  f"{(s.get('title') or '')[:70]!r} src={src} e.g. {[m.get('yes_sub_title') for m in ms[:3]]}")

    # Sports futures: few events, many outcomes. Pair series sharing team names.
    fut = {}
    for t in picked["sports"]:
        ms = [m for e in evs[t] for m in e.get("markets") or []]
        if len(evs[t]) <= 3 and len(ms) >= 8:
            fut[t] = {name(m): m for m in ms if name(m)}
    print(f"\n== sports futures series: {len(fut)}")
    pairs = []
    ts = sorted(fut)
    for i, a in enumerate(ts):
        for b in ts[i + 1:]:
            both = set(fut[a]) & set(fut[b])
            if len(both) >= 5:
                pairs.append((len(both), a, b))
    pairs.sort(reverse=True)
    for n, a, b in pairs[:80]:
        print(f"  {n:>3} shared: {a} {series[a].get('title')!r} <> {b} {series[b].get('title')!r}")

    gaps, checked = [], 0
    for n, a, b in pairs:
        ta, tb = series[a].get("title") or "", series[b].get("title") or ""
        if bool(NARROW.search(ta)) == bool(NARROW.search(tb)):
            continue   # need one title-wide series and one narrower series
        champ, narrow = (a, b) if NARROW.search(tb) else (b, a)
        for team in set(fut[champ]) & set(fut[narrow]):
            mc, mn = fut[champ][team], fut[narrow][team]
            bid_c, ask_n = price(mc, "yes_bid"), price(mn, "yes_ask")
            if not (bid_c and ask_n and 0 < bid_c < 1 and 0 < ask_n < 1):
                continue
            checked += 1
            cost = ask_n + (1 - bid_c) + fee(1, ask_n) + fee(1, round(1 - bid_c, 4))
            gaps.append((round(1 - cost, 4), champ, narrow, team, bid_c, ask_n, mc.get("close_time"), mn.get("close_time")))
    gaps.sort(reverse=True)
    print(f"\n== champion vs narrower pairs: {checked} team pairs priced; {sum(g[0] > 0 for g in gaps)} with a gap after fees")
    for g in gaps[:25]:
        print("  " + json.dumps(dict(zip(("edge", "champion", "narrower", "team", "champ_yes_bid", "narrow_yes_ask",
                                          "champ_close", "narrow_close"), g))))

    print("\n== fed events (up to 12)")
    shown = 0
    for t in picked["fed"]:
        for e in evs[t]:
            if shown >= 12:
                break
            shown += 1
            ms = e.get("markets") or []
            print(f"  {t} {e.get('event_ticker')} {e.get('title')!r} close={ms[0].get('close_time') if ms else None}")
            for m in ms[:12]:
                print(f"    {m.get('ticker'):<34} {(m.get('yes_sub_title') or '')[:40]!r:<44} "
                      f"bid={price(m, 'yes_bid')} ask={price(m, 'yes_ask')}")
    print(f"\ndone in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
