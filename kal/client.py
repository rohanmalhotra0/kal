"""Kalshi API client and field helpers.

Kalshi moved to fixed-point fields in 2026 (`*_dollars` price strings,
`*_fp` count strings). Every reader here accepts both those and the legacy
integer fields, so a schema change can't silently zero out a price.
"""
import base64
import math
import os
import time
import uuid
from datetime import datetime

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

API = "/trade-api/v2"
BASES = {"prod": "https://api.elections.kalshi.com", "demo": "https://demo-api.kalshi.co"}
FEE_RATE = 0.07  # taker fee: ceil(0.07 * C * P * (1-P)) to the cent


class KalshiError(RuntimeError):
    pass


# ---------- auth ----------

def load_key(home=None):
    """(key_id, private_key) from env vars or $KAL_HOME/kalshi.pem, or (None, None)."""
    key_id = os.environ.get("KALSHI_API_KEY_ID")
    pem = os.environ.get("KALSHI_PRIVATE_KEY")
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH") or (home and os.path.join(home, "kalshi.pem"))
    if not pem and path and os.path.exists(path):
        with open(path, "rb") as f:
            pem = f.read().decode()
    if not key_id or not pem:
        return None, None
    pem = pem.replace("\\n", "\n").strip()
    return key_id, serialization.load_pem_private_key(pem.encode(), password=None)


def sign(private_key, timestamp_ms, method, path):
    msg = f"{timestamp_ms}{method}{path}".encode()
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
            time.sleep(0.07)  # stay under the basic tier's ~20 requests/second
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
            raise KalshiError(f"GET {endpoint} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def paginate(self, endpoint, key, params=None, limit=None):
        params, out = dict(params or {}), []
        while True:
            d = self.get(endpoint, params)
            out += d.get(key, [])
            if not d.get("cursor") or (limit and len(out) >= limit):
                return out[:limit] if limit else out
            params["cursor"] = d["cursor"]

    # ---------- market data (public) ----------

    def markets(self, limit=None, **params):
        return self.paginate("/markets", "markets", {"limit": 1000, **params}, limit)

    def markets_by_ticker(self, tickers):
        out = {}
        tickers = list(tickers)
        for i in range(0, len(tickers), 100):
            for m in self.markets(tickers=",".join(tickers[i:i + 100])):
                out[m["ticker"]] = m
        return out

    def market(self, ticker):
        return self.get(f"/markets/{ticker}").get("market", {})

    def events(self, **params):
        return self.paginate("/events", "events", {"limit": 200, **params})

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
        d = self.get(f"/series/{series}/markets/{ticker}/candlesticks",
                     {"start_ts": int(start_ts), "end_ts": int(end_ts), "period_interval": period})
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
        for p in self.paginate("/portfolio/positions", "market_positions", {"limit": 1000}):
            pos = num(p, "position")
            if pos:
                out.append({"ticker": p["ticker"], "position": pos,
                            "exposure": price(p, "market_exposure")})
        return out

    def settlements(self, min_ts=None):
        params = {"limit": 1000, **({"min_ts": int(min_ts)} if min_ts else {})}
        return self.paginate("/portfolio/settlements", "settlements", params)

    def fills(self, min_ts=None):
        params = {"limit": 1000, **({"min_ts": int(min_ts)} if min_ts else {})}
        return self.paginate("/portfolio/fills", "fills", params)

    def buy(self, ticker, side, count, limit_price, client_order_id=None):
        """Immediate-or-cancel limit buy; returns the order dict. Tries the fixed-point
        body first, then the legacy integer body. A 400 creates no order, and the shared
        client_order_id stops a double fill either way."""
        coid = client_order_id or str(uuid.uuid4())
        base = {"ticker": ticker, "client_order_id": coid, "action": "buy", "side": side,
                "time_in_force": "immediate_or_cancel"}
        bodies = [
            {**base, "count_fp": f"{count:.2f}", f"{side}_price_dollars": f"{limit_price:.4f}"},
            {**base, "count": int(count), "type": "limit", f"{side}_price": round(limit_price * 100)},
        ]
        r = None
        for body in bodies:
            r = self._req("POST", "/portfolio/orders", body=body)
            if r.status_code < 300:
                d = r.json()
                return d.get("order", d)
            if r.status_code != 400:
                break
        raise KalshiError(f"Order rejected ({r.status_code}): {r.text[:300]}")


# ---------- field helpers ----------

def num(d, field):
    """A count from `x_fp` (fixed-point string) or a legacy integer `x`."""
    v = d.get(f"{field}_fp", d.get(field, 0))
    return float(v or 0)


def price(d, field):
    """A price in dollars from `x_dollars` or legacy integer cents `x`."""
    if d.get(f"{field}_dollars") not in (None, ""):
        return float(d[f"{field}_dollars"])
    v = d.get(field)
    return v / 100 if v else 0.0


def filled(order, requested):
    """Contracts an immediate-or-cancel order actually bought."""
    if "fill_count_fp" in order or "fill_count" in order:
        return num(order, "fill_count")
    return requested if order.get("status") == "executed" else 0


def candle_close(candle, field):
    """Closing yes_bid / yes_ask / price of a candle, in dollars."""
    return price(candle.get(field) or {}, "close")


def ts(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def fee(count, p):
    """Kalshi taker fee for one order, rounded up to the cent."""
    return math.ceil(round(FEE_RATE * count * p * (1 - p) * 100, 6)) / 100


def fee_per_contract(p):
    """Fee per contract for a large order, where cent rounding stops mattering."""
    return FEE_RATE * p * (1 - p)


def settled_result(m):
    """'yes' / 'no' once a market has settled, else None."""
    if m.get("status") in ("settled", "finalized", "determined") and m.get("result") in ("yes", "no"):
        return m["result"]
    return None
