"""
ledger_backfill.py
==================
Rebuild the live calibration ledger honestly from git history + the fight store.

    ./.venv/bin/python ledger_backfill.py            # dry run: print what would change
    ./.venv/bin/python ledger_backfill.py --write    # rewrite predictions_log.json

Why: ESPN result scoring was dead from 2026-08-03 to 2026-10-01, and open
predictions keep being refreshed by every sync cycle — including after the
fight, with post-fight stats. So an open entry's current p_a can't be trusted
for a fight that already happened.

What it does, for every ledger entry whose bout is in data_store/history.sqlite:
  1. Restores p_a (and the method / distance probabilities) from the last git
     snapshot of predictions_log.json committed at least 30 minutes before
     ESPN's listed card start (18:00 UTC the day before when ESPN has no
     start time) — strictly pre-fight.
  2. Scores it from the store: winner, plus method and goes-the-distance (the
     live ledger never graded those before).
Entries with no pre-fight snapshot are excluded (`backfill: "no_prefight"`),
never guessed. Draws / no-contests are marked `backfill: "void"`, and bouts
on a completed card that never happened (cancelled, or the opponent changed)
are marked `backfill: "cancelled"`; neither is scored.

Bouts the store doesn't cover (the Contender Series isn't in the UFCStats
mirror) fall back to ESPN's scoreboard: the winner flag, plus "went the
distance" when judges' scores are present. Upcoming cards are left untouched.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sqlite3
import subprocess
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import calibration as C
from sources.base import http_get
from sources.espn_common import SCOREBOARD

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(ROOT, "data_store", "predictions_log.json")
LOG_REL = "data_store/predictions_log.json"
DB_PATH = os.path.join(ROOT, "data_store", "history.sqlite")
METHOD_NAMES = {"KO/TKO": "KO/TKO", "SUB": "Submission", "DEC": "Decision"}


def _norm(s: str) -> str:
    s = s.lower().replace(".", "").replace("-", " ").replace("'", "")
    for a, b in (("é", "e"), ("ć", "c"), ("á", "a"), ("ó", "o"), ("í", "i"), ("ñ", "n")):
        s = s.replace(a, b)
    return " ".join(s.split())


def _snapshots() -> List[Tuple[datetime, str]]:
    out = subprocess.run(["git", "log", "--format=%H %cI", "--", LOG_REL],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout
    snaps = []
    for line in out.splitlines():
        sha, ts = line.split()
        snaps.append((datetime.fromisoformat(ts).astimezone(timezone.utc), sha))
    return sorted(snaps)


_cache: Dict[str, List[Dict]] = {}


def _entries_at(sha: str) -> Optional[List[Dict]]:
    """Ledger entries in one committed snapshot; None if it isn't valid JSON
    (one hand-edited commit on 2026-07-16 has a comment in it)."""
    if sha not in _cache:
        raw = subprocess.run(["git", "show", f"{sha}:{LOG_REL}"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        try:
            _cache[sha] = json.loads(raw).get("entries", [])
        except json.JSONDecodeError:
            _cache[sha] = None
    return _cache[sha]


def _prefight(entry: Dict, cutoff: datetime,
              snaps: List[Tuple[datetime, str]]) -> Optional[Tuple[Dict, datetime]]:
    """The entry's state in the last readable snapshot committed before
    `cutoff`; None if it wasn't in the ledger yet at that point."""
    pair = {_norm(entry["a"]), _norm(entry["b"])}
    for ts, sha in reversed([s for s in snaps if s[0] <= cutoff]):
        entries = _entries_at(sha)
        if entries is None:
            continue
        for e in reversed(entries):
            if {_norm(e["a"]), _norm(e["b"])} == pair:
                return e, ts
        return None
    return None


def _store_bouts(con) -> Dict[str, Dict]:
    """Event name (normalized) -> {date, bouts by normalized fighter pair}."""
    events = {}
    for name, d, eid in con.execute("SELECT name, date, event_id FROM events "
                                    "WHERE date >= '2026-07-01'"):
        bouts = {}
        for f in con.execute("""
                SELECT f.*, a.name an, b.name bn FROM fights f
                JOIN fighters a ON a.fighter_id = f.fighter_a
                JOIN fighters b ON b.fighter_id = f.fighter_b
                WHERE f.event_id = ?""", (eid,)):
            bouts[(_norm(f["an"]), _norm(f["bn"]))] = dict(f)
        events[_norm(name)] = {"date": d, "bouts": bouts}
    return events


def _event_key(name: str, keys) -> Optional[str]:
    """Match an event name to one of `keys`: exact after normalising, else a
    near-identical name (e.g. "Gamrot vs Salkilld" vs "Gamrot vs. Salkilld").
    Contender Series weeks differ only by number, so they must match exactly.
    Loose matching once mapped an upcoming card onto a past one, because every
    "UFC Fight Night: X vs. Y" name shares most of its characters."""
    n = _norm(name)
    if n in keys:
        return n
    if "contender" in n:
        return None
    m = difflib.get_close_matches(n, list(keys), n=1, cutoff=0.93)
    return m[0] if m else None


def _cutoff(entry: Dict, fight_date: str, espn: Dict) -> datetime:
    """Latest moment a prediction still counts as pre-fight: 30 minutes before
    ESPN's listed card start, else 18:00 UTC the day before the fight."""
    k = _event_key(entry["event"], espn)
    if k and espn[k].get("start"):
        return espn[k]["start"] - timedelta(minutes=30)
    return datetime.combine(date.fromisoformat(fight_date) - timedelta(days=1),
                            datetime.min.time(), timezone.utc) + timedelta(hours=18)


def _find_bout(entry: Dict, events: Dict) -> Optional[Tuple[str, Dict]]:
    k = _event_key(entry["event"], events)
    if not k:
        return None
    ev = events[k]
    a, b = _norm(entry["a"]), _norm(entry["b"])

    def same(x: str, y: str) -> bool:
        return x == y or x.split()[-1] == y.split()[-1] or \
            difflib.SequenceMatcher(None, x, y).ratio() >= 0.8

    for (an, bn), f in ev["bouts"].items():
        if (same(a, an) and same(b, bn)) or (same(a, bn) and same(b, an)):
            return ev["date"], f
    return None


def _espn_events(start: date, end: date) -> Dict[str, Dict]:
    """Completed ESPN events (normalized name -> {date, bouts}) in 3-week
    windows — one long range is what ESPN refused during the 2026 outage."""
    events: Dict[str, Dict] = {}
    d = start
    while d <= end:
        stop = min(d + timedelta(days=20), end)
        r = http_get(f"{SCOREBOARD}?dates={d:%Y%m%d}-{stop:%Y%m%d}")
        if r is not None and r.status_code == 200:
            for ev in r.json().get("events", []) or []:
                bouts = {}
                for c in ev.get("competitions", []) or []:
                    st = (c.get("status") or {}).get("type") or {}
                    comp = c.get("competitors") or []
                    if not st.get("completed") or len(comp) != 2:
                        continue
                    names = [_norm((x.get("athlete") or {}).get("displayName", ""))
                             for x in comp]
                    won = [bool(x.get("winner")) for x in comp]
                    judged = all(x.get("linescores") for x in comp)
                    bouts[tuple(names)] = {"winner": names[won.index(True)]
                                           if any(won) else None,
                                           "decision": judged}
                start = None
                if ev.get("date"):
                    start = datetime.fromisoformat(
                        ev["date"].replace("Z", "+00:00")).astimezone(timezone.utc)
                events[_norm(ev.get("name", ""))] = {
                    "date": (ev.get("date") or "")[:10], "start": start,
                    "bouts": bouts}
        d = stop + timedelta(days=1)
    return events


def _espn_bout(entry: Dict, events: Dict) -> Optional[Tuple[str, Dict]]:
    k = _event_key(entry["event"], events)
    if not k:
        return None
    ev = events[k]
    a, b = _norm(entry["a"]), _norm(entry["b"])
    for (x, y), bout in ev["bouts"].items():
        if {x.split()[-1], y.split()[-1]} == {a.split()[-1], b.split()[-1]}:
            return ev["date"], bout
    return None


def _event_date(entry: Dict, events: Dict) -> Optional[str]:
    k = _event_key(entry["event"], events)
    return events[k]["date"] if k else None


def backfill(write: bool) -> Dict:
    log = json.load(open(LOG_PATH))
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    events = _store_bouts(con)
    snaps = _snapshots()
    espn = _espn_events(date(2026, 7, 10), date.today())
    stats = {"in_store": 0, "rescored": 0, "newly_scored": 0, "no_prefight": 0,
             "void": 0, "cancelled": 0, "via_espn": 0, "not_yet_fought": 0,
             "p_changed_vs_ledger": 0, "winner_disagrees_with_espn": 0}
    today = date.today().isoformat()
    for e in log["entries"]:
        hit = _find_bout(e, events)
        if not hit:
            card_date = _event_date(e, events)
            if card_date and card_date < today:
                # The card happened but this pairing never did.
                e.update({"resolved": False, "actual_a": None, "brier": None,
                          "backfill": "cancelled"})
                stats["cancelled"] += 1
                continue
            if _espn_score(e, espn, snaps, stats):
                continue
            stats["not_yet_fought"] += 1
            continue
        stats["in_store"] += 1
        d, f = hit
        pre = _prefight(e, _cutoff(e, d, espn), snaps)
        was_resolved, old_actual = e["resolved"], e.get("actual_a")
        if pre is None:
            e.update({"resolved": False, "actual_a": None, "brier": None,
                      "backfill": "no_prefight"})
            stats["no_prefight"] += 1
            continue
        snap, snap_ts = pre
        if abs(snap["p_a"] - e["p_a"]) > 1e-9:
            stats["p_changed_vs_ledger"] += 1
        e["p_a"] = snap["p_a"]
        for k in ("p_ko", "p_sub", "p_dec", "p_distance"):
            if k in snap:
                e[k] = snap[k]
            else:
                e.pop(k, None)
        for k in ("actual_method", "actual_round", "distance_brier",
                  "method_brier", "method_correct"):
            e.pop(k, None)
        e["prefight_snapshot"] = snap_ts.isoformat(timespec="minutes")
        e["fight_date"] = d
        if f["result"] != "win":
            e.update({"resolved": False, "actual_a": None, "brier": None,
                      "backfill": "void"})
            stats["void"] += 1
            continue
        a_is_store_a = _norm(e["a"]).split()[-1] == _norm(f["an"]).split()[-1]
        a_id = f["fighter_a"] if a_is_store_a else f["fighter_b"]
        e["resolved"] = False
        winner, loser = (e["a"], e["b"]) if f["winner"] == a_id else (e["b"], e["a"])
        C.resolve_result({"entries": [e]}, winner, loser,
                         METHOD_NAMES.get(f["method"], ""), f["end_round"] or 0)
        e["backfill"] = "store"
        if was_resolved:
            stats["rescored"] += 1
            if old_actual is not None and old_actual != e["actual_a"]:
                stats["winner_disagrees_with_espn"] += 1
        else:
            stats["newly_scored"] += 1
    shrink = C.fit_shrink(log)
    summ = C.summary(log, shrink)
    if write:
        json.dump(log, open(LOG_PATH, "w"), indent=2)
        json.dump(summ, open(os.path.join(ROOT, "reports", "calibration.json"), "w"),
                  indent=2)
    return {"changes": stats, "summary": summ}


def _espn_score(e: Dict, espn: Dict, snaps, stats: Dict) -> bool:
    """Score an entry the store doesn't cover from ESPN. True if handled."""
    hit = _espn_bout(e, espn)
    if not hit or not hit[1]["winner"]:
        return False
    d, bout = hit
    pre = _prefight(e, _cutoff(e, d, espn), snaps)
    if pre is None:
        e.update({"resolved": False, "actual_a": None, "brier": None,
                  "backfill": "no_prefight"})
        stats["no_prefight"] += 1
        return True
    snap, snap_ts = pre
    was_resolved, old_actual = e["resolved"], e.get("actual_a")
    e["p_a"] = snap["p_a"]
    for k in ("p_ko", "p_sub", "p_dec", "p_distance"):
        if k in snap:
            e[k] = snap[k]
    for k in ("actual_method", "actual_round", "distance_brier",
              "method_brier", "method_correct"):
        e.pop(k, None)
    e["prefight_snapshot"] = snap_ts.isoformat(timespec="minutes")
    e["fight_date"] = d
    a_won = bout["winner"].split()[-1] == _norm(e["a"]).split()[-1]
    winner, loser = (e["a"], e["b"]) if a_won else (e["b"], e["a"])
    e["resolved"] = False
    # ESPN gives the winner and (via judges' scores) decision vs finish; the
    # finish type isn't given, so only decisions grade method/distance.
    C.resolve_result({"entries": [e]}, winner, loser,
                     "Decision" if bout["decision"] else "", 0)
    e["backfill"] = "espn"
    stats["via_espn"] += 1
    if was_resolved and old_actual is not None and old_actual != e["actual_a"]:
        stats["winner_disagrees_with_espn"] += 1
    if not was_resolved:
        stats["newly_scored"] += 1
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    res = backfill(ap.parse_args().write)
    print(json.dumps(res, indent=2))
