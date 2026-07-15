"""
sync.py
=======
Auto-update orchestrator. One cycle:

    1. Load persisted state (seed from watchlist.json on first run).
    2. Run each enabled live source (results, odds, injury news) defensively.
    3. Merge patches/odds into the store; collect prediction-relevant changes.
    4. Re-run the Monte Carlo prediction for every matchup whose inputs changed
       (or all matchups on first run / --force).
    5. Write reports/<matchup>.json + reports/index.json and persist state.
    6. Optionally git commit + push (so GitHub always holds the latest state).

Run modes:
    python sync.py                 # fetch live, predict changed, no git
    python sync.py --push          # ...and commit+push if anything changed
    python sync.py --force         # re-predict every matchup regardless
    python sync.py --no-network    # skip live sources (deterministic local test)
    python sync.py --iterations N  # Monte Carlo iterations (default 10000)

Designed to be the single entrypoint a scheduler (GitHub Actions triggered by
cron-job.org) calls each cycle.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List

import analytics_reporting as ar
import calibration
import config
import data_store as ds
from data_ingestion import MatchupOdds
from main import run_pipeline

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def _enabled_sources(no_network: bool) -> List:
    """Instantiate the live sources. Kept import-local so --no-network works
    even if `requests` isn't installed."""
    if no_network:
        return []
    from sources.espn_schedule import EspnScheduleSource
    from sources.espn_results import EspnResultsSource
    from sources.odds_api import OddsApiSource
    from sources.injury_news import InjuryNewsSource
    # Schedule discovery runs FIRST so newly-added fighters exist before the
    # results/odds/news sources try to match against them this same cycle.
    return [EspnScheduleSource(), EspnResultsSource(), OddsApiSource(),
            InjuryNewsSource()]


def _apply_shrink(report: Dict[str, Any], shrink: float) -> None:
    """
    Pull the decisive win probabilities toward 50/50 by the learned calibration
    factor λ, then recompute the value metrics on the calibrated probabilities.
    A no-op when λ == 1.0 (default until enough fights have resolved).
    """
    if shrink >= 0.999:
        return
    wp = report["win_probability"]
    a, b = wp["fighter_a_pct"], wp["fighter_b_pct"]
    decisive = a + b
    if decisive <= 0:
        return
    p_a = a / decisive
    p_a2 = 0.5 + shrink * (p_a - 0.5)          # shrink toward 0.5
    wp["fighter_a_pct"] = round(p_a2 * decisive, 2)
    wp["fighter_b_pct"] = round((1 - p_a2) * decisive, 2)
    wp["calibration_shrink_applied"] = shrink

    # Recompute value on calibrated probabilities.
    for side, prob in (("fighter_a", p_a2), ("fighter_b", 1 - p_a2)):
        v = report["value_betting"][side]
        ml = v["market_moneyline"]
        implied = ar.implied_probability(ml)
        kelly = ar.kelly_fraction(prob, ml)
        v["model_win_prob_pct"] = round(prob * 100, 2)
        v["edge_pct"] = round((prob - implied) * 100, 2)
        v["kelly_fraction_full"] = round(kelly, 4)
        v["kelly_fraction_half"] = round(max(0.0, kelly) / 2, 4)
        v["value_bet"] = bool((prob - implied) > 0 and kelly > 0)


def _predict_matchup(state: Dict[str, Any], m: Dict[str, Any],
                     iterations: int, shrink: float = 1.0) -> Dict[str, Any]:
    """Run the full pipeline for one matchup dict and return the report."""
    a_raw = ds.state_fighter_to_raw(state["fighters"][m["a"]])
    b_raw = ds.state_fighter_to_raw(state["fighters"][m["b"]])
    odds = MatchupOdds(
        fighter_a_moneyline=int(m.get("a_ml", -110)),
        fighter_b_moneyline=int(m.get("b_ml", -110)),
        scheduled_rounds=int(m.get("rounds", 3)),
        is_title_fight=bool(m.get("is_title_fight", False)),
    )
    needs_stats = (state["fighters"][m["a"]].get("needs_stats")
                   or state["fighters"][m["b"]].get("needs_stats"))
    # Placeholder-stat fights are ~50/50 regardless — don't waste iterations.
    iters = config.SIM_ITERATIONS_PLACEHOLDER if needs_stats else iterations
    report = run_pipeline(a_raw, b_raw, odds, iterations=iters, seed=42)

    # Adaptive calibration: temper confidence by the learned shrink (real fights
    # only; placeholder fights are already 50/50 and get their value suppressed).
    if not needs_stats:
        _apply_shrink(report, shrink)

    # SAFETY: a fighter on placeholder (league-average) stats produces a ~50/50
    # model, which would falsely flag the market underdog as "value" on every
    # such fight. Suppress value flags unless BOTH fighters have real stats, so
    # the system never emits a misleading bet signal it can't actually support.
    if needs_stats:
        for side in ("fighter_a", "fighter_b"):
            report["value_betting"][side]["value_bet"] = False
        report["value_betting"]["insufficient_data"] = True
        report["value_betting"]["note"] = (
            "Value flags suppressed: one or both fighters use placeholder "
            "league-average stats. Add real stats in watchlist.json to enable.")

    # Annotate with any live health flags so the report is self-describing.
    for side, key in (("fighter_a", m["a"]), ("fighter_b", m["b"])):
        rec = state["fighters"][key]
        report["matchup"][f"{side}_flags"] = {
            "active_injury": rec.get("active_injury", False),
            "missed_weight": rec.get("missed_weight", False),
            "withdrawn": rec.get("withdrawn", False),
            "injury_note": rec.get("injury_note", ""),
            "stats_approx": rec.get("stats_approx", False),
            "needs_real_stats": rec.get("needs_stats", False),
        }
    return report


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=os.path.dirname(__file__),
                          capture_output=True, text=True)


def _commit_and_push(summary: str) -> bool:
    """Commit changed reports/state and push. Returns True if a commit was made."""
    _git("add", "-A", "reports", "data_store")
    status = _git("status", "--porcelain", "reports", "data_store").stdout.strip()
    if not status:
        return False
    # Identity: rely on CI/global config, but fall back so local runs don't fail.
    env_name = _git("config", "user.name").stdout.strip()
    cfg = [] if env_name else ["-c", "user.name=UFC AutoUpdater",
                               "-c", "user.email=autoupdate@localhost"]
    msg = f"auto-update: {summary}"
    res = subprocess.run(["git", *cfg, "commit", "-m", msg],
                         cwd=os.path.dirname(__file__), capture_output=True, text=True)
    if res.returncode != 0:
        print("commit failed:", res.stderr, file=sys.stderr)
        return False
    push = _git("push")
    if push.returncode != 0:
        print("push failed (committed locally):", push.stderr, file=sys.stderr)
    return True


def run_cycle(iterations: int = config.SIM_ITERATIONS, no_network: bool = False,
              force: bool = False, push: bool = False) -> Dict[str, Any]:
    first_run = not os.path.exists(ds.STATE_PATH)
    state = ds.load_state()

    # Snapshot budget-throttle meta BEFORE sources run. If a source spends budget
    # (an odds call) or updates a per-fighter news cooldown, that state MUST be
    # committed even on an otherwise no-op cycle — otherwise the next CI run
    # (fresh checkout) wouldn't see it and could call the API again too soon,
    # defeating the throttle. This makes "spent budget" a commit-worthy change.
    _meta0 = state.get("meta", {})
    odds_count0 = _meta0.get("odds", {}).get("count", 0)
    news_sig0 = json.dumps(_meta0.get("news_last", {}), sort_keys=True)

    all_changes: List = []
    changed_fighter_keys = set()
    changed_matchup_labels = set()
    discovered_labels: List[str] = []
    fight_results: List[Dict[str, str]] = []
    notes: List[str] = []

    for src in _enabled_sources(no_network):
        result = src.safe_fetch(state)
        notes.extend(f"[{src.name}] {n}" for n in result.notes)
        # Auto-discovery additions first, so later sources see the new fighters.
        if result.new_fighters or result.new_matchups:
            added_f, added_m = ds.add_discovered(
                state, result.new_fighters, result.new_matchups)
            discovered_labels.extend([l for l in added_m if l])
            for lbl in added_m:
                if lbl:
                    ds.record_events(state, [{"type": "matchup_discovered",
                                              "detail": lbl}])
        fchanges = ds.apply_patches(state, result.patches)
        for key, field, old, new in fchanges:
            changed_fighter_keys.add(key)
        all_changes.extend(fchanges)
        changed_matchup_labels.update(ds.apply_odds(state, result.odds))
        fight_results.extend(result.fight_results)
        ds.record_events(state, result.events)

    # ── Calibration: score any newly-completed fights, then refit confidence ──
    cal_log = calibration.load_log()
    resolved_now = []
    for fr in fight_results:
        entry = calibration.resolve_result(cal_log, fr["winner"], fr["loser"])
        if entry:
            resolved_now.append(entry)
            ds.record_events(state, [{"type": "calibration_resolved",
                                      "detail": f"{fr['winner']} beat {fr['loser']} "
                                                f"(Brier {entry['brier']})"}])
    shrink = calibration.fit_shrink(cal_log)
    state.setdefault("meta", {})["calibration"] = {"shrink": shrink}

    # Which matchups need re-prediction?
    to_predict = []
    for m in state["matchups"]:
        label = m.get("label", f"{m['a']} vs {m['b']}")
        touched = (m["a"] in changed_fighter_keys or m["b"] in changed_fighter_keys
                   or label in changed_matchup_labels)
        report_path = os.path.join(REPORTS_DIR, f"{_slug(label)}.json")
        if force or first_run or touched or not os.path.exists(report_path):
            to_predict.append(m)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    index = {}
    for m in state["matchups"]:
        label = m.get("label", f"{m['a']} vs {m['b']}")
        path = os.path.join(REPORTS_DIR, f"{_slug(label)}.json")
        if m in to_predict:
            report = _predict_matchup(state, m, iterations, shrink)
            with open(path, "w") as fh:
                json.dump(report, fh, indent=2)
            # Log real-stat predictions so we can score them when they resolve.
            if not report["matchup"].get("fighter_a_flags", {}).get("needs_real_stats"):
                calibration.log_prediction(
                    cal_log, label,
                    report["matchup"]["fighter_a"], report["matchup"]["fighter_b"],
                    report["win_probability"]["fighter_a_pct"] / 100.0,
                    event=m.get("event", ""))
        # Build the index entry from whatever report exists.
        if os.path.exists(path):
            with open(path) as fh:
                rep = json.load(fh)
            index[label] = {
                "win_probability": rep["win_probability"],
                "value_flags": {
                    "fighter_a": rep["value_betting"]["fighter_a"]["value_bet"],
                    "fighter_b": rep["value_betting"]["fighter_b"]["value_bet"],
                },
                "report_file": f"reports/{_slug(label)}.json",
            }
    with open(os.path.join(REPORTS_DIR, "index.json"), "w") as fh:
        json.dump({"matchups": index}, fh, indent=2)

    # Persist calibration ledger + publish the accuracy scorecard.
    calibration.save_log(cal_log)
    with open(os.path.join(REPORTS_DIR, "calibration.json"), "w") as fh:
        json.dump(calibration.summary(cal_log, shrink), fh, indent=2)

    ds.save_state(state)

    summary = (f"{len(to_predict)} matchup(s) predicted, "
               f"{len(discovered_labels)} new bout(s), "
               f"{len(all_changes)} field change(s), "
               f"{len(changed_matchup_labels)} odds move(s), "
               f"{len(resolved_now)} result(s) scored")

    # A budget-throttle update (odds call spent, or news cooldown stamps) MUST
    # persist so the throttle survives across CI runs.
    budget_meta_changed = (
        state.get("meta", {}).get("odds", {}).get("count", 0) != odds_count0
        or json.dumps(state.get("meta", {}).get("news_last", {}), sort_keys=True) != news_sig0
    )

    # Only commit when something MEANINGFUL changed — not just the last_sync
    # heartbeat. Otherwise a frequent schedule would spam junk commits.
    meaningful = bool(to_predict or all_changes or changed_matchup_labels
                      or discovered_labels or resolved_now or budget_meta_changed)
    committed = False
    if push and meaningful:
        committed = _commit_and_push(summary)

    return {
        "first_run": first_run,
        "changes": all_changes,
        "odds_moves": sorted(changed_matchup_labels),
        "discovered": discovered_labels,
        "predicted": [m.get("label") for m in to_predict],
        "resolved": [f"{e['a']} vs {e['b']} (Brier {e['brier']})" for e in resolved_now],
        "shrink": shrink,
        "notes": notes,
        "summary": summary,
        "committed": committed,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="UFC ecosystem auto-update cycle")
    p.add_argument("--iterations", type=int, default=config.SIM_ITERATIONS)
    p.add_argument("--no-network", action="store_true",
                   help="skip live sources (deterministic local test)")
    p.add_argument("--force", action="store_true",
                   help="re-predict every matchup regardless of change")
    p.add_argument("--push", action="store_true",
                   help="commit + push changed reports/state to GitHub")
    args = p.parse_args(argv)

    out = run_cycle(iterations=args.iterations, no_network=args.no_network,
                    force=args.force, push=args.push)

    print("── UFC auto-update cycle ─────────────────────────────")
    print("first run     :", out["first_run"])
    print("discovered    :", ", ".join(out["discovered"]) or "(none)")
    print("predicted     :", ", ".join(out["predicted"]) or "(none)")
    print("field changes :", len(out["changes"]))
    for k, f, old, new in out["changes"]:
        print(f"    {k}.{f}: {old!r} -> {new!r}")
    print("odds moves    :", ", ".join(out["odds_moves"]) or "(none)")
    print("results scored:", ", ".join(out["resolved"]) or "(none)")
    print("calib. shrink :", out["shrink"], "(1.0 = no adjustment yet)")
    print("committed     :", out["committed"])
    if out["notes"]:
        print("notes         :")
        for n in out["notes"][:20]:
            print("    -", n)
    print("summary       :", out["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
