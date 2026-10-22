"""
store/ingest.py
===============
Build data_store/history.sqlite from the public UFCStats mirror
(github.com/Greco1899/scrape_ufc_stats, refreshed weekly). The mirror holds
every UFC event, bout result and per-round stat line UFCStats publishes, so we
never have to fight UFCStats' JavaScript bot wall for history.

    ./.venv/bin/python -m store.ingest              # download + rebuild
    ./.venv/bin/python -m store.ingest --offline    # rebuild from data_store/raw/

The raw CSVs (~12MB) and the SQLite file are rebuildable, so both are
gitignored. Rebuilding is idempotent: the database is recreated from scratch.

Fighter identity: per-round stats are keyed by NAME, not UFCStats id. Names
unique in the biometrics file map to its UFCStats id. Nine names belong to two
different people (e.g. Bruno Silva); those resolve to the candidate whose
listed weight is closest to the bout's weight-class limit, and the fight is
flagged `identity_guess=1`. Fighters missing from the biometrics file (the
mirror's newest debutants) get a `name:<slug>` id.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "data_store", "raw")
DB_PATH = os.path.join(ROOT, "data_store", "history.sqlite")

MIRROR = "https://raw.githubusercontent.com/Greco1899/scrape_ufc_stats/main/"
FILES = ("ufc_event_details", "ufc_fight_results", "ufc_fight_stats",
         "ufc_fighter_tott")

# Upper weight limit (lbs) per division — used only to tell same-name
# fighters apart. Catchweights/openweights get None (no disambiguation hint).
DIVISION_LIMITS = {
    "Strawweight": 115, "Flyweight": 125, "Bantamweight": 135,
    "Featherweight": 145, "Lightweight": 155, "Welterweight": 170,
    "Middleweight": 185, "Light Heavyweight": 205, "Heavyweight": 265,
}

SCHEMA = """
CREATE TABLE fighters (
    fighter_id TEXT PRIMARY KEY,   -- UFCStats id, or name:<slug> if unknown
    name       TEXT NOT NULL,
    height_in  REAL,
    reach_in   REAL,
    weight_lbs REAL,
    stance     TEXT,
    dob        TEXT                -- YYYY-MM-DD
);
CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    date     TEXT NOT NULL,        -- YYYY-MM-DD
    location TEXT
);
CREATE TABLE fights (
    fight_id         TEXT PRIMARY KEY,
    event_id         TEXT NOT NULL REFERENCES events(event_id),
    date             TEXT NOT NULL,
    fighter_a        TEXT NOT NULL REFERENCES fighters(fighter_id),
    fighter_b        TEXT NOT NULL REFERENCES fighters(fighter_id),
    winner           TEXT,         -- fighter_id; NULL for draw / no contest
    result           TEXT NOT NULL, -- 'win' | 'draw' | 'nc'
    method           TEXT NOT NULL, -- 'KO/TKO' | 'SUB' | 'DEC' | 'OTHER' | 'NC'
    method_raw       TEXT,
    method_detail    TEXT,
    end_round        INTEGER,
    end_time_s       INTEGER,      -- clock time elapsed in end_round
    fight_seconds    INTEGER,      -- total elapsed fight time
    scheduled_rounds INTEGER,
    round_lengths    TEXT,         -- e.g. '5-5-5' (minutes)
    weight_class     TEXT,         -- e.g. 'Lightweight', "Women's Flyweight"
    is_title         INTEGER NOT NULL,
    is_women         INTEGER NOT NULL,
    referee          TEXT,
    identity_guess   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX fights_date ON fights(date);
CREATE TABLE round_stats (
    fight_id   TEXT NOT NULL REFERENCES fights(fight_id),
    fighter_id TEXT NOT NULL REFERENCES fighters(fighter_id),
    round      INTEGER NOT NULL,
    kd INTEGER, sig_l INTEGER, sig_a INTEGER, tot_l INTEGER, tot_a INTEGER,
    td_l INTEGER, td_a INTEGER, sub_att INTEGER, rev INTEGER, ctrl_s INTEGER,
    head_l INTEGER, head_a INTEGER, body_l INTEGER, body_a INTEGER,
    leg_l INTEGER, leg_a INTEGER, dist_l INTEGER, dist_a INTEGER,
    clinch_l INTEGER, clinch_a INTEGER, ground_l INTEGER, ground_a INTEGER,
    PRIMARY KEY (fight_id, fighter_id, round)
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def download(raw_dir: str = RAW_DIR) -> None:
    os.makedirs(raw_dir, exist_ok=True)
    for name in FILES:
        r = requests.get(MIRROR + name + ".csv", timeout=60)
        r.raise_for_status()
        with open(os.path.join(raw_dir, name + ".csv"), "wb") as fh:
            fh.write(r.content)


def _read(raw_dir: str, name: str) -> List[Dict[str, str]]:
    with open(os.path.join(raw_dir, name + ".csv"), newline="",
              encoding="utf-8") as fh:
        return [{k.strip(): (v or "").strip() for k, v in row.items()}
                for row in csv.DictReader(fh)]


# ---------------------------------------------------------------------------
# Field parsers
# ---------------------------------------------------------------------------
def _ws(s: str) -> str:
    return " ".join(s.split())


def _id_from_url(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _slug(name: str) -> str:
    return "name:" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _date(text: str) -> Optional[str]:
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _inches(text: str) -> Optional[float]:
    m = re.match(r"(\d+)'\s*(\d+)", text)
    if m:
        return int(m.group(1)) * 12 + int(m.group(2))
    m = re.match(r"([\d.]+)\"", text)
    return float(m.group(1)) if m else None


def _lbs(text: str) -> Optional[float]:
    m = re.match(r"([\d.]+)\s*lbs", text)
    return float(m.group(1)) if m else None


def _of(text: str) -> Tuple[Optional[int], Optional[int]]:
    """'9 of 37' -> (9, 37)."""
    m = re.match(r"(\d+)\s+of\s+(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def _int(text: str) -> Optional[int]:
    """'3' or '3.0' (the mirror writes some counts as floats) -> 3."""
    m = re.fullmatch(r"(\d+)(?:\.0+)?", text)
    return int(m.group(1)) if m else None


def _clock(text: str) -> Optional[int]:
    """'2:32' -> 152 seconds."""
    m = re.match(r"(\d+):(\d{2})$", text)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _round_lengths(fmt: str) -> List[int]:
    """'3 Rnd (5-5-5)' -> [5, 5, 5]; 'No Time Limit' -> []."""
    m = re.search(r"\(([\d-]+)\)", fmt)
    return [int(x) for x in m.group(1).split("-")] if m else []


def _method(raw: str) -> str:
    u = raw.upper()
    if u.startswith("KO/TKO") or "DOCTOR" in u:
        return "KO/TKO"
    if u.startswith("SUBMISSION"):
        return "SUB"
    if u.startswith("DECISION"):
        return "DEC"
    if u.startswith("OVERTURNED"):
        return "NC"
    return "OTHER"                        # DQ, Could Not Continue, Other


def _weight_class(raw: str) -> Tuple[str, int, int]:
    """'UFC Women's Flyweight Title Bout' -> ("Women's Flyweight", title, women)."""
    is_title = int("TITLE" in raw.upper())
    is_women = int("WOMEN" in raw.upper())
    base = None
    for div in sorted(DIVISION_LIMITS, key=len, reverse=True):
        if div.upper() in raw.upper():
            base = div
            break
    if base is None:
        base = "Catch Weight" if "CATCH" in raw.upper() else "Open Weight"
    return (("Women's " + base) if is_women else base), is_title, is_women


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build(raw_dir: str = RAW_DIR, db_path: str = DB_PATH) -> Dict[str, int]:
    events = _read(raw_dir, "ufc_event_details")
    results = _read(raw_dir, "ufc_fight_results")
    stats = _read(raw_dir, "ufc_fight_stats")
    tott = _read(raw_dir, "ufc_fighter_tott")

    # --- fighters ---------------------------------------------------------
    fighters: Dict[str, Dict] = {}
    by_name: Dict[str, List[str]] = defaultdict(list)
    for t in tott:
        if not t["URL"]:
            continue
        fid = _id_from_url(t["URL"])
        if fid in fighters:
            continue
        name = _ws(t["FIGHTER"])
        dob = _date(t["DOB"]) if t["DOB"] not in ("", "--") else None
        fighters[fid] = {
            "fighter_id": fid, "name": name, "height_in": _inches(t["HEIGHT"]),
            "reach_in": _inches(t["REACH"]), "weight_lbs": _lbs(t["WEIGHT"]),
            "stance": t["STANCE"] or None, "dob": dob,
        }
        by_name[name].append(fid)

    def resolve(name: str, wc_raw: str) -> Tuple[str, int]:
        cands = by_name.get(name, [])
        if len(cands) == 1:
            return cands[0], 0
        if not cands:
            fid = _slug(name)
            fighters.setdefault(fid, {
                "fighter_id": fid, "name": name, "height_in": None,
                "reach_in": None, "weight_lbs": None, "stance": None,
                "dob": None})
            return fid, 0
        limit = next((v for k, v in sorted(DIVISION_LIMITS.items(),
                                           key=lambda kv: -len(kv[0]))
                      if k.upper() in wc_raw.upper()), None)
        if limit is None:
            return cands[0], 1
        best = min(cands, key=lambda f: abs((fighters[f]["weight_lbs"] or 999)
                                            - limit))
        return best, 1

    # --- events -----------------------------------------------------------
    ev_rows, ev_by_name = [], {}
    for e in events:
        d = _date(e["DATE"])
        if not d or not e["URL"]:
            continue
        eid = _id_from_url(e["URL"])
        ev_rows.append((eid, _ws(e["EVENT"]), d, e["LOCATION"] or None))
        ev_by_name[_ws(e["EVENT"])] = (eid, d)

    # --- fights -----------------------------------------------------------
    fight_rows, bout_key = {}, {}
    duplicates = unmatched = 0
    for r in results:
        fid = _id_from_url(r["URL"]) if r["URL"] else None
        if fid in fight_rows:
            duplicates += 1
            continue
        ev = ev_by_name.get(_ws(r["EVENT"]))
        names = _ws(r["BOUT"]).split(" vs. ")
        if not fid or not ev or len(names) != 2:
            unmatched += 1
            continue
        wc, is_title, is_women = _weight_class(r["WEIGHTCLASS"])
        a, ga = resolve(names[0], r["WEIGHTCLASS"])
        b, gb = resolve(names[1], r["WEIGHTCLASS"])
        oc = r["OUTCOME"].split("/")
        if oc == ["W", "L"]:
            result, winner = "win", a
        elif oc == ["L", "W"]:
            result, winner = "win", b
        elif oc[0] == "D":
            result, winner = "draw", None
        else:
            result, winner = "nc", None
        method = _method(r["METHOD"])
        if result == "nc":
            method = "NC"
        lengths = _round_lengths(r["TIME FORMAT"])
        end_round, end_time = _int(r["ROUND"]), _clock(r["TIME"])
        secs = None
        if end_round and end_time is not None:
            secs = sum(lengths[:end_round - 1]) * 60 + end_time
        fight_rows[fid] = (
            fid, ev[0], ev[1], a, b, winner, result, method, r["METHOD"],
            r["DETAILS"] or None, end_round, end_time, secs,
            len(lengths) or None, "-".join(map(str, lengths)) or None,
            wc, is_title, is_women, r["REFEREE"] or None, int(ga or gb))
        bout_key[(_ws(r["EVENT"]), _ws(r["BOUT"]))] = fid

    # --- round stats ------------------------------------------------------
    round_rows = {}
    for s in stats:
        fid = bout_key.get((_ws(s["EVENT"]), _ws(s["BOUT"])))
        m = re.match(r"Round (\d+)", s["ROUND"])
        if not fid or not m or not s["FIGHTER"]:
            continue
        f = fight_rows[fid]
        name = _ws(s["FIGHTER"])
        a_name, b_name = _ws(s["BOUT"]).split(" vs. ")
        if name == a_name:
            fighter = f[3]
        elif name == b_name:
            fighter = f[4]
        else:
            continue
        cols = [_int(s["KD"])]
        for key in ("SIG.STR.", "TOTAL STR.", "TD"):
            cols += _of(s[key])
        cols += [_int(s["SUB.ATT"]), _int(s["REV."]), _clock(s["CTRL"])]
        for key in ("HEAD", "BODY", "LEG", "DISTANCE", "CLINCH", "GROUND"):
            cols += _of(s[key])
        round_rows[(fid, fighter, int(m.group(1)))] = cols

    # --- write ------------------------------------------------------------
    tmp = db_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO fighters VALUES (?,?,?,?,?,?,?)",
                    [tuple(f.values()) for f in fighters.values()])
    con.executemany("INSERT INTO events VALUES (?,?,?,?)", ev_rows)
    con.executemany(f"INSERT INTO fights VALUES ({','.join('?' * 20)})",
                    fight_rows.values())
    con.executemany(f"INSERT INTO round_stats VALUES ({','.join('?' * 25)})",
                    [k + tuple(v) for k, v in round_rows.items()])
    con.executemany("INSERT INTO meta VALUES (?,?)", [
        ("built_at", datetime.now().isoformat(timespec="seconds")),
        ("source", MIRROR),
        ("latest_event", max(e[2] for e in ev_rows)),
    ])
    con.commit()
    con.close()
    os.replace(tmp, db_path)
    return {"fighters": len(fighters), "events": len(ev_rows),
            "fights": len(fight_rows), "round_rows": len(round_rows),
            "duplicate_results": duplicates, "unmatched_results": unmatched}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--offline", action="store_true",
                    help="rebuild from data_store/raw/ without downloading")
    args = ap.parse_args()
    if not args.offline:
        download()
    print(build())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
