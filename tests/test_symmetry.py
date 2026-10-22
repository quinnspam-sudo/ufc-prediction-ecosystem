"""
tests/test_symmetry.py
======================
The simulator must not care which fighter is in the A slot.

    ./.venv/bin/python tests/test_symmetry.py      # ~40s, exits 1 on failure

1. Identical fighters: P(A wins) within 3 standard errors of 50%. A fixed
   A-first finish order once gave 50.4% here (fixed 2026-10-22).
2. Profiles are slot-independent: build_matchup(x, y) builds the same
   profile for x as build_matchup(y, x) does.
3. Swap test on varied synthetic matchups: P(x wins) with x in A minus
   P(x wins) with x in B averages to zero within 3 standard errors.
   Each order uses its own seed — reusing one seed for both orders couples
   the runs and can fake (or hide) a bias.
"""

from __future__ import annotations

import dataclasses
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_ingestion import FighterRawStats  # noqa: E402
from feature_engineering import build_matchup  # noqa: E402
from simulation_engine import run_simulation  # noqa: E402


def _fighter(name: str, rng: np.random.Generator | None = None) -> FighterRawStats:
    """A league-average lightweight, or a randomised one when rng is given."""
    r = (lambda lo, hi, mid: float(rng.uniform(lo, hi))) if rng is not None \
        else (lambda lo, hi, mid: mid)
    wins = int(r(3, 15, 7))
    return FighterRawStats(
        name=name, age=int(r(24, 37, 30)), height_in=r(66, 74, 70),
        reach_in=r(66, 78, 72), weight_class="Lightweight", stance="Orthodox",
        slpm=r(2.5, 6.5, 4.2), strike_acc=r(0.38, 0.58, 0.46),
        sapm=r(2.5, 5.5, 3.6), strike_def=r(0.45, 0.65, 0.56),
        td_avg=r(0.0, 4.5, 1.4), td_acc=r(0.2, 0.55, 0.38),
        td_def=r(0.45, 0.9, 0.65), sub_avg=r(0.0, 1.5, 0.5),
        career_fights=wins + 3, career_wins=wins,
        career_ko_wins=int(r(0, wins, 3)) if rng is not None else 3,
        career_sub_wins=0 if rng is not None else 2,
        last_fight_date="2026-05-01", fight_date="2026-10-24",
    )


def _p_a(x, y, rounds, iterations, seed) -> float:
    px, py = build_matchup(x, y, rounds)
    s = run_simulation(px, py, rounds, iterations=iterations, seed=seed)
    return s.a_wins / (s.a_wins + s.b_wins)


def test_identical_fighters() -> list:
    errors = []
    n = 200_000
    for rounds in (3, 5):
        p = _p_a(_fighter("X"), _fighter("Y"), rounds, n, seed=11 + rounds)
        se = math.sqrt(0.25 / n)
        status = "ok" if abs(p - 0.5) <= 3 * se else "FAIL"
        print(f"  identical, {rounds} rounds: P(A) = {p:.4f} "
              f"(50% ± {3 * se:.4f}) {status}")
        if status == "FAIL":
            errors.append(f"identical fighters {rounds}rd: P(A)={p:.4f}")
    return errors


def test_profiles_slot_independent() -> list:
    rng = np.random.default_rng(3)
    errors = []
    for i in range(40):
        x, y = _fighter(f"X{i}", rng), _fighter(f"Y{i}", rng)
        px1, py1 = build_matchup(x, y, 3)
        py2, px2 = build_matchup(y, x, 3)
        if (dataclasses.asdict(px1) != dataclasses.asdict(px2)
                or dataclasses.asdict(py1) != dataclasses.asdict(py2)):
            errors.append(f"profile depends on slot for pair {i}")
    print(f"  profiles slot-independent: {40 - len(errors)}/40 ok")
    return errors


def test_swap() -> list:
    rng = np.random.default_rng(5)
    diffs = []
    for i in range(60):
        x, y = _fighter(f"X{i}", rng), _fighter(f"Y{i}", rng)
        p_in_a = _p_a(x, y, 3, 15_000, seed=1_000 + i)
        p_in_b = 1.0 - _p_a(y, x, 3, 15_000, seed=5_000 + i)
        diffs.append(p_in_a - p_in_b)
    d = np.array(diffs)
    mean, se = d.mean(), d.std(ddof=1) / math.sqrt(len(d))
    status = "ok" if abs(mean) <= 3 * se else "FAIL"
    print(f"  swap test, 60 matchups: mean A-slot effect {mean * 100:+.2f} pts "
          f"(SE {se * 100:.2f}) {status}")
    return [] if status == "ok" else [f"swap test mean {mean:+.4f}, SE {se:.4f}"]


def main() -> int:
    errors = []
    for test in (test_identical_fighters, test_profiles_slot_independent, test_swap):
        print(test.__name__)
        errors += test()
    print("PASS" if not errors else "FAIL: " + "; ".join(errors))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
