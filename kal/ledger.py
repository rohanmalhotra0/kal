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
from .client import UnknownSettlement, every_fill, fee, num, outcome_side, price, ts, yes_payout

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
    """Book P&L for bot positions whose markets have settled. Returns how many settled.
    Without a key it books nothing: it couldn't check Kalshi's fills for contracts sold
    outside the bot, and a payout booked unchecked is never looked at again."""
    rows = open_positions()
    if not rows or not getattr(client, "can_trade", False):
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
        if _write_off_sold_outside(client, ticker, legs, markets.get(ticker, {}).get("open_time")):
            legs = [r for r in open_positions() if r["ticker"] == ticker]
        revenue = sum(r["count"] * (paid if r["side"] == "yes" else 1 - paid) for r in legs)
        revenue = max(0.0, revenue - _restored_gain(ticker, paid))
        cost = sum(r["cost"] for r in legs)
        result = "yes" if paid == 1 else "no" if paid == 0 else f"{paid:.4f}"
        db.insert("settlements", ticker=ticker, settled_ts=time.time(), result=result,
                  revenue=revenue, cost=cost, pnl=revenue - cost)
        n += 1
    return n


def _write_off_sold_outside(client, ticker, legs, opened=None):
    """Before a market's payout is booked, write off the bot's contracts on it that Kalshi's
    fills show someone sold outside the bot (or netted away by buying the other side). A run
    sees that in Kalshi's positions only while the market trades, so a sale while the bot
    was stopped, or just before the close, would be booked as paid. Fills only ever lower
    the books. Every fill since the market opened (`opened`, its open_time; None: all of
    them) counts, from Kalshi's historical store too: the open time only decides whether the
    store is read. Returns True if it wrote anything off."""
    try:
        since = ts(opened) - 3600 if opened else None   # no fills before a market opens
    except (AttributeError, TypeError, ValueError):
        since = None
    mine = {r["order_id"] for r in db.query("SELECT DISTINCT order_id FROM orders WHERE ticker = ? "
                                            "AND order_id != ''", ticker)}
    toward_yes = 0.0   # how much everyone else's fills moved the account's position to YES
    try:
        for f in every_fill(client, ticker=ticker, earliest=since):
            oid, t, side, action, n, _, _ = _parse_fill(f)
            if t == ticker and oid not in mine:
                toward_yes += n if (side == "yes") == (action == "buy") else -n
    except CatchUpError as e:
        raise UnknownSettlement(f"{ticker}: can't read Kalshi's fills to check what the bot still held ({e})")
    # A sale a run already saw in Kalshi's positions (engine._write_off_sold) is written off once.
    done = {w["side"]: w["n"] for w in db.query("SELECT side, SUM(count) AS n FROM writeoffs WHERE ticker = ? "
                                                  "GROUP BY side", ticker)}
    lost = 0.0
    for r in legs:
        n = (toward_yes if r["side"] == "no" else -toward_yes) - (done.get(r["side"]) or 0.0)
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


def _restored_gain(ticker, paid, side=None):
    """What orders added after a restore (catch_up) are worth over their cost on a market,
    at a YES value of `paid` (its payout, or for one side, that side's bid as 1 - paid on NO).
    Some may be the owner's own trades, so only their losses count, never their gains: at
    settlement and in equity alike, so a win settling never reads as a loss."""
    gain = 0.0
    for r in db.query("SELECT side, filled, price FROM orders WHERE ticker = ? AND strategy = 'restored' "
                      "AND filled > 0", ticker):
        if side and r["side"] != side:
            continue
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


def written_off_shares():
    """{order id: the share (0 to 1) of its contracts written off}, for reports. Kalshi can't
    say whose contracts went, so a write-off falls on the orders on its market and side bought
    before it, in proportion to what each still held (a re-buy after a sale keeps its own).
    Rounded, so float dust can't leave a sliver of a contract held."""
    offs = db.query("SELECT ticker, side, count, ts FROM writeoffs ORDER BY ts, id")
    if not offs:
        return {}
    keys = {(w["ticker"], w["side"]) for w in offs}
    orders = {}
    for o in db.query("SELECT id, ticker, side, filled, ts FROM orders WHERE filled > 0 ORDER BY ts, id"):
        if (o["ticker"], o["side"]) in keys:
            orders.setdefault((o["ticker"], o["side"]), []).append(o)
    gone = {}
    for w in offs:
        mine, left = orders.get((w["ticker"], w["side"]), []), w["count"] or 0.0
        when = w["ts"] if w["ts"] is not None else float("inf")
        # The orders bought before it first; anything left (clocks) on the rest.
        for group in ([o for o in mine if (o["ts"] or 0) <= when], mine):
            held = {o["id"]: max(0.0, o["filled"] - gone.get(o["id"], 0.0)) for o in group}
            total = sum(held.values())
            if left <= 0 or total <= 0:
                continue
            take = min(left, total)
            for o in group:
                gone[o["id"]] = gone.get(o["id"], 0.0) + take * held[o["id"]] / total
            left -= take
    filled = {o["id"]: o["filled"] for group in orders.values() for o in group}
    return {oid: min(1.0, round(n / filled[oid], 9)) for oid, n in gone.items() if n > 0}


def settled_order_pnl(shares=None):
    """{order id: profit} for each filled order whose market settled, as the books count it,
    for reports. Per market it adds up to what settle() booked less the write-offs, so the
    strategies add up to realized(). Contracts written off earn nothing (written_off_shares).
    A restored order's gain never counts: settle() took it off the market's pay, so restored
    orders get only what it kept beyond the other orders' pay."""
    shares = written_off_shares() if shares is None else shares
    markets = {}
    for r in db.query("""SELECT o.id, o.strategy, o.side, o.filled, o.price, s.ticker, s.result, s.revenue
            FROM orders o JOIN settlements s ON s.ticker = o.ticker WHERE o.filled > 0"""):
        markets.setdefault(r["ticker"], []).append(r)
    out = {}
    for rows in markets.values():
        try:
            paid = {r["id"]: payout_of(r["result"], r["side"]) for r in rows}
        except (TypeError, ValueError):
            continue   # not a payout: nothing to report
        cost = {r["id"]: r["filled"] * r["price"] + fee(r["filled"], r["price"]) for r in rows}
        pay = {r["id"]: r["filled"] * (1 - shares.get(r["id"], 0.0)) * paid[r["id"]] for r in rows}
        restored = [r for r in rows if r["strategy"] == "restored"]
        if restored:
            booked = rows[0]["revenue"]
            if booked is None:   # as settle() works it out
                gain = sum(max(0.0, r["filled"] * paid[r["id"]] - cost[r["id"]]) for r in restored)
                booked = max(0.0, sum(pay.values()) - gain)
            others = sum(pay[r["id"]] for r in rows if r["strategy"] != "restored")
            n = sum(r["filled"] for r in restored)
            for r in restored:
                pay[r["id"]] = (booked - others) * r["filled"] / n
        for r in rows:
            out[r["id"]] = pay[r["id"]] - cost[r["id"]]
    return out


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
        if bid > 0:
            yes = bid if r["side"] == "yes" else 1 - bid
            value += max(0.0, r["count"] * bid - _restored_gain(r["ticker"], yes, r["side"]))
        else:
            value += r["cost"]
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


def _direction(f):
    """(side, action) of a fill. Kalshi's deprecated side and action say exactly what was
    done. Once they're gone, the outcome side says which way: buy YES and sell NO leave the
    same position, so it reads as a buy of that side, priced on that side. Both there but
    saying different things: CatchUpError, never a guess."""
    try:
        outcome = outcome_side(f)
    except ValueError as e:
        raise CatchUpError(f"a fill's direction fields disagree: {e}")
    side, action = f.get("side"), f.get("action")
    if side in ("yes", "no") and action in ("buy", "sell"):
        long = side if action == "buy" else ("no" if side == "yes" else "yes")
        if outcome and outcome != long:
            raise CatchUpError(f"a fill says {action} {side} but outcome {outcome}: they should agree")
        return side, action
    if outcome:
        return outcome, "buy"
    return side, action or "buy"   # as before: a side with no action was a buy


def _parse_fill(f):
    side, action = _direction(f)
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
    CatchUpError if a fill can't be read: never guess. Fills older than Kalshi's cutoff are
    read from its historical store, so an old backup still gets every buy since its day.

    Never added: a fill on the other side of contracts the books hold at that moment. It
    nets them away (the owner selling them, or buying the other side; the bot never buys
    both sides of a market), so shortfalls() and the sold-outside check write them off.
    Added as a buy, it would hide that, and the payout would be booked. Fills go in time
    order, so a buy and its sale inside the gap work out too."""
    gap = db.get("restored")
    if not gap:
        return 0
    known = {r["order_id"] for r in db.query("SELECT DISTINCT order_id FROM orders WHERE order_id != ''")}
    held = {}
    for r in open_positions():
        held[(r["ticker"], r["side"])] = held.get((r["ticker"], r["side"]), 0.0) + r["count"]
    found = {}
    for oid, ticker, side, action, n, p, when in sorted((_parse_fill(f) for f in every_fill(client, min_ts=gap["since"])),
                                                        key=lambda x: x[6]):
        if action != "buy" or oid in known:
            continue  # the bot never sells; a sold position shows up in shortfalls()
        if held.get((ticker, "no" if side == "yes" else "yes"), 0.0) > 1e-6:
            continue  # nets held contracts away: written off, never booked as the bot's buy
        held[(ticker, side)] = held.get((ticker, side), 0.0) + n
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
