"""
sources/espn_common.py
======================
Shared ESPN scoreboard fetch. Both the schedule (discovery) and results sources
need the same scoreboard JSON, so we fetch it ONCE per cycle and cache it for the
life of the process (each sync run is a fresh process). This halves ESPN traffic
and guarantees the two sources see a consistent snapshot.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Optional

from .base import http_get

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"

# ESPN's scoreboard endpoint with no `dates` param only returns the single
# nearest upcoming event — a card rolls off that default window (and becomes
# unscorable) within a day or two of finishing. Request an explicit range that
# reaches back far enough to still catch last week's results and forward far
# enough to keep discovering upcoming cards.
DAYS_BACK = 10
DAYS_FORWARD = 21

_cache: Dict[str, Any] = {"fetched": False, "data": None}


def get_scoreboard(force: bool = False) -> Optional[Dict[str, Any]]:
    """Return the parsed scoreboard JSON, fetching at most once per process."""
    if _cache["fetched"] and not force:
        return _cache["data"]
    today = date.today()
    start = (today - timedelta(days=DAYS_BACK)).strftime("%Y%m%d")
    end = (today + timedelta(days=DAYS_FORWARD)).strftime("%Y%m%d")
    r = http_get(f"{SCOREBOARD}?dates={start}-{end}")
    data = None
    if r is not None and r.status_code == 200:
        try:
            data = r.json()
        except Exception:
            data = None
    _cache["fetched"] = True
    _cache["data"] = data
    return data


def days_until_next_card(today_iso: str) -> Optional[int]:
    """
    Days from `today_iso` (YYYY-MM-DD) to the soonest upcoming (non-completed)
    event on the scoreboard. None if unknown/no upcoming card. Used to gate the
    odds and news sources so we don't spend budget when nothing is imminent.
    """
    data = get_scoreboard()
    if not data:
        return None
    try:
        y, m, d = (int(x) for x in today_iso.split("-"))
        today = date(y, m, d)
    except Exception:
        return None

    soonest: Optional[int] = None
    for ev in data.get("events", []) or []:
        ev_date = (ev.get("date") or "")[:10]
        if not ev_date:
            continue
        try:
            ey, em, ed = (int(x) for x in ev_date.split("-"))
            delta = (date(ey, em, ed) - today).days
        except Exception:
            continue
        # Only count cards that haven't fully passed.
        if delta >= -1 and (soonest is None or delta < soonest):
            soonest = delta
    return soonest
