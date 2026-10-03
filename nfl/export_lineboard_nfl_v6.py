#!/usr/bin/env python3
"""
v2 (2026-09-29): adds an information-only `injury_report` to each slate doc, read from
injury_report.json (key players on the ESPN injury report). Picks are unchanged.

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
# v3 (2026-10-03): adds the NFL_WEATHER_TOTALS_LIVE_V1 pick (live test only) as `weather_total`.
# v4 (2026-10-03): before the official pick exists, shows the PREVIEW (WEATHER_TOTALS_PREVIEW_V1.json) on the
#   slate doc only, flagged preview=True. Previews never go on decisions docs, so they are never counted.
# v5 (2026-10-03): adds KEY_LADDER_LIVE_V1 alternate-spread rungs (key_ladder) -- top rungs on the slate doc,
#   the one official test bet per game on the decisions doc with outcome.key_ladder_correct.
# v6 (2026-10-03): adds LOWTOTAL_DOG_LIVE_V1 (`lowtotal_dog`): the underdog getting +3 to +7.5 on the tracker's
#   locked line when Pinnacle's official total (weather live-test snapshot) is <= 41. Decisions doc + outcome
#   .lowtotal_dog_correct only once the official total exists; before that, a preview on the slate doc only.
WX_DB = SCRIPT_DIR.parent / "WEATHER_TOTALS_LIVE_V1.sqlite3"
NAME2CODE = {"Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
             "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
             "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
             "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
             "Kansas City Chiefs": "KC", "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
             "Los Angeles Rams": "LA", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
             "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
             "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
             "San Francisco 49ers": "SF", "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
             "Tennessee Titans": "TEN", "Washington Commanders": "WAS"}


def load_weather():
    """{(home_code, away_code): [(kickoff_dt, weather_total, correct)]} for OVER/UNDER picks. Never fails the export."""
    out = {}
    if not WX_DB.exists():
        return out
    try:
        c = sqlite3.connect(WX_DB)
        c.row_factory = sqlite3.Row
        for r in c.execute("SELECT * FROM games WHERE pick IN ('OVER','UNDER')"):
            h, a = NAME2CODE.get(r["home"]), NAME2CODE.get(r["away"])
            if not h or not a:
                continue
            ko = datetime.fromisoformat(str(r["kickoff_utc"]).replace("Z", "+00:00"))
            if ko.tzinfo is None:
                ko = ko.replace(tzinfo=timezone.utc)
            ev = r["ev_over"] if r["pick"] == "OVER" else r["ev_under"]
            wt = {"side": r["pick"].lower(), "line": num(r["total_line"]), "odds": odds_int(r["pick_odds"]),
                  "ev": round(float(ev), 4) if ev is not None else None, "book": "Pinnacle",
                  "forecast_wind_mph": num(r["wind_mph"]), "forecast_temp_f": num(r["temp_f"]),
                  "indoor": bool(r["indoor"]) if r["indoor"] is not None else None,
                  "result": r["result"], "test": "NFL_WEATHER_TOTALS_LIVE_V1"}
            res = r["result"]
            correct = True if res == "WIN" else (False if res == "LOSS" else None)
            out.setdefault((h, a), []).append((ko, wt, correct))
    except sqlite3.Error as e:
        print(f"Weather DB not readable ({e}); exporting without weather picks.")
    return out


WX_PREVIEW = SCRIPT_DIR.parent / "WEATHER_TOTALS_PREVIEW_V1.json"


def load_weather_official_keys():
    """(home, away, kickoff) for every game the live test already logged (any pick, incl. PASS/SKIP)."""
    keys = []
    if not WX_DB.exists():
        return keys
    try:
        c = sqlite3.connect(WX_DB)
        for h, a, ko in c.execute("SELECT home, away, kickoff_utc FROM games"):
            h, a = NAME2CODE.get(h), NAME2CODE.get(a)
            if h and a:
                k = datetime.fromisoformat(str(ko).replace("Z", "+00:00"))
                keys.append((h, a, k if k.tzinfo else k.replace(tzinfo=timezone.utc)))
    except sqlite3.Error:
        pass
    return keys


def load_preview():
    out = {}
    if not WX_PREVIEW.exists():
        return out
    try:
        js = json.loads(WX_PREVIEW.read_text())
    except ValueError:
        return out
    for r in js.get("games", []):
        if r.get("pick") not in ("OVER", "UNDER", "PASS"):
            continue
        h, a = NAME2CODE.get(r.get("home")), NAME2CODE.get(r.get("away"))
        if not h or not a:
            continue
        ko = datetime.fromisoformat(str(r["kickoff_utc"]).replace("Z", "+00:00"))
        ev = r.get("ev_over") if r["pick"] == "OVER" else r.get("ev_under") if r["pick"] == "UNDER" else \
            max(r.get("ev_over") or -9, r.get("ev_under") or -9)
        wt = {"side": r["pick"].lower(), "line": num(r.get("total_line")), "odds": odds_int(r.get("pick_odds")),
              "ev": round(float(ev), 4) if ev is not None else None, "book": "Pinnacle",
              "forecast_wind_mph": num(r.get("wind_mph")), "forecast_temp_f": num(r.get("temp_f")),
              "indoor": r.get("indoor"), "preview": True, "preview_generated_utc": js.get("generated_utc"),
              "test": "PREVIEW (not counted)"}
        out.setdefault((h, a), []).append((ko if ko.tzinfo else ko.replace(tzinfo=timezone.utc), wt))
    return out


def preview_for(prev, official_keys, home, away, kickoff, now_dt):
    if kickoff <= now_dt:
        return None
    if any(h == home and a == away and abs((k - kickoff).total_seconds()) <= 12 * 3600 for h, a, k in official_keys):
        return None  # the official live-test row (pick, PASS or SKIP) exists; no preview
    for ko, wt in prev.get((home, away), []):
        if abs((ko - kickoff).total_seconds()) <= 12 * 3600:
            return wt
    return None


KL_DB = SCRIPT_DIR.parent / "KEY_LADDER_LIVE_V1.sqlite3"
TIER_RANK = {"HIGH": 0, "MEDIUM": 1, "LEAN": 2}


def load_ladder():
    """{(home_code, away_code): [(kickoff, block, official, correct)]}. Never fails the export."""
    out = {}
    if not KL_DB.exists():
        return out
    try:
        c = sqlite3.connect(KL_DB)
        c.row_factory = sqlite3.Row
        by_ev = {}
        for r in c.execute("SELECT * FROM rungs"):
            by_ev.setdefault(r["event_id"], []).append(r)
    except sqlite3.Error as e:
        print(f"Key ladder DB not readable ({e}); exporting without it.")
        return out
    for eid, rs in by_ev.items():
        h, a = NAME2CODE.get(rs[0]["home"]), NAME2CODE.get(rs[0]["away"])
        if not h or not a:
            continue
        ko = datetime.fromisoformat(str(rs[0]["kickoff_utc"]).replace("Z", "+00:00"))
        ko = ko if ko.tzinfo else ko.replace(tzinfo=timezone.utc)

        def item(r):
            return {"team": NAME2CODE.get(r["team"], r["team"]), "point": num(r["point"]),
                    "odds": odds_int(r["price_american"]), "book": r["book"], "tier": r["tier"],
                    "ev": round(float(r["ev"]), 4), "ev_low": round(float(r["ev_low"]), 4),
                    "p_cover": round(float(r["p_cover"]), 4), "p_low": round(float(r["p_low"]), 4),
                    "p_high": round(float(r["p_high"]), 4), "n": r["n"], "crosses": r["crosses"] or "",
                    "pin_agree": None if r["pin_agree"] is None else bool(r["pin_agree"]),
                    "official": bool(r["official"]), "result": r["result"]}
        ranked = sorted(rs, key=lambda r: (TIER_RANK.get(r["tier"], 9), -float(r["ev"])))
        off = next((r for r in rs if r["official"]), None)
        block = {"snapshot_utc": rs[0]["snapshot_utc"], "main_home_point": num(rs[0]["main_home_point"]),
                 "rungs": [item(r) for r in ranked[:4]], "test": "KEY_LADDER_LIVE_V1"}
        official = item(off) if off else None
        correct = None
        if off is not None and off["result"] in ("WIN", "LOSS"):
            correct = off["result"] == "WIN"
        out.setdefault((h, a), []).append((ko, block, official, correct))
    return out


def ladder_for(kl, home, away, kickoff):
    for ko, block, official, correct in kl.get((home, away), []):
        if abs((ko - kickoff).total_seconds()) <= 12 * 3600:
            return block, official, correct
    return None, None, None


def load_wx_totals():
    """{(home, away): [(kickoff, total_line)]} from every official weather-test row (any pick). Never fails."""
    out = {}
    if not WX_DB.exists():
        return out
    try:
        c = sqlite3.connect(WX_DB)
        for h, a, ko, tl in c.execute("SELECT home, away, kickoff_utc, total_line FROM games WHERE total_line IS NOT NULL"):
            h, a = NAME2CODE.get(h), NAME2CODE.get(a)
            if h and a:
                k = datetime.fromisoformat(str(ko).replace("Z", "+00:00"))
                out.setdefault((h, a), []).append((k if k.tzinfo else k.replace(tzinfo=timezone.utc), float(tl)))
    except sqlite3.Error:
        pass
    return out


def total_for(tot, home, away, kickoff):
    for ko, tl in tot.get((home, away), []):
        if abs((ko - kickoff).total_seconds()) <= 12 * 3600:
            return tl
    return None


def lowtotal_dog(point, home, away, market, total, preview):
    """LOWTOTAL_DOG_LIVE_V1: dog +3..+7.5 (inclusive) and total <= 41. point = home spread (neg = home favored)."""
    if point is None or total is None or point == 0:
        return None
    dog_home = point > 0
    pts = abs(point)
    if not (3 <= pts <= 7.5) or total > 41:
        return None
    return {"team": home if dog_home else away, "point": pts,
            "odds": market["spread_home_odds"] if dog_home else market["spread_away_odds"],
            "total_line": total, "preview": preview,
            "test": "PREVIEW (not counted)" if preview else "LOWTOTAL_DOG_LIVE_V1"}


def weather_for(wx, home, away, kickoff):
    for ko, wt, correct in wx.get((home, away), []):
        if abs((ko - kickoff).total_seconds()) <= 12 * 3600:
            return wt, correct
    return None, None
INJ = SCRIPT_DIR / "injury_report.json"   # written by injury_report_nfl_v1.py (information only)


def load_injuries():
    """(report or None, {game_id: previous injury_report}) -- the model never sees either."""
    report = None
    if INJ.exists():
        try:
            report = json.loads(INJ.read_text())
        except ValueError:
            report = None
    prev = {}
    if OUT.exists():
        try:
            for d in json.loads(OUT.read_text()).get("docs", []):
                if d.get("collection") == "slate" and d["data"].get("injury_report"):
                    prev[d["doc_id"]] = d["data"]["injury_report"]
        except (ValueError, KeyError):
            pass
    return report, prev


def injury_block(gid, home, away, kickoff, now_dt, report, prev):
    """Live report for games not yet started; after kickoff, freeze the last one shown."""
    if kickoff <= now_dt or report is None:
        return prev.get(gid)
    teams = report.get("teams", {})
    block = {"home": teams.get(home, []), "away": teams.get(away, []),
             "source": report.get("source"), "rule": report.get("rule"),
             "note": "Information only -- the pick was made by the frozen model and does not account for injuries."}
    old = prev.get(gid)
    same = old is not None and {k: v for k, v in old.items() if k != "as_of"} == block
    block["as_of"] = old.get("as_of") if same else report.get("generated_at")
    return block

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

    inj_report, inj_prev = load_injuries()
    wx = load_weather()
    wx_prev, wx_keys = load_preview(), load_weather_official_keys()
    kl = load_ladder()
    wx_tot = load_wx_totals()
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
        inj = injury_block(gid, home, away, kickoff, now_dt, inj_report, inj_prev)
        if inj is not None:
            slate["injury_report"] = inj
        decision = dict(base, locked_at=r["lock_timestamp_utc"],
                        decision={"action": "ML" if tier else "PASS", "prob": wp,
                                  "side": side if tier else None, "tier": tier},
                        market_snapshot=market, spread_read=spread_read, teaser_signal=teaser,
                        outcome=outcome, tracker_deciders=tracker)
        wt, wt_ok = weather_for(wx, home, away, kickoff)
        if wt is not None:
            slate["weather_total"] = wt
            decision["weather_total"] = wt
            outcome["weather_total_correct"] = wt_ok
        else:
            pv = preview_for(wx_prev, wx_keys, home, away, kickoff, now_dt)
            if pv is not None:
                slate["weather_total"] = pv  # slate only: previews are never counted
        tl = total_for(wx_tot, home, away, kickoff)
        ltd = lowtotal_dog(point, home, away, market, tl, False) if tl is not None else None
        if ltd is not None:
            slate["lowtotal_dog"] = ltd
            decision["lowtotal_dog"] = ltd
            if outcome["resolved"]:
                dm = (outcome["home_score"] - outcome["away_score"]) * (1 if ltd["team"] == home else -1)
                outcome["lowtotal_dog_correct"] = hit(dm + ltd["point"])
        elif tl is None:
            pv2 = preview_for(wx_prev, wx_keys, home, away, kickoff, now_dt)
            if pv2 is not None and pv2.get("line") is not None:
                ltp = lowtotal_dog(point, home, away, market, float(pv2["line"]), True)
                if ltp is not None:
                    slate["lowtotal_dog"] = ltp
        klb, klo, klc = ladder_for(kl, home, away, kickoff)
        if klb is not None:
            slate["key_ladder"] = klb
            if klo is not None:
                decision["key_ladder"] = klo
                outcome["key_ladder_correct"] = klc
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
