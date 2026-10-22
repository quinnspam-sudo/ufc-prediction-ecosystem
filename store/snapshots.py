"""
store/snapshots.py
==================
Point-in-time fighter state: every fighter's record and rate stats exactly as
they stood BEFORE a given date.

    ./.venv/bin/python -m store.snapshots          # (re)build fighter_snapshots

Two entry points, one code path (so training rows and live predictions can
never disagree):

* build_snapshots()  — replays all of UFC history in date order and writes one
  row per (fight, fighter) to the `fighter_snapshots` table: that fighter's
  state going INTO the fight. This is the training feature store.
* state_as_of(date)  — replays history strictly before `date` and returns the
  live state for any fighter, for predicting an upcoming card.

Leakage rule enforced by construction: on each fight date, every snapshot for
that date is taken BEFORE any of that date's fights are added. A fighter's
same-night tournament bouts (early UFCs) therefore never see each other.

Rates follow UFCStats' career definitions (SLpM, SApM, Str. Acc., Str. Def,
TD Avg., TD Acc., TD Def., Sub. Avg.) so they can be checked against the site.
Each rate also has a `_s` version shrunk toward the division's pooled rate,
also point-in-time, so a 30-second KO doesn't produce a 10 SLpM outlier:

    rate_s = (count + K * division_rate) / (exposure + K)
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .ingest import DB_PATH

# Shrinkage strength, in units of each rate's exposure (step 3 tunes these by
# walk-forward log loss; these are reasonable starting priors).
K_MINUTES = 20.0       # per-minute rates (SLpM, SApM, TD/15, Sub/15, ctrl)
K_SIG_ATT = 60.0       # significant-strike accuracy / defense
K_TD_ATT = 8.0         # takedown accuracy / defense

# Running totals kept per fighter (and pooled per division for the priors).
COUNTS = ("minutes", "sig_l", "sig_a", "opp_sig_l", "opp_sig_a", "td_l",
          "td_a", "opp_td_l", "opp_td_a", "sub_att", "ctrl_s", "opp_ctrl_s",
          "kd", "opp_kd", "head_l", "body_l", "leg_l", "dist_l", "clinch_l",
          "ground_l")

# (name, numerator, denominator, scale, K) — scale turns per-minute into the
# UFCStats unit (per 15 min for TD/Sub); defense rates are 1 - opp share.
RATES = (
    ("slpm",    "sig_l",     "minutes",   1.0,  K_MINUTES),
    ("sapm",    "opp_sig_l", "minutes",   1.0,  K_MINUTES),
    ("str_acc", "sig_l",     "sig_a",     1.0,  K_SIG_ATT),
    ("str_def", "opp_sig_l", "opp_sig_a", 1.0,  K_SIG_ATT),   # 1 - x
    ("td_avg",  "td_l",      "minutes",   15.0, K_MINUTES),
    ("td_acc",  "td_l",      "td_a",      1.0,  K_TD_ATT),
    ("td_def",  "opp_td_l",  "opp_td_a",  1.0,  K_TD_ATT),    # 1 - x
    ("sub_avg", "sub_att",   "minutes",   15.0, K_MINUTES),
    ("ctrl_pct", "ctrl_s",   "minutes",   1 / 60.0, K_MINUTES),
)
DEFENSE = {"str_def", "td_def"}


def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------------------
# Load fights + per-fighter per-fight stat lines
# ---------------------------------------------------------------------------
def _load(con: sqlite3.Connection, before: Optional[str] = None):
    where = "WHERE date < ?" if before else ""
    fights = [dict(r) for r in con.execute(
        f"SELECT * FROM fights {where} ORDER BY date, fight_id",
        (before,) if before else ())]
    lines: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(
        lambda: defaultdict(int))
    for r in con.execute("""
            SELECT fight_id, fighter_id,
                   SUM(kd) kd, SUM(sig_l) sig_l, SUM(sig_a) sig_a,
                   SUM(td_l) td_l, SUM(td_a) td_a, SUM(sub_att) sub_att,
                   SUM(ctrl_s) ctrl_s, SUM(head_l) head_l, SUM(body_l) body_l,
                   SUM(leg_l) leg_l, SUM(dist_l) dist_l,
                   SUM(clinch_l) clinch_l, SUM(ground_l) ground_l
            FROM round_stats GROUP BY fight_id, fighter_id"""):
        lines[(r["fight_id"], r["fighter_id"])] = {
            k: (r[k] or 0) for k in r.keys() if k not in ("fight_id",
                                                          "fighter_id")}
    dob = {r["fighter_id"]: r["dob"] for r in con.execute(
        "SELECT fighter_id, dob FROM fighters")}
    return fights, lines, dob


def _fight_counts(f: Dict, me: str, opp: str, lines) -> Optional[Dict]:
    """This fighter's contribution from one fight (None if no stat lines)."""
    mine, theirs = lines.get((f["fight_id"], me)), lines.get((f["fight_id"], opp))
    if mine is None or theirs is None or not f["fight_seconds"]:
        return None
    c = {"minutes": f["fight_seconds"] / 60.0}
    for k in ("sig_l", "sig_a", "td_l", "td_a", "sub_att", "ctrl_s", "kd",
              "head_l", "body_l", "leg_l", "dist_l", "clinch_l", "ground_l"):
        c[k] = mine[k]
    for k in ("sig_l", "sig_a", "td_l", "td_a", "ctrl_s", "kd"):
        c["opp_" + k] = theirs[k]
    return c


# ---------------------------------------------------------------------------
# Accumulator
# ---------------------------------------------------------------------------
class _Fighter:
    __slots__ = ("totals", "n", "wins", "losses", "draws", "ncs", "ko_w",
                 "sub_w", "dec_w", "ko_l", "sub_l", "dec_l", "titles",
                 "five_rd", "last_date", "streak", "results")

    def __init__(self) -> None:
        self.totals = dict.fromkeys(COUNTS, 0.0)
        self.n = self.wins = self.losses = self.draws = self.ncs = 0
        self.ko_w = self.sub_w = self.dec_w = 0
        self.ko_l = self.sub_l = self.dec_l = 0
        self.titles = self.five_rd = self.streak = 0
        self.last_date: Optional[str] = None
        self.results: List[str] = []


class History:
    """Replays fights in date order; snapshot() never sees the current date."""

    def __init__(self) -> None:
        self.fighters: Dict[str, _Fighter] = defaultdict(_Fighter)
        self.division: Dict[str, Dict[str, float]] = defaultdict(
            lambda: dict.fromkeys(COUNTS, 0.0))
        self.pooled = dict.fromkeys(COUNTS, 0.0)

    def add(self, f: Dict, lines) -> None:
        for me, opp in ((f["fighter_a"], f["fighter_b"]),
                        (f["fighter_b"], f["fighter_a"])):
            s = self.fighters[me]
            s.n += 1
            s.last_date = f["date"]
            s.titles += f["is_title"]
            s.five_rd += (f["scheduled_rounds"] or 0) >= 5
            if f["result"] == "win":
                won = f["winner"] == me
                m = f["method"]
                if won:
                    s.wins += 1
                    s.ko_w += m == "KO/TKO"
                    s.sub_w += m == "SUB"
                    s.dec_w += m == "DEC"
                    s.streak = s.streak + 1 if s.streak > 0 else 1
                else:
                    s.losses += 1
                    s.ko_l += m == "KO/TKO"
                    s.sub_l += m == "SUB"
                    s.dec_l += m == "DEC"
                    s.streak = s.streak - 1 if s.streak < 0 else -1
                s.results.append("W" if won else "L")
            else:
                s.draws += f["result"] == "draw"
                s.ncs += f["result"] == "nc"
                s.results.append("D" if f["result"] == "draw" else "N")
            c = _fight_counts(f, me, opp, lines)
            if c:
                for k, v in c.items():
                    s.totals[k] += v
                    self.division[f["weight_class"]][k] += v
                    self.pooled[k] += v

    def snapshot(self, fighter_id: str, as_of: str, weight_class: str,
                 dob: Optional[str]) -> Dict[str, Any]:
        s = self.fighters.get(fighter_id) or _Fighter()
        t = s.totals
        prior = self.division.get(weight_class)
        if not prior or prior["minutes"] < 600:      # thin division: pool all
            prior = self.pooled
        out: Dict[str, Any] = {
            "n_fights": s.n, "wins": s.wins, "losses": s.losses,
            "draws": s.draws, "no_contests": s.ncs,
            "ko_wins": s.ko_w, "sub_wins": s.sub_w, "dec_wins": s.dec_w,
            "ko_losses": s.ko_l, "sub_losses": s.sub_l, "dec_losses": s.dec_l,
            "title_fights": s.titles, "five_round_fights": s.five_rd,
            "win_streak": s.streak,
            "last3": "".join(s.results[-3:]),
            "last_fight_date": s.last_date,
            "days_since_last": _days(s.last_date, as_of),
            "age_days": _days(dob, as_of),
            "stat_minutes": round(t["minutes"], 3),
        }
        for k in COUNTS[1:]:
            out[k] = int(t[k])
        for name, num, den, scale, k in RATES:
            raw = _ratio(t[num], t[den])
            pr = _ratio(prior[num], prior[den])
            shrunk = (t[num] + k * (pr or 0.0)) / (t[den] + k)
            if name in DEFENSE:
                raw = None if raw is None else 1.0 - raw
                shrunk = 1.0 - shrunk
            out[name] = None if raw is None else round(raw * scale, 4)
            out[name + "_s"] = round(shrunk * scale, 4)
        return out


def _ratio(a: float, b: float) -> Optional[float]:
    return a / b if b else None


def _days(start: Optional[str], end: str) -> Optional[int]:
    if not start:
        return None
    from datetime import date
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _by_date(fights: List[Dict]) -> Iterable[Tuple[str, List[Dict]]]:
    i = 0
    while i < len(fights):
        j = i
        while j < len(fights) and fights[j]["date"] == fights[i]["date"]:
            j += 1
        yield fights[i]["date"], fights[i:j]
        i = j


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def compute_snapshots(con: sqlite3.Connection,
                      before: Optional[str] = None) -> List[Dict[str, Any]]:
    """Pre-fight snapshot rows for every fight (optionally only fights
    dated before `before`, which is how the leakage test truncates history)."""
    fights, lines, dob = _load(con, before)
    hist = History()
    rows = []
    for day, todays in _by_date(fights):
        for f in todays:                    # snapshot first...
            for side, me in (("a", f["fighter_a"]), ("b", f["fighter_b"])):
                snap = hist.snapshot(me, day, f["weight_class"], dob.get(me))
                rows.append({"fight_id": f["fight_id"], "fighter_id": me,
                             "side": side, "as_of": day, **snap})
        for f in todays:                    # ...then learn from the day
            hist.add(f, lines)
    return rows


def state_as_of(as_of: str, fighter_ids: Iterable[str],
                weight_class: str, db_path: str = DB_PATH) -> Dict[str, Dict]:
    """Live state of `fighter_ids` going into a fight on `as_of`."""
    con = _connect(db_path)
    try:
        fights, lines, dob = _load(con, before=as_of)
        hist = History()
        for f in fights:
            hist.add(f, lines)
        return {fid: hist.snapshot(fid, as_of, weight_class, dob.get(fid))
                for fid in fighter_ids}
    finally:
        con.close()


def build_snapshots(db_path: str = DB_PATH) -> int:
    con = _connect(db_path)
    try:
        rows = compute_snapshots(con)
        cols = list(rows[0])
        con.execute("DROP TABLE IF EXISTS fighter_snapshots")
        con.execute(
            f"CREATE TABLE fighter_snapshots ({', '.join(cols)}, "
            "PRIMARY KEY (fight_id, fighter_id))")
        con.executemany(
            f"INSERT INTO fighter_snapshots VALUES ({','.join('?' * len(cols))})",
            [tuple(r[c] for c in cols) for r in rows])
        con.commit()
        return len(rows)
    finally:
        con.close()


if __name__ == "__main__":
    print(f"fighter_snapshots: {build_snapshots()} rows")
