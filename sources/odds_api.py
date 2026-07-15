"""
sources/odds_api.py
===================
Live UFC moneylines from The Odds API (https://the-odds-api.com).

Requires an API key in the ODDS_API_KEY environment variable. The free tier
allows ~500 requests/month, so we make ONE call per cycle (all UFC events).

Endpoint:
    GET /v4/sports/mma_mixed_martial_arts/odds
        ?regions=us&markets=h2h&oddsFormat=american&apiKey=...

We match each event's two competitors against watched matchups (by fighter
display name) and emit an odds update {a_ml, b_ml} for the matching label.
Consensus line = median across returned bookmakers.
"""

from __future__ import annotations

import os
import statistics
from typing import Any, Dict, List

from .base import Source, SourceResult, http_get

ODDS_URL = ("https://api.the-odds-api.com/v4/sports/"
            "mma_mixed_martial_arts/odds")


def _norm(name: str) -> str:
    return " ".join(name.lower().replace(".", "").split())


class OddsApiSource(Source):
    name = "odds_api"

    def fetch(self, state: Dict[str, Any]) -> SourceResult:
        res = SourceResult()
        key = os.environ.get("ODDS_API_KEY", "").strip()
        if not key:
            res.ok = False
            res.notes.append("ODDS_API_KEY not set — odds source skipped")
            return res

        r = http_get(ODDS_URL, params={
            "regions": "us",
            "markets": "h2h",
            "oddsFormat": "american",
            "apiKey": key,
        })
        if r is None:
            res.ok = False
            res.notes.append("Odds API unreachable")
            return res
        if r.status_code != 200:
            res.ok = False
            res.notes.append(f"Odds API HTTP {r.status_code} "
                             f"({'bad/no key or quota' if r.status_code in (401, 429) else 'error'})")
            return res

        events = r.json()

        # Map watched matchups by the (normalized) pair of fighter display names.
        matchups = state.get("matchups", [])
        fighters = state.get("fighters", {})

        def display(fkey: str) -> str:
            rec = fighters.get(fkey, {})
            return _norm(rec.get("display_name") or rec.get("name") or fkey)

        pair_to_matchup = {}
        for m in matchups:
            a, b = display(m["a"]), display(m["b"])
            pair_to_matchup[frozenset((a, b))] = m

        for ev in events:
            home = _norm(ev.get("home_team", ""))
            away = _norm(ev.get("away_team", ""))
            match = pair_to_matchup.get(frozenset((home, away)))
            if not match:
                continue

            # Collect each fighter's price across bookmakers, take the median.
            prices: Dict[str, List[int]] = {home: [], away: []}
            for bm in ev.get("bookmakers", []) or []:
                for mk in bm.get("markets", []) or []:
                    if mk.get("key") != "h2h":
                        continue
                    for oc in mk.get("outcomes", []) or []:
                        nm = _norm(oc.get("name", ""))
                        if nm in prices and oc.get("price") is not None:
                            prices[nm].append(int(oc["price"]))

            if not prices[home] or not prices[away]:
                continue

            a_name = display(match["a"])
            a_prices = prices.get(a_name, [])
            b_name = display(match["b"])
            b_prices = prices.get(b_name, [])
            if not a_prices or not b_prices:
                continue

            a_ml = int(statistics.median(a_prices))
            b_ml = int(statistics.median(b_prices))
            label = match.get("label", f"{match['a']} vs {match['b']}")
            res.odds[label] = {"a_ml": a_ml, "b_ml": b_ml}
            res.events.append({
                "type": "odds_update",
                "detail": f"{label}: consensus {a_ml:+d} / {b_ml:+d} "
                          f"({len(a_prices)} books)",
            })
        return res
