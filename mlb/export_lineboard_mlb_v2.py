#!/usr/bin/env python3
"""
export_lineboard_mlb_v2.py -- same as export_lineboard_mlb.py (kept, unmodified),
plus one information-only addition for playoff games: the model's chance of
winning the whole SERIES, shown next to the single-game read.

How the series number is worked out (stated plainly on the card too): the
model's per-game win probability for today's game is assumed to hold for every
remaining game of the series, and the series status (games in series, wins so
far) comes from MLB's public schedule API at export time. It ignores who
pitches later games and where they're played. It is NOT a validated pick -- no
test has been run on it -- only a way to compare against a book's series price.
Only games not yet final get it; if MLB's API can't be reached, the export
still runs, just without the series line.

Original description follows.

export_lineboard_mlb.py -- turns the MLB tracker database into the exact
Lineboard (DeVig.AI app) documents, so the app can be updated without anyone
hand-building them.

Reads only. Never touches MLB_PROSPECTIVE_TRACKER_V1.sqlite3's contents; it
writes one file, lineboard_export.json, next to itself. A scheduled Claude
task copies changed documents from that file into the app's database.

What it produces, per game that has a locked moneyline pick:
  - a `slate` doc   (game card: market line + model read)
  - a `decisions` doc (the app's ML/PASS call, run-line COVER/PASS call,
    and -- once the game is graded -- the real outcome)

Decision rules are the app's own validated MLB cutoffs, unchanged:
  moneyline: winner prob >= 0.5857 -> ML top25, >= 0.5511 -> ML top50, else PASS
  run line:  pick prob   >= 0.6747 -> COVER top25, >= 0.6383 -> COVER top50, else PASS
Run-line picks are only exported when the lock captured a real market line
(the same rows the v2 grader grades); line-less placeholder rows are skipped
so the app never shows a pick that can't be graded.

Outcomes come straight from the graders' own tables (grades / spread_grades,
the latter written by mlb_spread_live_tracker_grade_results_v2.py, which grades
against the model's home-anchored -1.5 definition). Nothing is re-derived here
except "did the app's ML side win", which is just the final score.

Each document carries `export_hash` (sha256 of its content, excluding
synced_at) so the sync step can skip documents that haven't changed.

Usage:
    python3 export_lineboard_mlb.py
"""
import hashlib
import json
import math
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DB = SCRIPT_DIR / "MLB_PROSPECTIVE_TRACKER_V1.sqlite3"
OUT = SCRIPT_DIR / "lineboard_export.json"

ML_TOP25, ML_TOP50 = 0.5857, 0.5511
SP_TOP25, SP_TOP50 = 0.6747, 0.6383
RECENT_DAYS = 5

# Retrosheet codes -> the display codes the app already uses
DISPLAY = {"ANA": "LAA", "CHA": "CHW", "CHN": "CHC", "KCA": "KC", "LAN": "LAD", "NYA": "NYY",
           "NYN": "NYM", "SDN": "SD", "SFN": "SF", "SLN": "STL", "TBA": "TB", "WAS": "WSH"}


def disp(code):
    return DISPLAY.get(code, code)


def tier(p, top25, top50):
    return "top25" if p >= top25 else ("top50" if p >= top50 else None)


POSTSEASON_TYPES = {"F", "D", "L", "W"}  # Wild Card, Division Series, LCS, World Series


def fetch_series_info(game_pks):
    """{gamePk: info} for postseason games that aren't final yet. Never raises."""
    if not game_pks:
        return {}
    url = ("https://statsapi.mlb.com/api/v1/schedule?sportId=1&hydrate=seriesStatus&gamePks="
           + ",".join(str(int(k)) for k in game_pks))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "lineboard-export/2.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # network hiccup: export without series lines
        print(f"NOTE: series info unavailable this run ({type(e).__name__}); reusing the last exported series lines.")
        return None
    out = {}
    for d in data.get("dates", []):
        for g in d.get("games", []):
            if g.get("gameType") not in POSTSEASON_TYPES:
                continue
            if (g.get("status") or {}).get("abstractGameState") == "Final":
                continue
            n = g.get("gamesInSeries")
            hw = ((g.get("teams") or {}).get("home") or {}).get("leagueRecord", {}).get("wins")
            aw = ((g.get("teams") or {}).get("away") or {}).get("leagueRecord", {}).get("wins")
            if not n or hw is None or aw is None:
                continue
            out[int(g["gamePk"])] = {"games_in_series": int(n), "series_game_number": g.get("seriesGameNumber"),
                                     "description": g.get("seriesDescription"), "home_wins": int(hw), "away_wins": int(aw)}
    return out


def series_win_prob(p, need_a, need_b):
    """P(team A wins the series) when A needs need_a more wins, B needs need_b,
    and A wins each remaining game with probability p (same every game)."""
    if need_a <= 0:
        return 1.0
    if need_b <= 0:
        return 0.0
    q = 1 - p
    return sum(math.comb(need_a - 1 + i, i) * p ** need_a * q ** i for i in range(need_b))


def fair_american(prob):
    if prob <= 0 or prob >= 1:
        return None
    return int(round(-100 * prob / (1 - prob))) if prob >= 0.5 else int(round(100 * (1 - prob) / prob))


def doc_hash(data):
    body = {k: v for k, v in data.items() if k != "synced_at"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    ml_grades = {r["pick_id"]: r for r in conn.execute("SELECT * FROM grades")}
    spreads = {r["game_id"]: r for r in conn.execute(
        "SELECT * FROM spread_picks WHERE home_spread_point IS NOT NULL AND away_spread_point IS NOT NULL")}
    sp_grades = {r["pick_id"]: r for r in conn.execute("SELECT * FROM spread_grades")}
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Keep the file small: only games from the last few days, plus anything
    # still waiting on a result. Older finished games are already in the app
    # and never change again.
    window_start = (datetime.now(timezone.utc).date() - timedelta(days=RECENT_DAYS)).isoformat()

    pending_pks = [r["statsapi_game_pk"] for r in conn.execute(
        "SELECT statsapi_game_pk FROM picks WHERE graded = 0 AND statsapi_game_pk IS NOT NULL")]
    series_info = fetch_series_info(pending_pks)
    prev_series = {}
    if series_info is None:  # API unreachable: keep what the last export said instead of flapping the app
        series_info = {}
        try:
            for d in json.loads(OUT.read_text())["docs"]:
                if d["collection"] == "slate" and d["data"].get("model", {}).get("series"):
                    prev_series[d["doc_id"]] = d["data"]["model"]["series"]
        except (OSError, ValueError, KeyError):
            pass

    docs = []
    for r in conn.execute("SELECT * FROM picks ORDER BY kickoff_time"):
        gid = r["game_id"]
        if r["game_date"] < window_start and r["graded"] == 1:
            continue
        home, away = disp(r["home_team"]), disp(r["away_team"])
        p_home = r["model_prob_home"]
        side = home if p_home >= 0.5 else away
        wp = max(p_home, 1 - p_home)
        ml_tier = tier(wp, ML_TOP25, ML_TOP50)

        if r["home_moneyline_kalshi"] is not None:
            book, mh, ma = "Kalshi", r["home_moneyline_kalshi"], r["away_moneyline_kalshi"]
        else:
            book, mh, ma = "Hard Rock Bet", r["home_moneyline_hardrockbet_fl"], r["away_moneyline_hardrockbet_fl"]
        market = {"book": book, "moneyline_home": int(mh), "moneyline_away": int(ma)}
        model = {
            "available": True, "status": "locked", "winner_prob": wp, "winner_side": side,
            "source_snapshot_time": r["lock_timestamp_utc"],
            "note": (f"Locked pregame by the tracker ({r['model_version']}). Probable starters "
                     f"{r['away_starter_name']} vs {r['home_starter_name']}. Odds from {book} "
                     f"(n_books_used={r['n_books_used']})."),
        }

        si = series_info.get(int(r["statsapi_game_pk"])) if r["statsapi_game_pk"] is not None else None
        if si:
            need = si["games_in_series"] // 2 + 1
            ph_series = series_win_prob(p_home, need - si["home_wins"], need - si["away_wins"])
            fav = home if ph_series >= 0.5 else away
            fav_p = max(ph_series, 1 - ph_series)
            model["series"] = {
                "description": si["description"], "games_in_series": si["games_in_series"],
                "series_game_number": si["series_game_number"], "home_wins": si["home_wins"],
                "away_wins": si["away_wins"], "p_home_series": round(ph_series, 4),
                "favored": fav, "favored_prob": round(fav_p, 4), "fair_price_favored": fair_american(fav_p),
                "method": "per-game model probability for this game applied to every remaining game; information only, not a validated pick",
            }

        elif gid in prev_series:
            model["series"] = prev_series[gid]

        spread_decision = None
        s = spreads.get(gid)
        if s is not None:
            pp = s["spread_pick_prob"]
            st = tier(pp, SP_TOP25, SP_TOP50)
            model.update({"spread_prob": pp, "spread_side": s["spread_side_display"],
                          "spread_side_team": s["spread_side_team"]})
            market.update({"spread_home": s["home_spread_point"], "spread_home_odds": s["home_spread_price"],
                           "spread_away_odds": s["away_spread_price"]})
            spread_decision = {"action": "COVER" if st else "PASS", "prob": pp if st else None,
                               "side": s["spread_side_display"] if st else None, "tier": st}

        outcome = {"resolved": False, "home_score": None, "away_score": None, "decision_correct": None,
                   "spread_decision_correct": None, "spread_correct": None, "teaser_correct": None}
        g = ml_grades.get(r["id"])
        if g is not None:
            hs, as_ = g["actual_home_score"], g["actual_away_score"]
            home_won = hs > as_
            outcome.update({"resolved": True, "home_score": hs, "away_score": as_,
                            "graded_source": "statsapi.mlb.com final score via the GitHub Actions graders"})
            if ml_tier:
                outcome["decision_correct"] = home_won if side == home else (not home_won)
            if spread_decision and spread_decision["action"] == "COVER":
                sg = sp_grades.get(s["id"])
                outcome["spread_decision_correct"] = bool(sg["model_pick_correct"]) if sg is not None else None
                if sg is None:
                    outcome["resolved"] = False  # wait until the run-line grade exists too

        base = {"game_id": gid, "home_team": home, "away_team": away, "game_date": r["game_date"],
                "kickoff_time": r["kickoff_time"], "season": r["season"], "sport": "MLB"}
        slate = dict(base, market=market, model=model, synced_at=now)
        decision = dict(base, locked_at=r["lock_timestamp_utc"],
                        decision={"action": "ML" if ml_tier else "PASS", "prob": wp,
                                  "side": side if ml_tier else None, "tier": ml_tier},
                        market_snapshot={"moneyline_home": int(mh), "moneyline_away": int(ma)},
                        outcome=outcome, spread_read=None, teaser_signal=None)
        if spread_decision:
            decision["spread_decision"] = spread_decision
        for coll, data in (("slate", slate), ("decisions", decision)):
            data["export_hash"] = doc_hash(data)
            docs.append({"collection": coll, "doc_id": gid, "data": data})

    n_res = sum(1 for d in docs if d["collection"] == "decisions" and d["data"]["outcome"]["resolved"])
    # Only rewrite the file when some document actually changed, so a grading
    # tick with nothing new doesn't create a pointless commit.
    new_keys = sorted((d["collection"], d["doc_id"], d["data"]["export_hash"]) for d in docs)
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text())
            old_keys = sorted((d["collection"], d["doc_id"], d["data"]["export_hash"]) for d in old["docs"])
            if old_keys == new_keys:
                print(f"No changes: {len(docs)} docs ({len(docs)//2} games, {n_res} resolved) already exported.")
                return
        except (ValueError, KeyError):
            pass
    OUT.write_text(json.dumps({"sport": "MLB", "generated_at": now, "n_docs": len(docs), "docs": docs},
                              indent=1, sort_keys=True, default=str))
    print(f"Wrote {len(docs)} Lineboard docs ({len(docs)//2} games, {n_res} resolved) to {OUT}")


if __name__ == "__main__":
    main()
