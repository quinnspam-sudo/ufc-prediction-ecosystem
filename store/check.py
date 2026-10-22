"""
store/check.py
==============
Gate-zero checks for the point-in-time store. Run after every rebuild:

    ./.venv/bin/python -m store.check

1. Strict past-only: every snapshot's last counted fight is before its as-of
   date, and its record matches an independent SQL count of earlier fights.
2. Future invariance: rebuilding with all fights on/after a cutoff deleted
   leaves every pre-cutoff snapshot byte-identical — the future cannot change
   the past, including the division priors behind the shrunk rates.
3. Accuracy vs UFCStats: today's state for curated fighters reproduces the
   career stats the live scraper stored from UFCStats (independent source).

Exits non-zero if 1 or 2 fail; 3 reports error statistics (a few fighters
drift because their stored UFCStats numbers predate their latest fight).
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from datetime import date, timedelta

from .ingest import DB_PATH, ROOT
from .snapshots import _connect, compute_snapshots, state_as_of

CUTOFFS = ("2010-01-01", "2018-06-01", "2024-04-13")
STATE_PATH = os.path.join(ROOT, "data_store", "state.json")


def check_past_only(con) -> list:
    errors = []
    expected = {}
    for r in con.execute("""
            SELECT s.fight_id, s.fighter_id,
                   COUNT(f.fight_id) n,
                   SUM(f.result = 'win' AND f.winner = s.fighter_id) w,
                   SUM(f.result = 'win' AND f.winner != s.fighter_id) l,
                   MAX(f.date) last
            FROM fighter_snapshots s
            LEFT JOIN fights f
              ON (f.fighter_a = s.fighter_id OR f.fighter_b = s.fighter_id)
             AND f.date < s.as_of
            GROUP BY s.fight_id, s.fighter_id"""):
        expected[(r[0], r[1])] = (r[2], r[3] or 0, r[4] or 0, r[5])
    for s in con.execute("""SELECT fight_id, fighter_id, as_of, n_fights,
                                   wins, losses, last_fight_date
                            FROM fighter_snapshots"""):
        if s["last_fight_date"] and s["last_fight_date"] >= s["as_of"]:
            errors.append(f"{s['fight_id']}/{s['fighter_id']}: last fight "
                          f"{s['last_fight_date']} not before {s['as_of']}")
        got = (s["n_fights"], s["wins"], s["losses"], s["last_fight_date"])
        if got != expected[(s["fight_id"], s["fighter_id"])]:
            errors.append(f"{s['fight_id']}/{s['fighter_id']}: record {got} "
                          f"!= SQL {expected[(s['fight_id'], s['fighter_id'])]}")
    return errors


def check_future_invariance(con) -> list:
    full = {(r["fight_id"], r["fighter_id"]): r for r in compute_snapshots(con)}
    errors = []
    for cutoff in CUTOFFS:
        truncated = compute_snapshots(con, before=cutoff)
        for r in truncated:
            if full[(r["fight_id"], r["fighter_id"])] != r:
                errors.append(f"cutoff {cutoff}: {r['fight_id']}/"
                              f"{r['fighter_id']} changed when future removed")
        print(f"  cutoff {cutoff}: {len(truncated)} snapshots compared")
    return errors


def check_vs_ufcstats(con) -> None:
    try:
        state = json.load(open(STATE_PATH))
    except OSError:
        print("  (no state.json — skipped)")
        return
    ids = {r["name"].lower(): r["fighter_id"] for r in con.execute(
        "SELECT name, fighter_id FROM fighters")}
    picks = {}
    for f in state["fighters"].values():
        if f.get("needs_stats") or f.get("stats_approx"):
            continue
        fid = ids.get((f.get("display_name") or f.get("name", "")).lower())
        if fid:
            picks[fid] = f
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    live = state_as_of(tomorrow, picks, "Open Weight")
    # Like-for-like only: 5+ UFC fights (small samples swing on one bout) and
    # the stored stats were scraped after the fighter's latest fight.
    picks = {fid: f for fid, f in picks.items()
             if live[fid]["n_fights"] >= 5
             and f.get("_updated", "")[:10] > (live[fid]["last_fight_date"] or "")}
    # store field -> state.json field
    fields = {"slpm": "slpm", "sapm": "sapm", "str_acc": "strike_acc",
              "str_def": "strike_def", "td_avg": "td_avg", "td_acc": "td_acc",
              "td_def": "td_def", "sub_avg": "sub_avg"}
    print(f"  {len(picks)} curated fighters (5+ fights, stats fresher than "
          "their last bout)")
    print(f"  {'stat':8} {'median |err|':>12} {'within tol':>11}")
    for k, sk in fields.items():
        tol = 0.02 if k in ("str_acc", "str_def", "td_acc", "td_def") else 0.1
        errs = [abs(live[fid][k] - f[sk]) for fid, f in picks.items()
                if live[fid][k] is not None and f.get(sk) is not None]
        if not errs:
            continue
        ok = sum(e <= tol for e in errs) / len(errs)
        print(f"  {k:8} {statistics.median(errs):12.3f} {ok:10.0%}")


def main() -> int:
    con = _connect(DB_PATH)
    print("1. strict past-only")
    e1 = check_past_only(con)
    print(f"  {'PASS' if not e1 else 'FAIL'} ({len(e1)} errors)")
    print("2. future invariance")
    e2 = check_future_invariance(con)
    print(f"  {'PASS' if not e2 else 'FAIL'} ({len(e2)} errors)")
    print("3. accuracy vs UFCStats career stats")
    check_vs_ufcstats(con)
    for e in (e1 + e2)[:10]:
        print("  !", e)
    return 1 if e1 or e2 else 0


if __name__ == "__main__":
    sys.exit(main())
