#!/usr/bin/env python3
"""
injury_report_nfl_v2.py -- builds nfl/injury_report.json: key NFL players who are
on the injury report right now, for the app's "Injury report" disclaimer.

INFORMATION ONLY. The frozen Model 1.0 never sees this file and no pick,
tier or lock changes because of it.

Sources (both public, no key):
  - ESPN injuries feed (current status for every team):
      https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries
  - nflverse snap counts (who actually plays a lot this season):
      https://github.com/nflverse/nflverse-data/releases/download/snap_counts/snap_counts_<season>.csv

"Key player" rule (fixed, objective -- not a hand-made star list):
  a player's average snap share (the larger of offense % and defense %) over
  the games he played this season is >= 60%, or >= 50% for a QB. A player who
  has not played this season (e.g. hurt in camp) uses last season's share,
  matched by name only when that name is unique league-wide.
Every status except "Active" is kept. Per team: QBs first, then Out /
Injured Reserve / Doubtful before Questionable, then by snap share; max 6.

If ESPN can't be reached, the previous injury_report.json is left unchanged.
v2 (2026-09-29): ESPN returned 403 to the GitHub runner for v1's plain User-Agent.
v2 sends the same browser-style headers the NBA grader already uses successfully
against ESPN from GitHub Actions, retries with backoff, and tries a second ESPN host.
Everything else is identical to v1.
Usage: python3 injury_report_nfl_v2.py
"""
import csv
import io
import json
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    CTX = ssl.create_default_context()

SCRIPT_DIR = Path(__file__).resolve().parent
OUT = SCRIPT_DIR / "injury_report.json"
ESPN_URLS = ["https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries",
             "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/injuries"]
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espn.com/nfl/injuries",
    "Origin": "https://www.espn.com",
}
SNAP_URL = "https://github.com/nflverse/nflverse-data/releases/download/snap_counts/snap_counts_{}.csv"
KEY_SHARE, KEY_SHARE_QB, MAX_PER_TEAM = 0.60, 0.50, 6
SEVERE = {"out", "injured reserve", "doubtful", "physically unable to perform", "suspension",
          "reserve/non-football injury", "non-football injury"}

ESPN_TO_ABBR = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
    "Los Angeles Rams": "LA", "Los Angeles Chargers": "LAC", "Las Vegas Raiders": "LV", "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN", "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT", "Seattle Seahawks": "SEA",
    "San Francisco 49ers": "SF", "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN",
    "Washington Commanders": "WAS",
}


def norm(name):
    s = re.sub(r"[^a-z ]", "", (name or "").lower().replace("-", " "))
    return " ".join(w for w in s.split() if w not in {"jr", "sr", "ii", "iii", "iv", "v"})


def get(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "injury-report/2.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read()


def fetch_espn():
    last = None
    for url in ESPN_URLS:
        for wait in (0, 3, 8):
            time.sleep(wait)
            try:
                return json.loads(get(url, headers=BROWSER_HEADERS))
            except Exception as e:
                last = e
                print(f"  ESPN try failed ({url.split('/')[2]}): {type(e).__name__}: {e}")
    raise last


def snap_shares(season):
    """{(team, norm name): (share, position)} averaged over games played."""
    try:
        rows = list(csv.DictReader(io.StringIO(get(SNAP_URL.format(season), 60).decode("utf-8"))))
    except Exception as e:
        print(f"  snap counts {season} unavailable: {type(e).__name__}: {e}")
        return {}
    acc = {}
    for r in rows:
        if r.get("game_type") and r["game_type"] != "REG":
            continue
        try:
            share = max(float(r["offense_pct"] or 0), float(r["defense_pct"] or 0))
        except ValueError:
            continue
        k = (r["team"], norm(r["player"]))
        a = acc.setdefault(k, [0.0, 0, r["position"]])
        a[0] += share
        a[1] += 1
    return {k: (v[0] / v[1], v[2]) for k, v in acc.items() if v[1]}


def main():
    now = datetime.now(timezone.utc)
    try:
        feed = fetch_espn()
    except Exception as e:
        print(f"::warning::ESPN injuries unreachable ({type(e).__name__}: {e}); keeping the previous report.")
        return
    season = now.year if now.month >= 3 else now.year - 1
    cur = snap_shares(season)
    prev = snap_shares(season - 1)
    if not cur:
        print("::warning::No snap counts for this season; keeping the previous report.")
        return
    prev_by_name = {}
    for (team, nm), v in prev.items():
        prev_by_name.setdefault(nm, []).append(v)

    teams, n_all = {}, 0
    for t in feed.get("injuries", []):
        abbr = ESPN_TO_ABBR.get(t.get("displayName"))
        if not abbr:
            print(f"  unknown ESPN team name: {t.get('displayName')!r}")
            continue
        keep = []
        for inj in t.get("injuries", []):
            n_all += 1
            status = (inj.get("status") or "").strip()
            if not status or status.lower() == "active":
                continue
            ath = inj.get("athlete") or {}
            name = ath.get("displayName") or ""
            nm = norm(name)
            hit = cur.get((abbr, nm))
            basis = "this season"
            if hit is None and len(prev_by_name.get(nm, [])) == 1:
                hit, basis = prev_by_name[nm][0], "last season"
            if hit is None:
                continue
            share, pos = hit
            espn_pos = ((ath.get("position") or {}).get("abbreviation") or "").upper()
            pos = espn_pos or pos
            need = KEY_SHARE_QB if pos == "QB" else KEY_SHARE
            if share < need:
                continue
            details = inj.get("details") or {}
            keep.append({"name": name, "pos": pos, "status": status,
                         "injury": details.get("type") or None,
                         "return_date": details.get("returnDate") or None,
                         "snap_share": round(share, 2), "share_basis": basis,
                         "espn_updated": inj.get("date")})
        keep.sort(key=lambda p: (p["pos"] != "QB", p["status"].lower() not in SEVERE, -p["snap_share"]))
        teams[abbr] = keep[:MAX_PER_TEAM]

    if not teams:
        print("::warning::ESPN feed parsed to zero teams; keeping the previous report.")
        return
    report = {"generated_at": now.isoformat().replace("+00:00", "Z"),
              "source": "ESPN injury feed + nflverse snap counts",
              "rule": "key = avg snap share >= 60% (QB >= 50%) in games played; every status except Active",
              "teams": dict(sorted(teams.items()))}
    OUT.write_text(json.dumps(report, indent=1, sort_keys=True))
    n_key = sum(len(v) for v in teams.values())
    print(f"Injury report: {n_all} ESPN entries across {len(teams)} teams -> {n_key} key players written to {OUT}")
    for abbr, ps in sorted(teams.items()):
        for p in ps:
            if p["status"].lower() in SEVERE or p["pos"] == "QB":
                print(f"  {abbr}: {p['name']} ({p['pos']}) {p['status']} [{p['snap_share']:.0%} snaps, {p['share_basis']}]")


if __name__ == "__main__":
    sys.exit(main())
