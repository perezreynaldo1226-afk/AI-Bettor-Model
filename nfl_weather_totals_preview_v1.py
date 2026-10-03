#!/usr/bin/env python3
"""nfl_weather_totals_preview_v1.py -- PREVIEW of the weather over/under for upcoming NFL games.

NOT part of the NFL_WEATHER_TOTALS_LIVE_V1 test and never counted. It runs the same frozen model
and the same pick rule as the live script, but on games more than 3.5h away, using today's forecast
and today's Pinnacle price. The official live-test pick (made <= 3.5h before kickoff) replaces it.

Read-only on the live test: it never writes WEATHER_TOTALS_LIVE_V1.sqlite3 (geocodes go to an
in-memory cache). Output: WEATHER_TOTALS_PREVIEW_V1.json next to this file.
Costs 1 Odds API credit per run, only if there are NFL games in the window.

Usage: python nfl_weather_totals_preview_v1.py [--hours 36]
"""
import argparse
import datetime as dt
import json
import os
import sqlite3

import nfl_weather_totals_live_v1 as L

OUT = os.path.join(L.HERE, "WEATHER_TOTALS_PREVIEW_V1.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=36.0)
    a = ap.parse_args()
    m = L.load_model()
    sha = L.file_sha(L.MODEL_PATH)
    now = L.utcnow()
    start = now + dt.timedelta(hours=L.WINDOW_H)   # inside 3.5h the official live test takes over
    end = now + dt.timedelta(hours=a.hours)
    dates = sorted({(now + dt.timedelta(hours=h)).strftime("%Y%m%d") for h in range(0, int(a.hours) + 7, 6)})
    games = [L.espn_info(e) for e in L.espn_events(dates)]
    games = [g for g in games if start < g["kickoff"] <= end]
    out = {"generated_utc": L.iso(now), "model_sha": sha, "counted": False, "games": []}
    if games:
        odds = L.odds_totals(start, end + dt.timedelta(minutes=5))
        cache = sqlite3.connect(":memory:")
        cache.executescript(L.SCHEMA)
        for g in games:
            ev = next((o for o in odds if o.get("home_team") == g["home"] and o.get("away_team") == g["away"]), None)
            rec = {"game_id": g["game_id"], "kickoff_utc": L.iso(g["kickoff"]), "home": g["home"], "away": g["away"],
                   "venue": g["venue"], "indoor": None if g["indoor"] is None else bool(g["indoor"])}
            books = {b["key"]: L.book_totals(b) for b in (ev or {}).get("bookmakers", [])}
            pin = books.get(L.PRIMARY_BOOK)
            reason, wind, temp = None, None, None
            if ev is None:
                reason = "no odds yet"
            elif pin is None:
                reason = "no Pinnacle total yet"
            elif g["indoor"] is None:
                reason = "roof unknown"
            if reason is None and not g["indoor"]:
                ll = L.geocode(cache, g["city"], g["state"], g["country"]) if g["city"] else None
                if ll is None:
                    reason = "venue not geocoded"
                else:
                    wind, temp = L.forecast(ll[0], ll[1], g["kickoff"])
                    if wind is None:
                        reason = "no forecast yet"
            if reason is None:
                po, pu = 1 / L.dec(pin["over"]), 1 / L.dec(pin["under"])
                q = po / (po + pu)
                p = L.p_over(m, L.features(q, pin["line"], bool(g["indoor"]), wind, temp))
                ev_o = p * (L.dec(pin["over"]) - 1) - (1 - p)
                ev_u = (1 - p) * (L.dec(pin["under"]) - 1) - p
                side = "OVER" if ev_o >= ev_u else "UNDER"
                pick = side if max(ev_o, ev_u) >= L.EV_MIN else "PASS"
                rec.update({"wind_mph": wind, "temp_f": temp, "total_line": pin["line"], "over_odds": pin["over"],
                            "under_odds": pin["under"], "p_over": p, "ev_over": ev_o, "ev_under": ev_u,
                            "pick": pick,
                            "pick_odds": pin["over"] if pick == "OVER" else pin["under"] if pick == "UNDER" else None})
            else:
                rec.update({"pick": "SKIP", "reason": reason})
            out["games"].append(rec)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    tally = {}
    for r in out["games"]:
        tally[r["pick"]] = tally.get(r["pick"], 0) + 1
    print("preview (not counted):", len(out["games"]), "game(s)", tally)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
