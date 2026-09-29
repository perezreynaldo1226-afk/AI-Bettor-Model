#!/usr/bin/env python3
"""odds_books_probe.py -- one-off check of which sportsbooks The Odds API returns on this plan.
Reads ODDS_API_KEY from the environment (GitHub secret); never prints or saves it.
Writes odds_probe/books_available.json: per sport, which bookmaker keys quoted h2h odds and
on how many events, plus the plan's remaining request quota. No picks, no bets, no DB writes.
Cost: 4 regions x 1 market = 4 credits per sport, 12 total."""
import json, os, sys, urllib.request, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

KEY = os.environ.get("ODDS_API_KEY")
if not KEY:
    sys.exit("ODDS_API_KEY not set")
SPORTS = {"NFL": "americanfootball_nfl", "NBA": "basketball_nba", "MLB": "baseball_mlb"}
SHARP = {"pinnacle", "betonlineag", "lowvig", "circasports", "matchbook", "betfair_ex_eu", "betfair_ex_uk"}
out = {"checked_at": datetime.now(timezone.utc).isoformat(), "sports": {}}
for name, key in SPORTS.items():
    q = urllib.parse.urlencode({"apiKey": KEY, "regions": "us,us2,eu,uk", "markets": "h2h",
                                "oddsFormat": "american"})
    try:
        with urllib.request.urlopen(f"https://api.the-odds-api.com/v4/sports/{key}/odds?{q}", timeout=30) as r:
            events = json.load(r)
            out["requests_remaining"] = r.headers.get("x-requests-remaining")
            out["requests_used"] = r.headers.get("x-requests-used")
    except urllib.error.HTTPError as e:
        out["sports"][name] = {"error": f"HTTP {e.code}"}
        continue
    books = {}
    for ev in events:
        for b in ev.get("bookmakers", []):
            books[b["key"]] = books.get(b["key"], 0) + 1
    out["sports"][name] = {"events": len(events), "books": dict(sorted(books.items(), key=lambda x: -x[1])),
                           "sharp_books_present": sorted(SHARP & set(books))}
Path("odds_probe").mkdir(exist_ok=True)
Path("odds_probe/books_available.json").write_text(json.dumps(out, indent=1))
for n, s in out["sports"].items():
    print(n, s.get("events"), "events; sharp:", s.get("sharp_books_present"), "; books:", len(s.get("books", {})))
print("quota remaining:", out.get("requests_remaining"))
