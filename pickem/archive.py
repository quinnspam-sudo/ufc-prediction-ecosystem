"""
pickem/archive.py
=================
Keep every Underdog UFC pick'em board we see, then grade it against what
actually happened. This is the dataset that answers "do unders really beat
Underdog's breakevens on real lines?" — the model can't answer it alone.

    # 1. capture the board (pickem/capture.js in your logged-in browser),
    #    save the string to a file, then:
    ./.venv/bin/python -m pickem.archive add board.txt --card "UFC 333" --date 2026-10-24

    # 2. after the card is in the store (the mirror refreshes weekly):
    ./.venv/bin/python -m store.ingest && ./.venv/bin/python -m store.snapshots
    ./.venv/bin/python -m pickem.archive grade

    # 3. how are real lines doing?
    ./.venv/bin/python -m pickem.archive report

Storage: data_store/pickem_lines.csv (tracked in git — this history can't be
rebuilt). One row per prop per capture; capturing the same card again later
adds a new snapshot (line movement), and `report` uses the latest capture of
each prop.

Grading (from data_store/history.sqlite):
  SS   fighter's significant strikes landed      TD   takedowns landed
  FT   fight length in minutes                   ML   1 if the fighter won
  KO   1 if the fighter won by KO/TKO            SUB  1 if won by submission
  FIN  1 if the fighter won by KO/TKO or submission
A no-contest grades every prop on that fight as "void"; a draw voids ML.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import math
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT, "data_store", "pickem_lines.csv")
DB_PATH = os.path.join(ROOT, "data_store", "history.sqlite")

FIELDS = ["card", "fight_date", "captured_at", "stat", "fighter", "line",
          "higher_pct", "lower_pct", "even_line", "store_fighter_id", "actual",
          "fight_minutes", "result", "graded_at"]
STATS = {"ML", "SS", "FT", "TD", "KO", "SUB", "FIN"}

# Standard-entry breakeven per leg (all legs must hit) at Underdog's usual
# multipliers — an assumption; check the multipliers shown in the app.
BREAKEVEN = {"2-pick 3x": (1 / 3) ** (1 / 2), "3-pick 6x": (1 / 6) ** (1 / 3),
             "4-pick 10x": (1 / 10) ** (1 / 4), "5-pick 20x": (1 / 20) ** (1 / 5)}


def _read() -> List[Dict[str, str]]:
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _write(rows: List[Dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    tmp = CSV_PATH + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    os.replace(tmp, CSV_PATH)


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------
def parse_board(text: str) -> List[Dict[str, str]]:
    """'STAT|Fighter|line|hi|lo;...' (capture.js output) -> prop dicts."""
    props = []
    for item in re.split(r"[;\n]", text.strip()):
        if not item.strip():
            continue
        parts = item.split("|")
        if len(parts) != 5 or parts[0] not in STATS:
            raise ValueError(f"bad board row: {item!r}")
        stat, fighter, line, hi, lo = (p.strip() for p in parts)
        float(line)
        even = stat != "ML" and hi == "50" and lo == "50"
        props.append({"stat": stat, "fighter": fighter, "line": line,
                      "higher_pct": hi, "lower_pct": lo,
                      "even_line": "1" if even else "0"})
    return props


def add(board_path: str, card: str, fight_date: str,
        captured_at: Optional[str] = None) -> int:
    datetime.strptime(fight_date, "%Y-%m-%d")
    props = parse_board(open(board_path, encoding="utf-8").read())
    stamp = captured_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = _read()
    for p in props:
        rows.append({"card": card, "fight_date": fight_date,
                     "captured_at": stamp, **p})
    _write(rows)
    return len(props)


# ---------------------------------------------------------------------------
# grade
# ---------------------------------------------------------------------------
def _norm(name: str) -> str:
    name = name.lower().replace("'", "").replace("-", " ").replace(".", "")
    return " ".join(name.split())


def _match(name: str, candidates: Dict[str, str]) -> Optional[str]:
    """Underdog name -> store fighter_id among that night's fighters.
    Handles spellings like Loneer/Lone'er, Jasmin/Iasmin, Cameron/Cam."""
    n = _norm(name)
    by_norm = {_norm(v): k for k, v in candidates.items()}
    if n in by_norm:
        return by_norm[n]
    last = n.split()[-1]
    same_last = [k for k, v in candidates.items() if _norm(v).split()[-1] == last]
    if len(same_last) == 1:
        return same_last[0]
    best = difflib.get_close_matches(n, list(by_norm), n=1, cutoff=0.6)
    return by_norm[best[0]] if best else None


def grade(db_path: str = DB_PATH) -> Dict[str, int]:
    rows = _read()
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    latest_in_store = con.execute("SELECT MAX(date) FROM fights").fetchone()[0]
    counts = defaultdict(int)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cache: Dict[str, Dict] = {}
    for r in rows:
        if r.get("result"):
            counts["already graded"] += 1
            continue
        d = r["fight_date"]
        if d > latest_in_store:
            counts["card not in store yet"] += 1
            continue
        if d not in cache:
            fights = [dict(f) for f in con.execute(
                "SELECT * FROM fights WHERE date BETWEEN date(?, '-1 day') "
                "AND date(?, '+1 day')", (d, d))]
            names = {f2["fighter_id"]: f2["name"] for f2 in con.execute(
                "SELECT fighter_id, name FROM fighters WHERE fighter_id IN "
                f"({','.join('?' * (2 * len(fights)))})",
                [x for f in fights for x in (f["fighter_a"], f["fighter_b"])])}
            stats = {(s["fight_id"], s["fighter_id"]): s for s in con.execute(
                "SELECT fight_id, fighter_id, SUM(sig_l) ss, SUM(td_l) td "
                "FROM round_stats WHERE fight_id IN "
                f"({','.join('?' * len(fights))}) GROUP BY 1, 2",
                [f["fight_id"] for f in fights])}
            cache[d] = {"fights": fights, "names": names, "stats": stats}
        night = cache[d]
        fid = _match(r["fighter"], night["names"])
        fight = next((f for f in night["fights"]
                      if fid in (f["fighter_a"], f["fighter_b"])), None)
        if not fid or not fight:
            counts["fighter not found (withdrawn?)"] += 1
            continue
        r["store_fighter_id"] = fid
        r["fight_minutes"] = f"{(fight['fight_seconds'] or 0) / 60:.2f}"
        won = fight["winner"] == fid
        st = r["stat"]
        line_stats = night["stats"].get((fight["fight_id"], fid))
        if fight["result"] == "nc" or (st == "ML" and fight["result"] == "draw"):
            r["actual"], r["result"] = "", "void"
        else:
            if st == "SS":
                actual = line_stats["ss"] if line_stats else None
            elif st == "TD":
                actual = line_stats["td"] if line_stats else None
            elif st == "FT":
                actual = round((fight["fight_seconds"] or 0) / 60, 2)
            elif st == "ML":
                actual = int(won)
            elif st == "KO":
                actual = int(won and fight["method"] == "KO/TKO")
            elif st == "SUB":
                actual = int(won and fight["method"] == "SUB")
            else:  # FIN
                actual = int(won and fight["method"] in ("KO/TKO", "SUB"))
            if actual is None:
                counts["no stats for fight"] += 1
                continue
            line = float(r["line"])
            r["actual"] = str(actual)
            r["result"] = ("higher" if actual > line else
                           "lower" if actual < line else "push")
        r["graded_at"] = stamp
        counts["graded"] += 1
    _write(rows)
    return dict(counts)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def _wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return mid - half, mid + half


def report() -> str:
    rows = _read()
    latest = {}
    for r in rows:                                   # latest capture per prop
        key = (r["card"], r["stat"], r["fighter"])
        if key not in latest or r["captured_at"] > latest[key]["captured_at"]:
            latest[key] = r
    graded = [r for r in latest.values() if r.get("result") in ("higher", "lower")]
    cards = sorted({r["card"] for r in latest.values()})
    out = [f"{len(latest)} props archived across {len(cards)} card(s); "
           f"{len(graded)} graded (pushes/voids excluded)."]
    out.append("Breakeven per leg: " + ", ".join(
        f"{k} {v:.1%}" for k, v in BREAKEVEN.items()))
    out.append("")
    out.append(f"{'group':34s} {'n':>4s} {'LOWER hit':>9s}  {'95% CI':>13s}  "
               f"{'mean actual-line':>16s}")
    groups = defaultdict(list)
    for r in graded:
        kind = "even 50/50" if r["even_line"] == "1" else "adjusted"
        groups[(r["stat"], kind)].append(r)
        if r["stat"] in ("SS", "TD") and r["even_line"] == "1":
            groups[("SS+TD", "even 50/50")].append(r)
    for (stat, kind), rs in sorted(groups.items()):
        n = len(rs)
        k = sum(r["result"] == "lower" for r in rs)
        lo, hi = _wilson(k, n)
        diff = [float(r["actual"]) - float(r["line"]) for r in rs
                if r["actual"] not in ("", None)]
        md = sum(diff) / len(diff) if diff else float("nan")
        out.append(f"{stat + ' (' + kind + ')':34s} {n:4d} {k / n:9.1%}  "
                   f"[{lo:5.1%}, {hi:5.1%}]  {md:+16.2f}")
    if not graded:
        out.append("(nothing graded yet)")
    out.append("")
    out.append("Read: a group is only actionable when the LOWER (or HIGHER) "
               "CI sits wholly above the breakeven for the entry you'd play. "
               "Expect ~200 even-line props before that's possible.")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Underdog pick'em board archive")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="append a captured board")
    a.add_argument("board", help="file holding capture.js output")
    a.add_argument("--card", required=True, help='e.g. "UFC 333"')
    a.add_argument("--date", required=True, help="fight date YYYY-MM-DD")
    a.add_argument("--captured-at", help="ISO timestamp (default: now)")
    sub.add_parser("grade", help="grade props whose card is in the store")
    sub.add_parser("report", help="hit rates on real lines")
    args = ap.parse_args(argv)
    if args.cmd == "add":
        n = add(args.board, args.card, args.date, args.captured_at)
        print(f"archived {n} props for {args.card} ({args.date}) -> {CSV_PATH}")
    elif args.cmd == "grade":
        print(grade())
    else:
        print(report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
