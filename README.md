# UFC Prediction Ecosystem

A modular, production-shaped Python pipeline that ingests multi-source MMA data,
builds deep matchup-aware fighter profiles, runs a round-by-round Monte Carlo
simulation, and outputs win probabilities, method-of-victory distributions, and
Kelly-based value-betting metrics.

## Pipeline

```
data_ingestion  →  feature_engineering  →  simulation_engine  →  analytics_reporting
   (raw stats)      (combat primitives)     (10k+ fight sims)      (JSON + CLI + Kelly)
                                    main.py orchestrates
```

| Module | Responsibility |
|--------|----------------|
| `data_ingestion.py` | Mock fetch pipelines + the canonical `FighterRawStats` / `MatchupOdds` schema and weight-class table. |
| `feature_engineering.py` | Turns raw stats into normalized, matchup-aware combat primitives (striking, grappling, chin, cardio, plus age/reach/stance/rust/travel/division modifiers). |
| `simulation_engine.py` | Round-by-round Monte Carlo: dominance scores, stamina decay, KO/submission finish triggers, and a 3-judge scorecard engine. |
| `analytics_reporting.py` | Aggregates sims into a detailed JSON schema + clean CLI tables; computes implied vs market odds and the Kelly Criterion stake. |
| `main.py` | CLI orchestrator tying it all together. |

## Setup & run

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

./.venv/bin/python main.py                      # demo matchup, 10k iterations
./.venv/bin/python main.py --iterations 50000   # tighter distribution
./.venv/bin/python main.py --rounds 3 --a-ml -120 --b-ml +100
./.venv/bin/python main.py --json-out report.json --json-only
```

CLI flags: `--a`, `--b` (fighter keys), `--iterations`, `--seed`, `--a-ml`, `--b-ml`
(market moneylines), `--rounds` (3/5), `--json-out`, `--json-only`.

## The demo matchup

Two deliberately archetypal fighters exercise every variable interaction:

- **Diego "Volume" Marquez** — high-volume southpaw pressure striker, elite/flat
  cardio, reach edge, KO power — but **poor submission defense** and a thinning chin.
- **Kenji "The Anaconda" Tanaka** — elite submission grappler with dominant control
  and takedowns — but an **aging chin** (36yo), steep cardio drop-off, and a
  **>365-day layoff** (ring rust).

The model resolves this as ~82/18 for Tanaka: he submits Marquez ~57% of the time
(exploiting the sub-defense hole), while Marquez's power still finds Tanaka's chin
for a ~15% KO share. ~17% reach the judges. Against a market line of Tanaka +140,
the sim flags him as a **value bet** (half-Kelly ≈ 20% of bankroll).

## Modeling notes & tuning levers

Every formula is commented at its definition. The highest-leverage knobs:

- **`feature_engineering.LEAGUE_AVG`** — normalization anchors ("average UFC fighter").
- **`feature_engineering.AGE_CLIFF` / `_age_factor`** — age-curve penalty, scaled by
  weight-class `lightness` in `data_ingestion.WEIGHT_CLASSES`.
- **`simulation_engine.KO_BASE` / `SUB_BASE`** — baseline per-round finish rates.
- **`KO_GAP_SENSITIVITY` / `SUB_GAP_SENSITIVITY` / `FINISH_GAP_CAP`** — how sharply
  finish odds respond to dominance gaps (capped so the exponential saturates).
- **`OFFENSE_COMPRESSION`** (in `_finish_probabilities`) — diminishing returns on
  elite offense primitives so one trait can't swamp the fight.
- **`BASE_ROUND_NOISE`** — core upset variance; ring-rust adds to it per fighter.
- **`BASE_STAMINA_BURN` / `_stamina_burn`** — cardio decay pace.
- **`TEN_EIGHT_GAP`** — dominance gap required for a 10-8 round.

## Output schema (JSON)

`win_probability`, `method_of_victory_matrix` (KO/TKO, Submission, Unanimous
Decision, Split/Majority Decision per fighter), `finish_round_distribution`, and
`value_betting` (market vs implied vs model probability, edge, full & half Kelly,
value flag) per side.

## Extending to live data

Each `fetch_*` in `data_ingestion.py` is the single seam to production data —
swap the mock bodies for a UFCStats/Sherdog scraper and a sportsbook odds feed;
nothing downstream changes as long as they return the same `FighterRawStats` /
`MatchupOdds` objects.
