"""
calibration.py
==============
Self-measuring accuracy + adaptive confidence. This is what lets the system
IMPROVE over time instead of staying static.

Flow
----
1. Every time we predict a real-stat fight, we log the model's win probability
   (`log_prediction`).
2. When ESPN reports that fight's result, we resolve the entry with the actual
   outcome (`resolve_result`) and score it with the Brier score
   (squared error between predicted probability and 0/1 outcome; 0.25 = a
   coin-flip baseline, lower is better).
3. Each cycle we refit a single "confidence shrink" factor λ ∈ (0,1] that pulls
   predictions toward 50/50 by the amount that would have MINIMISED historical
   Brier (`fit_shrink`). If the model has been over-confident, λ < 1 tempers it;
   if well-calibrated, λ ≈ 1. λ is only applied once we have enough resolved
   fights (`MIN_RESOLVED`), so we never over-tune on noise.

Everything here is local JSON — no network, no budget cost.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

LOG_PATH = os.path.join(os.path.dirname(__file__), "data_store", "predictions_log.json")

# Don't apply any learned shrink until we've resolved at least this many fights —
# below this, the sample is too small to trust (avoid over-fitting to noise).
MIN_RESOLVED = 15
BASELINE_BRIER = 0.25   # a 50/50 guess scores 0.25; we want to beat this


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(name: str) -> str:
    return " ".join((name or "").lower().replace(".", "").split())


def load_log() -> Dict[str, Any]:
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH) as fh:
            return json.load(fh)
    return {"entries": []}


def save_log(log: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w") as fh:
        json.dump(log, fh, indent=2)


def log_prediction(log: Dict[str, Any], label: str, a_name: str, b_name: str,
                   p_a: float, event: str = "") -> None:
    """
    Record (or update) the OPEN prediction for a matchup. `p_a` is the model's
    raw probability that fighter A wins. Only call for real-stat fights.
    """
    for e in reversed(log["entries"]):
        if e["label"] == label and not e["resolved"]:
            e["p_a"] = round(p_a, 4)          # refresh the still-open prediction
            e["logged"] = _now()
            return
    log["entries"].append({
        "label": label, "a": a_name, "b": b_name,
        "p_a": round(p_a, 4), "event": event,
        "logged": _now(), "resolved": False,
        "actual_a": None, "brier": None, "resolved_at": None,
    })


def resolve_result(log: Dict[str, Any], winner_name: str, loser_name: str) -> Optional[Dict]:
    """
    Resolve the open prediction matching this bout (by the two fighter names).
    Returns the resolved entry, or None if no open prediction matched.
    """
    w, l = _norm(winner_name), _norm(loser_name)
    for e in reversed(log["entries"]):
        if e["resolved"]:
            continue
        names = {_norm(e["a"]), _norm(e["b"])}
        if names == {w, l}:
            actual_a = 1.0 if _norm(e["a"]) == w else 0.0
            e["actual_a"] = actual_a
            e["brier"] = round((e["p_a"] - actual_a) ** 2, 4)
            e["resolved"] = True
            e["resolved_at"] = _now()
            return e
    return None


def _brier_at(entries: List[Dict], lam: float) -> float:
    """Mean Brier over resolved entries if predictions were shrunk by λ."""
    resolved = [e for e in entries if e["resolved"] and e["actual_a"] is not None]
    if not resolved:
        return BASELINE_BRIER
    total = 0.0
    for e in resolved:
        p = 0.5 + lam * (e["p_a"] - 0.5)      # shrink toward 0.5
        total += (p - e["actual_a"]) ** 2
    return total / len(resolved)


def fit_shrink(log: Dict[str, Any]) -> float:
    """
    Grid-search the λ ∈ [0.1, 1.0] that minimises historical Brier. Returns 1.0
    (no shrink) until we have MIN_RESOLVED fights, so we don't tune on noise.
    """
    resolved = [e for e in log["entries"] if e["resolved"]]
    if len(resolved) < MIN_RESOLVED:
        return 1.0
    best_lam, best_brier = 1.0, float("inf")
    for i in range(2, 21):                     # 0.10 .. 1.00
        lam = i / 20.0
        b = _brier_at(log["entries"], lam)
        if b < best_brier:
            best_lam, best_brier = lam, b
    return best_lam


def summary(log: Dict[str, Any], shrink: float) -> Dict[str, Any]:
    """Human-readable calibration scorecard for reports/calibration.json."""
    resolved = [e for e in log["entries"] if e["resolved"] and e["actual_a"] is not None]
    n = len(resolved)
    open_n = sum(1 for e in log["entries"] if not e["resolved"])
    if n == 0:
        return {
            "resolved_fights": 0, "open_predictions": open_n,
            "brier_raw": None, "brier_calibrated": None, "accuracy_pct": None,
            "baseline_brier": BASELINE_BRIER, "shrink_factor": shrink,
            "min_resolved_before_adapt": MIN_RESOLVED,
            "note": "Collecting outcomes. Adaptive shrink activates after "
                    f"{MIN_RESOLVED} resolved fights.",
        }
    # Accuracy = share of fights where the model's favorite actually won.
    correct = sum(1 for e in resolved
                  if (e["p_a"] >= 0.5) == (e["actual_a"] == 1.0))
    return {
        "resolved_fights": n, "open_predictions": open_n,
        "brier_raw": round(_brier_at(resolved, 1.0), 4),
        "brier_calibrated": round(_brier_at(resolved, shrink), 4),
        "accuracy_pct": round(100.0 * correct / n, 1),
        "baseline_brier": BASELINE_BRIER, "shrink_factor": shrink,
        "min_resolved_before_adapt": MIN_RESOLVED,
        "beating_coinflip": bool(_brier_at(resolved, shrink) < BASELINE_BRIER),
    }
