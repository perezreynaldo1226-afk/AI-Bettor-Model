#!/usr/bin/env python3
"""
nba_live_tracker_grade_results.py -- grades ungraded rows in
NBA_PROSPECTIVE_TRACKER_V1.sqlite3 (written by nba_live_tracker_lock.py)
against real final ESPN scores, mirroring
MLB_AI_MODEL/SUPPORT/mlb_live_tracker_grade_results.py's design and
discipline.

WHY THIS SHOULD NOT REPEAT MLB'S DATE-BOUNDARY BUG: MLB's grader had to
re-match each pick to a real final game using a guessed "official date"
(the same ambiguous local-date concept that caused the original lock-time
bug), which is fragile. NBA does not have that problem in the WRITE
direction -- nba_live_tracker_lock.py already stored each pick's own real,
unambiguous ESPN event_id at lock time. So grading never re-matches by
date or team name at all: it only needs to confirm the STATUS of an
event_id it already knows, then read that one event's final score.

The only place a "date" is used here at all is to narrow which day(s) of
ESPN's scoreboard to fetch (an efficiency choice, not an identity claim --
see FETCH_WINDOW_DAYS below), and the match is always verified by exact
event_id equality against the fetched payload's own event["id"], never by
trusting that a game is on the day queried. If an event_id is not found
within the search window, this script says so explicitly and skips it
(graded=0 stays 0) rather than guessing.

Still logs MONITORING data only. No bet was ever placed on any of these
picks; nothing here is a recommendation. "model_favored_correct" grades
whether the side nba_live_tracker_lock.py flagged as having a positive
de-vigged edge (home_favored_by_model: 1 = model_prob_home > market's
de-vigged home probability, i.e. edge favors the home side; 0 = edge
favors the away side) actually won -- a monitoring signal about whether
the model's edge DIRECTION tracked real outcomes, not a claim that the
model "picked straight up winners" in a betting sense.

Usage:
    python3 nba_live_tracker_grade_results.py

Needs network access to ESPN's public scoreboard endpoint (same one
nba_live_tracker_lock.py already uses, and shares its on-disk cache dir --
grading a game already fetched during a lock run costs zero extra
requests).
"""
import datetime as dt
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data"
CACHE_DIR = DATA_DIR / "espn_raw_nba"  # shared cache with nba_live_tracker_lock.py
DB_PATH = SCRIPT_DIR / "NBA_PROSPECTIVE_TRACKER_V1.sqlite3"

GAMES_URL_TMPL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={d}&limit=1000"
REQUEST_DELAY_SECONDS = 0.35

# How many days on either side of a pick's own stored kickoff date to
# search for its event_id. This is purely a search-window efficiency
# knob -- correctness never depends on picking the "right" day, only on
# an exact event_id match somewhere inside the window. 2 is generous
# (NBA games never span a UTC-date ambiguity anywhere near this wide);
# widened automatically (see WIDEN_DAYS below) if a pick still isn't
# found, rather than silently giving up.
FETCH_WINDOW_DAYS = 1
WIDEN_DAYS = 3  # second attempt, only if the first narrow window misses


def fetch_day(day, cache_dir):
    """Identical fetch/retry/cache logic to nba_live_tracker_lock.py's
    fetch_day, duplicated per this project's self-contained-script
    convention. Reuses the same on-disk cache directory, so a day already
    fetched during a lock run is read from disk here, not re-requested."""
    cache_path = cache_dir / f"{day.strftime('%Y%m%d')}.json"
    if cache_path.exists():
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)
    url = GAMES_URL_TMPL.format(d=day.strftime("%Y%m%d"))
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.espn.com/nba/scoreboard",
        "Origin": "https://www.espn.com",
    }
    req = urllib.request.Request(url, headers=headers)
    max_attempts = 5
    backoff_seconds = [2, 5, 10, 20, 30]
    content = None
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                content = resp.read()
            break
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                sys.exit(f"HTTP {e.code} fetching {url} -- unexpected. Stopping.")
            print(f"  HTTP {e.code} on {day} (attempt {attempt + 1}/{max_attempts}) -- retrying...")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print(f"  {type(e).__name__}: {e} on {day} (attempt {attempt + 1}/{max_attempts}) -- retrying...")
        if attempt < max_attempts - 1:
            time.sleep(backoff_seconds[attempt])
    if content is None:
        sys.exit(f"Gave up on {day} after {max_attempts} attempts, all transient network errors. Re-run the same command.")
    payload = json.loads(content)
    cache_dir.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    time.sleep(REQUEST_DELAY_SECONDS)
    return payload


def find_event(event_id, kickoff_dt, window_days):
    """Search ESPN scoreboard days within +/-window_days of kickoff_dt's
    UTC date for an event whose own event["id"] exactly equals event_id.
    Returns the raw event dict, or None if not found in the window --
    never guesses, never returns a different event for the same team
    pair."""
    center = kickoff_dt.date()
    for offset in range(-window_days, window_days + 1):
        day = center + dt.timedelta(days=offset)
        payload = fetch_day(day, CACHE_DIR)
        for event in payload.get("events", []):
            if str(event.get("id")) == str(event_id):
                return event
    return None


def parse_final(event):
    """Given a raw ESPN event dict already confirmed to match the target
    event_id, return (played, home_points, away_points) or (False, None,
    None) if not yet final. Structurally identical to
    nba_live_tracker_lock.py's parse_day row-building, just for one event
    instead of a whole day's list."""
    competition = (event.get("competitions") or [{}])[0]
    competitors = {side.get("homeAway"): side for side in competition.get("competitors", [])}
    home, away = competitors.get("home"), competitors.get("away")
    status = competition.get("status", {}).get("type", {})
    played = bool(status.get("completed"))
    if not played or home is None or away is None:
        return played, None, None, status.get("detail")
    return True, int(home.get("score", -1)), int(away.get("score", -1)), status.get("detail")


def init_grades_table(conn):
    """Defensive only -- nba_live_tracker_lock.py already creates this
    table via init_db(). Never touches the picks table's schema."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS grades (
            pick_id INTEGER PRIMARY KEY,
            graded_at_utc TEXT, actual_home_score INTEGER, actual_away_score INTEGER,
            home_won INTEGER, model_favored_correct INTEGER
        )
    """)
    conn.commit()


def main():
    if not DB_PATH.exists():
        sys.exit(f"Tracker DB not found: {DB_PATH}. Run nba_live_tracker_lock.py at least once first.")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_grades_table(conn)

    ungraded = conn.execute(
        "SELECT id, event_id, kickoff_time, home_team, away_team, home_favored_by_model, edge_home "
        "FROM picks WHERE graded = 0 ORDER BY kickoff_time"
    ).fetchall()

    if not ungraded:
        print("No ungraded picks -- nothing to do.")
        conn.close()
        return

    print(f"{len(ungraded)} ungraded pick(s) to check.\n")

    now = dt.datetime.now(dt.timezone.utc)
    n_graded, n_not_final, n_not_found = 0, 0, 0

    for pick in ungraded:
        kickoff_dt = dt.datetime.fromisoformat(pick["kickoff_time"].replace("Z", "+00:00"))
        if kickoff_dt > now:
            print(f"  SKIP {pick['event_id']} ({pick['away_team']} @ {pick['home_team']}): "
                  f"kickoff {pick['kickoff_time']} hasn't happened yet.")
            continue

        event = find_event(pick["event_id"], kickoff_dt, FETCH_WINDOW_DAYS)
        if event is None:
            event = find_event(pick["event_id"], kickoff_dt, WIDEN_DAYS)
        if event is None:
            print(f"  NOT FOUND {pick['event_id']} ({pick['away_team']} @ {pick['home_team']}): "
                  f"no ESPN event with this exact id within +/-{WIDEN_DAYS}d of its stored kickoff. "
                  f"Left ungraded rather than guessing.")
            n_not_found += 1
            continue

        played, home_pts, away_pts, detail = parse_final(event)
        if not played:
            print(f"  NOT FINAL {pick['event_id']} ({pick['away_team']} @ {pick['home_team']}): status={detail!r}")
            n_not_final += 1
            continue

        home_won = int(home_pts > away_pts)
        home_favored = pick["home_favored_by_model"]
        model_favored_correct = home_won if home_favored == 1 else (1 - home_won)

        conn.execute(
            "INSERT OR REPLACE INTO grades "
            "(pick_id, graded_at_utc, actual_home_score, actual_away_score, home_won, model_favored_correct) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pick["id"], now.isoformat(), home_pts, away_pts, home_won, model_favored_correct),
        )
        conn.execute("UPDATE picks SET graded = 1 WHERE id = ?", (pick["id"],))
        conn.commit()
        n_graded += 1
        favored_side = pick["home_team"] if home_favored == 1 else pick["away_team"]
        result = "correct" if model_favored_correct else "wrong"
        print(f"  GRADED {pick['event_id']}: {pick['away_team']} {away_pts} @ {pick['home_team']} {home_pts}  "
              f"(edge favored {favored_side}, edge={pick['edge_home']:+.3f} -> {result})")

    conn.close()
    print(f"\n{'=' * 70}")
    print(f"DONE. Graded {n_graded}, not final yet {n_not_final}, not found {n_not_found}, "
          f"still ungraded {len(ungraded) - n_graded}.")
    print(f"DB: {DB_PATH}")


if __name__ == "__main__":
    main()
