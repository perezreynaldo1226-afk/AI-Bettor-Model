#!/usr/bin/env python3
"""
export_lineboard_nfl.py -- turns the NFL tracker database into the exact
Lineboard (DeVig.AI app) documents, the same way export_lineboard_mlb.py does
for baseball.

Reads only. Never changes PROSPECTIVE_TRACKER_V1_1.sqlite3; it writes one
file, lineboard_export.json, next to itself. A scheduled Claude task copies
changed documents from that file into the app's database.

Per game, it uses the LATEST lock in the tracker (the same snapshot the
grader treats as official) and produces:
  - a `slate` doc     (game card: locked market line + model read)
  - a `decisions` doc (the app's ML/PASS call, the informational spread read
    and teaser signal, and -- once graded -- the real outcome)

Decision rule is the app's own validated NFL cutoff, unchanged:
  winner prob >= 0.7194 -> ML top25, >= 0.6387 -> ML top50, else PASS
The spread read is information only (no demonstrated edge), exactly as the
app already labels it. It is exported only for locks written by
PROSPECTIVE_TRACKER_V1_5_LOG_PICKS.py or later (spread_input_convention set);
earlier locks fed the spread model the wrong sign, so their spread read is
left out rather than shown.

Spread numbers are in the sportsbook convention the app displays: spread_home
is the home point (negative = home favored). The model's own predicted line
is shown as a home point too: pred = point - spread_edge.

Teaser signal (information only): the underdog getting 2 to <3 points -> the
10-point band; 1.5 to <2 -> the 6-point band.

Outcomes use the final scores the grader stored (grades table). A push or a
tie is recorded as neither a hit nor a miss (null).

Usage:
    python3 export_lineboard_nfl.py
"""
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DB = SCRIPT_DIR / "PROSPECTIVE_TRACKER_V1_1.sqlite3"
OUT = SCRIPT_DIR / "lineboard_export.json"

CONF_TOP25, CONF_TOP50 = 0.7194, 0.6387
RECENT_DAYS = 10
BASE_DECIDER = "OLD_LAB28_43_HYBRID"


def num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def odds_int(x):
    v = num(x)
    return None if v is None else int(round(v))


def doc_hash(data):
    body = {k: v for k, v in data.items() if k != "synced_at"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def teaser_signal(point, home, away):
    if point is None or point == 0:
        return None
    dog_points, dog_team = (point, home) if point > 0 else (-point, away)
    if 2.0 <= dog_points < 3.0:
        return {"dogTeam": dog_team, "dogPoints": dog_points, "tease": 10, "strength": "strongest"}
    if 1.5 <= dog_points < 2.0:
        return {"dogTeam": dog_team, "dogPoints": dog_points, "tease": 6, "strength": "marginal"}
    return None


def hit(x):
    """>0 hit, <0 miss, 0 push (null)."""
    return None if x == 0 else bool(x > 0)


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(picks)")}
    has_conv = "spread_input_convention" in cols
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat().replace("+00:00", "Z")
    cutoff = now_dt - timedelta(days=RECENT_DAYS)

    rows = conn.execute("SELECT * FROM picks WHERE decider_version = ? ORDER BY lock_timestamp_utc",
                        (BASE_DECIDER,)).fetchall()
    latest = {}
    for r in rows:  # ordered by lock time, so the last one per game wins
        latest[r["game_id"]] = r
    grades = {r["pick_id"]: r for r in conn.execute("SELECT * FROM grades")}
    all_deciders = {}
    for r in conn.execute("SELECT game_id, decider_version, lock_timestamp_utc, action, side FROM picks "
                          "ORDER BY lock_timestamp_utc"):
        all_deciders.setdefault(r["game_id"], {})[r["decider_version"]] = (r["lock_timestamp_utc"], r["action"], r["side"])

    docs = []
    for gid, r in sorted(latest.items(), key=lambda kv: kv[1]["kickoff_time"]):
        kickoff = datetime.fromisoformat(str(r["kickoff_time"]).replace("Z", "+00:00"))
        g = grades.get(r["id"])
        if kickoff < cutoff and g is not None:
            continue
        season, week, away, home = gid.split("_", 3)
        p_home = float(r["winner_p_home"])
        side = home if p_home >= 0.5 else away
        wp = max(p_home, 1 - p_home)
        tier = "top25" if wp >= CONF_TOP25 else ("top50" if wp >= CONF_TOP50 else None)

        point = num(r["market_home_margin"])
        market = {"moneyline_home": odds_int(r["home_moneyline"]), "moneyline_away": odds_int(r["away_moneyline"]),
                  "spread_home": point, "spread_home_odds": odds_int(r["home_spread_odds"]),
                  "spread_away_odds": odds_int(r["away_spread_odds"])}

        fixed = has_conv and r["spread_input_convention"] is not None
        edge = num(r["spread_edge"])
        spread_read = None
        model = {"available": True, "status": "locked", "winner_prob": wp, "winner_side": side,
                 "source_snapshot_time": r["lock_timestamp_utc"]}
        if fixed and edge is not None and point is not None:
            sp_team = home if edge > 0 else away
            pred_point = point - edge
            spread_read = {"side": sp_team, "pred_home_margin": pred_point}
            model.update({"spread_side": sp_team, "pred_home_margin": pred_point})
            model["note"] = ("Locked pregame by the NFL tracker on GitHub (Model 1.0, frozen). Line shown is the one "
                             "the tracker locked. Spread read is information only -- no demonstrated edge.")
        else:
            model["note"] = ("Locked pregame by the NFL tracker (Model 1.0, frozen). This lock was made before the "
                             "spread sign fix (V1.5), so no spread read is shown for it; the moneyline read is "
                             "unaffected.")
        teaser = teaser_signal(point, home, away)

        outcome = {"resolved": False, "home_score": None, "away_score": None, "decision_correct": None,
                   "spread_correct": None, "teaser_correct": None}
        if g is not None and g["actual_home_score"] is not None:
            hs, as_ = float(g["actual_home_score"]), float(g["actual_away_score"])
            ahm = hs - as_
            outcome.update({"resolved": True, "home_score": int(hs), "away_score": int(as_),
                            "graded_source": "nflverse final score via the GitHub Actions grader"})
            if tier:
                outcome["decision_correct"] = hit(ahm if side == home else -ahm)
            if spread_read:
                cover = ahm + point
                outcome["spread_correct"] = hit(cover if spread_read["side"] == home else -cover)
            if teaser:
                dog_margin = ahm if teaser["dogTeam"] == home else -ahm
                outcome["teaser_correct"] = hit(dog_margin + teaser["dogPoints"] + teaser["tease"])

        tracker = {dv: {"action": a, "side": s} for dv, (ts, a, s) in sorted(all_deciders.get(gid, {}).items())
                   if ts == r["lock_timestamp_utc"]}
        base = {"game_id": gid, "home_team": home, "away_team": away, "kickoff_time": r["kickoff_time"],
                "season": int(season), "week": int(week), "sport": "NFL"}
        slate = dict(base, market=market, model=model, synced_at=now)
        decision = dict(base, locked_at=r["lock_timestamp_utc"],
                        decision={"action": "ML" if tier else "PASS", "prob": wp,
                                  "side": side if tier else None, "tier": tier},
                        market_snapshot=market, spread_read=spread_read, teaser_signal=teaser,
                        outcome=outcome, tracker_deciders=tracker)
        for coll, data in (("slate", slate), ("decisions", decision)):
            data["export_hash"] = doc_hash(data)
            docs.append({"collection": coll, "doc_id": gid, "data": data})

    n_res = sum(1 for d in docs if d["collection"] == "decisions" and d["data"]["outcome"]["resolved"])
    new_keys = sorted((d["collection"], d["doc_id"], d["data"]["export_hash"]) for d in docs)
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text())
            old_keys = sorted((d["collection"], d["doc_id"], d["data"]["export_hash"]) for d in old["docs"])
            if old_keys == new_keys:
                print(f"No changes: {len(docs)} docs ({len(docs)//2} games, {n_res} resolved) already exported.")
                return
        except (ValueError, KeyError):
            pass
    OUT.write_text(json.dumps({"sport": "NFL", "generated_at": now, "n_docs": len(docs), "docs": docs},
                              indent=1, sort_keys=True, default=str))
    print(f"Wrote {len(docs)} Lineboard docs ({len(docs)//2} games, {n_res} resolved) to {OUT}")


if __name__ == "__main__":
    main()
