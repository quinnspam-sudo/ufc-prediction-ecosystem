"""
sources/injury_news.py
======================
Approximate fighter health / injury / weight-miss detection from news headlines.

There is NO structured feed for fighter health, so this scans Google News RSS
for each watched fighter and pattern-matches headlines. This is intentionally
conservative and WILL produce false positives/negatives — treat its output as a
heads-up flag to verify, never as ground truth. Every flag carries the source
headline in `injury_note` so a human can sanity-check.

Google News RSS (reachable, no key, returns XML):
    https://news.google.com/rss/search?q=<query>%20when:14d&hl=en-US&gl=US&ceid=US:en
"""

from __future__ import annotations

import html
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import config
from .base import Source, SourceResult, FighterPatch, http_get
from .espn_common import days_until_next_card

RSS = "https://news.google.com/rss/search"

# Keyword → (field to set, severity). Ordered; first strong match wins.
# Word-boundary regexes keep "outlast" from matching "out", etc.
WITHDRAW_PAT = re.compile(r"\b(withdraw|withdraws|pulls out|pulled|out of|off the card|scrapped)\b", re.I)
INJURY_PAT = re.compile(r"\b(injury|injured|injures|torn|tear|fractur|broken|surgery|hurt|banged up|undisclosed injury)\b", re.I)
WEIGHT_PAT = re.compile(r"\b(missed weight|misses weight|missed the mark|weight miss|overweight|off weight)\b", re.I)


def _titles(xml: str) -> List[str]:
    # Grab <title> nodes; skip the first (feed title). Unescape entities.
    raw = re.findall(r"<title>(.*?)</title>", xml, re.S)
    return [html.unescape(t).strip() for t in raw[1:]]


# Max character distance between the fighter's surname and the trigger keyword
# for the story to be attributed to that fighter. Rejects headlines like
# "Usman has sympathy for McGregor following injury" (keyword ~55 chars away,
# with a different name in between) while keeping "Du Plessis out with injury".
PROXIMITY_WINDOW = 32


def _near(title: str, surname: str, pat: re.Pattern) -> bool:
    """True if any keyword match sits within PROXIMITY_WINDOW of the surname."""
    low = title.lower()
    name_pos = [m.start() for m in re.finditer(re.escape(surname), low)]
    if not name_pos:
        return False
    for km in pat.finditer(title):
        kpos = km.start()
        if min(abs(kpos - np) for np in name_pos) <= PROXIMITY_WINDOW:
            return True
    return False


def _classify(title: str, fighter_name: str) -> Tuple[Dict[str, Any], str]:
    """
    Return (fields_to_set, matched_kind) for a single headline, or ({}, "").

    Requires the trigger keyword to be in close PROXIMITY to the fighter's
    surname, so a story whose injury subject is someone else (but which happens
    to mention our fighter) does not flag them. Still imperfect — verify flags.
    """
    surname = fighter_name.split()[-1].lower()
    if surname not in title.lower():
        return {}, ""
    if _near(title, surname, WEIGHT_PAT):
        return {"missed_weight": True, "injury_note": title}, "missed_weight"
    if _near(title, surname, WITHDRAW_PAT):
        return {"withdrawn": True, "active_injury": True, "injury_note": title}, "withdrawn"
    if _near(title, surname, INJURY_PAT):
        return {"active_injury": True, "injury_note": title}, "injury"
    return {}, ""


class InjuryNewsSource(Source):
    name = "injury_news"

    def fetch(self, state: Dict[str, Any]) -> SourceResult:
        res = SourceResult()
        fighters = state.get("fighters", {})
        now = datetime.now(timezone.utc)

        # Budget guard: skip news entirely when no card is imminent — nothing
        # urgent to learn, and it saves Google News from needless traffic.
        days = days_until_next_card(now.strftime("%Y-%m-%d"))
        if days is not None and days > config.NEWS_LOOKAHEAD_DAYS:
            res.ok = True
            res.notes.append(f"news SKIPPED: next card {days}d away "
                             f"(> {config.NEWS_LOOKAHEAD_DAYS}d lookahead)")
            return res

        # Only search fighters that appear in a watched matchup.
        watched_keys = set()
        for m in state.get("matchups", []):
            watched_keys.add(m["a"])
            watched_keys.add(m["b"])

        news_meta = state.setdefault("meta", {}).setdefault("news_last", {})

        # Build the queue: skip fictional fighters and any queried too recently
        # (per-fighter cooldown), then cap the number of requests this cycle.
        queue: List[str] = []
        for key in sorted(watched_keys):
            rec = fighters.get(key, {})
            name = rec.get("display_name") or rec.get("name") or ""
            if not name or rec.get("fictional"):
                continue
            last = news_meta.get(key)
            if last:
                try:
                    if (now - datetime.fromisoformat(last)).total_seconds() / 3600 \
                            < config.NEWS_MIN_INTERVAL_HOURS:
                        continue  # queried recently — cooldown
                except Exception:
                    pass
            queue.append(key)

        # Prioritise the fighters we've gone longest without checking (oldest
        # timestamp first, never-checked first), then cap the burst.
        queue.sort(key=lambda k: news_meta.get(k) or "")
        capped = queue[:config.NEWS_MAX_PER_CYCLE]
        if len(queue) > len(capped):
            res.notes.append(f"news: {len(queue)} due, querying {len(capped)} "
                             f"this cycle (rest next cycle)")

        any_ok = False
        for key in capped:
            rec = fighters.get(key, {})
            name = rec.get("display_name") or rec.get("name") or ""
            news_meta[key] = now.isoformat()   # stamp attempt (success or not)

            query = f'"{name}" UFC injury OR weight OR withdraw when:14d'
            url = f"{RSS}?q={urllib.parse.quote(query)}&hl=en-US&gl=US&ceid=US:en"
            r = http_get(url)
            if r is None or r.status_code != 200:
                res.notes.append(f"news unavailable for {name}")
                continue
            any_ok = True

            # Take the strongest signal across the recent headlines.
            best_fields: Dict[str, Any] = {}
            best_kind = ""
            priority = {"missed_weight": 3, "withdrawn": 2, "injury": 1}
            for title in _titles(r.text)[:12]:
                fields, kind = _classify(title, name)
                if kind and priority.get(kind, 0) > priority.get(best_kind, 0):
                    best_fields, best_kind = fields, kind

            if not best_kind:
                # No adverse news: clear a previously-set injury flag ONLY if it
                # came from news (we don't clobber a result-confirmed state).
                if rec.get("active_injury") and rec.get("_injury_from_news"):
                    res.patches.append(FighterPatch(
                        key=key,
                        fields={"active_injury": False, "withdrawn": False,
                                "missed_weight": False, "injury_note": "",
                                "_injury_from_news": False},
                        reason=f"{name}: no adverse news in last 14d — clearing flag",
                    ))
                continue

            best_fields["_injury_from_news"] = True
            res.patches.append(FighterPatch(
                key=key, fields=best_fields,
                reason=f"{name}: {best_kind} — \"{best_fields.get('injury_note','')[:80]}\"",
            ))
            res.events.append({
                "type": f"health_{best_kind}",
                "detail": f"{name}: {best_fields.get('injury_note','')[:120]}",
            })

        res.ok = any_ok
        return res
