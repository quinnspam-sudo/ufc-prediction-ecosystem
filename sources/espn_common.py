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

_cache: Dict[str, Any] = {"fetched": False, "data": None, "error": ""}


def _fetch(start: date, end: date):
    """One scoreboard request; returns (json or None, error text)."""
    r = http_get(f"{SCOREBOARD}?dates={start:%Y%m%d}-{end:%Y%m%d}")
    if r is None:
        return None, "no response after retries (timeout, 5xx or connection error)"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    try:
        return r.json(), ""
    except Exception:
        return None, "HTTP 200 but body was not JSON"


def get_scoreboard(force: bool = False) -> Optional[Dict[str, Any]]:
    """Return the parsed scoreboard JSON, fetching at most once per process.

    ESPN refused every request from 2026-08-04 to 2026-09-30 and the reason was
    never logged, so this now (1) records why a fetch failed (last_error()) and
    (2) falls back to two narrower windows (past / upcoming) before giving up.
    """
    if _cache["fetched"] and not force:
        return _cache["data"]
    today = date.today()
    back, fwd = today - timedelta(days=DAYS_BACK), today + timedelta(days=DAYS_FORWARD)
    data, err = _fetch(back, fwd)
    if data is None:
        past, e1 = _fetch(back, today)
        upcoming, e2 = _fetch(today, fwd)
        if past or upcoming:
            seen, events = set(), []
            for part in (past, upcoming):
                for ev in (part or {}).get("events", []) or []:
                    if ev.get("id") not in seen:
                        seen.add(ev.get("id"))
                        events.append(ev)
            data = {"events": events}
            err = f"full window failed ({err}); used narrower windows"
            if not (past and upcoming):
                err += f"; past: {e1 or 'ok'}, upcoming: {e2 or 'ok'}"
        else:
            err = f"full window: {err}; past window: {e1}; upcoming window: {e2}"
    _cache.update(fetched=True, data=data, error=err)
    return data


def last_error() -> str:
    """Why the last scoreboard fetch failed or degraded ("" if it was clean)."""
    return _cache.get("error", "")


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
