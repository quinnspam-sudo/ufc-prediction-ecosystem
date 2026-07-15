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
"""

from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
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

# ── Simulation ────────────────────────────────────────────────────────────
# Iterations for a normal prediction. Placeholder-stat fights use fewer (their
# ~50/50 output doesn't need precision) to save runner time. Free on public
# repos, but courteous and faster.
SIM_ITERATIONS = _int("SIM_ITERATIONS", 10_000)
SIM_ITERATIONS_PLACEHOLDER = _int("SIM_ITERATIONS_PLACEHOLDER", 2_000)
