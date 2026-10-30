"""An in-memory stand-in for kal.client.Kalshi. Tests set its fields directly."""
from kal.client import ts as _ts


class _OrderLog(list):
    """The list of buy() calls, which can also be called like Kalshi.orders() to look up
    orders (returns FakeKalshi.order_list)."""

    def __init__(self, owner):
        super().__init__()
        self._owner = owner

    def __call__(self, ticker=None, min_ts=None, archive=False):
        if archive:
            self._owner._store()
        found = self._owner.archived_orders if archive else self._owner.order_list
        return [o for o in found if ticker in (None, o.get("ticker")) and self._owner._since(o, min_ts)]


class FakeKalshi:
    key_id = "test-key"

    def __init__(self, can_trade=True, balance=100.0):
        self.can_trade = can_trade
        self._balance = balance
        self.event_list = []          # what events() returns
        self.market_list = []         # what markets() returns
        self.books = {}               # ticker -> {"yes": [(p, q)...], "no": [...]}
        self.market_info = {}         # ticker -> market dict for markets_by_ticker / market()
        self.position_list = None     # what positions() returns; None = what buy() bought
        self.held = {}                # ticker -> net contracts bought (+ YES, - NO)
        self.fill_list = []           # what fills() returns
        self.archived_fills = []      # what fills(archive=True) returns: Kalshi's historical store
        self.archived_orders = []     # what the orders(archive=True) lookup returns
        self.archived_markets = {}    # ticker -> market, for market(archive=True)
        self.cutoffs = {}             # what cutoff() returns (an exception: raised); {}: unknown
        self.archive_reads = 0        # how many times the historical store was read
        self.archive_error = None     # an exception reading the store raises
        self.honor_min_ts = False     # True: fills and order lookups keep only records from min_ts on
        self.settled = {}             # series -> [market dicts]
        self.candle_data = {}         # ticker -> [candles]
        self.series_list = []
        self.orders = _OrderLog(self)  # every buy() call; also callable as the orders() lookup
        self.order_list = []          # what the orders() lookup returns (for reconciliation)
        self.cancelled = []
        self.trade_list = {}          # ticker -> public trades, for the informed-flow check
        self.series_fees = {}         # series -> {"fee_type", "fee_multiplier"} (default: quadratic x1)
        self.fee_overrides = []       # what event_fee_changes() returns
        self.fill = lambda ticker, side, count, price: count  # override to simulate partial fills

    def events(self, **params):
        return self.event_list

    def markets(self, limit=None, **params):
        return self.market_list

    def series_info(self, ticker):
        return {"ticker": ticker, **self.series_fees.get(ticker, {"fee_type": "quadratic", "fee_multiplier": 1})}

    def event_fee_changes(self):
        return self.fee_overrides

    def trades(self, ticker, min_ts=None, limit=500):
        return [t for t in self.trade_list.get(ticker, [])
                if min_ts is None or _ts(t["created_time"]) >= min_ts]

    def markets_by_ticker(self, tickers):
        return {t: self.market_info[t] for t in tickers if t in self.market_info}

    def market(self, ticker, archive=False):
        return (self.archived_markets if archive else self.market_info).get(ticker, {})

    def cutoff(self):
        if isinstance(self.cutoffs, Exception):
            raise self.cutoffs
        return dict(self.cutoffs)

    def orderbook(self, ticker):
        return self.books.get(ticker, {"yes": [], "no": []})

    def best_bid(self, ticker, side="yes"):
        levels = self.orderbook(ticker)[side]
        return levels[0] if levels else (0.0, 0.0)

    trading_active = True

    def exchange_status(self):
        return {"exchange_active": True, "trading_active": self.trading_active}

    def balance(self):
        return self._balance

    def positions(self):
        if self.position_list is not None:
            return self.position_list
        return [{"ticker": t, "position": n, "exposure": 0.0} for t, n in self.held.items() if n]

    def settled_markets(self, series, limit=None):
        return self.settled.get(series, [])

    def candles(self, series, ticker, start_ts, end_ts, period=60):
        return [c for c in self.candle_data.get(ticker, []) if start_ts <= c["end_period_ts"] <= end_ts]

    def series(self, category=None):
        return self.series_list

    def settlements(self, min_ts=None):
        return []

    def _store(self):
        self.archive_reads += 1
        if self.archive_error:
            raise self.archive_error

    def _since(self, record, min_ts):
        return not (self.honor_min_ts and min_ts and record.get("created_time")
                    and _ts(record["created_time"]) < min_ts)

    def fills(self, min_ts=None, ticker=None, archive=False):
        if archive:
            self._store()
        found = self.archived_fills if archive else self.fill_list
        return [f for f in found if (ticker is None or f.get("ticker") == ticker) and self._since(f, min_ts)]

    def buy(self, ticker, side, count, limit_price, client_order_id=None):
        n = self.fill(ticker, side, count, limit_price)
        self.orders.append((ticker, side, count, limit_price))
        self.held[ticker] = self.held.get(ticker, 0) + (n if side == "yes" else -n)
        return {"order_id": f"o{len(self.orders)}", "status": "executed" if n == count else "canceled",
                "fill_count_fp": f"{n:.2f}"}

    def cancel(self, order_id):
        self.cancelled.append(order_id)
        return {}
