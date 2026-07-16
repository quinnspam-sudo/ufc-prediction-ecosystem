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
| `main.py` | CLI orchestrator for a single ad-hoc matchup. |
| `sync.py` | **Auto-update** orchestrator: poll sources → diff → re-predict changed matchups → commit to GitHub. |
| `sources/` | Pluggable live-data adapters (ESPN results, The Odds API, Google News injury scan). |
| `data_store.py` | Persistent fighter/matchup state + changelog + change detection. |
| `watchlist.json` | Which fighters/matchups to track. |

## Budget safety ($0, always)

Every external call is capped so the system can never incur a charge or exhaust a
free tier, no matter how often the scheduler fires. All knobs live in
[config.py](config.py) (env-overridable).

| Source | Free limit | Protection |
|--------|-----------|------------|
| **The Odds API** | 500 req/month | Hard monthly ceiling (450) **+** min 4h between calls **+** only when a card is ≤10 days out. Any one keeps us under 500; combined, max ≈186/month. |
| **Google News** | soft IP block | Max 8 fighters/cycle, 12h per-fighter cooldown, skipped when no card ≤21 days out. |
| **ESPN JSON** | none (be polite) | Scoreboard fetched **once per cycle**, shared by discovery + results. |
| **GitHub Actions** | unlimited on public repos | n/a — free. |

The odds source also reads The Odds API's `x-requests-remaining` header each call and
logs it, so you always know the true remaining quota.

## Adaptive calibration (gets better over time)

[calibration.py](calibration.py) makes the system self-correcting:
1. Every real-stat prediction is logged with its model probability.
2. When ESPN reports the result, the fight is scored with the **Brier score**
   (0.25 = coin-flip; lower is better).
3. Each cycle it refits a **confidence-shrink factor λ** that minimises historical
   Brier and tempers predictions toward 50/50 if the model has been over-confident.
   λ only activates after 15 resolved fights (no over-tuning on noise), and is
   **walk-forward validated**: fitted on the first 80% of resolved fights
   (chronologically — never shuffled), applied only if it beats no-shrink on the
   held-out final 20%. In-sample-only calibration is a known trap
   (r/algobetting: it consistently degraded a 5-year UFC model's live ROI).

The live scorecard is published to `reports/calibration.json` (accuracy, raw vs
calibrated Brier, current λ). This is the foundation for future weight auto-tuning.

## Accuracy upgrades (community-research pass, 2026-07-15)

Findings from r/algobetting / r/MMAbetting modelers, implemented:

| Upgrade | Where | What it does |
|---------|-------|--------------|
| **Consensus forecast + confirmation gate** | `analytics_reporting.py`, `MARKET_BLEND_MODEL_WEIGHT` in [config.py](config.py) | The model's pick stays PURE (no market input). Separately, a `consensus_forecast` (40% model + 60% devigged market) is reported as the best-accuracy estimate, and acts as a confirmation gate: `value_bet` fires only when the model sees an edge AND that edge survives the market-blended consensus. Model-only edges the market rejects are flagged `◇ model only`, informational not actionable. Only real odds participate, never the -110/-110 placeholder. |
| **Bayesian finish-rate shrink** | `_finish_propensity` in [feature_engineering.py](feature_engineering.py) | Career KO/sub win rates are shrunk toward divisional base rates (`ko_base`/`sub_base` in `WEIGHT_CLASSES`) with 5 pseudo-wins, then tilt the sim's KO/sub triggers. A 3-for-3 KO record no longer reads as a 100% finisher. Sharpens the method-of-victory matrix and finish-round distribution. |
| **Venue elevation** | `_elevation_adjustments` + `venue_elevation_ft`/`trains_at_altitude` fields | Above 3,000 ft, non-altitude-trained fighters lose up to 12% cardio pool and fade faster (Denver ≈ half tax, Mexico City ≈ near-full). |
| **Stat time-decay** | `_staleness_trust` | Offensive primitives regress toward league average as a fighter's most recent data ages (full trust ≤3 yrs, bottoming at −40% by ~10 yrs). |
| **Walk-forward λ validation** | [calibration.py](calibration.py) | See above. |
| **External benchmarking** | [benchmark.py](benchmark.py) | Compare our probabilities to any external model (e.g. the open-sourced `DanMcInerney/mma-ai`, ~8% ROI since 2024) and Brier-score both on resolved bouts. Disagreements are the improvement signal. |
| **Finish-count scraping** | [sources/ufcstats.py](sources/ufcstats.py) | The fighter-page scrape now also parses the fight-history table (same page, zero extra requests) into `career_wins/ko_wins/sub_wins`, feeding the Bayesian finish-rate shrink real data. |

## Parlay recommendations

[parlay_engine.py](parlay_engine.py) builds per-card parlays from the reports
every sync cycle → `reports/parlays.json` (+ CLI section in `sync.py` output).

* **Fight Night**: a 3-leg and a 5-leg parlay. **Numbered/PPV card** (title
  matches `UFC <number>`): one 4–6 leg parlay.
* A fight only becomes a leg when: both fighters have REAL stats, real market
  odds exist, the model pick and market-blended consensus AGREE on the side,
  and the consensus probability ≥ 60%. Legs rank by consensus probability.
* Each parlay reports combined probability, combined odds, and EV per unit at
  quoted prices; each leg is annotated with its most-likely method and modal
  finish round so upgrading a leg to a method/round prop is a one-look call.
* Empty parlays on a card mean the quality bar wasn't met (usually fighters
  still on placeholder stats) — the engine never pads with junk legs.

## Auto-update

The system keeps itself current: a scheduled job polls live sources, and any
change (fight result, line move, injury/weight news) re-runs the affected
prediction and commits the new report to GitHub. See
**[SETUP_AUTOUPDATE.md](SETUP_AUTOUPDATE.md)** for the full wiring (Odds API key +
cron-job.org trigger).

```bash
python sync.py --no-network     # deterministic local test
python sync.py                  # live fetch + predict (no commit)
python sync.py --push           # live fetch + predict + commit/push
```

What auto-updates today: **fight results/status** (ESPN JSON API), **betting odds**
(The Odds API), and **injury/weight/withdrawal flags** (Google News keyword scan,
approximate — every flag keeps its source headline for verification). Granular
per-minute striking/grappling stats stay seeded from `watchlist.json` because
UFCStats is behind a JS bot-wall; adding a headless-browser fetcher is a documented
next step.

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
