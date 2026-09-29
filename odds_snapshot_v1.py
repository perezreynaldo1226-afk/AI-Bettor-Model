#!/usr/bin/env python3
"""odds_snapshot_v1.py -- EV_SHARP_CLV_V1 data collection (prereg sha256 15ce8c80...a7a9).
Stores moneyline prices from Pinnacle (sharp reference) and the user's books into
ODDS_SNAPSHOTS_V1.sqlite3 next to this file. Monitoring data only: no picks, no bets.

Quota-aware: lists games with the free /events endpoint first and calls /odds (1 credit:
1 market, <=10 books) only when a sport has games inside the window.
Reads ODDS_API_KEY from the environment; never prints or stores it.

Usage: python3 odds_snapshot_v1.py --sports NBA,MLB [--hours 36]
"""
import argparse, json, os, sqlite3, sys, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path(__file__).resolve().parent / "ODDS_SNAPSHOTS_V1.sqlite3"
SPORTS = {"NFL": "americanfootball_nfl", "NBA": "basketball_nba", "MLB": "baseball_mlb"}
BOOKS = "pinnacle,draftkings,polymarket,hardrockbet_fl,kalshi"
API = "https://api.the-odds-api.com/v4/sports"


def get(url, params):
    q = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{q}", timeout=30) as r:
        return json.load(r), r.headers.get("x-requests-remaining")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sports", required=True)
    ap.add_argument("--hours", type=float, default=36)
    a = ap.parse_args()
    key = os.environ.get("ODDS_API_KEY") or sys.exit("ODDS_API_KEY not set")
    now = datetime.now(timezone.utc)
    fmt = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
    window = {"commenceTimeFrom": fmt(now), "commenceTimeTo": fmt(now + timedelta(hours=a.hours))}
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        snapshot_utc TEXT, sport TEXT, event_id TEXT, commence_utc TEXT, home_team TEXT, away_team TEXT,
        book TEXT, book_last_update TEXT, home_price REAL, away_price REAL,
        UNIQUE(snapshot_utc, event_id, book))""")
    conn.execute("CREATE TABLE IF NOT EXISTS runs (snapshot_utc TEXT, sport TEXT, n_events INTEGER, "
                 "n_rows INTEGER, credits_remaining TEXT, note TEXT)")
    snap = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    for s in [x.strip().upper() for x in a.sports.split(",") if x.strip()]:
        try:
            events, _ = get(f"{API}/{SPORTS[s]}/events", {"apiKey": key, **window})
            if not events:
                conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", (snap, s, 0, 0, None, "no games in window"))
                print(f"{s}: no games in the next {a.hours:.0f}h, skipped (no credit used)")
                continue
            odds, left = get(f"{API}/{SPORTS[s]}/odds", {"apiKey": key, "markets": "h2h", "oddsFormat": "decimal",
                                                       "bookmakers": BOOKS, **window})
        except urllib.error.HTTPError as e:
            conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", (snap, s, None, 0, None, f"HTTP {e.code}"))
            print(f"{s}: HTTP {e.code}")
            continue
        rows = 0
        for ev in odds:
            h, aw = ev["home_team"], ev["away_team"]
            for b in ev.get("bookmakers", []):
                for m in b.get("markets", []):
                    if m.get("key") != "h2h":
                        continue
                    pr = {o["name"]: o["price"] for o in m.get("outcomes", [])}
                    if h in pr and aw in pr:
                        conn.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
                                     (snap, s, ev["id"], ev["commence_time"], h, aw, b["key"],
                                      b.get("last_update"), pr[h], pr[aw]))
                        rows += 1
        conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", (snap, s, len(odds), rows, left, None))
        print(f"{s}: {len(odds)} games, {rows} book prices saved; credits left {left}")
    conn.commit()


if __name__ == "__main__":
    main()
