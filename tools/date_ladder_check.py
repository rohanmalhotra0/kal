"""Does date_ladder_arb accept real date ladders? Side branch only, never merged.
Public data only: no key, no orders. Reads events and order books, runs the scan."""
import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal import config  # noqa: E402
from kal.client import Kalshi  # noqa: E402
from kal.strategies import date_ladder_arb as d  # noqa: E402
from kal.strategies.base import Context, plain_payouts  # noqa: E402
from kal.strategies.ladder_arb import own_markets  # noqa: E402

GAPS = ["KXBRIANCONF-26JUL08", "KXALBUMRELEASEDATE-NEWDRA", "KXHEGSETHOUT-26APR", "KXFISAEXTEND-26APR",
        "KXMLBDEBUT-KANDERSON", "KXMLBDEBUT-AMILLER"]


def why(ev):
    ms = ev.get("markets") or []
    if len(ms) < 2:
        return "fewer than 2 markets"
    if ev.get("mutually_exclusive") is not False:
        return f"mutually_exclusive={ev.get('mutually_exclusive')!r}"
    if not own_markets(ev):
        return "own_markets: tickers or plain payouts"
    days = [d.deadline(m) for m in ms]
    if None in days:
        return "a subtitle isn't 'Before <date>'"
    if len(set(days)) != len(days):
        return "a deadline repeats"
    about = {d._about(m) for m in ms}
    if None in about:
        return "custom_strike date disagrees with the wording"
    if len(about) != 1:
        return "custom_strike differs"
    rules = {d._rules(m) for m in ms}
    if None in rules:
        return "no rules"
    if len(rules) != 1:
        return "rules differ beyond dates"
    if any(d.close_ts(m) is None for m in ms):
        return "close_time unreadable"
    return "ok"


def before_family(ev):
    """The old probe's test: every market reads 'Before <date>', no repeats."""
    ms = ev.get("markets") or []
    days = [d.deadline(m) for m in ms]
    return len(ms) >= 2 and None not in days and len(set(days)) == len(days)


def main():
    t0 = time.time()
    c = Kalshi()
    counts, examples = collections.Counter(), collections.defaultdict(list)
    events = list(c.events(status="open", with_nested_markets="true"))
    print(f"open events: {len(events)}")
    for ev in events:
        if not before_family(ev):
            continue
        r = why(ev)
        counts[r] += 1
        if len(examples[r]) < 3:
            examples[r].append(ev)
    keys = collections.Counter()
    for ev in events:
        if before_family(ev) and why(ev) == "custom_strike differs":
            keys[tuple(sorted({k for m in ev["markets"] for k in (m.get("custom_strike") or {})}))] += 1
    print("custom_strike keys in those families:", keys.most_common(10))
    print("open 'Before <date>' families, by what date_ladder_arb says:")
    for r, n in counts.most_common():
        print(f"  {n:4d}  {r}")
        for ev in examples[r]:
            print(f"        {ev['event_ticker']}: {ev.get('title', '')[:70]}")
            if r == "rules differ beyond dates":
                seen = []
                for m in ev["markets"]:
                    x = d._rules(m)
                    if x not in seen:
                        seen.append(x)
                for x in seen[:2]:
                    print(f"          | {x[:400]}")
            if r == "custom_strike differs":
                for m in ev["markets"][:4]:
                    print(f"          | {m['ticker']} {m.get('yes_sub_title')!r} custom_strike={m.get('custom_strike')!r}"
                          f" strike_type={m.get('strike_type')!r} floor={m.get('floor_strike')!r}")
            if r.startswith("own_markets"):
                m = ev["markets"][0]
                print(f"          | {m.get('market_type')} {m.get('settlement_bounds_type')} "
                      f"{(m.get('rules_primary') or '')[:200]} || {(m.get('rules_secondary') or '')[:300]}")
    print("\nthe events where the probe saw gaps:")
    for t in GAPS:
        try:
            ev = c.get(f"/events/{t}", {"with_nested_markets": "true"})
            e = dict(ev.get("event") or {})
            e.setdefault("markets", ev.get("markets") or [])
            print(f"  {t}: {why(e)}  ({len(e['markets'])} markets, me={e.get('mutually_exclusive')!r})")
            if why(e) != "ok":
                for m in e["markets"][:3]:
                    print(f"      {m['ticker']} | {m.get('yes_sub_title')!r} | event_ticker={m.get('event_ticker')!r}"
                          f" | custom_strike={m.get('custom_strike')!r} | plain={plain_payouts([m])}")
        except Exception as ex:
            print(f"  {t}: lookup failed: {ex}")
    cfg = config.load()
    ctx = Context(c, cfg, "paper", 300.0, 300.0, set(), {}, {})
    sigs = d.STRATEGY.scan(ctx)
    print(f"\nscan now: {len(sigs)} signal(s)")
    for s in sigs:
        print(f"  {s.event}: cost ${s.cost:.2f} pays ${s.payout_min:.2f} | {s.note}")
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
