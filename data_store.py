"""
data_store.py
=============
Persistent "current known state" of every tracked fighter + matchup, plus a
changelog. This is what makes the system *incremental*: each sync compares
freshly-fetched data against this store and only re-predicts + commits when
something actually changed.

State schema (data_store/state.json):
{
  "fighters": { "<key>": { ...FighterRawStats fields..., "_updated": iso } },
  "matchups": [ {"a","b","rounds","label","a_ml","b_ml"} ],
  "changelog": [ {"ts","type","detail"} ],   # newest last
  "meta": {"last_sync": iso}
}

The store is seeded once from watchlist.json (which may pull fully-specified
demo fighters from the data_ingestion mock). Thereafter the live sources keep
it current.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from data_ingestion import FighterRawStats, fetch_fighter_stats

STORE_DIR = os.path.join(os.path.dirname(__file__), "data_store")
STATE_PATH = os.path.join(STORE_DIR, "state.json")
WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), "watchlist.json")

# Fields we consider "prediction-relevant": a change in any of these triggers a
# re-simulation. Cosmetic/bookkeeping fields (display_name, notes) don't.
PREDICTION_FIELDS = {
    "slpm", "sapm", "strike_acc", "strike_def", "td_avg", "td_acc", "td_def",
    "sub_avg", "sub_def", "control_time_pct", "age", "reach_in", "height_in",
    "last_fight_date", "division_move", "active_injury", "missed_weight",
    "withdrawn", "career_knockdowns_suffered", "career_fights",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fighter_to_dict(f: FighterRawStats) -> Dict[str, Any]:
    d = dataclasses.asdict(f)
    d["display_name"] = f.display_name or f.name
    return d


def load_state() -> Dict[str, Any]:
    """Load the persisted state, seeding from watchlist.json on first run."""
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as fh:
            return json.load(fh)
    return seed_state()


def seed_state() -> Dict[str, Any]:
    """
    Build the initial state from watchlist.json.

    A watchlist fighter entry is either:
      * {"seed": "mock"}  -> pull the fully-specified fighter from the
        data_ingestion mock (used for the fictional demo archetypes), or
      * a full/partial FighterRawStats field dict for a real fighter.
    """
    with open(WATCHLIST_PATH) as fh:
        wl = json.load(fh)

    mock = fetch_fighter_stats()
    fighters: Dict[str, Any] = {}
    for key, entry in wl.get("fighters", {}).items():
        if entry.get("seed") == "mock":
            src = mock.get(key)
            if src is None:
                raise ValueError(f"watchlist '{key}' seeds from mock but no such mock fighter")
            rec = _fighter_to_dict(src)
            rec["fictional"] = True          # skip news lookups for demo fighters
        else:
            # Real fighter: start from FighterRawStats defaults, overlay entry.
            base = FighterRawStats(
                name=entry.get("name", key),
                age=entry.get("age", 30),
                height_in=entry.get("height_in", 70.0),
                reach_in=entry.get("reach_in", 72.0),
                weight_class=entry.get("weight_class", "Welterweight"),
                stance=entry.get("stance", "Orthodox"),
                slpm=entry.get("slpm", 4.0),
                strike_acc=entry.get("strike_acc", 0.45),
            )
            rec = _fighter_to_dict(base)
            rec.update({k: v for k, v in entry.items() if k != "seed"})
            rec["display_name"] = entry.get("display_name") or entry.get("name") or key
            rec.setdefault("stats_approx", True)  # flag: granular stats are seeded
        rec["_updated"] = _now()
        fighters[key] = rec

    state = {
        "fighters": fighters,
        "matchups": wl.get("matchups", []),
        "changelog": [{"ts": _now(), "type": "seed",
                       "detail": f"seeded {len(fighters)} fighters, "
                                 f"{len(wl.get('matchups', []))} matchups"}],
        "meta": {"last_sync": None},
    }
    save_state(state)
    return state


def save_state(state: Dict[str, Any]) -> None:
    os.makedirs(STORE_DIR, exist_ok=True)
    state.setdefault("meta", {})["last_sync"] = _now()
    with open(STATE_PATH, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)


def apply_patches(state: Dict[str, Any], patches: List) -> List[Tuple[str, str, Any, Any]]:
    """
    Merge FighterPatch objects into state. Returns a list of
    (fighter_key, field, old_value, new_value) for prediction-relevant changes.
    """
    changes: List[Tuple[str, str, Any, Any]] = []
    for p in patches:
        rec = state["fighters"].get(p.key)
        if rec is None:
            continue
        for field, new_val in p.fields.items():
            old_val = rec.get(field)
            if old_val != new_val:
                rec[field] = new_val
                if field in PREDICTION_FIELDS:
                    changes.append((p.key, field, old_val, new_val))
        rec["_updated"] = _now()
    return changes


def apply_odds(state: Dict[str, Any], odds: Dict[str, Dict[str, int]]) -> List[str]:
    """Merge odds updates into the matching matchups. Returns changed labels."""
    changed: List[str] = []
    by_label = {m.get("label"): m for m in state["matchups"]}
    for label, line in odds.items():
        m = by_label.get(label)
        if not m:
            continue
        if m.get("a_ml") != line["a_ml"] or m.get("b_ml") != line["b_ml"]:
            m["a_ml"] = line["a_ml"]
            m["b_ml"] = line["b_ml"]
            changed.append(label)
    return changed


def record_events(state: Dict[str, Any], events: List[Dict[str, Any]]) -> None:
    """Append events to the changelog (keep the last 500)."""
    ts = _now()
    for e in events:
        state.setdefault("changelog", []).append(
            {"ts": ts, "type": e.get("type", "event"), "detail": e.get("detail", "")}
        )
    state["changelog"] = state["changelog"][-500:]


def state_fighter_to_raw(rec: Dict[str, Any]) -> FighterRawStats:
    """Rebuild a FighterRawStats from a stored record (drops private keys)."""
    field_names = {f.name for f in dataclasses.fields(FighterRawStats)}
    clean = {k: v for k, v in rec.items() if k in field_names}
    return FighterRawStats(**clean)
