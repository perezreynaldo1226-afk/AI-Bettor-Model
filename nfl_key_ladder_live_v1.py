#!/usr/bin/env python3
"""nfl_key_ladder_live_v1.py -- KEY_LADDER_LIVE_V1 (pre-registered live test). Monitoring only, never bets.

For NFL games starting within --hours, pulls the FULL spread + alternate-spread ladder from the user's books
(DraftKings, Hard Rock) and Pinnacle, prices every rung against the frozen historical table of real NFL final
margins (nfl_key_ladder_fair_table_v1.json), and logs the rungs whose price beats the real cover chance, with a
confidence tier:
  HIGH   : EV >= 0 even at the low end of the 95% range of the real cover chance, and Pinnacle doesn't disagree
  MEDIUM : EV >= +3% at the best estimate
  LEAN   : EV +1% to +3% at the best estimate (information only)
One official test bet per game = the highest-EV HIGH/MEDIUM rung (first snapshot only; never replaced).

  snapshot : log ladders for games starting within --hours (2 Odds API credits per game; none if no games)
  grade    : fill results from ESPN's scoreboard for finished games
Reads ODDS_API_KEY from the environment; never prints or stores it.
"""
import argparse, datetime as dt, hashlib, json, math, os, sqlite3, sys, urllib.parse

import nfl_weather_totals_live_v1 as W   # shared, tested helpers: get_json (browser headers), ESPN, time utils

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE_PATH = os.path.join(HERE, "nfl_key_ladder_fair_table_v1.json")
TABLE_SHA = "5c32c251adb90ec54cce647fc5987d4a5a6b6506de143ae48b2ef58c4fc2eea2"
DB_PATH = os.path.join(HERE, "KEY_LADDER_LIVE_V1.sqlite3")
SHARP = "pinnacle"
USER_BOOKS = ["draftkings", "hardrockbet_fl"]
MAX_DIST, MIN_N = 14.0, 200
KEYS = (-10, -7, -3, 3, 7, 10)

SCHEMA = """
CREATE TABLE IF NOT EXISTS rungs (
  event_id TEXT, snapshot_utc TEXT, kickoff_utc TEXT, home TEXT, away TEXT, main_home_point REAL,
  book TEXT, team TEXT, point REAL, price_american REAL, p_cover REAL, p_low REAL, p_high REAL, p_push REAL,
  n INTEGER, ev REAL, ev_low REAL, pin_agree INTEGER, crosses TEXT, tier TEXT, official INTEGER DEFAULT 0,
  final_home REAL, final_away REAL, result TEXT, profit_units REAL, graded_utc TEXT,
  PRIMARY KEY (event_id, book, team, point));
CREATE TABLE IF NOT EXISTS games (event_id TEXT PRIMARY KEY, snapshot_utc TEXT, kickoff_utc TEXT, home TEXT, away TEXT,
  main_home_point REAL, n_rungs_priced INTEGER, n_flagged INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS runlog (run_utc TEXT, cmd TEXT, note TEXT);
"""


def db():
    c = sqlite3.connect(DB_PATH)
    c.executescript(SCHEMA)
    return c


def load_table():
    raw = open(TABLE_PATH, "rb").read()
    if hashlib.sha256(raw).hexdigest() != TABLE_SHA:
        raise SystemExit("Frozen fair table changed; refusing to run.")
    return json.loads(raw)["table"]


def dec(a):
    a = float(a)
    return 1 + a / 100 if a > 0 else 1 + 100 / -a


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, c - h), min(1.0, c + h)


def cover_stats(table, s_home, team_is_home, point):
    """Real cover chance for `team` at `point` given the market's main home spread s_home (home favored > 0)."""
    key = f"{round(s_home * 2) / 2:.1f}"
    row = table.get(key)
    if row is None or row["n"] < MIN_N:
        return None
    win = push = n = 0
    for m, cnt in row["hist"].items():
        tm = int(m) if team_is_home else -int(m)
        v = tm + point
        n += cnt
        if v > 0:
            win += cnt
        elif v == 0:
            push += cnt
    np_ = n - push
    p, lo, hi = wilson(win, np_)
    return {"p": p, "lo": lo, "hi": hi, "push": push / n, "n": n}


def ev_of(p_np, p_push, d):
    return (1 - p_push) * (p_np * (d - 1) - (1 - p_np))


def ladder(bm, home, away):
    """{(team, point): american_price} from spreads + alternate_spreads."""
    out = {}
    for mk in bm.get("markets", []):
        if mk.get("key") not in ("spreads", "alternate_spreads"):
            continue
        for o in mk.get("outcomes", []):
            if o.get("name") in (home, away) and o.get("point") is not None:
                out[(o["name"], float(o["point"]))] = float(o["price"])
    return out


def crosses(main_pt, alt_pt):
    lo, hi = sorted((-main_pt, -alt_pt))   # margin thresholds the team must beat
    return ",".join(str(abs(k)) for k in KEYS if lo < k < hi) or ""


def cmd_snapshot(a):
    table = load_table()
    key = os.environ.get("ODDS_API_KEY") or sys.exit("ODDS_API_KEY not set")
    c = db()
    now = W.utcnow()
    end = now + dt.timedelta(hours=a.hours)
    base = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
    events = W.get_json(base + "/events", {"apiKey": key, "commenceTimeFrom": W.iso(now), "commenceTimeTo": W.iso(end)})
    todo = [e for e in events if not c.execute("SELECT 1 FROM games WHERE event_id=?", (e["id"],)).fetchone()]
    if not todo:
        c.execute("INSERT INTO runlog VALUES (?,?,?)", (W.iso(now), "snapshot", "no new games in window"))
        c.commit(); print("snapshot: no new games in window"); return 0
    tally = {"HIGH": 0, "MEDIUM": 0, "LEAN": 0, "games": 0}
    for e in todo:
        js = W.get_json(f"{base}/events/{e['id']}/odds", {"apiKey": key, "regions": "us", "oddsFormat": "american",
                        "markets": "spreads,alternate_spreads", "bookmakers": ",".join([SHARP] + USER_BOOKS)})
        home, away = js["home_team"], js["away_team"]
        books = {b["key"]: ladder(b, home, away) for b in js.get("bookmakers", [])}
        # main line: Pinnacle's main spread for the home team, else the first user book's
        mains = {}
        for b in js.get("bookmakers", []):
            for mk in b.get("markets", []):
                if mk.get("key") == "spreads":
                    for o in mk.get("outcomes", []):
                        if o.get("name") == home and o.get("point") is not None:
                            mains[b["key"]] = float(o["point"])
        main = mains.get(SHARP, next((mains[b] for b in USER_BOOKS if b in mains), None))
        note = None if main is not None else "no main spread"
        s_home = -main if main is not None else None
        pin = books.get(SHARP, {})
        rows, priced = [], 0
        for bk in USER_BOOKS:
            for (team, pt), price in books.get(bk, {}).items():
                if s_home is None:
                    continue
                is_home = team == home
                main_pt = main if is_home else -main
                if abs(pt - main_pt) > MAX_DIST:
                    continue
                st = cover_stats(table, s_home, is_home, pt)
                if st is None:
                    continue
                priced += 1
                d = dec(price)
                ev, ev_low = ev_of(st["p"], st["push"], d), ev_of(st["lo"], st["push"], d)
                pa = None
                opp = away if is_home else home
                if (team, pt) in pin and (opp, -pt) in pin:
                    it, io = 1 / dec(pin[(team, pt)]), 1 / dec(pin[(opp, -pt)])
                    fair = it / (it + io)
                    pa = 1 if fair * (d - 1) - (1 - fair) > 0 else 0
                tier = ("HIGH" if (ev_low >= 0 and pa != 0) else "MEDIUM" if ev >= 0.03 else
                        "LEAN" if ev >= 0.01 else None)
                if tier is None:
                    continue
                rows.append({"event_id": e["id"], "snapshot_utc": W.iso(now), "kickoff_utc": js["commence_time"],
                             "home": home, "away": away, "main_home_point": main, "book": bk, "team": team,
                             "point": pt, "price_american": price, "p_cover": st["p"], "p_low": st["lo"],
                             "p_high": st["hi"], "p_push": st["push"], "n": st["n"], "ev": ev, "ev_low": ev_low,
                             "pin_agree": pa, "crosses": crosses(main_pt, pt), "tier": tier, "official": 0})
        cands = [r for r in rows if r["tier"] in ("HIGH", "MEDIUM")]
        if cands:
            best = max(cands, key=lambda r: (r["tier"] == "HIGH", r["ev"]))
            best["official"] = 1
        for r in rows:
            c.execute("INSERT OR REPLACE INTO rungs (%s) VALUES (%s)" % (",".join(r), ",".join("?" * len(r))),
                      list(r.values()))
            tally[r["tier"]] += 1
        c.execute("INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?,?)",
                  (e["id"], W.iso(now), js["commence_time"], home, away, main, priced, len(rows), note))
        tally["games"] += 1
    c.execute("INSERT INTO runlog VALUES (?,?,?)", (W.iso(now), "snapshot", json.dumps(tally)))
    c.commit()
    print("snapshot:", tally)
    return 0


def cmd_grade(a):
    c = db()
    now = W.utcnow()
    rows = c.execute("SELECT DISTINCT event_id, kickoff_utc, home, away FROM rungs WHERE result IS NULL").fetchall()
    rows = [r for r in rows if W.parse_time(r[1]) + dt.timedelta(hours=4) < now]
    if not rows:
        print("grade: nothing due"); return 0
    dates = sorted({(W.parse_time(r[1]) + dt.timedelta(hours=h)).strftime("%Y%m%d") for r in rows for h in (-6, 0)})
    info = [W.espn_info(e) for e in W.espn_events(dates)]
    done = 0
    for eid, ko, home, away in rows:
        k = W.parse_time(ko)
        g = next((x for x in info if x["home"] == home and x["away"] == away
                  and abs((x["kickoff"] - k).total_seconds()) < 12 * 3600 and x["completed"]), None)
        if g is None:
            continue
        hs, as_ = float(g["home_score"]), float(g["away_score"])
        for rid, team, pt, price in c.execute("SELECT rowid, team, point, price_american FROM rungs WHERE event_id=?",
                                              (eid,)).fetchall():
            v = (hs - as_ if team == home else as_ - hs) + pt
            res, prof = ("PUSH", 0.0) if v == 0 else (("WIN", dec(price) - 1) if v > 0 else ("LOSS", -1.0))
            c.execute("UPDATE rungs SET final_home=?, final_away=?, result=?, profit_units=?, graded_utc=? WHERE rowid=?",
                      (hs, as_, res, prof, W.iso(now), rid))
        done += 1
    c.execute("INSERT INTO runlog VALUES (?,?,?)", (W.iso(now), "grade", "graded %d games" % done))
    c.commit()
    s = c.execute("SELECT COUNT(*), COALESCE(SUM(profit_units),0) FROM rungs WHERE official=1 AND result IN ('WIN','LOSS','PUSH')").fetchone()
    print("grade: %d game(s) graded; official bets so far %d, %+.2f units" % (done, s[0], s[1]))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["snapshot", "grade"])
    ap.add_argument("--hours", type=float, default=36.0)
    a = ap.parse_args()
    raise SystemExit(cmd_snapshot(a) if a.cmd == "snapshot" else cmd_grade(a))
