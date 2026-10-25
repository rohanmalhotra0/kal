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
    # What stricter rules would cost (review round 2)
    always = unknown = 0
    words = collections.Counter()
    one_sentence = ends_otherwise = 0
    WORDS = ["retract", "withdraw", "revers", "overturn", "rescind", "revoke", "repeal", "annul", "invalidat",
             "expiration", "expiry", "expire", "within", "yet to", "avoid", " no ", "none", "neither", " nor ",
             "days before", "weeks before", "day before", "open", "trading begins", "contract", "issu", "listed"]
    accepted = [ev for ev in events if before_family(ev) and why(ev) == "ok"]
    import re as _re
    for ev in accepted:
        ms = sorted(ev["markets"], key=d.deadline)
        for i, a in enumerate(ms):
            for b in ms[i + 1:]:
                oa, ob = d._opened(a), d._opened(b)
                if oa is None or ob is None:
                    unknown += 1
                elif ob > oa:
                    always += 1
        text = " ".join(f" {(m.get('rules_primary') or '')} {(m.get('rules_secondary') or '')} " for m in ms).lower()
        for w in WORDS:
            if w in text:
                words[w] += 1
        prim = (ms[0].get("rules_primary") or "").strip()
        g = d._CLAIM.match(prim)
        rest = prim[g.end():].strip() if g else "?"
        if rest in ("", "."):
            one_sentence += 1
        elif _re.fullmatch(r"\.?\s*otherwise,? the market resolves to no\.?", rest, _re.I):
            ends_otherwise += 1
    print(f"if every pair needed listing order: {always} of {pairs} pairs refused ({unknown} with no open_time)")
    print(f"accepted events whose rules_primary is just the claim: {one_sentence}; claim + 'Otherwise ... No': "
          f"{ends_otherwise}; of {len(accepted)}")
    print("accepted events whose rules (primary+secondary) contain:", sorted(words.items(), key=lambda x: -x[1]))
    tails = collections.Counter()
    for ev in accepted:
        prim = (sorted(ev["markets"], key=d.deadline)[0].get("rules_primary") or "").strip()
        g = d._CLAIM.match(prim)
        rest = prim[g.end():].strip() if g else "?"
        if rest not in ("", "."):
            tails[rest[:90]] += 1
    print("what follows the claim in rules_primary (top):")
    for t, n in tails.most_common(12):
        print(f"   {n:3d}  {t!r}")
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
    # The 7 real gap pair-hours that passed the bot's money rules (after the 25% cap), by pair.
    PAIRS = [("KXALBUMRELEASEDATE-NEWDRA", "26SEP26", "26OCT01", "4 hours: 09-04 01-03h (14-15c), 09-18 17h (2c)"),
             ("KXHEGSETHOUT-26APR", "MAY01", "JUN01", "1 hour: 04-07 01h (2c)"),
             ("KXFISAEXTEND-26APR", "MAY02", "MAY03", "1 hour: 04-30 01h (2c)"),
             ("KXMLBDEBUT-KANDERSON", "26SEP01", "26OCT01", "1 hour: 08-19 10h (1c)")]

    def market(t):
        for path in (f"/markets/{t}", f"/historical/markets/{t}"):
            try:
                got = c.get(path).get("market")
                if got:
                    return got
            except Exception:
                pass
        return None

    print("\nthe 7 gap hours, pair by pair, through the strategy's checks:")
    for ev_t, a_s, b_s, hours in PAIRS:
        a, b = market(f"{ev_t}-{a_s}"), market(f"{ev_t}-{b_s}")
        if not a or not b:
            print(f"  {ev_t} {a_s}/{b_s} ({hours}): market not found ({bool(a)}, {bool(b)})")
            continue
        try:
            evd = c.get(f"/events/{ev_t}").get("event") or {}
        except Exception:
            evd = {}
        checks = {
            "mutually_exclusive is False": evd.get("mutually_exclusive") is False,
            "plain payouts": plain_payouts([a, b]),
            "wording 'Before <date>'": bool(d.deadline(a) and d.deadline(b)),
            "rules claim 'if it happens before <its date>'": d.claims_it_happens_before(a) and d.claims_it_happens_before(b),
            "rules match (own deadline out)": d._rules(a) == d._rules(b),
            "custom_strike matches": d.same_about([a, b]),
            "listing order nests": d.nested(a, b),
        }
        bad = [k for k, ok in checks.items() if not ok]
        print(f"  {ev_t} {a_s} -> {b_s} ({hours}): {'PASS' if not bad else 'REFUSED: ' + '; '.join(bad)}")
        print(f"      opened {a.get('open_time')} / {b.get('open_time')}; can_close_early {a.get('can_close_early')}"
              f"/{b.get('can_close_early')}; counts from listing: {d.counts_from_listing(a)}")
        print(f"      rules: {(a.get('rules_primary') or '')[:160]!r}")
        if not checks["plain payouts"]:
            print(f"      why not plain: {(a.get('rules_secondary') or '')[:240]!r}")
    cfg = config.load()
    ctx = Context(c, cfg, "paper", 300.0, 300.0, set(), {}, {})
    sigs = d.STRATEGY.scan(ctx)
    print(f"\nscan now: {len(sigs)} signal(s)")
    for s in sigs:
        print(f"  {s.event}: cost ${s.cost:.2f} pays ${s.payout_min:.2f} | {s.note}")
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
