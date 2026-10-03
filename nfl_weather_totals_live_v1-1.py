#!/usr/bin/env python3
"""NFL_WEATHER_TOTALS_LIVE_V1 -- live-forward test of the weather/totals lead.

Rebuilt 2026-09-29 (see NFL_WEATHER_TOTALS_LIVE_V1_AMENDMENT_1.md). Monitoring only:
never places bets.

Commands:
  fit       Fit the frozen model from nflverse games.csv (only if the model file
            is missing). Needs pandas + scikit-learn.
            python nfl_weather_totals_live_v1.py fit --games nfldata/data/games.csv
  snapshot  Log every NFL game starting in the next 3.5h (pick / pass / skip).
  grade     Grade logged picks whose games are final.

Data: Odds API totals (1 credit per call, only called when a game is in the
window), ESPN scoreboard (free), open-meteo geocoding + forecast (free).
The API key comes from the ODDS_API_KEY environment variable and is never printed.
"""
import argparse, datetime as dt, hashlib, json, math, os, sqlite3, sys, time
import urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "nfl_weather_totals_model_v1.json")
DB_PATH = os.path.join(HERE, "WEATHER_TOTALS_LIVE_V1.sqlite3")
PREREG_SHA = "18c885d58673b7c0dd11be6485cfc117d2c1770f49a373169a5dc64f60fe235b"
FEATURES = ["logit_q", "total_line", "wind_out", "wind_high", "temp_out", "cold", "roof_closed"]
WINDOW_H = 3.5
EV_MIN = 0.02
PRIMARY_BOOK = "pinnacle"
USER_BOOKS = ["draftkings", "hardrockbet_fl", "kalshi", "polymarket"]  # fix: Odds API key is hardrockbet_fl
# v1 pipeline fix (2026-10-03, logged): browser-style headers so ESPN does not 403 GitHub runners.
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36", "Accept": "application/json,text/plain,*/*",
      "Accept-Language": "en-US,en;q=0.9"}

US_STATES = {"AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California","CO":"Colorado",
 "CT":"Connecticut","DE":"Delaware","DC":"District of Columbia","FL":"Florida","GA":"Georgia","HI":"Hawaii",
 "ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa","KS":"Kansas","KY":"Kentucky","LA":"Louisiana",
 "ME":"Maine","MD":"Maryland","MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi",
 "MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire","NJ":"New Jersey",
 "NM":"New Mexico","NY":"New York","NC":"North Carolina","ND":"North Dakota","OH":"Ohio","OK":"Oklahoma",
 "OR":"Oregon","PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina","SD":"South Dakota",
 "TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont","VA":"Virginia","WA":"Washington",
 "WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming"}


# ---------------------------------------------------------------- helpers
def utcnow():
    return dt.datetime.now(dt.timezone.utc)

def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_time(s):
    s = s.replace("Z", "+00:00")
    if len(s) == 22 and s[16] == "+":  # ESPN style 2026-10-04T17:00+00:00
        s = s[:16] + ":00" + s[16:]
    return dt.datetime.fromisoformat(s).astimezone(dt.timezone.utc)

def get_json(url, params=None, tries=3):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 and e.code != 429:
                raise
            last = e
        except Exception as e:  # network blip
            last = e
        time.sleep(2 * (i + 1))
    raise last

def dec(american):
    a = float(american)
    return 1 + a / 100.0 if a > 0 else 1 + 100.0 / -a

def logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))

def file_sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

def features(q_over, total_line, indoor, wind_mph, temp_f):
    outdoor = not indoor
    wind = (wind_mph if wind_mph is not None else 0.0) if outdoor else 0.0
    temp = (temp_f if temp_f is not None else 60.0) if outdoor else 60.0
    return {"logit_q": logit(q_over), "total_line": float(total_line), "wind_out": wind,
            "wind_high": 1.0 if (outdoor and wind >= 15) else 0.0, "temp_out": temp,
            "cold": 1.0 if (outdoor and temp <= 32) else 0.0, "roof_closed": 0.0 if outdoor else 1.0}


# ---------------------------------------------------------------- fit
def cmd_fit(args):
    if os.path.exists(MODEL_PATH):
        print("Model already exists; refusing to refit (frozen).", file=sys.stderr)
        return 0
    import pandas as pd
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    g = pd.read_csv(args.games, low_memory=False)
    g = g[(g.season >= 2006) & (g.season <= 2025)]
    g = g.dropna(subset=["total_line", "over_odds", "under_odds", "home_score", "away_score"])
    tot = g.home_score + g.away_score
    g = g[tot != g.total_line].copy()
    tot = g.home_score + g.away_score
    po, pu = 1 / g.over_odds.map(dec), 1 / g.under_odds.map(dec)
    q = po / (po + pu)
    outdoor = g.roof.isin(["outdoors", "open"])
    wind = g.wind.fillna(0).where(outdoor, 0.0).astype(float)
    temp = g.temp.fillna(60).where(outdoor, 60.0).astype(float)
    X = pd.DataFrame({
        "logit_q": q.map(logit), "total_line": g.total_line.astype(float), "wind_out": wind,
        "wind_high": (outdoor & (wind >= 15)).astype(float), "temp_out": temp,
        "cold": (outdoor & (temp <= 32)).astype(float), "roof_closed": (~outdoor).astype(float)})[FEATURES]
    y = (tot > g.total_line).astype(int)
    sc = StandardScaler().fit(X.values)
    lr = LogisticRegression(C=1.0).fit(sc.transform(X.values), y.values)
    model = {
        "name": "nfl_weather_totals_model_v1", "features": FEATURES,
        "scaler_mean": [float(v) for v in sc.mean_], "scaler_scale": [float(v) for v in sc.scale_],
        "coef": [float(v) for v in lr.coef_[0]], "intercept": float(lr.intercept_[0]),
        "n_train": int(len(y)), "seasons": [2006, 2025], "over_rate": float(y.mean()),
        "source": "nflverse/nfldata games.csv", "source_commit": args.source_commit or "",
        "fitted_utc": iso(utcnow()), "prereg_sha256": PREREG_SHA}
    with open(MODEL_PATH, "w") as f:
        json.dump(model, f, indent=2, sort_keys=True)
    print("fit n=%d (prereg expected 5216) model_sha=%s" % (len(y), file_sha(MODEL_PATH)))
    if abs(len(y) - 5216) > 50:
        print("WARNING: training count differs from prereg's 5,216 -- check before first pick.", file=sys.stderr)
    return 0

def load_model():
    with open(MODEL_PATH) as f:
        m = json.load(f)
    assert m["features"] == FEATURES, "feature list mismatch"
    return m

def p_over(m, feats):
    z = m["intercept"]
    for i, k in enumerate(FEATURES):
        z += m["coef"][i] * (feats[k] - m["scaler_mean"][i]) / m["scaler_scale"][i]
    return 1 / (1 + math.exp(-z))


# ---------------------------------------------------------------- db
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS games (
  game_id TEXT PRIMARY KEY, snapshot_utc TEXT, kickoff_utc TEXT, season INTEGER, week INTEGER,
  home TEXT, away TEXT, venue TEXT, city TEXT, state TEXT, indoor INTEGER,
  lat REAL, lon REAL, wind_mph REAL, temp_f REAL,
  total_line REAL, over_odds REAL, under_odds REAL, q_over REAL, p_over REAL,
  ev_over REAL, ev_under REAL, pick TEXT, pick_odds REAL, skip_reason TEXT,
  user_books_json TEXT, model_sha TEXT,
  final_total REAL, result TEXT, profit_units REAL, graded_utc TEXT);
CREATE TABLE IF NOT EXISTS geocache (key TEXT PRIMARY KEY, lat REAL, lon REAL);
CREATE TABLE IF NOT EXISTS runlog (run_utc TEXT, cmd TEXT, note TEXT);
"""

def db():
    c = sqlite3.connect(DB_PATH)
    c.executescript(SCHEMA)
    return c

def check_model_frozen(c, sha):
    row = c.execute("SELECT v FROM meta WHERE k='model_sha'").fetchone()
    if row is None:
        c.execute("INSERT INTO meta VALUES ('model_sha', ?)", (sha,))
        c.execute("INSERT OR IGNORE INTO meta VALUES ('prereg_sha', ?)", (PREREG_SHA,))
        c.commit()
    elif row[0] != sha:
        raise SystemExit("Model file changed after first run (frozen %s, now %s). Stopping." % (row[0][:8], sha[:8]))


# ---------------------------------------------------------------- sources
def espn_events(dates):
    out = {}
    for d in dates:
        js = get_json("https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard", {"dates": d})
        for ev in js.get("events", []):
            out[ev["id"]] = ev
    return list(out.values())

def espn_info(ev):
    comp = ev["competitions"][0]
    teams = {c["homeAway"]: c for c in comp["competitors"]}
    venue = comp.get("venue", {}) or {}
    addr = venue.get("address", {}) or {}
    return {
        "game_id": ev["id"], "kickoff": parse_time(ev["date"]),
        "season": (ev.get("season") or {}).get("year"), "week": (ev.get("week") or {}).get("number"),
        "home": teams["home"]["team"]["displayName"], "away": teams["away"]["team"]["displayName"],
        "home_score": teams["home"].get("score"), "away_score": teams["away"].get("score"),
        "completed": bool(comp.get("status", ev.get("status", {})).get("type", {}).get("completed")),
        "venue": venue.get("fullName"), "indoor": venue.get("indoor"),
        "city": addr.get("city"), "state": addr.get("state"), "country": addr.get("country")}

def geocode(c, city, state, country):
    key = "|".join([city or "", state or "", country or ""])
    row = c.execute("SELECT lat, lon FROM geocache WHERE key=?", (key,)).fetchone()
    if row:
        return row
    js = get_json("https://geocoding-api.open-meteo.com/v1/search", {"name": city, "count": 10, "language": "en"})
    res = js.get("results") or []
    want = US_STATES.get((state or "").upper(), state)
    pick = None
    for r in res:
        if want and r.get("admin1") == want:
            pick = r; break
    if pick is None and country and country.upper() not in ("USA", "US", "UNITED STATES"):
        pick = next((r for r in res if (r.get("country") or "").lower().startswith(country.lower()[:4])), None)
    if pick is None and res:
        pick = res[0]
    if pick is None:
        return None
    c.execute("INSERT OR REPLACE INTO geocache VALUES (?,?,?)", (key, pick["latitude"], pick["longitude"]))
    return pick["latitude"], pick["longitude"]

def forecast(lat, lon, kickoff):
    js = get_json("https://api.open-meteo.com/v1/forecast", {
        "latitude": lat, "longitude": lon, "hourly": "temperature_2m,wind_speed_10m",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "UTC", "forecast_days": 3})
    h = js["hourly"]
    best, bi = None, None
    for i, t in enumerate(h["time"]):
        d = abs((dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc) - kickoff).total_seconds())
        if best is None or d < best:
            best, bi = d, i
    if bi is None or best > 5400:
        return None, None
    return h["wind_speed_10m"][bi], h["temperature_2m"][bi]

def odds_totals(t_from, t_to):
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        raise SystemExit("ODDS_API_KEY not set.")
    base = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"
    p = {"apiKey": key, "markets": "totals", "oddsFormat": "american",
         "commenceTimeFrom": iso(t_from), "commenceTimeTo": iso(t_to),
         "bookmakers": ",".join([PRIMARY_BOOK] + USER_BOOKS)}
    try:
        return get_json(base, p)
    except urllib.error.HTTPError as e:
        # An unknown book key must never cost us the Pinnacle price.
        print("odds call failed (HTTP %s); retrying Pinnacle only" % e.code, file=sys.stderr)
        p["bookmakers"] = PRIMARY_BOOK
        return get_json(base, p)

def book_totals(bm):
    for mk in bm.get("markets", []):
        if mk.get("key") == "totals":
            o = {x["name"].lower(): x for x in mk.get("outcomes", [])}
            if "over" in o and "under" in o and o["over"].get("point") == o["under"].get("point"):
                return {"line": float(o["over"]["point"]), "over": float(o["over"]["price"]),
                        "under": float(o["under"]["price"])}
    return None


# ---------------------------------------------------------------- snapshot
def cmd_snapshot(args):
    m = load_model()
    sha = file_sha(MODEL_PATH)
    c = db()
    check_model_frozen(c, sha)
    now = utcnow()
    horizon = now + dt.timedelta(hours=WINDOW_H)
    dates = sorted({(now + dt.timedelta(hours=h)).strftime("%Y%m%d") for h in (-6, 0, WINDOW_H)})
    games = [espn_info(e) for e in espn_events(dates)]
    todo = []
    for g in games:
        if not (now < g["kickoff"] <= horizon):
            continue
        row = c.execute("SELECT pick FROM games WHERE game_id=?", (g["game_id"],)).fetchone()
        if row and row[0] != "SKIP":
            continue  # first real snapshot is the locked one
        todo.append(g)
    if not todo:
        c.execute("INSERT INTO runlog VALUES (?,?,?)", (iso(now), "snapshot", "no games in window"))
        c.commit(); print("snapshot: no games in window"); return 0

    odds = odds_totals(now, horizon + dt.timedelta(minutes=5))
    n = {"OVER": 0, "UNDER": 0, "PASS": 0, "SKIP": 0}
    for g in todo:
        ev = next((o for o in odds if o.get("home_team") == g["home"] and o.get("away_team") == g["away"]), None)
        rec = {"game_id": g["game_id"], "snapshot_utc": iso(now), "kickoff_utc": iso(g["kickoff"]),
               "season": g["season"], "week": g["week"], "home": g["home"], "away": g["away"],
               "venue": g["venue"], "city": g["city"], "state": g["state"],
               "indoor": None if g["indoor"] is None else int(bool(g["indoor"])), "model_sha": sha}
        reason = None
        books = {b["key"]: book_totals(b) for b in (ev or {}).get("bookmakers", [])}
        pin = books.get(PRIMARY_BOOK)
        if ev is None:
            reason = "no odds event matched"
        elif pin is None:
            reason = "no Pinnacle totals price"
        elif g["indoor"] is None:
            reason = "ESPN indoor flag missing"
        wind = temp = None
        if reason is None and not g["indoor"]:
            ll = geocode(c, g["city"], g["state"], g["country"]) if g["city"] else None
            if ll is None:
                reason = "venue city not geocoded"
            else:
                rec["lat"], rec["lon"] = ll
                wind, temp = forecast(ll[0], ll[1], g["kickoff"])
                if wind is None:
                    reason = "no forecast hour near kickoff"
        if reason is None:
            po, pu = 1 / dec(pin["over"]), 1 / dec(pin["under"])
            q = po / (po + pu)
            p = p_over(m, features(q, pin["line"], bool(g["indoor"]), wind, temp))
            ev_o = p * (dec(pin["over"]) - 1) - (1 - p)
            ev_u = (1 - p) * (dec(pin["under"]) - 1) - p
            side = "OVER" if ev_o >= ev_u else "UNDER"
            best = max(ev_o, ev_u)
            pick = side if best >= EV_MIN else "PASS"
            user = {k: v for k, v in books.items() if k != PRIMARY_BOOK and v and v["line"] == pin["line"]}
            rec.update({"wind_mph": wind, "temp_f": temp, "total_line": pin["line"],
                        "over_odds": pin["over"], "under_odds": pin["under"], "q_over": q, "p_over": p,
                        "ev_over": ev_o, "ev_under": ev_u, "pick": pick,
                        "pick_odds": pin["over"] if pick == "OVER" else pin["under"] if pick == "UNDER" else None,
                        "user_books_json": json.dumps(user, sort_keys=True)})
        else:
            rec.update({"pick": "SKIP", "skip_reason": reason})
        n[rec["pick"]] += 1
        cols = ",".join(rec.keys())
        c.execute("INSERT OR REPLACE INTO games (%s) VALUES (%s)" % (cols, ",".join("?" * len(rec))),
                  list(rec.values()))
    c.execute("INSERT INTO runlog VALUES (?,?,?)", (iso(now), "snapshot", json.dumps(n)))
    c.commit()
    print("snapshot:", n)
    return 0


# ---------------------------------------------------------------- grade
def cmd_grade(args):
    c = db()
    now = utcnow()
    rows = c.execute("SELECT game_id, kickoff_utc, pick, total_line, pick_odds FROM games "
                     "WHERE result IS NULL AND pick IN ('OVER','UNDER','PASS')").fetchall()
    rows = [r for r in rows if parse_time(r[1]) + dt.timedelta(hours=3) < now]
    if not rows:
        print("grade: nothing due"); return 0
    dates = sorted({(parse_time(r[1]) + dt.timedelta(hours=h)).strftime("%Y%m%d") for r in rows for h in (-6, 0)})
    info = {e["id"]: espn_info(e) for e in espn_events(dates)}
    done = 0
    for gid, _, pick, line, odds in rows:
        g = info.get(gid)
        if not g or not g["completed"]:
            continue
        total = float(g["home_score"]) + float(g["away_score"])
        if pick == "PASS":
            res, prof = "PASS", 0.0
        elif total == line:
            res, prof = "PUSH", 0.0
        else:
            won = (total > line) == (pick == "OVER")
            res, prof = ("WIN", dec(odds) - 1) if won else ("LOSS", -1.0)
        c.execute("UPDATE games SET final_total=?, result=?, profit_units=?, graded_utc=? WHERE game_id=?",
                  (total, res, prof, iso(now), gid))
        done += 1
    c.execute("INSERT INTO runlog VALUES (?,?,?)", (iso(now), "grade", "graded %d" % done))
    c.commit()
    s = c.execute("SELECT COUNT(*), COALESCE(SUM(profit_units),0) FROM games WHERE result IN ('WIN','LOSS','PUSH')").fetchone()
    print("grade: graded %d now; running %d picks, %+.2f units" % (done, s[0], s[1]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    f = sp.add_parser("fit"); f.add_argument("--games", required=True); f.add_argument("--source-commit", default="")
    sp.add_parser("snapshot"); sp.add_parser("grade")
    a = ap.parse_args()
    return {"fit": cmd_fit, "snapshot": cmd_snapshot, "grade": cmd_grade}[a.cmd](a)

if __name__ == "__main__":
    sys.exit(main())
