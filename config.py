"""
config.py
=========
Central budget & behavior knobs. THE GOVERNING PRINCIPLE: this system must run
at $0 forever. Every value here exists to keep us safely inside a free tier, no
matter how often the scheduler fires. All are overridable via environment
variables so you can tune without editing code.

Why each cap exists
-------------------
* The Odds API free tier = 500 requests/month. We treat 450 as the hard ceiling
  (buffer for manual runs) AND rate-limit to at most one call every few hours
  AND only when a card is actually near. Any ONE of these alone keeps us under
  500; together they make an overage essentially impossible.
* Google News RSS has no published quota but WILL soft-block an IP that hammers
  it. Auto-discovery can add 20+ fighters, so we bound news to a handful per
  cycle with a per-fighter cooldown.
* ESPN's unofficial JSON has no key but shouldn't be spammed; we fetch its
  scoreboard once per cycle and share it.
* UFCStats has no quota either, but a headless-browser scrape costs real
  runner CPU/time per fighter, so we cap fighters-per-cycle and add a
  per-fighter cooldown to keep it a light, polite scrape.
"""

from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ── The Odds API budget guards ────────────────────────────────────────────
# Hard monthly ceiling (free tier is 500; leave headroom for manual runs).
ODDS_MONTHLY_CAP = _int("ODDS_MONTHLY_CAP", 450)
# Minimum hours between odds calls, regardless of how often the job runs.
# 4h → ≤6/day → ≤186/month even if cron fires every minute.
ODDS_MIN_INTERVAL_HOURS = _int("ODDS_MIN_INTERVAL_HOURS", 4)
# Only spend an odds call if the next scheduled card is within this many days.
# No point pricing a fight three weeks out; saves the budget for fight week.
ODDS_LOOKAHEAD_DAYS = _int("ODDS_LOOKAHEAD_DAYS", 10)

# ── Google News (injury/health) request guards ────────────────────────────
# Max fighters to query for news in a single cycle (bounds RSS request burst).
NEWS_MAX_PER_CYCLE = _int("NEWS_MAX_PER_CYCLE", 8)
# Per-fighter cooldown: don't re-query the same fighter more often than this.
NEWS_MIN_INTERVAL_HOURS = _int("NEWS_MIN_INTERVAL_HOURS", 12)
# Skip news entirely when no card is within this many days (nothing urgent).
NEWS_LOOKAHEAD_DAYS = _int("NEWS_LOOKAHEAD_DAYS", 21)

# ── UFCStats scraper (Playwright, headless-browser) guards ────────────────
# No published quota (unofficial site, no key) — these exist purely to be a
# polite scraper and to bound GH Actions runner minutes, not to protect $.
# Max fighters to scrape for real stats in a single cycle.
UFCSTATS_MAX_PER_CYCLE = _int("UFCSTATS_MAX_PER_CYCLE", 6)
# Per-fighter cooldown: don't re-scrape the same fighter more often than this
# (real stats barely move between cycles; also caps retries on a miss/no-match).
UFCSTATS_MIN_INTERVAL_HOURS = _int("UFCSTATS_MIN_INTERVAL_HOURS", 24)

# ── Simulation ────────────────────────────────────────────────────────────
# Iterations for a normal prediction. Placeholder-stat fights use fewer (their
# ~50/50 output doesn't need precision) to save runner time. Free on public
# repos, but courteous and faster.
SIM_ITERATIONS = _int("SIM_ITERATIONS", 10_000)
SIM_ITERATIONS_PLACEHOLDER = _int("SIM_ITERATIONS_PLACEHOLDER", 2_000)

# ── Market blend ──────────────────────────────────────────────────────────
# Weight given to the MODEL when blending the simulated win probability with
# the devigged market-implied probability. 0.0 = consensus is the market alone;
# 1.0 disables blending. Blending only happens when the matchup carries REAL
# market odds (not the -110/-110 placeholder).
#
# Set to 0.0 on 2026-10-21 after the point-in-time backtest (backtest_pit.py,
# reports/backtest_pit.json): on 862 fights with closing lines, the honest
# model added nothing to the market, and the old 0.4 weight made the consensus
# WORSE than the market alone (Brier 0.201 vs 0.196). The earlier "blend beats
# market" result came from lookahead-leaked stats. Raise this only after a
# model beats the market on point-in-time data.
MARKET_BLEND_MODEL_WEIGHT = _float("MARKET_BLEND_MODEL_WEIGHT", 0.0)

# Kill switch for every model-derived betting signal: value_bet flags and the
# parlay engine. On while no model beats the market on the point-in-time
# backtest (same 2026-10-21 finding: the model picked a different winner than
# the market on 268 fights and was right on 38% of them). Set
# MODEL_EDGE_SUSPENDED=0 to re-enable once one does.
MODEL_EDGE_SUSPENDED = os.environ.get("MODEL_EDGE_SUSPENDED", "1") != "0"
MODEL_EDGE_SUSPENDED_NOTE = (
    "Value bets and parlays suspended: on point-in-time stats the model "
    "doesn't beat the betting market (see reports/backtest_pit.json).")

# ── Calibration prior ─────────────────────────────────────────────────────
# Confidence-shrink λ to use BEFORE the live ledger has enough resolved
# fights to fit its own (and as the fallback when a live fit fails holdout
# validation). Re-fitted on the 1,152-fight backtest since UFC 300 after the
# 2026-07-21 finish/divisional/TDD recalibration and the 2026-07-27 KO/SUB
# base correction (which also brought the method-of-victory finish_scale fit
# to 1.0, i.e. calibrated): optimal prior shrink is 0.7, backtest Brier@λ
# ~0.209. 1.0 disables the prior.
CALIBRATION_PRIOR_SHRINK = _float("CALIBRATION_PRIOR_SHRINK", 0.7)
