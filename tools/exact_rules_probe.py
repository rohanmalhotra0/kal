"""Idea #17, step 2: the full rules of the "exactly" (KXECONSTAT*) and "above" series.
Probe branch only. Public data only (no key, no orders).

Prints, per series: the series' terms (and its contract-terms PDF as text), the wording
templates of every market's rules (numbers and dates blanked) with counts, one open event in
full, and how the 2025 shutdown months settled (Oct 2025 CPI and jobs numbers were never
published).
"""
import collections
import io
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal.client import Kalshi  # noqa: E402

PAIRS = [("KXECONSTATCPI", "KXCPI"), ("KXECONSTATCPICORE", "KXCPICORE"), ("KXECONSTATCPIYOY", "KXCPIYOY"),
         ("KXECONSTATCORECPIYOY", "KXCPICOREYOY"), ("KXECONSTATU3", "KXU3")]
SERIES = [s for p in PAIRS for s in p]
SERIES_KEYS = ("title", "category", "frequency", "settlement_sources", "contract_url", "contract_terms_url",
               "fee_type", "fee_multiplier", "product_metadata", "additional_prohibitions", "tags")
EVENT_KEYS = ("event_ticker", "series_ticker", "title", "sub_title", "mutually_exclusive", "strike_date",
              "strike_period", "category", "collateral_return_type", "available_on_brokers")
MARKET_KEYS = ("ticker", "event_ticker", "market_type", "status", "strike_type", "floor_strike", "cap_strike",
               "functional_strike", "custom_strike", "title", "subtitle", "yes_sub_title", "no_sub_title",
               "open_time", "close_time", "expected_expiration_time", "expiration_time", "latest_expiration_time",
               "can_close_early", "early_close_condition", "settlement_timer_seconds", "response_price_units",
               "price_level_structure", "tick_size", "notional_value", "result", "settlement_value",
               "settlement_value_dollars", "expiration_value", "yes_ask_dollars", "yes_bid_dollars",
               "volume_fp", "open_interest_fp", "liquidity_dollars")
MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
SHUTDOWN = re.compile(r"\b(?:sep|oct|nov)[a-z]*\.? 2025\b", re.I)


def plain(text):
    return " ".join(str(text or "").split())


def template(text):
    """The wording with numbers, months and years blanked, so markets that differ only there
    look the same."""
    t = plain(text)
    t = re.sub(rf"\b{MONTH} \d{{1,2}}, \d{{4}}", "<DATE>", t, flags=re.I)
    t = re.sub(rf"\b{MONTH} \d{{4}}", "<MONTH>", t, flags=re.I)
    t = re.sub(r"-?\d+(?:\.\d+)?", "<N>", t)
    return t


def pdf_text(url):
    try:
        from pypdf import PdfReader
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(r.content)).pages)
    except Exception as e:
        return f"(couldn't read it: {type(e).__name__}: {e})"


def pick(d, keys):
    return {k: d.get(k) for k in keys if k in d}


def main():
    c = Kalshi()
    t0 = time.time()
    pdfs = {}
    for s in SERIES:
        print(f"\n\n######## {s}")
        try:
            info = c.series_info(s)
        except Exception as e:
            print(f"series: failed: {e}")
            info = {}
        print("SERIES " + json.dumps(pick(info, SERIES_KEYS)))
        url = info.get("contract_terms_url")
        if url and url not in pdfs:
            pdfs[url] = pdf_text(url)
            print(f"--- TERMS {url}\n{' '.join(pdfs[url].split())[:7000]}\n--- END TERMS")
        elif url:
            print(f"--- TERMS {url}: same as above")

        try:
            settled = c.settled_markets(s, limit=4000)
        except Exception as e:
            print(f"settled: failed: {e}")
            settled = []
        try:
            events = c.events(series_ticker=s, status="open", with_nested_markets="true")
        except Exception as e:
            print(f"open: failed: {e}")
            events = []
        opened = [m for ev in events for m in ev.get("markets") or []]
        print(f"markets: {len(settled)} settled, {len(opened)} open in {len(events)} open events")

        for field in ("rules_primary", "rules_secondary", "yes_sub_title", "strike_type"):
            seen = collections.Counter(template(m.get(field)) for m in settled + opened)
            print(f"-- {field}: {len(seen)} template(s)")
            for t, n in seen.most_common(4):
                print(f"   {n:>5}  {t[:700]}")
        results = collections.Counter(str(m.get("result")) for m in settled)
        print(f"-- results: {dict(results)}")
        first = min((m.get("close_time") or "" for m in settled), default="")
        print(f"-- earliest settled close: {first}")

        if events:
            ev = min(events, key=lambda e: min((m.get("close_time") or "9") for m in e.get("markets") or [{}]))
            print("EVENT " + json.dumps(pick(ev, EVENT_KEYS)))
            ms = sorted(ev.get("markets") or [], key=lambda m: str(m.get("ticker")))
            for i, m in enumerate(ms):
                if i < 2:
                    print("MARKET " + json.dumps(pick(m, MARKET_KEYS)))
                    print("   RULES1 " + plain(m.get("rules_primary")))
                    print("   RULES2 " + plain(m.get("rules_secondary")))
                else:
                    print(f"   m {m.get('ticker')} {m.get('strike_type')} floor={m.get('floor_strike')} "
                          f"cap={m.get('cap_strike')} '{m.get('yes_sub_title')}' "
                          f"ask={m.get('yes_ask_dollars')} bid={m.get('yes_bid_dollars')}")

        hit = [m for m in settled if SHUTDOWN.search(str(m.get("rules_primary") or ""))]
        print(f"-- Sep-Nov 2025 markets: {len(hit)}")
        by_event = collections.defaultdict(list)
        for m in hit:
            by_event[str(m.get("event_ticker") or str(m.get("ticker")).rsplit("-", 1)[0])].append(m)
        for ev, ms in sorted(by_event.items()):
            m = ms[0]
            res = collections.Counter(str(x.get("result")) for x in ms)
            print(f"   SHUT {ev}: {len(ms)} markets {dict(res)} value={m.get('expiration_value')} "
                  f"close={m.get('close_time')} expired={m.get('expiration_time')}")
            print("        2: " + plain(m.get("rules_secondary"))[:600])
        print(f"({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
