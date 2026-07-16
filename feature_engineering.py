"""
feature_engineering.py
======================
Transforms raw fighter stats into a normalized, matchup-aware
`FighterProfile` of derived combat attributes that the simulation engine
consumes.

Design philosophy
-----------------
The simulation engine should NOT know about raw UFCStats columns. It reasons
about a small set of *combat primitives* on a roughly 0..~1.5 scale:

    striking_offense, striking_defense, striking_power,
    grappling_offense, grappling_defense,
    submission_offense, submission_defense,
    chin, cardio_pool, cardio_slope,
    plus matchup modifiers (reach, stance, age, rust, travel, division).

Every derived number below is heavily commented with the intent and the
tuning levers, so you can adjust weights without reverse-engineering the math.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Tuple

from data_ingestion import FighterRawStats, WEIGHT_CLASSES

# Reference date used for layoff computation. In production, pass the actual
# scheduled fight date. Kept as a module constant so results are reproducible.
REFERENCE_FIGHT_DATE = date(2026, 7, 15)


# ---------------------------------------------------------------------------
# Tunable global weights
# ---------------------------------------------------------------------------
# League-average anchors used to normalize raw rates into ~0..1.5 attributes.
# These are the "average UFC fighter" reference points. Tune to taste.
LEAGUE_AVG = {
    "slpm": 4.0,          # avg significant strikes landed / min
    "sapm": 4.0,          # avg absorbed / min
    "td_avg": 1.7,        # avg takedowns / 15 min
    "sub_avg": 0.6,       # avg sub attempts / 15 min
    "control_pct": 0.30,  # avg dominant-control fraction
}

AGE_CLIFF = 35            # age past which the decline penalty kicks in
LAYOFF_RUST_DAYS = 365    # layoff beyond this adds ring-rust variance

# --- Bayesian finish-rate shrink ------------------------------------------
# Pseudo-fight count for shrinking a fighter's career KO/sub rates toward the
# divisional base rate: shrunk = (wins_by_method + K * base) / (wins + K).
# K=5 means a 3-for-3 KO record reads as ~(3 + 5*base)/8, not 100%.
FINISH_PRIOR_STRENGTH = 5.0
# Clamp on the resulting propensity multiplier so an outlier record can tilt
# but never dominate the sim's finish triggers.
FINISH_PROPENSITY_RANGE = (0.65, 1.55)

# --- Elevation ---------------------------------------------------------------
# Venue elevation above this many feet starts taxing cardio for fighters who
# don't train at altitude. Fully saturates at ELEVATION_MAX_FT.
ELEVATION_FLOOR_FT = 3000.0
ELEVATION_MAX_FT = 8000.0
ELEVATION_MAX_CARDIO_TAX = 0.12   # up to -12% cardio pool at/above 8000ft
ELEVATION_MAX_SLOPE_ADD = 0.03    # and a faster per-round fade

# --- Stat staleness (time decay) ----------------------------------------------
# Career-aggregate stats stop being trustworthy as they age. Community
# consensus (r/algobetting): full weight ~3 years, decay out to ~10 years.
# We implement the sim-side analog: shrink offensive primitives toward the
# league average (1.0) as the fighter's most recent data point gets stale.
STALENESS_FULL_TRUST_DAYS = 3 * 365
STALENESS_ZERO_EXTRA_DAYS = 7 * 365   # trust bottoms out ~10 years total
STALENESS_MAX_SHRINK = 0.40           # at most 40% pull toward league average


@dataclass
class FighterProfile:
    """Derived, simulation-ready combat attributes for one fighter."""

    name: str
    scheduled_rounds: int

    # --- Striking primitives ----------------------------------------------
    striking_offense: float      # volume * accuracy, normalized (~1.0 = league avg)
    striking_defense: float      # ability to avoid damage (~1.0 = avg)
    striking_power: float        # KO threat per landed strike (~1.0 = avg)
    strike_differential: float   # SLpM - SApM, raw
    absorption_resilience: float # how well output holds after heavy damage [0,1]

    # --- Grappling primitives ---------------------------------------------
    grappling_offense: float     # takedown + control pressure (~1.0 = avg)
    grappling_defense: float     # takedown defense mapped to [~0..1.2]
    submission_offense: float    # sub threat (~1.0 = avg)
    submission_defense: float    # sub survival [0..1.2]
    control_dominance: float     # dominant control fraction [0,1]

    # --- Durability / cardio ----------------------------------------------
    chin: float                  # durability multiplier; <1 = fragile, >1 = granite
    cardio_pool: float           # starting stamina units (100 = elite baseline)
    cardio_slope: float          # per-round intrinsic decay fraction [0..~0.15]

    # --- Matchup modifiers (filled during build_matchup) ------------------
    reach_advantage: float = 0.0     # normalized reach edge vs opponent [-~0.15..0.15]
    ape_index: float = 0.0           # reach - height, inches
    age_factor: float = 1.0          # multiplicative penalty (<=1.0)
    rust_variance: float = 0.0       # extra round-to-round variance from layoff
    stance_edge: float = 0.0         # southpaw/switch advantage bonus
    travel_penalty: float = 0.0      # cardio/sharpness penalty for travelling
    division_power_adj: float = 1.0  # power scaling from moving up/down
    division_cardio_adj: float = 1.0 # cardio scaling from moving up/down


# ---------------------------------------------------------------------------
# Individual feature calculators (single-fighter)
# ---------------------------------------------------------------------------
def _striking_offense(s: FighterRawStats) -> float:
    """
    Offense = normalized (volume * accuracy).

    We reward *landed* volume, not thrown volume, so a high-accuracy,
    moderate-volume sniper and a high-volume, lower-accuracy pressure
    fighter can land in a similar band. Divided by the league anchor so
    ~1.0 == an average UFC striker.
    """
    effective_landed = s.slpm * (0.5 + s.strike_acc)   # accuracy tilts the rate
    return effective_landed / LEAGUE_AVG["slpm"]


def _striking_defense(s: FighterRawStats) -> float:
    """
    Defense blends published strike-defense % with how little a fighter
    absorbs relative to league average. Both matter: you can have a high
    published defense but still eat volume from pressure.
    """
    absorb_ratio = LEAGUE_AVG["sapm"] / max(s.sapm, 0.1)  # >1 == absorbs less than avg
    # 60% weight on published defense, 40% on relative absorption.
    return 0.6 * (s.strike_def / 0.55) + 0.4 * absorb_ratio


def _striking_power(s: FighterRawStats) -> float:
    """
    Power (KO threat per strike) is NOT directly published, so we proxy it.

    Signal: a fighter who absorbs a lot yet has a low knockdown-suffered rate
    has a good chin (handled elsewhere); a fighter whose *offense* produces
    finishes relative to volume implies power. Here we use accuracy as a
    crude power proxy (clean strikes land flush) blended with a heavyweight
    lightness bonus. Replace with real knockdown-scored data when available.
    """
    lightness = WEIGHT_CLASSES[s.weight_class]["lightness"]
    # Heavier classes -> more raw power per strike (lightness is inverse).
    power_class_bonus = 1.0 + (1.0 - lightness) * 0.6
    return (0.7 + s.strike_acc) * power_class_bonus * 0.85


def _absorption_resilience(s: FighterRawStats) -> float:
    """
    Strike Absorption Resilience: how well a fighter maintains output after
    absorbing >50 significant strikes in a fight.

    We derive it from career damage load per fight and knockdowns suffered:
    a fighter dropped often per fight degrades hard once hurt. Result [0,1],
    where 1.0 == output essentially unaffected by heavy damage.
    """
    fights = max(s.career_fights, 1)
    kd_rate = s.career_knockdowns_suffered / fights           # drops per fight
    absorbed_per_fight = s.career_sig_strikes_absorbed / fights
    # Each drop-per-fight costs resilience; heavy absorption costs a little more.
    resilience = 1.0 - (kd_rate * 0.45) - max(0.0, (absorbed_per_fight - 60) / 400)
    return float(max(0.15, min(1.0, resilience)))


def _grappling_offense(s: FighterRawStats) -> float:
    """
    Grappling offense = takedown threat + control pressure.

    Landing takedowns you can't hold is worth little, so we weight by both
    takedown accuracy and control-time dominance. ~1.0 == league average.
    """
    td_component = (s.td_avg * (0.4 + s.td_acc)) / LEAGUE_AVG["td_avg"]
    control_component = s.control_time_pct / LEAGUE_AVG["control_pct"]
    return 0.6 * td_component + 0.4 * control_component


def _grappling_defense(s: FighterRawStats) -> float:
    """Takedown defense mapped so league-avg TDD (~0.65) == ~1.0."""
    return s.td_def / 0.65


def _submission_offense(s: FighterRawStats) -> float:
    """
    Submission threat from attempt rate + sweep/reversal activity (scramble
    dominance creates sub openings). ~1.0 == league average.
    """
    return (s.sub_avg + 0.5 * s.sweep_rate) / LEAGUE_AVG["sub_avg"]


def _chin(s: FighterRawStats) -> float:
    """
    Chin / durability multiplier.

    Low knockdown-suffered rate over a long career == granite (>1). A high
    rate, especially on a lot of absorbed damage, == fragile (<1). This
    multiplier later *divides down* an opponent's effective KO probability.
    """
    fights = max(s.career_fights, 1)
    kd_rate = s.career_knockdowns_suffered / fights
    # 0 KDs/fight -> ~1.2 (granite); 0.5 KDs/fight -> ~0.7 (fragile).
    chin = 1.2 - kd_rate * 1.0
    return float(max(0.5, min(1.3, chin)))


def _cardio(s: FighterRawStats) -> Tuple[float, float]:
    """
    Returns (cardio_pool, cardio_slope).

    * cardio_pool: starting stamina units. Baseline 100. Championship-round
      experience adds to the pool (proven deep-water conditioning).
    * cardio_slope: intrinsic per-round decay, derived from the fighter's
      historical round-by-round output slope. A steep drop-off in career
      round output -> higher slope -> faster attribute decay in the sim.
    """
    outputs = s.round_strike_output or [50, 50, 50]
    r1 = outputs[0] if outputs else 50.0
    last = outputs[-1] if outputs else 50.0
    # Fractional decline from first to last recorded round.
    total_decline = (r1 - last) / max(r1, 1.0)
    rounds_spanned = max(len(outputs) - 1, 1)
    per_round_decline = max(0.0, total_decline / rounds_spanned)

    # Championship-round experience -> bigger tank + slightly flatter slope.
    champ_bonus = min(s.championship_round_fights, 6) * 2.5
    cardio_pool = 100.0 + champ_bonus
    cardio_slope = per_round_decline * (1.0 - min(s.championship_round_fights, 5) * 0.04)
    return float(cardio_pool), float(max(0.01, min(0.15, cardio_slope)))


def _finish_propensity(s: FighterRawStats) -> Tuple[float, float]:
    """
    Returns (ko_propensity, sub_propensity) multipliers (~1.0 = divisional
    average finisher).

    Bayesian shrink: the fighter's career KO-rate and sub-rate (per win) are
    pulled toward the divisional base rates by FINISH_PRIOR_STRENGTH
    pseudo-wins, then expressed as a ratio to that base. This keeps a 3-fight
    sample honest while letting a 20-win finisher's record speak. Fighters
    with no recorded wins (or unpopulated fields) get a neutral 1.0.
    """
    if s.career_wins <= 0:
        return 1.0, 1.0
    wc = WEIGHT_CLASSES[s.weight_class]
    ko_base, sub_base = wc["ko_base"], wc["sub_base"]
    k = FINISH_PRIOR_STRENGTH
    lo, hi = FINISH_PROPENSITY_RANGE
    ko_shrunk = (s.career_ko_wins + k * ko_base) / (s.career_wins + k)
    sub_shrunk = (s.career_sub_wins + k * sub_base) / (s.career_wins + k)
    ko_prop = max(lo, min(hi, ko_shrunk / ko_base))
    sub_prop = max(lo, min(hi, sub_shrunk / sub_base))
    return float(ko_prop), float(sub_prop)


def _days_since_last_fight(s: FighterRawStats) -> int:
    try:
        y, m, d = (int(x) for x in s.last_fight_date.split("-"))
        return (REFERENCE_FIGHT_DATE - date(y, m, d)).days
    except Exception:
        return 0


def _staleness_trust(s: FighterRawStats) -> float:
    """
    Time-decay weight on career stats, in [1 - STALENESS_MAX_SHRINK, 1.0].

    A fighter whose most recent bout is <3 years old gets full trust (1.0);
    trust then decays linearly, bottoming out ~10 years after the last fight.
    Offensive primitives are blended toward the league average (1.0) by
    (1 - trust), so ancient stat lines regress instead of being taken at face
    value.
    """
    days_off = _days_since_last_fight(s)
    if days_off <= STALENESS_FULL_TRUST_DAYS:
        return 1.0
    excess = min(days_off - STALENESS_FULL_TRUST_DAYS, STALENESS_ZERO_EXTRA_DAYS)
    shrink = STALENESS_MAX_SHRINK * (excess / STALENESS_ZERO_EXTRA_DAYS)
    return float(1.0 - shrink)


def _decay_toward_avg(value: float, trust: float) -> float:
    """Blend a ~1.0-anchored primitive toward the league-average 1.0."""
    return trust * value + (1.0 - trust) * 1.0


def _elevation_adjustments(s: FighterRawStats) -> Tuple[float, float]:
    """
    Returns (cardio_pool_mult, cardio_slope_add) for the bout's venue
    elevation. Sea-level venues and altitude-trained fighters pay nothing;
    everyone else loses tank and fades faster, scaling from
    ELEVATION_FLOOR_FT up to ELEVATION_MAX_FT (Denver ~5280ft lands at
    roughly half the maximum tax).
    """
    if s.trains_at_altitude or s.venue_elevation_ft <= ELEVATION_FLOOR_FT:
        return 1.0, 0.0
    span = ELEVATION_MAX_FT - ELEVATION_FLOOR_FT
    severity = min(1.0, (s.venue_elevation_ft - ELEVATION_FLOOR_FT) / span)
    return (1.0 - ELEVATION_MAX_CARDIO_TAX * severity,
            ELEVATION_MAX_SLOPE_ADD * severity)


def _submission_defense(s: FighterRawStats) -> float:
    """Submission defense mapped so league-avg (~0.65) == ~1.0."""
    return s.sub_def / 0.65


# ---------------------------------------------------------------------------
# Matchup-relative modifiers (need both fighters)
# ---------------------------------------------------------------------------
def _age_factor(s: FighterRawStats) -> float:
    """
    Age Curve Factor: multiplicative performance penalty for fighters over 35,
    scaled HEAVIER for lighter weight classes (speed decays before power).

    age <= 35 -> 1.0 (no penalty).
    Each year past 35 costs `0.02 * lightness` of performance.
    A 38yo flyweight (lightness 1.35) loses ~8%; a 38yo heavyweight ~3.6%.
    """
    if s.age <= AGE_CLIFF:
        return 1.0
    lightness = WEIGHT_CLASSES[s.weight_class]["lightness"]
    years_over = s.age - AGE_CLIFF
    penalty = years_over * 0.02 * lightness
    return float(max(0.70, 1.0 - penalty))


def _rust_variance(s: FighterRawStats) -> float:
    """
    Layoff / Ring-Rust Factor.

    Days since last bout beyond `LAYOFF_RUST_DAYS` inject extra round-to-round
    variance (timing/sharpness gambles) rather than a flat penalty — rusty
    fighters are less predictable, not strictly worse. Returned as an extra
    std-dev added to the per-round dominance noise.
    """
    try:
        y, m, d = (int(x) for x in s.last_fight_date.split("-"))
        last = date(y, m, d)
    except Exception:
        return 0.0
    days_off = (REFERENCE_FIGHT_DATE - last).days
    if days_off <= LAYOFF_RUST_DAYS:
        return 0.0
    # Scale: every extra 180 days beyond a year adds ~0.05 std-dev, capped.
    excess = days_off - LAYOFF_RUST_DAYS
    return float(min(0.25, (excess / 180.0) * 0.05))


def _reach_metrics(a: FighterRawStats, b: FighterRawStats) -> Tuple[float, float]:
    """
    Returns (reach_advantage, ape_index) for fighter `a` vs `b`.

    reach_advantage is normalized: every inch of reach edge == ~1.5% striking
    leverage, capped at +/-15%. Ape index (reach - height) is reported raw.
    """
    reach_edge_inches = a.reach_in - b.reach_in
    reach_advantage = max(-0.15, min(0.15, reach_edge_inches * 0.015))
    ape_index = a.reach_in - a.height_in
    return float(reach_advantage), float(ape_index)


def _stance_edge(a: FighterRawStats, b: FighterRawStats) -> float:
    """
    Switch-Stance / Southpaw Advantage matrix.

    Southpaws enjoy a small statistical edge vs orthodox opponents (unfamiliar
    look). Switch fighters get a smaller, matchup-agnostic adaptability bonus.
    Returns an additive dominance bonus.
    """
    a_st, b_st = a.stance.lower(), b.stance.lower()
    if a_st == "switch":
        return 0.03
    if a_st == "southpaw" and b_st == "orthodox":
        return 0.05
    if a_st == "orthodox" and b_st == "southpaw":
        return -0.02   # orthodox mildly disadvantaged vs southpaw
    return 0.0


def _travel_penalty(s: FighterRawStats) -> float:
    """
    Home-Octagon / Travel factor. A fighter competing outside their home
    country eats a small sharpness/cardio penalty (jet lag, weight cut abroad,
    hostile crowd). Home fighters get 0. Returns a small negative modifier.
    """
    if s.home_country != s.fight_location_country:
        return -0.02
    return 0.0


def _division_adjustments(s: FighterRawStats) -> Tuple[float, float]:
    """
    Weight Class Adaptation. Returns (power_adj, cardio_adj).

    Moving UP a division: you keep your speed but your power is diluted
    against bigger bodies (power_adj < 1), while cardio is relatively easier
    (cardio_adj > 1, less severe cut).
    Moving DOWN: power concentrates (power_adj > 1) but the harder cut taxes
    cardio (cardio_adj < 1).
    """
    if s.division_move > 0:      # moving up
        return 0.90, 1.05
    if s.division_move < 0:      # moving down
        return 1.08, 0.93
    return 1.0, 1.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _health_adjustments(s: FighterRawStats) -> Tuple[float, float, float]:
    """
    Live health/weight modifiers. Returns
    (power_mult, cardio_mult, extra_variance).

    * missed_weight: a brutal/failed cut saps power and (especially) cardio —
      the tank is smaller and the fighter fades faster.
    * active_injury: an unresolved injury adds unpredictability (variance) and
      a small across-the-board performance tax.
    These are intentionally conservative; tune once you trust the news source.
    """
    power_mult, cardio_mult, extra_var = 1.0, 1.0, 0.0
    if s.missed_weight:
        power_mult *= 0.95
        cardio_mult *= 0.88     # the cut hits cardio hardest
    if s.active_injury:
        # Penalty is intentionally larger than the variance bump so the central
        # tendency drops even for an underdog (more variance alone would help a
        # dog). Net: an injured fighter is worse AND less predictable.
        power_mult *= 0.90
        cardio_mult *= 0.90
        extra_var += 0.08       # injured fighters are less predictable
    return power_mult, cardio_mult, extra_var


def build_profile(s: FighterRawStats, scheduled_rounds: int) -> FighterProfile:
    """Build the single-fighter (matchup-independent) portion of a profile."""
    cardio_pool, cardio_slope = _cardio(s)
    power_adj, cardio_adj = _division_adjustments(s)
    health_power, health_cardio, _ = _health_adjustments(s)
    power_adj *= health_power
    cardio_adj *= health_cardio

    # Venue elevation taxes the tank and steepens the fade for fighters who
    # don't train at altitude.
    elev_cardio_mult, elev_slope_add = _elevation_adjustments(s)
    cardio_adj *= elev_cardio_mult
    cardio_slope = min(0.15, cardio_slope + elev_slope_add)

    # Method-of-victory propensity: career finish rates, Bayesian-shrunk
    # toward the divisional base, tilt the sim's KO and submission triggers.
    ko_prop, sub_prop = _finish_propensity(s)

    # Stat staleness: regress offensive primitives toward league average when
    # the underlying career data is old.
    trust = _staleness_trust(s)

    return FighterProfile(
        name=s.name,
        scheduled_rounds=scheduled_rounds,
        striking_offense=_decay_toward_avg(_striking_offense(s), trust),
        striking_defense=_striking_defense(s),
        striking_power=_decay_toward_avg(_striking_power(s), trust) * power_adj * ko_prop,
        strike_differential=s.slpm - s.sapm,
        absorption_resilience=_absorption_resilience(s),
        grappling_offense=_decay_toward_avg(_grappling_offense(s), trust),
        grappling_defense=_grappling_defense(s),
        submission_offense=_decay_toward_avg(_submission_offense(s), trust) * sub_prop,
        submission_defense=_submission_defense(s),
        control_dominance=s.control_time_pct,
        chin=_chin(s),
        cardio_pool=cardio_pool * cardio_adj,
        cardio_slope=cardio_slope,
        division_power_adj=power_adj,
        division_cardio_adj=cardio_adj,
    )


def build_matchup(
    a_raw: FighterRawStats,
    b_raw: FighterRawStats,
    scheduled_rounds: int,
) -> Tuple[FighterProfile, FighterProfile]:
    """
    Build BOTH profiles and populate the matchup-relative modifiers
    (reach, stance, age, rust, travel) on each side. Returns (profile_a,
    profile_b) ready for the simulation engine.
    """
    a = build_profile(a_raw, scheduled_rounds)
    b = build_profile(b_raw, scheduled_rounds)

    # Reach is symmetric-opposite between the two fighters.
    a.reach_advantage, a.ape_index = _reach_metrics(a_raw, b_raw)
    b.reach_advantage, b.ape_index = _reach_metrics(b_raw, a_raw)

    # Stance edge is directional.
    a.stance_edge = _stance_edge(a_raw, b_raw)
    b.stance_edge = _stance_edge(b_raw, a_raw)

    # Independent per-fighter modifiers.
    for prof, raw in ((a, a_raw), (b, b_raw)):
        prof.age_factor = _age_factor(raw)
        # Layoff rust variance plus any live injury variance stack.
        _, _, health_var = _health_adjustments(raw)
        prof.rust_variance = _rust_variance(raw) + health_var
        prof.travel_penalty = _travel_penalty(raw)

    return a, b
