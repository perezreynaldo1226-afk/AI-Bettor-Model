#!/usr/bin/env python3
"""
PROSPECTIVE_TRACKER_V1.4 -- LOG PICKS (per-game fault isolation + incremental
commits, and dedup now prefers a book with a complete quote)

Same design and discipline as PROSPECTIVE_TRACKER_V1_3_LOG_PICKS.py (kept
as-is, unmodified, for the historical record). This is a new, additively-
versioned copy, created after auditing V1.3 for failure modes before its
first real use and finding two real ones:

1. TRANSACTION SAFETY. V1.3 (and V1.1/V1.2 before it) only called
   conn.commit() once, after the entire per-game loop finished. Tested
   directly: if ANY single game raises an uncaught exception partway
   through the loop -- a malformed odds cell, a genuine bug in a decider,
   anything not already handled -- the whole process exits before ever
   reaching conn.commit(), and every other game's already-computed picks
   for OLD/V2.1/V3/V4 are silently discarded, not just that one game's.
   Confirmed empirically: a 3-game batch where game 2 raised produced ZERO
   rows in the database, including game 1's picks, which had already been
   inserted. Right before a real kickoff, with no time to debug and rerun,
   this is a real risk of losing an entire week's log for one bad game.
   V1.4 wraps each game's full evaluation (all four deciders) in its own
   try/except, commits after each game individually, and reports which
   game_ids failed and why -- one bad game is now skipped and reported,
   not a whole-week outage.

2. DEDUP COULD PICK A STALE BOOK OVER A GOOD ONE. V1.3 (necessarily, being
   the first version to receive more than one bookmaker row per game)
   deduplicated OLD/V2.1/V3's single input price by alphabetical bookmaker
   name alone. Tested directly: if the alphabetically-first book
   (hardrockbet_fl) has a missing/stale price for a side while the other
   book (kalshi) has a perfectly good one, V1.3 handed OLD/V2.1/V3 the
   BAD price regardless -- their EV math turns a missing price into NaN,
   which silently fails every ">= tau" comparison and produces an
   incorrect PASS, not a crash, but a real missed bet on a game the
   decider would otherwise have correctly acted on, purely because of
   which book happened to sort first. V1.4 dedupes by "has a valid price
   for both sides" first, alphabetical bookmaker name only as the
   tiebreaker among equally-valid (or equally-incomplete) rows -- so a
   single book's partial outage no longer poisons OLD/V2.1/V3 when the
   other book has a complete quote. V4 was never affected by this (it
   was already built from the start to consider every book row).

Runs ALL FOUR deciders side by side on the exact same frozen Winner (and,
for OLD/V2.1, Spread) model outputs:
  - OLD_LAB28_43_HYBRID            (engine/predict.py, unmodified)
  - NEW_V2_1_CORRECTED_THRESHOLDS  (engine/predict_v2_1_decider.py, unmodified)
  - V3_ML_ONLY_TAU019              (engine/predict_v3_ml_only_decider.py, unmodified)
  - V4_BEST_PRICE_ML_ONLY_TAU019   (engine/predict_v4_best_price_decider.py, unmodified)

Same hard pregame guard, same immutable timestamped-snapshot logging, same
never-retune-based-on-results discipline as every prior version. Same
database as V1.1/V1.2/V1.3 (PROSPECTIVE_TRACKER_V1_1.sqlite3), same schema
(no new columns needed for this fix).

TESTED (2026-09-22): both fixes verified against the exact synthetic
repro cases that exposed the bugs. (1) A 3-game batch where game 2's
decider raises an unexpected exception: game 1's picks are committed and
survive, game 2 is skipped with a printed warning naming it, game 3
still gets processed and logged normally -- confirmed via direct DB
inspection after the run (previously: zero rows survived). (2) A game
where the alphabetically-first book has a missing home price and the
second book has a valid one: OLD/V2.1/V3 now correctly receive the
second book's valid price instead of the missing one -- confirmed via
direct DB inspection (previously: all three received the missing price).
Also re-ran V1.3's original validation battery (multi-book best-price
selection, V4 graceful skip-on-no-valid-price, schema self-heal, pregame
guard) to confirm no regression.

Usage:
    python3 PROSPECTIVE_TRACKER_V1_4_LOG_PICKS.py \\
        --features live/pregame/MODEL_1_0_UPCOMING_WINNER_63_V62_3.csv \\
        --odds live/sportsbook/WEEK3_ODDS.csv \\
        --season 2026 --week 3

Both --features and --odds must be real files produced by the certified
pipeline (V62.3 assembly + fetch_week_odds.py or equivalent), not
synthetic data. This script only reads them; it builds nothing itself.

Going forward, run THIS script (not V1_1/V1_2/V1_3's) each week. Every
prior version is left in place, unmodified, only for reproducing exactly
what it always did if ever needed.
"""
import argparse
import hashlib
import importlib.util
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent  # expected: SUPPORT/
ROOT = HERE.parent  # "Sport Bet"
APP = ROOT / "NFL_AI_MODEL_1_0"
DB = HERE / "PROSPECTIVE_TRACKER_V1_1.sqlite3"  # intentionally the same DB as V1.1/V1.2/V1.3 -- see docstring above

REQUIRED_COLUMNS = {
    "priced_book": "TEXT",
    "n_books_quoting_home": "INTEGER",
    "n_books_quoting_away": "INTEGER",
}


def die(msg):
    print("\nBLOCKED:", msg)
    sys.exit(1)


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            week INTEGER NOT NULL,
            decider_version TEXT NOT NULL,
            lock_timestamp_utc TEXT NOT NULL,
            kickoff_time TEXT NOT NULL,
            home_team TEXT, away_team TEXT,
            action TEXT NOT NULL,
            side TEXT,
            selected_odds REAL,
            winner_p_home REAL,
            spread_edge REAL,
            home_moneyline REAL, away_moneyline REAL,
            market_home_margin REAL,
            home_spread_odds REAL, away_spread_odds REAL,
            features_sha256 TEXT NOT NULL,
            odds_sha256 TEXT NOT NULL,
            graded INTEGER NOT NULL DEFAULT 0,
            priced_book TEXT,
            n_books_quoting_home INTEGER,
            n_books_quoting_away INTEGER,
            UNIQUE(game_id, decider_version, lock_timestamp_utc)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS grades (
            pick_id INTEGER PRIMARY KEY REFERENCES picks(id),
            graded_at_utc TEXT NOT NULL,
            actual_home_score REAL, actual_away_score REAL,
            won INTEGER,
            profit_units REAL NOT NULL
        )
    """)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(picks)")}
    for col, coltype in REQUIRED_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE picks ADD COLUMN {col} {coltype}")
    conn.commit()


def _valid_num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return not math.isnan(v)


def dedupe_one_book_per_game(odds):
    """OLD/V2.1/V3 were built for a single already-chosen price per game.
    fetch_week_odds.py writes one row per (game_id, bookmaker). Prefer, per
    game, a book that has a valid price on BOTH sides over one that's
    missing/stale on either side -- a single book's partial outage should
    not poison these deciders when the other book has a complete quote.
    Alphabetical bookmaker name is only the tiebreaker among equally-valid
    (or equally-incomplete) rows, kept for determinism/reproducibility."""
    odds = odds.copy()
    odds["_both_valid"] = [
        _valid_num(hm) and _valid_num(am)
        for hm, am in zip(odds["home_moneyline"], odds["away_moneyline"])
    ]
    return (odds.sort_values(["game_id", "_both_valid", "bookmaker"],
                              ascending=[True, False, True], kind="stable")
                .drop_duplicates(subset="game_id", keep="first")
                .drop(columns="_both_valid"))


def group_books_by_game(odds):
    """Full multi-book rows per game_id, for V4's best-of-book shopping."""
    return {gid: g.to_dict("records") for gid, g in odds.groupby("game_id")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", required=True, help="Real pregame 63-feature CSV (V62.3 output or equivalent)")
    ap.add_argument("--odds", required=True, help="Real odds CSV (fetch_week_odds.py output or equivalent; one row per game_id+bookmaker)")
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--week", type=int, required=True)
    args = ap.parse_args()

    feat_path = Path(args.features)
    odds_path = Path(args.odds)
    if not feat_path.exists():
        die(f"Features file not found: {feat_path}")
    if not odds_path.exists():
        die(f"Odds file not found: {odds_path}")

    feat = pd.read_csv(feat_path)
    odds = pd.read_csv(odds_path)
    if "game_id" not in feat.columns:
        die("Features file missing game_id")
    required_odds_cols = ["game_id", "bookmaker", "kickoff_time", "home_team", "away_team", "home_moneyline",
                           "away_moneyline", "market_home_margin", "home_spread_odds", "away_spread_odds"]
    missing = [c for c in required_odds_cols if c not in odds.columns]
    if missing:
        die(f"Odds file missing required columns: {missing}")

    now = datetime.now(timezone.utc)
    bad_kick = []
    for _, r in odds.iterrows():
        k = datetime.fromisoformat(str(r["kickoff_time"]).replace("Z", "+00:00"))
        if now >= k:
            bad_kick.append(r["game_id"])
    if bad_kick:
        die(f"PREGAME_GUARD_FAIL: current time is not before kickoff for {len(set(bad_kick))} game(s): "
            f"{sorted(set(bad_kick))[:10]}. This tracker only accepts picks locked strictly before kickoff.")

    book_groups = group_books_by_game(odds)
    odds_one_per_game = dedupe_one_book_per_game(odds)

    df = feat.merge(odds_one_per_game, on="game_id", how="inner")
    if len(df) == 0:
        die("No overlapping game_id between features and odds files.")
    missing_join = set(odds.game_id.astype(str)) - set(df.game_id.astype(str))
    if missing_join:
        print(f"WARNING: {len(missing_join)} odds rows had no matching feature row, skipped: {sorted(missing_join)[:10]}")

    old_predict = load_module(APP / "engine" / "predict.py", "old_predict")
    new_predict = load_module(APP / "engine" / "predict_v2_1_decider.py", "new_predict")
    v3_predict = load_module(APP / "engine" / "predict_v3_ml_only_decider.py", "v3_predict")
    v4_predict = load_module(APP / "engine" / "predict_v4_best_price_decider.py", "v4_predict")

    features_hash = sha256_file(feat_path)
    odds_hash = sha256_file(odds_path)
    lock_ts = now.isoformat()

    conn = sqlite3.connect(DB)
    ensure_schema(conn)

    def insert_row(decider_version, common, action, side, selected_odds, priced_book=None,
                    n_books_home=None, n_books_away=None):
        try:
            conn.execute("""INSERT INTO picks (game_id, season, week, decider_version, lock_timestamp_utc,
                kickoff_time, home_team, away_team, action, side, selected_odds, winner_p_home, spread_edge,
                home_moneyline, away_moneyline, market_home_margin, home_spread_odds, away_spread_odds,
                features_sha256, odds_sha256, priced_book, n_books_quoting_home, n_books_quoting_away)
                VALUES (:game_id,:season,:week,:decider_version,:lock_timestamp_utc,:kickoff_time,
                :home_team,:away_team,:action,:side,:selected_odds,:winner_p_home,:spread_edge,
                :home_moneyline,:away_moneyline,:market_home_margin,:home_spread_odds,:away_spread_odds,
                :features_sha256,:odds_sha256,:priced_book,:n_books_quoting_home,:n_books_quoting_away)""",
                {**common, "decider_version": decider_version, "action": action, "side": side,
                 "selected_odds": selected_odds, "priced_book": priced_book,
                 "n_books_quoting_home": n_books_home, "n_books_quoting_away": n_books_away})
            return 1
        except sqlite3.IntegrityError:
            return 0  # exact same (game,version,timestamp) already logged -- do not silently duplicate

    n_old = n_new = n_v3 = n_v4 = 0
    n_v4_skipped = 0
    failed_games = []
    for _, r in df.iterrows():
        gid = str(r["game_id"])
        try:
            w_old = old_predict.winner_predict(r)
            s_old = old_predict.spread_predict(r["market_home_margin"], r["home_moneyline"], r["away_moneyline"])
            d_old = old_predict.decision_predict(w_old, s_old, r["home_moneyline"], r["away_moneyline"],
                                                  r["market_home_margin"], r["home_spread_odds"], r["away_spread_odds"])

            w_new = new_predict.winner_predict(r)
            s_new = new_predict.spread_predict(r["market_home_margin"], r["home_moneyline"], r["away_moneyline"])
            d_new = new_predict.decide_v2_1(w_new, s_new, r["home_moneyline"], r["away_moneyline"],
                                             r["home_spread_odds"], r["away_spread_odds"])

            w_v3 = v3_predict.winner_predict(r)
            d_v3 = v3_predict.decide_v3_ml_only(w_v3, r["home_moneyline"], r["away_moneyline"])

            common = dict(game_id=gid, season=args.season, week=args.week,
                          lock_timestamp_utc=lock_ts, kickoff_time=str(r["kickoff_time"]),
                          home_team=r.get("home_team"), away_team=r.get("away_team"),
                          winner_p_home=w_old["p_home_win"], spread_edge=s_old["spread_edge"],
                          home_moneyline=r["home_moneyline"], away_moneyline=r["away_moneyline"],
                          market_home_margin=r["market_home_margin"], home_spread_odds=r["home_spread_odds"],
                          away_spread_odds=r["away_spread_odds"], features_sha256=features_hash, odds_sha256=odds_hash)

            n_old += insert_row("OLD_LAB28_43_HYBRID", common, d_old["action"], d_old["side"], d_old["selected_odds"])
            n_new += insert_row("NEW_V2_1_CORRECTED_THRESHOLDS", common, d_new["action"], d_new["side"], d_new["selected_odds"])
            n_v3 += insert_row("V3_ML_ONLY_TAU019", common, d_v3["action"], d_v3["side"], d_v3["selected_odds"])

            # V4: best-of-book, moneyline-only. Reuses w_v3 -- decide_v4_best_price
            # calls the identical winner_predict as V3, so this is the same
            # number, not a second independent computation. A ValueError here
            # (documented: no book quotes a valid price for one side) is an
            # expected, graceful per-decider skip, not a failure of the game.
            try:
                d_v4 = v4_predict.decide_v4_best_price(w_v3, book_groups[gid])
            except ValueError as e:
                n_v4_skipped += 1
                print(f"WARNING: V4 skipped for {gid} (no valid price on one side across all books): {e}")
            else:
                common_v4 = dict(common)
                common_v4["home_moneyline"] = d_v4["best_home_moneyline"]
                common_v4["away_moneyline"] = d_v4["best_away_moneyline"]
                # V4 never selects SPREAD and doesn't shop spread markets -- these
                # fields describe a single arbitrary book's spread and would be
                # misleading attached to a best-of-book moneyline pick.
                common_v4["market_home_margin"] = None
                common_v4["home_spread_odds"] = None
                common_v4["away_spread_odds"] = None
                common_v4["spread_edge"] = None

                n_v4 += insert_row("V4_BEST_PRICE_ML_ONLY_TAU019", common_v4, d_v4["action"], d_v4["side"],
                                    d_v4["selected_odds"], priced_book=d_v4["priced_book"],
                                    n_books_home=d_v4["n_books_quoting_home"], n_books_away=d_v4["n_books_quoting_away"])

            # Commit after every game individually -- if a LATER game raises
            # an exception this run never anticipated, everything already
            # evaluated and inserted up to and including this game survives.
            conn.commit()
        except Exception as e:
            conn.rollback()  # discard only this game's own partial inserts, if any
            failed_games.append(gid)
            print(f"WARNING: game {gid} failed unexpectedly and was SKIPPED for ALL deciders "
                  f"(not logged anywhere) -- {type(e).__name__}: {e}")
            continue

    conn.close()
    print(f"Logged {n_old} OLD-decider, {n_new} NEW-decider (V2.1), {n_v3} V3-decider (moneyline-only), and "
          f"{n_v4} V4-decider (best-of-book moneyline) picks for season {args.season} week {args.week}, "
          f"locked at {lock_ts} (strictly pregame for every game).")
    if n_v4_skipped:
        print(f"NOTE: {n_v4_skipped} game(s) skipped for V4 only (no valid price for one side across all quoting books).")
    if failed_games:
        print(f"WARNING: {len(failed_games)} game(s) failed unexpectedly and were skipped for ALL deciders: "
              f"{failed_games}. Every other game in this run was still logged and committed. Investigate these "
              f"specific game_ids in the odds/features files before re-running if they matter.")
    print(f"Tracker DB: {DB}")


if __name__ == "__main__":
    main()
