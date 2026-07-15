"""
data_ingestion.py
==================
Mock / placeholder ingestion layer for the UFC Prediction Ecosystem.

In production, each `fetch_*` function below would hit a real source
(UFCStats scraper, Sherdog, a stats vendor API, a sportsbook odds feed).
Here they return richly-structured mock data so the rest of the pipeline
can be exercised end-to-end.

The canonical unit of data is the `FighterRawStats` dataclass. Every
downstream module consumes this object — nothing else reaches back into
the raw source. Keep this schema stable; add fields, don't rename.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Weight class reference table
# ---------------------------------------------------------------------------
# `lightness` scales the age penalty: lighter fighters rely more on speed /
# reflexes, which decay earliest, so their age curve is steeper. Heavyweights
# (lightness ~0.6) carry punching power far later, so they age more gracefully
# in terms of finishing ability. Tune these freely.
WEIGHT_CLASSES: Dict[str, Dict[str, float]] = {
    "Flyweight":        {"limit_lbs": 125, "lightness": 1.35},
    "Bantamweight":     {"limit_lbs": 135, "lightness": 1.25},
    "Featherweight":    {"limit_lbs": 145, "lightness": 1.15},
    "Lightweight":      {"limit_lbs": 155, "lightness": 1.05},
    "Welterweight":     {"limit_lbs": 170, "lightness": 0.95},
    "Middleweight":     {"limit_lbs": 185, "lightness": 0.85},
    "Light Heavyweight":{"limit_lbs": 205, "lightness": 0.72},
    "Heavyweight":      {"limit_lbs": 265, "lightness": 0.60},
}


@dataclass
class FighterRawStats:
    """
    Raw, source-of-truth statistics for a single fighter.

    All rate stats (SLpM, SApM, td_avg, sub_avg) are per-minute or per-15-min
    exactly as UFCStats publishes them. Accuracy / defense are fractions [0,1].
    """

    # --- Identity & biometrics ---------------------------------------------
    name: str
    age: int
    height_in: float                 # standing height in inches
    reach_in: float                  # arm reach in inches
    weight_class: str                # must be a key in WEIGHT_CLASSES
    stance: str                      # "Orthodox" | "Southpaw" | "Switch"

    # --- Striking (offense) ------------------------------------------------
    slpm: float                      # Significant Strikes Landed per Minute
    strike_acc: float                # Significant striking accuracy [0,1]
    # Target-differentiated accuracy. If a source can't split by target,
    # fall back to the overall `strike_acc` for all three.
    strike_acc_head: float = 0.0
    strike_acc_body: float = 0.0
    strike_acc_leg: float = 0.0

    # --- Striking (defense) ------------------------------------------------
    sapm: float = 0.0                # Significant Strikes Absorbed per Minute
    strike_def: float = 0.0          # Significant strike defense [0,1]

    # --- Grappling ---------------------------------------------------------
    td_avg: float = 0.0              # Takedowns landed per 15 min
    td_acc: float = 0.0              # Takedown accuracy [0,1]
    td_def: float = 0.0              # Takedown defense [0,1]
    sub_avg: float = 0.0             # Submission attempts per 15 min
    sweep_rate: float = 0.0          # Sweeps/reversals per 15 min
    control_time_pct: float = 0.0    # Fraction of ground time in dominant control [0,1]
    sub_def: float = 0.5             # Submission defense [0,1] (escapes / survives)

    # --- Durability / damage load ------------------------------------------
    career_knockdowns_suffered: int = 0     # total times dropped in career
    career_sig_strikes_absorbed: int = 0    # cumulative absorbed damage load
    career_fights: int = 0

    # --- Cardio / experience -----------------------------------------------
    # Average significant strikes landed by round across the fighter's career.
    # Used to compute the cardio-trajectory (output-slope) factor. Index 0=R1.
    round_strike_output: List[float] = field(default_factory=list)
    championship_round_fights: int = 0      # # of 5-round fights fought

    # --- Situational -------------------------------------------------------
    last_fight_date: str = "2025-01-01"     # ISO date of most recent bout
    # Division movement relative to THIS matchup's contracted weight:
    #   +1 moving UP, -1 moving DOWN, 0 staying. Affects power/cardio.
    division_move: int = 0
    home_country: str = "USA"
    fight_location_country: str = "USA"     # where THIS bout takes place

    def __post_init__(self) -> None:
        if self.weight_class not in WEIGHT_CLASSES:
            raise ValueError(
                f"Unknown weight class '{self.weight_class}'. "
                f"Valid: {list(WEIGHT_CLASSES)}"
            )
        # Backfill target-split accuracy from the overall figure if missing.
        if self.strike_acc_head == 0.0:
            self.strike_acc_head = self.strike_acc
        if self.strike_acc_body == 0.0:
            self.strike_acc_body = min(1.0, self.strike_acc * 1.15)  # body easier
        if self.strike_acc_leg == 0.0:
            self.strike_acc_leg = min(1.0, self.strike_acc * 1.25)   # legs easiest


@dataclass
class MatchupOdds:
    """Market moneyline odds (American) for a matchup, plus context."""
    fighter_a_moneyline: int
    fighter_b_moneyline: int
    scheduled_rounds: int = 3        # 3 or 5
    is_title_fight: bool = False


# ---------------------------------------------------------------------------
# Mock fetch pipelines
# ---------------------------------------------------------------------------
def fetch_fighter_stats() -> Dict[str, FighterRawStats]:
    """
    Return the mock fighter universe.

    The two demo fighters are deliberately archetypal to show off the
    multi-variable interactions the ecosystem models:

      * "Diego 'Volume' Marquez" — high-volume pressure striker, elite
        cardio and output, but POOR submission defense and a thinning chin.
      * "Kenji 'The Anaconda' Tanaka" — elite submission grappler with
        world-class takedowns and control, but an AGING chin, lower striking
        volume, and a long layoff (ring rust).
    """
    fighters = {
        "marquez": FighterRawStats(
            name="Diego 'Volume' Marquez",
            age=29,
            height_in=71.0,
            reach_in=74.0,                      # positive ape index
            weight_class="Welterweight",
            stance="Southpaw",
            slpm=6.8,                           # very high output
            strike_acc=0.49,
            sapm=4.1,
            strike_def=0.58,
            td_avg=0.4,
            td_acc=0.30,
            td_def=0.55,                        # mediocre TD defense
            sub_avg=0.2,
            sweep_rate=0.1,
            control_time_pct=0.20,
            sub_def=0.35,                       # <-- key weakness
            career_knockdowns_suffered=4,
            career_sig_strikes_absorbed=1850,
            career_fights=22,
            round_strike_output=[95, 92, 90, 88, 85],  # flat, elite cardio
            championship_round_fights=3,
            last_fight_date="2025-04-12",       # active, no rust
            division_move=0,
            home_country="USA",
            fight_location_country="USA",
        ),
        "tanaka": FighterRawStats(
            name="Kenji 'The Anaconda' Tanaka",
            age=36,                             # over the age-curve cliff
            height_in=72.0,
            reach_in=73.0,                      # near-neutral ape index
            weight_class="Welterweight",
            stance="Orthodox",
            slpm=3.2,                           # low striking volume
            strike_acc=0.44,
            sapm=3.0,
            strike_def=0.61,
            td_avg=4.6,                         # elite takedowns
            td_acc=0.47,
            td_def=0.78,
            sub_avg=2.4,                        # elite sub threat
            sweep_rate=0.8,
            control_time_pct=0.62,              # dominant control
            sub_def=0.80,
            career_knockdowns_suffered=9,       # aging chin / damage load
            career_sig_strikes_absorbed=2400,
            career_fights=27,
            round_strike_output=[70, 60, 52, 45, 40],  # steep cardio drop-off
            championship_round_fights=5,
            last_fight_date="2024-02-20",       # >365 day layoff -> ring rust
            division_move=0,
            home_country="Japan",
            fight_location_country="USA",       # travelling opponent
        ),
    }
    return fighters


def fetch_historical_results() -> List[Dict[str, object]]:
    """
    Mock historical fight-result feed.

    Downstream modules currently derive trajectory from the per-fighter
    `round_strike_output` aggregate, so this feed is illustrative /
    reserved for future expansion (e.g. opponent-adjusted metrics).
    """
    return [
        {"fighter": "marquez", "opponent": "Journeyman A",
         "result": "W", "method": "KO/TKO", "round": 2, "date": "2025-04-12"},
        {"fighter": "marquez", "opponent": "Contender B",
         "result": "L", "method": "Submission", "round": 3, "date": "2024-09-01"},
        {"fighter": "tanaka", "opponent": "Grinder C",
         "result": "W", "method": "Submission", "round": 1, "date": "2024-02-20"},
        {"fighter": "tanaka", "opponent": "Striker D",
         "result": "L", "method": "KO/TKO", "round": 4, "date": "2023-06-15"},
    ]


def fetch_market_odds() -> MatchupOdds:
    """
    Mock sportsbook feed for the demo matchup.

    Market has Marquez as a moderate favourite. The simulation will decide
    whether that price contains value on either side.
    """
    return MatchupOdds(
        fighter_a_moneyline=-165,   # Marquez favourite
        fighter_b_moneyline=+140,   # Tanaka underdog
        scheduled_rounds=5,         # main-event 5-rounder to exercise cardio
        is_title_fight=True,
    )
