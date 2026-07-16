"""
sources/espn_schedule.py
========================
Auto-discovery of upcoming UFC matchups from ESPN's scoreboard JSON.

Each cycle this pulls the upcoming card(s) and, for every bout, extracts:
  * both fighters' names,
  * the weight class            (competition.type.abbreviation),
  * the scheduled round count    (competition.format.regulation.periods → 3/5).

New fighters are seeded with LEAGUE-AVERAGE placeholder stats (flagged
`needs_stats`) so a prediction can run immediately — but those numbers are not
real, so the report flags them until you supply true stats (UFCStats is
JS-bot-walled; a headless fetcher is the documented upgrade). New matchups are
added with no odds yet; the odds source fills them in when available.

De-duplication (matching an already-tracked fighter/matchup by name) is handled
downstream in `data_store.add_discovered`, so curated fighters like a manually
tuned Du Plessis are reused rather than overwritten with placeholder stats.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from data_ingestion import FighterRawStats, WEIGHT_CLASSES
from .base import Source, SourceResult, FighterPatch
from .espn_common import get_scoreboard

# Approximate elevation (feet above sea level) for cities that host UFC cards.
# Anything not listed is treated as sea level (elevation only matters above
# ~3000ft — see feature_engineering.ELEVATION_FLOOR_FT — so only high-altitude
# venues have any effect; missing low-altitude cities cost nothing).
CITY_ELEVATION_FT: Dict[str, float] = {
    "las vegas": 2001, "denver": 5280, "mexico city": 7349,
    "salt lake city": 4226, "albuquerque": 5312, "calgary": 3428,
    "edmonton": 2192, "johannesburg": 5751, "guadalajara": 5138,
    "sao paulo": 2493, "são paulo": 2493, "madrid": 2188,
    "monterrey": 1765, "phoenix": 1086, "kansas city": 910,
}


def _venue_elevation(comp: Dict[str, Any], ev: Dict[str, Any]) -> float:
    """Resolve the bout's venue city to an elevation (ft); 0 when unknown."""
    venue = comp.get("venue") or ev.get("venue") or {}
    city = ((venue.get("address") or {}).get("city") or "").strip().lower()
    return CITY_ELEVATION_FT.get(city, 0.0)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _map_weight_class(espn_abbr: str) -> str:
    """
    Map an ESPN weight-class label to one of our model's WEIGHT_CLASSES keys.

    Women's divisions ('W Flyweight') map to the same-named men's class for the
    model's age/lightness scaling. Strawweight (no entry in our table) maps to
    Flyweight; catchweights and anything unrecognised fall back to Lightweight.
    """
    t = (espn_abbr or "").replace("W ", "").replace("Women's ", "").strip()
    if t in WEIGHT_CLASSES:
        return t
    aliases = {"Strawweight": "Flyweight", "Super Heavyweight": "Heavyweight"}
    if t in aliases:
        return aliases[t]
    return "Lightweight"  # safe neutral default (incl. Catchweight/unknown)


def _default_fields(name: str, weight_class: str) -> Dict[str, Any]:
    """
    League-average placeholder stats for a newly-discovered fighter. Non-zero so
    feature engineering produces a sane (≈50/50) profile, flagged as unreal.
    """
    f = FighterRawStats(
        name=name, age=30, height_in=70.0, reach_in=72.0,
        weight_class=weight_class, stance="Orthodox",
        slpm=4.0, strike_acc=0.45, sapm=4.0, strike_def=0.55,
        td_avg=1.5, td_acc=0.40, td_def=0.65, sub_avg=0.6, sweep_rate=0.2,
        control_time_pct=0.30, sub_def=0.65,
        career_knockdowns_suffered=2, career_sig_strikes_absorbed=800,
        career_fights=12, round_strike_output=[60, 58, 56, 54, 52],
        last_fight_date="2025-06-01", display_name=name,
    )
    import dataclasses
    d = dataclasses.asdict(f)
    d["display_name"] = name
    d["stats_approx"] = True
    d["needs_stats"] = True      # placeholder league-average numbers, not real
    return d


class EspnScheduleSource(Source):
    name = "espn_schedule"

    def fetch(self, state: Dict[str, Any]) -> SourceResult:
        res = SourceResult()
        data = get_scoreboard()
        if not data:
            res.ok = False
            res.notes.append("ESPN scoreboard unavailable")
            return res

        for ev in data.get("events", []) or []:
            ev_name = ev.get("name", "")
            for comp in ev.get("competitions", []) or []:
                # Only discover bouts that haven't happened yet.
                status = (comp.get("status", {}) or {}).get("type", {}) or {}
                if status.get("completed"):
                    continue

                competitors = comp.get("competitors", []) or []
                names = [(c.get("athlete", {}) or {}).get("displayName")
                         for c in competitors]
                names = [n for n in names if n]
                if len(names) != 2:
                    continue

                wc = _map_weight_class(((comp.get("type") or {}) or {}).get("abbreviation", ""))
                rounds = (((comp.get("format") or {}).get("regulation") or {})
                          .get("periods")) or 3
                rounds = 5 if int(rounds) >= 5 else 3

                a_name, b_name = names
                a_key, b_key = _slug(a_name), _slug(b_name)
                label = f"{a_name} vs {b_name}"

                # Seed both fighters (dedup happens in data_store.add_discovered).
                res.new_fighters[a_key] = _default_fields(a_name, wc)
                res.new_fighters[b_key] = _default_fields(b_name, wc)
                res.new_matchups.append({
                    "a": a_key, "b": b_key, "rounds": rounds, "label": label,
                    "weight_class": wc, "event": ev_name,
                })

                # Venue elevation: patch BOTH fighters (covers pre-existing
                # fighters too — new ones were seeded above and dedup runs
                # before patches apply). Only patch when it matters, so
                # sea-level cards don't generate change-noise every cycle.
                elev = _venue_elevation(comp, ev)
                if elev > 0:
                    for key in (a_key, b_key):
                        res.patches.append(FighterPatch(
                            key=key, fields={"venue_elevation_ft": elev},
                            reason=f"venue elevation {elev:.0f}ft ({ev_name})",
                        ))

        res.notes.append(f"discovered {len(res.new_matchups)} bout(s) on the schedule")
        return res
