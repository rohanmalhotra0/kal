"""Cross-venue research (idea #11): python -m unittest tests.test_xvenue

Matching games across Kalshi and Polymarket, prices at the decision time with no
peeking, the fee, the statistics, and the runner end to end. All data is fake."""
import io
import json
import math
import os
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone

from kal.research import backtest, xvenue
from kal.strategies import favorites
from tests.fakes import FakeKalshi
from tests.helpers import TempHome

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import xvenue_research  # noqa: E402

H = 3600
DAY = 86400
SAT = 1_790_424_000          # 2026-09-26 12:00 UTC, a Saturday
NOW = SAT + 20 * DAY


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def poly_time(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00")


# ---------- fake Kalshi games ----------

def kalshi_game(series, event, teams, start, winner, end=None, **extra):
    """Settled Kalshi markets for one game. teams: [(code, name)]; "TIE" is the draw."""
    end = start + 3 * H if end is None else end
    out = []
    for code, name in teams:
        won = code == winner
        out.append({"ticker": f"{event}-{code}", "event_ticker": event, "yes_sub_title": name,
                    "open_time": iso(start - 7 * DAY), "close_time": iso(start + 2 * H + 1234),
                    "occurrence_datetime": iso(end), "expected_expiration_time": iso(end), "can_close_early": True,
                    "status": "finalized", "result": "yes" if won else "no",
                    "settlement_value_dollars": "1.0000" if won else "0.0000", **extra})
    return out


def candle(t, ask, bid, volume=500.0):
    return {"end_period_ts": t, "yes_ask": {"close_dollars": f"{ask:.4f}"},
            "yes_bid": {"close_dollars": f"{bid:.4f}"}, "volume_fp": f"{volume:.2f}"}


class AllCandles(FakeKalshi):
    """Sends every candle whatever range is asked for, like a buggy API would."""

    def candles(self, series, ticker, start_ts, end_ts, period=60):
        return self.candle_data.get(ticker, [])


# ---------- fake Polymarket ----------

def two_way(league, slug, outcomes, tokens, start, finals=("0", "1"), closed=True, listed=None):
    return {"id": slug, "slug": slug, "question": " vs. ".join(outcomes), "outcomes": json.dumps(outcomes),
            "clobTokenIds": json.dumps(tokens), "outcomePrices": json.dumps(list(finals)),
            "gameStartTime": poly_time(start), "sportsMarketType": "moneyline", "closed": closed,
            "startDate": iso(listed if listed is not None else start - 3 * DAY),
            "events": [{"id": "ev-" + slug, "slug": slug, "title": " vs. ".join(outcomes), "startTime": iso(start)}]}


def three_way(slug, home, away, tokens, start, winner):
    """Soccer: one Yes/No market per outcome (home, draw, away). tokens: (home, draw, away)."""
    out = []
    for part, title, tok in ((home[0], home[1], tokens[0]), ("draw", f"Draw ({home[1]} vs. {away[1]})", tokens[1]),
                             (away[0], away[1], tokens[2])):
        won = part == winner
        out.append({"id": f"{slug}-{part}", "slug": f"{slug}-{part}", "question": f"Will {title} win?",
                    "outcomes": '["Yes", "No"]', "clobTokenIds": json.dumps([tok, tok + "-no"]),
                    "outcomePrices": json.dumps(["1" if won else "0", "0" if won else "1"]),
                    "groupItemTitle": title, "gameStartTime": poly_time(start), "closed": True,
                    "startDate": iso(start - 5 * DAY),
                    "events": [{"id": "ev-" + slug, "slug": slug, "title": f"{home[1]} vs. {away[1]}",
                                "startTime": iso(start)}]})
    return out


class FakePolymarket:
    """Answers like Polymarket's gamma and clob APIs. Price history comes back whole,
    later points included, whatever range is asked for."""

    def __init__(self):
        self.sports = [{"sport": "nfl", "primaryTagId": 450, "tags": "1,450,100639"},
                       {"sport": "mlb", "primaryTagId": 100381, "tags": "1,100639,100381"},
                       {"sport": "cfb", "primaryTagId": 100351, "tags": "1,100351,100639"},
                       {"sport": "epl", "primaryTagId": 306, "tags": "1,82,306,100639,100350"}]
        self.teams = {"nfl": [{"name": "New England Patriots", "abbreviation": "ne", "alias": "Patriots"},
                              {"name": "Chicago Bears", "abbreviation": "chi", "alias": "Bears"},
                              {"name": "Jacksonville Jaguars", "abbreviation": "jax", "alias": "Jaguars"},
                              {"name": "Philadelphia Eagles", "abbreviation": "phi", "alias": "Eagles"}],
                      "mlb": [{"name": "Baltimore Orioles", "abbreviation": "bal", "alias": "Orioles"},
                              {"name": "New York Yankees", "abbreviation": "nyy", "alias": "Yankees"}],
                      "cfb": [{"name": "Volunteers", "abbreviation": "tenn", "alias": "Tennessee"},
                              {"name": "Tigers", "abbreviation": "aubrn", "alias": "Auburn"}],
                      "epl": [{"name": "Arsenal FC", "abbreviation": "ars", "alias": "Arsenal"},
                              {"name": "Chelsea FC", "abbreviation": "che", "alias": "Chelsea"}]}
        self.markets = {}      # tag -> [market]
        self.history = {}      # token -> [{"t", "p"}]
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url.rsplit("/", 1)[-1], dict(params)))
        if url.endswith("/sports"):
            return self.sports
        if url.endswith("/teams"):
            return self.teams.get(params["league"], [])[params["offset"]:params["offset"] + params["limit"]]
        if url.endswith("/markets"):
            ms = [m for m in self.markets.get(str(params["tag_id"]), [])
                  if str(bool(m.get("closed"))).lower() == params["closed"]
                  and params["start_date_min"] <= m["startDate"] <= params["start_date_max"]]
            return ms[params["offset"]:params["offset"] + params["limit"]]
        if url.endswith("/prices-history"):
            return {"history": self.history.get(params["market"], [])}
        raise AssertionError(f"unexpected url {url}")


# ---------- helpers ----------

def kgames(markets, league="NFL", series="KXNFLGAME"):
    return xvenue.kalshi_games(markets, league, series)


def pgames(markets, teams=()):
    return xvenue.poly_games(markets, teams)


def nfl_game(start, event="KXNFLGAME-26SEP27NECHI", winner="CHI"):
    return kalshi_game("KXNFLGAME", event, [("NE", "New England"), ("CHI", "Chicago")], start, winner)


def row(event="KXNFLGAME-26SEP27NECHI", ask=0.50, bid=0.48, poly=0.50, won=True, volume=1000.0, day=0,
        league="NFL", series="KXNFLGAME", final=None):
    anchor = SAT + day * DAY + 3 * H
    return {"league": league, "series": series, "event": event, "ticker": event + "-X", "anchor": anchor,
            "decided": anchor - 6 * H, "close": anchor, "yes_ask": ask, "yes_bid": bid, "volume": volume,
            "poly": poly, "result": "yes" if won else "no", "yes_value": 1.0 if won else 0.0,
            "poly_final": final}


# ---------- 1. the scheduled end ----------

class ScheduledEndTests(TempHome):
    def test_listed_end_on_the_minute(self):
        m = kalshi_game("S", "S-1", [("A", "A")], SAT, "A")[0]
        self.assertEqual(xvenue.scheduled_end(m), SAT + 3 * H)

    def test_a_long_game_keeps_its_listed_end(self):
        # After overtime Kalshi rewrites occurrence_datetime to the real end (off the minute).
        m = kalshi_game("S", "S-1", [("A", "A")], SAT, "A")[0]
        m["occurrence_datetime"] = iso(SAT + 3 * H + 2690)
        self.assertEqual(xvenue.scheduled_end(m), SAT + 3 * H)
        m["expected_expiration_time"] = iso(SAT + 3 * H + 2690)   # nothing listed left: skip it
        self.assertIsNone(xvenue.scheduled_end(m))

    def test_never_the_real_close(self):
        m = kalshi_game("S", "S-1", [("A", "A")], SAT, "A")[0]
        del m["occurrence_datetime"], m["expected_expiration_time"]
        self.assertIsNone(xvenue.scheduled_end(m))
        m["can_close_early"] = False    # then the close is fixed, and is the schedule
        self.assertEqual(xvenue.scheduled_end(m), SAT + 2 * H + 1234)


# ---------- 2. team names ----------

class NameTests(TempHome):
    def test_names_that_mean_the_same_team(self):
        for a, b in [("Los Angeles F", "Los Angeles FC"), ("Los Angeles G", "Los Angeles Galaxy"),
                     ("Bodoe/Glimt", "FK Bodø/Glimt"),
                     ("Alcorn St.", "Alcorn State"), ("Hawaii", "Hawai'i"), ("M´gladbach", "Borussia Mönchengladbach"),
                     ("Manchester City", "Manchester City FC"), ("Reg Time: Arsenal", "Arsenal FC")]:
            self.assertEqual(xvenue.name_score(a, b), 1.0, (a, b))

    def test_close_enough(self):
        rb = xvenue.name_score("New York RB", "New York Red Bulls")    # RB = Red Bulls
        self.assertGreaterEqual(rb, xvenue.SOME)
        self.assertGreater(rb, xvenue.name_score("New York RB", "New York City FC"))
        self.assertGreaterEqual(xvenue.name_score("Saint Louis", "St. Louis City SC"), xvenue.SOME)   # St. = Saint
        self.assertGreaterEqual(xvenue.name_score("Be`er Sheva", "MH Hapoel Be'er Sheva"), xvenue.SOME)
        self.assertEqual(xvenue.name_score("Ohio St.", "Ohio State"), 1.0)                             # St. = State

    def test_names_that_dont(self):
        self.assertLess(xvenue.name_score("Los Angeles F", "Los Angeles Galaxy"), 0.7)
        self.assertLess(xvenue.name_score("New York City", "New York Red Bulls"), 0.7)
        self.assertEqual(xvenue.name_score("Texas", "Texas Tech"), 0.5)    # never sure
        self.assertEqual(xvenue.name_score("Manchester City", "Manchester United FC"), 0.5)
        self.assertEqual(xvenue.name_score("Bilbao", "Villarreal CF"), 0.0)

    def test_a_draw_only_matches_a_draw(self):
        tie = {"code": "TIE", "name": "Tie", "draw": True}
        team = {"code": "ARS", "name": "Arsenal", "draw": False}
        draw = {"draw": True, "abbr": None, "names": ["Draw (Arsenal FC vs. Chelsea FC)"]}
        arsenal = {"draw": False, "abbr": "ars", "names": ["Arsenal FC"]}
        self.assertEqual(xvenue.side_score("epl", tie, draw), 1.0)
        self.assertEqual(xvenue.side_score("epl", tie, arsenal), 0.0)
        self.assertEqual(xvenue.side_score("epl", team, draw), 0.0)   # "Draw (Arsenal ...)" names Arsenal

    def test_league_markets_tries_the_leagues_other_tags(self):
        # On 2026-10-25 Serie A's primary tag had no markets; another of its tags did.
        pm = FakePolymarket()
        sport = {"sport": "sea", "primaryTagId": 100618, "tags": "1,100639,101962,100350,100618"}
        mine = two_way("sea", "sea-int-mil-2026-09-26", ["Inter", "Milan"], ["i", "m"], SAT)
        theirs = two_way("ucl", "ucl-int-bar-2026-09-27", ["Inter", "Barcelona"], ["i2", "b"], SAT)
        pm.markets = {"101962": [mine, theirs]}
        got = xvenue.Polymarket(pm).league_markets(sport, SAT - 10 * DAY, SAT + DAY)
        self.assertEqual([m["slug"] for m in got], ["sea-int-mil-2026-09-26"])
        tags = {str(p["tag_id"]) for name, p in pm.calls if name == "markets"}
        self.assertEqual(tags, {"100618", "101962"})        # never the shared Sports/Games/Soccer tags

    def test_codes_that_differ_between_venues(self):
        jac = {"code": "JAC", "name": "Jacksonville", "draw": False}
        self.assertEqual(xvenue.side_score("nfl", jac, {"draw": False, "abbr": "jax", "names": ["Jaguars"]}), 1.0)
        self.assertEqual(xvenue.side_score("nhl", jac, {"draw": False, "abbr": "jax", "names": ["Jaguars"]}), 0.0)


# ---------- 3. matching ----------

class MatchTests(TempHome):
    def setUp(self):
        super().setUp()
        self.teams = FakePolymarket().teams

    def match(self, kalshi, poly, league="nfl"):
        return xvenue.match(kalshi, pgames(poly, self.teams[league]), league)

    def test_the_right_game(self):
        k = kgames(nfl_game(SAT))
        p = [two_way("nfl", "nfl-ne-chi-2026-09-26", ["Patriots", "Bears"], ["tNE", "tCHI"], SAT)]
        pairs, why = self.match(k, p)
        self.assertEqual(len(pairs), 1, why)
        _, pg, sides = pairs[0]
        self.assertEqual(pg["slug"], "nfl-ne-chi-2026-09-26")
        self.assertEqual(sides["KXNFLGAME-26SEP27NECHI-NE"]["token"], "tNE")
        self.assertEqual(sides["KXNFLGAME-26SEP27NECHI-CHI"]["token"], "tCHI")

    def test_swapped_home_and_away_keep_each_teams_own_price(self):
        k = kgames(nfl_game(SAT))
        p = [two_way("nfl", "nfl-chi-ne-2026-09-26", ["Bears", "Patriots"], ["tCHI", "tNE"], SAT)]
        (_, _, sides), = self.match(k, p)[0]
        self.assertEqual(sides["KXNFLGAME-26SEP27NECHI-NE"]["token"], "tNE")
        self.assertEqual(sides["KXNFLGAME-26SEP27NECHI-CHI"]["token"], "tCHI")

    def test_the_same_teams_on_another_date_dont_match(self):
        k = kgames(nfl_game(SAT))
        later = [two_way("nfl", "nfl-ne-chi-2026-09-27", ["Patriots", "Bears"], ["t1", "t2"], SAT + DAY)]
        pairs, why = self.match(k, later)
        self.assertEqual(pairs, [])
        self.assertEqual(sum(why.values()), 1)
        both = later + [two_way("nfl", "nfl-ne-chi-2026-09-26", ["Patriots", "Bears"], ["t3", "t4"], SAT)]
        (_, pg, _), = self.match(k, both)[0]
        self.assertEqual(pg["slug"], "nfl-ne-chi-2026-09-26")

    def test_a_baseball_series_never_matches_the_next_days_game(self):
        # Kalshi's time was wrong by 5 hours: the only Polymarket games are this one and
        # tomorrow's. The teams meet twice within 36 hours, so it is not guessed.
        k = kgames(kalshi_game("KXMLBGAME", "KXMLBGAME-26SEP261305BALNYY", [("BAL", "Baltimore"), ("NYY", "New York Y")],
                               SAT, "NYY"), "MLB", "KXMLBGAME")
        p = [two_way("mlb", "mlb-bal-nyy-2026-09-26", ["Baltimore Orioles", "New York Yankees"], ["a", "b"], SAT + 5 * H),
             two_way("mlb", "mlb-bal-nyy-2026-09-27", ["Baltimore Orioles", "New York Yankees"], ["c", "d"], SAT + DAY)]
        self.assertEqual(self.match(k, p, "mlb")[0], [])

    def test_doubleheader_games_each_find_their_own(self):
        g1 = kalshi_game("KXMLBGAME", "KXMLBGAME-26SEP261305BALNYYG1", [("BAL", "Baltimore"), ("NYY", "New York Y")],
                         SAT, "NYY")
        g2 = kalshi_game("KXMLBGAME", "KXMLBGAME-26SEP261635BALNYYG2", [("BAL", "Baltimore"), ("NYY", "New York Y")],
                         SAT + 3.5 * H, "BAL")
        k = kgames(g1 + g2, "MLB", "KXMLBGAME")
        p = [two_way("mlb", "mlb-bal-nyy-2026-09-26-game-2", ["Baltimore Orioles", "New York Yankees"], ["b1", "y1"],
                     SAT + 3.5 * H),
             two_way("mlb", "mlb-bal-nyy-2026-09-26", ["New York Yankees", "Baltimore Orioles"], ["y0", "b0"], SAT)]
        pairs, why = self.match(k, p, "mlb")
        self.assertEqual(len(pairs), 2, why)
        got = {kg["event"]: (pg["slug"], sides[kg["event"] + "-BAL"]["token"]) for kg, pg, sides in pairs}
        self.assertEqual(got["KXMLBGAME-26SEP261305BALNYYG1"], ("mlb-bal-nyy-2026-09-26", "b0"))
        self.assertEqual(got["KXMLBGAME-26SEP261635BALNYYG2"], ("mlb-bal-nyy-2026-09-26-game-2", "b1"))

    def test_a_placeholder_start_still_finds_the_one_game_these_teams_play(self):
        # Kalshi lists every college game of a Saturday at one time; the real kickoff is 6h later.
        k = kgames(kalshi_game("KXNCAAFGAME", "KXNCAAFGAME-26SEP26AUBTENN", [("AUB", "Auburn"), ("TENN", "Tennessee")],
                               SAT, "TENN"), "CFB", "KXNCAAFGAME")
        p = [two_way("cfb", "cfb-aubrn-tenn-2026-09-26", ["Auburn", "Tennessee"], ["au", "te"], SAT + 6 * H)]
        pairs, why = self.match(k, p, "cfb")
        self.assertEqual(len(pairs), 1)
        self.assertEqual(why["matched, Kalshi's start a placeholder"], 1)
        self.assertEqual(pairs[0][2]["KXNCAAFGAME-26SEP26AUBTENN-AUB"]["token"], "au")
        far = [two_way("cfb", "cfb-aubrn-tenn-2026-09-27", ["Auburn", "Tennessee"], ["au", "te"], SAT + 13 * H)]
        self.assertEqual(self.match(k, far, "cfb")[0], [])

    def test_unsure_names_dont_match(self):
        k = kgames(kalshi_game("KXNFLGAME", "KXNFLGAME-26SEP27NECHI", [("NE", "New England"), ("CHI", "Chicago")],
                               SAT, "CHI"))
        other = [two_way("nfl", "nfl-jax-phi-2026-09-26", ["Jaguars", "Eagles"], ["j", "p"], SAT)]
        self.assertEqual(self.match(k, other)[0], [])
        half = [two_way("nfl", "nfl-ne-phi-2026-09-26", ["Patriots", "Eagles"], ["n", "p"], SAT)]
        self.assertEqual(self.match(k, half)[0], [])    # one team agreeing isn't enough in a two-way game

    def test_soccer_maps_the_tie_to_the_draw(self):
        k = kgames(kalshi_game("KXEPLGAME", "KXEPLGAME-26SEP26ARSCFC", [("ARS", "Arsenal"), ("CFC", "Chelsea"),
                                                                        ("TIE", "Tie")], SAT, "TIE"), "EPL", "KXEPLGAME")
        p = three_way("epl-ars-che-2026-09-26", ("ars", "Arsenal FC"), ("che", "Chelsea FC"), ("h", "d", "a"), SAT, "draw")
        pairs, why = xvenue.match(k, pgames(p, self.teams["epl"]), "epl")
        self.assertEqual(len(pairs), 1, why)
        sides = pairs[0][2]
        self.assertEqual({t: s["token"] for t, s in sides.items()},
                         {"KXEPLGAME-26SEP26ARSCFC-ARS": "h", "KXEPLGAME-26SEP26ARSCFC-TIE": "d",
                          "KXEPLGAME-26SEP26ARSCFC-CFC": "a"})
        self.assertEqual(sides["KXEPLGAME-26SEP26ARSCFC-TIE"]["final"], 1.0)

    def test_each_polymarket_game_is_used_once(self):
        a = nfl_game(SAT, "KXNFLGAME-26SEP27NECHI")
        b = nfl_game(SAT + 1800, "KXNFLGAME-26SEP27NECHI2")   # a duplicate listing
        p = [two_way("nfl", "nfl-ne-chi-2026-09-26", ["Patriots", "Bears"], ["n", "c"], SAT)]
        pairs, why = self.match(kgames(a + b), p)
        self.assertEqual([kg["event"] for kg, _, _ in pairs], ["KXNFLGAME-26SEP27NECHI"])
        self.assertEqual(why["its Polymarket game was taken by a surer match"], 1)


# ---------- 4. prices at the decision time, no peeking ----------

class PriceTests(TempHome):
    def test_kalshi_quote_ignores_later_candles(self):
        c = AllCandles()
        m = nfl_game(SAT)[0]
        t = SAT - 3 * H
        c.candle_data[m["ticker"]] = [candle(t - H, 0.50, 0.48, 300), candle(t - 30 * H, 0.10, 0.08, 9999),
                                      candle(t + H, 0.99, 0.98, 5000)]
        self.assertEqual(xvenue.kalshi_quote(c, "KXNFLGAME", m, t), (0.50, 0.48, 300.0, t - H))
        c.candle_data[m["ticker"]] = [candle(t + H, 0.99, 0.98)]       # only a later candle: no price
        self.assertIsNone(xvenue.kalshi_quote(c, "KXNFLGAME", m, t))
        c.candle_data[m["ticker"]] = [candle(t - 13 * H, 0.50, 0.48)]   # too old to be "the price"
        self.assertIsNone(xvenue.kalshi_quote(c, "KXNFLGAME", m, t))

    def test_volume_is_the_day_before_the_decision(self):
        c = FakeKalshi()
        m = nfl_game(SAT)[0]
        t = SAT - 3 * H
        c.candle_data[m["ticker"]] = [candle(t - H, 0.5, 0.48, 120), candle(t - 10 * H, 0.5, 0.48, 70),
                                      candle(t - 25 * H, 0.5, 0.48, 5000), candle(t + H, 0.5, 0.48, 5000)]
        self.assertEqual(xvenue.kalshi_quote(c, "KXNFLGAME", m, t)[2], 190.0)

    def test_polymarket_price_never_comes_from_later(self):
        t = SAT - 3 * H
        hist = [{"t": t - 600, "p": 0.41}, {"t": t - 30, "p": 0.55}, {"t": t + 60, "p": 0.99}, {"t": t, "p": 0.98}]
        # the point 30 s before t covers a minute not over yet; later points never count
        self.assertEqual(xvenue.poly_price(hist, t), (0.41, 600))
        self.assertIsNone(xvenue.poly_price([{"t": t + 1, "p": 0.5}], t))
        self.assertIsNone(xvenue.poly_price([{"t": t - 13 * H, "p": 0.5}], t))   # too old
        self.assertIsNone(xvenue.poly_price([{"t": t - 600, "p": True}, {"t": "x", "p": 0.5}], t))

    def test_rows_use_prices_at_the_decision_time_only(self):
        c, pm = AllCandles(), FakePolymarket()
        markets = nfl_game(SAT)
        t = SAT - 3 * H                       # 6 hours before the scheduled end
        for m, ask in zip(markets, (0.40, 0.62)):
            c.candle_data[m["ticker"]] = [candle(t - H, ask, ask - 0.02), candle(t + H, 0.99, 0.98)]
        # Kalshi's candle ended an hour before t: Polymarket is read then too, so its later
        # points (even ones before t) never count.
        pm.history = {"tNE": [{"t": t - H - 120, "p": 0.43}, {"t": t - 600, "p": 0.30}, {"t": t + 120, "p": 0.01}],
                      "tCHI": [{"t": t - H - 120, "p": 0.57}, {"t": t - 600, "p": 0.70}, {"t": t + 120, "p": 0.99}]}
        p = [two_way("nfl", "nfl-ne-chi-2026-09-26", ["Patriots", "Bears"], ["tNE", "tCHI"], SAT)]
        pairs, _ = xvenue.match(kgames(markets), pgames(p, pm.teams["nfl"]), "nfl")
        rows, why = xvenue.price_rows(c, xvenue.Polymarket(pm), pairs)
        got = {r["side"]: (r["yes_ask"], r["yes_bid"], r["poly"], r["decided"], r["priced_at"], r["yes_value"])
               for r in rows}
        self.assertEqual(got, {"NE": (0.40, 0.38, 0.43, t, t - H, 0.0), "CHI": (0.62, 0.60, 0.57, t, t - H, 1.0)})
        asked = [p for name, p in pm.calls if name == "prices-history"]
        self.assertEqual(len(asked), 2)
        self.assertTrue(all(p["endTs"] <= t for p in asked))

    def test_no_row_when_the_decision_would_be_after_polymarkets_start(self):
        c, pm = FakeKalshi(), FakePolymarket()
        markets = kalshi_game("KXNCAAFGAME", "KXNCAAFGAME-26SEP26AUBTENN", [("AUB", "Auburn"), ("TENN", "Tennessee")],
                              SAT + 5 * H, "TENN")    # Kalshi says 5 hours later than the real kickoff
        t = SAT + 2 * H
        for m in markets:
            c.candle_data[m["ticker"]] = [candle(t - H, 0.5, 0.48)]
        pm.history = {"au": [{"t": t - 600, "p": 0.5}], "te": [{"t": t - 600, "p": 0.5}]}
        p = [two_way("cfb", "cfb-aubrn-tenn-2026-09-26", ["Auburn", "Tennessee"], ["au", "te"], SAT)]
        pairs, _ = xvenue.match(kgames(markets, "CFB", "KXNCAAFGAME"), pgames(p, pm.teams["cfb"]), "cfb")
        self.assertEqual(len(pairs), 1)
        rows, why = xvenue.price_rows(c, xvenue.Polymarket(pm), pairs)
        self.assertEqual(rows, [])
        self.assertEqual(why["decision too close to Polymarket's start"], 2)


# ---------- 5. trades, fees and the statistics ----------

class TradeTests(TempHome):
    def test_fee_is_a_full_size_order_rounded_up_to_the_cent(self):
        t = xvenue.trades([row(ask=0.50, bid=0.48, poly=0.60, won=True)], 0.02)
        (event, cost, pnl, _), = t["yes"]
        n = favorites.full_size(0.50)
        self.assertEqual(n, 19)
        fee = math.ceil(round(0.07 * n * 0.50 * 0.50 * 100, 6)) / 100     # 33.25 cents -> 34
        self.assertEqual(fee, 0.34)
        self.assertAlmostEqual(cost, n * 0.50 + fee)
        self.assertAlmostEqual(pnl, n - cost)
        self.assertEqual(event, "KXNFLGAME:2026-09-26")
        (_, cost, pnl, _), = xvenue.trades([row(ask=0.50, bid=0.48, poly=0.40, won=True)], 0.02)["no"]
        n = favorites.full_size(0.52)                                     # NO at 1 - bid
        self.assertAlmostEqual(cost, n * 0.52 + math.ceil(round(0.07 * n * 0.52 * 0.48 * 100, 6)) / 100)
        self.assertAlmostEqual(pnl, -cost)

    def test_margin_must_be_beaten(self):
        self.assertEqual(xvenue.trades([row(ask=0.50, poly=0.52)], 0.02), {"yes": [], "no": []})
        self.assertEqual(len(xvenue.trades([row(ask=0.50, poly=0.5201)], 0.02)["yes"]), 1)
        self.assertEqual(xvenue.trades([row(bid=0.48, poly=0.46)], 0.02), {"yes": [], "no": []})
        self.assertEqual(len(xvenue.trades([row(bid=0.48, poly=0.4599)], 0.02)["no"]), 1)

    def test_only_busy_markets_trade(self):
        thin = row(ask=0.50, poly=0.70, volume=xvenue.BUSY - 1)
        self.assertEqual(xvenue.trades([thin], 0.02)["yes"], [])
        self.assertEqual(xvenue.brier([thin])["markets"], 1)          # still scored for calibration

    def test_one_game_day_per_league_is_one_event(self):
        rows = [row(event=f"KXNFLGAME-G{i}", poly=0.6) for i in range(3)] + [row(event="KXNFLGAME-X", poly=0.6, day=1)]
        self.assertEqual(len({x[0] for x in xvenue.trades(rows, 0.02)["yes"]}), 2)

    def test_brier_scores(self):
        rows = [row(ask=0.71, bid=0.69, poly=0.60, won=True), row(event="E2", ask=0.31, bid=0.29, poly=0.40, won=False)]
        b = xvenue.brier(rows)
        self.assertAlmostEqual(b["kalshi"], 0.09)
        self.assertAlmostEqual(b["poly"], 0.16)
        self.assertAlmostEqual(b["diff"], -0.07)

    def planted(self, informed):
        """200 game-days with two busy games each, every team at 50c on Kalshi. Polymarket
        says 70c on one game and 30c on the other. If informed, those teams win 70% and 30%
        of the time; if not, every team wins half the time."""
        rows = []
        for day in range(200):
            for poly, rate in ((0.70, 7), (0.30, 3)):
                won = day % 10 < rate if informed else day % 2 == 0
                rows.append(row(event=f"KXNFLGAME-{day}-{poly}", ask=0.50, bid=0.48, poly=poly, won=won, day=day))
        return rows

    def test_a_planted_edge_is_found(self):
        out = xvenue.results(self.planted(True))
        pick = {(r["group"], r["margin"], r["side"]): r for r in out["trades"]}
        for side in ("yes", "no"):
            self.assertTrue(pick[("NFL", 0.02, side)]["edge"], pick[("NFL", 0.02, side)])
            self.assertGreater(pick[("NFL", 0.02, side)]["roi_lo"], 0)
        self.assertGreater(out["brier"]["ALL"]["diff"], 0)     # Kalshi - Polymarket > 0: Polymarket closer
        self.assertGreater(out["brier"]["ALL"]["lo"], 0)

    def test_a_fair_market_shows_none(self):
        out = xvenue.results(self.planted(False))
        self.assertFalse(any(r["edge"] for r in out["trades"]))
        self.assertTrue(all(r["roi"] < 0 for r in out["trades"] if r["trades"]))   # the fee
        self.assertLess(out["brier"]["ALL"]["diff"], 0)                             # Kalshi closer

    def test_bonferroni_counts_every_margin_side_and_group(self):
        rows = self.planted(True) + [dict(r, league="EPL", series="KXEPLGAME") for r in self.planted(True)[:20]]
        out = xvenue.results(rows)
        self.assertEqual(out["groups"], ["NFL", "Soccer", "ALL"])
        self.assertEqual(out["tries"], 3 * 3 * 2)
        self.assertAlmostEqual(out["z"], backtest.z_for(18))

    def test_agreement_is_only_a_check(self):
        rows = [row(final=1.0), row(won=False, final=1.0), row(final=None), row(final=0.5)]
        self.assertEqual(xvenue.agreement(rows), (1, 1))


# ---------- 6. the runner, end to end ----------

class RunnerTests(TempHome):
    def world(self):
        c, pm = FakeKalshi(), FakePolymarket()
        s1, s2, s3 = SAT, SAT + 7 * DAY, SAT + 8 * DAY
        nfl = nfl_game(s1) + kalshi_game("KXNFLGAME", "KXNFLGAME-26OCT04JACPHI",
                                         [("JAC", "Jacksonville"), ("PHI", "Philadelphia")], s2, "JAC")
        epl = kalshi_game("KXEPLGAME", "KXEPLGAME-26OCT04ARSCFC", [("ARS", "Arsenal"), ("CFC", "Chelsea"),
                                                                    ("TIE", "Tie")], s3, "ARS")
        c.settled = {"KXNFLGAME": nfl, "KXEPLGAME": epl}
        prices = {"KXNFLGAME-26SEP27NECHI-NE": 0.40, "KXNFLGAME-26SEP27NECHI-CHI": 0.62,
                  "KXNFLGAME-26OCT04JACPHI-JAC": 0.30, "KXNFLGAME-26OCT04JACPHI-PHI": 0.72,
                  "KXEPLGAME-26OCT04ARSCFC-ARS": 0.45, "KXEPLGAME-26OCT04ARSCFC-CFC": 0.30,
                  "KXEPLGAME-26OCT04ARSCFC-TIE": 0.27}
        for m in nfl + epl:
            t = xvenue.scheduled_end(m) - 6 * H
            ask = prices[m["ticker"]]
            c.candle_data[m["ticker"]] = [candle(t - H, ask, ask - 0.02, 800), candle(t + H, 0.99, 0.98, 800)]
        pm.markets = {"450": [two_way("nfl", "nfl-ne-chi-2026-09-26", ["Patriots", "Bears"], ["tNE", "tCHI"], s1,
                                      listed=s1 - 100 * DAY),
                              two_way("nfl", "nfl-jax-phi-2026-10-03", ["Eagles", "Jaguars"], ["tPHI", "tJAX"], s2,
                                      finals=("0", "1"), closed=False)],
                      "306": three_way("epl-ars-che-2026-10-04", ("ars", "Arsenal FC"), ("che", "Chelsea FC"),
                                       ("tA", "tD", "tC"), s3, "ars")}
        poly = {"tNE": 0.45, "tCHI": 0.55, "tPHI": 0.80, "tJAX": 0.20, "tA": 0.50, "tD": 0.26, "tC": 0.24}
        for tok, p in poly.items():
            start = s1 if tok in ("tNE", "tCHI") else s2 if tok in ("tPHI", "tJAX") else s3
            t = start - 3 * H
            pm.history[tok] = [{"t": t - H - 300, "p": p}, {"t": t - 300, "p": 0.999}, {"t": t + 300, "p": 0.999}]
        return c, pm

    def test_end_to_end(self):
        c, pm = self.world()
        buf = io.StringIO()
        with redirect_stdout(buf):
            out = xvenue_research.main(["--days", "30", "--leagues", "NFL", "EPL"], client=c,
                                       poly=xvenue.Polymarket(pm), now=NOW)
        text = buf.getvalue()
        self.assertEqual(out["leagues"]["NFL"]["matched"], 2)
        self.assertEqual(out["leagues"]["EPL"]["matched"], 1)
        self.assertEqual(out["markets"], 7)
        self.assertEqual((out["agree"], out["differ"]), (7, 0))
        self.assertEqual(out["groups"], ["NFL", "Soccer", "ALL"])
        # JAC at a 30c ask vs Polymarket's 20c: no YES; PHI's bid 70c vs 80c: no NO. NE's ask 40c
        # vs 45c: YES at 2c and 4c. CHI's bid 60c vs 55c: NO at 2c and 4c. ARS 45c vs 50c: YES.
        # Polymarket above Kalshi's ask: NE (45c vs 40c), PHI (80c vs 72c), ARS (50c vs 45c).
        # Below the bid: CHI (55c vs 60c), JAC (20c vs 28c), CFC (24c vs 28c). TIE sits inside.
        t = {(r["group"], r["margin"], r["side"]): r["trades"] for r in out["trades"]}
        self.assertEqual((t[("NFL", 0.02, "yes")], t[("NFL", 0.04, "yes")], t[("NFL", 0.06, "yes")]), (2, 2, 1))
        self.assertEqual((t[("NFL", 0.02, "no")], t[("NFL", 0.04, "no")], t[("NFL", 0.06, "no")]), (2, 2, 1))
        self.assertEqual((t[("Soccer", 0.04, "yes")], t[("Soccer", 0.02, "no")], t[("Soccer", 0.04, "no")]), (1, 1, 0))
        self.assertEqual(t[("ALL", 0.02, "yes")], 3)
        self.assertIn("Examples", text)
        self.assertIn("KXNFLGAME-26OCT04JACPHI (Jacksonville, Philadelphia)  =  nfl-jax-phi-2026-10-03 (Jaguars, Eagles)",
                      text)
        with open(os.path.join(self.home, "research", "xvenue.json")) as f:
            self.assertEqual(json.load(f)["markets"], 7)

    def test_a_failing_league_is_skipped_not_fatal(self):
        c, pm = self.world()

        def broken(series, limit=None):
            if series == "KXEPLGAME":
                raise RuntimeError("boom")
            return FakeKalshi.settled_markets(c, series, limit)
        c.settled_markets = broken
        with redirect_stdout(io.StringIO()) as buf:
            out = xvenue_research.main(["--days", "30", "--leagues", "NFL", "EPL"], client=c,
                                       poly=xvenue.Polymarket(pm), now=NOW)
        self.assertNotIn("EPL", out["leagues"])
        self.assertIn("EPL: skipped: boom", buf.getvalue())
        self.assertEqual(out["markets"], 4)
