"""
analytics_reporting.py
======================
Aggregates a `SimulationResult` into (1) a detailed JSON schema and (2) clean
CLI tables. Also computes value-betting metrics (implied vs market odds and
the Kelly Criterion stake) against user-supplied moneyline odds.

No third-party table library is required — the CLI renderer is a small,
dependency-free box-drawing helper so the ecosystem stays lean.
"""

from __future__ import annotations

import json
from typing import Dict, List, Tuple

import config
from data_ingestion import MatchupOdds
from simulation_engine import SimulationResult


# ---------------------------------------------------------------------------
# Odds helpers
# ---------------------------------------------------------------------------
def american_to_decimal(moneyline: int) -> float:
    """Convert American moneyline odds to decimal odds."""
    if moneyline > 0:
        return moneyline / 100.0 + 1.0
    return 100.0 / abs(moneyline) + 1.0


def implied_probability(moneyline: int) -> float:
    """Market-implied probability (includes the book's vig)."""
    return 1.0 / american_to_decimal(moneyline)


def devig_probabilities(a_ml: int, b_ml: int) -> Tuple[float, float]:
    """
    Remove the book's vig from a two-way market: normalize the two implied
    probabilities so they sum to 1. Returns (p_a_fair, p_b_fair).
    """
    ia, ib = implied_probability(a_ml), implied_probability(b_ml)
    total = ia + ib
    if total <= 0:
        return 0.5, 0.5
    return ia / total, ib / total


def blend_with_market(p_model: float, a_ml: int, b_ml: int,
                      model_weight: float) -> float:
    """
    Blend the model's P(A wins) with the devigged market probability.

    The market is a strong prior — market-aware models consistently beat
    stats-only models — so by default the market gets the majority weight
    (see config.MARKET_BLEND_MODEL_WEIGHT). Returns the blended P(A).
    """
    w = min(1.0, max(0.0, model_weight))
    p_market_a, _ = devig_probabilities(a_ml, b_ml)
    return w * p_model + (1.0 - w) * p_market_a


def kelly_fraction(win_prob: float, moneyline: int) -> float:
    """
    Kelly Criterion optimal stake fraction.

        f* = (b*p - q) / b

    where:
        b = decimal odds - 1   (net fractional odds; profit per unit staked)
        p = model win probability
        q = 1 - p

    A negative f* means NO bet (the market price offers no edge). We return the
    raw f* (can be negative) so the caller can decide on fractional-Kelly
    staking; the report also surfaces a clamped, half-Kelly suggestion.
    """
    b = american_to_decimal(moneyline) - 1.0
    p = win_prob
    q = 1.0 - p
    if b <= 0:
        return 0.0
    return (b * p - q) / b


# ---------------------------------------------------------------------------
# Aggregation -> JSON schema
# ---------------------------------------------------------------------------
def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 2) if d else 0.0


def _fight_level(result: SimulationResult, odds: MatchupOdds) -> Dict:
    """
    Side-agnostic fight outcome probabilities + expected duration.

    * p(KO/TKO), p(Submission), p(Decision) — either fighter.
    * goes_the_distance_pct — decisions + draws.
    * expected_fight_minutes — finishes count (round-1)*5 + 2.5 (mid-round
      expectation, since the sim only records the finish round), decisions
      count the full scheduled distance.
    """
    n = result.iterations
    mc = result.method_counts
    ko = mc["A"]["KO/TKO"] + mc["B"]["KO/TKO"]
    sub = mc["A"]["Submission"] + mc["B"]["Submission"]
    dec = (mc["A"]["Unanimous Decision"] + mc["A"]["Split/Majority Decision"]
           + mc["B"]["Unanimous Decision"] + mc["B"]["Split/Majority Decision"])
    distance = dec + result.draws

    finish_minutes = 0.0
    for side in ("A", "B"):
        for rnd, count in result.finish_round_counts[side].items():
            finish_minutes += count * ((rnd - 1) * 5.0 + 2.5)
    total_minutes = finish_minutes + distance * odds.scheduled_rounds * 5.0

    return {
        "ko_tko_pct": _pct(ko, n),
        "submission_pct": _pct(sub, n),
        "decision_pct": _pct(dec, n),
        "goes_the_distance_pct": _pct(distance, n),
        "expected_fight_minutes": round(total_minutes / n, 2) if n else None,
    }


def build_report(result: SimulationResult, odds: MatchupOdds) -> Dict:
    """
    Aggregate simulation counts + market odds into the full JSON report dict.
    """
    n = result.iterations
    a_wins, b_wins, draws = result.a_wins, result.b_wins, result.draws

    # --- Win probabilities (normalized to exclude draws for the h2h price) --
    decisive = a_wins + b_wins
    # The MODEL'S OWN probability. This is the pick, and it is what value
    # detection compares against the market — never blended, so the market
    # can't mute the model's independent voice.
    a_win_prob = a_wins / decisive if decisive else 0.0
    b_win_prob = b_wins / decisive if decisive else 0.0

    # --- Consensus forecast (separate from the pick) ------------------------
    # A market-majority blend of model + devigged odds. This is the best
    # ACCURACY estimate of who actually wins (the market prices late info the
    # model can't see), reported alongside — but it never replaces the model's
    # pick and never feeds edge/Kelly math. It is also the confirmation gate:
    # a value flag must survive this tempered probability too.
    model_weight = config.MARKET_BLEND_MODEL_WEIGHT
    blended = bool(odds.is_real_market and decisive and model_weight < 1.0)
    a_blend_prob = blend_with_market(
        a_win_prob, odds.fighter_a_moneyline, odds.fighter_b_moneyline,
        model_weight) if blended else a_win_prob

    # --- Method-of-victory matrix (as % of ALL iterations) -----------------
    def method_matrix(side: str) -> Dict[str, float]:
        mc = result.method_counts[side]
        return {
            "KO/TKO": _pct(mc["KO/TKO"], n),
            "Submission": _pct(mc["Submission"], n),
            "Unanimous Decision": _pct(mc["Unanimous Decision"], n),
            "Split/Majority Decision": _pct(mc["Split/Majority Decision"], n),
        }

    # --- Value / Kelly for each side --------------------------------------
    # Two-stage gate:
    #   1. model_sees_value — the RAW model probability beats the implied
    #      price (the pick "makes sense" on our own numbers).
    #   2. consensus_agrees — the market-tempered consensus probability ALSO
    #      beats the implied price. Since the consensus is majority-market,
    #      surviving it means the model's edge is big enough that even after
    #      respecting the market there is still value ("a good pairing").
    # `value_bet` (what Kelly staking and the CLI flag key off) requires BOTH.
    def value_block(win_prob: float, blend_prob: float, moneyline: int) -> Dict:
        implied = implied_probability(moneyline)
        edge = win_prob - implied            # model's own edge
        blend_edge = blend_prob - implied    # edge after respecting the market
        kelly = kelly_fraction(win_prob, moneyline)
        model_sees_value = bool(edge > 0 and kelly > 0)
        consensus_agrees = bool(blend_edge > 0)
        return {
            "market_moneyline": moneyline,
            "market_decimal_odds": round(american_to_decimal(moneyline), 3),
            "market_implied_prob_pct": round(implied * 100, 2),
            "model_win_prob_pct": round(win_prob * 100, 2),
            "edge_pct": round(edge * 100, 2),
            "consensus_edge_pct": round(blend_edge * 100, 2),
            "kelly_fraction_full": round(kelly, 4),
            "kelly_fraction_half": round(max(0.0, kelly) / 2, 4),
            "model_sees_value": model_sees_value,
            "consensus_agrees": consensus_agrees,
            "value_bet": bool(model_sees_value and consensus_agrees),
        }

    report = {
        "matchup": {
            "fighter_a": result.a_name,
            "fighter_b": result.b_name,
            "scheduled_rounds": odds.scheduled_rounds,
            "is_title_fight": odds.is_title_fight,
            "iterations": n,
            "avg_rounds_simulated": round(result.avg_rounds, 3),
        },
        "win_probability": {
            # The model's own pick — pure simulation output, no market input.
            "fighter_a_pct": round(a_win_prob * 100, 2),
            "fighter_b_pct": round(b_win_prob * 100, 2),
            "draw_pct": _pct(draws, n),
        },
        # Best-accuracy forecast (model blended with the devigged market).
        # Reported separately: informs the confirmation gate and the reader,
        # never overwrites the model's pick.
        "consensus_forecast": {
            "applied": blended,
            "model_weight": model_weight if blended else 1.0,
            "fighter_a_pct": round(a_blend_prob * 100, 2),
            "fighter_b_pct": round((1.0 - a_blend_prob) * 100, 2) if decisive else 0.0,
            "market_devig_a_pct": round(
                devig_probabilities(odds.fighter_a_moneyline,
                                    odds.fighter_b_moneyline)[0] * 100, 2)
                if blended else None,
        },
        "method_of_victory_matrix": {
            "fighter_a": method_matrix("A"),
            "fighter_b": method_matrix("B"),
        },
        # Fight-level outcome probabilities (side-agnostic), derived from the
        # same sim counts at zero extra cost. These are first-class predictions
        # that get logged and graded by calibration alongside the winner.
        "fight_level": _fight_level(result, odds),
        "finish_round_distribution": {
            "fighter_a": {f"round_{r}": c
                          for r, c in result.finish_round_counts["A"].items()},
            "fighter_b": {f"round_{r}": c
                          for r, c in result.finish_round_counts["B"].items()},
        },
        "value_betting": {
            "fighter_a": value_block(a_win_prob, a_blend_prob,
                                     odds.fighter_a_moneyline),
            "fighter_b": value_block(b_win_prob, 1.0 - a_blend_prob,
                                     odds.fighter_b_moneyline),
        },
    }
    return report


def report_to_json(report: Dict, indent: int = 2) -> str:
    """Serialize the report dict to a pretty JSON string."""
    return json.dumps(report, indent=indent)


# ---------------------------------------------------------------------------
# Dependency-free CLI table rendering
# ---------------------------------------------------------------------------
def _render_table(headers: List[str], rows: List[List[str]], title: str = "") -> str:
    """Render a simple box-drawn table. All cells are stringified upstream."""
    cols = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(str(row[i])))

    def line(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def fmt(cells: List[str]) -> str:
        return "│" + "│".join(f" {str(c):<{widths[i]}} " for i, c in enumerate(cells)) + "│"

    out: List[str] = []
    if title:
        out.append(title)
    out.append(line("┌", "┬", "┐"))
    out.append(fmt(headers))
    out.append(line("├", "┼", "┤"))
    for row in rows:
        out.append(fmt(row))
    out.append(line("└", "┴", "┘"))
    return "\n".join(out)


def render_cli(report: Dict) -> str:
    """Produce the full human-readable CLI report from a report dict."""
    m = report["matchup"]
    wp = report["win_probability"]
    mv = report["method_of_victory_matrix"]
    vb = report["value_betting"]
    a_name, b_name = m["fighter_a"], m["fighter_b"]

    blocks: List[str] = []

    # Header
    blocks.append("═" * 74)
    blocks.append(f"  UFC MATCHUP SIMULATION  —  {a_name}  vs  {b_name}")
    blocks.append(
        f"  {m['scheduled_rounds']}-round"
        f"{' TITLE' if m['is_title_fight'] else ''} bout   |   "
        f"{m['iterations']:,} iterations   |   "
        f"avg {m['avg_rounds_simulated']} rounds"
    )
    blocks.append("═" * 74)

    # Win probability: the model's pick, with the market-blended consensus
    # forecast alongside when real odds exist.
    cf = report.get("consensus_forecast", {})
    if cf.get("applied"):
        blocks.append(_render_table(
            ["Fighter", "Model (pick)", "Consensus (w/ market)"],
            [[a_name, f"{wp['fighter_a_pct']:.2f}%", f"{cf['fighter_a_pct']:.2f}%"],
             [b_name, f"{wp['fighter_b_pct']:.2f}%", f"{cf['fighter_b_pct']:.2f}%"],
             ["Draw", f"{wp['draw_pct']:.2f}%", "—"]],
            title="\n▸ WIN PROBABILITY",
        ))
    else:
        blocks.append(_render_table(
            ["Fighter", "Win Probability"],
            [[a_name, f"{wp['fighter_a_pct']:.2f}%"],
             [b_name, f"{wp['fighter_b_pct']:.2f}%"],
             ["Draw", f"{wp['draw_pct']:.2f}%"]],
            title="\n▸ WIN PROBABILITY",
        ))

    # Method of victory matrix
    methods = ["KO/TKO", "Submission", "Unanimous Decision", "Split/Majority Decision"]
    rows = [[method] +
            [f"{mv['fighter_a'][method]:.2f}%", f"{mv['fighter_b'][method]:.2f}%"]
            for method in methods]
    blocks.append(_render_table(
        ["Method", a_name, b_name], rows,
        title="\n▸ METHOD OF VICTORY MATRIX  (% of all fights)",
    ))

    # Value betting
    def vrow(side_name: str, v: Dict) -> List[str]:
        if v["value_bet"]:
            flag = "★ VALUE"                      # model edge + market survives
        elif v.get("model_sees_value"):
            flag = "◇ model only"                 # unconfirmed — market disagrees hard
        else:
            flag = "—"
        return [
            side_name,
            f"{v['market_moneyline']:+d}",
            f"{v['market_implied_prob_pct']:.2f}%",
            f"{v['model_win_prob_pct']:.2f}%",
            f"{v['edge_pct']:+.2f}%",
            f"{v['kelly_fraction_half']*100:.2f}%",
            flag,
        ]

    blocks.append(_render_table(
        ["Fighter", "Line", "Implied", "Model", "Edge", "½-Kelly", "Flag"],
        [vrow(a_name, vb["fighter_a"]), vrow(b_name, vb["fighter_b"])],
        title="\n▸ VALUE BETTING  (Kelly vs market moneyline)",
    ))
    blocks.append(
        "\n  ½-Kelly = suggested stake as % of bankroll (half-Kelly, clamped "
        "at 0).\n  ★ VALUE = model sees an edge AND it survives the "
        "market-blended consensus.\n  ◇ model only = model edge the consensus "
        "rejects — informational, not a bet signal."
    )

    return "\n".join(blocks)
