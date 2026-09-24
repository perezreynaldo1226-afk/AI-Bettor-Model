#!/usr/bin/env python3
"""
mlb_spread_live_lock_v1.py -- the live-inference counterpart to
mlb_live_tracker_lock_v3.py, but for Model C_spread instead of Model C
(moneyline). New, additively-named file per this project's versioning
discipline -- mlb_live_tracker_lock_v3.py is untouched, and this script
writes to its OWN new sqlite table (`spread_picks`, in the same
MLB_PROSPECTIVE_TRACKER_V1.sqlite3 file) rather than touching the existing
`picks` table, so nothing about the moneyline tracker's schema or data can
be affected by this script no matter what it does.

WHY THIS EXISTS: MLB_SPREAD_SELECTIVE_COVERAGE_V1 (claude/mlb_spread_selective_coverage_v1_confidence_gated_real.md)
found real, Bonferroni-corrected confidence-tiering in Model C_spread's own
predictions -- the same statistical bar the moneyline live tracker's model
already passed. This script is what makes that finding a real, live pick
instead of only a backtest number: it fits Model C_spread the same way
mlb_live_tracker_lock_v3.py fits Model C (walk-forward, refit fresh at lock
time on all available history, so no future data ever leaks into a live
pick), then predicts P(home_covers) against a fixed home -1.5 run line for
today's real games, using the exact same real, live statsapi.mlb.com
trailing-stat features (team 15-game trailing runs scored/allowed, starting
pitcher 5-start trailing ERA/K-BB, rest days) that mlb_live_tracker_lock_v3.py
already validated and uses for moneyline. The feature-pulling functions
below (team_trailing_live, pitcher_trailing_live, find_team_id, resolve_code,
fetch_probables_for_dates, get_json, implied_prob, load_odds) are duplicated
VERBATIM from mlb_live_tracker_lock_v3.py, not reimplemented from scratch --
this is deliberate, not an oversight: reusing exactly-tested code here avoids
introducing a new, unvalidated feature-computation path for a decision that
is about to be shown to a real user.

WHAT'S DIFFERENT FROM THE MONEYLINE TRACKER, on purpose:
  1. load_training_data_spread() keeps each clean game's real home/away final
     score (mlb_live_tracker_lock_v3.py's load_training_data() drops these
     after computing trailing stats, since moneyline only needs home_win).
     home_covers = 1 if (home_runs - away_runs) >= 2 else 0 -- identical
     definition to mlb_spread_v1_full_run.py's home_covers target, which is
     what MLB_SPREAD_SELECTIVE_COVERAGE_V1 tested and validated.
  2. fit_model_c_spread() trains on home_covers instead of home_win, but uses
     the exact same feat_C feature list and the exact same pipeline
     (median-impute -> standard-scale -> LogisticRegression(C=0.2)) as both
     mlb_live_tracker_lock_v3.py's fit_model_c() and mlb_spread_v1_full_run.py
     -- this is the same model family already tested, not a new architecture.
  3. Writes to a NEW `spread_picks` table, not `picks` -- see above.
  4. This script does NOT require a spread/run-line market odds column to
     run -- Model C_spread's own confidence tiering (validated by
     MLB_SPREAD_SELECTIVE_COVERAGE_V1) is a claim about the model's own
     held-out accuracy, not a market-edge claim, so it needs no market price
     to produce a pick. If the odds CSV passed via --odds was fetched with
     fetch_mlb_odds_v3.py's --markets h2h,spreads flag (so it carries
     home_spread_point/price and away_spread_point/price columns), those are
     logged for transparency/context only -- NEVER used to compute the
     prediction or gate whether a pick is logged. If a plain h2h-only CSV is
     passed (the default), those columns are simply left NULL. Either way,
     the --odds CSV is still required, purely as the JOIN key identifying
     which real games to lock picks for and their real kickoff times --
     exactly the same role it plays in mlb_live_tracker_lock_v3.py.

STANDING PROJECT RULES followed here, same as every other live script this
session: never place a real bet or auto-attest anything (this table has no
attestation fields at all, same as `picks`); never read the .env file
directly (ODDS_API_KEY is only read via os.getenv, same pattern as v3);
pregame guard enforced (lock_timestamp_utc must be strictly before
kickoff_time); real statsapi/network calls -- run this from your own real
Mac Terminal, never from a sandboxed tool that can't reach statsapi.mlb.com.

Usage (identical odds-CSV contract to mlb_live_tracker_lock_v3.py):
    python3 MLB_AI_MODEL/SUPPORT/mlb_spread_live_lock_v1.py \\
        --odds MLB_AI_MODEL/live/sportsbook/MLB_ODDS_LIVE.csv
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import sqlite3
import ssl
import sys
import urllib.parse
import urllib.request
import warnings
from collections import defaultdict
from pathlib import Path

warnings.filterwarnings("ignore")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
DATA_DIR = PROJECT_ROOT / "MLB_AI_MODEL" / "data" / "retrosheet_basic"
TEAMSTATS_PATH = DATA_DIR / "teamstats.csv"
PITCHING_PATH = DATA_DIR / "pitching.csv"
DB_PATH = SCRIPT_DIR / "MLB_PROSPECTIVE_TRACKER_V1.sqlite3"

TEAM_TRAIL_WINDOW = 15
TEAM_TRAIL_MIN = 10
SP_TRAIL_WINDOW = 5
SP_TRAIL_MIN = 3
REST_DAYS_CAP = 10
RUN_LINE = 1.5  # standard MLB run line; home_covers iff margin >= 2 -- same as mlb_spread_v1_full_run.py
MODEL_VERSION = "MLB_MODEL_C_SPREAD_V1_10FEAT_LIVE_V1_REGSEASON_ONLY"
KICKOFF_MATCH_TOLERANCE_SECONDS = 180  # matching an odds event to a statsapi game -- same tolerance as v3

TEAM_NAME_TO_CODE = {
    "Los Angeles Angels": "ANA", "Arizona Diamondbacks": "ARI", "Athletics": "ATH",
    "Oakland Athletics": "ATH", "Atlanta Braves": "ATL", "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS", "Chicago White Sox": "CHA", "Chicago Cubs": "CHN",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE", "Colorado Rockies": "COL",
    "Detroit Tigers": "DET", "Houston Astros": "HOU", "Kansas City Royals": "KCA",
    "Los Angeles Dodgers": "LAN", "Miami Marlins": "MIA", "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN", "New York Yankees": "NYA", "New York Mets": "NYN",
    "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT", "San Diego Padres": "SDN",
    "Seattle Mariners": "SEA", "San Francisco Giants": "SFN", "St Louis Cardinals": "SLN",
    "St. Louis Cardinals": "SLN", "Tampa Bay Rays": "TBA", "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR", "Washington Nationals": "WAS",
}

# Retrosheet's internal codes -> the standard display codes real sportsbooks
# use -- same mapping already used when syncing MLB picks into Lineboard
# (build_mlb_slate_writes.py). Written here too so this script's own printed
# game_id / display codes are consistent with what eventually reaches the app.
RETRO_TO_DISPLAY = {
    "SFN": "SF", "SLN": "STL", "NYA": "NYY", "NYN": "NYM", "CHN": "CHC", "CHA": "CHW",
    "LAN": "LAD", "SDN": "SD", "TBA": "TB", "KCA": "KC", "ANA": "LAA",
}


def get_json(url, timeout=20):
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "mlb-spread-live-lock/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------------
# Training data -- SAME regular-season-only, gametype-filtered, clean-game,
# eligibility-gated construction as mlb_live_tracker_lock_v3.py's
# load_training_data(), except home/away final scores are kept so
# home_covers can be computed, instead of being dropped after trailing
# stats are built.
# ---------------------------------------------------------------------

def load_training_data_spread():
    import numpy as np
    import pandas as pd

    print("Loading Retrosheet 2010-2025 team/pitcher data for training (this can take a minute)...")
    cols = ["gid", "team", "b_r", "p_r", "win", "loss", "tie", "date", "number", "vishome", "opp", "gametype"]
    ts = pd.read_csv(TEAMSTATS_PATH, usecols=cols, low_memory=False)
    ts["date"] = pd.to_datetime(ts["date"], format="%Y%m%d", errors="coerce")
    for c in ("b_r", "p_r", "win", "loss", "tie", "number"):
        ts[c] = pd.to_numeric(ts[c], errors="coerce")
    ts = ts.dropna(subset=["date", "b_r", "p_r", "win", "loss", "tie", "number", "vishome"]).copy()
    ts = ts[ts["date"] >= pd.Timestamp("2010-01-01")].copy()
    n_before_gametype = len(ts)
    ts = ts[ts["gametype"] == "regular"].copy()
    print(f"  gametype filter (regular season only): {n_before_gametype} rows -> {len(ts)} rows "
          f"({n_before_gametype - len(ts)} postseason/all-star/other rows excluded).")
    ts["number"] = ts["number"].astype(int)

    pcols = ["gid", "id", "team", "p_gs", "p_er", "p_ipouts", "p_k", "p_w", "p_bfp", "date", "number", "gametype"]
    sp = pd.read_csv(PITCHING_PATH, usecols=pcols, low_memory=False)
    sp = sp[sp["p_gs"] == 1].copy()
    sp["date"] = pd.to_datetime(sp["date"], format="%Y%m%d", errors="coerce")
    for c in ("p_er", "p_ipouts", "p_k", "p_w", "p_bfp", "number"):
        sp[c] = pd.to_numeric(sp[c], errors="coerce")
    sp = sp.dropna(subset=["date", "p_er", "p_ipouts", "p_k", "p_w", "p_bfp", "number"]).copy()
    sp = sp[sp["date"] >= pd.Timestamp("2010-01-01")].copy()
    sp = sp[sp["gametype"] == "regular"].copy()
    sp["number"] = sp["number"].astype(int)
    dup_mask = sp.duplicated(subset=["gid", "team"], keep=False)
    sp = sp[~dup_mask].copy()

    ts_sorted = ts.sort_values(["gid", "vishome"]).copy()
    pairs = ts_sorted.groupby("gid").size()
    good_gids = pairs[pairs == 2].index
    ts_sorted = ts_sorted[ts_sorted["gid"].isin(good_gids)].copy()
    piv = ts_sorted.set_index(["gid", "vishome"])
    home = piv.xs("h", level="vishome")
    away = piv.xs("v", level="vishome")
    common = home.index.intersection(away.index)
    home = home.loc[common]
    away = away.loc[common]
    tie_mask = (home["tie"] == 1) | (away["tie"] == 1)
    complementary = (
        ((home["win"] == 1) & (home["loss"] == 0) & (away["win"] == 0) & (away["loss"] == 1)) |
        ((home["win"] == 0) & (home["loss"] == 1) & (away["win"] == 1) & (away["loss"] == 0))
    )
    clean_mask = (~tie_mask) & complementary

    # DIFFERENCE FROM v3's load_training_data(): keep b_r (runs scored) for
    # both sides so home_margin / home_covers can be computed below. v3 drops
    # these immediately since moneyline only needs home_win.
    games = pd.DataFrame({
        "gid": common, "date": home["date"].values, "number": home["number"].values,
        "home_team": home["team"].values, "away_team": away["team"].values,
        "home_win": (home["win"] == 1).astype(int).values, "clean": clean_mask.values,
        "home_runs": home["b_r"].values, "away_runs": away["b_r"].values,
    })
    games["home_margin"] = games["home_runs"] - games["away_runs"]
    games["home_covers"] = (games["home_margin"] >= (RUN_LINE + 0.5)).astype(int)  # margin >= 2, same as mlb_spread_v1_full_run.py

    long_rows = []
    for side, df_side in (("home", home), ("away", away)):
        d = df_side.loc[clean_mask].reset_index()
        long_rows.append(pd.DataFrame({
            "team": d["team"].values, "date": d["date"].values, "number": d["number"].values,
            "gid": d["gid"].values, "rs": d["b_r"].values, "ra": d["p_r"].values,
        }))
    team_long = pd.concat(long_rows, ignore_index=True)
    team_long = team_long.sort_values(["team", "date", "number"]).reset_index(drop=True)
    g = team_long.groupby("team", sort=False)
    prior_count = g.cumcount()
    trail_rs = g["rs"].apply(lambda s: s.shift(1).rolling(TEAM_TRAIL_WINDOW, min_periods=1).mean())
    trail_ra = g["ra"].apply(lambda s: s.shift(1).rolling(TEAM_TRAIL_WINDOW, min_periods=1).mean())
    team_long["prior_count"] = prior_count.reset_index(drop=True)
    team_long["trail_rs"] = trail_rs.reset_index(drop=True)
    team_long["trail_ra"] = trail_ra.reset_index(drop=True)
    prev_date = g["date"].apply(lambda s: s.shift(1))
    team_long["rest_days"] = (team_long["date"] - prev_date.reset_index(drop=True)).dt.days
    team_long["rest_days"] = team_long["rest_days"].clip(upper=REST_DAYS_CAP)
    team_long["eligible"] = team_long["prior_count"] >= TEAM_TRAIL_MIN

    sp_sorted = sp.sort_values(["id", "date", "number"]).reset_index(drop=True)
    gp = sp_sorted.groupby("id", sort=False)
    sp_prior = gp.cumcount()
    er_sum = gp["p_er"].apply(lambda s: s.shift(1).rolling(SP_TRAIL_WINDOW, min_periods=1).sum())
    ipouts_sum = gp["p_ipouts"].apply(lambda s: s.shift(1).rolling(SP_TRAIL_WINDOW, min_periods=1).sum())
    k_sum = gp["p_k"].apply(lambda s: s.shift(1).rolling(SP_TRAIL_WINDOW, min_periods=1).sum())
    w_sum = gp["p_w"].apply(lambda s: s.shift(1).rolling(SP_TRAIL_WINDOW, min_periods=1).sum())
    bfp_sum = gp["p_bfp"].apply(lambda s: s.shift(1).rolling(SP_TRAIL_WINDOW, min_periods=1).sum())
    sp_sorted["prior_starts"] = sp_prior.reset_index(drop=True)
    ip_thirds = ipouts_sum.reset_index(drop=True) / 3.0
    sp_sorted["trail_era"] = 9.0 * er_sum.reset_index(drop=True) / ip_thirds.replace(0, np.nan)
    sp_sorted["trail_kbb"] = (k_sum.reset_index(drop=True) - w_sum.reset_index(drop=True)) / bfp_sum.reset_index(drop=True).replace(0, np.nan)
    sp_sorted["sp_eligible"] = sp_sorted["prior_starts"] >= SP_TRAIL_MIN
    sp_feat = sp_sorted[["gid", "team", "trail_era", "trail_kbb", "sp_eligible"]]

    tl = team_long[["team", "gid", "trail_rs", "trail_ra", "rest_days", "eligible"]]
    df = games.merge(tl.rename(columns={"team": "home_team", "trail_rs": "home_trail15_rs",
                      "trail_ra": "home_trail15_ra", "rest_days": "home_rest_days", "eligible": "home_team_eligible"}),
                      on=["gid", "home_team"], how="left")
    df = df.merge(tl.rename(columns={"team": "away_team", "trail_rs": "away_trail15_rs",
                      "trail_ra": "away_trail15_ra", "rest_days": "away_rest_days", "eligible": "away_team_eligible"}),
                      on=["gid", "away_team"], how="left")
    df = df.merge(sp_feat.rename(columns={"team": "home_team", "trail_era": "home_sp_trail5_era",
                      "trail_kbb": "home_sp_trail5_kbb", "sp_eligible": "home_sp_eligible"}),
                      on=["gid", "home_team"], how="left")
    df = df.merge(sp_feat.rename(columns={"team": "away_team", "trail_era": "away_sp_trail5_era",
                      "trail_kbb": "away_sp_trail5_kbb", "sp_eligible": "away_sp_eligible"}),
                      on=["gid", "away_team"], how="left")

    ok = (df["clean"] & df["home_team_eligible"].fillna(False) & df["away_team_eligible"].fillna(False)
          & df["home_sp_eligible"].fillna(False) & df["away_sp_eligible"].fillna(False))
    df_ok = df[ok].copy()
    print(f"  Training set: {len(df)} clean regular-season games -> {len(df_ok)} after eligibility exclusion.")
    print(f"  home_covers base rate (training set): {df_ok['home_covers'].mean():.4f}")
    return df_ok


def fit_model_c_spread(df_ok):
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    feat_C = ["home_trail15_rs", "home_trail15_ra", "away_trail15_rs", "away_trail15_ra",
              "home_sp_trail5_era", "home_sp_trail5_kbb", "away_sp_trail5_era", "away_sp_trail5_kbb",
              "home_rest_days", "away_rest_days"]
    pipe = Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
        ("lr", LogisticRegression(C=0.2, max_iter=2000)),
    ])
    pipe.fit(df_ok[feat_C], df_ok["home_covers"].values)
    print(f"  Model C_spread fit on {len(df_ok)} regular-season games (2010-2025), {len(feat_C)} features, target=home_covers.")
    return pipe, feat_C


# ---------------------------------------------------------------------
# Live statsapi feature pull -- VERBATIM from mlb_live_tracker_lock_v3.py,
# duplicated rather than imported (this project's convention for additive
# scripts -- see fetch_mlb_odds_v3.py). Includes the same v3 gameType=R fix
# on pitcher_trailing_live() and the same regular-season-only design lock.
# ---------------------------------------------------------------------

def resolve_code(full_name):
    if full_name in TEAM_NAME_TO_CODE:
        return TEAM_NAME_TO_CODE[full_name]
    nick = full_name.split()[-1]
    for name, code in TEAM_NAME_TO_CODE.items():
        if name.split()[-1] == nick:
            return code
    return None


_TEAM_ID_CACHE = None


def find_team_id(full_name):
    global _TEAM_ID_CACHE
    if _TEAM_ID_CACHE is None:
        data = get_json("https://statsapi.mlb.com/api/v1/teams?sportId=1")
        _TEAM_ID_CACHE = {t["name"]: t["id"] for t in data.get("teams", [])}
    if full_name not in _TEAM_ID_CACHE:
        raise ValueError(f"Team '{full_name}' not in statsapi team list")
    return _TEAM_ID_CACHE[full_name]


def team_trailing_live(team_id, cutoff_date, season):
    start = f"{season}-01-01"
    end = (cutoff_date - dt.timedelta(days=1)).isoformat()
    if end < start:
        return None, None, None, 0
    url = (f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&teamId={team_id}"
           f"&startDate={start}&endDate={end}&gameType=R&hydrate=linescore")
    data = get_json(url)
    games = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            if g.get("status", {}).get("detailedState") != "Final":
                continue
            teams = g.get("teams", {})
            is_home = teams.get("home", {}).get("team", {}).get("id") == team_id
            side, other = ("home", "away") if is_home else ("away", "home")
            rs = teams.get(side, {}).get("score")
            ra = teams.get(other, {}).get("score")
            if rs is None or ra is None:
                continue
            games.append((g.get("officialDate"), rs, ra))
    games.sort(key=lambda x: x[0])
    n_prior = len(games)
    last = games[-TEAM_TRAIL_WINDOW:]
    if not last:
        return None, None, None, n_prior
    trail_rs = sum(x[1] for x in last) / len(last)
    trail_ra = sum(x[2] for x in last) / len(last)
    last_date = dt.date.fromisoformat(last[-1][0])
    rest_days = min((cutoff_date - last_date).days, REST_DAYS_CAP)
    return trail_rs, trail_ra, rest_days, n_prior


def pitcher_trailing_live(person_id, cutoff_date, season):
    url = (f"https://statsapi.mlb.com/api/v1/people/{person_id}/stats"
           f"?stats=gameLog&season={season}&group=pitching&gameType=R")
    data = get_json(url)
    splits = data.get("stats", [{}])[0].get("splits", [])
    starts = []
    for s in splits:
        game_date = s.get("date")
        if not game_date or game_date >= cutoff_date.isoformat():
            continue
        stat = s.get("stat", {})
        if int(stat.get("gamesStarted", 0)) != 1:
            continue
        ip = stat.get("inningsPitched", "0.0")
        whole, _, frac = ip.partition(".")
        ipouts = int(whole) * 3 + int(frac or 0)
        starts.append({"date": game_date, "er": stat.get("earnedRuns", 0), "ipouts": ipouts,
                        "k": stat.get("strikeOuts", 0), "bb": stat.get("baseOnBalls", 0),
                        "bfp": stat.get("battersFaced", 0)})
    starts.sort(key=lambda x: x["date"])
    n_prior = len(starts)
    last = starts[-SP_TRAIL_WINDOW:]
    if not last:
        return None, None, n_prior
    er_sum = sum(x["er"] for x in last)
    ipouts_sum = sum(x["ipouts"] for x in last)
    k_sum = sum(x["k"] for x in last)
    bb_sum = sum(x["bb"] for x in last)
    bfp_sum = sum(x["bfp"] for x in last)
    era = 9.0 * er_sum / (ipouts_sum / 3.0) if ipouts_sum else None
    kbb = (k_sum - bb_sum) / bfp_sum if bfp_sum else None
    return era, kbb, n_prior


def fetch_probables_for_dates(dates):
    out = []
    for d in sorted(dates):
        url = (f"https://statsapi.mlb.com/api/v1/schedule?sportId=1"
               f"&startDate={d.isoformat()}&endDate={d.isoformat()}"
               f"&hydrate=probablePitcher,team")
        data = get_json(url)
        for dd in data.get("dates", []):
            official_date = dd.get("date")
            for g in dd.get("games", []):
                teams = g.get("teams", {})
                away = teams.get("away", {})
                home = teams.get("home", {})
                away_full = away.get("team", {}).get("name")
                home_full = home.get("team", {}).get("name")
                away_code = resolve_code(away_full) if away_full else None
                home_code = resolve_code(home_full) if home_full else None
                away_p = away.get("probablePitcher") or {}
                home_p = home.get("probablePitcher") or {}
                out.append({
                    "away_code": away_code, "home_code": home_code,
                    "away_full": away_full, "home_full": home_full,
                    "away_pitcher_id": away_p.get("id"), "away_pitcher_name": away_p.get("fullName"),
                    "home_pitcher_id": home_p.get("id"), "home_pitcher_name": home_p.get("fullName"),
                    "kickoff": g.get("gameDate"), "official_date": official_date,
                    "game_pk": g.get("gamePk"),
                })
    return out


def implied_prob(american_odds):
    if american_odds < 0:
        return (-american_odds) / ((-american_odds) + 100.0)
    return 100.0 / (american_odds + 100.0)


def load_odds(odds_path):
    rows = []
    with open(odds_path) as f:
        for row in csv.DictReader(f):
            rows.append(row)
    by_key = defaultdict(list)
    for row in rows:
        by_key[row["join_key"]].append(row)
    return by_key


def _require_pregame(lock_time, kickoff_time):
    k = dt.datetime.fromisoformat(str(kickoff_time).replace("Z", "+00:00"))
    if lock_time >= k:
        raise ValueError("Pregame guard failed: lock_timestamp_utc must be before kickoff_time")


def init_db(conn):
    # NEW table, separate from `picks` (moneyline) -- single-writer discipline:
    # this script only ever writes spread_picks, never touches picks/grades.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS spread_picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT, season INTEGER, game_date TEXT,
            home_team TEXT, away_team TEXT, home_team_full TEXT, away_team_full TEXT,
            home_starter_name TEXT, away_starter_name TEXT,
            home_starter_statsapi_id INTEGER, away_starter_statsapi_id INTEGER,
            model_version TEXT, model_prob_home_covers REAL,
            spread_side_team TEXT, spread_side_display TEXT, spread_pick_prob REAL,
            home_trail15_rs REAL, home_trail15_ra REAL, away_trail15_rs REAL, away_trail15_ra REAL,
            home_sp_trail5_era REAL, home_sp_trail5_kbb REAL, away_sp_trail5_era REAL, away_sp_trail5_kbb REAL,
            home_rest_days REAL, away_rest_days REAL,
            home_spread_point REAL, home_spread_price REAL,
            away_spread_point REAL, away_spread_price REAL,
            spread_market_book TEXT,
            lock_timestamp_utc TEXT, kickoff_time TEXT, statsapi_game_pk INTEGER,
            features_sha256 TEXT, odds_sha256 TEXT, odds_csv_path TEXT,
            graded INTEGER DEFAULT 0, note TEXT,
            UNIQUE(game_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS spread_grades (
            pick_id INTEGER PRIMARY KEY,
            graded_at_utc TEXT, actual_home_score INTEGER, actual_away_score INTEGER,
            actual_home_covers INTEGER, model_pick_correct INTEGER
        )
    """)
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--odds", type=Path, required=True,
                         help="Same odds CSV mlb_live_tracker_lock_v3.py uses (join_key/kickoff_time/team codes). "
                              "A plain h2h-only CSV works fine -- spread market columns are logged for context "
                              "only if present (fetch_mlb_odds_v3.py --markets h2h,spreads), never required.")
    args = parser.parse_args()

    if not TEAMSTATS_PATH.exists() or not PITCHING_PATH.exists():
        raise SystemExit(f"Retrosheet data not found under {DATA_DIR}")
    if not args.odds.exists():
        raise SystemExit(f"Odds CSV not found: {args.odds}")

    odds_bytes = args.odds.read_bytes()
    odds_sha256 = hashlib.sha256(odds_bytes).hexdigest()

    df_ok = load_training_data_spread()
    model, feat_C = fit_model_c_spread(df_ok)

    odds_by_key = load_odds(args.odds)
    print(f"\nLoaded odds for {len(odds_by_key)} real game(s) from {args.odds}")

    date_candidates = set()
    for key, book_rows in odds_by_key.items():
        kt = dt.datetime.fromisoformat(book_rows[0]["kickoff_time"].replace("Z", "+00:00"))
        date_candidates.add(kt.date())
        date_candidates.add(kt.date() - dt.timedelta(days=1))
    print(f"Querying statsapi across {len(date_candidates)} official date(s): {sorted(date_candidates)}")
    probables = fetch_probables_for_dates(date_candidates)
    print(f"Found {len(probables)} real statsapi game(s) across that window.")

    lock_time = dt.datetime.now(dt.timezone.utc)
    print(f"\nLock timestamp: {lock_time.isoformat()}")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    pending = []
    n_skipped = 0
    skip_reasons = []

    for join_key, book_rows in sorted(odds_by_key.items()):
        home_team = book_rows[0]["home_team"]
        away_team = book_rows[0]["away_team"]
        kickoff_time = book_rows[0]["kickoff_time"]
        kickoff_dt = dt.datetime.fromisoformat(kickoff_time.replace("Z", "+00:00"))

        candidates = [p for p in probables if p["home_code"] == home_team and p["away_code"] == away_team]

        def _kdist(p):
            try:
                pk = dt.datetime.fromisoformat(p["kickoff"].replace("Z", "+00:00"))
                return abs((pk - kickoff_dt).total_seconds())
            except Exception:
                return 1e18

        candidates_in_tol = [p for p in candidates if _kdist(p) <= KICKOFF_MATCH_TOLERANCE_SECONDS]
        if not candidates_in_tol:
            skip_reasons.append((join_key, f"no statsapi game within {KICKOFF_MATCH_TOLERANCE_SECONDS}s of real kickoff "
                                            f"({len(candidates)} same-team-pair candidates found, closest "
                                            f"{min((_kdist(p) for p in candidates), default=None)}s away)"))
            n_skipped += 1
            continue
        match = min(candidates_in_tol, key=_kdist)

        if not match["home_pitcher_id"] or not match["away_pitcher_id"]:
            skip_reasons.append((join_key, f"probable pitcher not yet listed (home={match['home_pitcher_name']}, away={match['away_pitcher_name']})"))
            n_skipped += 1
            continue

        official_date = match["official_date"]
        dh_key = (home_team, official_date)

        season = int(official_date[:4])
        cutoff_date = dt.date.fromisoformat(official_date)

        try:
            home_team_id = find_team_id(match["home_full"])
            away_team_id = find_team_id(match["away_full"])
            h_rs, h_ra, h_rest, h_n = team_trailing_live(home_team_id, cutoff_date, season)
            a_rs, a_ra, a_rest, a_n = team_trailing_live(away_team_id, cutoff_date, season)
            h_era, h_kbb, h_sp_n = pitcher_trailing_live(match["home_pitcher_id"], cutoff_date, season)
            a_era, a_kbb, a_sp_n = pitcher_trailing_live(match["away_pitcher_id"], cutoff_date, season)
        except Exception as e:
            skip_reasons.append((join_key, f"live feature pull failed: {type(e).__name__}: {e}"))
            n_skipped += 1
            continue

        if h_n < TEAM_TRAIL_MIN or a_n < TEAM_TRAIL_MIN:
            skip_reasons.append((join_key, f"insufficient team history (home_n={h_n}, away_n={a_n}, need >={TEAM_TRAIL_MIN})"))
            n_skipped += 1
            continue
        if h_sp_n < SP_TRAIL_MIN or a_sp_n < SP_TRAIL_MIN:
            skip_reasons.append((join_key, f"insufficient starter history (home_sp_n={h_sp_n}, away_sp_n={a_sp_n}, need >={SP_TRAIL_MIN})"))
            n_skipped += 1
            continue
        if None in (h_rs, h_ra, h_rest, a_rs, a_ra, a_rest, h_era, h_kbb, a_era, a_kbb):
            skip_reasons.append((join_key, "a required live feature came back None -- refusing to impute/fabricate"))
            n_skipped += 1
            continue

        try:
            _require_pregame(lock_time, kickoff_time)
        except ValueError as e:
            skip_reasons.append((join_key, str(e)))
            n_skipped += 1
            continue

        import pandas as pd
        feat_row = pd.DataFrame([{
            "home_trail15_rs": h_rs, "home_trail15_ra": h_ra,
            "away_trail15_rs": a_rs, "away_trail15_ra": a_ra,
            "home_sp_trail5_era": h_era, "home_sp_trail5_kbb": h_kbb,
            "away_sp_trail5_era": a_era, "away_sp_trail5_kbb": a_kbb,
            "home_rest_days": h_rest, "away_rest_days": a_rest,
        }])[feat_C]
        model_prob_home_covers = float(model.predict_proba(feat_row)[0, 1])
        features_sha256 = hashlib.sha256(feat_row.to_csv(index=False).encode()).hexdigest()

        # Resolve the pick side exactly like build_mlb_slate_writes.py resolved
        # the moneyline winner_side/winner_prob: whichever side the model
        # favors becomes the pick, at its own (already-flipped) probability.
        home_disp = RETRO_TO_DISPLAY.get(home_team, home_team)
        away_disp = RETRO_TO_DISPLAY.get(away_team, away_team)
        if model_prob_home_covers >= 0.5:
            spread_side_team, spread_side_display, spread_pick_prob = home_disp, f"{home_disp} -1.5", model_prob_home_covers
        else:
            spread_side_team, spread_side_display, spread_pick_prob = away_disp, f"{away_disp} +1.5", 1.0 - model_prob_home_covers

        # Optional, context-only real market run-line price -- never required,
        # never used in the prediction above. Present only if --odds was
        # fetched with fetch_mlb_odds_v3.py --markets h2h,spreads.
        home_spread_point = home_spread_price = away_spread_point = away_spread_price = None
        spread_market_book = None
        for br in book_rows:
            hp = br.get("home_spread_point"); hpx = br.get("home_spread_price")
            if hp not in (None, "") and hpx not in (None, ""):
                home_spread_point = float(hp); home_spread_price = float(hpx)
                ap = br.get("away_spread_point"); apx = br.get("away_spread_price")
                if ap not in (None, ""): away_spread_point = float(ap)
                if apx not in (None, ""): away_spread_price = float(apx)
                spread_market_book = br.get("bookmaker")
                break  # first book with a real spread market wins -- context only, no averaging/devig needed

        row = {
            "official_date": official_date, "home_team": home_team, "away_team": away_team,
            "home_team_full": match["home_full"], "away_team_full": match["away_full"],
            "home_starter_name": match["home_pitcher_name"], "away_starter_name": match["away_pitcher_name"],
            "home_starter_statsapi_id": match["home_pitcher_id"], "away_starter_statsapi_id": match["away_pitcher_id"],
            "model_version": MODEL_VERSION, "model_prob_home_covers": model_prob_home_covers,
            "spread_side_team": spread_side_team, "spread_side_display": spread_side_display,
            "spread_pick_prob": spread_pick_prob,
            "home_trail15_rs": h_rs, "home_trail15_ra": h_ra, "away_trail15_rs": a_rs, "away_trail15_ra": a_ra,
            "home_sp_trail5_era": h_era, "home_sp_trail5_kbb": h_kbb, "away_sp_trail5_era": a_era, "away_sp_trail5_kbb": a_kbb,
            "home_rest_days": h_rest, "away_rest_days": a_rest,
            "home_spread_point": home_spread_point, "home_spread_price": home_spread_price,
            "away_spread_point": away_spread_point, "away_spread_price": away_spread_price,
            "spread_market_book": spread_market_book,
            "lock_timestamp_utc": lock_time.isoformat(), "kickoff_time": kickoff_time,
            "statsapi_game_pk": match["game_pk"],
            "features_sha256": features_sha256, "odds_sha256": odds_sha256, "odds_csv_path": str(args.odds),
            "graded": 0, "note": None, "season": season, "kickoff_dt": kickoff_dt,
            "dh_key": dh_key, "join_key": join_key,
            "away_pitcher_name_log": match["away_pitcher_name"], "home_pitcher_name_log": match["home_pitcher_name"],
        }
        pending.append(row)

    groups = defaultdict(list)
    for row in pending:
        groups[row["dh_key"]].append(row)

    n_logged = 0
    for dh_key, rows_in_group in groups.items():
        rows_in_group.sort(key=lambda r: r["kickoff_dt"])
        single = len(rows_in_group) == 1
        for i, row in enumerate(rows_in_group):
            game_number = 0 if single else (i + 1)
            home_team = row["home_team"]
            official_date = row["official_date"]
            game_id = f"{home_team}{official_date.replace('-', '')}{game_number}"
            row["game_id"] = game_id
            row["game_date"] = official_date
            away_name = row["away_pitcher_name_log"]
            home_name = row["home_pitcher_name_log"]
            for k in ("kickoff_dt", "dh_key", "join_key", "away_pitcher_name_log", "home_pitcher_name_log", "official_date"):
                del row[k]

            try:
                cols = list(row.keys())
                placeholders = ",".join("?" for _ in cols)
                conn.execute(f"INSERT INTO spread_picks ({','.join(cols)}) VALUES ({placeholders})", [row[c] for c in cols])
                conn.commit()
                n_logged += 1
                print(f"  LOGGED {game_id} (officialDate={official_date}): {row['away_team']}@{home_team} "
                      f"[{away_name} vs {home_name}] "
                      f"spread_pick={row['spread_side_display']} prob={row['spread_pick_prob']:.3f} "
                      f"(raw model_prob_home_covers={row['model_prob_home_covers']:.3f})")
            except sqlite3.IntegrityError as e:
                skip_reasons.append((game_id, f"DB write failed (likely already logged): {e}"))
                n_skipped += 1

    conn.close()

    print(f"\n{'=' * 70}")
    print(f"DONE. Logged {n_logged} spread pick(s), skipped {n_skipped}.")
    if skip_reasons:
        print("Skip reasons:")
        for key, reason in skip_reasons:
            print(f"  {key}: {reason}")
    print(f"DB: {DB_PATH} (table: spread_picks)")


if __name__ == "__main__":
    main()
