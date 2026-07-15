# RESUME — UFC Prediction Ecosystem (handoff state)

**Read this first.** It's the single source of truth for picking up work on this
project in a fresh session. Last updated: 2026-07-15.

---

## What this is
A modular, self-updating UFC fight-prediction system. It ingests live MMA data,
runs a round-by-round Monte Carlo simulation per matchup, and outputs win
probabilities, method-of-victory, and Kelly value-bet metrics. It auto-updates on
a schedule and commits fresh reports to GitHub. **Hard constraint: it must run at
$0 forever** — never introduce anything that can exceed a free tier.

## Where it lives
- **Local:** `/Users/quinnmccarn/Claude/UFC predicitions/` (note the folder-name
  typo "predicitions" — intentional, keep it).
- **GitHub:** https://github.com/quinnspam-sudo/ufc-prediction-ecosystem  (**PUBLIC**)
- **gh account:** `quinnspam-sudo` (pseudonymous — see Conventions).
- **Python:** use the venv → `./.venv/bin/python` (deps: numpy, requests,
  beautifulsoup4, lxml). Recreate with `python3 -m venv .venv && ./.venv/bin/pip
  install -r requirements.txt` if missing.

## Current status — FULLY LIVE ✅
- Scheduler works: cron-job.org card "UFC Cron" POSTs a `repository_dispatch`
  (`event_type: refresh`) → GitHub Action `update.yml` runs `sync.py --push`.
- `ODDS_API_KEY` secret **is set**; odds source pulls real consensus lines
  (~492/500 requests remaining this month as of last check).
- Auto-discovery pulls the entire upcoming ESPN card each cycle (11 bouts for the
  2026-07-18 Du Plessis vs Usman card).
- Budget guards verified in the cloud; calibration ledger active.

## Architecture (file map)
| File | Role |
|------|------|
| `data_ingestion.py` | `FighterRawStats`/`MatchupOdds` schema, weight classes, mock demo fighters (Marquez/Tanaka, used only by `main.py`). |
| `feature_engineering.py` | Raw stats → combat primitives + age/reach/stance/rust/**health** modifiers. |
| `simulation_engine.py` | Round-by-round Monte Carlo, finish triggers, 3-judge scorecards. |
| `analytics_reporting.py` | JSON + CLI tables + Kelly value metrics. |
| `main.py` | One-off ad-hoc matchup CLI. |
| `sync.py` | **Auto-update orchestrator** (the entrypoint CI runs). |
| `config.py` | **All budget/behavior caps** (env-overridable). |
| `calibration.py` | Brier scoring + adaptive confidence shrink. |
| `data_store.py` | Persistent state, name-dedup, discovery merge. |
| `sources/espn_common.py` | Shared ESPN scoreboard fetch (once/cycle). |
| `sources/espn_schedule.py` | Auto-discovers the upcoming card. |
| `sources/espn_results.py` | Fight results/status → calibration + layoff. |
| `sources/odds_api.py` | The Odds API (throttled). |
| `sources/injury_news.py` | Google News injury/weight scan (bounded). |
| `watchlist.json` | Curated fighter overrides (real stats reused via dedup). |
| `data_store/state.json` | Persistent known state (tracked in git). |
| `data_store/predictions_log.json` | Calibration ledger (tracked). |
| `reports/*.json` | Per-matchup reports + `index.json` + `calibration.json`. |

## Budget guards (the $0 guarantee) — do NOT weaken these
All in `config.py`, env-overridable. Every external call is capped:
- **Odds API (500/mo free):** hard `ODDS_MONTHLY_CAP=450` + `ODDS_MIN_INTERVAL_HOURS=4`
  + `ODDS_LOOKAHEAD_DAYS=10` (only when a card is near). Max ~186/mo even if cron
  fires every minute. Throttle state persists across CI runs (spending an API call
  forces a commit). The source logs `x-requests-remaining`.
- **Google News:** `NEWS_MAX_PER_CYCLE=8`, `NEWS_MIN_INTERVAL_HOURS=12`,
  `NEWS_LOOKAHEAD_DAYS=21`.
- **ESPN:** one shared scoreboard fetch per cycle.
- **GitHub Actions:** free/unlimited on public repos.
> Rule for any future change: if it adds or increases an external call, it must go
> through a `config.py` cap and respect the "spending budget forces a commit" rule.

## Adaptive calibration
`calibration.py` logs each real-stat prediction, scores it via **Brier** when ESPN
reports the result, and refits a confidence-shrink λ (activates only after
`MIN_RESOLVED=15` fights). Scorecard: `reports/calibration.json`.

## Key limitation (drives the top priority)
UFCStats (richest granular per-minute stats) is behind a **JavaScript bot-wall**, so
plain HTTP can't read it. Auto-discovered fighters therefore use **league-average
placeholder stats** (`needs_real_stats: true`), which produce ~50/50 predictions.
Their value flags are **deliberately suppressed** (`insufficient_data`) so the system
never emits fake bet signals. Only curated fighters in `watchlist.json` (currently
Du Plessis, Usman) have real stats and trustworthy outputs.

## THE NEXT STEP (highest-value, still $0)
**Build a headless-browser (Playwright) UFCStats fetcher** that runs on the free
GitHub Actions runner to pull real per-fighter stats, replacing the placeholders.
This is the single biggest accuracy unlock — it would light up trustworthy
predictions + value flags across the whole card. Design it as one more `sources/`
adapter (`sources/ufcstats.py`) that patches real stats onto discovered fighters
and clears `needs_real_stats`. Respect budget rules (Playwright is free on the
runner; add a per-fighter refresh cooldown so we don't re-scrape every cycle).

## Other backlog (optional)
- Human-readable `reports/SUMMARY.md` regenerated each cycle (today's value bets).
- Silence the Node.js-20 deprecation warning by bumping action versions in
  `.github/workflows/update.yml`.
- Set the cron-job.org interval (throttle makes any interval budget-safe; ~30–120
  min is fine for fresh results/injuries).

## Conventions a new session MUST follow
- **$0 budget is non-negotiable.** Never add an uncapped external call.
- **Public repo + pseudonymous account:** never commit secrets or Quinn's real
  email. Commit author identity is **`Quinn McCarn
  <quinnspam-sudo@users.noreply.github.com>`** — do NOT use the gmail (history was
  scrubbed to remove it). Keys live only in GitHub Actions secrets / env.
- Use `./.venv/bin/python` for everything.
- Test deterministically with `./.venv/bin/python sync.py --no-network`; live runs
  hit real APIs (odds throttle protects the budget).
- End commit messages with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- The scheduler PAT (in cron-job.org) **expires 2026-08-14** — updates stop then;
  rotate a `repo`-scoped token before that.

## Run / verify quickly
```bash
cd "/Users/quinnmccarn/Claude/UFC predicitions"
./.venv/bin/python sync.py --no-network              # deterministic dry run
./.venv/bin/python main.py                           # ad-hoc demo matchup
cat reports/index.json reports/calibration.json      # current outputs
gh run list --workflow="UFC Auto-Update" --limit 3   # recent CI runs
```
