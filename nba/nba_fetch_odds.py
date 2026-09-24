#!/usr/bin/env python3
"""
nba_fetch_odds.py -- standalone live NBA odds fetcher (moneyline/h2h only),
mirroring MLB_AI_MODEL/SUPPORT/fetch_mlb_odds.py's design exactly, including
the lesson learned from that project's real date-boundary bug (see
claude/mlb_tracker_date_boundary_bug.md): this script never guesses a date
or builds a date-based game identifier. It writes one row per (event,
bookmaker) keyed by an UNAMBIGUOUS composite of team codes + the exact
kickoff UTC instant. Resolving which real ESPN game each row belongs to is
left entirely to nba_live_tracker_lock.py, which matches by team pair AND
closest real kickoff timestamp -- never by date label alone. NBA has an
easier version of MLB's problem to begin with: ESPN's own event_id and
UTC kickoff timestamp are unambiguous already, so there is no "official
date" concept to get wrong here.

Moneyline (h2h) only -- NBA_MONEYLINE_V1 never used a spread feature.

Requires ODDS_API_KEY (or THE_ODDS_API_KEY) in the environment:
    source ../../NFL_AI_MODEL_1_BRAIN/.env

Writes a CSV with columns:
    join_key, bookmaker, kickoff_time, snapshot_time, home_team, away_team,
    home_team_full, away_team_full, home_moneyline, away_moneyline

join_key = "{away_code}_{home_code}_{kickoff_iso_compact}" -- purely a
join key for nba_live_tracker_lock.py, not a real identifier on its own.

Usage:
    python3 nba_fetch_odds.py --out ../live/sportsbook/NBA_ODDS_LIVE.csv [--hours-ahead 72]

Default bookmakers are the same two real accounts this project already
uses for NFL/MLB (hardrockbet_fl, kalshi) -- whether either actually posts
NBA lines is not yet known and will be discovered empirically on the first
real run, the same way MLB discovered Hard Rock hadn't posted lines yet
for its first slate.

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

SPORT_KEY = "basketball_nba"

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


def fetch_odds(api_key, bookmakers):
    q = {"apiKey": api_key, "regions": "us", "markets": "h2h", "oddsFormat": "american",
         "dateFormat": "iso", "bookmakers": bookmakers}
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT_KEY}/odds?" + urllib.parse.urlencode(q)
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "nba-odds-fetch/1.0"})
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--bookmakers", type=str, default="hardrockbet_fl,kalshi")
    parser.add_argument("--hours-ahead", type=float, default=72.0,
                         help="Only keep events with kickoff within this many hours from now. "
                              "Purely a sanity filter, not used to build any identifier.")
    args = parser.parse_args()

    api_key = os.getenv("ODDS_API_KEY") or os.getenv("THE_ODDS_API_KEY")
    if not api_key:
        raise SystemExit("ODDS_API_KEY not set. Run: source NFL_AI_MODEL_1_BRAIN/.env  first.")

    events = fetch_odds(api_key, args.bookmakers)
    print(f"Fetched {len(events)} total NBA events from The Odds API (bookmakers={args.bookmakers}).", file=sys.stderr)

    now = dt.datetime.now(dt.timezone.utc)
    horizon = now + dt.timedelta(hours=args.hours_ahead)

    rows = []
    unmatched = []
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
            rows.append({
                "join_key": join_key, "bookmaker": bookmaker_key,
                "kickoff_time": norm["kickoff_time"], "snapshot_time": norm["snapshot_time"],
                "home_team": home_code, "away_team": away_code,
                "home_team_full": norm["home_team_full"], "away_team_full": norm["away_team_full"],
                "home_moneyline": norm["home_moneyline"], "away_moneyline": norm["away_moneyline"],
            })

    if not rows:
        raise SystemExit(f"No odds rows in the next {args.hours_ahead}h. Unmatched sample: {unmatched[:10]}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    n_games = len({r["join_key"] for r in rows})
    print(f"Wrote {len(rows)} odds rows covering {n_games} games (next {args.hours_ahead}h) to {args.out}")
    if unmatched:
        print(f"NOTE: {len(unmatched)} events/bookmakers skipped. Sample: {unmatched[:5]}", file=sys.stderr)


if __name__ == "__main__":
    main()
