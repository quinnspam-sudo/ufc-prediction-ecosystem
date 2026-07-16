"""
sources/ufcstats.py
====================
Real per-fighter career stats from ufcstats.com, via a headless browser.

UFCStats has the richest granular stats (SLpM, SApM, TD/Sub rates, etc.) but
sits behind a JS proof-of-work bot-wall — plain `requests` gets a "Checking
your browser…" stub with no data. A real (headless) browser executes the
challenge script and gets the actual page, so we use Playwright's bundled
Chromium, which is free on the GitHub Actions runner.

Each cycle we pick a small batch of fighters still flagged `needs_stats`
(auto-discovered, currently on league-average placeholders — see
`sources/espn_schedule.py`), search UFCStats by name, open the best-matching
fighter page, and scrape the "Career statistics" box. A successful scrape
clears `needs_stats` and `stats_approx`, which lights up real predictions +
value flags for that fighter in `sync.py`.

Budget: no external quota exists here (unofficial site, no key), but a
headless-browser scrape costs real runner time per fighter, so we cap the
batch size and add a per-fighter cooldown (`config.UFCSTATS_MAX_PER_CYCLE`,
`config.UFCSTATS_MIN_INTERVAL_HOURS`) purely to stay a light, polite scraper.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
from .base import Source, SourceResult, FighterPatch

SEARCH_URL = "http://ufcstats.com/statistics/fighters/search"

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # Playwright is optional until this source actually runs
    sync_playwright = None  # type: ignore

# UFCStats "Career statistics" label -> FighterRawStats field, with a
# converter (percentages arrive as e.g. "51%").
_STAT_MAP = {
    "SLpM": ("slpm", float),
    "Str. Acc.": ("strike_acc", lambda s: float(s.strip("%")) / 100.0),
    "SApM": ("sapm", float),
    "Str. Def": ("strike_def", lambda s: float(s.strip("%")) / 100.0),
    "TD Avg.": ("td_avg", float),
    "TD Acc.": ("td_acc", lambda s: float(s.strip("%")) / 100.0),
    "TD Def.": ("td_def", lambda s: float(s.strip("%")) / 100.0),
    "Sub. Avg.": ("sub_avg", float),
}


def _norm(name: str) -> str:
    return " ".join(name.lower().replace(".", "").split())


def _parse_career_box(text: str) -> Dict[str, float]:
    """Parse the 'CAREER STATISTICS:' info-box inner_text into raw fields."""
    out: Dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        label, value = label.strip(), value.strip()
        mapping = _STAT_MAP.get(label)
        if not mapping:
            continue
        field, conv = mapping
        try:
            out[field] = conv(value)
        except (ValueError, ZeroDivisionError):
            continue
    return out


class _Scraper:
    """Thin wrapper so we launch one browser per cycle, not one per fighter."""

    def __init__(self) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()

    def find_fighter_url(self, name: str) -> Optional[str]:
        # Full-name queries return nothing useful on this site's search box;
        # searching by surname (last word) works and we disambiguate below.
        surname = urllib.parse.quote(name.split()[-1])
        page = self._browser.new_page()
        try:
            page.goto(f"{SEARCH_URL}?query={surname}",
                      wait_until="networkidle", timeout=20_000)
            page.wait_for_timeout(500)
            rows = page.query_selector_all("table.b-statistics__table tbody tr")
            target = _norm(name)
            candidates = []
            for row in rows:
                link = row.query_selector("a")
                if not link:
                    continue
                href = link.get_attribute("href")
                cells = [c.strip() for c in row.inner_text().split("\t") if c.strip()]
                if not href or len(cells) < 2:
                    continue
                row_name = _norm(f"{cells[0]} {cells[1]}")
                candidates.append((row_name, href))
            for row_name, href in candidates:
                if row_name == target:
                    return href
            # No exact match: only trust a single unambiguous result.
            if len(candidates) == 1:
                return candidates[0][1]
            return None
        finally:
            page.close()

    def scrape_career_stats(self, url: str) -> Dict[str, float]:
        page = self._browser.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=20_000)
            page.wait_for_timeout(500)
            stats: Dict[str, float] = {}
            for box in page.query_selector_all(".b-list__info-box"):
                text = box.inner_text()
                if "CAREER STATISTICS" in text.upper():
                    stats = _parse_career_box(text)
                    break
            if stats:
                # Same page also lists the full fight history (result + method
                # per bout) — harvest method-of-victory counts for the
                # Bayesian finish-rate shrink at zero extra requests.
                stats.update(self._parse_fight_history(page))
            return stats
        finally:
            page.close()

    @staticmethod
    def _parse_fight_history(page) -> Dict[str, int]:
        """
        Count wins / KO wins / sub wins from the fight-history table rows.

        Each row's inner_text starts with the result flag ("win"/"loss"/...)
        and contains the method code (KO/TKO, SUB, U-DEC, S-DEC, M-DEC).
        Only listed (UFC-era) fights are counted — consistent, clean data
        beats an inflated all-career record with unknown methods. Returns {}
        when the table isn't found so we never overwrite with zeros.
        """
        rows = page.query_selector_all(
            "table.b-fight-details__table tbody tr.b-fight-details__table-row")
        wins = ko = sub = 0
        parsed_any = False
        for row in rows:
            text = row.inner_text()
            if not text.strip():
                continue
            first = text.strip().split()[0].lower()
            if first not in ("win", "loss", "draw", "nc", "next"):
                continue
            parsed_any = True
            if first != "win":
                continue
            wins += 1
            upper = text.upper()
            if "KO/TKO" in upper:
                ko += 1
            elif re.search(r"\bSUB\b", upper):
                sub += 1
        if not parsed_any:
            return {}
        return {"career_wins": wins, "career_ko_wins": ko,
                "career_sub_wins": sub}


class UfcStatsSource(Source):
    name = "ufcstats"

    def fetch(self, state: Dict[str, Any]) -> SourceResult:
        res = SourceResult()

        if sync_playwright is None:
            res.ok = False
            res.notes.append("playwright not installed — ufcstats source skipped")
            return res

        fighters = state.get("fighters", {})
        now = datetime.now(timezone.utc)
        cooldown_meta = state.setdefault("meta", {}).setdefault("ufcstats_last", {})

        queue: List[str] = []
        for key, rec in fighters.items():
            if not rec.get("needs_stats") or rec.get("fictional"):
                continue
            last = cooldown_meta.get(key)
            if last:
                try:
                    if (now - datetime.fromisoformat(last)).total_seconds() / 3600 \
                            < config.UFCSTATS_MIN_INTERVAL_HOURS:
                        continue
                except Exception:
                    pass
            queue.append(key)

        # Longest-waiting (or never-tried) fighters first, then cap the batch.
        queue.sort(key=lambda k: cooldown_meta.get(k) or "")
        capped = queue[:config.UFCSTATS_MAX_PER_CYCLE]
        if len(queue) > len(capped):
            res.notes.append(f"ufcstats: {len(queue)} due, scraping {len(capped)} "
                             f"this cycle (rest next cycle)")
        if not capped:
            res.ok = True
            return res

        scraper = _Scraper()
        any_ok = False
        try:
            for key in capped:
                rec = fighters[key]
                name = rec.get("display_name") or rec.get("name") or key
                cooldown_meta[key] = now.isoformat()  # stamp attempt either way
                try:
                    url = scraper.find_fighter_url(name)
                    if not url:
                        res.notes.append(f"ufcstats: no unambiguous match for {name}")
                        continue
                    stats = scraper.scrape_career_stats(url)
                    if not stats:
                        res.notes.append(f"ufcstats: no career stats parsed for {name}")
                        continue
                    metric_fields = {f for f, _ in _STAT_MAP.values()}
                    if all(v == 0.0 for k, v in stats.items()
                           if k in metric_fields):
                        # No real UFC fight-metric data yet (e.g. a debut whose
                        # page shows all zeros/dashes) — a degenerate all-zero
                        # profile is worse than the placeholder, so leave
                        # needs_stats set and retry after the next cooldown.
                        res.notes.append(f"ufcstats: {name} has no fight data yet "
                                         f"(all-zero career box) — keeping placeholder")
                        continue
                    any_ok = True
                    fields = dict(stats)
                    fields["needs_stats"] = False
                    fields["stats_approx"] = False
                    res.patches.append(FighterPatch(
                        key=key, fields=fields,
                        reason=f"{name}: real career stats scraped from UFCStats",
                    ))
                    res.events.append({
                        "type": "ufcstats_scraped",
                        "detail": f"{name}: real stats populated ({url})",
                    })
                except Exception as exc:
                    res.notes.append(f"ufcstats: error scraping {name}: {exc!r}")
        finally:
            scraper.close()

        res.ok = any_ok
        return res
