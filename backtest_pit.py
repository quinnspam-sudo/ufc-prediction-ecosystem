"""
backtest_pit.py
===============
Honest re-baseline (roundtable step 2): the SAME simulator as backtest.py, fed
point-in-time fighter stats from the store instead of today's career averages.

    ./.venv/bin/python backtest_pit.py
    ./.venv/bin/python backtest_pit.py --odds path/to/ufc-master.csv
    ./.venv/bin/python backtest_pit.py --since 2015-01-01 --no-leaky

Variants, all scored on the identical fight set:
  leaky       backtest.py's inputs (data_store/backtest_cache.json, as-of-today
              career stats). Only fights the old cache covers.
  pit         store snapshots: raw career rates as of fight night, falling back
              to the division-shrunk rate when a fighter has no UFC minutes yet.
  pit_shrunk  store snapshots, always the division-shrunk rates.

Every fight is simulated in both A/B orders and averaged, because the
simulator gives the A slot a small systematic edge (~1pt) and backtest.py
always put the actual winner in A. The only difference between variants is
where the stats come from, so any gap between leaky and pit is the leak.

With --odds (shortlikeafox/ultimate_ufc_dataset ufc-master.csv), fights that
have closing lines are also scored against the de-vigged market favourite.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

from backtest import CACHE_PATH, _age_on, _build_raw, _map_wc
from data_ingestion import FighterRawStats
from feature_engineering import build_matchup
from simulation_engine import run_simulation
from store.ingest import DB_PATH

OUT_PATH = os.path.join(os.path.dirname(__file__), "reports",
                        "backtest_pit.json")
METHODS = {"KO/TKO": "KO/TKO", "SUB": "Submission", "DEC": "Decision"}


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
def _pit_raw(snap: Dict, fighter: Dict, wc: str, shrunk: bool) -> FighterRawStats:
    """FighterRawStats from a pre-fight snapshot, mirroring backtest._build_raw
    field for field so only the data source differs."""
    def rate(k: str, default: float) -> float:
        v = snap[k + "_s"] if shrunk else snap[k]
        if v is None:
            v = snap[k + "_s"]
        return default if v is None else v

    n = max(snap["n_fights"], 1)
    sapm = rate("sapm", 3.5)
    td_avg = rate("td_avg", 1.0)
    return FighterRawStats(
        name=fighter["name"],
        age=max(18, int(snap["age_days"] / 365.25)) if snap["age_days"] else 30,
        height_in=fighter["height_in"] or 70.0,
        reach_in=fighter["reach_in"] or 72.0,
        weight_class=wc,
        stance=(fighter["stance"] or "Orthodox").split()[0].title(),
        slpm=rate("slpm", 3.5),
        strike_acc=rate("str_acc", 0.45),
        sapm=sapm,
        strike_def=rate("str_def", 0.55),
        td_avg=td_avg,
        td_acc=rate("td_acc", 0.35),
        td_def=rate("td_def", 0.6),
        sub_avg=rate("sub_avg", 0.4),
        sub_def=0.65,
        control_time_pct=min(0.9, td_avg * 0.12 + 0.15),
        career_knockdowns_suffered=snap["ko_losses"] * 2,
        career_sig_strikes_absorbed=int(sapm * 12 * n),
        career_fights=n,
        career_wins=snap["wins"],
        career_ko_wins=snap["ko_wins"],
        career_sub_wins=snap["sub_wins"],
        round_strike_output=[55, 53, 51, 49, 47],
        last_fight_date=snap["last_fight_date"] or "2023-01-01",
    )


def _load_fights(con: sqlite3.Connection, since: str) -> List[Dict]:
    con.row_factory = sqlite3.Row
    fights = [dict(r) for r in con.execute(
        "SELECT * FROM fights WHERE date >= ? AND result = 'win' "
        "ORDER BY date, fight_id", (since,))]
    snaps = {(r["fight_id"], r["fighter_id"]): dict(r) for r in con.execute(
        "SELECT * FROM fighter_snapshots WHERE as_of >= ?", (since,))}
    fighters = {r["fighter_id"]: dict(r) for r in con.execute(
        "SELECT * FROM fighters")}
    for f in fights:
        f["loser"] = f["fighter_b"] if f["winner"] == f["fighter_a"] else f["fighter_a"]
        f["snap_w"] = snaps[(f["fight_id"], f["winner"])]
        f["snap_l"] = snaps[(f["fight_id"], f["loser"])]
        f["fw"], f["fl"] = fighters[f["winner"]], fighters[f["loser"]]
    return fights


def _leaky_index() -> Dict[Tuple[str, str], Tuple[Dict, Dict, Dict]]:
    """(date, winner_id) -> (cache fight, winner data, loser data)."""
    if not os.path.exists(CACHE_PATH):
        return {}
    cache = json.load(open(CACHE_PATH))
    out = {}
    for f in cache["fights"]:
        w, l = cache["fighters"].get(f["winner_url"]), cache["fighters"].get(f["loser_url"])
        if w and l and "slpm" in w and "slpm" in l:
            wid = f["winner_url"].rstrip("/").rsplit("/", 1)[-1]
            out[(f["date"], wid)] = (f, w, l)
    return out


def _norm(name: str) -> str:
    return " ".join(name.lower().replace(".", "").replace("-", " ").split())


def _load_odds(path: str) -> Dict[Tuple[str, str], Tuple[str, float, float]]:
    """(date, norm name) -> (opponent norm name, own ml, opp ml)."""
    out = {}
    for r in csv.DictReader(open(path)):
        try:
            rm, bm = float(r["R_odds"]), float(r["B_odds"])
        except (ValueError, KeyError):
            continue
        d, rf, bf = r["date"][:10], _norm(r["R_fighter"]), _norm(r["B_fighter"])
        out[(d, rf)] = (bf, rm, bm)
        out[(d, bf)] = (rf, bm, rm)
    return out


def _implied(ml: float) -> float:
    return 100.0 / (ml + 100.0) if ml > 0 else -ml / (-ml + 100.0)


# ---------------------------------------------------------------------------
# Simulate
# ---------------------------------------------------------------------------
def _simulate(a: FighterRawStats, b: FighterRawStats, rounds: int,
              iterations: int) -> Tuple[float, Dict[str, float]]:
    """P(a wins) and fight-level method shares, averaged over both A/B orders."""
    p_sum, meth = 0.0, {"KO/TKO": 0.0, "Submission": 0.0, "Decision": 0.0}
    for x, y, a_is_first in ((a, b, True), (b, a, False)):
        px, py = build_matchup(x, y, rounds)
        sim = run_simulation(px, py, rounds, iterations=iterations, seed=7)
        decisive = sim.a_wins + sim.b_wins
        pa = (sim.a_wins if a_is_first else sim.b_wins) / max(decisive, 1)
        p_sum += pa
        mc = sim.method_counts
        counts = {
            "KO/TKO": mc["A"]["KO/TKO"] + mc["B"]["KO/TKO"],
            "Submission": mc["A"]["Submission"] + mc["B"]["Submission"],
            "Decision": sum(mc[s][k] for s in "AB" for k in
                            ("Unanimous Decision", "Split/Majority Decision")),
        }
        tot = max(sum(counts.values()), 1)
        for k in meth:
            meth[k] += counts[k] / tot / 2
    return p_sum / 2, meth


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------
def _score(rows: List[Dict], key: str) -> Dict[str, Any]:
    """Metrics for variant `key`; each row holds p (on the actual winner)."""
    ps = [r[key]["p"] for r in rows]
    n = len(ps)
    if not n:
        return {"n": 0}
    eps = 1e-4
    out = {
        "n": n,
        "winner_accuracy_pct": round(100 * sum(p > 0.5 for p in ps) / n, 1),
        "brier": round(sum((1 - p) ** 2 for p in ps) / n, 4),
        "log_loss": round(-sum(math.log(min(max(p, eps), 1 - eps))
                               for p in ps) / n, 4),
    }
    buckets = {"50-60": [0, 0], "60-70": [0, 0], "70-80": [0, 0], "80+": [0, 0]}
    for p in ps:
        c = max(p, 1 - p)
        b = ("80+" if c >= .8 else "70-80" if c >= .7 else "60-70" if c >= .6
             else "50-60")
        buckets[b][0] += p > 0.5
        buckets[b][1] += 1
    out["accuracy_by_confidence"] = {
        k: {"n": v[1], "accuracy_pct": round(100 * v[0] / v[1], 1) if v[1] else None}
        for k, v in buckets.items()}
    best = min((sum((1 - (0.5 + lam / 20 * (p - 0.5))) ** 2 for p in ps) / n, lam / 20)
               for lam in range(2, 21))
    out["fitted_lambda"], out["brier_at_lambda"] = best[1], round(best[0], 4)
    meth = [r for r in rows if r["method"] and "m" in r[key]]
    if meth:
        out["method_accuracy_pct"] = round(100 * sum(
            max(r[key]["m"], key=r[key]["m"].get) == r["method"] for r in meth)
            / len(meth), 1)
        out["distance_accuracy_pct"] = round(100 * sum(
            (r[key]["m"]["Decision"] >= 0.5) == (r["method"] == "Decision")
            for r in meth) / len(meth), 1)
    return out


def run(since: str, iterations: int, odds_path: Optional[str],
        leaky: bool) -> Dict[str, Any]:
    con = sqlite3.connect(DB_PATH)
    fights = _load_fights(con, since)
    leaky_idx = _leaky_index() if leaky else {}
    odds = _load_odds(odds_path) if odds_path else {}

    rows = []
    for i, f in enumerate(fights):
        wc = _map_wc(f["weight_class"])
        rounds = f["scheduled_rounds"] if f["scheduled_rounds"] in (3, 5) else 3
        row: Dict[str, Any] = {
            "fight_id": f["fight_id"], "date": f["date"],
            "winner": f["fw"]["name"], "loser": f["fl"]["name"],
            "method": METHODS.get(f["method"]),
            "debut": min(f["snap_w"]["n_fights"], f["snap_l"]["n_fights"]) == 0,
        }
        for key, shrunk in (("pit", False), ("pit_shrunk", True)):
            a = _pit_raw(f["snap_w"], f["fw"], wc, shrunk)
            b = _pit_raw(f["snap_l"], f["fl"], wc, shrunk)
            p, m = _simulate(a, b, rounds, iterations)
            row[key] = {"p": round(p, 4), "m": m}
        hit = leaky_idx.get((f["date"], f["winner"]))
        if hit:
            cf, wd, ld = hit
            a = _build_raw(wd, cf["winner"], wc, f["date"])
            b = _build_raw(ld, cf["loser"], wc, f["date"])
            p, m = _simulate(a, b, rounds, iterations)
            row["leaky"] = {"p": round(p, 4), "m": m}
        o = odds.get((f["date"], _norm(f["fw"]["name"])))
        if o and o[0] == _norm(f["fl"]["name"]):
            iw, il = _implied(o[1]), _implied(o[2])
            row["market"] = {"p": round(iw / (iw + il), 4)}
        rows.append(row)
        if (i + 1) % 200 == 0:
            print(f"[sim] {i + 1}/{len(fights)}", file=sys.stderr)

    def block(subset: List[Dict], keys) -> Dict[str, Any]:
        return {k: _score(subset, k) for k in keys}

    variants = ["pit", "pit_shrunk"] + (["leaky"] if leak_ok(rows) else [])
    common = [r for r in rows if all(k in r for k in variants)]
    mkt = [r for r in common if "market" in r]
    out = {
        "note": ("Same simulator for every variant; only the stat source "
                 "differs. Each fight averaged over both A/B orders."),
        "since": since, "iterations_per_order": iterations,
        "fights_in_store": len(fights),
        "all_fights": block(common, variants),
        "debut_fights": block([r for r in common if r["debut"]], variants),
        "veteran_fights": block([r for r in common if not r["debut"]], variants),
        "vs_market": block(mkt, variants + ["market"]) if mkt else None,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    json.dump(out, open(OUT_PATH, "w"), indent=2)
    json.dump(rows, open(OUT_PATH.replace(".json", "_rows.json"), "w"))
    return out


def leak_ok(rows: List[Dict]) -> bool:
    return any("leaky" in r for r in rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2024-04-13")         # UFC 300
    ap.add_argument("--iterations", type=int, default=2500)  # per A/B order
    ap.add_argument("--odds", help="ufc-master.csv with closing moneylines")
    ap.add_argument("--no-leaky", action="store_true")
    args = ap.parse_args()
    res = run(args.since, args.iterations, args.odds, not args.no_leaky)
    print(json.dumps(res, indent=2))
