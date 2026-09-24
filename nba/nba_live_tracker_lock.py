#!/usr/bin/env python3
"""
nba_live_tracker_lock.py -- the NBA live-forward tracker, mirroring
MLB_AI_MODEL/SUPPORT/mlb_live_tracker_lock.py's design and discipline.

Trains Model C (the NBA_MONEYLINE_V1 8-feature team-scoring-form + rest
model, the one that passed its pre-registered bar -- see
claude/nba_moneyline_v1_results_pass.md) FRESH on every real game currently
known (the 5 validated historical seasons plus the current season's real
games-to-date), pulls each real upcoming odds event's trailing features,
predicts home-win probability, compares to the real de-vigged market price,
and logs everything to a NBA-only SQLite tracker DB
(NBA_PROSPECTIVE_TRACKER_V1.sqlite3), separate from MLB's and NFL's.

STILL LOGS MONITORING DATA ONLY, NOT A BETTING DECIDER. Model C passed its
pre-registered bar against a team-record baseline built from free outcome
data -- it has NEVER been tested against real market odds. Whether it has
any edge against a real line is exactly the open question this tracker
exists to accumulate real evidence about. No "BET" or "PASS" label, no
recommendation, no human-attestation field is ever auto-set. Never places
a real bet.

WHY THIS SHOULD NOT REPEAT MLB'S DATE-BOUNDARY BUG: MLB's tracker had to
invent a Retrosheet-style game_id from a guessed "official date", which
broke for West-coast night games whose local date differs from their UTC
kickoff date. NBA has no equivalent problem -- ESPN's own event_id is
already a real, unambiguous, globally-unique identifier, and every
timestamp here is a precise UTC instant, not a date label. This script
never constructs a game_id or guesses a date; it matches each real odds
event to a real ESPN event purely by team pair + closest real kickoff
timestamp (tolerance below), then uses that ESPN event's own event_id as
the tracker's key.

Usage:
    python3 nba_live_tracker_lock.py --odds ../live/sportsbook/NBA_ODDS_LIVE.csv

Needs pandas, numpy, scikit-learn (already present -- sportsbet depends on
them) and network access to ESPN's public scoreboard endpoint for the
current season's real games (unauthenticated, no key needed; see
claude/nba_espn_data_source_validated.md for why this endpoint is used
instead of the sports-betting library's own broken NBAStats).
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import warnings
from collections import defaultdict
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data"
CACHE_DIR = DATA_DIR / "espn_raw_nba"
DB_PATH = SCRIPT_DIR / "NBA_PROSPECTIVE_TRACKER_V1.sqlite3"

HISTORICAL_SEASONS = [2022, 2023, 2024, 2025, 2026]  # the 5 validated seasons
TRAIL_N = 10
MIN_HISTORY = 10
REST_CAP = 5
MODEL_VERSION = "NBA_MODEL_C_V1_8FEAT_LIVE"
KICKOFF_MATCH_TOLERANCE_SECONDS = 900  # 15 minutes -- wider than MLB's 3, since NBA broadcast-driven tip time shifts of a few minutes are common and not yet empirically calibrated for this tracker
LOOKAHEAD_DAYS = 4  # DEFAULT/floor only -- see REAL BUG note below. how far past "today" to fetch ESPN's schedule, to catch upcoming games to match against odds
LOOKAHEAD_BUFFER_DAYS = 2  # extra safety margin added past the odds file's own furthest real kickoff

# REAL BUG, found on the user's first real run (2026-09-24) and fixed here:
# The Odds API posts NBA lines weeks before tip-off (confirmed: 41 events
# posted ~4 weeks ahead of the 2026-27 opener). A fixed LOOKAHEAD_DAYS=4
# fetched the ESPN schedule only through today+4 days, so every one of
# those already-posted games had no corresponding ESPN schedule row to
# match against -- each one fell back to matching some unrelated, far-off
# HISTORICAL game between the same two teams (same team codes, from a
# past season) and was correctly rejected by the kickoff-tolerance check,
# but never logged. Root cause was the ESPN lookahead window being too
# short relative to how far ahead odds are actually posted, not a
# matching-logic bug. Fixed in main() below: it inspects the real odds
# file's furthest real kickoff BEFORE fetching the ESPN schedule, and
# widens the fetch window to cover it (plus LOOKAHEAD_BUFFER_DAYS) rather
# than trusting a fixed guess.

TEAM_NAME_TO_CODE = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA", "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
    "LA Clippers": "LAC", "Los Angeles Clippers": "LAC", "Los Angeles Lakers": "LAL",
    "Memphis Grizzlies": "MEM", "Miami Heat": "MIA", "Milwaukee Bucks": "MIL",
    "Minnesota Timberwolves": "MIN", "New Orleans Pelicans": "NOP", "New York Knicks": "NYK",
    "Oklahoma City Thunder": "OKC", "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI",
    "Phoenix Suns": "PHX", "Portland Trail Blazers": "POR", "Sacramento Kings": "SAC",
    "San Antonio Spurs": "SAS", "Toronto Raptors": "TOR", "Utah Jazz": "UTA",
    "Washington Wizards": "WAS",
}


def resolve_code(full_name):
    if full_name in TEAM_NAME_TO_CODE:
        return TEAM_NAME_TO_CODE[full_name]
    nick = full_name.split()[-1]
    for name, code in TEAM_NAME_TO_CODE.items():
        if name.split()[-1] == nick:
            return code
    return None


# ---------------------------------------------------------------------
# Current-season ESPN fetch -- same fetch_day/parse_day/wanted logic as
# the already-validated nba_espn_scoreboard_fetch_v1.py, copied verbatim
# (this project's convention: standalone, self-contained scripts, no
# cross-script imports) rather than re-derived.
# ---------------------------------------------------------------------
GAMES_URL_TMPL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={d}&limit=1000"
PRESEASON = 1
EXHIBITION = "ALLSTAR"
REQUEST_DELAY_SECONDS = 0.35


def current_season_end_year(today):
    """A season is named by the year it ends in; the season year rolls
    over on Sep 1 (matches sportsbet's own MONTHS convention and this
    project's fetcher)."""
    return today.year + 1 if today.month >= 9 else today.year


def fetch_day(day, cache_dir):
    cache_path = cache_dir / f"{day.strftime('%Y%m%d')}.json"
    if cache_path.exists():
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)
    url = GAMES_URL_TMPL.format(d=day.strftime("%Y%m%d"))
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.espn.com/nba/scoreboard",
        "Origin": "https://www.espn.com",
    }
    req = urllib.request.Request(url, headers=headers)
    max_attempts = 5
    backoff_seconds = [2, 5, 10, 20, 30]
    content = None
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                content = resp.read()
            break
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                sys.exit(f"HTTP {e.code} fetching {url} -- unexpected. Stopping.")
            print(f"  HTTP {e.code} on {day} (attempt {attempt + 1}/{max_attempts}) -- retrying...")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print(f"  {type(e).__name__}: {e} on {day} (attempt {attempt + 1}/{max_attempts}) -- retrying...")
        if attempt < max_attempts - 1:
            time.sleep(backoff_seconds[attempt])
    if content is None:
        sys.exit(f"Gave up on {day} after {max_attempts} attempts, all transient network errors. Re-run the same command.")
    payload = json.loads(content)
    cache_dir.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    time.sleep(REQUEST_DELAY_SECONDS)
    return payload


def wanted(event):
    competitions = event.get("competitions") or [{}]
    season_type = (event.get("season") or {}).get("type")
    competition_type = (competitions[0].get("type") or {}).get("abbreviation")
    return season_type != PRESEASON and competition_type != EXHIBITION


def parse_day(payload, fetch_date):
    rows = []
    for event in payload.get("events", []):
        if not wanted(event):
            continue
        competition = (event.get("competitions") or [{}])[0]
        competitors = {side.get("homeAway"): side for side in competition.get("competitors", [])}
        home, away = competitors.get("home"), competitors.get("away")
        if home is None or away is None or not event.get("date"):
            continue
        status = competition.get("status", {}).get("type", {})
        played = bool(status.get("completed"))
        rows.append({
            "event_id": event.get("id"), "date_utc": event["date"], "fetch_date": fetch_date,
            "season_type": (event.get("season") or {}).get("type"),
            "home_team": (home.get("team") or {}).get("displayName"),
            "away_team": (away.get("team") or {}).get("displayName"),
            "home_points": int(home.get("score", -1)) if played else None,
            "away_points": int(away.get("score", -1)) if played else None,
            "played": played, "status_detail": status.get("detail"),
        })
    return rows


def fetch_current_season(today, season_end_year, lookahead_days=LOOKAHEAD_DAYS):
    """Fetch every day from this season's start through today+lookahead_days
    (cached, so re-runs only fetch new days), and write it to
    data/nba_games_raw_season{N}.csv -- same file convention as the main
    fetcher, so this stays reusable/inspectable exactly like the 5
    historical files.

    lookahead_days defaults to the module constant but is widened by
    main() when the real odds file already has kickoffs further out than
    that -- see the REAL BUG note on LOOKAHEAD_DAYS above main()."""
    start = dt.date(season_end_year - 1, 9, 1)
    end = today + dt.timedelta(days=lookahead_days)
    days = []
    d = start
    while d <= end:
        days.append(d)
        d += dt.timedelta(days=1)
    print(f"Fetching current season {season_end_year - 1}-{season_end_year}: {len(days)} days ({start} to {end})")
    all_rows = []
    for day in days:
        payload = fetch_day(day, CACHE_DIR)
        all_rows.extend(parse_day(payload, day.strftime("%Y-%m-%d")))
    out_path = DATA_DIR / f"nba_games_raw_season{season_end_year}.csv"
    fieldnames = ["event_id", "date_utc", "fetch_date", "season_type", "home_team", "away_team",
                  "home_points", "away_points", "played", "status_detail"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in sorted(all_rows, key=lambda r: r["date_utc"]):
            writer.writerow(r)
    print(f"  {len(all_rows)} rows (played + scheduled). Wrote {out_path}")
    return out_path, season_end_year


# ---------------------------------------------------------------------
# Feature engineering -- identical logic to NBA_MONEYLINE_V1_PREREG's
# walk-forward script, except unplayed/future rows are kept (not dropped)
# so a not-yet-played game can still have its own trailing features
# computed from the real games strictly before it. A future row's own
# points are NaN, so it never contaminates any OTHER row's trailing
# window (shift(1) never looks at a row's own value) -- the only edge
# case is two of a team's future games both falling inside the lookahead
# window, in which case the later one's window would fall short of
# min_periods and come back NaN, and that game is correctly SKIPPED
# below rather than given a fabricated number.
# ---------------------------------------------------------------------
def build_features(games):
    games = games.sort_values("date_utc").reset_index(drop=True)
    if games["event_id"].duplicated().any():
        dupes = games.loc[games["event_id"].duplicated(keep=False), "event_id"].unique()
        sys.exit(f"Duplicate event_ids across combined season files: {list(dupes)}. Stopping.")

    home = games.rename(columns={"home_team": "team", "away_team": "opponent",
                                  "home_points": "team_points", "away_points": "opp_points"})[
        ["event_id", "date_utc", "season", "team", "opponent", "team_points", "opp_points", "played"]
    ].copy()
    home["is_home"] = True
    away = games.rename(columns={"away_team": "team", "home_team": "opponent",
                                  "away_points": "team_points", "home_points": "opp_points"})[
        ["event_id", "date_utc", "season", "team", "opponent", "team_points", "opp_points", "played"]
    ].copy()
    away["is_home"] = False

    long_df = pd.concat([home, away], ignore_index=True)
    long_df["win"] = np.where(long_df["played"], (long_df["team_points"] > long_df["opp_points"]).astype(float), np.nan)
    long_df = long_df.sort_values(["team", "date_utc"]).reset_index(drop=True)

    grouped = long_df.groupby("team", group_keys=False)
    long_df["trail10_winpct"] = grouped["win"].transform(lambda s: s.shift(1).rolling(TRAIL_N, min_periods=TRAIL_N).mean())
    long_df["trail10_ppg"] = grouped["team_points"].transform(lambda s: s.shift(1).rolling(TRAIL_N, min_periods=TRAIL_N).mean())
    long_df["trail10_papg"] = grouped["opp_points"].transform(lambda s: s.shift(1).rolling(TRAIL_N, min_periods=TRAIL_N).mean())
    long_df["prior_games_count"] = grouped.cumcount()

    long_df["game_date"] = long_df["date_utc"].dt.date
    long_df["prev_game_date"] = grouped["game_date"].transform(lambda s: s.shift(1))
    gap_days = (pd.to_datetime(long_df["game_date"]) - pd.to_datetime(long_df["prev_game_date"])).dt.days
    long_df["rest_days"] = (gap_days - 1).clip(lower=0, upper=REST_CAP)
    long_df["b2b"] = (long_df["rest_days"] == 0).astype(float)
    long_df.loc[long_df["prior_games_count"] == 0, ["rest_days", "b2b"]] = np.nan

    home_feat = long_df[long_df["is_home"]].set_index("event_id").sort_index()
    away_feat = long_df[~long_df["is_home"]].set_index("event_id").sort_index()

    feat = pd.DataFrame(index=home_feat.index)
    feat["season"] = home_feat["season"]
    feat["date_utc"] = home_feat["date_utc"]
    feat["home_team"] = home_feat["team"]
    feat["away_team"] = away_feat["team"]
    feat["played"] = home_feat["played"]
    feat["home_win"] = home_feat["win"]
    feat["home_prior_count"] = home_feat["prior_games_count"]
    feat["away_prior_count"] = away_feat["prior_games_count"]
    feat["home_trail10_winpct"] = home_feat["trail10_winpct"]
    feat["away_trail10_winpct"] = away_feat["trail10_winpct"]
    feat["home_trail10_ppg"] = home_feat["trail10_ppg"]
    feat["home_trail10_papg"] = home_feat["trail10_papg"]
    feat["away_trail10_ppg"] = away_feat["trail10_ppg"]
    feat["away_trail10_papg"] = away_feat["trail10_papg"]
    feat["home_rest_days"] = home_feat["rest_days"]
    feat["away_rest_days"] = away_feat["rest_days"]
    feat["home_b2b"] = home_feat["b2b"]
    feat["away_b2b"] = away_feat["b2b"]

    mismatch = (feat["home_team"] != home_feat["team"]).sum() + (away_feat["opponent"] != home_feat["team"]).sum()
    if mismatch:
        sys.exit(f"Internal join sanity check failed on {mismatch} rows.")
    return feat


FEATURES_C = [
    "home_trail10_ppg", "home_trail10_papg", "away_trail10_ppg", "away_trail10_papg",
    "home_rest_days", "away_rest_days", "home_b2b", "away_b2b",
]


def fit_model_c(feat):
    train = feat[
        (feat["played"] == True) &  # noqa: E712
        (feat["home_prior_count"] >= MIN_HISTORY) & (feat["away_prior_count"] >= MIN_HISTORY) &
        feat["home_win"].notna()
    ].copy()
    pipe = Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
        ("clf", LogisticRegression(C=0.2, max_iter=2000)),
    ])
    pipe.fit(train[FEATURES_C], train["home_win"])
    print(f"  Model C fit on {len(train)} real games (all seasons through today).")
    return pipe


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


def _require_pregame(lock_time, kickoff_time):
    k = dt.datetime.fromisoformat(str(kickoff_time).replace("Z", "+00:00"))
    if lock_time >= k:
        raise ValueError("Pregame guard failed: lock_timestamp_utc must be before kickoff_time")


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT, season INTEGER, kickoff_time TEXT,
            home_team TEXT, away_team TEXT,
            model_version TEXT, model_prob_home REAL,
            home_trail10_ppg REAL, home_trail10_papg REAL, away_trail10_ppg REAL, away_trail10_papg REAL,
            home_trail10_winpct REAL, away_trail10_winpct REAL,
            home_rest_days REAL, away_rest_days REAL, home_b2b INTEGER, away_b2b INTEGER,
            home_prior_count INTEGER, away_prior_count INTEGER,
            home_moneyline_hardrockbet_fl REAL, away_moneyline_hardrockbet_fl REAL,
            home_moneyline_kalshi REAL, away_moneyline_kalshi REAL,
            market_prob_home_devigged REAL, n_books_used INTEGER,
            edge_home REAL, home_favored_by_model INTEGER,
            lock_timestamp_utc TEXT,
            features_sha256 TEXT, odds_sha256 TEXT, odds_csv_path TEXT,
            graded INTEGER DEFAULT 0, note TEXT,
            UNIQUE(event_id)
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

    if not args.odds.exists():
        raise SystemExit(f"Odds CSV not found: {args.odds}")
    odds_bytes = args.odds.read_bytes()
    odds_sha256 = hashlib.sha256(odds_bytes).hexdigest()

    today = dt.datetime.now(dt.timezone.utc).date()
    season_end_year = current_season_end_year(today)

    # Load the real odds file BEFORE fetching the ESPN schedule, purely to
    # find how many days out its furthest real kickoff is -- see the REAL
    # BUG note above. This is the actual fix for a real bug caught on the
    # user's first real run: odds were posted ~29 days out, but a fixed
    # 4-day ESPN lookahead meant none of those games had a schedule row to
    # match against.
    odds_by_key = load_odds(args.odds)
    max_kickoff_days_out = 0
    for book_rows in odds_by_key.values():
        for br in book_rows:
            try:
                kdt = dt.datetime.fromisoformat(br["kickoff_time"].replace("Z", "+00:00"))
            except ValueError:
                continue
            max_kickoff_days_out = max(max_kickoff_days_out, (kdt.date() - today).days)
    effective_lookahead = max(LOOKAHEAD_DAYS, max_kickoff_days_out + LOOKAHEAD_BUFFER_DAYS)
    if effective_lookahead > LOOKAHEAD_DAYS:
        print(f"Odds file's furthest real kickoff is {max_kickoff_days_out}d out -- widening the ESPN "
              f"schedule fetch to {effective_lookahead}d ahead (default LOOKAHEAD_DAYS={LOOKAHEAD_DAYS}).")

    current_csv_path, _ = fetch_current_season(today, season_end_year, lookahead_days=effective_lookahead)

    print("Loading all seasons (5 validated historical + current season-to-date)...")
    frames = []
    for season in HISTORICAL_SEASONS:
        path = DATA_DIR / f"nba_games_raw_season{season}.csv"
        if not path.exists():
            sys.exit(f"Missing validated historical file {path}. Fetch it first with nba_espn_scoreboard_fetch_v1.py.")
        df = pd.read_csv(path)
        df["season"] = season
        frames.append(df)
    current_df = pd.read_csv(current_csv_path)
    current_df["season"] = season_end_year
    if season_end_year not in HISTORICAL_SEASONS:
        frames.append(current_df)
    else:
        frames[-1] = current_df  # current season IS one of the "historical" files (re-fetched fresh with lookahead)
    games = pd.concat(frames, ignore_index=True)
    games = games[games["season_type"] == 2].copy()  # regular season only, matching the prereg's scope decision
    games["date_utc"] = pd.to_datetime(games["date_utc"], utc=True)
    games["played"] = games["played"].astype(str).str.strip().str.lower().isin(["true", "1"])

    feat = build_features(games)
    model = fit_model_c(feat)

    # odds_by_key was already loaded above (needed early, to size the ESPN
    # lookahead window) -- reused here rather than re-reading the file.
    print(f"\nLoaded odds for {len(odds_by_key)} real game(s) from {args.odds}")

    # Match each real odds event to a real ESPN schedule row by team pair +
    # closest kickoff timestamp -- never by date label (see module docstring).
    schedule = feat.reset_index()[["event_id", "date_utc", "home_team", "away_team"]].copy()
    schedule["home_code"] = schedule["home_team"].map(resolve_code)
    schedule["away_code"] = schedule["away_team"].map(resolve_code)

    lock_time = dt.datetime.now(dt.timezone.utc)
    print(f"\nLock timestamp: {lock_time.isoformat()}")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    n_logged, n_skipped = 0, 0
    skip_reasons = []

    for join_key, book_rows in sorted(odds_by_key.items()):
        home_code = book_rows[0]["home_team"]
        away_code = book_rows[0]["away_team"]
        kickoff_time = book_rows[0]["kickoff_time"]
        kickoff_dt = dt.datetime.fromisoformat(kickoff_time.replace("Z", "+00:00"))

        candidates = schedule[(schedule["home_code"] == home_code) & (schedule["away_code"] == away_code)].copy()
        if candidates.empty:
            skip_reasons.append((join_key, "no ESPN schedule row for this team pair (yet) -- ESPN may not have listed it, or the lookahead window is too short"))
            n_skipped += 1
            continue
        candidates["kdist"] = (candidates["date_utc"] - pd.Timestamp(kickoff_dt)).abs().dt.total_seconds()
        match = candidates.loc[candidates["kdist"].idxmin()]
        if match["kdist"] > KICKOFF_MATCH_TOLERANCE_SECONDS:
            skip_reasons.append((join_key, f"closest ESPN kickoff is {match['kdist']:.0f}s away, over the {KICKOFF_MATCH_TOLERANCE_SECONDS}s tolerance"))
            n_skipped += 1
            continue

        event_id = match["event_id"]
        row = feat.loc[event_id]

        if row["home_prior_count"] < MIN_HISTORY or row["away_prior_count"] < MIN_HISTORY:
            skip_reasons.append((join_key, f"insufficient history (home_n={row['home_prior_count']}, away_n={row['away_prior_count']}, need >={MIN_HISTORY})"))
            n_skipped += 1
            continue
        feat_cols = row[FEATURES_C]
        if feat_cols.isna().any():
            skip_reasons.append((join_key, "a required live feature came back NaN -- refusing to impute/fabricate"))
            n_skipped += 1
            continue

        try:
            _require_pregame(lock_time, kickoff_time)
        except ValueError as e:
            skip_reasons.append((join_key, str(e)))
            n_skipped += 1
            continue

        feat_row = pd.DataFrame([row[FEATURES_C].to_dict()])[FEATURES_C]
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
            skip_reasons.append((join_key, "no usable book odds for this game (bookmaker not hardrockbet_fl/kalshi)"))
            n_skipped += 1
            continue

        market_prob_home = sum(devig_probs) / len(devig_probs)
        edge_home = model_prob_home - market_prob_home

        db_row = {
            "event_id": str(event_id), "season": int(row["season"]), "kickoff_time": kickoff_time,
            "home_team": row["home_team"], "away_team": row["away_team"],
            "model_version": MODEL_VERSION, "model_prob_home": model_prob_home,
            "home_trail10_ppg": row["home_trail10_ppg"], "home_trail10_papg": row["home_trail10_papg"],
            "away_trail10_ppg": row["away_trail10_ppg"], "away_trail10_papg": row["away_trail10_papg"],
            "home_trail10_winpct": row["home_trail10_winpct"], "away_trail10_winpct": row["away_trail10_winpct"],
            "home_rest_days": row["home_rest_days"], "away_rest_days": row["away_rest_days"],
            "home_b2b": int(row["home_b2b"]), "away_b2b": int(row["away_b2b"]),
            "home_prior_count": int(row["home_prior_count"]), "away_prior_count": int(row["away_prior_count"]),
            "home_moneyline_hardrockbet_fl": book_ml["hardrockbet_fl"].get("home"),
            "away_moneyline_hardrockbet_fl": book_ml["hardrockbet_fl"].get("away"),
            "home_moneyline_kalshi": book_ml["kalshi"].get("home"),
            "away_moneyline_kalshi": book_ml["kalshi"].get("away"),
            "market_prob_home_devigged": market_prob_home, "n_books_used": len(devig_probs),
            "edge_home": edge_home, "home_favored_by_model": int(edge_home > 0),
            "lock_timestamp_utc": lock_time.isoformat(),
            "features_sha256": features_sha256, "odds_sha256": odds_sha256, "odds_csv_path": str(args.odds),
            "graded": 0, "note": None,
        }
        try:
            cols = list(db_row.keys())
            placeholders = ",".join("?" for _ in cols)
            conn.execute(f"INSERT INTO picks ({','.join(cols)}) VALUES ({placeholders})", [db_row[c] for c in cols])
            conn.commit()
            n_logged += 1
            print(f"  LOGGED {event_id}: {row['away_team']} @ {row['home_team']} "
                  f"model_p_home={model_prob_home:.3f} market_p_home={market_prob_home:.3f} edge={edge_home:+.3f}")
        except sqlite3.IntegrityError as e:
            skip_reasons.append((join_key, f"DB write failed (likely already logged): {e}"))
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
