#!/usr/bin/env python3
"""nba_preseason_trial_v1.py -- NBA PRESEASON TRIAL RUN (dress rehearsal). Never counted, never bets.

Runs the frozen NBA Model C (same features, same training data, same thresholds as the live tracker) on
PRESEASON games, to rehearse the whole pipeline -- odds fetch, team/time matching, locking, grading and the
app export -- before the regular season opens. Preseason results are NOT evidence about the model: starters
play limited minutes and the features come from last season's final 10 games. Nothing here is ever added to
NBA_PROSPECTIVE_TRACKER_V1.sqlite3 or to any test.

  python3 nba_preseason_trial_v1.py          # grade finished trial picks, lock new ones (<= 48h), write export
  python3 nba_preseason_trial_v1.py --grade-only

Writes NBA_PRESEASON_TRIAL_V1.sqlite3 and preseason_trial_export.json next to itself. Costs 1 Odds API credit
per locking run, and only when ESPN lists preseason games in the window. Reads ODDS_API_KEY from the
environment; never prints or stores it.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

import nba_live_tracker_lock as L   # frozen live tracker: data paths, features, Model C fit, team codes

HERE = Path(__file__).resolve().parent
DB = HERE / "NBA_PRESEASON_TRIAL_V1.sqlite3"
OUT = HERE / "preseason_trial_export.json"
BOOKS = "hardrockbet_fl,kalshi,draftkings"
WINDOW_H = 48
TOP25, TOP50 = 0.6830, 0.6083          # NBA_SELECTIVE_COVERAGE_V1 cutoffs (same as the app)
TRIAL_NOTE = ("PRESEASON TRIAL -- pipeline dress rehearsal, never counted. Preseason starters play limited "
              "minutes and the model uses last season's final 10 games, so these picks say nothing about the "
              "model's real accuracy.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS picks (
  event_id TEXT PRIMARY KEY, kickoff_utc TEXT, home_team TEXT, away_team TEXT, home_code TEXT, away_code TEXT,
  model_prob_home REAL, market_prob_home REAL, n_books INTEGER, books_json TEXT, features_json TEXT,
  lock_utc TEXT, home_score INTEGER, away_score INTEGER, graded_utc TEXT);
CREATE TABLE IF NOT EXISTS runlog (run_utc TEXT, note TEXT);
"""


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_t(s):
    t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def odds_get(path, params):
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    url = "https://api.the-odds-api.com/v4" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "nba-preseason-trial/1.0"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def preseason_games(start, end):
    """ESPN preseason (season type 1) games with tip-off in (start, end], plus all completed ones for grading."""
    rows, d = [], start.date() - dt.timedelta(days=1)
    while d <= end.date():
        for ev in L.fetch_day(d, L.CACHE_DIR).get("events", []):
            if (ev.get("season") or {}).get("type") != 1:
                continue
            comp = (ev.get("competitions") or [{}])[0]
            sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
            if "home" not in sides or "away" not in sides:
                continue
            h, a = (sides["home"].get("team") or {}), (sides["away"].get("team") or {})
            rows.append({"event_id": str(ev.get("id")), "kickoff": parse_t(ev["date"]),
                         "home_team": h.get("displayName"), "away_team": a.get("displayName"),
                         "home_code": L.resolve_code(h.get("displayName") or ""),
                         "away_code": L.resolve_code(a.get("displayName") or "")})
        d += dt.timedelta(days=1)
    return [r for r in rows if r["home_code"] and r["away_code"]]


def espn_final(event_id, kickoff):
    for off in (0, -1, 1):
        day = (kickoff + dt.timedelta(days=off)).date()
        cache = L.CACHE_DIR / f"{day.strftime('%Y%m%d')}.json"
        if cache.exists() and day >= utcnow().date() - dt.timedelta(days=1):
            cache.unlink()  # recent day: re-fetch so final scores are current
        for ev in L.fetch_day(day, L.CACHE_DIR).get("events", []):
            if str(ev.get("id")) != str(event_id):
                continue
            comp = (ev.get("competitions") or [{}])[0]
            if not ((comp.get("status") or {}).get("type") or {}).get("completed"):
                return None
            s = {c.get("homeAway"): int(c.get("score", 0)) for c in comp.get("competitors", [])}
            return s.get("home"), s.get("away")
    return None


def load_regular():
    frames = []
    for season in L.HISTORICAL_SEASONS:
        df = pd.read_csv(L.DATA_DIR / f"nba_games_raw_season{season}.csv")
        df["season"] = season
        frames.append(df)
    g = pd.concat(frames, ignore_index=True)
    g = g[g["season_type"] == 2].copy()
    g["date_utc"] = pd.to_datetime(g["date_utc"], utc=True)
    g["played"] = g["played"].astype(str).str.strip().str.lower().isin(["true", "1"])
    g = g[g["played"]]   # only real, finished regular-season games feed the trailing features
    g["event_id"] = g["event_id"].astype(str)
    return g


def features_for(reg, game):
    """Trailing features for one preseason game, from regular-season games only (one game at a time so a
    team's other upcoming preseason games never enter the window)."""
    row = pd.DataFrame([{"event_id": "TRIAL_" + game["event_id"], "date_utc": pd.Timestamp(game["kickoff"]),
                         "season": 0, "home_team": game["home_team"], "away_team": game["away_team"],
                         "home_points": None, "away_points": None, "played": False, "season_type": 1}])
    feat = L.build_features(pd.concat([reg, row], ignore_index=True))
    return feat.loc["TRIAL_" + game["event_id"]]


def lock(c, now):
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        sys.exit("ODDS_API_KEY not set")
    games = [g for g in preseason_games(now, now + dt.timedelta(hours=WINDOW_H))
             if now < g["kickoff"] <= now + dt.timedelta(hours=WINDOW_H)
             and not c.execute("SELECT 1 FROM picks WHERE event_id=?", (g["event_id"],)).fetchone()]
    if not games:
        print("lock: no new preseason games in the next %dh" % WINDOW_H)
        return 0
    sports = {s["key"] for s in odds_get("/sports", {"apiKey": key})}   # free call
    sport = "basketball_nba_preseason" if "basketball_nba_preseason" in sports else "basketball_nba"
    events = odds_get(f"/sports/{sport}/odds", {"apiKey": key, "regions": "us", "markets": "h2h",
                                                 "oddsFormat": "american", "bookmakers": BOOKS})
    reg = load_regular()
    model = L.fit_model_c(L.build_features(reg))
    n = 0
    for g in games:
        ev = next((e for e in events if L.resolve_code(e["home_team"]) == g["home_code"]
                   and L.resolve_code(e["away_team"]) == g["away_code"]
                   and abs((parse_t(e["commence_time"]) - g["kickoff"]).total_seconds()) <= 3 * 3600), None)
        if ev is None:
            print(f"  skip {g['away_code']}@{g['home_code']}: no odds posted yet")
            continue
        books, probs = {}, []
        for b in ev.get("bookmakers", []):
            m = next((m for m in b.get("markets", []) if m["key"] == "h2h"), None)
            if not m:
                continue
            o = {x["name"]: x["price"] for x in m["outcomes"]}
            if ev["home_team"] in o and ev["away_team"] in o:
                hm, am = float(o[ev["home_team"]]), float(o[ev["away_team"]])
                books[b["key"]] = {"home": hm, "away": am}
                ph, pa = L.implied_prob(hm), L.implied_prob(am)
                probs.append(ph / (ph + pa))
        if not probs:
            print(f"  skip {g['away_code']}@{g['home_code']}: no usable book odds")
            continue
        row = features_for(reg, g)
        x = row[L.FEATURES_C]
        if x.isna().any():
            print(f"  skip {g['away_code']}@{g['home_code']}: a feature came back empty")
            continue
        p = float(model.predict_proba(pd.DataFrame([x.to_dict()])[L.FEATURES_C])[0, 1])
        c.execute("INSERT INTO picks (event_id, kickoff_utc, home_team, away_team, home_code, away_code, "
                  "model_prob_home, market_prob_home, n_books, books_json, features_json, lock_utc) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (g["event_id"], iso(g["kickoff"]), g["home_team"], g["away_team"], g["home_code"], g["away_code"],
                   p, sum(probs) / len(probs), len(probs), json.dumps(books, sort_keys=True),
                   json.dumps({k: (None if pd.isna(v) else float(v)) for k, v in x.items()}), iso(now)))
        n += 1
        print(f"  locked {g['away_code']}@{g['home_code']}: model P(home)={p:.3f}, market={sum(probs)/len(probs):.3f}")
    print(f"lock: {n} trial pick(s) from {sport}")
    return n


def grade(c, now):
    done = 0
    for eid, ko in c.execute("SELECT event_id, kickoff_utc FROM picks WHERE graded_utc IS NULL").fetchall():
        k = parse_t(ko)
        if k + dt.timedelta(hours=3) > now:
            continue
        r = espn_final(eid, k)
        if r and None not in r:
            c.execute("UPDATE picks SET home_score=?, away_score=?, graded_utc=? WHERE event_id=?", (r[0], r[1], iso(now), eid))
            done += 1
    print(f"grade: {done} trial game(s) graded")


def american(p_book):
    return None if p_book is None else int(round(p_book))


def export(c, now):
    docs = []
    pref = {"draftkings": "DraftKings", "hardrockbet_fl": "Hard Rock", "kalshi": "Kalshi"}
    for r in c.execute("SELECT * FROM picks ORDER BY kickoff_utc").fetchall():
        (eid, ko, hn, an, hc, ac, p, mp, nb, bj, fj, lu, hs, as_, gu) = r
        books = json.loads(bj)
        bk = next((b for b in ("draftkings", "hardrockbet_fl", "kalshi") if b in books), None)
        side = hc if p >= 0.5 else ac
        wp = max(p, 1 - p)
        tier = "top25" if wp >= TOP25 else ("top50" if wp >= TOP50 else None)
        market = {"book": pref.get(bk, bk), "moneyline_home": american(books[bk]["home"]),
                  "moneyline_away": american(books[bk]["away"])} if bk else {}
        base = {"game_id": "NBATRIAL_" + eid, "espn_event_id": eid, "home_team": hc, "away_team": ac,
                "home_team_name": hn, "away_team_name": an, "kickoff_time": ko, "game_date": ko[:10],
                "season": 2027, "sport": "NBA", "trial": True, "trial_label": "PRESEASON TRIAL (not counted)"}
        slate = dict(base, market=market, model={"available": True, "status": "locked", "winner_prob": wp,
                                                  "winner_side": side, "source_snapshot_time": lu,
                                                  "note": TRIAL_NOTE})
        resolved = hs is not None
        correct = None
        if resolved and tier and hs != as_:
            correct = (hs > as_) == (side == hc)
        dec = dict(base, locked_at=lu, decision={"action": "ML" if tier else "PASS", "prob": wp,
                                                 "side": side if tier else None, "tier": tier},
                   market_snapshot=market, spread_read=None, teaser_signal=None,
                   outcome={"resolved": resolved, "home_score": hs, "away_score": as_, "decision_correct": correct,
                            "spread_correct": None, "teaser_correct": None})
        for coll, data in (("slate", slate), ("decisions", dec)):
            data["export_hash"] = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
            docs.append({"collection": coll, "doc_id": "NBATRIAL_" + eid, "data": data})
    OUT.write_text(json.dumps({"sport": "NBA_TRIAL", "generated_at": iso(now), "n_docs": len(docs), "docs": docs},
                              indent=1, sort_keys=True))
    print(f"export: {len(docs)} doc(s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grade-only", action="store_true")
    a = ap.parse_args()
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    now = utcnow()
    grade(c, now)
    n = 0 if a.grade_only else lock(c, now)
    c.execute("INSERT INTO runlog VALUES (?,?)", (iso(now), f"grade-only={a.grade_only} locked={n}"))
    c.commit()
    export(c, now)


if __name__ == "__main__":
    main()
