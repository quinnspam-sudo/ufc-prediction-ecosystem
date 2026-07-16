"""
sources/espn_results.py
=======================
Fight results + card status from ESPN's unofficial MMA JSON API.

Endpoint (no key, no JS wall):
    https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard

What we extract each cycle:
  * For every watched fighter (matched by display name), whether their next/most
    recent bout has gone FINAL, and if so the winner + method + the event date.
  * A "fight_result" changelog event when a bout flips to Final.
  * A last_fight_date patch so the layoff / ring-rust factor stays current.

We deliberately keep parsing shallow and defensive: ESPN's schema shifts, so we
guard every access and skip anything we don't recognise.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .base import Source, SourceResult, FighterPatch
from .espn_common import get_scoreboard


def _norm(name: str) -> str:
    return " ".join(name.lower().replace(".", "").split())


class EspnResultsSource(Source):
    name = "espn_results"

    def fetch(self, state: Dict[str, Any]) -> SourceResult:
        res = SourceResult()
        data = get_scoreboard()
        if not data:
            res.ok = False
            res.notes.append("ESPN scoreboard unavailable")
            return res

        events = data.get("events", []) or []
        upcoming = 0

        # Build a name -> fighter-key map for the watched fighters.
        name_to_key: Dict[str, str] = {}
        for key, rec in state.get("fighters", {}).items():
            dn = rec.get("display_name") or rec.get("name") or ""
            if dn:
                name_to_key[_norm(dn)] = key

        for ev in events:
            ev_name = ev.get("name", "")
            ev_date = (ev.get("date") or "")[:10]  # ISO date prefix
            for comp in ev.get("competitions", []) or []:
                full_status = comp.get("status", {}) or {}
                status = full_status.get("type", {}) or {}
                completed = bool(status.get("completed"))
                competitors = comp.get("competitors", []) or []

                # Bout-level result (for calibration): who won, who lost, and
                # (when ESPN provides it on FINAL bouts) method + finish round
                # so method-of-victory / goes-the-distance predictions get
                # graded too, not just the winner.
                if completed:
                    win_name = lose_name = ""
                    for c in competitors:
                        nm = (c.get("athlete", {}) or {}).get("displayName", "")
                        if c.get("winner") is True:
                            win_name = nm
                        else:
                            lose_name = nm
                    if win_name and lose_name:
                        raw_method = ((full_status.get("result") or {})
                                      .get("displayName") or "").upper()
                        if "KO" in raw_method:          # covers KO and TKO
                            method = "KO/TKO"
                        elif "SUB" in raw_method:
                            method = "Submission"
                        elif "DEC" in raw_method:
                            method = "Decision"
                        else:
                            method = ""                 # unknown -> ungraded
                        res.fight_results.append({
                            "winner": win_name, "loser": lose_name,
                            "method": method,
                            "round": int(full_status.get("period") or 0),
                        })

                for c in competitors:
                    ath = (c.get("athlete", {}) or {})
                    fname = ath.get("displayName") or ath.get("shortName") or ""
                    key = name_to_key.get(_norm(fname))
                    if not key:
                        continue

                    if completed and ev_date:
                        # Refresh layoff clock for this fighter.
                        prior = state["fighters"][key].get("last_fight_date")
                        if prior != ev_date:
                            won = c.get("winner") is True
                            res.patches.append(FighterPatch(
                                key=key,
                                fields={"last_fight_date": ev_date,
                                        # A completed bout resolves any prior
                                        # injury/withdrawal flags for that fighter.
                                        "active_injury": False,
                                        "withdrawn": False},
                                reason=f"{fname}: bout FINAL on {ev_date} "
                                       f"({'W' if won else 'L/NC'}) at {ev_name}",
                            ))
                            res.events.append({
                                "type": "fight_result",
                                "detail": f"{fname} {'won' if won else 'did not win'} "
                                          f"at {ev_name} ({ev_date})",
                            })
                    elif ev_date:
                        upcoming += 1
        if upcoming:
            res.notes.append(f"{upcoming} tracked fighter(s) have upcoming bouts")
        return res
