#!/usr/bin/env python3
"""
mlb_spread_live_tracker_grade_results_v2.py

Bug-fix successor to mlb_spread_live_tracker_grade_results.py (v1 kept,
not modified, for the record).

THE BUG IN v1: v1 judged each run-line pick against the MARKET's stored
spread points (home_spread_point / away_spread_point). But the spread
model (mlb_spread_v1_full_run.py) is home-anchored: its target is always
home_covers = (home_runs - away_runs >= 2), i.e. "home -1.5", regardless
of who the market favors. So model_prob_home_covers is always
P(home wins by 2+), and the model's pick is always either "home -1.5"
(p >= 0.5) or "away +1.5" (p < 0.5) -- which is exactly what
spread_side_display already shows.

When the AWAY team is the market favorite, the market's standard run line
is home +1.5 / away -1.5 -- the opposite orientation. v1 graded those
games against the market's away -1.5 instead of the model's away +1.5,
so any game where the away favorite won by exactly 1 was wrongly scored
as a miss. Found on the first real graded slate (2026-09-23): KC 5, CWS 4
-- the model's pick CWS +1.5 covered, v1 scored it a loss.

v2 grades every pick against the model's own home-anchored definition,
matching the test that validated the thresholds. It:
  1. Re-grades rows v1 already graded, using the final scores v1 already
     stored in spread_grades (no network needed, no scores re-fetched).
  2. Grades any new, not-yet-graded rows via statsapi.mlb.com, same as v1.
Only spread_grades.actual_home_covers / model_pick_correct are rewritten;
spread_picks rows (the pregame lock record) are never touched.

Note, not fixed here: for away-favorite games the lock script logs the
market's standard-run-line price, which is for away -1.5, not the
model's away +1.5 (an alternate line). Prices aren't used in grading, but
any future ROI calculation must not use those stored prices for those rows.

Usage:
    python3 mlb_spread_live_tracker_grade_results_v2.py
"""
import json
import sqlite3
import ssl
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    CTX = ssl.create_default_context()

SCRIPT_DIR = Path(__file__).resolve().parent
DB = SCRIPT_DIR / "MLB_PROSPECTIVE_TRACKER_V1.sqlite3"
RUN_LINE_MARGIN = 2  # home covers -1.5 iff home_runs - away_runs >= 2


def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "mlb-spread-tracker-grade/2.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_final_score(game_pk):
    data = get_json(f"https://statsapi.mlb.com/api/v1/schedule?gamePk={game_pk}&hydrate=linescore")
    for d in data.get("dates", []):
        for g in d.get("games", []):
            if g.get("gamePk") != game_pk:
                continue
            status = g.get("status", {}).get("detailedState")
            if status != "Final":
                return None, status
            teams = g.get("teams", {})
            hs = teams.get("home", {}).get("score")
            as_ = teams.get("away", {}).get("score")
            if hs is None or as_ is None:
                return None, f"Final but score missing ({status})"
            return (int(hs), int(as_)), status
    return None, "gamePk not found in schedule response"


def grade(p_home_covers, home_score, away_score):
    """Model picks home -1.5 if p >= 0.5, else away +1.5. Returns
    (actual_home_covers, model_pick_correct). No push is possible on a
    half-run line."""
    actual_home_covers = int(home_score - away_score >= RUN_LINE_MARGIN)
    model_picked_home = p_home_covers >= 0.5
    return actual_home_covers, int(model_picked_home == bool(actual_home_covers))


def main():
    if not DB.exists():
        print("No MLB tracker DB found yet -- nothing to grade.")
        return
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()

    # 1. Re-grade anything v1 already graded, from stored scores.
    already = conn.execute("""SELECT p.id, p.game_id, p.model_prob_home_covers, p.spread_side_display,
                                     g.actual_home_score, g.actual_away_score, g.model_pick_correct AS old
                              FROM spread_picks p JOIN spread_grades g ON g.pick_id = p.id""").fetchall()
    n_changed = 0
    for r in already:
        ahc, correct = grade(r["model_prob_home_covers"], r["actual_home_score"], r["actual_away_score"])
        if correct != r["old"]:
            n_changed += 1
            print(f"  RE-GRADED {r['game_id']} {r['spread_side_display']}: "
                  f"{'COVERED' if r['old'] else 'MISSED'} (v1) -> {'COVERED' if correct else 'MISSED'} (v2)")
        conn.execute("UPDATE spread_grades SET actual_home_covers = ?, model_pick_correct = ? WHERE pick_id = ?",
                     (ahc, correct, r["id"]))
    conn.commit()
    print(f"Re-checked {len(already)} previously graded pick(s); {n_changed} corrected.")

    # 2. Grade new rows. Rows with no market line are still skipped, same as
    # v1, so this grader only ever covers picks that had a real market.
    ungraded = conn.execute(
        "SELECT * FROM spread_picks WHERE graded = 0 AND home_spread_point IS NOT NULL "
        "AND away_spread_point IS NOT NULL").fetchall()
    n_graded = n_not_final = 0
    for row in ungraded:
        if not row["statsapi_game_pk"]:
            print(f"  {row['game_id']}: no statsapi_game_pk stored -- skipping.")
            continue
        score, status = fetch_final_score(row["statsapi_game_pk"])
        if score is None:
            print(f"  {row['game_id']}: not final yet (status={status})")
            n_not_final += 1
            continue
        hs, as_ = score
        ahc, correct = grade(row["model_prob_home_covers"], hs, as_)
        conn.execute("""INSERT OR REPLACE INTO spread_grades
            (pick_id, graded_at_utc, actual_home_score, actual_away_score, actual_home_covers, model_pick_correct)
            VALUES (?,?,?,?,?,?)""", (row["id"], now, hs, as_, ahc, correct))
        conn.execute("UPDATE spread_picks SET graded = 1 WHERE id = ?", (row["id"],))
        conn.commit()
        n_graded += 1
        print(f"  GRADED {row['game_id']}: final {as_}-{hs} | model picked "
              f"{row['spread_side_display']} -> {'COVERED' if correct else 'MISSED'}")
    print(f"Graded {n_graded} new spread pick(s), {n_not_final} not final yet.")

    summary = conn.execute("""SELECT p.model_version, COUNT(*), SUM(g.model_pick_correct)
                              FROM spread_picks p JOIN spread_grades g ON g.pick_id = p.id
                              GROUP BY p.model_version""").fetchall()
    print("\n=== Running MLB spread tracker ledger (real graded games only, v2 grading) ===")
    for mv, n, c in summary:
        print(f"  {mv}: model_correct={c}/{n}")
    conn.close()


if __name__ == "__main__":
    main()
