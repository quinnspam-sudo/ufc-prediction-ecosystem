"""
simulation_engine.py
====================
Round-by-round Monte Carlo fight simulator.

Each iteration simulates a full fight one round at a time:

    1. Compute a per-round *Dominance Score* for each fighter from their
       striking + grappling primitives, scaled by REMAINING stamina and all
       matchup modifiers, plus gaussian noise (+ ring-rust variance).
    2. Roll finishing triggers (KO/TKO, Submission) as conditional
       probabilities that depend on the dominance gap, power vs chin, and
       fatigue.
    3. If no finish, score the round 10-9 / 10-8 from the dominance gap and
       accumulate toward a 3-judge scorecard.
    4. Decay each fighter's stamina by an amount driven by the round's pace.

Run >= 10,000 iterations to get a stable distribution.

All magic numbers are commented as tuning levers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import numpy as np

from feature_engineering import FighterProfile


# ---------------------------------------------------------------------------
# Tuning levers for the fight model
# ---------------------------------------------------------------------------
# Relative weight of striking vs grappling in the raw dominance score.
STRIKING_WEIGHT = 1.0
GRAPPLING_WEIGHT = 0.9

# Base per-round noise (std-dev) on the dominance score. MMA is high-variance;
# this is what lets underdogs win. Ring-rust variance is added on top.
BASE_ROUND_NOISE = 0.28

# Finish-probability shaping constants (see _finish_probabilities).
# These are calibrated so that even the deliberately finish-prone demo
# archetypes still see a realistic share of fights reach the judges. Raise
# the *_BASE values for a more finish-heavy meta, lower them for a
# grind-it-out, decision-heavy meta.
KO_BASE = 0.026          # baseline KO chance for an even striking round
SUB_BASE = 0.020         # baseline sub chance for an even grappling round
KO_GAP_SENSITIVITY = 0.35 # how sharply KO odds rise with striking dominance
SUB_GAP_SENSITIVITY = 0.40
# Dominance gaps can run to ~2.5 between mismatched fighters; left unbounded,
# exp(sensitivity * gap) explodes and pins finishes at the per-round cap. Cap
# the gap that feeds the exponential so it saturates instead.
FINISH_GAP_CAP = 2.0

# Stamina decay: fraction of the cardio pool burned per round at "average"
# pace, before per-round pace and intrinsic-slope adjustments.
BASE_STAMINA_BURN = 0.14

# 10-8 threshold: dominance gap (in normalized units) above which a round is
# scored 10-8 instead of 10-9.
TEN_EIGHT_GAP = 0.55


@dataclass
class RoundResult:
    """Outcome of a single simulated round (for optional deep inspection)."""
    round_number: int
    a_dominance: float
    b_dominance: float
    finished: bool
    finish_by: str = ""        # "A" or "B"
    finish_method: str = ""    # "KO/TKO" | "Submission"
    a_score: int = 10
    b_score: int = 10


@dataclass
class FightOutcome:
    """Outcome of a single complete simulated fight."""
    winner: str                # "A" | "B" | "Draw"
    method: str                # "KO/TKO" | "Submission" | "Unanimous Decision"
                               # | "Split/Majority Decision" | "Draw"
    round_ended: int
    rounds: List[RoundResult] = field(default_factory=list)


@dataclass
class SimulationResult:
    """Aggregated result of N simulated fights."""
    iterations: int
    a_name: str
    b_name: str
    a_wins: int
    b_wins: int
    draws: int
    # method_counts["A"]["KO/TKO"] = count, etc.
    method_counts: Dict[str, Dict[str, int]]
    # Distribution of the round in which finishes occurred, per fighter.
    finish_round_counts: Dict[str, Dict[int, int]]
    avg_rounds: float


# Canonical method labels.
FINISH_METHODS = ("KO/TKO", "Submission")
DECISION_METHODS = ("Unanimous Decision", "Split/Majority Decision")


def _effective_striking(p: FighterProfile, stamina_frac: float) -> float:
    """
    Fighter's effective striking dominance contribution this round.

    Scaled by:
      * current stamina fraction (tired fighters throw/land less),
      * age factor, reach advantage, stance edge, travel penalty.
    Absorption resilience is applied later, only once a fighter is hurt.
    """
    base = p.striking_offense * STRIKING_WEIGHT
    # Stamina hits offense more than defense (output is the first thing to go).
    stamina_scaled = base * (0.55 + 0.45 * stamina_frac)
    modifier = (
        p.age_factor
        + p.reach_advantage
        + p.stance_edge
        + p.travel_penalty
    )
    return stamina_scaled * modifier


def _effective_grappling(p: FighterProfile, opp: FighterProfile,
                         stamina_frac: float) -> float:
    """
    Fighter's effective grappling dominance this round: offense pressed
    against the opponent's takedown defense, scaled by stamina and control.
    """
    # Grappling offense that actually gets through the opponent's TDD.
    penetration = p.grappling_offense / max(opp.grappling_defense, 0.4)
    stamina_scaled = penetration * (0.6 + 0.4 * stamina_frac)
    # Control dominance amplifies grappling scoring impact.
    return stamina_scaled * (0.8 + 0.5 * p.control_dominance) * GRAPPLING_WEIGHT * p.age_factor


def _dominance_score(p: FighterProfile, opp: FighterProfile,
                     stamina_frac: float, rng: np.random.Generator) -> float:
    """
    Full per-round dominance score = striking + grappling + noise.

    Noise std-dev = BASE_ROUND_NOISE + this fighter's ring-rust variance.
    This gaussian term is the engine's primary source of upset variance.
    """
    strike = _effective_striking(p, stamina_frac)
    grapple = _effective_grappling(p, opp, stamina_frac)
    signal = strike + grapple
    noise = rng.normal(0.0, BASE_ROUND_NOISE + p.rust_variance)
    return signal + noise


def _finish_probabilities(
    attacker: FighterProfile,
    defender: FighterProfile,
    strike_gap: float,
    grapple_gap: float,
    defender_stamina_frac: float,
    defender_hurt: bool,
) -> Tuple[float, float]:
    """
    Conditional finish probabilities for the attacker this round.

    Returns (p_ko, p_sub).

    KO/TKO trigger
    --------------
    Rises with the attacker's striking dominance gap and raw power, and
    with the defender's DEFENSE DEGRADATION (low stamina + being hurt), and
    is divided down by the defender's chin. Logistic-style saturating growth.

        p_ko = KO_BASE
               * power_term
               * exp(KO_GAP_SENSITIVITY * max(0, strike_gap))
               * defense_degradation
               / defender_chin

    Submission trigger
    ------------------
    Rises with the attacker's submission offense and grappling control gap,
    and with the defender's grappling FATIGUE, divided by the defender's
    submission defense.
    """
    # Elite offense primitives normalize to ~4-5x league average. Feeding that
    # in linearly makes a single elite trait swamp the whole fight, so we apply
    # diminishing-returns compression: a 4x edge becomes ~2.2x, not 4x. Lower
    # OFFENSE_COMPRESSION -> flatter (elite traits matter less).
    OFFENSE_COMPRESSION = 0.55

    # --- KO/TKO -----------------------------------------------------------
    power_term = attacker.striking_power ** OFFENSE_COMPRESSION
    # Defense degradation: a fully-fresh, unhurt fighter ~= 1.0; a gassed
    # fighter climbs toward ~1.5x KO exposure (0.5 sensitivity on the deficit).
    degradation = 1.0 + (1.0 - defender_stamina_frac) * 0.5
    if defender_hurt:
        # Once hurt, the defender's own absorption-resilience decides how much
        # they crumble. Low resilience -> big extra multiplier.
        degradation *= (1.0 + (1.0 - defender.absorption_resilience) * 0.8)

    strike_gap_c = min(max(0.0, strike_gap), FINISH_GAP_CAP)
    p_ko = (
        KO_BASE
        * power_term
        * float(np.exp(KO_GAP_SENSITIVITY * strike_gap_c))
        * degradation
        / max(defender.chin, 0.4)
    )

    # --- Submission -------------------------------------------------------
    # Grappling fatigue: defending subs while tired is harder (0.6 sensitivity).
    grapple_fatigue = 1.0 + (1.0 - defender_stamina_frac) * 0.6
    grapple_gap_c = min(max(0.0, grapple_gap), FINISH_GAP_CAP)
    p_sub = (
        SUB_BASE
        * (attacker.submission_offense ** OFFENSE_COMPRESSION)
        * float(np.exp(SUB_GAP_SENSITIVITY * grapple_gap_c))
        * grapple_fatigue
        / max(defender.submission_defense, 0.4)
    )

    # Clamp each to a sane per-round ceiling so no single round is a coin flip
    # on a finish (keeps distributions realistic).
    return min(p_ko, 0.55), min(p_sub, 0.45)


def _stamina_burn(p: FighterProfile, pace: float) -> float:
    """
    Fraction of cardio pool burned this round.

    Burn = BASE_STAMINA_BURN * pace_multiplier * (1 + intrinsic_slope)
    where `pace` reflects how frantic the round was (heavy striking or heavy
    grappling-defense both drain harder). Cardio pool > 100 (deep-water
    fighters) reduces the *relative* burn.
    """
    pace_multiplier = 0.75 + pace  # pace ~0..0.6 typical
    intrinsic = 1.0 + p.cardio_slope * 3.0
    pool_factor = 100.0 / max(p.cardio_pool, 60.0)  # bigger tank -> smaller burn
    return BASE_STAMINA_BURN * pace_multiplier * intrinsic * pool_factor


def simulate_fight(
    a: FighterProfile,
    b: FighterProfile,
    scheduled_rounds: int,
    rng: np.random.Generator,
) -> FightOutcome:
    """Simulate ONE complete fight round-by-round."""
    # Stamina fractions start at 1.0 (full pool).
    a_stam, b_stam = 1.0, 1.0
    # Cumulative significant strikes eaten this fight (for the >50 hurt flag).
    a_absorbed, b_absorbed = 0.0, 0.0
    # Judge scorecards: three independent judges accumulating points.
    a_cards = np.zeros(3)
    b_cards = np.zeros(3)
    rounds: List[RoundResult] = []

    for rnd in range(1, scheduled_rounds + 1):
        a_hurt = a_absorbed > 50
        b_hurt = b_absorbed > 50

        a_dom = _dominance_score(a, b, a_stam, rng)
        b_dom = _dominance_score(b, a, b_stam, rng)

        # A hurt fighter's dominance is further suppressed by how poorly they
        # carry damage (absorption resilience).
        if a_hurt:
            a_dom *= (0.6 + 0.4 * a.absorption_resilience)
        if b_hurt:
            b_dom *= (0.6 + 0.4 * b.absorption_resilience)

        strike_gap_a = _effective_striking(a, a_stam) - _effective_striking(b, b_stam)
        grapple_gap_a = _effective_grappling(a, b, a_stam) - _effective_grappling(b, a, b_stam)

        # Finish rolls — each fighter is the attacker vs the other as defender.
        a_ko, a_sub = _finish_probabilities(a, b, strike_gap_a, grapple_gap_a,
                                            b_stam, b_hurt)
        b_ko, b_sub = _finish_probabilities(b, a, -strike_gap_a, -grapple_gap_a,
                                            a_stam, a_hurt)

        # Resolve finishes. Draw four uniforms; the largest exceeded prob that
        # "fires" first (by a fixed priority) ends the fight. Priority order is
        # arbitrary but consistent; probabilities are small so collisions rare.
        events = [
            ("A", "KO/TKO", a_ko),
            ("B", "KO/TKO", b_ko),
            ("A", "Submission", a_sub),
            ("B", "Submission", b_sub),
        ]
        finished = False
        for who, method, prob in events:
            if rng.random() < prob:
                rounds.append(RoundResult(rnd, a_dom, b_dom, True, who, method))
                return FightOutcome(winner=who, method=method,
                                    round_ended=rnd, rounds=rounds)

        # --- No finish: score the round --------------------------------
        gap = a_dom - b_dom
        if gap >= 0:
            a_pts, b_pts = (10, 8) if gap >= TEN_EIGHT_GAP else (10, 9)
        else:
            a_pts, b_pts = (8, 10) if -gap >= TEN_EIGHT_GAP else (9, 10)

        # Three judges: same round, small independent scoring noise so that
        # close rounds (|gap| small) can flip on one or two cards -> split
        # decisions. Wide-gap rounds are unanimous across judges.
        for j in range(3):
            judge_gap = gap + rng.normal(0.0, 0.12)  # per-judge perception noise
            if judge_gap >= 0:
                a_cards[j] += 10
                b_cards[j] += 8 if judge_gap >= TEN_EIGHT_GAP else 9
            else:
                b_cards[j] += 10
                a_cards[j] += 8 if -judge_gap >= TEN_EIGHT_GAP else 9

        rounds.append(RoundResult(rnd, a_dom, b_dom, False, a_score=a_pts, b_score=b_pts))

        # --- Track damage absorbed (drives the >50 hurt flag) ----------
        # Approximate strikes eaten this round from opponent striking output
        # scaled by stamina; used only for the cumulative-damage trigger.
        a_absorbed += max(0.0, _effective_striking(b, b_stam) * 10.0)
        b_absorbed += max(0.0, _effective_striking(a, a_stam) * 10.0)

        # --- Stamina decay driven by round pace ------------------------
        # Pace ~ total offensive output in the round (both fighters), mapped
        # to ~0..0.6. A frantic round drains both fighters harder.
        pace = min(0.6, (abs(a_dom) + abs(b_dom)) * 0.15)
        a_stam = max(0.05, a_stam - _stamina_burn(a, pace))
        b_stam = max(0.05, b_stam - _stamina_burn(b, pace))

    # --- Went the distance: read the 3 judge scorecards ---------------
    judge_winners = []
    for j in range(3):
        if a_cards[j] > b_cards[j]:
            judge_winners.append("A")
        elif b_cards[j] > a_cards[j]:
            judge_winners.append("B")
        else:
            judge_winners.append("Draw")

    a_votes = judge_winners.count("A")
    b_votes = judge_winners.count("B")

    if a_votes == 3 or b_votes == 3:
        winner = "A" if a_votes == 3 else "B"
        method = "Unanimous Decision"
    elif a_votes >= 2 or b_votes >= 2:
        # 2-1 (split) or 2-0-1 (majority) — grouped per the output schema.
        winner = "A" if a_votes > b_votes else "B"
        method = "Split/Majority Decision"
    else:
        winner, method = "Draw", "Draw"

    return FightOutcome(winner=winner, method=method,
                        round_ended=scheduled_rounds, rounds=rounds)


def run_simulation(
    a: FighterProfile,
    b: FighterProfile,
    scheduled_rounds: int,
    iterations: int = 10_000,
    seed: int = 42,
) -> SimulationResult:
    """
    Run `iterations` (>= 10,000 recommended) fights and aggregate.

    A single seeded numpy Generator drives all randomness for reproducibility;
    pass a different `seed` for an independent run.
    """
    if iterations < 10_000:
        # Enforce the spec's stability floor but allow explicit override upstream.
        iterations = max(iterations, 1)

    rng = np.random.default_rng(seed)

    a_wins = b_wins = draws = 0
    method_counts = {
        "A": {"KO/TKO": 0, "Submission": 0,
              "Unanimous Decision": 0, "Split/Majority Decision": 0},
        "B": {"KO/TKO": 0, "Submission": 0,
              "Unanimous Decision": 0, "Split/Majority Decision": 0},
    }
    finish_round_counts: Dict[str, Dict[int, int]] = {
        "A": {r: 0 for r in range(1, scheduled_rounds + 1)},
        "B": {r: 0 for r in range(1, scheduled_rounds + 1)},
    }
    total_rounds = 0

    for _ in range(iterations):
        outcome = simulate_fight(a, b, scheduled_rounds, rng)
        total_rounds += outcome.round_ended

        if outcome.winner == "A":
            a_wins += 1
            method_counts["A"][outcome.method] += 1
            if outcome.method in FINISH_METHODS:
                finish_round_counts["A"][outcome.round_ended] += 1
        elif outcome.winner == "B":
            b_wins += 1
            method_counts["B"][outcome.method] += 1
            if outcome.method in FINISH_METHODS:
                finish_round_counts["B"][outcome.round_ended] += 1
        else:
            draws += 1

    return SimulationResult(
        iterations=iterations,
        a_name=a.name,
        b_name=b.name,
        a_wins=a_wins,
        b_wins=b_wins,
        draws=draws,
        method_counts=method_counts,
        finish_round_counts=finish_round_counts,
        avg_rounds=total_rounds / max(iterations, 1),
    )
