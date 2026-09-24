#!/usr/bin/env python3
"""
mlb_live_tracker_lock_v3.py -- playoff-readiness fix, built while preparing
for the 2026 MLB postseason. v2 is untouched (single-writer/versioning
discipline: v1/v2 were direct edits while this script was brand-new and
unshipped; it has since logged real picks, so from here on every change is
a new additively-named version, same as NFL's V1.1-V1.4).

TWO real, previously-undisclosed data-composition bugs found and fixed here,
both discovered while investigating what "building for the playoffs" would
actually require -- not found by accident during a live run:

1. load_training_data() never filtered on Retrosheet's own `gametype`
   column. A direct check against mlb_moneyline_v4_pooled_predictions.csv
   (the already-graded backtest pool) showed 385 postseason games
   (wildcard/divisionseries/lcs/worldseries) and 4 All-Star exhibition
   games silently mixed into what every V1-V4 backtest treated as "the"
   2010-2025 dataset -- about 1.75% of the pool. Model C's accuracy on
   just the postseason subset (54.81%, n=385) was close to but not
   identical to the regular-season-only subset (55.91%, n=21,948); by
   round it ranged from 48.98% (wildcard, n=49) to 61.21% (division
   series, n=165) to 39.66% (world series, n=58) -- all samples too small
   to draw a real conclusion from, but real enough to fix the underlying
   data hygiene rather than ignore it. v3 filters both `ts` and `sp` to
   `gametype == "R"` before training, matching what team_trailing_live()
   already did live (see #2) but load_training_data() never did.

2. team_trailing_live() already correctly passes `gameType=R` to statsapi
   -- team-level trailing rs/ra has always been regular-season-only.
   pitcher_trailing_live() did NOT pass any gameType filter to its
   `/people/{id}/stats?stats=gameLog` call. Once the postseason starts,
   if that endpoint's default gameLog includes postseason starts (this
   has NOT been empirically verified -- statsapi's live behavior can only
   be tested from a real Terminal with real network access, and no 2026
   postseason start exists yet to test against), team and pitcher trailing
   features would silently rest on two DIFFERENT definitions of "this
   season's games" for the same pick -- exactly the kind of quiet
   inconsistency this project has caught before (the LAN date-boundary
   bug, the leftover-dh_counts NameError). Fixed by adding an explicit
   `gameType=R` parameter to the gameLog call, matching team-level
   treatment. FLAGGED, NOT CONFIRMED: verify this parameter actually
   restricts the gameLog response once real postseason data exists to
   test against (see PLAYOFF_READINESS_TODO below) -- if statsapi ignores
   an unsupported query param rather than erroring, this fix could be a
   silent no-op, so it needs a real check, not just code review.

DESIGN DECISION LOCKED HERE, not left to accident: trailing stats (both
team and pitcher) stay REGULAR-SEASON-ONLY straight through the postseason.
This was the only definition ever validated (against real Retrosheet ground
truth, in mlb_feature_validation.py) and the only one any backtest ever
tested. Updating trailing stats to include already-completed postseason
rounds this October would be a real, untested behavior change introduced
right as stakes get highest -- rejected for that reason, not attempted here.

PLAYOFF_READINESS_TODO (cannot be resolved from this sandbox -- needs a
real Terminal, and in most cases needs the actual bracket to exist):
  - Confirm fetch_probables_for_dates()'s date-range schedule query
    actually returns postseason games once they're scheduled (no gameType
    filter is applied there today, so this SHOULD already work, but is
    unverified against real postseason data).
  - Confirm gameType=R on pitcher_trailing_live() actually restricts the
    gameLog response (see #2 above).
  - Confirm fetch_mlb_odds.py / The Odds API actually lists postseason MLB
    events once they exist -- not yet checked.
  - Decide, before the bracket is set, whether teams that didn't make the
    playoffs should simply stop generating picks (they will, naturally,
    once they stop appearing on the statsapi schedule) -- no code change
    needed, just confirming the natural behavior is the intended one.

Everything else (kickoff-based matching, doubleheader-leg numbering,
pregame guard, monitoring-only design, no BET recommendation, no
auto-attestation) is unchanged from v2. See v2's own docstring for the
original date-boundary bug this whole script exists to avoid repeating.

Usage:
    python3 MLB_AI_MODEL/SUPPORT/mlb_live_tracker_lock_v3.py \\
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
MODEL_VERSION = "MLB_MODEL_C_V1_10FEAT_LIVE_V3_REGSEASON_ONLY"
KICKOFF_MATCH_TOLERANCE_SECONDS = 180  # matching an odds event to a statsapi game

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

try:
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    CTX = ssl.create_default_context()


def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "mlb-tracker-lock/3.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------------
# Step 1: train Model C on 2010-2025 Retrosheet data.
# v3 FIX: filter to gametype == "R" (regular season only) -- v1/v2 loaded
# every gametype in the file (regular + postseason + all-star) with no
# filter at all. See module docstring, bug #1.
# ---------------------------------------------------------------------

def load_training_data():
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

    games = pd.DataFrame({
        "gid": common, "date": home["date"].values, "number": home["number"].values,
        "home_team": home["team"].values, "away_team": away["team"].values,
        "home_win": (home["win"] == 1).astype(int).values, "clean": clean_mask.values,
    })

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
    return df_ok


def fit_model_c(df_ok):
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
    pipe.fit(df_ok[feat_C], df_ok["home_win"].values)
    print(f"  Model C fit on {len(df_ok)} regular-season games (2010-2025), {len(feat_C)} features.")
    return pipe, feat_C


# ---------------------------------------------------------------------
# Live statsapi feature pull.
# team_trailing_live(): unchanged from v2 -- already gameType=R.
# pitcher_trailing_live(): v3 FIX -- adds gameType=R explicitly (was
# missing in v1/v2). UNVERIFIED against real postseason data, see module
# docstring bug #2 and PLAYOFF_READINESS_TODO.
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
    # v3 FIX: added gameType=R (was missing in v1/v2 -- see module
    # docstring bug #2). NOT yet empirically verified that statsapi's
    # gameLog endpoint honors this parameter; verify in a real Terminal
    # once real postseason start data exists to test against.
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
    """Returns a flat list of real statsapi game entries across every
    officialDate in `dates` (a set of date objects), each carrying its
    TRUE officialDate, real gameDate (UTC), team codes, and probable
    pitchers. This is the authoritative source for both date AND
    doubleheader-leg identity -- never guessed. No gameType filter here,
    unchanged from v2 -- this SHOULD already surface postseason games
    once they're scheduled, but that is unverified against real
    postseason data (see PLAYOFF_READINESS_TODO in module docstring)."""
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


# ---------------------------------------------------------------------
# Odds
# ---------------------------------------------------------------------

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


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def _require_pregame(lock_time, kickoff_time):
    k = dt.datetime.fromisoformat(str(kickoff_time).replace("Z", "+00:00"))
    if lock_time >= k:
        raise ValueError("Pregame guard failed: lock_timestamp_utc must be before kickoff_time")


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT, season INTEGER, game_date TEXT,
            home_team TEXT, away_team TEXT, home_team_full TEXT, away_team_full TEXT,
            home_starter_name TEXT, away_starter_name TEXT,
            home_starter_statsapi_id INTEGER, away_starter_statsapi_id INTEGER,
            model_version TEXT, model_prob_home REAL,
            home_trail15_rs REAL, home_trail15_ra REAL, away_trail15_rs REAL, away_trail15_ra REAL,
            home_sp_trail5_era REAL, home_sp_trail5_kbb REAL, away_sp_trail5_era REAL, away_sp_trail5_kbb REAL,
            home_rest_days REAL, away_rest_days REAL,
            home_moneyline_hardrockbet_fl REAL, away_moneyline_hardrockbet_fl REAL,
            home_moneyline_kalshi REAL, away_moneyline_kalshi REAL,
            market_prob_home_devigged REAL, n_books_used INTEGER,
            edge_home REAL, home_favored_by_model INTEGER,
            lock_timestamp_utc TEXT, kickoff_time TEXT, statsapi_game_pk INTEGER,
            features_sha256 TEXT, odds_sha256 TEXT, odds_csv_path TEXT,
            graded INTEGER DEFAULT 0, note TEXT,
            UNIQUE(game_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS grades (
            pick_id INTEGER PRIMARY KEY,
            graded_at_utc TEXT, actual_home_score INTEGER, actual_away_score INTEGER,
            home_won INTEGER, model_favored_correct INTEGER
        )
    """)
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--odds", type=Path, required=True)
    args = parser.parse_args()

    if not TEAMSTATS_PATH.exists() or not PITCHING_PATH.exists():
        raise SystemExit(f"Retrosheet data not found under {DATA_DIR}")
    if not args.odds.exists():
        raise SystemExit(f"Odds CSV not found: {args.odds}")

    odds_bytes = args.odds.read_bytes()
    odds_sha256 = hashlib.sha256(odds_bytes).hexdigest()

    df_ok = load_training_data()
    model, feat_C = fit_model_c(df_ok)

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
        model_prob_home = float(model.predict_proba(feat_row)[0, 1])
        features_sha256 = hashlib.sha256(feat_row.to_csv(index=False).encode()).hexdigest()

        book_ml = {"hardrockbet_fl": {}, "kalshi": {}}
        devig_probs = []
        for br in book_rows:
            bk = br["bookmaker"]
            if bk not in book_ml:
                continue
            hm = float(br["home_moneyline"]); am = float(br["away_moneyline"])
            book_ml[bk] = {"home": hm, "away": am}
            ph_raw = implied_prob(hm); pa_raw = implied_prob(am)
            devig_probs.append(ph_raw / (ph_raw + pa_raw))

        if not devig_probs:
            skip_reasons.append((join_key, "no usable book odds for this game"))
            n_skipped += 1
            continue

        market_prob_home = sum(devig_probs) / len(devig_probs)
        edge_home = model_prob_home - market_prob_home

        row = {
            "official_date": official_date, "home_team": home_team, "away_team": away_team,
            "home_team_full": match["home_full"], "away_team_full": match["away_full"],
            "home_starter_name": match["home_pitcher_name"], "away_starter_name": match["away_pitcher_name"],
            "home_starter_statsapi_id": match["home_pitcher_id"], "away_starter_statsapi_id": match["away_pitcher_id"],
            "model_version": MODEL_VERSION, "model_prob_home": model_prob_home,
            "home_trail15_rs": h_rs, "home_trail15_ra": h_ra, "away_trail15_rs": a_rs, "away_trail15_ra": a_ra,
            "home_sp_trail5_era": h_era, "home_sp_trail5_kbb": h_kbb, "away_sp_trail5_era": a_era, "away_sp_trail5_kbb": a_kbb,
            "home_rest_days": h_rest, "away_rest_days": a_rest,
            "home_moneyline_hardrockbet_fl": book_ml["hardrockbet_fl"].get("home"),
            "away_moneyline_hardrockbet_fl": book_ml["hardrockbet_fl"].get("away"),
            "home_moneyline_kalshi": book_ml["kalshi"].get("home"),
            "away_moneyline_kalshi": book_ml["kalshi"].get("away"),
            "market_prob_home_devigged": market_prob_home, "n_books_used": len(devig_probs),
            "edge_home": edge_home, "home_favored_by_model": int(edge_home > 0),
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
                conn.execute(f"INSERT INTO picks ({','.join(cols)}) VALUES ({placeholders})", [row[c] for c in cols])
                conn.commit()
                n_logged += 1
                print(f"  LOGGED {game_id} (officialDate={official_date}): {row['away_team']}@{home_team} "
                      f"[{away_name} vs {home_name}] "
                      f"model_p_home={row['model_prob_home']:.3f} market_p_home={row['market_prob_home_devigged']:.3f} "
                      f"edge={row['edge_home']:+.3f}")
            except sqlite3.IntegrityError as e:
                skip_reasons.append((game_id, f"DB write failed (likely already logged): {e}"))
                n_skipped += 1

    conn.close()

    print(f"\n{'=' * 70}")
    print(f"DONE. Logged {n_logged} game(s), skipped {n_skipped}.")
    if skip_reasons:
        print("Skip reasons:")
        for key, reason in skip_reasons:
            print(f"  {key}: {reason}")
    print(f"DB: {DB_PATH}")


if __name__ == "__main__":
    main()
