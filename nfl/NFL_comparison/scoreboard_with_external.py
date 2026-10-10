"""Add normalized external exports to the research scoreboard on IDENTICAL games (research evidence only).

Usage (from nfl_comparison/):
    python3 scoreboard_with_external.py external/results/*.normalized.json --out results/external_scoreboard.json

Rules
  * Only rows with record_type RESEARCH_OOS and a training cutoff before their evaluation season are used
    (import_external.normalize already enforces this).
  * Every model, the three challengers, the averages and the closing-market benchmark are scored on the SAME
    games: the intersection of all panels. Nothing is filled in for missing predictions.
  * Exports flagged lookahead_safe = false (e.g. the AS_IS variant) are reported in a separate
    'reference_not_ranked' block and never enter the ranked board.
  * Nothing here changes rule.json, the registered members, or any weights.
"""
import argparse, glob, json
from pathlib import Path
import pandas as pd
from comparison.report import score, paired_ci
from comparison.research import MEMBERS

BASE = MEMBERS + ["SIMPLE_AVERAGE", "WEIGHTED_ENSEMBLE", "MARKET_CLOSE_BENCHMARK"]


def load_external(paths):
    safe, ref = {}, {}
    for p in paths:
        recs = json.loads(Path(p).read_text())
        df = pd.DataFrame(recs)
        if df.empty:
            continue
        if set(df.record_type) != {"RESEARCH_OOS"}:
            raise SystemExit(f"{p}: only RESEARCH_OOS exports can enter the research board")
        flag = False  # External exports remain quarantined pending independent leakage validation.
        for mid, g in df.groupby("model_id"):
            s = g.drop_duplicates("game_id", keep="last").set_index("game_id")["p_home_win"].astype(float)
            (safe if flag else ref)[mid] = s
    return safe, ref


def board(panel, names):
    out = []
    for n in names:
        r = {"model": n, **score(panel.y, panel[n]),
             "paired_logloss_difference_vs_baseline_95ci": paired_ci(panel, n, "BASELINE_LR"),
             "by_season": {str(s): score(g.y, g[n]) for s, g in panel.groupby("season")}}
        out.append(r)
    return sorted(out, key=lambda r: r["log_loss"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("exports", nargs="+")
    ap.add_argument("--panel", default="results/comparison_predictions.csv")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    paths = sorted({p for x in a.exports for p in glob.glob(x)})
    wide = pd.read_csv(a.panel).set_index("game_id")
    safe, ref = load_external(paths)
    for mid, s in {**safe, **ref}.items():
        wide[mid] = s
    ranked = BASE + list(safe)
    common = wide.dropna(subset=ranked).copy()
    payload = {"status": "RESEARCH ONLY -- historical, previously examined seasons; not a registered evaluation",
               "sources": paths, "ranked_models": ranked, "common_games": int(len(common)),
               "seasons": sorted(int(s) for s in common.season.unique()) if len(common) else [],
               "scoreboard": board(common, ranked) if len(common) else [],
               "reference_not_ranked": {}}
    for mid in ref:
        c2 = wide.dropna(subset=BASE + [mid])
        payload["reference_not_ranked"][mid] = {
            "why": "external reproduction/leakage validation incomplete; reference only",
            "games": int(len(c2)), "model": score(c2.y, c2[mid]) if len(c2) else None,
            "baseline_same_games": score(c2.y, c2["BASELINE_LR"]) if len(c2) else None}
    Path(a.out).write_text(json.dumps(payload, indent=2, allow_nan=False))
    for r in payload["scoreboard"]:
        print(f"{r['model']:<28} n={r['games']:>5}  LL={r['log_loss']:.4f}  acc={r['accuracy']:.3f}")


if __name__ == "__main__":
    main()
