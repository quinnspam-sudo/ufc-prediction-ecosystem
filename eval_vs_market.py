"""
eval_vs_market.py
=================
Join the backtest cache (data_store/backtest_cache.json, built by backtest.py)
with a historical odds CSV (ufc-master.csv schema: R_fighter, B_fighter,
R_odds, B_odds American moneylines, date) and answer the questions a raw
accuracy number can't:

  1. Does the model beat the MARKET FAVORITE baseline on the same fights?
  2. Would flat-betting the model's picks at closing odds have made money?
  3. Does the market-blend + λ-shrink pipeline (what production runs) improve
     Brier over the raw sim on real odds?

Usage:
    ./.venv/bin/python eval_vs_market.py path/to/ufc-master.csv

Same lookahead caveat as backtest.py (rate stats are as-of-today).
"""

from __future__ import annotations

import csv
import json
import os
import sys
from typing import Dict, Optional, Tuple

from backtest import _build_raw, _map_wc, CACHE_PATH
from feature_engineering import build_matchup
from simulation_engine import run_simulation

ITERATIONS = 1000
LAMBDA = 0.55            # backtest-fitted calibration prior
MODEL_WEIGHT = 0.4       # production market-blend weight


def _norm(name: str) -> str:
    return " ".join((name or "").lower().replace(".", "").replace("'", "").split())


def _decimal(ml: float) -> float:
    return ml / 100.0 + 1.0 if ml > 0 else 100.0 / abs(ml) + 1.0


def _load_odds(csv_path: str) -> Dict[Tuple[str, str, str], Tuple[float, float]]:
    """{(date, norm_a, norm_b): (a_ml, b_ml)} keyed both name orders."""
    out: Dict[Tuple[str, str, str], Tuple[float, float]] = {}
    for row in csv.DictReader(open(csv_path)):
        try:
            r_ml, b_ml = float(row["R_odds"]), float(row["B_odds"])
        except (ValueError, KeyError):
            continue
        d = (row.get("date") or "")[:10]
        r, b = _norm(row.get("R_fighter", "")), _norm(row.get("B_fighter", ""))
        if not (d and r and b):
            continue
        out[(d, r, b)] = (r_ml, b_ml)
        out[(d, b, r)] = (b_ml, r_ml)
    return out


def main(csv_path: str) -> Dict:
    cache = json.load(open(CACHE_PATH))
    odds = _load_odds(csv_path)
    fights, fighters = cache["fights"], cache["fighters"]

    n = 0
    model_correct = market_correct = 0
    blend_correct = 0
    # Flat 1u on the model's pick at closing odds.
    pnl_model = pnl_market = 0.0
    # Brier for raw sim, shrunk sim, and production blend(shrunk, market).
    br_raw = br_shrunk = br_blend = br_market = 0.0
    disagree = disagree_model_right = 0

    for f in fights:
        key = (f["date"], _norm(f["winner"]), _norm(f["loser"]))
        if key not in odds:
            continue
        w_ml, l_ml = odds[key]
        wd, ld = fighters.get(f["winner_url"]), fighters.get(f["loser_url"])
        if not wd or not ld or "slpm" not in wd or "slpm" not in ld:
            continue
        wc = _map_wc(f["weight_class_raw"])
        rounds = 5 if f["round"] > 3 else 3
        try:
            a_raw = _build_raw(wd, f["winner"], wc, f["date"])
            b_raw = _build_raw(ld, f["loser"], wc, f["date"])
            pa, pb = build_matchup(a_raw, b_raw, rounds)
            sim = run_simulation(pa, pb, rounds, iterations=ITERATIONS, seed=7)
        except Exception:
            continue
        decisive = sim.a_wins + sim.b_wins
        if not decisive:
            continue
        p_raw = sim.a_wins / decisive                     # P(actual winner)
        p_shrunk = 0.5 + LAMBDA * (p_raw - 0.5)
        iw, il = 1.0 / _decimal(w_ml), 1.0 / _decimal(l_ml)
        p_mkt = iw / (iw + il)                            # devigged, on winner
        p_blend = MODEL_WEIGHT * p_shrunk + (1 - MODEL_WEIGHT) * p_mkt

        n += 1
        model_correct += p_raw >= 0.5
        market_correct += p_mkt >= 0.5
        blend_correct += p_blend >= 0.5
        br_raw += (p_raw - 1.0) ** 2
        br_shrunk += (p_shrunk - 1.0) ** 2
        br_blend += (p_blend - 1.0) ** 2
        br_market += (p_mkt - 1.0) ** 2

        # Flat-stake PnL: model picks its favorite; market picks its own.
        if p_raw >= 0.5:                    # model picked the actual winner
            pnl_model += _decimal(w_ml) - 1.0
        else:
            pnl_model -= 1.0
        if p_mkt >= 0.5:
            pnl_market += _decimal(w_ml) - 1.0
        else:
            pnl_market -= 1.0

        if (p_raw >= 0.5) != (p_mkt >= 0.5):
            disagree += 1
            disagree_model_right += p_raw >= 0.5

    out = {
        "caveat": "Rate stats as-of-today (lookahead) — treat model numbers "
                  "as upper bounds; market numbers are exact.",
        "fights_matched": n,
        "accuracy_pct": {
            "model_raw": round(100 * model_correct / n, 1),
            "market_favorite": round(100 * market_correct / n, 1),
            "production_blend": round(100 * blend_correct / n, 1),
        },
        "brier_mean": {
            "model_raw": round(br_raw / n, 4),
            "model_shrunk_055": round(br_shrunk / n, 4),
            "market_devig": round(br_market / n, 4),
            "production_blend": round(br_blend / n, 4),
        },
        "flat_1u_pnl_units": {
            "model_picks": round(pnl_model, 1),
            "market_favorites": round(pnl_market, 1),
        },
        "model_vs_market_disagreements": {
            "n": disagree,
            "model_right_pct": round(100 * disagree_model_right / disagree, 1)
                if disagree else None,
        },
    }
    json.dump(out, open(os.path.join(os.path.dirname(__file__), "reports",
                                     "eval_vs_market.json"), "w"), indent=2)
    return out


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1]), indent=2))
