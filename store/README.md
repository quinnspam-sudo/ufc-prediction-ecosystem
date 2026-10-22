# Point-in-time feature store

Every UFC fight since 1994 with per-round stats, and every fighter's stats **as
they stood before each fight**. This is gate zero from the design roundtable: the
old backtest used today's career averages, so its 68.1% winner accuracy leaked
the future. Anything trained or backtested from here on reads this store.

```bash
./.venv/bin/python -m store.ingest      # download mirror CSVs, build data_store/history.sqlite (~1s)
./.venv/bin/python -m store.snapshots   # build fighter_snapshots (~1s)
./.venv/bin/python -m store.check       # leakage gates + accuracy vs UFCStats (~10s)
```

## Source

[Greco1899/scrape_ufc_stats](https://github.com/Greco1899/scrape_ufc_stats), a public
weekly mirror of UFCStats (events, results, per-round stats, biometrics).
UFCStats itself sits behind a JavaScript bot wall, so the mirror spares us a
headless-browser crawl of ~9,000 fight pages. It lags by up to a week, which
doesn't matter for prediction: a pre-fight snapshot only needs past fights.
Raw CSVs and the SQLite file are gitignored and rebuild from scratch.

## Tables (`data_store/history.sqlite`)

| Table | One row per | Notes |
|---|---|---|
| `fighters` | fighter | UFCStats id, height, reach, weight, stance, DOB |
| `events` | event | date, location |
| `fights` | bout | winner, method (KO/TKO, SUB, DEC, OTHER, NC), end round/time, total fight seconds, scheduled rounds, weight class, title/women flags |
| `round_stats` | fighter × round | KD, sig/total strikes, TD, sub attempts, reversals, control, target and position splits (landed + attempted) |
| `fighter_snapshots` | fighter × bout | that fighter's state going **into** the bout |

`fighter_snapshots` holds record and finish splits, streak, last 3 results,
layoff, age, title and five-round experience, raw stat totals, and UFCStats-defined
rates (`slpm`, `sapm`, `str_acc`, `str_def`, `td_avg`, `td_acc`, `td_def`,
`sub_avg`, `ctrl_pct`). Each rate also has a `_s` version shrunk toward the
division's pooled rate *as of that date* (K constants in `snapshots.py`).

For an upcoming card, `snapshots.state_as_of(card_date, fighter_ids, weight_class)`
replays the same code path, so training rows and live predictions can't disagree.

## Leakage rules (enforced by `store.check`)

1. **Past only.** On each date, snapshots are taken before that date's fights are
   added. Records are re-derived in SQL and must match.
2. **Future invariance.** Deleting every fight on or after a cutoff (2010, 2018,
   2024) must leave all earlier snapshots identical, division priors included.
3. **Accuracy.** Spot-checked against live UFCStats pages (Dvalishvili and
   Makhachev match to two decimals) and against the curated stats in `state.json`.

## Known limits

- Nine names belong to two people. Those resolve by weight class and are flagged
  `fights.identity_guess` (52 bouts).
- 22 early-UFC bouts have no round stats. They count toward records, not rates.
- 430 old-era round rows have no control time. It's counted as 0, so pre-2000s `ctrl_pct` runs low.
- Fighters who fought twice on one night (early tournaments) don't see their
  same-night bouts in either snapshot.
- Not built yet (roundtable step 3): opponent-adjusted rates, Glicko-2, recency
  weighting, cardio decay.
