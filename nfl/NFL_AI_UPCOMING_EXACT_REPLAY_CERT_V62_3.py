#!/usr/bin/env python3
"""
NFL AI V62.3 — Correct upcoming Winner-63 assembly + historical replay
certification, SINGLE-NEAREST-WEEK SCOPED.

Why this supersedes V62.2:
V62.2 injected dummy 0-0 rows for EVERY remaining game in the season with
no score yet (i.e. every not-yet-played week at once) and ran the rolling
feature builder over that whole batch in one pass. Because the builder's
rolling/trailing features use shift(1) over the full schedule sequence,
V62.2 only guaranteed a game's OWN dummy result could not leak into its
OWN features -- it did not guard against a LATER unplayed week's features
being computed by looking back at an EARLIER unplayed week's dummy 0-0
placeholder as if it were real prior history. Confirmed by
MODEL_1_1_INPUT_EVIDENCE_AUDIT.py against the real 2026-09-18 batch:
240 of 256 rows carried prior_upcoming_dummy_games exposure.

V62.3 eliminates this structurally rather than patching around it: it
restricts the "upcoming" target set to ONLY the single nearest week that
has no completed games yet (by nflverse's own `week` column), not the
full remaining season. With that restriction, every row used as "prior
history" for the target week is guaranteed either (a) a genuinely
completed game with a real score, or (b) not included in the dummy
batch at all -- there is no other still-upcoming week in the batch for
a cascading dummy reference to come from.

Practical consequence: this script must be re-run once per week, close
to that week's own kickoff, after all prior weeks have completed. It
will refuse to run (die()) if the nearest upcoming week is not yet
fully determinable, or if it would still batch more than one week,
which should not happen given the restriction below but is checked
explicitly as a hard invariant.

Everything else (certified V53 builder invocation, 63-feature exact
match, historical Week-1 replay equality certification against the
frozen reference) is unchanged from V62.2. No prediction, training,
tuning, or model selection is performed. This script does not touch
the frozen Model 1.0/1.1 contracts, bundles, or the certified V53
builder itself -- it only changes which rows are selected for dummy
injection into that builder's input schedule.
"""
from pathlib import Path
import sys, json, importlib.util
import numpy as np
import pandas as pd

ROOT = Path.home() / "Desktop" / "Sport Bet"
MODEL = ROOT / "NFL_AI_MODEL_1_0"
BUILDER = MODEL / "live" / "winner_builder" / "live_winner_63_exact_v53.py"
REF = MODEL / "validation_2026" / "MODEL_1_0_2026_WINNER_63_FEATURE_MATRIX_CARRYFORWARD.csv"
OUTDIR = MODEL / "live" / "pregame"
OUTDIR.mkdir(parents=True, exist_ok=True)
OUT = OUTDIR / "MODEL_1_0_UPCOMING_WINNER_63_V62_3.csv"
REPORT = OUTDIR / "UPCOMING_PREGAME_63_REPLAY_CERTIFICATION_V62_3.json"

def die(msg):
    print("\nBLOCKED:", msg)
    sys.exit(1)

if not BUILDER.exists():
    die(f"Certified V53 builder missing: {BUILDER}")
if not REF.exists():
    die(f"Frozen V53 Week-1 reference missing: {REF}")

spec = importlib.util.spec_from_file_location("v53exact", BUILDER)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

cands = []
for name in dir(mod):
    obj = getattr(mod, name)
    if callable(obj) and getattr(obj, "__module__", None) == mod.__name__:
        try:
            import inspect
            sig = inspect.signature(obj)
            if len(sig.parameters) >= 4:
                cands.append((name, obj, sig))
        except Exception:
            pass
if not cands:
    die("Could not discover the certified builder callable.")
print("Certified builder callable candidates:", [x[0] for x in cands])

try:
    import nflreadpy as nfl
except Exception as e:
    die(f"nflreadpy unavailable: {e}")

def to_pd(x):
    if isinstance(x, pd.DataFrame): return x.copy()
    if hasattr(x, "to_pandas"): return x.to_pandas()
    return pd.DataFrame(x)

hist_years = list(range(2018, 2026))
print("Loading nflverse historical + current 2026 feeds...")
sh = to_pd(nfl.load_schedules(hist_years))
ph = to_pd(nfl.load_pbp(hist_years))
s26 = to_pd(nfl.load_schedules([2026]))
p26 = to_pd(nfl.load_pbp([2026]))

gid = "game_id"
for df, nm in [(sh,"historical schedules"),(ph,"historical PBP"),(s26,"2026 schedules"),(p26,"2026 PBP")]:
    if gid not in df.columns:
        die(f"{nm} missing game_id")

home_score = next((c for c in ["home_score","home_points"] if c in s26.columns), None)
away_score = next((c for c in ["away_score","away_points"] if c in s26.columns), None)
if not home_score or not away_score:
    die("Could not identify schedule score columns.")
if "week" not in s26.columns:
    die("2026 schedule missing week column; cannot restrict to single nearest week.")

ref = pd.read_csv(REF)
if "game_id" not in ref.columns:
    die("Reference matrix missing game_id.")
meta = {"game_id","season","week","gameday","game_date","home_team","away_team",
        "home_score","away_score","home_win"}
features = [c for c in ref.columns if c not in meta and (
    c.startswith("H_") or c.startswith("A_") or c.startswith("D_")
)]
if len(features) != 63:
    for key in ["F","FEATURES","FEATURE_COLS"]:
        v = getattr(mod, key, None)
        if isinstance(v, (list,tuple)) and len(v)==63:
            features=list(v); break
if len(features) != 63:
    die(f"Could not resolve exact 63 features; found {len(features)}")

ref_ids = set(ref.game_id.astype(str))
s26["_gidstr"] = s26.game_id.astype(str)
replay_ids = [x for x in s26["_gidstr"].tolist() if x in ref_ids]
if len(replay_ids) < 16:
    if len(replay_ids) != len(ref):
        die(f"Replay reference overlap incomplete: {len(replay_ids)}/{len(ref)}")

def invoke_builder(s_hist, p_hist, s_cur, p_cur):
    errors=[]
    for name, fn, sig in cands:
        attempts = [
            (s_hist,p_hist,s_cur,p_cur,features),
            (s_hist,s_cur,p_hist,p_cur,features),
            (s_hist,p_hist,s_cur,p_cur),
            (s_hist,s_cur,p_hist,p_cur),
        ]
        for args in attempts:
            try:
                z=fn(*args)
                if isinstance(z, tuple):
                    dfs=[x for x in z if isinstance(x,pd.DataFrame)]
                    if dfs: z=dfs[0]
                z=to_pd(z)
                if len(z) and "game_id" in z.columns:
                    return z, name
            except Exception as e:
                errors.append(f"{name}{sig}: {type(e).__name__}: {e}")
    die("Certified builder invocation failed.\n" + "\n".join(errors[-8:]))

# REPLAY CERTIFICATION: unchanged from V62.2.
sr = s26.drop(columns=["_gidstr"]).copy()
mask = sr.game_id.astype(str).isin(replay_ids)
sr.loc[mask, home_score] = 0
sr.loc[mask, away_score] = 0
pr = p26[~p26.game_id.astype(str).isin(replay_ids)].copy()

built_replay, callable_name = invoke_builder(sh, ph, sr, pr)
br = built_replay[built_replay.game_id.astype(str).isin(replay_ids)].copy()
rr = ref[ref.game_id.astype(str).isin(replay_ids)].copy()
br["__gid"]=br.game_id.astype(str); rr["__gid"]=rr.game_id.astype(str)
br=br.set_index("__gid"); rr=rr.set_index("__gid")
common=sorted(set(br.index)&set(rr.index))
if len(common)!=len(replay_ids):
    die(f"Certified builder returned only {len(common)}/{len(replay_ids)} replay targets.")
A=br.loc[common,features].apply(pd.to_numeric,errors="coerce").to_numpy(float)
B=rr.loc[common,features].apply(pd.to_numeric,errors="coerce").to_numpy(float)
finite = np.isfinite(A).all(axis=1) & np.isfinite(B).all(axis=1)
maxdiff = float(np.nanmax(np.abs(A-B)))
tol=1e-10
if not finite.all() or maxdiff > tol:
    die(f"Historical dummy-target replay FAILED. finite={finite.sum()}/{len(finite)} max_abs_diff={maxdiff}")

# CURRENT UPCOMING -- V62.3 FIX: restrict to the SINGLE NEAREST week with
# no completed games yet, not every remaining not-yet-scored game in the
# season. This is the entire structural fix for the cascading-dummy-
# history defect found by MODEL_1_1_INPUT_EVIDENCE_AUDIT.py.
sc = s26.drop(columns=["_gidstr"]).copy()
hs = pd.to_numeric(sc[home_score], errors="coerce")
aws = pd.to_numeric(sc[away_score], errors="coerce")
all_upmask = hs.isna() | aws.isna()
if not all_upmask.any():
    die("No upcoming 2026 games found in current nflverse schedule.")

nearest_week = int(sc.loc[all_upmask, "week"].min())
upmask = all_upmask & (sc["week"] == nearest_week)
up_ids = sc.loc[upmask, "game_id"].astype(str).tolist()
if not up_ids:
    die(f"No upcoming games found in nearest week {nearest_week}.")

# Hard invariant: every game in the injected dummy batch must belong to
# exactly the nearest upcoming week. If this ever fails, refuse to write
# output rather than silently reintroducing the cascading-dummy risk.
target_weeks = set(sc.loc[sc.game_id.astype(str).isin(up_ids), "week"].tolist())
if target_weeks != {nearest_week}:
    die(f"Invariant violated: upcoming batch spans weeks {sorted(target_weeks)}, expected only {{{nearest_week}}}.")

# Also confirm no OTHER not-yet-played week is present anywhere in the
# schedule handed to the builder as anything other than its natural,
# already-completed (real-score) state -- i.e. weeks beyond nearest_week
# still carry their real (missing) scores untouched, but since they are
# not in up_ids they are never read back out as target rows, and they are
# NOT zeroed, so they cannot masquerade as completed "prior" games either.
sc.loc[upmask, home_score] = 0
sc.loc[upmask, away_score] = 0
pc = p26[~p26.game_id.astype(str).isin(up_ids)].copy()

built_up, _ = invoke_builder(sh, ph, sc, pc)
up = built_up[built_up.game_id.astype(str).isin(up_ids)].copy()
if len(up)!=len(up_ids):
    die(f"Upcoming assembly returned {len(up)}/{len(up_ids)} target games.")
X=up[features].apply(pd.to_numeric,errors="coerce")
complete=np.isfinite(X.to_numpy(float)).all(axis=1)
if not complete.all():
    bad=up.loc[~complete,"game_id"].astype(str).tolist()[:10]
    die(f"Upcoming features incomplete: {complete.sum()}/{len(up)}. Examples: {bad}")

keep=[c for c in ["game_id","season","week","gameday","game_date","away_team","home_team"] if c in up.columns] + features
up[keep].to_csv(OUT,index=False)

report={
    "classification":"MODEL_1_0_UPCOMING_PREGAME_EXACT_REPLAY_CERTIFIED_V62_3_SINGLE_WEEK",
    "supersedes":"V62.2 (batched all remaining weeks; cascading-dummy-history risk)",
    "fix_applied":"Restricted upcoming target batch to the single nearest not-yet-completed week only.",
    "nearest_week_assembled":nearest_week,
    "certified_builder":str(BUILDER),
    "builder_callable":callable_name,
    "historical_replay_games":len(common),
    "features":len(features),
    "replay_complete_finite":int(finite.sum()),
    "replay_max_abs_difference":maxdiff,
    "equality_tolerance":tol,
    "upcoming_games":len(up),
    "upcoming_complete_finite":int(complete.sum()),
    "output":str(OUT),
    "prediction_executed":False,
    "training_tuning_model_selection_performed":False,
    "2026_use":"validation/live assembly only; not training, tuning, or model selection",
    "must_rerun_weekly":"This script must be re-run once per week, after all prior weeks are complete, to assemble the next week. It never batches more than one not-yet-played week at a time.",
}
REPORT.write_text(json.dumps(report,indent=2))

print("\nV62.3 COMPLETE (single-nearest-week scoped)")
print("Classification:",report["classification"])
print("Nearest week assembled:", nearest_week)
print("Historical dummy-target replay:",f'{len(common)}/{len(replay_ids)}')
print("Replay max abs difference:",maxdiff)
print("Equality tolerance:",tol)
print("Upcoming games assembled:",len(up))
print("Winner features: 63/63")
print("Complete finite:",f'{complete.sum()}/{len(up)}')
print("Prediction executed: False")
print("Training/tuning/model selection performed: False")
print("Upcoming matrix:",OUT)
print("Report:",REPORT)
print("\nNEXT: MATCH SAVED CURRENT ODDS -> FROZEN MODEL 1.0/1.1 -> MODEL_1_1_PROSPECTIVE_GUARDED.py --lock")
