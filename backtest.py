"""
backtest.py
===========
Backtest the simulation pipeline against every completed UFC fight since a
cutoff date (default: UFC 300, 2024-04-13), using UFCStats as the source of
both results and fighter stats.

    ./.venv/bin/python backtest.py                 # scrape + simulate + score
    ./.venv/bin/python backtest.py --since 2024-04-13 --iterations 1000

All scraped data is cached in data_store/backtest_cache.json so re-runs
(e.g. after tuning constants) skip the ~1hr scrape and only re-simulate.

HONESTY CAVEAT (printed on every report): fighter stats are scraped AS OF
TODAY, not as of each fight night. Career averages therefore already contain
the fights being predicted (lookahead bias) — treat the resulting accuracy as
an UPPER BOUND, not the live number. The one leak we CAN remove, we do:
career win/KO/sub finish counts are recomputed per-fight from the fight
history using only bouts BEFORE the fight date.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

CACHE_PATH = os.path.join(os.path.dirname(__file__), "data_store",
                          "backtest_cache.json")
OUT_PATH = os.path.join(os.path.dirname(__file__), "reports", "backtest.json")

EVENTS_URL = "http://ufcstats.com/statistics/events/completed?page=all"
POLITE_DELAY_S = 0.4          # between page fetches — stay a polite scraper

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


def _parse_us_date(text: str) -> Optional[str]:
    """'April 13, 2024' -> '2024-04-13'."""
    m = re.search(r"([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})", text)
    if not m or m.group(1) not in _MONTHS:
        return None
    return f"{int(m.group(3)):04d}-{_MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"


# ---------------------------------------------------------------------------
# Scraping (Playwright; reuses the prod scraper's browser wrapper)
# ---------------------------------------------------------------------------
class BacktestScraper:
    def __init__(self) -> None:
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()

    def _page_text_rows(self, url: str, selector: str) -> List[Tuple[str, str]]:
        """Return (inner_text, first-link-href) for each row of `selector`."""
        page = self._browser.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(400)
            out = []
            for row in page.query_selector_all(selector):
                link = row.query_selector("a")
                out.append((row.inner_text(),
                            link.get_attribute("href") if link else ""))
            return out
        finally:
            page.close()
            time.sleep(POLITE_DELAY_S)

    def list_events(self, since_iso: str) -> List[Dict[str, str]]:
        rows = self._page_text_rows(
            EVENTS_URL, "table.b-statistics__table-events tbody tr")
        events = []
        for text, href in rows:
            d = _parse_us_date(text)
            if not d or not href or d < since_iso:
                continue
            if d >= date.today().isoformat():
                continue                      # future/today: not resolved
            name = text.strip().splitlines()[0].strip()
            events.append({"name": name, "date": d, "url": href})
        return events

    def event_fights(self, url: str) -> List[Dict[str, Any]]:
        """Each completed fight row: winner/loser names+urls, method, round,
        weight class. UFCStats lists the WINNER first on decided bouts."""
        page = self._browser.new_page()
        fights = []
        try:
            page.goto(url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(400)
            for row in page.query_selector_all(
                    "tbody.b-fight-details__table-body tr"):
                cells = row.query_selector_all("td")
                if len(cells) < 9:
                    continue
                flag = cells[0].inner_text().strip().lower()
                if not flag.startswith("win"):
                    continue                  # draws/NCs: skip (no "winner")
                links = cells[1].query_selector_all("a")
                if len(links) != 2:
                    continue
                w_name = links[0].inner_text().strip()
                l_name = links[1].inner_text().strip()
                w_url = links[0].get_attribute("href") or ""
                l_url = links[1].get_attribute("href") or ""
                wc_txt = cells[6].inner_text().strip()
                method_txt = cells[7].inner_text().strip().upper()
                rnd_txt = cells[8].inner_text().strip()
                if "KO" in method_txt:
                    method = "KO/TKO"
                elif "SUB" in method_txt:
                    method = "Submission"
                elif "DEC" in method_txt:
                    method = "Decision"
                else:
                    method = ""              # DQ/overturned etc.
                try:
                    rnd = int(rnd_txt.split()[0])
                except (ValueError, IndexError):
                    rnd = 0
                fights.append({
                    "winner": w_name, "loser": l_name,
                    "winner_url": w_url, "loser_url": l_url,
                    "weight_class_raw": wc_txt, "method": method, "round": rnd,
                })
        finally:
            page.close()
            time.sleep(POLITE_DELAY_S)
        return fights

    def fighter(self, url: str) -> Dict[str, Any]:
        """Career box stats + biometrics + dated fight history."""
        page = self._browser.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(400)
            out: Dict[str, Any] = {}
            body = page.inner_text("body")
            # Career stat box labels (same site-wide format the prod scraper uses).
            for label, field, conv in (
                ("SLpM", "slpm", float),
                ("Str. Acc.", "strike_acc", lambda s: float(s.strip("%")) / 100),
                ("SApM", "sapm", float),
                ("Str. Def", "strike_def", lambda s: float(s.strip("%")) / 100),
                ("TD Avg.", "td_avg", float),
                ("TD Acc.", "td_acc", lambda s: float(s.strip("%")) / 100),
                ("TD Def.", "td_def", lambda s: float(s.strip("%")) / 100),
                ("Sub. Avg.", "sub_avg", float),
            ):
                m = re.search(rf"{re.escape(label)}:\s*([\d.]+%?)", body)
                if m:
                    try:
                        out[field] = conv(m.group(1))
                    except ValueError:
                        pass
            for label, field in (("Height:", "height"), ("Reach:", "reach"),
                                 ("STANCE:", "stance"), ("DOB:", "dob")):
                m = re.search(rf"{label}\s*([^\n]+)", body)
                if m:
                    out[field] = m.group(1).strip()
            # Fight history rows with dates + result + method (oldest data we
            # need for point-in-time finish counts).
            hist = []
            for row in page.query_selector_all(
                    "table.b-fight-details__table tbody tr.b-fight-details__table-row"):
                text = row.inner_text()
                first = text.strip().split()[0].lower() if text.strip() else ""
                if first not in ("win", "loss", "draw", "nc"):
                    continue
                d = _parse_us_date(text)
                upper = text.upper()
                if "KO/TKO" in upper:
                    meth = "KO/TKO"
                elif re.search(r"\bSUB\b", upper):
                    meth = "Submission"
                else:
                    meth = "Decision"
                hist.append({"result": first, "date": d or "", "method": meth})
            out["history"] = hist
            return out
        finally:
            page.close()
            time.sleep(POLITE_DELAY_S)


# ---------------------------------------------------------------------------
# Raw-stat assembly
# ---------------------------------------------------------------------------
def _inches(txt: str) -> Optional[float]:
    m = re.match(r"(\d+)'\s*(\d+)", txt or "")
    if m:
        return int(m.group(1)) * 12 + int(m.group(2))
    m = re.match(r'(\d+)"', (txt or "").strip())
    return float(m.group(1)) if m else None


def _age_on(dob_txt: str, fight_date: str) -> int:
    d = _parse_us_date(dob_txt or "")
    if not d:
        return 30
    born = datetime.fromisoformat(d).date()
    fd = datetime.fromisoformat(fight_date).date()
    return max(18, fd.year - born.year - ((fd.month, fd.day) < (born.month, born.day)))


def _map_wc(raw: str) -> str:
    from data_ingestion import WEIGHT_CLASSES
    t = re.sub(r"(WOMEN'S|UFC|TITLE|BOUT|INTERIM)", "", (raw or "").upper())
    for wc in WEIGHT_CLASSES:
        if wc.upper() in t:
            return wc
    if "STRAW" in t:
        return "Flyweight"
    return "Lightweight"


def _build_raw(fd: Dict[str, Any], name: str, wc: str, fight_date: str):
    """FighterRawStats from scraped data; finish counts are POINT-IN-TIME
    (only history rows dated before the fight)."""
    from data_ingestion import FighterRawStats
    hist = fd.get("history", [])
    prior = [h for h in hist if h["date"] and h["date"] < fight_date]
    wins = [h for h in prior if h["result"] == "win"]
    losses_by = [h["method"] for h in prior if h["result"] == "loss"]
    last_before = max((h["date"] for h in prior), default="2023-01-01")
    n_fights = max(len(prior), 1)
    return FighterRawStats(
        name=name,
        age=_age_on(fd.get("dob", ""), fight_date),
        height_in=_inches(fd.get("height", "")) or 70.0,
        reach_in=_inches(fd.get("reach", "")) or 72.0,
        weight_class=wc,
        stance=(fd.get("stance") or "Orthodox").split()[0].title() or "Orthodox",
        slpm=fd.get("slpm", 3.5),
        strike_acc=fd.get("strike_acc", 0.45),
        sapm=fd.get("sapm", 3.5),
        strike_def=fd.get("strike_def", 0.55),
        td_avg=fd.get("td_avg", 1.0),
        td_acc=fd.get("td_acc", 0.35),
        td_def=fd.get("td_def", 0.6),
        sub_avg=fd.get("sub_avg", 0.4),
        sub_def=0.65,
        control_time_pct=min(0.9, fd.get("td_avg", 1.0) * 0.12 + 0.15),
        # Chin proxy: KO losses per prior fight.
        career_knockdowns_suffered=sum(
            1 for m in losses_by if m == "KO/TKO") * 2,
        career_sig_strikes_absorbed=int(fd.get("sapm", 3.5) * 12 * n_fights),
        career_fights=n_fights,
        career_wins=len(wins),
        career_ko_wins=sum(1 for h in wins if h["method"] == "KO/TKO"),
        career_sub_wins=sum(1 for h in wins if h["method"] == "Submission"),
        round_strike_output=[55, 53, 51, 49, 47],
        last_fight_date=last_before,
    )


# ---------------------------------------------------------------------------
# Cache -> simulate -> score
# ---------------------------------------------------------------------------
def collect(since: str) -> Dict[str, Any]:
    if os.path.exists(CACHE_PATH):
        cache = json.load(open(CACHE_PATH))
        if cache.get("since") == since and cache.get("complete"):
            print(f"[cache] using {CACHE_PATH}", file=sys.stderr)
            return cache
    s = BacktestScraper()
    try:
        events = s.list_events(since)
        print(f"[scrape] {len(events)} events since {since}", file=sys.stderr)
        fights: List[Dict[str, Any]] = []
        fighter_urls: Dict[str, str] = {}
        for i, ev in enumerate(events):
            evf = s.event_fights(ev["url"])
            for f in evf:
                f["event"] = ev["name"]
                f["date"] = ev["date"]
                fighter_urls[f["winner_url"]] = f["winner"]
                fighter_urls[f["loser_url"]] = f["loser"]
            fights.extend(evf)
            print(f"[scrape] {i+1}/{len(events)} {ev['name']}: "
                  f"{len(evf)} fights", file=sys.stderr)
        fighters: Dict[str, Dict] = {}
        urls = [u for u in fighter_urls if u]
        for i, u in enumerate(urls):
            try:
                fighters[u] = s.fighter(u)
            except Exception as exc:
                print(f"[scrape] fighter fail {u}: {exc!r}", file=sys.stderr)
            if (i + 1) % 25 == 0:
                print(f"[scrape] fighters {i+1}/{len(urls)}", file=sys.stderr)
                # Checkpoint the cache so a crash doesn't lose the scrape.
                json.dump({"since": since, "complete": False, "fights": fights,
                           "fighters": fighters}, open(CACHE_PATH, "w"))
        cache = {"since": since, "complete": True, "fights": fights,
                 "fighters": fighters}
        json.dump(cache, open(CACHE_PATH, "w"))
        return cache
    finally:
        s.close()


def run(since: str, iterations: int) -> Dict[str, Any]:
    from feature_engineering import build_matchup
    from simulation_engine import run_simulation

    cache = collect(since)
    fights, fighters = cache["fights"], cache["fighters"]

    n = correct = 0
    method_n = method_correct = 0
    dist_n = dist_correct = 0
    brier_sum = 0.0
    by_conf = {"50-60": [0, 0], "60-70": [0, 0], "70-80": [0, 0], "80+": [0, 0]}
    skipped = 0
    # Per-fight predictions, kept for post-hoc constant fitting (λ shrink,
    # finish-rate scaling) without re-simulating.
    preds: List[Dict[str, Any]] = []

    for i, f in enumerate(fights):
        wd = fighters.get(f["winner_url"])
        ld = fighters.get(f["loser_url"])
        if not wd or not ld or "slpm" not in wd or "slpm" not in ld:
            skipped += 1
            continue
        wc = _map_wc(f["weight_class_raw"])
        rounds = 5 if f["round"] > 3 else (5 if "TITLE" in f["weight_class_raw"].upper() else 3)
        try:
            a_raw = _build_raw(wd, f["winner"], wc, f["date"])   # A = actual winner
            b_raw = _build_raw(ld, f["loser"], wc, f["date"])
            pa, pb = build_matchup(a_raw, b_raw, rounds)
            sim = run_simulation(pa, pb, rounds, iterations=iterations, seed=7)
        except Exception:
            skipped += 1
            continue
        decisive = sim.a_wins + sim.b_wins
        if not decisive:
            skipped += 1
            continue
        p_winner = sim.a_wins / decisive       # model prob on the ACTUAL winner
        n += 1
        hit = p_winner >= 0.5
        correct += hit
        brier_sum += (p_winner - 1.0) ** 2
        conf = max(p_winner, 1 - p_winner)
        bucket = ("80+" if conf >= 0.8 else "70-80" if conf >= 0.7
                  else "60-70" if conf >= 0.6 else "50-60")
        # A is the actual winner, so "the model's favorite won" == hit.
        by_conf[bucket][0] += hit
        by_conf[bucket][1] += 1

        # Method scoring: model's most likely fight-level method vs actual.
        if f["method"]:
            mc = sim.method_counts
            probs = {
                "KO/TKO": mc["A"]["KO/TKO"] + mc["B"]["KO/TKO"],
                "Submission": mc["A"]["Submission"] + mc["B"]["Submission"],
                "Decision": (mc["A"]["Unanimous Decision"]
                             + mc["A"]["Split/Majority Decision"]
                             + mc["B"]["Unanimous Decision"]
                             + mc["B"]["Split/Majority Decision"]),
            }
            method_n += 1
            method_correct += max(probs, key=probs.get) == f["method"]
            dist_n += 1
            went = f["method"] == "Decision"
            pred_distance = probs["Decision"] >= probs["KO/TKO"] + probs["Submission"]
            dist_correct += pred_distance == went
            tot = max(sum(probs.values()), 1)
            preds.append({"p": p_winner,
                          "ko": probs["KO/TKO"] / tot,
                          "sub": probs["Submission"] / tot,
                          "dec": probs["Decision"] / tot,
                          "actual": f["method"]})

        if (i + 1) % 100 == 0:
            print(f"[sim] {i+1}/{len(fights)} fights "
                  f"(acc so far {100*correct/max(n,1):.1f}%)", file=sys.stderr)

    # ── Post-hoc constant fitting on the collected predictions ────────────
    # 1. λ shrink minimising winner Brier (what calibration.py fits live).
    best_lam, best_brier = 1.0, float("inf")
    for i in range(2, 21):
        lam = i / 20.0
        b = sum((0.5 + lam * (pr["p"] - 0.5) - 1.0) ** 2 for pr in preds) / max(len(preds), 1)
        if b < best_brier:
            best_lam, best_brier = lam, b
    # 2. Finish-scale f: multiply KO+sub shares by f, renormalise, and see
    #    which f maximises modal-method accuracy (proxy for KO_BASE/SUB_BASE).
    best_f, best_meth = 1.0, -1.0
    for fi in range(4, 21):
        fscale = fi / 10.0
        hits = 0
        for pr in preds:
            ko, sub = pr["ko"] * fscale, pr["sub"] * fscale
            probs = {"KO/TKO": ko, "Submission": sub, "Decision": pr["dec"]}
            hits += max(probs, key=probs.get) == pr["actual"]
        acc = hits / max(len(preds), 1)
        if acc > best_meth:
            best_f, best_meth = fscale, acc

    out = {
        "caveat": ("Fighter rate stats are AS-OF-TODAY (UFCStats shows only "
                   "current career averages), so results contain lookahead "
                   "bias — treat accuracy as an UPPER BOUND. Finish counts "
                   "and age/layoff ARE point-in-time."),
        "fitted": {
            "lambda_shrink": best_lam,
            "brier_at_lambda": round(best_brier, 4),
            "finish_scale": best_f,
            "method_accuracy_at_scale_pct": round(100 * best_meth, 1),
        },
        "since": since, "iterations_per_fight": iterations,
        "fights_scored": n, "fights_skipped": skipped,
        "winner_accuracy_pct": round(100 * correct / n, 1) if n else None,
        "winner_brier_mean": round(brier_sum / n, 4) if n else None,
        "method_fights_scored": method_n,
        "method_accuracy_pct": round(100 * method_correct / method_n, 1)
            if method_n else None,
        "distance_accuracy_pct": round(100 * dist_correct / dist_n, 1)
            if dist_n else None,
        "accuracy_by_confidence": {
            k: {"n": v[1], "accuracy_pct": round(100 * v[0] / v[1], 1) if v[1] else None}
            for k, v in by_conf.items()},
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    json.dump(out, open(OUT_PATH, "w"), indent=2)
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--since", default="2024-04-13")     # UFC 300
    p.add_argument("--iterations", type=int, default=1000)
    args = p.parse_args()
    result = run(args.since, args.iterations)
    print(json.dumps(result, indent=2))
