"""Idea #6, Fed and crypto links: find the series and read their rules. Probe branch only.
Public data only (no key, no orders)."""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal.client import Kalshi  # noqa: E402

FED = re.compile(r"\bfed\b|fomc|federal funds|fed funds|rate cut|rate hike|interest rate|powell", re.I)
CRYPTO = re.compile(r"bitcoin|ethereum|solana|\bxrp\b|dogecoin|\bbtc\b|\beth\b", re.I)
CRYPTO_T = re.compile(r"BTC|ETH|SOL|XRP|DOGE")


def short(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n] + "..."


def main():
    c = Kalshi()
    t0 = time.time()
    allser = c.series()
    print(f"series total: {len(allser)}")
    fed, cry = [], []
    for s in allser:
        t, title = s.get("ticker", ""), s.get("title", "")
        if FED.search(title) or t.startswith("KXFED"):
            fed.append(s)
        elif CRYPTO.search(title) or CRYPTO_T.search(t):
            cry.append(s)
    for name, group in (("FED", fed), ("CRYPTO", cry)):
        print(f"\n== {name}: {len(group)} series")
        for s in sorted(group, key=lambda s: s.get("ticker", "")):
            print(f"  {s.get('ticker')} | {s.get('frequency')} | {short(s.get('title'), 90)}")
    shown = 0
    for s in sorted(fed, key=lambda s: s["ticker"]) + sorted(cry, key=lambda s: s["ticker"]):
        if shown >= 90:
            print("(stopped at 90 series)")
            break
        try:
            evs = c.events(limit=3, series_ticker=s["ticker"], status="open", with_nested_markets="true")
        except Exception as e:
            print(f"\n## {s['ticker']}: events failed: {e}")
            continue
        if not evs:
            continue
        shown += 1
        ev = evs[0]
        ms = ev.get("markets") or []
        print(f"\n## {s['ticker']} open events>={len(evs)} | {ev.get('event_ticker')} | {short(ev.get('title'), 80)} "
              f"| sub={short(ev.get('sub_title'), 40)} | markets={len(ms)} | mutually_exclusive={ev.get('mutually_exclusive')}")
        for m in ms[:8]:
            print("   M " + json.dumps({k: m.get(k) for k in (
                "ticker", "yes_sub_title", "strike_type", "floor_strike", "cap_strike", "close_time",
                "expected_expiration_time", "yes_bid_dollars", "yes_ask_dollars", "volume_fp")}))
        if ms:
            print("   RULES1 " + short(ms[0].get("rules_primary"), 900))
            print("   RULES2 " + short(ms[0].get("rules_secondary"), 500))
    print(f"\ndone in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
