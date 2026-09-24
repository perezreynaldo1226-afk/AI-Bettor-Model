#!/usr/bin/env python3
"""
fetch_mlb_odds_v3.py -- additive successor to fetch_mlb_odds.py (v2, which
fixed the date-boundary join_key bug; see that file's own docstring and
claude/mlb_tracker_date_boundary_bug.md). v2 is left untouched -- it is
paired with the live tracker and has already been used to fetch real odds
for logged picks. This is a new, additively-named file per this project's
versioning discipline, not an in-place edit.

Two things changed from v2, both purely additive -- v2's own default
behavior (--markets h2h, same columns, same join_key scheme) is reproduced
exactly when this script is run with no --markets flag:

1. OPTIONAL run-line (spread) market support, via --markets h2h,spreads.
   This exists because MLB_SPREAD_V1 (claude/mlb_spread_v1_run_line_test.md)
   found a real, if modest, run-line signal in Model C_spread (confidence
   tiers up to 73.00% accuracy at edge>=0.20, n=2,804) with NO way yet to
   compare it to a real sportsbook run line -- that report's own "what's
   next" section named this exact gap. When "spreads" is requested, four
   extra columns are appended per row: home_spread_point, home_spread_price,
   away_spread_point, away_spread_price (blank/omitted if that bookmaker
   does not carry a spread market for that event). The h2h columns and
   join_key are completely unchanged, so anything already reading v2's CSV
   output (mlb_live_tracker_lock_v3.py) keeps working unmodified if pointed
   at a v3 h2h-only CSV -- DictReader just sees the same known columns.

2. POSTSEASON READINESS NOTE, not a code change: this script was already
   postseason-safe before this rewrite and needed no season-phase logic.
   It never guesses a date, never assumes "regular season," and never
   filters by game type -- it just asks The Odds API for whatever
   baseball_mlb events fall inside [now, now+hours_ahead) and lets
   mlb_live_tracker_lock_v3.py (which DOES now filter gametype=='regular'
   for its own TRAINING data, per claude/mlb_playoff_readiness_v3_gametype_fix.md)
   resolve the real MLB game against statsapi.mlb.com. So a postseason
   game showing up in The Odds API's response would flow through this
   script with zero changes required. What is still UNVERIFIED, and can
   only be checked once real playoff games exist and from the user's own
   real Terminal (this sandboxed device shell cannot reach
   api.the-odds-api.com): whether The Odds API's hardrockbet_fl/kalshi
   bookmakers actually list postseason MLB markets at all, for either h2h
   or spreads. That is a live-data question, not a code question, and is
   recorded as an open PLAYOFF_READINESS_TODO item.

Requires ODDS_API_KEY (or THE_ODDS_API_KEY) in the environment:
    source NFL_AI_MODEL_1_BRAIN/.env

Usage (identical to v2 when --markets is omitted):
    python3 MLB_AI_MODEL/SUPPORT/fetch_mlb_odds_v3.py \
        --out MLB_AI_MODEL/live/sportsbook/MLB_ODDS_LIVE.csv \
        [--hours-ahead 48] [--markets h2h] [--markets h2h,spreads]

This calls a metered external API -- run it once per real lock, not
repeatedly.
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SPORT_KEY = "baseball_mlb"

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


def _require_pregame(snapshot_time, kickoff_time):
    s = dt.datetime.fromisoformat(str(snapshot_time).replace("Z", "+00:00"))
    k = dt.datetime.fromisoformat(str(kickoff_time).replace("Z", "+00:00"))
    if s >= k:
        raise ValueError("Pregame guard failed: snapshot_time must be before kickoff_time")


def resolve_code(full_name):
    if full_name in TEAM_NAME_TO_CODE:
        return TEAM_NAME_TO_CODE[full_name]
    nick = full_name.split()[-1]
    for name, code in TEAM_NAME_TO_CODE.items():
        if name.split()[-1] == nick:
            return code
    return None


def fetch_odds(api_key, bookmakers, markets):
    q = {"apiKey": api_key, "regions": "us", "markets": markets, "oddsFormat": "american",
         "dateFormat": "iso", "bookmakers": bookmakers}
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT_KEY}/odds?" + urllib.parse.urlencode(q)
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "mlb-odds-fetch/3.0"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def normalize_ml(event, bookmaker_key):
    books = {b["key"]: b for b in event.get("bookmakers", [])}
    if bookmaker_key not in books:
        raise KeyError(f"{bookmaker_key} not available for event")
    b = books[bookmaker_key]
    markets = {m["key"]: m for m in b.get("markets", [])}
    if "h2h" not in markets:
        raise ValueError("h2h market unavailable")
    h = {x["name"]: x for x in markets["h2h"]["outcomes"]}
    home = event["home_team"]; away = event["away_team"]
    if home not in h or away not in h:
        raise ValueError("h2h outcomes missing a side")
    snapshot = b.get("last_update") or markets["h2h"].get("last_update")
    kickoff = event["commence_time"]
    _require_pregame(snapshot, kickoff)
    return {"kickoff_time": kickoff, "snapshot_time": snapshot, "home_team_full": home,
            "away_team_full": away, "home_moneyline": float(h[home]["price"]),
            "away_moneyline": float(h[away]["price"])}


def normalize_spread(event, bookmaker_key):
    """Mirrors normalize_ml but for the 'spreads' (run line) market. Returns
    None (not an error) if this bookmaker simply doesn't carry a spread
    market for this event -- that's routine, not a data problem, since not
    every book posts run lines for every game."""
    books = {b["key"]: b for b in event.get("bookmakers", [])}
    if bookmaker_key not in books:
        return None
    b = books[bookmaker_key]
    mkts = {m["key"]: m for m in b.get("markets", [])}
    if "spreads" not in mkts:
        return None
    s = {x["name"]: x for x in mkts["spreads"]["outcomes"]}
    home = event["home_team"]; away = event["away_team"]
    if home not in s or away not in s:
        return None
    snapshot = b.get("last_update") or mkts["spreads"].get("last_update")
    kickoff = event["commence_time"]
    _require_pregame(snapshot, kickoff)
    return {
        "home_spread_point": float(s[home]["point"]), "home_spread_price": float(s[home]["price"]),
        "away_spread_point": float(s[away]["point"]), "away_spread_price": float(s[away]["price"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--bookmakers", type=str, default="hardrockbet_fl,kalshi")
    parser.add_argument("--markets", type=str, default="h2h",
                         help="Comma-separated Odds API market keys. Default 'h2h' reproduces "
                              "fetch_mlb_odds.py v2's exact output. Add 'spreads' for run-line "
                              "columns (home_spread_point/price, away_spread_point/price).")
    parser.add_argument("--hours-ahead", type=float, default=60.0,
                         help="Only keep events with kickoff within this many hours from now. "
                              "Purely a sanity filter, not used to build any identifier.")
    args = parser.parse_args()

    api_key = os.getenv("ODDS_API_KEY") or os.getenv("THE_ODDS_API_KEY")
    if not api_key:
        raise SystemExit("ODDS_API_KEY not set. Run: source NFL_AI_MODEL_1_BRAIN/.env  first.")

    want_spreads = "spreads" in [m.strip() for m in args.markets.split(",")]

    events = fetch_odds(api_key, args.bookmakers, args.markets)
    print(f"Fetched {len(events)} total MLB events from The Odds API "
          f"(bookmakers={args.bookmakers}, markets={args.markets}).", file=sys.stderr)

    now = dt.datetime.now(dt.timezone.utc)
    horizon = now + dt.timedelta(hours=args.hours_ahead)

    rows = []
    unmatched = []
    spread_skipped = 0
    for event in events:
        kickoff_iso = event.get("commence_time", "")
        try:
            kickoff_dt = dt.datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
        except ValueError:
            unmatched.append((event.get("away_team"), event.get("home_team"), "bad commence_time"))
            continue
        if not (now <= kickoff_dt <= horizon):
            continue
        away_code = resolve_code(event.get("away_team", ""))
        home_code = resolve_code(event.get("home_team", ""))
        if not away_code or not home_code:
            unmatched.append((event.get("away_team"), event.get("home_team"), "team name not resolved"))
            continue
        kickoff_compact = re.sub(r"[:\-]", "", kickoff_iso)
        join_key = f"{away_code}_{home_code}_{kickoff_compact}"
        for book in event.get("bookmakers", []):
            bookmaker_key = book["key"]
            try:
                norm = normalize_ml(event, bookmaker_key)
            except (KeyError, ValueError) as e:
                unmatched.append((event.get("away_team"), event.get("home_team"), f"{bookmaker_key}: {e}"))
                continue
            row = {
                "join_key": join_key, "bookmaker": bookmaker_key,
                "kickoff_time": norm["kickoff_time"], "snapshot_time": norm["snapshot_time"],
                "home_team": home_code, "away_team": away_code,
                "home_team_full": norm["home_team_full"], "away_team_full": norm["away_team_full"],
                "home_moneyline": norm["home_moneyline"], "away_moneyline": norm["away_moneyline"],
            }
            if want_spreads:
                sp = normalize_spread(event, bookmaker_key)
                if sp is None:
                    spread_skipped += 1
                    row.update({"home_spread_point": "", "home_spread_price": "",
                                "away_spread_point": "", "away_spread_price": ""})
                else:
                    row.update(sp)
            rows.append(row)

    if not rows:
        raise SystemExit(f"No odds rows in the next {args.hours_ahead}h. Unmatched sample: {unmatched[:10]}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    n_games = len({r["join_key"] for r in rows})
    print(f"Wrote {len(rows)} odds rows covering {n_games} games (next {args.hours_ahead}h) to {args.out}")
    if want_spreads:
        print(f"NOTE: {spread_skipped} (event,bookmaker) rows had no spread market available.", file=sys.stderr)
    if unmatched:
        print(f"NOTE: {len(unmatched)} events/bookmakers skipped. Sample: {unmatched[:5]}", file=sys.stderr)


if __name__ == "__main__":
    main()
