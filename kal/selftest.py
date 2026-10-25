"""Checks that Kalshi's real API still looks the way kal expects.

Kalshi renames fields now and then (cents became `*_dollars` strings in 2026). A
renamed field doesn't crash anything: prices just read as 0 and the bot quietly
finds nothing, or worse, misreads a price. These checks catch that early.

run() never raises. Every problem becomes a row with ok=False.

The order probe (only with a key, only when asked) sends two real orders, both
immediate-or-cancel, in one busy market: buy 1 YES at $0.01 where the YES ask is $0.10
or more, and buy 1 NO at $0.01 where the YES bid is $0.90 or less. Nobody trades that
far off, so neither can fill. If Kalshi accepts both and fills 0, our order format
works, at zero risk.

The clock check compares this machine's clock with Kalshi's: Kalshi refuses signed
requests with a wrong timestamp, so a bad clock would make orders fail.
"""
import logging
import time

from . import config
from .client import Kalshi, KalshiError, candle_close, num, price_strict, ts

BOOK_TRIES = 5            # markets to try before saying no order book parses
CANDLE_TRIES = 8          # busy markets to try before saying no candles parse
CANDLE_DAYS = 3
PRICES = ("yes_bid", "yes_ask", "no_bid")   # the engine and ledger read these
OPEN = ("active", "open")

log = logging.getLogger(__name__)


def _row(check, ok, detail):
    return {"check": check, "ok": bool(ok), "detail": str(detail)[:300]}


def _first_page(client, endpoint, key, params, fallback):
    """One page from the real API, so the self-test stays fast. A fake has no get()."""
    if hasattr(client, "paginate") and callable(getattr(client, "get", None)):
        page = client.get(endpoint, params)
        return page.get(key, []) if isinstance(page, dict) else []
    return fallback()


def _has(m, field):
    return f"{field}_dollars" in m or field in m


def _volume(m):
    try:
        return num(m, "volume_24h") or num(m, "volume")
    except (TypeError, ValueError):
        return 0


def _by_volume(markets):
    """Open markets, busiest first."""
    live = [m for m in markets if isinstance(m, dict) and m.get("ticker") and m.get("status", "active") in OPEN]
    return sorted(live, key=_volume, reverse=True)


class _Checks:
    def __init__(self, client):
        self.c = client
        self.markets = []
        self.events = []
        self.books = {}       # ticker -> parsed book, from the order book check

    # ---------- public market data ----------

    def markets_check(self):
        ms = self.c.markets(limit=200, status="open", mve_filter="exclude")
        if not isinstance(ms, list) or not ms:
            return False, "no open markets came back"
        self.markets = [m for m in ms if isinstance(m, dict)]
        priced, bad, crossed = 0, 0, 0
        for m in self.markets:
            if not all(_has(m, f) for f in PRICES):
                continue
            try:
                p = {f: price_strict(m, f) for f in PRICES}
            except (TypeError, ValueError):
                bad += 1
                continue
            if not all(0 <= v <= 1 for v in p.values()):
                bad += 1
                continue
            priced += 1
            if p["yes_bid"] > 0 and p["yes_ask"] > 0 and p["yes_bid"] > p["yes_ask"] + 1e-9:
                crossed += 1
        detail = f"{len(self.markets)} open markets, {priced} with {'/'.join(PRICES)}"
        if bad:
            detail += f", {bad} with prices we can't read (or over $1)"
        if crossed:
            detail += f", {crossed} where the bid is above the ask"
        return priced > 0 and not bad and crossed <= max(1, priced // 20), detail

    def market_fields_check(self):
        if not self.markets:
            return False, "no markets to look at"
        times = sum(1 for m in self.markets if _parses(lambda: ts(m["close_time"])))
        vols = sum(1 for m in self.markets if _has_count(m, "volume_24h") and _has_count(m, "volume"))
        tickers = sum(1 for m in self.markets if m.get("event_ticker"))
        detail = f"close_time parses on {times}, volume fields on {vols}, event_ticker on {tickers}"
        return times > 0 and vols > 0 and tickers > 0, detail

    def events_check(self):
        params = {"limit": 100, "status": "open", "with_nested_markets": "true"}
        evs = _first_page(self.c, "/events", "events", params,
                          lambda: self.c.events(status="open", with_nested_markets="true"))
        self.events = [e for e in evs or [] if isinstance(e, dict)]
        if not self.events:
            return False, "no open events came back"
        missing = sum(1 for e in self.events if not isinstance(e.get("mutually_exclusive"), bool))
        exclusive = sum(1 for e in self.events if e.get("mutually_exclusive") is True)
        nested = sum(1 for e in self.events if isinstance(e.get("markets"), list) and e["markets"])
        series = sum(1 for e in self.events if e.get("series_ticker"))
        detail = (f"{len(self.events)} events, {exclusive} mutually exclusive, "
                  f"{nested} with markets, {series} with series_ticker")
        if missing:
            detail += f"; {missing} have no mutually_exclusive field"
        return not missing and nested > 0 and all(e.get("event_ticker") for e in self.events), detail

    def orderbook_check(self):
        tried = []
        for m in self._candidates()[:BOOK_TRIES]:
            t = m["ticker"]
            book = self.c.orderbook(t)
            tried.append(t)
            problem = _book_problem(book)
            if problem:
                return False, f"{t}: {problem}"
            if not (book["yes"] or book["no"]):
                continue
            self.books[t] = book
            yes, no = book["yes"], book["no"]
            detail = f"{t}: {len(yes)} YES bids, {len(no)} NO bids"
            if no:
                detail += f", YES ask ${1 - max(no)[0]:.2f}"
            return True, detail
        if not tried:
            return False, "no market to test"
        return False, f"no bids on {', '.join(tried)}: the book format may have changed"

    def candles_check(self):
        """A wrong guess at a series (404) or a quiet market just moves on to the next
        market. A candle in the wrong shape fails at once: that's a format change."""
        now = int(time.time())
        tried, zeros, errors = 0, 0, []
        for series, t in self._with_series():
            tried += 1
            try:
                candles = self.c.candles(series, t, now - CANDLE_DAYS * 86400, now, period=60)
            except KalshiError as e:
                errors.append(f"{t}: {e}"[:120])
                continue
            if not isinstance(candles, list):
                return False, f"{t}: candlesticks is not a list"
            if not candles:
                continue
            closes = []
            for c in candles:
                if not isinstance(c, dict) or "end_period_ts" not in c:
                    return False, f"{t}: a candle has no end_period_ts"
                int(c["end_period_ts"])
                for f in ("yes_bid", "yes_ask"):
                    v = candle_close(c, f)
                    if not 0 <= v <= 1:
                        return False, f"{t}: candle {f} close is ${v}, over $1"
                    closes.append(v)
            if any(v > 0 for v in closes):
                last = candle_close(candles[-1], "yes_bid")
                return True, f"{t}: {len(candles)} candles, last YES bid close ${last:.2f}"
            zeros += 1
        if not tried:
            return False, "no market with a series to test"
        if zeros:
            return False, f"candles on {zeros} markets, but every close reads $0: the format may have changed"
        return False, f"no candles on {tried} busy markets" + (f" ({errors[0]})" if errors else "")

    def series_check(self):
        ss = self.c.series()
        if not isinstance(ss, list) or not ss:
            return False, "no series came back"
        good = sum(1 for s in ss if isinstance(s, dict) and s.get("ticker"))
        return good > 0, f"{len(ss)} series, {good} with a ticker"

    def clock_check(self):
        """Our clock against Kalshi's (one public request). Never trips: it's a test."""
        if not isinstance(getattr(self.c, "base", None), str):
            return True, "skipped: not talking to the real API"
        from . import maintenance
        r = maintenance.clock_ok(self.c, trip=False)
        return r["ok"], r["detail"]

    # ---------- account (needs a key) ----------

    def balance_check(self):
        b = self.c.balance()
        if not isinstance(b, (int, float)) or b < 0:
            return False, f"balance reads as {b!r}"
        return True, f"${b:.2f}"

    def positions_check(self):
        ps = self.c.positions()
        if not isinstance(ps, list):
            return False, "positions is not a list"
        bad = [p for p in ps if not (isinstance(p, dict) and p.get("ticker")
                                     and isinstance(p.get("position"), (int, float)))]
        if bad:
            return False, f"{len(bad)} positions have no ticker or position"
        return True, f"{len(ps)} open positions"

    def order_probe(self):
        """Two IOC buys at $0.01, 1 YES then 1 NO, where the price is far off. Never more.
        The same test the engine runs before real money (kal/gate.py), so a pass here also
        unlocks real trading for this key and exchange, and a fill or a resting order stops it."""
        if not self.c.can_trade:
            return False, "skipped: needs an API key"
        from . import gate
        return gate.probe(self.c, config.load().get("env", "prod"))

    # ---------- helpers ----------

    def _candidates(self):
        """Busy open markets: from the market list, else from the events' nested markets."""
        found = _by_volume(self.markets)
        if not found:
            found = _by_volume([m for e in self.events for m in e.get("markets") or []])
        return found

    def _with_series(self):
        """(series, ticker) for busy markets. Events know their series; a market alone doesn't,
        so for the busy market list we use the ticker prefix (KXHIGHNY-25OCT24 is in KXHIGHNY)."""
        pairs = []
        series_of = {e.get("event_ticker"): e.get("series_ticker") for e in self.events}
        for e in self.events:
            for m in _by_volume(e.get("markets") or []):
                if e.get("series_ticker"):
                    pairs.append((_volume(m), e["series_ticker"], m["ticker"]))
        pairs.sort(reverse=True)
        out = [(s, t) for _, s, t in pairs[:CANDLE_TRIES // 2]]
        for m in self._candidates():
            ev = m.get("event_ticker") or ""
            series = series_of.get(ev) or ev.split("-")[0]
            if series and all(t != m["ticker"] for _, t in out):
                out.append((series, m["ticker"]))
        return out[:CANDLE_TRIES]


def _parses(fn):
    try:
        fn()
        return True
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _has_count(m, field):
    return (f"{field}_fp" in m or field in m) and _parses(lambda: num(m, field))


def _book_problem(book):
    """Why a parsed order book looks wrong, or None."""
    if not isinstance(book, dict) or not isinstance(book.get("yes"), list) or not isinstance(book.get("no"), list):
        return "order book has no yes/no lists"
    for side in ("yes", "no"):
        for lv in book[side]:
            if len(lv) != 2 or not 0 < lv[0] < 1 or not lv[1] > 0:
                return f"bad {side} level {lv!r}"
    if book["yes"] and book["no"] and max(book["yes"])[0] + max(book["no"])[0] > 1 + 1e-9:
        return "YES bid + NO bid is over $1: prices are being misread"
    return None


def _client():
    """The configured client, or a public one if the key can't be loaded."""
    try:
        cfg = config.load()
        return Kalshi.from_env(config.home(), cfg["env"]), None
    except Exception as e:
        try:
            env = config.load().get("env", "prod")
        except Exception:
            env = "prod"
        return Kalshi(env=env if env in ("prod", "demo") else "prod"), f"couldn't load the API key: {e}"


def run(client=None, order_probe=False):
    """[{check, ok, detail}]. Never raises."""
    rows = []
    try:
        if client is None:
            client, problem = _client()
            if problem:
                rows.append(_row("api key", False, problem))
    except Exception as e:
        return [_row("client", False, f"couldn't make a client: {e}")]
    checks = _Checks(client)

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            log.debug("self-test %s failed", name, exc_info=True)
            ok, detail = False, f"error: {type(e).__name__}: {e}"
        rows.append(_row(name, ok, detail))

    check("markets", checks.markets_check)
    check("market fields", checks.market_fields_check)
    check("events", checks.events_check)
    check("order book", checks.orderbook_check)
    check("candlesticks", checks.candles_check)
    check("series", checks.series_check)
    check("clock", checks.clock_check)
    try:
        can_trade = bool(client.can_trade)
    except Exception:
        can_trade = False
    if can_trade:
        check("balance", checks.balance_check)
        check("positions", checks.positions_check)
    else:
        rows.append(_row("account", True, "skipped: no API key, so only public checks ran"))
    if order_probe:
        check("order probe", checks.order_probe)
    return rows
