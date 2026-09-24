#!/usr/bin/env python3
"""
PROSPECTIVE_TRACKER_V1.1 -- GRADE RESULTS

Identical logic to PROSPECTIVE_TRACKER_V1_GRADE_RESULTS.py (kept as-is,
unmodified, for the record), pointed at the V1.1 database
(PROSPECTIVE_TRACKER_V1_1.sqlite3) that PROSPECTIVE_TRACKER_V1_1_LOG_PICKS.py
writes to, which compares OLD_LAB28_43_HYBRID against
NEW_V2_1_CORRECTED_THRESHOLDS instead of the earlier NEW_V2_WALKFORWARD_GATED.

Run this any time after a week's games have finished (e.g. Monday/Tuesday)
to grade every ungraded row against real final scores. Pulls scores fresh
from nflverse's public games.csv release -- does not touch odds (already
stored per-pick at lock time) and does not touch any frozen model. Only
ever INSERTS into the grades table; the original picks rows (and their
lock_timestamp_utc) are never modified, so the audit trail of "what was
picked, and when" stays intact forever.

For each ungraded pick, only the LATEST lock_timestamp_utc per
(game_id, decider_version) is graded as "the" pick for that game -- earlier
same-week snapshots (e.g. from an odds refresh) are kept in the table for
the record but are not double-counted in the ROI totals.

Usage:
    python3 PROSPECTIVE_TRACKER_V1_1_GRADE_RESULTS.py [--season 2026] [--week 3]
    (omit --week to grade every ungraded pick for the season so far)
"""
import argparse
import io
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DB = HERE / "PROSPECTIVE_TRACKER_V1_1.sqlite3"
GAMES_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"


def american_profit(o):
    o = float(o)
    return o / 100 if o > 0 else 100 / abs(o)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--week", type=int, default=None)
    args = ap.parse_args()

    if not DB.exists():
        print("No V1.1 tracker DB found yet -- nothing to grade. Run PROSPECTIVE_TRACKER_V1_1_LOG_PICKS.py first, "
              "during a real week.")
        return

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    q = "SELECT * FROM picks WHERE graded = 0"
    params = []
    if args.season is not None:
        q += " AND season = ?"
        params.append(args.season)
    if args.week is not None:
        q += " AND week = ?"
        params.append(args.week)
    ungraded = pd.DataFrame([dict(r) for r in conn.execute(q, params).fetchall()])
    if len(ungraded) == 0:
        print("No ungraded picks match that filter.")
        conn.close()
        return

    # Keep only the latest lock_timestamp_utc per (game_id, decider_version)
    ungraded["lock_dt"] = pd.to_datetime(ungraded.lock_timestamp_utc)
    latest_idx = ungraded.groupby(["game_id", "decider_version"])["lock_dt"].idxmax()
    to_grade = ungraded.loc[latest_idx].copy()
    stale = ungraded.loc[~ungraded.index.isin(latest_idx)]
    if len(stale):
        conn.executemany("UPDATE picks SET graded = -1 WHERE id = ?", [(int(i),) for i in stale["id"]])
        conn.commit()
        print(f"Marked {len(stale)} superseded same-week snapshot(s) as graded=-1 (not counted; a later pregame "
              f"snapshot for the same game exists and is the one being graded).")

    print(f"Fetching real final scores from nflverse ({GAMES_URL}) ...")
    with urllib.request.urlopen(GAMES_URL, timeout=60) as resp:
        raw = resp.read()
    games = pd.read_csv(io.BytesIO(raw), low_memory=False)
    games = games[["game_id", "home_score", "away_score"]].dropna()

    to_grade = to_grade.merge(games, on="game_id", how="left")
    not_final = to_grade[to_grade.home_score.isna()]
    if len(not_final):
        print(f"{len(not_final)} game(s) not final yet, skipping for now: {sorted(not_final.game_id.unique())[:10]}")
    ready = to_grade.dropna(subset=["home_score", "away_score"]).copy()
    if len(ready) == 0:
        print("Nothing final yet to grade.")
        conn.close()
        return

    now = datetime.now(timezone.utc).isoformat()
    graded_rows = 0
    for _, r in ready.iterrows():
        actual_home_margin = float(r["home_score"]) - float(r["away_score"])
        home_won = actual_home_margin > 0
        action = r["action"]
        if action == "PASS":
            won, profit = None, 0.0
        elif action == "ML":
            won = (r["side"] == "HOME") == home_won
            profit = american_profit(r["selected_odds"]) if won else -1.0
        else:  # SPREAD
            cover = actual_home_margin - float(r["market_home_margin"])
            if r["side"] == "HOME":
                won = cover > 0
            else:
                won = cover < 0
            push = cover == 0
            profit = 0.0 if push else (american_profit(r["selected_odds"]) if won else -1.0)

        conn.execute("""INSERT OR REPLACE INTO grades (pick_id, graded_at_utc, actual_home_score,
            actual_away_score, won, profit_units) VALUES (?,?,?,?,?,?)""",
            (int(r["id"]), now, float(r["home_score"]), float(r["away_score"]),
             None if won is None else int(won), profit))
        conn.execute("UPDATE picks SET graded = 1 WHERE id = ?", (int(r["id"]),))
        graded_rows += 1

    conn.commit()

    print(f"\nGraded {graded_rows} pick(s).")
    summary = pd.read_sql_query("""
        SELECT p.decider_version, p.season,
               COUNT(*) AS games,
               SUM(CASE WHEN p.action != 'PASS' THEN 1 ELSE 0 END) AS acted_bets,
               SUM(CASE WHEN p.action != 'PASS' AND g.won = 1 THEN 1 ELSE 0 END) AS wins,
               ROUND(SUM(g.profit_units), 3) AS total_profit_units
        FROM picks p JOIN grades g ON g.pick_id = p.id
        WHERE p.graded = 1
        GROUP BY p.decider_version, p.season
        ORDER BY p.season, p.decider_version
    """, conn)
    print("\n=== Running prospective ledger (OLD decider vs NEW V2.1 decider, real forward results only) ===")
    print(summary.to_string(index=False))
    conn.close()


if __name__ == "__main__":
    main()
