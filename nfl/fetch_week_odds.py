#!/usr/bin/env python3
"""
Fetch LIVE pregame odds via the project's existing OddsAPIProvider (The
Odds API), normalize them with the project's own normalize_event logic
(imported, not reimplemented), match each event to its nflverse game_id
by team names + kickoff date, restrict to a single target week, and write
a CSV in the EXACT schema MODEL_1_1_PROSPECTIVE_GUARDED.py --lock expects
for --odds:
    game_id, bookmaker, kickoff_time, snapshot_time, home_team, away_team,
    market_home_margin, home_moneyline, away_moneyline, home_spread_odds,
    away_spread_odds

Requires ODDS_API_KEY (or THE_ODDS_API_KEY) in the environment -- this
project already keeps it in NFL_AI_MODEL_1_BRAIN/.env; load that file
into your shell environment first, e.g.:
    export $(grep -v '^#' NFL_AI_MODEL_1_BRAIN/.env | xargs)

This calls a metered external API (quota shown in
NFL_AI_MODEL_1_BRAIN/live/sportsbook/SPORTSBOOK_LIVE_GATE_V63_3.json as of
its last fetch) -- each run consumes real API quota, so run it once,
close to when you intend to lock, not repeatedly.

Usage:
    python3 SUPPORT/fetch_week_odds.py --week 3 --season 2026 \
        --out NFL_AI_MODEL_1_0/live/sportsbook/WEEK3_ODDS_FOR_LOCK.csv
"""
import argparse
import sys
from pathlib import Path

import pandas as pd


def _require_pregame_local(snapshot_time, kickoff_time):
    import datetime as _dt
    s = _dt.datetime.fromisoformat(str(snapshot_time).replace("Z", "+00:00"))
    k = _dt.datetime.fromisoformat(str(kickoff_time).replace("Z", "+00:00"))
    if s >= k:
        raise ValueError("Pregame guard failed: snapshot_time must be before kickoff_time")


ROOT = Path.home() / "Desktop" / "Sport Bet"
sys.path.insert(0, str(ROOT / "NFL_AI_MODEL_1_0"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--week", type=int, required=True)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--bookmakers", type=str, default="hardrockbet_fl,kalshi",
                         help="Comma-separated Odds API bookmaker keys to restrict to. Default: "
                              "hardrockbet_fl,kalshi -- the only two accounts actually in use. "
                              "Passing bookmakers directly (vs. the default 'us' region) is also "
                              "cheaper: up to 10 named bookmakers counts as a single region-equivalent.")
    args = parser.parse_args()

    from data.providers.live_providers import OddsAPIProvider

    try:
        import nflreadpy as nfl
    except ImportError as e:
        raise SystemExit("Install nflreadpy first: python3 -m pip install nflreadpy") from e

    sched = nfl.load_schedules([args.season])
    if hasattr(sched, "to_pandas"):
        sched = sched.to_pandas()
    week_games = sched[sched.week == args.week].copy()
    if week_games.empty:
        raise SystemExit(f"No games found for season {args.season} week {args.week} in the nflverse schedule.")

    # Build a lookup from (home_team_name, away_team_name) is not directly
    # available from nflverse abbreviations vs The Odds API full names, so
    # match on kickoff date + team abbreviation membership instead: nflverse
    # game_id already encodes season_week_AWAY_HOME by abbreviation. We match
    # The Odds API's full team names to nflverse abbreviations via a small
    # explicit map built from nflverse's own team description table.
    teams = nfl.load_teams()
    if hasattr(teams, "to_pandas"):
        teams = teams.to_pandas()
    if "team_abbr" not in teams.columns:
        raise SystemExit("nflreadpy load_teams() schema unexpected; cannot map team names to abbreviations.")

    # nflreadpy's team table carries one row per abbreviation a franchise has
    # ever used (e.g. the Rams appear as LA, LAR, AND the historical STL, all
    # sharing the same team_name/team_nick). A plain name->abbr dict silently
    # collides on these and keeps whichever row iteration happens to visit
    # last -- confirmed to actually pick the wrong one (LAR) for a live 2026
    # game whose own schedule row uses LA, causing every event for that game
    # to be silently dropped as "not in this week" even though it was.
    # Restricting to abbreviations that actually appear in THIS SEASON's own
    # schedule removes the ambiguity for every franchise, not just the Rams.
    active_abbrs = set(sched["home_team"].astype(str)) | set(sched["away_team"].astype(str))

    name_to_abbr = {}
    for _, row in teams.iterrows():
        abbr = row["team_abbr"]
        if abbr not in active_abbrs:
            continue
        for col in ("team_name", "team_nick", "team_full_name"):
            if col in teams.columns and isinstance(row.get(col), str):
                name_to_abbr[row[col]] = abbr

    def resolve_abbr(full_name):
        if full_name in name_to_abbr:
            return name_to_abbr[full_name]
        # fallback: match by last word (nickname) e.g. "Atlanta Falcons" -> "Falcons"
        nick = full_name.split()[-1]
        for _, row in teams.iterrows():
            if row.get("team_nick") == nick and row["team_abbr"] in active_abbrs:
                return row["team_abbr"]
        return None

    week_ids = {}
    for _, row in week_games.iterrows():
        week_ids[(row.away_team, row.home_team)] = row.game_id

    provider = OddsAPIProvider()
    events = provider.fetch_nfl(bookmaker=args.bookmakers)
    print(f"Fetched {len(events)} events from The Odds API (bookmakers={args.bookmakers}).", file=sys.stderr)

    def normalize_event_ml_only(event, bookmaker_key):
        """Fallback for a book that has h2h (moneyline) but no spreads market --
        plausible for an exchange like Kalshi, which trades event contracts, not
        traditional point-spread lines. V3/V4 only ever use moneyline, so a
        missing spread market is not a reason to drop the book's moneyline data
        entirely. Raises KeyError/ValueError the same way normalize_event does
        if even h2h is unavailable, so the caller's existing except clause still
        catches it and logs to `unmatched` rather than fabricating a price."""
        books = {b["key"]: b for b in event.get("bookmakers", [])}
        if bookmaker_key not in books:
            raise KeyError(f"{bookmaker_key} not available for event")
        b = books[bookmaker_key]
        markets = {m["key"]: m for m in b.get("markets", [])}
        if "h2h" not in markets:
            raise ValueError("Required h2h market unavailable")
        h = {x["name"]: x for x in markets["h2h"]["outcomes"]}
        home = event["home_team"]; away = event["away_team"]
        snapshot = b.get("last_update") or markets["h2h"].get("last_update")
        kickoff = event["commence_time"]
        _require_pregame_local(snapshot, kickoff)
        return {"kickoff_time": kickoff, "snapshot_time": snapshot, "home_team": home, "away_team": away,
                "home_moneyline": float(h[home]["price"]), "away_moneyline": float(h[away]["price"]),
                "market_home_margin": None, "home_spread_odds": None, "away_spread_odds": None}

    rows = []
    unmatched = []
    for event in events:
        away_abbr = resolve_abbr(event.get("away_team", ""))
        home_abbr = resolve_abbr(event.get("home_team", ""))
        if not away_abbr or not home_abbr:
            unmatched.append((event.get("away_team"), event.get("home_team"), "team name not resolved"))
            continue
        game_id = week_ids.get((away_abbr, home_abbr))
        if not game_id:
            continue  # not in the target week; skip silently, it belongs to another week
        for book in event.get("bookmakers", []):
            bookmaker_key = book["key"]
            try:
                norm = OddsAPIProvider.normalize_event(event, bookmaker_key)
            except (KeyError, ValueError) as e_full:
                try:
                    norm = normalize_event_ml_only(event, bookmaker_key)
                except (KeyError, ValueError) as e_ml:
                    unmatched.append((event.get("away_team"), event.get("home_team"),
                                       f"{bookmaker_key}: full={e_full}; ml_only={e_ml}"))
                    continue
            rows.append({
                "game_id": game_id,
                "bookmaker": bookmaker_key,
                "kickoff_time": norm["kickoff_time"],
                "snapshot_time": norm["snapshot_time"],
                "home_team": norm["home_team"],
                "away_team": norm["away_team"],
                "market_home_margin": norm["market_home_margin"],
                "home_moneyline": norm["home_moneyline"],
                "away_moneyline": norm["away_moneyline"],
                "home_spread_odds": norm.get("home_spread_odds"),
                "away_spread_odds": norm["away_spread_odds"],
            })

    if not rows:
        raise SystemExit(f"No odds rows matched week {args.week} games. Unmatched sample: {unmatched[:10]}")

    out = pd.DataFrame(rows)
    dupe = out.duplicated(subset=["game_id", "bookmaker"])
    if dupe.any():
        raise SystemExit("Duplicate game_id/bookmaker rows produced -- refusing to write.")

    out.to_csv(args.out, index=False)
    print(f"Wrote {len(out)} odds rows covering {out.game_id.nunique()} week-{args.week} games to {args.out}")
    if unmatched:
        print(f"NOTE: {len(unmatched)} events/bookmakers were skipped (not this week, or missing markets). "
              f"Sample: {unmatched[:5]}", file=sys.stderr)


if __name__ == "__main__":
    main()
