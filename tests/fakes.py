"""An in-memory stand-in for kal.client.Kalshi. Tests set its fields directly."""


class _OrderLog(list):
    """The list of buy() calls, which can also be called like Kalshi.orders() to look up
    orders (returns FakeKalshi.order_list)."""

    def __init__(self, owner):
        super().__init__()
        self._owner = owner

    def __call__(self, ticker=None, min_ts=None):
        return [o for o in self._owner.order_list if ticker in (None, o.get("ticker"))]


class FakeKalshi:
    def __init__(self, can_trade=True, balance=100.0):
        self.can_trade = can_trade
        self._balance = balance
        self.event_list = []          # what events() returns
        self.market_list = []         # what markets() returns
        self.books = {}               # ticker -> {"yes": [(p, q)...], "no": [...]}
        self.market_info = {}         # ticker -> market dict for markets_by_ticker / market()
        self.position_list = []
        self.settled = {}             # series -> [market dicts]
        self.candle_data = {}         # ticker -> [candles]
        self.series_list = []
        self.orders = _OrderLog(self)  # every buy() call; also callable as the orders() lookup
        self.order_list = []          # what the orders() lookup returns (for reconciliation)
        self.cancelled = []
        self.fill = lambda ticker, side, count, price: count  # override to simulate partial fills

    def events(self, **params):
        return self.event_list

    def markets(self, limit=None, **params):
        return self.market_list

    def markets_by_ticker(self, tickers):
        return {t: self.market_info[t] for t in tickers if t in self.market_info}

    def market(self, ticker):
        return self.market_info.get(ticker, {})

    def orderbook(self, ticker):
        return self.books.get(ticker, {"yes": [], "no": []})

    def best_bid(self, ticker, side="yes"):
        levels = self.orderbook(ticker)[side]
        return levels[0] if levels else (0.0, 0.0)

    def balance(self):
        return self._balance

    def positions(self):
        return self.position_list

    def settled_markets(self, series, limit=None):
        return self.settled.get(series, [])

    def candles(self, series, ticker, start_ts, end_ts, period=60):
        return [c for c in self.candle_data.get(ticker, []) if start_ts <= c["end_period_ts"] <= end_ts]

    def series(self, category=None):
        return self.series_list

    def settlements(self, min_ts=None):
        return []

    def fills(self, min_ts=None):
        return []

    def buy(self, ticker, side, count, limit_price, client_order_id=None):
        n = self.fill(ticker, side, count, limit_price)
        self.orders.append((ticker, side, count, limit_price))
        return {"order_id": f"o{len(self.orders)}", "status": "executed" if n == count else "canceled",
                "fill_count_fp": f"{n:.2f}"}

    def cancel(self, order_id):
        self.cancelled.append(order_id)
        return {}
