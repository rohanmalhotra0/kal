"""The bot's own books, kept separate from whatever else is in the Kalshi account.

The rule: the bot may never lose more than the capital it was given (`budget`).
So its spendable money is budget + its realized profit (profit only counts if
reinvest_profits is on; losses always count), minus what's tied up in its open
positions. Built only from our own fills plus public settlement results, so a
deposit, a manual trade, or a schema change in Kalshi's settlement feed can't
inflate it. Kalshi's fills are read only to lower it (contracts sold outside the bot).
"""
import time

from . import db
from .client import UnknownSettlement, fee, num, price, ts, yes_payout

OPEN = ("active", "open")


class CatchUpError(RuntimeError):
    """Kalshi's fills can't be read, so the books can't be brought up to date."""


def _open_fills():
    """Filled live orders on markets we haven't seen settle yet, grouped by ticker and side."""
    return db.query("""SELECT o.ticker, o.side, SUM(o.filled) AS count,
        SUM(o.filled * o.price) AS notional, GROUP_CONCAT(o.filled || ':' || o.price) AS parts
        FROM orders o LEFT JOIN settlements s ON s.ticker = o.ticker
        WHERE o.filled > 0 AND s.ticker IS NULL GROUP BY o.ticker, o.side""")


def _cost(row):
    """Notional plus the fee on each fill. Recorded at our limit price, so if Kalshi filled us
    at a better price this overstates cost: the safe direction."""
    total = 0.0
    for part in (row["parts"] or "").split(","):
        if part:
            n, p = part.split(":")
            total += float(n) * float(p) + fee(float(n), float(p))
    return total


def _written_off():
    """{(ticker, side): (count, cost)} written off on markets that haven't settled yet."""
    rows = db.query("""SELECT w.ticker, w.side, SUM(w.count) AS count, SUM(w.cost) AS cost
        FROM writeoffs w LEFT JOIN settlements s ON s.ticker = w.ticker
        WHERE s.ticker IS NULL GROUP BY w.ticker, w.side""")
    return {(r["ticker"], r["side"]): (r["count"], r["cost"]) for r in rows}


def open_positions():
    """What the bot holds on markets that haven't settled, less anything written off. A
    position written off in full stays, at 0 contracts, until its market settles."""
    rows, gone = _open_fills(), _written_off()
    for r in rows:
        n, cost = gone.get((r["ticker"], r["side"]), (0.0, 0.0))
        r["count"] = max(0.0, r["count"] - n)
        r["cost"] = max(0.0, _cost(r) - cost)
    return rows


def open_cost():
    return sum(r["cost"] for r in open_positions())


def realized():
    """Settled profit and loss, minus everything written off (counted as lost in full)."""
    settled = db.query("SELECT COALESCE(SUM(pnl), 0) AS p FROM settlements")[0]["p"]
    return settled - db.query("SELECT COALESCE(SUM(cost), 0) AS c FROM writeoffs")[0]["c"]


def settle(client):
    """Book P&L for bot positions whose markets have settled. Returns how many settled."""
    rows = open_positions()
    if not rows:
        return 0
    markets = client.markets_by_ticker({r["ticker"] for r in rows})
    by_ticker = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)
    n = 0
    for ticker, legs in by_ticker.items():
        paid = yes_payout(markets.get(ticker, {}))  # raises UnknownSettlement: the engine trips
        if paid is None:
            continue
        if getattr(client, "can_trade", False) and _write_off_sold_outside(client, ticker, legs):
            legs = [r for r in open_positions() if r["ticker"] == ticker]
        revenue = sum(r["count"] * (paid if r["side"] == "yes" else 1 - paid) for r in legs)
        revenue = max(0.0, revenue - _restored_gain(ticker, paid))
        cost = sum(r["cost"] for r in legs)
        result = "yes" if paid == 1 else "no" if paid == 0 else f"{paid:.4f}"
        db.insert("settlements", ticker=ticker, settled_ts=time.time(), result=result,
                  revenue=revenue, cost=cost, pnl=revenue - cost)
        n += 1
    return n


def _write_off_sold_outside(client, ticker, legs):
    """Before a market's payout is booked, write off the bot's contracts on it that Kalshi's
    fills show someone sold outside the bot (or netted away by buying the other side). A run
    sees that in Kalshi's positions only while the market trades, so a sale while the bot
    was stopped, or just before the close, would be booked as paid. Fills only ever lower
    the books. Returns True if it wrote anything off."""
    mine = {r["order_id"] for r in db.query("SELECT DISTINCT order_id FROM orders WHERE ticker = ? "
                                            "AND order_id != ''", ticker)}
    toward_yes = 0.0   # how much everyone else's fills moved the account's position to YES
    try:
        for f in client.fills(ticker=ticker):
            oid, t, side, action, n, _, _ = _parse_fill(f)
            if t == ticker and oid not in mine:
                toward_yes += n if (side == "yes") == (action == "buy") else -n
    except CatchUpError as e:
        raise UnknownSettlement(f"{ticker}: can't read Kalshi's fills to check what the bot still held ({e})")
    lost = 0.0
    for r in legs:
        n = toward_yes if r["side"] == "no" else -toward_yes
        if n > 0.5:
            lost += write_off(ticker, r["side"], n, "sold outside the bot before it settled (Kalshi's fills)")
    if lost:
        try:
            from . import notify
            notify.send("kal wrote off some contracts",
                        f"Kalshi's fills show the bot's contracts on {ticker} were sold outside the bot "
                        f"before it settled. It counted them as lost (${lost:.2f}). If you sold them, "
                        f"that's fine.", tags=("warning",))
        except Exception:
            pass   # the write-off is booked either way; an alert failure can't undo it
    return lost > 0


def _restored_gain(ticker, paid):
    """What orders added after a restore (catch_up) make over their cost on a settled market.
    Some may be the owner's own trades, so only their losses count, never their gains."""
    gain = 0.0
    for r in db.query("SELECT side, filled, price FROM orders WHERE ticker = ? AND strategy = 'restored' "
                      "AND filled > 0", ticker):
        pay = r["filled"] * (paid if r["side"] == "yes" else 1 - paid)
        gain += max(0.0, pay - r["filled"] * r["price"] - fee(r["filled"], r["price"]))
    return gain


def payout_of(result, side):
    """Per-contract payout for a booked settlement result ("yes", "no", or a YES value)."""
    if result == "yes":
        paid = 1.0
    elif result == "no":
        paid = 0.0
    else:
        paid = float(result)
    return paid if side == "yes" else 1 - paid


def capital(cfg):
    """What the bot has to work with: its starting budget plus (or minus) what it has made."""
    r = realized()
    return cfg["budget"] + (r if cfg.get("reinvest_profits", True) else min(0.0, r))


def available(cfg):
    return max(0.0, capital(cfg) - open_cost())


def equity(client, cfg):
    """Capital with open positions marked at their best bid instead of at cost.

    A market with no bid at all (thin books, or a lookup that failed) is valued at cost,
    not $0: no buyer right now isn't a loss, and treating it as one would trip the daily
    loss limit for nothing. Real losses still land when the market settles."""
    rows = open_positions()
    if not rows:
        return capital(cfg)
    markets = client.markets_by_ticker({r["ticker"] for r in rows})
    value = 0.0
    for r in rows:
        bid = price(markets.get(r["ticker"], {}), f"{r['side']}_bid")
        value += r["count"] * bid if bid > 0 else r["cost"]
    return capital(cfg) - open_cost() + value


def open_by_event():
    """Dollars in open bot positions per event (from the signal each order belonged to)."""
    rows = db.query("""SELECT COALESCE(sg.event, o.ticker) AS event, o.filled, o.price
        FROM orders o LEFT JOIN settlements s ON s.ticker = o.ticker
        LEFT JOIN signals sg ON sg.id = o.signal_id
        WHERE o.filled > 0 AND s.ticker IS NULL""")
    out = {}
    for r in rows:
        out[r["event"]] = out.get(r["event"], 0.0) + r["filled"] * r["price"] + fee(r["filled"], r["price"])
    return out


def unresolved_orders():
    """Orders whose outcome we don't know: the send may or may not have reached Kalshi."""
    return db.query("SELECT * FROM orders WHERE status IN ('sending', 'unknown')")


def open_directional():
    """Dollars in open bot positions that came from bets, not locked-in arbitrage."""
    rows = db.query("""SELECT o.filled, o.price FROM orders o
        LEFT JOIN settlements s ON s.ticker = o.ticker
        LEFT JOIN signals sg ON sg.id = o.signal_id
        WHERE o.filled > 0 AND s.ticker IS NULL AND COALESCE(sg.guaranteed, 0) = 0""")
    return sum(r["filled"] * r["price"] + fee(r["filled"], r["price"]) for r in rows)


def peak_equity():
    rows = db.query("SELECT MAX(equity) AS p FROM equity WHERE balance > 0 OR exposure > 0 OR equity > 0")
    return rows[0]["p"] if rows and rows[0]["p"] is not None else None


def shortfalls(positions):
    """Markets where Kalshi holds fewer of the bot's contracts than its books say: someone
    sold them outside the bot, most likely. Returns [(ticker, side, missing)]."""
    held = {}
    for p in positions:
        held[p["ticker"]] = held.get(p["ticker"], 0.0) + p["position"]
    books = {}
    for r in open_positions():
        books[r["ticker"]] = books.get(r["ticker"], 0.0) + (r["count"] if r["side"] == "yes" else -r["count"])
    out = []
    for t, mine in books.items():
        kalshi = held.get(t, 0.0)
        if kalshi and (kalshi > 0) != (mine > 0):
            continue  # opposite sides: unexplained() stops the bot for that
        missing = abs(mine) - abs(kalshi)
        if missing > 0.5:
            out.append((t, "yes" if mine > 0 else "no", missing))
    return out


def write_off(ticker, side, count, note=""):
    """Count `count` of the bot's contracts on a market as lost: their share of the cost
    leaves capital now, and they earn nothing when the market settles. Returns the dollars."""
    row = next((r for r in open_positions() if r["ticker"] == ticker and r["side"] == side), None)
    if not row or row["count"] <= 0:
        return 0.0
    count = min(float(count), row["count"])
    cost = row["cost"] * count / row["count"]
    db.insert("writeoffs", ticker=ticker, side=side, count=count, cost=cost, note=note[:300])
    return cost


def _parse_fill(f):
    side, action = f.get("side"), f.get("action", "buy")
    n = num(f, "count")
    p = price(f, f"{side}_price") if side in ("yes", "no") else 0.0
    if side not in ("yes", "no") or action not in ("buy", "sell") or not n > 0 or not 0 < p < 1 \
            or not f.get("order_id") or not f.get("ticker"):
        raise CatchUpError(f"a fill looks different than expected (fields: {', '.join(sorted(f))[:200]})")
    try:
        when = ts(f["created_time"]) if f.get("created_time") else time.time()
    except (TypeError, ValueError, AttributeError):
        raise CatchUpError(f"a fill has an unreadable time: {f.get('created_time')!r}")
    return f["order_id"], f["ticker"], side, action, n, p, when


def catch_up(client):
    """After the books were restored from a backup, add the buys Kalshi shows since that
    backup's day that the books don't have, at the highest price paid. Fills from the
    owner's own trades in that window get counted too: the safe direction, since they only
    make the bot think it spent more, and settle() never books their gains, only their
    losses. Returns how many orders were added (0 if no restore is waiting). Raises
    CatchUpError if a fill can't be read: never guess."""
    gap = db.get("restored")
    if not gap:
        return 0
    known = {r["order_id"] for r in db.query("SELECT DISTINCT order_id FROM orders WHERE order_id != ''")}
    found = {}
    for f in client.fills(min_ts=gap["since"]):
        oid, ticker, side, action, n, p, when = _parse_fill(f)
        if action != "buy" or oid in known:
            continue  # the bot never sells; a sold position shows up in shortfalls()
        o = found.setdefault(oid, {"ticker": ticker, "side": side, "n": 0.0, "p": 0.0, "ts": when})
        o["n"] += n
        o["p"] = max(o["p"], p)
        o["ts"] = min(o["ts"], when)
    for oid, o in found.items():
        db.insert("orders", ts=o["ts"], strategy="restored", ticker=o["ticker"], side=o["side"],
                  count=o["n"], price=o["p"], filled=o["n"], status="imported", order_id=oid,
                  client_order_id="")
    db.put("restore_check", True)   # strangers() gets one look at Kalshi's positions next
    db.execute("DELETE FROM kv WHERE key = 'restored'")
    return len(found)


def strangers(positions):
    """Markets Kalshi holds that the bot's books have never traded. Normally the owner's own
    business; right after a restore they could be the bot's, missed by catch_up()."""
    traded = {r["ticker"] for r in db.query("SELECT DISTINCT ticker FROM orders")}
    return sorted({p["ticker"] for p in positions if p["ticker"] not in traded})


def unexplained(positions):
    """Markets where Kalshi says we hold more than the bot's own books can explain.

    The books are built from our order records; Kalshi's positions are the truth. If an
    order's reply was lost and reconciliation missed it, the books would undercount and the
    loss cap could be exceeded. So any market the bot traded where Kalshi shows a bigger (or
    opposite) position than the books is a reason to stop. Markets the bot never traded are
    the owner's own business and ignored. Returns [(ticker, kalshi_net, books_net)]."""
    books = {}
    for r in open_positions():
        books[r["ticker"]] = books.get(r["ticker"], 0.0) + (r["count"] if r["side"] == "yes" else -r["count"])
    traded = {r["ticker"] for r in db.query("SELECT DISTINCT ticker FROM orders")}
    out = []
    for p in positions:
        t = p["ticker"]
        if t not in traded:
            continue
        held, mine = p["position"], books.get(t, 0.0)
        if abs(held) > abs(mine) + 0.5 or (held and mine and (held > 0) != (mine > 0)):
            out.append((t, held, mine))
    return out
