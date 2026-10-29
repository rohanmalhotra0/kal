"""Dump the real shapes of the crypto range / above events (public data only: no key, no orders).

For building range_link_arb: every field, wording and ticker form the open events use now.
"""
import collections
import json
import re
from decimal import Decimal, ROUND_HALF_UP

from kal.client import Kalshi

SERIES = ["KXBTC", "KXBTCD", "KXETH", "KXETHD", "KXSOLE", "KXSOLD", "KXXRP", "KXXRPD", "KXDOGE", "KXDOGED",
          "KXBNB", "KXBNBD", "KXHYPE", "KXHYPED", "KXSHIBA", "KXSHIBAD"]
PRICES = re.compile(r"(_dollars|_fp)$|^(yes|no)_(bid|ask)|^(last|previous)_price|volume|open_interest|liquidity")


def tmpl(s):
    return re.sub(r"\$?\d[\d,]*(\.\d+)?", "#", s or "")


def dec(v):
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def strikes(m):
    """(kind, floor, cap) from the strike fields, or from custom_strike for "custom"."""
    if m.get("strike_type") == "custom":
        cs = m.get("custom_strike") or {}
        return cs.get("strike_type"), dec(cs.get("floor_strike")), dec(cs.get("cap_strike"))
    return m.get("strike_type"), dec(m.get("floor_strike")), dec(m.get("cap_strike"))


def main():
    c = Kalshi()
    total_events = total_markets = 0
    for s in SERIES:
        evs = c.events(series_ticker=s, status="open", with_nested_markets="true")
        print(f"\n######## {s}: {len(evs)} open events")
        r1, r2, subs, labels, kinds_full, ev_keys, mk_keys = (collections.Counter(), collections.Counter(),
                                                              collections.Counter(), collections.Counter(), {},
                                                              collections.Counter(), collections.Counter())
        examples, r2_full, sub_ex = {}, {}, {}
        mid_ok = mid_bad = t_ok = t_bad = rules_ok = rules_bad = 0
        bad_ex = []
        for ev in evs:
            total_events += 1
            ms = ev.get("markets") or []
            total_markets += len(ms)
            ev_keys.update(k for k in ev if k != "markets")
            closes = collections.Counter(m.get("close_time") for m in ms)
            exps = collections.Counter(m.get("expected_expiration_time") for m in ms)
            sts = collections.Counter(strikes(m)[0] for m in ms)
            print(f"  EV {ev.get('event_ticker')} series={ev.get('series_ticker')} me={ev.get('mutually_exclusive')} "
                  f"n={len(ms)} strike_date={ev.get('strike_date')} title={ev.get('title')!r} "
                  f"sub={ev.get('sub_title')!r} kinds={dict(sts)} closes={dict(closes)} exp={dict(exps)} "
                  f"status={dict(collections.Counter(m.get('status') for m in ms))} "
                  f"types={dict(collections.Counter((m.get('market_type'), m.get('settlement_bounds_type')) for m in ms))} "
                  f"early={dict(collections.Counter(m.get('can_close_early') for m in ms))} "
                  f"rd={dict(collections.Counter((m.get('custom_strike') or {}).get('round_digits') for m in ms))}")
            for m in ms:
                mk_keys.update(list(m))
                kind, lo, hi = strikes(m)
                t1 = tmpl(m.get("rules_primary"))
                r1[(kind, t1)] += 1
                examples.setdefault((kind, t1), m.get("rules_primary"))
                r2[m.get("rules_secondary")] += 1
                st = (kind, tmpl(m.get("yes_sub_title")))
                subs[st] += 1
                sub_ex.setdefault(st, m.get("yes_sub_title"))
                et = ev.get("event_ticker") or ""
                tail = m["ticker"][len(et):] if m["ticker"].startswith(et) else "!!" + m["ticker"]
                letter = re.sub(r"[\d.]+", "", tail)
                label = re.sub(r"^-[A-Z]+", "", tail)
                d = len(label.split(".")[1]) if "." in label else 0
                labels[(kind, letter, d)] += 1
                if kind not in kinds_full:
                    kinds_full[kind] = {k: v for k, v in m.items() if not PRICES.search(k)}
                # label vs strikes
                lab = dec(label)
                if kind == "between" and lo is not None and hi is not None and lab is not None:
                    mid = ((lo + hi) / 2).quantize(Decimal(1).scaleb(-d), rounding=ROUND_HALF_UP)
                    if mid == lab and lo <= lab <= hi:
                        mid_ok += 1
                    else:
                        mid_bad += 1
                        bad_ex.append(("mid", m["ticker"], str(lo), str(hi), label))
                elif kind in ("greater", "less") and lab is not None:
                    want = lo if kind == "greater" else hi
                    if want is not None and want == lab:
                        t_ok += 1
                    else:
                        t_bad += 1
                        bad_ex.append(("tail", m["ticker"], str(lo), str(hi), label))
                # rules numbers vs strikes
                nums = [Decimal(x.replace(",", "")) for x in re.findall(r"(?<![\d.])\$?(\d[\d,]*\.?\d*)",
                                                                         (m.get("rules_primary") or "").replace("$", ""))]
                want = [x for x in (lo, hi) if x is not None]
                if all(w in nums for w in want):
                    rules_ok += 1
                else:
                    rules_bad += 1
                    bad_ex.append(("rules", m["ticker"], str(lo), str(hi), m.get("rules_primary")))
        print(f"  event keys: {dict(ev_keys)}")
        print(f"  market keys: {sorted(mk_keys)}")
        print(f"  labels (kind, letters, decimals): {dict(labels)}")
        print(f"  B label = rounded midpoint: ok {mid_ok}, bad {mid_bad}; T label = strike: ok {t_ok}, bad {t_bad}; "
              f"rules hold the strikes: ok {rules_ok}, bad {rules_bad}")
        for b in bad_ex[:8]:
            print("    BAD", b)
        for (kind, t1), n in sorted(r1.items(), key=lambda x: -x[1]):
            print(f"  R1 [{kind}] x{n}: {examples[(kind, t1)]}")
        for t2, n in r2.most_common():
            print(f"  R2 x{n}: {t2!r}")
        for st, n in subs.most_common():
            print(f"  SUB [{st[0]}] x{n}: {sub_ex[st]!r}")
        for kind, m in kinds_full.items():
            print(f"  FULL [{kind}] {json.dumps(m, sort_keys=True)}")
    print(f"\nTOTAL open events {total_events}, markets {total_markets}")


if __name__ == "__main__":
    main()
