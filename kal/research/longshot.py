"""Research for longshot_fade: did buying NO on cheap YES pay, by market type?

For each Kalshi category (from the series list) and each YES price range under 20c, buy
NO at 1 - YES bid at the tested time before the event's scheduled close, in every settled
market whose book was tight then, and see if it made money after fees. Prices, the
no-look-ahead rule and the close anchoring all come from backtest.load_observations. Its
price cache is shared with daily discovery, and series are picked by discovery's own
rotation, so most days this costs few extra API calls.

Only busy series count (discover.active_series), never combos or CPI/Fed brackets. A cell
counts only if a cautious lower bound on its ROI is above zero, with every cell tested a
separate try (Bonferroni). Trades are grouped by series and close day, not just by event:
hourly BTC events, or one city's weather, win or lose together. That's stricter than the
favorites backtest, on purpose: the edge here is a few cents and the losses are big.

Writes $KAL_HOME/research/longshot.json, a list of cells:
  {id: "CATEGORY:lo-hi", category, lo, hi, hours_before_close, trades, events, groups,
   roi, roi_lo, series, status, first_seen, last_tested, note}
  status: "candidate" (paper only) | "live" | "retired"
  series: the busy series whose markets made up the cell. The strategy trades only those.
apply_longshot() moves cells between statuses by promote.py's rules.
"""
import logging
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone

from ..client import fee
from ..strategies import longshot_fade as fade
from . import backtest, discover, promote

FILE = "longshot.json"

log = logging.getLogger(__name__)


# ---------- the file ----------

def load_cells():
    return discover.recalled(fade.NAME, fade.cells_of({"longshot": discover._load(FILE, [])}))


def save_cells(cells):
    discover._save(FILE, sorted(cells, key=lambda c: c["id"]))
    discover.remember_all(fade.NAME, cells)


def cell_id(category, lo, hi):
    return f"{category}:{lo:.2f}-{hi:.2f}"


# ---------- the backtest ----------

def bucket(yes_bid):
    """The (lo, hi) YES price range a bid falls in, or None (under 1c or 20c and up)."""
    for lo, hi in fade.BUCKETS:
        if lo <= yes_bid < hi:
            return lo, hi
    return None


def fade_trade(o, hours):
    """(group, cost, pnl, fair_var) for one full-size NO bet at 1 - YES bid, priced the way
    the strategy places it (fee rounded up per order). None if the book was too wide to
    trade or the row is unreadable. A cancelled market counts at what NO really paid: a NO
    bought at 95c that paid $0.67 lost 28c."""
    try:
        bid, ask = float(o["yes_bid"]), float(o["yes_ask"])
        anchor = float(o["decided"]) + hours * 3600 if o.get("decided") else float(o["close"])
        group = fade.group_of(o["series"], anchor)
        won = backtest.paid(o, "no")
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None
    if not fade.tight(bid, ask):
        return None
    p = round(1 - bid, 4)
    n = fade.full_size(p)
    cost = n * p + fee(n, p)
    return group, cost, n * won - cost, n * n * p * (1 - p)


def compact(cat, obs, hours, trades):
    """Add one category's tight longshot trades from `obs` to `trades`
    ({(category, lo, hi): [(trade, event, series)]}), keeping a few numbers per trade and
    nothing else. The daily run calls this one series at a time and then drops the rows, so
    memory stays small however big the price cache grows."""
    for o in obs:
        try:
            b = bucket(float(o.get("yes_bid") or 0))
        except (TypeError, ValueError, AttributeError):
            continue
        t = fade_trade(o, hours) if b else None
        if t:
            trades[(cat, *b)].append((t, o.get("event") or o.get("ticker"), o.get("series")))
    return trades


def calibrate(obs_by_category, hours):
    """One row per (category, YES price range) that had trades: what buying NO on every
    tight longshot there paid. Every row is one more try, so z grows with the count."""
    trades = defaultdict(list)
    for cat, obs in obs_by_category.items():
        compact(cat, obs, hours, trades)
    return rows_of(trades)


def rows_of(trades):
    """calibrate()'s rows from compact() trades."""
    z = backtest.z_for(len(trades))
    rows = []
    for cat, lo, hi in sorted(trades):
        got = trades[(cat, lo, hi)]
        ts_ = [t for t, _, _ in got]
        roi, roi_lo = backtest.roi_stats(ts_, z)  # clustered by group (t[0])
        rows.append({"category": cat, "lo": lo, "hi": hi, "trades": len(got),
                     "events": len({ev for _, ev, _ in got}), "groups": len({t[0] for t in ts_}),
                     "win_rate": sum(1 for t in ts_ if t[2] > 0) / len(ts_), "roi": roi, "roi_lo": roi_lo,
                     "series": sorted({s for _, _, s in got if s})})
    return rows


def is_edge(row):
    return (row["roi_lo"] is not None and row["roi_lo"] > 0
            and row["trades"] >= backtest.MIN_TRADES and row["groups"] >= backtest.MIN_EVENTS)


def merge_cells(cells, rows, hours, stamp):
    """Fold calibration rows into the cell list (in place), like discover.merge_edges:
    a cell that passes is added or refreshed; an active one that now fails with enough
    data is retired; one retired for losing paper or real money stays retired."""
    changes = {"new": [], "retired": [], "revived": []}
    by_id = {c["id"]: c for c in cells}
    for r in rows:
        cid = cell_id(r["category"], r["lo"], r["hi"])
        c = by_id.get(cid)
        if c is not None and c.get("hours_before_close") != hours:
            continue  # tested at another decision time: not comparable
        stats = {k: r[k] for k in ("roi", "roi_lo", "trades", "events", "groups", "series")}
        if is_edge(r):
            if c is None:
                c = discover.recalled(fade.NAME, [{"id": cid, "category": r["category"], "lo": r["lo"], "hi": r["hi"],
                                                   "hours_before_close": hours, "first_seen": stamp,
                                                   "status": "candidate", "note": "new: trading on paper first"}])[0]
                cells.append(c)
                by_id[cid] = c
                if c["status"] == "candidate":
                    changes["new"].append(cid)
            elif c.get("status") == "retired" and c.get("retired_by") == "backtest":
                c.update(status="candidate", note="passed the backtest again: back on paper", retired_by="")
                changes["revived"].append(cid)
            c.update(stats, last_tested=stamp)
        elif c is not None:
            c.update(stats, last_tested=stamp)
            if c.get("status") in fade.ACTIVE and r["trades"] >= backtest.MIN_TRADES:
                c.update(status="retired", retired_by="backtest", note="backtest no longer shows an edge")
                changes["retired"].append(cid)
    return changes


# ---------- the daily run ----------

def run(client, cfg, quick=False, now=None, series=None):
    """Test every category's longshot cells and save longshot.json. Returns a summary.
    Never raises: a failing series goes in "failed" and the rest carry on; anything
    worse comes back as "error" with ok False. `series` names series to test instead of
    today's rotation (for manual runs and tests)."""
    now = time.time() if now is None else now
    summary = {"ok": False, "day": discover._iso(now)[:10], "liquid": 0, "tested": [], "failed": {},
               "categories": {}, "new_cells": [], "retired": [], "revived": []}
    try:
        _run(client, cfg, quick, now, series, summary)
    except Exception as e:
        summary.update(ok=False, error=f"longshot research failed: {e}"[:300])
        log.error("longshot research failed: %s", traceback.format_exc())
    return summary


def _run(client, cfg, quick, now, named, summary):
    stamp = discover._iso(now)
    hours = float(cfg.get("research_hours_before_close", discover.HOURS_BEFORE_CLOSE))
    max_series = int(cfg.get("research_max_series", discover.MAX_SERIES))
    max_markets = int(cfg.get("research_max_markets", discover.MAX_MARKETS))
    if quick:
        max_series, max_markets = min(max_series, 3), min(max_markets, 200)

    info = {s["ticker"]: s for s in client.series() if isinstance(s, dict) and s.get("ticker")}
    # Busy series only: thin ones can't be traded, and their prices say little.
    liquid = [t for t in discover.active_series(client) if t in info and info[t].get("frequency") != "one_off"]
    summary["liquid"] = len(liquid)
    if not liquid and not named:
        summary["error"] = "couldn't tell which series are busy (Kalshi's open events list failed)"
        return
    usable = {t for t in set(liquid) | set(named or ()) if t in info
              and str(info[t].get("category") or "").strip() and not fade.excluded(info[t])}
    day = datetime.fromtimestamp(now, timezone.utc).date().toordinal()
    # Discovery's own rotation, so most of today's series are already in its price cache.
    picks = list(named) if named else discover.pick_series(liquid, [], {}, day, max_series, now)

    # Each series' rows become compact trades at once and are dropped before the next
    # loads: holding every busy series' rows together could pass the service's memory cap.
    trades, seen, counts = defaultdict(list), [], defaultdict(int)

    def take(s, obs):
        obs = backtest.busy(obs)   # only what the live strategy could have traded
        cat = str(info[s]["category"]).strip()
        compact(cat, obs, hours, trades)
        counts[cat] += len(obs)
        seen.append(s)

    for s in picks:
        if s not in usable:
            continue
        try:
            take(s, backtest.load_observations(client, s, hours, max_markets=max_markets))
        except Exception as e:  # one series failing must never stop the others
            log.warning("longshot: %s failed: %s", s, e, exc_info=True)
            summary["failed"][s] = str(e)[:300]
            continue
        summary["tested"].append(s)
    # Busy series tested on earlier days (by this or by discovery) count too, from the
    # cache: no API calls. A series that failed today still has what it saved before.
    for s in sorted(usable - set(seen)):
        try:
            obs = backtest.load_observations(client, s, hours, max_markets=max_markets, cached_only=True)
        except Exception as e:
            log.warning("longshot: cached %s unreadable: %s", s, e)
            summary["failed"].setdefault(s, str(e)[:300])
            continue
        if obs:
            take(s, obs)
        del obs

    summary["categories"] = dict(sorted(counts.items()))
    rows = rows_of(trades)
    cells = load_cells()
    changes = merge_cells(cells, rows, hours, stamp)
    save_cells(cells)
    backtest.record("longshot", {"hours_before_close": hours, "series": sorted(seen), "max_spread": fade.MAX_SPREAD},
                    {"observations": sum(counts.values()), "rows": rows,
                     "cells": [r for r in rows if is_edge(r)]})
    summary.update(new_cells=changes["new"], retired=changes["retired"], revived=changes["revived"])
    summary["ok"] = bool(summary["tested"]) or not summary["failed"]
    summary["cells"] = {st: sum(1 for c in cells if c.get("status") == st) for st in ("candidate", "live", "retired")}


# ---------- promotion ----------

def _label(c):
    """ "Climate and Weather 5-10c": a cell's name in plain words."""
    try:
        return f"{c['category']} {float(c['lo']) * 100:.0f}-{float(c['hi']) * 100:.0f}c"
    except (KeyError, TypeError, ValueError):
        return str(c.get("id", "?"))


def apply_longshot(cfg, client=None):
    """Promote and retire cells from what paper (and real) trades did, by promote.py's rules:
    candidate -> live  30+ settled paper trades in 20+ groups, worst case above zero, with
                       the bar raised on every look. Only when cfg["auto_promote"] is on.
    live -> retired    its last 30 settled trades lost money. Always on: stopping a loser
                       is never risky.
    Trades group by series and close day (signal meta "group"), as in the backtest.
    Paper rows find their cell through paper.signal_id -> signals.meta["cell"].
    Returns short, plain notes."""
    cells = load_cells()
    if not cells:
        return []
    by_cell = defaultdict(list)
    for t in promote.paper_trades(fade.NAME) + promote.live_trades(fade.NAME, client):
        cid = t["meta"].get("cell")
        if cid:
            by_cell[cid].append(dict(t, event=t["meta"].get("group") or t["event"]))
    # Every candidate judged now is a separate try, so each needs more confidence.
    tries = {c["id"] for c in cells if c.get("status") == "candidate"
             and sum(not t["live"] for t in by_cell[c["id"]]) >= promote.MIN_PAPER_TRADES}
    notes, stamp = [], discover._iso(time.time())
    for c in cells:
        trades = by_cell.get(c["id"], [])
        if not trades:
            continue
        if c["id"] in tries:
            c["looks"] = int(c.get("looks", 0)) + 1
            c["rank"] = promote._rank(f"{fade.NAME}:{c['id']}")
        stats = promote._stats(trades, promote.z_look(c.get("looks", 1), len(tries), c.get("rank")))
        c["results"] = stats
        label = _label(c)
        if c.get("status") == "candidate" and promote._ready(stats):
            if cfg.get("auto_promote"):
                c.update(status="live", promoted_at=stamp,
                         note=f"won on paper: {stats['paper_trades']} trades, worst case {stats['roi_lo']:+.1%}")
                notes.append(f"Longshot fade {label} won on paper: {stats['paper_trades']} trades, worst case "
                             f"{stats['roi_lo']:+.1%}. {promote._go_live(cfg, fade.NAME)}")
            else:
                notes.append(f"Longshot fade {label} is ready for real money. Auto promote is off.")
        elif c.get("status") == "live":
            lost, dollars, n = promote._losing(trades)
            if lost:
                c.update(status="retired", retired_by="results", retired_at=stamp,
                         note=f"lost ${dollars:.2f} in its last {n} trades")
                notes.append(f"Longshot fade {label} stopped. It lost ${dollars:.2f} in its last {n} trades.")
    save_cells(cells)
    return notes
