"""
parlay_engine.py
================
Builds recommended parlays for each upcoming card from the per-matchup
reports.

Card policy
-----------
* **Numbered / PPV card** (event title matches "UFC <number>"): one 4-6 leg
  parlay (as many of the top legs as clear the quality bar, min 4, max 6).
* **Fight Night / other card**: a 3-leg AND a 5-leg parlay.

Leg selection (strict — a parlay is only as honest as its weakest leg)
----------------------------------------------------------------------
A fight yields an eligible leg only when ALL hold:
  1. Both fighters have real stats (no placeholder ~50/50 sims).
  2. Real market odds exist for the bout.
  3. The MODEL's pick and the market-blended CONSENSUS pick agree on the
     same fighter (disagreement = uncertainty, not parlay material).
  4. The consensus win probability for that side >= MIN_LEG_PROB.

Legs are ranked by consensus probability (the best accuracy estimate) and the
top-N become the parlay. For each leg we annotate the most likely METHOD and
round bucket from the simulation's method-of-victory matrix, so upgrading a
leg to a method/round prop (for a better price) is a one-look decision.

Parlay math
-----------
  combined_prob  = product of leg consensus probabilities (independence
                   assumption — fights on one card are effectively independent)
  combined_odds  = product of leg decimal odds
  ev_per_unit    = combined_prob * combined_odds - 1   (>0 == +EV at the quoted prices)

Everything is written to reports/parlays.json by the sync cycle.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")

# A leg must be at least this likely (consensus) to belong in a parlay.
MIN_LEG_PROB = 0.60
NUMBERED_CARD_RE = re.compile(r"\bUFC\s*#?\s*\d+\b", re.IGNORECASE)


def _american_to_decimal(ml: int) -> float:
    return ml / 100.0 + 1.0 if ml > 0 else 100.0 / abs(ml) + 1.0


def _surname(name: str) -> str:
    parts = [p for p in re.sub(r"[^A-Za-z\s]", "", name).split() if len(p) > 2]
    return parts[-1].lower() if parts else ""


def _event_for(m: Dict[str, Any], events: List[str]) -> str:
    """Resolve a matchup's event, attaching event-less bouts (often the main
    event, discovered separately) to a card whose title names both fighters."""
    ev = m.get("event", "") or ""
    if ev:
        return ev
    label = (m.get("label") or "").lower()
    for candidate in events:
        cl = candidate.lower()
        surnames = [_surname(x) for x in re.split(r"\s+vs\.?\s+", label)]
        if surnames and all(s and s in cl for s in surnames):
            return candidate
    return "(unassigned)"


def _leg_from_report(m: Dict[str, Any], report: Dict[str, Any]) -> Optional[Dict]:
    """Extract an eligible parlay leg from one matchup report, or None."""
    flags = report["matchup"]
    if (flags.get("fighter_a_flags", {}).get("needs_real_stats")
            or flags.get("fighter_b_flags", {}).get("needs_real_stats")):
        return None
    cf = report.get("consensus_forecast", {})
    if not cf.get("applied"):          # no real market odds -> no parlay leg
        return None

    wp = report["win_probability"]
    model_side = "a" if wp["fighter_a_pct"] >= wp["fighter_b_pct"] else "b"
    cons_side = "a" if cf["fighter_a_pct"] >= cf["fighter_b_pct"] else "b"
    if model_side != cons_side:        # model and consensus disagree -> skip
        return None

    side = cons_side
    name = flags["fighter_a"] if side == "a" else flags["fighter_b"]
    cons_prob = (cf["fighter_a_pct"] if side == "a" else cf["fighter_b_pct"]) / 100.0
    model_prob = (wp["fighter_a_pct"] if side == "a" else wp["fighter_b_pct"]) / 100.0
    if cons_prob < MIN_LEG_PROB:
        return None

    ml = report["value_betting"]["fighter_a" if side == "a" else "fighter_b"][
        "market_moneyline"]

    # Most likely method for the pick (to inform optional prop upgrades).
    mv = report["method_of_victory_matrix"]["fighter_a" if side == "a" else "fighter_b"]
    likely_method = max(mv, key=mv.get)
    # Modal finish round, if the likely method is a finish.
    frd = report["finish_round_distribution"]["fighter_a" if side == "a" else "fighter_b"]
    modal_round = max(frd, key=frd.get) if any(frd.values()) else None

    return {
        "matchup": m.get("label", ""),
        "pick": name,
        "moneyline": ml,
        "decimal_odds": round(_american_to_decimal(ml), 3),
        "consensus_prob": round(cons_prob, 4),
        "model_prob": round(model_prob, 4),
        "likely_method": likely_method,
        "likely_method_pct": mv[likely_method],
        "modal_finish_round": modal_round,
    }


def _assemble(legs: List[Dict], size: int) -> Optional[Dict]:
    """Build a parlay dict from the top `size` legs (None if not enough)."""
    if len(legs) < size:
        return None
    chosen = legs[:size]
    prob = 1.0
    odds = 1.0
    for leg in chosen:
        prob *= leg["consensus_prob"]
        odds *= leg["decimal_odds"]
    return {
        "legs": chosen,
        "leg_count": size,
        "combined_prob_pct": round(prob * 100, 2),
        "combined_decimal_odds": round(odds, 3),
        "combined_american": round((odds - 1) * 100),
        "ev_per_unit": round(prob * odds - 1, 4),
        "positive_ev": bool(prob * odds - 1 > 0),
    }


def build_parlays(state: Dict[str, Any]) -> Dict[str, Any]:
    """Produce the per-card parlay recommendations from existing reports."""
    events = sorted({m.get("event", "") for m in state["matchups"] if m.get("event")})

    # Collect eligible legs per card.
    cards: Dict[str, List[Dict]] = {}
    for m in state["matchups"]:
        label = m.get("label", f"{m['a']} vs {m['b']}")
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
        path = os.path.join(REPORTS_DIR, f"{slug}.json")
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            report = json.load(fh)
        leg = _leg_from_report(m, report)
        if leg is None:
            continue
        cards.setdefault(_event_for(m, events), []).append(leg)

    out: Dict[str, Any] = {"cards": {}, "min_leg_consensus_prob": MIN_LEG_PROB}
    for event, legs in cards.items():
        # Rank by per-leg EV (consensus_prob * decimal_odds), not raw
        # probability: every candidate already clears the 60% confidence
        # floor, so among safe legs we prefer the ones the book underprices.
        # Ranking by probability alone stacks over-priced chalk and bakes the
        # vig of every leg into the parlay.
        legs.sort(key=lambda l: (-(l["consensus_prob"] * l["decimal_odds"]),
                                 -l["consensus_prob"]))
        numbered = bool(NUMBERED_CARD_RE.search(event))
        card: Dict[str, Any] = {
            "card_type": "numbered/PPV" if numbered else "fight_night",
            "eligible_legs": len(legs),
            "parlays": {},
        }
        if numbered:
            # 4-6 legs: take up to 6, but never below 4.
            size = min(6, len(legs))
            p = _assemble(legs, size) if size >= 4 else None
            card["parlays"]["main_card_parlay"] = p
            if p is None:
                card["note"] = (f"Only {len(legs)} eligible legs "
                                f"(need >=4 for a numbered-card parlay).")
        else:
            card["parlays"]["3_leg"] = _assemble(legs, 3)
            card["parlays"]["5_leg"] = _assemble(legs, 5)
            missing = [k for k, v in card["parlays"].items() if v is None]
            if missing:
                card["note"] = (f"Only {len(legs)} eligible legs; "
                                f"{', '.join(missing)} not buildable.")
        out["cards"][event] = card
    return out


def render_cli(parlays: Dict[str, Any]) -> str:
    lines: List[str] = []
    for event, card in parlays.get("cards", {}).items():
        lines.append(f"── {event}  [{card['card_type']}] "
                     f"({card['eligible_legs']} eligible legs)")
        for pname, p in card["parlays"].items():
            if p is None:
                lines.append(f"   {pname}: not buildable")
                continue
            ev_flag = "+EV" if p["positive_ev"] else "-EV"
            lines.append(f"   {pname}: {p['combined_prob_pct']}% to hit @ "
                         f"{p['combined_decimal_odds']}x "
                         f"(+{p['combined_american']}) [{ev_flag} "
                         f"{p['ev_per_unit']:+.2%}]")
            for leg in p["legs"]:
                rnd = (f", modal {leg['modal_finish_round'].replace('_', ' ')}"
                       if leg["modal_finish_round"] and "Decision" not in leg["likely_method"]
                       else "")
                lines.append(f"      • {leg['pick']} ({leg['moneyline']:+d}) "
                             f"cons {leg['consensus_prob']:.0%} — likely "
                             f"{leg['likely_method']}{rnd}")
        if "note" in card:
            lines.append(f"   note: {card['note']}")
    return "\n".join(lines) if lines else "(no cards with eligible parlay legs)"
