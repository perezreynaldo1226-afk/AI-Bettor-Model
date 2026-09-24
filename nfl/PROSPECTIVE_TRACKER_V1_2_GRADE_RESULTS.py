#!/usr/bin/env python3
"""
PROSPECTIVE_TRACKER_V1.2 -- GRADE RESULTS (spread grading in the sportsbook
point convention)

Additively-versioned copy of PROSPECTIVE_TRACKER_V1_1_GRADE_RESULTS.py (kept
as-is, unmodified, for the record). Same database, same "grade only the
latest lock per (game_id, decider_version)" rule, same nflverse score source.

THE FIX. Every row the live tracker has ever written stores
market_home_margin as the HOME SPREAD POINT the sportsbook quotes
(negative = home favored; e.g. -5.5 means home -5.5). V1.1 graded SPREAD
picks with  cover = actual_home_margin - market_home_margin, which is only
right for the opposite (expected-margin) convention. For a point, the home
side covers iff

    actual_home_margin + market_home_margin > 0      (== 0 is a push)

and the away side covers iff that quantity is < 0. Moneyline and PASS
grading are unchanged.

RE-GRADE PASS. Because V1.1 may already have written grades with the wrong
formula, every run also re-checks every already-graded SPREAD pick from its
own stored final scores (no new data needed) and rewrites the grade row
only if the result differs, printing each correction. Picks rows are never
modified except the graded flag, exactly as before. The grades table gets
one new, additive column, grader_version, set on every row this script
writes.

Usage:
    python3 PROSPECTIVE_TRACKER_V1_2_GRADE_RESULTS.py [--season 2026] [--week 3]
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


GRADER_VERSION = "V1_2_POINT_CONVENTION"


def grade_pick(action, side, selected_odds, point, home_score, away_score):
    """Returns (won, profit_units). won is None for PASS and for a push."""
    actual_home_margin = float(home_score) - float(away_score)
    if action == "PASS":
        return None, 0.0
    if action == "ML":
        won = (side == "HOME") == (actual_home_margin > 0)
        return int(won), (american_profit(selected_odds) if won else -1.0)
    # SPREAD: market_home_margin is the home spread point (negative = home favored)
    cover = actual_home_margin + float(point)
    if cover == 0:
        return None, 0.0
    won = (cover > 0) if side == "HOME" else (cover < 0)
    return int(won), (american_profit(selected_odds) if won else -1.0)


def ensure_grades_column(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(grades)")}
    if "grader_version" not in cols:
        conn.execute("ALTER TABLE grades ADD COLUMN grader_version TEXT")
        conn.commit()


def regrade_existing_spreads(conn):
    rows = conn.execute("""SELECT p.id, p.game_id, p.decider_version, p.side, p.selected_odds,
               p.market_home_margin, g.actual_home_score, g.actual_away_score, g.won, g.profit_units
        FROM picks p JOIN grades g ON g.pick_id = p.id
        WHERE p.graded = 1 AND p.action = 'SPREAD'""").fetchall()
    now = datetime.now(timezone.utc).isoformat()
    fixed = 0
    for r in rows:
        won, profit = grade_pick("SPREAD", r["side"], r["selected_odds"], r["market_home_margin"],
                                 r["actual_home_score"], r["actual_away_score"])
        if won != r["won"] or abs(profit - float(r["profit_units"])) > 1e-9:
            conn.execute("""INSERT OR REPLACE INTO grades (pick_id, graded_at_utc, actual_home_score,
                actual_away_score, won, profit_units, grader_version) VALUES (?,?,?,?,?,?,?)""",
                (int(r["id"]), now, r["actual_home_score"], r["actual_away_score"], won, profit, GRADER_VERSION))
            fixed += 1
            print(f"RE-GRADED {r['game_id']} {r['decider_version']} SPREAD {r['side']} "
                  f"(home point {r['market_home_margin']}, final {r['actual_away_score']:.0f}-"
                  f"{r['actual_home_score']:.0f} away-home): won {r['won']} -> {won}, "
                  f"profit {r['profit_units']} -> {round(profit, 4)}")
    conn.commit()
    print(f"Re-check of already-graded SPREAD picks: {len(rows)} checked, {fixed} corrected.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--week", type=int, default=None)
    args = ap.parse_args()

    if not DB.exists():
        print("No tracker DB found yet -- nothing to grade.")
        return

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    ensure_grades_column(conn)
    regrade_existing_spreads(conn)

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
        won, profit = grade_pick(r["action"], r["side"], r["selected_odds"], r["market_home_margin"],
                                 r["home_score"], r["away_score"])
        conn.execute("""INSERT OR REPLACE INTO grades (pick_id, graded_at_utc, actual_home_score,
            actual_away_score, won, profit_units, grader_version) VALUES (?,?,?,?,?,?,?)""",
            (int(r["id"]), now, float(r["home_score"]), float(r["away_score"]), won, profit, GRADER_VERSION))
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
