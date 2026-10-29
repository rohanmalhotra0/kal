"""The run's copy of all open events must fit the server's memory cap (deploy/kal.service,
MemoryMax=400M). The real list is 15,600 events and 163,000 markets: 350 MB of JSON, over
700 MB in Python (measured 2026-10-29). So a run keeps only events a strategy can act on,
reads them a page at a time, and slims each market, without changing what a strategy sees
in the events it keeps. python -m unittest tests.test_engine_memory
"""
import glob
import os
import re
import time
from datetime import datetime, timezone
from unittest import mock

from kal import client, config, db, engine
from kal.research import discover
from kal.strategies import load_all
from tests.fakes import FakeKalshi
from tests.helpers import TempHome

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAY = 86400


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def event(name, days, **market):
    return {"event_ticker": name, "series_ticker": name.split("-")[0], "mutually_exclusive": False,
            "markets": [{"ticker": f"{name}-1", "event_ticker": name, "close_time": iso(time.time() + days * DAY),
                         **market}]}


def text(s):
    """An equal string that is a different object, as JSON parsing makes them."""
    return "".join(list(s))


class Pages(FakeKalshi):
    """A client that hands out the events a page at a time, like the real one."""

    def __init__(self, pages, **kw):
        super().__init__(**kw)
        self.page_list = pages
        self.event_list = [ev for p in pages for ev in p]

    def event_pages(self, **params):
        yield from self.page_list


class ScopeTests(TempHome):
    def events(self, c, keep=(), horizon=45):
        rc = engine.RunCache(c)
        rc.scope(keep, horizon)
        return {ev["event_ticker"] for ev in rc.events(status="open", with_nested_markets="true")}

    def test_far_events_are_left_out_unless_held(self):
        c = Pages([[event("NEAR-A", 2), event("FAR-A", 200)], [event("FAR-B", 200), event("EDGE-A", 44)]])
        self.assertEqual(self.events(c), {"NEAR-A", "EDGE-A"})
        self.assertEqual(self.events(c, keep={"FAR-B-1"}), {"NEAR-A", "EDGE-A", "FAR-B"})

    def test_a_bet_timed_by_its_scheduled_end_is_kept(self):
        # Bets time themselves by base.scheduled_end: the event's strike_date or a market's
        # occurrence_datetime. Kept if that comes soon, whatever the close_time says.
        game = event("GAME-A", 200, occurrence_datetime=iso(time.time() + 3600))
        index = {**event("INDEX-A", 200), "strike_date": iso(time.time() + 3600)}
        odd = event("ODDTIME-A", 200, occurrence_datetime="soon")
        self.assertEqual(self.events(Pages([[game, index, odd]])), {"GAME-A", "INDEX-A"})

    def test_one_market_in_range_keeps_the_whole_event(self):
        ev = event("MIX-A", 200)
        ev["markets"].append({"ticker": "MIX-A-2", "close_time": iso(time.time() + DAY)})
        rc = engine.RunCache(Pages([[ev]]))
        rc.scope((), 45)
        got = rc.events(status="open", with_nested_markets="true")
        self.assertEqual([m["ticker"] for m in got[0]["markets"]], ["MIX-A-1", "MIX-A-2"])

    def test_anything_unreadable_is_kept_for_the_strategies_to_judge(self):
        odd = [event("BADTIME-A", 200, close_time="soon"), event("NOTIME-A", 200), {"event_ticker": "NOMARKETS-A"},
               {"event_ticker": "ODD-A", "markets": ["not a market"]}]
        odd[1]["markets"][0].pop("close_time")
        self.assertEqual(self.events(Pages([odd])), {"BADTIME-A", "NOTIME-A", "NOMARKETS-A", "ODD-A"})

    def test_a_client_without_pages_still_works(self):
        c = FakeKalshi()
        c.event_list = [event("NEAR-A", 2), event("FAR-A", 200)]
        self.assertEqual(self.events(c), {"NEAR-A"})

    def test_one_download_per_run_and_scope_starts_it_afresh(self):
        c = Pages([[event("NEAR-A", 2), event("FAR-A", 200)]])
        rc = engine.RunCache(c)
        rc.scope((), 45)
        rc.events(status="open", with_nested_markets="true")
        rc.events(status="open", with_nested_markets="true")
        self.assertEqual(rc.calls, 1)
        rc.scope({"FAR-A-1"}, 45)
        got = rc.events(status="open", with_nested_markets="true")
        self.assertEqual({ev["event_ticker"] for ev in got}, {"NEAR-A", "FAR-A"})

    def test_other_event_lists_are_not_filtered(self):
        c = FakeKalshi()
        c.event_list = [event("FAR-A", 200)]
        rc = engine.RunCache(c)
        rc.scope((), 45)
        self.assertEqual(len(rc.events(status="settled", series_ticker="FAR")), 1)

    def test_the_horizon_covers_every_trade_the_risk_check_allows(self):
        cfg = config.load()
        self.assertEqual(engine.horizon_days(cfg), cfg["max_arb_days"] + 1)
        self.assertEqual(engine.horizon_days({**cfg, "max_arb_days": 90}), 91)
        self.assertEqual(engine.horizon_days({**cfg, "research_hours_before_close": 2400}), 101)
        self.assertEqual(engine.horizon_days({**cfg, "max_arb_days": "junk"}), float("inf"))
        self.assertEqual(engine.horizon_days({}), float("inf"))


class LeanTests(TempHome):
    def test_unread_fields_go_and_repeated_text_is_shared(self):
        rules = "In the case of a delay in data caused by a federal government shutdown, the market waits."
        ms = [{"ticker": f"E-{i}", "close_time": iso(time.time() + DAY), "rules_secondary": text(rules),
               "price_ranges": [{"start": "0.00", "end": "1.00", "step": "0.01"}], "updated_time": "x",
               "early_close_condition": text("This market will close and expire early if the event occurs."),
               "yes_bid_dollars": "0.5000", "floor_strike": 1.5} for i in range(2)]
        self.assertIsNot(ms[0]["rules_secondary"], ms[1]["rules_secondary"])
        rc = engine.RunCache(Pages([[{"event_ticker": "E", "markets": ms}]]))
        rc.scope((), 45)
        a, b = rc.events(status="open", with_nested_markets="true")[0]["markets"]
        self.assertIs(a["rules_secondary"], b["rules_secondary"])
        self.assertEqual(a["rules_secondary"], rules)
        for k in ("price_ranges", "updated_time", "early_close_condition"):
            self.assertNotIn(k, a)
        self.assertEqual((a["ticker"], a["yes_bid_dollars"], a["floor_strike"]), ("E-0", "0.5000", 1.5))

    def test_no_code_reads_a_field_the_run_drops(self):
        # If code starts to read one, take it out of engine.UNREAD: else it reads None.
        names = set(engine.UNREAD) | {re.sub(r"_(dollars|fp)$", "", k) for k in engine.UNREAD}
        found = []
        for path in glob.glob(os.path.join(ROOT, "kal", "**", "*.py"), recursive=True):
            with open(path) as f:
                src = f.read()
            if path.endswith(os.path.join("kal", "engine.py")):
                src = src.replace(src[src.index("UNREAD = ("):src.index(")\n", src.index("UNREAD = ("))], "")
            found += [f"{os.path.relpath(path, ROOT)}: {n}" for n in names if re.search(rf"\b{n}\b", src)]
        self.assertEqual(found, [])


class EngineScopeTests(TempHome):
    def test_a_strategy_sees_far_events_the_bot_holds(self):
        config.update({"enabled": True, "budget": 0,
                       "strategies": {k: {"mode": "off"} for k in config.DEFAULTS["strategies"]}})
        config.update({"strategies": {"ladder_arb": {"mode": "paper"}}})
        db.insert("paper", signal_id=0, strategy="ladder_arb", event="FAR-A", ticker="FAR-A-1", side="yes",
                  count=1, price=0.5, cost=0.5)
        c = Pages([[event("NEAR-A", 2), event("FAR-A", 200), event("FAR-B", 200)]])
        c.market_info["FAR-A-1"] = {"ticker": "FAR-A-1", "status": "active"}
        seen = []
        strat = load_all()["ladder_arb"]
        with mock.patch.object(type(strat), "scan",
                               lambda self, ctx: seen.append(ctx.client.events(status="open",
                                                                               with_nested_markets="true")) or []):
            engine.run_once(c)
        self.assertEqual([{ev["event_ticker"] for ev in evs} for evs in seen], [{"NEAR-A", "FAR-A"}])


class StreamTests(TempHome):
    def test_pages_and_paginate_agree(self):
        replies = [{"events": [1, 2], "cursor": "a"}, {"events": [3], "cursor": "b"}, {"events": None, "cursor": ""}]
        c = client.Kalshi()
        with mock.patch.object(c, "get", side_effect=lambda e, p: replies[{None: 0, "a": 1, "b": 2}[p.get("cursor")]]):
            self.assertEqual(list(c.pages("/events", "events")), [[1, 2], [3], []])
            self.assertEqual(c.paginate("/events", "events"), [1, 2, 3])
            self.assertEqual(c.paginate("/events", "events", limit=2), [1, 2])

    def test_strict_pages_refuse_a_reply_of_another_shape(self):
        c = client.Kalshi()
        with mock.patch.object(c, "get", return_value={"other": []}):
            with self.assertRaises(client.KalshiError):
                list(c.pages("/portfolio/fills", "fills", strict=True))

    def test_research_counts_volume_a_page_at_a_time(self):
        def ev(series, vol):
            return {"series_ticker": series, "markets": [{"volume_24h_fp": str(vol)}]}
        c = Pages([[ev("KXA", 5), ev("KXB", 1)], [ev("KXA", 5), ev("KXMVEX", 99), ev("KXC", 0)]])
        self.assertEqual(discover.active_series(c), ["KXA", "KXB"])

    def test_research_gives_up_cleanly_if_a_page_fails(self):
        class Broken(Pages):
            def event_pages(self, **params):
                yield [{"series_ticker": "KXA", "markets": [{"volume_24h_fp": "5"}]}]
                raise client.KalshiError("GET /events -> 500")
        self.assertEqual(discover.active_series(Broken([])), [])
