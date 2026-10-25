"""Cross-venue research on real data (idea #11): do Polymarket's prices tell us when
Kalshi's are wrong?

Public data only: Kalshi's settled game markets and candles, Polymarket's game-winner
markets and price history. No key, no orders. kal/research/xvenue.py has the rules.
Prints the games matched per league (with examples), whose price was closer to the
results (Brier score), and what buying Kalshi where Polymarket disagreed would have made
after Kalshi's fee. Saves a summary to research/xvenue.json and every priced market to
cache/xvenue_rows.json.

  python tools/xvenue_research.py --days 90
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kal.client import Kalshi  # noqa: E402
from kal.research import backtest, xvenue  # noqa: E402
from kal.research.discover import research_dir  # noqa: E402

DAY = 86400


def pct(x):
    return "   n/a" if x is None else f"{x:+6.1%}"


def num(x, f="{:.4f}"):
    return "   n/a" if x is None else f.format(x)


def load(client, poly, names, since, now, say):
    """(pairs, {league: counts}) for the leagues named: Kalshi games whose scheduled end
    falls in [since, now], matched to Polymarket games."""
    sports = poly.sports()
    pairs, counts = [], {}
    for name in names:
        series, code = xvenue.LEAGUES[name]
        try:
            kg = [g for g in xvenue.kalshi_games(client.settled_markets(series), name, series)
                  if since <= g["end"] <= now]
            sport = sports.get(code)
            listed = poly.league_markets(sport, since - xvenue.LISTED_BEFORE, now) if sport else []
            pg = [g for g in xvenue.poly_games(listed, poly.teams(code)) if since - DAY <= g["start"] <= now]
            got, why = xvenue.match(kg, pg, code)
        except Exception as e:  # one league failing shouldn't sink the others
            say(f"{name}: skipped: {e}")
            continue
        counts[name] = {"kalshi": len(kg), "polymarket": len(pg), "matched": len(got), "why": dict(why)}
        say(f"{name:11} {len(kg):6} {len(pg):10} {len(got):8}")
        pairs += got
    return pairs, counts


def example(kg, pg, sides):
    k = ", ".join(s["name"] for s in kg["sides"])
    p = ", ".join((sides[s["market"]["ticker"]]["names"] or ["?"])[0] for s in kg["sides"])
    return f"{kg['event']} ({k})  =  {pg['slug']} ({p})"


def main(argv=None, client=None, poly=None, now=None, say=print):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=float, default=90, help="games whose scheduled end is this recent")
    ap.add_argument("--hours", type=float, default=6, help="decide this long before the scheduled end")
    ap.add_argument("--leagues", nargs="+", default=list(xvenue.LEAGUES), choices=list(xvenue.LEAGUES))
    args = ap.parse_args(argv)
    t0, now = time.time(), now or time.time()
    client, poly = client or Kalshi(), poly or xvenue.Polymarket()

    say(f"Game winners on Kalshi and Polymarket, last {args.days:g} days, decided {args.hours:g} hours "
        "before Kalshi's scheduled end.\n")
    say("league      Kalshi Polymarket  matched")
    pairs, counts = load(client, poly, args.leagues, now - args.days * DAY, now, say)
    say(f"{'all':11} {sum(c['kalshi'] for c in counts.values()):6} "
        f"{sum(c['polymarket'] for c in counts.values()):10} {len(pairs):8}")
    left = {}
    for c in counts.values():
        for k, v in c["why"].items():
            left[k] = left.get(k, 0) + v
    say("Kalshi games: " + "; ".join(f"{k}: {v}" for k, v in sorted(left.items())))
    shown, examples = {}, []
    for kg, pg, sides in pairs:
        if shown.get(kg["league"], 0) < 2:
            shown[kg["league"]] = shown.get(kg["league"], 0) + 1
            examples.append(f"{kg['league']}: {example(kg, pg, sides)}")
    say("\nExamples:\n  " + "\n  ".join(examples))

    say(f"\nReading both venues' prices at the decision time for {sum(len(k['sides']) for k, _, _ in pairs)} markets...")
    rows, why = xvenue.price_rows(client, poly, pairs, args.hours, progress=say)
    busy = xvenue.busy(rows)
    agree, differ = xvenue.agreement(rows)
    say(f"\n{len(rows)} markets priced on both venues at the decision time ({len(busy)} busy: "
        f"{xvenue.BUSY}+ contracts in the day before). Left out: "
        + ("; ".join(f"{k}: {v}" for k, v in sorted(why.items())) or "none") + ".")
    say(f"Both venues settled the same way on {agree} of {agree + differ} markets with a clear result on both "
        "(a check on the matching; no test uses Polymarket's result).")
    out = xvenue.results(rows)

    say("\nWho was closer to the results? Brier score, lower is better.")
    say("group    markets games  Kalshi mid  Polymarket  Kalshi - Poly (95% range)")
    for name in out["groups"]:
        b = out["brier"][name]
        if b:
            rng = "" if b["lo"] is None else f" ({b['lo']:+.4f} to {b['hi']:+.4f})"
            say(f"{name:8} {b['markets']:7} {b['games']:5}  {b['kalshi']:10.4f}  {b['poly']:10.4f}  "
                f"{b['diff']:+.4f}{rng}")
    b = out["brier_busy"].get("ALL")
    if b:
        say(f"busy only: {b['markets']} markets, Kalshi mid {b['kalshi']:.4f}, Polymarket {b['poly']:.4f}")
    for g in xvenue.gap_shares(rows):
        say(f"Polymarket outside Kalshi's bid-ask by more than {g['margin'] * 100:.0f}c: {g['share']:.1%} of markets")

    say("\nBuy Kalshi when Polymarket disagrees (busy markets, Kalshi's taker fee paid):")
    say("group    margin side trades events  win    ROI   worst  edge?")
    for r in out["trades"]:
        if r["trades"]:
            say(f"{r['group']:8} {r['margin'] * 100:4.0f}c  {r['side']:4} {r['trades']:6} {r['events']:6} "
                f"{r['win']:5.0%} {pct(r['roi'])} {pct(r['roi_lo'])}  {'EDGE' if r['edge'] else ''}")
    say(f"\nz = {out['z']:.2f} for {out['tries']} tries (every margin x side x group). Worst = a cautious 95% "
        "bound over game-days; below 0 means it could be luck. One look: an EDGE here would still need "
        f"paper trading first. {(time.time() - t0) / 60:.0f} min.")

    summary = {"days": args.days, "hours_before_end": args.hours, "leagues": counts, "matched": len(pairs),
               "markets": len(rows), "busy": len(busy), "left_out": dict(why), "agree": agree, "differ": differ,
               "examples": examples, **out}
    os.makedirs(research_dir(), exist_ok=True)
    path = os.path.join(research_dir(), "xvenue.json")
    with open(path, "w") as f:
        json.dump(summary, f, indent=1)
    # Every priced market, for a closer look. In cache/, which the bot never loads.
    rows_path = os.path.join(backtest.folder("cache"), "xvenue_rows.json")
    with open(rows_path, "w") as f:
        json.dump(rows, f)
    say(f"Saved {path} and {rows_path}")
    return summary


if __name__ == "__main__":
    main()
