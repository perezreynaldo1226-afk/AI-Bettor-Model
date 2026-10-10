#!/usr/bin/env python3
"""
nfl_comparison_v1.py -- connects the weekly NFL lock to the experimental comparison package
(nfl_comparison/). New file; it never modifies Model 1.0, its builder, the tracker DB, the
deciders or any app pick. Everything it writes is append-only.

  lock    run inside the lock workflow AFTER the official V1.5 lock and the injury report:
          1. copies this run's certified feature CSV, odds CSV and injury report into an immutable
             snapshot folder (nfl/comparison_snapshots/<season>_w<week>/<stamp>/) with SHA-256s;
          2. runs nfl_comparison/predict_features.py on that snapshot (three challengers, trained
             only on seasons before the prediction season);
          3. appends one prospective record per game x member (+ the frozen Model 1.0 probability
             from the tracker row of the same run, as a reference) with comparison.core.append_record,
             including provider quote time (odds snapshot_time) and local retrieval time;
          4. applies the frozen rule (rule.json) and appends a decision record per game.
  settle  appends a settlement event (final score) for every logged game the NFL grader has graded.
  export  writes nfl/comparison_lineboard_export.json for the app (collection comparison_live +
          comparison_report). Rows are recomputed from the append-only DB every time.

Nothing is bet. Decisions are hypothetical, for evaluating the registered experiment only.
"""
import argparse, hashlib, json, re, shutil, sqlite3, subprocess, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent              # repo/nfl
PKG = HERE.parent / "nfl_comparison"                 # repo/nfl_comparison
sys.path.insert(0, str(PKG))
from comparison.core import append_record, decide, devig, utc  # noqa: E402

DB = HERE / "NFL_COMPARISON_PROSPECTIVE.sqlite3"
SNAP_ROOT = HERE / "comparison_snapshots"
EXPORT = HERE / "comparison_lineboard_export.json"
TRACKER = HERE / "PROSPECTIVE_TRACKER_V1_1.sqlite3"
RULE = json.loads((PKG / "rule.json").read_text())
REG_PATH = PKG / "REGISTRATION_EXPERIMENTAL_V1.json"
REG = json.loads(REG_PATH.read_text())
REG_SHA = hashlib.sha256(REG_PATH.read_bytes()).hexdigest()
RULE_VERSION = f"{RULE['version']}+reg:{REG_SHA[:12]}"
TRAINING_CUTOFF_UTC = "2026-02-09T12:00:00Z"  # after the last 2025-season game (Super Bowl, 2026-02-08)
QB_SEVERE = re.compile(r"^(out|injured reserve|doubtful|physically unable|suspension|reserve|non-football)", re.I)


def now_utc():
    return datetime.now(timezone.utc)


def iso(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def conn():
    c = sqlite3.connect(DB)
    for t in ("decisions", "settlements", "lock_runs"):
        c.execute(f"CREATE TABLE IF NOT EXISTS {t} (digest TEXT PRIMARY KEY, game_id TEXT, payload TEXT NOT NULL)")
        c.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} BEGIN SELECT RAISE(ABORT,'Append only'); END")
        c.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} BEGIN SELECT RAISE(ABORT,'Append only'); END")
    return c


def append(table, game_id, payload):
    body = json.dumps(payload, sort_keys=True, allow_nan=False)
    d = hashlib.sha256(body.encode()).hexdigest()
    with conn() as c:
        c.execute(f"INSERT INTO {table} VALUES (?,?,?)", (d, game_id, body))
    return d


def registration_evidence():
    """Require a matching registration blob in Git history before the deadline.

    Commit times are repository evidence, not an independently trusted timestamp.
    Missing/shallow history fails closed; CI fetches full history.
    """
    repo = HERE.parent
    try:
        for path, expected in ((REG_PATH, REG_SHA),
                               (PKG / "rule.json", REG["rule_sha256"]),
                               (PKG / "data/features.csv", REG["training_data_sha256"])):
            if sha(path) != expected:
                return {"verified": False, "reason": "registered file hash mismatch"}
        rel = REG_PATH.relative_to(repo).as_posix()
        history = subprocess.check_output(
            ["git", "log", "--format=%H %cI", "--", rel], cwd=repo, text=True)
        for line in reversed(history.splitlines()):
            commit, stamp = line.split(" ", 1)
            blob = subprocess.check_output(["git", "show", f"{commit}:{rel}"], cwd=repo)
            if hashlib.sha256(blob).hexdigest() == REG_SHA and utc(stamp) < utc(REG["start_deadline_utc"]):
                return {"verified": True, "commit": commit, "committed_utc": stamp,
                        "registration_sha256": REG_SHA}
        return {"verified": False, "reason": "no matching pre-deadline registration commit"}
    except (OSError, subprocess.CalledProcessError, ValueError):
        return {"verified": False, "reason": "registration Git history unavailable"}


def eligibility(home, away, injuries_path, deadline):
    p = Path(injuries_path) if injuries_path else None
    if not p or not p.exists():
        return "UNKNOWN", "injury report missing"
    try:
        rep = json.loads(p.read_text())
        if not isinstance(rep, dict):
            raise ValueError("report must be an object")
    except (OSError, ValueError):
        return "UNKNOWN", "injury report unreadable"
    gen = rep.get("generated_at")
    try:
        if gen is None or not timedelta(0) <= deadline - utc(gen) <= timedelta(hours=24):
            return "UNKNOWN", f"injury report stale or undated (generated_at={gen})"
    except (ValueError, TypeError, AttributeError):
        return "UNKNOWN", "injury report timestamp unreadable"
    teams = rep.get("teams", {})
    if not isinstance(teams, dict) or any(t not in teams or not isinstance(teams[t], list)
                                         for t in (home, away)):
        return "UNKNOWN", "injury report lacks explicit coverage of both teams"
    if any(not isinstance(x, dict) for t in (home, away) for x in teams[t]):
        return "UNKNOWN", "malformed injury entries"
    out = [f"{t} QB {x.get('name')} {x.get('status')}" for t in (home, away) for x in teams.get(t, [])
           if str(x.get("pos")).upper() == "QB" and QB_SEVERE.search(str(x.get("status", "")))]
    return ("INELIGIBLE_QB", "; ".join(out)) if out else ("ELIGIBLE", "no QB of either team out/doubtful in the key-player report")


def pick_price(rows):
    """Registered price selection: freshest provider quote among books with both sides; tie -> alphabetical."""
    ok = rows.dropna(subset=["home_moneyline", "away_moneyline"]).copy()
    if ok.empty:
        return None
    ok["_q"] = pd.to_datetime(ok["snapshot_time"], utc=True, errors="coerce")
    ok = ok.sort_values(["_q", "bookmaker"], ascending=[False, True], na_position="last")
    return ok.iloc[0].to_dict()


def cmd_lock(a):
    evidence = registration_evidence()
    t0 = now_utc()
    stamp = t0.strftime("%Y%m%dT%H%M%SZ")
    snap = SNAP_ROOT / f"{a.season}_w{a.week:02d}" / stamp
    if snap.exists():
        raise SystemExit(f"snapshot folder already exists: {snap}")
    snap.mkdir(parents=True)
    feats = snap / "features.csv"; shutil.copy2(a.features, feats)
    odds_p = snap / "odds.csv"; shutil.copy2(a.odds, odds_p)
    inj_p = None
    if a.injuries and Path(a.injuries).exists():
        inj_p = snap / "injury_report.json"; shutil.copy2(a.injuries, inj_p)
    manifest = {"created_utc": iso(t0), "season": a.season, "week": a.week,
                "files": {p.name: sha(p) for p in sorted(snap.iterdir())}}
    (snap / "SNAPSHOT_MANIFEST.json").write_text(json.dumps(manifest, indent=1))
    rel_feats = feats.relative_to(HERE.parent).as_posix()

    deadline = now_utc()
    out = snap / "challengers.json"
    subprocess.run([sys.executable, "predict_features.py", "--features", str(feats),
                    "--deadline-utc", iso(deadline), "--out", str(out)], cwd=PKG, check=True)
    ch = pd.DataFrame(json.loads(out.read_text()))
    prediction_ts = now_utc()
    deadline = prediction_ts                      # registered: deadline = prediction time of this step
    odds = pd.read_csv(odds_p)
    F = pd.read_csv(feats)
    trk = {}
    if TRACKER.exists():
        tc = sqlite3.connect(TRACKER); tc.row_factory = sqlite3.Row
        for r in tc.execute("SELECT game_id, lock_timestamp_utc, winner_p_home FROM picks WHERE decider_version='OLD_LAB28_43_HYBRID' "
                            "AND season=? AND week=? ORDER BY lock_timestamp_utc", (a.season, a.week)):
            trk[r["game_id"]] = dict(r)                 # last lock per game wins
    import numpy, sklearn
    log = {"lock_stamp": stamp, "prediction_timestamp_utc": iso(prediction_ts), "rule_version": RULE_VERSION,
           "runtime": {"python": sys.version.split()[0], "pandas": pd.__version__, "numpy": numpy.__version__,
                       "scikit_learn": sklearn.__version__},
           "appended": 0, "skipped": [], "decisions": 0}
    for _, g in F.iterrows():
        gid = g.game_id
        orows = odds[odds.game_id == gid]
        if orows.empty:
            log["skipped"].append([gid, "no odds row"]); continue
        kickoff = utc(str(orows.kickoff_time.iloc[0]))
        if kickoff <= prediction_ts:
            log["skipped"].append([gid, "already kicked off"]); continue
        price = pick_price(orows)
        elig, why = eligibility(g.home_team, g.away_team, inj_p, deadline)
        quote = price["snapshot_time"] if price and isinstance(price.get("snapshot_time"), str) else None
        retrieved = price.get("retrieved_utc") if price and isinstance(price.get("retrieved_utc"), str) else None
        if quote and not retrieved:
            quote_for_record = None                  # cannot prove quote <= retrieval without a retrieval time
        else:
            quote_for_record = quote
        common = {"sport": "NFL", "market": "moneyline", "game_id": gid, "season": a.season, "week": a.week,
                  "home_team": g.home_team, "away_team": g.away_team, "kickoff_utc": iso(kickoff),
                  "deadline_utc": iso(deadline), "rule_version": RULE_VERSION, "registration_sha256": REG_SHA,
                  "eligibility": elig, "eligibility_reason": why,
                  "lock_id": stamp, "registration_evidence": evidence,
                  "feature_snapshot_ref": rel_feats, "feature_snapshot_sha256": manifest["files"]["features.csv"],
                  "odds_snapshot_sha256": manifest["files"]["odds.csv"],
                  "injury_snapshot_sha256": manifest["files"].get("injury_report.json"),
                  "bookmaker": price["bookmaker"] if price else None,
                  "home_odds": price["home_moneyline"] if price else None,
                  "away_odds": price["away_moneyline"] if price else None,
                  "odds_quote_timestamp_utc": quote_for_record, "odds_retrieval_timestamp_utc": retrieved}
        probs = {}
        for _, c in ch[ch.game_id == gid].iterrows():
            rec = dict(common, model_id=c.model_id, model_version=c.model_version, p_home_win=float(c.p_home_win),
                       prediction_timestamp_utc=iso(prediction_ts), training_cutoff_utc=TRAINING_CUTOFF_UTC,
                       training_max_season=int(c.training_max_season), data_status="LIVE_PREGAME")
            try:
                append_record(DB, rec); probs[c.model_id] = float(c.p_home_win); log["appended"] += 1
            except (ValueError, sqlite3.IntegrityError) as e:
                log["skipped"].append([gid, c.model_id, str(e)])
        if gid in trk:
            t = trk[gid]
            rec = dict(common, model_id="MODEL_1_0_FROZEN", model_version="NFL_AI_MODEL_1.0 (WINNER_EPA_CORE_LR_PRE2026)",
                       p_home_win=float(t["winner_p_home"]), prediction_timestamp_utc=iso(prediction_ts),
                       source_prediction_timestamp_utc=t["lock_timestamp_utc"],
                       training_cutoff_utc=TRAINING_CUTOFF_UTC, data_status="LIVE_PREGAME_REFERENCE_NOT_IN_RULE")
            try:
                append_record(DB, rec); log["appended"] += 1
            except (ValueError, sqlite3.IntegrityError) as e:
                log["skipped"].append([gid, "MODEL_1_0_FROZEN", str(e)])
        reference = None
        if gid in trk:
            reference = float(trk[gid]["winner_p_home"])
        members = [probs.get(m) for m in RULE["members"]]
        if elig != "ELIGIBLE":
            d = {"action": "PASS", "reason": f"Eligibility {elig}: {why}"}
        elif not price:
            d = {"action": "PASS", "reason": "No two-sided price"}
        else:
            d = decide(members, price["home_moneyline"], price["away_moneyline"], quote_for_record, iso(deadline),
                       RULE["min_ev"], RULE["max_age_minutes"])
        append("decisions", gid, dict(common, decision=d, member_probabilities=probs, reference_probability=reference,
                                      prediction_timestamp_utc=iso(prediction_ts),
                                      quote_age_minutes=((deadline - utc(quote_for_record)).total_seconds() / 60
                                                         if quote_for_record else None)))
        log["decisions"] += 1
    append("lock_runs", None, log)
    print(json.dumps(log, indent=1, default=str))
    cmd_export(a)


def cmd_settle(a):
    if not TRACKER.exists() or not DB.exists():
        print("nothing to settle"); return
    with sqlite3.connect(DB) as c:
        logged = {r[0] for r in c.execute("SELECT json_extract(payload,'$.game_id') FROM predictions")}
        settled = {r[0] for r in c.execute("SELECT game_id FROM settlements")}
    tc = sqlite3.connect(TRACKER)
    scores = {r[0]: (r[1], r[2]) for r in tc.execute(
        "SELECT p.game_id, g.actual_home_score, g.actual_away_score FROM picks p JOIN grades g ON g.pick_id=p.id "
        "WHERE g.actual_home_score IS NOT NULL")}
    n = 0
    for gid in sorted(logged - settled):
        if gid in scores:
            hs, as_ = scores[gid]
            append("settlements", gid, {"game_id": gid, "home_score": hs, "away_score": as_,
                                        "settled_at_utc": iso(now_utc()),
                                        "source": "NFL tracker grades (nflverse final score)"})
            n += 1
    print(f"settled {n} game(s)")
    cmd_export(a)


def latest_by_game(rows, ts_key="prediction_timestamp_utc"):
    out = {}
    for p in rows:
        k = p["game_id"]
        if k not in out or p[ts_key] >= out[k][ts_key]:
            out[k] = p
    return out


def cmd_export(a=None):
    preds, decs, sets = [], {}, {}
    if DB.exists():  # before the first lock there is no DB: export the report doc with an empty live log
        c = conn()
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='predictions'").fetchone():
            preds = [json.loads(r[0]) for r in c.execute("SELECT payload FROM predictions")]
        decs = latest_by_game([json.loads(r[0]) for r in c.execute("SELECT payload FROM decisions")])
        sets = {r[0]: json.loads(r[1]) for r in c.execute("SELECT game_id, payload FROM settlements")}
    by = {}
    for p in preds:  # last record per game x model before kickoff
        k = (p["game_id"], p["model_id"])
        if p["prediction_timestamp_utc"] < p["kickoff_utc"] and (k not in by or p["prediction_timestamp_utc"] >= by[k]["prediction_timestamp_utc"]):
            by[k] = p
    docs, tally = [], {"games": 0, "settled": 0, "ml_decisions": 0, "w": 0, "l": 0, "p": 0, "units": 0.0}
    for gid, d in sorted(decs.items(), key=lambda kv: kv[1]["kickoff_utc"]):
        mem = dict(d.get("member_probabilities", {}))
        if d.get("reference_probability") is not None:
            mem["MODEL_1_0_FROZEN"] = d["reference_probability"]
        avg = (sum(mem[m] for m in RULE["members"]) / 3) if all(m in mem for m in RULE["members"]) else None
        mk = None
        try:
            mk = devig(d["home_odds"], d["away_odds"]) if d.get("home_odds") is not None else None
        except (ValueError, TypeError):
            pass
        s = sets.get(gid); dec = d["decision"]; res, units = None, None
        counts = (utc(d["prediction_timestamp_utc"]) >= utc(REG["start_deadline_utc"])
                  and utc(d["prediction_timestamp_utc"]) < utc(d["kickoff_utc"])
                  and d.get("registration_evidence", {}).get("verified") is True
                  and d.get("registration_sha256") == REG_SHA)
        if counts:
            tally["games"] += 1
        if s:
            if counts:
                tally["settled"] += 1
            if dec.get("action") == "ML":
                hs, as_ = s["home_score"], s["away_score"]
                if hs == as_:
                    res, units = "PUSH", 0.0
                else:
                    won = (hs > as_) == (dec["side"] == "HOME")
                    o = float(d["home_odds"] if dec["side"] == "HOME" else d["away_odds"])
                    res, units = ("WIN", (o / 100 if o > 0 else 100 / -o)) if won else ("LOSS", -1.0)
                if counts:
                    tally["ml_decisions"] += 1; tally[res[0].lower()] += 1; tally["units"] += units
        data = {"sport": "NFL", "game_id": gid, "season": d["season"], "week": d["week"],
                "home_team": d["home_team"], "away_team": d["away_team"], "kickoff_time": d["kickoff_utc"],
                "status_note": "EXPERIMENTAL comparison -- not an app pick, no validated edge",
                "counts_for_registered_evaluation": counts,
                "registration_evidence": d.get("registration_evidence"), "lock_id": d.get("lock_id"),
                "rule_version": d["rule_version"], "prediction_timestamp_utc": d["prediction_timestamp_utc"],
                "eligibility": d["eligibility"], "eligibility_reason": d["eligibility_reason"],
                "members": mem, "simple_average": avg, "market_p_home_devig": mk,
                "price": {"book": d.get("bookmaker"), "home": d.get("home_odds"), "away": d.get("away_odds"),
                          "quote_utc": d.get("odds_quote_timestamp_utc"), "retrieved_utc": d.get("odds_retrieval_timestamp_utc"),
                          "quote_age_minutes": d.get("quote_age_minutes")},
                "decision": dec,
                "outcome": {"resolved": bool(s), "home_score": s["home_score"] if s else None,
                            "away_score": s["away_score"] if s else None, "decision_result": res, "units": units}}
        data["export_hash"] = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        docs.append({"collection": "comparison_live", "doc_id": gid, "data": data})
    rep = json.loads((PKG / "results" / "report.json").read_text())
    reg = json.loads((PKG / "registry.json").read_text())
    rpt = {"doc": "EXPERIMENTAL_V1", "status": rep["status"], "rule_version": RULE_VERSION,
           "registration": {"start_deadline_utc": REG["start_deadline_utc"], "sha256": REG_SHA, "status": REG["status"]},
           "research": {"common_games": rep["common_games"], "first_season": rep["first_season"], "last_season": rep["last_season"],
                        "scoreboard": [{k: r[k] for k in ("model", "games", "accuracy", "brier", "log_loss",
                                                           "paired_logloss_difference_vs_baseline_95ci", "by_season")}
                                       for r in rep["scoreboard"]],
                        "limitations": rep["limitations"]},
           "registry": reg, "live_tally": tally}
    ext = PKG / "external" / "results" / "external_scoreboard.json"
    if ext.exists():  # research-only board incl. external exports (identical games; non-lookahead-safe never ranked)
        e = json.loads(ext.read_text())
        rpt["external_research"] = {k: e.get(k) for k in ("status", "common_games", "seasons", "ranked_models",
                                                         "scoreboard", "reference_not_ranked")}
    rpt["export_hash"] = hashlib.sha256(json.dumps(rpt, sort_keys=True).encode()).hexdigest()
    docs.append({"collection": "comparison_report", "doc_id": "EXPERIMENTAL_V1", "data": rpt})
    EXPORT.write_text(json.dumps({"sport": "NFL_COMPARISON", "generated_at": iso(now_utc()), "n_docs": len(docs), "docs": docs},
                                 indent=1, allow_nan=False))
    print(f"exported {len(docs)} docs -> {EXPORT}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    lk = sp.add_parser("lock")
    lk.add_argument("--features", required=True); lk.add_argument("--odds", required=True)
    lk.add_argument("--injuries"); lk.add_argument("--season", type=int, required=True); lk.add_argument("--week", type=int, required=True)
    sp.add_parser("settle"); sp.add_parser("export")
    a = ap.parse_args()
    {"lock": cmd_lock, "settle": cmd_settle, "export": cmd_export}[a.cmd](a)
