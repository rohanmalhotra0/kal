# Working on kal

kal trades real money on Kalshi. The owner gave it $300 of capital. The bot may
reinvest its profits, but it must never lose more than that $300 in total.
`kal/ledger.py` enforces this; never weaken it.

## Rules for every change

- Money safety beats features. When unsure, make it paper-only or refuse.
- All money limits live in `kal/risk.py` (`Limits.refuse`) and `kal/ledger.py`.
  Strategies never place orders or bypass these.
- New strategies start in `paper` mode. Only `kal/research/promote.py` moves them to
  `live`, and only after they prove themselves on paper.
- Never change defaults that make the bot riskier (budget, caps, kelly_fraction,
  daily_loss_limit, auto_promote thresholds) without the owner saying so.
- Run `python -m unittest discover -s tests -t .` before every commit. Add a test for
  every bug fixed and every money-path change.
- Never commit secrets: the Kalshi key ID, `kalshi.pem`, `.env`, or GitHub tokens.
- Keep user-facing text short and plain. The owner has dyslexia and ADHD.

## Where things are

See `docs/ARCHITECTURE.md` for the map, `docs/RESEARCH.md` for strategy ideas and
the sizing rules, and `docs/DEPLOY.md` for the server.

## Check-ins (every 2 hours)

A scheduled Claude session checks on the bot. Each one should:

1. Read the latest report on the `status` branch of the private status repo named in
   `KAL_GITHUB_REPO` (`report.md`, `report.json`). It is never in this public repo.
2. If the bot tripped, find out why. Fix the cause in a PR. Never clear a trip itself.
3. Check CI and the `market-check` workflow on `main`. Fix real failures in a PR.
4. Pick the top untested idea in `docs/RESEARCH.md`. Research it, then build it as a
   paper-only strategy with tests, in a PR.
5. Log what was done in `docs/JOURNAL.md` (date, finding, PR link), newest first.

Code changes go through pull requests for the owner to merge. Don't push to `main`
directly, except for `docs/JOURNAL.md`.
