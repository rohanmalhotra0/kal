"""Does date_ladder_arb accept real date ladders? Side branch only, never merged.
Public data only: no key, no orders. Reads events and order books, runs the scan."""
import calendar
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
    if not d.same_about(ms):
        return "custom_strike differs (or its date disagrees)"
    bad = [m for m in ms if not d.claims_it_happens_before(m)]
    if bad:
        return "rules don't say 'If <it happens> before <its date>, then ... Yes'"
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
    import re
    tails, sample = collections.Counter(), []
    for ev in events:
        if before_family(ev) and why(ev).startswith("rules don't say"):
            m = next(m for m in ev["markets"] if not d.claims_it_happens_before(m))
            text = m.get("rules_primary") or ""
            g = re.search(r"\bbefore\s+[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}(.{0,40})", text)
            tails[(g.group(1)[:25] if g else "(no 'before <date>')").lower()] += 1
            if len(sample) < 25:
                sample.append(f"{ev['event_ticker']}: {text[:230]!r}")
    print("what follows 'before <date>' in rejected rules:", tails.most_common(15))
    for line in sample:
        print("   ", line)
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
            if r.startswith("rules don't say"):
                m = next(m for m in ev["markets"] if not d.claims_it_happens_before(m))
                print(f"          | {m['ticker']} {m.get('yes_sub_title')!r}: {(m.get('rules_primary') or '')[:220]!r}")
            if r.startswith("custom_strike differs"):
                for m in ev["markets"][:4]:
                    print(f"          | {m['ticker']} {m.get('yes_sub_title')!r} custom_strike={m.get('custom_strike')!r}"
                          f" strike_type={m.get('strike_type')!r} floor={m.get('floor_strike')!r}")
            if r.startswith("own_markets"):
                m = ev["markets"][0]
                print(f"          | {m.get('market_type')} {m.get('settlement_bounds_type')} "
                      f"{(m.get('rules_primary') or '')[:200]} || {(m.get('rules_secondary') or '')[:300]}")
    pairs = listed = 0
    for ev in events:
        if before_family(ev) and why(ev) == "ok":
            ms = sorted(ev["markets"], key=d.deadline)
            for i, a in enumerate(ms):
                for b in ms[i + 1:]:
                    pairs += 1
                    listed += not d.nested(a, b)
    print(f"accepted ladders: {pairs} earlier/later pairs; {listed} refused for listing order (after issuance)")
    gaps = []
    for ev in events:
        if before_family(ev) and why(ev) == "ok":
            for m in ev["markets"]:
                dl = d.deadline(m)
                gaps.append((d.close_ts(m) - calendar.timegm(dl.timetuple())) / 86400)
    gaps.sort()
    if gaps:
        q = lambda f: gaps[min(len(gaps) - 1, int(f * len(gaps)))]
        print(f"\naccepted ladders: close_time minus written deadline, days: min {gaps[0]:.1f}, "
              f"10% {q(.1):.1f}, median {q(.5):.1f}, 90% {q(.9):.1f}, max {gaps[-1]:.1f} ({len(gaps)} markets)")
    print("\nthe events where the probe saw gaps:")
    for t in GAPS:
        try:
            ev = c.get(f"/events/{t}", {"with_nested_markets": "true"})
            e = dict(ev.get("event") or {})
            e.setdefault("markets", ev.get("markets") or [])
            print(f"  {t}: {why(e)}  ({len(e['markets'])} markets, me={e.get('mutually_exclusive')!r})")
            if why(e).startswith("rules don't say"):
                for m in e["markets"][:2]:
                    print(f"      {m['ticker']} | {m.get('yes_sub_title')!r}\n        P: {m.get('rules_primary')!r}"
                          f"\n        S: {(m.get('rules_secondary') or '')[:400]!r}")
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
