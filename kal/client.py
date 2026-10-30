"""Kalshi API client and field helpers.

Kalshi moved to fixed-point fields in 2026 (`*_dollars` price strings,
`*_fp` count strings). Every reader here accepts both those and the legacy
integer fields, so a schema change can't silently zero out a price.
"""
import base64
import logging
import math
import os
import threading
import time
import uuid
from datetime import datetime

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

API = "/trade-api/v2"
BASES = {"prod": "https://api.elections.kalshi.com", "demo": "https://demo-api.kalshi.co"}
FEE_RATE = 0.07  # taker fee: ceil(0.07 * C * P * (1-P)) to the cent

log = logging.getLogger(__name__)


class KalshiError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status

    @property
    def rejected(self):
        """True when Kalshi refused the request outright (4xx), so no order exists."""
        return self.status is not None and 400 <= self.status < 500


def is_transient(e):
    """True for failures that are Kalshi's or the network's, not ours: timeouts, dropped
    connections, 5xx, rate limits. They can't lose money (an order that fails to send
    either never happened or gets reconciled), so they shouldn't trip the bot the way a
    bug would. Kalshi's weekly maintenance looks exactly like this."""
    if isinstance(e, requests.RequestException):
        return True
    if isinstance(e, KalshiError) and e.status is not None:
        return e.status >= 500 or e.status == 429
    return False


class _Pacer:
    """One request pace shared by every client in the process (trade + research threads),
    so together they stay under Kalshi's ~20 requests/second basic tier."""

    def __init__(self, gap):
        self.gap, self.next, self.lock = gap, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            delay = self.next - now
            self.next = max(now, self.next) + self.gap
        if delay > 0:
            time.sleep(delay)


# Kalshi's basic tier: 20 reads and 10 writes a second. Stay a little under both.
PACER = _Pacer(0.06)
WRITE_PACER = _Pacer(0.11)


# ---------- auth ----------

def load_key(home=None):
    """(key_id, private_key) from env vars, or the files `kal setup` writes in $KAL_HOME
    (kalshi_key_id, kalshi.pem), or (None, None). Env vars win (CI, Docker and the server's
    kal.env set them); `kal setup` stops rather than save a key ID that one would override."""
    key_id = os.environ.get("KALSHI_API_KEY_ID")
    id_path = home and os.path.join(home, "kalshi_key_id")
    if not key_id and id_path and os.path.exists(id_path):
        with open(id_path) as f:
            key_id = f.read().strip()
    pem = os.environ.get("KALSHI_PRIVATE_KEY")
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH") or (home and os.path.join(home, "kalshi.pem"))
    if not pem and path and os.path.exists(path):
        with open(path, "rb") as f:
            pem = f.read().decode()
    if not key_id or not pem:
        return None, None
    pem = pem.replace("\\n", "\n").strip()
    return key_id, parse_key(pem.encode())


def parse_key(pem):
    """A Kalshi private key from PEM bytes. Kalshi makes Ed25519 keys by default (2026) and
    RSA ones if asked; the PEM header doesn't say which. Raises ValueError for anything else."""
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, (ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)):
        raise ValueError(f"Kalshi keys are Ed25519 or RSA, not {type(key).__name__}")
    return key


def sign(private_key, timestamp_ms, method, path):
    """Kalshi's request signature: timestamp + method + path (no query), base64. Ed25519
    keys sign the text itself; RSA keys use RSA-PSS with SHA-256 (docs.kalshi.com, API Keys)."""
    msg = f"{timestamp_ms}{method}{path}".encode()
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        sig = private_key.sign(msg)
    else:
        sig = private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return base64.b64encode(sig).decode()


class Kalshi:
    def __init__(self, key_id=None, private_key=None, env="prod"):
        self.key_id, self.private_key = key_id, private_key
        self.base = os.environ.get("KALSHI_API_BASE") or BASES[env]
        self.s = requests.Session()

    @classmethod
    def from_env(cls, home=None, env="prod"):
        return cls(*load_key(home), env=env)

    @property
    def can_trade(self):
        return self.private_key is not None

    def _req(self, method, endpoint, params=None, body=None):
        path = API + endpoint
        r = None
        for attempt in range(5):
            headers = {}
            if self.private_key:
                ts_ms = str(int(time.time() * 1000))
                headers = {
                    "KALSHI-ACCESS-KEY": self.key_id,
                    "KALSHI-ACCESS-TIMESTAMP": ts_ms,
                    "KALSHI-ACCESS-SIGNATURE": sign(self.private_key, ts_ms, method, path),
                }
            PACER.wait()
            if method != "GET":
                WRITE_PACER.wait()
            try:
                r = self.s.request(method, self.base + path, params=params, json=body, headers=headers, timeout=30)
            except requests.RequestException:
                if method != "GET" or attempt == 4:
                    raise  # never blindly resend an order: it may have gone through
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or (method == "GET" and r.status_code >= 500):
                time.sleep(2 ** attempt)
                continue
            return r
        return r

    def get(self, endpoint, params=None):
        r = self._req("GET", endpoint, params=params)
        if r.status_code >= 400:
            raise KalshiError(f"GET {endpoint} -> {r.status_code}: {r.text[:300]}", r.status_code)
        return r.json()

    def paginate(self, endpoint, key, params=None, limit=None, strict=False):
        """Every page of a list. strict: a reply without `key` raises instead of reading as
        an empty list. For account data (positions, orders, fills) a missing key means the
        reply changed shape (Kalshi renamed fields in 2026), and reading that as "nothing"
        would blind the checks that catch lost fills. A key that's there but null is empty."""
        out = []
        for page in self.pages(endpoint, key, params, strict):
            out += page
            if limit and len(out) >= limit:
                return out[:limit]
        return out

    def pages(self, endpoint, key, params=None, strict=False):
        """Like paginate(), one page at a time, so a caller can keep only what it needs:
        all open events with their markets is 350 MB of JSON, over 700 MB in Python."""
        params = dict(params or {})
        while True:
            d = self.get(endpoint, params)
            if strict and (not isinstance(d, dict) or key not in d):
                keys = ", ".join(sorted(d)) if isinstance(d, dict) else type(d).__name__
                raise KalshiError(f"{endpoint}: reply has no '{key}' (got: {keys[:200]})")
            yield d.get(key) or []
            if not d.get("cursor"):
                return
            params["cursor"] = d["cursor"]

    # ---------- market data (public) ----------

    def exchange_status(self):
        """{"trading_active": bool, "exchange_active": bool, ...}. Kalshi closes for
        scheduled maintenance; trading then would only fail."""
        return self.get("/exchange/status")

    def markets(self, limit=None, **params):
        return self.paginate("/markets", "markets", {"limit": 1000, **params}, limit)

    def series_info(self, ticker):
        """One series, with its fee_type and fee_multiplier. 404 (KalshiError) if no such series."""
        return self.get(f"/series/{ticker}").get("series") or {}

    def event_fee_changes(self):
        """Event-level fee overrides Kalshi has scheduled: event_ticker, fee_type_override,
        fee_multiplier_override, scheduled_ts."""
        return self.paginate("/events/fee_changes", "event_fee_changes", {})

    def trades(self, ticker, min_ts=None, limit=500):
        """Public trades in one market, newest first. Each says which side its taker bought
        (taker_outcome_side; the deprecated taker_side before it), how many (count_fp) and
        when (created_time)."""
        params = {"ticker": ticker, "limit": min(limit, 1000)}
        if min_ts:
            params["min_ts"] = int(min_ts)
        return self.paginate("/markets/trades", "trades", params, limit, strict=True)   # renamed: an error, not "no trades"

    def markets_by_ticker(self, tickers):
        """{ticker: market}. Batches with tickers=, then fetches any it missed one by one:
        the real API returned nothing for some tickers in a batch, and a position we can't
        see would never settle in the ledger. Last, Kalshi's historical store, where markets
        settled before its cutoff (about 2 months back in Oct 2026) can move."""
        out = {}
        tickers = list(dict.fromkeys(tickers))
        for i in range(0, len(tickers), 100):
            try:
                for m in self.markets(tickers=",".join(tickers[i:i + 100])):
                    out[m["ticker"]] = m
            except KalshiError:
                pass
        for t in tickers:
            for archive in (False, True):
                if t in out:
                    break
                try:
                    m = self.market(t, archive=True) if archive else self.market(t)
                except KalshiError:
                    continue
                if m:
                    out[t] = m
        return out

    def market(self, ticker, archive=False):
        return self.get(f"/historical/markets/{ticker}" if archive else f"/markets/{ticker}").get("market", {})

    def cutoff(self):
        """When Kalshi's historical store starts, per kind of record, as unix times: e.g.
        {"trades_created_ts": ..., "orders_updated_ts": ..., "market_settled_ts": ...}.
        Fills before trades_created_ts, and orders done before orders_updated_ts, are only in
        /historical (about 2 months and 15 days back in Oct 2026). A field it can't read is left
        out, so callers read the store too."""
        out = {}
        for k, v in self.get("/historical/cutoff").items():
            try:
                out[k] = ts(v)
            except (AttributeError, TypeError, ValueError):
                log.warning("historical cutoff: can't read %s=%r, so the store gets read every time", k, v)
        return out

    def events(self, limit=None, **params):
        return self.paginate("/events", "events", {"limit": 200, **params}, limit)

    def event_pages(self, **params):
        """events(), one page at a time (see pages())."""
        return self.pages("/events", "events", {"limit": 200, **params})

    def series(self, category=None):
        return self.paginate("/series", "series", {"category": category} if category else {})

    def settled_markets(self, series, limit=None):
        """Recent settled markets, plus older ones if Kalshi has moved them to /historical."""
        found = {m["ticker"]: m for m in self.markets(limit, series_ticker=series, status="settled")}
        try:
            for m in self.paginate("/historical/markets", "markets",
                                   {"limit": 1000, "series_ticker": series}, limit):
                found.setdefault(m["ticker"], m)
        except KalshiError:
            pass
        return list(found.values())

    def candles(self, series, ticker, start_ts, end_ts, period=60):
        params = {"start_ts": int(start_ts), "end_ts": int(end_ts), "period_interval": period}
        try:
            d = self.get(f"/series/{series}/markets/{ticker}/candlesticks", params)
        except KalshiError as e:
            if e.status != 404:
                raise
            # Older settled markets move to Kalshi's historical store.
            d = self.get(f"/historical/markets/{ticker}/candlesticks", params)
        return d.get("candlesticks", [])

    def orderbook(self, ticker):
        """{'yes': [(price, size)...], 'no': [...]} bids, best first, from either format."""
        d = self.get(f"/markets/{ticker}/orderbook")
        book = d.get("orderbook_fp") or d.get("orderbook") or {}
        out = {}
        for side in ("yes", "no"):
            if book.get(f"{side}_dollars"):
                levels = [(float(p), float(q)) for p, q in book[f"{side}_dollars"]]
            else:
                levels = [(p / 100, float(q)) for p, q in book.get(side) or []]
            out[side] = sorted(levels, reverse=True)
        return out

    def best_bid(self, ticker, side="yes"):
        """(price, size) of the best bid on one side; (0, 0) if none."""
        levels = self.orderbook(ticker)[side]
        return levels[0] if levels else (0.0, 0.0)

    # ---------- account (needs key) ----------

    def balance(self):
        d = self.get("/portfolio/balance")
        if "balance_dollars" in d:
            return float(d["balance_dollars"])
        return d["balance"] / 100

    def positions(self):
        """Open market positions: [{'ticker', 'position' (+yes/-no), 'exposure' dollars}]."""
        out = []
        for p in self.paginate("/portfolio/positions", "market_positions", {"limit": 1000}, strict=True):
            pos = num(p, "position")
            if pos:
                out.append({"ticker": p["ticker"], "position": pos,
                            "exposure": price(p, "market_exposure")})
        return out

    def settlements(self, min_ts=None):
        params = {"limit": 1000, **({"min_ts": int(min_ts)} if min_ts else {})}
        return self.paginate("/portfolio/settlements", "settlements", params, strict=True)

    def fills(self, min_ts=None, ticker=None, archive=False):
        """The account's fills. archive: from Kalshi's historical store instead, where fills
        older than its cutoff move. every_fill() reads both."""
        params = {"limit": 1000}
        if min_ts:
            params["min_ts"] = int(min_ts)
        if ticker:
            params["ticker"] = ticker
        return self.paginate("/historical/fills" if archive else "/portfolio/fills", "fills", params, strict=True)

    def orders(self, ticker=None, min_ts=None, archive=False):
        """The account's orders. archive: from Kalshi's historical store instead, where
        orders done (filled or cancelled) before its cutoff move. every_order() reads both."""
        params = {"limit": 1000}
        if ticker:
            params["ticker"] = ticker
        if min_ts:
            params["min_ts"] = int(min_ts)
        return self.paginate("/historical/orders" if archive else "/portfolio/orders", "orders", params, strict=True)

    def cancel(self, order_id):
        r = self._req("DELETE", f"/portfolio/events/orders/{order_id}")
        if r.status_code >= 400:
            raise KalshiError(f"cancel {order_id} -> {r.status_code}: {r.text[:300]}", r.status_code)
        return r.json() if r.text else {}

    def buy(self, ticker, side, count, limit_price, client_order_id=None):
        """Immediate-or-cancel limit buy through Kalshi's V2 order endpoint; returns the order.

        Kalshi retired POST /portfolio/orders (410 deprecated_v1_order_endpoint). V2 has one
        YES book with bid/ask sides: buying YES at p is a bid at p, buying NO at p is an ask
        at 1 - p. Count and price are fixed-point strings. Reusing a client_order_id returns
        the original order instead of placing a second one, so the id is our retry guard.
        Raises KalshiError: rejected=True means no order exists; otherwise it MAY exist."""
        if side not in ("yes", "no"):
            raise ValueError(f"side must be yes or no, not {side!r}")
        book_side, book_price = ("bid", limit_price) if side == "yes" else ("ask", 1 - limit_price)
        body = {"ticker": ticker, "client_order_id": client_order_id or str(uuid.uuid4()),
                "side": book_side, "count": f"{float(count):.2f}", "price": f"{book_price:.4f}",
                "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}
        r = self._req("POST", "/portfolio/events/orders", body=body)
        if r.status_code >= 300:
            raise KalshiError(f"Order failed ({r.status_code}): {r.text[:300]}", r.status_code)
        d = r.json() if r.text else {}
        return d.get("order", d) if isinstance(d, dict) else {}


# ---------- account history: live list plus Kalshi's historical store ----------

def _archived_since(client, field, since):
    """True if Kalshi may have moved records from `since` on (None: from the start) to its
    historical store. Asked after the live list is read: records only move from live to the
    store, and the cutoff only moves forward, so a cutoff at or before `since` now was one
    then too. Can't read it: True, so the store gets read as well."""
    if since is None:
        return True
    try:
        cut = client.cutoff().get(field)
    except Exception:
        return True
    return cut is None or cut > since


def _merged(live, stored, ids):
    """live + whatever of stored isn't already there. A record can show up in both while
    Kalshi moves it: any id the two copies share makes it one record. Counting a fill twice
    is never safe (the owner's buy-back counted twice would hide their sale)."""
    seen = {i for r in live for i in ids(r)}
    return live + [r for r in stored if not seen & set(ids(r))]


def _fill_ids(f):
    out = [("fill", f["fill_id"])] if f.get("fill_id") else []
    if f.get("trade_id"):
        out.append(("trade", f["trade_id"], f.get("order_id")))   # a self-trade is two fills, one trade
    return out or [("raw", repr(sorted(f.items())))]


def _order_ids(o):
    out = [("order", o["order_id"])] if o.get("order_id") else []
    if o.get("client_order_id"):
        out.append(("client", o["client_order_id"]))   # unique per account: Kalshi won't reuse one
    return out or [("raw", repr(sorted(o.items())))]


def every_fill(client, min_ts=None, ticker=None, earliest=None, store=None):
    """The account's fills from min_ts on (None: all of them), each once: the live list,
    plus Kalshi's historical store when some may have moved there (fills older than about 2
    months, Oct 2026). Reading only the live list would miss those. `earliest`: no fill
    asked for is older than this (say, the market's open time); it only decides whether
    the store is read, never filters. store=True reads the store whatever the cutoff says."""
    live = list(client.fills(min_ts=min_ts, ticker=ticker))
    if not store and not _archived_since(client, "trades_created_ts", earliest or min_ts):
        return live
    return _merged(live, list(client.fills(min_ts=min_ts, ticker=ticker, archive=True)), _fill_ids)


def every_order(client, ticker=None, min_ts=None, store=None):
    """The account's orders from min_ts on (None: all of them), each once: the live list,
    plus Kalshi's historical store when some may have moved there (orders done more than
    about 15 days ago, Oct 2026). Resting orders always stay in the live list. store=True
    reads the store whatever the cutoff says."""
    live = list(client.orders(ticker=ticker, min_ts=min_ts))
    if not store and not _archived_since(client, "orders_updated_ts", min_ts):
        return live
    return _merged(live, list(client.orders(ticker=ticker, min_ts=min_ts, archive=True)), _order_ids)


# ---------- field helpers ----------

BOOK_SIDES = {"bid": "yes", "ask": "no"}


def outcome_side(d, prefix=""):
    """Which outcome a fill or order (or, with prefix "taker_", a public trade's taker) is
    long: "yes", "no", or None if it doesn't say. Kalshi's outcome_side, else book_side
    (bid is yes, ask is no): the same bit twice. Kalshi made these the canonical fields and
    deprecated side/action (and taker_side), which it may drop since May 2026. Raises
    ValueError if the two disagree: never a guess."""
    side, book = d.get(f"{prefix}outcome_side"), d.get(f"{prefix}book_side")
    side = side if side in ("yes", "no") else None
    book = BOOK_SIDES.get(book) if isinstance(book, str) else None
    if side and book and side != book:
        raise ValueError(f"{prefix}outcome_side {side} but {prefix}book_side says {book}")
    return side or book


def num(d, field):
    """A count from `x_fp` (fixed-point string) or a legacy integer `x`."""
    v = d.get(f"{field}_fp", d.get(field, 0))
    return float(v or 0)


def to_dollars(v):
    """One price value in dollars, whatever shape Kalshi sent it in.

    Seen on the real API: "0.8000" (fixed-point dollar string, also used without a
    _dollars suffix in historical candles), 93 (legacy integer cents), None/"" (no
    price). JSON integers and digit-only strings are cents; anything with a decimal
    point is dollars. Unreadable values count as no price, never as a tradeable one."""
    if v is None or v == "" or isinstance(v, bool):
        return 0.0
    try:
        if isinstance(v, str):
            f = float(v)
            f = f if "." in v else f / 100
        elif isinstance(v, int):
            f = v / 100
        else:
            f = float(v)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def price(d, field):
    """A price in dollars from `x_dollars` (always dollars), else from `x` (see to_dollars)."""
    v = d.get(f"{field}_dollars")
    if v not in (None, ""):
        try:
            f = float(v)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return f if math.isfinite(f) else 0.0
    return to_dollars(d.get(field))


def price_strict(d, field):
    """Like price(), but raises ValueError on a value it can't read, so the self-test can
    catch a format change instead of quietly seeing "no price"."""
    raw = d.get(f"{field}_dollars")
    if raw in (None, ""):
        raw = d.get(field)
        if raw in (None, ""):
            return 0.0
        if isinstance(raw, bool):
            raise ValueError(f"{field}: {raw!r}")
        if isinstance(raw, str):
            float(raw)  # raises on junk
        return to_dollars(raw)
    if isinstance(raw, bool):
        raise ValueError(f"{field}: {raw!r}")
    return float(raw)


_BAD = object()   # a count that is there but can't be read


def _count(order, field):
    """One count: the first of `field`_fp and legacy `field` that isn't null or empty. A
    number; _BAD for a bool, text, NaN or infinity; None if neither is there."""
    for k in (f"{field}_fp", field):
        v = order.get(k)
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            return _BAD
        try:
            x = float(v)
        except (TypeError, ValueError, OverflowError):
            return _BAD
        return x if math.isfinite(x) else _BAD
    return None


def fill_count(order, requested=None):
    """How many contracts an order bought, or None if its reply doesn't say for sure: then it
    is looked up or the bot stops, never guessed. Kalshi's fill count when there is one.
    Without it, only an order "executed" in full, with nothing remaining: its initial count,
    else what was asked (`requested`). Initial less remaining says nothing otherwise: Kalshi
    zeroes "remaining" when it cancels the rest. Counts that contradict each other (a fill
    above the order, an "executed" order short of its size or with some remaining) are None.
    Rounded to Kalshi's 2 decimals: 10.00 - 1.13 is 8.870000000000001 in floats."""
    if not isinstance(order, dict):
        return None
    got, start, left = (_count(order, f) for f in ("fill_count", "initial_count", "remaining_count"))
    if _BAD in (got, start, left):
        return None
    done = order.get("status") == "executed"
    if done and left is not None and left > 0.005:
        return None
    if got is None:
        if not done:
            return None
        got = start if start is not None else requested
        if got is None:
            return None
    if got < 0 or (start is not None and (got > start + 0.005 or (done and got < start - 0.005))):
        return None
    return round(got, 2)


def filled(order, requested):
    """Contracts an order bought, as fill_count() reads it; 0 if it doesn't say (fill_known()
    is False then). Never more than was asked for."""
    n = fill_count(order, requested)
    return 0.0 if n is None else max(0.0, min(float(requested), n))


def fill_known(order):
    """True if an order reply says for sure how much it bought (fill_count()). A "canceled"
    alone could hide a part fill, and so could initial and remaining counts: Kalshi zeroes
    "remaining" when it cancels the rest. The order gets looked up instead."""
    return fill_count(order, 0) is not None


DONE = ("executed", "canceled", "cancelled")


def resting(order):
    """True if the order is still on the book (it should never be, for immediate-or-cancel).

    Kalshi's statuses are resting / canceled / executed. A V2 reply may carry only fill and
    remaining counts, so with no status, contracts still remaining means it's resting."""
    status = order.get("status")
    if status:
        return status not in DONE
    if "remaining_count_fp" in order or "remaining_count" in order:
        return num(order, "remaining_count") > 0
    return False


def candle_close(candle, field):
    """Closing yes_bid / yes_ask / price of a candle, in dollars."""
    return price(candle.get(field) or {}, "close")


BRIEF = ("ticker", "title", "category", "frequency")


def brief_series(rows):
    """What research and the dashboard read from Kalshi's list of every series (15,000+ rows,
    12 MB of JSON, some 50 MB in full): kept for a whole research run or an hour."""
    return [{k: s.get(k) for k in BRIEF} for s in rows or []
            if isinstance(s, dict) and isinstance(s.get("ticker"), str)]


def ts(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def fee(count, p):
    """Kalshi taker fee for one order, rounded up to the cent."""
    return math.ceil(round(FEE_RATE * count * p * (1 - p) * 100, 6)) / 100


def fee_per_contract(p):
    """Fee per contract for a large order, where cent rounding stops mattering."""
    return FEE_RATE * p * (1 - p)


FINAL = ("settled", "finalized")


class UnknownSettlement(RuntimeError):
    """A market is final but we can't tell what it paid. Never guess with real money."""


def yes_payout(m):
    """What one YES contract paid once a market is final: 1.0, 0.0, or in between (scalar or
    voided markets). One NO paid 1 - that. None if the result isn't final yet.

    Prefers Kalshi's settlement value (`settlement_value_dollars`, seen on real settled
    markets), checks it agrees with `result`, and raises UnknownSettlement on anything else,
    so a void or a new result type stops the bot instead of being booked wrong."""
    if m.get("status") not in FINAL:
        return None
    t, res = m.get("ticker", "?"), m.get("result")
    v = m.get("settlement_value_dollars")
    if v not in (None, ""):
        try:
            value = float(v)
        except (TypeError, ValueError):
            raise UnknownSettlement(f"{t}: settlement value {v!r} isn't a number")
    elif isinstance(m.get("settlement_value"), int) and not isinstance(m.get("settlement_value"), bool):
        value = m["settlement_value"] / 100  # legacy cents
    elif res in ("yes", "no"):
        value = 1.0 if res == "yes" else 0.0
    else:
        raise UnknownSettlement(f"{t}: final with result {res!r} and no settlement value")
    if not 0 <= value <= 1:
        raise UnknownSettlement(f"{t}: settlement value {value} is outside $0-$1")
    if (res == "yes" and value != 1.0) or (res == "no" and value != 0.0):
        raise UnknownSettlement(f"{t}: result {res!r} doesn't match settlement value {value}")
    return value


def settled_result(m):
    """'yes' / 'no' once a market's result is final, else None. "determined" isn't final:
    a result can still be disputed and amended then, and the ledger books it for good."""
    if m.get("status") in ("settled", "finalized") and m.get("result") in ("yes", "no"):
        return m["result"]
    return None
