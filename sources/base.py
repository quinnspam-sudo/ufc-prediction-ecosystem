"""
sources/base.py
===============
Common contract for every live-data source adapter.

Each source implements `fetch(state) -> SourceResult`, returning:
  * fighter field patches (partial updates merged into the data store),
  * changelog events (human-readable "what changed" records),
  * odds updates keyed by matchup label.

Sources must NEVER raise on network / parse failure — they catch, log to the
returned `notes`, and return an empty result. A flaky source must not take the
whole pipeline down. This is enforced by the `safe_fetch` wrapper.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:  # requests is optional until a live source is enabled
    requests = None  # type: ignore

DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (UFC-Prediction-Ecosystem)"}


def http_get(url: str, *, timeout: int = 25, tries: int = 4,
             headers: Optional[Dict[str, str]] = None,
             params: Optional[Dict[str, Any]] = None):
    """
    GET with retries + backoff. Returns a `requests.Response` or None.

    Never raises — callers treat None as "source unavailable this cycle".
    """
    if requests is None:
        return None
    hdr = {**DEFAULT_HEADERS, **(headers or {})}
    for i in range(tries):
        try:
            r = requests.get(url, headers=hdr, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            # 4xx (e.g. 401 missing key) won't fix on retry — bail early.
            if 400 <= r.status_code < 500:
                return r
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return None


@dataclass
class FighterPatch:
    """A partial update to one fighter's stored record."""
    key: str                       # data-store fighter key
    fields: Dict[str, Any]         # FighterRawStats field -> new value
    reason: str = ""               # why (for the changelog)


@dataclass
class SourceResult:
    """Everything a single source produced this cycle."""
    patches: List[FighterPatch] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)   # {type, detail}
    odds: Dict[str, Dict[str, int]] = field(default_factory=dict)  # label -> {a_ml,b_ml}
    # Auto-discovery: brand-new fighters/matchups to add to the store.
    new_fighters: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # tempkey -> field dict
    new_matchups: List[Dict[str, Any]] = field(default_factory=list)       # {a,b,rounds,label,...}
    # Completed bouts for calibration scoring: {winner, loser}.
    fight_results: List[Dict[str, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)               # diagnostics
    ok: bool = True                # False if the source was unavailable


class Source(ABC):
    """Base class for a live-data source."""

    name: str = "source"

    @abstractmethod
    def fetch(self, state: Dict[str, Any]) -> SourceResult:
        """Pull fresh data given the current data-store `state`."""
        raise NotImplementedError

    def safe_fetch(self, state: Dict[str, Any]) -> SourceResult:
        """Run `fetch`, converting any exception into a clean empty result."""
        try:
            res = self.fetch(state)
            return res if res is not None else SourceResult(ok=False)
        except Exception as exc:  # defensive: one bad source can't crash sync
            return SourceResult(ok=False, notes=[f"{self.name} error: {exc!r}"])
