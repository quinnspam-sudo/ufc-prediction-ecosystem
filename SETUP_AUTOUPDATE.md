# Auto-Update Setup

The ecosystem keeps itself current by polling live sources on a schedule,
re-running predictions when inputs change, and committing the results back to
GitHub. This doc covers how it works and the two things **you** need to wire up.

## How it works

```
cron-job.org  ──POST /dispatches──►  GitHub Action  ──►  python sync.py --push
   (schedule)                          (update.yml)             │
                                                                ▼
        ESPN results ┐                                   diff vs data_store/state.json
        The Odds API ┼─►  sources/  ─►  merge into store  ─►  re-predict changed matchups
        Google News  ┘                                        write reports/*.json
                                                                │
                                                                ▼
                                                        git commit + push
```

- **`sync.py`** is the single entrypoint each cycle. It loads `data_store/state.json`,
  runs each source, merges changes, and only re-simulates matchups whose inputs
  moved. If nothing changed, it commits nothing.
- **What auto-updates today:** fight results/status (ESPN), betting lines
  (The Odds API), and injury/weight/withdrawal flags (Google News keyword scan).
- **What's still seeded:** granular per-minute striking/grappling stats. UFCStats
  (the richest source) is behind a JavaScript bot-wall, so those numbers come from
  `watchlist.json` until a headless-browser fetcher is added (see "Upgrades").

## What you need to do

### 1. Add your Odds API key (enables live odds)

1. Get a free key at <https://the-odds-api.com> (free tier ≈ 500 requests/month;
   `sync.py` uses exactly **one** request per cycle).
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `ODDS_API_KEY`
   - Value: your key

Without it, the odds source skips cleanly and everything else still runs.

### 2. Set up the cron-job.org trigger (reliable schedule)

GitHub's own `schedule:` cron has fired unreliably on your account, so the primary
schedule comes from an external pinger — same pattern as your stock monitor.

1. Create a GitHub **Personal Access Token** with `repo` scope (or a fine-grained
   token with **Contents: Read/write** on this repo). You can reuse your existing
   PAT if it still has the scope.
2. At <https://cron-job.org> create a cron job:
   - **URL:** `https://api.github.com/repos/quinnspam-sudo/ufc-prediction-ecosystem/dispatches`
   - **Method:** `POST`
   - **Headers:**
     - `Authorization: Bearer YOUR_PAT`
     - `Accept: application/vnd.github+json`
     - `Content-Type: application/json`
   - **Body:** `{"event_type":"refresh"}`
   - **Schedule:** whatever cadence you want (e.g. every 30 min during fight week,
     hourly otherwise). This is the real "how often does it update" knob.

Test it immediately with:

```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_PAT" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/quinnspam-sudo/ufc-prediction-ecosystem/dispatches \
  -d '{"event_type":"refresh"}'
```

A `204 No Content` means the Action was triggered — watch it under the repo's
**Actions** tab. You can also click **Run workflow** there manually anytime.

> Heads-up: your notes show a PAT expiring **2026-08-07**. If you reuse it, the
> auto-update stops when it expires — rotate it before then.

## Tuning what's tracked

Edit **`watchlist.json`** to control which fighters and matchups are monitored.
Real fighters carry approximate granular stats (`stats_approx: true`); replace those
blocks with real numbers as you get them. The live sources keep results/odds/health
current regardless.

## Running it manually

```bash
python sync.py --no-network            # deterministic local test (no live calls)
python sync.py                          # live fetch + predict, no commit
python sync.py --push                   # live fetch + predict + commit/push
python sync.py --force                  # re-predict every matchup
python sync.py --iterations 20000       # more Monte Carlo iterations
```

## Upgrades (documented next steps)

- **Granular UFCStats numbers:** add a Playwright/headless-browser fetcher (runs
  fine on the GitHub Actions runner) to get past the JS bot-wall and refresh
  SLpM/SApM/TD/etc. Slot it in as another `sources/` adapter — nothing else changes.
- **Injury precision:** the Google News scan is a proximity keyword heuristic and
  will occasionally mis-flag. Each flag stores its source headline in `injury_note`
  so you can eyeball it. Tighten `sources/injury_news.py` patterns as needed.
