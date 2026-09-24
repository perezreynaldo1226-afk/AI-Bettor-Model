#!/usr/bin/env python3
"""
mlb_spread_live_tracker_grade_results.py

Counterpart to mlb_live_tracker_grade_results.py, for the spread_picks /
spread_grades tables that mlb_spread_live_lock_v1.py populates. Same
discipline, same data source, same never-fabricate-a-score rule:

Grades ungraded rows in MLB_PROSPECTIVE_TRACKER_V1.sqlite3's spread_picks
table against real final scores, pulled directly from statsapi.mlb.com
using each pick's own stored statsapi_game_pk (set at lock time). A
run-line pick's own side (spread_side_team) is judged against that
team's own stored spread point (home_spread_point if it's the home team,
away_spread_point if away) -- both are always +/-1.5 in this project's
data so no push case is expected, but a push (margin exactly cancels
the point) is graded as model_pick_correct = 0 rather than crashing or
being silently miscounted as a win, and is printed distinctly so it is
never confused with a real loss.

Only ever INSERTS into spread_grades and flips spread_picks.graded to 1;
never touches an existing spread_picks row's own fields, matching the
moneyline grader's audit-trail-preservation rule exactly.

Usage:
    python3 MLB_AI_MODEL/SUPPORT/mlb_spread_live_tracker_grade_results.py
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


def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "mlb-spread-tracker-grade/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_final_score(game_pk):
    """Returns ((home_score, away_score), status) if the game is Final,
    else (None, status). Never fabricates a score -- a non-Final game
    (postponed, suspended, in progress) is left ungraded until it
    genuinely is Final."""
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


def main():
    if not DB.exists():
        print("No MLB tracker DB found yet -- nothing to grade.")
        return

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # Only rows with a real synced spread line can be graded -- a row with
    # no home_spread_point/away_spread_point (preview-only, market not
    # synced yet) has nothing to grade against and is skipped, not graded
    # as a loss.
    ungraded = conn.execute(
        "SELECT * FROM spread_picks WHERE graded = 0 AND home_spread_point IS NOT NULL "
        "AND away_spread_point IS NOT NULL"
    ).fetchall()
    n_no_line = conn.execute(
        "SELECT COUNT(*) FROM spread_picks WHERE graded = 0 AND "
        "(home_spread_point IS NULL OR away_spread_point IS NULL)"
    ).fetchone()[0]

    if not ungraded:
        print(f"No ungraded spread picks with a synced line. ({n_no_line} ungraded row(s) have no line yet -- skipped, not graded.)")
        conn.close()
        return

    print(f"{len(ungraded)} ungraded spread pick(s) with a real line. "
          f"({n_no_line} more have no synced line yet -- skipped.) Checking real final scores via statsapi...")
    now = datetime.now(timezone.utc).isoformat()
    n_graded, n_not_final, n_push = 0, 0, 0

    for row in ungraded:
        game_pk = row["statsapi_game_pk"]
        if not game_pk:
            print(f"  {row['game_id']}: no statsapi_game_pk stored -- cannot grade, skipping.")
            continue
        score, status = fetch_final_score(game_pk)
        if score is None:
            print(f"  {row['game_id']}: not final yet (status={status})")
            n_not_final += 1
            continue
        home_score, away_score = score
        home_margin = home_score - away_score  # home team's real margin, signed

        pick_is_home = row["spread_side_team"] == row["home_team"]
        pick_point = row["home_spread_point"] if pick_is_home else row["away_spread_point"]
        # The picked side's own signed margin plus its own point, relative to zero:
        # home side covers iff home_margin + home_spread_point > 0 (away side
        # is exactly the mirror -- away_margin = -home_margin, and
        # away_spread_point = -home_spread_point in this project's data).
        pick_margin = home_margin if pick_is_home else -home_margin
        cover_value = pick_margin + pick_point

        if cover_value > 0:
            pick_covered = 1
        elif cover_value == 0:
            pick_covered = None  # push -- graded as not-correct below, flagged distinctly
            n_push += 1
        else:
            pick_covered = 0

        actual_home_covers = int((home_margin + row["home_spread_point"]) > 0)
        model_pick_correct = int(pick_covered == 1) if pick_covered is not None else 0

        conn.execute("""INSERT OR REPLACE INTO spread_grades
            (pick_id, graded_at_utc, actual_home_score, actual_away_score, actual_home_covers, model_pick_correct)
            VALUES (?,?,?,?,?,?)""",
            (row["id"], now, home_score, away_score, actual_home_covers, model_pick_correct))
        conn.execute("UPDATE spread_picks SET graded = 1 WHERE id = ?", (row["id"],))
        conn.commit()
        n_graded += 1
        push_note = " (PUSH -- graded as not-correct, not a loss)" if pick_covered is None else ""
        print(f"  GRADED {row['game_id']}: final {away_score}-{home_score} | model picked "
              f"{row['spread_side_display']} -> "
              f"{'COVERED' if pick_covered == 1 else ('DID NOT COVER' if pick_covered == 0 else 'PUSH')}{push_note}")

    conn.close()
    print(f"\nGraded {n_graded} spread pick(s), {n_not_final} not final yet, {n_push} push(es) among graded.")

    if n_graded:
        conn = sqlite3.connect(DB)
        summary = conn.execute("""
            SELECT p.model_version,
                   COUNT(*) AS n,
                   SUM(g.model_pick_correct) AS model_correct
            FROM spread_picks p JOIN spread_grades g ON g.pick_id = p.id
            WHERE p.graded = 1
            GROUP BY p.model_version
        """).fetchall()
        print("\n=== Running MLB spread tracker ledger (real graded games only) ===")
        for r in summary:
            print(f"  {r[0]}: n={r[1]}, model_correct={r[2]}/{r[1]}")
        conn.close()


if __name__ == "__main__":
    main()
