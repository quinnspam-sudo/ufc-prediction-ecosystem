"""
main.py
=======
Central orchestrator for the UFC Prediction Ecosystem.

Pipeline:
    data_ingestion  ->  feature_engineering  ->  simulation_engine
                    ->  analytics_reporting  ->  CLI + JSON output

Usage:
    python main.py                       # run the demo matchup, 10k iters
    python main.py --iterations 50000    # more iterations = tighter distribution
    python main.py --json-out report.json
    python main.py --a marquez --b tanaka
"""

from __future__ import annotations

import argparse
import sys
from typing import Dict

from data_ingestion import (
    FighterRawStats,
    MatchupOdds,
    fetch_fighter_stats,
    fetch_market_odds,
)
from feature_engineering import build_matchup
from simulation_engine import run_simulation
from analytics_reporting import build_report, render_cli, report_to_json


def run_pipeline(
    a_raw: FighterRawStats,
    b_raw: FighterRawStats,
    odds: MatchupOdds,
    iterations: int,
    seed: int,
) -> Dict:
    """Execute the full ecosystem end-to-end and return the report dict."""
    # 1. Feature engineering — raw stats -> matchup-aware combat profiles.
    profile_a, profile_b = build_matchup(a_raw, b_raw, odds.scheduled_rounds)

    # 2. Monte Carlo simulation — round-by-round, N iterations.
    sim = run_simulation(
        profile_a, profile_b,
        scheduled_rounds=odds.scheduled_rounds,
        iterations=iterations,
        seed=seed,
    )

    # 3. Aggregate into the JSON report + value metrics.
    return build_report(sim, odds)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="UFC Prediction Ecosystem")
    p.add_argument("--a", default="marquez",
                   help="Fighter A key from the mock dataset (default: marquez)")
    p.add_argument("--b", default="tanaka",
                   help="Fighter B key from the mock dataset (default: tanaka)")
    p.add_argument("--iterations", type=int, default=10_000,
                   help="Monte Carlo iterations (>=10,000 recommended)")
    p.add_argument("--seed", type=int, default=42, help="RNG seed")
    p.add_argument("--a-ml", type=int, default=None,
                   help="Override Fighter A market moneyline (American)")
    p.add_argument("--b-ml", type=int, default=None,
                   help="Override Fighter B market moneyline (American)")
    p.add_argument("--rounds", type=int, choices=(3, 5), default=None,
                   help="Override scheduled rounds (3 or 5)")
    p.add_argument("--json-out", default=None,
                   help="Write the full JSON report to this path")
    p.add_argument("--json-only", action="store_true",
                   help="Print only JSON to stdout (suppress the CLI tables)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    fighters = fetch_fighter_stats()
    if args.a not in fighters or args.b not in fighters:
        print(f"Unknown fighter key. Available: {list(fighters)}", file=sys.stderr)
        return 2

    a_raw, b_raw = fighters[args.a], fighters[args.b]

    # Odds: start from the mock feed, apply any CLI overrides.
    odds = fetch_market_odds()
    if args.a_ml is not None:
        odds.fighter_a_moneyline = args.a_ml
    if args.b_ml is not None:
        odds.fighter_b_moneyline = args.b_ml
    if args.rounds is not None:
        odds.scheduled_rounds = args.rounds

    report = run_pipeline(a_raw, b_raw, odds, args.iterations, args.seed)

    if args.json_out:
        with open(args.json_out, "w") as fh:
            fh.write(report_to_json(report))
        print(f"JSON report written to {args.json_out}", file=sys.stderr)

    if args.json_only:
        print(report_to_json(report))
    else:
        print(render_cli(report))
        print("\n▸ FULL JSON REPORT")
        print(report_to_json(report))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
