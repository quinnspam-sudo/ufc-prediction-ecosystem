"""
benchmark.py
============
Compare this system's predictions against an EXTERNAL model — e.g. Dan
McInerney's open-sourced mma-ai (github.com/DanMcInerney/mma-ai, weights and
database on HuggingFace), which reports ~8% ROI since 2024 and is the best
free public benchmark available.

Why: two decent models agreeing is mild confirmation; two decent models
DISAGREEING is a flag that one of them is missing something. Mining the
disagreements is where the improvement signal lives.

Usage
-----
1. Produce an external predictions file (any model), JSON of the form:

       [
         {"a": "Fighter A Name", "b": "Fighter B Name", "p_a": 0.63},
         ...
       ]

   `p_a` = external model's probability that fighter `a` wins.

2. Run:

       python benchmark.py external_preds.json
       python benchmark.py external_preds.json --threshold 0.10

Outputs a side-by-side table of our (blended, calibrated) probability vs the
external one, flags disagreements above the threshold, and — for any bouts
already resolved in the calibration ledger — scores BOTH models on Brier so
you can see who was right where they diverged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional

import calibration

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")


def _norm(name: str) -> str:
    return " ".join((name or "").lower().replace(".", "").split())


def _load_our_predictions() -> Dict[frozenset, Dict]:
    """Map {frozenset(norm_a, norm_b): {a, b, p_a}} from reports/index.json +
    per-matchup report files."""
    index_path = os.path.join(REPORTS_DIR, "index.json")
    if not os.path.exists(index_path):
        return {}
    with open(index_path) as fh:
        index = json.load(fh).get("matchups", {})
    out: Dict[frozenset, Dict] = {}
    for label, meta in index.items():
        path = os.path.join(os.path.dirname(__file__), meta["report_file"])
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            rep = json.load(fh)
        a = rep["matchup"]["fighter_a"]
        b = rep["matchup"]["fighter_b"]
        wp = rep["win_probability"]
        decisive = wp["fighter_a_pct"] + wp["fighter_b_pct"]
        if decisive <= 0:
            continue
        out[frozenset({_norm(a), _norm(b)})] = {
            "label": label, "a": a, "b": b,
            "p_a": wp["fighter_a_pct"] / decisive,
        }
    return out


def _resolved_outcomes() -> Dict[frozenset, Dict]:
    """Map bouts already resolved in the calibration ledger to their outcome."""
    log = calibration.load_log()
    out: Dict[frozenset, Dict] = {}
    for e in log["entries"]:
        if e["resolved"] and e.get("actual_a") is not None:
            out[frozenset({_norm(e["a"]), _norm(e["b"])})] = e
    return out


def compare(external: List[Dict], threshold: float = 0.10) -> Dict:
    ours = _load_our_predictions()
    resolved = _resolved_outcomes()

    matched, disagreements = [], []
    our_briers, ext_briers = [], []

    for ext in external:
        key = frozenset({_norm(ext["a"]), _norm(ext["b"])})
        mine = ours.get(key)
        if mine is None:
            continue
        # Align the external p_a to OUR fighter-a orientation.
        ext_p_a = (ext["p_a"] if _norm(ext["a"]) == _norm(mine["a"])
                   else 1.0 - ext["p_a"])
        gap = mine["p_a"] - ext_p_a
        row = {
            "matchup": mine["label"],
            "our_p_a": round(mine["p_a"], 3),
            "external_p_a": round(ext_p_a, 3),
            "gap": round(gap, 3),
            "disagree": abs(gap) >= threshold,
        }
        outcome = resolved.get(key)
        if outcome is not None:
            actual = outcome["actual_a"] if _norm(outcome["a"]) == _norm(mine["a"]) \
                else 1.0 - outcome["actual_a"]
            row["actual_a"] = actual
            row["our_brier"] = round((mine["p_a"] - actual) ** 2, 4)
            row["external_brier"] = round((ext_p_a - actual) ** 2, 4)
            our_briers.append(row["our_brier"])
            ext_briers.append(row["external_brier"])
        matched.append(row)
        if row["disagree"]:
            disagreements.append(row)

    return {
        "matched_bouts": len(matched),
        "disagreements": sorted(disagreements, key=lambda r: -abs(r["gap"])),
        "rows": matched,
        "scored_bouts": len(our_briers),
        "our_mean_brier": round(sum(our_briers) / len(our_briers), 4) if our_briers else None,
        "external_mean_brier": round(sum(ext_briers) / len(ext_briers), 4) if ext_briers else None,
    }


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Benchmark vs an external model")
    p.add_argument("external_json", help="Path to external predictions JSON")
    p.add_argument("--threshold", type=float, default=0.10,
                   help="Probability gap that counts as a disagreement")
    p.add_argument("--json-out", default=None)
    args = p.parse_args(argv)

    with open(args.external_json) as fh:
        external = json.load(fh)

    result = compare(external, args.threshold)

    print(f"Matched bouts: {result['matched_bouts']}   "
          f"Disagreements (≥{args.threshold:.0%} gap): {len(result['disagreements'])}")
    for r in result["rows"]:
        flag = "  ⚠ DISAGREE" if r["disagree"] else ""
        scored = (f"   [actual={r['actual_a']:.0f} "
                  f"ours={r['our_brier']} ext={r['external_brier']}]"
                  if "actual_a" in r else "")
        print(f"  {r['matchup']}: ours {r['our_p_a']:.3f} vs ext "
              f"{r['external_p_a']:.3f} (gap {r['gap']:+.3f}){flag}{scored}")
    if result["our_mean_brier"] is not None:
        print(f"\nResolved-bout Brier — ours: {result['our_mean_brier']}  "
              f"external: {result['external_mean_brier']}  (lower is better)")
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(result, fh, indent=2)
        print(f"Written to {args.json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
