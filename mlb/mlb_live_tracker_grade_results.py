#!/usr/bin/env python3
"""
mlb_spread_live_tracker_grade_results_v2.py

Grades ungraded rows in MLB_PROSPECTIVE_TRACKER_V1.sqlite3 against real
final scores, pulled directly from statsapi.mlb.com using each pick's
own stored statsapi_game_pk (set at lock time -- no re-matching by team
name/date needed, so the date-boundary bug class from the lock script
can't recur here).

This tracker logs MONITORING data, not bets (see
claude/mlb_live_tracker_v1_first_real_lock.md) -- there is no action/
side/profit to grade. Grading here means: was the real outcome recorded,
and did the model's favored side (home_favored_by_model, i.e.
edge_home > 0) match the real winner? The market's own favored side
(market_prob_home_devigged > 0.5) is graded the same way in the summary
report, so the two can be compared head to head on identical real games
-- that comparison, accumulated over real games, is the actual point of
this tracker.

Only ever INSERTS into the grades table and flips picks.graded to 1;
never touches an existing picks row's own fields, so the lock-time audit
trail (what was predicted, and when) stays intact forever.

Usage:
    python3 MLB_AI_MODEL/SUPPORT/mlb_live_tracker_grade_results.py
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
    req = urllib.request.Request(url, headers={"User-Agent": "mlb-tracker-grade/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_final_score(game_pk):
    """Returns (home_score, away_score) if the game is Final, else None.
    Never fabricates a score -- a non-Final game (postponed, suspended,
    in progress) is left ungraded until it genuinely is Final."""
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

    ungraded = conn.execute("SELECT * FROM picks WHERE graded = 0").fetchall()
    if not ungraded:
        print("No ungraded picks.")
        conn.close()
        return

    print(f"{len(ungraded)} ungraded pick(s). Checking real final scores via statsapi...")
    now = datetime.now(timezone.utc).isoformat()
    n_graded, n_not_final = 0, 0

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
        home_won = int(home_score > away_score)
        model_favored_home = int(row["home_favored_by_model"]) == 1
        model_favored_correct = int(model_favored_home == bool(home_won))

        conn.execute("""INSERT OR REPLACE INTO grades
            (pick_id, graded_at_utc, actual_home_score, actual_away_score, home_won, model_favored_correct)
            VALUES (?,?,?,?,?,?)""",
            (row["id"], now, home_score, away_score, home_won, model_favored_correct))
        conn.execute("UPDATE picks SET graded = 1 WHERE id = ?", (row["id"],))
        conn.commit()
        n_graded += 1
        print(f"  GRADED {row['game_id']}: final {away_score}-{home_score} "
              f"({'home' if home_won else 'away'} won) | model favored "
              f"{'home' if model_favored_home else 'away'} -> {'CORRECT' if model_favored_correct else 'WRONG'}")

    conn.close()
    print(f"\nGraded {n_graded} pick(s), {n_not_final} not final yet.")

    if n_graded:
        conn = sqlite3.connect(DB)
        summary = conn.execute("""
            SELECT p.model_version,
                   COUNT(*) AS n,
                   SUM(g.model_favored_correct) AS model_correct,
                   SUM(CASE WHEN (p.market_prob_home_devigged > 0.5) = (g.home_won = 1) THEN 1 ELSE 0 END) AS market_correct
            FROM picks p JOIN grades g ON g.pick_id = p.id
            WHERE p.graded = 1
            GROUP BY p.model_version
        """).fetchall()
        print("\n=== Running MLB tracker ledger (model favorite vs market favorite, real graded games only) ===")
        for r in summary:
            print(f"  {r[0]}: n={r[1]}, model_correct={r[2]}/{r[1]}, market_correct={r[3]}/{r[1]}")
        conn.close()


if __name__ == "__main__":
    main()
